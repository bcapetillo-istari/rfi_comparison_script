"""Column-variance tests for extracted-table parsing.

Extraction output isn't guaranteed to use one header vocabulary or column
count: different documents yield 'Req ID' vs 'requirement id', tables with a
missing label column, tables with extra columns, and JSON wrappers of varying
shape. These tests pin down how compile_responses / rows_from_artifact handle
each family of variation.

Known gaps (parser is positional; it never reads header *names* to map
columns) are marked xfail(strict=True) so they flip loudly to XPASS the day
the parser learns to handle them.
"""

import json

import pytest

import rfi_compare as rc


def compile_rows(rows, vendor="v"):
    return rc.compile_responses(rows, vendor)


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


# ----------------------------------------------------------------------------
# Extra columns
# ----------------------------------------------------------------------------

class TestExtraColumns:
    def test_four_columns_joined_into_response(self):
        rows = [["ID", "Code", "Remarks", "Notes"],
                ["1.1", "C", "410 km.", "See Annex B"]]
        responses, _ = compile_rows(rows)
        assert responses == {"1.1": "410 km., See Annex B"}

    def test_empty_extra_cells_not_joined(self):
        rows = [["1.1", "C", "410 km.", "", ""]]
        responses, _ = compile_rows(rows)
        assert responses == {"1.1": "410 km."}

    def test_many_columns(self):
        rows = [["1.1", "C", "a", "b", "c", "d"]]
        responses, _ = compile_rows(rows)
        assert responses == {"1.1": "a, b, c, d"}


# ----------------------------------------------------------------------------
# Missing columns
# ----------------------------------------------------------------------------

class TestMissingColumns:
    def test_two_columns_second_read_as_label(self):
        # current positional rule: (ID, label) — response comes up empty
        responses, labels = compile_rows([["1.1", "Range"]])
        assert responses == {"1.1": ""}
        assert labels == {"1.1": "Range"}

    def test_id_only_row(self):
        responses, labels = compile_rows([["1.1"]])
        assert responses == {"1.1": ""}
        assert labels == {"1.1": ""}

    @pytest.mark.xfail(
        strict=True,
        reason="positional parser reads col 2 as the label; a 2-column "
               "(ID, response) table loses its responses — needs a decision "
               "or header-aware mapping once real examples exist",
    )
    def test_two_column_id_response_table(self):
        rows = [["Req ID", "Response"], ["1.1", "410 km demonstrated."]]
        responses, _ = compile_rows(rows)
        assert responses == {"1.1": "410 km demonstrated."}


# ----------------------------------------------------------------------------
# Column order (parser is positional; header names are not consulted)
# ----------------------------------------------------------------------------

class TestColumnOrder:
    @pytest.mark.xfail(
        strict=True,
        reason="parser never maps columns by header name, so a reordered "
               "table (ID not first) is dropped entirely",
    )
    def test_id_column_not_first(self):
        rows = [["Requirement", "Req ID", "Response"],
                ["Range", "1.1", "410 km."]]
        responses, _ = compile_rows(rows)
        assert responses == {"1.1": "410 km."}

    def test_label_and_response_swapped_is_undetectable(self):
        # (ID, response, label) parses "successfully" with the columns
        # crossed — nothing in-band distinguishes it. Documented, not fixed.
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
        rows = list(rc.rows_from_artifact(raw))
        responses, _ = compile_rows(rows)
        assert responses == {"1.1": "410 km."}

    def test_numeric_cells_stringified(self):
        raw = json.dumps({"tables": [{"rows": [[1.1, "C", 410]]}]}).encode()
        responses, _ = compile_rows(rc.rows_from_artifact(raw))
        assert responses == {"1.1": "410"}

    def test_null_cells_are_not_data(self):
        raw = json.dumps({"tables": [{"rows": [["1.1", None, "410 km."]]}]}).encode()
        responses, labels = compile_rows(rc.rows_from_artifact(raw))
        assert responses == {"1.1": "410 km."}
        assert labels == {"1.1": "None"} or labels == {"1.1": ""}


# ----------------------------------------------------------------------------
# CSV content quirks
# ----------------------------------------------------------------------------

class TestCsvQuirks:
    def test_blank_lines_and_whitespace(self):
        raw = b"ID,Code,Remarks\n\n  1.1 , C , 410 km. \n\n"
        responses, _ = compile_rows(rc.rows_from_artifact(raw))
        assert responses == {"1.1": "410 km."}

    def test_quoted_multiline_cell(self):
        raw = b'ID,Code,Remarks\n1.1,C,"line one\nline two"\n'
        responses, _ = compile_rows(rc.rows_from_artifact(raw))
        assert responses == {"1.1": "line one\nline two"}

    def test_ragged_rows(self):
        raw = b"1.1,C,410 km.\n1.2,PC\n1.3\n"
        responses, _ = compile_rows(rc.rows_from_artifact(raw))
        assert responses == {"1.1": "410 km.", "1.2": "", "1.3": ""}
