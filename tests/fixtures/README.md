# Example extracted-table artifacts

Drop example inputs here (any layout, subfolders fine):

- `*.json` — extractor `tables.json`-style outputs
- `*.csv` — per-table CSV outputs

Every file is auto-discovered by `tests/test_fixture_artifacts.py` and run
through the real `rows_from_artifact` -> `compile_responses` pipeline.

To pin exact expected output for a fixture, add a sibling
`<filename>.expected.json`:

```json
{
  "responses": {"1.1": "410 km demonstrated.", "1.2": "9 hr."},
  "labels":    {"1.1": "Range",                "1.2": "Endurance"}
}
```

(`labels` is optional.) To generate one from the parser's current output for
review:

```sh
RFI_UPDATE_EXPECTED=1 poetry run pytest tests/test_fixture_artifacts.py
```

Fixtures without an expectation file get structural checks only: must parse,
must yield >=1 requirement, and at least one non-empty response.
