"""Tests for src/process_node_signal.py with small synthetic fixtures."""
from pathlib import Path
import sys

import geopandas as gpd
import pandas as pd
from shapely.geometry import Point, box

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from process_node_signal import build_signal, build_review_sheet  # noqa: E402


def _fixtures():
    # EPSG:3161 metres; to_crs(3161)->3161 is identity.
    layer = gpd.GeoDataFrame(
        {"licence_no": ["ED-A", "ED-B", "ED-2003-0043"],
         "utility_name": ["A LDC", "Far LDC", "Hydro One Networks Inc."],
         "geometry": [box(0, 0, 10_000, 10_000),       # 10 km square
                      box(500_000, 500_000, 510_000, 510_000),
                      box(0, 0, 10_000, 10_000)]},      # same area as ED-A
        crs="EPSG:3161")
    nodes = gpd.GeoDataFrame(
        {"Location": ["N1", "N2", "N3"],
         "geometry": [Point(5_000, 5_000),      # inside A / Hydro One
                      Point(15_000, 5_000),     # 5 km east of A
                      Point(5_000, 5_000)]},    # same spot as N1
        crs="EPSG:3161")
    cong = pd.DataFrame({
        "node": ["N1", "N2", "N3"],
        "n_days": [30, 30, 30],
        "mean_congestion": [1.0, 3.0, -1.0],
        "mean_lmp": [50.0, 52.0, 48.0],
        "mean_loss": [0.5, 0.6, 0.4],
    })
    return layer, nodes, cong


def test_in_polygon_means_node_congestion():
    layer, nodes, cong = _fixtures()
    sig = build_signal(layer, nodes, cong)
    a = sig[sig["licence_no"] == "ED-A"].iloc[0]
    assert a["method"] == "in_polygon"
    assert a["n_nodes"] == 2  # N1 and N3 are inside; N2 is outside
    # N1 (1.0) and N3 (-1.0) are inside -> mean 0.0
    assert a["mean_congestion"] == 0.0
    assert a["distance_km"] == 0.0


def test_nearest_within_30km_and_none_beyond():
    layer, nodes, cong = _fixtures()
    # polygon with no node inside but one 5 km away -> nearest
    small = gpd.GeoDataFrame(
        {"licence_no": ["ED-C"], "utility_name": ["Near LDC"],
         "geometry": [box(20_000, 0, 25_000, 5_000)]}, crs="EPSG:3161")
    sig = build_signal(small, nodes, cong)
    c = sig.iloc[0]
    assert c["method"] == "nearest_within_30km"
    assert c["n_nodes"] == 1 and c["node_names"] == "N2"
    assert c["distance_km"] == 5.0
    # far polygon -> none
    sig2 = build_signal(layer[layer["licence_no"] == "ED-B"], nodes, cong)
    b = sig2.iloc[0]
    assert b["method"] == "none" and b["n_nodes"] == 0


def test_hydro_one_excluded_from_correlation():
    layer, nodes, cong = _fixtures()
    sig = build_signal(layer, nodes, cong)
    h = sig[sig["licence_no"] == "ED-2003-0043"].iloc[0]
    assert h["exclude_from_correlation"]
    a = sig[sig["licence_no"] == "ED-A"].iloc[0]
    assert not a["exclude_from_correlation"]


def test_review_sheet_top20_and_blank_status():
    layer, nodes, cong = _fixtures()
    sig = build_signal(layer, nodes, cong)
    adoption = pd.DataFrame({
        "licence_no": ["ED-A", "ED-B", "ED-2003-0043"],
        "utility_name": ["A LDC", "Far LDC", "Hydro One Networks Inc."],
        "customers": [100.0, 50.0, 1000.0], "reported": [True, True, True],
    })
    sheet = build_review_sheet(sig, adoption)
    assert list(sheet["licence_no"]) == ["ED-2003-0043", "ED-A", "ED-B"]
    assert (sheet["owner_status"] == "").all()
    for col in ("node_names", "n_nodes", "method", "distance_km",
                "mean_congestion"):
        assert col in sheet.columns
