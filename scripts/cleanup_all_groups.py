#!/usr/bin/env python3
"""Cleanup duplicate invoice/summary JSON records across all groups.

This script is intended to be run from the repo root and will scan the
`data/` tree for group folders containing `*_invoices.json` and
`*_summary.json` files.

It uses the same merge/dedupe logic as the current fetch workflow:
- invoices are merged/deduped by MARK (and stray duplicates removed)
- receipts are merged/deduped by identity (AA+date+issuer) and not duplicated

This is useful to clean up existing JSON files after a fix was deployed.

Usage:
    python scripts/cleanup_all_groups.py

"""

import os
import re
import shutil
import datetime
import sys

# Ensure repo root is on sys.path so we can import app.py cleanly.
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Import the core helper functions from the app so we share identical logic.
from app import (
    BASE_DIR,
    _set_thread_group_base_dir,
    append_doc_to_customer_file,
    append_summary_to_customer_file,
    json_read,
    json_write,
)

DATA_DIR = os.path.join(BASE_DIR, "data")

INVOICE_RE = re.compile(r"^(.+?)_invoices\.json$")
SUMMARY_RE = re.compile(r"^(.+?)_summary\.json$")
BACKUP_SUFFIX = ".cleanup_bak"


def _backup_file(path: str):
    if not os.path.exists(path):
        return
    backup_path = path + BACKUP_SUFFIX
    shutil.copy2(path, backup_path)


def _process_group(group_dir: str) -> dict:
    """Clean all invoice/summary files in a group folder."""
    stats = {
        "group": os.path.basename(group_dir),
        "invoices_files": 0,
        "summary_files": 0,
        "invoices_processed": 0,
        "summary_processed": 0,
        "errors": [],
    }

    # ensure group functions that rely on group_path() see this folder
    _set_thread_group_base_dir(group_dir)

    for fname in sorted(os.listdir(group_dir)):
        m_inv = INVOICE_RE.match(fname)
        m_sum = SUMMARY_RE.match(fname)
        if not (m_inv or m_sum):
            continue

        path = os.path.join(group_dir, fname)
        vat = (m_inv or m_sum).group(1)

        try:
            data = json_read(path, default=[])
            if not isinstance(data, list):
                continue
        except Exception as e:
            stats["errors"].append(f"{path}: read failed: {e}")
            continue

        # backup before rewriting
        try:
            _backup_file(path)
        except Exception as e:
            stats["errors"].append(f"{path}: backup failed: {e}")

        # reset file to empty list and re-add using app's merge logic
        try:
            json_write(path, [])
        except Exception as e:
            stats["errors"].append(f"{path}: wipe failed: {e}")
            continue

        if m_inv:
            stats["invoices_files"] += 1
            for doc in data:
                try:
                    if append_doc_to_customer_file(doc, vat):
                        stats["invoices_processed"] += 1
                except Exception as e:
                    stats["errors"].append(f"{path}: append_doc error: {e}")
        else:
            stats["summary_files"] += 1
            for doc in data:
                try:
                    if append_summary_to_customer_file(doc, vat):
                        stats["summary_processed"] += 1
                except Exception as e:
                    stats["errors"].append(f"{path}: append_summary error: {e}")

    # clear override after processing
    _set_thread_group_base_dir(None)
    return stats


def main():
    print("Starting cleanup of all group JSON files (invoices/summary)...")

    if not os.path.isdir(DATA_DIR):
        print(f"Data directory does not exist: {DATA_DIR}")
        return 1

    group_dirs = [
        os.path.join(DATA_DIR, d)
        for d in os.listdir(DATA_DIR)
        if os.path.isdir(os.path.join(DATA_DIR, d)) and d not in ("system", "sessions")
    ]

    if not group_dirs:
        print("No group directories found under data/.")
        return 0

    overall = {
        "groups": 0,
        "invoices_files": 0,
        "summary_files": 0,
        "invoices_processed": 0,
        "summary_processed": 0,
        "errors": [],
    }

    for group_dir in sorted(group_dirs):
        stats = _process_group(group_dir)
        overall["groups"] += 1
        overall["invoices_files"] += stats["invoices_files"]
        overall["summary_files"] += stats["summary_files"]
        overall["invoices_processed"] += stats["invoices_processed"]
        overall["summary_processed"] += stats["summary_processed"]
        overall["errors"].extend(stats["errors"])

        print(f"Processed group {stats['group']}: invoices files={stats['invoices_files']} (docs={stats['invoices_processed']}), summaries files={stats['summary_files']} (docs={stats['summary_processed']})")

    print("\nCleanup complete.")
    print(f"Groups scanned: {overall['groups']}")
    print(f"Invoices files: {overall['invoices_files']} (docs processed: {overall['invoices_processed']})")
    print(f"Summary files: {overall['summary_files']} (docs processed: {overall['summary_processed']})")
    if overall["errors"]:
        print("Errors:")
        for e in overall["errors"]:
            print(" -", e)

    return 0


if __name__ == "__main__":
    exit(main())
