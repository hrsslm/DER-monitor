"""Build the four Phase 3 chart pages as standalone Plotly HTML files.

Outputs (docs/charts/):
  page1_value_wedge.html      Net metering value wedge (summer preview)
  page2_adoption.html         DER adoption baseline (choropleth)
  page3_nodal_signal.html     Wholesale locational signal (node level)
  page4_adoption_vs_signal.html  Adoption vs wholesale signal (owner-gated)

Standalone HTML, Plotly JS from CDN (no tokens, no API keys). Pages 2 and 3
use Plotly's MapLibre traces (choroplethmap / scattermap, Plotly 5.24+)
with the carto-darkmatter basemap; maplibre-gl JS/CSS load from CDN and no
map token is needed. Each page carries a caption with source, data window,
as-of date and caveat text. div_id values are fixed so reruns are
byte-identical. No analytical commentary is written anywhere:
interpretation placeholders read [ANALYST COMMENTARY - TO BE WRITTEN BY AUTHOR].
"""
import json
from datetime import timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import plotly.io as pio

ROOT = Path(__file__).resolve().parent.parent
PROCESSED = ROOT / "data" / "processed"
INPUTS = ROOT / "data" / "inputs"
DOCS_DATA = ROOT / "docs" / "data"
CHARTS = ROOT / "docs" / "charts"

PREVIEW_LABEL = "Summer preview, not annual"
ANALYST = "[ANALYST COMMENTARY - TO BE WRITTEN BY AUTHOR]"

# --- MapLibre basemap (Pages 2 and 3) --------------------------------------
# Plotly's scattermap/choroplethmap traces (Plotly 5.24+) render with
# maplibre-gl, loaded from CDN; carto-darkmatter needs no token and keeps
# its built-in OSM/CARTO attribution.
MAPLIBRE_VERSION = "5.23.0"
MAPLIBRE_JS = ("https://cdn.jsdelivr.net/npm/maplibre-gl@"
               f"{MAPLIBRE_VERSION}/dist/maplibre-gl.js")
MAPLIBRE_CSS = ("https://cdn.jsdelivr.net/npm/maplibre-gl@"
                f"{MAPLIBRE_VERSION}/dist/maplibre-gl.css")
MAP_STYLE = "carto-darkmatter"
ONTARIO_VIEW = {"lat": 46.5, "lon": -82.0, "zoom": 4.8}
SOUTHERN_ONTARIO_VIEW = {"lat": 43.7, "lon": -79.4, "zoom": 7.0}
DIVERGING = "RdBu"  # diverging scale; centred at zero when data span it


def clip_range(values):
    """2nd/98th percentile colour range for a metric.

    Returns (lo, hi); guards degenerate input so the scale never collapses.
    """
    vals = np.asarray(list(values), dtype=float)
    vals = vals[np.isfinite(vals)]
    if not len(vals):
        return 0.0, 1.0
    lo, hi = (float(v) for v in np.percentile(vals, [2, 98]))
    if not (np.isfinite(lo) and np.isfinite(hi)) or hi <= lo:
        lo, hi = float(vals.min()), float(vals.max())
        if hi <= lo:
            hi = lo + 1e-6
    return lo, hi


def centred_mid(lo, hi):
    """zmid/cmid: 0 when the range straddles zero, else None (sequential)."""
    return 0 if lo < 0 < hi else None


def base_map_layout():
    """Shared MapLibre layout: carto-darkmatter, centred on Ontario."""
    return dict(style=MAP_STYLE,
                center=dict(lat=ONTARIO_VIEW["lat"], lon=ONTARIO_VIEW["lon"]),
                zoom=ONTARIO_VIEW["zoom"])


# Button rows sit just above the map frame; the page shell already
# renders the <h1>, so the figures carry no Plotly title of their own
# (avoids title/button overlap). The metric toggle gets its own row;
# the zoom buttons sit on a second row so long metric lists (Page 2
# has nine) never overflow or collide.
BUTTONS_Y_METRIC = 1.12
BUTTONS_Y_ZOOM = 1.05


def zoom_menu():
    """Ontario / Southern Ontario view buttons (second row, top-right)."""
    def view(v):
        return {"map.center.lat": v["lat"], "map.center.lon": v["lon"],
                "map.zoom": v["zoom"]}
    return dict(type="buttons", direction="right", x=1.0, xanchor="right",
                y=BUTTONS_Y_ZOOM, yanchor="bottom", showactive=False,
                buttons=[
                    dict(label="Ontario", method="relayout",
                         args=[view(ONTARIO_VIEW)]),
                    dict(label="Southern Ontario", method="relayout",
                         args=[view(SOUTHERN_ONTARIO_VIEW)]),
                ])


def metric_menu(buttons):
    """Metric toggle (first row, top-left, above the map)."""
    return dict(type="buttons", direction="right", x=0.0, xanchor="left",
                y=BUTTONS_Y_METRIC, yanchor="bottom", showactive=True,
                buttons=buttons)


def map_margins():
    """Room for the two button rows above and the colourbar at right."""
    return dict(l=10, r=110, t=110, b=10)


def map_layout_extra():
    """Shared sizing: fill the container width, fixed height."""
    return dict(autosize=True, height=640)


def c_per_kwh(dollars_per_mwh):
    """$/MWh -> Canadian cents/kWh."""
    return dollars_per_mwh / 10.0


def page_shell(title, heading, status_html, body_html, caption_html,
               maplibre=False):
    """Wrap a chart body in a consistent page with status card and caption."""
    maplibre_head = ""
    if maplibre:
        maplibre_head = (
            f'<link href="{MAPLIBRE_CSS}" rel="stylesheet">\n'
            f'<script src="{MAPLIBRE_JS}"></script>\n'
            # The zoom buttons sit below the metric row; drop the Plotly
            # modebar below both rows so nothing overlaps.
            '<style>.js-plotly-plot .modebar { top: 92px !important; }</style>\n')
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
{maplibre_head}<title>{title}</title>
<style>
body {{ font-family: system-ui, -apple-system, sans-serif; margin: 0 auto;
       max-width: 1100px; padding: 1.5rem; color: #1a1a1a; }}
h1 {{ font-size: 1.4rem; }}
.card {{ border: 1px solid #ccc; border-radius: 8px; padding: 1rem 1.25rem;
        margin: 1rem 0; background: #fafafa; }}
.card strong {{ font-size: 1.05rem; }}
.caption {{ font-size: 0.85rem; color: #444; border-top: 1px solid #ddd;
           margin-top: 1.25rem; padding-top: 0.75rem; }}
table.preview {{ border-collapse: collapse; font-size: 0.85rem; margin: 1rem 0; }}
table.preview th, table.preview td {{ border: 1px solid #ccc; padding: 0.35rem 0.7rem;
        text-align: right; }}
table.preview th {{ background: #f0f0f0; }}
.note {{ font-size: 0.9rem; color: #555; }}
</style>
</head>
<body>
<h1>{heading}</h1>
<div class="card"><strong>Status</strong><br>{status_html}</div>
{body_html}
<div class="caption">{caption_html}</div>
</body>
</html>
"""


def write_page(path, title, heading, status_html, body_html, caption_html,
               maplibre=False):
    CHARTS.mkdir(parents=True, exist_ok=True)
    html = page_shell(title, heading, status_html, body_html, caption_html,
                      maplibre=maplibre)
    path.write_text(html, encoding="utf-8")
    print(f"wrote {path.name} ({path.stat().st_size / 1e6:.2f} MB)")


def chart_div(fig, div_id):
    return pio.to_html(fig, include_plotlyjs="cdn", full_html=False,
                       div_id=div_id, config={"responsive": True})


# ---------------------------------------------------------------------------
# Page 1: Net metering value wedge (preview)
# ---------------------------------------------------------------------------

def page1():
    hourly = pd.read_csv(PROCESSED / "value_hourly.csv")
    summary = json.load(open(PROCESSED / "value_summary.json"))
    ts = pd.to_datetime(hourly["timestamp_utc"], utc=True)
    hourly["toronto_hour"] = ts.dt.tz_convert("America/Toronto").dt.hour
    hourly["toronto_date"] = ts.dt.tz_convert("America/Toronto").dt.date
    for col in ("oemp_dollars_per_mwh", "tou_offset_dollars_per_mwh",
                "ulo_offset_dollars_per_mwh"):
        hourly[col.replace("dollars_per_mwh", "c_kwh")] = c_per_kwh(hourly[col])

    window = f'{summary["window_start"][:10]} to {summary["window_end"][:10]}'
    days = (pd.Timestamp(summary["window_end"]) -
            pd.Timestamp(summary["window_start"])).days
    months = round(days / 30.44, 1)
    as_of = summary["as_of"]
    preview_tag = f"Summer preview, {window}, not annual"

    # Average day by Toronto-local hour.
    avg = (hourly.groupby("toronto_hour", as_index=False)
           .agg(oemp=("oemp_c_kwh", "mean"),
                tou=("tou_offset_c_kwh", "mean"),
                ulo=("ulo_offset_c_kwh", "mean"),
                solar_mw=("solar_mw", "mean")))
    # Daily time series.
    daily = (hourly.groupby("toronto_date", as_index=False)
             .agg(oemp=("oemp_c_kwh", "mean"),
                  tou=("tou_offset_c_kwh", "mean"),
                  ulo=("ulo_offset_c_kwh", "mean"),
                  solar_mw=("solar_mw", "mean")))
    daily["toronto_date"] = pd.to_datetime(daily["toronto_date"])

    fig = go.Figure()
    fig.add_trace(go.Scatter(x=avg["toronto_hour"], y=avg["oemp"],
                             mode="lines", name="OEMP (¢/kWh)"))
    fig.add_trace(go.Scatter(x=avg["toronto_hour"], y=avg["tou"],
                             mode="lines", name="TOU commodity offset (¢/kWh)",
                             line_shape="hv"))
    fig.add_trace(go.Scatter(x=avg["toronto_hour"], y=avg["ulo"],
                             mode="lines", name="ULO commodity offset (¢/kWh)",
                             line_shape="hv", visible=False))
    fig.add_trace(go.Scatter(x=avg["toronto_hour"], y=avg["solar_mw"],
                             mode="lines", name="Solar output, MW (right axis)",
                             yaxis="y2", opacity=0.7))
    fig.update_layout(
        title=f"Average day by hour — {preview_tag}",
        xaxis_title="Hour of day (America/Toronto)",
        yaxis_title="¢/kWh",
        yaxis2=dict(title="Solar MW (province-wide, transmission-visible)",
                    overlaying="y", side="right"),
        updatemenus=[dict(
            type="buttons", direction="right", x=0.0, y=1.12,
            showactive=True,
            buttons=[
                dict(label="Plan: TOU",
                     method="update",
                     args=[{"visible": [True, True, False, True]},
                           {"title": f"Average day by hour — {preview_tag}"}]),
                dict(label="Plan: ULO",
                     method="update",
                     args=[{"visible": [True, False, True, True]},
                           {"title": f"Average day by hour — {preview_tag}"}]),
            ])],
    )
    avg_div = chart_div(fig, "page1-avg-day")

    fig2 = go.Figure()
    fig2.add_trace(go.Scatter(x=daily["toronto_date"], y=daily["oemp"],
                              mode="lines", name="OEMP daily mean (¢/kWh)"))
    fig2.add_trace(go.Scatter(x=daily["toronto_date"], y=daily["tou"],
                              mode="lines", name="TOU offset daily mean (¢/kWh)",
                              line_shape="hv"))
    fig2.add_trace(go.Scatter(x=daily["toronto_date"], y=daily["ulo"],
                              mode="lines", name="ULO offset daily mean (¢/kWh)",
                              line_shape="hv", visible=False))
    fig2.add_trace(go.Scatter(x=daily["toronto_date"], y=daily["solar_mw"],
                              mode="lines",
                              name="Solar daily mean, MW (right axis)",
                              yaxis="y2", opacity=0.7))
    fig2.update_layout(
        title=f"Daily time series — {preview_tag}",
        xaxis_title="Date", yaxis_title="¢/kWh",
        yaxis2=dict(title="Solar MW", overlaying="y", side="right"),
        updatemenus=[dict(
            type="buttons", direction="right", x=0.0, y=1.12,
            showactive=True,
            buttons=[
                dict(label="Plan: TOU", method="update",
                     args=[{"visible": [True, True, False, True]}]),
                dict(label="Plan: ULO", method="update",
                     args=[{"visible": [True, False, True, True]}]),
            ])],
    )
    daily_div = chart_div(fig2, "page1-daily")

    # Preview table: average offset vs OEMP by TOU period.
    by_period = (hourly.groupby("tou_period", as_index=False)
                 .agg(offset_c_kwh=("tou_offset_c_kwh", "mean"),
                      oemp_c_kwh=("oemp_c_kwh", "mean"),
                      hours=("tou_period", "size"))
                 .sort_values("tou_period"))
    rows = "".join(
        f"<tr><td>{r['tou_period']}</td><td>{r['offset_c_kwh']:.2f}</td>"
        f"<td>{r['oemp_c_kwh']:.2f}</td><td>{int(r['hours'])}</td></tr>"
        for _, r in by_period.iterrows())
    table = (f"<p><b>Average offset vs OEMP by TOU period (¢/kWh) — "
             f"preview only, {preview_tag}.</b></p>"
             f"<table class='preview'><tr><th>TOU period</th>"
             f"<th>Offset ¢/kWh</th><th>OEMP ¢/kWh</th><th>Hours</th></tr>"
             f"{rows}</table>")

    status_card = (
        f"Annual headline unavailable: {months} months of OEMP-era price "
        f"history; accumulating daily.")
    caption = (
        f"Source: IESO day-ahead Ontario zonal price + LFDA (OEMP = DA-OZP + "
        f"LFDA), IESO fuel mix (solar shape), OEB RPP TOU/ULO commodity "
        f"rates (effective-dated). Data window: {window}. As of: {as_of}. "
        f"{PREVIEW_LABEL}. No headline percentage is computed for a preview. "
        f"Caveats: RPP commodity rates include a Global Adjustment estimate "
        f"and RPP cost recovery, so the wedge is not only an energy-value "
        f"gap; the solar shape is province-wide transmission-visible solar, "
        f"not rooftop net-metered output. {ANALYST}")
    body = f"{avg_div}{daily_div}{table}"
    write_page(CHARTS / "page1_value_wedge.html",
               "Net metering value wedge (preview)",
               f"Net metering value wedge — {preview_tag}",
               status_card, body, caption)
    return {"months": months, "window": window}


# ---------------------------------------------------------------------------
# Page 2: DER adoption baseline
# ---------------------------------------------------------------------------

FUELS = ["solar", "wind", "water", "biomass", "fossil_fuel",
         "exporting_storage", "non_exporting_storage", "other"]


def page2():
    adoption = pd.read_csv(PROCESSED / "ldc_adoption.csv")
    geo = json.load(open(DOCS_DATA / "ldc_service_areas_simplified.geojson"))
    for feat in geo["features"]:
        feat["id"] = feat["properties"]["licence_no"]

    # Table 1 only: net-metered capacity per 1,000 customers. The committed
    # capacity_per_1000_customers_mw column mixes Table 1 + Table 3 and is
    # NOT used here (combined metric not accepted).
    adoption["nm_per_1000"] = (
        adoption["netmetered_capacity_mw"] / adoption["customers"] * 1000.0)
    for fuel in FUELS:
        adoption[f"{fuel}_per_1000"] = (
            adoption[f"{fuel}_capacity_mw"] / adoption["customers"] * 1000.0)

    licences = [f["id"] for f in geo["features"]]
    assert set(licences) == set(adoption["licence_no"]), \
        "geojson/adoption licence mismatch"
    rep = adoption[adoption["reported"]].set_index("licence_no")
    main_lic = [l for l in licences
                if l in rep.index and not rep.loc[l, "multi_zone"]]
    hz_lic = [l for l in licences
              if l in rep.index and rep.loc[l, "multi_zone"]]
    excl_lic = [l for l in licences if l not in rep.index]

    def hover_main(lic, layer):
        r = rep.loc[lic]
        if layer == "nm":
            return (f"{r['utility_name']}<br>Net-metered: "
                    f"{r['netmetered_customers']:.0f} customers, "
                    f"{r['netmetered_capacity_mw']:.2f} MW "
                    f"({r['nm_per_1000']:.3f} MW/1000 customers)<br>"
                    f"Data year {int(r['data_year'])}, vintage {r['vintage']}")
        fac = r[f"{layer}_facilities"]
        cap = r[f"{layer}_capacity_mw"]
        return (f"{r['utility_name']}<br>Embedded {layer}: {fac:.0f} "
                f"facilities, {cap:.2f} MW "
                f"({r[f'{layer}_per_1000']:.3f} MW/1000 customers)<br>"
                f"Data year {int(r['data_year'])}, vintage {r['vintage']}")

    short = {"nm": "Net metering"}
    for fuel in FUELS:
        short[fuel] = fuel.replace("_", " ").title().replace(
            "Non Exporting Storage", "Non-exporting storage")
    layers = {"nm": ("nm_per_1000", "MW/1k customers")}
    for fuel in FUELS:
        layers[fuel] = (f"{fuel}_per_1000", f"{short[fuel]} MW/1k")
    keys = ["nm", *FUELS]

    def layer_arrays(key):
        col = layers[key][0]
        vals = [rep.loc[l, col] for l in main_lic]
        return vals, [hover_main(l, key) for l in main_lic]

    # One choroplethmap trace per metric (own colourbar + short title);
    # the toggle flips visibility. Grey traces stay on in every layer.
    fig = go.Figure()
    for i, key in enumerate(keys):
        vals, hovers = layer_arrays(key)
        lo, hi = clip_range(vals)
        fig.add_trace(go.Choroplethmap(
            geojson=geo, locations=main_lic, z=vals,
            colorscale=DIVERGING, zmin=lo, zmax=hi,
            zmid=centred_mid(lo, hi),
            colorbar=dict(title=layers[key][1], x=1.0, xanchor="left",
                          len=0.75),
            marker=dict(line=dict(width=0.5,
                                   color="rgba(255,255,255,0.45)")),
            customdata=hovers, hovertemplate="%{customdata}<extra></extra>",
            name=short[key], visible=(i == 0)))
    grey_traces = 0
    if hz_lic:
        hz_hover = [f"{rep.loc[l, 'utility_name']}<br>Multi-zone/residual: "
                    f"kept out of colour-scale bounds"
                    for l in hz_lic]
        fig.add_trace(go.Choroplethmap(
            geojson=geo, locations=hz_lic, z=[0.5] * len(hz_lic),
            colorscale=[[0, "#9e9e9e"], [1, "#9e9e9e"]], showscale=False,
            marker=dict(line=dict(width=0.5,
                                   color="rgba(255,255,255,0.45)")),
            customdata=hz_hover, hovertemplate="%{customdata}<extra></extra>",
            name="Multi-zone (excluded from scale)", visible=True))
        grey_traces += 1
    if excl_lic:
        names = {l: adoption.set_index("licence_no").loc[l, "utility_name"]
                 for l in excl_lic}
        fig.add_trace(go.Choroplethmap(
            geojson=geo, locations=excl_lic, z=[0.5] * len(excl_lic),
            colorscale=[[0, "#d9d9d9"], [1, "#d9d9d9"]], showscale=False,
            marker=dict(line=dict(width=0.5,
                                   color="rgba(255,255,255,0.45)")),
            customdata=[f"{names[l]}<br>Not reported in RRR 2.1.2"
                        for l in excl_lic],
            hovertemplate="%{customdata}<extra></extra>",
            name="Not reported", visible=True))
        grey_traces += 1
    n_traces = len(keys) + grey_traces
    buttons = [dict(
        label=short[key], method="update",
        args=[{"visible": ([i == j for j in range(len(keys))]
                            + [True] * grey_traces)},
              list(range(n_traces))])
        for i, key in enumerate(keys)]
    fig.update_layout(
        map=base_map_layout(),
        updatemenus=[metric_menu(buttons), zoom_menu()],
        margin=map_margins(),
        **map_layout_extra(),
    )
    div = chart_div(fig, "page2-adoption")

    excl_names = sorted(
        adoption.set_index("licence_no").loc[l, "utility_name"]
        for l in excl_lic)
    excl_list = "".join(f"<li>{n} — not reported</li>" for n in excl_names)
    vintage = rep["vintage"].iloc[0]
    data_year = int(rep["data_year"].iloc[0])
    status = (f"{len(main_lic)} LDCs drawn in the colour scale; "
              f"{len(hz_lic)} multi-zone LDC(s) drawn grey and kept out of "
              f"colour-scale bounds; {len(excl_lic)} LDCs not reported "
              f"(grey, listed below).")
    caption = (
        f"Source: OEB RRR 2.1.2, vintage {vintage}, data year {data_year}. "
        f"Primary layer: net-metered capacity per 1,000 customers (Table 1 "
        f"only). Embedded generation by fuel type (Table 3) is a separate "
        f"selectable layer; the two are never combined. Colour range is "
        f"clipped to the 2nd-98th percentile per layer. "
        f"Basemap: CARTO dark matter (c) OpenStreetMap contributors "
        f"(c) CARTO. "
        f"Polygons are indicative, not legal boundaries. "
        
        f"{ANALYST}")
    body = (f"{div}<p class='note'>LDCs not reported in RRR 2.1.2:</p>"
            f"<ul class='note'>{excl_list}</ul>")
    write_page(CHARTS / "page2_adoption.html", "DER adoption baseline",
               "DER adoption baseline", status, body, caption, maplibre=True)
    return {"drawn": len(main_lic), "multi_zone": len(hz_lic),
            "excluded": len(excl_lic)}


# ---------------------------------------------------------------------------
# Page 3: Wholesale locational signal (node level)
# ---------------------------------------------------------------------------

def page3():
    cong = pd.read_csv(PROCESSED / "node_congestion_30d.csv")
    loc = pd.read_csv(INPUTS / "ieso_node_locations.csv")
    nodes = cong.merge(loc, left_on="node", right_on="Location", how="inner")
    n_total = len(cong)
    n_drawn = len(nodes)
    n_missing = n_total - n_drawn
    for col in ("mean_congestion", "mean_lmp", "mean_loss"):
        assert nodes[col].notna().all() and \
            ~nodes[col].isin([float("inf"), float("-inf")]).any()

    metrics = {"mean_congestion": ("Congestion", "Congestion $/MWh"),
               "mean_lmp": ("LMP", "LMP $/MWh"),
               "mean_loss": ("Loss", "Loss $/MWh")}
    keys = list(metrics)

    # One scattermap trace per metric (own colourbar + short title);
    # the toggle flips visibility. Diverging scale centred at zero,
    # clipped to the 2nd-98th percentile per metric.
    fig = go.Figure()
    for i, m in enumerate(keys):
        label, cbar = metrics[m]
        lo, hi = clip_range(nodes[m])
        fig.add_trace(go.Scattermap(
            lon=nodes["Longitude"], lat=nodes["Latitude"],
            mode="markers",
            marker=dict(size=6, opacity=0.7, color=nodes[m],
                        colorscale=DIVERGING, cmin=lo, cmax=hi,
                        cmid=centred_mid(lo, hi),
                        colorbar=dict(title=cbar, x=1.0, xanchor="left",
                                      len=0.75)),
            customdata=nodes[["node", "mean_congestion", "mean_lmp",
                              "mean_loss", "n_days"]].values,
            hovertemplate=(
                "%{customdata[0]}<br>congestion: %{customdata[1]:.2f} $/MWh<br>"
                "LMP: %{customdata[2]:.2f} $/MWh<br>"
                "loss: %{customdata[3]:.2f} $/MWh<br>"
                "%{customdata[4]:.0f} days<extra></extra>"),
            name=label, visible=(i == 0)))
    buttons = [dict(
        label=metrics[m][0], method="update",
        args=[{"visible": [k == m for k in keys]}, list(range(len(keys)))])
        for m in keys]
    fig.update_layout(
        map=base_map_layout(),
        updatemenus=[metric_menu(buttons), zoom_menu()],
        margin=map_margins(),
        **map_layout_extra(),
    )
    div = chart_div(fig, "page3-nodal-signal")

    status = (f"{n_drawn} of {n_total} LMP nodes drawn "
              f"({n_missing} have no coordinates in ieso_node_locations.csv). "
              f"<b>Zone aggregation withheld:</b> node-to-zone coverage is "
              f"54.2%, below the 90% bar, so no zone-level congestion "
              f"findings are shown.")
    daily = pd.read_csv(PROCESSED / "lmp_node_daily.csv", usecols=["date"])
    win_start, win_end = daily["date"].min(), daily["date"].max()
    win_days = daily["date"].nunique()
    caption = (
        f"Source: IESO day-ahead hourly LMP, {win_days}-day window "
        f"{win_start} to {win_end}; node coordinates from "
        f"ieso_node_locations.csv (user-supplied). Values are per-node "
        f"means of daily means; negatives kept. Colour range is clipped "
        f"to the 2nd-98th percentile per metric. Basemap: CARTO dark "
        f"matter (c) OpenStreetMap contributors (c) CARTO. "
        f"This is a wholesale locational signal for "
        f"generators and dispatchable resources — not a retail price and "
        f"not a distribution deferral value. {ANALYST}")
    write_page(CHARTS / "page3_nodal_signal.html",
               "Wholesale locational signal (node level)",
               "Wholesale locational signal — node level",
               status, div, caption, maplibre=True)
    return {"drawn": n_drawn, "total": n_total}


# ---------------------------------------------------------------------------
# Page 4: Adoption vs wholesale signal (owner-gated)
# ---------------------------------------------------------------------------

def spearman(x, y):
    """Rank correlation without scipy: Pearson on average ranks."""
    rx = pd.Series(x).rank()
    ry = pd.Series(y).rank()
    return float(rx.corr(ry))


def page4_marker_style(method, heterogeneous):
    """Filled vs hollow marker assignment for Page 4.

    Directive: heterogeneous LDCs and LDCs on borrowed nearest nodes are
    drawn hollow and excluded from the correlation (they would inflate n
    or mix unlike territories).
    """
    if method == "in_polygon" and not heterogeneous:
        return "filled"
    return "hollow"


def page4():
    signal = pd.read_csv(PROCESSED / "ldc_node_signal.csv")
    signal["owner_status"] = signal["owner_status"].fillna("").str.strip()
    approved = signal[signal["owner_status"].str.lower() == "approved"].copy()
    n_approved = len(approved)

    daily = pd.read_csv(PROCESSED / "lmp_node_daily.csv", usecols=["date"])
    dates = pd.to_datetime(daily["date"]).dt.date
    window_end = dates.max().isoformat()
    window_start = (dates.max() - timedelta(days=31)).isoformat()
    window = f"{window_start} to {window_end}"

    x_label = ("Wholesale locational premium: mean day-ahead LMP minus "
               f"OZP ($/MWh), load nodes, {window}")
    caption_base = (
        f"x-axis: {x_label}. The premium is the LDC's equal-weighted mean "
        "over its load nodes of the per-node mean over matched hours of "
        "(DA LMP_h - DA OZP_h); nodes need 15 matched days. The premium is "
        "mostly marginal losses (about 0.97 correlation with the loss "
        "component); congestion is near zero at most nodes in this window. "
        "The window is a roughly 32-day shoulder-season sample and not "
        "annual. y-axis: net-metered capacity per 1,000 customers (OEB RRR "
        "2.1.2 Table 1 only). Hollow markers are approved LDCs excluded "
        "from the correlation (heterogeneous or borrowed nearest node). "
        "No causal claim is made. "
        f"{ANALYST}")

    if n_approved == 0:
        body = ("<div class='card'><strong>No approved LDCs.</strong> "
                "No rows have owner_status = approved, so no chart is "
                "drawn.</div>")
        status = "0 approved rows."
        write_page(CHARTS / "page4_adoption_vs_signal.html",
                   "Where DER is growing vs where wholesale energy is priced "
                   "above or below the provincial average",
                   "Adoption vs wholesale signal", status, body,
                   caption_base)
        return {"approved": 0}

    adoption = pd.read_csv(PROCESSED / "ldc_adoption.csv")
    adoption["nm_per_1000"] = (
        adoption["netmetered_capacity_mw"] / adoption["customers"] * 1000.0)
    df = approved.merge(adoption[["licence_no", "nm_per_1000"]],
                        on="licence_no")
    df = df.dropna(subset=["mean_premium", "nm_per_1000"])
    # Correlation set: filled markers only -- in-polygon, non-heterogeneous
    # approved LDCs. Hollow markers (heterogeneous or borrowed nearest
    # node) are drawn for transparency but excluded from the correlation.
    df["marker"] = [page4_marker_style(m, h)
                    for m, h in zip(df["method"], df["heterogeneous"])]
    corr = df[df["marker"] == "filled"]
    n = len(corr)

    fig = go.Figure()
    for marker, sub in (("filled", df[df["marker"] == "filled"]),
                        ("hollow", df[df["marker"] == "hollow"])):
        if not len(sub):
            continue
        filled = marker == "filled"
        fig.add_trace(go.Scatter(
            x=sub["mean_premium"], y=sub["nm_per_1000"],
            mode="markers+text", text=sub["utility_name"],
            textposition="top center",
            marker=dict(
                color="rgba(31,119,180,1)" if filled else "rgba(0,0,0,0)",
                line=dict(color="rgba(31,119,180,1)", width=2),
                size=10),
            customdata=sub[["licence_no", "n_nodes", "method",
                            "mean_congestion", "mean_loss"]].values,
            hovertemplate=(
                "%{text}<br>premium: %{x:.3f} $/MWh<br>"
                "net-metered: %{y:.3f} MW/1000 customers<br>"
                "%{customdata[1]:.0f} load node(s), %{customdata[2]}<br>"
                "congestion %{customdata[3]:.3f}, loss %{customdata[4]:.3f} "
                "$/MWh (tooltip only)<extra></extra>"),
            name=("Approved LDCs" if filled
                  else "Approved, excluded from correlation")))
    corr_text = ""
    if n >= 8:
        rho = spearman(corr["mean_premium"], corr["nm_per_1000"])
        corr_text = f" Spearman rho = {rho:.2f} (n = {n})."
    fig.update_layout(
        title=("Where DER is growing vs where wholesale energy is priced "
               f"above or below the provincial average (n = {n})"),
        xaxis_title=x_label,
        yaxis_title="Net-metered MW per 1,000 customers (Table 1)")
    div = chart_div(fig, "page4-scatter")

    status = (f"{n_approved} approved LDCs plotted "
              f"({n} in correlation).{corr_text}")
    write_page(CHARTS / "page4_adoption_vs_signal.html",
               "Where DER is growing vs where wholesale energy is priced "
               "above or below the provincial average",
               "Adoption vs wholesale signal", status, div, caption_base)
    return {"approved": n_approved, "n_correlation": n,
            "spearman": corr_text}


def main():
    p1 = page1()
    p2 = page2()
    p3 = page3()
    p4 = page4()
    print("charts complete:",
          {"page1_months": p1["months"], "page2": p2,
           "page3": p3, "page4": p4})


if __name__ == "__main__":
    main()
