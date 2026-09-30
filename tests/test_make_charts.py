"""Tests for src/make_charts.py: smoke tests per page plus state tests.

Charts are built once per test module run into the real docs/charts/
(the builder is deterministic, so this is also the rerun-identical
check's baseline). Fixture-based tests monkeypatch PROCESSED.
"""
import hashlib
import re
from pathlib import Path
import sys

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import make_charts as mc  # noqa: E402

CHARTS = Path(__file__).resolve().parent.parent / "docs" / "charts"
PAGES = {
    "page1_value_wedge.html": "page1-avg-day",
    "page2_adoption.html": "page2-adoption",
    "page3_nodal_signal.html": "page3-nodal-signal",
    "page4_adoption_vs_signal.html": None,  # awaiting state has no figure
}


@pytest.fixture(scope="module")
def built():
    mc.main()
    return {name: (CHARTS / name).read_text(encoding="utf-8")
            for name in PAGES}


def test_all_pages_exist_and_under_5mb(built):
    for name in PAGES:
        p = CHARTS / name
        assert p.exists(), name
        assert p.stat().st_size < 5 * 1024 * 1024, name


def test_expected_div_ids(built):
    for name, div_id in PAGES.items():
        if div_id:
            assert f'id="{div_id}"' in built[name], name


def test_no_nan_infinity_or_tokens(built):
    for name, html in built.items():
        assert "NaN" not in html, name
        assert "Infinity" not in html, name
        assert "pk.eyJ" not in html, name  # mapbox public token prefix
        assert "mapbox" not in html.lower(), name
        if PAGES[name]:  # pages with a figure load Plotly from CDN
            assert "cdn.plot.ly" in html, name


def test_page1_preview_label_and_no_headline(built):
    html = built["page1_value_wedge.html"]
    assert "Summer preview" in html
    assert "not annual" in html
    assert "Annual headline unavailable" in html
    assert "2.5 months" in html
    assert "gap %" not in html
    assert "Plan: TOU" in html and "Plan: ULO" in html


def test_page2_layers_kept_separate(built):
    html = built["page2_adoption.html"]
    assert "Net metering (Table 1)" in html
    assert "Embedded: solar (Table 3)" in html
    assert "never combined" in html
    assert "not reported" in html
    assert "Multi-zone" in html


def test_page3_states_withheld_aggregation(built):
    html = built["page3_nodal_signal.html"]
    assert "withheld" in html.lower()
    assert "54.2%" in html
    assert "90%" in html
    assert "1,022 of 1,055" in html or "1022 of 1055" in html


def test_page4_awaiting_review_with_zero_approved(built):
    html = built["page4_adoption_vs_signal.html"]
    assert "Awaiting owner review" in html
    assert "n = 0 approved rows" in html
    assert "not findings" in html


def _fixture_processed(tmp_path, n_approved):
    proc = tmp_path / "processed"
    proc.mkdir()
    rows = []
    for i in range(10):
        rows.append({
            "licence_no": f"ED-{i:04d}", "utility_name": f"LDC {i}",
            "customers": 1000.0 + i, "node_names": f"N{i}", "n_nodes": 2,
            "method": "in_polygon", "distance_km": 0.0,
            "mean_congestion": float(i) - 4.5,
            "owner_status": "approved" if i < n_approved else "",
        })
    pd.DataFrame(rows).to_csv(proc / "crosswalk_review_top20.csv",
                              index=False)
    sig = pd.DataFrame([{
        "licence_no": r["licence_no"], "utility_name": r["utility_name"],
        "n_nodes": 2, "node_names": r["node_names"],
        "mean_congestion": r["mean_congestion"], "mean_lmp": 50.0,
        "mean_loss": 0.5, "method": "in_polygon", "distance_km": 0.0,
        "exclude_from_correlation": False} for r in rows])
    sig.to_csv(proc / "ldc_node_signal.csv", index=False)
    adop = pd.DataFrame([{
        "licence_no": r["licence_no"], "utility_name": r["utility_name"],
        "customers": r["customers"],
        "netmetered_capacity_mw": 0.5 + 0.1 * i}
        for i, r in enumerate(rows)])
    adop.to_csv(proc / "ldc_adoption.csv", index=False)
    return proc


def test_page4_fixture_with_approved_rows_shows_chart(monkeypatch, tmp_path):
    proc = _fixture_processed(tmp_path, n_approved=8)
    monkeypatch.setattr(mc, "PROCESSED", proc)
    monkeypatch.setattr(mc, "CHARTS", tmp_path / "charts")
    result = mc.page4()
    assert result["approved"] == 8
    html = (tmp_path / "charts" / "page4_adoption_vs_signal.html"
            ).read_text(encoding="utf-8")
    assert "8 approved LDCs" in html
    assert "Spearman rho" in html  # n >= 8


def test_page4_fixture_below_8_omits_spearman(monkeypatch, tmp_path):
    proc = _fixture_processed(tmp_path, n_approved=3)
    monkeypatch.setattr(mc, "PROCESSED", proc)
    monkeypatch.setattr(mc, "CHARTS", tmp_path / "charts")
    result = mc.page4()
    assert result["approved"] == 3
    html = (tmp_path / "charts" / "page4_adoption_vs_signal.html"
            ).read_text(encoding="utf-8")
    assert "Spearman rho" not in html


def test_reruns_are_byte_identical(tmp_path):
    mc.main()
    first = {n: hashlib.sha256((CHARTS / n).read_bytes()).hexdigest()
             for n in PAGES}
    mc.main()
    for n in PAGES:
        h = hashlib.sha256((CHARTS / n).read_bytes()).hexdigest()
        assert h == first[n], f"{n} changed between reruns"
