"""LDC-level wholesale signal from LMP nodes (Page 4 input).

Zone aggregation is withheld (node-to-zone coverage 54.2% < 90% bar), so
the adoption-vs-signal chart needs an LDC-level signal computed directly
from nodes:

  * method "in_polygon": mean of the 30-day mean congestion of the LMP
    nodes whose coordinates fall inside the LDC polygon;
  * method "nearest_within_30km": no node inside -> the single nearest
    node within 30 km of the polygon boundary (distance in EPSG:3161);
  * method "none": no node inside and none within 30 km.

Writes data/processed/ldc_node_signal.csv (one row per LDC polygon) and
rebuilds data/processed/crosswalk_review_top20.csv (top 20 reported LDCs
by customers) with the node(s) used plus a blank owner_status column for
the owner to fill: approved / pending_owner_review / rejected.

Multi-zone or residual LDCs (Hydro One Networks) get a signal row but are
flagged exclude_from_correlation so the chart never uses them.

Inputs: data/processed/ldc_analysis_layer.geojson,
        data/inputs/ieso_node_locations.csv,
        data/processed/node_congestion_30d.csv,
        data/processed/ldc_adoption.csv
"""
import sys
from pathlib import Path

import geopandas as gpd
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from qa_log import log_rows  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PROCESSED = ROOT / "data" / "processed"
INPUTS = ROOT / "data" / "inputs"

NEAREST_KM = 30.0
TOP_N = 20
CORR_EXCLUDE_LICENCES = {"ED-2003-0043"}  # Hydro One Networks: multi-zone


def load_inputs():
    layer = gpd.read_file(PROCESSED / "ldc_analysis_layer.geojson")
    loc = pd.read_csv(INPUTS / "ieso_node_locations.csv")
    nodes = loc.dropna(subset=["Longitude", "Latitude"]).copy()
    points = gpd.GeoDataFrame(
        nodes, geometry=gpd.points_from_xy(nodes["Longitude"],
                                           nodes["Latitude"]),
        crs="EPSG:4326")
    cong = pd.read_csv(PROCESSED / "node_congestion_30d.csv")
    adoption = pd.read_csv(PROCESSED / "ldc_adoption.csv")
    return layer, points, cong, adoption


def build_signal(layer, node_points, cong):
    """One signal row per LDC polygon. Returns (signal_df, joined_inside)."""
    nodes = node_points.merge(cong, left_on="Location", right_on="node",
                              how="inner")
    inside = gpd.sjoin(nodes[["Location", "mean_congestion", "mean_lmp",
                               "mean_loss", "geometry"]],
                       layer[["licence_no", "utility_name", "geometry"]],
                       how="inner", predicate="intersects")
    agg = (inside.groupby(["licence_no", "utility_name"], as_index=False)
           .agg(n_nodes=("Location", "nunique"),
                node_names=("Location", lambda s: "; ".join(sorted(set(s)))),
                mean_congestion=("mean_congestion", "mean"),
                mean_lmp=("mean_lmp", "mean"),
                mean_loss=("mean_loss", "mean")))
    agg["method"] = "in_polygon"
    agg["distance_km"] = 0.0

    have = set(agg["licence_no"])
    missing = layer[~layer["licence_no"].isin(have)].copy()
    nearest_rows = []
    if len(missing):
        proj_layer = missing.to_crs("EPSG:3161")[["licence_no",
                                                  "utility_name",
                                                  "geometry"]]
        proj_nodes = nodes.to_crs("EPSG:3161")[["Location", "mean_congestion",
                                                "mean_lmp", "mean_loss",
                                                "geometry"]]
        sindex = proj_nodes.sindex
        for _, ldc in proj_layer.iterrows():
            cand_idx = list(sindex.nearest(ldc.geometry, 1)[1])
            if not cand_idx:
                continue
            node = proj_nodes.iloc[cand_idx[0]]
            dist_km = ldc.geometry.distance(node.geometry) / 1000.0
            if dist_km <= NEAREST_KM:
                nearest_rows.append({
                    "licence_no": ldc["licence_no"],
                    "utility_name": ldc["utility_name"],
                    "n_nodes": 1,
                    "node_names": node["Location"],
                    "mean_congestion": node["mean_congestion"],
                    "mean_lmp": node["mean_lmp"],
                    "mean_loss": node["mean_loss"],
                    "method": "nearest_within_30km",
                    "distance_km": round(dist_km, 2),
                })
    nearest = pd.DataFrame(nearest_rows)
    signal = pd.concat([agg, nearest], ignore_index=True)
    done = set(signal["licence_no"])
    none_rows = layer[~layer["licence_no"].isin(done)][
        ["licence_no", "utility_name"]].copy()
    none_rows["n_nodes"] = 0
    none_rows["node_names"] = ""
    none_rows[["mean_congestion", "mean_lmp", "mean_loss"]] = float("nan")
    none_rows["method"] = "none"
    none_rows["distance_km"] = float("nan")
    signal = pd.concat([signal, none_rows], ignore_index=True)
    signal["exclude_from_correlation"] = signal["licence_no"].isin(
        CORR_EXCLUDE_LICENCES)
    cols = ["licence_no", "utility_name", "n_nodes", "node_names",
            "mean_congestion", "mean_lmp", "mean_loss", "method",
            "distance_km", "exclude_from_correlation"]
    return signal[cols].sort_values("licence_no").reset_index(drop=True)


def build_review_sheet(signal, adoption):
    """Top-20 reported LDCs by customers, with the node(s) used."""
    rep = adoption[adoption["reported"]].copy()
    top = (rep.sort_values("customers", ascending=False).head(TOP_N)
           [["licence_no", "utility_name", "customers"]])
    sheet = top.merge(signal, on=["licence_no", "utility_name"], how="left")
    sheet["owner_status"] = ""
    cols = ["licence_no", "utility_name", "customers", "node_names",
            "n_nodes", "method", "distance_km", "mean_congestion",
            "owner_status"]
    return sheet[cols]


def main():
    layer, node_points, cong, adoption = load_inputs()
    print(f"LDC polygons: {len(layer)}; nodes with coordinates: "
          f"{len(node_points)}; congestion rows: {len(cong)}")

    signal = build_signal(layer, node_points, cong)
    out = PROCESSED / "ldc_node_signal.csv"
    signal.to_csv(out, index=False)

    counts = signal["method"].value_counts().to_dict()
    print(f"signal methods: {counts} -> {out.name}")
    none = signal[signal["method"] == "none"]
    if len(none):
        log_rows([{
            "phase": "2C", "dataset": "ldc_node_signal",
            "action": "no_node_within_30km", "record_id": r["licence_no"],
            "count": 1,
            "note": f'{r["utility_name"]}: no LMP node inside polygon or '
                    f'within {NEAREST_KM:.0f} km',
        } for _, r in none.iterrows()])
    excl = signal[signal["exclude_from_correlation"]]
    if len(excl):
        log_rows([{
            "phase": "2C", "dataset": "ldc_node_signal",
            "action": "excluded_from_correlation", "record_id": r["licence_no"],
            "count": 1,
            "note": f'{r["utility_name"]}: multi-zone/residual; kept out of '
                    f'the Page 4 correlation',
        } for _, r in excl.iterrows()])

    sheet = build_review_sheet(signal, adoption)
    sheet_path = PROCESSED / "crosswalk_review_top20.csv"
    sheet.to_csv(sheet_path, index=False)
    print(f"review sheet: {len(sheet)} rows -> {sheet_path.name} "
          f"(all owner_status blank)")


if __name__ == "__main__":
    main()
