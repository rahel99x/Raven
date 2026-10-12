"""Real bounded provider subprocesses and exact-attempt retained reader paths."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from raven import gpu_collectors as G, projects, reporting
from raven import retained_telemetry as T
from raven.model import Job, Store
from raven.sampler import Sampler
from raven.views import _prepare_trace_metrics, _prepare_retained_process_metrics


@pytest.fixture
def run(tmp_path):
    (tmp_path / "job.sh").write_text("#!/bin/bash\ntrue\n")
    return reporting.begin_run(tmp_path, "observed", name="training", script="job.sh", job_id="7")


def wait_for(fn):
    deadline = time.monotonic() + 3
    while not fn():
        if time.monotonic() > deadline:
            pytest.fail("timed out waiting for recorder publication")
        time.sleep(.005)


def source(recording, family):
    return next(value for value in recording.sources if value["family"] == family)


def fake_nvidia(tmp_path, text, *, script=None):
    folder = tmp_path / "tools"
    folder.mkdir(exist_ok=True)
    binary = folder / "nvidia-smi"
    binary.write_text(script or ("#!/bin/sh\ncat <<'RAVEN_FIXTURE'\n" + text + "\nRAVEN_FIXTURE\n"))
    binary.chmod(0o700)
    return {"PATH": str(folder) + ":/usr/bin:/bin", "SLURM_JOB_ID": "7", "SLURM_JOB_GPUS": "GPU-b", "SLURMD_NODENAME": "n1"}


def test_real_probe_only_retains_verified_uuid_and_preserves_missing_counter(tmp_path):
    env = fake_nvidia(tmp_path, "0, 98, 10, 100, Other, GPU-a, 0000:01:00.0, Disabled\n5, N/A, 0, 24000, Mine, GPU-b, 0000:05:00.0, Disabled")
    rows, _, _ = T.gpu_observation(env, "nvidia", 1., {}, threading.Event())
    assert len(rows) == 1
    assert rows[0].uuid == "GPU-b" and rows[0].index == 5
    assert rows[0].util is None and rows[0].used == 0


def test_numeric_subset_is_unavailable_instead_of_guessing_gpu_zero(tmp_path):
    env = fake_nvidia(tmp_path, "0, 98, 10, 100, Other, GPU-a\n1, 80, 20, 100, Mine, GPU-b")
    env["SLURM_JOB_GPUS"] = "0"
    rows, _, warnings = T.gpu_observation(env, "nvidia", 1., {}, threading.Event())
    assert rows == [] and any("do not prove" in item for item in warnings)


def test_mig_parent_never_becomes_partition_measurement(tmp_path):
    env = fake_nvidia(tmp_path, "0, 98, 10, 100, Parent, GPU-b, 0000:01:00.0, Enabled")
    rows, _, warnings = T.gpu_observation(env, "nvidia", 1., {}, threading.Event())
    assert rows == [] and any("MIG parent" in item for item in warnings)


def test_slow_provider_is_bounded_and_returns_visible_failure(tmp_path):
    env = fake_nvidia(tmp_path, "", script="#!/bin/sh\nexec sleep 30\n")
    started = time.monotonic()
    rows, _, warnings = T.gpu_observation(env, "nvidia", .2, {}, threading.Event())
    assert time.monotonic() - started < 2
    assert rows == [] and any("timed out" in item for item in warnings)


def test_provider_cancel_interrupts_and_reaps_probe_group(tmp_path):
    env = fake_nvidia(tmp_path, "", script="#!/bin/sh\nexec sleep 30\n")
    stop = threading.Event()
    timer = threading.Timer(.1, stop.set)
    timer.start()
    try:
        with pytest.raises(TimeoutError, match="cancelled"):
            T.gpu_observation(env, "nvidia", 5., {}, stop)
    finally:
        timer.join()


@pytest.mark.parametrize("vendor", ["amd", "intel"])
def test_provider_fixture_meaning_survives_retention(run, monkeypatch, vendor):
    fixture = Path(__file__).parent / "fixtures/gpu"
    if vendor == "amd":
        rows = G.parse_amd((fixture / "amd-metric.json").read_text(), (fixture / "amd-list.json").read_text(), "node")
    else:
        rows = G.parse_intel((fixture / "intel-stats.json").read_text(), (fixture / "intel-discovery.json").read_text(), "node")
    monkeypatch.setattr(T, "gpu_observation", lambda *a: (rows, {}, []))
    recording = T.start_recording(run, {"gpu": True}, environ={"SLURM_JOB_ID": "7", "SLURM_JOB_NUM_NODES": "2"})
    wait_for(lambda: source(recording, "gpu")["coverage"]["samples"] == len(rows))
    recording.close()
    index = json.loads((run / "telemetry/index.json").read_text())
    gpu = next(item for item in index["sources"] if item["family"] == "gpu")
    assert gpu["status"] == "partial" and "supervisor node" in gpu["reason"]
    saved = [json.loads(line) for line in (run / "telemetry/gpu.jsonl").read_text().splitlines()]
    assert saved[0]["provider"] == vendor
    assert saved[0]["memory_used_mib"] == rows[0].used
    assert saved[0]["device_uuid"] == rows[0].uuid
    assert saved[0]["timestamp_utc"].endswith("Z") and "." in saved[0]["timestamp_utc"]
    assert saved[0]["elapsed_seconds"] >= 0
    assert gpu["coverage"]["expected_nodes"] == 2


def test_storage_cap_preserves_prefix_without_stopping_workload(run, monkeypatch):
    rows = [G.parse_nvidia(f"{i}, 50, 10, 100, GPU, GPU-{i}", "n")[0] for i in range(30)]
    monkeypatch.setattr(T, "gpu_observation", lambda *a: (rows, {}, []))
    recording = T.start_recording(run, {"gpu": True, "settings": {"telemetry": {"max_bytes_per_stream": 4096}}}, environ={})
    wait_for(lambda: source(recording, "gpu")["status"] == "partial")
    recording.close()
    data = (run / "telemetry/gpu.jsonl").read_bytes()
    assert 0 < len(data) <= 4096 and data.endswith(b"\n")
    saved = [json.loads(line) for line in data.splitlines()]
    assert saved[0]["device_index"] == 0 and len(saved) < len(rows)
    assert "Storage budget" in source(recording, "gpu")["reason"]


def test_failed_provider_backoff_does_not_spin_or_stop_cpu_recording(run, monkeypatch):
    calls = []
    def fail(*args):
        calls.append(time.monotonic())
        raise TimeoutError("GPU provider timed out")
    monkeypatch.setattr(T, "gpu_observation", fail)
    recording = T.start_recording(run, {"gpu": True, "runtime_pid": os.getpid(), "settings": {"telemetry": {"interval_seconds": 1}}}, environ={})
    wait_for(lambda: len(calls) == 1)
    time.sleep(.1)
    recording.close()
    gpu, cpu = source(recording, "gpu"), source(recording, "cpu-memory")
    assert len(calls) == 1 and gpu["coverage"]["missed_samples"] == 1
    assert gpu["status"] == "unavailable" and "timed out" in gpu["reason"]
    assert cpu["coverage"]["samples"] >= 1


def test_real_workload_process_metrics_have_explicit_non_job_scope():
    process = subprocess.Popen([sys.executable, "-c", "import time; data=bytearray(2000000); time.sleep(10)"])
    try:
        values, identity = T.process_observation(process.pid)
        assert values["process_count"] == 1 and values["cpu_seconds"] >= 0
        assert values["rss_bytes"] >= 0 and "shared pages" in values["memory_scope"]
        assert "departed" in values["cpu_scope"]
        with pytest.raises(ValueError, match="reused"):
            T.process_observation(process.pid, identity + 1)
    finally:
        process.terminate()
        process.wait(timeout=3)


def test_missing_pid_is_unavailable_not_supervisor_usage(run):
    recording = T.start_recording(run, {})
    recording.close()
    cpu = source(recording, "cpu-memory")
    assert cpu["status"] == "unavailable" and "workload child PID" in cpu["reason"]
    assert not (run / "telemetry/cpu-memory.jsonl").exists()


def test_existing_or_symlink_telemetry_directory_is_never_overwritten(run, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (run / "telemetry").symlink_to(outside, target_is_directory=True)
    recording = T.start_recording(run, {"runtime_pid": os.getpid()})
    assert recording.close()["status"] == "failed"
    assert not list(outside.iterdir())


def publish(run, monkeypatch):
    rows = G.parse_nvidia("4, 25.125, 100, 1000, GPU, GPU-unique", "nodeA")
    monkeypatch.setattr(T, "gpu_observation", lambda *a: (rows, {}, []))
    recording = T.start_recording(run, {"gpu": True, "runtime_pid": os.getpid()}, environ={"SLURM_JOB_ID": "7"})
    wait_for(lambda: source(recording, "gpu")["coverage"]["samples"] == 1)
    recording.close()
    return projects.select_run(str(run.parent.parent), run.name)["binding"]


def test_exact_attempt_reader_and_sampler_auto_attach_retained_sources(run, monkeypatch):
    binding = publish(run, monkeypatch)
    value = projects.read_bound_telemetry(binding)
    assert value["gpu"][0]["utilization_percent"] == 25.125
    assert value["cpu-memory"]
    store = Store(persist=False)
    store.apply_jobs([Job("7", "training", "local", "RUNNING", gpus=1)])
    sampler = Sampler(SimpleNamespace(), store, {}, [], workers=1)
    try:
        sampler.bind_telemetry(binding)
        sampler.want_trace = "7"
        sampler.src_trace()
        assert store.trace["7"][0]["device_id"] == "nodeA/nvidia/GPU-unique"
        assert sampler.retained_telemetry["7"]["cpu-memory"]
        sampler.bind_telemetry(None)
        assert not sampler.retained_telemetry and "7" not in store.trace
    finally:
        sampler.shutdown()


def test_changed_attempt_and_symlink_stream_are_rejected(run, monkeypatch, tmp_path):
    binding = publish(run, monkeypatch)
    path = run / "telemetry/gpu.jsonl"
    path.unlink()
    path.symlink_to(tmp_path / "unrelated")
    with pytest.raises((OSError, ValueError)):
        projects.read_bound_telemetry(binding)
    manifest = json.loads((run / "run.json").read_text())
    manifest["attempt"] = 2
    (run / "run.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="attempt identity"):
        projects.read_bound_telemetry(binding)


def test_dashboard_tail_is_bounded_and_drops_incomplete_last_row(run, monkeypatch):
    binding = publish(run, monkeypatch)
    path = run / "telemetry/gpu.jsonl"
    complete = path.read_bytes()
    with path.open("ab") as stream:
        stream.write(complete * 30 + b'{"schema":')
    read = projects.read_bound_telemetry(binding, max_bytes=4096)
    assert 0 < len(read["gpu"]) < 31
    assert any("bounded tail" in value for value in read["warnings"])


def test_device_identity_keeps_same_ordinal_on_two_nodes_separate():
    base = {"schema": T.GPU_SCHEMA, "run_id": "r", "timestamp": 1.25, "device_index": 0, "provider": "nvidia", "device_uuid": "GPU-id", "utilization_percent": 30}
    data = T.display_gpu_rows([dict(base, node="A"), dict(base, node="B")])
    prepared = _prepare_trace_metrics(data)
    assert len(prepared["devices"]) == 2
    assert list(prepared["plots"][0][5]) == [1.25]


def test_cpu_chart_preserves_cpu_seconds_and_shared_rss_semantics():
    plots = _prepare_retained_process_metrics([{"timestamp": 1, "cpu_seconds": 3, "rss_bytes": 1024**3}, {"timestamp": 2, "cpu_seconds": 2, "rss_bytes": None}])
    assert plots[0][1] == [3, 2] and plots[0][3] == "s"
    assert plots[1][1] == [1., None]
    assert "departed" in plots[0][0] and "shared pages" in plots[1][0]


def test_declared_native_metric_producer_is_confined_and_revalidated(run, tmp_path):
    from raven.metrics import MetricReader
    path = tmp_path / "native-observed.jsonl"
    path.write_text('{"t":1.25,"metrics":{"loss":0.125}}\n')
    manifest = json.loads((run / "run.json").read_text())
    manifest["metadata"] = {"automation": {"metric_binding": {"path": str(path), "format": "raven.metrics/v1"}}}
    (run / "run.json").write_text(json.dumps(manifest))
    selected = projects.select_run(str(tmp_path), run.name)
    binding = selected["binding"]
    assert binding["metrics_file"] == str(path)
    assert any("separate attempt" in warning for warning in selected["warnings"])
    assert MetricReader().read_confined(path, binding)["latest"]["loss"] == .125
    manifest["metadata"]["automation"]["metric_binding"]["path"] = str(tmp_path / "another.jsonl")
    (run / "run.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="declaration changed"):
        MetricReader().read_confined(path, binding)


@pytest.mark.parametrize("malicious", ["../outside.jsonl", "symlink.jsonl"])
def test_declared_native_metric_producer_refuses_escapes_and_symlinks(run, tmp_path, malicious):
    path = tmp_path / malicious
    if malicious == "symlink.jsonl":
        path.symlink_to(tmp_path.parent / "not-our-metrics")
    manifest = json.loads((run / "run.json").read_text())
    manifest["metadata"] = {"automation": {"metric_binding": {"path": str(path), "format": "raven.metrics/v1"}}}
    (run / "run.json").write_text(json.dumps(manifest))
    selected = projects.select_run(str(tmp_path), run.name)
    assert selected["binding"]["metrics_file"] == ""
    assert any("binding refused" in warning for warning in selected["warnings"])


def test_retained_sources_read_through_isolated_ssh_helper(run, monkeypatch):
    from raven.remote import RemoteFiles, SshBackend
    publish(run, monkeypatch)
    class SSH:
        def run(self, command, timeout):
            assert len(SshBackend("example.invalid").argv(command)[-1].encode()) < 131072
            value = subprocess.run(command, cwd="/tmp", capture_output=True, text=True, timeout=timeout)
            assert value.returncode == 0, value.stderr
            return value.stdout, 0
    files = RemoteFiles(SSH())
    binding = projects.select_run(str(run.parent.parent), run.name, files=files)["binding"]
    value = projects.read_bound_telemetry(binding, files=files)
    assert value["gpu"][0]["device_uuid"] == "GPU-unique"
    assert value["cpu-memory"][0]["scope"] == "local-workload-process-tree"


def test_storage_write_failure_is_visible_and_cpu_stream_survives(run, monkeypatch):
    import errno
    original = os.write
    def full(fd, data):
        if os.readlink('/proc/self/fd/' + str(fd)).endswith('/telemetry/gpu.jsonl'):
            raise OSError(errno.ENOSPC, 'No space left on device')
        return original(fd, data)
    monkeypatch.setattr(T.os, 'write', full)
    monkeypatch.setattr(T, 'gpu_observation', lambda *a: (G.parse_nvidia('0, 25, 1, 2, GPU, GPU-id', 'n'), {}, []))
    recording = T.start_recording(run, {'gpu': True, 'runtime_pid': os.getpid()}, environ={})
    wait_for(lambda: source(recording, 'gpu')['status'] == 'failed')
    recording.close()
    assert 'No space' in source(recording, 'gpu')['reason']
    assert source(recording, 'cpu-memory')['coverage']['samples'] >= 1


@pytest.mark.parametrize('change', [
    {'coverage': []}, {'status': 'fabricated'}, {'path': '../outside'},
    {'devices': list(range(257))}, {'schema': 'unknown/v1'},
])
def test_malformed_telemetry_metadata_fails_closed(change):
    entry = {'family': 'gpu', 'path': 'telemetry/gpu.jsonl', 'schema': T.GPU_SCHEMA,
             'status': 'complete', 'reason': '', 'coverage': {}, **change}
    with pytest.raises(ValueError):
        T.validate_index({'schema': T.INDEX_SCHEMA, 'run_id': 'r', 'sources': [entry]}, 'r')


def test_collection_off_does_not_hide_retained_cpu_timeline(run, monkeypatch):
    binding = publish(run, monkeypatch)
    store = Store(persist=False)
    sampler = Sampler(SimpleNamespace(), store, {}, [], workers=1, collection={'gpu': 'off'})
    try:
        sampler.bind_telemetry(binding)
        sampler.set_collection_demand('analytics', '7')
        assert sampler._collection_needed('trace')
    finally:
        sampler.shutdown()


def test_explicit_cpu_recording_disable_never_samples_workload(run, monkeypatch):
    monkeypatch.setattr(T, 'process_observation', lambda *a: pytest.fail('disabled source sampled'))
    recording = T.start_recording(run, {'runtime_pid': os.getpid(), 'settings': {'cpu_memory': False}}, environ={})
    recording.close()
    assert source(recording, 'cpu-memory')['status'] == 'unavailable'
    assert 'disabled' in source(recording, 'cpu-memory')['reason']
    assert not (run / 'telemetry/cpu-memory.jsonl').exists()


def test_actual_retained_contracts_validate_against_published_schemas(run, monkeypatch):
    import jsonschema
    publish(run, monkeypatch)
    schemas = Path(__file__).resolve().parents[1] / 'docs/schemas'
    index = json.loads((run / 'telemetry/index.json').read_text())
    jsonschema.Draft202012Validator(json.loads((schemas / 'telemetry-index.v1.schema.json').read_text())).validate(index)
    for item in index['sources']:
        schema = json.loads((schemas / (item['family'] + '-sample.v1.schema.json')).read_text())
        validator = jsonschema.Draft202012Validator(schema)
        for line in (run / item['path']).read_text().splitlines():
            validator.validate(json.loads(line))


def test_fresh_dashboard_reopens_completed_attempt_and_renders_retained_cpu_gpu(run, monkeypatch):
    from raven import project_ui, layout
    from raven.config import Config
    from raven.controller import App
    from raven.model import Finished
    from raven.research import ResearchHub
    from raven.views import Views
    publish(run, monkeypatch)
    reporting.finish_run(run, state='COMPLETED', exit_code=0, runtime_seconds=1.)
    store = Store(persist=False)
    store.finished = [Finished('7', 'training', 'COMPLETED')]
    store.details['7'] = {'WorkDir': str(run.parent.parent)}
    sampler = Sampler(SimpleNamespace(), store, {}, [], workers=1)
    app = App(store, sampler, None, Config(), 'tester', interactive=False)
    app.research = ResearchHub(app.cfg)
    app.tab, app.selected_id, app.analytics_job = 'analytics', '7', '7'
    views = Views(layout.Glyphs(False), app.cfg)
    app.views_ref = views
    plotted = []
    def curve(app, values, width, height, identity, **kwargs):
        plotted.append((identity[2], list(values)))
        return []
    monkeypatch.setattr(views, 'metric_curve', curve)
    try:
        binding = project_ui.settle(app)
        assert binding['run_id'] == run.name and sampler.telemetry_binding['run_id'] == run.name
        sampler.select_trace('7')
        sampler.src_trace()
        text = layout.to_text(views.analytics_job(store.snapshot(), app, 120, None), 120)
        assert any(key == 'retained-cpu-seconds' and values for key, values in plotted)
        assert any(key == 'retained-rss' and values for key, values in plotted)
        assert any('GPU-unique' in key for key, values in plotted)
        assert 'Retained telemetry' in text
    finally:
        sampler.shutdown()
        app.research.close()
