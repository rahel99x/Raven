import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from raven import trace_protocol as P
from raven.trace_recorder import Recorder, register_trace


def make_trace(tmp_path, count=3, **options):
    recorder = Recorder(tmp_path, job_id="123", attempt="submit/start", **options)
    for n in range(count):
        recorder.emit("metric", {"value": n})
    recorder.close()
    return recorder


def test_real_roundtrip_has_checked_identity_and_durable_prefix(tmp_path):
    recorder = make_trace(tmp_path)
    result = P.read_trace(recorder.path)
    assert result["complete"]
    assert result["status"] == "COMPLETE"
    assert result["identity"]["pid"] == os.getpid()
    assert result["identity"]["process_start"] != "unknown"
    assert result["read_seq"] == 5
    assert not result["warnings"]
    assert P.discover_trace(tmp_path, "123", "submit/start") == [str(recorder.path)]
    assert not P.discover_trace(tmp_path, "123", "different")


def test_flush_makes_blocked_function_visible_without_another_event(tmp_path):
    with Recorder(tmp_path, flush_interval=.01) as recorder:
        recorder.emit("call", {"function": "blocked", "file": "app.py", "line": 1, "frame": "f"})
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            result = P.read_trace(recorder.path)
            if result["stacks"]:
                break
            threading.Event().wait(.01)
        assert result["status"] == "LIVE"
        assert result["stacks"][0]["frames"][0]["function"] == "blocked"
        assert not result["complete"]


def test_incremental_reader_keeps_stack_while_retaining_bounded_events(tmp_path):
    recorder = make_trace(tmp_path, count=40)
    reader = P.TraceReader(recorder.path, retain=10)
    result = reader.refresh(max_events=5)
    assert result["read_seq"] == 5 and not result["caught_up"]
    while not result["caught_up"]:
        result = reader.refresh(max_events=5)
    assert result["complete"] and result["read_seq"] == 42
    assert len(result["events"]) == 10 and result["evicted"] == 32
    assert reader.refresh()["read_seq"] == 42


def test_writer_concurrency_preserves_one_sequence(tmp_path):
    with Recorder(tmp_path, flush_events=16) as recorder:
        threads = [threading.Thread(target=lambda i=i: [recorder.emit("sample", {"i": i, "n": n}) for n in range(75)]) for i in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    result = P.read_trace(recorder.path)
    assert result["complete"] and result["read_seq"] == 452
    assert [e["seq"] for e in result["events"]] == list(range(1, 453))


def _rewrite(recorder, mutate):
    path = recorder.directory / "events.jsonl"
    records = [json.loads(line) for line in path.read_text().splitlines()]
    records = mutate(records)
    raw = b"".join(P.encoded(record) + b"\n" for record in records)
    path.write_bytes(raw)
    manifest = json.loads(recorder.path.read_text())
    manifest["committed_bytes"] = len(raw)
    recorder.path.write_text(json.dumps(manifest))


@pytest.mark.parametrize("mutation", ["checksum", "gap", "duplicate", "chain", "stream", "backwards"])
def test_committed_corruption_never_certifies_complete(tmp_path, mutation):
    recorder = make_trace(tmp_path)
    def alter(records):
        if mutation == "duplicate":
            records.insert(2, records[1])
        else:
            record = records[2]
            if mutation == "checksum":
                record["event"]["data"]["value"] = 900
            elif mutation == "gap":
                record["seq"] = 90
            elif mutation == "chain":
                record["prev"] = "a" * 64
            elif mutation == "stream":
                record["stream"] = "a" * 32
            elif mutation == "backwards":
                record["event"]["mono_ns"] = 0
            if mutation != "checksum":
                record["sha256"] = P.event_digest(record)
        return records
    _rewrite(recorder, alter)
    result = P.read_trace(recorder.path)
    assert result["status"] == "INVALID" and not result["complete"]


def test_uncommitted_crash_tail_is_not_parsed_or_certified(tmp_path):
    recorder = Recorder(tmp_path, flush_interval=60, flush_events=65536)
    recorder.emit("call", {"function": "f"})
    recorder.flush()
    recorder._flush_stop.set()
    recorder._flush_thread.join()
    os.write(recorder.fd, b'{"partial":')
    os.close(recorder.fd)
    recorder.fd = None
    result = P.read_trace(recorder.path)
    assert result["status"] == "LIVE" and not result["complete"]
    assert result["read_seq"] == 2
    assert any("uncommitted tail" in warning for warning in result["warnings"])


def test_storage_budget_stops_capture_explicitly(tmp_path):
    recorder = Recorder(tmp_path, max_bytes=4096, flush_events=1)
    accepted = [recorder.emit("sample", {"text": "x" * 800}) for _ in range(10)]
    recorder.close()
    result = P.read_trace(recorder.path)
    assert any(accepted) and not all(accepted)
    assert result["status"] == "PARTIAL" and not result["complete"]
    assert result["manifest"]["dropped"] > 0
    assert (recorder.directory / "events.jsonl").stat().st_size <= 4096


def test_replaced_journal_and_attempt_are_refused(tmp_path):
    recorder = make_trace(tmp_path)
    reader = P.TraceReader(recorder.path)
    reader.refresh(max_events=1)
    path = recorder.directory / "events.jsonl"
    new = recorder.directory / "other"
    new.write_bytes(path.read_bytes())
    new.replace(path)
    with pytest.raises(ValueError, match="replaced"):
        reader.refresh()
    manifest = json.loads(recorder.path.read_text())
    manifest["identity"]["attempt"] = "other"
    recorder.path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="identity changed"):
        reader.refresh()


def test_registration_forgery_and_escape_refused(tmp_path):
    recorder = make_trace(tmp_path)
    path = tmp_path / ".raven/traces/index.json"
    doc = json.loads(path.read_text())
    doc["records"][0]["path"] = "../manifest.json"
    path.write_text(json.dumps(doc))
    with pytest.raises(ValueError, match="escapes"):
        P.discover_trace(tmp_path, "123")


def test_symlink_and_fifo_refused_without_blocking(tmp_path):
    recorder = make_trace(tmp_path)
    link = tmp_path / "link"
    link.symlink_to(recorder.path)
    with pytest.raises(OSError):
        P.read_trace(link)
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(ValueError, match="regular"):
        P.read_trace(fifo)


@pytest.mark.parametrize("data", [{"text": "\x1b[2J"}, {"x": float("nan")}, {"x": "x" * 8193}, {"x": 1 << 80}])
def test_bad_metadata_never_reaches_terminal(tmp_path, data):
    with Recorder(tmp_path) as recorder:
        with pytest.raises(ValueError):
            recorder.emit("sample", data)


def test_snapshot_is_explicitly_not_complete_history(tmp_path):
    with Recorder(tmp_path, coverage={"mode": "snapshot", "scope": "Observed frames"}) as recorder:
        recorder.emit("stack_checkpoint", {"frames": [{"function": "f", "file": "a.py", "line": 4}]}, thread="1")
    result = P.read_trace(recorder.path)
    assert not result["complete"] and result["stacks"][0]["frames"][0]["function"] == "f"


@pytest.mark.skipif(not hasattr(os, "fork"), reason="POSIX fork")
def test_fork_never_appends_child_events_to_parent_stream(tmp_path):
    # Deliberately exercise the recorder's fork guard without inheriting other
    # test modules' worker threads or locks. Forking arbitrary threaded Python
    # applications is outside the supported capture contract.
    script = '''
import os
import sys
from raven.trace_protocol import read_trace
from raven.trace_recorder import Recorder

with Recorder(sys.argv[1]) as recorder:
    pid = os.fork()
    if pid == 0:
        accepted = recorder.emit("child", {})
        os._exit(1 if accepted else 0)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    assert recorder.emit("parent", {})
result = read_trace(recorder.path)
assert result["complete"]
assert any(event["kind"] == "parent" for event in result["events"])
assert all(event["kind"] != "child" for event in result["events"])
'''
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_cancelled_reader_preserves_progress(tmp_path):
    recorder = make_trace(tmp_path, count=30)
    reader = P.TraceReader(recorder.path)
    cancel = threading.Event()
    cancel.set()
    result = reader.refresh(cancel=cancel)
    assert result["read_seq"] == 0 and not result["complete"]
    assert reader.refresh()["complete"]


def test_explicit_source_loss_survives_clean_close(tmp_path):
    with Recorder(tmp_path) as recorder:
        recorder.emit("loss", {"count": 7})
    result = P.read_trace(recorder.path)
    assert not result["complete"]
    assert any("Source reports" in warning for warning in result["warnings"])


def test_manifest_terminal_controls_are_rejected(tmp_path):
    recorder = make_trace(tmp_path)
    value = json.loads(recorder.path.read_text())
    value["coverage"]["scope"] = "bad\x1b[2J"
    recorder.path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="printable"):
        P.read_trace(recorder.path)


def test_closed_recorders_are_not_retained_by_fork_registry(tmp_path):
    from raven.trace_recorder import _RECORDERS
    recorder = make_trace(tmp_path)
    assert recorder not in _RECORDERS


def test_array_job_identity_uses_task_not_parent(tmp_path, monkeypatch):
    monkeypatch.setenv("SLURM_JOB_ID", "456")
    monkeypatch.setenv("SLURM_ARRAY_JOB_ID", "123")
    monkeypatch.setenv("SLURM_ARRAY_TASK_ID", "7")
    with Recorder(tmp_path) as recorder:
        pass
    assert P.read_trace(recorder.path)["identity"]["job_id"] == "123_7"


def test_missing_old_registration_does_not_hide_current_trace(tmp_path):
    old = make_trace(tmp_path)
    with Recorder(tmp_path, job_id="123", attempt="new-attempt") as current:
        pass
    old.path.unlink()
    assert P.discover_trace(tmp_path, "123") == [str(current.path)]


@pytest.mark.parametrize('data', [{}, {'text': 'unicode λ and ",\"stream\": fake'}, {'nested': {'sha256': 'not the envelope', 'stream': 'other'}}])
def test_single_serialization_keeps_exact_v1_bytes_and_hash(data):
    record = {'schema': 'raven.trace-event/v1', 'stream': 'abcdefgh', 'seq': 1, 'prev': '0' * 64,
              'event': {'kind': 'note', 'mono_ns': 15, 'thread': 't', 'data': data}}
    raw, digest = P.encode_event_record(record)
    assert digest == P.event_digest(record)
    assert raw == P.encoded(dict(record, sha256=digest)) + b'\n'
    assert 'sha256' not in record
