"""Tests for src/qa_log.py: namespace-replacing writes.

The invariant: log_rows() replaces only the (phase, dataset) namespaces
it is given and leaves every other writer's rows untouched, so reruns
never duplicate and two writers never clobber each other -- provided they
use distinct dataset names.
"""
import csv
from pathlib import Path

import qa_log


def _entry(phase, dataset, action, record_id):
    return {"phase": phase, "dataset": dataset, "action": action,
            "record_id": record_id, "count": "1", "note": "test"}


def _read():
    with open(qa_log.QA_LOG, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def test_namespaces_are_independent(monkeypatch, tmp_path):
    monkeypatch.setattr(qa_log, "QA_LOG", tmp_path / "qa_log.csv")
    qa_log.log_rows([_entry("phase1", "a", "repaired", "X")])
    qa_log.log_rows([_entry("phase1", "b", "repaired", "Y")])
    rows = _read()
    assert len(rows) == 2
    # rerunning writer A replaces only A's namespace
    qa_log.log_rows([_entry("phase1", "a", "repaired", "Z")])
    rows = _read()
    assert [(r["dataset"], r["record_id"]) for r in rows] == [
        ("a", "Z"), ("b", "Y")]


def test_rerun_does_not_duplicate(monkeypatch, tmp_path):
    monkeypatch.setattr(qa_log, "QA_LOG", tmp_path / "qa_log.csv")
    entries = [_entry("2b", "ldc_adoption", "excluded_no_value", "ED-1")]
    qa_log.log_rows(entries)
    qa_log.log_rows(entries)
    assert len(_read()) == 1


def test_rows_sorted_deterministically(monkeypatch, tmp_path):
    monkeypatch.setattr(qa_log, "QA_LOG", tmp_path / "qa_log.csv")
    qa_log.log_rows([_entry("p", "d", "b_act", "2"),
                     _entry("p", "d", "a_act", "1")])
    rows = _read()
    assert [(r["action"], r["record_id"]) for r in rows] == [
        ("a_act", "1"), ("b_act", "2")]
