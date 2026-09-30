"""Tests for src/reconcile_ldcs.py: conservative name matching."""
import csv
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from reconcile_ldcs import match_name, norm, strip_parenthetical, REMOTE_LICENCES

PROCESSED = Path(__file__).resolve().parent.parent / "data" / "processed"


def _dicts():
    current = {"Toronto Hydro-Electric System Limited",
               "GrandBridge Energy Inc.",
               "North Bay Hydro Distribution Limited"}
    historical = {"Espanola Regional Hydro Distribution Corporation":
                  "North Bay Hydro Distribution Limited"}
    return {norm(c): c for c in current}, {norm(h): c for h, c in historical.items()}


def test_match_exact_current():
    cur, hist = _dicts()
    name, kind = match_name("Toronto Hydro-Electric System Limited", cur, hist)
    assert (name, kind) == ("Toronto Hydro-Electric System Limited",
                           "exact_current")


def test_match_ignores_case_and_whitespace():
    cur, hist = _dicts()
    name, kind = match_name("  toronto HYDRO-electric system limited ", cur, hist)
    assert kind == "exact_current"


def test_match_strips_formerly_parenthetical():
    cur, hist = _dicts()
    name, kind = match_name("GrandBridge Energy Inc. (Formerly Energy+ Inc.)",
                            cur, hist)
    assert (name, kind) == ("GrandBridge Energy Inc.", "parenthetical_stripped")


def test_match_historical_name():
    cur, hist = _dicts()
    name, kind = match_name("Espanola Regional Hydro Distribution Corporation",
                            cur, hist)
    assert (name, kind) == ("North Bay Hydro Distribution Limited",
                           "exact_historical")


def test_match_rejects_fuzzy():
    cur, hist = _dicts()
    name, kind = match_name("Toronto Hydro", cur, hist)
    assert (name, kind) == (None, "unmatched")


def test_strip_parenthetical_only_trailing():
    assert strip_parenthetical("GrandBridge Energy Inc. (Formerly X)") == \
        "GrandBridge Energy Inc."
    assert strip_parenthetical("No Parens Here") == "No Parens Here"


def _read_csv(name):
    with open(PROCESSED / name, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def test_reconciliation_outputs_exist_with_expected_shape():
    recon = PROCESSED / "ldc_reconciliation.csv"
    review = PROCESSED / "ldc_name_map_review.csv"
    if not recon.exists():
        pytest.skip("reconciliation not run yet; run src/reconcile_ldcs.py first")
    rows = _read_csv("ldc_reconciliation.csv")
    assert len(rows) == 60
    matched = [r for r in rows if r["match_type"] != "unmatched"]
    unmatched = [r for r in rows if r["match_type"] == "unmatched"]
    assert len(matched) == 54
    assert len(unmatched) == 6
    # No fuzzy match type may ever appear.
    assert {r["match_type"] for r in rows} <= {
        "exact_current", "exact_historical", "parenthetical_stripped",
        "unmatched"}
    # Remote flags land on the four confirmed non-filers.
    remote = {r["licence_no"] for r in rows if r["remote"] == "True"}
    assert remote == REMOTE_LICENCES
    # Hydro One Networks is flagged multi-zone.
    hon = [r for r in rows if r["licence_no"] == "ED-2003-0043"]
    assert hon and hon[0]["multi_zone"] == "True"

    review_rows = _read_csv("ldc_name_map_review.csv")
    assert len(review_rows) == 6
    assert all(r["action_required"] == "owner decision" for r in review_rows)
