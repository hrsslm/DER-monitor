"""Tests for 2C: conservative node-zone matching, the coverage stop
condition, rolling-mean math, and crosswalk integrity."""

import hashlib
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import process_zones as pz

PROCESSED = pz.PROCESSED


def test_station_base_strips_longest_suffix_first():
    # "ARLEN MTS": MTS (municipal transformer station) must win over TS.
    assert pz.station_base("ARLEN MTS") == "ARLEN"
    assert pz.station_base("BRUCE A TS") == "BRUCEA"
    assert pz.station_base("ASHFIELD SS") == "ASHFIELD"


def test_node_base_extraction():
    assert pz.node_base("BRUCEA-LT.AG_T1") == "BRUCEA"
    assert pz.node_base("CAMBRIAN-LT.TOTAL_LOAD") == "CAMBRIAN"
    assert pz.node_base("ALANBRG28KV-LT_H1") == "ALANBRG28KV"


def test_match_zone_exact_and_longest_wins():
    base_zones = {"WALLACE": ("WALLACE DS", "ESSA"),
                  "WALLACEBURG": ("WALLACEBURG TS", "WEST"),
                  "ASHFIELD": ("ASHFIELD SS", "BRUCE")}
    # Longest base wins: WALLACEBURG is in WEST, not ESSA.
    assert pz.match_zone("WALLACEBURG-LT.TT_LF", base_zones)[0] == "WEST"
    assert pz.match_zone("ASHFIELD-LT.AG1", base_zones)[0] == "BRUCE"
    assert pz.match_zone("NOSUCHSTATION-LT.AG1", base_zones) == (None, None)


def test_match_zone_no_match_returns_none():
    base_zones = {"ASHFIELD": ("ASHFIELD SS", "BRUCE")}
    assert pz.match_zone("NOSUCHSTATION-LT.AG1", base_zones) == (None, None)
    # Short bases below MIN_BASE_LEN are ignored.
    assert pz.match_zone("LEE-LT.AG1", {"LEE": ("LEE DS", "EAST")}) == \
        (None, None)


def test_zone_table_derives_ten_zones():
    base_zones, zones, ambiguous = pz.load_zone_table()
    assert len(zones) == 10
    assert "TORONTO" in zones and "NORTHWEST" in zones
    # CUMBERLAND stations sit in two different zones: excluded as ambiguous.
    assert "CUMBERLAND" in ambiguous
    assert "CUMBERLAND" not in base_zones


def _daily_and_zonal(tmp_path):
    """Small compact-daily fixture + matching zonal OZP file.

    NODE-KEEP: 15 days; premium 10.0 on day 1 (2 matched hours), 0.0 on
    the other 14 days (20 matched hours each) -> hour-weighted mean
    20/282, NOT the mean of daily means (10/15).
    NODE-DROP: only 14 distinct days in the window -> dropped.
    """
    daily = tmp_path / "lmp_node_daily.csv"
    rows = [{"node_id": "NODE-KEEP", "date": "2026-09-01",
             "mean_lmp_minus_ozp": 10.0, "mean_loss": 0.5,
             "mean_congestion": 0.1, "n_hours": 2}]
    for d in range(2, 16):
        rows.append({"node_id": "NODE-KEEP", "date": f"2026-09-{d:02d}",
                     "mean_lmp_minus_ozp": 0.0, "mean_loss": 0.5,
                     "mean_congestion": 0.1, "n_hours": 20})
    for d in range(1, 15):
        rows.append({"node_id": "NODE-DROP", "date": f"2026-09-{d:02d}",
                     "mean_lmp_minus_ozp": 1.0, "mean_loss": 0.1,
                     "mean_congestion": 0.1, "n_hours": 20})
    pd.DataFrame(rows).to_csv(daily, index=False)
    zonal = tmp_path / "da_zonal_hourly.csv"
    zrows = []
    # one extra UTC day so every Toronto date in the fixture has OZP
    # (00:00/01:00 UTC fall on the previous Toronto date)
    for d in range(1, 17):
        for h in range(2):
            zrows.append({"timestamp_utc": f"2026-09-{d:02d}T{h:02d}:00:00+00:00",
                          "da_ozp_dollars_per_mwh": 40.0})
    pd.DataFrame(zrows).to_csv(zonal, index=False)
    return daily, zonal


def test_rolling_mean_is_hour_weighted_mean_of_matched_diffs(tmp_path):
    daily, zonal = _daily_and_zonal(tmp_path)
    kept, dropped, last, first = pz.per_node_rolling_means(
        daily, days=30, zonal_path=zonal)
    keep = kept[kept["node"] == "NODE-KEEP"].iloc[0]
    # (10*2 + 0*14*20) / (2 + 14*20) = 20/282 -- hour-weighted, i.e. the
    # mean over matched hourly (LMP - OZP) diffs, not the mean of daily
    # means (10/15).
    assert keep["mean_premium"] == pytest.approx(20 / 282, abs=1e-6)
    assert keep["n_days"] == 15
    # mean_lmp is recovered as premium + daily mean OZP (40.0)
    assert keep["mean_lmp"] == pytest.approx(40.0 + 20 / 282, abs=1e-6)
    assert last == "2026-09-15" and first == "2026-08-17"


def test_fifteen_day_minimum_drops_short_nodes(tmp_path):
    daily, zonal = _daily_and_zonal(tmp_path)
    kept, dropped, _, _ = pz.per_node_rolling_means(
        daily, days=30, zonal_path=zonal)
    assert "NODE-KEEP" in set(kept["node"])
    assert "NODE-DROP" not in set(kept["node"])
    assert "NODE-DROP" in set(dropped["node"])
    assert dropped[dropped["node"] == "NODE-DROP"].iloc[0]["n_days"] == 14


def test_fifteen_day_boundary_is_inclusive(tmp_path):
    daily, zonal = _daily_and_zonal(tmp_path)
    rows = pd.read_csv(daily)
    rows = pd.concat([rows, pd.DataFrame([{
        "node_id": "NODE-EDGE", "date": "2026-09-15",
        "mean_lmp_minus_ozp": 1.0, "mean_loss": 0.1,
        "mean_congestion": 0.1, "n_hours": 20}])], ignore_index=True)
    for d in range(1, 15):
        rows = pd.concat([rows, pd.DataFrame([{
            "node_id": "NODE-EDGE", "date": f"2026-08-{d:02d}",
            "mean_lmp_minus_ozp": 1.0, "mean_loss": 0.1,
            "mean_congestion": 0.1, "n_hours": 20}])], ignore_index=True)
    rows.to_csv(daily, index=False)
    kept, dropped, _, _ = pz.per_node_rolling_means(
        daily, days=60, zonal_path=zonal)
    assert "NODE-EDGE" in set(kept["node"])  # exactly 15 days -> kept


def test_stop_condition_fired_no_zone_aggregation():
    cov = pd.read_csv(PROCESSED / "zone_coverage.csv")
    methods = dict(zip(cov["zone_method"], cov["n_nodes"]))
    assert methods["k3_inference"] == 0
    matched = methods["capacity_auction_table"]
    total = cov["n_nodes"].sum()
    assert matched / total < 0.90  # stop condition: no zone aggregation
    assign = pd.read_csv(PROCESSED / "node_zone_assignment.csv")
    assert len(assign) == 1055
    assert (assign["zone_method"] == "capacity_auction_table").sum() == matched


def test_node_rolling_output_shape_and_negatives():
    df = pd.read_csv(PROCESSED / "node_congestion_30d.csv")
    # 1,055 nodes in the compact store; one dropped by the 15-day rule.
    assert len(df) == 1054
    for col in ["node", "n_days", "mean_premium", "mean_congestion",
                "mean_lmp", "mean_loss", "zone", "zone_method"]:
        assert col in df.columns
    assert (df["n_days"] >= 15).all()
    # Negative congestion/loss values are kept, not clipped.
    assert (df["mean_congestion"] < 0).any() or \
        (df["mean_loss"] < 0).any()


def test_crosswalk_weights_sum_to_one_and_sanity_zones():
    cw = pd.read_csv(PROCESSED / "ldc_zone_crosswalk.csv")
    sums = cw.groupby("ldc_id")["weight"].sum()
    assert ((sums - 1.0).abs() < 1e-9).all()
    assert (cw["weight"] > 0).all()
    top = cw.sort_values("weight", ascending=False).drop_duplicates("ldc_id")
    top = top.set_index("ldc_id")["zone_name"]
    th = cw[cw["ldc_name"].str.contains("Toronto Hydro")]["ldc_id"].iloc[0]
    ho = cw[cw["ldc_name"].str.contains("Hydro Ottawa")]["ldc_id"].iloc[0]
    assert top[th] == "TORONTO"
    assert top[ho] == "OTTAWA"


def test_review_sheet_carries_rule_approval():
    # Node-based review sheet (built by process_node_signal.py): rows are
    # rule-approved or rule-rejected; ldc_owner_overrides.csv wins.
    review = pd.read_csv(PROCESSED / "crosswalk_review_top20.csv")
    assert "owner_status" in review.columns
    assert set(review["owner_status"].fillna("").str.strip()
               .str.lower()).issubset({"approved", "rejected"})
    assert (review["owner_status"] == "approved").any()
    assert (review["owner_status"] == "rejected").any()
    for col in ("node_names", "node_types", "n_nodes", "method",
                "distance_km", "mean_premium", "heterogeneous",
                "owner_override", "status_reason"):
        assert col in review.columns


def test_zones_deterministic():
    # crosswalk_review_top20.csv is built by process_node_signal.py, not
    # process_zones.py, so it is not in this determinism check.
    files = ["node_zone_assignment.csv", "zone_coverage.csv",
             "node_congestion_30d.csv", "ldc_zone_crosswalk.csv",
             "ieso_zone_table.csv"]
    before = {f: hashlib.sha256((PROCESSED / f).read_bytes()).hexdigest()
              for f in files}
    pz.main()
    for f in files:
        assert hashlib.sha256(
            (PROCESSED / f).read_bytes()).hexdigest() == before[f], f
