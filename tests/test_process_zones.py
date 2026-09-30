"""Tests for 2C: conservative node-zone matching, the coverage stop
condition, rolling-mean math, and crosswalk integrity."""

import hashlib
import sys
from pathlib import Path

import pandas as pd

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


def test_rolling_mean_is_mean_of_daily_means():
    df = pd.DataFrame({
        "node": ["N1"] * 4,
        "timestamp_utc": ["2026-09-01T00:00:00+00:00",
                          "2026-09-01T01:00:00+00:00",
                          "2026-09-02T00:00:00+00:00",
                          "2026-09-02T01:00:00+00:00"],
        "congestion_dollars_per_mwh": [10.0, 20.0, 30.0, 40.0],
        "lmp_dollars_per_mwh": [50.0, 60.0, 70.0, 80.0],
        "loss_dollars_per_mwh": [-5.0, -5.0, -5.0, -5.0],
    })
    tmp = Path("/tmp/test_lmp.csv")
    df.to_csv(tmp, index=False)
    out, last, first = pz.per_node_rolling_means(tmp, days=30)
    # daily means: 15, 35 -> rolling mean 25. Negatives kept.
    assert out.loc[0, "mean_congestion"] == 25.0
    assert out.loc[0, "mean_lmp"] == 65.0
    assert out.loc[0, "mean_loss"] == -5.0
    assert out.loc[0, "n_days"] == 2
    tmp.unlink()


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
    assert len(df) == 1055
    for col in ["node", "n_days", "mean_congestion", "mean_lmp", "mean_loss",
                "zone", "zone_method"]:
        assert col in df.columns
    assert (df["n_days"] > 0).all()
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


def test_review_sheet_is_not_findings():
    # Node-based review sheet (built by process_node_signal.py): rows are
    # unapproved until the owner fills owner_status.
    review = pd.read_csv(PROCESSED / "crosswalk_review_top20.csv")
    assert "owner_status" in review.columns
    valid = {"", "approved", "pending_owner_review", "rejected"}
    assert set(review["owner_status"].fillna("").str.strip()
               .str.lower()).issubset(valid)
    for col in ("node_names", "n_nodes", "method", "distance_km",
                "mean_congestion"):
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
