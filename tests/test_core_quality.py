"""Regression coverage for bounded sampling, byte-safe logs, and faithful replay."""
from __future__ import annotations

import concurrent.futures
import json
from pathlib import Path
import subprocess
import threading
from types import SimpleNamespace

import pytest

from raven.logs import LogBuffer
from raven.model import GpuSample, Job, Live, Store, secs
from raven.record import RecordingBackend, ReplayBackend, ReplayClock
from raven.remote import LocalFiles, RemoteFiles, SshBackend
from raven.sampler import Sampler
from raven.slurm import CommandError, FakeBackend, Slurm


def job(jid="1"):
    return Job(id=jid, name="training", partition="gpu", state="RUNNING", cpus=4, gpus=1)


def sampler_for(slurm, jobs, workers=1):
    store = Store(persist=False)
    store.apply_jobs(jobs)
    sampler = Sampler(slurm, store, {}, [], workers=workers)
    for health in store.health.values():
        health.enabled = health.name == "gpu"
    return sampler, store


def test_gpu_sampling_completes_with_one_source_worker():
    slurm = SimpleNamespace(gpu=lambda j: [GpuSample("node", 0, 73, 512, 1024)], gpu_timeout=0.01)
    sampler, store = sampler_for(slurm, [job()])
    try:
        futures = sampler.round()
        concurrent.futures.wait(futures, timeout=1)
        assert all(f.done() for f in futures), "GPU work must not wait on its own occupied executor"
        assert store.gpu["1"][0].util == 73
        assert store.health["gpu"].errors == 0
    finally:
        sampler.shutdown()
    assert sampler.round() == []


def test_gpu_submission_is_bounded_and_failures_reach_health():
    entered, release = threading.Event(), threading.Event()
    submitted = []

    def gpu(j):
        entered.set()
        assert release.wait(2)
        raise CommandError("GPU permission denied")

    sampler, store = sampler_for(SimpleNamespace(gpu=gpu, gpu_timeout=0.01), [job(str(i)) for i in range(12)], workers=2)
    original_submit = sampler.gpu_pool.submit

    def submit(fn, item):
        submitted.append(item.id)
        return original_submit(fn, item)

    sampler.gpu_pool.submit = submit
    try:
        futures = sampler.round()
        assert entered.wait(1)
        assert len(submitted) <= 2
        release.set()
        concurrent.futures.wait(futures, timeout=2)
        assert len(submitted) == 12
        assert store.health["gpu"].errors == 1
        assert "12/12" in store.health["gpu"].error
        assert all(value is None for value in store.gpu.values())
    finally:
        release.set()
        sampler.shutdown()


def test_unavailable_live_metrics_are_not_reported_as_idle():
    store = Store(persist=False)
    store.apply_live("1", Live(avg=0.7, t=1))
    store.apply_live("1", Live(t=2))
    store.apply_live("1", Live(avg=0.0, t=3))
    assert list(store.hist_cpu["1"]) == [0.7, 0.0]


def test_incremental_logs_stay_bounded_and_render_only_a_page(tmp_path, monkeypatch):
    path = tmp_path / "growing.log"
    path.write_bytes(b"")
    buf = LogBuffer(str(path), max_bytes=128)
    for i in range(50):
        with path.open("ab") as f:
            f.write(f"row {i:03d} abcdefghijklmnop\n".encode())
        assert buf.refresh()
        assert buf._retained_bytes <= 128
    assert buf.truncated and buf.skipped_bytes > 0
    assert buf.lines[-1].startswith("row 049")
    monkeypatch.setattr(buf, "all_lines", lambda: pytest.fail("rendering must not copy the whole buffer"))
    lines, start = buf.window(None, 2)
    assert len(lines) == 2 and lines[-1].startswith("row 049") and start == buf.total - 2


def test_huge_log_append_and_unterminated_lines_have_bounded_reads(tmp_path):
    class TrackingFiles(LocalFiles):
        def __init__(self):
            self.lengths = []

        def read(self, path, offset, length):
            self.lengths.append(length)
            return super().read(path, offset, length)

    path = tmp_path / "burst.log"
    path.write_bytes(b"before\n")
    files = TrackingFiles()
    buf = LogBuffer(str(path), max_bytes=64, files=files)
    buf.refresh()
    with path.open("ab") as f:
        f.write(b"x" * 10_000)
    buf.refresh()
    assert max(files.lengths) <= 65
    assert len(buf.partial.encode()) <= 64 and buf._retained_bytes <= 64
    with path.open("ab") as f:
        f.write(b"y" * 32)
    buf.refresh()
    assert len(buf.partial.encode()) == 64 and buf.partial.endswith("y" * 32)


def test_log_utf8_split_across_appends_is_preserved(tmp_path):
    path = tmp_path / "utf8.log"
    data = "training 🚀 complete\n".encode()
    split = data.index(b"\xf0") + 2
    path.write_bytes(data[:split])
    buf = LogBuffer(str(path))
    buf.refresh()
    assert "\ufffd" not in buf.partial
    with path.open("ab") as f:
        f.write(data[split:])
    buf.refresh()
    assert buf.lines == ["training 🚀 complete"]


def test_remote_log_reads_preserve_bytes_and_spaced_names(tmp_path):
    def runner(argv, timeout):
        result = subprocess.run(["sh", "-c", argv[-1]], capture_output=True, text=True, timeout=timeout)
        return result.returncode, result.stdout, result.stderr

    files = RemoteFiles(SshBackend("localhost", control=False, runner=runner))
    path = tmp_path / "job's output.log"
    raw = b"\xff\r\n" + "🚀".encode() + b"\x00tail\n"
    path.write_bytes(raw)
    assert files.read(str(path), 0, len(raw)) == raw
    assert files.read(str(path), 4, 2) == raw[4:6]
    assert path.name in files.listdir(str(tmp_path))
    with pytest.raises(OSError):
        files.read(str(tmp_path / "missing"), 0, 10)


def test_replay_keeps_argument_boundaries_and_latest_equal_timestamp(tmp_path):
    path = tmp_path / "session.jsonl"
    records = [
        {"t": 1, "cmd": ["query", "a b"], "out": "one argument"},
        {"t": 2, "cmd": ["query", "a", "b"], "out": "two arguments"},
        {"t": 3, "cmd": ["query", "a b"], "out": "older"},
        {"t": 3, "cmd": ["query", "a b"], "out": "latest"},
    ]
    path.write_text("\n".join(json.dumps(rec) for rec in records))
    replay = ReplayBackend(str(path), paused=True)
    assert replay.run(["query", "a b"])[0] == "one argument"
    replay.clock.seek(2)
    assert replay.run(["query", "a", "b"])[0] == "two arguments"
    assert replay.run(["query", "a b"])[0] == "one argument"
    replay.clock.seek(3)
    assert replay.run(["query", "a b"])[0] == "latest"


def test_replay_playback_uses_monotonic_elapsed_time(monkeypatch):
    from raven import record
    now = [10.0]
    monkeypatch.setattr(record.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(record.time, "time", lambda: 1_000_000)
    clock = ReplayClock(100, 200, speed=2)
    now[0] += 5
    monkeypatch.setattr(record.time, "time", lambda: -1_000_000)
    assert clock.now() == 110
    clock.toggle_pause()
    now[0] += 50
    assert clock.now() == 110


def test_series_load_keeps_valid_tail_after_interrupted_write(tmp_path):
    series = tmp_path / "series"
    series.mkdir()
    store = Store(state_dir=str(tmp_path), series_keep=5)
    Path(store._series_path("1")).write_text("".join(json.dumps({"t": i, "k": "live"}) + "\n" for i in range(100)) + '{"t":')
    assert [row["t"] for row in store.series_of("1")] == list(range(95, 100))


def test_late_recording_response_after_shutdown_does_not_fail(tmp_path):
    recording = RecordingBackend(FakeBackend(), str(tmp_path / "recording.jsonl"))
    recording.close()
    recording._write({"t": 1, "out": "late command result"})
    assert recording.f.closed


def test_gpu_ssh_fallback_is_refused_without_allocation_ownership():
    class Backend:
        def __init__(self):
            self.calls = []

        def run(self, cmd, timeout):
            self.calls.append(cmd)
            if cmd[0] == "srun":
                raise CommandError("overlap unavailable")
            return "0, 50, 512, 1024, GPU\n", 0.0

    backend = Backend()
    running = job()
    running.hosts = ["node01"]
    running.gpus = 1
    with pytest.raises(CommandError, match="SSH fallback is disabled"):
        Slurm(backend, "user").gpu(running)
    assert all(cmd[0] != "ssh" for cmd in backend.calls)


def test_trace_at_exact_size_keeps_its_first_complete_sample():
    raw = b"2026/10/01 06:00:01.000, 0, 50, 100\n"
    files = SimpleNamespace(tail=lambda path, maximum: (raw, len(raw)))
    assert len(Slurm.gpu_trace("trace.csv", files, max_bytes=len(raw))) == 1


def test_invalid_day_duration_is_unknown():
    assert secs("invalid-01:00:00") is None


def test_initial_weather_waits_for_jobs_partitions_and_discovered_account():
    from raven.model import Partition

    ready = {"jobs": False, "partitions": False, "share": False}

    def jobs():
        ready["jobs"] = True
        return [job()]

    def partitions(types):
        ready["partitions"] = True
        return [Partition("gpu", gpus={"a100": {"total": 2}})], {}, {}

    def share():
        ready["share"] = True
        return [{"account": "research"}]

    def probe(partition, gres, cpus, mem, limit, account):
        assert all(ready.values())
        assert account == "research"
        return {"partition": partition, "account": account, "gres": gres}

    slurm = SimpleNamespace(jobs=jobs, partitions=partitions, share=share, pending_ahead=lambda: {}, probe=probe)
    store = Store(persist=False)
    sampler = Sampler(slurm, store, {}, [], workers=1)
    for health in store.health.values():
        health.enabled = health.name in {"jobs", "partitions", "share", "weather"}
    # Gating is observable before any jobs start, independent of thread scheduling.
    assert not sampler.due("weather", 1_000)
    try:
        sampler.round(wait=True)
        assert len(store.weather) == 1
        assert store.weather[0]["account"] == "research"
        assert store.health["weather"].calls == 1
        assert all(health.errors == 0 for health in store.health.values())
    finally:
        sampler.shutdown()
