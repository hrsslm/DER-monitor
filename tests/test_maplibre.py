"""Smoke tests for the MapLibre-based chart pages (2 and 3).

Pages 2 and 3 must use Plotly's MapLibre traces (choroplethmap /
scattermap, Plotly 5.24+) with the carto-darkmatter basemap. No chart may
use the old scatter_geo traces or need a map token.
"""
import json
import re
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import make_charts as mc  # noqa: E402

CHARTS = Path(__file__).resolve().parent.parent / "docs" / "charts"
MAP_PAGES = ["page2_adoption.html", "page3_nodal_signal.html"]
ALL_PAGES = MAP_PAGES + ["page1_value_wedge.html",
                         "page4_adoption_vs_signal.html"]
DIV_IDS = {"page2_adoption.html": "page2-adoption",
           "page3_nodal_signal.html": "page3-nodal-signal",
           "page1_value_wedge.html": "page1-avg-day",
           "page4_adoption_vs_signal.html": "page4-scatter"}


def fig_traces(html, div_id):
    """Trace dicts from the figure's data array (not template defaults)."""
    start = html.find("Plotly.newPlot(")
    assert start != -1, div_id
    assert f'"{div_id}"' in html[start:start + 200], div_id
    i = html.find("[", start)
    depth, end = 0, i
    instr, esc = False, False
    for end in range(i, len(html)):
        ch = html[end]
        if instr:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                instr = False
        elif ch == '"':
            instr = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                break
    return json.loads(html[i:end + 1])


@pytest.fixture(scope="module")
def html():
    mc.main()
    return {n: (CHARTS / n).read_text(encoding="utf-8") for n in ALL_PAGES}


def trace_types(html):
    """All trace types actually drawn, across every chart page."""
    types = set()
    for name, text in html.items():
        div_id = DIV_IDS[name]
        if f'Plotly.newPlot("{div_id}"' not in text:
            continue  # page with no figure (e.g. awaiting-owner state)
        types.update(t.get("type") for t in fig_traces(text, div_id))
    return types


def test_no_scattergeo_anywhere(html):
    types = trace_types(html)
    assert "scattergeo" not in types, types
    assert not any("scatter_geo" in str(t) for t in types)


def test_no_token_needed_anywhere(html):
    for name, text in html.items():
        low = text.lower()
        assert "accesstoken" not in low, name
        assert "pk.eyJ" not in text, name  # mapbox public token prefix
        assert "api_key=" not in low and "apikey=" not in low, name


def test_maplibre_trace_types(html):
    p2 = {t.get("type") for t in
          fig_traces(html["page2_adoption.html"], DIV_IDS["page2_adoption.html"])}
    p3 = {t.get("type") for t in
          fig_traces(html["page3_nodal_signal.html"], DIV_IDS["page3_nodal_signal.html"])}
    assert p2 == {"choroplethmap"}, p2
    assert p3 == {"scattermap"}, p3


def test_carto_darkmatter_default(html):
    for name in MAP_PAGES:
        assert "carto-darkmatter" in html[name], name


def test_ontario_default_view(html):
    for name in MAP_PAGES:
        compact = html[name].replace(" ", "")
        assert '"lat":46.5' in compact, name
        assert '"lon":-82' in compact, name
        assert '"zoom":4.8' in compact, name


def test_southern_ontario_zoom_button(html):
    for name in MAP_PAGES:
        assert "Southern Ontario" in html[name], name
        compact = html[name].replace(" ", "")
        assert '"map.center.lat":43.7' in compact, name  # zoom target


def test_maplibre_js_only_on_map_pages(html):
    for name in MAP_PAGES:
        assert "maplibre-gl" in html[name], name
    assert "maplibre-gl" not in html["page1_value_wedge.html"]
    assert "maplibre-gl" not in html["page4_adoption_vs_signal.html"]


def test_metric_toggle_buttons_in_one_row(html):
    for name in MAP_PAGES:
        compact = html[name].replace(" ", "")
        assert '"direction":"right"' in compact, name
        # metric toggle on its own row, zoom buttons on a second row
        assert '"y":1.12' in compact, name
        assert '"y":1.05' in compact, name


def test_colorbar_at_right_edge(html):
    for name in MAP_PAGES:
        compact = html[name].replace(" ", "")
        # every metric trace carries a colourbar pinned to the right edge
        assert compact.count('"x":1.0,"xanchor":"left"') >= 1, name


def test_diverging_scale_percentile_clip():
    vals = list(range(101))
    lo, hi = mc.clip_range(vals)
    assert lo == pytest.approx(2.0)
    assert hi == pytest.approx(98.0)
    # centred at zero only when the range straddles it
    assert mc.centred_mid(-2.0, 3.0) == 0
    assert mc.centred_mid(1.0, 5.0) is None
    assert mc.centred_mid(-5.0, -1.0) is None
    # degenerate input never collapses the scale
    lo, hi = mc.clip_range([3.0, 3.0, 3.0])
    assert hi > lo
