#!/usr/bin/env python3
"""Extract all HTML tables from a single SEC filing URL using parser_new."""

import argparse
import csv
import json
from pathlib import Path

from parser import FILING_URL, HEADERS, parse_all_tables


def _safe_slug(text):
    text = (text or "").strip().lower()
    keep = []
    for ch in text:
        if ch.isalnum():
            keep.append(ch)
        elif ch in (" ", "-", "_"):
            keep.append("_")
    slug = "".join(keep)
    while "__" in slug:
        slug = slug.replace("__", "_")
    return slug.strip("_") or "table"


def _write_strict_csvs(tables, out_dir):
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    written = []

    for t in tables:
        columns = list(t.get("columns") or [])
        data_rows = [list(r) for r in (t.get("data_rows") or [])]
        if not columns:
            rows = t.get("rows") or []
            if not rows:
                continue
            width = max(len(r) for r in rows)
            columns = [f"Column {i + 1}" for i in range(width)]
            data_rows = [list(r) + [""] * (width - len(r)) for r in rows]

        if not data_rows:
            continue

        width = len(columns)
        padded_data = [r + [""] * (width - len(r)) for r in data_rows]
        heading = _safe_slug(t.get("heading", ""))
        file_name = f"table_{t['index']:03d}_{heading}.csv"
        csv_path = out_path / file_name
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(columns)
            writer.writerows(padded_data)
        written.append(str(csv_path))
    return written


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--url",
        default=FILING_URL,
        help="SEC filing .htm URL (default: parser_new.FILING_URL)",
    )
    ap.add_argument(
        "--output-json",
        default="",
        help="Optional path to write full extracted table payload as JSON.",
    )
    ap.add_argument(
        "--strict-csv-dir",
        default="",
        help="Optional directory to write one fixed-width CSV per table.",
    )
    args = ap.parse_args()

    tables = parse_all_tables(args.url, headers=HEADERS)
    print(f"Extracted {len(tables)} tables from {args.url}\n")
    print("=" * 80)
    for t in tables:
        print(f"\n--- Table {t['index']} ---")
        if t["heading"]:
            print(f"Heading: {t['heading']}")
        text = t["text"]
        print(text[:2000])
        if len(text) > 2000:
            print(f"  ... (truncated, full length {len(text)} chars)")
        print()

    if args.output_json:
        with open(args.output_json, "w", encoding="utf-8") as f:
            json.dump(tables, f, indent=2, ensure_ascii=False)
        print(f"Wrote full table payload to {args.output_json}")

    if args.strict_csv_dir:
        written = _write_strict_csvs(tables, args.strict_csv_dir)
        print(f"Wrote {len(written)} strict CSV files to {args.strict_csv_dir}")


if __name__ == "__main__":
    main()
