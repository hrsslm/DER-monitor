"""Shared QA-log writer.

qa_log.csv has one row per affected record with a stable key
(phase, dataset, action, record_id). Each pipeline script owns the rows in
its own (phase, dataset) namespaces: log_rows() replaces only those rows and
leaves every other script's rows untouched, so reruns and multi-script
pipelines never duplicate or clobber entries.
"""
import csv
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
QA_LOG = ROOT / "data" / "processed" / "qa_log.csv"

HEADER = ["phase", "dataset", "action", "record_id", "count", "note"]


def read_existing():
    if not QA_LOG.exists():
        return []
    with open(QA_LOG, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        return [row for row in reader if any(row.values())]


def log_rows(entries):
    """Replace rows in the given (phase, dataset) namespaces with `entries`.

    Each entry is a dict with keys: phase, dataset, action, record_id,
    count, note. Rows are written in a deterministic sorted order.
    """
    namespaces = {(e["phase"], e["dataset"]) for e in entries}
    kept = [r for r in read_existing()
            if (r["phase"], r["dataset"]) not in namespaces]
    kept.extend(entries)
    kept.sort(key=lambda r: (r["phase"], r["dataset"], r["action"], r["record_id"]))
    QA_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(QA_LOG, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=HEADER)
        writer.writeheader()
        for row in kept:
            writer.writerow({k: row.get(k, "") for k in HEADER})
    return len(kept)
