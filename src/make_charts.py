"""Build the four Phase 3 chart pages as standalone Plotly HTML files.

Outputs (docs/charts/):
  page1_value_wedge.html      Net metering value wedge (summer preview)
  page2_adoption.html         DER adoption baseline (choropleth)
  page3_nodal_signal.html     Wholesale locational signal (node level)
  page4_adoption_vs_signal.html  Adoption vs wholesale signal (owner-gated)

Standalone HTML, Plotly JS from CDN (no tokens, no API keys). Each page
carries a caption with source, data window, as-of date and caveat text.
div_id values are fixed so reruns are byte-identical. No analytical
commentary is written anywhere: interpretation placeholders read
[ANALYST COMMENTARY - TO BE WRITTEN BY AUTHOR].
"""
import json
from datetime import timezone
from pathlib import Path

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


def c_per_kwh(dollars_per_mwh):
    """$/MWh -> Canadian cents/kWh."""
    return dollars_per_mwh / 10.0


def page_shell(title, heading, status_html, body_html, caption_html):
    """Wrap a chart body in a consistent page with status card and caption."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
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


def write_page(path, title, heading, status_html, body_html, caption_html):
    CHARTS.mkdir(parents=True, exist_ok=True)
    html = page_shell(title, heading, status_html, body_html, caption_html)
    path.write_text(html, encoding="utf-8")
    print(f"wrote {path.name} ({path.stat().st_size / 1e6:.2f} MB)")


def chart_div(fig, div_id):
    return pio.to_html(fig, include_plotlyjs="cdn", full_html=False,
                       div_id=div_id)


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

    layers = {"nm": ("Net metering (Table 1)", "nm_per_1000",
                     "Net-metered MW per 1,000 customers")}
    for fuel in FUELS:
        layers[fuel] = (f"Embedded: {fuel} (Table 3)", f"{fuel}_per_1000",
                        f"Embedded {fuel} MW per 1,000 customers")

    def layer_arrays(key):
        col = layers[key][1]
        vals = [rep.loc[l, col] for l in main_lic]
        return vals, [hover_main(l, key) for l in main_lic]

    vals, hovers = layer_arrays("nm")
    zmin, zmax = min(vals), max(vals)

    fig = go.Figure()
    fig.add_trace(go.Choropleth(
        geojson=geo, locations=main_lic, z=vals,
        colorscale="YlGn", zmin=zmin, zmax=zmax,
        colorbar_title="MW / 1,000 customers",
        customdata=hovers, hovertemplate="%{customdata}<extra></extra>",
        name="reported LDCs", marker_line_width=0.5))
    if hz_lic:
        hz_hover = [f"{rep.loc[l, 'utility_name']}<br>Multi-zone/residual: "
                    f"kept out of colour-scale bounds"
                    for l in hz_lic]
        fig.add_trace(go.Choropleth(
            geojson=geo, locations=hz_lic, z=[1] * len(hz_lic),
            colorscale=[[0, "#b0b0b0"], [1, "#b0b0b0"]], showscale=False,
            customdata=hz_hover, hovertemplate="%{customdata}<extra></extra>",
            name="multi-zone (excluded from scale)", marker_line_width=0.5))
    if excl_lic:
        names = {l: adoption.set_index("licence_no").loc[l, "utility_name"]
                 for l in excl_lic}
        fig.add_trace(go.Choropleth(
            geojson=geo, locations=excl_lic, z=[1] * len(excl_lic),
            colorscale=[[0, "#d9d9d9"], [1, "#d9d9d9"]], showscale=False,
            customdata=[f"{names[l]}<br>Not reported in RRR 2.1.2"
                        for l in excl_lic],
            hovertemplate="%{customdata}<extra></extra>",
            name="not reported", marker_line_width=0.5))
    buttons = []
    for key, (label, _col, cbar) in layers.items():
        v, h = layer_arrays(key)
        buttons.append(dict(
            label=label, method="restyle",
            args=[{"z": [v], "customdata": [h],
                   "zmin": [min(v)], "zmax": [max(v)],
                   "colorbar.title.text": [cbar]}, [0]]))
    fig.update_layout(
        title="DER adoption baseline — Table 1 and Table 3 stay separate",
        geo=dict(scope="north america", center=dict(lat=46.5, lon=-80.5),
                 projection_scale=28, showland=False),
        updatemenus=[dict(type="dropdown", x=0.0, y=1.02, showactive=True,
                          buttons=buttons)],
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
        f"selectable layer; the two are never combined. "
        f"Polygons are indicative, not legal boundaries; polygon provenance "
        f"and licence to be confirmed by owner before public release. "
        f"{ANALYST}")
    body = (f"{div}<p class='note'>LDCs not reported in RRR 2.1.2:</p>"
            f"<ul class='note'>{excl_list}</ul>")
    write_page(CHARTS / "page2_adoption.html", "DER adoption baseline",
               "DER adoption baseline", status, body, caption)
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

    metrics = {"mean_congestion": "30-day mean congestion ($/MWh)",
               "mean_lmp": "30-day mean LMP ($/MWh)",
               "mean_loss": "30-day mean loss ($/MWh)"}
    bounds = {m: float(nodes[m].abs().max()) for m in metrics}

    def colorbar(m):
        return metrics[m]

    m0 = "mean_congestion"
    fig = go.Figure()
    fig.add_trace(go.Scattergeo(
        lon=nodes["Longitude"], lat=nodes["Latitude"],
        mode="markers",
        marker=dict(color=nodes[m0], colorscale="RdBu_r",
                    cmin=-bounds[m0], cmax=bounds[m0], cmid=0,
                    colorbar_title=colorbar(m0),
                    size=5, opacity=0.8),
        customdata=nodes[["node", "mean_congestion", "mean_lmp",
                          "mean_loss", "n_days"]].values,
        hovertemplate=(
            "%{customdata[0]}<br>congestion: %{customdata[1]:.2f} $/MWh<br>"
            "LMP: %{customdata[2]:.2f} $/MWh<br>"
            "loss: %{customdata[3]:.2f} $/MWh<br>"
            "%{customdata[4]:.0f} days<extra></extra>"),
        name="LMP nodes"))
    buttons = [dict(
        label=metrics[m].split("30-day mean ")[1].capitalize(),
        method="restyle",
        args=[{"marker.color": [nodes[m].tolist()],
               "marker.cmin": [-bounds[m]], "marker.cmax": [bounds[m]],
               "marker.colorbar.title.text": [colorbar(m)]}, [0]])
        for m in metrics]
    fig.update_layout(
        title="Wholesale locational signal — node level",
        geo=dict(scope="north america", center=dict(lat=46.5, lon=-80.5),
                 projection_scale=28, showland=False),
        updatemenus=[dict(type="buttons", direction="right", x=0.0, y=1.02,
                          showactive=True, buttons=buttons)],
    )
    div = chart_div(fig, "page3-nodal-signal")

    status = (f"{n_drawn} of {n_total} LMP nodes drawn "
              f"({n_missing} have no coordinates in ieso_node_locations.csv). "
              f"<b>Zone aggregation withheld:</b> node-to-zone coverage is "
              f"54.2%, below the 90% bar, so no zone-level congestion "
              f"findings are shown.")
    caption = (
        f"Source: IESO day-ahead hourly LMP, 30-day window ending "
        f"2026-10-01; node coordinates from ieso_node_locations.csv "
        f"(user-supplied). Values are per-node means of daily means; "
        f"negatives kept. This is a wholesale locational signal for "
        f"generators and dispatchable resources — not a retail price and "
        f"not a distribution deferral value. {ANALYST}")
    write_page(CHARTS / "page3_nodal_signal.html",
               "Wholesale locational signal (node level)",
               "Wholesale locational signal — node level",
               status, div, caption)
    return {"drawn": n_drawn, "total": n_total}


# ---------------------------------------------------------------------------
# Page 4: Adoption vs wholesale signal (owner-gated)
# ---------------------------------------------------------------------------

def spearman(x, y):
    """Rank correlation without scipy: Pearson on average ranks."""
    rx = pd.Series(x).rank()
    ry = pd.Series(y).rank()
    return float(rx.corr(ry))


def page4():
    review = pd.read_csv(PROCESSED / "crosswalk_review_top20.csv")
    review["owner_status"] = review["owner_status"].fillna("").str.strip()
    approved = review[review["owner_status"].str.lower() == "approved"]
    n = len(approved)

    caption_base = (
        "x-axis: LDC-level wholesale signal = mean 30-day congestion of "
        "the LMP node(s) inside the LDC polygon, else the nearest node "
        "within 30 km (EPSG:3161). Multi-zone/residual LDCs (Hydro One) are "
        "excluded from the correlation. y-axis: net-metered capacity per "
        "1,000 customers (OEB RRR 2.1.2 Table 1 only). Review assignments "
        "are not findings before owner approval. No causal claim is made. "
        f"{ANALYST}")

    if n == 0:
        rows = "".join(
            f"<tr><td>{r['licence_no']}</td><td>{r['utility_name']}</td>"
            f"<td>{r['n_nodes']}</td><td>{r['method']}</td>"
            f"<td>{r['mean_congestion']:.3f}</td></tr>"
            for _, r in review.iterrows())
        body = (f"<div class='card'><strong>Awaiting owner review.</strong> "
                f"No rows have owner_status = approved, so no chart is "
                f"drawn. n = 0 approved rows.</div>"
                f"<p><b>Rows under owner review (not findings):</b></p>"
                f"<table class='preview'><tr><th>Licence</th><th>LDC</th>"
                f"<th>Nodes</th><th>Method</th><th>Mean congestion "
                f"$/MWh</th></tr>{rows}</table>")
        status = ("0 approved rows — awaiting owner review. "
                  "Unreviewed rows are never plotted as findings.")
        write_page(CHARTS / "page4_adoption_vs_signal.html",
                   "Adoption vs wholesale signal",
                   "Adoption vs wholesale signal", status, body,
                   caption_base)
        return {"approved": 0}

    signal = pd.read_csv(PROCESSED / "ldc_node_signal.csv")
    adoption = pd.read_csv(PROCESSED / "ldc_adoption.csv")
    adoption["nm_per_1000"] = (
        adoption["netmetered_capacity_mw"] / adoption["customers"] * 1000.0)
    df = (approved[["licence_no", "utility_name", "customers",
                    "owner_status"]]
          .merge(signal, on=["licence_no", "utility_name"])
          .merge(adoption[["licence_no", "nm_per_1000"]], on="licence_no"))
    df = df[~df["exclude_from_correlation"]]
    df = df.dropna(subset=["mean_congestion", "nm_per_1000"])
    n = len(df)

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=df["mean_congestion"], y=df["nm_per_1000"], mode="markers+text",
        text=df["utility_name"], textposition="top center",
        customdata=df[["licence_no", "n_nodes", "method"]].values,
        hovertemplate=(
            "%{text}<br>signal: %{x:.3f} $/MWh<br>"
            "net-metered: %{y:.3f} MW/1000 customers<br>"
            "%{customdata[1]:.0f} node(s), %{customdata[2]}<extra></extra>"),
        name="approved LDCs"))
    corr_text = ""
    if n >= 8:
        rho = spearman(df["mean_congestion"], df["nm_per_1000"])
        corr_text = f" Spearman rho = {rho:.2f} (n = {n})."
    fig.update_layout(
        title=f"Adoption vs wholesale signal — {n} approved LDCs",
        xaxis_title="LDC wholesale signal: mean 30-day congestion ($/MWh)",
        yaxis_title="Net-metered MW per 1,000 customers (Table 1)")
    div = chart_div(fig, "page4-scatter")

    status = f"{n} approved rows plotted.{corr_text}"
    write_page(CHARTS / "page4_adoption_vs_signal.html",
               "Adoption vs wholesale signal",
               "Adoption vs wholesale signal", status, div, caption_base)
    return {"approved": n}


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
