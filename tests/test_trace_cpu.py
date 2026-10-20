"""CPU observations retain actual emitter provenance and never invent residency."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from raven import launch, launch_settings, trace_cpu
from raven.trace_protocol import read_trace
from raven.trace_recorder import PythonCapture, Recorder


def probe(monkeypatch, *, cpus=(2,), topology=None):
    values = iter(cpus)
    latest = cpus[-1]
    def query():
        return next(values, latest)
    monkeypatch.setattr(trace_cpu, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(trace_cpu, "_cpu_function", lambda: query)
    monkeypatch.setattr(trace_cpu, "_topology", lambda **kw: ({2: (1, 3)} if topology is None else topology, True))
    return trace_cpu.CPUPlacement()


def test_observation_brackets_actual_query_and_preserves_unknown_hotplug(monkeypatch):
    observer = probe(monkeypatch, cpus=(2, 2, 7))
    ticks = iter((100, 104, 110, 115))
    monkeypatch.setattr(trace_cpu.time, "monotonic_ns", lambda: next(ticks))
    assert observer.observe() == {"schema": trace_cpu.SCHEMA, "logical_cpu": 2, "socket_id": 1,
        "core_id": 3, "sample_start_ns": 100, "sample_end_ns": 104, "source": "linux-sched_getcpu"}
    unknown = observer.observe()
    assert unknown["logical_cpu"] == 7
    assert unknown["socket_id"] is unknown["core_id"] is None
    assert observer.coverage["topology_cpus"] == 1


@pytest.mark.parametrize("platform", ["darwin", "win32", "freebsd14"])
def test_unsupported_system_does_not_probe(monkeypatch, platform):
    monkeypatch.setattr(trace_cpu, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setattr(trace_cpu, "_cpu_function", lambda: pytest.fail("unsupported host probed"))
    observer = trace_cpu.CPUPlacement()
    assert observer.observe() is None
    assert observer.coverage["status"] == "unavailable"
    assert "Linux" in observer.coverage["reason"]


@pytest.mark.parametrize("failure", [OSError, AttributeError])
def test_probe_failure_is_explicit_and_fail_open(monkeypatch, failure):
    monkeypatch.setattr(trace_cpu, "sys", SimpleNamespace(platform="linux"))
    def fail():
        raise failure("not available")
    monkeypatch.setattr(trace_cpu, "_cpu_function", fail)
    observer = trace_cpu.CPUPlacement()
    assert observer.observe() is None
    assert observer.coverage["status"] == "unavailable"
    assert failure.__name__ in observer.coverage["reason"]


@pytest.mark.parametrize("value", [-1, True, 1 << 31, None])
def test_failed_later_observation_disables_probe_without_retry_cost(monkeypatch, value):
    observer = probe(monkeypatch, cpus=(2, value))
    assert observer.observe() is None
    assert observer.coverage["status"] == "unavailable"
    assert observer.function is None
    assert observer.observe() is None


def test_forked_probe_is_not_reused(monkeypatch):
    observer = probe(monkeypatch)
    monkeypatch.setattr(trace_cpu.os, "getpid", lambda: observer.pid + 1)
    assert observer.observe() is None
    assert "fork" in observer.coverage["reason"]


def topology_cpu(root, number, socket, core):
    path = root / ("cpu" + str(number)) / "topology"
    path.mkdir(parents=True)
    (path / "physical_package_id").write_text(str(socket))
    (path / "core_id").write_text(str(core))


def test_topology_missing_negative_or_malformed_fields_stay_unknown(tmp_path):
    topology_cpu(tmp_path, 0, 0, 2)
    topology_cpu(tmp_path, 1, -1, "unknown")
    (tmp_path / "cpu2").mkdir()
    (tmp_path / "cpufreq").mkdir()
    assert trace_cpu._topology(tmp_path) == ({0: (0, 2), 1: (None, None), 2: (None, None)}, False)
    assert trace_cpu._topology(tmp_path / "absent") == ({}, False)


def test_topology_cache_is_bounded(tmp_path, monkeypatch):
    for number in range(5):
        topology_cpu(tmp_path, number, 0, number)
    monkeypatch.setattr(trace_cpu, "MAX_CPUS", 2)
    result, complete = trace_cpu._topology(tmp_path)
    assert len(result) == 2
    assert not complete


def test_initial_affinity_bounds_topology_reads(tmp_path, monkeypatch):
    for number in range(5):
        topology_cpu(tmp_path, number, 0, number)
    monkeypatch.setattr(os, "scandir", lambda *a, **kw: pytest.fail("rank scanned every host CPU"))
    assert trace_cpu._topology(tmp_path, cpus={1, 3}) == ({1: (0, 1), 3: (0, 3)}, True)
    monkeypatch.setattr(trace_cpu, "MAX_CPUS", 1)
    result, complete = trace_cpu._topology(tmp_path, cpus={1, 3})
    assert len(result) == 1 and not complete


def test_probe_records_affinity_scope_and_falls_back_if_unavailable(monkeypatch):
    probe(monkeypatch)
    seen = []
    monkeypatch.setattr(os, "sched_getaffinity", lambda pid: {2}, raising=False)
    monkeypatch.setattr(trace_cpu, "_topology", lambda **kw: (seen.append(kw["cpus"]) or {2: (0, 1)}, True))
    assert trace_cpu.CPUPlacement().coverage["topology_scope"] == "initial-affinity"
    assert seen == [{2}]
    def unavailable(pid):
        raise OSError("unavailable")
    monkeypatch.setattr(os, "sched_getaffinity", unavailable)
    assert trace_cpu.CPUPlacement().coverage["topology_scope"] == "bounded-host-metadata"
    assert seen[-1] is None


def test_observations_do_no_filesystem_reads(monkeypatch):
    observer = probe(monkeypatch)
    monkeypatch.setattr(Path, "open", lambda *a, **kw: pytest.fail("per-event filesystem read"))
    monkeypatch.setattr(os, "scandir", lambda *a, **kw: pytest.fail("per-event topology probe"))
    for _ in range(10):
        assert observer.observe()["logical_cpu"] == 2


def test_recorder_attributes_current_emitter_only_and_copies_payload(tmp_path, monkeypatch):
    probe(monkeypatch)
    data = {"function": "work"}
    recorder = Recorder(tmp_path, cpu_placement=True, background_flush=False, journal_format="blocks")
    recorder.emit("call", data)
    recorder.emit("return", data, thread=str(threading.get_ident()))
    recorder.emit("note", {"name": "historical"}, mono_ns=time.monotonic_ns())
    recorder.emit("note", {"name": "foreign"}, thread="remote-thread")
    recorder.emit("note", {str(i): i for i in range(64)})
    with pytest.raises(ValueError, match="reserved"):
        recorder.emit("note", {"cpu_observation": {"logical_cpu": 999}})
    recorder.close()
    snapshot = read_trace(recorder.path)
    assert snapshot["status"] == "COMPLETE", snapshot["warnings"]
    assert data == {"function": "work"}
    local = [event for event in snapshot["events"] if event["kind"] in {"call", "return"}]
    assert len(local) == 2
    for event in local:
        observed = event["data"]["cpu_observation"]
        assert event["mono_ns"] <= observed["sample_start_ns"] <= observed["sample_end_ns"]
        assert observed["logical_cpu"] == 2
    assert all("cpu_observation" not in event["data"] for event in snapshot["events"] if event["kind"] == "note")
    assert snapshot["covered"]["cpu_placement"]["status"] == "available"


def test_default_capture_does_not_probe_or_add_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(trace_cpu, "CPUPlacement", lambda: pytest.fail("default capture enabled CPU probe"))
    recorder = Recorder(tmp_path, background_flush=False)
    recorder.emit("note", {"name": "ordinary"})
    recorder.close()
    assert all("cpu_observation" not in event["data"] for event in read_trace(recorder.path)["events"])


def test_full_scope_does_not_trace_its_own_cpu_observer(tmp_path, monkeypatch):
    probe(monkeypatch)
    recorder = Recorder(tmp_path, cpu_placement=True, background_flush=False)
    capture = PythonCapture(recorder, legacy=True)
    assert not capture._included(trace_cpu.CPUPlacement.observe.__code__)
    recorder.close()


def test_python_capture_alias_is_observed_on_actual_emitter_threads(tmp_path, monkeypatch):
    probe(monkeypatch)
    recorder = Recorder(tmp_path, cpu_placement=True, background_flush=False)
    capture = PythonCapture(recorder, legacy=True, scope="project", roots=(tmp_path,))
    namespace = {}
    exec(compile("def work():\n    return 17\n", str(tmp_path / "work.py"), "exec"), namespace)
    capture.start()
    try:
        assert namespace["work"]() == 17
        thread = threading.Thread(target=namespace["work"])
        thread.start()
        thread.join()
    finally:
        capture.stop()
        recorder.close()
    events = [event for event in read_trace(recorder.path)["events"] if event["data"].get("function") == "work"]
    assert len({event["thread"] for event in events}) == 2
    assert all(event["data"]["cpu_observation"]["logical_cpu"] == 2 for event in events)


def test_optional_cpu_observation_failure_keeps_recording(tmp_path, monkeypatch):
    monkeypatch.setattr(trace_cpu, "sys", SimpleNamespace(platform="darwin"))
    recorder = Recorder(tmp_path, cpu_placement=True, background_flush=False)
    recorder.emit("note", {"name": "still-recorded"})
    recorder.close()
    result = read_trace(recorder.path)
    assert result["status"] == "COMPLETE"
    assert result["covered"]["cpu_placement"]["status"] == "unavailable"
    assert any(e["data"].get("name") == "still-recorded" for e in result["events"])


def test_cpu_setting_cli_precedence_and_schema(tmp_path):
    job = tmp_path / "job.sh"
    job.write_text("#!/bin/sh\ntrue\n")
    saved = launch_settings.save_settings(tmp_path, {"launch": {"trace": True, "trace_cpu_placement": True}})
    plan = launch.resolve_plan([str(job), "--no-trace-cpu-placement", "--check"], cwd=tmp_path, environ={})
    assert plan["settings"]["trace_cpu_placement"] is False
    assert saved == launch_settings.load_settings(tmp_path)
    plan = launch.resolve_plan([str(job), "--trace-cpu-placement", "--check"], cwd=tmp_path, environ={})
    assert plan["settings"]["trace_cpu_placement"] is True
    assert any(row["feature"] == "trace-cpu-placement" for row in plan["readiness"])
    with pytest.raises(ValueError, match="trace_cpu_placement"):
        launch_settings.save_settings(tmp_path, {"launch": {"trace_cpu_placement": 1}})
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((Path(__file__).parents[1] / "docs/schemas/automation-settings.v1.schema.json").read_text())
    jsonschema.Draft202012Validator(schema).validate(saved)


@pytest.mark.skipif(sys.platform != "linux", reason="actual Linux sched_getcpu qualification")
def test_actual_linux_cpu_observation():
    observer = trace_cpu.CPUPlacement()
    assert observer.coverage["status"] == "available", observer.coverage
    observation = observer.observe()
    assert observation["sample_start_ns"] <= observation["sample_end_ns"]
    assert observation["logical_cpu"] >= 0
    if hasattr(os, "sched_getaffinity"):
        assert observation["logical_cpu"] in os.sched_getaffinity(0)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux affinity and sched_getcpu qualification")
def test_actual_cpu_change_is_an_observation_on_each_target_core(tmp_path):
    available = sorted(os.sched_getaffinity(0))
    if len(available) < 2:
        pytest.skip("Need two available logical CPUs to qualify a migration observation")
    selected = available[:2]
    # Restrict only this disposable child's emitting thread, then restore its
    # original binding. No application or scheduler-owned task is altered.
    source = '''
import json,os,sys
from raven.trace_recorder import Recorder
cpus=json.loads(sys.argv[2]); original=os.sched_getaffinity(0)
recorder=Recorder(sys.argv[1],cpu_placement=True,background_flush=False)
try:
    for cpu in cpus:
        os.sched_setaffinity(0,{cpu})
        recorder.emit('note',{'name':'cpu-'+str(cpu)})
finally:
    os.sched_setaffinity(0,original)
    recorder.close()
print(recorder.path)
'''
    process = subprocess.run([sys.executable, "-c", source, str(tmp_path), json.dumps(selected)],
                             text=True, capture_output=True, timeout=15)
    assert process.returncode == 0, process.stderr
    snapshot = read_trace(process.stdout.strip())
    assert snapshot["status"] == "COMPLETE", snapshot["warnings"]
    notes = [event for event in snapshot["events"] if event["kind"] == "note"]
    assert [event["data"]["cpu_observation"]["logical_cpu"] for event in notes] == selected
    topology, _ = trace_cpu._topology(cpus=set(selected))
    for event in notes:
        observed = event["data"]["cpu_observation"]
        assert (observed["socket_id"], observed["core_id"]) == topology[observed["logical_cpu"]]
    from raven.trace_cpu_timeline import build
    timeline = build(snapshot, snapshot["events"][0]["mono_ns"])
    assert timeline["status"] == "observed"
    assert {lane["logical_cpu"] for lane in timeline["lanes"]} >= set(selected)
    assert [row["logical_cpu"] for row in timeline["observations"] if row["name"].startswith("cpu-")] == selected


@pytest.mark.skipif(sys.platform != "linux", reason="actual local Linux bundled launch qualification")
def test_actual_portable_launch_retains_cpu_observations(tmp_path):
    from raven.launch_runtime import execute_plan
    (tmp_path / "work.py").write_text("def work(): return 3\nassert work() == 3\n")
    job = tmp_path / "job.sh"
    job.write_text("#!/bin/sh\n" + shlex.join([sys.executable, str(tmp_path / "work.py")]) + "\n")
    args = [str(job), "--local", "--trace", "--trace-cpu-placement"]
    if sys.version_info < (3, 12):
        args += ["--trace-adapter", "python-legacy-profile"]
    environment = {key: value for key, value in os.environ.items() if not key.startswith("RAVEN_") and key != "PYTHONPATH"}
    plan = launch.resolve_plan(args, cwd=tmp_path, environ=environment)
    result = execute_plan(plan, environ=environment)
    assert result["exit_code"] == 0
    run = Path(result["run_dir"])
    sources = json.loads((run / "trace/index.json").read_text())["sources"]
    manifests = [run / entry["path"] for entry in sources if entry.get("schema") == "raven.trace/v1"]
    assert len(manifests) == 1
    snapshot = read_trace(manifests[0])
    assert snapshot["status"] == "COMPLETE", snapshot["warnings"]
    assert snapshot["covered"]["cpu_placement"]["status"] == "available"
    assert any(event["data"].get("function") == "work" and "cpu_observation" in event["data"] for event in snapshot["events"])


def test_standalone_trace_run_exposes_cpu_option(tmp_path):
    script = tmp_path / "work.py"
    script.write_text("def work(): return 3\nwork()\n")
    result = subprocess.run([sys.executable, "-m", "raven.trace_recorder", "run", "--project", str(tmp_path),
        "--legacy-profile", "--cpu-placement", "--", str(script)], text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    manifest = next((tmp_path / ".raven/traces").glob("*/manifest.json"))
    snapshot = read_trace(manifest)
    assert snapshot["status"] == "COMPLETE", snapshot["warnings"]
    assert "cpu_placement" in snapshot["covered"]
    if sys.platform == "linux":
        assert any(event["data"].get("function") == "work" and "cpu_observation" in event["data"] for event in snapshot["events"])
