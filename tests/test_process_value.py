"""Tests for the 2A preview pipeline (timestamps, LFDA parsing, tariffs)."""
import csv
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from fetch_price_history import est_fixed, parse_lfda, UTC  # noqa: E402
from process_value import (  # noqa: E402
    classify, load_tariffs, load_holidays, check_solar_peak,
    PREVIEW_LABEL, TORONTO,
)


def wall(year, month, day, hour):
    """True Toronto wall clock (America/Toronto)."""
    return datetime(year, month, day, hour, tzinfo=TORONTO)


# ---------------------------------------------------------------------------
# est_fixed: deterministic on DST transition days (explicit stop-condition)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("date", ["2026-03-08", "2026-11-01"])
def test_est_fixed_dst_days_deterministic(date):
    stamps = [est_fixed(date, he) for he in range(1, 25)]
    assert len({s.isoformat() for s in stamps}) == 24  # all distinct
    assert all(s.utcoffset() == timedelta(hours=-5) for s in stamps)
    # interval start is one hour before the hour-ending wall clock
    assert stamps[0].hour == 0 and stamps[13].hour == 13


def test_est_fixed_joins_in_utc():
    a = est_fixed("2026-07-15", 14).astimezone(UTC)
    assert a.isoformat() == "2026-07-15T18:00:00+00:00"


# ---------------------------------------------------------------------------
# LFDA parser: both historical column spellings, blank rates skipped
# ---------------------------------------------------------------------------

def _write_lfda(tmp_path, header, rows):
    p = tmp_path / "lfda.csv"
    with open(p, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)
    return p


def test_lfda_parses_both_column_spellings(tmp_path):
    for header in (["Date", "Hour", "Status", "LFDA Rate ($/MWh)"],
                   ["Date", "Hour", "Status", "LFDC Rate ($/MWh)"]):
        p = _write_lfda(tmp_path, header,
                        [["2026-07-20", "14", "Final", "1.25"]])
        rows = parse_lfda(p)
        assert len(rows) == 1
        assert rows[0]["lfda_dollars_per_mwh"] == 1.25
        assert rows[0]["status"] == "Final"
        # 2026-07-20 HE14 -> start 13:00 at -05:00 -> 18:00 UTC
        assert rows[0]["timestamp_utc"] == "2026-07-20T18:00:00+00:00"


def test_lfda_blank_rates_skipped(tmp_path):
    p = _write_lfda(tmp_path, ["Date", "Hour", "Status", "LFDA Rate ($/MWh)"],
                    [["2025-05-01", "1", "Final", ""],
                     ["2025-05-01", "2", "Final", "0.50"]])
    rows = parse_lfda(p)
    assert len(rows) == 1
    assert rows[0]["lfda_dollars_per_mwh"] == 0.50


# ---------------------------------------------------------------------------
# Tariff classification against the real OEB table
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def tariffs():
    return load_tariffs()


@pytest.fixture(scope="module")
def holidays():
    return load_holidays()


def test_tou_summer_weekday_on_peak(tariffs, holidays):
    # 2026-07-20 is a Monday; 13:00 Toronto wall clock, 2025-11-01 term
    cls = classify(wall(2026, 7, 20, 13), tariffs, holidays)
    assert cls["TOU"] == ("on_peak", 20.3)


def test_tou_summer_on_peak_starts_at_11_toronto(tariffs, holidays):
    # Summer on-peak is 11:00-17:00 Toronto local. Under the old EST_fixed
    # classification this hour read as 10:00 and misclassified as mid-peak.
    cls = classify(wall(2026, 7, 20, 11), tariffs, holidays)
    assert cls["TOU"] == ("on_peak", 20.3)
    cls = classify(wall(2026, 7, 20, 10), tariffs, holidays)
    assert cls["TOU"] == ("mid_peak", 15.7)


def test_tou_winter_weekday_morning_on_peak(tariffs, holidays):
    # 2026-01-15 is a Thursday; winter on-peak is 7-11 and 17-19
    cls = classify(wall(2026, 1, 15, 8), tariffs, holidays)
    assert cls["TOU"] == ("on_peak", 20.3)


def test_tou_weekend_off_peak(tariffs, holidays):
    # 2026-07-18 is a Saturday
    cls = classify(wall(2026, 7, 18, 13), tariffs, holidays)
    assert cls["TOU"] == ("off_peak", 9.8)


def test_statutory_holiday_off_peak(tariffs, holidays):
    # 2026-09-07 is Labour Day (Monday)
    assert "2026-09-07" in holidays
    cls = classify(wall(2026, 9, 7, 13), tariffs, holidays)
    assert cls["TOU"] == ("off_peak", 9.8)


def test_ulo_overnight(tariffs, holidays):
    cls = classify(wall(2026, 7, 20, 2), tariffs, holidays)
    assert cls["ULO"] == ("ultra_low_overnight", 3.9)


def test_tariff_term_boundary(tariffs, holidays):
    # 2025-10-31 (Friday): old term, summer on-peak 11-17 -> 15.8
    cls = classify(wall(2025, 10, 31, 13), tariffs, holidays)
    assert cls["TOU"] == ("on_peak", 15.8)
    # 2025-11-03 (Monday): new term, winter mid-peak 11-17 -> 15.7
    cls = classify(wall(2025, 11, 3, 13), tariffs, holidays)
    assert cls["TOU"] == ("mid_peak", 15.7)
    # winter on-peak still prices correctly in the new term
    cls = classify(wall(2025, 11, 3, 8), tariffs, holidays)
    assert cls["TOU"] == ("on_peak", 20.3)


# ---------------------------------------------------------------------------
# DST transition days: wall clock comes from America/Toronto, not the
# fixed -05:00 stamp (explicit stop-condition coverage)
# ---------------------------------------------------------------------------

def test_dst_spring_forward_2026_03_08(tariffs, holidays):
    # 2026-03-08 02:00 local does not exist; 07:00 UTC is 03:00 EDT.
    w = datetime(2026, 3, 8, 7, 0, tzinfo=timezone.utc).astimezone(TORONTO)
    assert (w.hour, w.utcoffset()) == (3, timedelta(hours=-4))
    # 2026-03-08 is a Sunday -> off-peak all day, no crash on the short day
    assert classify(w, tariffs, holidays)["TOU"][0] == "off_peak"


def test_dst_fall_back_2026_11_01(tariffs, holidays):
    # 2026-11-01 has two 01:00 hours: 05:00 UTC is 01:00 EDT, 06:00 UTC is
    # 01:00 EST. Both must convert without ambiguity.
    first = datetime(2026, 11, 1, 5, 0, tzinfo=timezone.utc).astimezone(TORONTO)
    second = datetime(2026, 11, 1, 6, 0, tzinfo=timezone.utc).astimezone(TORONTO)
    assert (first.hour, first.utcoffset()) == (1, timedelta(hours=-4))
    assert (second.hour, second.utcoffset()) == (1, timedelta(hours=-5))
    assert first.isoformat() != second.isoformat()
    # 2026-11-01 is past the last tariff term (ends 2026-10-31), so no
    # plan matches -- but classification must not crash on the 25-hour day.
    assert classify(first, tariffs, holidays) == {}
    assert classify(second, tariffs, holidays) == {}


# ---------------------------------------------------------------------------
# Solar-peak stop-condition gate (true Toronto local time via
# America/Toronto; expected band 12:00-14:00 for Jan and Jul)
# ---------------------------------------------------------------------------

def _solar_rows(month, peak_hour_toronto):
    """Synthetic fuel-mix rows whose Toronto-local solar peak is peak_hour."""
    rows = []
    for day in range(1, 29):
        for h in range(24):
            solar = 1000.0 if h == peak_hour_toronto else 10.0
            ts = datetime(2026, int(month), day, h,
                          tzinfo=TORONTO).astimezone(timezone.utc).isoformat()
            rows.append({"timestamp_utc": ts, "solar": solar})
    return rows


def test_solar_peak_passes_at_12_toronto():
    check_solar_peak(_solar_rows("01", 12) + _solar_rows("07", 12))


def test_solar_peak_fails_at_11_toronto():
    with pytest.raises(SystemExit):
        check_solar_peak(_solar_rows("01", 11) + _solar_rows("07", 12))


def test_solar_peak_real_data_passes_in_toronto():
    # Real fuel-mix history: Jan and Jul mean peaks at 12:00 Toronto.
    import process_value
    fuel = process_value.load_csv(
        Path(__file__).resolve().parent.parent / "data" / "processed"
        / "fuelmix_hourly.csv")
    check_solar_peak(fuel)  # raises SystemExit if outside 12-14


# ---------------------------------------------------------------------------
# Preview labelling contract
# ---------------------------------------------------------------------------

def test_preview_label_constant():
    assert PREVIEW_LABEL == "summer preview, not annual"


# ---------------------------------------------------------------------------
# merge_rows: composite dedupe key keeps every node at a timestamp
# ---------------------------------------------------------------------------

def test_merge_rows_composite_key(tmp_path):
    from fetch_price_history import merge_rows
    out = tmp_path / "lmp.csv"
    rows = [
        {"timestamp_utc": "2026-09-01T04:00:00+00:00", "node": "A",
         "lmp_dollars_per_mwh": 50.0},
        {"timestamp_utc": "2026-09-01T04:00:00+00:00", "node": "B",
         "lmp_dollars_per_mwh": 51.0},
        # a revision of node A keeps last
        {"timestamp_utc": "2026-09-01T04:00:00+00:00", "node": "A",
         "lmp_dollars_per_mwh": 52.0},
    ]
    total = merge_rows(out, rows, ("timestamp_utc", "node"))
    assert total == 2
    back = { (r["timestamp_utc"], r["node"]): r
             for r in csv.DictReader(open(out)) }
    assert back[("2026-09-01T04:00:00+00:00", "A")][
        "lmp_dollars_per_mwh"] == "52.0"
    assert back[("2026-09-01T04:00:00+00:00", "B")][
        "lmp_dollars_per_mwh"] == "51.0"
