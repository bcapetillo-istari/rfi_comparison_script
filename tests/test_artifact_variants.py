"""Column-variance tests for extracted-table parsing.

Extraction output isn't guaranteed to use one header vocabulary or column
count: different documents yield 'Req ID' vs 'requirement id', tables with a
missing label column, tables with extra columns, reordered columns, and JSON
wrappers of varying shape. These tests pin down how detect_columns /
compile_tables handle each family of variation, and how
clean_requirement_id keeps typographic ID variants correlating across
vendors without ever rewriting a vendor's designation.
"""

import json

import pytest

import rfi_compare as rc


def compile_rows(rows, vendor="v"):
    """Compile a single table's rows through the real pipeline."""
    return rc.compile_tables([rows], vendor)


# ----------------------------------------------------------------------------
# Header-name variants: any spelling must be skipped, never read as data
# ----------------------------------------------------------------------------

HEADER_VARIANTS = [
    ["Req ID", "Requirement", "Vendor Response"],
    ["requirement id", "description", "answer"],
    ["REQ_ID", "LABEL", "RESPONSE"],
    ["Requirement", "Code", "Remarks"],
    ["req", "desc", "resp"],
    ["Id", "Title", "Compliance"],
    ["ID", "Code", "Remarks"],
    ["#", "Item", "Response"],
    ["Req. No.", "Requirement Text", "Offeror Response"],
    ["Number", "Description", "Answer"],
]


class TestHeaderNameVariants:
    @pytest.mark.parametrize("header", HEADER_VARIANTS,
                             ids=[h[0] for h in HEADER_VARIANTS])
    def test_variant_header_skipped(self, header):
        rows = [header, ["1.1", "Range", "410 km."]]
        responses, labels = compile_rows(rows)
        assert responses == {"1.1": "410 km."}
        assert labels == {"1.1": "Range"}

    def test_repeated_headers_mid_table(self):
        # multi-page tables re-emit the header on each page
        rows = [["Req ID", "Requirement", "Response"],
                ["1.1", "Range", "410 km."],
                ["Req ID", "Requirement", "Response"],
                ["1.2", "Endurance", "9 hr."]]
        responses, _ = compile_rows(rows)
        assert responses == {"1.1": "410 km.", "1.2": "9 hr."}

    def test_headerless_table_is_all_data(self):
        rows = [["1.1", "Range", "410 km."], ["1.2", "Endurance", "9 hr."]]
        responses, _ = compile_rows(rows)
        assert responses == {"1.1": "410 km.", "1.2": "9 hr."}

    def test_title_and_prose_rows_skipped(self):
        rows = [["Table 3: Compliance Matrix", "", ""],
                ["Loiter Time", "x", "y"],
                ["1.1", "Range", "410 km."]]
        responses, _ = compile_rows(rows)
        assert responses == {"1.1": "410 km."}

    def test_split_title_row_not_an_id(self):
        # a truncated heading like 'KPP 6 -' must not become a requirement
        rows = [["KPP 6 -", "Camera", "Sensor | KPP 7 - FMV Sensor"],
                ["6.1", "Image Type", "EO/IR."]]
        responses, _ = compile_rows(rows)
        assert responses == {"6.1": "EO/IR."}


# ----------------------------------------------------------------------------
# ID normalization: typography correlates, designations stay verbatim
# ----------------------------------------------------------------------------

class TestIdNormalization:
    def test_typographic_variants_share_a_column(self):
        vendors = [
            ("alpha", rc.compile_tables([[["KSA-2", "IFF", "Mode 3."]]], "alpha")[0]),
            ("bravo", rc.compile_tables([[["ksa–2 ", "IFF", "Mode 5."]]], "bravo")[0]),
        ]
        matrix = rc.build_matrix(vendors, {})
        assert matrix[0] == ["vendor", "KSA-2"]
        assert matrix[2] == ["alpha", "Mode 3."]
        assert matrix[3] == ["bravo", "Mode 5."]

    def test_trailing_punctuation_row_recovered(self):
        responses, _ = compile_rows([["1.3:", "Cruise Altitude", "17,500 ft."]])
        assert responses == {"1.3": "17,500 ft."}

    def test_designations_never_rewritten(self):
        rows = [["KPP 1.1", "Range", "760 km."],
                ["1.04", "Climb Rate", "850 ft/min."]]
        responses, _ = compile_rows(rows)
        # vendor's own numbering is kept verbatim — mismatches surface as
        # separate matrix columns for manual review, never silently merged
        assert set(responses) == {"KPP 1.1", "1.04"}


# ----------------------------------------------------------------------------
# Extra columns
# ----------------------------------------------------------------------------

class TestExtraColumns:
    def test_unhinted_extra_column_dropped_with_header(self):
        rows = [["ID", "Code", "Remarks", "Notes"],
                ["1.1", "C", "410 km.", "See Annex B"]]
        responses, _ = compile_rows(rows)
        assert responses == {"1.1": "410 km."}  # 'Notes' is neither ID/label/response

    def test_multiple_response_columns_joined(self):
        rows = [["Req ID", "Requirement", "Threshold Response", "Objective Response"],
                ["7.4", "MTI Tracking", "N/A", "Single-target MTI track."]]
        responses, _ = compile_rows(rows)
        assert responses == {"7.4": "N/A, Single-target MTI track."}

    def test_headerless_extras_joined(self):
        responses, _ = compile_rows([["1.1", "C", "a", "b", "c", "d"]])
        assert responses == {"1.1": "a, b, c, d"}

    def test_empty_extra_cells_not_joined(self):
        responses, _ = compile_rows([["1.1", "C", "410 km.", "", ""]])
        assert responses == {"1.1": "410 km."}


# ----------------------------------------------------------------------------
# Missing columns
# ----------------------------------------------------------------------------

class TestMissingColumns:
    def test_two_column_id_response_with_header(self):
        rows = [["Req ID", "Vendor Response"], ["1.1", "410 km demonstrated."]]
        responses, labels = compile_rows(rows)
        assert responses == {"1.1": "410 km demonstrated."}
        assert labels == {"1.1": ""}

    def test_two_column_headerless_second_is_response(self):
        # with only two columns the non-ID column is the response — a
        # comparison without responses is useless, labels are decoration
        responses, labels = compile_rows(
            [["KSA-1", "Group 2/3 airworthiness self-certification."]])
        assert responses == {"KSA-1": "Group 2/3 airworthiness self-certification."}
        assert labels == {"KSA-1": ""}

    def test_id_only_row(self):
        responses, labels = compile_rows([["1.1"]])
        assert responses == {"1.1": ""}
        assert labels == {"1.1": ""}


# ----------------------------------------------------------------------------
# Column order
# ----------------------------------------------------------------------------

class TestColumnOrder:
    def test_id_column_not_first(self):
        rows = [["Requirement", "Req ID", "Vendor Response"],
                ["Range", "1.1", "410 km."],
                ["Loiter Time", "1.2", "9 hr."]]
        responses, labels = compile_rows(rows)
        assert responses == {"1.1": "410 km.", "1.2": "9 hr."}
        assert labels == {"1.1": "Range", "1.2": "Loiter Time"}

    def test_id_column_detection_needs_header_hint(self):
        # value scanning alone must not hijack a numeric non-ID column
        # (TRL/MRL scores, quantities) — without a header hint, col 0 rules
        rows = [["Milestone", "TRL", "MRL", "Date"],
                ["Prototype flight", "6", "5", "2025-08"],
                ["LRIP readiness", "7", "7", "2026-11"]]
        responses, _ = compile_rows(rows)
        assert responses == {}

    def test_label_and_response_swapped_is_undetectable(self):
        # headerless (ID, response, label) parses with the columns crossed —
        # nothing in-band distinguishes it. Documented, not fixed.
        rows = [["1.1", "410 km.", "Range"]]
        responses, labels = compile_rows(rows)
        assert responses == {"1.1": "Range"}
        assert labels == {"1.1": "410 km."}


# ----------------------------------------------------------------------------
# JSON wrapper shapes
# ----------------------------------------------------------------------------

TABLE = [["ID", "Code", "Remarks"], ["1.1", "C", "410 km."]]


class TestJsonWrapperVariants:
    @pytest.mark.parametrize("payload", [
        {"tables": [{"rows": TABLE}]},          # real extractor shape
        {"data": [{"rows": TABLE}]},            # 'data' wrapper
        {"rows": TABLE},                        # bare rows dict
        [{"rows": TABLE}],                      # list of table objects
        [TABLE],                                # list of tables
        TABLE,                                  # single table
    ], ids=["tables-key", "data-key", "rows-key", "obj-list", "table-list",
            "bare-table"])
    def test_wrapper_shapes(self, payload):
        raw = json.dumps(payload).encode()
        tables = list(rc.tables_from_artifact(raw))
        responses, _ = rc.compile_tables(tables, "v")
        assert responses == {"1.1": "410 km."}

    def test_numeric_cells_stringified(self):
        raw = json.dumps({"tables": [{"rows": [[1.1, "C", 410]]}]}).encode()
        responses, _ = rc.compile_tables(list(rc.tables_from_artifact(raw)), "v")
        assert responses == {"1.1": "410"}

    def test_null_cells_are_not_data(self):
        raw = json.dumps({"tables": [{"rows": [["1.1", None, "410 km."]]}]}).encode()
        responses, labels = rc.compile_tables(list(rc.tables_from_artifact(raw)), "v")
        assert responses == {"1.1": "410 km."}
        assert labels == {"1.1": ""}


# ----------------------------------------------------------------------------
# CSV content quirks
# ----------------------------------------------------------------------------

def compile_csv(raw: bytes, vendor="v"):
    return rc.compile_tables(list(rc.tables_from_artifact(raw)), vendor)


class TestCsvQuirks:
    def test_blank_lines_and_whitespace(self):
        raw = b"ID,Code,Remarks\n\n  1.1 , C , 410 km. \n\n"
        responses, _ = compile_csv(raw)
        assert responses == {"1.1": "410 km."}

    def test_quoted_multiline_cell(self):
        raw = b'ID,Code,Remarks\n1.1,C,"line one\nline two"\n'
        responses, _ = compile_csv(raw)
        assert responses == {"1.1": "line one\nline two"}

    def test_ragged_rows(self):
        raw = b"1.1,C,410 km.\n1.2,PC\n1.3\n"
        responses, _ = compile_csv(raw)
        assert responses == {"1.1": "410 km.", "1.2": "", "1.3": ""}
