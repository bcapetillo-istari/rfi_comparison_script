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
       requirement-ID -> response mapping: per table, the requirement-ID
       column is detected by value scanning (header-name hints as tiebreak),
       the response/label columns by header hints or position, and any other
       columns are dropped; IDs are normalized (uppercase, whitespace and
       edge punctuation trimmed, unicode dashes unified) for cross-vendor
       correlation but never rewritten ('1.04' and 'KPP 1.1' stay verbatim)
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
import os
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from istari_digital_client import Configuration
from istari_digital_client.sdk import Istari

# Header cells that name a requirement-ID column (matched by equality) and
# ones that name a response column (matched by substring, so 'Vendor Response'
# and 'Compliance Statement' both hit).
HEADER_ID_HINTS = {
    "id",
    "req",
    "requirement",
    "requirement id",
    "req id",
    "req_id",
    "req #",
    "req no",
    "req. no",
    "req. no.",
    "number",
    "#",
}
HEADER_RESPONSE_HINTS = ("response", "answer", "compliance", "statement", "remarks")

# An ID-shaped cell: numeric-dotted ('1.10', '7.8') or a short alphanumeric
# code carrying digits ('KSA-1', 'KPP.3', 'A-2.1'). Prose ('Loiter Time',
# 'KPP 7 (2 of 2)') doesn't match, so those rows are still skipped.
REQ_ID_PATTERN = re.compile(r"^[A-Za-z]{0,8}[-. ]?\d+(?:[.\-]\d+)*$")

NOT_FOUND_MSG = "Not Found - Manual review required"

# Keep in sync with pyproject.toml
VERSION = "1.0.0"


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", file=sys.stderr)


# Typographic dash variants (hyphen, en/em dash, minus) unified to '-'.
_DASH_TRANSLATION = str.maketrans(dict.fromkeys("‐‑‒–—―−", "-"))
# Stripped from the ends of an ID cell. Deliberately excludes dashes: a
# trailing dash marks a truncated heading ('KPP 6 -'), and stripping it
# would mint a false requirement ID.
_EDGE_PUNCTUATION = ".,:;!?()[]{}'\""


def clean_requirement_id(cell) -> str:
    """Normalize an ID for cross-vendor comparison — typography only.

    Uppercase, trim whitespace and edge punctuation, unify unicode dashes.
    Never rewrites the designation itself: '1.04' and 'KPP 1.1' stay verbatim
    (vendors are accountable for their own numbering; mismatches surface as
    separate matrix columns for manual review).
    """
    text = str(cell).translate(_DASH_TRANSLATION).strip()
    return text.strip(_EDGE_PUNCTUATION).strip().upper()


def is_requirement_id(cell) -> bool:
    """True when the cell, once cleaned, is ID-shaped ('1.10', 'KSA-1')."""
    cleaned = clean_requirement_id(cell)
    return bool(cleaned and REQ_ID_PATTERN.match(cleaned))


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


def tables_from_artifact(raw: bytes):
    """Parse an artifact's bytes into tables, accepting JSON or CSV content."""
    text = raw.decode("utf-8-sig", errors="replace")
    stripped = text.lstrip()
    if stripped.startswith("[") or stripped.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None
        if data is not None:
            yield from iter_tables(data)
            return
    rows = list(csv.reader(io.StringIO(text)))
    if rows:
        yield rows


def detect_columns(table):
    """Decide which columns hold the requirement ID, label, and response(s).

    The ID column is found by value scanning: column 0 by default, overridden
    by a header-hinted column whose cells are more often ID-shaped (so a
    'Requirement, Req ID, Response' layout still correlates). Response
    columns come from header hints; without any, the column after the label
    (extras joined), or the sole other column in a two-column table. Columns
    identified as none of the three are dropped.

    Returns (id_col, label_col, resp_cols, header_idx); label_col and
    header_idx may be None.
    """
    width = max(len(row) for row in table)

    def norm(cell):
        return str(cell or "").strip().lower()

    header_idx = None
    for i, row in enumerate(table[:3]):
        if any(is_requirement_id(c) for c in row):
            continue  # data rows carry IDs; header rows never do
        cells = [norm(c) for c in row]
        if any(c in HEADER_ID_HINTS for c in cells) or any(
            hint in c for c in cells for hint in HEADER_RESPONSE_HINTS
        ):
            header_idx = i
            break
    header = table[header_idx] if header_idx is not None else []
    data = table[header_idx + 1 :] if header_idx is not None else table

    def id_share(col):
        if not data:
            return 0.0
        hits = sum(1 for r in data if col < len(r) and is_requirement_id(r[col]))
        return hits / len(data)

    id_col = 0
    hinted = [i for i, cell in enumerate(header) if norm(cell) in HEADER_ID_HINTS]
    best = max(hinted, key=id_share, default=None)
    if best is not None and id_share(best) > id_share(0):
        id_col = best

    resp_cols = [
        i
        for i, cell in enumerate(header)
        if i != id_col and any(hint in norm(cell) for hint in HEADER_RESPONSE_HINTS)
    ]
    remaining = [i for i in range(width) if i != id_col and i not in resp_cols]
    label_col = None
    if resp_cols:
        label_col = remaining[0] if remaining else None
    elif len(remaining) == 1:
        resp_cols = remaining
    elif remaining:
        label_col, resp_cols = remaining[0], remaining[1:]
    return id_col, label_col, resp_cols, header_idx


def compile_tables(tables, vendor: str):
    """Fold extracted tables into {req_id: response} and {req_id: label}.

    Column roles are detected per table (see detect_columns); requirement IDs
    are normalized with clean_requirement_id so typographic variants
    correlate across vendors. Rows whose ID cell isn't ID-shaped (headers,
    titles, prose) are skipped. Duplicate IDs are joined with ' | ' and
    warned about in the log (this catches numbering typos like a second '1.1' meant as
    '1.10').
    """
    responses: dict[str, str] = {}
    labels: dict[str, str] = {}
    for index, table in enumerate(tables, 1):
        if not table:
            continue
        id_col, label_col, resp_cols, header_idx = detect_columns(table)
        log(
            f"{vendor}: table {index}: id=col {id_col}, "
            f"label={f'col {label_col}' if label_col is not None else 'none'}, "
            f"response=col(s) {resp_cols or 'none'}"
            + (
                f", header row {header_idx}"
                if header_idx is not None
                else ", no header"
            )
        )
        start = header_idx + 1 if header_idx is not None else 0
        for row in table[start:]:
            cells = ["" if c is None else str(c).strip() for c in row]
            rid = clean_requirement_id(cells[id_col]) if id_col < len(cells) else ""
            if not rid or not REQ_ID_PATTERN.match(rid):
                continue
            label = (
                cells[label_col]
                if label_col is not None and label_col < len(cells)
                else ""
            )
            response = ", ".join(
                c for c in (cells[i] for i in resp_cols if i < len(cells)) if c
            )
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
    # Builds a union of all requirement IDs found in all responses
    all_ids = sorted({rid for _, resp in vendors for rid in resp}, key=req_id_key)
    rows = [["vendor", *all_ids], ["", *(labels.get(rid, "") for rid in all_ids)]]
    for vendor, responses in vendors:
        rows.append([vendor, *(responses.get(rid, NOT_FOUND_MSG) for rid in all_ids)])
    return rows


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
    """Extracted-table artifacts produced from the model's current file revision.

    Extraction jobs record a 'produces' relationship with the model's file
    revision on the left and each output artifact's revision on the right;
    the right side is a revision DTO whose owning_entity_id is the artifact.
    """
    artifacts, related, seen = [], [], set()
    for rel in client.resources.relationships.list(
        model.file_revision_id,
        left_revision_id=[model.file_revision_id],
        owning_entity_type=["artifact"],
    ):
        out = rel.right_revision
        name = (out.name or "").lower()
        ext = (out.extension or "").lower().lstrip(".")
        related.append(name)
        if (
            ext in ("json", "csv")
            and "table" in name
            and out.owning_entity_id not in seen
        ):
            seen.add(out.owning_entity_id)
            artifacts.append(client.resources.get(out.owning_entity_id))
    if debug and not artifacts:
        log(
            f"debug: {vendor_name(model)}: no artifact matched the table filter; "
            f"related artifacts: {related or 'none'}"
        )
    return artifacts


def vendor_name(model) -> str:
    name = model.display_name or model.name or model.resource_id
    return Path(name).stem


def gather_tables(artifacts, vendor: str = "?"):
    """Parse a vendor's artifacts into tables, avoiding double counting.

    Extraction produces both a combined tables.json and one CSV per table, and
    a re-extracted model carries a second full set — so keep only the newest
    artifact per filename, and prefer the JSON (falling back to the CSVs only
    when no JSON yields any rows).
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

    def parse_all(subset):
        tables = []
        for a in subset:
            found = list(tables_from_artifact(a.read_bytes()))
            log(f"{vendor}: {a.name}: {sum(1 for t in found if t)} table(s)")
            tables.extend(found)
        return tables

    tables, used = parse_all(json_arts), json_arts
    if not any(tables):
        if json_arts:
            log(
                f"warning: {vendor}: no JSON artifact yielded tables — "
                f"falling back to {len(csv_arts)} CSV artifact(s)"
            )
        tables, used = parse_all(csv_arts), csv_arts
    return tables, used


def main() -> int:

    # Load config
    load_dotenv()
    EXTRACT_FUNCTION = os.environ.get("EXTRACT_FUNCTION", "@istari:extract_tables")
    ISTARI_API_URL = os.environ.get("ISTARI_API_URL", "https://api.dev.istari.app")
    ISTARI_CREDENTIALS_PATH = os.environ.get(
        "ISTARI_CREDENTIALS_PATH", ".istari_credentials.json"
    )
    DEFAULT_JOB_TIMEOUT_S = float(os.environ.get("DEFAULT_JOB_TIMEOUT_S", 3600))

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
        default="baseline",
        help="System branch to read models from (default: baseline)",
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
        default=DEFAULT_JOB_TIMEOUT_S,
        help=f"Seconds to wait for each extraction job (default: {DEFAULT_JOB_TIMEOUT_S:g})",
    )
    args = ap.parse_args()

    rfi_ref = (
        f"file:{args.rfi_file}" if args.rfi_file is not None else f"id:{args.rfi_id}"
    )
    log(
        f"rfi_compare v{VERSION}: api={ISTARI_API_URL} "
        f"credentials={ISTARI_CREDENTIALS_PATH} system={args.system_id} "
        f"branch={args.branch} rfi={rfi_ref} function={args.function} "
        f"force={args.force} job-timeout={args.job_timeout:g}s output={args.output}"
    )

    client = Istari(
        config=Configuration(
            digital_api_url=ISTARI_API_URL,
            identity_service_secret_file=ISTARI_CREDENTIALS_PATH,
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
        try:
            status = job.poll(timeout=args.job_timeout)
        except TimeoutError:
            log(
                f"error: extraction job {job.id} for {vendor_name(model)} still "
                f"running after {args.job_timeout:g}s — giving up (raise "
                f"--job-timeout if extractions legitimately take longer)"
            )
            return 1
        if status != "Completed":
            log(f"error: extraction job for {vendor_name(model)} ended '{status}'")
            try:  # best-effort: surface the agent's failure reason in the log
                detail = client.jobs.get(job.id).status
                if detail is not None and detail.message:
                    log(f"error: job {job.id} last status message: {detail.message}")
            except Exception:
                pass
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
        tables, used = gather_tables(artifacts, vendor_name(model))
        responses, names = compile_tables(tables, vendor_name(model))
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
    try:
        raise SystemExit(main())
    except Exception as e:
        log(f"error: {e.__class__.__name__}: {e}")
        raise SystemExit(1)
