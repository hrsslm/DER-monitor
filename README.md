# Ontario DER Monitor

A static GitHub Pages dashboard quantifying the Ontario Energy Board's
distributed-energy-resource valuation argument (docket EB-2025-0268): how the
wholesale value of distributed solar + storage compares to what customers are
paid under net metering and the proposed net billing.

**Status: Phase 4 complete (2026-09-30). Draft — not for public release.**
See `RELEASE_CHECKLIST.md` and the release gate below.

## What it is

Four evidence pages plus a methods tab, rendered as static HTML in `docs/`:

- **Page 1 — Net metering value wedge:** OEMP vs the RPP commodity offset,
  summer preview only (no annual headline until 12 months of OEMP-era data
  exist).
- **Page 2 — DER adoption baseline:** net-metered capacity per 1,000
  customers by LDC (OEB RRR Table 1 only; Table 3 fuels are separate
  selectable layers, never combined).
- **Page 3 — Wholesale locational signal:** node-level day-ahead LMP
  congestion (zone aggregation withheld below 90% coverage).
- **Page 4 — Adoption vs wholesale signal:** renders only after the owner
  reviews the crosswalk rows; currently "Awaiting owner review".

All chart pages are standalone Plotly HTML (CDN-hosted Plotly JS, no tokens,
no analytics). `docs/index.html` embeds them as iframes with a data-status
panel, per-page commentary placeholders, key figures, and sources/caveats.

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python src/fetch_data.py          # one-time upstream fetch
.venv/bin/python src/fetch_price_history.py # daily price-history accumulation
.venv/bin/python src/process_value.py       # Page 1 inputs
.venv/bin/python src/process_adoption.py    # Page 2 inputs (needs data/raw OEB + contract files)
.venv/bin/python src/process_zones.py       # Page 3 inputs
.venv/bin/python src/process_node_signal.py # Page 4 inputs
.venv/bin/python src/make_charts.py         # docs/charts/*.html
.venv/bin/python src/render_dashboard.py    # docs/index.html
.venv/bin/pytest
```

## Project layout

- `src/` — pipeline code (`config.py`, fetchers, processors, `make_charts.py`,
  `render_dashboard.py`)
- `content/` — author commentary Markdown (`commentary_page1..4.md`);
  machine-filled, never written by the agent
- `templates/` — reserved for page templates (currently fragments are built
  in `render_dashboard.py`)
- `data/inputs/` — user-supplied files (committed, except large originals)
- `data/raw/` — downloaded raw files (git-ignored scratch)
- `data/processed/` — processed CSVs + `manifest.json` (committed)
- `docs/` — dashboard pages (`index.html`, `charts/`), method notes, data
- `tests/` — pytest suite with real small fixtures (131+ tests)
- `.github/workflows/` — `collect.yml` (data) and `build.yml` (site)

## Methods summary

Key analytical assumptions (each also configurable in `src/config.py`):

- **IESO market hours are fixed EST (UTC−5), year-round, no DST**
  (`TIMEZONE_MODE = "EST_fixed"`). Verified against IESO documentation and
  the 2026-03-08 spring-forward feed (exactly 24 rows). Joins happen in UTC;
  tariff classification uses Toronto local wall clock.
- **OEMP = DA-OZP + LFDA.** The `HourlyLFDA` feed's value column is headed
  "LFDC Rate ($/MWh)". HOEP was retired for settlement on 2025-05-01 and is
  never spliced into OEMP; older data is labelled "HOEP era".
- **RPP holidays (configurable):** New Year's Day, Family Day, Good Friday,
  Victoria Day, Canada Day, Labour Day, Thanksgiving Day, Christmas Day,
  Boxing Day. No Civic Holiday, no weekend-shift.
- **Retail offset is commodity-only** (conservative lower bound). An optional
  delivery-inclusive scenario reads `delivery_variable_c_per_kwh` for one
  named benchmark LDC once the owner populates it. Fixed charges, HST, and
  the Ontario Electricity Rebate are excluded.
- **RPP commodity rates include a Global Adjustment estimate and RPP cost
  recovery**, so the wedge is not only an energy-value gap.
- **Node-to-zone:** 572/1,055 nodes (54.2%) via the Capacity Auction Zone
  Table; below the 90% bar, so zone aggregation is withheld and Page 3 is
  node-level.
- **LDC polygons** are indicative, not legal boundaries; source and licence
  to be confirmed by the owner before public release.
- **IESO Active Generation Contract List** is validation-only; never summed
  with OEB embedded-generation capacity.
- **Province-wide solar** in fuel-mix data is transmission-visible, not
  rooftop/net-metered.
- Commentary is the author's: `[ANALYST COMMENTARY - TO BE WRITTEN BY
  AUTHOR]` placeholders are never filled by the agent.
- **Annual headline metrics appear only once 12 months of OEMP-era data
  exist.** Until then Page 1 is a labelled summer preview with no headline
  percentage.

## Failure policy

- Missing or delayed upstream report: warn, skip, retain last-good data,
  do not crash. The scheduled collector retries incomplete downloads on
  later runs.
- Scheduled collection fetch failure visibly fails the workflow
  (`fetch_price_history.py --strict` exits nonzero; data is preserved).
- Malformed supplied input fails clearly with nonzero exit.
- A failed build run shows as failed; collect and build share one
  concurrency group so they never race.

## Timezone convention (EST_fixed)

`TIMEZONE_MODE = "EST_fixed"` in `src/config.py`: IESO market reports use
fixed EST (UTC−5) all year, no daylight saving. Source timestamps are
rederived to true UTC from fixed −05:00; **joins happen in UTC**; RPP tariff
classification uses Toronto local wall-clock fields (`America/Toronto`),
including the DST transitions 2026-03-08 and 2026-11-01. Stored datetimes
carry explicit ISO-8601 offsets.

## Data sources and retention

- **IESO public reports** (https://reports-public.ieso.ca/public/) — subject
  to the IESO's public-report terms. Day-ahead zonal price, LFDA, day-ahead
  hourly LMP, and fuel mix are accumulated daily by `collect.yml` into
  `data/processed/`; raw files are git-ignored scratch. The IESO index
  retains roughly 3 months of daily files, so daily collection is the only
  durable history.
- **OEB open data** (https://www.oeb.ca/ontarios-energy-sector/open-data) —
  under the Open Government Licence – Ontario. RRR vintage 2024-2, data year
  2024; fetched manually at phase time.
- Full source notes: `docs/DATA_SOURCES.md`.

## Workflows

- `collect.yml` — **data only.** Twice daily (06:00, 18:00 UTC): fetches
  price history with `--strict`, commits `data/processed/` when changed.
  Never builds charts or the site.
- `build.yml` — **site only.** Triggered after a successful collect run,
  by push to `src/**`, `content/**`, `templates/**`, or manually. Rebuilds
  derived outputs, charts, and `docs/index.html`; commits `docs/` only when
  changed (rebase-and-retry against collect's commits). The build itself
  takes seconds excluding dependency install.

## Release gate

The repo contains third-party data whose licence the owner has **not**
confirmed (LDC polygons, node coordinates). Until `PUBLIC_RELEASE_APPROVED`
is `True` in `src/config.py`, every page carries a "DRAFT: not for public
release. Data licences pending." banner and `<meta name="robots"
content="noindex">`. The gates are listed in `RELEASE_CHECKLIST.md`; the
owner flips the flag only after all are satisfied. GitHub Pages ("Deploy
from a branch: main, /docs") is enabled only after the checklist is
complete.

## Attribution

OEB data under the Open Government Licence – Ontario. IESO public reports
under the IESO's terms of use.
