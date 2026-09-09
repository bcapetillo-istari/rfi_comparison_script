"""Unit tests for rfi_compare — pure parsing/compile logic plus the
Istari-facing helpers exercised against small fakes. No network access."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import rfi_compare as rc


# ----------------------------------------------------------------------------
# Row/ID handling
# ----------------------------------------------------------------------------

class TestLooksLikeHeader:
    @pytest.mark.parametrize("row", [
        ["Req ID", "Requirement", "Vendor Response"],
        ["ID", "Code", "Remarks"],
        ["Loiter Time", "x", "y"],
        ["KPP 7 (2 of 2)", "", ""],
        ["", "", ""],
    ])
    def test_non_data_rows_are_headers(self, row):
        assert rc.looks_like_header(row)

    @pytest.mark.parametrize("row", [
        ["1.1", "Range", "410 km"],
        ["1.10", "Navigation", "GPS"],
        ["7.8", "", ""],
        ["KSA-1", "Airworthiness", "Meets USAF criteria"],
        ["KSA-10", "", ""],
    ])
    def test_id_rows_are_data(self, row):
        assert not rc.looks_like_header(row)

    def test_empty_row(self):
        assert not rc.looks_like_header([])


class TestReqIdKey:
    def test_numeric_dotted_order(self):
        ids = ["1.11", "1.2", "10.1", "1.1", "2.1", "1.10"]
        assert sorted(ids, key=rc.req_id_key) == [
            "1.1", "1.2", "1.10", "1.11", "2.1", "10.1"]

    def test_alphanumeric_order_and_placement(self):
        ids = ["KSA-10", "1.1", "KSA-2", "KSA-1", "10.2"]
        assert sorted(ids, key=rc.req_id_key) == [
            "1.1", "10.2", "KSA-1", "KSA-2", "KSA-10"]


# ----------------------------------------------------------------------------
# Artifact parsing
# ----------------------------------------------------------------------------

TABLE = [["ID", "Code", "Remarks"], ["1.1", "C", "410 km."]]


class TestIterTables:
    def test_extractor_shape(self):
        # the real tables.json shape: dict -> tables -> list of table objects
        data = {"n_tables": 2, "tables": [{"rows": TABLE}, {"rows": [["1.2", "C", "x"]]}]}
        assert list(rc.iter_tables(data)) == [TABLE, [["1.2", "C", "x"]]]

    def test_list_of_tables(self):
        assert list(rc.iter_tables([TABLE, TABLE])) == [TABLE, TABLE]

    def test_single_table(self):
        assert list(rc.iter_tables(TABLE)) == [TABLE]

    @pytest.mark.parametrize("data", [{}, [], {"n_tables": 0, "tables": []}, "text", None])
    def test_empty_or_foreign_shapes(self, data):
        assert list(rc.iter_tables(data)) == []


class TestRowsFromArtifact:
    def test_json_bytes(self):
        raw = json.dumps({"tables": [{"rows": TABLE}]}).encode()
        assert list(rc.rows_from_artifact(raw)) == TABLE

    def test_csv_bytes_with_bom_and_quotes(self):
        raw = '﻿ID,Code,Remarks\r\n"1.1","C","410 km, demonstrated."\r\n'.encode("utf-8")
        rows = list(rc.rows_from_artifact(raw))
        assert rows == [["ID", "Code", "Remarks"], ["1.1", "C", "410 km, demonstrated."]]

    def test_malformed_json_falls_back_to_csv(self):
        rows = list(rc.rows_from_artifact(b"[not json,but has,a comma"))
        assert rows == [["[not json", "but has", "a comma"]]


# ----------------------------------------------------------------------------
# Compile and matrix
# ----------------------------------------------------------------------------

class TestCompileResponses:
    def test_basic_three_column(self):
        responses, labels = rc.compile_responses(
            [["ID", "Code", "Remarks"], ["1.1", "C", "Range answer."]], "v")
        assert responses == {"1.1": "Range answer."}
        assert labels == {"1.1": "C"}

    def test_alphanumeric_ids_kept_and_prose_skipped(self):
        rows = [["KSA-1", "C", "Airworthiness answer."],
                ["KPP 7 (2 of 2)", "", ""],
                ["", "", ""]]
        responses, _ = rc.compile_responses(rows, "v")
        assert responses == {"KSA-1": "Airworthiness answer."}

    def test_duplicates_joined_with_pipe(self, capsys):
        rows = [["1.12", "C", "Mission Planning."], ["1.12", "PC", "On-board Processing."]]
        responses, labels = rc.compile_responses(rows, "v")
        assert responses["1.12"] == "Mission Planning. | On-board Processing."
        assert labels["1.12"] == "C"  # first occurrence wins
        assert "duplicate requirement ID '1.12'" in capsys.readouterr().err

    def test_wide_rows_joined_short_rows_padded(self):
        responses, _ = rc.compile_responses(
            [["1.1", "C", "part a", "part b"], ["1.2", "PC"]], "v")
        assert responses["1.1"] == "part a, part b"
        assert responses["1.2"] == ""


class TestBuildMatrix:
    def test_union_sort_labels_and_gaps(self):
        vendors = [("alpha", {"1.1": "a1", "1.10": "a10"}),
                   ("bravo", {"1.1": "b1", "KSA-1": "bk"})]
        labels = {"1.1": "Range", "KSA-1": "Airworthiness"}
        matrix = rc.build_matrix(vendors, labels)
        assert matrix[0] == ["vendor", "1.1", "1.10", "KSA-1"]
        assert matrix[1] == ["", "Range", "", "Airworthiness"]
        assert matrix[2] == ["alpha", "a1", "a10", ""]
        assert matrix[3] == ["bravo", "b1", "", "bk"]


# ----------------------------------------------------------------------------
# Fakes for Istari objects
# ----------------------------------------------------------------------------

def fake_artifact(name, content=b"", created=0, extension=None):
    ext = extension if extension is not None else name.rsplit(".", 1)[-1]
    return SimpleNamespace(name=name, extension=ext, created=created,
                           read_bytes=lambda: content)


def fake_model(name, resource_id, content=b"", resource_type="MODEL"):
    return SimpleNamespace(
        name=name, display_name=None, resource_id=resource_id,
        resource_type=resource_type, size=len(content),
        file_revision_id=f"rev-{resource_id}",
        read_bytes=lambda: content)


class FakeBranches:
    def __init__(self, branches, files):
        self._branches, self._files = branches, files

    def list(self, system_id):
        return self._branches

    def list_files(self, branch, name=None):
        return [f for f in self._files if name is None or f.name == name]


def fake_client(branches=(), files=()):
    return SimpleNamespace(systems=SimpleNamespace(
        branches=FakeBranches(list(branches), list(files))))


# ----------------------------------------------------------------------------
# gather_rows
# ----------------------------------------------------------------------------

class TestGatherRows:
    def test_prefers_json_over_redundant_csvs(self):
        js = fake_artifact("tables.json", json.dumps({"tables": [{"rows": TABLE}]}).encode())
        cs = fake_artifact("table_01_p2.csv", b"1.1,C,dup of same data\n")
        rows, used = rc.gather_rows([cs, js])
        assert rows == TABLE
        assert [a.name for a in used] == ["tables.json"]

    def test_falls_back_to_csv_when_json_empty(self):
        js = fake_artifact("tables.json", json.dumps({"n_tables": 0, "tables": []}).encode())
        cs = fake_artifact("table_01_p2.csv", b"1.1,C,resp\n")
        rows, used = rc.gather_rows([js, cs])
        assert rows == [["1.1", "C", "resp"]]
        assert [a.name for a in used] == ["table_01_p2.csv"]

    def test_newest_artifact_per_filename_wins(self):
        old = fake_artifact("tables.json", json.dumps({"tables": [{"rows": [["1.1", "C", "old"]]}]}).encode(), created=1)
        new = fake_artifact("tables.json", json.dumps({"tables": [{"rows": [["1.1", "C", "new"]]}]}).encode(), created=2)
        rows, _ = rc.gather_rows([old, new])
        assert rows == [["1.1", "C", "new"]]


# ----------------------------------------------------------------------------
# Branch / RFI identification
# ----------------------------------------------------------------------------

class TestGetBranch:
    def test_exact_match(self):
        b = SimpleNamespace(tag="baseline")
        client = fake_client(branches=[b])
        assert rc.get_branch(client, "sys", "baseline") is b

    def test_sole_branch_fallback(self, capsys):
        b = SimpleNamespace(tag="baseline")
        client = fake_client(branches=[b])
        assert rc.get_branch(client, "sys", "main") is b
        assert "using sole branch" in capsys.readouterr().err

    def test_missing_among_many_exits(self):
        client = fake_client(branches=[SimpleNamespace(tag="a"), SimpleNamespace(tag="b")])
        with pytest.raises(SystemExit):
            rc.get_branch(client, "sys", "main")


class TestIdentifyRfi:
    def test_content_match_beats_name(self, tmp_path):
        rfi = tmp_path / "doc.pdf"
        rfi.write_bytes(b"RFI CONTENT")
        by_content = fake_model("renamed.pdf", "m1", b"RFI CONTENT")
        by_name = fake_model("doc.pdf", "m2", b"other bytes!")
        assert rc.identify_rfi([by_name, by_content], rfi) is by_content

    def test_name_fallback(self, tmp_path, capsys):
        rfi = tmp_path / "doc.pdf"
        rfi.write_bytes(b"local-only bytes")
        model = fake_model("doc.pdf", "m1", b"different")
        assert rc.identify_rfi([model], rfi) is model
        assert "matched by filename only" in capsys.readouterr().err

    def test_no_match(self, tmp_path):
        rfi = tmp_path / "doc.pdf"
        rfi.write_bytes(b"x")
        assert rc.identify_rfi([fake_model("other.pdf", "m1", b"yy")], rfi) is None


class TestListResponseModels:
    def _system(self, tmp_path):
        rfi_bytes = b"RFI DOCUMENT"
        rfi_file = tmp_path / "rfi.pdf"
        rfi_file.write_bytes(rfi_bytes)
        branch = SimpleNamespace(tag="baseline")
        files = [
            fake_model("rfi.pdf", "rfi-id", rfi_bytes),
            fake_model("vendor_a.pdf", "a", b"aaa"),
            fake_model("vendor_b.pdf", "b", b"bbb"),
            fake_model("report.csv", "art", b"c", resource_type="ARTIFACT"),
        ]
        return fake_client(branches=[branch], files=files), branch, rfi_file

    def test_excludes_rfi_by_file_and_non_models(self, tmp_path):
        client, branch, rfi_file = self._system(tmp_path)
        models = rc.list_response_models(client, branch, None, rfi_file)
        assert sorted(m.resource_id for m in models) == ["a", "b"]

    def test_excludes_rfi_by_id(self, tmp_path):
        client, branch, _ = self._system(tmp_path)
        models = rc.list_response_models(client, branch, "rfi-id", None)
        assert sorted(m.resource_id for m in models) == ["a", "b"]

    def test_unidentified_rfi_warns_and_keeps_all(self, tmp_path, capsys):
        client, branch, _ = self._system(tmp_path)
        stray = tmp_path / "unrelated.pdf"
        stray.write_bytes(b"not in system")
        models = rc.list_response_models(client, branch, None, stray)
        assert sorted(m.resource_id for m in models) == ["a", "b", "rfi-id"]
        assert "could not identify the RFI" in capsys.readouterr().err


# ----------------------------------------------------------------------------
# Misc helpers
# ----------------------------------------------------------------------------

class TestTypeName:
    def test_plain_string(self):
        assert rc.type_name("MODEL") == "MODEL"

    def test_enum_like(self):
        class FakeEnum:
            value = "ResourceTypeDto.ARTIFACT"
        assert rc.type_name(FakeEnum()) == "ARTIFACT"


class TestVendorName:
    def test_stem_of_name(self):
        m = SimpleNamespace(display_name=None, name="RFI_Response_B_Talon.pdf",
                            resource_id="x")
        assert rc.vendor_name(m) == "RFI_Response_B_Talon"

    def test_display_name_priority(self):
        m = SimpleNamespace(display_name="Talon Dynamics", name="f.pdf", resource_id="x")
        assert rc.vendor_name(m) == "Talon Dynamics"
