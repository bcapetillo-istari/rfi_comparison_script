#!/usr/bin/env python3
"""
rfi_compare.py — Build a cross-vendor RFI response comparison from an Istari system.

Built to run as a cl_module function: the job's input model IS the RFI
document, and the report is left in the working directory so it becomes an
output of the job (nothing is uploaded or committed at the system level).

Given an Istari System ID and the RFI document (--rfi-file, the job's local
input file, or --rfi-id, its model UUID), this script:

    1. lists the models tracked on the system's branch; the RFI is identified
       (by exact content match, falling back to filename) and excluded, and
       every other model is treated as one vendor's RFI response
    2. ensures each response model has extracted-table artifacts, submitting an
       extraction job (``@istari:extract_tables`` by default, see --function)
       where they are missing (or always, with --force) and waiting for the
       jobs to finish
    3. compiles each model's extracted-table artifacts into a single
       requirement-ID -> response mapping (three-column tables assumed:
       requirement ID, label, vendor response; header names may vary)
    4. correlates requirement IDs across vendors and writes a wide comparison
       matrix — one column per requirement ID, one row per vendor — as CSV in
       the working directory (default rfi_response_comparison.csv), which the
       job machinery uploads as a job output

Vendor responses are passed through untouched: no unit conversion, no rewriting.
Duplicate requirement IDs within one vendor are joined with ' | ' and warned
about (this catches numbering typos like a second '1.1' meant as '1.10').

Usage:
    rfi_compare SYSTEM_ID --rfi-file "$input_model"     # cl_module job
    rfi_compare SYSTEM_ID --rfi-id RFI_UUID             # manual run
    rfi_compare SYSTEM_ID --rfi-file f.pdf --force      # re-extract all
"""

import argparse
import csv
import io
import json
import re
import sys
from pathlib import Path

from dotenv import load_dotenv
from istari_digital_client import Configuration
from istari_digital_client.sdk import Istari

EXTRACT_FUNCTION = "@istari:extract_tables"

HEADER_HINTS = {"id", "req", "requirement", "requirement id", "req id", "req_id"}

# An ID-shaped cell: numeric-dotted ('1.10', '7.8') or a short alphanumeric
# code carrying digits ('KSA-1', 'KPP.3', 'A-2.1'). Prose ('Loiter Time',
# 'KPP 7 (2 of 2)') doesn't match, so those rows are still skipped.
REQ_ID_PATTERN = re.compile(r"^[A-Za-z]{0,8}[-. ]?\d+(?:[.\-]\d+)*$")


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


# ----------------------------------------------------------------------------
# Table normalization (pure functions, no API access)
# ----------------------------------------------------------------------------


def looks_like_header(row: list) -> bool:
    """True for rows that carry no requirement (headers, titles, blanks)."""
    if not row:
        return False
    first = str(row[0]).strip().lower()
    if not first or first in HEADER_HINTS:
        return True
    return not REQ_ID_PATTERN.match(first)


def req_id_key(req_id: str):
    """Numeric-aware key: '1.11' sorts after '1.2', 'KSA-10' after 'KSA-2',
    and alphanumeric IDs sort after purely numeric ones."""
    parts = []
    for token in re.findall(r"\d+|\D+", str(req_id).strip()):
        if token.isdigit():
            parts.append((0, int(token), ""))
        else:
            parts.append((1, 0, token.lower()))
    return tuple(parts)


def iter_tables(data):
    """Yield tables (lists of rows) from extracted-table artifact JSON.

    Tolerates the common shapes: a single table (list of rows), a list of
    tables, or a dict wrapping either under a 'tables'/'data' key.
    """
    if isinstance(data, dict):
        for key in ("tables", "data", "rows"):
            if key in data:
                yield from iter_tables(data[key])
                return
        return
    if not isinstance(data, list) or not data:
        return
    first = data[0]
    if isinstance(first, dict):
        for table in data:  # list of table objects, each with a 'rows' key
            yield from iter_tables(table)
    elif isinstance(first, list) and first and isinstance(first[0], list):
        for table in data:  # list of tables
            yield table
    elif isinstance(first, list):
        yield data  # single table (list of rows)


def rows_from_artifact(raw: bytes):
    """Parse an artifact's bytes into rows, accepting JSON or CSV content."""
    text = raw.decode("utf-8-sig", errors="replace")
    stripped = text.lstrip()
    if stripped.startswith("[") or stripped.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None
        if data is not None:
            for table in iter_tables(data):
                yield from table
            return
    yield from csv.reader(io.StringIO(text))


def compile_responses(rows, vendor: str):
    """Fold three-column rows into {req_id: response} and {req_id: label}.

    Rows are taken as (requirement ID, label, response); extra columns are
    joined into the response, short rows are padded. Header rows and blank
    rows are skipped. Duplicate IDs are joined with ' | ' and warned about.
    """
    responses: dict[str, str] = {}
    labels: dict[str, str] = {}
    for row in rows:
        row = [str(c).strip() for c in row]
        if not row or not any(row) or looks_like_header(row):
            continue
        if len(row) > 3:
            row = row[:2] + [", ".join(c for c in row[2:] if c)]
        while len(row) < 3:
            row.append("")
        rid, label, response = row
        if rid in responses:
            log(
                f"warning: {vendor}: duplicate requirement ID {rid!r} — "
                f"responses joined with ' | ' (check for a numbering typo)"
            )
            responses[rid] = f"{responses[rid]} | {response}"
        else:
            responses[rid] = response
        labels.setdefault(rid, label)
    return responses, labels


def build_matrix(vendors: list[tuple[str, dict]], labels: dict[str, str]):
    """Wide matrix: header of requirement IDs, label row, one row per vendor."""
    all_ids = sorted({rid for _, resp in vendors for rid in resp}, key=req_id_key)
    rows = [["vendor", *all_ids], ["", *(labels.get(rid, "") for rid in all_ids)]]
    for vendor, responses in vendors:
        rows.append([vendor, *(responses.get(rid, "") for rid in all_ids)])
    return rows


# ----------------------------------------------------------------------------
# Istari access
# ----------------------------------------------------------------------------


def type_name(resource_type) -> str:
    """Normalize a resource type (plain string or enum like ResourceTypeDto.MODEL)."""
    value = getattr(resource_type, "value", resource_type)
    return str(value).split(".")[-1].upper()


def get_branch(client: Istari, system_id: str, branch_name: str):
    branches = {b.tag: b for b in client.systems.branches.list(system_id)}
    if branch_name in branches:
        return branches[branch_name]
    if len(branches) == 1:
        only = next(iter(branches.values()))
        log(f"note: branch {branch_name!r} not found — using sole branch {only.tag!r}")
        return only
    raise SystemExit(
        f"error: branch {branch_name!r} not found; " f"available: {sorted(branches)}"
    )


def identify_rfi(models: list, rfi_file: Path):
    """Find which tracked model is the RFI by matching the local input file.

    Exact content match first (size prefilter, so normally only one download),
    then filename match as a fallback for re-uploaded/re-encoded documents.
    """
    data = rfi_file.read_bytes()
    for model in models:
        if model.size == len(data) and model.read_bytes() == data:
            return model
    for model in models:
        if (model.name or "") == rfi_file.name:
            log(f"note: RFI matched by filename only (content differs): {model.name}")
            return model
    return None


def list_response_models(
    client: Istari, branch, rfi_id: str | None, rfi_file: Path | None
):
    """Return the branch's MODEL resources, excluding the RFI document.

    The RFI is identified either by its UUID (--rfi-id) or by matching the
    job's local input file (--rfi-file) against the tracked models.
    """
    models = [
        tracked
        for tracked in client.systems.branches.list_files(branch)
        if type_name(tracked.resource_type) == "MODEL"
    ]
    if rfi_file is not None:
        rfi = identify_rfi(models, rfi_file)
    else:
        rfi = next((m for m in models if m.resource_id == rfi_id), None)
    if rfi is None:
        log(
            "warning: could not identify the RFI among the branch's models — "
            f"treating all {len(models)} models as responses"
        )
        return models
    log(f"RFI identified: {rfi.name} ({rfi.resource_id}) — excluded from comparison")
    return [m for m in models if m.resource_id != rfi.resource_id]


def find_table_artifacts(client: Istari, model, debug: bool = False) -> list:
    """Extracted-table artifacts related to the model's current file revision.

    Relationship sides are revision DTOs carrying the owning entity, not a
    resource_id: an extraction output has owning_entity_type 'artifact' and
    owning_entity_id pointing at the artifact resource.
    """
    artifacts, related, seen = [], [], set()
    for rel in client.resources.relationships.list(model.file_revision_id):
        for side in (rel.left_revision, rel.right_revision):
            if side is None:
                continue
            if getattr(side, "file_revision_id", None) == model.file_revision_id:
                continue
            owner_type = str(getattr(side, "owning_entity_type", "") or "").lower()
            resource_id = getattr(side, "resource_id", None) or getattr(
                side, "owning_entity_id", None
            )
            name = (getattr(side, "name", "") or "").lower()
            ext = (getattr(side, "extension", "") or "").lower().lstrip(".")
            related.append(f"{owner_type or '?'}:{name} [{rel.relationship_type_name}]")
            if not resource_id or resource_id in seen:
                continue
            if owner_type and owner_type != "artifact":
                continue
            if ext in ("json", "csv") and "table" in name:
                seen.add(resource_id)
                artifacts.append(client.resources.get(resource_id))
    if debug and not artifacts:
        log(
            f"debug: {vendor_name(model)}: no artifact matched the table filter; "
            f"related resources: {related or 'none'}"
        )
    return artifacts


def vendor_name(model) -> str:
    name = model.display_name or model.name or model.resource_id
    return Path(name).stem


def gather_rows(artifacts):
    """Parse a vendor's artifacts into rows, avoiding double counting.

    Extraction produces both a combined tables.json and one CSV per table, and
    a re-extracted model carries a second full set — so keep only the newest
    artifact per filename, and prefer the JSON (falling back to the CSVs only
    when no JSON yields rows).
    """
    by_name: dict = {}
    for a in artifacts:
        prev = by_name.get(a.name)
        if prev is None or (
            (a.created or 0) and (prev.created or 0) and a.created > prev.created
        ):
            by_name[a.name] = a
    arts = list(by_name.values())
    json_arts = [a for a in arts if (a.extension or "").lower().lstrip(".") == "json"]
    csv_arts = [a for a in arts if a not in json_arts]
    rows = []
    for a in json_arts:
        rows.extend(rows_from_artifact(a.read_bytes()))
    used = json_arts
    if not rows:
        for a in csv_arts:
            rows.extend(rows_from_artifact(a.read_bytes()))
        used = csv_arts
    return rows, used


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("system_id", help="Istari System UUID")
    rfi_group = ap.add_mutually_exclusive_group(required=True)
    rfi_group.add_argument(
        "--rfi-file",
        type=Path,
        help="Local path of the RFI document (the job's input model file); the "
        "matching tracked model is excluded from the comparison",
    )
    rfi_group.add_argument(
        "--rfi-id",
        help="Model UUID of the RFI document (excluded from the comparison)",
    )
    ap.add_argument(
        "--branch",
        default="main",
        help="System branch to read models from (default: main)",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="Re-run extraction even when table artifacts already exist",
    )
    ap.add_argument(
        "--function",
        default=EXTRACT_FUNCTION,
        help=f"Extraction function name (default: {EXTRACT_FUNCTION})",
    )
    ap.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("rfi_response_comparison.csv"),
        help="Local report path (default: rfi_response_comparison.csv)",
    )
    ap.add_argument(
        "--job-timeout",
        type=float,
        default=900.0,
        help="Seconds to wait for each extraction job (default: 900)",
    )
    args = ap.parse_args()

    load_dotenv()
    client = Istari(
        config=Configuration(
            digital_api_url="https://api.dev.istari.app",
            identity_service_secret_file=".istari_credentials.json",
            identity_service_enabled=True,
        )
    )

    if args.rfi_file is not None and not args.rfi_file.is_file():
        log(f"error: RFI file not found: {args.rfi_file}")
        return 1

    branch = get_branch(client, args.system_id, args.branch)
    models = list_response_models(client, branch, args.rfi_id, args.rfi_file)
    if not models:
        log("error: no response models found on the branch")
        return 1
    log(
        f"found {len(models)} response model(s): "
        + ", ".join(vendor_name(m) for m in models)
    )

    # Submit extraction jobs for models that need them, then wait on all of them.
    pending = []
    for model in models:
        if not args.force and find_table_artifacts(client, model):
            log(f"{vendor_name(model)}: reusing existing extracted-table artifacts")
            continue
        job = client.jobs.create(resource_id=model.resource_id, function=args.function)
        log(f"{vendor_name(model)}: submitted {args.function} job {job.id}")
        pending.append((model, job))
    for model, job in pending:
        status = job.poll(timeout=args.job_timeout)
        if status != "Completed":
            log(f"error: extraction job for {vendor_name(model)} ended '{status}'")
            return 1
        log(f"{vendor_name(model)}: extraction {status}")

    # Compile each vendor's artifacts into one requirement -> response picture.
    vendors: list[tuple[str, dict]] = []
    labels: dict[str, str] = {}
    for model in models:
        artifacts = find_table_artifacts(client, model, debug=True)
        if not artifacts:
            log(
                f"warning: {vendor_name(model)}: no extracted-table artifacts found — skipping"
            )
            continue
        rows, used = gather_rows(artifacts)
        responses, names = compile_responses(rows, vendor_name(model))
        log(
            f"{vendor_name(model)}: {len(responses)} requirements from "
            f"{len(used)} artifact(s): {', '.join(a.name or '?' for a in used)}"
        )
        vendors.append((vendor_name(model), responses))
        for rid, label in names.items():
            labels.setdefault(rid, label)

    if not vendors:
        log("error: no vendor produced any usable extracted tables")
        return 1

    matrix = build_matrix(vendors, labels)
    with args.output.open("w", encoding="utf-8", newline="") as f:
        csv.writer(f).writerows(matrix)
    log(
        f"wrote {len(vendors)} vendor rows x {len(matrix[0]) - 1} requirement columns "
        f"-> {args.output}"
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
