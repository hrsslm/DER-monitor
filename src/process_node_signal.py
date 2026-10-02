"""LDC-level wholesale signal from LMP nodes (Page 4 input).

Page 4 resolution directive (2026-10-01; recorded in docs/DATA_SOURCES.md):

  * Metric: per-node premium = mean over matched hours of (DA LMP_h -
    DA OZP_h); LDC value = equal-weighted mean over its load nodes.
    Loss and congestion means are kept for tooltips only.
  * Load nodes only (name-pattern heuristic; IESO publishes no node
    type): aggregates (AG*/APG*), generators, batteries, *_DRA and
    ':LMP' transmission-bus nodes are excluded.
  * Suspect coordinates are quarantined, never repaired:
    NORTHBAY-LT.TT_LF, plus any node more than 25 km from every sibling
    sharing its name stem.
  * Heterogeneous LDCs (within-LDC std of node premium above $1.00/MWh,
    or load nodes spanning more than 100 km) are flagged.
  * Rule-based approval replaces row-by-row review; the owner_override
    column wins over the rule.

Writes data/processed/ldc_node_signal.csv (one row per LDC polygon) and
rebuilds data/processed/crosswalk_review_top20.csv (top 20 reported LDCs
by customers, kept for transparency).

Inputs: data/processed/ldc_analysis_layer.geojson,
        data/inputs/ieso_node_locations.csv,
        data/processed/node_congestion_30d.csv (has mean_premium),
        data/processed/ldc_adoption.csv,
        data/inputs/ldc_owner_overrides.csv
"""
import math
import re
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
QUARANTINE_KM = 25.0
HETERO_STD = 1.00    # $/MWh within-LDC std of node premium
HETERO_SPAN_KM = 100.0

# Node types kept in the signal. Everything else (aggregate, generator,
# battery, dra, bus) is excluded by the Page 4 resolution directive.
LOAD_NODE_TYPES = ("load",)

# Explicit quarantine (directive assumption 3); the automated stem check
# below catches it too, but the explicit entry documents the finding.
EXPLICIT_QUARANTINE = {"NORTHBAY-LT.TT_LF"}


def classify_node_type(name):
    """Heuristic node type from the IESO pricing-location name.

    Neither ieso_node_locations.csv nor the DA LMP reports carry an
    authoritative node-type field, so this is a name-pattern heuristic,
    not a finding:
      bus       '*:LMP'                 transmission-bus reference nodes
      dra       '*_DRA'                 demand-response aggregation
      battery   '*BATT*', '*STRG*',     storage (batteries; STRGE stations)
              station '*STRGE*'
      generator unit specifiers G1..G15, G861.., SG1-8, GTG/STG/CTG units
      aggregate 'AG*'/'APG*'            station aggregate nodes
      load      everything else (LF/T feeders, transformers, ...)
    """
    up = name.upper()
    if up.endswith(":LMP"):
        return "bus"
    if "." not in up:
        return "load"
    station, spec = up.split(".", 1)
    if "_DRA" in spec:
        return "dra"
    if "BATT" in spec or "STRG" in spec or "STRGE" in station:
        return "battery"
    if re.fullmatch(r"((ST|CT|GT|S|C)?G\d+[A-Z]*|G\d+_[A-Z0-9]+)", spec):
        return "generator"
    if spec.startswith("AG") or spec.startswith("APG"):
        return "aggregate"
    return "load"


def name_stem(node_name):
    """Name stem for the sibling-coordinate check: station base with the
    voltage class stripped and generating-station/storage suffixes
    removed, so NORTHBAY-LT and NORTHBAYGS-LT share a stem."""
    station = re.sub(r":LMP$", "", node_name.split(".", 1)[0].upper())
    base = re.sub(r"-(LT|115|230|500)$", "", station)
    return re.sub(r"(GS|BESS\d*|STRGE)$", "", base)


def haversine_km(lon1, lat1, lon2, lat2):
    """Great-circle distance in km."""
    lon1, lat1, lon2, lat2 = map(math.radians, (lon1, lat1, lon2, lat2))
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2)
         * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def find_quarantined_nodes(node_points):
    """Nodes whose coordinates are suspect: the explicit NORTHBAY finding
    plus any node more than QUARANTINE_KM from every sibling sharing its
    name stem. Coordinates are never altered; quarantined nodes are
    excluded from the signal and logged."""
    quarantine = set(EXPLICIT_QUARANTINE) & set(node_points["Location"])
    pts = node_points.dropna(subset=["Longitude", "Latitude"]).copy()
    pts["stem"] = pts["Location"].map(name_stem)
    for stem, grp in pts.groupby("stem"):
        if len(grp) < 2:
            continue
        names = grp["Location"].tolist()
        lons = grp["Longitude"].tolist()
        lats = grp["Latitude"].tolist()
        for i, name in enumerate(names):
            nearest_sibling = min(
                haversine_km(lons[i], lats[i], lons[j], lats[j])
                for j in range(len(names)) if j != i)
            if nearest_sibling > QUARANTINE_KM:
                quarantine.add(name)
    return quarantine


def type_breakdown(types):
    counts = pd.Series(list(types)).value_counts()
    return ", ".join(f"{t}:{k}" for t, k in counts.items())


def load_owner_overrides():
    """Owner decisions keyed by licence_no. Values must be approved or
    rejected; anything else fails clearly."""
    path = INPUTS / "ldc_owner_overrides.csv"
    ov = pd.read_csv(path)
    bad = ov[~ov["owner_override"].isin(["approved", "rejected"])]
    if len(bad):
        raise ValueError(
            "ldc_owner_overrides.csv has invalid owner_override values "
            f"(must be approved/rejected): {bad['owner_override'].unique()}")
    return {r["licence_no"]: (r["owner_override"], str(r.get("reason", "")))
            for _, r in ov.iterrows()}


def load_inputs():
    layer = gpd.read_file(PROCESSED / "ldc_analysis_layer.geojson")
    loc = pd.read_csv(INPUTS / "ieso_node_locations.csv")
    nodes = loc.dropna(subset=["Longitude", "Latitude"]).copy()
    nodes["node_type"] = nodes["Location"].map(classify_node_type)
    points = gpd.GeoDataFrame(
        nodes, geometry=gpd.points_from_xy(nodes["Longitude"],
                                           nodes["Latitude"]),
        crs="EPSG:4326")
    cong = pd.read_csv(PROCESSED / "node_congestion_30d.csv")
    adoption = pd.read_csv(PROCESSED / "ldc_adoption.csv")
    return layer, points, cong, adoption


def _span_km(points_3161):
    """Max pairwise distance (km) between points in EPSG:3161."""
    if len(points_3161) < 2:
        return 0.0
    xy = [(g.x, g.y) for g in points_3161.geometry]
    return max(math.dist(xy[i], xy[j])
               for i in range(len(xy)) for j in range(i + 1, len(xy))) / 1000.0


def build_signal(layer, node_points, cong, node_types):
    """One signal row per LDC polygon for the given node-type set.

    The premium is the equal-weighted mean over the LDC's load nodes of
    mean_premium. Returns the signal DataFrame.
    """
    nodes = node_points.merge(cong, left_on="Location", right_on="node",
                              how="inner")
    nodes = nodes[nodes["node_type"].isin(node_types)].copy()
    inside = gpd.sjoin(nodes[["Location", "node_type", "mean_premium",
                              "mean_congestion", "mean_loss", "geometry"]],
                       layer[["licence_no", "utility_name", "geometry"]],
                       how="inner", predicate="intersects")
    proj = inside.to_crs("EPSG:3161")
    span_rows = [{"licence_no": lic, "utility_name": name,
                 "span_km": _span_km(grp)}
                for (lic, name), grp in
                proj.groupby(["licence_no", "utility_name"])]
    span = pd.DataFrame(span_rows,
                      columns=["licence_no", "utility_name", "span_km"])
    agg = (inside.groupby(["licence_no", "utility_name"], as_index=False)
           .agg(n_nodes=("Location", "nunique"),
                node_names=("Location", lambda s: "; ".join(sorted(set(s)))),
                node_types=("node_type", type_breakdown),
                mean_premium=("mean_premium", "mean"),
                std_premium=("mean_premium", "std"),
                mean_congestion=("mean_congestion", "mean"),
                mean_loss=("mean_loss", "mean")))
    agg = agg.merge(span, on=["licence_no", "utility_name"], how="left")
    agg["method"] = "in_polygon"
    agg["distance_km"] = 0.0

    have = set(agg["licence_no"])
    missing = layer[~layer["licence_no"].isin(have)].copy()
    nearest_rows = []
    if len(missing):
        proj_layer = missing.to_crs("EPSG:3161")[["licence_no",
                                                  "utility_name",
                                                  "geometry"]]
        proj_nodes = nodes.to_crs("EPSG:3161")[["Location", "node_type",
                                                "mean_premium",
                                                "mean_congestion",
                                                "mean_loss", "geometry"]]
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
                    "node_types": node["node_type"],
                    "mean_premium": node["mean_premium"],
                    "std_premium": float("nan"),
                    "mean_congestion": node["mean_congestion"],
                    "mean_loss": node["mean_loss"],
                    "span_km": 0.0,
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
    none_rows["node_types"] = ""
    none_rows[["mean_premium", "std_premium", "mean_congestion",
               "mean_loss", "span_km", "distance_km"]] = float("nan")
    none_rows["method"] = "none"
    signal = pd.concat([signal, none_rows], ignore_index=True)
    signal["heterogeneous"] = (
        (signal["std_premium"] > HETERO_STD)
        | (signal["span_km"] > HETERO_SPAN_KM)).fillna(False)
    cols = ["licence_no", "utility_name", "n_nodes", "node_names",
            "node_types", "mean_premium", "std_premium", "mean_congestion",
            "mean_loss", "span_km", "heterogeneous", "method",
            "distance_km"]
    return signal[cols].sort_values("licence_no").reset_index(drop=True)


def apply_approval(signal, adoption, overrides):
    """Rule-based approval; owner_override wins. Returns the signal with
    owner_status / owner_override / status_reason columns."""
    reported = adoption.set_index("licence_no")["reported"].to_dict()
    signal = signal.copy()
    signal["owner_override"] = signal["licence_no"].map(
        lambda lic: overrides.get(lic, ("", ""))[0])
    statuses, reasons = [], []
    for _, r in signal.iterrows():
        lic = r["licence_no"]
        rule_problems = []
        if r["n_nodes"] == 0:
            rule_problems.append("no load node within 30 km")
        if r["method"] != "in_polygon":
            rule_problems.append(f"method={r['method']}")
        if r["heterogeneous"]:
            rule_problems.append("heterogeneous")
        if not reported.get(lic, False):
            rule_problems.append("no adoption data")
        rule_status = ("rejected" if rule_problems else "approved")
        rule_reason = ("; ".join(rule_problems) if rule_problems
                       else "rule: >=1 load node, in_polygon, "
                            "not heterogeneous, adoption reported")
        ov_status, ov_reason = overrides.get(lic, ("", ""))
        if ov_status:
            statuses.append(ov_status)
            reasons.append(f"owner_override={ov_status}: {ov_reason} "
                           f"(rule would be {rule_status})")
        else:
            statuses.append(rule_status)
            reasons.append(rule_reason)
    signal["owner_status"] = statuses
    signal["status_reason"] = reasons
    return signal


def build_review_sheet(signal, adoption):
    """Top-20 reported LDCs by customers, kept for transparency."""
    rep = adoption[adoption["reported"]].copy()
    top = (rep.sort_values("customers", ascending=False).head(TOP_N)
           [["licence_no", "utility_name", "customers"]])
    sheet = top.merge(signal, on=["licence_no", "utility_name"], how="left")
    cols = ["licence_no", "utility_name", "customers", "node_names",
            "node_types", "n_nodes", "method", "distance_km",
            "mean_premium", "heterogeneous", "owner_status",
            "owner_override", "status_reason"]
    return sheet[cols]


def main():
    layer, node_points, cong, adoption = load_inputs()
    print(f"LDC polygons: {len(layer)}; nodes with coordinates: "
          f"{len(node_points)}; premium rows: {len(cong)}")

    type_counts = node_points["node_type"].value_counts().to_dict()
    print(f"node types (name-pattern heuristic): {type_counts}")
    log_rows([{
        "phase": "2C", "dataset": "ieso_node_locations",
        "action": "node_type_classification", "record_id": t,
        "count": k,
        "note": "name-pattern heuristic; no authoritative IESO node-type "
                "field exists in the inputs",
    } for t, k in type_counts.items()])

    quarantine = find_quarantined_nodes(node_points)
    if quarantine:
        log_rows([{
            "phase": "2C", "dataset": "ieso_node_locations",
            "action": "suspect_coordinate_quarantined", "record_id": n,
            "count": 1,
            "note": "node more than 25 km from every sibling sharing its "
                    "name stem (or the explicit NORTHBAY finding); "
                    "coordinate kept as-is, node excluded from the signal",
        } for n in sorted(quarantine)])
        print(f"quarantined {len(quarantine)} suspect-coordinate nodes: "
              f"{sorted(quarantine)}")
    node_points = node_points[~node_points["Location"].isin(quarantine)]

    # Assumption 2 effect: premium with load-only vs load+aggregate.
    prev = build_signal(layer, node_points, cong, ("load", "aggregate"))
    signal = build_signal(layer, node_points, cong, LOAD_NODE_TYPES)
    both = signal[["licence_no", "utility_name", "mean_premium"]].merge(
        prev[["licence_no", "mean_premium"]].rename(
            columns={"mean_premium": "prev_premium"}), on="licence_no")
    changed = both[(both["mean_premium"] - both["prev_premium"]).abs() > 0.50]
    print(f"LDCs changing by >$0.50/MWh on excluding aggregates: "
          f"{len(changed)} of {len(both)}")
    for _, r in changed.iterrows():
        print(f'  {r["utility_name"]}: {r["prev_premium"]:.2f} -> '
              f'{r["mean_premium"]:.2f}')

    overrides = load_owner_overrides()
    unknown = [lic for lic in overrides if lic not in set(layer["licence_no"])]
    if unknown:
        print(f"WARNING: overrides for unknown licences: {unknown}")
    signal = apply_approval(signal, adoption, overrides)

    out = PROCESSED / "ldc_node_signal.csv"
    signal.to_csv(out, index=False)
    counts = signal["method"].value_counts().to_dict()
    appr = signal[signal["owner_status"] == "approved"]
    print(f"signal methods: {counts}; approval: {len(appr)} approved, "
          f"{len(signal) - len(appr)} rejected -> {out.name}")

    sheet = build_review_sheet(signal, adoption)
    sheet_path = PROCESSED / "crosswalk_review_top20.csv"
    sheet.to_csv(sheet_path, index=False)
    print(f"review sheet: {len(sheet)} rows -> {sheet_path.name}")


if __name__ == "__main__":
    main()
