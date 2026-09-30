"""Re-derive the LDC service-area layer from the official OEB KMZ.

Phase 1 acceptance (directive 4.3.1): read doc.kml inside
data/inputs/Electric_260825.kmz (the attribute table sits in each placemark's
HTML description) and assert: 61 source features, 60 unique licences,
7 repaired geometries, and per-licence area within 0.1% of the supplied
GeoJSON (data/inputs/ldc_service_areas.geojson).

The KMZ is the untouched original extracted from the official OEB service-
area ZIP; the GeoJSON is the cleaned analysis copy. Every repaired geometry
is logged to the QA log with its licence number.
"""
import re
import sys
import zipfile
from pathlib import Path
import xml.etree.ElementTree as ET

import geopandas as gpd
from shapely.geometry import Polygon, MultiPolygon
from shapely.validation import make_valid

from qa_log import log_rows

ROOT = Path(__file__).resolve().parent.parent
INPUTS = ROOT / "data" / "inputs"
KMZ_PATH = INPUTS / "Electric_260825.kmz"
SUPPLIED = INPUTS / "ldc_service_areas.geojson"

KML_NS = {"k": "http://www.opengis.net/kml/2.2"}
TOLERANCE = 0.001  # 0.1% per-licence area agreement


def _field(description_html, label):
    """Pull a value out of the placemark's HTML attribute table.

    Rows look like: <td>OEB Licence No.</td> <td>ED-2002-0559</td>
    """
    pattern = re.compile(
        r"<td>\s*" + re.escape(label) + r"\s*</td>\s*<td>(.*?)</td>",
        re.DOTALL | re.IGNORECASE,
    )
    match = pattern.search(description_html or "")
    if not match:
        return ""
    value = re.sub(r"<[^>]+>", "", match.group(1))  # strip inner tags
    return value.strip()


def _ring(text):
    """Parse a KML coordinate string into a list of (lon, lat) tuples."""
    coords = []
    for token in text.split():
        parts = token.split(",")
        if len(parts) >= 2:
            coords.append((float(parts[0]), float(parts[1])))
    return coords


def _placemark_geometry(placemark):
    """Build a (Multi)Polygon from a placemark's MultiGeometry."""
    polys = []
    for poly in placemark.findall("k:MultiGeometry/k:Polygon", KML_NS):
        outer = poly.find("k:outerBoundaryIs/k:LinearRing/k:coordinates", KML_NS)
        if outer is None or not outer.text:
            continue
        holes = []
        for inner in poly.findall("k:innerBoundaryIs/k:LinearRing/k:coordinates",
                                  KML_NS):
            if inner.text:
                holes.append(_ring(inner.text))
        polys.append(Polygon(_ring(outer.text), holes))
    if not polys:
        return None
    return polys[0] if len(polys) == 1 else MultiPolygon(polys)


def parse_kmz(kmz_path=KMZ_PATH):
    """Read the KMZ into a GeoDataFrame (EPSG:4326), one row per placemark."""
    with zipfile.ZipFile(kmz_path) as archive:
        kml_names = [n for n in archive.namelist() if n.lower().endswith(".kml")]
        with archive.open(kml_names[0]) as handle:
            root = ET.parse(handle).getroot()
    rows = []
    for placemark in root.findall(".//k:Placemark", KML_NS):
        desc = placemark.findtext("k:description", namespaces=KML_NS)
        geom = _placemark_geometry(placemark)
        if geom is None:
            continue
        rows.append({
            "licence_no": _field(desc, "OEB Licence No."),
            "utility_name": _field(desc, "Utility Name"),
            "ldc_type": _field(desc, "LDC_Type"),
            "ccim_url": _field(desc, "CCIM_LDC_URL"),
            "geometry": geom,
        })
    frame = gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326")
    return frame


def _as_multipolygon(geom):
    """Coerce a repaired geometry to a (Multi)Polygon, dropping stray parts."""
    if geom.geom_type == "Polygon":
        return MultiPolygon([geom])
    if geom.geom_type == "MultiPolygon":
        return geom
    parts = [g for g in geom.geoms
             if g.geom_type in ("Polygon", "MultiPolygon")]
    polys = []
    for part in parts:
        polys.extend(part.geoms if part.geom_type == "MultiPolygon" else [part])
    return MultiPolygon(polys)


def reproduce(kmz_path=KMZ_PATH, supplied_path=SUPPLIED):
    """Run the reproduction check; returns a dict of measured quantities."""
    source = parse_kmz(kmz_path)
    n_features = len(source)
    assert (source["ldc_type"] == "Service Area").all(), \
        "expected every KMZ placemark to be a service area"

    n_licences = source["licence_no"].nunique()
    dupes = source[source.duplicated("licence_no", keep=False)]
    if not dupes.empty:
        log_rows([{
            "phase": "phase1", "dataset": "ldc_service_areas",
            "action": "duplicate_licence_in_kmz", "record_id": lic,
            "count": int((source["licence_no"] == lic).sum()),
            "note": "licence appears on more than one KMZ placemark; "
                    "geometries dissolved by licence",
        } for lic in dupes["licence_no"].unique()])

    invalid_before = int((~source.geometry.is_valid).sum())
    repaired = source.copy()
    repaired["geometry"] = repaired.geometry.apply(
        lambda g: _as_multipolygon(make_valid(g)) if not g.is_valid else g)
    assert repaired.geometry.is_valid.all(), "geometries still invalid after repair"
    if invalid_before:
        bad = source[~source.geometry.is_valid]
        log_rows([{
            "phase": "phase1", "dataset": "ldc_service_areas",
            "action": "geometry_repaired", "record_id": row["licence_no"],
            "count": 1,
            "note": f"KMZ geometry invalid; repaired with make_valid "
                    f"({row['utility_name']})",
        } for _, row in bad.iterrows()])

    dissolved = repaired.dissolve(by="licence_no", as_index=False)

    supplied = gpd.read_file(supplied_path)
    assert set(dissolved["licence_no"]) == set(supplied["licence_no"]), \
        "licence sets differ between KMZ reproduction and supplied GeoJSON"

    # Per-licence area comparison in EPSG:3161 (metres).
    left = dissolved[["licence_no", "geometry"]].to_crs("EPSG:3161")
    right = supplied[["licence_no", "geometry"]].to_crs("EPSG:3161")
    left["area_kmz"] = left.geometry.area
    right["area_supplied"] = right.geometry.area
    merged = left.merge(right, on="licence_no")
    merged["rel_diff"] = (merged["area_kmz"] - merged["area_supplied"]).abs() \
        / merged["area_supplied"]
    worst = merged.loc[merged["rel_diff"].idxmax()]

    return {
        "n_features": n_features,
        "n_licences": n_licences,
        "invalid_before": invalid_before,
        "max_rel_area_diff": float(merged["rel_diff"].max()),
        "worst_licence": worst["licence_no"],
    }


def main():
    results = reproduce()
    print(f"KMZ placemarks: {results['n_features']}")
    print(f"unique licences: {results['n_licences']}")
    print(f"invalid geometries repaired: {results['invalid_before']}")
    print(f"max per-licence area difference: "
          f"{results['max_rel_area_diff']:.6%} ({results['worst_licence']})")
    assert results["n_features"] == 61, "expected 61 KMZ placemarks"
    assert results["n_licences"] == 60, "expected 60 unique licences"
    assert results["invalid_before"] == 7, "expected 7 repaired geometries"
    assert results["max_rel_area_diff"] <= TOLERANCE, \
        "per-licence area differs by more than 0.1%"
    print("KMZ reproduction: PASS")


if __name__ == "__main__":
    sys.exit(main())
