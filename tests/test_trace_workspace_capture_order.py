"""Concurrent callbacks must not commit a backwards verified journal."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from raven.tracing.capture import Recorder
from raven.tracing.protocol import read_trace


def test_slow_cpu_observation_cannot_reorder_other_threads_journal_timestamps(tmp_path):
    slow_observing, release_slow, fast_done = (threading.Event() for _ in range(3))

    class Observer:
        def observe(self):
            if threading.current_thread().name == "slow-observer":
                slow_observing.set()
                assert release_slow.wait(2), "test observation release timed out"

    with Recorder(
        tmp_path,
        job_id="42",
        attempt="concurrent",
        journal_format="blocks",
        background_flush=False,
    ) as recorder:
        recorder._cpu_placement = Observer()

        def enqueue(name):
            assert recorder.enqueue("note", {"name": name}, thread=name)
            if name == "fast":
                fast_done.set()

        slow = threading.Thread(target=enqueue, args=("slow",), name="slow-observer")
        fast = threading.Thread(target=enqueue, args=("fast",), name="fast-observer")
        slow.start()
        assert slow_observing.wait(1)
        fast.start()
        # Before the fix fast can commit ahead of the older captured stamp.
        # A correct serialized capture may block fast; release either case.
        fast_done.wait(0.05)
        release_slow.set()
        slow.join(1)
        fast.join(1)
        assert not slow.is_alive() and not fast.is_alive()
    result = read_trace(recorder.path)
    assert result["status"] == "COMPLETE", result["warnings"]
    assert {
        row["data"].get("name") for row in result["events"] if row["kind"] == "note"
    } == {"slow", "fast"}
    stamps = [row["mono_ns"] for row in result["events"]]
    assert stamps == sorted(stamps)


def test_direct_span_cannot_overtake_an_already_detached_callback_batch(
    tmp_path, monkeypatch
):
    detached, release, direct_started = (threading.Event() for _ in range(3))
    with Recorder(
        tmp_path,
        job_id="42",
        attempt="mixed",
        journal_format="blocks",
        background_flush=False,
    ) as recorder:
        assert recorder.enqueue("note", {"name": "older"}, thread="callback")
        write = recorder._write_frames

        def delayed(pending):
            if threading.current_thread().name == "drainer":
                detached.set()
                assert release.wait(2)
            return write(pending)

        monkeypatch.setattr(recorder, "_write_frames", delayed)
        drainer = threading.Thread(target=recorder._drain_frames, name="drainer")
        drainer.start()
        assert detached.wait(1)

        def direct():
            direct_started.set()
            with recorder.span("direct-span"):
                pass

        writer = threading.Thread(target=direct)
        writer.start()
        assert direct_started.wait(1)
        assert writer.is_alive()  # It must wait for the owned writer, not overtake it.
        release.set()
        drainer.join(2)
        writer.join(2)
        assert not drainer.is_alive() and not writer.is_alive()
    result = read_trace(recorder.path)
    assert result["status"] == "COMPLETE", result["warnings"]
    kinds = [row["kind"] for row in result["events"]]
    assert kinds.index("note") < kinds.index("span_start") < kinds.index("span_end")


def test_callbacks_accepted_during_direct_prefix_encoding_follow_its_reserved_timestamp(
    tmp_path, monkeypatch
):
    encoding, accepted = threading.Event(), threading.Event()
    with Recorder(
        tmp_path,
        job_id="42",
        attempt="boundary",
        journal_format="blocks",
        background_flush=False,
    ) as recorder:
        assert recorder.enqueue("note", {"name": "older"}, thread="older")
        write = recorder._write_frames
        intercepted = False

        def delayed(pending):
            nonlocal intercepted
            if pending and not intercepted:
                intercepted = True
                encoding.set()
                assert accepted.wait(2), (
                    "encoding held the queue lock or lost callback acceptance"
                )
            return write(pending)

        monkeypatch.setattr(recorder, "_write_frames", delayed)

        def enqueue_after_boundary():
            assert encoding.wait(1)
            assert recorder.enqueue("note", {"name": "newer"}, thread="newer")
            accepted.set()

        callback = threading.Thread(target=enqueue_after_boundary)
        callback.start()
        assert recorder.emit("note", {"name": "direct"}, thread="direct")
        callback.join(2)
        assert not callback.is_alive()
    result = read_trace(recorder.path)
    assert result["status"] == "COMPLETE", result["warnings"]
    assert [
        row["data"]["name"] for row in result["events"] if row["kind"] == "note"
    ] == ["older", "direct", "newer"]
    stamps = [row["mono_ns"] for row in result["events"]]
    assert stamps == sorted(stamps)


@pytest.mark.parametrize("journal_format", ["events", "blocks"])
def test_fsync_and_encoding_never_hold_callback_queue_lock(
    tmp_path, monkeypatch, journal_format
):
    with Recorder(
        tmp_path,
        job_id="42",
        attempt="lock-order",
        journal_format=journal_format,
        background_flush=False,
    ) as recorder:
        original = os.fsync

        def fsync(fd):
            if fd == recorder.fd:
                assert recorder._queue_lock.acquire(blocking=False), (
                    "fsync held callback queue lock"
                )
                recorder._queue_lock.release()
            return original(fd)

        monkeypatch.setattr(os, "fsync", fsync)
        for n in range(128):
            assert recorder.enqueue("note", {"name": str(n)}, thread="callback")
        recorder.flush()
    assert read_trace(recorder.path)["status"] == "COMPLETE"


@pytest.mark.parametrize("legacy", [True, False])
def test_actual_two_thread_capture_with_cpu_points_and_direct_spans_is_strictly_ordered(
    tmp_path, legacy
):
    if not legacy and sys.version_info < (3, 12):
        pytest.skip("sys.monitoring requires CPython3.12+")
    script = tmp_path / "two_threads.py"
    script.write_text(
        "import threading\n"
        "barrier=threading.Barrier(2);answers=[]\n"
        "def leaf(value): return (value*17+3)%101\n"
        "def worker(index):\n"
        " barrier.wait()\n"
        " total=0\n"
        " for phase in range(8):\n"
        "  with recorder.span('cooperative-'+str(index)):\n"
        "   total+=sum(leaf(value) for value in range(64))\n"
        " answers.append(total)\n"
        "threads=[threading.Thread(target=worker,args=(i,)) for i in range(2)]\n"
        "for thread in threads:thread.start()\n"
        "for thread in threads:thread.join()\n"
        "assert answers==[sum(leaf(value) for value in range(64))*8]*2\n"
    )
    program = (
        "import json,runpy,sys\nfrom pathlib import Path\n"
        "from raven.tracing.capture import Recorder,PythonCapture\n"
        "root=Path(sys.argv[1]);recorder=Recorder(root,job_id='42',attempt='threads',journal_format='blocks',cpu_placement=True,flush_interval=.01)\n"
        f"capture=PythonCapture(recorder,legacy={legacy!r},scope='project',roots=[root])\n"
        "try:\n capture.start();runpy.run_path(str(root/'two_threads.py'),init_globals={'recorder':recorder})\n"
        "finally:\n capture.stop();recorder.close()\n"
        "print(json.dumps({'path':str(recorder.path)}))\n"
    )
    environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    result = subprocess.run(
        [sys.executable, "-c", program, str(tmp_path)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    value = read_trace(json.loads(result.stdout)["path"])
    assert value["status"] == "COMPLETE", value["warnings"]
    assert value["manifest"]["dropped"] == 0
    times = [row["mono_ns"] for row in value["events"]]
    assert times == sorted(times)
    assert sum(row["kind"] == "span_start" for row in value["events"]) == 16
    assert sum(row["kind"] == "span_end" for row in value["events"]) == 16


@pytest.mark.parametrize(
    "malformed",
    [
        {"frame": "bad", "function": "bad\x1b", "file": "science.py", "line": 1},
        {"frame": [], "function": "bad", "file": "science.py", "line": 1},
        {"frame": "missing-fields"},
    ],
)
def test_queued_metadata_fault_preserves_valid_science_and_committed_prefix(
    tmp_path, malformed
):
    with Recorder(
        tmp_path,
        job_id="42",
        attempt="invalid-queue",
        journal_format="blocks",
        background_flush=False,
    ) as recorder:
        assert recorder.enqueue("note", {"name": "verified-before"}, thread="t")
        assert recorder.enqueue("call", malformed, thread="t")
        assert recorder.enqueue("note", {"name": "unobserved-after"}, thread="t")
        outcome = []
        with recorder.span("scientific-span"):
            outcome.append(6 * 7)
        assert outcome == [42]
        assert recorder.state == "ERROR"
        assert recorder.dropped == 2
        assert not recorder.enqueue(
            "note", {"name": "rejected-after-error"}, thread="t"
        )
        prefix = read_trace(recorder.path)
        assert prefix["status"] != "INVALID", prefix["warnings"]
        assert prefix["complete"] is False
        assert prefix["manifest"]["state"] == "ERROR"
        assert any(
            "metadata failed validation" in value for value in prefix["warnings"]
        )
        assert [
            row["data"].get("name") for row in prefix["events"] if row["kind"] == "note"
        ] == ["verified-before"]
    final = read_trace(recorder.path)
    assert final["manifest"]["dropped"] == 2
    assert final["read_seq"] == final["manifest"]["committed_seq"]


def test_direct_caller_validation_is_not_hidden_by_callback_fault_containment(tmp_path):
    with Recorder(
        tmp_path, job_id="42", attempt="direct-validation", background_flush=False
    ) as recorder:
        assert recorder.enqueue("note", {"name": "queued"}, thread="t")
        with pytest.raises(ValueError, match="printable"):
            recorder.emit("note", {"name": "invalid\x1b"})
        assert recorder.state == "OPEN"
        assert recorder.dropped == 0
        assert recorder.emit("note", {"name": "valid-direct"})
    final = read_trace(recorder.path)
    assert final["status"] == "COMPLETE"
    assert [
        row["data"].get("name") for row in final["events"] if row["kind"] == "note"
    ] == ["queued", "valid-direct"]


def test_writer_owned_metadata_failure_flushes_without_waiting_on_itself(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    with Recorder(
        tmp_path,
        job_id="42",
        attempt="failure-flush",
        journal_format="blocks",
        background_flush=False,
    ) as recorder:
        recorder._async_frames = True
        recorder._flush_thread = SimpleNamespace(is_alive=lambda: True)

        def no_wait(*args, **kwargs):
            pytest.fail(
                "writer-held failure waited for another writer needing its own lock"
            )

        monkeypatch.setattr(recorder._flush_complete, "wait", no_wait)
        assert recorder.enqueue("call", {"frame": "missing-fields"}, thread="t")
        try:
            assert recorder.emit("note", {"name": "valid-scientific-point"}) is False
            assert recorder.state == "ERROR"
            assert read_trace(recorder.path)["manifest"]["state"] == "ERROR"
        finally:
            recorder._flush_thread = None
