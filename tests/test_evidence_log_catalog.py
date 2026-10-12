"""Selected-job multi-log investigations retain source identity and coverage."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from raven.investigate import investigate, MAX_TOTAL_LOG_BYTES
from raven.model import Finished
from raven.remote import LocalFiles
from raven.research import ResearchHub


def case(hub, path, *, manifest="", jid="77", state="FAILED"):
    job = Finished(id=jid, name="training", state=state)
    return {"view": "evidence", "jid": jid, "job": job,
            "snap": {"details": {jid: {"StdOut": str(path), "WorkDir": str(Path(path).parent)}}},
            "settings": dict(hub.settings), "generation": hub.generation,
            "log_settings": {"manifest_file": manifest}}


def test_manifest_rank_logs_contribute_cited_evidence_with_exact_original_lines(tmp_path):
    stdout = tmp_path / "slurm-77.out"
    stdout.write_bytes(b"started\n")
    rank_dir = tmp_path / "worker-rank-2"
    rank_dir.mkdir()
    rank = rank_dir / "stderr.log"
    rank.write_bytes(b"rank 2 initialized\r\nCUDA out of memory\r\n")
    other = tmp_path / "slurm-777.err"
    other.write_bytes(b"disk quota exceeded\n")
    manifest = tmp_path / "logs.json"
    manifest.write_text(json.dumps({"schema": "raven.logs/v1", "job_id": "77", "logs": [
        {"id": "rank2", "path": "worker-rank-2/stderr.log", "label": "Rank 2", "group": "Workers"}]}))
    hub = ResearchHub({})
    try:
        result = hub.request(case(hub, stdout, manifest=str(manifest)), wait=True)
        assert any(item["key"] == "gpu_oom" for item in result["hypotheses"])
        assert not any(item["key"] == "quota" for item in result["hypotheses"])
        citation = next(item for item in result["evidence"] if item.get("path") == str(rank))
        assert citation["line"] == 2 and citation["line_basis"] == "original"
        assert citation["file_identity"]["size"] == rank.stat().st_size
        assert citation["tail_distance"] == 0
        assert result["coverage"]["inspected_files"] == 2
        assert result["coverage"]["omitted_files"] == 0
        assert any(item["group"] == "Workers" for item in result["log_sources"])
        json.dumps(result)
    finally:
        hub.close()


def test_job_scoped_directory_logs_use_same_catalog_as_browser(tmp_path):
    out = tmp_path / "slurm-77.out"
    out.write_bytes(b"started\n")
    failed_worker = tmp_path / "worker-77-rank2.err"
    failed_worker.write_bytes(b"NCCL unhandled error\n")
    unrelated = tmp_path / "worker-777-rank2.err"
    unrelated.write_bytes(b"disk quota exceeded\n")
    hub = ResearchHub({})
    try:
        result = hub.request(case(hub, out), wait=True)
        assert any(item["key"] == "communication" for item in result["hypotheses"])
        paths = {item["path"] for item in result["log_sources"]}
        assert str(failed_worker) in paths and str(unrelated) not in paths
    finally:
        hub.close()


def test_wrong_job_manifest_is_rejected_atomically(tmp_path):
    out = tmp_path / "slurm-77.out"
    out.write_bytes(b"progress\n")
    wrong = tmp_path / "not-scanned"
    wrong.mkdir()
    wrong_log = wrong / "99.err"
    wrong_log.write_bytes(b"disk quota exceeded\n")
    manifest = tmp_path / "logs.json"
    manifest.write_text(json.dumps({"schema": "raven.logs/v1", "job_id": "99", "logs": [{"id": "bad", "path": "not-scanned/99.err"}]}))
    hub = ResearchHub({})
    try:
        result = hub.request(case(hub, out, manifest=str(manifest)), wait=True)
        assert not any(item["key"] == "quota" for item in result["hypotheses"])
        assert any("does not match" in item for item in result["limitations"])
        assert result["coverage"]["catalog_files"] == 1
    finally:
        hub.close()


def test_combined_byte_budget_is_exact_and_omissions_are_visible(tmp_path):
    out = tmp_path / "out.log"
    out.write_bytes(b"info\n" * 30000)
    manifest = tmp_path / "logs.json"
    entries = []
    for index in range(40):
        path = tmp_path / f"rank{index}.log"
        path.write_bytes(b"debug\n" * 30000)
        entries.append({"id": f"rank{index}", "path": path.name, "group": "Workers"})
    manifest.write_text(json.dumps({"schema": "raven.logs/v1", "job_id": "77", "logs": entries}))
    hub = ResearchHub({})
    try:
        result = hub.request(case(hub, out, manifest=str(manifest)), wait=True)
        coverage = result["coverage"]
        assert coverage["catalog_files"] == 41
        assert 0 < coverage["bytes_examined"] <= MAX_TOTAL_LOG_BYTES
        assert coverage["inspected_files"] <= 32
        assert coverage["omitted_files"] == coverage["catalog_files"] - coverage["inspected_files"]
        assert any("omitted by" in item for item in result["limitations"])
        assert sum(item["bytes_examined"] for item in result["log_sources"]) <= MAX_TOTAL_LOG_BYTES
    finally:
        hub.close()


def test_unreadable_manifest_log_is_counted_unavailable_not_healthy(tmp_path):
    out = tmp_path / "out.log"
    out.write_bytes(b"progress\n")
    manifest = tmp_path / "logs.json"
    manifest.write_text(json.dumps({"schema": "raven.logs/v1", "job_id": "77", "logs": [{"id": "missing", "path": "missing.log"}]}))
    hub = ResearchHub({})
    try:
        result = hub.request(case(hub, out, manifest=str(manifest)), wait=True)
        assert result["coverage"]["unavailable_files"] == 1
        assert result["coverage"]["inspected_files"] == 1
        assert result["coverage"]["omitted_files"] == 0
        assert any("missing" in item for item in result["limitations"])
    finally:
        hub.close()


def test_remote_registered_sources_never_fall_back_to_local_files(tmp_path):
    out = tmp_path / "remote-77.out"
    out.write_bytes(b"disk quota exceeded\n")
    class Remote:
        remote = True
        def stat(self, path):
            raise OSError("SSH unavailable")
        def read(self, path, offset, length):
            pytest.fail("No data read after metadata failed")
        def snapshot_stat(self, path):
            raise OSError("SSH unavailable")
        def tail(self, path, limit):
            raise OSError("SSH unavailable")
        def listdir(self, path):
            raise OSError("SSH unavailable")
    hub = ResearchHub({}, Remote())
    try:
        result = hub.request(case(hub, out), wait=True)
        assert not any(item["key"] == "quota" for item in result["hypotheses"])
        assert result["coverage"]["unavailable_files"] == 1
        assert any("SSH unavailable" in item for item in result["limitations"])
    finally:
        hub.close()


def test_changed_registered_file_does_not_contribute_stale_errors(tmp_path):
    out = tmp_path / "77.out"
    out.write_bytes(b"CUDA out of memory\n")
    class Changing(LocalFiles):
        def tail(self, path, limit):
            raw, size = super().tail(path, limit)
            Path(path).write_bytes(b"successful replacement\n")
            return raw, size
    hub = ResearchHub({}, Changing())
    try:
        result = hub.request(case(hub, out), wait=True)
        assert not any(item["key"] == "gpu_oom" for item in result["hypotheses"])
        assert result["coverage"]["unavailable_files"] == 1
        assert any("changed during inspection" in item for item in result["limitations"])
    finally:
        hub.close()


def test_large_tail_drops_incomplete_prefix_and_cites_relative_line(tmp_path):
    out = tmp_path / "77.out"
    out.write_bytes(b"prefix" * 30000 + b"\nCUDA out of memory\nlast line\n")
    hub = ResearchHub({})
    try:
        result = hub.request(case(hub, out), wait=True)
        citation = next(item for item in result["evidence"] if item.get("path"))
        assert citation["line_basis"] == "tail-relative"
        assert citation["line"] == 1 and citation["tail_distance"] == 1
        assert "tail-relative line" in citation["location"]
        assert result["coverage"]["bytes_examined"] <= 131072
    finally:
        hub.close()


def test_project_context_without_scheduler_id_never_borrows_unrelated_job(monkeypatch):
    from raven import project_ui
    monkeypatch.setattr(project_ui, "log_entries", lambda app: [])
    hub = ResearchHub({})
    app = SimpleNamespace(selected_id="99", research_job_id=None, research_view="evidence", cfg={},
                          project_state={"binding": {"run_id": "run-a", "run_root": "/tmp/run-a", "job_id": None}})
    try:
        context = hub.context({"jobs": [Finished(id="99", state="RUNNING")], "finished": []}, app)
        assert context["jid"] is None and context["job"] is None
        assert context["run_id"] == "run-a" and context["run_root"] == "/tmp/run-a"
        assert hub.request(context, wait=True)["status"] == "empty"
    finally:
        hub.close()


def test_bound_project_uses_confined_cached_entries_without_reopening_manifest(monkeypatch, tmp_path):
    from raven import project_ui
    from raven import log_catalog
    out = tmp_path / "77.out"
    out.write_bytes(b"progress\n")
    allowed = tmp_path / "worker.log"
    allowed.write_bytes(b"CUDA out of memory\n")
    external = tmp_path / "external.log"
    external.write_bytes(b"disk quota exceeded\n")
    entries = [{"id": "worker", "path": str(allowed), "label": "Worker", "group": "Application"}]
    monkeypatch.setattr(project_ui, "log_entries", lambda app: entries)
    monkeypatch.setattr(log_catalog, "build_catalog", lambda *a, **kw: pytest.fail("Bound project must retain validated cached inventory"))
    hub = ResearchHub({})
    app = SimpleNamespace(selected_id="77", research_job_id="77", research_view="evidence", cfg={},
                          project_state={"binding": {"run_id": "run-a", "run_root": str(tmp_path), "job_id": "77", "stdout": str(out), "log_manifest": str(external)},
                                         "run_warnings": ["External log declaration was blocked"]})
    try:
        job = Finished(id="77", state="FAILED")
        selected = hub.context({"jobs": [], "finished": [job], "details": {"77": {"StdOut": str(out)}}}, app)
        result = hub.request(selected, wait=True)
        assert any(item["key"] == "gpu_oom" for item in result["hypotheses"])
        assert not any(item["key"] == "quota" for item in result["hypotheses"])
        assert "External log declaration was blocked" in result["limitations"]
        assert result["coverage"]["catalog_files"] == 1
    finally:
        hub.close()


def test_additional_excerpt_input_is_bounded_even_for_infinite_generator():
    def logs():
        while True:
            yield {"text": b"CUDA out of memory\n", "path": "/tmp/worker", "first_line": 1}
    result = investigate(Finished(id="77", state="FAILED"), logs=logs())
    assert len(result["evidence"]) <= 80
    assert len(result["log_sources"]) <= 34
    assert any("source limit" in item for item in result["limitations"])


def test_excerpt_metadata_remains_json_safe_and_preserves_exact_long_paths():
    path = "/tmp/" + "a/" * 300 + "worker.log"
    result = investigate(Finished(id="77", state="FAILED"), logs=[
        {"text": b"CUDA out of memory\n", "path": path, "first_line": 1,
         "file_identity": {"ident": (1, 2), "size": 19, "private": object(), "updated": object()}}])
    citation = next(item for item in result["evidence"] if item.get("path"))
    assert citation["path"] == path
    assert citation["file_identity"] == {"ident": [1, 2], "size": 19}
    json.dumps(result)


@pytest.mark.parametrize("prefix,has_time", [("2026-10-05T12:34:56Z ", True), ("[2026-10-05T12:34:56.250+00:00] ", True),
                                           ("2026-10-05 12:34:56 ", False), ("epoch 123 ", False)])
def test_log_timeline_only_uses_explicit_timezone_timestamps(prefix, has_time):
    result = investigate(Finished(id="77", state="FAILED"), logs=[
        {"text": prefix + "CUDA out of memory\n", "path": "/tmp/worker.log", "first_line": 1}])
    citation = next(item for item in result["evidence"] if item.get("path"))
    assert ("t" in citation) == has_time
    assert any(item["evidence_id"] == citation["id"] for item in result["chronology"]) == has_time


@pytest.mark.parametrize("logs", ["log", b"log", {}, None, ["not a mapping"]])
def test_malformed_additional_excerpt_interface_is_rejected(logs):
    with pytest.raises(TypeError):
        investigate(Finished(id="77", state="FAILED"), logs=logs)
