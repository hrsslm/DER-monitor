"""Backfill and accumulate IESO price / fuel-mix history.

For each feed (da_zonal, lfda, da_lmp, fuelmix):
  1. list the dated files in the IESO folder index,
  2. download any file not yet incorporated (tracked in manifest.json),
  3. parse to hourly rows keyed on the UTC interval start,
  4. merge into data/processed/<feed>_hourly.csv (dedupe keep-last, sorted).

DA LMP is node-level, so only a 35-day rolling window is kept (project
rule: never commit node-level history). The rest accumulate full history
so the rolling upstream retention does not lose data.

Timestamps: IESO hours are hour-ending in Toronto local time. est_fixed()
below attaches a FIXED -05:00 offset (documented in docs/DATA_SOURCES.md);
the stored join key is the UTC instant as ISO-8601.
"""

import csv
import json
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_data import (  # noqa: E402
    IESO_REPORTS_ROOT,
    USER_AGENT,
    HTTP_TIMEOUT,
    RETRYABLE,
    download,
    encode_url,
    load_manifest,
    save_manifest,
    warn,
)

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw" / "ieso"
PROCESSED = ROOT / "data" / "processed"

# Fixed Eastern Standard Time offset. IESO labels hours 1-24 for the hour
# *ending* at that Toronto-local hour; we attach a fixed -05:00 offset so
# the mapping from label to UTC instant is deterministic on every day of
# the year, including the DST transition days (2026-03-08 spring forward,
# 2026-11-01 fall back), where a named timezone would need a fold
# decision. The stored join key is the true UTC instant. Downstream
# wall-clock use (tariffs, solar-peak gate) converts UTC to
# America/Toronto -- never the stamp's fields, which read an hour behind
# true local time during EDT (see docs/DATA_SOURCES.md).
EST_FIXED = timezone(timedelta(hours=-5), name="EST")
UTC = timezone.utc


def est_fixed(date_str, hour_ending):
    """IESO hour-ending -> interval start as an aware datetime at -05:00.

    date_str: "YYYY-MM-DD". hour_ending: 1-24 (25 on fall-back day =
    next-day midnight). Interval start is hour_ending - 1.
    """
    year, month, day = (int(p) for p in date_str.split("-"))
    he = int(hour_ending)
    if not 1 <= he <= 25:
        raise ValueError(f"hour_ending out of range: {hour_ending!r}")
    start = datetime(year, month, day) + timedelta(hours=he - 1)
    return start.replace(tzinfo=EST_FIXED)


def utc_key(dt):
    """Join key: the UTC instant as ISO-8601 with explicit +00:00."""
    return dt.astimezone(UTC).isoformat()


# ---------------------------------------------------------------------------
# Index listing
# ---------------------------------------------------------------------------

def list_index(folder, attempts=4):
    """Return [(filename, mtime, size)] for a reports folder, or [].

    Fetches via download() so large indexes survive the server's habit of
    hanging up mid-response (Range resume). Returns [] only when the
    index cannot be fetched at all; the manifest keeps last-good data.
    """
    import tempfile
    url = encode_url(f"{IESO_REPORTS_ROOT}/{folder}/?C=M;O=D")
    tmp = Path(tempfile.gettempdir()) / f"der_index_{folder}.html"
    ok = False
    for attempt in range(1, attempts + 1):
        if download(url, tmp):
            ok = True
            break
        warn(f"index fetch failed for {folder} (attempt {attempt}/{attempts})")
    if not ok:
        return []
    html = tmp.read_text(encoding="utf-8", errors="replace")
    rows = []
    for m in re.finditer(
            r'href="([^"]+)"></a>\s*</td><td[^>]*>([^<]*)</td><td[^>]*>([^<]*)</td>',
            html):
        rows.append((m.group(1), m.group(2).strip(), m.group(3).strip()))
    if not rows:  # fall back to bare href list when the table parse misses
        for name in sorted(set(re.findall(r'href="([^"]+)"', html))):
            rows.append((name, "", ""))
    return rows


def pick_winners(names, prefix):
    """Map date-key -> winning filename.

    Dated files win by date; for one date the non-versioned (final) file
    wins over _vN revisions. Returns {date_key: filename}.
    """
    winners = {}
    for name in names:
        if not name.startswith(prefix):
            continue
        m = re.search(r"(\d{8}|\d{4})", name)
        if not m:
            continue  # global/rolling link, handled separately
        date = m.group(1)
        ver = re.search(r"_v(\d+)", name)
        key = (1 if not ver else 0, int(ver.group(1)) if ver else 0)
        if date not in winners or key > winners[date][0]:
            winners[date] = (key, name)
    return {d: n for d, (_, n) in winners.items()}


# ---------------------------------------------------------------------------
# Parsers (each returns a list of dict rows with a timestamp_utc key)
# ---------------------------------------------------------------------------

NS = {"ieso": "http://www.ieso.ca/schema"}


def parse_da_zonal(path):
    """Day-ahead hourly Ontario zonal price XML -> hourly rows."""
    root = ET.parse(path).getroot()
    day = root.find("ieso:DocBody/ieso:DeliveryDate", NS).text.strip()
    rows = []
    for comp in root.findall("ieso:DocBody/ieso:HourlyPriceComponents", NS):
        he = comp.findtext("ieso:PricingHour", namespaces=NS).strip()
        price = comp.findtext("ieso:ZonalPrice", namespaces=NS)
        if price is None or not price.strip():
            continue
        rows.append({
            "timestamp_utc": utc_key(est_fixed(day, he)),
            "da_ozp_dollars_per_mwh": float(price.strip()),
        })
    return rows


def parse_lfda(path):
    """Hourly LFDA CSV -> hourly rows. Units are $/MWh (column header).

    The 2025 yearly file spells the column "LFDA Rate ($/MWh)"; the 2026
    file and rolling file spell it "LFDC Rate ($/MWh)". Both are accepted.
    Rows with a blank rate (early OEMP era) are skipped and counted.
    """
    rows = []
    blank = 0
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        cols = reader.fieldnames or []
        rate_col = next((c for c in cols if "Rate ($/MWh)" in c), None)
        if rate_col is None:
            raise ValueError(f"no rate column in {path}: {cols}")
        for rec in reader:
            date = (rec.get("Date") or "").strip()
            hour = (rec.get("Hour") or "").strip()
            rate = (rec.get(rate_col) or "").strip()
            if not date or not hour:
                continue
            if not rate:
                blank += 1
                continue
            try:
                rows.append({
                    "timestamp_utc": utc_key(est_fixed(date, hour)),
                    "lfda_dollars_per_mwh": float(rate),
                    "status": (rec.get("Status") or "").strip(),
                })
            except ValueError:
                continue
    if blank:
        warn(f"{Path(path).name}: {blank} rows with blank rate skipped")
    return rows


def parse_da_lmp(path):
    """Day-ahead hourly nodal LMP CSV -> hourly per-node rows."""
    text = open(path, encoding="utf-8-sig").read().splitlines()
    # First line: "CREATED AT 2026/09/29 12:33:20 FOR 2026/09/30"
    m = re.search(r"FOR (\d{4})/(\d{2})/(\d{2})", text[0])
    if not m:
        raise ValueError(f"no delivery date in {path}")
    day = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    rows = []
    reader = csv.DictReader(
        (ln for ln in text[1:] if ln.strip() and not ln.startswith("CREATED")))
    for rec in reader:
        he = (rec.get("Delivery Hour") or "").strip()
        node = (rec.get("Pricing Location") or "").strip()
        if not he or not node:
            continue
        def num(k):
            v = (rec.get(k) or "").strip()
            return float(v) if v else None
        rows.append({
            "timestamp_utc": utc_key(est_fixed(day, he)),
            "node": node,
            "lmp_dollars_per_mwh": num("LMP"),
            "loss_dollars_per_mwh": num("Energy Loss Price"),
            "congestion_dollars_per_mwh": num("Energy Congestion Price"),
        })
    return rows


def parse_fuelmix(path):
    """GenOutputbyFuelHourly XML -> hourly rows (MW by fuel).

    Element names and the EnergyValue/Output path are reused from the
    iesometeo scraper; only the timestamp derivation differs (est_fixed).
    FuelTotal entries with no Output value (quality -1) are skipped, and
    CONTROL_ACTIONS pseudo-fuel rows are dropped (operator dispatch, not
    generation).
    """
    root = ET.parse(path).getroot()
    rows = []
    for day_el in root.findall(".//ieso:DailyData", NS):
        day = day_el.findtext("ieso:Day", namespaces=NS)
        if not day:
            continue
        day = day.strip()
        for hour_el in day_el.findall("ieso:HourlyData", NS):
            he = hour_el.findtext("ieso:Hour", namespaces=NS)
            if not he:
                continue
            record = {"timestamp_utc": utc_key(est_fixed(day, he.strip()))}
            for fuel_el in hour_el.findall("ieso:FuelTotal", NS):
                fuel = fuel_el.findtext("ieso:Fuel", namespaces=NS)
                out = fuel_el.findtext("ieso:EnergyValue/ieso:Output", namespaces=NS)
                if not fuel or out is None or not out.strip():
                    continue
                key = fuel.strip().lower().replace(" ", "_")
                if key == "control_actions":
                    continue
                record[key] = float(out.strip())
            rows.append(record)
    return rows


FEEDS = {
    # feed: (folder, prefix, parser, processed csv, key fields, rolling days)
    "da_zonal": ("DAHourlyOntarioZonalPrice", "PUB_DAHourlyOntarioZonalPrice_",
                 parse_da_zonal, "da_zonal_hourly.csv", ("timestamp_utc",),
                 None),
    "lfda": ("HourlyLFDA", "PUB_HourlyLFDA", parse_lfda,
             "lfda_hourly.csv", ("timestamp_utc",), None),
    "da_lmp": ("DAHourlyEnergyLMP", "PUB_DAHourlyEnergyLMP_", parse_da_lmp,
               "da_lmp_hourly.csv", ("timestamp_utc", "node"), 32),
    "fuelmix": ("GenOutputbyFuelHourly", "PUB_GenOutputbyFuelHourly",
                parse_fuelmix, "fuelmix_hourly.csv", ("timestamp_utc",),
                None),
}

# Extra rolling/global links (no date in the name) fetched in full each run;
# skipped when the bytes are unchanged since the last parse.
ROLLING = {
    "lfda": ("HourlyLFDA", "PUB_HourlyLFDA.csv"),
    "fuelmix": ("GenOutputbyFuelHourly", "PUB_GenOutputbyFuelHourly.xml"),
}


def file_needs_parse(manifest_files, name, mtime, size):
    """True when the file is new or its index mtime/size changed."""
    rec = manifest_files.get(name)
    return rec is None or rec.get("mtime") != mtime or rec.get("size") != size


def file_date_within(name, days):
    """True when the filename's 8-digit date is within the last `days`.

    Used by --backfill-days to force re-download/re-parse of recent
    files even when the manifest says unchanged (idempotent: merge is
    keep-last, so this only refreshes rows).
    """
    if not days:
        return False
    m = re.search(r"(\d{8})", name)
    if not m:
        return False
    day = datetime.strptime(m.group(1), "%Y%m%d").date()
    return (datetime.now(UTC).date() - day).days <= days


# Fetch failures recorded during a run. Default mode warns and keeps
# last-good data (exit 0); --strict exits nonzero so a scheduled workflow
# fails visibly instead of passing silently.
_FAILURES = []


def fail(message):
    warn(message)
    _FAILURES.append(message)


def merge_rows(existing_path, new_rows, key_fields, rolling_days=None):
    """Merge new rows into a processed CSV: dedupe keep-last, sorted.

    key_fields: tuple of column names forming the dedupe key
    (e.g. ("timestamp_utc",) or ("timestamp_utc", "node")).
    """
    old = []
    if existing_path.exists():
        with open(existing_path, newline="", encoding="utf-8") as fh:
            old = list(csv.DictReader(fh))

    def key_of(r):
        return tuple(r[k] for k in key_fields)

    by_key = {key_of(r): r for r in old}
    for r in new_rows:
        by_key[key_of(r)] = r  # keep-last: revisions overwrite
    rows = sorted(by_key.values(), key=key_of)
    if rolling_days and rows:
        cutoff = (datetime.now(UTC) - timedelta(days=rolling_days)).isoformat()
        rows = [r for r in rows if r[key_fields[0]] >= cutoff]
    if not rows:
        return 0
    cols = sorted({c for r in rows for c in r},
                  key=lambda c: (c not in key_fields, c))
    with open(existing_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    return len(rows)


def run_feed(feed, manifest, force_days=0):
    folder, prefix, parser, out_name, key_fields, rolling_days = FEEDS[feed]
    hist = manifest.setdefault("history", {}).setdefault(feed, {})
    files = hist.setdefault("files", {})
    entries = list_index(folder)
    if not entries:
        fail(f"history fetch: index unavailable for {folder}; "
             f"keeping last good")
        return
    names = [n for n, _, _ in entries]
    meta = {n: (mt, sz) for n, mt, sz in entries}

    winners = pick_winners(names, prefix)
    targets = list(winners.values())
    for rfeed, (rfolder, rname) in ROLLING.items():
        if rfeed == feed and rfolder == folder and rname in names:
            targets.append(rname)

    parsed, skipped = 0, 0
    new_rows = []
    import hashlib
    for name in sorted(set(targets)):
        mtime, size = meta.get(name, ("", ""))
        if (not file_needs_parse(files, name, mtime, size)
                and not file_date_within(name, force_days)):
            skipped += 1
            continue
        dest = RAW / f"hist_{feed}_{name}"
        url = encode_url(f"{IESO_REPORTS_ROOT}/{folder}/{name}")
        if not download(url, dest):
            fail(f"history fetch: download failed for {name}; keeping last good")
            continue
        try:
            rows = parser(dest)
        except Exception as exc:
            fail(f"history fetch: parse failed for {name}: {exc}")
            continue
        new_rows.extend(rows)
        sha = hashlib.sha256(dest.read_bytes()).hexdigest()
        files[name] = {"sha256": sha, "mtime": mtime, "size": size,
                       "rows": len(rows),
                       "parsed_at": datetime.now(UTC).isoformat()}
        parsed += 1
        print(f"  {feed}: {name}: {len(rows)} rows", flush=True)
    if new_rows:
        total = merge_rows(PROCESSED / out_name, new_rows, key_fields,
                           rolling_days)
        print(f"  {feed}: {len(new_rows)} new rows -> {total} total",
              flush=True)
    hist["updated_at"] = datetime.now(UTC).isoformat()
    print(f"{feed}: parsed {parsed}, skipped {skipped} unchanged", flush=True)


def main():
    import argparse
    ap = argparse.ArgumentParser(
        description="Backfill and accumulate IESO price / fuel-mix history.")
    ap.add_argument("--backfill-days", type=int, default=0,
                    help="force re-download/re-parse of files dated within "
                         "the last N days (idempotent; default 0 = only new "
                         "or changed files)")
    ap.add_argument("--strict", action="store_true",
                    help="exit nonzero when any fetch fails, so a scheduled "
                         "workflow fails visibly instead of passing silently "
                         "(last-good data is still kept)")
    args = ap.parse_args()
    RAW.mkdir(parents=True, exist_ok=True)
    PROCESSED.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest()
    for feed in FEEDS:
        run_feed(feed, manifest, force_days=args.backfill_days)
    save_manifest(manifest)
    print("price history update complete", flush=True)
    if args.strict and _FAILURES:
        print(f"STRICT: {len(_FAILURES)} fetch failure(s):", flush=True)
        for message in _FAILURES:
            print(f"  - {message}", flush=True)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
