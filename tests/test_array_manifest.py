"""Scientific identities never follow row order, mutable files, or another cluster."""
import copy
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from raven import array_manifest as M, submission
from raven.research_commands import array_plan
from raven.remote import LocalFiles
from raven.model import Finished


def document(**updates):
    value = {"schema": M.SCHEMA, "array_id": "52", "cluster": "", "entries": [
        {"index": 19, "id": "sample-B", "label": "B / seed 9", "parameters": {"seed": 9}, "inputs": ["missing-input-B"], "outputs": ["missing-output-B"]},
        {"index": 2, "id": "sample-A", "parameters": {"seed": 7, "rate": 0.001}}]}
    value.update(updates)
    return value


def parse(value=None):
    return M.parse(json.dumps(value or document()))


def test_sparse_exact_identity_never_matches_launch_order_steps_or_another_cluster():
    manifest = parse(document(cluster="alpha"))
    assert [entry.index for entry in manifest.entries] == [2, 19]
    assert manifest.lookup("52_19", "alpha").id == "sample-B"
    for jid, cluster in [("52_1", "alpha"), ("53_19", "alpha"), ("52_19", "beta"),
                         ("52_19", ""), ("52_19.batch", "alpha"), ("52_[2-19]", "alpha"), ("52", "alpha")]:
        assert manifest.lookup(jid, cluster) is None
    assert manifest.search("SEED")[0].index == 2
    assert manifest.search("sample-b")[0].index == 19
    assert manifest.search("absent") == ()


def test_manifest_immutable_revision_ignores_entry_order_and_whitespace():
    source = document()
    manifest = parse(source)
    source["entries"].reverse()
    assert parse(source).revision == manifest.revision
    assert M.parse(json.dumps(source, indent=4)).revision == manifest.revision
    source["entries"][0]["parameters"]["seed"] = 99
    assert manifest.by_index[2].parameters["seed"] == 7
    assert parse(source).revision != manifest.revision
    with pytest.raises(TypeError):
        manifest.by_index[2].parameters["seed"] = 99
    with pytest.raises(TypeError):
        manifest.by_index[3] = manifest.entries[0]
    assert copy.deepcopy(manifest) is manifest


@pytest.mark.parametrize("change", [
    {"schema": "raven.array-manifest/v2"}, {"array_id": "052"}, {"array_id": 52},
    {"array_id": "0"}, {"array_id": str(2**63)}, {"array_id": "52_2"},
    {"cluster": "x\n52"}, {"cluster": None}, {"cluster": "x" * 129},
    {"entries": []}, {"entries": {}}, {"unknown": True},
])
def test_root_schema_rejects_ambiguous_or_unbounded_values(change):
    with pytest.raises(ValueError):
        parse(document(**change))


@pytest.mark.parametrize("entry", [
    {"index": True, "id": "a"}, {"index": -1, "id": "a"}, {"index": "2", "id": "a"},
    {"index": 1.0, "id": "a"}, {"index": 2**63, "id": "a"}, {"index": 1},
    {"index": 1, "id": "a\x1b"}, {"index": 1, "id": ""},
    {"index": 1, "id": "a", "params": {}}, {"index": 1, "id": "a", "label": []},
    {"index": 1, "id": "a", "parameters": {"a": {"nested": 1}}},
    {"index": 1, "id": "a", "parameters": {"a": [1]}},
    {"index": 1, "id": "a", "parameters": {"a": float("nan")}},
    {"index": 1, "id": "a", "parameters": {"a": float("inf")}},
    {"index": 1, "id": "a", "parameters": {"a": "x" * 1025}},
    {"index": 1, "id": "a", "inputs": "path"},
    {"index": 1, "id": "a", "inputs": ["x"] * 33},
    {"index": 1, "id": "a", "outputs": ["x\ny"]},
])
def test_entry_validation(entry):
    with pytest.raises(ValueError):
        parse(document(entries=[entry]))


def test_duplicate_ids_indices_and_json_keys_fail_explicitly():
    for entries in [[{"index": 0, "id": "a"}, {"index": 0, "id": "b"}],
                    [{"index": 0, "id": "a"}, {"index": 1, "id": "a"}]]:
        with pytest.raises(ValueError):
            parse(document(entries=entries))
    with pytest.raises(ValueError, match="duplicate JSON key"):
        M.parse(json.dumps(document()).replace('"seed": 9', '"seed": 9, "seed": 8'))


@pytest.mark.parametrize("data", [b"\xff", b"[" * 2000, b"x" * (M.MAX_BYTES + 1), "{}"])
def test_malformed_oversized_or_deep_documents_are_ordinary_errors(data):
    with pytest.raises(ValueError):
        M.parse(data)


def test_maximum_index_and_large_sparse_retry_do_not_enumerate_unseen_tasks():
    manifest = parse(document(entries=[{"index": M.MAX_INDEX, "id": "last"}]))
    metadata = M.retry_metadata(manifest, "/map.json", f"0-{M.MAX_INDEX}")
    assert metadata["unmapped_count"] == M.MAX_INDEX
    assert metadata["entries"][0]["index"] == M.MAX_INDEX


def test_remote_reads_bounded_detect_changes_without_touching_declared_paths():
    class Files:
        remote = True
        def __init__(self):
            self.reads, self.changes, self.calls = [], False, 0
        def snapshot_stat(self, path):
            assert path == "/mapping.json"
            self.calls += 1
            return {"size": len(data), "ident": (1, 2), "updated": 2 if self.changes and self.calls % 2 == 0 else 1}
        def read(self, path, offset, size):
            self.reads.append((path, offset, size))
            return data
    data = json.dumps(document()).encode()
    files = Files()
    result, stamp = M.read("/mapping.json", files)
    assert result.by_index[19].inputs == ("missing-input-B",)
    assert files.reads == [("/mapping.json", 0, len(data) + 1)]
    files.changes = True
    with pytest.raises(ValueError, match="changed during"):
        M.read("/mapping.json", files)


def test_local_read_refuses_fifo_and_directory_without_blocking(tmp_path):
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    for path in (fifo, tmp_path):
        with pytest.raises((OSError, ValueError)):
            M.read(str(path), LocalFiles())


@pytest.fixture
def retry(tmp_path):
    source = tmp_path / "map.json"
    source.write_text(json.dumps(document()))
    script = tmp_path / "job.sh"
    script.write_text("#!/bin/bash\ntrue\n")
    manifest = parse()
    records = [Finished("52_19", "B", "FAILED"), Finished("52_2", "A", "FAILED"), Finished("52_7", "C", "FAILED")]
    args = ["retry", "52", str(script), "--workdir", str(tmp_path), "--indices", "2,7,19", "--limit", "2"]
    plan = array_plan(args, [], records, manifest=(manifest, str(source)), files=LocalFiles())
    return SimpleNamespace(source=source, script=script, manifest=manifest, records=records, args=args, plan=plan, tmp=tmp_path)


class Scheduler:
    b = SimpleNamespace()
    def __init__(self):
        self.calls = []
    def submit(self, argv, workdir):
        self.calls.append((argv, workdir))
        return True, "123456", "123456"


def test_retry_preserves_sparse_indices_scope_mapping_and_passport(retry):
    plan = retry.plan
    assert plan["resources"]["array"] == "2,7,19%2"
    metadata = plan["array_retry"]["manifest"]
    assert metadata["array_id"] == "52" and metadata["revision"] == retry.manifest.revision
    assert [entry["index"] for entry in metadata["entries"]] == [2, 19]
    assert metadata["unmapped_count"] == 1
    result = submission.submit(plan, Scheduler())
    assert result["ok"], result
    assert result["array_retry"]["manifest"] == metadata
    assert result["passport"]["resources"]["raven_array_manifest"]["revision"] == metadata["revision"]


def test_changed_source_blocks_preparation_and_submission(retry):
    retry.source.write_text(json.dumps(document(entries=[{"index": 2, "id": "CHANGED"}])))
    with pytest.raises(ValueError, match="changed"):
        array_plan(retry.args, [], retry.records, manifest=(retry.manifest, str(retry.source)), files=LocalFiles())
    scheduler = Scheduler()
    result = submission.submit(retry.plan, scheduler)
    assert not result["ok"] and not result["submitted"] and not scheduler.calls
    assert "changed" in result["error"]


def test_missing_source_after_review_never_submits(retry):
    retry.source.unlink()
    scheduler = Scheduler()
    assert not submission.submit(retry.plan, scheduler)["submitted"]
    assert not scheduler.calls


@pytest.mark.parametrize("redigest", [False, True])
def test_metadata_tampering_is_detected_even_with_recomputed_digest(retry, redigest):
    retry.plan["array_retry"]["manifest"]["entries"][0]["id"] = "tampered"
    if redigest:
        retry.plan["plan_id"] = submission._digest(retry.plan)
    scheduler = Scheduler()
    result = submission.submit(retry.plan, scheduler)
    assert not result["submitted"] and not scheduler.calls


def test_changed_during_passport_capture_is_rechecked(retry, monkeypatch):
    from raven import provenance
    original = provenance.capture
    def capture(*args, **kwargs):
        result = original(*args, **kwargs)
        retry.source.write_text(json.dumps(document(entries=[{"index": 2, "id": "later"}])))
        return result
    monkeypatch.setattr(provenance, "capture", capture)
    scheduler = Scheduler()
    result = submission.submit(retry.plan, scheduler)
    assert not result["submitted"] and not scheduler.calls


def test_unrelated_cluster_map_does_not_modify_retry(retry):
    other = parse(document(cluster="different"))
    plan = array_plan(retry.args, [], retry.records, manifest=(other, "/must-not-read"), files=LocalFiles())
    assert "manifest" not in plan["array_retry"]


def test_mapping_plan_save_load_roundtrip(retry):
    path = retry.tmp / "review.json"
    submission.save(retry.plan, path)
    restored = submission.load(path)
    assert restored["array_retry"] == retry.plan["array_retry"]
    assert submission.submit(restored, Scheduler())["ok"]


def test_preflight_resource_edits_preserve_reviewed_scientific_mapping(retry):
    from raven.execution_ui import _form_plan
    state = {"base": retry.plan, "values": {"script": str(retry.script), "workdir": str(retry.tmp), "cpus": "4"}}
    edited = _form_plan(state)
    assert edited["resources"]["cpus_per_task"] == "4"
    assert edited["array_retry"] == retry.plan["array_retry"]
    assert edited["array_retry"] is not retry.plan["array_retry"]
    assert submission.submit(edited, Scheduler())["ok"]


def test_changed_known_submission_blocks_retry_mapping(retry):
    for record in retry.records:
        record.submit = "2026-10-08T12:00:00"
    with pytest.raises(ValueError, match="submission identity"):
        array_plan(retry.args, [], retry.records,
                   manifest=(retry.manifest, str(retry.source), ("2026-10-01T12:00:00",)), files=LocalFiles())


def test_example_and_schema_parse():
    root = Path(__file__).parents[1]
    parsed = M.parse((root / "examples/array-manifest.json").read_bytes())
    assert parsed.by_index[19].id == "sample-B-seed-9"
    schema = json.loads((root / "docs/schemas/array-manifest.schema.json").read_text())
    assert schema["properties"]["schema"]["const"] == M.SCHEMA

