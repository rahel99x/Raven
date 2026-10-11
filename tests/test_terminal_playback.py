"""Recorded bytes are inert terminal data; playback never submits commands."""
from pathlib import Path
import subprocess
import sys
import time

import pytest

pytest.importorskip("pyte")

from tower.terminal_playback import Playback, render
from tower.terminal_recording import Recorder
from tower.terminal_service import TerminalService


def text(player):
    return "\n".join("".join(part[0] for part in row) for row in player.engine.snapshot()["lines"])


def drain(player):
    for _ in range(200):
        player.tick(budget=.05)
        if player.index == len(player.events) or player.events[player.index]["t"] > player.target:
            return
    pytest.fail("Replay did not finish bounded fixture")


def fixture():
    return Playback({"rows": 4, "cols": 30}, (
        {"event": "output", "t": 0.0, "data": b"first\r\n"},
        {"event": "resize", "t": 1.0, "rows": 8, "cols": 40},
        {"event": "output", "t": 2.0, "data": b"\x1b[32msecond\x1b[0m"},
    ))


def test_seek_applies_recorded_resize_and_resets_parser_on_rewind():
    player = fixture()
    player.playing = False
    drain(player)
    assert "first" in text(player) and "second" not in text(player)
    player.seek(2)
    drain(player)
    assert "second" in text(player)
    assert (player.engine.rows, player.engine.cols) == (8, 40)
    player.seek(0)
    drain(player)
    assert "second" not in text(player)
    assert (player.engine.rows, player.engine.cols) == (4, 30)


def test_transport_controls_are_bounded_and_do_not_execute_input():
    player = fixture()
    assert player.handle(" ") and not player.playing
    for _ in range(20):
        player.handle("+")
    assert player.speed == 16
    for _ in range(20):
        player.handle("-")
    assert player.speed == .125
    player.handle("G")
    assert player.target == player.duration
    player.handle("g")
    assert player.target == 0
    assert player.handle(";") and not player.handle("q")
    with pytest.raises(ValueError):
        player.seek(float("nan"))


def test_renderer_never_reemits_child_control_strings():
    player = Playback({"rows": 4, "cols": 40}, ({"event": "output", "t": 0.,
        "data": b"safe\x1b]52;c;c2VjcmV0\x07\x1b]8;;https://example.test\x1b\\link\x1b]8;;\x1b\\"},))
    drain(player)
    result = render(player, 8, 30)
    assert "\x1b]" not in result and "\x07" not in result
    assert "safe" in result
    assert result.count("\x1b[2K") == 8


def test_real_pty_player_displays_recording_and_q_exits_without_execution(tmp_path):
    marker = tmp_path / "must-not-exist"
    recording = tmp_path / "session.jsonl"
    recorder = Recorder(recording, "fixture", cols=120, rows=10)
    recorder.output(("touch " + str(marker) + "\r\nREPLAY_PROOF\r\n").encode())
    recorder.close()
    service = TerminalService()
    try:
        player = Path(__file__).resolve().parents[1] / "tower" / "terminal_playback.py"
        sid = service.open(argv=[sys.executable, str(player), str(recording)], rows=16, cols=120)
        deadline = time.monotonic()+5
        while time.monotonic() < deadline:
            service.poll()
            current = "\n".join("".join(part[0] for part in row) for row in service.snapshot(sid).get("lines", ()))
            if "REPLAY_PROOF" in current:
                break
            time.sleep(.01)
        assert "REPLAY_PROOF" in current, service.snapshot(sid)
        assert not marker.exists()
        assert service.send(sid, {"type": "text", "text": "q"})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            service.poll()
            if service.snapshot(sid).get("status") == "exited":
                break
            time.sleep(.01)
        assert service.snapshot(sid).get("exit_code") == 0
        assert not marker.exists()
    finally:
        service.shutdown()


def test_noninteractive_replay_prints_text_without_terminal_controls(tmp_path):
    recording = tmp_path / "session.jsonl"
    with_recorder = Recorder(recording, "fixture")
    with_recorder.output(b"\x1b[31mhello\x1b[0m\r\n")
    with_recorder.close()
    source = Path(__file__).resolve().parents[1] / "tower" / "terminal_playback.py"
    result = subprocess.run([sys.executable, str(source), str(recording)], cwd=tmp_path,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert "hello" in result.stdout and "\x1b" not in result.stdout


def test_cached_renderer_emits_only_changed_rows_and_resizes_cleanly():
    player = fixture()
    player.playing = False
    drain(player)
    cache = {}
    first = render(player, 8, 30, cache=cache)
    assert first.count("\x1b[2K") == 8
    assert render(player, 8, 30, cache=cache) == ""
    player.target = .5
    timer_only = render(player, 8, 30, cache=cache)
    assert timer_only.count("\x1b[2K") == 1
    assert "0.5/2.0s" in timer_only
    player.engine.feed(b"new")
    assert render(player, 8, 30, cache=cache).count("\x1b[2K") == 1
    assert render(player, 9, 35, cache=cache).count("\x1b[2K") == 9


def test_timeline_shows_advancing_playhead_between_sparse_events():
    player = fixture()
    player.tick(player.anchor_time + .5)
    assert player.position == 0
    assert player.target == .5
    assert "0.5/2.0s" in render(player, 5, 50)


@pytest.mark.parametrize("chunk", [1, 2, 7, 1024])
def test_control_frames_and_pastes_never_become_replay_hotkeys(chunk):
    from tower.terminal_playback import PlaybackInput
    controls = PlaybackInput()
    payload = (b"\x1b]0;qgGhljk\x07\x1bPqgGhljk\x1b\\"
               b"\x1b[200~qgGhljk +-\x1b[201~"
               b"\x1bq\x1b[?1;2q\x1b[<0;20;30M\x1b[<0;20;30m"
               b"\x1b[A\x1b[B\x1b[C\x1b[D\x1b[H\x1b[Fq")
    result = []
    for offset in range(0, len(payload), chunk):
        result.extend(controls.feed(payload[offset:offset + chunk]))
    assert result == ["k", "j", "l", "h", "g", "G", "q"]


def test_hostile_input_frame_state_is_bounded_and_can_recover():
    from tower.terminal_playback import PlaybackInput
    controls = PlaybackInput()
    assert controls.feed(b"\x1b[" + b"1" * 100000) == []
    assert len(controls.value) <= 64
    assert controls.feed(b"mq") == ["q"]
    assert controls.feed(b"\x1b[200~" + b"q" * 100000) == []
    assert len(controls.value) <= 6
    assert controls.feed(b"\x1b[201~h") == ["h"]
    assert controls.feed(b"\x1b]2;" + b"q" * 100000) == []
    assert len(controls.value) <= 64
    assert controls.feed(b"\x1b\\l") == ["l"]


def test_recording_caps_reject_invalid_events_before_playback():
    from tower.terminal_recording import MAX_EVENT, MAX_EVENTS
    header = {"rows": 2, "cols": 10}
    for events in (
        ({"event": "command", "t": 0, "data": b"touch example"},),
        ({"event": "output", "t": float("inf"), "data": b""},),
        ({"event": "output", "t": 10**500, "data": b""},),
        ({"event": "output", "t": -1, "data": b""},),
        ({"event": "output", "t": 0, "data": "wrong type"},),
        ({"event": "output", "t": 0, "data": b"x" * (MAX_EVENT + 1)},),
        ({"event": "resize", "t": 0, "rows": 4096, "cols": 4096},),
        ({"event": "output", "t": 0, "data": b""},) * (MAX_EVENTS + 1),
    ):
        with pytest.raises(ValueError):
            Playback(header, events)


def test_replay_total_decoded_output_budget_and_immutable_event_entries():
    from tower.terminal_recording import MAX_EVENT, MAX_FILE
    header = {"rows": 2, "cols": 10}
    event = {"event": "output", "t": 0, "data": b"safe"}
    player = Playback(header, [event])
    event["data"] = b"changed"
    drain(player)
    assert "safe" in text(player) and "changed" not in text(player)
    large = {"event": "output", "t": 0, "data": b"x" * MAX_EVENT}
    with pytest.raises(ValueError, match="byte budget"):
        Playback(header, [large] * (MAX_FILE // MAX_EVENT + 1))


def test_resize_and_seek_clamp_scroll_position():
    player = fixture()
    player.playing = False
    player.seek(2)
    drain(player)
    player.scroll = 7
    player.seek(0)
    assert player.scroll == 3
    player = Playback({"rows": 10, "cols": 20}, (
        {"event": "resize", "t": 0., "rows": 2, "cols": 10},
    ))
    player.scroll = 9
    drain(player)
    assert player.scroll == 1


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, 0])
def test_invalid_speed_cannot_poison_playback_clock(value):
    player = fixture()
    with pytest.raises(ValueError):
        player.change_speed(value)


def test_tick_rejects_nonfinite_clocks_and_never_has_negative_playhead():
    player = fixture()
    with pytest.raises(ValueError):
        player.tick(float("nan"))
    with pytest.raises(ValueError):
        player.tick(budget=float("inf"))
    player.tick(player.anchor_time - 10)
    assert player.target == 0


def test_loader_errors_cannot_emit_controls_from_hostile_filename(tmp_path):
    source = Path(__file__).resolve().parents[1] / "tower" / "terminal_playback.py"
    missing = tmp_path / "\x1b]52;c;c2VjcmV0\x07.jsonl"
    result = subprocess.run([sys.executable, str(source), str(missing)], cwd=tmp_path,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 1
    assert "Tower replay:" in result.stderr
    assert "\x1b" not in result.stderr and "\x07" not in result.stderr
