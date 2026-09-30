"""Apply the owner-reviewed LDC reconciliation decisions.

Reads data/inputs/ldc_decisions.csv (schema: licence_no, utility_name,
decision, target_licence_no, reason, source_url, decided_by, status).

Decisions are data, not code: they are never hard-coded here.

- merge_into: dissolve the source polygon into the target polygon at load
  time (the supplied GeoJSON is never modified).
- relicensed_as: rename the polygon's licence_no to the target (a new
  licence number not in the source layer) and keep the old number in
  legacy_licence_no.
- exclude_no_rrr: keep the polygon but flag it excluded; downstream code
  must draw it grey ("not reported"), never as zero, and keep it out of
  rank correlations and per-1,000-customer scales.
- unresolved_lookup: keep the polygon but flag licence_not_current; the
  agent could not find a current same-entity licence for it.

Malformed file -> ValueError (fail fast). Missing file -> warn and return [],
which leaves all decisions unresolved (no merges, no exclusions).
"""
import csv
import re
import sys
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INPUTS = ROOT / "data" / "inputs"
PROCESSED = ROOT / "data" / "processed"

REQUIRED_COLUMNS = ["licence_no", "utility_name", "decision",
                    "target_licence_no", "reason", "source_url",
                    "decided_by", "status"]
VALID_DECISIONS = {"merge_into", "exclude_no_rrr",
                   "relicensed_as", "unresolved_lookup"}
VALID_STATUSES = {"final", "provisional"}
LICENCE_RE = re.compile(r"^ED-\d{4}-\d{4}$")

QA_ENTRIES = []


def warn(message):
    warnings.warn(message)
    print(f"WARNING: {message}", file=sys.stderr)


def load_decisions(path=None, known_licences=None):
    """Load and validate the decisions CSV. Returns a list of dicts.

    known_licences: the set of licence numbers in the supplied polygon
    layer; every decision must reference one (a decision for an unknown
    licence is malformed).
    """
    path = Path(path) if path else INPUTS / "ldc_decisions.csv"
    if not path.exists():
        warn(f"ldc_decisions.csv not found at {path}; "
             "all reconciliation decisions treated as unresolved")
        return []

    with open(path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != REQUIRED_COLUMNS:
            raise ValueError(
                f"malformed ldc_decisions.csv: expected columns "
                f"{REQUIRED_COLUMNS}, got {reader.fieldnames}")
        rows = list(reader)

    seen = set()
    for i, row in enumerate(rows, start=2):  # 1-based, header is line 1
        where = f"ldc_decisions.csv line {i}"
        # A short row yields None for the missing fields; treat as malformed.
        lic = (row["licence_no"] or "").strip()
        decision = (row["decision"] or "").strip()
        target = (row["target_licence_no"] or "").strip()
        status = (row["status"] or "").strip()
        decided_by = (row["decided_by"] or "").strip()
        if not LICENCE_RE.match(lic):
            raise ValueError(f"malformed {where}: bad licence_no {lic!r}")
        if lic in seen:
            raise ValueError(f"malformed {where}: duplicate licence {lic}")
        seen.add(lic)
        if known_licences is not None and lic not in known_licences:
            raise ValueError(
                f"malformed {where}: licence {lic} not in polygon layer")
        if decision not in VALID_DECISIONS:
            raise ValueError(
                f"malformed {where}: bad decision {decision!r}")
        if status not in VALID_STATUSES:
            raise ValueError(f"malformed {where}: bad status {status!r}")
        if not decided_by:
            raise ValueError(f"malformed {where}: decided_by is empty")
        if decision == "merge_into":
            if not target:
                raise ValueError(
                    f"malformed {where}: merge_into needs target_licence_no")
            if not LICENCE_RE.match(target):
                raise ValueError(
                    f"malformed {where}: bad target {target!r}")
            if target == lic:
                raise ValueError(
                    f"malformed {where}: cannot merge a licence into itself")
            if known_licences is not None and target not in known_licences:
                raise ValueError(
                    f"malformed {where}: target {target} not in polygon layer")
        elif decision == "relicensed_as":
            # Target is a NEW licence number, so it must not be in the
            # source layer; the source keeps its geometry under the new id.
            if not target:
                raise ValueError(
                    f"malformed {where}: relicensed_as needs target_licence_no")
            if not LICENCE_RE.match(target):
                raise ValueError(
                    f"malformed {where}: bad target {target!r}")
            if target == lic:
                raise ValueError(
                    f"malformed {where}: relicensed_as needs a new number")
            if known_licences is not None and target in known_licences:
                raise ValueError(
                    f"malformed {where}: target {target} already in layer; "
                    f"use merge_into instead")
        else:  # exclude_no_rrr, unresolved_lookup
            if target:
                raise ValueError(
                    f"malformed {where}: {decision} takes no target")
    return rows


def apply_decisions(gdf, decisions):
    """Apply decisions to a polygon GeoDataFrame.

    Returns (analysis_gdf, qa_entries). The input GeoDataFrame is not
    modified. Added properties: decision, decision_status, excluded
    (bool), merged_licences (on merge targets).
    """
    import geopandas as gpd

    out = gdf.copy()
    out["decision"] = "none"
    out["decision_status"] = ""
    out["excluded"] = False
    out["merged_licences"] = ""
    out["legacy_licence_no"] = ""
    out["licence_not_current"] = False
    qa_entries = []

    by_licence = {row["licence_no"]: i for i, row in out.iterrows()}

    for dec in decisions:
        lic = dec["licence_no"].strip()
        idx = by_licence[lic]
        status = dec["status"].strip()
        kind = dec["decision"].strip()
        out.at[idx, "decision_status"] = status
        if kind == "merge_into":
            target = dec["target_licence_no"].strip()
            tidx = by_licence[target]
            merged = out.at[tidx, "geometry"].union(out.at[idx, "geometry"])
            if not merged.is_valid:
                from shapely.validation import make_valid
                merged = make_valid(merged)
            out.at[tidx, "geometry"] = merged
            prev = out.at[tidx, "merged_licences"]
            out.at[tidx, "merged_licences"] = (prev + "," + lic).strip(",")
            out.at[tidx, "decision"] = "merge_target"
            out.at[tidx, "decision_status"] = status
            # Drop the absorbed source row: the analysis layer has one
            # feature per surviving licence.
            out = out.drop(idx)
            by_licence = {row["licence_no"]: i
                          for i, row in out.iterrows()}
            qa_entries.append({
                "phase": "phase1",
                "dataset": "ldc_decisions",
                "action": "merge_into",
                "record_id": lic,
                "count": 1,
                "note": (f"dissolved {lic} ({dec['utility_name'].strip()}) "
                         f"into {target} at load time; status={status}"),
            })
        elif kind == "relicensed_as":
            target = dec["target_licence_no"].strip()
            out.at[idx, "legacy_licence_no"] = lic
            out.at[idx, "licence_no"] = target
            out.at[idx, "decision"] = "relicensed_as"
            by_licence = {row["licence_no"]: i
                          for i, row in out.iterrows()}
            qa_entries.append({
                "phase": "phase1",
                "dataset": "ldc_decisions",
                "action": "relicensed_as",
                "record_id": target,
                "count": 1,
                "note": (f"renamed {lic} -> {target} "
                         f"({dec['utility_name'].strip()}); status={status}"),
            })
        elif kind == "unresolved_lookup":
            out.at[idx, "decision"] = "unresolved_lookup"
            out.at[idx, "licence_not_current"] = True
            qa_entries.append({
                "phase": "phase1",
                "dataset": "ldc_decisions",
                "action": "unresolved_lookup",
                "record_id": lic,
                "count": 1,
                "note": (f"no current same-entity licence found; polygon kept "
                         f"and flagged licence_not_current; status={status}"),
            })
        else:  # exclude_no_rrr
            out.at[idx, "decision"] = "exclude_no_rrr"
            out.at[idx, "excluded"] = True
            qa_entries.append({
                "phase": "phase1",
                "dataset": "ldc_decisions",
                "action": "exclude_no_rrr",
                "record_id": lic,
                "count": 1,
                "note": (f"flagged excluded (no RRR data); draw grey as "
                         f"'not reported', never as zero; status={status}"),
            })

    out = out.reset_index(drop=True)
    return out, qa_entries


def main():
    import geopandas as gpd
    from qa_log import log_rows

    gdf = gpd.read_file(INPUTS / "ldc_service_areas.geojson")
    known = set(gdf["licence_no"])
    decisions = load_decisions(known_licences=known)
    analysis, qa_entries = apply_decisions(gdf, decisions)
    PROCESSED.mkdir(parents=True, exist_ok=True)
    analysis.to_file(PROCESSED / "ldc_analysis_layer.geojson",
                     driver="GeoJSON")
    log_rows(qa_entries)
    print(f"decisions applied: {len(decisions)}; "
          f"analysis features: {len(analysis)} "
          f"(source licences: {len(gdf)})")


if __name__ == "__main__":
    main()
