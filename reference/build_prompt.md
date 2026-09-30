# Build Prompt: Ontario DER Monitor (static dashboard + automated pipeline)

## 0. Role and goal

You are an expert Python data engineer and visualization specialist. Build **Ontario DER Monitor**: a static, self-updating dashboard on GitHub Pages that quantifies the argument in the Ontario Energy Board's (OEB) DER valuation work, using only free public IESO and OEB data. It extends the pattern of the author's existing project `iesometeo` (Python + pandas + geopandas + Plotly + GitHub Actions + GitHub Pages, no API keys, no servers, no databases). If the `iesometeo` source or README is provided in `reference/iesometeo/`, follow its conventions. If not, implement the equivalents below and ask before diverging.

The audience is regulatory and policy staff. **Accuracy of terminology, docket references and caveats matters more than visual polish.**

Work in phases (Section 8). Complete and verify each phase before starting the next. Start with Phase 0.

---

## 1. Non-negotiable rules

### 1.1 Data integrity
- **Never invent, synthesize, interpolate or "reasonably guess" data**: no coordinates, tariffs, prices, capacities or filenames. If something is missing, follow the failure policy in 1.2.
- **Never guess an upstream filename or schema.** Verify it against a real downloaded sample (Phase 0). If you have no network access, stop and ask the user to place sample files in `tests/fixtures/`.
- Log every dropped, unmatched or repaired record (counts and identifiers) to `data/processed/qa_log.csv`.

### 1.2 Failure policy (apply consistently)
| Situation | Behaviour |
|---|---|
| Upstream IESO/OEB report missing or delayed | Log a warning, skip, keep last good data. Never crash. |
| User-supplied input file (Section 3) missing | Log a warning and use the defined fallback for that input. Never crash. |
| User-supplied input file present but malformed | **Fail loudly**: clear error message, non-zero exit code. |
| Required tariff table missing | Page 1 shows an "unavailable" state and a warning. Other pages still build. |

### 1.3 Terminology (Market Renewal Program, effective May 1, 2025)
- Use **Ontario Electricity Market Price (OEMP)** = Day-Ahead Ontario Zonal Price (DA-OZP) + Load Forecast Deviation Adjustment (LFDA), and **Ontario Zonal Price (OZP)**.
- **HOEP was retired for settlement on May 1, 2025.** Never use it for the headline metric or any current calculation. Any pre-May-2025 series must be labelled "HOEP era" and never spliced into an OEMP series.
- Non-dispatchable loads (including LDC retail customers) pay the OZP-based price, **not** nodal LMPs. Nodal LMPs are the wholesale price signal for generators and dispatchable resources. Every nodal chart must be captioned as a "wholesale locational signal", never as "what LDC customers pay".
- OEB RPP prices (TOU, ULO, Tiered) **already include an estimate of the Global Adjustment (GA)**. Do not add GA on top of RPP prices.
- Policy language: docket **EB-2025-0268** is titled *Review of the Valuation of Distributed Energy Resources*. The OEB's Report to the Minister (released September 2, 2026) **recommends** transitioning from net metering to time- and location-based net billing. Say "recommended", never "decided" or "transitioned".

### 1.4 Timestamps
- Convert IESO "Hour Ending 1-24" to interval **start**, stored as ISO 8601 with explicit UTC offset, using `zoneinfo`. Do joins on UTC.
- **Do not assume the report time zone.** IESO market reports may use Eastern Standard Time year-round rather than Toronto local time. Determine this from report headers or documentation in Phase 0 and implement it in **one** constant/function (`TIMEZONE_MODE`).
- Add a sanity test: on July and January fixture days, solar output must peak between 12:00 and 14:00 Toronto local time. A one-hour shift in summer indicates a timezone-mode error.
- Test DST days (2026-03-08 and 2026-11-01) explicitly.

### 1.5 Repository hygiene (as in `iesometeo`)
- Idempotent reruns. Track downloads in `data/processed/manifest.json` (URL, ETag/Last-Modified, sha256, fetched_at, row count). A rerun with no upstream change downloads nothing.
- `data/raw/` is git-ignored. Commit only tidy CSVs in `data/processed/` and outputs in `docs/`.
- Do **not** commit node-level history. Commit zone-level aggregates, a compact rolling window, and the latest node snapshot only.
- No API keys, no paid map tiles, no accounts.
- Pin dependencies in `requirements.txt`. Python 3.12.
- Attribution in the README and page footer: OEB data under the Open Government Licence - Ontario; IESO data under IESO's public-report terms.

### 1.6 Spatial standards
- Store and plot in `EPSG:4326`.
- For distance and area operations use **`EPSG:3161`** (NAD83 / Ontario MNR Lambert), not a single UTM zone, because Ontario spans several.
- Validate geometries (`is_valid`); repair with `make_valid` and log each repair.

---

## 2. Data sources (verify in Phase 0, then record in `docs/DATA_SOURCES.md`)

For each feed, Phase 0 must record: verified URL pattern, saved sample file in `tests/fixtures/`, schema, cadence, retention window, and time zone.

| Purpose | Source | Notes / must verify |
|---|---|---|
| Day-ahead Ontario zonal price | IESO Public Reports: Day-Ahead Hourly Ontario Zonal Energy Price | Feeds OEMP |
| LFDA | IESO public reports / settlement pages | **Locate the actual source.** If not published as a feed, label the metric "DA-OZP based" and warn. Do not approximate LFDA. |
| Real-time OZP (optional "latest" view) | IESO RealtimeOntarioZonalPrice (5-min; hourly average) | Already used by `iesometeo` |
| Day-ahead nodal LMP (LMP, congestion, loss) | IESO Public Reports: Day-Ahead Hourly Energy LMP | Verify filename pattern and retention |
| Node-to-zone map | IESO node-zone map report | Defines zones. **Derive the zone count from this file; do not hard-code it.** |
| Solar output shape (primary) | `iesometeo` processed fuel-mix CSV (province-wide solar) | Transmission-visible solar only; label as such |
| Solar shape (optional secondary) | IESO Variable Generation Forecast | This is a **forecast**, not measured output. Label it and store forecast vintage. |
| Net metering and embedded generation by LDC | OEB Open Data, RRR section 2.1.14 (Table 1 net metering, Table 3 embedded generation, Table 4 by type) | Annual. Use the categories actually published; do not assume "battery" exists. |
| LDC customer counts | OEB Open Data (RRR) | **Identify the correct section** from the OEB Open Data inventory. |
| LDC service area polygons | OEB / Ontario open data | **Verify the source and licence.** If none is found, stop and ask the user. |
| RPP TOU/ULO prices | OEB RPP price reports | Set annually on November 1; TOU hours switch May 1 and November 1 |
| IESO contract list (validation only) | IESO Active Generation Contract List (Excel) | See Page 2 |

---

## 3. User-supplied inputs (`data/inputs/`)

| File | Schema | If missing |
|---|---|---|
| `ieso_node_locations.csv` | `node_id, latitude, longitude, zone_name` | Warn. Page 3 falls back to zone-level view (see Page 3). **Never create coordinates.** |
| `ieso_zones.geojson` | IESO zone polygons, property `zone_name` | Warn. Page 3 shows a zone table/bar chart instead of a map. |
| `ldc_zone_crosswalk.csv` | `ldc_id, ldc_name, zone_name, weight` (weights sum to 1 per LDC; multi-zone LDCs such as Hydro One get several rows) | Warn. Page 4 shows "unavailable". |
| `retail_tariffs.csv` | `effective_from, effective_to, plan (TOU\|ULO\|TIERED), period, weekday_rule, hours, commodity_c_per_kwh, delivery_variable_c_per_kwh, source_url` | Page 1 shows "unavailable". |
| `policy_events.csv` (Phase 5) | `date, docket, title, url, summary` | Page 5 omitted. |

Effective-dated rows are mandatory: the retail series changes each November 1, and TOU hours change seasonally.

---

## 4. Pages

### Page 1: Net Metering Value Wedge (hero exhibit)
**Archetype (default):** residential RPP customer on TOU, with ULO as a selectable alternative. Optional secondary scenario: Class B market-price customer.

**Definitions** (all in c/kWh; convert from $/MWh explicitly):
```
Market value_t   = OEMP_t = DA_OZP_t + LFDA_t
Retail offset_t  = RPP commodity price_t (TOU/ULO; includes GA estimate)
                   + variable delivery components_t (volumetric distribution,
                     retail transmission, regulatory charges)
Wedge_t          = Retail offset_t - Market value_t
Headline gap (%) = sum(solar_t * (offset_t - market_t)) / sum(solar_t * offset_t)
```
- Exclude fixed charges, HST and the Ontario Electricity Rebate. State this on the page.
- Class B scenario: offset = OEMP + monthly Class B GA rate + variable delivery. Show separately, never combined with the RPP scenario.
- Headline metric window: trailing 12 months of OEMP-era data only. Also show the unweighted gap as a sensitivity.
- Negative or near-zero prices stay in the data. Note them in the caveats.

**Visual:** hourly (or daily-shape) overlay of solar output profile, OEMP, and the effective-dated retail offset as a step line. Add a headline card with the gap, window and as-of date.
**Policy reference:** EB-2025-0268 and the OEB's recommendation of net billing (see 1.3 wording).
**Caveat block (required):** solar shape is province-wide transmission-visible solar, not rooftop net-metered output; the gap depends on the tariff archetype.

### Page 2: DER Adoption and Capacity Baseline
- **Primary metric:** net metered plus embedded generation capacity per 1,000 customers, by LDC, from OEB RRR 2.1.14.
- **IESO contract list is for validation only** (cross-check totals and flag large discrepancies). **Never add it to OEB capacity**: OEB embedded generation data already includes contracted projects and would be double-counted.
- Choropleth by LDC polygon (`EPSG:4326`), tooltips with facility counts and capacity by the fuel categories actually published. Include the data year on the page.

### Page 3: Wholesale Locational Signal (day-ahead nodal LMP and congestion)
- Data: day-ahead hourly nodal LMP with LMP, congestion and loss components; node-to-zone map.
- Metric: **30-day rolling mean of the day-ahead congestion component** per node, then per zone. Use a simple mean across nodes unless verified load weights exist; state the method on the page. Never use single-hour snapshots for the headline view.
- Fallback ladder:
  1. Node coordinates present: node-level scatter map, colored by rolling congestion (toggle: LMP).
  2. Coordinates missing but `ieso_zones.geojson` present: zone-level choropleth.
  3. Neither: zone table and bar chart.
- Loss and congestion decomposition shown per zone.
- Caption: "Wholesale locational signal for generators and dispatchable resources. Not a retail price and not a distribution deferral value."

### Page 4: DER Adoption vs Wholesale Locational Signal
- Join Page 2 (LDC) to Page 3 (zone) through `ldc_zone_crosswalk.csv`, using weights for multi-zone LDCs.
- Scatter: adoption per 1,000 customers vs congestion signal, labelled by LDC; show n and Spearman rank correlation. No causal language.
- Thesis wording: "where DER is growing versus where the wholesale locational signal is strongest". Do not name specific regions as congested unless the data shows it. Do not claim non-wires-deferral value.

### Page 5 (Phase 5, optional): Policy Tracker
- Timeline from `policy_events.csv` covering EB-2025-0268 and the OEB's DSO capabilities consultation, with links to the OEB record.
- Default is manually curated. An automated docket watcher is optional: at most weekly, polite request rate, descriptive User-Agent, respect robots.txt, and never break the build on failure.

---

## 5. Commentary (author-written)

Do **not** write analytical commentary. For each page create `content/commentary_pageN.md` containing the placeholder `[ANALYST COMMENTARY - TO BE WRITTEN BY AUTHOR]` plus a machine-filled "Key figures" block (headline values, data window, as-of dates, sources) using template variables. `render_dashboard.py` embeds the author's text plus the computed block. Every chart caption cites its data source and docket or report.

---

## 6. Repository layout

```
data/inputs/          # user-supplied files (Section 3)
data/raw/             # git-ignored
data/processed/       # committed tidy CSVs, manifest.json, qa_log.csv
docs/                 # index.html, charts/, DATA_SOURCES.md
content/              # author commentary
src/
  fetch_data.py       # download + manifest
  process_spatial.py  # geometry, zone mapping, rolling metrics, joins
  process_value.py    # OEMP, tariff join, wedge, headline metric
  make_charts.py
  render_dashboard.py
tests/                # pytest + fixtures/
.github/workflows/update.yml
README.md             # methods, caveats, sources, attribution
```

---

## 7. Automation and runtime

- **Cadence:** one scheduled workflow. Incremental fast path hourly at minute 35 UTC (IESO feeds; skip work when the manifest shows nothing new). OEB annual files are checked at most weekly using ETag/Last-Modified. `workflow_dispatch` supports a manual `--backfill-days 30` run.
- **Runtime budget:** the incremental run (excluding dependency install; use pip caching) should complete in about 3 minutes. Never download 30 days of real-time 5-minute nodal files; the rolling metric uses day-ahead hourly files.
- Workflow requirements: `permissions: contents: write`, a `concurrency` group to prevent overlapping runs, `actions/cache` for `data/raw/`, commit only if changed using `GITHUB_TOKEN`.
- Maps: use a **tokenless** basemap (Plotly MapLibre-based `scatter_map`/`choropleth_map` if the pinned version supports it; otherwise `*_mapbox` with `carto-positron`). Plotly JS loads from CDN, as in `iesometeo`.

---

## 8. Phases and acceptance tests

Use `pytest`. Real (small) samples live in `tests/fixtures/`.

**Phase 0: Reconnaissance (no application code).**
Produce `docs/DATA_SOURCES.md` (Section 2 table completed with verified facts) and save fixtures. *Accept:* every feed has a verified URL, schema, cadence, retention and timezone. Anything unverifiable is listed under "OPEN QUESTIONS" for the user. Stop and report before Phase 1.

**Phase 1: Fetch + manifest (`fetch_data.py`).**
*Accept:* first run populates `data/processed/` and `manifest.json`; an immediate second run downloads nothing; a simulated upstream 404 logs a warning and exits 0.

**Phase 2: Processing (`process_value.py`, `process_spatial.py`).**
*Accept:* no null timestamps; DST-day tests pass; solar-peak sanity test passes; every node maps to a zone (unmatched nodes logged); geometry validation log present; removing `ieso_node_locations.csv` triggers the fallback ladder with a warning and zero synthesized coordinates; a malformed input file exits non-zero; tariff join is effective-dated (test across a November 1 boundary); headline gap reproducible from fixtures with a hand-computed expected value.

**Phase 3: Charts (`make_charts.py`).**
*Accept:* one standalone HTML per page in `docs/charts/`; each carries caption, source, as-of date; pages with missing inputs render their "unavailable" state instead of failing.

**Phase 4: Dashboard + workflow (`render_dashboard.py`, `update.yml`).**
*Accept:* `docs/index.html` builds with navigation, embedded charts, commentary placeholders and footer attribution; incremental pipeline runtime within budget; workflow passes a dry run.

**Phase 5 (optional): Policy tracker.**

---

## 9. Definition of done and prohibitions

Done means: all acceptance tests pass, README documents methods and caveats (including the tariff archetype, the OEMP-era window, zone aggregation method, and every fallback), and `docs/DATA_SOURCES.md` has no unresolved open questions.

Do not: use HOEP for current calculations; add GA to RPP prices; sum IESO contract capacity with OEB capacity; describe nodal LMP as a retail or LDC price; present net billing as decided; write analytical commentary; scrape beyond the listed sources; commit raw data or node-level history; require any key or paid service.

**Begin with Phase 0.**
