"""Phase 2B: DER adoption inputs (Page 2).

Reads the OEB RRR tables (latest vintage in data/raw/oeb/) and builds
per-distributor adoption metrics:

  data/processed/ldc_adoption.csv      keyed by licence_no
  data/processed/adoption_crosscheck.csv  contract-list cross-check

Definitions (all from published OEB RRR data, latest year per table):
  - net-metered customers / capacity: RRR 2.1.14 Table 1 (kW -> MW)
  - embedded generation facilities / capacity: RRR 2.1.14 Table 3 (kW -> MW)
  - embedded capacity by fuel type: RRR 2.1.14 Table 4 (kW -> MW)
  - denominator: RRR 2.1.2, sum of rows with Customer_or_Connections =
    "Customers" (all rate classes, not residential only)
  - headline metric: total DER capacity (net-metered + embedded) per
    1,000 customers

Name matching reuses data/processed/ldc_reconciliation.csv
(licence_no -> RRR Current_Company_Name) from Phase 1.

Failure policy: a schema change that breaks a loader raises loudly
(nonzero exit) instead of guessing, per the directive's stop conditions.
"""

import csv
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import qa_log as qa_log_module  # noqa: E402  (namespace-replacing log_rows)

REPO = Path(__file__).resolve().parent.parent
RAW_OEB = REPO / "data" / "raw" / "oeb"
PROCESSED = REPO / "data" / "processed"

T1_FILE = "ED 2.1.14 Table 1 Net Metering.xml"
T3_FILE = "ED 2.1.14 Table 3 Embedded Generation.xml"
T4_FILE = "ED 2.1.14 Table 4 Embedded Generation by Type.xml"
T212_FILE = "ED 2.1.2 Customers & Connections.xml"
CONTRACT_XLSX = REPO / "data" / "raw" / "ieso" / "IESO-Active-Contracted-Generation-List.xlsx"

# Table 4 fuel types, in a fixed order for stable output columns.
FUEL_TYPES = [
    "Solar", "Wind", "Water", "Biomass", "Fossil Fuel",
    "Exporting Storage", "Non-Exporting Storage", "Other",
]

# IESO contract-list (Fuel Group, Fuel Type) -> Table 4 Facility_Type.
# Used only for the validation cross-check; the two sources are never
# blended into one total.
CONTRACT_FUEL_MAP = {
    ("Renewables", "Solar"): "Solar",
    ("Renewables", "Wind"): "Wind",
    ("Renewables", "Waterpower"): "Water",
    ("Renewables", "Biogas"): "Biomass",
    ("Renewables", "Biogas (On-Farm)"): "Biomass",
    ("Renewables", "Biomass"): "Biomass",
    ("Renewables", "Landfill Gas"): "Biomass",
    ("Gas", "Natural Gas"): "Fossil Fuel",
    ("Gas", "By Product Gas"): "Fossil Fuel",
    ("Storage", "Battery"): "Exporting Storage",
    ("Storage", "Compressed Air"): "Exporting Storage",
    ("Other", "Municipal Waste"): "Other",
}

REQUIRED_COLUMNS = {
    T1_FILE: {"Current_Company_Name", "Year", "Renewable_Energy_Type",
              "No_of_Net-Metered_Generators",
              "Cumulative_Installed_Capacity_kW"},
    T3_FILE: {"Current_Company_Name", "Year",
              "Number_of_Embedded_Generation_Facilities",
              "Total_Installed_Capacity__x0028_kW_x0029__Embedded_Generator"},
    T4_FILE: {"Current_Company_Name", "Year", "Facility_Type",
              "Number_of_Facilities", "Installed_Capacity_kW"},
    T212_FILE: {"Current_Company_Name", "Year", "Rate_Class",
                "Customer_or_Connections", "Total_Customers_or_Connections"},
}

_QA_ENTRIES = []


def qa_log(phase, dataset, action, record_id, count, note):
    """Buffer one QA-log row (written via the shared namespace-replacing
    log_rows at the end, so reruns never duplicate rows)."""
    _QA_ENTRIES.append({
        "phase": phase, "dataset": dataset, "action": action,
        "record_id": record_id, "count": count, "note": note,
    })


def warn(message):
    print(f"WARNING: {message}", flush=True)


def load_rrr_table(filename):
    """Parse one RRR XML file; fail loudly if the schema changed."""
    path = RAW_OEB / filename
    root = ET.parse(path).getroot()
    records = list(root)
    if not records:
        raise RuntimeError(f"{filename}: no records; refusing to guess")
    columns = {child.tag for child in records[0]}
    missing = REQUIRED_COLUMNS[filename] - columns
    if missing:
        raise RuntimeError(
            f"{filename}: schema changed, missing columns {sorted(missing)}; "
            "loader stopped instead of guessing")
    return records


def num(text):
    """Parse a numeric field; missing or blank means 0."""
    if text is None or not text.strip():
        return 0.0
    return float(text.replace(",", "").strip())


def latest_year(records):
    """The newest Year value present in a table."""
    years = {r.findtext("Year").strip() for r in records
             if r.findtext("Year") and r.findtext("Year").strip().isdigit()}
    return max(years)


def aggregate_t1(records, year):
    """Net metering: customers and cumulative capacity per company (kW)."""
    out = {}
    for rec in records:
        if (rec.findtext("Year") or "").strip() != year:
            continue
        company = (rec.findtext("Current_Company_Name") or "").strip()
        agg = out.setdefault(company, {"customers": 0.0, "capacity_kw": 0.0})
        agg["customers"] += num(rec.findtext("No_of_Net-Metered_Generators"))
        agg["capacity_kw"] += num(rec.findtext("Cumulative_Installed_Capacity_kW"))
    return out


def aggregate_t3(records, year):
    """Embedded generation: facilities and capacity per company (kW)."""
    out = {}
    for rec in records:
        if (rec.findtext("Year") or "").strip() != year:
            continue
        company = (rec.findtext("Current_Company_Name") or "").strip()
        agg = out.setdefault(company, {"facilities": 0.0, "capacity_kw": 0.0})
        agg["facilities"] += num(rec.findtext("Number_of_Embedded_Generation_Facilities"))
        agg["capacity_kw"] += num(rec.findtext(
            "Total_Installed_Capacity__x0028_kW_x0029__Embedded_Generator"))
    return out


def aggregate_t4(records, year):
    """Embedded generation by fuel type: per company, per fuel (kW)."""
    out = {}
    for rec in records:
        if (rec.findtext("Year") or "").strip() != year:
            continue
        company = (rec.findtext("Current_Company_Name") or "").strip()
        fuel = (rec.findtext("Facility_Type") or "").strip()
        agg = out.setdefault(company, {})
        slot = agg.setdefault(fuel, {"facilities": 0.0, "capacity_kw": 0.0})
        slot["facilities"] += num(rec.findtext("Number_of_Facilities"))
        slot["capacity_kw"] += num(rec.findtext("Installed_Capacity_kW"))
    return out


def aggregate_t212(records, year):
    """Denominator: total customers per company (Customer rows, all classes)."""
    out = {}
    for rec in records:
        if (rec.findtext("Year") or "").strip() != year:
            continue
        if (rec.findtext("Customer_or_Connections") or "").strip() != "Customers":
            continue
        company = (rec.findtext("Current_Company_Name") or "").strip()
        out[company] = out.get(company, 0.0) + num(
            rec.findtext("Total_Customers_or_Connections"))
    return out


def fuel_key(fuel):
    """Column-safe key for a Table 4 fuel type."""
    return fuel.lower().replace("-", " ").replace("  ", " ").replace(" ", "_")


def load_contract_dx():
    """Distribution-connected, in-service contracts: MW per Table 4 fuel.

    The contract list has no distributor column (only Closest City/Town),
    so this cross-check runs at the province/fuel-group level. The two
    sources are never summed or blended.
    """
    import openpyxl
    wb = openpyxl.load_workbook(CONTRACT_XLSX, read_only=True, data_only=True)
    rows = list(wb["Contract Data"].iter_rows(values_only=True))
    header = [c for c in rows[2]]
    ci = {name: i for i, name in enumerate(header)}
    per_fuel = {}
    unmatched = 0
    for row in rows[3:]:
        if row[ci["Connection Type"]] != "Dx":
            continue
        if row[ci["Contract Status"]] != "CO":  # in-service only
            continue
        key = (row[ci["Fuel Group"]], row[ci["Fuel Type"]])
        fuel = CONTRACT_FUEL_MAP.get(key)
        if fuel is None:
            unmatched += 1
            continue
        per_fuel[fuel] = per_fuel.get(fuel, 0.0) + float(row[ci["Contract Capacity (MW)"]] or 0)
    if unmatched:
        warn(f"contract list: {unmatched} Dx+CO rows had no fuel mapping; excluded from cross-check")
        qa_log("2b", "ieso_contract_list", "unmapped_fuel_excluded",
               "dx_inservice", unmatched,
               "contract rows whose (Fuel Group, Fuel Type) has no Table 4 equivalent")
    return per_fuel


def main():
    PROCESSED.mkdir(parents=True, exist_ok=True)

    t1 = load_rrr_table(T1_FILE)
    t3 = load_rrr_table(T3_FILE)
    t4 = load_rrr_table(T4_FILE)
    t212 = load_rrr_table(T212_FILE)

    year_t1, year_t3 = latest_year(t1), latest_year(t3)
    year_t4, year_t212 = latest_year(t4), latest_year(t212)
    years = {year_t1, year_t3, year_t4, year_t212}
    if len(years) != 1:
        warn(f"RRR tables disagree on latest year: "
             f"t1={year_t1} t3={year_t3} t4={year_t4} t212={year_t212}; "
             "using each table's own latest year and recording it per LDC")
        qa_log("2b", "oeb_rrr_xml", "vintage_mismatch", "latest_year",
               len(years),
               f"t1={year_t1} t3={year_t3} t4={year_t4} t212={year_t212}")
    data_year = year_t212  # denominator's year anchors the headline metric

    agg_t1 = aggregate_t1(t1, year_t1)
    agg_t3 = aggregate_t3(t3, year_t3)
    agg_t4 = aggregate_t4(t4, year_t4)
    customers = aggregate_t212(t212, year_t212)

    # Name bridge: for each of the 58 analysis-layer features, find the RRR
    # company name(s) via the Phase 1 reconciliation. Relicensed features
    # look up their legacy licence; merged features look up both the target
    # and the merged licence(s) and the merge rule below decides how to
    # combine them.
    recon = {}
    with open(PROCESSED / "ldc_reconciliation.csv", newline="",
              encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            recon[row["licence_no"]] = row

    import json as _json
    layer = _json.load(open(PROCESSED / "ldc_analysis_layer.geojson"))["features"]
    features = sorted(layer, key=lambda x: x["properties"]["licence_no"])

    def rrr_names_for(licence):
        row = recon.get(licence, {})
        names = [n.strip() for n in row.get("rrr_name", "").split(";")
                 if n.strip()]
        return names, row.get("match_type", ""), row.get("multi_zone", "False")

    adoption_rows = []
    for feat in features:
        props = feat["properties"]
        licence = props["licence_no"]
        utility = props["utility_name"]
        legacy = (props.get("legacy_licence_no") or "").strip()
        merged = [m.strip() for m in
                  (props.get("merged_licences") or "").split(";") if m.strip()]

        # The merge rule (directive A.4): in the analysis vintage, if both
        # the source and the target have RRR rows, sum additive quantities;
        # if only the target has rows, use the target alone.
        rrr_names, match_type, multi_zone = rrr_names_for(legacy or licence)
        if merged:
            extra_names, extra_types = [], []
            for mlic in merged:
                names, mtype, _ = rrr_names_for(mlic)
                extra_names += names
                extra_types.append(f"{mlic}:{mtype}")
            # Do the merged licences actually have rows under their own
            # (pre-merger) identity in the vintage? Names that merely resolve
            # to the target's own current name (via a historical alias) do
            # not count -- that is the target's data, not the source's.
            target_names = set(rrr_names)
            have = [n for n in extra_names
                    if n not in target_names
                    and (n in agg_t1 or n in agg_t3 or n in agg_t4
                         or n in customers)]
            if have:
                rrr_names = rrr_names + extra_names
                match_type = match_type + "+" + "+".join(extra_types)
                merge_case = (f"both target and {merged} have rows; "
                              "additive quantities summed")
            else:
                merge_case = (f"only the target has rows in {data_year}; "
                              f"{merged} contributed nothing")
            qa_log("2b", "oeb_rrr_xml", "merge_rule_applied", licence,
                   len(have), merge_case)

        row = {
            "licence_no": licence,
            "utility_name": utility,
            "data_year": data_year,
            "vintage": "2024-2",
            "rrr_name": "; ".join(rrr_names),
            "match_type": match_type,
            "reported": "True",
            "multi_zone": multi_zone,
        }
        if props["excluded"] or not rrr_names:
            row["reported"] = "False"
            qa_log("2b", "ldc_adoption", "excluded_no_value", licence, 0,
                   "owner decision exclude_no_rrr (or no RRR name match): "
                   "reported=False, no metric")
            adoption_rows.append(row)
            continue

        # Sum across the (usually one) matched RRR company names.
        nm_cust = nm_kw = emb_fac = emb_kw = cust = 0.0
        fuel = {f: {"facilities": 0.0, "capacity_kw": 0.0} for f in FUEL_TYPES}
        for name in rrr_names:
            a1 = agg_t1.get(name, {})
            nm_cust += a1.get("customers", 0.0)
            nm_kw += a1.get("capacity_kw", 0.0)
            a3 = agg_t3.get(name, {})
            emb_fac += a3.get("facilities", 0.0)
            emb_kw += a3.get("capacity_kw", 0.0)
            for f, slot in agg_t4.get(name, {}).items():
                if f in fuel:
                    fuel[f]["facilities"] += slot["facilities"]
                    fuel[f]["capacity_kw"] += slot["capacity_kw"]
                else:
                    warn(f"unexpected Table 4 fuel type {f!r}; kept out of per-fuel columns")
                    qa_log("2b", "oeb_rrr_xml", "unexpected_fuel_type", f, 1,
                           "fuel type not in the published list; excluded from per-fuel columns")
            cust += customers.get(name, 0.0)

        if cust == 0:
            warn(f"{licence}: no customer denominator; reported=False")
            qa_log("2b", "ldc_adoption", "missing_denominator", licence, 0,
                   "no 2.1.2 customer rows for the matched RRR name(s); "
                   "listed, never drawn as zero")
            row["reported"] = "False"
            adoption_rows.append(row)
            continue

        total_mw = (nm_kw + emb_kw) / 1000.0
        row.update({
            "netmetered_customers": round(nm_cust),
            "netmetered_capacity_mw": round(nm_kw / 1000.0, 3),
            "embedded_facilities": round(emb_fac),
            "embedded_capacity_mw": round(emb_kw / 1000.0, 3),
            "customers": round(cust),
            "total_der_capacity_mw": round(total_mw, 3),
            "capacity_per_1000_customers_mw": round(total_mw / cust * 1000.0, 4),
        })
        for f in FUEL_TYPES:
            key = fuel_key(f)
            row[f"{key}_facilities"] = round(fuel[f]["facilities"])
            row[f"{key}_capacity_mw"] = round(fuel[f]["capacity_kw"] / 1000.0, 3)
        adoption_rows.append(row)

    with open(PROCESSED / "ldc_adoption.csv", "w", newline="",
              encoding="utf-8") as handle:
        fieldnames = ["licence_no", "utility_name", "data_year", "vintage",
                      "rrr_name", "match_type", "reported", "multi_zone",
                      "customers", "netmetered_customers",
                      "netmetered_capacity_mw", "embedded_facilities",
                      "embedded_capacity_mw", "total_der_capacity_mw",
                      "capacity_per_1000_customers_mw"]
        for f in FUEL_TYPES:
            key = fuel_key(f)
            fieldnames += [f"{key}_facilities", f"{key}_capacity_mw"]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in adoption_rows:
            writer.writerow({k: row.get(k, "") for k in fieldnames})

    # ---- validation-only cross-check against the IESO contract list ----
    contract_mw = load_contract_dx()
    oeb_mw = {f: 0.0 for f in FUEL_TYPES}
    for licence_rows in [adoption_rows]:
        for row in licence_rows:
            if row.get("reported") != "True":
                continue
            for f in FUEL_TYPES:
                oeb_mw[f] += float(row.get(f"{fuel_key(f)}_capacity_mw") or 0)
    cross_rows = []
    for f in FUEL_TYPES:
        oeb, con = oeb_mw[f], contract_mw.get(f, 0.0)
        ratio = con / oeb if oeb else None
        discrepancy = (ratio is not None and abs(ratio - 1.0) > 0.20)
        cross_rows.append({
            "fuel_type": f,
            "oeb_embedded_capacity_mw": round(oeb, 1),
            "ieso_dx_contracted_mw": round(con, 1),
            "contract_over_oeb_ratio": round(ratio, 3) if ratio is not None else "",
            "discrepancy_over_20pct": str(discrepancy),
            "note": ("sources are different universes (OEB: distributor-"
                     "connected embedded; IESO: contracted Dx); never blended"),
        })
    with open(PROCESSED / "adoption_crosscheck.csv", "w", newline="",
              encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(cross_rows[0].keys()))
        writer.writeheader()
        writer.writerows(cross_rows)

    qa_log_module.log_rows(_QA_ENTRIES)
    _QA_ENTRIES.clear()

    # ---- checkpoint numbers ----
    reported = [r for r in adoption_rows if r.get("reported") == "True"]
    metric = [(r["licence_no"], r["utility_name"],
               r["capacity_per_1000_customers_mw"]) for r in reported
              if not r.get("multi_zone") == "True"]
    metric.sort(key=lambda x: x[2])
    print(f"2B: {len(reported)} LDCs with data, "
          f"{len(adoption_rows) - len(reported)} excluded/unreported, "
          f"data_year={data_year}")
    print("  bottom 5 per-1,000:", [(l, v) for l, _, v in metric[:5]])
    print("  top 5 per-1,000:", [(l, v) for l, _, v in metric[-5:]])
    flags = [r for r in cross_rows if r["discrepancy_over_20pct"] == "True"]
    print(f"  cross-check discrepancies over 20%: {len(flags)}")
    for r in flags:
        print(f"    {r['fuel_type']}: OEB {r['oeb_embedded_capacity_mw']} MW vs "
              f"contract {r['ieso_dx_contracted_mw']} MW "
              f"(ratio {r['contract_over_oeb_ratio']})")


if __name__ == "__main__":
    main()
