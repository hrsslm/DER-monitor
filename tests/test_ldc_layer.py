"""Tests for the LDC service-area layer: KMZ reproduction and landmarks."""
import sys
from pathlib import Path

import geopandas as gpd
import pytest
from shapely.geometry import Point

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from kmz_reproduce import reproduce, KMZ_PATH

ROOT = Path(__file__).resolve().parent.parent
INPUTS = ROOT / "data" / "inputs"
SIMPLIFIED = ROOT / "docs" / "data" / "ldc_service_areas_simplified.geojson"

# Directive 4.4: each point must fall in the named distributor's polygon.
# (lat, lon, expected match: substring of utility_name or licence_no)
LANDMARKS = [
    (43.6426, -79.3871, "Toronto Hydro-Electric System"),   # CN Tower
    (45.4236, -75.7009, "Hydro Ottawa"),                    # Parliament Hill
    (42.9849, -81.2453, "London Hydro"),                    # London City Hall
    (42.3149, -83.0364, "ENWIN Utilities"),                 # Downtown Windsor
    (46.4917, -80.9930, "Greater Sudbury Hydro"),           # Downtown Sudbury
    (48.3809, -89.2477, "Synergy North"),                   # Thunder Bay
    (44.2312, -76.4860, "Kingston Hydro"),                  # Kingston City Hall
    (43.5931, -79.6418, "Alectra Utilities"),               # Square One
    (43.4516, -80.4925, "Enova Power"),                     # Kitchener City Hall
    (43.3616, -80.3144, "ED-2021-0280"),                    # Cambridge (GrandBridge)
    (43.8971, -78.8658, "Oshawa PUC Networks"),             # Oshawa
    (43.0896, -79.0849, "Niagara Peninsula Energy"),        # Niagara Falls
    (43.5448, -80.2482, "Alectra Utilities"),               # Guelph
    (43.2557, -79.8711, "Alectra Utilities"),               # Hamilton City Hall
]


@pytest.fixture(scope="module")
def layer():
    return gpd.read_file(INPUTS / "ldc_service_areas.geojson")


def _containing(layer, lat, lon):
    point = Point(lon, lat)
    hits = layer[layer.geometry.contains(point)]
    return hits


def test_kmz_reproduction():
    """Directive 4.3.1 acceptance: 61 features, 60 licences, 7 repairs,
    per-licence area within 0.1% of the supplied GeoJSON."""
    if not KMZ_PATH.exists():
        pytest.skip("KMZ not downloaded yet; run src/fetch_data.py first")
    results = reproduce()
    assert results["n_features"] == 61
    assert results["n_licences"] == 60
    assert results["invalid_before"] == 7
    assert results["max_rel_area_diff"] <= 0.001


@pytest.mark.parametrize("lat,lon,expected", LANDMARKS)
def test_landmark_in_expected_distributor(layer, lat, lon, expected):
    hits = _containing(layer, lat, lon)
    assert len(hits) == 1, f"point ({lat}, {lon}) hit {len(hits)} polygons"
    row = hits.iloc[0]
    assert expected in row["utility_name"] or expected == row["licence_no"], \
        f"point ({lat}, {lon}) fell in {row['utility_name']} ({row['licence_no']})"


def test_layer_covers_all_licences_once(layer):
    assert len(layer) == 60
    assert layer["licence_no"].is_unique
    assert layer.geometry.is_valid.all()
    assert (layer.geometry.geom_type == "MultiPolygon").all()


def test_simplified_copy_is_light_and_rounded():
    if not SIMPLIFIED.exists():
        pytest.skip("simplified copy not built yet; run src/fetch_data.py first")
    simple = gpd.read_file(SIMPLIFIED)
    assert len(simple) == 58  # matches the post-merge analysis layer
    assert SIMPLIFIED.stat().st_size < 1_000_000
    # 4-decimal coordinate precision: re-rounding must not change anything.
    coords_ok = True
    for geom in simple.geometry:
        for part in (geom.geoms if geom.geom_type == "MultiPolygon" else [geom]):
            for ring in [part.exterior, *part.interiors]:
                for x, y in ring.coords:
                    if round(x, 4) != x or round(y, 4) != y:
                        coords_ok = False
    assert coords_ok, "simplified copy has coordinates beyond 4 decimals"
