"""Loaders and validators for the user-supplied input files.

Each loader reads one file from data/inputs/ and checks it against the
schema the pipeline expects. A malformed input raises ValueError with a
plain-language message — the pipeline fails loudly (nonzero exit) rather
than guessing, per the project's failure policy.
"""

import pandas as pd
import geopandas as gpd

# Columns every tariff row must have. start_hour/end_hour are integers;
# end_hour is exclusive (a block ending at 7 covers hours 0..6).
TARIFF_COLUMNS = [
    "effective_from", "effective_to", "plan", "season", "period",
    "day_type", "start_hour", "end_hour",
    "commodity_c_per_kwh", "delivery_variable_c_per_kwh", "source_url",
]


def load_tariffs(path):
    """Read retail_tariffs.csv and validate it. Returns a DataFrame.

    Validation (malformed-input policy: raise on any failure):
    - all expected columns present
    - every (term, plan, season, day_type) block covers exactly 24 hours
    - terms do not overlap
    """
    tariffs = pd.read_csv(path)
    missing = [c for c in TARIFF_COLUMNS if c not in tariffs.columns]
    if missing:
        raise ValueError(f"retail_tariffs.csv is missing columns: {missing}")

    # Every (term, plan, season, day_type) block must cover exactly 24 hours.
    blocks = tariffs.groupby(["effective_from", "plan", "season", "day_type"])
    for key, block in blocks:
        hours = (block["end_hour"] - block["start_hour"]).sum()
        if hours != 24:
            raise ValueError(
                f"retail_tariffs.csv: block {key} covers {hours} hours, not 24"
            )

    # Terms must not overlap.
    terms = (
        tariffs[["effective_from", "effective_to"]]
        .drop_duplicates()
        .sort_values("effective_from")
        .reset_index(drop=True)
    )
    for i in range(1, len(terms)):
        if terms.loc[i, "effective_from"] <= terms.loc[i - 1, "effective_to"]:
            raise ValueError(
                "retail_tariffs.csv: terms overlap: "
                f"{terms.loc[i-1, 'effective_from']}–{terms.loc[i-1, 'effective_to']} "
                f"and {terms.loc[i, 'effective_from']}–{terms.loc[i, 'effective_to']}"
            )

    return tariffs


def load_nodes(path):
    """Read the node-location CSV and map Location -> node_id.

    The owner's file uses columns Longitude,Latitude,Location (no zone_name).
    zone_name is derived later (node-to-zone mapping), never read from here.
    """
    nodes = pd.read_csv(path)
    for col in ["Longitude", "Latitude", "Location"]:
        if col not in nodes.columns:
            raise ValueError(f"node file is missing column: {col}")
    nodes = nodes.rename(columns={"Location": "node_id"})
    if nodes["node_id"].duplicated().any():
        dupes = nodes.loc[nodes["node_id"].duplicated(), "node_id"].unique()
        raise ValueError(f"node file has duplicate node_ids: {list(dupes)[:5]}")
    return nodes[["node_id", "Latitude", "Longitude"]]


def load_ldc_polygons(path):
    """Read the cleaned LDC service-area GeoJSON. Returns a GeoDataFrame."""
    polys = gpd.read_file(path)
    if len(polys) == 0:
        raise ValueError("ldc_service_areas.geojson has no features")
    if polys["licence_no"].duplicated().any():
        raise ValueError("ldc_service_areas.geojson has duplicate licence_no values")
    n_invalid = int((~polys.geometry.is_valid).sum())
    if n_invalid:
        raise ValueError(
            f"ldc_service_areas.geojson has {n_invalid} invalid geometries"
        )
    return polys


def unmatched_lmp_nodes(lmp_node_ids, nodes):
    """Return sorted LMP node ids that have no coordinates in the node file."""
    known = set(nodes["node_id"])
    return sorted(set(lmp_node_ids) - known)
