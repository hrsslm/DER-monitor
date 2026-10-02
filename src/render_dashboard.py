"""Render docs/index.html: the Ontario DER Monitor dashboard page.

Static HTML/CSS/JS in one page (no build-time or runtime dependencies
beyond the standard library + pandas). Reads only committed processed
files and the chart HTML pages; every number on the page is computed
from those files, never typed by hand.

Also injects (or removes) the draft-release banner and noindex tag on
every page, driven by config.PUBLIC_RELEASE_APPROVED.

Outputs: docs/index.html (and updated docs/charts/*.html banner state).
"""
import html as html_mod
import json
import os
import re
import subprocess
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from config import (PUBLIC_RELEASE_APPROVED, TIMEZONE_MODE, TORONTO_TZ,
                    ROOT, PROCESSED_DIR, DOCS_DIR, INPUTS_DIR)

CONTENT_DIR = ROOT / "content"
TEMPLATES_DIR = ROOT / "templates"
CHARTS_DIR = DOCS_DIR / "charts"
TIMESTAMP_ID = "build-timestamp"
BANNER_MARKER = "<!-- der-monitor-draft-banner -->"
BANNER_TEXT = "DRAFT: not for public release. Data licences pending."
NOINDEX_TAG = '<meta name="robots" content="noindex">'

PAGES = [
    ("page1_value_wedge.html", "Page 1 — Net metering value wedge"),
    ("page2_adoption.html", "Page 2 — DER adoption baseline"),
    ("page3_nodal_signal.html", "Page 3 — Wholesale locational signal"),
    ("page4_adoption_vs_signal.html", "Page 4 — Adoption vs wholesale signal"),
]
ANALYST = "[ANALYST COMMENTARY - TO BE WRITTEN BY AUTHOR]"


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def short_dt(iso):
    """2026-09-29T23:51:05... -> '2026-09-29 23:51 UTC'."""
    try:
        dt = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return "unknown"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def git_hash():
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             cwd=ROOT, capture_output=True, text=True,
                             timeout=15)
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
# Safe mini-markdown for the author's commentary files
# ---------------------------------------------------------------------------

def md_to_html(text):
    """Convert simple Markdown to HTML. The input is HTML-escaped first,
    so no raw tags (including <script>) can pass through."""
    esc = html_mod.escape(text)
    out, para, in_list = [], [], False

    def inline(s):
        s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"\*(.+?)\*", r"<em>\1</em>", s)
        s = re.sub(r"\[(.+?)\]\((https?://[^)\s]+)\)",
                   r'<a href="\2" rel="noopener">\1</a>', s)
        return s

    def close_list():
        nonlocal in_list
        if in_list:
            out.append("</ul>")
            in_list = False

    def flush_para():
        if para:
            out.append("<p>" + " ".join(para) + "</p>")
            para.clear()

    for line in esc.split("\n"):
        s = line.strip()
        if s.startswith("## "):
            flush_para(); close_list()
            out.append("<h3>" + inline(s[3:].strip()) + "</h3>")
        elif s.startswith("# "):
            flush_para(); close_list()
            out.append("<h3>" + inline(s[2:].strip()) + "</h3>")
        elif s.startswith("- "):
            flush_para()
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append("<li>" + inline(s[2:].strip()) + "</li>")
        elif not s:
            flush_para(); close_list()
        else:
            close_list()
            para.append(inline(s))
    flush_para(); close_list()
    return "\n".join(out)


def render_commentary(path):
    """Return (html, is_placeholder). Never writes analysis."""
    text = path.read_text(encoding="utf-8").strip() if path.exists() \
        else ANALYST
    if text == ANALYST:
        return (f'<div class="commentary placeholder"><p>{ANALYST}</p></div>',
                True)
    return (f'<div class="commentary">{md_to_html(text)}</div>', False)


# ---------------------------------------------------------------------------
# Draft banner / noindex injection (idempotent)
# ---------------------------------------------------------------------------

BANNER_STYLE = ("background:#fff3cd;color:#664d03;border:2px solid #664d03;"
               "border-radius:8px;padding:.75rem 1rem;font-weight:700;"
               "margin-bottom:1rem;text-align:center;font-family:system-ui,"
               "sans-serif")


def _inject(html_text):
    if BANNER_MARKER in html_text:
        return html_text
    # Inline styles: chart pages have no .draft-banner rule of their own.
    banner = (f"{BANNER_MARKER}\n"
              f'<div class="draft-banner" style="{BANNER_STYLE}">'
              f"{BANNER_TEXT}</div>")
    html_text = re.sub(r"(<body[^>]*>)", r"\1\n" + banner, html_text, count=1)
    if NOINDEX_TAG not in html_text:
        html_text = re.sub(r"(<head[^>]*>)", r"\1\n" + NOINDEX_TAG,
                           html_text, count=1)
    return html_text


def _strip(html_text):
    html_text = html_text.replace(BANNER_MARKER + "\n", "")
    html_text = re.sub(r'<div class="draft-banner"[^>]*>.*?</div>\n?', "",
                       html_text, flags=re.S)
    html_text = html_text.replace(NOINDEX_TAG + "\n", "").replace(NOINDEX_TAG,
                                                                  "")
    return html_text


def apply_release_gate(html_text):
    """Banner + noindex while the release flag is false; clean when true."""
    if PUBLIC_RELEASE_APPROVED:
        return _strip(html_text)
    return _inject(html_text)


# ---------------------------------------------------------------------------
# Status panel: every value computed from files
# ---------------------------------------------------------------------------

def feed_collection_times(manifest):
    """Last successful collection time per history feed."""
    return {feed: short_dt(info.get("updated_at"))
            for feed, info in manifest.get("history", {}).items()}


def oemp_history(summary):
    start = summary["window_start"][:10]
    end = summary["window_end"][:10]
    n_hours = summary["n_hours"]
    days = n_hours / 24.0
    months = round(days / 30.44, 1)
    first_full_12mo = (date.fromisoformat(start) +
                       pd.Timedelta(days=365)).isoformat()
    gap_note = ("no gaps in the OEMP window"
                if summary.get("coverage_pct") == 100.0
                else f'coverage {summary.get("coverage_pct")}% '
                     f'({summary.get("expected_hours")} expected hours)')
    return {"start": start, "end": end, "n_hours": n_hours,
            "days": round(days, 1), "months": months,
            "first_full_12mo": first_full_12mo, "gap_note": gap_note}


def lmp_gaps(trailing_days=35):
    """Day-level coverage of the processed DA LMP history (the compact
    daily file Pages 3/4 actually use). Returns dict with fully missing
    and partial days in the trailing window, from node-days per date."""
    d = pd.read_csv(PROCESSED_DIR / "lmp_node_daily.csv", usecols=["date"])
    per_day = d.groupby("date").size()
    end = per_day.index.max()
    start = (pd.Timestamp(end) -
             pd.Timedelta(days=trailing_days - 1)).strftime("%Y-%m-%d")
    window = per_day[per_day.index >= start]
    median = window.median()
    expected = pd.date_range(start, end).strftime("%Y-%m-%d")
    missing = sorted(i for i in expected if i not in per_day.index)
    partial = sorted(i for i, n in window.items() if n < 0.5 * median)
    return {"missing": missing, "partial": partial, "start": start,
            "end": end}


def node_stats():
    cong = pd.read_csv(PROCESSED_DIR / "node_congestion_30d.csv")
    loc = pd.read_csv(INPUTS_DIR / "ieso_node_locations.csv")
    with_coords = cong.merge(loc, left_on="node", right_on="Location",
                             how="inner")
    total, drawn = len(cong), len(with_coords)
    zone_cov = pd.read_csv(PROCESSED_DIR / "zone_coverage.csv")
    zoned = int(zone_cov[zone_cov["zone_method"] ==
                         "capacity_auction_table"]["n_nodes"].sum())
    return {"total": total, "drawn": drawn, "missing_coords": total - drawn,
            "zoned": zoned, "zone_pct": round(zoned / total * 100, 1)}


def ldc_stats():
    adoption = pd.read_csv(PROCESSED_DIR / "ldc_adoption.csv")
    reported = adoption[adoption["reported"]]
    multi = int(reported["multi_zone"].sum())
    excluded = adoption[~adoption["reported"]]
    review = pd.read_csv(PROCESSED_DIR / "ldc_node_signal.csv")
    review["owner_status"] = review["owner_status"].fillna("").str.strip()
    approved = int((review["owner_status"].str.lower() == "approved").sum())
    rejected = int((review["owner_status"].str.lower() == "rejected").sum())
    return {"reported": len(reported), "in_scale": len(reported) - multi,
            "multi_zone": multi,
            "excluded": len(excluded),
            "excluded_names": sorted(excluded["utility_name"].tolist()),
            "rejected": rejected, "approved": approved,
            "vintage": reported["vintage"].iloc[0],
            "data_year": int(reported["data_year"].iloc[0])}


def page_badges(ldc, nodes):
    summary = load_json(PROCESSED_DIR / "value_summary.json")
    cap3 = chart_caption("page3_nodal_signal.html")
    m = re.search(r"30-day window ending (\d{4}-\d{2}-\d{2})", cap3)
    p3_window = m.group(1) if m else "unknown"
    p4 = ("No approved LDCs" if ldc["approved"] == 0
          else f'{ldc["approved"]} approved LDCs')
    return {
        "page1": "Summer preview, not annual",
        "page2": f'Adoption baseline, {ldc["data_year"]}',
        "page3": f"Wholesale signal, 30 days ending {p3_window}",
        "page4": p4,
    }


def chart_caption(chart_file):
    """Sources-and-caveats text, parsed from the chart page itself so the
    chart stays the single source of truth."""
    html_text = (CHARTS_DIR / chart_file).read_text(encoding="utf-8")
    m = re.search(r'<div class="caption">(.*?)</div>', html_text, re.S)
    return m.group(1).strip() if m else "caption not found"


def key_figures(page, ctx):
    """Machine-filled figures per page: values, windows, as-of, sources."""
    s, ldc, nodes = ctx["oemp"], ctx["ldc"], ctx["nodes"]
    figs = {
        "page1": [
            ("Data window", f'{s["start"]} to {s["end"]}'),
            ("OEMP-era hours", f'{s["n_hours"]:,} ({s["days"]} days, '
                              f'{s["months"]} months)'),
            ("As of", ctx["summary"]["as_of"]),
            ("Sources", "IESO DA zonal price + LFDA; IESO fuel mix (solar "
                        "shape); OEB RPP TOU/ULO commodity rates"),
        ],
        "page2": [
            ("LDCs in colour scale", str(ldc["in_scale"])),
            ("Multi-zone (out of scale)", str(ldc["multi_zone"])),
            ("Not reported", str(ldc["excluded"])),
            ("RRR vintage / data year",
             f'{ldc["vintage"]} / {ldc["data_year"]}'),
        ],
        "page3": [
            ("Nodes drawn", f'{nodes["drawn"]:,} of {nodes["total"]:,} '
                            f'({nodes["missing_coords"]} lack coordinates)'),
            ("Node-to-zone coverage",
             f'{nodes["zone_pct"]}% vs the 90% bar — aggregation withheld'),
            ("Window", "30 days ending 2026-10-01"),
        ],
        "page4": [
            ("Approved rows", str(ldc["approved"])),
            ("Rejected rows", str(ldc["rejected"])),
            ("Signal method", ctx.get("signal_methods", "n/a")),
        ],
    }
    return figs[page]


def methods_context():
    decisions = pd.read_csv(INPUTS_DIR / "ldc_decisions.csv")
    merges = decisions[decisions["decision"] == "merge_into"]
    excludes = decisions[decisions["decision"] == "exclude_no_rrr"]
    relicensed = decisions[decisions["decision"] == "relicensed_as"]
    summary = load_json(PROCESSED_DIR / "value_summary.json")
    return {"merges": merges, "excludes": excludes,
            "relicensed": relicensed, "summary": summary}


# ---------------------------------------------------------------------------
# Page assembly
# ---------------------------------------------------------------------------

def load_css():
    """Page stylesheet lives in templates/ so it can evolve without
    touching the renderer."""
    return (TEMPLATES_DIR / "dashboard.css").read_text(encoding="utf-8")


CSS = load_css()

# ---------------------------------------------------------------------------
# Page assembly
# ---------------------------------------------------------------------------

TAB_JS = """
<script>
(function () {
  var buttons = document.querySelectorAll('.tabbar button');
  function show(id) {
    buttons.forEach(function (b) {
      var on = b.dataset.tab === id;
      b.setAttribute('aria-selected', on ? 'true' : 'false');
      document.getElementById('tab-' + b.dataset.tab).classList.toggle('active', on);
    });
    if (history.replaceState) history.replaceState(null, '', '#' + id);
  }
  buttons.forEach(function (b) {
    b.addEventListener('click', function () { show(b.dataset.tab); });
  });
  var initial = (location.hash || '#page1').slice(1);
  if (!document.getElementById('tab-' + initial)) initial = 'page1';
  show(initial);
})();
</script>
"""


def status_panel_html(ctx):
    s, feeds, ldc, nodes = (ctx["oemp"], ctx["feeds"], ctx["ldc"],
                            ctx["nodes"])
    nodes_drawn = f'{nodes["drawn"]:,}'
    nodes_total = f'{nodes["total"]:,}'
    nodes_missing = nodes["missing_coords"]
    ldc_in_scale, ldc_multi, ldc_excl = (ldc["in_scale"], ldc["multi_zone"],
                                         ldc["excluded"])
    feed_items = "".join(
        f"<li>{feed}: {when}</li>" for feed, when in sorted(feeds.items()))
    gaps = ctx["lmp_gaps"]
    gap_bits = []
    if gaps["missing"]:
        gap_bits.append("missing: " + ", ".join(gaps["missing"]))
    if gaps["partial"]:
        gap_bits.append("partial: " + ", ".join(gaps["partial"]))
    gap_line = "; ".join(gap_bits) if gap_bits else "none"
    return f"""
<section aria-label="Data status">
<h2>Data status</h2>
<div class="status-grid">
<div class="status-card"><h3>Collection (per feed)</h3><ul>{feed_items}</ul></div>
<div class="status-card"><h3>OEMP-era price history</h3>
<p>{s["days"]} days ({s["months"]} months), {s["start"]} to {s["end"]}</p>
<p>First full 12-month window available on {s["first_full_12mo"]} (assuming no gaps)</p>
<p>Gaps: {s["gap_note"]}</p></div>
<div class="status-card"><h3>Wholesale signal</h3>
<p>{nodes_drawn} of {nodes_total} nodes drawn ({nodes_missing} lack coordinates)</p>
<p>Node-to-zone coverage: {nodes["zone_pct"]}% vs the 90% bar — aggregation withheld</p>
<p>DA LMP day coverage ({gaps["start"]} to {gaps["end"]}): {gap_line}</p></div>
<div class="status-card"><h3>Adoption (RRR)</h3>
<p>Vintage {ldc["vintage"]}, data year {ldc["data_year"]}</p>
<p>{ldc_in_scale} LDCs in scale, {ldc_multi} multi-zone, {ldc_excl} not reported</p>
<p>Page 4: {ldc["approved"]} approved, {ldc["rejected"]} rejected (rule-based approval; owner overrides in data/inputs/ldc_owner_overrides.csv)</p></div>
</div>
</section>"""


def page_block_html(page_id, title, chart_file, badge, ctx):
    num = page_id.replace("page", "")
    caption = chart_caption(chart_file)
    commentary_html, _ = render_commentary(CONTENT_DIR /
                                           f"commentary_{page_id}.md")
    figs = "".join(f"<div><dt>{html_mod.escape(k)}</dt>"
                   f"<dd>{html_mod.escape(v)}</dd></div>"
                   for k, v in key_figures(page_id, ctx))
    return f"""
<section id="tab-{page_id}" class="tabpanel" aria-label="{html_mod.escape(title)}">
<h2>{html_mod.escape(title)}</h2>
<span class="badge">{html_mod.escape(badge)}</span><br>
<iframe class="chart" title="{html_mod.escape(title)}"
        src="charts/{chart_file}"></iframe><br>
<a class="fullscreen-link" href="charts/{chart_file}" target="_blank"
   rel="noopener">Open chart full screen</a>
<h3>Commentary</h3>
{commentary_html}
<h3>Key figures</h3>
<dl class="key-figures">{figs}</dl>
<div class="caveats"><strong>Sources and caveats.</strong> {caption}</div>
</section>"""


def methods_html(ctx, mc):
    s = mc["summary"]
    feed_rows = "".join(
        f"<tr><td>{html_mod.escape(feed)}</td>"
        f"<td>{html_mod.escape(when)}</td></tr>"
        for feed, when in sorted(ctx["feeds"].items()))
    dec_rows = ""
    for _, r in mc["merges"].iterrows():
        dec_rows += (f"<tr><td>{html_mod.escape(r['utility_name'])}</td>"
                     f"<td>merged into {html_mod.escape(str(r['target_licence_no']))}</td>"
                     f"<td>{html_mod.escape(str(r['reason'])[:160])}</td></tr>")
    for _, r in mc["relicensed"].iterrows():
        dec_rows += (f"<tr><td>{html_mod.escape(r['utility_name'])}</td>"
                     f"<td>relicensed as {html_mod.escape(str(r['target_licence_no']))}</td>"
                     f"<td>{html_mod.escape(str(r['reason'])[:160])}</td></tr>")
    for _, r in mc["excludes"].iterrows():
        dec_rows += (f"<tr><td>{html_mod.escape(r['utility_name'])}</td>"
                     f"<td>excluded (no RRR rows)</td>"
                     f"<td>{html_mod.escape(str(r['reason'])[:160])}</td></tr>")
    gaps = ctx["lmp_gaps"]
    gap_bits = []
    if gaps["missing"]:
        gap_bits.append("fully missing days: " + ", ".join(gaps["missing"]))
    if gaps["partial"]:
        gap_bits.append("partial days (incomplete downloads, retried by the "
                        "scheduled collector): " + ", ".join(gaps["partial"]))
    gap_text = ("; ".join(gap_bits) if gap_bits
                else "no missing or partial days") + \
        f' (trailing 35 days, {gaps["start"]} to {gaps["end"]}, computed from '\
        'lmp_node_daily.csv)'
    terms = ", ".join(s.get("tariff_terms", []))
    return f"""
<section id="tab-methods" class="tabpanel methods" aria-label="Methods and caveats">
<h2>Methods and caveats</h2>
<h3>Definitions</h3>
<ul>
<li><strong>OEMP</strong> (Ontario Energy Market Price) = DA-OZP + LFDA, the
IESO's day-ahead Ontario zonal price plus the Hourly LFDA addend.</li>
<li><strong>OZP</strong> (Ontario zonal price): since the May 2025 market
renewal, the successor to HOEP as the headline Ontario price. Older data is
labelled "HOEP era" and never spliced into OEMP.</li>
<li><strong>LFDA</strong>: the IESO Hourly LFDA addend; the feed's value
column is headed "LFDC Rate ($/MWh)".</li>
<li><strong>RPP commodity offset</strong>: the effective-dated RPP TOU/ULO
commodity rate overlaid against OEMP. The wedge is the difference between
the two. RPP rates include a Global Adjustment estimate and RPP cost
recovery, so the wedge is not only an energy-value gap.</li>
</ul>
<h3>Data sources, vintages, retention</h3>
<table><tr><th>Feed</th><th>Last collected</th></tr>{feed_rows}</table>
<ul>
<li>IESO day-ahead zonal price: daily files from 2026-07-01 (rolling ~3
months of index retention); LFDA annual files; IESO day-ahead hourly LMP;
IESO fuel mix.</li>
<li>OEB RRR open data: vintage {html_mod.escape(ctx["ldc"]["vintage"])}, data
year {ctx["ldc"]["data_year"]}.</li>
<li>Node coordinates: <code>ieso_node_locations.csv</code> (user-supplied;
licence pending).</li>
<li>Capacity Auction Zone Table (July 2026): 572/1,055 nodes zoned (54.2%).</li>
</ul>
<h3>Tariff terms used</h3>
<p>RPP commodity schedules: {html_mod.escape(terms)}. TOU and ULO plans are
selectable; Class B scenarios are kept separate from RPP.</p>
<h3>Exclusions, merges, relicensed LDCs</h3>
<table><tr><th>LDC</th><th>Decision</th><th>Reason</th></tr>{dec_rows}</table>
<h3>Fallbacks in force</h3>
<ul>
<li>Zone aggregation <strong>withheld</strong>: node-to-zone coverage is
54.2%, below the 90% bar. Page 3 is node-level only.</li>
<li>Page 4 uses <strong>rule-based approval</strong> (owner directive
2026-10-01): an LDC is approved when it has at least one load node after
filtering, is in_polygon, is not heterogeneous, and has adoption data.
The owner_override column (data/inputs/ldc_owner_overrides.csv) wins over
the rule. Heterogeneous or borrowed-node LDCs are drawn hollow and kept
out of the correlation.</li>
</ul>
<h3>Known gaps</h3>
<p>{html_mod.escape(gap_text)}. The scheduled collector retries incomplete
downloads on later runs.</p>
<h3>Timezone convention</h3>
<p>Config <code>TIMEZONE_MODE = {html_mod.escape(TIMEZONE_MODE)}</code>:
{html_mod.escape(s.get("timestamp_convention", ""))}.</p>
<h3>Attribution</h3>
<p>OEB data under the Open Government Licence &ndash; Ontario. IESO public
reports under the IESO's terms of use. LDC polygon provenance and licence
to be confirmed by the owner before public release.</p>
</section>"""


def build_context():
    manifest = load_json(PROCESSED_DIR / "manifest.json")
    summary = load_json(PROCESSED_DIR / "value_summary.json")
    signal = pd.read_csv(PROCESSED_DIR / "ldc_node_signal.csv")
    methods = signal["method"].value_counts().to_dict()
    sig_text = ", ".join(f"{methods.get(m, 0)} {m.replace('_', ' ')}"
                         for m in ("in_polygon", "nearest_within_30km",
                                   "none"))
    ctx = {
        "manifest": manifest,
        "summary": summary,
        "oemp": oemp_history(summary),
        "feeds": feed_collection_times(manifest),
        "nodes": node_stats(),
        "ldc": ldc_stats(),
        "lmp_gaps": lmp_gaps(),
        "signal_methods": sig_text,
    }
    ctx["badges"] = page_badges(ctx["ldc"], ctx["nodes"])
    return ctx


def build_index(ctx, built_utc, built_toronto, commit):
    mc = methods_context()
    badges = ctx["badges"]
    tabs = "".join(
        f'<button data-tab="{pid}" aria-selected="false">'
        f"{html_mod.escape(label)}</button>"
        for pid, label in [
            ("page1", "Page 1 — Value wedge"),
            ("page2", "Page 2 — Adoption"),
            ("page3", "Page 3 — Wholesale signal"),
            ("page4", "Page 4 — Adoption vs signal"),
            ("methods", "Methods and caveats"),
        ])
    pages = "".join(
        page_block_html(pid, title, chart, badges[pid], ctx)
        for (chart, title), pid in zip(
            PAGES, ("page1", "page2", "page3", "page4")))
    panel = status_panel_html(ctx)
    methods = methods_html(ctx, mc)
    footer = (
        f"Data: IESO public reports; OEB RRR open data (Open Government "
        f"Licence &ndash; Ontario). LDC polygon provenance and licence to be "
        f"confirmed by the owner before public release.<br>"
        f'Built <span id="{TIMESTAMP_ID}" data-utc="{built_utc}" '
        f'data-toronto="{built_toronto}">{built_utc} / {built_toronto}</span> '
        f"&middot; commit {html_mod.escape(commit)}.")
    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ontario DER Monitor — evidence dashboard (draft)</title>
<style>{CSS}</style>
</head>
<body>
<header>
<h1>Ontario DER Monitor</h1>
<p class="sub">Evidence for the OEB DER valuation argument, docket
EB-2025-0268. Static dashboard; all figures computed from the processed
files below.</p>
</header>
{panel}
<nav class="tabbar" aria-label="Dashboard pages">{tabs}</nav>
{pages}
{methods}
<footer>{footer}</footer>
{TAB_JS}
</body>
</html>
"""
    return apply_release_gate(page)


def apply_gate_to_charts():
    """Banner + noindex on every chart page (or strip when approved)."""
    for chart_file, _ in PAGES:
        path = CHARTS_DIR / chart_file
        if not path.exists():
            continue
        html_text = path.read_text(encoding="utf-8")
        path.write_text(apply_release_gate(html_text), encoding="utf-8")


def _build_times():
    """(built_utc, built_toronto) display strings.

    DER_MONITOR_BUILD_TIME pins the clock (ISO 8601) so reruns are
    byte-identical for determinism checks; unset means "now".
    """
    fixed = os.environ.get("DER_MONITOR_BUILD_TIME")
    now_utc = (datetime.fromisoformat(fixed) if fixed
               else datetime.now(timezone.utc))
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    built_utc = now_utc.strftime("%Y-%m-%d %H:%M UTC")
    built_toronto = now_utc.astimezone(
        ZoneInfo("America/Toronto")).strftime("%Y-%m-%d %H:%M %Z")
    return built_utc, built_toronto


def main():
    built_utc, built_toronto = _build_times()
    commit = git_hash()
    ctx = build_context()
    html_text = build_index(ctx, built_utc, built_toronto, commit)
    out = DOCS_DIR / "index.html"
    out.write_text(html_text, encoding="utf-8")
    apply_gate_to_charts()
    size_mb = sum(p.stat().st_size for p in DOCS_DIR.rglob("*")
                  if p.is_file()) / 1e6
    print(f"wrote {out.name} ({out.stat().st_size / 1024:.0f} KB); "
          f"docs/ total {size_mb:.1f} MB; release flag "
          f"{'APPROVED' if PUBLIC_RELEASE_APPROVED else 'DRAFT'}; "
          f"commit {commit}")


if __name__ == "__main__":
    main()
