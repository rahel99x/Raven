"""Slow state storage cannot hold the lock used by interactive graph feedback."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import builtins
import json
import threading
import os
from types import SimpleNamespace
from pathlib import Path

import pytest

from raven.model import GpuSample, Job, Live, Store
from raven.report import _ReportStore
from raven.sampler import Sampler
from raven import series_archive as archive


def write_series(directory, jid, samples, store=None):
    path = Path(store._series_path(jid)) if store else directory / "series" / (jid + ".jsonl")
    path.parent.mkdir(exist_ok=True)
    path.write_text("".join(json.dumps(item) + "\n" for item in samples))
    return path


@contextmanager
def blocked_open(monkeypatch, path, mode):
    real_open, entered, release = builtins.open, threading.Event(), threading.Event()
    calls = []
    def delayed(target, selected="r", *args, **kwargs):
        if str(target) == str(path) and selected == mode:
            calls.append(threading.current_thread().name)
            entered.set()
            if not release.wait(5):
                raise AssertionError("test did not release blocked storage")
        return real_open(target, selected, *args, **kwargs)
    monkeypatch.setattr(builtins, "open", delayed)
    if mode=='a':
        real_write=os.write
        def delayed_write(fd,data):
            try:
                selected=os.readlink('/proc/self/fd/'+str(fd))
            except OSError:
                selected=''
            if selected==str(path):
                calls.append(threading.current_thread().name);entered.set()
                if not release.wait(5):
                    raise AssertionError('test did not release blocked storage')
            return real_write(fd,data)
        monkeypatch.setattr(os,'write',delayed_write)
    try:
        yield entered, release, calls
    finally:
        release.set()


def assert_store_available(store, jid="7"):
    # The storage operation is still deliberately blocked. A timeout here is
    # an exact lock-ownership check, not a rendering-performance assertion.
    acquired = store.lock.acquire(timeout=.2)
    assert acquired, "filesystem IO retained the foreground Store lock"
    try:
        store.job_attempt(jid)
        store.record_context(jid)
        store.snapshot()
    finally:
        store.lock.release()


@pytest.mark.parametrize("source", ["live", "gpu", "jobs"])
def test_sampler_publishes_atomically_before_slow_disk_and_releases_store_lock(tmp_path, monkeypatch, source):
    store = Store(state_dir=str(tmp_path))
    job = Job("7", "worker", "desktop", "RUNNING", gpus=1)
    store.apply_jobs([job])
    live = Live(cpu_time=2., avg=.5, rate=.5, t=1700000000.0005)
    gpu = [GpuSample("node", 0, 75., 100., 1000.)]
    slurm = SimpleNamespace(live=lambda *args: (live, (2., live.t), ["step"]),
                            gpu=lambda *args: gpu, jobs=lambda: [])
    sampler = Sampler(slurm, store, {}, [], workers=1)
    store._series_last_flush = 0.  # Make the batching deadline due in this I/O fault test.
    path = tmp_path / "events.jsonl" if source == "jobs" else Path(store._series_path("7"))
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        with blocked_open(monkeypatch, path, "a") as (entered, release, calls):
            task = pool.submit(getattr(sampler, "src_" + source))
            assert entered.wait(2)
            assert_store_available(store)
            if source == "live":
                assert store.live["7"] is live
                assert store.prev_cpu["7"] == (2., live.t)
                assert store.steps["7"] == ["step"]
                assert store.series["7"][0]["t"] == live.t
            elif source == "gpu":
                assert store.gpu["7"] == gpu
                assert list(store.hist_gpu["7:node:0"]) == [.75]
            else:
                assert not store.jobs and "7" in store.departed_jobs
                assert store.events[-1]["job"] == "7"
            release.set()
            task.result(timeout=2)
        saved = [json.loads(line) for line in path.read_text().splitlines()]
        captured = list(store.events) if source == "jobs" else list(store.series["7"])
        assert saved == captured
        assert len(calls) == 1
    finally:
        pool.shutdown(wait=True)
        sampler.shutdown()


def test_background_cold_restore_returns_immediately_deduplicates_active_request_and_keeps_live_append(tmp_path, monkeypatch):
    store = Store(state_dir=str(tmp_path))
    path = write_series(tmp_path, "7", [{"t": 1., "k": "live", "cpu": .1}, {"t": 2., "k": "live", "cpu": .2}], store)
    pool = ThreadPoolExecutor(max_workers=1)
    store.configure_series_loader(pool.submit)
    try:
        with blocked_open(monkeypatch, path, "rb") as (entered, release, calls):
            assert store.series_view("7") == []
            assert entered.wait(2)
            for _ in range(40):
                assert store.series_view("7") == []
            assert store.series_status("7")["status"] == "loading"
            assert not store._series_archive.pending
            assert_store_available(store)
            # Append to the actual file during restore. Memory wins over its
            # overlapping saved observation and exact fractional time survives.
            sample = {"t": 2.0005, "k": "live", "cpu": .9}
            store.record("7", sample)
            assert store.series_view("7") == [sample]
            release.set()
        pool.shutdown(wait=True)
        assert store.series_view("7") == [{"t": 1., "k": "live", "cpu": .1}, {"t": 2., "k": "live", "cpu": .2}, sample]
        assert store.series_status("7")["status"] == "ready"
        assert len(calls) == 1 and calls[0] != threading.current_thread().name
    finally:
        store.configure_series_loader(None)
        pool.shutdown(wait=True)


@pytest.mark.parametrize("change", ["attempt", "shutdown"])
def test_inflight_restore_cannot_publish_into_another_attempt_or_after_shutdown(tmp_path, monkeypatch, change):
    store = Store(state_dir=str(tmp_path))
    store.apply_jobs([Job("7", "old", "cpu", "RUNNING", submit="old")])
    path = write_series(tmp_path, "7", [{"t": 1., "k": "live"}], store)
    pool = ThreadPoolExecutor(max_workers=1)
    store.configure_series_loader(pool.submit)
    try:
        with blocked_open(monkeypatch, path, "rb") as (entered, release, calls):
            store.series_view("7")
            assert entered.wait(2)
            if change == "attempt":
                store.apply_jobs([])
                store.apply_jobs([Job("7", "new", "cpu", "RUNNING", submit="new")])
            else:
                store.configure_series_loader(None)
            sample = {"t": 2., "k": "live"}
            store.record("7", sample)
            release.set()
        pool.shutdown(wait=True)
        assert list(store.series["7"]) == [sample]
        assert "7" not in store._series_loaded
    finally:
        store.configure_series_loader(None)
        pool.shutdown(wait=True)


def test_missing_file_is_checked_once_and_archive_queue_is_bounded(tmp_path):
    store = Store(state_dir=str(tmp_path))
    pending = []
    store.configure_series_loader(pending.append)
    for jid in map(str, range(archive.MAX_REQUESTS + 8)):
        store.series_view(jid)
    assert len(pending) == 1
    assert len(store._series_archive.pending) == archive.MAX_REQUESTS
    assert store.series_status(str(archive.MAX_REQUESTS))["status"] == "busy"
    pending.pop()()
    assert store.series_status("0")["status"] == "missing"
    for _ in range(20):
        assert store.series_view("0") == []
    assert not pending
    store.configure_series_loader(None)
    store.series_view("new")
    assert store.series_status("new")["status"] == "unavailable"


def test_directory_inventory_is_off_thread_and_cached(tmp_path, monkeypatch):
    write_series(tmp_path, "7", [{"t": 1.}])
    store = Store(state_dir=str(tmp_path))
    queued, calls = [], []
    original = archive.os.scandir
    def observed(path):
        calls.append(threading.current_thread().name)
        return original(path)
    monkeypatch.setattr(archive.os, "scandir", observed)
    store.configure_series_loader(queued.append)
    assert store.series_jobs_view() == []
    for _ in range(20):
        store.series_jobs_view()
    assert calls == [] and len(queued) == 1
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(queued.pop()).result(timeout=2)
    assert store.series_jobs_view() == ["7"]
    assert not queued and calls[0] != threading.current_thread().name


def test_persistence_queue_keeps_publication_order_during_competing_flushes(tmp_path, monkeypatch):
    store = Store(state_dir=str(tmp_path))
    path = Path(store._series_path("7"))
    first = {"t": 1.0005, "k": "live"}
    second = {"t": 1.001, "k": "gpu", "gpu": {"node:0": [50., 100., 1000.]}}
    with ThreadPoolExecutor(max_workers=2) as pool:
        with blocked_open(monkeypatch, path, "a") as (entered, release, calls):
            task = pool.submit(store.record, "7", first)
            assert entered.wait(2)
            # The second source publishes while the first writer is blocked.
            store.record("7", second, _persist=False)
            competing = pool.submit(store.flush_persistence)
            assert_store_available(store)
            assert list(store.series["7"]) == [first, second]
            release.set()
            task.result(timeout=2)
            competing.result(timeout=2)
    assert [json.loads(line) for line in path.read_text().splitlines()] == [first, second]
    assert not store._persist_queue


def test_reverse_tail_keeps_latest_valid_records_across_utf8_chunks_and_corruption(tmp_path, monkeypatch):
    monkeypatch.setattr(archive, "READ_CHUNK", 13)
    samples = [{"t": index, "k": "live", "label": "éλ"} for index in range(20)]
    path = write_series(tmp_path, "7", samples)
    with path.open("a") as stream:
        stream.write('not-json\n[1,2]\n{"incomplete":\n')
    result, status = archive.read_tail(path, 5)
    assert result == samples[-5:]
    assert status["status"] == "ready" and status["ignored"] == 3
    assert status["bytes"] < path.stat().st_size


@pytest.mark.parametrize("limit", ["bytes", "line"])
def test_bounded_restore_is_explicit_and_synchronous_export_upgrades_it(tmp_path, monkeypatch, limit):
    old = {"t": 1., "k": "live"}
    large = {"t": 2., "k": "live", "extra": "x" * 200}
    recent = {"t": 3., "k": "gpu"}

    monkeypatch.setattr(archive, "READ_CHUNK", 32)
    monkeypatch.setattr(archive, "COLD_READ_BYTES", 90 if limit == "bytes" else 4096)
    monkeypatch.setattr(archive, "COLD_LINE_BYTES", 1024 if limit == "bytes" else 64)
    store = Store(state_dir=str(tmp_path))
    write_series(tmp_path, "7", [old, large, recent], store)
    queued = []
    store.configure_series_loader(queued.append)
    store.series_view("7")
    queued.pop()()
    assert store.series_status("7")["status"] == "limited"
    assert store.series_view("7")[-1] == recent
    # Diagnostics are bounded, but an evicted limited notice must never turn
    # an explicitly requested full export into a silently partial one.
    for index in range(archive.MAX_STATUS + 1):
        store._series_archive._status(str(index + 1000), {"status": "ready"})
    assert store.series_status("7")["status"] == "limited"
    snap = store.snapshot()
    report = _ReportStore(store, snap, 2.5)
    assert report.series_of("7") == [old, large]
    assert store.series_status("7")["status"] == "limited"
    assert list(store.series["7"])[-1] == recent
    assert report._reader._series_archive.store is report._reader
    assert store.series_of("7") == [old, large, recent]
    assert store.series_status("7")["status"] == "ready"


def test_bad_timestamp_types_do_not_disable_worker_or_lose_other_jobs(tmp_path):
    store = Store(state_dir=str(tmp_path))
    write_series(tmp_path, "7", [{"t": [], "k": {}}, {"t": 2., "k": "live"}], store)
    write_series(tmp_path, "8", [{"t": 3., "k": "gpu"}], store)
    queue = []
    store.configure_series_loader(queue.append)
    store.series_view("7")
    store.series_view("8")
    queue.pop()()
    assert len(store.series_view("7")) == 2
    assert store.series_view("8") == [{"t": 3., "k": "gpu"}]
    assert not store._series_archive.worker


@pytest.mark.parametrize("blocked", ["series", "events"])
def test_independent_event_and_metric_writes_never_wait_for_each_other(tmp_path, monkeypatch, blocked):
    store = Store(state_dir=str(tmp_path))
    series_path, event_path = Path(store._series_path("7")), tmp_path / "events.jsonl"
    path = series_path if blocked == "series" else event_path
    first = (lambda: store.record("7", {"t": 1., "k": "live"})) if blocked == "series" else (lambda: store.event("queued", "queued 7"))
    second = (lambda: store.event("copy", "copied lines")) if blocked == "series" else (lambda: store.record("7", {"t": 2., "k": "gpu"}))
    with ThreadPoolExecutor(max_workers=2) as pool:
        with blocked_open(monkeypatch, path, "a") as (entered, release, calls):
            first_task = pool.submit(first)
            assert entered.wait(2)
            second_task = pool.submit(second)
            second_task.result(timeout=.5)
            assert_store_available(store)
            independent = event_path if blocked == "series" else series_path
            assert independent.exists() and independent.read_text()
            release.set()
            first_task.result(timeout=2)
    assert not store._persist_queue and not store._persist_event_queue


def test_special_saved_files_report_error_without_blocking_a_worker(tmp_path):
    import os
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO files are not supported on this platform")
    path = tmp_path / "metrics.jsonl"
    os.mkfifo(path)
    records, status = archive.read_tail(path, 4, max_bytes=1024, max_line=1024)
    assert records == []
    assert status["status"] == "error" and "regular file" in status["message"]
