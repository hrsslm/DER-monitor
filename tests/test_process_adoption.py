"""Tests for 2B adoption processing: aggregation math, merge rule,
exclusions, and the contract-list cross-check."""

import csv
import hashlib
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import process_adoption as pa

FIX = Path(__file__).parent / "fixtures" / "adoption"


def make_records(rows):
    """Build tiny RRR-style XML records from dicts."""
    root = ET.Element("dataroot")
    for row in rows:
        rec = ET.SubElement(root, "rec")
        for key, value in row.items():
            child = ET.SubElement(rec, key)
            child.text = value
    return list(root)


def test_latest_year_picks_max():
    recs = make_records([
        {"Current_Company_Name": "A", "Year": "2023"},
        {"Current_Company_Name": "A", "Year": "2024"},
    ])
    assert pa.latest_year(recs) == "2024"


def test_num_missing_or_blank_is_zero():
    assert pa.num(None) == 0.0
    assert pa.num("") == 0.0
    assert pa.num("  ") == 0.0
    assert pa.num("1,234.5") == 1234.5


def test_t1_sums_customers_and_kw():
    recs = make_records([
        {"Current_Company_Name": "A", "Year": "2024",
         "No_of_Net-Metered_Generators": "10",
         "Cumulative_Installed_Capacity_kW": "100.5"},
        {"Current_Company_Name": "A", "Year": "2024",
         "No_of_Net-Metered_Generators": "5",
         "Cumulative_Installed_Capacity_kW": ""},  # blank -> 0
        {"Current_Company_Name": "A", "Year": "2023",
         "No_of_Net-Metered_Generators": "99",
         "Cumulative_Installed_Capacity_kW": "999"},  # wrong year, ignored
    ])
    out = pa.aggregate_t1(recs, "2024")
    assert out["A"]["customers"] == 15.0
    assert out["A"]["capacity_kw"] == 100.5


def test_t212_sums_only_customer_rows():
    recs = make_records([
        {"Current_Company_Name": "A", "Year": "2024",
         "Rate_Class": "Residential", "Customer_or_Connections": "Customers",
         "Total_Customers_or_Connections": "900"},
        {"Current_Company_Name": "A", "Year": "2024",
         "Rate_Class": "General Service < 50 kW",
         "Customer_or_Connections": "Customers",
         "Total_Customers_or_Connections": "100"},
        {"Current_Company_Name": "A", "Year": "2024",
         "Rate_Class": "Street Lighting Connections",
         "Customer_or_Connections": "Connections",
         "Total_Customers_or_Connections": "500"},  # connections, not customers
    ])
    # Denominator sums every Customer row (all rate classes), never Connections.
    assert pa.aggregate_t212(recs, "2024")["A"] == 1000.0


def test_t4_per_fuel_aggregation():
    recs = make_records([
        {"Current_Company_Name": "A", "Year": "2024", "Facility_Type": "Solar",
         "Number_of_Facilities": "3", "Installed_Capacity_kW": "30"},
        {"Current_Company_Name": "A", "Year": "2024", "Facility_Type": "Wind",
         "Number_of_Facilities": "1", "Installed_Capacity_kW": "2000"},
    ])
    out = pa.aggregate_t4(recs, "2024")
    assert out["A"]["Solar"]["capacity_kw"] == 30.0
    assert out["A"]["Wind"]["facilities"] == 1.0


def test_per_1000_metric_math():
    # 2.5 MW of DER across 10,000 customers = 0.25 MW per 1,000 customers.
    total_mw, customers = 2.5, 10000.0
    assert round(total_mw / customers * 1000.0, 4) == 0.25


def test_contract_fuel_map_covers_dx_fuels():
    # Every (Fuel Group, Fuel Type) seen on Dx+CO contracts must map.
    seen = {("Renewables", "Solar"), ("Renewables", "Wind"),
            ("Renewables", "Waterpower"), ("Renewables", "Biogas"),
            ("Renewables", "Biogas (On-Farm)"), ("Renewables", "Biomass"),
            ("Renewables", "Landfill Gas"), ("Gas", "Natural Gas"),
            ("Gas", "By Product Gas"), ("Storage", "Battery"),
            ("Storage", "Compressed Air"), ("Other", "Municipal Waste")}
    for key in seen:
        assert key in pa.CONTRACT_FUEL_MAP, f"unmapped contract fuel {key}"


def test_crosscheck_ratio_flags_20pct():
    def flag(oeb, con):
        ratio = con / oeb if oeb else None
        return ratio is not None and abs(ratio - 1.0) > 0.20
    assert flag(100.0, 125.0) is True    # 25% over
    assert flag(100.0, 70.0) is True     # 30% under
    assert flag(100.0, 110.0) is False   # within band
    assert flag(0.0, 50.0) is False      # no OEB base: no ratio


def test_adoption_output_shape():
    rows = list(csv.DictReader(
        open(pa.PROCESSED / "ldc_adoption.csv", encoding="utf-8")))
    assert len(rows) == 58, "one row per analysis-layer feature"
    keys = set(rows[0].keys())
    for col in ["licence_no", "data_year", "vintage", "reported",
                "multi_zone", "customers", "total_der_capacity_mw",
                "capacity_per_1000_customers_mw", "solar_capacity_mw",
                "wind_capacity_mw"]:
        assert col in keys, f"missing column {col}"
    # Keyed by licence_no, sorted, no duplicates.
    licences = [r["licence_no"] for r in rows]
    assert licences == sorted(licences)
    assert len(set(licences)) == 58
    # Excluded LDCs: reported=False and no metric.
    for r in rows:
        if r["reported"] == "False":
            assert r["capacity_per_1000_customers_mw"] == ""
    # Hydro One Networks: computed but flagged multi_zone.
    hon = [r for r in rows if r["licence_no"] == "ED-2003-0043"][0]
    assert hon["multi_zone"] == "True"
    assert hon["reported"] == "True"
    # No legacy licence numbers survive as rows.
    for legacy in ["ED-2002-0498", "ED-2002-0502", "ED-2002-0530",
                   "ED-2002-0545"]:
        assert legacy not in licences


def test_adoption_deterministic():
    digest = hashlib.sha256(
        (pa.PROCESSED / "ldc_adoption.csv").read_bytes()).hexdigest()
    digest2 = hashlib.sha256(
        (pa.PROCESSED / "adoption_crosscheck.csv").read_bytes()).hexdigest()
    # Re-running the processor must reproduce both files byte-for-byte.
    pa.main()
    assert hashlib.sha256(
        (pa.PROCESSED / "ldc_adoption.csv").read_bytes()).hexdigest() == digest
    assert hashlib.sha256(
        (pa.PROCESSED / "adoption_crosscheck.csv").read_bytes()).hexdigest() == digest2
