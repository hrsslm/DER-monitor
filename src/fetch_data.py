"""Phase 1: fetch upstream data and record it in the manifest.

What this script does:
  1. Downloads the latest file from each IESO feed folder (folder indexes
     are read dynamically — filenames are never hard-coded).
  2. Discovers the current OEB RRR vintage from the OEB open-data pages and
     downloads the five RRR XML files.
  3. Downloads the IESO Active Generation Contract List and the OEB
     electricity service-area map (source of the LDC polygons).
  4. Validates the user-supplied inputs (tariffs, node locations, polygons),
     cross-checks node ids against the fresh LMP node list, and logs anything
     unmatched to data/processed/qa_log.csv.
  5. Writes data/processed/manifest.json recording, for every file: its URL,
     ETag/Last-Modified, SHA-256, fetch time and row count.

Failure policy: a missing or delayed upstream report logs a warning, keeps
the last good data, and the script still exits 0. A malformed *supplied*
input raises loudly (nonzero exit) instead of guessing.

Idempotency: a rerun with no upstream changes downloads nothing — files are
skipped when the server's ETag/Last-Modified match the manifest.
"""

import csv
import datetime as dt
import hashlib
import http.client
import json
import re
import shutil
from html import unescape as html_unescape
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pandas as pd
import geopandas as gpd
from shapely.geometry import Polygon, MultiPolygon

from config import (
    PROCESSED_DIR, RAW_DIR, INPUTS_DIR, DOCS_DIR,
    IESO_REPORTS_ROOT, CONTRACT_LIST_URL, OEB_OPEN_DATA_PAGE,
    HTTP_TIMEOUT,
)
from inputs import load_tariffs, load_nodes, load_ldc_polygons, unmatched_lmp_nodes
from qa_log import log_rows

# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

MANIFEST_PATH = PROCESSED_DIR / "manifest.json"


def load_manifest():
    """Read the manifest, or start empty on first run."""
    if MANIFEST_PATH.exists():
        return json.loads(MANIFEST_PATH.read_text())
    return {"generated_at": None, "oeb_vintage": None, "files": {}}


def save_manifest(manifest):
    manifest["generated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2) + "\n")


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
# NOTE: downloads use urllib, not requests. The IESO reports server hangs up
# cleanly partway through files often enough that requests dies mid-download
# while urllib/curl complete; urllib also makes HTTP Range resumes simple.
# See AGENTS.md for the full story.

USER_AGENT = "ontario-der-monitor/phase1"


def encode_url(url):
    """Percent-encode a URL's path/query (urllib rejects raw spaces)."""
    parts = urllib.parse.urlsplit(url)
    path = urllib.parse.quote(parts.path, safe="/")
    query = urllib.parse.quote(parts.query, safe="=&")
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, path, query, parts.fragment))

# Errors worth retrying (network hiccups, proxy drops, truncated reads).
RETRYABLE = (
    urllib.error.URLError,
    http.client.HTTPException,
    ConnectionError,
    TimeoutError,
)


def warn(message):
    """Log a warning to stdout (the pipeline never crashes on upstream gaps)."""
    print(f"WARNING: {message}", flush=True)


def fetch_text(url):
    """GET a page and return its text, or None after retries."""
    url = encode_url(url)
    for attempt in range(4):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
                return response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                warn(f"404 not found: {url}")
                return None
            warn(f"fetch failed (attempt {attempt + 1}/4): {url}: HTTP {exc.code}")
        except RETRYABLE as exc:
            warn(f"fetch failed (attempt {attempt + 1}/4): {url}: {exc}")
        time.sleep(2 * (attempt + 1))
    return None


def fetch_index_head(url, max_bytes=65536):
    """Read the head of a directory index, newest entries first.

    IESO folder indexes are Apache listings; ?C=M;O=D sorts by last-modified
    descending so the newest files come first. Only the first max_bytes are
    read: for huge indexes (DAHourlyEnergyLMP) the server drops the
    connection after ~32KB, and the partial body is still usable because the
    entries we need are at the top. Returns text, or None after retries.
    """
    base = encode_url(url).rstrip("/") + "/?C=M;O=D"
    for attempt in range(4):
        try:
            request = urllib.request.Request(
                base, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request,
                                        timeout=HTTP_TIMEOUT) as response:
                try:
                    data = response.read(max_bytes)
                except http.client.IncompleteRead as exc:
                    # Server hung up mid-listing; the head is what we need.
                    data = exc.partial
            return data.decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                warn(f"404 not found: {url}")
                return None
            warn(f"index fetch failed (attempt {attempt + 1}/4): {url}: "
                 f"HTTP {exc.code}")
        except RETRYABLE as exc:
            warn(f"index fetch failed (attempt {attempt + 1}/4): {url}: {exc}")
        time.sleep(2 * (attempt + 1))
    return None


def _index_file_meta(html, filename):
    """Pull (last-modified, size) for one file from an Apache index listing."""
    pattern = re.compile(
        r'href="' + re.escape(filename) + r'">.*?</a>\s+'
        r"(\d{2}-[A-Za-z]{3}-\d{4} \d{2}:\d{2})\s+(\S+)",
        re.DOTALL,
    )
    match = pattern.search(html or "")
    if not match:
        return None, None
    return match.group(1), match.group(2)


def head_validators(url):
    """Return (etag, last_modified) from a HEAD request, or (None, None).

    Retried: a transient HEAD failure must not trigger a needless re-download
    of a file we already hold.
    """
    for attempt in range(3):
        try:
            request = urllib.request.Request(
                encode_url(url), headers={"User-Agent": USER_AGENT}, method="HEAD"
            )
            with urllib.request.urlopen(request,
                                        timeout=HTTP_TIMEOUT) as response:
                headers = response.headers
                return headers.get("ETag"), headers.get("Last-Modified")
        except Exception as exc:
            if attempt == 2:
                warn(f"HEAD failed, validators unknown: {url}: {exc}")
            time.sleep(2 * (attempt + 1))
    return None, None
    """Return (etag, last_modified) from a HEAD request, or (None, None)."""
    try:
        request = urllib.request.Request(
            encode_url(url), headers={"User-Agent": USER_AGENT}, method="HEAD"
        )
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            headers = response.headers
            return headers.get("ETag"), headers.get("Last-Modified")
    except Exception:
        return None, None


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(url, dest, attempts=4):
    """Download a URL to dest, resuming partial files with HTTP Range.

    The IESO server sometimes hangs up cleanly partway through a file, so a
    short read is resumed (not restarted) and the final byte count is always
    verified against Content-Length when the server sends one. A leftover
    .part file is kept in data/raw/ (git-ignored) so the next run resumes it.
    Large directory indexes may need more attempts than data files.
    Returns True on success, False on failure (caller warns, keeps old data).
    """
    dest = Path(dest)
    tmp = dest.with_suffix(dest.suffix + ".part")
    url = encode_url(url)
    for attempt in range(attempts):
        try:
            start = tmp.stat().st_size if tmp.exists() else 0
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            if start:
                request.add_header("Range", f"bytes={start}-")
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
                if response.status == 206 and start:
                    mode, downloaded = "ab", start
                else:
                    # Server ignored Range (or fresh start): begin over.
                    mode, downloaded = "wb", 0
                length = response.headers.get("Content-Length")
                total = int(length) + downloaded if length else None
                with open(tmp, mode) as handle:
                    while True:
                        chunk = response.read(1 << 20)
                        if not chunk:
                            break
                        handle.write(chunk)
                        downloaded += len(chunk)
            if total is not None and downloaded != total:
                warn(f"incomplete download ({downloaded}/{total} bytes): {url}")
                continue  # next attempt resumes where this one stopped
            tmp.replace(dest)
            return True
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                warn(f"404 not found (upstream report missing or delayed): {url}")
                return False
            warn(f"download failed (attempt {attempt + 1}/{attempts}): "
                 f"{url}: HTTP {exc.code}")
        except RETRYABLE as exc:
            warn(f"download failed (attempt {attempt + 1}/{attempts}): {url}: {exc}")
        time.sleep(2 * (attempt + 1))
    return False


# ---------------------------------------------------------------------------
# Row counting (for the manifest's row count)
# ---------------------------------------------------------------------------

def count_rows(path):
    """Best-effort record count: CSV data rows, XML record elements, xlsx rows."""
    path = Path(path)
    try:
        if path.suffix.lower() == ".csv":
            with open(path, newline="", encoding="utf-8-sig") as handle:
                reader = csv.reader(handle)
                rows = [r for r in reader if any(c.strip() for c in r)]
            if not rows:
                return 0
            # Some IESO CSVs start with a one-row metadata header (fewer
            # columns than the real header, e.g. "CREATED AT ...").
            skip = 1 if len(rows[0]) < len(rows[1]) else 0
            return max(0, len(rows) - skip - 1)  # minus the header row
        if path.suffix.lower() == ".xml":
            root = ET.parse(path).getroot()
            # IESO files wrap hour records in <DocBody> alongside one
            # <DeliveryDate> element; OEB files list records under <dataroot>.
            body = root.find("{http://www.ieso.ca/schema}DocBody")
            if body is not None:
                records = [c for c in body
                           if c.tag != "{http://www.ieso.ca/schema}DeliveryDate"]
            else:
                records = list(root)
            return len(records)
        if path.suffix.lower() == ".xlsx":
            # The IESO contract list keeps its data on the "Contract Data"
            # sheet (the first sheet is a disclaimer). Fall back to the
            # largest sheet for anything else.
            book = pd.ExcelFile(path)
            if "Contract Data" in book.sheet_names:
                sheet = "Contract Data"
            else:
                sizes = {name: book.parse(name, header=None).dropna(how="all").shape[0]
                         for name in book.sheet_names}
                sheet = max(sizes, key=sizes.get)
            frame = book.parse(sheet, header=None).dropna(how="all")
            return int(frame.shape[0])
    except Exception as exc:  # counting must never break the fetch
        warn(f"could not count rows in {path.name}: {exc}")
    return None


# ---------------------------------------------------------------------------
# IESO feeds
# ---------------------------------------------------------------------------

# feed key -> (folder on the reports server, filename prefix in the index)
IESO_FEED_FOLDERS = {
    "da_zonal": ("DAHourlyOntarioZonalPrice", "PUB_DAHourlyOntarioZonalPrice_"),
    "lfda": ("HourlyLFDA", "PUB_HourlyLFDA"),
    "da_lmp": ("DAHourlyEnergyLMP", "PUB_DAHourlyEnergyLMP_"),
    "vg_forecast": ("VGForecastSummary", "PUB_VGForecastSummary_"),
}


def latest_ieso_file(folder, prefix):
    """Pick the latest file from an IESO folder index.

    Rules: a global link (no date, e.g. PUB_HourlyLFDA.csv) is the rolling
    latest and wins; otherwise the newest date wins; for one date the
    non-versioned file is the final revision and wins over _vN.

    Returns (filename, index_mtime, index_size); the mtime/size come from the
    index listing and act as weak change validators for files whose server
    sends no ETag/Last-Modified. (None, None, None) when the index is
    unreachable.
    """
    html = fetch_index_head(f"{IESO_REPORTS_ROOT}/{folder}/")
    if html is None:
        return None, None, None
    names = sorted(set(re.findall(r'href="([^"]+)"', html)))
    candidates = [n for n in names if n.startswith(prefix)]
    if not candidates:
        warn(f"no files matching {prefix} in {folder}/")
        return None, None, None
    # Global (dateless) link wins: it always resolves to the latest period.
    chosen = None
    for name in candidates:
        if re.fullmatch(re.escape(prefix) + r"\.(xml|csv)", name):
            chosen = name
            break

    def sort_key(name):
        date = re.search(r"(\d{8}|\d{4})", name)
        version = re.search(r"_v(\d+)", name)
        # Non-versioned (final) sorts after any _vN for the same date.
        return (date.group(1) if date else "", 1 if not version else 0,
                int(version.group(1)) if version else 0)

    if chosen is None:
        chosen = max(candidates, key=sort_key)
    mtime, size = _index_file_meta(html, chosen)
    return chosen, mtime, size


def fetch_ieso_feed(feed_key, manifest):
    """Download the latest file for one IESO feed. Returns True if attempted."""
    folder, prefix = IESO_FEED_FOLDERS[feed_key]
    filename, mtime, size = latest_ieso_file(folder, prefix)
    if filename is None:
        return False  # warning already logged
    url = f"{IESO_REPORTS_ROOT}/{folder}/{filename}"
    dest = RAW_DIR / "ieso" / filename
    dest.parent.mkdir(parents=True, exist_ok=True)
    manifest_key = f"ieso/{filename}"
    # Weak validators: the reports server sends no ETag/Last-Modified, so the
    # index listing's mtime+size stand in. Documented limitation: a change
    # within the same minute and size bucket would be missed.
    weak = {"index_mtime": mtime, "index_size": size} if mtime else None
    return fetch_one(url, dest, manifest_key, manifest, weak_validators=weak)


def fetch_one(url, dest, manifest_key, manifest, weak_validators=None):
    """Download url to dest unless the manifest says it is unchanged.

    Change detection, strongest first: ETag, Last-Modified, then the
    caller-supplied weak validators (index mtime/size or Content-Length).
    Returns True if a download was attempted (success or failure), False if
    skipped as unchanged.
    """
    entry = manifest["files"].get(manifest_key, {})
    etag, last_modified = head_validators(url)
    if entry and dest.exists():
        # Compare weak validators against the stored values BEFORE learning
        # the current ones; otherwise every run would "match" itself.
        weak_match = (weak_validators and all(
            entry.get(key) == value
            for key, value in weak_validators.items()))
        if weak_validators:
            # Remember every validator we learn, so a later run can skip
            # even when the stronger ones are temporarily unavailable.
            entry.update(weak_validators)
        if etag and entry.get("etag") == etag:
            print(f"unchanged (ETag), skipping: {manifest_key}")
            return False
        if last_modified and entry.get("last_modified") == last_modified:
            print(f"unchanged (Last-Modified), skipping: {manifest_key}")
            return False
        if weak_match:
            print(f"unchanged (weak validators), skipping: {manifest_key}")
            return False
    print(f"downloading: {url}")
    if not download(url, dest):
        return True  # failed; warning logged, old data kept
    new_etag, new_last_modified = head_validators(url)
    manifest["files"][manifest_key] = {
        "url": url,
        "etag": new_etag or etag,
        "last_modified": new_last_modified or last_modified,
        "sha256": sha256_of(dest),
        "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "bytes": dest.stat().st_size,
        "rows": count_rows(dest),
    }
    if weak_validators:
        manifest["files"][manifest_key].update(weak_validators)
    entry_rows = manifest["files"][manifest_key]["rows"]
    print(f"  saved {dest.name} ({dest.stat().st_size} bytes, {entry_rows} rows)")
    return True


# ---------------------------------------------------------------------------
# OEB RRR files
# ---------------------------------------------------------------------------

# Detail pages on the OEB open-data site for the sections we need, and the
# filename fragment identifying each file. The vintage folder (e.g. 2024-2)
# is discovered from these pages at fetch time — never guessed.
OEB_DATASETS = {
    "rrr_2114_t1": (
        "electricity-reporting-record-keeping-requirements-rrr-sections-2114-and-21141-net",
        "Table 1 Net Metering",
    ),
    "rrr_2114_t3": (
        "electricity-reporting-record-keeping-requirements-rrr-sections-2114-and-21141-net",
        "Table 3 Embedded Generation",
    ),
    "rrr_2114_t4": (
        "electricity-reporting-record-keeping-requirements-rrr-sections-2114-and-21141-net",
        "Table 4 Embedded Generation by Type",
    ),
    "rrr_2154": (
        "electricity-reporting-record-keeping-requirements-rrr-section-2154-demand-and-revenue",
        "Demand and Revenue",
    ),
    "rrr_212": (
        "electricity-reporting-record-keeping-requirements-rrr-section-212-market-monitoring",
        "Customers",
    ),
}
OEB_BASE = "https://www.oeb.ca"


def discover_oeb_files():
    """Find the current download URL for each OEB dataset.

    Parses the OEB open-data detail pages for links shaped like
    documents/opendata/rrr/<vintage>/<file>. Prefers the newest vintage
    folder (e.g. 2024-2) whose filename starts with "ED ". Returns
    (vintage, {dataset: url}); on any failure returns (None, {}) and the
    caller keeps the manifest's last-known URLs.
    """
    found = {}
    vintage = None
    for dataset, (page_slug, fragment) in OEB_DATASETS.items():
        html = fetch_text(f"{OEB_BASE}/open-data/{page_slug}")
        if html is None:
            continue
        links = re.findall(r'href="(https://www\.oeb\.ca/documents/opendata/rrr/[^"]+)"', html)
        # href attributes are HTML-escaped (e.g. "Customers &amp; Connections");
        # unescape them before using the links as download URLs.
        links = [html_unescape(l) for l in links]
        # Keep vintage-folder links whose filename matches this dataset.
        options = []
        for link in links:
            m = re.match(r".*/rrr/([^/]+)/([^/]+)$", link)
            if m and fragment.lower() in m.group(2).lower():
                options.append((m.group(1), link))
        if not options:
            warn(f"OEB discovery: no download link for {dataset} on {page_slug}")
            continue
        # Newest vintage first; within a vintage prefer the "ED "-prefixed file.
        options.sort(key=lambda o: (o[0], "/ED " in o[1]), reverse=True)
        vintage_here, url = options[0]
        found[dataset] = url
        vintage = vintage_here if vintage is None else max(vintage, vintage_here)
        print(f"OEB discovery: {dataset} -> {url}")
    return vintage, found


def fetch_oeb_rrr(manifest):
    """Download the four RRR XML files at the discovered vintage."""
    vintage, urls = discover_oeb_files()
    if not urls:
        warn("OEB discovery found no files; keeping last good data")
        return
    manifest["oeb_vintage"] = vintage
    for dataset, url in urls.items():
        filename = url.rsplit("/", 1)[-1]
        dest = RAW_DIR / "oeb" / filename
        dest.parent.mkdir(parents=True, exist_ok=True)
        fetch_one(url, dest, f"oeb/{filename}", manifest)


# ---------------------------------------------------------------------------
# One-off files
# ---------------------------------------------------------------------------

OEB_MAP_ZIP_URL = (
    "https://www.oeb.ca/documents/opendata/"
    "open-data-electricity-map-20260825.zip"
)


def fetch_contract_list(manifest):
    dest = RAW_DIR / "ieso" / "IESO-Active-Contracted-Generation-List.xlsx"
    dest.parent.mkdir(parents=True, exist_ok=True)
    # ieso.ca sends no ETag/Last-Modified; Content-Length from HEAD is the
    # only change signal. Weak but documented: a same-length change is
    # missed (the file is republished rarely).
    length = None
    try:
        request = urllib.request.Request(
            encode_url(CONTRACT_LIST_URL), headers={"User-Agent": USER_AGENT},
            method="HEAD",
        )
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            raw = response.headers.get("Content-Length")
            length = int(raw) if raw else None
    except Exception as exc:
        warn(f"contract list HEAD failed: {exc}")
    weak = {"content_length": length} if length else None
    fetch_one(CONTRACT_LIST_URL, dest, "ieso/contract_list.xlsx", manifest,
              weak_validators=weak)


def fetch_oeb_map(manifest):
    """The official OEB service-area map (source of the LDC polygons).

    The directive requires data/inputs/Electric_260825.kmz to be kept as the
    untouched original; it is extracted from the ZIP (never modified) so the
    Phase 1 KMZ reproduction test can re-derive the layer from it.
    """
    dest = RAW_DIR / "oeb" / "open-data-electricity-map-20260825.zip"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not fetch_one(OEB_MAP_ZIP_URL, dest, "oeb/service_area_map.zip", manifest):
        return
    with zipfile.ZipFile(dest) as archive:
        kmz_names = [n for n in archive.namelist()
                     if n.lower().endswith(".kmz")]
        if not kmz_names:
            warn("no KMZ found inside the OEB service-area ZIP")
            return
        target = INPUTS_DIR / Path(kmz_names[0]).name
        if target.exists():
            print(f"KMZ already retained: {target.name}")
            return
        with archive.open(kmz_names[0]) as src, open(target, "wb") as out:
            shutil.copyfileobj(src, out)
        print(f"KMZ retained untouched: {target} "
              f"({target.stat().st_size} bytes)")


# ---------------------------------------------------------------------------
# Input validation + QA log
# ---------------------------------------------------------------------------

QA_LOG_NAMESPACE = ("phase1", "node_locations")


def qa_log(entries):
    """Write QA-log entries via the shared qa_log module.

    Each entry is a dict with keys: phase, dataset, action, record_id,
    count, note. Rows are replaced (not duplicated) on rerun.
    """
    log_rows(entries)


def validate_inputs(manifest):
    """Validate supplied inputs and log unmatched LMP nodes to the QA log."""
    tariffs = load_tariffs(INPUTS_DIR / "retail_tariffs.csv")
    print(f"tariffs: {len(tariffs)} rows, "
          f"{tariffs['effective_from'].nunique()} terms, "
          f"plans {sorted(tariffs['plan'].unique())} — 24h coverage OK")

    nodes = load_nodes(INPUTS_DIR / "ieso_node_locations.csv")
    print(f"nodes: {len(nodes)} locations (Location -> node_id)")

    polys = load_ldc_polygons(INPUTS_DIR / "ldc_service_areas.geojson")
    print(f"LDC polygons: {len(polys)} licences, all geometries valid")

    # Cross-check: every LMP pricing location should have coordinates.
    lmp_files = [k for k in manifest["files"] if k.startswith("ieso/PUB_DAHourlyEnergyLMP_")]
    if lmp_files:
        latest = sorted(lmp_files)[-1]
        lmp_path = RAW_DIR / latest
        # The LMP CSV starts with a one-row metadata header, then the header.
        frame = pd.read_csv(lmp_path, skiprows=1)
        lmp_nodes = frame["Pricing Location"].dropna().unique().tolist()
        missing = unmatched_lmp_nodes(lmp_nodes, nodes)
        print(f"LMP locations: {len(lmp_nodes)}; without coordinates: {len(missing)}")
        entries = [{
            "phase": "phase1", "dataset": "node_locations",
            "action": "unmatched_no_coordinates", "record_id": node,
            "count": 1,
            "note": "LMP pricing location has no coordinates in "
                    "ieso_node_locations.csv",
        } for node in missing]
        entries.append({
            "phase": "phase1", "dataset": "node_locations",
            "action": "unmatched_no_coordinates_summary", "record_id": "ALL",
            "count": len(missing),
            "note": f"{len(missing)} of {len(lmp_nodes)} LMP pricing locations "
                    "have no coordinates; logged per owner decision",
        })
        qa_log(entries)
    else:
        warn("no LMP file in manifest; skipping node cross-check")

    # Rendering-only simplified copy: prefer the 58-feature analysis layer
    # (post-merge licences) so it matches the joins; fall back to the raw
    # inputs on a Phase 1 bootstrap where the analysis layer is absent.
    analysis = PROCESSED_DIR / "ldc_analysis_layer.geojson"
    if analysis.exists():
        build_simplified_geojson(gpd.read_file(analysis))
    else:
        build_simplified_geojson(polys)


def _round_ring(ring):
    """Round a linear ring to 4 decimals, dropping consecutive duplicates
    that rounding may collapse into each other."""
    pts = [(round(x, 4), round(y, 4)) for x, y in ring.coords]
    cleaned = []
    for point in pts:
        if not cleaned or point != cleaned[-1]:
            cleaned.append(point)
    return cleaned


def _round_geom_4dp(geom):
    """Round all coordinates of a (Multi)Polygon to 4 decimal places."""
    if geom.geom_type == "Polygon":
        parts = [geom]
    elif geom.geom_type == "MultiPolygon":
        parts = list(geom.geoms)
    else:
        return geom
    rounded = []
    for part in parts:
        exterior = _round_ring(part.exterior)
        holes = [_round_ring(r) for r in part.interiors]
        holes = [h for h in holes if len(h) >= 4]
        if len(exterior) >= 4:
            rounded.append(Polygon(exterior, holes))
    if not rounded:
        return geom  # never collapse a polygon away; keep the original
    return rounded[0] if geom.geom_type == "Polygon" else MultiPolygon(rounded)


def _polygons_only(geom):
    """Keep just the polygonal parts of a repaired geometry."""
    if geom.geom_type == "Polygon":
        return MultiPolygon([geom])
    if geom.geom_type == "MultiPolygon":
        return geom
    parts = []
    for part in geom.geoms:
        if part.geom_type == "Polygon":
            parts.append(part)
        elif part.geom_type == "MultiPolygon":
            parts.extend(part.geoms)
    return MultiPolygon(parts)


def build_simplified_geojson(polys):
    """Write a light copy of the LDC polygons for Plotly rendering only.

    Simplified at 100 m tolerance in EPSG:3161, coordinates rounded to
    4 decimals (~0.4 MB). Never used for joins or area calculations.
    """
    out = DOCS_DIR / "data" / "ldc_service_areas_simplified.geojson"
    out.parent.mkdir(parents=True, exist_ok=True)
    metric = polys.to_crs("EPSG:3161")
    metric["geometry"] = metric.geometry.simplify(100)
    wgs = metric.to_crs("EPSG:4326")
    def _round_and_repair(geom):
        """Round to 4 decimals; if rounding breaks validity, repair and
        round again until the geometry is both valid and 4-decimal."""
        from shapely.validation import make_valid
        rounded = _round_geom_4dp(geom)
        for _ in range(3):
            if rounded.is_valid:
                return rounded
            rounded = _round_geom_4dp(_polygons_only(make_valid(rounded)))
        return rounded

    repaired_idx = wgs[~wgs.geometry.apply(
        lambda g: _round_geom_4dp(g).is_valid)].index
    wgs["geometry"] = wgs.geometry.apply(_round_and_repair)
    if len(repaired_idx):
        log_rows([{
            "phase": "phase1", "dataset": "ldc_service_areas_simplified",
            "action": "simplified_geometry_repaired", "record_id": row["licence_no"],
            "count": 1,
            "note": "rendering-only copy: self-intersection introduced by "
                    "4-decimal rounding; repaired with make_valid",
        } for _, row in wgs.loc[repaired_idx].iterrows()])
    assert wgs.geometry.is_valid.all(), "simplified copy still invalid"
    wgs.to_file(out, driver="GeoJSON")
    print(f"simplified polygons for rendering: {out} "
          f"({out.stat().st_size / 1e6:.1f} MB)")


# ---------------------------------------------------------------------------
# Tariff spot-check record
# ---------------------------------------------------------------------------

TARIFF_SPOTCHECK = {
    "checked_at": "2026-09-29",
    "method": "hand-compared all 14 commodity_c_per_kwh values in "
              "data/inputs/retail_tariffs.csv against the two OEB RPP price "
              "reports (rpp-price-report-20241018.pdf, "
              "rpp-price-report-20251017.pdf)",
    "result": "pass",
    "mismatches": [],
}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    """Run the fetch pipeline.

    Failure policy: malformed *supplied* input fails clearly with a nonzero
    exit; missing/delayed *upstream* reports only warn (each fetch step
    already handles that internally) and the run exits 0 with old data
    retained.
    """
    try:
        _run()
    except ValueError as exc:
        print(f"ERROR: malformed supplied input: {exc}", flush=True)
        return 1
    return 0


def _run():
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest()

    print("== IESO feeds ==")
    for feed_key in IESO_FEED_FOLDERS:
        fetch_ieso_feed(feed_key, manifest)

    print("== IESO contract list ==")
    fetch_contract_list(manifest)

    print("== OEB RRR files ==")
    fetch_oeb_rrr(manifest)

    print("== OEB service-area map ==")
    fetch_oeb_map(manifest)

    print("== input validation ==")
    validate_inputs(manifest)

    (PROCESSED_DIR / "tariff_spotcheck.json").write_text(
        json.dumps(TARIFF_SPOTCHECK, indent=2) + "\n"
    )

    save_manifest(manifest)
    print(f"manifest: {len(manifest['files'])} files, "
          f"oeb_vintage={manifest.get('oeb_vintage')}")
    print("done")


if __name__ == "__main__":
    sys.exit(main())
