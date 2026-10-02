"""Tests for src/process_node_signal.py (Page 4 resolution directive).

Covers each directive assumption with small synthetic fixtures:
matched-hour premium is computed upstream (see test_process_zones.py);
here: node filtering, quarantine, heterogeneity flag, rule approval,
override precedence, and the >$0.50 aggregate-exclusion report.
"""
from pathlib import Path
import sys

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import Point, box

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from process_node_signal import (apply_approval, build_review_sheet,  # noqa: E402
                                 build_signal, classify_node_type,
                                 find_quarantined_nodes,
                                 load_owner_overrides, name_stem)


def _fixtures():
    # EPSG:3161 metres; to_crs(3161)->3161 is identity.
    layer = gpd.GeoDataFrame(
        {"licence_no": ["ED-A", "ED-B"],
         "utility_name": ["A LDC", "Far LDC"],
         "geometry": [box(0, 0, 10_000, 10_000),       # 10 km square
                      box(500_000, 500_000, 510_000, 510_000)]},
        crs="EPSG:3161")
    nodes = gpd.GeoDataFrame(
        {"Location": ["N1", "N2", "N3", "N4", "N5"],
         "Longitude": [0.0] * 5, "Latitude": [0.0] * 5,
         "node_type": ["load", "generator", "dra", "battery", "aggregate"],
         "geometry": [Point(5_000, 5_000),      # inside A
                      Point(15_000, 5_000),     # 5 km east of A
                      Point(5_000, 5_000),
                      Point(5_000, 5_000),
                      Point(5_000, 5_000)]},    # aggregate inside A
        crs="EPSG:3161")
    cong = pd.DataFrame({
        "node": ["N1", "N2", "N3", "N4", "N5"],
        "n_days": [30] * 5,
        "mean_premium": [1.0, 3.0, -1.0, 5.0, 9.0],
        "mean_congestion": [0.1, 0.3, -0.1, 0.5, 0.9],
        "mean_lmp": [50.0, 52.0, 48.0, 54.0, 58.0],
        "mean_loss": [0.5, 0.6, 0.4, 0.7, 0.8],
    })
    adoption = pd.DataFrame({
        "licence_no": ["ED-A", "ED-B"],
        "utility_name": ["A LDC", "Far LDC"],
        "customers": [100.0, 50.0], "reported": [True, True],
    })
    return layer, nodes, cong, adoption


def test_classify_node_type_excludes_bus():
    assert classify_node_type("WAWA-230:LMP") == "bus"
    assert classify_node_type("RICHVIEW-230.REFERENCE:LMP") == "bus"
    assert classify_node_type("SAULTRSVC-LT.BATT_LF") == "battery"
    assert classify_node_type("WEYERHAEUSER-LT.T1_LF_DRA") == "dra"
    assert classify_node_type("STURGEONFALL-LT.G2") == "generator"
    assert classify_node_type("NORTHBAYGS-LT.AG12") == "aggregate"
    assert classify_node_type("ABITIBISF-LT.APG_T1") == "aggregate"
    assert classify_node_type("TORONTO-LT.T1_LF") == "load"
    assert classify_node_type("NORTHBAY-LT.TT_LF") == "load"


def test_in_polygon_uses_load_nodes_only():
    layer, nodes, cong, _ = _fixtures()
    sig = build_signal(layer, nodes, cong, ("load",))
    a = sig[sig["licence_no"] == "ED-A"].iloc[0]
    assert a["method"] == "in_polygon"
    # N1 is the only load node inside; generator/dra/battery/aggregate
    # and bus nodes are all excluded by the directive.
    assert a["n_nodes"] == 1
    assert a["node_names"] == "N1"
    assert a["node_types"] == "load:1"
    assert a["mean_premium"] == 1.0
    assert a["distance_km"] == 0.0


def test_aggregates_change_signal_when_included():
    layer, nodes, cong, _ = _fixtures()
    load_only = build_signal(layer, nodes, cong, ("load",))
    with_agg = build_signal(layer, nodes, cong, ("load", "aggregate"))
    a = load_only[load_only["licence_no"] == "ED-A"].iloc[0]
    b = with_agg[with_agg["licence_no"] == "ED-A"].iloc[0]
    assert a["mean_premium"] == 1.0
    assert b["mean_premium"] == pytest.approx(5.0)  # (1+9)/2
    assert b["n_nodes"] == 2


def test_nearest_uses_load_nodes_only():
    layer, nodes, cong, _ = _fixtures()
    # polygon with no load node inside: N2 is 5 km away but a generator,
    # so the nearest *load* node is N1 at 15 km.
    small = gpd.GeoDataFrame(
        {"licence_no": ["ED-C"], "utility_name": ["Near LDC"],
         "geometry": [box(20_000, 0, 25_000, 5_000)]}, crs="EPSG:3161")
    sig = build_signal(small, nodes, cong, ("load",))
    c = sig.iloc[0]
    assert c["method"] == "nearest_within_30km"
    assert c["n_nodes"] == 1 and c["node_names"] == "N1"
    assert c["distance_km"] == 15.0
    # far polygon -> none
    sig2 = build_signal(layer[layer["licence_no"] == "ED-B"], nodes, cong,
                        ("load",))
    b = sig2.iloc[0]
    assert b["method"] == "none" and b["n_nodes"] == 0


def test_name_stem_groups_generating_stations():
    assert name_stem("NORTHBAY-LT.TT_LF") == name_stem("NORTHBAYGS-LT.AG12")
    assert name_stem("CUMBERLAND-LT.LFBQ") == name_stem("CUMBERLAND-LT.TT12_LF")
    # generating-station suffixes fold into the same stem by design
    assert name_stem("TORONTO-LT.T1_LF") == name_stem("TORONTOGS-LT.G1")
    assert name_stem("TORONTO-LT.T1_LF") != name_stem("OTTAWA-LT.T1_LF")


def test_quarantine_flags_far_siblings():
    # Two NORTHBAY nodes ~380 km apart: both are >25 km from their only
    # sibling, so both are quarantined (fail-safe direction).
    nodes = pd.DataFrame(
        {"Location": ["NORTHBAY-LT.TT_LF", "NORTHBAYGS-LT.AG12",
                      "TORONTO-LT.T1_LF", "TORONTO-LT.T2_LF"],
         "Longitude": [-84.32767, -79.46921, -79.38, -79.39],
         "Latitude": [46.55507, 46.38032, 43.65, 43.66]})
    q = find_quarantined_nodes(nodes)
    assert "NORTHBAY-LT.TT_LF" in q
    assert "NORTHBAYGS-LT.AG12" in q
    assert "TORONTO-LT.T1_LF" not in q
    assert "TORONTO-LT.T2_LF" not in q


def test_quarantine_explicit_northbay_without_sibling():
    # The explicit NORTHBAY finding is quarantined even with no sibling.
    nodes = pd.DataFrame(
        {"Location": ["NORTHBAY-LT.TT_LF", "TORONTO-LT.T1_LF"],
         "Longitude": [-84.32767, -79.38],
         "Latitude": [46.55507, 43.65]})
    assert "NORTHBAY-LT.TT_LF" in find_quarantined_nodes(nodes)


def test_heterogeneous_flag_std_and_span():
    layer = gpd.GeoDataFrame(
        {"licence_no": ["ED-S", "ED-W"],
         "utility_name": ["Std LDC", "Wide LDC"],
         "geometry": [box(0, 0, 10_000, 10_000),
                      box(300_000, 0, 500_000, 200_000)]},
        crs="EPSG:3161")
    # ED-S: two nodes with very different premia -> std > $1.00
    # ED-W: two nodes 150 km apart with similar premia -> span > 100 km
    nodes = gpd.GeoDataFrame(
        {"Location": ["S1", "S2", "W1", "W2"],
         "Longitude": [0.0] * 4, "Latitude": [0.0] * 4,
         "node_type": ["load"] * 4,
         "geometry": [Point(5_000, 5_000), Point(6_000, 5_000),
                      Point(305_000, 5_000), Point(455_000, 5_000)]},
        crs="EPSG:3161")
    cong = pd.DataFrame({
        "node": ["S1", "S2", "W1", "W2"],
        "n_days": [30] * 4,
        "mean_premium": [0.0, 5.0, 0.1, 0.2],
        "mean_congestion": [0.0] * 4,
        "mean_lmp": [50.0] * 4,
        "mean_loss": [0.0] * 4,
    })
    sig = build_signal(layer, nodes, cong, ("load",))
    s = sig[sig["licence_no"] == "ED-S"].iloc[0]
    w = sig[sig["licence_no"] == "ED-W"].iloc[0]
    assert s["heterogeneous"] and s["std_premium"] > 1.00
    assert w["heterogeneous"] and w["span_km"] > 100.0


def test_rule_approval():
    layer, nodes, cong, adoption = _fixtures()
    sig = build_signal(layer, nodes, cong, ("load",))
    out = apply_approval(sig, adoption, {})
    a = out[out["licence_no"] == "ED-A"].iloc[0]
    assert a["owner_status"] == "approved"
    assert a["owner_override"] == ""
    b = out[out["licence_no"] == "ED-B"].iloc[0]
    assert b["owner_status"] == "rejected"
    assert "no load node within 30 km" in b["status_reason"]
    assert "method=none" in b["status_reason"]


def test_rule_rejects_heterogeneous_and_unreported():
    layer = gpd.GeoDataFrame(
        {"licence_no": ["ED-H", "ED-U"],
         "utility_name": ["Het LDC", "Unreported LDC"],
         "geometry": [box(0, 0, 10_000, 10_000),
                      box(300_000, 300_000, 310_000, 310_000)]},
        crs="EPSG:3161")
    nodes = gpd.GeoDataFrame(
        {"Location": ["H1", "H2", "U1"],
         "Longitude": [0.0] * 3, "Latitude": [0.0] * 3,
         "node_type": ["load"] * 3,
         "geometry": [Point(5_000, 5_000), Point(6_000, 5_000),
                      Point(305_000, 305_000)]},
        crs="EPSG:3161")
    cong = pd.DataFrame({
        "node": ["H1", "H2", "U1"], "n_days": [30] * 3,
        "mean_premium": [0.0, 5.0, 1.0],
        "mean_congestion": [0.0] * 3, "mean_lmp": [50.0] * 3,
        "mean_loss": [0.0] * 3,
    })
    adoption = pd.DataFrame({
        "licence_no": ["ED-H", "ED-U"],
        "utility_name": ["Het LDC", "Unreported LDC"],
        "customers": [100.0, 50.0], "reported": [True, False],
    })
    sig = build_signal(layer, nodes, cong, ("load",))
    out = apply_approval(sig, adoption, {})
    h = out[out["licence_no"] == "ED-H"].iloc[0]
    assert h["owner_status"] == "rejected"
    assert "heterogeneous" in h["status_reason"]
    u = out[out["licence_no"] == "ED-U"].iloc[0]
    assert u["owner_status"] == "rejected"
    assert "no adoption data" in u["status_reason"]


def test_owner_override_wins_over_rule():
    layer, nodes, cong, adoption = _fixtures()
    sig = build_signal(layer, nodes, cong, ("load",))
    # ED-A would be approved by the rule; the override rejects it.
    # ED-B would be rejected by the rule; the override approves it.
    overrides = {"ED-A": ("rejected", "owner says no"),
                 "ED-B": ("approved", "owner says yes")}
    out = apply_approval(sig, adoption, overrides)
    a = out[out["licence_no"] == "ED-A"].iloc[0]
    assert a["owner_status"] == "rejected"
    assert a["owner_override"] == "rejected"
    assert "owner_override=rejected" in a["status_reason"]
    assert "rule would be approved" in a["status_reason"]
    b = out[out["licence_no"] == "ED-B"].iloc[0]
    assert b["owner_status"] == "approved"
    assert "rule would be rejected" in b["status_reason"]


def test_load_owner_overrides_validates_values(tmp_path, monkeypatch):
    import process_node_signal as pns
    good = tmp_path / "ldc_owner_overrides.csv"
    good.write_text("licence_no,utility_name,owner_override,reason\n"
                    "ED-A,A LDC,approved,ok\n")
    monkeypatch.setattr(pns, "INPUTS", tmp_path)
    assert load_owner_overrides() == {"ED-A": ("approved", "ok")}
    bad = tmp_path / "ldc_owner_overrides.csv"
    bad.write_text("licence_no,utility_name,owner_override,reason\n"
                   "ED-A,A LDC,maybe,ok\n")
    with pytest.raises(ValueError, match="must be approved/rejected"):
        load_owner_overrides()


def test_review_sheet_carries_approval_columns():
    layer, nodes, cong, adoption = _fixtures()
    sig = build_signal(layer, nodes, cong, ("load",))
    sig = apply_approval(sig, adoption, {})
    sheet = build_review_sheet(sig, adoption)
    assert list(sheet["licence_no"]) == ["ED-A", "ED-B"]
    for col in ("node_names", "node_types", "n_nodes", "method",
                "distance_km", "mean_premium", "heterogeneous",
                "owner_status", "owner_override", "status_reason"):
        assert col in sheet.columns
    assert sheet.iloc[0]["owner_status"] == "approved"
    assert sheet.iloc[1]["owner_status"] == "rejected"
