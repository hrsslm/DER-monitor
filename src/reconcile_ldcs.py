"""Reconcile OEB RRR company names against the LDC polygon layer.

Findings encoded here (2026-09-29, from the downloaded 2024-2 vintage):
- The raw RRR XML contains NO licence-number elements at all (verified by
  grepping all four files), so the join key is the distributor company name.
- Matching is conservative: a GeoJSON name is accepted only on
  (1) exact match to a RRR Current_Company_Name,
  (2) exact match after stripping a "(Formerly ...)" suffix, or
  (3) exact match to a RRR Historical_Company_Name (amalgamations).
  Normalization is just lowercase + collapsed whitespace. Nothing fuzzy is
  ever accepted silently; the rest goes to ldc_name_map_review.csv.
- The supplied GeoJSON concatenates two source-feature names with " | " for
  the one duplicate-licence case (ED-2021-0280, GrandBridge); both parts are
  tried individually.

Outputs:
- data/processed/ldc_reconciliation.csv
- data/processed/ldc_name_map_review.csv
- rows appended to data/processed/qa_log.csv
"""
import csv
import glob
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import geopandas as gpd

from qa_log import log_rows

ROOT = Path(__file__).resolve().parent.parent
RAW_OEB = ROOT / "data" / "raw" / "oeb"
PROCESSED = ROOT / "data" / "processed"
INPUTS = ROOT / "data" / "inputs"


def norm(name):
    """Conservative name normalization: lowercase, collapsed whitespace."""
    return re.sub(r"\s+", " ", name.strip().lower())


def strip_parenthetical(name):
    """Remove a trailing '(...)' note, e.g. '(Formerly Energy+ Inc.)'."""
    return re.sub(r"\s*\([^)]*\)\s*$", "", name).strip()


_QA_ENTRIES = []


def qa_log(phase, dataset, action, record_id, count, note):
    """Buffer one QA-log row; rows are written via log_rows() at the end."""
    _QA_ENTRIES.append({
        "phase": phase, "dataset": dataset, "action": action,
        "record_id": record_id, "count": count, "note": note,
    })


def load_rrr_names():
    """Return (current_names, historical_map) from the four RRR XML files.

    current_names: set of Current_Company_Name strings.
    historical_map: Historical_Company_Name -> (Current_Company_Name, table).
    Also returns the observed year range per table.
    """
    current, historical, years = set(), {}, {}
    files = sorted(glob.glob(str(RAW_OEB / "ED 2.1.*.xml")))
    if not files:
        print("WARNING: no RRR XML files in data/raw/oeb/; reconciliation skipped",
              flush=True)
        return current, historical, years
    for path in files:
        table = Path(path).name
        root = ET.parse(path).getroot()
        for rec in root:
            c = (rec.findtext("Current_Company_Name") or "").strip()
            h = (rec.findtext("Historical_Company_Name") or "").strip()
            y = (rec.findtext("Year") or "").strip()
            if c:
                current.add(c)
            if c and h:
                historical.setdefault(h, (c, table))
            if y.isdigit():
                years.setdefault(table, [9999, 0])
                years[table][0] = min(years[table][0], int(y))
                years[table][1] = max(years[table][1], int(y))
    return current, historical, years


def match_name(part, current_n, historical_n):
    """Match one GeoJSON name part against RRR names.

    Returns (rrr_name, match_type) or (None, 'unmatched').
    """
    key = norm(part)
    if key in current_n:
        return current_n[key], "exact_current"
    stripped = norm(strip_parenthetical(part))
    if stripped != key and stripped in current_n:
        return current_n[stripped], "parenthetical_stripped"
    if key in historical_n:
        return historical_n[key], "exact_historical"
    return None, "unmatched"


# Distributors confirmed (from the 2024-2 RRR vintage) to have no RRR rows and
# that are remote/non-filing territories. Per the directive they are marked
# remote=True and excluded from per-1,000-customer metrics when customer
# counts are missing; they are listed on the page, never drawn as zero.
REMOTE_LICENCES = {
    "ED-2001-0090",  # Attawapiskat Power Corporation
    "ED-2003-0079",  # Fort Albany Power Corporation
    "ED-2003-0081",  # Kashechewan Power Corporation
    "ED-2003-0037",  # Hydro One Remote Communities Inc.
}

# Hydro One Networks is residual territory wrapped around other LDCs, not an
# urban distributor (multi_zone=True for Page 4).
MULTI_ZONE_LICENCES = {
    "ED-2003-0043",  # Hydro One Networks Inc. (licence no. verified below)
}


def main():
    PROCESSED.mkdir(parents=True, exist_ok=True)
    current, historical, years = load_rrr_names()
    current_n = {norm(c): c for c in current}
    # historical: normalized historical name -> RRR current (filing) name.
    historical_n = {norm(h): pair[0] for h, pair in historical.items()}

    print("RRR year ranges:")
    for table, (lo, hi) in sorted(years.items()):
        print(f"  {table[:45]:45s} {lo}-{hi}")

    qa_log("phase1", "oeb_rrr_xml", "licence_number_check",
           "all_four_tables", 0,
           "no licence-number elements exist in the raw RRR XML; "
           "company name is the only join key")
    qa_log("phase1", "oeb_rrr_xml", "name_inventory",
           "distinct_current_names", len(current),
           "distinct RRR Current_Company_Name values across the four tables")
    qa_log("phase1", "oeb_rrr_xml", "name_inventory",
           "distinct_historical_names", len(historical),
           "distinct RRR Historical_Company_Name values across the four tables")

    ldc = gpd.read_file(INPUTS / "ldc_service_areas.geojson")

    recon_rows, review_rows = [], []
    matched = unmatched = 0
    for _, row in ldc.iterrows():
        licence = row["licence_no"]
        utility = row["utility_name"]
        # The supplied GeoJSON concatenates two source names with " | "
        # for the duplicate-licence case (ED-2021-0280).
        parts = [p.strip() for p in str(utility).split("|")]
        if len(parts) > 1:
            qa_log("phase1", "ldc_names", "name_split",
                   licence, len(parts),
                   f"supplied utility_name contains ' | '; tried parts: {parts}")
        results = [match_name(p, current_n, historical_n) for p in parts]
        good = [r for r in results if r[1] != "unmatched"]
        if good and len(good) == len(results):
            # All parts matched (usually one part, one match).
            rrr_name = "; ".join(r[0] for r in good)
            mtype = "+".join(sorted({r[1] for r in good}))
            matched += 1
        else:
            rrr_name, mtype = "", "unmatched"
            unmatched += 1
        recon_rows.append({
            "licence_no": licence,
            "utility_name": utility,
            "rrr_name": rrr_name,
            "match_type": mtype,
            "remote": str(licence in REMOTE_LICENCES),
            "multi_zone": str(licence in MULTI_ZONE_LICENCES),
        })

    hon = ldc[ldc["licence_no"] == "ED-2003-0043"]
    assert len(hon) == 1 and "Hydro One Networks" in hon.iloc[0]["utility_name"], \
        "Hydro One Networks licence ED-2003-0043 not found in the layer"

    with open(PROCESSED / "ldc_reconciliation.csv", "w",
              newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["licence_no", "utility_name",
                                                    "rrr_name", "match_type",
                                                    "remote", "multi_zone"])
        writer.writeheader()
        writer.writerows(recon_rows)
    print(f"reconciliation: {matched} matched, {unmatched} unmatched "
          f"(of {len(ldc)})")

    # Candidate notes for the unmatched, from the data only (no assumptions).
    candidates = {
        "Orillia Power Distribution Corporation":
            "RRR historical name 'Hydro One Networks Inc. (Orillia-Peterborough "
            "service areas)' -> files under 'Hydro One Networks Inc.'; needs "
            "owner decision on whether to attribute to Hydro One Networks",
        "Hydro One Remote Communities Inc.":
            "no RRR rows under this or any similar name in any of the four "
            "tables (2015-2024 vintage); confirmed non-filer from the data",
        "Kashechewan Power Corporation":
            "no RRR rows under this or any similar name in any of the four "
            "tables (2015-2024 vintage); confirmed non-filer from the data",
        "Attawapiskat Power Corporation":
            "no RRR rows under this or any similar name in any of the four "
            "tables (2015-2024 vintage); confirmed non-filer from the data",
        "Fort Albany Power Corporation":
            "no RRR rows under this or any similar name in any of the four "
            "tables (2015-2024 vintage); confirmed non-filer from the data",
        "Cornwall Street Railway Light and Power Company Limited":
            "no RRR rows under this or any similar name in any of the four "
            "tables (2015-2024 vintage); confirmed non-filer from the data",
    }
    for r in recon_rows:
        if r["match_type"] == "unmatched":
            review_rows.append({
                "licence_no": r["licence_no"],
                "utility_name": r["utility_name"],
                "candidate_note": candidates.get(r["utility_name"],
                                                "no candidate; manual review"),
                "action_required": "owner decision",
            })
            qa_log("phase1", "ldc_reconciliation", "unmatched_name",
                   r["licence_no"], 1,
                   f"{r['utility_name']} has no RRR rows; see "
                   f"ldc_name_map_review.csv")

    with open(PROCESSED / "ldc_name_map_review.csv", "w",
              newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["licence_no", "utility_name",
                                                    "candidate_note",
                                                    "action_required"])
        writer.writeheader()
        writer.writerows(review_rows)
    print(f"review file: {len(review_rows)} rows -> "
          f"{PROCESSED / 'ldc_name_map_review.csv'}")
    total = log_rows(_QA_ENTRIES)
    print(f"qa log: {total} rows -> {PROCESSED / 'qa_log.csv'}")


if __name__ == "__main__":
    sys.exit(main())
