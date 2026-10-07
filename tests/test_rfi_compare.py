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

class TestCleanRequirementId:
    @pytest.mark.parametrize("raw,cleaned", [
        ("1.1", "1.1"),
        (" 1.5 ", "1.5"),            # whitespace
        ("1.3:", "1.3"),             # trailing punctuation
        ("(1.1)", "1.1"),            # wrapping punctuation
        ("ksa-3", "KSA-3"),          # case
        ("KSA–2", "KSA-2"),     # en dash -> hyphen
        ("1.04", "1.04"),            # designation kept verbatim
        ("KPP 1.1", "KPP 1.1"),      # prefix kept verbatim
        ("KPP 6 -", "KPP 6 -"),      # trailing dash NOT stripped (truncated title)
    ])
    def test_normalization(self, raw, cleaned):
        assert rc.clean_requirement_id(raw) == cleaned


class TestIsRequirementId:
    @pytest.mark.parametrize("cell", [
        "1.1", "1.10", "7.8", "KSA-1", "KSA-10", "ksa-3", "1.3:", "KSA–2",
    ])
    def test_id_shaped(self, cell):
        assert rc.is_requirement_id(cell)

    @pytest.mark.parametrize("cell", [
        "Req ID", "ID", "Loiter Time", "KPP 7 (2 of 2)", "", "KPP 6 -",
        "10 units", "Table 3: Compliance Matrix",
    ])
    def test_not_id_shaped(self, cell):
        assert not rc.is_requirement_id(cell)


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


class TestTablesFromArtifact:
    def test_json_bytes(self):
        raw = json.dumps({"tables": [{"rows": TABLE}]}).encode()
        assert list(rc.tables_from_artifact(raw)) == [TABLE]

    def test_csv_bytes_with_bom_and_quotes(self):
        raw = '﻿ID,Code,Remarks\r\n"1.1","C","410 km, demonstrated."\r\n'.encode("utf-8")
        tables = list(rc.tables_from_artifact(raw))
        assert tables == [[["ID", "Code", "Remarks"], ["1.1", "C", "410 km, demonstrated."]]]

    def test_malformed_json_falls_back_to_csv(self):
        tables = list(rc.tables_from_artifact(b"[not json,but has,a comma"))
        assert tables == [[["[not json", "but has", "a comma"]]]


# ----------------------------------------------------------------------------
# Compile and matrix
# ----------------------------------------------------------------------------

class TestCompileTables:
    def test_basic_three_column(self):
        responses, labels = rc.compile_tables(
            [[["ID", "Code", "Remarks"], ["1.1", "C", "Range answer."]]], "v")
        assert responses == {"1.1": "Range answer."}
        assert labels == {"1.1": "C"}

    def test_alphanumeric_ids_kept_and_prose_skipped(self):
        table = [["KSA-1", "C", "Airworthiness answer."],
                 ["KPP 7 (2 of 2)", "", ""],
                 ["", "", ""]]
        responses, _ = rc.compile_tables([table], "v")
        assert responses == {"KSA-1": "Airworthiness answer."}

    def test_duplicates_joined_with_pipe(self, capsys):
        table = [["1.12", "C", "Mission Planning."], ["1.12", "PC", "On-board Processing."]]
        responses, labels = rc.compile_tables([table], "v")
        assert responses["1.12"] == "Mission Planning. | On-board Processing."
        assert labels["1.12"] == "C"  # first occurrence wins
        assert "duplicate requirement ID '1.12'" in capsys.readouterr().err

    def test_duplicates_joined_across_tables(self, capsys):
        tables = [[["1.12", "C", "Mission Planning."]],
                  [["1.12", "PC", "On-board Processing."]]]
        responses, _ = rc.compile_tables(tables, "v")
        assert responses["1.12"] == "Mission Planning. | On-board Processing."
        assert "duplicate requirement ID '1.12'" in capsys.readouterr().err

    def test_wide_rows_joined_short_rows_padded(self):
        responses, _ = rc.compile_tables(
            [[["1.1", "C", "part a", "part b"], ["1.2", "PC"]]], "v")
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
        assert matrix[2] == ["alpha", "a1", "a10", rc.NOT_FOUND_MSG]
        assert matrix[3] == ["bravo", "b1", rc.NOT_FOUND_MSG, "bk"]

    def test_numeric_aware_order(self):
        ids = ["1.11", "1.2", "10.1", "1.1", "KSA-10", "KSA-2", "2.1", "1.10"]
        assert sorted(ids, key=rc.req_id_key) == [
            "1.1", "1.2", "1.10", "1.11", "2.1", "10.1", "KSA-2", "KSA-10"]

    def test_header_order_is_deterministic_for_id_variants(self):
        # '1.4' and '1.04' share a numeric key; the raw-text tiebreak keeps
        # repeated runs producing identical header rows
        vendors = [("alpha", {"1.4": "a", "1.04": "b", "KPP 1.1": "c"})]
        for _ in range(3):
            matrix = rc.build_matrix(vendors, {})
            assert matrix[0] == ["vendor", "1.04", "1.4", "KPP 1.1"]


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
        # Real SDK (13.0.x) matches name as a case-insensitive substring.
        return [
            f
            for f in self._files
            if name is None or name.lower() in (f.name or "").lower()
        ]


def fake_client(branches=(), files=()):
    return SimpleNamespace(systems=SimpleNamespace(
        branches=FakeBranches(list(branches), list(files))))


# ----------------------------------------------------------------------------
# gather_rows
# ----------------------------------------------------------------------------

class TestGatherTables:
    def test_prefers_json_over_redundant_csvs(self):
        js = fake_artifact("tables.json", json.dumps({"tables": [{"rows": TABLE}]}).encode())
        cs = fake_artifact("table_01_p2.csv", b"1.1,C,dup of same data\n")
        tables, used = rc.gather_tables([cs, js])
        assert tables == [TABLE]
        assert [a.name for a in used] == ["tables.json"]

    def test_falls_back_to_csv_when_json_empty(self):
        js = fake_artifact("tables.json", json.dumps({"n_tables": 0, "tables": []}).encode())
        cs = fake_artifact("table_01_p2.csv", b"1.1,C,resp\n")
        tables, used = rc.gather_tables([js, cs])
        assert tables == [[["1.1", "C", "resp"]]]
        assert [a.name for a in used] == ["table_01_p2.csv"]

    def test_newest_artifact_per_filename_wins(self):
        old = fake_artifact("tables.json", json.dumps({"tables": [{"rows": [["1.1", "C", "old"]]}]}).encode(), created=1)
        new = fake_artifact("tables.json", json.dumps({"tables": [{"rows": [["1.1", "C", "new"]]}]}).encode(), created=2)
        tables, _ = rc.gather_tables([old, new])
        assert tables == [[["1.1", "C", "new"]]]


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
        models, rfi = rc.list_response_models(client, branch, None, rfi_file)
        assert sorted(m.resource_id for m in models) == ["a", "b"]
        assert rfi.resource_id == "rfi-id"

    def test_excludes_rfi_by_id(self, tmp_path):
        client, branch, _ = self._system(tmp_path)
        models, rfi = rc.list_response_models(client, branch, "rfi-id", None)
        assert sorted(m.resource_id for m in models) == ["a", "b"]
        assert rfi.resource_id == "rfi-id"

    def test_report_model_never_treated_as_vendor(self, tmp_path):
        # the report is uploaded as a MODEL; a tracked copy must be excluded
        # from the response set or each run would ingest its own output
        rfi_bytes = b"RFI DOCUMENT"
        rfi_file = tmp_path / "rfi.pdf"
        rfi_file.write_bytes(rfi_bytes)
        branch = SimpleNamespace(tag="baseline")
        files = [
            fake_model("rfi.pdf", "rfi-id", rfi_bytes),
            fake_model(rc.REPORT_FILENAME, "report-id", b"vendor,1.1\n"),
            fake_model("vendor_a.pdf", "a", b"aaa"),
        ]
        client = fake_client(branches=[branch], files=files)
        models, _ = rc.list_response_models(client, branch, None, rfi_file)
        assert sorted(m.resource_id for m in models) == ["a"]

    def test_unidentified_rfi_warns_and_keeps_all(self, tmp_path, capsys):
        client, branch, _ = self._system(tmp_path)
        stray = tmp_path / "unrelated.pdf"
        stray.write_bytes(b"not in system")
        models, rfi = rc.list_response_models(client, branch, None, stray)
        assert sorted(m.resource_id for m in models) == ["a", "b", "rfi-id"]
        assert rfi is None
        assert "could not identify the RFI" in capsys.readouterr().err


# ----------------------------------------------------------------------------
# Report lineage
# ----------------------------------------------------------------------------

class FakeWriter:
    """Legacy-client stand-in capturing add_model/update_model uploads.

    Sources are recorded as (revision_id, relationship_identifier) tuples —
    the registry turns each NewSource into a 'produces' edge, so capturing
    what was sent is the whole lineage contract at this level.
    """

    def __init__(self, fail=False):
        self._fail = fail
        self.adds = []     # [(path, [(rev_id, rel_id), ...])]
        self.updates = []  # [(model_id, path, [(rev_id, rel_id), ...])]
        self.descriptions = []

    @staticmethod
    def _model(model_id, rev_id):
        return SimpleNamespace(
            id=model_id,
            file=SimpleNamespace(
                id=f"file-{model_id}",
                revisions=[SimpleNamespace(id=rev_id, file_id=f"file-{model_id}")],
            ),
        )

    @staticmethod
    def _sources(sources):
        return [(s.revision_id, s.relationship_identifier) for s in sources or []]

    def add_model(self, path, sources=None, **kw):
        if self._fail:
            raise RuntimeError("upload rejected")
        self.adds.append((Path(path).name, self._sources(sources)))
        self.descriptions.append(kw.get("description"))
        return self._model("art-1", "rev-art-1")

    def update_model(self, model_id, path, sources=None, **kw):
        if self._fail:
            raise RuntimeError("upload rejected")
        self.updates.append((model_id, Path(path).name, self._sources(sources)))
        self.descriptions.append(kw.get("description"))
        return self._model(model_id, "rev-art-2")


class FakeLineageClient:
    def __init__(self, tracked=()):
        # tracked: (pinned_revision_id, resource_type) per existing branch entry
        self.commits = []
        self._tracked = list(tracked)
        outer = self

        class Branches:
            def list_files(self, branch, name=None):
                # tracked entries mimic a previously tracked report: same
                # filename as the report under test ('r.csv'), owned by art-1.
                # As in the real SDK, current_file_revision_id is the revision
                # the branch entry is pinned to, while file_revision_id is the
                # resource's latest revision — deliberately different here so a
                # regression to the wrong field fails the re-pin assertions.
                return [SimpleNamespace(current_file_revision_id=r,
                                        file_revision_id="rev-art-1",
                                        name="r.csv",
                                        resource_id="art-1",
                                        resource_type=t)
                        for (r, t) in outer._tracked]

            def commit(self, branch, add=None, remove=None):
                outer.commits.append((tuple(a.id for a in add or ()),
                                      tuple(r.id for r in remove or ())))

        self.systems = SimpleNamespace(branches=Branches())


class TestRecordReportLineage:
    @pytest.fixture
    def writer(self, monkeypatch):
        w = FakeWriter()
        monkeypatch.setattr(rc, "Client", lambda config=None: w)
        return w

    @pytest.fixture
    def failing_writer(self, monkeypatch):
        w = FakeWriter(fail=True)
        monkeypatch.setattr(rc, "Client", lambda config=None: w)
        return w

    def _models(self):
        return [fake_model("vendor_a.pdf", "a"), fake_model("vendor_b.pdf", "b")]

    def test_sources_ride_the_upload(self, tmp_path, capsys, writer):
        rc.record_report_lineage(FakeLineageClient(), None, self._models(),
                                 tmp_path / "r.csv")
        assert writer.adds == [
            ("r.csv", [("rev-a", "input"), ("rev-b", "input")]),
        ]
        assert "with 2 source(s)" in capsys.readouterr().err

    def test_rfi_recorded_in_description_not_graph(self, tmp_path, writer):
        # the RFI has no extraction artifact, and citing its (always-pinned)
        # model revision would pin every report revision into the files panel
        rfi = fake_model("rfi.pdf", "rfi-id")
        rc.record_report_lineage(FakeLineageClient(), None, self._models(),
                                 tmp_path / "r.csv", rfi=rfi)
        (_, sources), = writer.adds
        assert ("rev-rfi-id", "input") not in sources
        assert "RFI: rfi.pdf, revision rev-rfi-id" in writer.descriptions[0]

    def test_upload_failure_is_nonfatal(self, tmp_path, capsys, failing_writer):
        client = FakeLineageClient()
        rc.record_report_lineage(client, None, self._models(), tmp_path / "r.csv")
        assert client.commits == []
        assert "could not record report lineage" in capsys.readouterr().err

    def test_writer_construction_failure_is_nonfatal(self, tmp_path, capsys,
                                                     monkeypatch):
        def boom(config=None):
            raise RuntimeError("auth rejected")
        monkeypatch.setattr(rc, "Client", boom)
        client = FakeLineageClient()
        rc.record_report_lineage(client, None, self._models(), tmp_path / "r.csv")
        assert client.commits == []
        assert "could not record report lineage" in capsys.readouterr().err

    def test_remove_local_deletes_copy_after_upload(self, tmp_path, writer):
        report = tmp_path / "r.csv"
        report.write_text("vendor\n")
        rc.record_report_lineage(FakeLineageClient(), None, self._models(),
                                 report, remove_local=True)
        assert not report.exists()

    def test_remove_local_keeps_copy_when_upload_fails(self, tmp_path,
                                                       failing_writer):
        report = tmp_path / "r.csv"
        report.write_text("vendor\n")
        rc.record_report_lineage(FakeLineageClient(), None, self._models(),
                                 report, remove_local=True)
        assert report.exists()  # fallback: rides the job's working-dir output

    def test_local_copy_kept_by_default(self, tmp_path, writer):
        report = tmp_path / "r.csv"
        report.write_text("vendor\n")
        rc.record_report_lineage(FakeLineageClient(), None, self._models(), report)
        assert report.exists()  # manual CLI runs keep their CSV

    def test_first_run_creates_and_tracks_once(self, tmp_path, capsys, writer):
        client = FakeLineageClient()
        rc.record_report_lineage(client, None, self._models(), tmp_path / "r.csv",
                                 branch=SimpleNamespace(tag="baseline"))
        # one commit, pinned to the first revision — same shape as a UI upload
        assert client.commits == [(("rev-art-1",), ())]
        assert "report tracked on branch 'baseline'" in capsys.readouterr().err

    def test_rerun_adds_revision_and_repins(self, tmp_path, capsys, writer):
        client = FakeLineageClient(
            tracked=[("rev-old-report", rc.REPORT_RESOURCE_TYPE)])
        report = tmp_path / "r.csv"
        report.write_text("content\n")
        rc.record_report_lineage(client, None, self._models(), report,
                                 branch=SimpleNamespace(tag="baseline"))
        # uploaded as a revision of the existing model, with fresh sources
        assert writer.updates == [
            ("art-1", "r.csv", [("rev-a", "input"), ("rev-b", "input")]),
        ]
        assert writer.adds == []
        # pin advanced in one commit; remove carries BOTH the entry's
        # pre-upload pin and the new revision, covering either matching
        # semantics (unmatched ids are no-ops)
        assert client.commits == [(("rev-art-2",), ("rev-old-report", "rev-art-2"))]
        err = capsys.readouterr().err
        assert "uploaded new revision" in err
        assert "re-pinned to the new revision" in err

    def test_identical_content_still_gets_a_revision(self, tmp_path, writer):
        # revision history is the run history: byte-identical reruns publish
        # too (storage dedupes the bytes; artifact-sourced lineage keeps the
        # files panel at one row regardless)
        client = FakeLineageClient(
            tracked=[("rev-old-report", rc.REPORT_RESOURCE_TYPE)])
        report = tmp_path / "r.csv"
        report.write_text("same content every run\n")
        rc.record_report_lineage(client, None, self._models(), report,
                                 branch=SimpleNamespace(tag="baseline"))
        assert len(writer.updates) == 1
        assert client.commits == [(("rev-art-2",), ("rev-old-report", "rev-art-2"))]

    def test_duplicate_entries_collapsed_on_rerun(self, tmp_path, capsys,
                                                  writer):
        # a stale second entry (e.g. legacy ARTIFACT copy) is un-pinned in the
        # same commit that re-pins the new revision
        client = FakeLineageClient(
            tracked=[("rev-old-report", rc.REPORT_RESOURCE_TYPE),
                     ("rev-stale-artifact", "ARTIFACT")])
        report = tmp_path / "r.csv"
        report.write_text("content\n")
        rc.record_report_lineage(client, None, self._models(), report,
                                 branch=SimpleNamespace(tag="baseline"))
        assert len(writer.updates) == 1
        assert client.commits == [
            (("rev-art-2",),
             ("rev-old-report", "rev-stale-artifact", "rev-art-2"))]

    def test_legacy_artifact_entry_collapsed_into_new_model(self, tmp_path,
                                                            capsys, writer):
        # pre-1.1 runs tracked the report as an ARTIFACT; a rerun must create
        # the MODEL report and un-pin the stale artifact entry in one commit
        client = FakeLineageClient(tracked=[("rev-old-report", "ARTIFACT")])
        rc.record_report_lineage(client, None, self._models(), tmp_path / "r.csv",
                                 branch=SimpleNamespace(tag="baseline"))
        assert writer.updates == []  # cannot revise an artifact as a model
        assert [name for name, _ in writer.adds] == ["r.csv"]
        assert client.commits == [(("rev-art-1",), ("rev-old-report", "rev-art-1"))]

    def test_tracking_failure_keeps_local_copy(self, tmp_path, capsys, writer):
        client = FakeLineageClient()

        def boom(branch, add=None, remove=None):
            raise RuntimeError("commit rejected")
        client.systems.branches.commit = boom
        report = tmp_path / "r.csv"
        report.write_text("vendor\n")
        rc.record_report_lineage(client, None, self._models(), report,
                                 branch=SimpleNamespace(tag="baseline"),
                                 remove_local=True)
        assert report.exists()  # fallback: rides the job's working-dir output
        assert "could not record report lineage" in capsys.readouterr().err


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
