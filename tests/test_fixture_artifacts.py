"""Fixture-driven tests: run every example artifact through the real
parse -> compile pipeline.

Drop example extracted-table artifacts (``*.json`` / ``*.csv``) into
``tests/fixtures/`` (or point ``RFI_FIXTURES_DIR`` at a folder); every file
found becomes a test case automatically.

Two modes per fixture:

- With a sibling ``<filename>.expected.json`` (``{"responses": {...},
  "labels": {...}}`` — labels optional): the compiled output must match it
  exactly.
- Without one: structural checks only — the file must parse and yield at
  least one requirement with at least one non-empty response. Set
  ``RFI_UPDATE_EXPECTED=1`` to write the expectation file from current
  output for review.
"""

import json
import os
from pathlib import Path

import pytest

import rfi_compare as rc

FIXTURES_DIR = Path(
    os.environ.get("RFI_FIXTURES_DIR", Path(__file__).parent / "fixtures")
)


def fixture_inputs():
    if not FIXTURES_DIR.is_dir():
        return []
    return sorted(
        p
        for p in FIXTURES_DIR.rglob("*")
        if p.suffix.lower() in (".json", ".csv")
        and not p.name.endswith(".expected.json")
    )


INPUTS = fixture_inputs()

if not INPUTS:
    pytest.skip(
        f"no example artifacts in {FIXTURES_DIR} "
        "(drop *.json/*.csv there, or set RFI_FIXTURES_DIR)",
        allow_module_level=True,
    )


@pytest.mark.parametrize(
    "path", INPUTS, ids=[str(p.relative_to(FIXTURES_DIR)) for p in INPUTS]
)
def test_fixture_compiles(path):
    tables = list(rc.tables_from_artifact(path.read_bytes()))
    responses, labels = rc.compile_tables(tables, vendor=path.stem)

    expected_path = path.with_name(path.name + ".expected.json")
    if expected_path.exists():
        expected = json.loads(expected_path.read_text())
        assert responses == expected["responses"], f"{path.name}: responses differ"
        if "labels" in expected:
            assert labels == expected["labels"], f"{path.name}: labels differ"
        return

    if os.environ.get("RFI_UPDATE_EXPECTED"):
        expected_path.write_text(
            json.dumps({"responses": responses, "labels": labels},
                       indent=2, sort_keys=True) + "\n"
        )
        pytest.fail(
            f"wrote {expected_path.name} from current output — review it, "
            "then re-run without RFI_UPDATE_EXPECTED"
        )

    assert responses, (
        f"{path.name}: parsed zero requirements — parser is not prepared "
        "for this shape"
    )
    assert any(v.strip() for v in responses.values()), (
        f"{path.name}: every response came out empty — column mapping "
        "likely wrong for this shape"
    )
