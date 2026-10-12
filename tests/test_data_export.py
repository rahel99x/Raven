"""Retained data correctness, filesystem confinement and snapshot behavior."""
import csv
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import threading

import pytest

from raven import data_export as E, reporting
from raven.data_export_cli import main


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project with λ"
    root.mkdir()
    (root / "job.sh").write_text("#!/bin/bash\ntrue\n")
    run = reporting.begin_run(root, "attempt-1", name="encoder-seed 3 / α", script="job.sh", job_id="42_7", attempt=2)
    return root, run


def request(project, **kwargs):
    return dict(project=str(project[0]), runs=[project[1].name], include=["application"], **kwargs)


def emit(run, rows):
    with (run / "metrics.jsonl").open("w", encoding="utf8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")


def output(receipt):
    assert receipt["status"] in {"complete", "partial"}, receipt
    return Path(receipt["outputs"][0])


def rows(path):
    with path.open(encoding="utf8", newline="") as stream:
        return list(csv.DictReader(stream))


def verify_bundle(path):
    manifest = json.loads((path / "manifest.json").read_bytes())
    for entry in manifest["files"]:
        raw = (path / entry["path"]).read_bytes()
        assert len(raw) == entry["bytes"]
        assert hashlib.sha256(raw).hexdigest() == entry["sha256"]
    marker = json.loads((path / "COMPLETE.json").read_bytes())
    assert marker["manifest_sha256"] == hashlib.sha256((path / "manifest.json").read_bytes()).hexdigest()
    return manifest


def test_original_measurements_unicode_missing_units_precision(project):
    emit(project[1], [{"t": 1720000000.125, "metrics": {"loss,α": 0.123456789012345, "missing": None}, "phase": "line1\nline2", "step": 4}])
    result = E.export_data(request(project))
    assert result["exit_code"] == 0
    path = output(result)
    data = rows(path / "application-metrics.csv")
    assert data[0]["value"] == "0.123456789012345"
    assert data[0]["metric"] == "loss,α"
    assert data[0]["phase"] == "line1\nline2"
    assert data[0]["unit"] == "unspecified"
    assert data[1]["value"] == ""
    assert data[1]["status"] == "unavailable"
    assert data[0]["job_id"] == "42_7" and data[0]["attempt"] == "2"
    manifest = verify_bundle(path)
    assert manifest["transformations"] == []
    assert manifest["attempts"][0]["active_at_capture"]


def test_check_and_discovery_create_nothing_and_never_call_scheduler(project, monkeypatch, capsys):
    emit(project[1], [{"t": 1, "metrics": {"loss": 1}}])
    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("No subprocess during export"))
    before = set(project[0].rglob("*"))
    assert main(["--project", str(project[0]), "--run", project[1].name, "--include", "application", "--relative-dir", "new/sub", "--check", "--json"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["schema"] == "raven.export-plan/v1"
    assert set(project[0].rglob("*")) == before


def test_time_selection_and_separate_metrics(project):
    emit(project[1], [{"t": t, "metrics": {"a/b": t, "a b": t * 2, "omit": 3}} for t in range(5)])
    path = output(E.export_data(request(project, metrics=["a/b", "a b"], start=1, end=3, layout="metric")))
    files = list((path / "application").glob("*.csv"))
    assert len(files) == 2 and len({x.name for x in files}) == 2
    assert all(len(rows(file)) == 3 for file in files)
    assert {row["metric"] for file in files for row in rows(file)} == {"a/b", "a b"}
    verify_bundle(path)


def test_append_after_preview_is_excluded(project):
    emit(project[1], [{"t": 1, "metrics": {"x": 1}}])
    plan = E.plan_export(request(project))
    with (project[1] / "metrics.jsonl").open("a") as stream:
        stream.write('{"t":2,"metrics":{"x":2}}\n')
    result = E.export_data(plan)
    assert result["status"] == "complete"
    assert [row["value"] for row in rows(output(result) / "application-metrics.csv")] == ["1"]


@pytest.mark.parametrize("mutation", ["replace", "truncate", "rewrite"])
def test_source_changes_are_partial_without_empty_success_file(project, mutation):
    emit(project[1], [{"t": 1, "metrics": {"x": 1}}])
    plan = E.plan_export(request(project))
    source = project[1] / "metrics.jsonl"
    if mutation == "replace":
        source.rename(source.with_suffix(".old"))
        source.write_text('{"t":1,"metrics":{"x":5}}\n')
    elif mutation == "truncate":
        source.write_text("")
    else:
        source.write_text(source.read_text().replace('1', '2'))
    result = E.export_data(plan)
    assert result["status"] == "partial"
    assert not (output(result) / "application-metrics.csv").exists()
    assert any(issue["code"] == "source-changed" for issue in result["issues"])


def test_malformed_oversized_and_incomplete_tail_are_counted(project):
    (project[1] / "metrics.jsonl").write_bytes(b'{"t":1,"metrics":{"x":1}}\ninvalid\n' + b'x' * (E.MAX_LINE + 2) + b'\n{"t":2')
    result = E.export_data(request(project))
    assert result["status"] == "partial"
    path = output(result)
    assert len(rows(path / "application-metrics.csv")) == 1
    codes = {issue["code"] for issue in result["issues"]}
    assert {"malformed", "oversized", "incomplete_tail"} <= codes
    counts = verify_bundle(path)["sources"][0]["counts"]
    assert counts["malformed"] == counts["oversized"] == counts["incomplete_tail"] == 1


@pytest.mark.parametrize("relative", ["../outside", "a/../../b", "/absolute", "new/\x00bad"])
def test_relative_escape_rejected_before_writes(project, relative):
    with pytest.raises(E.ExportError):
        E.plan_export(request(project, destination={"mode": "relative", "path": relative}))
    assert not (project[0] / "exports").exists()


def test_symlink_destination_and_source_are_refused(project, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (project[0] / "linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(E.ExportError):
        E.plan_export(request(project, destination={"mode": "relative", "path": "linked/data"}))
    source = project[1] / "metrics.jsonl"
    source.unlink()
    (outside / "secret").write_text('{"metrics":{"secret":1}}\n')
    source.symlink_to(outside / "secret")
    plan = E.plan_export(request(project))
    assert not plan["valid"]
    assert not list(outside.glob("data*"))


def test_symlink_race_between_preview_and_export_is_refused(project, tmp_path):
    emit(project[1], [{"t": 1, "metrics": {"x": 1}}])
    plan = E.plan_export(request(project, destination={"mode": "relative", "path": "later/data"}))
    outside = tmp_path / "outside"
    outside.mkdir()
    (project[0] / "later").symlink_to(outside, target_is_directory=True)
    result = E.export_data(plan)
    assert result["status"] == "failed"
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("destination", [{"mode": "absolute", "path": "relative"}, {"mode": "project", "host": "other.remote.example"}])
def test_explicit_destination_errors_never_fallback(project, destination):
    with pytest.raises(E.ExportError):
        E.plan_export(request(project, destination=destination))
    assert not (project[0] / "exports").exists()


def test_remote_source_path_never_reads_same_spelling_local_file(project):
    with pytest.raises(E.ExportError, match="no local fallback"):
        E.discover({"attempts": [{"project": str(project[0]), "run_id": project[1].name, "host": "other.remote.example"}]})


def test_concurrent_exports_never_overwrite_and_recover_frozen_plan(project):
    emit(project[1], [{"t": 1, "metrics": {"x": 1}}])
    with ThreadPoolExecutor(max_workers=4) as pool:
        receipts = list(pool.map(lambda _: E.export_data(request(project)), range(8)))
    paths = {output(receipt) for receipt in receipts}
    assert len(paths) == 8
    plan = E.plan_export(request(project))
    first = E.export_data(plan)
    second = E.export_data(plan)
    assert first == second == E.validate_existing(plan)
    for path in paths:
        verify_bundle(path)


def test_foreign_collision_refused_and_owned_validation_detects_tampering(project):
    emit(project[1], [{"t": 1, "metrics": {"x": 1}}])
    plan = E.plan_export(request(project))
    target = Path(plan["destinations"][0]["path"])
    target.mkdir(parents=True)
    (target / "keep").write_text("mine")
    receipt = E.export_data(plan)
    assert receipt["status"] == "failed" and (target / "keep").read_text() == "mine"
    plan = E.plan_export(request(project))
    result = E.export_data(plan)
    (output(result) / "application-metrics.csv").write_text("tampered")
    with pytest.raises(E.ExportError, match="differs"):
        E.validate_existing(plan)


def test_cancellation_cleans_only_owned_stage(project):
    emit(project[1], [{"t": t, "metrics": {"x": t}} for t in range(500)])
    cancel = threading.Event()
    plan = E.plan_export(request(project))
    parent = Path(plan["destinations"][0]["path"]).parent
    parent.mkdir(parents=True)
    other = parent / ".raven-stage-other"
    other.mkdir()
    (other / "keep").write_text("mine")
    result = E.export_data(plan, cancel=cancel, progress=lambda _: cancel.set())
    assert result["status"] == "cancelled" and result["exit_code"] == 130
    assert not Path(plan["destinations"][0]["path"]).exists()
    assert list(parent.iterdir()) == [other]
    assert (other / "keep").read_text() == "mine"


def test_failed_publication_preserves_source_and_existing_parent(project, monkeypatch):
    emit(project[1], [{"t": 1, "metrics": {"x": 1}}])
    source = (project[1] / "metrics.jsonl").read_bytes()
    monkeypatch.setattr(E, "_publish", lambda *args: (_ for _ in ()).throw(OSError(28, "No space left on device")))
    result = E.export_data(request(project))
    assert result["status"] == "failed" and result["exit_code"] == 4
    assert (project[1] / "metrics.jsonl").read_bytes() == source
    assert not list((project[0] / "exports").rglob(".raven-stage-*"))


def test_multirun_and_multiproject_default_and_explicit_combined(project, tmp_path):
    emit(project[1], [{"t": 1, "metrics": {"x": 1}}])
    second_root = tmp_path / "second"
    second_root.mkdir()
    (second_root / "job.sh").write_text("true")
    second = reporting.begin_run(second_root, "attempt-1", name="same", script="job.sh")
    emit(second, [{"t": 1, "metrics": {"x": 9}}])
    selection = {"attempts": [{"project": str(root), "run_id": run.name} for root, run in (project, (second_root, second))], "include": ["application"]}
    result = E.export_data(selection)
    assert result["status"] == "complete" and len(result["outputs"]) == 2
    combined = E.export_data(dict(selection, destination={"mode": "absolute", "path": str(tmp_path / "combined"), "combined": True}))
    path = output(combined)
    all_rows = [row for file in path.rglob("application-metrics.csv") for row in rows(file)]
    assert len(all_rows) == 2 and len({row["project"] for row in all_rows}) == 2
    assert len(list((path / "attempts").iterdir())) == 2
    verify_bundle(path)


def test_declared_logs_artifacts_only_and_all_excludes_large_files(project):
    (project[1] / "outputs" / "result.bin").write_bytes(b"\x00\xffscience")
    (project[1] / "outputs" / "undeclared.bin").write_bytes(b"private")
    contract = project[1] / "contract.json"
    contract.write_text(json.dumps({"version": 1, "outputs": [{"path": "outputs/result.bin", "required": True}]}))
    manifest = json.loads((project[1] / "run.json").read_text())
    manifest["paths"]["contract"] = "contract.json"
    (project[1] / "run.json").write_text(json.dumps(manifest))
    plan = E.plan_export(dict(request(project), include=["all"]))
    assert "logs" not in plan["request"]["include"] and "artifacts" not in plan["request"]["include"]
    result = E.export_data(dict(request(project), include=["logs", "artifacts"]))
    path = output(result)
    manifest = verify_bundle(path)
    exported = [(path / entry["path"]).read_bytes() for entry in manifest["files"] if entry.get("family") == "artifacts"]
    assert exported == [b"\x00\xffscience"]
    assert not any(b"private" == raw for raw in exported)


def test_telemetry_device_clock_scope_selection(project):
    telemetry = project[1] / "telemetry"
    telemetry.mkdir()
    source = telemetry / "gpu.jsonl"
    with source.open("w") as stream:
        for device in ("GPU-A", "GPU-B"):
            stream.write(json.dumps({"schema": "raven.gpu-sample/v1", "timestamp": 1.125, "timestamp_utc": "1970-01-01T00:00:01.125000Z", "source_time": "1.125", "clock": "UTC", "device_uuid": device, "node": "node1", "utilization_percent": 0, "memory_used_mib": None, "scope": "shared-device", "status": "partial"}) + "\n")
    (telemetry / "index.json").write_text(json.dumps({"schema": "raven.telemetry-index/v1", "sources": [{"family": "gpu", "path": "telemetry/gpu.jsonl", "schema": "raven.gpu-sample/v1", "status": "partial", "reason": "one node of two", "coverage": {"expected_nodes": 2, "observed_nodes": 1}}]}))
    result = E.export_data(dict(request(project, devices=["GPU-B"]), include=["gpu"]))
    data = rows(output(result) / "gpu.csv")
    assert len(data) == 1 and data[0]["device_uuid"] == "GPU-B"
    assert data[0]["utilization_percent"] == "0" and data[0]["memory_used_mib"] == ""
    assert data[0]["source_time"] == "1.125" and data[0]["scope"] == "shared-device"
    assert result["status"] == "partial"


def test_lru_limits_open_files_with_many_metric_names(project, monkeypatch):
    emit(project[1], [{"t": t, "metrics": {f"m-{i}": i for i in range(100)}} for t in range(2)])
    observed = []
    original = E._Writer._handle
    def handle(self, name):
        result = original(self, name)
        observed.append(len(self.open))
        return result
    monkeypatch.setattr(E._Writer, "_handle", handle)
    result = E.export_data(request(project, layout="metric"))
    assert result["status"] == "complete"
    assert max(observed) <= E.MAX_OPEN_FILES
    assert len(list((output(result) / "application").iterdir())) == 100
    verify_bundle(output(result))


def test_unknown_time_not_guessed_for_range(project):
    emit(project[1], [{"source_time": "2026-10-12 12:00:00", "metrics": {"x": 1}}])
    result = E.export_data(request(project, start=0, end=10))
    assert result["status"] == "partial"
    assert any(issue["code"] == "unknown_time" for issue in result["issues"])


def test_missing_selected_metric_is_machine_readable_partial(project, capsys):
    emit(project[1], [{"t": 1, "metrics": {"loss": 1}}])
    assert main(["--project", str(project[0]), "--run", project[1].name, "--include", "application", "--metric", "other", "--json"]) == 3
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["status"] == "partial" and any(issue["code"] == "unavailable-metric" for issue in receipt["issues"])


def test_cli_invalid_has_no_stdout(project, capsys):
    assert main(["--project", str(project[0]), "--run", "../bad", "--json"]) == 2
    captured = capsys.readouterr()
    assert not captured.out and "exact safe" in captured.err


def test_identity_replacement_with_same_job_and_new_script_or_token_is_rejected(project):
    emit(project[1], [{"t": 1, "metrics": {"x": 1}}])
    plan = E.plan_export(request(project))
    manifest = json.loads((project[1] / "run.json").read_text())
    manifest["provenance"]["script_sha256"] = "a" * 64
    (project[1] / "run.json").write_text(json.dumps(manifest))
    result = E.export_data(plan)
    assert result["status"] == "failed" and "identity changed" in result["issues"][0]["message"]


def test_array_mapping_submit_and_scheduler_outcome_are_separate(project):
    manifest = json.loads((project[1] / "run.json").read_text())
    manifest.update(submit=100.25, metadata={"raven_launch": {"cluster": "lab", "array_id": "42", "array_index": 7, "submission_token": "abc"}})
    (project[1] / "run.json").write_text(json.dumps(manifest))
    evidence = {"schema": "raven.scheduler-outcome/v1", "state": "CANCELLED", "job_id": "42_7", "cluster": "lab", "attempt": 2, "submit_time": 100., "observed_at": 200}
    (project[1] / "scheduler-outcome.json").write_text(json.dumps(evidence))
    plan = E.plan_export(dict(request(project), include=["summary"]))
    attempt = plan["attempts"][0]
    assert attempt["scheduler_state"] == "CANCELLED" and attempt["application_state"] == "RUNNING"
    assert attempt["identity"]["array_parent"] == "42" and attempt["identity"]["array_task"] == 7
    assert (output(E.export_data(plan)) / "scheduler-outcome.json").exists()
    evidence["attempt"] = 1
    (project[1] / "scheduler-outcome.json").write_text(json.dumps(evidence))
    with pytest.raises(E.ExportError, match="exact attempt"):
        E.plan_export(dict(request(project), include=["summary"]))


def test_native_trace_keeps_sibling_paths_and_omits_incomplete_tail(project):
    trace = project[1] / ".raven" / "traces" / "stream-a"
    trace.mkdir(parents=True)
    (trace / "manifest.json").write_text(json.dumps({"schema": "raven.trace/v1", "identity": {"run_id": project[1].name, "job_id": "42_7", "attempt": project[1].name}, "journal": "events.jsonl", "committed_bytes": len(b'{"kind":"valid"}\n'), "committed_seq": 1, "state": "CLOSED", "coverage": {"scope": "explicit"}}))
    (trace / "events.jsonl").write_bytes(b'{"kind":"valid"}\n{"kind":')
    (project[1] / "trace").mkdir()
    (project[1] / "trace" / "index.json").write_text(json.dumps({"schema": "raven.trace-index/v1", "sources": [{"id": name, "path": ".raven/traces/stream-a/" + name} for name in ("manifest.json", "events.jsonl")]}))
    result = E.export_data(dict(request(project), include=["trace"]))
    assert result["status"] == "partial"
    output_trace = output(result) / "trace" / ".raven" / "traces" / "stream-a"
    assert (output_trace / "events.jsonl").read_bytes() == b'{"kind":"valid"}\n'
    assert json.loads((output_trace / "manifest.json").read_text())["journal"] == "events.jsonl"


def test_bounded_metric_hints_and_explicit_source_selection(project):
    emit(project[1], [{"t": 1, "metrics": {"loss": 1}}])
    discovery = E.discover(request(project))
    assert discovery["availability"]["application"]["metrics"] == ["loss"]
    assert discovery["availability"]["application"]["metrics_complete"] is False
    (project[1] / "logs" / "stdout.log").write_text("stdout")
    (project[1] / "logs" / "stderr.log").write_text("stderr")
    result = E.export_data(dict(request(project, sources=["logs:application.stderr"]), include=["logs"]))
    exported = [entry for entry in verify_bundle(output(result))["files"] if entry.get("family") == "logs"]
    assert len(exported) == 1 and (output(result) / exported[0]["path"]).read_text() == "stderr"


def test_explicit_application_binding_and_artifact_declaration(project):
    (project[0] / "producer.jsonl").write_text('{"t":1,"metrics":{"external":3}}\n')
    (project[0] / "result.txt").write_text("declared")
    manifest = json.loads((project[1] / "run.json").read_text())
    manifest["metadata"] = {"automation": {"project_root": str(project[0]), "filesystem_host": E.local_host(), "metric_binding": {"path": str(project[0] / "producer.jsonl")}, "artifact_bindings": [{"path": str(project[0] / "result.txt"), "required": True}]}}
    (project[1] / "run.json").write_text(json.dumps(manifest))
    result = E.export_data(dict(request(project), include=["application", "artifacts"]))
    assert rows(output(result) / "application-metrics.csv")[0]["metric"] == "external"
    assert len(list((output(result) / "artifacts").iterdir())) == 1


def test_readonly_reconcile_does_not_create_project_state(project, capsys):
    before = set(project[0].rglob("*"))
    assert main(["--project", str(project[0]), "--reconcile", "--check", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"schema": "raven.export-reconciliation/v1", "requests": [], "launches": []}
    assert set(project[0].rglob("*")) == before


def test_all_selects_available_families_but_keeps_explicit_unavailable_requests(project):
    emit(project[1], [{"t": 1, "metrics": {"x": 1}}])
    reporting.finish_run(project[1], state="COMPLETED", runtime_seconds=1, exit_code=0)
    request_all = dict(request(project), include=["all"])
    result = E.export_data(request_all)
    assert result["status"] == "complete", result
    manifest = verify_bundle(output(result))
    assert manifest["request"]["include"] == ["application", "summary"]
    assert manifest["request"]["all_available"] is True
    explicit = E.export_data(dict(request_all, include=["all", "gpu"]))
    assert explicit["status"] == "partial"
    assert any(issue.get("family") == "gpu" for issue in explicit["issues"])


def shared_project(project, origin="login.cluster"):
    marker = {"schema": "raven.project-filesystem/v1", "id": "a" * 32, "origin_host": origin}
    (project[0] / ".raven").mkdir(exist_ok=True)
    (project[0] / ".raven" / "filesystem.json").write_text(json.dumps(marker))
    manifest = json.loads((project[1] / "run.json").read_text())
    manifest["metadata"] = {"automation": {"project_root": str(project[0]), "filesystem_id": marker["id"], "filesystem_host": origin, "runtime_host": "node1.cluster"}}
    (project[1] / "run.json").write_text(json.dumps(manifest))
    return marker


def test_verified_shared_anchor_login_compute_login_recovery(project, monkeypatch):
    emit(project[1], [{"t": 1, "metrics": {"loss": 1}}])
    marker = shared_project(project)
    monkeypatch.setattr(E, "local_host", lambda: "login.cluster")
    plan = E.plan_export(request(project))
    assert plan["destinations"][0]["host"] == "login.cluster"
    monkeypatch.setattr(E, "local_host", lambda: "node1.cluster")
    result = E.export_data(plan)
    assert result["status"] == "complete", result
    manifest = verify_bundle(output(result))
    assert manifest["filesystem_host"] == "login.cluster"
    assert manifest["access_host_at_export"] == "node1.cluster"
    assert manifest["attempts"][0]["filesystem_anchor"] == marker
    assert rows(output(result) / "application-metrics.csv")[0]["host"] == "login.cluster"
    monkeypatch.setattr(E, "local_host", lambda: "login.cluster")
    assert E.validate_existing(plan) == result


@pytest.mark.parametrize("change", ["missing", "wrong-id", "wrong-root", "wrong-host"])
def test_shared_anchor_missing_or_mismatched_never_guesses_host(project, monkeypatch, change):
    marker = shared_project(project)
    path = project[0] / ".raven" / "filesystem.json"
    if change == "missing":
        path.unlink()
    elif change in {"wrong-id", "wrong-host"}:
        marker["id" if change == "wrong-id" else "origin_host"] = "b" * 32
        path.write_text(json.dumps(marker))
    else:
        manifest = json.loads((project[1] / "run.json").read_text())
        manifest["metadata"]["automation"]["project_root"] = "/different/path"
        (project[1] / "run.json").write_text(json.dumps(manifest))
    monkeypatch.setattr(E, "local_host", lambda: "node1.cluster")
    with pytest.raises((E.ExportError, FileNotFoundError)):
        E.plan_export(request(project))
    assert not (project[0] / "exports").exists()


def test_shared_anchor_does_not_authorize_arbitrary_remote_absolute_parent(project, monkeypatch, tmp_path):
    shared_project(project)
    monkeypatch.setattr(E, "local_host", lambda: "node1.cluster")
    with pytest.raises(E.ExportError, match="no verified export write adapter"):
        E.plan_export(request(project, destination={"mode": "absolute", "path": str(tmp_path / "scratch"), "host": "login.cluster"}))
    plan = E.plan_export(request(project, destination={"mode": "absolute", "path": str(tmp_path / "scratch"), "host": "node1.cluster"}))
    assert plan["destinations"][0]["host"] == "node1.cluster"


def test_trace_complete_uncommitted_records_are_excluded(project):
    from raven.trace_recorder import Recorder
    recorder = Recorder(project[1], job_id="42_7", attempt=project[1].name,
                        identity_extra={"run_id": project[1].name}, background_flush=False)
    recorder.emit("point", {"name": "observed"})
    recorder.close()
    manifest_path = recorder.path
    event_path = manifest_path.with_name("events.jsonl")
    committed = event_path.read_bytes()
    with event_path.open("ab") as stream:
        stream.write(b'{"looks":"complete-but-not-committed"}\n')
    (project[1] / "trace").mkdir()
    (project[1] / "trace" / "index.json").write_text(json.dumps({"schema": "raven.trace-index/v1", "sources": [{"id": file.name, "path": str(file.relative_to(project[1]))} for file in (manifest_path, event_path)]}))
    result = E.export_data(dict(request(project), include=["trace"]))
    copied = output(result) / "trace" / event_path.relative_to(project[1])
    assert copied.read_bytes() == committed
    assert any(issue["code"] == "uncommitted-trace-tail" for issue in result["issues"])


def test_invalid_structured_family_does_not_discard_other_valid_measurements(project):
    emit(project[1], [{"t": 1, "metrics": {"loss": 1}}])
    (project[1] / "summary.json").write_text("broken JSON")
    result = E.export_data(dict(request(project), include=["application", "summary"]))
    assert result["status"] == "partial"
    assert rows(output(result) / "application-metrics.csv")[0]["value"] == "1"
    assert not (output(result) / "summary.json").exists()


def test_qualified_mig_device_keeps_full_node_provider_and_partition(project):
    telemetry = project[1] / "telemetry"
    telemetry.mkdir()
    descriptors = []
    for index, node in enumerate(("node1", "node2")):
        device = {"node": node, "provider": "nvidia", "device_uuid": "MIG-GPU-a/3/0", "device_index": 0, "partition": "slice/2"}
        (telemetry / f"gpu{index}.jsonl").write_text(json.dumps(dict(device, timestamp=1, utilization_percent=10)) + "\n")
        descriptors.append({"family": "gpu", "path": f"telemetry/gpu{index}.jsonl", "status": "complete", "devices": [device]})
    (telemetry / "index.json").write_text(json.dumps({"schema": "raven.telemetry-index/v1", "sources": descriptors}))
    selected = "node1/nvidia/MIG-GPU-a/3/0/slice/2"
    discovery = E.discover(dict(request(project), include=["gpu"]))
    assert selected in discovery["availability"]["gpu"]["devices"]
    result = E.export_data(dict(request(project, devices=[selected]), include=["gpu"]))
    assert result["status"] == "complete", result
    data = rows(output(result) / "gpu.csv")
    assert len(data) == 1 and data[0]["node"] == "node1" and data[0]["device_uuid"] == "MIG-GPU-a/3/0"
    missing = E.export_data(dict(request(project, devices=["node3/nvidia/MIG-GPU-a/3/0/slice/2"]), include=["gpu"]))
    assert missing["status"] == "partial"
    assert any(issue["code"] == "unavailable-device" for issue in missing["issues"])


def test_final_publication_race_preserves_other_owners_file(project):
    emit(project[1], [{"t": 1, "metrics": {"x": 1}}])
    plan = E.plan_export(request(project))
    target = Path(plan["destinations"][0]["path"])
    def occupy(_):
        target.write_text("another owner")
    result = E.export_data(plan, progress=occupy)
    assert result["status"] == "failed"
    assert target.read_text() == "another owner"
    assert not list(target.parent.glob(".raven-stage-*"))


def test_source_rewritten_during_stream_never_publishes_inconsistent_bundle(project):
    emit(project[1], [{"t": t, "metrics": {"x": t}} for t in range(200)])
    source = project[1] / "metrics.jsonl"
    def rewrite(_):
        data = source.read_bytes()
        source.write_bytes(data.replace(b'"x": 1}', b'"x": 9}'))
    result = E.export_data(request(project), progress=rewrite)
    assert result["status"] == "failed"
    assert result["outputs"] == []
    assert not list((project[0] / "exports").rglob(".raven-stage-*"))


def test_machine_contracts_load_as_independent_json_schema_and_match_columns(project):
    jsonschema = pytest.importorskip("jsonschema")
    emit(project[1], [{"t": 1, "metrics": {"x": 1}}])
    plan = E.plan_export(request(project))
    receipt = E.export_data(plan)
    manifest = verify_bundle(output(receipt))
    schema_dir = Path(__file__).parents[1] / "docs" / "schemas"
    # Independent consumers can resolve the colocated request schema.
    from referencing import Registry, Resource
    resources = [(name.name, Resource.from_contents(json.loads(name.read_text()))) for name in schema_dir.glob("export-*.schema.json")]
    registry = Registry().with_resources(resources)
    for value, filename in ((plan["request"], "export-request.v1.schema.json"), (plan, "export-plan.v1.schema.json"), (receipt, "export-receipt.v1.schema.json"), (manifest, "export-manifest.v1.schema.json")):
        jsonschema.Draft202012Validator(json.loads((schema_dir / filename).read_text()), registry=registry).validate(value)
    columns = {"schema": "raven.export-columns/v1", "application": list(E.APPLICATION_COLUMNS), "gpu": list(E.GPU_COLUMNS), "cpu-memory": list(E.CPU_COLUMNS)}
    jsonschema.validate(columns, json.loads((schema_dir / "export-columns.v1.schema.json").read_text()))


def test_explicit_native_passport_binding_exported_without_directory_guessing(project):
    from raven.provenance import capture, save
    passport = capture(project[0], script="job.sh", parameters={}, resources={}, inputs=[], environment_names=[], job_id="42_7")
    saved = save(passport, project[1] / "passports")
    manifest = json.loads((project[1] / "run.json").read_text())
    manifest["metadata"] = {"automation": {"research_bindings": [{"id": "passport", "path": str(saved.relative_to(project[1])), "schema": passport["schema"]}]}}
    (project[1] / "run.json").write_text(json.dumps(manifest))
    (project[1] / "passports" / "undeclared.json").write_text('{"secret":true}')
    result = E.export_data(dict(request(project), include=["research"]))
    entries = [item for item in verify_bundle(output(result))["files"] if item.get("family") == "research"]
    assert len(entries) == 1
    assert json.loads((output(result) / entries[0]["path"]).read_text()) == passport


def test_per_metric_fsync_scales_with_files_not_observation_count(project, monkeypatch):
    original_sync = os.fsync
    calls = []
    def sync(fd):
        calls.append(os.fstat(fd).st_ino)
        return original_sync(fd)
    monkeypatch.setattr(E.os, "fsync", sync)
    metrics = {f"metric-{index}": index for index in range(E.MAX_OPEN_FILES * 2)}
    counts = []
    for count in (2, 80):
        emit(project[1], [{"t": index, "metrics": metrics} for index in range(count)])
        calls.clear()
        result = E.export_data(request(project, layout="metric"))
        assert result["status"] == "complete", result
        counts.append(len(calls))
        verify_bundle(output(result))
    assert counts[0] == counts[1]
    assert counts[0] <= len(metrics) + 8  # README, manifest, marker and directory sync


def test_payload_sync_failure_removes_only_owned_stage_and_retains_other_bundle(project, monkeypatch):
    emit(project[1], [{"t": 1, "metrics": {"x": 1}}])
    prior = output(E.export_data(request(project)))
    plan = E.plan_export(request(project))
    original_sync = os.fsync
    def fail_payload(fd):
        path = os.readlink(f"/proc/self/fd/{fd}")
        if path.endswith("application-metrics.csv"):
            raise OSError(28, "Filesystem full during payload sync")
        return original_sync(fd)
    monkeypatch.setattr(E.os, "fsync", fail_payload)
    result = E.export_data(plan)
    assert result["status"] == "failed" and result["outputs"] == []
    assert not Path(plan["destinations"][0]["path"]).exists()
    assert not list(prior.parent.glob(".raven-stage-*"))
    verify_bundle(prior)
    assert (project[1] / "metrics.jsonl").exists()
