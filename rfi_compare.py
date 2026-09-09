#!/usr/bin/env python3
"""
rfi_compare.py — Build a cross-vendor RFI response comparison from an Istari system.

Given an Istari System ID and the RFI document's model UUID, this script:

    1. lists the models tracked on the system's branch; every model other than
       the RFI itself is treated as one vendor's RFI response
    2. ensures each response model has extracted-table artifacts, submitting an
       extraction job (``@istari:extract_tables`` by default, see --function)
       where they are missing (or always, with --force) and waiting for the
       jobs to finish
    3. compiles each model's extracted-table artifacts into a single
       requirement-ID -> response mapping (three-column tables assumed:
       requirement ID, label, vendor response; header names may vary)
    4. correlates requirement IDs across vendors and writes a wide comparison
       matrix — one column per requirement ID, one row per vendor — as CSV,
       uploads it as a single ARTIFACT resource (re-runs add a new revision
       instead of duplicating), and commits that revision onto the system's
       branch so the report is linked at the system level

Vendor responses are passed through untouched: no unit conversion, no rewriting.
Duplicate requirement IDs within one vendor are joined with ' | ' and warned
about (this catches numbering typos like a second '1.1' meant as '1.10').

Authentication comes from the environment:
    ISTARI_REGISTRY_URL, ISTARI_REGISTRY_AUTH_TOKEN

Usage:
    python3 rfi_compare.py SYSTEM_ID RFI_UUID
    python3 rfi_compare.py SYSTEM_ID RFI_UUID -o report.csv --branch main
    python3 rfi_compare.py SYSTEM_ID RFI_UUID --force          # re-extract all
    python3 rfi_compare.py SYSTEM_ID RFI_UUID --no-upload      # local CSV only
"""

import argparse
import csv
import io
import json
import sys
from pathlib import Path

from dotenv import load_dotenv
from istari_digital_client import Configuration
from istari_digital_client.sdk import Istari

EXTRACT_FUNCTION = "@istari:extract_tables"

HEADER_HINTS = {"id", "req", "requirement", "requirement id", "req id", "req_id"}


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


# ----------------------------------------------------------------------------
# Table normalization (pure functions, no API access)
# ----------------------------------------------------------------------------


def looks_like_header(row: list) -> bool:
    if not row:
        return False
    first = str(row[0]).strip().lower()
    if first in HEADER_HINTS:
        return True
    return not first.replace(".", "").isdigit() or not first


def req_id_key(req_id: str):
    """Numeric-aware key so '1.11' sorts after '1.2'."""
    parts = []
    for p in str(req_id).strip().split("."):
        try:
            parts.append((0, int(p)))
        except ValueError:
            parts.append((1, p))
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


def list_response_models(client: Istari, branch, rfi_id: str):
    """Return the branch's MODEL resources, excluding the RFI document itself."""
    models, skipped = [], []
    for tracked in client.systems.branches.list_files(branch):
        if type_name(tracked.resource_type) != "MODEL":
            continue
        if tracked.resource_id == rfi_id:
            skipped.append(tracked)
            continue
        models.append(tracked)
    if not skipped:
        log(
            f"note: RFI {rfi_id} was not among the branch's models — "
            f"treating all {len(models)} models as responses"
        )
    return models


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
            resource_id = (getattr(side, "resource_id", None)
                           or getattr(side, "owning_entity_id", None))
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


def upload_report(client: Istari, path: Path, branch, system_id: str):
    """Upload the report once and attach it at the system level.

    The report is registered as a single ARTIFACT resource. If a resource with
    the same filename already exists, a new revision is added instead of
    creating a duplicate. The new revision is then committed onto the system's
    branch as a tracked file (replacing the previously tracked report revision,
    if any), so the report is linked to the system itself — not to each input.
    """
    existing = next(iter(client.resources.list(name=[path.name], size=2).items), None)
    description = f"RFI response comparison matrix for system {system_id}"
    if existing:
        add_obj = client.resources.revisions.create(
            existing.resource_id, path, description=description
        )
        report_id, report_revision_id = existing.resource_id, add_obj.id
        log(f"report exists — added revision {report_revision_id} "
            f"to resource {report_id}")
    else:
        add_obj = client.resources.create(
            path, "ARTIFACT", description=description, display_name=path.name
        )
        report_id, report_revision_id = add_obj.resource_id, add_obj.file_revision_id
        log(f"created report resource {report_id} (revision {report_revision_id})")

    # Swap the previously tracked report revision (if any) for the new one.
    # commit(remove=...) wants objects with a revision `.id`, which
    # TrackedResource lacks — resolve each to its ResourceRevision.
    stale = [
        client.resources.revisions.get(
            tracked.resource_id, tracked.current_file_revision_id
        )
        for tracked in client.systems.branches.list_files(branch, name=path.name)
        if tracked.resource_id == report_id
    ]
    client.systems.branches.commit(branch, add=[add_obj], remove=stale or None)
    log(f"committed report revision {report_revision_id} onto branch "
        f"{branch.tag!r}" + (f" (replaced {len(stale)} stale revision(s))" if stale else ""))
    return report_id


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
        if prev is None or ((a.created or 0) and (prev.created or 0) and a.created > prev.created):
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
    ap.add_argument(
        "rfi_id", help="Model UUID of the RFI document (excluded from comparison)"
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
        "--no-upload",
        action="store_true",
        help="Skip uploading the report back to the Istari system",
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

    branch = get_branch(client, args.system_id, args.branch)
    models = list_response_models(client, branch, args.rfi_id)
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

    if not args.no_upload:
        upload_report(client, args.output, branch, args.system_id)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
