# RFI Comparison Script

Builds a cross-vendor RFI response comparison matrix from an Istari system.
Packaged as a single executable and wired into an Istari **cl_module**
function: the job's input model is the RFI document, every other model on the
system's branch is treated as one vendor's response, and the comparison report
becomes an output of the job.

## What it does

1. **Discover** — lists the MODEL resources tracked on the system's branch.
   The RFI itself is identified by exact content match against the job's input
   file (filename match as fallback, or `--rfi-id` for manual runs) and
   excluded; the remaining models are the vendor responses.
2. **Extract** — for each response model without extracted-table artifacts,
   submits an extraction job (`@istari:extract_tables` by default) and waits
   for completion. Existing artifacts are reused; `--force` re-extracts.
3. **Compile** — parses each vendor's artifacts (the combined `tables.json`
   is preferred; per-table CSVs are a fallback; the newest artifact per
   filename wins) into a `{requirement ID -> response}` mapping. Per table,
   the requirement-ID column is detected by value scanning (column 0 by
   default, overridden by a header-hinted column whose cells are more often
   ID-shaped), the response column(s) by header hints (`Response`, `Answer`,
   `Compliance`, ...) or position, an optional label column, and any other
   columns are dropped. Requirement IDs may be numeric-dotted (`1.10`, `7.8`)
   or alphanumeric codes (`KSA-1`); they are normalized for cross-vendor
   correlation (uppercase, whitespace/edge punctuation trimmed, unicode
   dashes unified) but never rewritten — `1.04` and `KPP 1.1` stay verbatim
   and surface as their own columns for manual review. Vendor response text
   is passed through untouched. Duplicate IDs within a vendor are joined with
   `" | "` and warned about.
4. **Report** — correlates requirement IDs across vendors into a wide CSV
   (one column per requirement, one row per vendor, a label row underneath the
   header) written to the working directory as `rfi_response_comparison.csv`.
   Nothing is uploaded by the script itself: the cl_module job machinery
   uploads the working directory as the job's output.

## Usage

As the cl_module function invokes it (the runner exposes the downloaded input
file path as the `input_model` environment variable):

```sh
rfi_compare "$system_id" --rfi-file "$input_model"
```

Manual runs:

```sh
rfi_compare SYSTEM_ID --rfi-id RFI_MODEL_UUID          # identify RFI by UUID
rfi_compare SYSTEM_ID --rfi-file path/to/rfi.pdf       # identify RFI by file
```

Options:

| Flag | Default | Purpose |
|---|---|---|
| `--rfi-file PATH` | — | Local RFI file; the matching tracked model is excluded (required, or `--rfi-id`) |
| `--rfi-id UUID` | — | RFI model UUID to exclude (alternative to `--rfi-file`) |
| `--branch NAME` | `baseline` | System branch to read models from (falls back to the sole branch) |
| `--force` | off | Re-run extraction even when table artifacts exist |
| `--function NAME` | `@istari:extract_tables` | Extraction function to submit |
| `-o PATH` | `rfi_response_comparison.csv` | Report path |
| `--job-timeout SECS` | `3600` | Max wait per extraction job |

### Configuration & authentication

Deployment configuration is read from environment variables (a `.env` in the
working directory is loaded first for local dev; real environment variables
win):

| Variable | Default | Purpose |
|---|---|---|
| `ISTARI_API_URL` | `https://api.dev.istari.app` | Registry to connect to |
| `ISTARI_CREDENTIALS_PATH` | `.istari_credentials.json` | Identity-service client-credentials JSON (`clientId` / `keyId` / `key`) |
| `EXTRACT_FUNCTION` | `@istari:extract_tables` | Default extraction function (`--function` overrides) |
| `DEFAULT_JOB_TIMEOUT_S` | `3600` | Default extraction wait (`--job-timeout` overrides) |

In the module, these are set in `module_config.json`'s `environment` block,
with `ISTARI_CREDENTIALS_PATH` pointing at the credentials file's permanent
location in the module install directory (`chmod 600`). The credentials are
read in place — they never enter the job's working directory, which is
uploaded as a job output.

### Note on viewing the report

The CSV stores requirement IDs as text (`1.10` is distinct from `1.1`).
Excel/Numbers will coerce `1.10` to the number `1.1` on open — that is a
display artifact, not data loss. Load programmatically with `dtype=str`
(pandas) or import as text columns.

## Development

Dependency management is Poetry (in-project `.venv/`):

```sh
poetry install          # create .venv and install deps (incl. dev)
poetry run pytest       # unit tests (mocked; no network)
poetry run rfi-compare SYSTEM_ID --rfi-id UUID
```

## Building the Linux executable

The cl_module ships a self-contained binary. Cross-compiling from macOS is not
possible, so the build runs PyInstaller inside a Linux container:

```sh
./scripts/build_linux.sh            # linux/amd64 -> dist/linux-amd64/rfi_compare
./scripts/build_linux.sh arm64      # linux/arm64
```

Tagged releases (`v*`) also build Linux/Windows/macOS executables via the
GitHub Actions workflow (`.github/workflows/build.yml`); the Linux leg runs in
the same bullseye container as the local script.

The image base is `python:3.12-slim-bullseye` (glibc 2.31) so the binary runs
on Ubuntu 20.04+ / Debian 11+ / RHEL 9+. Copy the result over
`cl_modules/rfi_comparison_module/scripts/rfi_compare` in the module repo.

## cl_module wiring (module repo)

Reference copies of the module files live in `cl_module_config_files/`.
`module_manifest.json` function inputs: `input_model` (`user_model`) and
`system_id` (`parameter`). `module_config.json` invokes the binary directly
(no wrapper script) and carries the environment block described above:

```json
"executable": "$RFI_MODULE_DIR/scripts/rfi_compare",
"arguments": ["\"$system_id\"", "--rfi-file", "\"$input_model\""]
```

The system ID must stay a parameter: the job runner passes no system/job
metadata to functions, and a model can be tracked by multiple systems, so the
input file alone cannot disambiguate which vendor set to compare against.

