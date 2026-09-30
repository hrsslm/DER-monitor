"""Tests for src/fetch_data.py: index parsing, downloads, manifest logic.

Network access is limited to a local test HTTP server, so these tests run
offline (except the IESO/OEB discovery tests, which are marked separately).
"""
import json
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

import fetch_data
from fetch_data import (
    latest_ieso_file, count_rows, download, fetch_one, discover_oeb_files,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


# ---------------------------------------------------------------------------
# latest_ieso_file: pure index-parsing logic (fetch_index_head is monkeypatched)
# ---------------------------------------------------------------------------

def _index_html(names):
    rows = "".join(
        f'<a href="{n}">{n}</a>         29-Sep-2026 08:20  158K  x\n'
        for n in names)
    return "<html><pre>" + rows + "</pre></html>"


def test_latest_prefers_global_link(monkeypatch):
    monkeypatch.setattr(
        fetch_data, "fetch_index_head",
        lambda url, max_bytes=65536: _index_html(
            ["PUB_HourlyLFDA.csv", "PUB_HourlyLFDA_2026.csv"]),
    )
    name, mtime, size = latest_ieso_file("HourlyLFDA", "PUB_HourlyLFDA")
    assert name == "PUB_HourlyLFDA.csv"
    assert (mtime, size) == ("29-Sep-2026 08:20", "158K")


def test_latest_picks_newest_date_then_final_revision(monkeypatch):
    monkeypatch.setattr(
        fetch_data, "fetch_index_head",
        lambda url, max_bytes=65536: _index_html([
            "PUB_DAHourlyOntarioZonalPrice_20260928.xml",
            "PUB_DAHourlyOntarioZonalPrice_20260929_v1.xml",
            "PUB_DAHourlyOntarioZonalPrice_20260929.xml",
        ]),
    )
    got = latest_ieso_file("DAHourlyOntarioZonalPrice",
                           "PUB_DAHourlyOntarioZonalPrice_")
    assert got[0] == "PUB_DAHourlyOntarioZonalPrice_20260929.xml"


def test_latest_returns_none_when_index_unreachable(monkeypatch):
    monkeypatch.setattr(fetch_data, "fetch_index_head",
                        lambda url, max_bytes=65536: None)
    assert latest_ieso_file("DAHourlyOntarioZonalPrice", "PUB_X") == \
        (None, None, None)


def test_index_file_meta_missing():
    assert fetch_data._index_file_meta("<html></html>", "nope.csv") == \
        (None, None)


def test_fetch_one_skips_on_weak_validators(tmp_path, monkeypatch, capsys):
    """No ETag from the server: matching index mtime/size skips the download."""
    dest = tmp_path / "f.csv"
    dest.write_text("a\n")
    manifest = {"files": {"k": {"url": "http://x/f.csv",
                                "index_mtime": "29-Sep-2026 08:20",
                                "index_size": "158K"}}}
    monkeypatch.setattr(fetch_data, "head_validators", lambda url: (None, None))
    attempted = fetch_data.fetch_one(
        "http://x/f.csv", dest, "k", manifest,
        weak_validators={"index_mtime": "29-Sep-2026 08:20",
                         "index_size": "158K"})
    assert attempted is False
    assert "unchanged (weak validators)" in capsys.readouterr().out


def test_fetch_one_learns_weak_validators_on_etag_skip(tmp_path, monkeypatch,
                                                       capsys):
    """An ETag skip still records the current weak validators, so a later
    run can skip even when HEAD is temporarily failing."""
    dest = tmp_path / "f.csv"
    dest.write_text("a\n")
    manifest = {"files": {"k": {"url": "http://x/f.csv", "etag": '"abc"'}}}
    monkeypatch.setattr(fetch_data, "head_validators",
                        lambda url: ('"abc"', None))
    attempted = fetch_data.fetch_one(
        "http://x/f.csv", dest, "k", manifest,
        weak_validators={"index_mtime": "29-Sep-2026 08:20",
                         "index_size": "158K"})
    assert attempted is False
    assert manifest["files"]["k"]["index_mtime"] == "29-Sep-2026 08:20"


def test_fetch_one_downloads_when_weak_validators_differ(tmp_path, monkeypatch):
    dest = tmp_path / "f.csv"
    manifest = {"files": {"k": {"url": "http://x/f.csv",
                                "index_mtime": "28-Sep-2026 08:20",
                                "index_size": "158K"}}}
    monkeypatch.setattr(fetch_data, "head_validators", lambda url: (None, None))
    monkeypatch.setattr(fetch_data, "download", lambda url, d, attempts=4: True)
    monkeypatch.setattr(fetch_data, "count_rows", lambda p: 1)
    # count_rows is monkeypatched; make download actually write the file
    def _dl(url, d, attempts=4):
        Path(d).write_text("a\n")
        return True
    monkeypatch.setattr(fetch_data, "download", _dl)
    attempted = fetch_data.fetch_one(
        "http://x/f.csv", dest, "k", manifest,
        weak_validators={"index_mtime": "29-Sep-2026 08:20",
                         "index_size": "158K"})
    assert attempted is True
    assert manifest["files"]["k"]["index_mtime"] == "29-Sep-2026 08:20"


# ---------------------------------------------------------------------------
# count_rows against the real Phase 0 fixtures
# ---------------------------------------------------------------------------

def test_count_rows_da_zonal():
    assert count_rows(FIXTURES / "DA_zonal_sample.xml") == 24


def test_count_rows_lmp():
    assert count_rows(FIXTURES / "LMP_sample.csv") == 25296


def test_count_rows_lfda():
    assert count_rows(FIXTURES / "LFDA_sample.csv") == 6192


def test_count_rows_contract_list_sample_xlsx():
    # The fixture mimics the real file: data lives on "Contract Data",
    # the first sheet is a disclaimer and must not be counted.
    assert count_rows(FIXTURES / "contract_list_sample.xlsx") == 2


def test_count_rows_rrr_table1():
    assert count_rows(FIXTURES / "rrr_2114_table1_sample.xml") > 0


# ---------------------------------------------------------------------------
# Local HTTP server for download()/fetch_one() tests
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    body = b"hello der monitor"
    etag = '"abc123"'

    def _send(self, code, body=b"", extra=()):
        self.send_response(code)
        for key, value in extra:
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        if self.path == "/missing.bin":
            self._send(404)
        elif self.path == "/flaky.bin":
            # Hangs up cleanly partway: half the promised bytes.
            self.send_response(200)
            self.send_header("Content-Length", str(2 * len(self.body)))
            self.end_headers()
            self.wfile.write(self.body)
        else:
            self._send(200, self.body, [("ETag", self.etag)])

    def do_HEAD(self):
        self.do_GET()

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def server():
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def test_download_404_warns_and_returns_false(server, tmp_path, capsys):
    ok = download(f"{server}/missing.bin", tmp_path / "x.bin")
    assert ok is False
    assert not (tmp_path / "x.bin").exists()
    assert "404" in capsys.readouterr().out


def test_download_incomplete_is_detected(server, tmp_path, capsys):
    # The flaky endpoint promises twice the bytes it sends; the byte-count
    # check must catch it and every attempt must fail cleanly.
    ok = download(f"{server}/flaky.bin", tmp_path / "flaky.bin")
    assert ok is False
    assert "incomplete download" in capsys.readouterr().out


def test_fetch_one_skips_unchanged_etag(server, tmp_path):
    manifest = {"files": {}}
    dest = tmp_path / "f.bin"
    assert fetch_one(f"{server}/f.bin", dest, "k", manifest) is True
    first_sha = manifest["files"]["k"]["sha256"]
    assert dest.read_bytes() == _Handler.body
    # Second call: server ETag matches the manifest, so nothing downloads.
    assert fetch_one(f"{server}/f.bin", dest, "k", manifest) is False
    assert manifest["files"]["k"]["sha256"] == first_sha


# ---------------------------------------------------------------------------
# Failure paths and idempotency
# ---------------------------------------------------------------------------

def _raise_404(url, *args, **kwargs):
    raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)


def test_download_404_warns_and_keeps_old_file(monkeypatch, tmp_path, capsys):
    dest = tmp_path / "old.xml"
    dest.write_bytes(b"<old/>")
    monkeypatch.setattr(fetch_data.urllib.request, "urlopen", _raise_404)
    assert download("https://example.com/missing.xml", dest) is False
    assert dest.read_bytes() == b"<old/>"  # old data retained
    assert "404" in capsys.readouterr().out


def test_fetch_one_skips_when_etag_unchanged(monkeypatch, tmp_path):
    def boom(url, dest):
        raise AssertionError("should not download")
    monkeypatch.setattr(fetch_data, "head_validators", lambda url: ('"abc"', None))
    monkeypatch.setattr(fetch_data, "download", boom)
    dest = tmp_path / "f.xml"
    dest.write_bytes(b"<old/>")
    manifest = {"files": {"k": {"url": "https://example.com/f.xml",
                                "etag": '"abc"'}}}
    assert fetch_one("https://example.com/f.xml", dest, "k", manifest) is False
    assert dest.read_bytes() == b"<old/>"


def test_fetch_one_redownloads_when_etag_changes(monkeypatch, tmp_path):
    monkeypatch.setattr(fetch_data, "head_validators", lambda url: ('"new"', None))

    def fake_download(url, dest):
        Path(dest).write_bytes(b"<new/>")
        return True
    monkeypatch.setattr(fetch_data, "download", fake_download)
    dest = tmp_path / "f.xml"
    manifest = {"files": {"k": {"url": "https://example.com/f.xml",
                                "etag": '"old"'}}}
    assert fetch_one("https://example.com/f.xml", dest, "k", manifest) is True
    assert manifest["files"]["k"]["etag"] == '"new"'
    assert dest.read_bytes() == b"<new/>"


def test_main_returns_1_on_malformed_supplied_input(monkeypatch, tmp_path,
                                                    capsys):
    # Only the tariffs file needs to exist; validate_inputs fails on it first.
    (tmp_path / "retail_tariffs.csv").write_text("plan\nTOU\n")
    monkeypatch.setattr(fetch_data, "INPUTS_DIR", tmp_path)
    monkeypatch.setattr(fetch_data, "fetch_ieso_feed",
                        lambda feed_key, manifest: None)
    monkeypatch.setattr(fetch_data, "fetch_contract_list", lambda manifest: None)
    monkeypatch.setattr(fetch_data, "fetch_oeb_rrr", lambda manifest: None)
    monkeypatch.setattr(fetch_data, "fetch_oeb_map", lambda manifest: None)
    assert fetch_data.main() == 1
    assert "malformed" in capsys.readouterr().out.lower()


def test_main_returns_0_when_upstream_missing(monkeypatch, tmp_path, capsys):
    # Every upstream fetch fails: main must warn, keep old data, exit 0.
    # Never touch the real repo outputs: redirect the docs dir to tmp.
    monkeypatch.setattr(fetch_data.urllib.request, "urlopen", _raise_404)
    monkeypatch.setattr(fetch_data, "INPUTS_DIR",
                        Path(__file__).resolve().parent.parent / "data" / "inputs")
    monkeypatch.setattr(fetch_data, "DOCS_DIR", tmp_path)
    assert fetch_data.main() == 0
    out = capsys.readouterr().out
    assert "WARNING" in out


def test_manifest_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch_data, "MANIFEST_PATH", tmp_path / "manifest.json")
    manifest = fetch_data.load_manifest()
    assert manifest["files"] == {}
    manifest["files"]["k"] = {"url": "http://example/x"}
    fetch_data.save_manifest(manifest)
    reloaded = json.loads((tmp_path / "manifest.json").read_text())
    assert reloaded["files"]["k"]["url"] == "http://example/x"
    assert reloaded["generated_at"]


# ---------------------------------------------------------------------------
# OEB vintage discovery (fetch_text monkeypatched with canned pages)
# ---------------------------------------------------------------------------

def _oeb_detail(*links):
    return "<html>" + "".join(f'<a href="{l}">x</a>' for l in links) + "</html>"


def test_discover_oeb_picks_newest_ed_prefixed_vintage(monkeypatch):
    pages = {
        "2114": _oeb_detail(
            "https://www.oeb.ca/documents/opendata/rrr/2023/2.1.14 Table 1 Net Metering.xml",
            "https://www.oeb.ca/documents/opendata/rrr/2024-2/ED 2.1.14 Table 1 Net Metering.xml",
            "https://www.oeb.ca/documents/opendata/rrr/2024-2/ED 2.1.14 Table 3 Embedded Generation.xml",
            "https://www.oeb.ca/documents/opendata/rrr/2024-2/ED 2.1.14 Table 4 Embedded Generation by Type.xml",
        ),
        "2154": _oeb_detail(
            "https://www.oeb.ca/documents/opendata/rrr/2024-2/ED 2.1.5.4 Demand and Revenue.xml",
        ),
    }

    def fake_fetch(url):
        if "2114" in url:
            return pages["2114"]
        return pages["2154"]

    monkeypatch.setattr(fetch_data, "fetch_text", fake_fetch)
    vintage, urls = discover_oeb_files()
    assert vintage == "2024-2"
    assert urls["rrr_2114_t1"].endswith("2024-2/ED 2.1.14 Table 1 Net Metering.xml")
    assert urls["rrr_2154"].endswith("2024-2/ED 2.1.5.4 Demand and Revenue.xml")


def test_discover_oeb_empty_when_pages_unreachable(monkeypatch):
    monkeypatch.setattr(fetch_data, "fetch_text", lambda url: None)
    vintage, urls = discover_oeb_files()
    assert vintage is None and urls == {}


def test_discover_oeb_unescapes_html_entities_in_hrefs(monkeypatch):
    # The 2.1.2 page serves its href with &amp; escaped; using it raw 404s.
    page = _oeb_detail(
        "https://www.oeb.ca/documents/opendata/rrr/2024-2/"
        "ED 2.1.2 Customers &amp; Connections.xml",
    )
    monkeypatch.setattr(fetch_data, "fetch_text",
                        lambda url: page if "212" in url else None)
    vintage, urls = discover_oeb_files()
    assert vintage == "2024-2"
    assert urls["rrr_212"].endswith("ED 2.1.2 Customers & Connections.xml")
    assert "&amp;" not in urls["rrr_212"]


# ---------------------------------------------------------------------------
# fetch_price_history: --backfill-days forcing and --strict failure mode
# ---------------------------------------------------------------------------

import sys
from datetime import datetime, timezone

import fetch_price_history as fph


def test_file_date_within_recent_and_old():
    today = datetime.now(timezone.utc).date()
    recent = today.strftime("PUB_DAHourlyOntarioZonalPrice_%Y%m%d.xml")
    old = "PUB_DAHourlyOntarioZonalPrice_20200101.xml"
    assert fph.file_date_within(recent, 7) is True
    assert fph.file_date_within(old, 7) is False
    assert fph.file_date_within(recent, 0) is False  # disabled
    assert fph.file_date_within("PUB_HourlyLFDA.xml", 7) is False  # no date


def _run_main(monkeypatch, argv, fail_during_run):
    fph._FAILURES.clear()
    monkeypatch.setattr(sys, "argv", ["fetch_price_history.py"] + argv)
    seen = []

    def fake_run_feed(feed, manifest, force_days=0):
        seen.append(force_days)
        if fail_during_run:
            fph.fail("simulated download failure")

    monkeypatch.setattr(fph, "run_feed", fake_run_feed)
    monkeypatch.setattr(fph, "load_manifest", lambda: {})
    monkeypatch.setattr(fph, "save_manifest", lambda m: None)
    fph.main()
    return seen


def test_strict_exits_nonzero_on_fetch_failure(monkeypatch):
    with pytest.raises(SystemExit) as exc:
        _run_main(monkeypatch, ["--strict", "--backfill-days", "3"],
                  fail_during_run=True)
    assert exc.value.code == 1


def test_non_strict_exits_zero_despite_failure(monkeypatch):
    seen = _run_main(monkeypatch, ["--backfill-days", "3"],
                     fail_during_run=True)
    assert seen and all(v == 3 for v in seen)  # backfill threaded through
    assert fph._FAILURES  # failures still recorded, just not fatal
    fph._FAILURES.clear()


def test_strict_clean_run_exits_zero(monkeypatch):
    seen = _run_main(monkeypatch, ["--strict"], fail_during_run=False)
    assert seen == [0, 0, 0, 0]  # one call per feed, no failures, no exit
