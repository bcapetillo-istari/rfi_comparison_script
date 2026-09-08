#!/usr/bin/env python3
"""
to_wide_matrix.py — Pivot a set of vendor requirement CSV/TSV files into one
wide-format comparison matrix:

    * one COLUMN per requirement ID (sorted numerically, e.g. 1.2 before 1.11)
    * one ROW per input file (vendor), labeled by the filename stem
    * each CELL = that vendor's response to that requirement

Expected row format in each input (no header required, but one is tolerated):
    <requirement_id> <requirement_name> <response text>

The delimiter (tab vs. comma) is auto-detected per file. If a vendor answers
the same requirement ID more than once, the responses are joined with ' | '
and a warning is printed (this also catches numbering typos like a second
'1.1' that was meant to be '1.10').

Usage:
    python3 to_wide_matrix.py vendor_a.csv vendor_b.csv -o matrix.csv
    python3 to_wide_matrix.py responses/*.csv -o matrix.csv
    python3 to_wide_matrix.py vendor_a.csv vendor_b.csv --names          # add a
        second header row with requirement names under the IDs

Only the Python standard library is used.
"""

import argparse
import csv
import sys
from pathlib import Path

HEADER_HINTS = {"id", "req", "requirement", "requirement id", "req id", "req_id"}


def detect_delimiter(path: Path) -> str:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        for line in f:
            if line.strip():
                return "\t" if "\t" in line else ","
    return ","


def looks_like_header(row: list[str]) -> bool:
    if not row:
        return False
    first = row[0].strip().lower()
    if first in HEADER_HINTS:
        return True
    return not first.replace(".", "").isdigit() or not first


def id_sort_key(req_id: str):
    """Numeric-aware key so '1.11' sorts after '1.2' instead of between '1.1' and '1.2'."""
    parts = []
    for p in req_id.strip().split("."):
        try:
            parts.append((0, int(p)))
        except ValueError:
            parts.append((1, p))
    return tuple(parts)


def read_vendor_file(path: Path):
    """Return (responses, names) dicts keyed by requirement ID."""
    delim = detect_delimiter(path)
    responses: dict[str, str] = {}
    names: dict[str, str] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f, delimiter=delim)
        for i, row in enumerate(reader):
            if not row or not any(cell.strip() for cell in row):
                continue
            if i == 0 and looks_like_header(row):
                continue
            row = [c.strip() for c in row]
            if len(row) > 3:
                row = row[:2] + [delim.join(row[2:]).strip()]
            while len(row) < 3:
                row.append("")
            req_id, req_name, response = row
            if req_id in responses:
                print(f"warning: {path.name}: duplicate requirement ID '{req_id}' "
                      f"— responses joined with ' | ' (check for a numbering typo)",
                      file=sys.stderr)
                responses[req_id] = f"{responses[req_id]} | {response}"
            else:
                responses[req_id] = response
            names.setdefault(req_id, req_name)
    return responses, names


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("inputs", type=Path, nargs="+", help="Vendor CSV/TSV files to pivot")
    ap.add_argument("-o", "--output", type=Path, default=None,
                    help="Output file (default: print to stdout). Extension picks the "
                         "delimiter: .tsv writes tabs, anything else writes commas.")
    ap.add_argument("--names", action="store_true",
                    help="Add a second header row with requirement names under the IDs")
    ap.add_argument("--missing", default="",
                    help="Cell value when a vendor has no response for an ID (default: empty)")
    args = ap.parse_args()

    vendors: list[tuple[str, dict[str, str]]] = []
    req_names: dict[str, str] = {}
    for path in args.inputs:
        if not path.is_file():
            print(f"error: file not found: {path}", file=sys.stderr)
            return 1
        responses, names = read_vendor_file(path)
        print(f"read {len(responses):>3} requirements from {path}", file=sys.stderr)
        vendors.append((path.stem, responses))
        for k, v in names.items():
            req_names.setdefault(k, v)

    all_ids = sorted({rid for _, resp in vendors for rid in resp}, key=id_sort_key)

    out_rows: list[list[str]] = [["vendor", *all_ids]]
    if args.names:
        out_rows.append(["", *(req_names.get(rid, "") for rid in all_ids)])
    for vendor, responses in vendors:
        out_rows.append([vendor, *(responses.get(rid, args.missing) for rid in all_ids)])

    if args.output:
        out_delim = "\t" if args.output.suffix.lower() == ".tsv" else ","
        with args.output.open("w", encoding="utf-8", newline="") as f:
            csv.writer(f, delimiter=out_delim).writerows(out_rows)
        print(f"wrote {len(vendors)} vendor rows x {len(all_ids)} requirement columns "
              f"-> {args.output}", file=sys.stderr)
    else:
        csv.writer(sys.stdout, delimiter="\t").writerows(out_rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())