"""Real PTY and IPC acceptance checks; no Slurm daemon or outer terminal needed."""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import struct
import sys
import time

import pytest

from tower.terminal_service import Decoder, MAX_EVENT, MAX_FRAME, TerminalService, dimensions, frame


def wait_for(service, predicate, timeout=6):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        service.poll()
        if predicate():
            return
        time.sleep(.005)
    pytest.fail(f"Terminal condition timed out: {service.sessions!r}; {service.error}")


def screen(service, sid):
    return "\n".join("".join(segment[0] for segment in row) for row in service.snapshot(sid).get("lines", []))


@pytest.fixture
def service():
    pytest.importorskip("pyte")
    with TerminalService() as value:
        yield value


@pytest.mark.parametrize("cut", range(1, 30))
def test_ipc_framing_handles_every_header_and_payload_boundary(cut):
    expected = [{"op": "hello", "text": "世界\u001b\n"}, {"op": "next", "n": 4}]
    data = b"".join(frame(value) for value in expected)
    decoder, actual = Decoder(), []
    for i in range(0, len(data), cut):
        decoder.feed(data[i:i + cut])
        actual.extend(decoder.take())
    assert actual == expected
    assert not decoder.buffer


def test_ipc_rejects_oversize_before_allocating_payload():
    decoder = Decoder()
    decoder.feed(struct.pack("!I", MAX_FRAME + 1))
    with pytest.raises(ValueError, match="exceeds"):
        decoder.take()


@pytest.mark.parametrize("value", [[], 42, None, "wrong"])
def test_ipc_requires_object_messages(value):
    data = json.dumps(value).encode()
    decoder = Decoder()
    decoder.feed(struct.pack("!I", len(data)) + data)
    with pytest.raises(ValueError, match="object"):
        decoder.take()


def test_decoder_budget_keeps_remaining_complete_frames():
    decoder = Decoder()
    decoder.feed(b"".join(frame({"n": n}) for n in range(90)))
    assert [m["n"] for m in decoder.take(40)] == list(range(40))
    assert [m["n"] for m in decoder.take(50)] == list(range(40, 90))


@pytest.mark.parametrize("size,expected", [((0, 0), (1, 2)), ((9999, 9999), (160, 400)), ((24, 80), (24, 80))])
def test_geometry_is_bounded(size, expected):
    assert dimensions(*size) == expected


@pytest.mark.parametrize("argv", [[], "bash", [""], [None], ["bad\0arg"]])
def test_invalid_launch_never_starts_broker(argv):
    service = TerminalService()
    with pytest.raises(ValueError):
        service.open(argv)
    assert service._process is None


@pytest.mark.parametrize("env", [{"BAD=KEY": "a"}, {"KEY": "\0"}, {"KEY": 2}])
def test_invalid_environment_never_starts_broker(env):
    service = TerminalService()
    with pytest.raises(ValueError):
        service.open(["/bin/sh"], env=env)
    assert service._process is None


def test_child_has_real_controlling_terminal_and_correct_session(service):
    program = "import os; print('PTYCHECK',*[os.isatty(n) for n in range(3)],os.getsid(0)==os.getpid(),os.tcgetpgrp(0)==os.getpgrp()); fd=os.open('/dev/tty',os.O_RDWR);os.close(fd)"
    sid = service.open([sys.executable, "-c", program], rows=6, cols=100)
    wait_for(service, lambda: service.snapshot(sid).get("status") == "exited")
    assert "PTYCHECK True True True True True" in screen(service, sid)
    assert service.snapshot(sid)["exit_code"] == 0


def test_source_checkout_launcher_and_child_work_from_unrelated_cwd(service, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sid = service.open([sys.executable, "-c", "import os; print('CWD',os.getcwd())"], cwd=tmp_path, cols=160)
    wait_for(service, lambda: service.snapshot(sid).get("status") == "exited")
    assert "CWD " + str(tmp_path) in screen(service, sid)


def test_environment_factory_not_persisted_in_metadata(service):
    calls = []
    def environment(sid):
        calls.append(sid)
        return {"TOWER_TEST_PRIVATE": "private-value"}
    sid = service.open([sys.executable, "-c", "import os; print('ENVOK',os.getenv('TOWER_TEST_PRIVATE')=='private-value')"], env_factory=environment)
    wait_for(service, lambda: service.snapshot(sid).get("status") == "exited")
    assert calls == [sid]
    assert "ENVOK True" in screen(service, sid)
    assert "private-value" not in repr(service.snapshot(sid))


def test_shell_retains_cwd_and_state_between_requests(service, tmp_path):
    sid = service.open(["/bin/bash", "--noprofile", "--norc", "-i"], cwd=tmp_path, env={"PS1": "READY> "})
    wait_for(service, lambda: "READY> " in screen(service, sid))
    assert service.send(sid, {"type": "text", "text": "TOWERTEST=42\r"})
    assert service.send(sid, {"type": "text", "text": "printf 'VALUE:%s\\n' \"$TOWERTEST\"\r"})
    wait_for(service, lambda: "VALUE:42" in screen(service, sid))
    assert service.snapshot(sid)["status"] == "running"


def test_ctrl_c_interrupts_foreground_command_without_killing_shell(service):
    sid = service.open(["/bin/bash", "--noprofile", "--norc", "-i"], env={"PS1": "READY> "})
    wait_for(service, lambda: "READY> " in screen(service, sid))
    service.send(sid, {"type": "text", "text": "sleep 30\r"})
    time.sleep(.1)
    service.send(sid, {"type": "key", "key": "ctrl-c"})
    service.send(sid, {"type": "text", "text": "printf 'STILL_%s\\n' alive\r"})
    wait_for(service, lambda: "STILL_alive" in screen(service, sid))
    assert service.snapshot(sid)["status"] == "running"


def test_ctrl_z_and_background_job_control(service):
    sid = service.open(["/bin/bash", "--noprofile", "--norc", "-i"], env={"PS1": "READY> "})
    wait_for(service, lambda: "READY> " in screen(service, sid))
    service.send(sid, {"type": "text", "text": "sleep 30\r"})
    time.sleep(.1)
    service.send(sid, {"type": "key", "key": "ctrl-z"})
    wait_for(service, lambda: "Stopped" in screen(service, sid))
    service.send(sid, {"type": "text", "text": "bg; jobs; kill %1\r"})
    wait_for(service, lambda: "Running" in screen(service, sid))


def test_resize_changes_kernel_winsize_and_screen_epoch(service):
    program = "import os,signal,time; signal.signal(signal.SIGWINCH,lambda *a: print('RESIZED',os.get_terminal_size().columns,os.get_terminal_size().lines,flush=True)); print('STARTED',flush=True); time.sleep(30)"
    sid = service.open([sys.executable, "-c", program], rows=8, cols=50)
    wait_for(service, lambda: "STARTED" in screen(service, sid))
    service.resize(sid, 12, 71)
    wait_for(service, lambda: "RESIZED 71 12" in screen(service, sid))
    snap = service.snapshot(sid)
    assert (snap["rows"], snap["cols"], snap["epoch"]) == (12, 71, 1)
    assert service.resize(sid, 12, 71)
    time.sleep(.05)
    service.poll()
    assert service.snapshot(sid)["epoch"] == 1


def test_final_output_and_nonzero_exit_are_retained(service):
    sid = service.open(["/bin/sh", "-c", "printf 'FINAL\\n'; exit 7"])
    wait_for(service, lambda: service.snapshot(sid).get("status") == "exited")
    assert "FINAL" in screen(service, sid)
    assert service.snapshot(sid)["exit_code"] == 7
    assert not service.send(sid, {"type": "key", "key": "q"})


def test_failed_command_does_not_crash_other_sessions(service):
    bad = service.open(["/missing/tower-test-command"])
    good = service.open(["/bin/sh", "-c", "echo GOOD"])
    wait_for(service, lambda: all(service.snapshot(s).get("status") == "exited" for s in (bad, good)))
    assert service.snapshot(bad)["exit_code"] == 127
    assert "GOOD" in screen(service, good)


def test_bad_cwd_is_visible_launch_error(service, tmp_path):
    sid = service.open(["/bin/sh"], cwd=tmp_path / "absent")
    wait_for(service, lambda: service.snapshot(sid).get("status") == "error")
    assert "No such file" in service.snapshot(sid)["error"]


def test_session_bound_input_does_not_cross_sessions(service):
    command = [sys.executable, "-u", "-c", "print('READY'); print('GOT:'+input())"]
    left, right = service.open(command), service.open(command)
    wait_for(service, lambda: all("READY" in screen(service, s) for s in (left, right)))
    service.send(left, {"type": "text", "text": "LEFT_ONLY\r"})
    service.send(right, {"type": "text", "text": "RIGHT_ONLY\r"})
    wait_for(service, lambda: all(service.snapshot(s).get("status") == "exited" for s in (left, right)))
    assert "LEFT_ONLY" not in screen(service, right)
    assert "RIGHT_ONLY" not in screen(service, left)


def test_closed_session_id_cannot_be_reused_or_receive_stale_input(service):
    sid = service.open(["/bin/sh", "-c", "sleep 30"])
    wait_for(service, lambda: service.snapshot(sid).get("status") == "running")
    assert service.close_session(sid)
    assert not service.send(sid, {"type": "text", "text": "bad"})
    assert not service.snapshot(sid)
    with pytest.raises(ValueError, match="already been used"):
        service.open(["/bin/sh"], session_id=sid)


def test_close_and_shutdown_reap_owned_children(service):
    sid = service.open(["/bin/sh", "-c", "sleep 30 & wait"])
    wait_for(service, lambda: service.snapshot(sid).get("pid") is not None)
    pid = service.snapshot(sid)["pid"]
    broker = service._process
    service.shutdown()
    assert broker.poll() is not None
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    service.shutdown()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux subreaper acceptance")
def test_shutdown_collects_background_grandchildren_without_zombies(service, tmp_path):
    path = tmp_path / "background.pid"
    sid = service.open(["/bin/sh", "-c", 'sleep 30 & echo $! > "$1"; wait', "sh", str(path)])
    wait_for(service, lambda: path.exists() and path.read_text().strip())
    child_pid = int(path.read_text())
    assert Path(f"/proc/{child_pid}").exists()
    service.shutdown()
    assert not Path(f"/proc/{child_pid}").exists()


def test_history_snapshot_pages_remain_coherent_after_new_output(service):
    command = "import sys; [print('OLD%03d'%i) for i in range(60)]; sys.stdout.flush(); input(); [print('NEW%03d'%i) for i in range(60)]; sys.stdout.flush(); input()"
    sid = service.open([sys.executable, "-u", "-c", command], rows=4, history_lines=20)
    wait_for(service, lambda: "OLD059" in screen(service, sid))
    service.request(sid, "history", offset=0, limit=10)
    wait_for(service, lambda: service.snapshot(sid).get("result", {}).get("action") == "history")
    first = service.snapshot(sid)["result"]["result"]
    service.send(sid, {"type": "key", "key": "enter"})
    wait_for(service, lambda: "NEW059" in screen(service, sid))
    service.request(sid, "history", offset=10, limit=10, snapshot_id=first["snapshot_id"])
    wait_for(service, lambda: service.snapshot(sid).get("result", {}).get("result", {}).get("offset") == 10)
    second = service.snapshot(sid)["result"]["result"]
    assert second["history_start"] == first["history_start"]
    assert all(line.startswith("OLD") for line in second["lines"])
    assert int(second["lines"][0][3:]) == int(first["lines"][-1][3:]) + 1


def test_slow_recording_storage_cannot_block_pty_owner(monkeypatch):
    import threading
    from tower import terminal_recording
    from tower.terminal_broker import RecordingWriter
    release = threading.Event()
    class SlowRecorder:
        closed = False
        reason = ""
        def __init__(self, *args, **kwargs):
            release.wait(2)
        def output(self, data):
            pass
        def close(self, reason):
            self.closed = True
    monkeypatch.setattr(terminal_recording, "Recorder", SlowRecorder)
    started = time.monotonic()
    writer = RecordingWriter("unused", "test", 80, 24)
    try:
        for _ in range(40):
            writer.output(b"x" * 4096)
        assert time.monotonic() - started < .5
        assert writer.stopping and "incomplete coverage" in writer.reason
        assert writer.queue.qsize() == 32
    finally:
        writer.close()
        release.set()
        writer.thread.join(2)
    assert writer.closed


def test_history_is_requested_separately_from_screen_publications(service):
    sid = service.open([sys.executable, "-c", "for i in range(100): print('HISTORY_%03d'%i)"], rows=5)
    wait_for(service, lambda: service.snapshot(sid).get("status") == "exited")
    assert service.snapshot(sid)["history"] == []
    assert service.snapshot(sid)["history_count"] > 50
    assert service.request(sid, "history", offset=2, limit=4)
    wait_for(service, lambda: service.snapshot(sid).get("result", {}).get("action") == "history")
    result = service.snapshot(sid)["result"]["result"]
    assert result["offset"] == 2 and len(result["lines"]) == 4
    assert result["lines"][0] == "HISTORY_002"
    service.request(sid, "search", query="HISTORY_010")
    wait_for(service, lambda: service.snapshot(sid).get("result", {}).get("action") == "search")
    assert service.snapshot(sid)["result"]["result"]["total"] == 1


def test_output_flood_does_not_starve_other_session_or_block_ui_poll(service):
    flood = service.open([sys.executable, "-c", "import os; [os.write(1,b'NOISY '+b'x'*100+b'\\n') for _ in range(4000)]"], rows=8, cols=60)
    quiet = service.open([sys.executable, "-u", "-c", "print('READY');print('ACK:'+input())"])
    wait_for(service, lambda: "READY" in screen(service, quiet) and service.snapshot(flood).get("output_bytes", 0) > 0)
    start = time.monotonic()
    service.send(quiet, {"type": "text", "text": "responsive\r"})
    wait_for(service, lambda: "ACK:responsive" in screen(service, quiet))
    assert time.monotonic() - start < 3
    assert service.snapshot(flood)["output_bytes"] > 0


def test_oversized_paste_rejected_before_any_bytes_are_queued(service):
    sid = service.open(["/bin/sh", "-c", "sleep 30"])
    before = service._pending
    assert not service.send(sid, {"type": "paste", "text": "x" * (MAX_EVENT + 1)})
    assert "1 MiB" in service.error
    assert service._pending <= before


def test_partial_socket_writes_preserve_exact_framing_order():
    class ShortSocket:
        def __init__(self):
            self.data = bytearray()
        def send(self, value):
            size = min(3, len(value))
            self.data.extend(value[:size])
            return size
    service = TerminalService()
    service._socket = ShortSocket()
    for n in range(20):
        assert service._queue({"n": n})
    decoder = Decoder()
    decoder.feed(service._socket.data)
    assert decoder.take(30) == [{"n": n} for n in range(20)]
    assert service._pending == 0


def test_broken_socket_rejects_input_and_surfaces_disconnection():
    class BrokenSocket:
        def send(self, value):
            raise BrokenPipeError()
        def close(self):
            pass
    service = TerminalService()
    service._socket = BrokenSocket()
    service.sessions["session"] = {"status": "running"}
    assert not service.send("session", {"type": "text", "text": "unsafe to retry automatically"})
    assert service.snapshot("session")["status"] == "error"
    assert not service._out and service._pending == 0


def test_shutdown_timeout_keeps_a_reaper_and_does_not_abort_other_cleanup():
    import subprocess
    import threading
    collected = threading.Event()
    class DelayedProcess:
        def wait(self, timeout=None):
            if timeout is not None:
                raise subprocess.TimeoutExpired("owned-broker", timeout)
            collected.set()
            return 0
        def terminate(self):
            pass
        def kill(self):
            pass
    service = TerminalService()
    service._process = DelayedProcess()
    service.shutdown()
    assert collected.wait(1)
    service._reaper.join(1)
    assert not service._reaper.is_alive()
    assert "cleanup reaper" in service.error


def test_expired_allocation_launch_review_refuses_child_creation(service, tmp_path):
    marker = tmp_path / "must-not-be-created"
    sid = service.open([sys.executable, "-c", "from pathlib import Path;Path(__import__('sys').argv[1]).touch()", str(marker)],
                       metadata={"launch_deadline": time.monotonic() - 1})
    wait_for(service, lambda: service.snapshot(sid).get("status") == "error")
    assert "review expired" in service.snapshot(sid)["error"]
    assert not marker.exists()


def test_stale_publications_cannot_overwrite_new_geometry():
    service = TerminalService()
    service.sessions["session"] = {"seq": 5, "epoch": 2}
    for seq, epoch in ((4, 2), (6, 1), (5, 2)):
        assert not service._apply({"op": "snapshot", "id": "session", "state": {"seq": seq, "epoch": epoch}})
    assert service._apply({"op": "snapshot", "id": "session", "state": {"seq": 7, "epoch": 3, "lines": []}})


def test_independent_request_results_survive_later_results_and_screens():
    service = TerminalService()
    service.sessions["session"] = {"seq": 0, "epoch": 0}
    for n in range(40):
        service._apply({"op": "result", "id": "session", "request_id": n, "action": "history" if n % 2 else "diagnostics"})
    assert len(service.snapshot("session")["results"]) == 32
    service._apply({"op": "snapshot", "id": "session", "state": {"seq": 1, "epoch": 0}})
    assert [r["request_id"] for r in service.snapshot("session")["results"]] == list(range(8, 40))


def test_history_configuration_and_pagination(service):
    sid = service.open([sys.executable, "-c", "for i in range(80): print(i)"], rows=4, history_lines=12)
    wait_for(service, lambda: service.snapshot(sid).get("status") == "exited")
    assert service.snapshot(sid)["history_count"] == 12
    assert service.snapshot(sid)["history_start"] > 0
    service.request(sid, "history", offset=0, limit=6)
    service.request(sid, "history", offset=6, limit=6)
    wait_for(service, lambda: len(service.snapshot(sid).get("results", ())) == 2)
    responses = service.snapshot(sid)["results"]
    assert len(responses[0]["result"]["lines"]) == len(responses[1]["result"]["lines"]) == 6
    assert int(responses[1]["result"]["lines"][0]) == int(responses[0]["result"]["lines"][-1]) + 1


def test_visibility_throttle_does_not_stop_background_output(service):
    sid = service.open([sys.executable, "-u", "-c", "import time; print('READY'); time.sleep(.2); print('HIDDEN_END')"])
    wait_for(service, lambda: "READY" in screen(service, sid))
    assert service.request(sid, "visible", visible=False)
    wait_for(service, lambda: service.snapshot(sid).get("status") == "exited")
    assert "HIDDEN_END" in screen(service, sid)


def test_session_limit_is_explicit(service):
    ids = [service.open(["/bin/sh", "-c", "sleep 30"]) for _ in range(8)]
    with pytest.raises(ValueError, match="limit: 8"):
        service.open(["/bin/sh"])
    assert len(set(ids)) == 8


def test_broker_input_backpressure_is_visible_and_cancellable(service):
    sid = service.open(["/bin/sh", "-c", "sleep 30"])
    wait_for(service, lambda: service.snapshot(sid).get("status") == "running")
    for _ in range(4):
        assert service.send(sid, {"type": "text", "text": "x" * MAX_EVENT})
    wait_for(service, lambda: service.snapshot(sid).get("input_rejected", 0) > 0)
    assert "not accepted" in service.snapshot(sid)["error"]
    service.request(sid, "cancel_input")
    wait_for(service, lambda: service.snapshot(sid).get("result", {}).get("action") == "cancel_input")
    result = service.snapshot(sid)["result"]["result"]
    assert result["cancelled_bytes"] >= 0 and result["delivered_bytes"] >= 0
    assert "cannot be retracted" in result["notice"]


def test_recording_failure_does_not_kill_terminal(service, tmp_path):
    existing = tmp_path / "existing"
    existing.write_text("preserve me")
    sid = service.open(["/bin/sh", "-c", "sleep 30"])
    wait_for(service, lambda: service.snapshot(sid).get("status") == "running")
    service.request(sid, "record_start", path=str(existing))
    wait_for(service, lambda: "Recording stopped" in service.snapshot(sid).get("error", ""))
    assert service.snapshot(sid)["status"] == "running"
    assert existing.read_text() == "preserve me"


def test_missing_optional_engine_is_a_visible_session_error(monkeypatch):
    from tower import terminal_emulator
    from tower.terminal_broker import Broker
    monkeypatch.setattr(terminal_emulator, "pyte", None)
    left, right = socket.socketpair()
    broker = Broker(left)
    try:
        sid = "a" * 32
        broker.open({"id": sid, "argv": ["/bin/sh"]})
        state = broker.sessions[sid]
        assert state.status == "error" and "slurm-tower[terminal]" in state.error
        assert state.process is None and state.fd == -1
    finally:
        broker.selector.close()
        left.close()
        right.close()


def test_optional_recording_is_private_and_can_be_stopped_without_shell_exit(service, tmp_path):
    path = tmp_path / "output.jsonl"
    sid = service.open(["/bin/bash", "--noprofile", "--norc", "-i"], env={"PS1": "READY> "})
    wait_for(service, lambda: "READY> " in screen(service, sid))
    service.request(sid, "record_start", path=str(path))
    wait_for(service, lambda: service.snapshot(sid).get("recording") is True)
    service.send(sid, {"type": "text", "text": "printf 'RECORD_%s\\n' test\r"})
    wait_for(service, lambda: "RECORD_test" in screen(service, sid))
    service.request(sid, "record_stop")
    wait_for(service, lambda: not service.snapshot(sid).get("recording") and not service.snapshot(sid).get("recording_flushing"))
    assert path.stat().st_mode & 0o777 == 0o600
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert records[0]["input_recorded"] is False
    assert any(r.get("event") == "output" for r in records)
    assert service.snapshot(sid)["status"] == "running"
