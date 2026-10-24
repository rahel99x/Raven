"""Actual bounded incident retention, journal recovery and process isolation."""

import json
import os
from pathlib import Path
import threading
import time

import pytest

from raven.tracing import activity_trigger as T
from raven.tracing.protocol import discover_trace, read_json, read_trace


def capture(project, **options):
    return T.TriggeredCapture(project, job_id="42", attempt="one", **options)


def metadata(snapshot):
    return [row for row in snapshot["events"] if row["kind"] == "phase"]


def test_trigger_publishes_bounded_original_pre_history_and_live_post_events(tmp_path):
    started = time.monotonic_ns() - 1_000_000
    with capture(tmp_path, pre_events=3, post_events=2) as recorder:
        value = {"name": "input", "nested": {"value": "before"}}
        recorder.emit("phase", value, mono_ns=started)
        value["nested"]["value"] = "mutated"
        for number in range(1, 5):
            recorder.emit("phase", {"name": str(number)}, mono_ns=started + number)
        assert recorder.trigger("Residual crossed the declared threshold")
        assert not recorder.trigger("Duplicate incident")
        saved = read_json(recorder.receipt_path)
        assert saved["retained_pre_events"] == 3
        assert saved["evicted_pre_events"] == 2
        assert saved["status"] == "recording"
        snapshot = read_trace(saved["manifest"])
        assert [row["data"]["name"] for row in metadata(snapshot)] == ["2", "3", "4"]
        assert [row["mono_ns"] for row in metadata(snapshot)] == [
            started + 2,
            started + 3,
            started + 4,
        ]
        recorder.emit("phase", {"name": "after-1"})
        recorder.emit("phase", {"name": "after-2"})
        assert recorder.receipt["stop_reason"] == "post-event-budget"
        assert not recorder.emit("phase", {"name": "not-recorded"})
    snapshot = read_trace(saved["manifest"])
    assert snapshot["status"] == "COMPLETE" and snapshot["caught_up"]
    assert [row["data"]["name"] for row in metadata(snapshot)] == [
        "2",
        "3",
        "4",
        "after-1",
        "after-2",
    ]
    assert discover_trace(tmp_path, "42", "one") == [saved["manifest"]]
    assert read_json(recorder.receipt_path)["retained_post_events"] == 2


def test_pre_metadata_is_a_snapshot_and_byte_budget_is_hard(tmp_path):
    with capture(tmp_path, pre_bytes=1024) as recorder:
        data = {"name": "snapshot", "nested": {"value": "before"}}
        assert recorder.emit("phase", data)
        data["nested"]["value"] = "later"
        assert recorder.trigger("inspect")
    recorded = metadata(read_trace(recorder.receipt["manifest"]))
    assert recorded[0]["data"]["nested"]["value"] == "before"
    other = tmp_path / "other"
    other.mkdir()
    with capture(other, pre_bytes=1024) as recorder:
        assert not recorder.emit("phase", {"name": "x" * 2000})
        assert recorder.receipt["evicted_pre_events"] == 1
        assert recorder._bytes == 0
        assert recorder.trigger("still usable")
    assert not metadata(read_trace(recorder.receipt["manifest"]))


def test_idle_pre_window_is_evicted_at_trigger_without_manufactured_intervals(tmp_path):
    with capture(tmp_path, pre_seconds=0.01) as recorder:
        recorder.emit(
            "phase", {"name": "old"}, mono_ns=time.monotonic_ns() - 100_000_000
        )
        assert recorder.trigger("new incident")
    assert recorder.receipt["retained_pre_events"] == 0
    assert recorder.receipt["evicted_pre_events"] == 1
    assert not metadata(read_trace(recorder.receipt["manifest"]))


def test_never_triggered_capture_saves_private_receipt_without_persisting_ring(
    tmp_path,
):
    with capture(tmp_path) as recorder:
        recorder.emit("phase", {"name": "not persisted"})
    saved = read_json(recorder.receipt_path)
    assert saved["status"] == "never-triggered" and saved["discarded_pre_events"] == 1
    assert saved["manifest"] is None and discover_trace(tmp_path, "42", "one") == []
    assert not list((tmp_path / ".raven" / "traces").glob("*/manifest.json"))
    assert recorder.receipt_path.stat().st_mode & 0o777 == 0o600
    assert recorder.receipt_path.parent.stat().st_mode & 0o777 == 0o700
    before = json.dumps(saved)
    recorder.close()
    assert json.dumps(read_json(recorder.receipt_path)) == before


def test_post_time_deadline_closes_only_capture_while_application_continues(tmp_path):
    with capture(tmp_path, post_seconds=0.02) as recorder:
        assert recorder.trigger("deadline test")
        limit = time.monotonic() + 2
        while recorder.receipt["status"] == "recording" and time.monotonic() < limit:
            time.sleep(0.005)
        assert recorder.receipt["stop_reason"] == "post-time-budget"
        assert read_trace(recorder.receipt["manifest"])["status"] == "COMPLETE"
        assert not recorder.emit("phase", {"name": "application still running"})
        scientific_result = sum(range(100))
    assert scientific_result == 4950


def test_deadline_receipt_is_not_terminal_until_journal_close_is_durable(
    tmp_path, monkeypatch
):
    entered, release = threading.Event(), threading.Event()
    with capture(tmp_path, post_seconds=60) as recorder:
        assert recorder.trigger("controlled deadline")
        receipt_alias = recorder.receipt
        original_close = recorder._recorder.close

        def held_close():
            entered.set()
            assert release.wait(5), "Test did not release the journal close barrier"
            original_close()

        monkeypatch.setattr(recorder._recorder, "close", held_close)
        deadline = threading.Thread(target=recorder._expire)
        deadline.start()
        try:
            assert entered.wait(2), "Deadline did not enter journal shutdown"
            # Deliberately read the public dict without the owner's lock, as a
            # monitoring caller does. No terminal success may precede fsync.
            assert recorder.receipt["status"] == "recording"
            assert "stop_reason" not in recorder.receipt
            assert read_json(recorder.receipt_path)["status"] == "recording"
            assert read_trace(recorder.receipt["manifest"])["status"] == "LIVE"
        finally:
            release.set()
            deadline.join(timeout=2)
        assert not deadline.is_alive()
        assert receipt_alias is recorder.receipt
        assert receipt_alias["status"] == "complete"
        assert receipt_alias["stop_reason"] == "post-time-budget"
        assert read_json(recorder.receipt_path)["status"] == "complete"
        assert read_trace(recorder.receipt["manifest"])["status"] == "COMPLETE"


def test_public_terminal_receipt_waits_for_its_own_atomic_publication(
    tmp_path, monkeypatch
):
    from raven import reporting

    entered, release = threading.Event(), threading.Event()
    with capture(tmp_path, post_seconds=60) as recorder:
        recorder.trigger("controlled receipt publication")
        publish = reporting._atomic_json

        def held_publish(path, value, **kwargs):
            if Path(path) == recorder.receipt_path and value["status"] == "complete":
                entered.set()
                assert release.wait(5), "Test did not release receipt publication"
            return publish(path, value, **kwargs)

        monkeypatch.setattr(reporting, "_atomic_json", held_publish)
        deadline = threading.Thread(target=recorder._expire)
        deadline.start()
        try:
            assert entered.wait(2), "Shutdown did not publish its final receipt"
            assert recorder.receipt["status"] == "recording"
            assert "stop_reason" not in recorder.receipt
            assert read_json(recorder.receipt_path)["status"] == "recording"
            assert read_trace(recorder.receipt["manifest"])["status"] == "COMPLETE"
        finally:
            release.set()
            deadline.join(timeout=2)
        assert not deadline.is_alive()
        assert recorder.receipt["status"] == "complete"
        assert read_json(recorder.receipt_path)["status"] == "complete"


@pytest.mark.parametrize(
    "footer,status", [("PARTIAL", "partial"), ("LIMIT", "partial"), ("ERROR", "error")]
)
def test_nonraising_incomplete_recorder_close_never_certifies_complete_capture(
    tmp_path, monkeypatch, footer, status
):
    with capture(tmp_path) as recorder:
        recorder.trigger("incomplete footer")
        original_close = recorder._recorder.close

        def incomplete_close():
            recorder._recorder.state = footer
            return original_close()

        monkeypatch.setattr(recorder._recorder, "close", incomplete_close)
        recorder.close()
        assert recorder._recorder.fd is None
        assert recorder.receipt["status"] == status
        assert recorder.receipt["capture_state"] == footer
        assert any(footer in error for error in recorder.receipt["errors"])
        assert read_json(recorder.receipt_path)["status"] == status
        assert read_trace(recorder.receipt["manifest"])["status"] != "COMPLETE"
        assert not recorder.emit("phase", {"name": "science continues"})
        scientific_result = sum(range(10))
    assert scientific_result == 45


def test_close_flush_error_preserves_scientific_exception_and_actual_prefix(
    tmp_path, monkeypatch
):
    with pytest.raises(LookupError, match="original scientific outcome"):
        with capture(tmp_path) as recorder:
            recorder.trigger("failed close flush")

            def failed_flush():
                raise OSError("fixture final journal fsync failure")

            monkeypatch.setattr(recorder._recorder, "flush", failed_flush)
            raise LookupError("original scientific outcome")
    assert recorder._recorder.fd is None
    # CLOSED in memory is not sufficient when its durable flush raised.
    assert recorder.receipt["capture_state"] == "CLOSED"
    assert recorder.receipt["status"] == "error"
    assert any("fsync failure" in error for error in recorder.receipt["errors"])
    assert read_json(recorder.receipt_path)["status"] == "error"
    assert read_trace(recorder.receipt["manifest"])["status"] == "LIVE"


def test_final_receipt_failure_does_not_publish_in_memory_success(
    tmp_path, monkeypatch
):
    from raven import reporting

    with capture(tmp_path) as recorder:
        recorder.trigger("failed final receipt")

        def failed_publish(*args, **kwargs):
            raise OSError("fixture final receipt unavailable")

        monkeypatch.setattr(reporting, "_atomic_json", failed_publish)
        recorder.close()
        assert recorder.receipt["status"] == "error"
        assert any(
            "receipt unavailable" in error for error in recorder.receipt["errors"]
        )
        assert read_json(recorder.receipt_path)["status"] == "recording"
        assert read_trace(recorder.receipt["manifest"])["status"] == "COMPLETE"
        scientific_result = 7 * 6
    assert scientific_result == 42


def test_journal_budget_stops_capture_without_registering_partial_as_complete(tmp_path):
    with capture(tmp_path, max_bytes=4096) as recorder:
        for number in range(10):
            recorder.emit("phase", {"name": str(number), "metadata": "x" * 500})
        assert not recorder.trigger("more than output quota")
        assert recorder.receipt["status"] == "partial"
        assert not recorder.emit("phase", {"name": "science continues"})
    snapshot = read_trace(recorder.receipt["manifest"])
    assert snapshot["status"] != "COMPLETE"
    assert discover_trace(tmp_path, "42", "one") == []


def test_storage_failure_does_not_replace_scientific_exception(tmp_path, monkeypatch):
    from raven.tracing import capture as C

    def failed(*_):
        raise OSError("fixture disk full")

    with pytest.raises(LookupError, match="original science failure"):
        with capture(tmp_path) as recorder:
            recorder.emit("phase", {"name": "before"})
            monkeypatch.setattr(C, "register_trace", failed)
            assert recorder.trigger("registration unavailable")
            assert not recorder.receipt["registered"]
            assert "disk full" in recorder.receipt["registration_error"]
            assert (
                recorder.receipt["registration_argv"][-1]
                == recorder.receipt["manifest"]
            )
            raise LookupError("original science failure")
    assert read_trace(recorder.receipt["manifest"])["status"] == "COMPLETE"
    assert recorder.receipt["stop_reason"] == "closed-by-user"


def test_post_write_error_is_saved_and_does_not_stop_application(tmp_path, monkeypatch):
    with capture(tmp_path) as recorder:
        recorder.trigger("test write failure")

        def failed(*args, **kwargs):
            raise OSError("fixture write failure")

        monkeypatch.setattr(recorder._recorder, "emit", failed)
        assert not recorder.emit("phase", {"name": "failed"})
        assert recorder.receipt["status"] == "error"
        assert any("write failure" in error for error in recorder.receipt["errors"])
        assert not recorder.emit("phase", {"name": "application still runs"})


def test_thread_lifetimes_differ_even_when_reported_native_tid_is_reused(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(T.threading, "get_native_id", lambda: 7)
    with capture(tmp_path) as recorder:
        for number in range(2):
            thread = threading.Thread(
                target=lambda: recorder.emit("phase", {"name": "worker"})
            )
            thread.start()
            thread.join(timeout=2)
            assert not thread.is_alive()
        assert recorder.trigger("workers")
    rows = metadata(read_trace(recorder.receipt["manifest"]))
    assert len(rows) == 2 and rows[0]["thread"] != rows[1]["thread"]


def test_inherited_capture_cannot_write_parent_journal_or_close_parent_timer(
    tmp_path, monkeypatch
):
    with capture(tmp_path) as recorder:
        recorder.trigger("owner")
        original_pid = os.getpid()
        with monkeypatch.context() as patch:
            patch.setattr(T.os, "getpid", lambda: original_pid + 1)
            assert not recorder.emit("phase", {"name": "foreign"})
            assert not recorder.trigger("foreign")
            recorder.close()
            assert not recorder._closed
        assert recorder.emit("phase", {"name": "parent"})
    assert [
        row["data"]["name"]
        for row in metadata(read_trace(recorder.receipt["manifest"]))
    ] == ["parent"]


def test_spans_keep_missing_prefix_explicit_and_preserve_job_exception(tmp_path):
    with pytest.raises(ArithmeticError, match="numerical failure"):
        with capture(tmp_path, pre_events=1) as recorder:
            with recorder.span("solve"):
                recorder.emit("phase", {"name": "begin evicted"})
                assert recorder.trigger("incident in solve")
                raise ArithmeticError("numerical failure")
    rows = read_trace(recorder.receipt["manifest"])["events"]
    assert not any(row["kind"] == "span_start" for row in rows)
    assert sum(row["kind"] == "span_end" for row in rows) == 1
    assert recorder.receipt["evicted_pre_events"] == 1


@pytest.mark.parametrize(
    "option",
    [
        {"pre_events": 0},
        {"pre_bytes": 1},
        {"post_seconds": float("nan")},
        {"post_events": True},
        {"max_bytes": 1 << 40},
    ],
)
def test_invalid_budgets_fail_before_creating_artifacts(tmp_path, option):
    with pytest.raises(ValueError):
        capture(tmp_path, **option)
    assert not (tmp_path / ".raven").exists()


def test_lifecycle_and_event_order_are_explicit(tmp_path):
    recorder = capture(tmp_path)
    with pytest.raises(RuntimeError):
        recorder.emit("phase")
    with recorder:
        now = time.monotonic_ns()
        recorder.emit("phase", {"name": "now"}, mono_ns=now)
        with pytest.raises(ValueError, match="backwards"):
            recorder.emit("phase", mono_ns=now - 1)
    with pytest.raises(RuntimeError):
        recorder.__enter__()


def test_two_minute_incident_example_runs_a_fast_real_smoke(tmp_path, capsys):
    import runpy

    example = (
        Path(__file__).resolve().parents[1]
        / "examples/trace-lab/triggered_incident_lab.py"
    )
    entry = runpy.run_path(str(example))["main"]
    assert (
        entry(
            [
                "--project",
                str(tmp_path),
                "--seconds",
                "0",
                "--steps",
                "8",
                "--size",
                "16",
                "--job-id",
                "42",
                "--attempt",
                "one",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert result["steps"] == 8 and result["energy"] > 0
    assert (
        result["capture"]["trigger"]["reason"]
        == "Injected model perturbation at step 4"
    )
    snapshot = read_trace(result["capture"]["manifest"])
    assert snapshot["status"] == "COMPLETE"
    assert sum(row["kind"] == "span_start" for row in snapshot["events"]) == 8
