#!/usr/bin/env python3
"""
concat_extracted_tables.py — Read vendor requirement CSV files and concatenate
them into one flat JSON table: a single list of rows (one header row first,
then every requirement row from all input files, sorted by requirement ID).

Expected row format in each input (a header row is tolerated and kept first):
    <requirement_id>,<requirement_name>,<response text>

Rows within each table are sorted numerically by requirement ID (1.2 before
1.11). A warning is printed if a requirement ID appears more than once in a
file — this also catches numbering typos like a second '1.1' that was meant
to be '1.10'.

Usage:
    python3 concat_extracted_tables.py vendor_a.csv vendor_b.csv -o tables.json
    python3 concat_extracted_tables.py test_data/*.csv -o tables.json

Only the Python standard library is used.
"""

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

DELIM = ","

HEADER_HINTS = {"id", "req", "requirement", "requirement id", "req id", "req_id"}

def extract_rows(path: Path):
    """Return a list of rows from a csv file"""
    rows = []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f, delimiter=DELIM)
        for row in reader:
            rows.append(row)

    return rows


def req_id_key(row):
    """Numeric sort key for a requirement ID like '1.11' (so 1.2 < 1.11)."""
    try:
        return [int(part) for part in row[0].split(".")]
    except (ValueError, IndexError):
        return []

def looks_like_header(row: list[str]) -> bool:
    if not row:
        return False
    first = row[0].strip().lower()
    if first in HEADER_HINTS:
        return True
    return not first.replace(".", "").isdigit() or not first


def sort_table(rows, path: Path):
    """Sort data rows by requirement ID, keeping a non-numeric header row first."""
    header, data = [], rows
    if rows and not req_id_key(rows[0]):
        header, data = rows[:1], rows[1:]

    dupes = [rid for rid, n in Counter(r[0] for r in data if r).items() if n > 1]
    for rid in sorted(dupes):
        print(f"warning: {path}: requirement ID {rid!r} appears more than once "
              f"(numbering typo? e.g. '1.1' meant as '1.10')", file=sys.stderr)

    return header + sorted(data, key=req_id_key)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("inputs", type=Path, nargs="+", help="Vendor CSV files to concatenate")
    ap.add_argument("-o", "--output", type=Path, default=None,
                    help="Output file: .json writes JSON, anything else writes CSV "
                         "(default: print JSON to stdout).")
    args = ap.parse_args()

    all_rows = []

    for i, path in enumerate(args.inputs):
        if not path.is_file():
            print(f"error: file not found: {path}", file=sys.stderr)
            return 1
        rows = extract_rows(path)
        rows = sort_table(rows, path)

        # Drop headers from subsequent files
        if i > 0 and rows and looks_like_header(rows[0]):
            rows = rows[1:]

        all_rows.extend(rows)

    # Re-sort across all files so requirement order doesn't depend on the
    # order the inputs were passed in; keep the header row first.
    if all_rows and looks_like_header(all_rows[0]):
        all_rows = all_rows[:1] + sorted(all_rows[1:], key=req_id_key)
    else:
        all_rows.sort(key=req_id_key)

    if args.output:
        # .json writes JSON; anything else writes CSV (so the result can feed
        # straight into to_wide_matrix.py).
        if args.output.suffix.lower() == ".json":
            with args.output.open("w", encoding="utf-8") as f:
                json.dump(all_rows, f, indent=2, ensure_ascii=False)
                f.write("\n")
        else:
            with args.output.open("w", encoding="utf-8", newline="") as f:
                csv.writer(f).writerows(all_rows)
        print(f"wrote {len(all_rows)} rows -> {args.output}", file=sys.stderr)
    else:
        print(json.dumps(all_rows, indent=2, ensure_ascii=False))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())