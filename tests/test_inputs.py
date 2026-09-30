"""Tests for src/inputs.py and src/config.py: input validation and holidays."""
import datetime as dt
from pathlib import Path

import pytest

from config import ontario_rpp_holidays, TIMEZONE_MODE, EST_OFFSET, LFDA_COLUMN_MAP
from inputs import load_tariffs, load_nodes, unmatched_lmp_nodes

INPUTS = Path(__file__).resolve().parent.parent / "data" / "inputs"


def test_timezone_is_est_fixed():
    assert TIMEZONE_MODE == "EST_fixed"
    assert EST_OFFSET == dt.timedelta(hours=-5)


def test_holidays_nine_per_year_with_known_dates():
    holidays = ontario_rpp_holidays(2026)
    assert len(holidays) == 9
    assert dt.date(2026, 1, 1) in holidays      # New Year's Day
    assert dt.date(2026, 2, 16) in holidays     # Family Day (3rd Mon of Feb)
    assert dt.date(2026, 4, 3) in holidays      # Good Friday
    assert dt.date(2026, 5, 18) in holidays     # Victoria Day (Mon before May 25)
    assert dt.date(2026, 7, 1) in holidays      # Canada Day
    assert dt.date(2026, 9, 7) in holidays      # Labour Day (1st Mon of Sep)
    assert dt.date(2026, 10, 12) in holidays    # Thanksgiving (2nd Mon of Oct)
    assert dt.date(2026, 12, 25) in holidays    # Christmas Day
    assert dt.date(2026, 12, 26) in holidays    # Boxing Day


def test_tariffs_load_and_cover_24h():
    tariffs = load_tariffs(INPUTS / "retail_tariffs.csv")
    assert len(tariffs) == 40
    assert set(tariffs["plan"].unique()) == {"TOU", "ULO"}


def test_tariffs_reject_bad_coverage(tmp_path):
    bad = INPUTS / "retail_tariffs.csv"
    text = bad.read_text().splitlines()
    # Drop one row so a block covers fewer than 24 hours.
    text = [line for i, line in enumerate(text) if i != 1]
    p = tmp_path / "tariffs.csv"
    p.write_text("\n".join(text) + "\n")
    with pytest.raises(ValueError, match="not 24"):
        load_tariffs(p)


def test_tariffs_reject_overlapping_terms(tmp_path):
    lines = (INPUTS / "retail_tariffs.csv").read_text().splitlines()
    header, rows = lines[0], lines[1:]
    # Duplicate the first term's rows as a new term starting inside it.
    extra = [r.replace("2024-11-01,2025-10-31", "2025-06-01,2026-05-31")
             for r in rows if r.startswith("2024-11-01")]
    p = tmp_path / "tariffs.csv"
    p.write_text("\n".join([header] + rows + extra) + "\n")
    with pytest.raises(ValueError, match="overlap"):
        load_tariffs(p)


def test_nodes_map_location_to_node_id():
    nodes = load_nodes(INPUTS / "ieso_node_locations.csv")
    assert list(nodes.columns) == ["node_id", "Latitude", "Longitude"]
    assert len(nodes) == 1022
    assert nodes["node_id"].is_unique


def test_nodes_reject_missing_column(tmp_path):
    p = tmp_path / "nodes.csv"
    p.write_text("Longitude,Latitude\n-79.38,43.64\n")
    with pytest.raises(ValueError, match="Location"):
        load_nodes(p)


def test_lfda_column_rename_fixture():
    """Regression: the raw LFDA header 'LFDC Rate ($/MWh)' must map to
    'lfda_rate_mwh' (owner decision 2026-09-29: LFDA == LFDC)."""
    import csv
    fixture = Path(__file__).resolve().parent / "fixtures" / "LFDA_sample.csv"
    with open(fixture, newline="") as handle:
        header = next(csv.reader(handle))
    renamed = [LFDA_COLUMN_MAP.get(col, col) for col in header]
    assert "lfda_rate_mwh" in renamed
    assert "LFDC Rate ($/MWh)" not in renamed
    assert set(LFDA_COLUMN_MAP.values()) == {"date", "hour", "status",
                                             "lfda_rate_mwh"}


def test_unmatched_lmp_nodes_reports_difference():
    nodes = load_nodes(INPUTS / "ieso_node_locations.csv")
    lmp = nodes["node_id"].tolist() + ["NOT_A_REAL_NODE", "ALSO_FAKE"]
    assert unmatched_lmp_nodes(lmp, nodes) == ["ALSO_FAKE", "NOT_A_REAL_NODE"]
