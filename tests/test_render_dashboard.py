"""Tests for src/render_dashboard.py (Phase 4).

Covers the release gate, build determinism, link/asset hygiene,
content-state assertions, HTML well-formedness, and the docs/ size cap.
"""
import hashlib
import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import render_dashboard as rd
from config import PUBLIC_RELEASE_APPROVED

DOCS = REPO / "docs"
INDEX = DOCS / "index.html"
CHART_FILES = [f"charts/{name}" for name, _ in rd.PAGES]
VOID = {"meta", "br", "img", "hr", "link", "input", "source", "wbr",
        "area", "base", "col", "embed", "track", "param"}


def read_index():
    return INDEX.read_text(encoding="utf-8")


def tabpanel(html_text, page_id):
    m = re.search(rf'<section id="tab-{page_id}"(.*?)</section>',
                  html_text, re.S)
    assert m, f"tabpanel {page_id} missing"
    return m.group(0)


# ---------------------------------------------------------------------------
# Release gate
# ---------------------------------------------------------------------------

def test_banner_and_noindex_present_when_flag_false():
    assert PUBLIC_RELEASE_APPROVED is False
    # Order-independent: other tests rebuild the charts without banners.
    rd.apply_gate_to_charts()
    html_text = read_index()
    assert rd.BANNER_TEXT in html_text
    assert rd.BANNER_MARKER in html_text
    assert rd.NOINDEX_TAG in html_text
    for chart in CHART_FILES:
        text = (DOCS / chart).read_text(encoding="utf-8")
        assert rd.BANNER_TEXT in text, chart
        assert rd.NOINDEX_TAG in text, chart


def test_gate_inject_is_idempotent():
    html_text = read_index()
    assert rd.apply_release_gate(html_text) == html_text


def test_gate_strips_when_approved(monkeypatch):
    monkeypatch.setattr(rd, "PUBLIC_RELEASE_APPROVED", True)
    html_text = read_index()
    clean = rd.apply_release_gate(html_text)
    assert rd.BANNER_TEXT not in clean
    assert rd.NOINDEX_TAG not in clean
    assert rd.BANNER_MARKER not in clean
    # re-adding the gate restores the banner exactly once
    monkeypatch.setattr(rd, "PUBLIC_RELEASE_APPROVED", False)
    assert rd.apply_release_gate(clean).count(rd.BANNER_MARKER) == 1


# ---------------------------------------------------------------------------
# Build determinism (build timestamp isolated to one element)
# ---------------------------------------------------------------------------

def docs_hashes():
    out = {}
    for p in sorted(DOCS.rglob("*")):
        if not p.is_file():
            continue
        data = p.read_bytes()
        if p.name == "index.html":
            # The build timestamp is isolated to one element; normalize it.
            data = re.sub(rb'<span id="build-timestamp"[^>]*>.*?</span>',
                          b'<span id="build-timestamp"></span>',
                          data, flags=re.S)
        out[str(p.relative_to(DOCS))] = hashlib.sha256(data).hexdigest()
    return out


def test_timestamp_isolated_to_one_element():
    html_text = read_index()
    assert html_text.count('id="build-timestamp"') == 1


def test_two_runs_give_identical_docs(monkeypatch):
    # pin the build clock so the check does not depend on same-minute luck
    monkeypatch.setenv("DER_MONITOR_BUILD_TIME", "2026-10-01T12:00:00+00:00")
    rd.main()
    first = docs_hashes()
    rd.main()
    second = docs_hashes()
    assert first == second
    assert first, "no docs files found"


# ---------------------------------------------------------------------------
# Links, assets, external requests
# ---------------------------------------------------------------------------

def test_relative_links_and_iframe_targets_exist():
    html_text = read_index()
    targets = set(re.findall(r'(?:src|href)="([^"#]+?)"', html_text))
    assert targets, "no links found"
    for t in targets:
        if t.startswith(("http://", "https://", "mailto:")):
            continue
        assert (DOCS / t).exists(), f"missing link target: {t}"
    iframes = re.findall(r'<iframe[^>]*src="([^"]+)"', html_text)
    assert len(iframes) == 4
    for src in iframes:
        assert (DOCS / src).exists(), f"missing iframe target: {src}"


def test_no_external_requests_other_than_plotly_cdn():
    # plotly.js CDN, maplibre-gl JS/CSS CDN (map pages only), and CARTO
    # basemap tiles loaded at runtime by the carto-darkmatter style.
    allowed = ("https://cdn.plot.ly",
               "https://cdn.jsdelivr.net/npm/maplibre-gl@",
               "https://basemaps.cartocdn.com")
    for page in ["index.html", *CHART_FILES]:
        text = (DOCS / page).read_text(encoding="utf-8")
        for url in set(re.findall(r'https?://[^\s"\')<>]+', text)):
            assert url.startswith(allowed), f"{page}: external URL {url}"


def test_no_tokens_or_analytics():
    banned = ("google-analytics", "googletagmanager", "segment.io",
              "api_key=", "apikey=", "access_token=", "client_secret")
    for page in ["index.html", *CHART_FILES]:
        text = (DOCS / page).read_text(encoding="utf-8").lower()
        for b in banned:
            assert b not in text, f"{page}: found {b}"


# ---------------------------------------------------------------------------
# Content state
# ---------------------------------------------------------------------------

def test_page1_preview_label_and_no_headline():
    html_text = read_index()
    p1 = tabpanel(html_text, "page1")
    assert "Summer preview, not annual" in p1
    # No headline percentage anywhere in the Page 1 tab.
    assert not re.search(r"\d+(?:\.\d+)?\s*%", p1), "headline % found on page 1"


def test_page4_rule_approval_counts():
    html_text = read_index()
    p4 = tabpanel(html_text, "page4")
    assert re.search(r"Approved rows</dt>\s*<dd>33</dd>", p4)
    assert re.search(r"Rejected rows</dt>\s*<dd>25</dd>", p4)
    assert "Awaiting owner review" not in p4


def test_commentary_placeholders_present():
    html_text = read_index()
    for n in range(1, 5):
        block = tabpanel(html_text, f"page{n}")
        assert rd.ANALYST in block, f"page{n} commentary placeholder missing"


def test_status_panel_matches_manifest_and_summary():
    html_text = read_index()
    manifest = json.loads(
        (REPO / "data/processed/manifest.json").read_text(encoding="utf-8"))
    summary = json.loads(
        (REPO / "data/processed/value_summary.json").read_text(encoding="utf-8"))
    for feed, info in manifest["history"].items():
        assert rd.short_dt(info["updated_at"]) in html_text, feed
    assert summary["window_start"][:10] in html_text
    assert summary["window_end"][:10] in html_text
    assert "54.2%" in html_text  # node-to-zone coverage vs the 90% bar
    assert "2024-2" in html_text  # RRR vintage
    assert "1,022 of 1,054" in html_text  # node coverage


# ---------------------------------------------------------------------------
# HTML parses cleanly; docs/ size cap
# ---------------------------------------------------------------------------

class TagChecker(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.errors = []

    def handle_starttag(self, tag, attrs):
        if tag not in VOID:
            self.stack.append((tag, self.getpos()))

    def handle_startendtag(self, tag, attrs):
        pass

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        if not self.stack:
            self.errors.append(f"closing </{tag}> with empty stack "
                               f"at {self.getpos()}")
            return
        open_tag, pos = self.stack.pop()
        if open_tag != tag:
            self.errors.append(f"<{open_tag}> opened at {pos} closed by "
                               f"</{tag}> at {self.getpos()}")


def test_index_html_parses_cleanly():
    checker = TagChecker()
    checker.feed(read_index())
    checker.close()
    assert not checker.errors, checker.errors[:5]
    assert not checker.stack, f"unclosed tags: {checker.stack[:5]}"


def test_docs_size_under_15mb():
    total = sum(p.stat().st_size for p in DOCS.rglob("*") if p.is_file())
    print(f"\ndocs/ total: {total / 1e6:.2f} MB")
    assert total < 15e6, f"docs/ is {total / 1e6:.1f} MB, over the 15 MB cap"
