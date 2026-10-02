"""Tests for the matched-hour premium calculation in
src/fetch_price_history.py::compact_lmp_daily (Page 4 resolution directive).

The premium must be the mean over *matched* hours of (LMP_h - OZP_h):
hours missing either LMP or OZP are excluded, never a difference of
separate means.
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import fetch_price_history as fph  # noqa: E402
import pandas as pd  # noqa: E402


def _frame(rows, ozp):
    return pd.DataFrame(fph.compact_lmp_daily(rows, ozp_map=ozp))


def _rows():
    return [
        {"node": "N1", "date": "2026-09-01",
         "timestamp_utc": "2026-09-01T00:00:00+00:00",
         "lmp_dollars_per_mwh": 50.0,
         "congestion_dollars_per_mwh": 2.0,
         "loss_dollars_per_mwh": 1.0},
        {"node": "N1", "date": "2026-09-01",
         "timestamp_utc": "2026-09-01T01:00:00+00:00",
         "lmp_dollars_per_mwh": 60.0,
         "congestion_dollars_per_mwh": 3.0,
         "loss_dollars_per_mwh": 2.0},
        {"node": "N2", "date": "2026-09-01",
         "timestamp_utc": "2026-09-01T00:00:00+00:00",
         "lmp_dollars_per_mwh": 70.0,
         "congestion_dollars_per_mwh": 4.0,
         "loss_dollars_per_mwh": 3.0},
    ]


def test_matched_hours_only_unmatched_ozp_excluded():
    # N1's second hour has no OZP: it must not enter the premium.
    ozp = {"2026-09-01T00:00:00+00:00": 40.0}
    out = _frame(_rows(), ozp)
    n1 = out[out["node_id"] == "N1"].iloc[0]
    # matched-hour premium: (50 - 40) / 1 = 10.0, not (55 - 40) = 15.0
    assert n1["mean_lmp_minus_ozp"] == 10.0
    assert n1["n_hours"] == 1
    assert n1["mean_loss"] == 1.0          # matched hour's components
    assert n1["mean_congestion"] == 2.0
    n2 = out[out["node_id"] == "N2"].iloc[0]
    assert n2["mean_lmp_minus_ozp"] == 30.0
    assert n2["n_hours"] == 1


def test_unmatched_lmp_hour_excluded():
    # hour with no LMP (None) is dropped even when OZP exists
    rows = [{"node": "N1", "date": "2026-09-01",
             "timestamp_utc": "2026-09-01T00:00:00+00:00",
             "lmp_dollars_per_mwh": None,
             "congestion_dollars_per_mwh": None,
             "loss_dollars_per_mwh": None},
            {"node": "N1", "date": "2026-09-01",
             "timestamp_utc": "2026-09-01T01:00:00+00:00",
             "lmp_dollars_per_mwh": 60.0,
             "congestion_dollars_per_mwh": 3.0,
             "loss_dollars_per_mwh": 2.0}]
    ozp = {"2026-09-01T00:00:00+00:00": 40.0,
           "2026-09-01T01:00:00+00:00": 50.0}
    out = _frame(rows, ozp)
    assert len(out) == 1
    assert out.iloc[0]["n_hours"] == 1
    assert out.iloc[0]["mean_lmp_minus_ozp"] == 10.0


def test_no_matched_hours_yields_no_row():
    ozp = {"2026-09-01T05:00:00+00:00": 40.0}  # nothing overlaps
    out = _frame(_rows(), ozp)
    assert len(out) == 0


def test_one_row_per_node_per_day():
    ozp = {"2026-09-01T00:00:00+00:00": 40.0,
           "2026-09-01T01:00:00+00:00": 50.0,
           "2026-09-02T00:00:00+00:00": 40.0}
    rows = _rows() + [
        {"node": "N1", "date": "2026-09-02",
         "timestamp_utc": "2026-09-02T00:00:00+00:00",
         "lmp_dollars_per_mwh": 80.0,
         "congestion_dollars_per_mwh": 5.0,
         "loss_dollars_per_mwh": 4.0}]
    out = _frame(rows, ozp)
    assert len(out) == 3  # N1 x2 days, N2 x1 day
    n1d1 = out[(out["node_id"] == "N1") & (out["date"] == "2026-09-01")].iloc[0]
    # both hours matched: mean of (10, 10)
    assert n1d1["mean_lmp_minus_ozp"] == 10.0
    assert n1d1["n_hours"] == 2
