# DATA_SOURCES.md — Ontario DER Monitor

## Phase 1 completion record (2026-09-29)

Phase 1 (fetch + manifest + validation) is complete and verified. What was
built and measured:

- **Fetcher** (`src/fetch_data.py`): downloads all 10 upstream files (4 IESO
  feeds, IESO contract list, 4 OEB RRR XML files, OEB service-area ZIP),
  validates the three user-supplied inputs, writes `data/processed/manifest.json`
  (URL, ETag/Last-Modified, SHA-256, fetch time, bytes, row count per file)
  and the rendering-only simplified GeoJSON. Exit 0 with warnings on missing
  upstream; exit 1 with a clear message on malformed supplied input.
- **Idempotency:** an immediate rerun downloads nothing for unchanged files.
  Change detection is ETag → Last-Modified → weak validators (index
  mtime/size for IESO feeds whose server sends no ETag; HEAD Content-Length
  for the IESO contract list). Known limitation: `PUB_HourlyLFDA.csv` is a
  rolling file the IESO republishes roughly every minute with identical
  content but a fresh mtime, so it re-downloads most runs (160 KB; harmless).
- **VG forecast index:** the `VGForecastSummary` folder index is large and the
  IESO server drops the connection mid-listing; indexes are now read
  newest-first (`?C=M;O=D`) with the partial body salvaged on `IncompleteRead`.
- **KMZ reproduction** (`src/kmz_reproduce.py`): 61 placemarks, 60 licences,
  7 repaired geometries, max per-licence area difference 0.0144% — PASS.
  The KMZ was extracted untouched from the official OEB service-area ZIP.
- **Landmarks:** all 14 pass against the supplied GeoJSON.
- **RRR reconciliation** (`src/reconcile_ldcs.py`): 54 of 60 licences matched
  (exact current, parenthetical-stripped, or historical names; never fuzzy);
  6 owner-review rows resolved 2026-09-29 via `data/inputs/ldc_decisions.csv`
  (see §4.6): Orillia merged into Hydro One Networks; 5 excluded as non-filers
  (Cornwall verified absent after an agent search of all four RRR tables).
  `remote`/`multi_zone` flags recorded. Deterministic across reruns
  (md5-identical outputs).
- **Analysis layer** (`data/processed/ldc_analysis_layer.geojson`, 58
  features): Orillia dissolved into Hydro One Networks and Espanola into
  North Bay Hydro at load time (area preserved exactly); Centre Wellington
  and Lakefront relicensed to their current licence numbers (both
  agent-confirmed in the OEB licence list); exclusions flagged, never dropped.
- **Simplified copy** (`docs/data/ldc_service_areas_simplified.geojson`):
  100 m in EPSG:3161, 4-decimal coordinates, valid, ~0.4 MB, rendering only.
- **Tests:** 56 pass (`pytest tests/`), covering tariff/node/polygon loading,
  KMZ reproduction, landmarks, reconciliation, index parsing, weak validators,
  404 handling, truncated downloads, and manifest round-trips.

## Phase 0 reconnaissance

Verified 2026-09-29. Every feed below was checked against a live download or an
opened official page; sample files are in `tests/fixtures/`. Nothing is guessed.

## Timezone convention (applies to ALL IESO market feeds)

**`TIMEZONE_MODE = "EST_fixed"` — IESO market reports use Eastern Standard Time
(UTC-5) year-round, with no daylight-saving shift.** Hour 1 = 00:00–01:00 EST.

Evidence:
1. Empirical: `HourlyLFDA` data for 2026-03-08 (spring-forward day) contains
   exactly 24 hourly rows. A Toronto-local-time report would have 23.
2. IESO training docs (ieso.ca): "Hours 1-24 are the hours from midnight one day
   through midnight the next day. Eastern Standard Time is used year-round."
3. IESO Market Rules, Chapter 6, Section 11.2: all metering references EST.

Consequence: in summer, EST hour H = Toronto (EDT) hour H+1. All market hours
must be localized as fixed UTC-5 and joined to Toronto-local series (e.g. the
`iesometeo` solar shape) **on UTC**. The Phase 2 solar-peak sanity test
(peak 12:00–14:00 Toronto local) will catch a one-hour mode error.

## IESO public reports (https://reports-public.ieso.ca/public/)

| # | Feed (dir name) | File pattern | Format / size | Cadence / retention | Schema (verified from sample) |
|---|---|---|---|---|---|
| 1 | `DAHourlyOntarioZonalPrice` | `PUB_DAHourlyOntarioZonalPrice[_YYYYMMDD][_vN].xml` + global-link rolling file | XML, ~6 KB/day | Daily, published ~12:30 the afternoon before the delivery day; index holds ~3 months (earliest file 2026-07-01) | `DeliveryDate`, `PricingHour` 1–24 (hour-ending), `ZonalPrice`, `LossPriceCapped`, `CongestionPriceCapped`, `Flag` (e.g. `DSO-RD`, meaning unverified). Units $/MWh. `CreatedAt` carries no offset. |
| 2 | `HourlyLFDA` | `PUB_HourlyLFDA[_YYYY].csv` (year-to-date + annual archives; ~10 files total) | CSV, ~160 KB/year | Annual files, updated in arrears (2026 file current through 2026-09-15 as of 2026-09-29) | `Date,Hour,Status,LFDC Rate ($/MWh)`; Hour 1–24; `Status` e.g. `Final`. **Note:** the column is named "LFDC Rate" while the report is "Hourly LFDA" — verify in Phase 2 that this is the correct OEMP addend (IESO's Ontario Price page states DA-OZP + LFDA = Ontario Price = OEMP). |
| 3 | `DAHourlyEnergyLMP` | `PUB_DAHourlyEnergyLMP[_YYYYMMDD].csv` + global-link rolling file | CSV, ~1 MB/day | Daily, published ~12:34 the afternoon before; retention similar to #1 | One-line preamble `CREATED AT <ts> FOR <date>` then header `Delivery Hour,Pricing Location,LMP,Energy Loss Price,Energy Congestion Price`. 24 h × 1,054 pricing locations = 25,296 rows/day. Node IDs match the user-supplied `ieso_node_locations.csv` `Location` values 1:1 (all 1,022 present). 32 extra locations exist, incl. 2 `:PZ` aggregates (`BRUCE_GEN:PZ`, `BRUCE_SOUTHWEST_LOAD:PZ`) — exclude from node mapping or treat explicitly. |
| 4 | `VGForecastSummary` | `PUB_VGForecastSummary[_YYYYMMDD].xml` (826 files in index) | XML, ~100 KB/day | Daily | `ForecastTimeStamp` (= forecast vintage, store it), `OrganizationType` (`MARKET PARTICIPANT` and `EMBEDDED`), `FuelType` (`Solar`, `Wind`), `ZoneName` (10 IESO zones + `OntarioTotal`; **NIAGARA absent** in the 2026-09-28 sample — no VG forecast rows for it that day), `ForecastDate` × `ForecastHour` 1–24 × `MWOutput`. 2-day horizon (file dated D covers D+1, D+2). **This is a forecast, never measured output.** |
| 5 | Contract list (IESO website, not a reports feed) | https://www.ieso.ca/-/media/Files/IESO/Document-Library/power-data/supply/IESO-Active-Contracted-Generation-List.xlsx | XLSX, ~400 KB | Single overwritten file, "current as of" 2026-06-30 (updates roughly quarterly; track via ETag/Last-Modified, there is no archive) | Sheet `Contract Data`, header at row 3 (rows 1–2 are title + as-of date). Columns: `Contract Type`, `Contract Capacity (MW)`, `Facility Name`, `Supplier Legal Name`, `Contract Status`, `Milestone Commercial Operation Date`, `Term Start Date`, `Term End Date`. 3,578 rows. Old `powerdata/supply/` URLs are dead (site redesign); the `power-data` URL above is current. **Validation only — never summed with OEB capacity.** |
| 6 | `RealtimeOntarioZonalPrice` | (as used by `iesometeo`) | XML | Hourly | Optional "latest" view only. |

### Node → zone mapping
Best verified source: **IESO Capacity Auction Zone Table**
(https://www.ieso.ca/-/media/files/ieso/document-library/engage/ca/ca-Capacity-Auction-Zone-Table.pdf,
updated 2026-07-16). Schema: `Station Name` → `Electrical Zone`. 593 stations,
**10 zones** (derived from the file, not hard-coded):
BRUCE, EAST, ESSA, NIAGARA, NORTHEAST, NORTHWEST, OTTAWA, SOUTHWEST, TORONTO, WEST
— identical to the `DemandZonal` zone columns.

**Caveat:** it maps *station names* (e.g. `ASHFIELD SS`), not pricing node IDs
(e.g. `ABITIBISF-LT.AG_T1`). A naive name-stem join matches only 315 of 662
distinct node stems (~48%); unmatched include newer resources (`*BESS`) and
naming variants. Phase 2 must implement the documented normalization
(uppercase, strip voltage/suffix tokens, exact stem first, then conservative
high-threshold fuzzy) and log every unmatched node per the failure policy.

**Node-zone map search (2026-09-29):** no `PUB_NodeZoneMap` was found or is
publicly accessible on the IESO reports server as of 2026-09-29 — not in the
root folder listing, not in `CA-PreAuction/`, `CA-PostAuction/` or
`DAHourlyEnergyLMP/` folders, no
`reports-public.ieso.ca/public/PUB_NodeZoneMap.{xml,csv}`, and no web
reference found. (This is a statement about what is findable, not a claim that
no such report exists internally.) Per the owner directive, proceed to the
Capacity Auction Zone Table (step 2), stopping at the first step that reaches
90% coverage of LMP nodes. Note: the IESO Data Directory mentions nine
*virtual transaction zones* — a different concept; do not conflate with the ten
electrical zones.

## OEB open data (https://www.oeb.ca/ontarios-energy-sector/open-data)

Licence for all: **Open Government Licence – Ontario**.

| # | Dataset | URL pattern | Format / size | Cadence / history | Schema (verified from raw file) |
|---|---|---|---|---|---|
| 7 | RRR §2.1.14 Table 1 – Net Metering | `https://www.oeb.ca/documents/opendata/rrr/<VINTAGE>/ED 2.1.14 Table 1 Net Metering.xml` | XML, ~1.1 MB | Annual; 2015–2024 (distributors file by Apr 30; latest corrections Apr 30, 2026) | Root `<dataroot>`, records `<tab1>`: `Current_Company_Name`, `Historical_Company_Name`, `Year`, `Renewable_Energy_Type` (Biomass/Solar/Water/Wind), `No_of_Net-Metered_Generators`, `Renewable_Generation_Installed_Capacity_kW`, `Electrical_Energy_Storage_Installed_Capacity_kW`, `Cumulative_Installed_Capacity_kW`, `Override` (Yes/No/Not applicable). **Elements are omitted when zero — not zero-filled.** 2,420 records. |
| 8 | RRR §2.1.14 Table 3 – Embedded Generation | `.../ED 2.1.14 Table 3 Embedded Generation.xml` | XML, ~250 KB | Annual; 2015–2024 | Records `<tab3>`: company names, `Year`, `Number_of_Embedded_Generation_Facilities`, `Total_Installed_Capacity_(kW)_Embedded_Generator` (parens XML-escaped as `_x0028_`/`_x0029_`). 605 records. |
| 9 | RRR §2.1.14 Table 4 – Embedded Generation by Type | `.../ED 2.1.14 Table 4 Embedded Generation by Type.xml` | XML, ~230 KB | Annual; **2023–2024 only** | Records `<tab4>`: company names, `Year`, `Facility_Type`, `Number_of_Facilities`, `Installed_Capacity_kW`. Facility types: Biomass, Exporting Storage, Fossil Fuel, Non-Exporting Storage, Other, Solar, Water, Wind — **no standalone "battery" category**. 856 records. |
| 10 | RRR §2.1.5.4 – Demand and Revenue (LDC customer counts) | `.../ED 2.1.5.4 Demand and Revenue.xml` | XML, ~3.1 MB | Annual; 2015–2024 | Records: company names, `Year`, `Customer_or_Connections` (Customers/Connections), `Rate_Class_-_Generic` (Residential, General Service < 50 kW, …), `Metered_Consumption_in_kWh_`, `Demand_in_kW`, `Annual_Billings_-_USoA_4080_-_Dollars`. 5,445 records, 53 LDCs. **Per-1,000-customer denominator = `Customer_or_Connections=Customers` × `Rate_Class_-_Generic=Residential`** (605 records). |

**OEB URL caveats:**
- The vintage folder changes per release (`2024-2` currently; archive used `2023/`; legacy basenames lacked the `ED ` prefix). Filenames contain spaces. **There is no stable "latest" URL** — `fetch_data.py` must track the current vintage explicitly (e.g. in the manifest) and re-resolve it.
- Distributor names change with mergers; each record carries both `Current_Company_Name` and `Historical_Company_Name` (e.g. Guelph Hydro filed under Alectra). Phase 2 needs a name-mapping step for the LDC join key.
- `Override` (Table 1) values Yes/No/"Not applicable" are unexplained on the inventory page; treat as a data-quality flag and log its distribution.

### LDC service-area polygons
**Resolved 2026-09-29.** The official OEB open-data service-area download
(`open-data-electricity-map-20260825.zip`, fetched from the OEB open-data
service-area page) contains `Electric_260825.kmz`, the OEB capacity-map (CCIM)
service-area layer export. The owner independently validated this KMZ and
supplied a cleaned analysis copy, `data/inputs/ldc_service_areas.geojson`
(60 features, one per licence, valid geometry, EPSG:4326). The Phase 1 KMZ
reproduction test re-derives the layer from the untouched KMZ and passes
(61 placemarks, 60 licences, 7 repairs, max per-licence area difference
0.0144%). All 14 landmark regression tests pass; Hydro One Networks (~518k
km²) + Hydro One Remote Communities (~471k km²) = ~97% of total area (~1.017M
km²). See Decisions §4 for the validation results and ingestion rules.
**Provenance caveat:** "OEB capacity-map layer; source and licence to be
confirmed by owner before public release." Treat polygons as indicative and
say so on Pages 2 and 4.

### RPP prices (TOU / ULO / Tiered)
- **No machine-readable feed exists — OEB publishes dated PDFs only**
  (e.g. `https://www.oeb.ca/sites/default/files/rpp-backgrounder-20251017.pdf`).
  The user-supplied `retail_tariffs.csv` (§3 of the build prompt) is the only
  workable path; values must be hand-transcribed each November.
- Verified: prices set annually on **November 1** (announced mid-October); TOU
  seasonal hours switch **May 1 / November 1** (winter: on-peak 7–11am & 5–7pm;
  summer: on-peak 11am–5pm; off-peak 7pm–7am + weekends/holidays); ULO overnight
  11pm–7am daily. RPP commodity prices already include the GA estimate.

### Docket EB-2025-0268 (context, not a feed)
- Docket page verified: https://oeb.ca/consultations-and-projects/policy-initiatives-and-consultations/review-valuation-distributed-energy
  — Case **EB-2025-0268**, *Review of the Valuation of Distributed Energy
  Resources*, launched Oct 27, 2025, status Active.
- OEB Report to the Minister on DER valuation released **September 2, 2026**;
  it **recommends** (not decides) transitioning from net metering to time- and
  location-based net billing.

## User-supplied inputs (`data/inputs/`) — status
- `ieso_node_locations.csv` — **present** (1,022 locations). Schema:
  `Longitude,Latitude,Location` (no `zone_name`). Owner confirmed: map
  `Location` → `node_id`; all 1,022 locations match LMP nodes. The ~32 LMP
  nodes with no coordinates are logged.
- `retail_tariffs.csv` — **present** (40 rows, two RPP terms: Nov 1 2024–Oct 31
  2025 and Nov 1 2025–Oct 31 2026; TOU + ULO). Machine-join schema: `season`,
  `day_type` (weekday | weekend_holiday), numeric `start_hour`/`end_hour`
  (end exclusive; overnight blocks split in two rows).
  `delivery_variable_c_per_kwh` intentionally blank. All 14 commodity prices
  spot-checked against the two OEB RPP price reports (2024-10-18, 2025-10-17)
  on 2026-09-29 — **all match**.
- `ldc_service_areas.geojson` — **present** (60 features, valid). See Decisions §4.
- `Electric_260825.kmz` — **present** (extracted untouched 2026-09-29 from the
  official OEB service-area ZIP; the Phase 1 reproduction test passes).
- `ieso_zones.geojson`, `ldc_zone_crosswalk.csv`, `policy_events.csv` — not
  yet supplied (generated/handled in later phases).

## Fixtures (`tests/fixtures/`)
`DA_zonal_sample.xml` (full day, 24 h), `LFDA_sample.csv` (2026-01-01–2026-09-15),
`LMP_sample.csv` (full day, 25,296 rows), `VG_sample.xml` (full day),
`contract_list_sample.csv` (15 rows), `zone_table_sample.csv` (593 stations, parsed
from the PDF), `rrr_2114_table1_sample.xml`, `rrr_2114_table3_sample.xml`,
`rrr_2114_table4_sample.xml`, `rrr_2154_sample.xml`.

## OPEN QUESTIONS — all resolved 2026-09-29 (see Decisions below)
1. **LDC polygons:** none exist publicly. Per build-prompt §2, how to proceed?
   (a) you supply polygons; (b) Page 2 falls back to table/bar chart; (c) drop the choropleth.
2. **RPP tariffs:** confirm hand-transcribed `retail_tariffs.csv` (from the annual
   November PDFs) remains the standing approach — no automated alternative exists.
3. **Node→zone mapping:** accept the Capacity Auction Zone Table + a Phase 2
   name-matching step with logged unmatched nodes, or can you supply a
   node-ID-level crosswalk? (Also note the uploaded CSV's schema differs from the
   spec — confirm the `Location` column is the node id and no `zone_name` is coming.)
4. **LFDA column naming:** the feed's value column is headed "LFDC Rate ($/MWh)".
   Phase 2 will sanity-check it as the OEMP addend (DA-OZP + LFDA); flag now in
   case your sources distinguish LFDC from LFDA.
5. **Timezone:** confirmed fixed EST (UTC-5) year-round for all IESO market hours.
   Phase 1+ will implement `TIMEZONE_MODE = "EST_fixed"`. (Note: this differs from
   the `iesometeo` project's America/Toronto assumption — worth a conscious
   confirmation since the two projects will share the solar shape.)

---

# Decisions (owner directive, 2026-09-29)

## 2A retention findings (2026-09-29) — STOP CONDITION FIRED

IESO public index listings checked in full (complete listings, not the
newest-first head):

- `DAHourlyOntarioZonalPrice`: daily files only, 2026-07-01 → 2026-09-30
  (92 days / 3.0 months). No yearly/monthly archive; the folder holds only
  the dated dailies plus the global latest-day link.
- `HourlyLFDA`: yearly files `PUB_HourlyLFDA_2025[_v1].csv` and
  `PUB_HourlyLFDA_2026[_v1].csv` plus the rolling global link → covers the
  full OEMP era from 2025-05-01. Header confirms units `$/MWh`.
- `GenOutputbyFuelHourly`: yearly files 2015 → 2026 (`_v271` latest) plus
  the global link → covers the full OEMP era. URL pattern and XML parsing
  confirmed present in the `iesometeo` source (`src/ieso_scraper.py`:
  feed `GenOutputbyFuelHourly`, index `https://reports-public.ieso.ca/public/GenOutputbyFuelHourly/`,
  namespace `http://www.ieso.ca/schema`, `EnergyValue/Output`).
- `DAHourlyEnergyLMP` (for 2C): daily files 2026-07-01 → 2026-09-30
  (~92 days) → the 30-day backfill is feasible.

**Stop condition D fired:** OEMP-era retention is under 6 months for the
day-ahead zonal price input (3.0 months available). 2A halted before any
metric was computed — no headline, no `value_hourly.csv`, no
`value_summary.json`. The fuel-mix feed was *not* added to `fetch_data.py`
yet; that work resumes when 2A is unblocked. 2B and 2C do not depend on the
day-ahead zonal price and await the owner's call.

*Update 2026-09-29/30: the owner authorized 2A as a preview on the
available window (see "2A preview" below); the stop condition still
applies to any annual headline.*

### Archive check (2026-09-29, owner request, bounded effort)

- `DAHourlyOntarioZonalPrice/` index (complete listing): daily files only,
  2026-07-01 → 2026-09-30; no yearly or monthly archive files. Only other
  file is the global latest-day link.
- IESO Data Directory entry for the Day-ahead Hourly Ontario Zonal Energy
  Price Report: confirms the report, offers no deeper archive.
- IESO Power Data "historical data" links resolve back to the Public
  Reports site (same rolling window).
- **Conclusion:** no public archive extends the DA zonal window. The daily
  scheduled fetch (below) is the only way history accumulates.

### Daily price-history fetch (owner request 2026-09-29)

`src/fetch_price_history.py` backfills and accumulates `da_zonal`, `lfda`,
`da_lmp` and `fuelmix` into `data/processed/*_hourly.csv` (dedupe
keep-last on `(timestamp_utc)` — `(timestamp_utc, node)` for LMP; DA LMP
kept as a 32-day rolling window, ~0.7M rows / ~44 MB, per the
no-node-history rule). A daily cron runs it and commits the processed
CSVs + manifest so history accumulates despite rolling upstream
retention.

Timestamp convention: IESO source timestamps are fixed -05:00 labels;
`est_fixed()` rederives the true UTC instant from them (stored
`timestamp_utc` values are correct UTC; deterministic on DST transition
days 2026-03-08 and 2026-11-01, both covered by tests). Joins use the UTC
instant. Tariff season/day/hour classification and the solar-peak gate
convert UTC to America/Toronto -- never the fixed -05:00 stamp's fields,
which read one hour behind true local time during EDT (corrected
2026-09-30; the earlier EST_fixed-field classification misclassified
summer TOU periods by one hour).

Physical verification of the convention (2026-09-30): Jan 2026 mean solar
peaks at 12:00 Toronto local, Jul 2026 at 12:00 Toronto local -- both in
the required 12:00-14:00 band, so the solar-peak gate asserts 12-14 on
the America/Toronto clock for both months.

### 2A preview (owner request 2026-09-29; timezone-corrected 2026-09-30)

`src/process_value.py` joins DA zonal price + LFDA (OEMP = DA-OZP + LFDA)
with fuel-mix on the available window and classifies each hour against
the OEB TOU/ULO tariff table, writing `data/processed/value_hourly.csv`
(1,848 hours, 2026-07-01 → 2026-09-16, the LFDA/DA-zonal overlap) and
`data/processed/value_summary.json`. Both are labelled "summer preview,
not annual" and carry NO headline gap metric. Dropped-record counts are
QA'd against the intended overlap window. The 2026-09-30 correction
changed summer TOU classifications (e.g. 11:00 Toronto on a summer Monday
is now on-peak, previously misclassified as mid-peak) and rebuilt both
outputs.

## Phase 2 directive (2026-09-29): legacy close-out + Phase 2 in 2A/2B/2C

- Phase 1 accepted; Cornwall confirmed a genuine RRR absence (final).
- The 9-row `ldc_decisions.csv` replaces the 6-row file. New decision types:
  `relicensed_as` (rename to current licence number, keep old in
  `legacy_licence_no`) and `unresolved_lookup` (keep polygon, flag
  `licence_not_current`).
- Centre Wellington → ED-2023-0202: agent-confirmed in the OEB licence list
  (Issued 2023-08-02), set final. Lakefront → ED-2023-0262: found by agent
  name search (Issued), recorded `relicensed_as`, final. Espanola → North Bay
  Hydro (ED-2003-0024) merge, final per OEB decision EB-2021-0312.
- **Espanola/RRR merge rule** (applies in 2B): in the analysis vintage, if
  both source and target have RRR rows, sum additive quantities; if only the
  target has rows, use the target alone. Record which case applied per LDC in
  `qa_log.csv`.
- Phase 2 runs 2A → 2B → 2C in order with a checkpoint report after each;
  stop conditions (Section D of the directive) halt work. Phase 3 waits for
  explicit approval.

## 1. Confirmed decisions
- **LFDA vs LFDC:** owner verified they are the same quantity. Use the
  `HourlyLFDA` feed, rename the column to `lfda_rate_mwh` on load, note the
  naming difference in the README. No reconciliation test beyond a fixture
  regression check.
- **Timezone:** `TIMEZONE_MODE = "EST_fixed"` (UTC-5, no DST) for **all** IESO
  market-report hours.
- **Node file schema:** the owner's file uses `Longitude, Latitude, Location`.
  Map `Location` to `node_id`. All 1,022 locations already match LMP nodes, so
  the mapping is confirmed. Log the ~32 LMP nodes with no coordinates.
  `zone_name` is derived (Section 3), not supplied.
- **LDC polygons:** owner's KMZ (`Electric_260825.kmz`) was independently
  validated and is **accepted**. Cleaned analysis copy
  `ldc_service_areas.geojson` supplied — see Section 4.
- **Tariffs:** use `retail_tariffs.csv` (Section 2).

## 2. Retail tariffs (`data/inputs/retail_tariffs.csv`)
Owner-approved starting file: 40 rows, two RPP terms (Nov 1 2024 – Oct 31 2025
and Nov 1 2025 – Oct 31 2026), covers the trailing-12-month headline window.
Schema changes from the original prompt (so it can be machine-joined):
`season`, `day_type` (`weekday` | `weekend_holiday`), numeric
`start_hour`/`end_hour` (end exclusive; overnight blocks split in two rows).
`delivery_variable_c_per_kwh` intentionally blank.

Handling rules:
1. **Classify using Toronto local wall-clock time, not EST.** RPP periods are
   defined in local clock time; IESO market data is EST-fixed. Convert each
   interval start to `America/Toronto` (from UTC) before applying
   `start_hour`/`end_hour`, `season`, `day_type`. Seasons: winter = Nov 1–Apr
   30, summer = May 1–Oct 31 (ULO is `all_year`).
2. **Holidays (assumption, configurable).** Nine treated as `weekend_holiday`:
   New Year's Day, Family Day, Good Friday, Victoria Day, Canada Day,
   Labour Day, Thanksgiving Day, Christmas Day, Boxing Day. Default: no Civic
   Holiday, no shift when a holiday falls on a weekend. Holiday list and shift
   flag live in `config.py`; cite the OEB definition and document the
   sensitivity (a handful of weekdays per year).
3. **Load validation.** Loader must fail (malformed-input policy) if any
   (term, plan, season, day_type) block does not cover exactly 24 hours, if
   terms overlap, or if a timestamp falls outside all terms (then: warn and
   mark the interval "no tariff", never extrapolate).
4. **Delivery charges.** Default headline offset is **commodity-only**
   (conservative lower bound). Support an optional second scenario
   ("delivery-inclusive") reading a `delivery_variable_c_per_kwh` column the
   owner will populate later for one named benchmark LDC. Label both
   scenarios. Do not fill delivery values.
5. **Next term.** OEB announces new RPP prices mid-October each year,
   effective Nov 1. Loader must handle an appended term; the pipeline must show
   a visible "tariff table ends on <date>" warning on Page 1 when the latest
   interval is within 30 days of the last term's end.

Source URLs are in the CSV. Phase 1 spot-check (2026-09-29): all 14 commodity
prices match the two OEB RPP price reports (rpp-price-report-20241018.pdf,
rpp-price-report-20251017.pdf). Result recorded; no edits needed.

## 3. Node-to-zone assignment
**Facts (verified):** IESO divides the transmission system into ten electrical
zones. The IESO Data Directory separately mentions nine *virtual transaction
zones* — a different concept; do not conflate them. A public project references
an IESO report named `PUB_NodeZoneMap` — **not found or publicly accessible as
of 2026-09-29** (searched the reports server root, `CA-PreAuction/`,
`CA-PostAuction/` and `DAHourlyEnergyLMP/` folders, the direct
`PUB_NodeZoneMap.{xml,csv}` URLs, and the web; this says nothing about whether
such a report exists internally).

**Procedure (stop at the first step reaching 90% coverage of LMP nodes):**
1. ~~Look for the IESO node-zone map report~~ — not found; skip.
2. **Capacity Auction Zone Table** (593 stations → ten zones, already located).
   Match station names to node names with documented normalization (uppercase,
   strip voltage/suffix tokens, exact stem match first, then conservative fuzzy
   with a high threshold). Tag `zone_method = "station_table"`. Fuzzy matches
   below threshold go to `data/processed/node_zone_review.csv`, not accepted.
3. **Geographic inference for gaps only.** k=3 nearest already-assigned nodes,
   distances in `EPSG:3161`, only if all three agree and the nearest is within
   25 km. Tag `zone_method = "knn_inferred"`, `confidence = "low"`.
   Assumption: zones follow major transmission interfaces, so neighbour
   agreement is a reasonable proxy away from zone boundaries.
4. Anything left stays `zone = "UNASSIGNED"` and is logged.

**Rules:** derive the zone list from the data (never hard-code ten); Page 3
headline zone aggregates use only `station_table`/node-map assignments
(inferred-inclusive version is a QA-log sensitivity only); publish the coverage
table (nodes by `zone_method`) in the README and `qa_log.csv`; if total
coverage excluding `knn_inferred` is below 90%, Page 3 stays node-level with a
visible note.

## 4. LDC service areas and LDC-to-zone crosswalk

### 4.1 KMZ validation results (owner, 2026-09-29; reproduce in Phase 1)
- Apparent source: OEB capacity-map (CCIM) service-area layer; attributes
  `Utility Name`, `OEB Licence No.`, `LDC_Type`, `CCIM_LDC_URL`. Filename and
  internal timestamps suggest a 2026-08-25 export (inferred, not stated).
- 61 placemarks, all `LDC_Type = Service Area`, all MultiPolygon (some with
  holes). 60 unique licences (`ED-YYYY-NNNN`); `ED-2021-0280` (GrandBridge)
  appears twice (former Energy+ and former Brantford Power) — dissolve by
  licence.
- 7 features had self-intersecting rings (GrandBridge/Energy+, Entegrus, Hydro
  Ottawa, Alectra, Elexicon, Hydro One Networks, Hydro One Remote Communities);
  all valid after `make_valid`; no empty geometries.
- Extent lon −95.15 to −74.34, lat 41.68 to 56.86: consistent with Ontario.
- Topology clean: only overlap above 0.01 km² is Alectra/Elexicon ≈ 0.01 km².
- 14/14 landmark tests pass; layer reflects recent mergers (see 4.4).
- Hydro One Networks (~518,000 km², 102 parts, 337 holes) and Hydro One Remote
  Communities (~471,000 km²) ≈ 97% of total area (~1.017M km²).
- **Not validated:** (a) the layer's source terms/licence, (b) completeness
  against the RRR filer list, (c) legal precision of boundaries. Treat
  polygons as **indicative**; say so on Pages 2 and 4.

### 4.2 Files
- `data/inputs/Electric_260825.kmz` — original, untouched (extracted 2026-09-29
  from the official OEB service-area ZIP).
- `data/inputs/ldc_service_areas.geojson` — cleaned analysis copy (60 features,
  one per licence, valid geometry, 5-decimal coordinates, ~4.6 MB). Properties:
  `licence_no, utility_name, ccim_url, n_source_features, source_file,
  provenance`. **Use this for all spatial joins.**
- `docs/data/ldc_service_areas_simplified.geojson` — simplify at 100 m
  tolerance in `EPSG:3161`, 4-decimal output, ~0.4 MB, **for Plotly rendering
  only**. Never for joins or area calculations.
- Provenance stays "OEB capacity-map layer; source and licence to be confirmed
  by owner before public release." Never remove it or the page caveat.

### 4.3 Ingestion and join rules
1. **Reproduction test (Phase 1 — done 2026-09-29, passing).** Re-derive the
   layer from the KMZ (read `doc.kml`; attribute table in each placemark's
   HTML description) and assert: 61 source features, 60 unique licences,
   7 repaired geometries, per-licence area within 0.1% of the supplied
   GeoJSON. Implemented in `src/kmz_reproduce.py`; measured max difference
   0.0144%.
2. **Join key is `OEB Licence No.`** First check whether RRR XML files carry a
   licence number field; if so, join on it. Otherwise join on normalized
   distributor name (drop "Formerly ..." text, punctuation, legal suffixes);
   write every non-exact match to `data/processed/ldc_name_map_review.csv`.
   Never accept fuzzy matches silently.
3. **Reconcile against RRR** in `ldc_reconciliation.csv`: licences in the layer
   absent from RRR, and RRR distributors absent from the layer. Expect the
   remote First Nation distributors (Attawapiskat, Fort Albany, Kashechewan)
   and possibly Hydro One Remote Communities to lack RRR data (a 2020 OEB
   yearbook listed those three as non-filers — confirm against current data,
   not assumed). Mark `remote = True`, exclude from per-1,000-customer metrics
   when customer counts are missing, list them on the page rather than drawing
   them as zero. [Phase 1 2026-09-29: confirmed against the 2024-2 RRR
   vintage — Attawapiskat, Fort Albany, Kashechewan, and Hydro One Remote
   Communities have no rows in any of the four tables; `remote = True` set
   for those four licences.]
4. **Hydro One Networks** is residual territory wrapped around other LDCs, not
   an urban distributor. Draw it, label it as such, treat as `multi_zone = True`
   on Page 4.
5. **No area-based metrics** (per-km² density, area-weighted shares) for the
   two Hydro One entities; their polygons are mostly unpopulated.
6. Choropleth colour scales must be clipped or log-scaled so Hydro One and the
   remote polygons do not dominate the legend.

### 4.4 Landmark regression tests (from validation; keep as fixtures)
Assert each point falls in the named distributor in the supplied layer. If a
test fails after a future layer refresh, mark it "to verify" rather than
editing the expectation, since mergers change ownership.
- CN Tower, Toronto (43.6426, −79.3871) → Toronto Hydro-Electric System
- Parliament Hill, Ottawa (45.4236, −75.7009) → Hydro Ottawa
- London City Hall (42.9849, −81.2453) → London Hydro
- Downtown Windsor (42.3149, −83.0364) → ENWIN Utilities
- Downtown Sudbury (46.4917, −80.9930) → Greater Sudbury Hydro
- Thunder Bay (48.3809, −89.2477) → Synergy North
- Kingston City Hall (44.2312, −76.4860) → Kingston Hydro
- Mississauga Square One (43.5931, −79.6418) → Alectra Utilities
- Kitchener City Hall (43.4516, −80.4925) → Enova Power
- Cambridge (43.3616, −80.3144) → GrandBridge Energy (ED-2021-0280)
- Oshawa (43.8971, −78.8658) → Oshawa PUC Networks
- Niagara Falls (43.0896, −79.0849) → Niagara Peninsula Energy
- Guelph (43.5448, −80.2482) → Alectra Utilities
- Hamilton City Hall (43.2557, −79.8711) → Alectra Utilities

(All 14 re-verified 2026-09-29 against the supplied GeoJSON.)

### 4.5 LDC-to-zone crosswalk (generated, not hand-made)
Build `data/processed/ldc_zone_crosswalk.csv`
(`licence_no, ldc_name, zone_name, weight, method, multi_zone`):
1. Spatially join assigned nodes (Section 3, excluding `knn_inferred`) to the
   cleaned polygons. Polygons have holes — a node inside an enclosed LDC
   belongs to that LDC, not the surrounding Hydro One polygon.
2. If an LDC contains assigned **load-type** nodes (use IESO node type if the
   LMP report or node table provides it; otherwise all nodes), set weights by
   share of nodes per zone. Assumption: node count is a proxy for load share
   (public nodal load data unavailable).
3. No assigned node → nearest assigned node within 30 km (`EPSG:3161`),
   weight 1.0, `method = "nearest_node"`.
4. Otherwise `zone = "UNASSIGNED"`.
5. **Multi-zone LDCs** (Hydro One Networks, possibly Alectra and others): keep
   multi-zone weights, flag `multi_zone = True`. On Page 4 plot them as hollow
   markers labelled multi-zone, excluded from rank correlation.
6. Support `data/inputs/ldc_zone_crosswalk_overrides.csv` (same schema);
   overrides win, each logged. Owner will review the ~20 largest LDCs by
   customer count.

**Scope Page 4** to those ~20 LDCs plus any LDC with an override. Do not
present unreviewed crosswalk rows as findings.

### 4.6 Resolved reconciliation decisions (owner, 2026-09-29)

Machine-readable: `data/inputs/ldc_decisions.csv` (schema `licence_no,
utility_name, decision, target_licence_no, reason, source_url, decided_by,
status`). Loaded by `src/ldc_decisions.py` as a validated input — malformed
file fails, missing file warns and leaves all decisions unresolved. Decisions
are never hard-coded.

| Licence | Distributor | Decision | Why |
|---|---|---|---|
| ED-2002-0530 | Orillia Power | `merge_into` ED-2003-0043 | Hydro One acquired it (closed 2020-09-01, integrated 2021-06-01); Hydro One's licence was amended to include the area (now the Orillia Rate Zone). RRR reports it inside Hydro One Networks. The KMZ polygon is a legacy licence area. |
| ED-2001-0090, ED-2003-0079, ED-2003-0081 | Attawapiskat, Fort Albany, Kashechewan | `exclude_no_rrr` | Remote First Nation distributors; confirmed non-filers in all four RRR tables. |
| ED-2003-0037 | Hydro One Remote Communities | `exclude_no_rrr` | Non-filer in the data. |
| ED-2004-0405 | Cornwall Electric | `exclude_no_rrr` (final) | Active licensed distributor (Fortis-owned, licence Issued to 2030) absent from RRR. Agent search 2026-09-29 found zero hits for `ED-2004-0405`, `cornwall`, `fortis`, `fortisontario` in all four RRR tables (the XML carries no licence-number field; its Fortis sibling Canadian Niagara Power does file, so the search is sound). Provisional → final. |

**How the decisions are applied** (`src/ldc_decisions.py`, output
`data/processed/ldc_analysis_layer.geojson`):
- Merges dissolve the source polygon into the target at load time; the
  supplied GeoJSON is untouched. All Hydro One data stays attributed to
  Hydro One, keeping numerators and denominators consistent. Analysis layer:
  **59 features** (60 licences minus the Orillia merge).
- Excluded LDCs are kept as polygons flagged `excluded = True`, drawn grey as
  "not reported" with an explanatory tooltip and listed under the map. Never
  plotted as zero; never in rank correlations or per-1,000-customer scales.

**Legacy-polygon check (agent, 2026-09-29, report only).** Compared all 60
polygon licences against the current OEB licensed-distributor list
(`https://www.oeb.ca/_html/xml/all_Licences_report_File.xml`, loaded
successfully). Four polygon licences are not current: ED-2002-0498 (Centre
Wellington Hydro), ED-2002-0502 (Espanola Regional Hydro), ED-2002-0530
(Orillia Power — expected, merged above), ED-2002-0545 (Lakefront Utilities).
Name mismatches for current licences are trivial punctuation only. No changes
made; owner to advise.

**Counts:** 60 source licences; 58 analysis features after the Orillia and
Espanola merges; 2 relicensed (Centre Wellington → ED-2023-0202, Lakefront →
ED-2023-0262, both agent-confirmed in the OEB licence list and final);
54 matched directly to RRR; Orillia covered through Hydro One Networks;
Espanola covered through North Bay Hydro (pre-merger RRR rows may exist —
see 2B merge rule); 5 without RRR data (excluded).

## 5. Additional implementation notes
- **OEB URL discovery.** Vintage folders change (e.g. `2024-2`). Parse the OEB
  Open Data landing page for current file links at fetch time, record the
  discovered vintage per dataset in `manifest.json`, never guess a next-year
  path. If the page structure changes and no links are found, warn and keep
  last good data.
- **iesometeo interaction.** The existing project treats IESO hours as Toronto
  local time. Do not import its processed timestamps. Re-derive the solar shape
  from raw IESO fuel-mix output using `EST_fixed`, or apply the documented
  correction. Add a test that the July solar peak falls between 12:00 and 14:00
  Toronto local time. Report separately (in
  `docs/IESOMETEO_TIMEZONE_NOTE.md`) whether iesometeo's summer timestamps
  appear shifted by one hour, so the owner can decide whether to fix that repo.
- **Contract list.** Use the `power-data` URL found in Phase 0. Validation-only;
  never summed with OEB capacity.

## 7. Phase 2B adoption inputs (2026-09-30)
`src/process_adoption.py` builds `data/processed/ldc_adoption.csv` (one row
per each of the 58 analysis-layer features, keyed by current `licence_no`)
and `data/processed/adoption_crosscheck.csv`.

- **Vintage / year.** OEB RRR vintage 2024-2; `data_year` = 2024 (latest year
  in every table; each table's own latest year is used if they ever disagree).
- **Numerator.** Net-metered customers and cumulative capacity from 2.1.14
  Table 1 plus embedded-generation facilities and capacity from 2.1.14
  Table 3; per-fuel columns from Table 4 (Solar, Wind, Water, Biomass,
  Fossil Fuel, Exporting Storage, Non-Exporting Storage, Other). All schema
  units are kW; converted to MW once at load. Headline metric: (net-metered
  + embedded) MW per 1,000 customers. The two OEB tables are separate
  reporting sections; adding them assumes no overlap, recorded here.
- **Denominator.** RRR 2.1.2, sum of `Total_Customers_or_Connections` over
  all rows with `Customer_or_Connections = "Customers"` (every rate class,
  not residential only).
- **Name bridge.** Phase 1 `ldc_reconciliation.csv` (licence -> RRR
  `Current_Company_Name`); relicensed features look up their
  `legacy_licence_no`; merged features look up target + merged licence(s).
- **Merge rule (directive A.4).** In 2024 only the target files rows for both
  merges, so North Bay Hydro is used alone for Espanola (ED-2002-0502) and
  Hydro One Networks alone for Orillia (ED-2002-0530); the applied case is
  recorded per LDC in `qa_log.csv`. A merged licence whose RRR name merely
  resolves to the target's current name via a historical alias does NOT
  count as "both have rows" (this was caught and fixed before commit:
  North Bay's data would otherwise have been doubled).
- **Exclusions / flags.** The 5 owner-excluded LDCs get `reported = False`
  and no metric (listed, never drawn as zero). Hydro One Networks
  (ED-2003-0043) is computed normally with `multi_zone = True` and is
  excluded from colour-scale bounds and the top/bottom sanity lists.
- **Contract cross-check.** The IESO contract list has no distributor column
  (only Closest City/Town), so LDC-level matching would require inventing a
  city->LDC map; the cross-check therefore compares province-level,
  in-service (`CO`) distribution-connected contract MW against OEB embedded
  MW by Table 4 fuel group. The two sources are different universes and are
  never blended. 2024-2 vs 2026-06-30 vintages differ. Discrepancies over
  20%: Water (0.68), Biomass (0.68), Fossil Fuel (0.60), Exporting Storage
  (1.65), Non-Exporting Storage (0.0 — behind-the-meter storage is not
  contracted), Other (0.08). Solar and Wind agree within 20%.
- **Result.** 53 LDCs with data, 5 excluded, 2 merged (targets used alone).

## 8. Phase 2C wholesale locational signal (2026-09-30)
`src/process_zones.py` builds the Page 3 inputs and the Page 4 crosswalk.

- **Retention.** DA LMP daily files run 2026-07-01 → 2026-09-30 (92 days);
  the processed file keeps a 32-day rolling window (711,190 rows, 1,055
  nodes, 2026-08-29 → 2026-10-01 UTC). The 30-day backfill requirement is
  met; realtime nodal history was not attempted.
- **Node-to-zone ladder.** (1) `PUB_NodeZoneMap` still 404s as of
  2026-09-30. (2) The IESO Capacity Auction Zone Table (PDF, last updated
  2026-07-16; extracted once to `data/processed/ieso_zone_table.csv`, 559
  stations) gives 10 electrical zones, derived from the file, never
  hard-coded. Conservative matching: longest normalized station-base
  prefix of the node ID (type suffixes SS/TS/GS/… stripped longest-first);
  bases claimed by stations in different zones (CUMBERLAND: OTTAWA vs
  SOUTHWEST) are excluded. Result: 572/1,055 nodes = 54.2% — the unmatched
  nodes are mostly generator/intertie-style nodes the station table does
  not list. **Stop condition fired** (below 90%): k=3 inference NOT
  applied, no zone aggregation; Page 3 falls back to the node-level view.
  Coverage by `zone_method` is published in `zone_coverage.csv`.
- **Per-node 30-day means.** `node_congestion_30d.csv`: 30-day rolling mean
  of daily means per node for LMP, congestion and loss components
  (2026-09-02 → 2026-10-01); negative values kept. This is the latest
  node-level output; no long-term node history is committed.
- **LDC-zone crosswalk.** Each LDC's zone weights are the share of zoned
  LMP nodes with coordinates inside its polygon
  (`in_polygon_zoned_nodes`; weights sum to 1 per LDC; multi-zone LDCs get
  several rows). 40/58 LDCs zoned; 18 small LDCs contain no zoned nodes and
  have no rows. `data/inputs/ldc_zone_crosswalk_overrides.csv` (columns
  `ldc_id, zone_name, weight`) overrides when present; absent on
  2026-09-30. Sanity: Toronto Hydro → TORONTO; Hydro Ottawa primary →
  OTTAWA (its polygon also holds 2 EAST-zoned stations per the zone
  table).
- **Review sheet.** `crosswalk_review_top20.csv` lists the ~20 largest
  LDCs by customers with assigned zones, weights, method and node counts;
  every row is `pending_owner_review` — assignments are NOT findings.
- **Wording.** Every nodal exhibit calls LMP a wholesale locational
  signal, never a retail price or a distribution-deferral value.

## 6. Phase 1 acceptance (directive additions)
Beyond the build prompt's acceptance tests:
- Tariff CSV loads, passes the 24-hour coverage check, and the OEB price
  spot-check is recorded. (**Done 2026-09-29: all 14 prices match.**)
- Node file loads with `Location` mapped to `node_id`; the ~32 unmatched LMP
  nodes are logged.
- KMZ reproduction test passes (61 features, 60 licences, 7 repairs,
  per-licence area within 0.1% of the supplied GeoJSON — measured 0.0144%);
  simplified copy generated; licence/name join to RRR reported in
  `ldc_reconciliation.csv` and `ldc_name_map_review.csv`. **Done 2026-09-29.**
- Report Phase 1 results, plus any "to verify" items, before starting Phase 2.

## 7. Phase 4 — site renderer + build workflow (2026-09-30)

- **Release gate.** `PUBLIC_RELEASE_APPROVED = False` in `src/config.py`.
  While false, every page (`docs/index.html` and all four chart pages)
  carries a "DRAFT: not for public release. Data licences pending." banner
  and `<meta name="robots" content="noindex">`. Gates are listed in
  `RELEASE_CHECKLIST.md` (polygon licence, node-coordinate provenance,
  Page 4 review, commentary, README attribution, repo visibility).
- **Renderer** (`src/render_dashboard.py` → `docs/index.html`): header, a
  data-status panel computed from files (per-feed collection times from
  `manifest.json`; OEMP-era days/months/window from `value_summary.json`;
  node coverage 1,022/1,055; node-to-zone 54.2% vs the 90% bar; RRR vintage
  2024-2 / data year 2024; LDC shown/excluded/review counts), tabs for
  Pages 1–4 plus a Methods tab, per-page status badges, chart iframes with
  "Open chart full screen" links, author commentary from
  `content/commentary_page{1..4}.md` (placeholders only; never written by
  the agent), machine-filled key-figures blocks, and per-page sources/caveats
  parsed from the chart captions (charts stay the single source of truth).
  Methods tab covers definitions, sources with vintages/retention, tariff
  terms, exclusions/merges/relicensed LDCs, fallbacks in force, known DA LMP
  gaps (computed from `da_lmp_hourly.csv` day coverage), the EST_fixed
  timezone convention, and attribution. Footer: attribution, build
  timestamp (UTC + Toronto), commit hash. Light/dark via CSS variables +
  `prefers-color-scheme`; responsive to phone width.
- **Build workflow** (`.github/workflows/build.yml`, "build-site"):
  separate from `collect.yml` (data-only). Triggers on successful
  `collect-price-history` completion, `workflow_dispatch`, and pushes to
  `src/**`, `content/**`, `templates/**`. Rebuilds derived outputs,
  charts, and the dashboard, committing `docs/` only when changed, with
  `git pull --rebase` and one push retry against collect's commits. Shares
  the `der-monitor-data` concurrency group (no cancel). Build takes seconds
  excluding dependency install. Publishing is via Pages "Deploy from a
  branch: main, /docs" — enabled only after the release checklist completes.
- **Retention.** IESO indexes retain roughly 3 months of daily files;
  `collect.yml` accumulates history daily into `data/processed/`.
- **Annual headline rule.** Annual headline metrics appear only once 12
  months of OEMP-era data exist; until then Page 1 is a labelled summer
  preview with no headline percentage.
