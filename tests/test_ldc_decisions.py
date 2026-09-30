"""Tests for src/ldc_decisions.py: owner-reviewed reconciliation decisions."""
import csv
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import ldc_decisions

HEADER = ldc_decisions.REQUIRED_COLUMNS

GOOD_ROW = ["ED-2002-0530", "Orillia Power Distribution Corporation",
            "merge_into", "ED-2003-0043", "reason", "", "owner", "final"]
GOOD_EXCLUDE = ["ED-2001-0090", "Attawapiskat Power Corporation",
                "exclude_no_rrr", "", "reason", "", "owner", "final"]


def write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(HEADER)
        writer.writerows(rows)
    return path


def test_load_decisions_ok(tmp_path):
    path = write_csv(tmp_path / "d.csv", [GOOD_ROW, GOOD_EXCLUDE])
    rows = ldc_decisions.load_decisions(
        path, known_licences={"ED-2002-0530", "ED-2003-0043", "ED-2001-0090"})
    assert len(rows) == 2
    assert rows[0]["decision"] == "merge_into"


def test_load_decisions_missing_file_warns(tmp_path):
    with pytest.warns(UserWarning):
        rows = ldc_decisions.load_decisions(tmp_path / "nope.csv")
    assert rows == []


@pytest.mark.parametrize("mutate", [
    lambda r: r[1:],                      # missing column
    lambda r: r[:7] + ["bogus"],          # bad decision
    lambda r: r[:2] + ["bogus"] + r[3:],  # bad decision (alt position)
    lambda r: r[:7] + ["draft"],         # bad status
    lambda r: ["ED-9999-9999"] + r[1:],   # unknown licence
    lambda r: r[:3] + [""] + r[4:],       # merge_into without target
    lambda r: r[:3] + ["ED-2003-0043"] + r[4:] if r[2] == "exclude_no_rrr" else r,
])
def test_load_decisions_malformed_fails(tmp_path, mutate):
    rows = [GOOD_ROW, GOOD_EXCLUDE]
    bad = [mutate(list(r)) for r in rows]
    path = tmp_path / "d.csv"
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(HEADER)
        writer.writerows(bad)
    with pytest.raises(ValueError, match="malformed"):
        ldc_decisions.load_decisions(
            path, known_licences={"ED-2002-0530", "ED-2003-0043",
                                  "ED-2001-0090"})


def test_real_decisions_file_loads():
    """The committed owner file must validate against the polygon layer."""
    import geopandas as gpd
    root = Path(__file__).resolve().parent.parent
    gdf = gpd.read_file(root / "data" / "inputs" / "ldc_service_areas.geojson")
    rows = ldc_decisions.load_decisions(
        known_licences=set(gdf["licence_no"]))
    assert len(rows) == 9
    assert {r["status"] for r in rows} == {"final"}


def test_analysis_layer_feature_count():
    """Regression: 60 source licences - 2 merges (Orillia, Espanola) = 58."""
    import geopandas as gpd
    root = Path(__file__).resolve().parent.parent
    gdf = gpd.read_file(root / "data" / "inputs" / "ldc_service_areas.geojson")
    rows = ldc_decisions.load_decisions(
        known_licences=set(gdf["licence_no"]))
    out, _ = ldc_decisions.apply_decisions(gdf, rows)
    assert len(out) == 58
    assert "ED-2002-0530" not in set(out["licence_no"])
    assert "ED-2002-0502" not in set(out["licence_no"])
    assert "ED-2023-0202" in set(out["licence_no"])
    assert "ED-2023-0262" in set(out["licence_no"])


def test_relicensed_as_validation():
    """relicensed_as needs a new number; merge_into needs an existing one."""
    import geopandas as gpd
    from shapely.geometry import box
    gdf = gpd.GeoDataFrame(
        {"licence_no": ["ED-2002-0498"], "utility_name": ["CW Hydro"]},
        geometry=[box(0, 0, 1, 1)], crs="EPSG:4326")
    good = {"licence_no": "ED-2002-0498", "utility_name": "CW Hydro",
            "decision": "relicensed_as", "target_licence_no": "ED-2023-0202",
            "reason": "", "source_url": "", "decided_by": "owner",
            "status": "final"}
    out, _ = ldc_decisions.apply_decisions(gdf, [good])
    assert out.iloc[0]["licence_no"] == "ED-2023-0202"
    assert out.iloc[0]["legacy_licence_no"] == "ED-2002-0498"
    # target already in layer -> malformed (should be merge_into)
    bad = dict(good, target_licence_no="ED-2002-0498")
    import csv as _csv
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False,
                                     newline="") as h:
        w = _csv.writer(h)
        w.writerow(ldc_decisions.REQUIRED_COLUMNS)
        w.writerow([bad[k] for k in ldc_decisions.REQUIRED_COLUMNS])
        tmppath = h.name
    with pytest.raises(ValueError, match="malformed"):
        ldc_decisions.load_decisions(tmppath,
                                     known_licences={"ED-2002-0498"})


def test_apply_decisions_merge_and_exclude():
    import geopandas as gpd
    from shapely.geometry import box
    gdf = gpd.GeoDataFrame(
        {"licence_no": ["ED-2002-0530", "ED-2003-0043", "ED-2001-0090"],
         "utility_name": ["Orillia", "HONI", "Attawapiskat"]},
        geometry=[box(0, 0, 1, 1), box(10, 10, 12, 12), box(20, 20, 21, 21)],
        crs="EPSG:4326")
    out, qa = ldc_decisions.apply_decisions(gdf, [
        {"licence_no": "ED-2002-0530", "utility_name": "Orillia",
         "decision": "merge_into", "target_licence_no": "ED-2003-0043",
         "reason": "", "source_url": "", "decided_by": "owner",
         "status": "final"},
        {"licence_no": "ED-2001-0090", "utility_name": "Attawapiskat",
         "decision": "exclude_no_rrr", "target_licence_no": "",
         "reason": "", "source_url": "", "decided_by": "owner",
         "status": "final"},
    ])
    # 3 licences -> 2 features; Orillia gone, its area merged into HONI
    assert len(out) == 2
    assert "ED-2002-0530" not in set(out["licence_no"])
    honi = out[out["licence_no"] == "ED-2003-0043"].iloc[0]
    assert honi["merged_licences"] == "ED-2002-0530"
    assert honi.geometry.area == pytest.approx(1.0 + 4.0)
    # source GeoDataFrame untouched
    assert len(gdf) == 3
    # exclusion flagged, not dropped
    excl = out[out["licence_no"] == "ED-2001-0090"].iloc[0]
    assert excl["excluded"] == True
    assert excl["decision"] == "exclude_no_rrr"
    assert len(qa) == 2
