"""2A preview: hourly avoided-cost value wedge on the available window.

Reads the accumulated history CSVs (da_zonal, lfda, fuelmix), joins them
in UTC, classifies each interval with the OEB TOU/ULO tariff table using
the true Toronto wall clock (America/Toronto), and writes:
  data/processed/value_hourly.csv
  data/processed/value_summary.json

PREVIEW, NOT ANNUAL: upstream retention for the day-ahead zonal price is
~2.4 months, so this covers only the available summer window. The outputs
are labelled "summer preview, not annual" and carry NO headline gap
metric (no solar-weighted gap %).

Timestamp convention: IESO source timestamps are fixed -05:00 (est_fixed
in fetch_price_history rederives the true UTC instant from them). Joins
use the UTC instant. Tariff season/day/hour classification converts UTC
to America/Toronto -- never the fixed -05:00 stamp's fields, which read
one hour behind true local time during EDT.
"""

import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

ROOT = Path(__file__).resolve().parent.parent
INPUTS = ROOT / "data" / "inputs"
PROCESSED = ROOT / "data" / "processed"
UTC = timezone.utc
TORONTO = ZoneInfo("America/Toronto")
PREVIEW_LABEL = "summer preview, not annual"

FUELS = ["nuclear", "gas", "hydro", "wind", "solar", "biofuel", "other"]


def load_csv(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def load_tariffs():
    tariffs = []
    for rec in load_csv(INPUTS / "retail_tariffs.csv"):
        tariffs.append({
            "from": rec["effective_from"], "to": rec["effective_to"],
            "plan": rec["plan"], "season": rec["season"],
            "period": rec["period"], "day_type": rec["day_type"],
            "start": int(rec["start_hour"]), "end": int(rec["end_hour"]),
            "rate": float(rec["commodity_c_per_kwh"]),
        })
    return tariffs


def load_holidays():
    days = set()
    for rec in load_csv(INPUTS / "statutory_holidays.csv"):
        days.add(rec["date"])
    return days


def classify(wall, tariffs, holidays):
    """True Toronto wall-clock datetime -> {plan: (period, rate_c_per_kwh)}.

    wall: datetime in America/Toronto. season: May-Oct summer, Nov-Apr
    winter.
    """
    season = "summer" if 5 <= wall.month <= 10 else "winter"
    day_type = ("weekend_holiday" if wall.weekday() >= 5
                or wall.strftime("%Y-%m-%d") in holidays else "weekday")
    date = wall.strftime("%Y-%m-%d")
    hour = wall.hour
    out = {}
    for t in tariffs:
        if not (t["from"] <= date <= t["to"]):
            continue
        if t["season"] != "all_year" and t["season"] != season:
            continue
        if t["day_type"] != day_type:
            continue
        if not (t["start"] <= hour < t["end"]):
            continue
        out[t["plan"]] = (t["period"], t["rate"])
    return out


def check_solar_peak(fuelmix_rows):
    """Stop-condition gate: Jan and Jul mean solar peaks at 12:00-14:00
    true Toronto local time.

    Converts each UTC instant to America/Toronto (not the fixed -05:00
    stamp, whose fields read an hour behind during EDT). Uses the full
    fuel-mix history (not just the preview window). Raises SystemExit on
    failure.
    """
    by_month_hour = {}
    for r in fuelmix_rows:
        wall = (datetime.fromisoformat(r["timestamp_utc"])
                .astimezone(TORONTO))
        key = (wall.strftime("%Y-%m"), wall.hour)
        by_month_hour.setdefault(key, []).append(float(r.get("solar") or 0))
    for month in ("2026-01", "2026-07"):
        hours = {h: sum(v) / len(v) for (m, h), v in by_month_hour.items()
                 if m == month}
        if not hours:
            print(f"STOP: no fuel-mix data for {month}; cannot run "
                  f"solar-peak check", flush=True)
            raise SystemExit(2)
        peak = max(hours, key=hours.get)
        print(f"solar peak check {month}: peak hour {peak}:00 Toronto "
              f"(mean {hours[peak]:.0f} MW)", flush=True)
        if not 12 <= peak <= 14:
            print(f"STOP: solar-peak timezone check failed for {month} "
                  f"(peak at {peak}:00 Toronto, expected 12:00-14:00)",
                  flush=True)
            raise SystemExit(2)


def main():
    PROCESSED.mkdir(parents=True, exist_ok=True)
    tariffs = load_tariffs()
    holidays = load_holidays()
    zonal = {r["timestamp_utc"]: r for r in
             load_csv(PROCESSED / "da_zonal_hourly.csv")}
    lfda = {r["timestamp_utc"]: r for r in
            load_csv(PROCESSED / "lfda_hourly.csv")}
    fuel = load_csv(PROCESSED / "fuelmix_hourly.csv")

    check_solar_peak(fuel)  # stop-condition gate before any metric

    common = sorted(set(zonal) & set(lfda))
    dropped = (len(set(zonal) | set(lfda)) - len(common))
    fuel_by_ts = {}
    for r in fuel:
        fuel_by_ts[r["timestamp_utc"]] = r

    rows = []
    for ts in common:
        z = float(zonal[ts]["da_ozp_dollars_per_mwh"])
        l = float(lfda[ts]["lfda_dollars_per_mwh"])
        oemp = z + l  # OEMP = DA-OZP + LFDA, by definition
        # True Toronto wall clock for tariff classification (never the
        # fixed -05:00 stamp's fields).
        wall = datetime.fromisoformat(ts).astimezone(TORONTO)
        cls = classify(wall, tariffs, holidays)
        f = fuel_by_ts.get(ts, {})
        total = sum(float(f.get(k) or 0) for k in FUELS)
        solar = float(f.get("solar") or 0)
        rec = {
            "timestamp_utc": ts,
            "da_ozp_dollars_per_mwh": round(z, 4),
            "lfda_dollars_per_mwh": round(l, 4),
            "oemp_dollars_per_mwh": round(oemp, 4),
            "solar_mw": round(solar, 1),
            "solar_share": round(solar / total, 6) if total else None,
            "season": "summer" if 5 <= wall.month <= 10 else "winter",
            "day_type": ("weekend_holiday" if wall.weekday() >= 5
                         or wall.strftime("%Y-%m-%d") in holidays
                         else "weekday"),
        }
        for plan in ("TOU", "ULO"):
            period, rate = cls.get(plan, (None, None))
            rec[f"{plan.lower()}_period"] = period
            # 1 cent/kWh = $10/MWh
            offset = rate * 10 if rate is not None else None
            rec[f"{plan.lower()}_offset_dollars_per_mwh"] = (
                round(offset, 2) if offset is not None else None)
            rec[f"{plan.lower()}_wedge_dollars_per_mwh"] = (
                round(offset - oemp, 4) if offset is not None else None)
        rows.append(rec)

    out_csv = PROCESSED / "value_hourly.csv"
    with open(out_csv, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    starts = [r["timestamp_utc"] for r in rows]
    wedges = [r["tou_wedge_dollars_per_mwh"] for r in rows
              if r["tou_wedge_dollars_per_mwh"] is not None]
    oemps = [r["oemp_dollars_per_mwh"] for r in rows]
    window_days = ((datetime.fromisoformat(max(starts))
                    - datetime.fromisoformat(min(starts))).days + 1)
    summary = {
        "label": PREVIEW_LABEL,
        "note": ("Preview on the available upstream window only "
                 "(day-ahead zonal retention 3.0 months: 2026-07-01 to "
                 "2026-09-30; LFDA ends 2026-09-16). Not annual. "
                 "No headline gap metric is computed for a preview."),
        "window_start": min(starts)[:10],
        "window_end": max(starts)[:10],
        "n_hours": len(rows),
        "expected_hours": window_days * 24,
        "coverage_pct": round(100 * len(rows) / (window_days * 24), 2),
        "join_dropped_timestamps": dropped,
        "as_of": datetime.now(UTC).strftime("%Y-%m-%d"),
        "tariff_terms": ["2024-11-01 to 2025-10-31",
                         "2025-11-01 to 2026-10-31"],
        "timestamp_convention": ("IESO source timestamps rederived to true "
                                 "UTC from fixed -05:00; joins in UTC; "
                                 "tariff classification on America/Toronto "
                                 "wall-clock fields"),
        "mean_oemp_dollars_per_mwh": round(sum(oemps) / len(oemps), 2),
        "mean_tou_wedge_dollars_per_mwh": round(sum(wedges) / len(wedges), 2),
        "analyst_commentary": "[ANALYST COMMENTARY - TO BE WRITTEN BY AUTHOR]",
    }
    with open(PROCESSED / "value_summary.json", "w",
              encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2)

    # qa log entry
    import qa_log
    qa_log.log_rows([{
        "phase": "2A", "dataset": "value_preview", "action": "run",
        "record_id": f"{min(starts)[:10]}..{max(starts)[:10]}",
        "count": len(rows),
        "note": (f"{dropped} timestamps dropped in join; "
                 f"label={PREVIEW_LABEL}; no headline gap metric"),
    }])
    print(f"value preview: {len(rows)} rows, "
          f"{min(starts)[:10]}..{max(starts)[:10]}", flush=True)


if __name__ == "__main__":
    main()
