"""2C: wholesale locational signal (Page 3) and LDC-zone crosswalk (Page 4).

Reads:
  data/processed/lmp_node_daily.csv    per-day compact DA LMP node history
                                       (node_id, date, mean_lmp_minus_ozp,
                                       mean_loss, mean_congestion, n_hours;
                                       append-only, hourly stays in raw)
  data/processed/ieso_zone_table.csv    station -> electrical zone (extracted
                                        once from the IESO Capacity Auction
                                        Zone Table PDF; zones derived from
                                        this file, never hard-coded)
  data/inputs/ieso_node_locations.csv   node coordinates (user-supplied)
  data/processed/ldc_analysis_layer.geojson   58 LDC service-area features
  data/processed/ldc_adoption.csv       customer counts for the review sheet
  data/inputs/ldc_zone_crosswalk_overrides.csv (optional)

Writes:
  data/processed/node_congestion_30d.csv  per-node 30-day rolling means of
                                          daily LMP / congestion / loss means
  data/processed/node_zone_assignment.csv node -> zone via the ladder
  data/processed/zone_coverage.csv        coverage table by zone_method
  data/processed/ldc_zone_crosswalk.csv   LDC -> zone weights (withheld from
                                          charts: coverage 54.2% < 90% bar)
NOTE: the owner review sheet (crosswalk_review_top20.csv) is built by
src/process_node_signal.py (node-based), not here.

Node-to-zone ladder (directive 2C):
  1. IESO node-zone report (PUB_NodeZoneMap): 404 as of 2026-09-30, skipped.
  2. Capacity Auction Zone Table with conservative matching.
  3. k=3 nearest-neighbour inference (only if non-inferred coverage >= 90%).

Stop condition: non-inferred coverage below 90% -> no zone aggregation,
no inference; Page 3 falls back to the node-level view per the earlier
directive. Negative congestion/loss values are kept throughout.

Every nodal exhibit must call LMP a wholesale locational signal, never a
retail price or a distribution-deferral value.
"""

import csv
import re
import sys
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

import geopandas as gpd
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import qa_log as qa

REPO = Path(__file__).resolve().parent.parent
PROCESSED = REPO / "data" / "processed"
INPUTS = REPO / "data" / "inputs"
OVERRIDES = INPUTS / "ldc_zone_crosswalk_overrides.csv"

# Type suffixes stripped from station names before matching ("BRUCE A TS"
# and node "BRUCEA-LT..." share the base "BRUCEA"). Longest first so
# "ARLEN MTS" strips "MTS", not "TS".
STATION_TYPE_SUFFIXES = ("CTS", "MTS", "JCT", "CSS",
                         "SS", "TS", "GS", "DS", "SW")
MIN_BASE_LEN = 5  # ignore very short station bases ("LEE DS" etc.)

def norm(s):
    """Uppercase alphanumeric only, for conservative name matching."""
    return re.sub(r"[^A-Z0-9]", "", s.upper())


def station_base(station_name):
    base = norm(station_name)
    for suffix in STATION_TYPE_SUFFIXES:
        if base.endswith(suffix) and len(base) - len(suffix) >= MIN_BASE_LEN:
            return base[: -len(suffix)]
    return base


def node_base(node_id):
    """Node IDs look like BASE-LT.SUFFIX; the base names the station."""
    head = re.split(r"-LT\b", node_id, maxsplit=1)[0]
    head = re.split(r"[-.]", head, maxsplit=1)[0]
    return norm(head)


def match_zone(node_id, base_zones):
    """Conservative match: longest station-base prefix wins.

    base_zones maps station base -> (station_name, zone) for unambiguous
    bases only (load_zone_table excludes bases claimed by stations in
    different zones). Returns (zone, matched_station) or (None, None).
    """
    nb = node_base(node_id)
    best_len, best = 0, (None, None)
    for sbase, (station, zone) in base_zones.items():
        if len(sbase) >= MIN_BASE_LEN and nb.startswith(sbase):
            if len(sbase) > best_len:
                best_len, best = len(sbase), (zone, station)
    return best


def load_zone_table():
    """Station -> zone from the committed extraction CSV. Zones are the
    distinct values in the file; nothing is hard-coded. Station bases
    claimed by stations in more than one zone are ambiguous and excluded
    from matching."""
    path = PROCESSED / "ieso_zone_table.csv"
    base_zones = {}
    ambiguous = {}
    zones = set()
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            base = station_base(row["station_name"])
            zones.add(row["electrical_zone"])
            entry = (row["station_name"], row["electrical_zone"])
            if base in ambiguous:
                continue
            if base in base_zones and base_zones[base][1] != entry[1]:
                ambiguous[base] = True
                del base_zones[base]
            else:
                base_zones[base] = entry
    return base_zones, sorted(zones), sorted(ambiguous)


MIN_MATCHED_DAYS = 15  # directive assumption 1: drop nodes below this


def per_node_rolling_means(daily_path, days=32, zonal_path=None):
    """Per-node window stats from the compact daily LMP store.

    mean_premium is hour-weighted: sum(mean_lmp_minus_ozp * n_hours) /
    sum(n_hours), which equals the mean over matched hours of
    (LMP_h - OZP_h) -- never a difference of separate means. mean_lmp is
    recovered as premium + the day's mean OZP (for the Page 3 toggle).
    Nodes with fewer than MIN_MATCHED_DAYS distinct dates in the window
    are dropped (returned separately for the QA log).
    Returns (kept_df, dropped_df, last_day, first_day) as ISO dates.
    """
    df = pd.read_csv(daily_path)
    df["date"] = pd.to_datetime(df["date"]).dt.date
    last_day = df["date"].max()
    first_day = last_day - timedelta(days=days - 1)
    window = df[df["date"] >= first_day].copy()
    # daily mean OZP, to recover the absolute LMP level for Page 3
    zpath = Path(zonal_path) if zonal_path else PROCESSED / "da_zonal_hourly.csv"
    zonal = pd.read_csv(zpath, usecols=["timestamp_utc",
                                        "da_ozp_dollars_per_mwh"])
    zonal["date"] = (pd.to_datetime(zonal["timestamp_utc"], utc=True)
                     .dt.tz_convert("America/Toronto").dt.date)
    ozp_day = zonal.groupby("date")["da_ozp_dollars_per_mwh"].mean()
    window["ozp_day"] = window["date"].map(ozp_day)
    window["d_lmp"] = window["mean_lmp_minus_ozp"] + window["ozp_day"]
    kept, dropped = [], []
    for node, grp in window.groupby("node_id"):
        rec = {"node": node, "n_days": int(grp["date"].nunique())}
        tot_h = float(grp["n_hours"].sum())
        for src, dst in (("mean_lmp_minus_ozp", "mean_premium"),
                         ("mean_congestion", "mean_congestion"),
                         ("mean_loss", "mean_loss"),
                         ("d_lmp", "mean_lmp")):
            rec[dst] = float((grp[src] * grp["n_hours"]).sum() / tot_h)
        (kept if rec["n_days"] >= MIN_MATCHED_DAYS else dropped).append(rec)
    kept_df = pd.DataFrame(kept, columns=["node", "n_days", "mean_premium",
                                         "mean_congestion", "mean_loss",
                                         "mean_lmp"])
    kept_df = kept_df.sort_values("node").reset_index(drop=True)
    dropped_df = pd.DataFrame(dropped, columns=["node", "n_days",
                                                "mean_premium",
                                                "mean_congestion",
                                                "mean_loss", "mean_lmp"])
    return kept_df, dropped_df, last_day.isoformat(), first_day.isoformat()


def build_crosswalk(layer, node_points, adoption):
    """Assign each LDC the zone-share of zoned LMP nodes with coordinates
    inside its polygon. Weights sum to 1 per LDC."""
    joined = gpd.sjoin(node_points, layer[["licence_no", "utility_name",
                                          "geometry"]],
                       how="inner", predicate="intersects")
    votes = (
        joined[joined["zone"] != ""]
        .groupby(["licence_no", "utility_name", "zone"], as_index=False)
        .agg(n_nodes=("node", "nunique"))
    )
    totals = votes.groupby("licence_no")["n_nodes"].transform("sum")
    votes["weight"] = votes["n_nodes"] / totals
    crosswalk = votes.rename(columns={"licence_no": "ldc_id",
                                      "utility_name": "ldc_name",
                                      "zone": "zone_name"})[
        ["ldc_id", "ldc_name", "zone_name", "weight", "n_nodes"]]
    crosswalk["method"] = "in_polygon_zoned_nodes"
    return crosswalk.sort_values(["ldc_id", "weight"],
                                ascending=[True, False]).reset_index(drop=True)


def apply_overrides(crosswalk):
    if not OVERRIDES.exists():
        print(f"note: {OVERRIDES.name} not present; no overrides applied")
        return crosswalk, 0
    ov = pd.read_csv(OVERRIDES)
    need = {"ldc_id", "zone_name", "weight"}
    if not need.issubset(ov.columns):
        raise SystemExit(
            f"malformed {OVERRIDES.name}: need columns {sorted(need)}")
    ov = ov[["ldc_id", "zone_name", "weight"]].copy()
    ov["method"] = "owner_override"
    n_nodes = crosswalk[["ldc_id", "n_nodes"]].drop_duplicates()
    ov = ov.merge(n_nodes, on="ldc_id", how="left")
    names = crosswalk[["ldc_id", "ldc_name"]].drop_duplicates()
    ov = ov.merge(names, on="ldc_id", how="left")
    overridden = set(ov["ldc_id"])
    kept = crosswalk[~crosswalk["ldc_id"].isin(overridden)]
    out = pd.concat([kept, ov[["ldc_id", "ldc_name", "zone_name", "weight",
                               "n_nodes", "method"]]], ignore_index=True)
    return out, len(overridden)


def main():
    entries = []

    def note(dataset, action, record_id, count, text):
        entries.append({"phase": "2C", "dataset": dataset, "action": action,
                        "record_id": record_id, "count": count, "note": text})

    # --- 1. zone table (ladder step 2; step 1 PUB_NodeZoneMap 404s) ---
    base_zones, zones, ambiguous = load_zone_table()
    print(f"zone table: {len(base_zones)} unambiguous station bases, "
          f"zones: {zones}")
    if ambiguous:
        note("zone_table", "ambiguous_base_excluded", ",".join(ambiguous),
             len(ambiguous),
             "station base(s) claimed by stations in different zones; "
             "excluded from matching")
    note("node_zone_report", "skipped_missing_source", "PUB_NodeZoneMap", 1,
         "PUB_NodeZoneMap.xml/csv still 404 on 2026-09-30; ladder step 1 "
         "skipped, Capacity Auction Zone Table used instead")

    # --- 2. node -> zone assignment ---
    lmp_path = PROCESSED / "lmp_node_daily.csv"
    nodes = (pd.read_csv(lmp_path, usecols=["node_id"])["node_id"]
             .unique().tolist())
    assignments = []
    for node in nodes:
        zone, station = match_zone(node, base_zones)
        assignments.append({
            "node": node,
            "zone": zone or "",
            "zone_method": "capacity_auction_table" if zone else "unassigned",
            "matched_station": station or "",
        })
    assign_df = pd.DataFrame(assignments)
    n_matched = int((assign_df["zone"] != "").sum())
    coverage = n_matched / len(nodes)
    print(f"station-table match: {n_matched}/{len(nodes)} = {coverage:.1%}")

    # --- 3. coverage table + stop condition ---
    cov_rows = [
        {"zone_method": "node_zone_report", "n_nodes": 0,
         "note": "PUB_NodeZoneMap 404 (not public)"},
        {"zone_method": "capacity_auction_table", "n_nodes": n_matched,
         "note": "conservative longest-prefix station match"},
        {"zone_method": "k3_inference", "n_nodes": 0,
         "note": "not applied: stop condition fired"},
        {"zone_method": "unassigned", "n_nodes": len(nodes) - n_matched,
         "note": "mostly generator/intertie-style nodes absent from the "
                 "station table"},
    ]
    pd.DataFrame(cov_rows).to_csv(PROCESSED / "zone_coverage.csv", index=False)

    if coverage < 0.90:
        note("zone_aggregation", "stop_condition_fired", "non_inferred_coverage",
             n_matched,
             f"non-inferred node-to-zone coverage {coverage:.1%} "
             f"({n_matched}/{len(nodes)}) below 90%; k=3 inference NOT "
             "applied, no zone aggregation; Page 3 falls back to the "
             "node-level view")
        print("STOP CONDITION: non-inferred coverage below 90%; "
              "no zone aggregation, no k=3 inference")
        zone_aggregation = False
    else:
        zone_aggregation = True

    assign_df.to_csv(PROCESSED / "node_zone_assignment.csv", index=False)

    # --- 4. per-node window stats (the node-level Page 3 metric + the
    # Page 4 premium). 15-day rule drops thin nodes; they are logged. ---
    rolling, dropped, last_day, first_day = per_node_rolling_means(lmp_path)
    if len(dropped):
        note("rolling_window", "dropped_below_15_days",
             ",".join(sorted(dropped["node"].tolist())), len(dropped),
             f"nodes with fewer than {MIN_MATCHED_DAYS} matched days in "
             f"{first_day}..{last_day} excluded from the signal")
        print(f"dropped {len(dropped)} nodes below {MIN_MATCHED_DAYS} "
              f"matched days")
    rolling = rolling.merge(assign_df[["node", "zone", "zone_method"]],
                            on="node", how="left")
    rolling.to_csv(PROCESSED / "node_congestion_30d.csv", index=False)
    print(f"window {first_day}..{last_day}: "
          f"{len(rolling)} nodes, mean days/node "
          f"{rolling['n_days'].mean():.1f}")
    note("rolling_window", "computed", f"{first_day}_to_{last_day}",
         len(rolling),
         "per-node window stats from the compact daily store: mean_premium "
         "is the hour-weighted mean over matched hours of (LMP_h - OZP_h); "
         "nodes below 15 matched days dropped; negatives kept; "
         "no zone aggregation per stop condition")

    # --- 5. LDC-zone crosswalk from in-polygon zoned nodes ---
    layer = gpd.read_file(PROCESSED / "ldc_analysis_layer.geojson")
    loc = pd.read_csv(INPUTS / "ieso_node_locations.csv")
    merged = assign_df.merge(loc, left_on="node", right_on="Location",
                             how="inner")
    pts = gpd.GeoDataFrame(
        merged,
        geometry=gpd.points_from_xy(merged["Longitude"], merged["Latitude"]),
        crs="EPSG:4326",
    )
    n_no_coords = len(nodes) - pts["node"].nunique()
    note("node_coordinates", "excluded_missing", "no_coordinates",
         n_no_coords,
         f"{pts['node'].nunique()}/{len(nodes)} LMP nodes have coordinates; "
         "nodes without are excluded from crosswalk voting")
    crosswalk = build_crosswalk(layer, pts, None)
    crosswalk, n_over = apply_overrides(crosswalk)
    # weights must sum to 1 per LDC
    sums = crosswalk.groupby("ldc_id")["weight"].sum()
    assert ((sums - 1.0).abs() < 1e-9).all(), \
        f"crosswalk weights do not sum to 1: {sums[abs(sums-1)>1e-9]}"
    crosswalk.to_csv(PROCESSED / "ldc_zone_crosswalk.csv", index=False)
    n_ldc_zoned = crosswalk["ldc_id"].nunique()
    print(f"crosswalk: {len(crosswalk)} rows, {n_ldc_zoned}/58 LDCs zoned, "
          f"{n_over} overridden")
    unzoned = sorted(set(layer["licence_no"]) - set(crosswalk["ldc_id"]))
    note("ldc_zone_crosswalk", "ldc_without_zoned_nodes", ",".join(unzoned),
         len(unzoned),
         "LDC polygons containing no zoned LMP nodes; no crosswalk rows, "
         "left for owner review")

    # sanity assertions (directive 2C): the top-weight zone of each must be
    # the obvious one. (Hydro Ottawa's polygon also contains two
    # EAST-zoned stations per the zone table, so it is multi-zone with
    # OTTAWA dominant -- the assertion checks the primary zone only.)
    def primary_zone(name_part):
        lic = layer[layer["utility_name"].str.contains(name_part, case=False,
                                                       na=False)]
        assert len(lic) == 1, f"expected 1 LDC matching {name_part!r}"
        rows = crosswalk[crosswalk["ldc_id"] == lic.iloc[0]["licence_no"]]
        assert len(rows) > 0, f"no crosswalk rows for {name_part}"
        top = rows.sort_values("weight", ascending=False).iloc[0]
        return lic.iloc[0]["utility_name"], top["zone_name"], \
            sorted(rows["zone_name"].unique())
    th_name, th_top, th_all = primary_zone("Toronto Hydro")
    ho_name, ho_top, ho_all = primary_zone("Hydro Ottawa")
    assert th_top == "TORONTO", f"{th_name} primary zone: {th_top}"
    assert ho_top == "OTTAWA", f"{ho_name} primary zone: {ho_top}"
    print(f"sanity: {th_name} -> {th_top} {th_all}; "
          f"{ho_name} -> {ho_top} {ho_all}")

    # --- 6. owner review sheet: moved to process_node_signal.py ---
    # The zone-based review sheet is superseded: zone aggregation is
    # withheld (54.2% node-to-zone coverage < 90% bar), so the review
    # sheet is rebuilt node-based (node(s) used per LDC, blank
    # owner_status) by src/process_node_signal.py. This script no longer
    # writes crosswalk_review_top20.csv.
    qa.log_rows(entries)
    print("done.")


if __name__ == "__main__":
    main()
