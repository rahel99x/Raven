"""Opt-in evidence must stay bounded, private, and outside the frame I/O path."""
import json
from types import SimpleNamespace

import pytest

from raven import cli, ui_trace


def app(**values):
    return SimpleNamespace(**dict(dict(tab="jobs", mode="main", width=160, height=40,
                                      keymap={"[": "prev_tab"}), **values))


def test_bounded_timing_separates_cpu_from_wait_and_tracks_transition(monkeypatch):
    wall, cpu = [0], [0]
    monkeypatch.setattr(ui_trace.time, "perf_counter_ns", lambda: wall[0])
    monkeypatch.setattr(ui_trace.time, "thread_time_ns", lambda: cpu[0])
    trace, dashboard = ui_trace.UITrace(), app()
    trace.input(dashboard, ("[", None))

    def delayed_transition():
        wall[0] += 50_000_000
        cpu[0] += 1_000_000
        dashboard.tab = "research" if dashboard.tab == "jobs" else "jobs"
        return 17

    for _ in range(ui_trace.MAX_SAMPLES + 20):
        assert trace.measure(dashboard, "input_dispatch", delayed_transition) == 17
    report = trace.report()
    phase = report["phases"]["input_dispatch"]
    assert phase["calls"] == ui_trace.MAX_SAMPLES + 20
    assert phase["retained_samples"] == ui_trace.MAX_SAMPLES
    assert phase["p99_ms"] == phase["max_ms"] == 50
    assert phase["cpu_ms"] == phase["calls"]
    assert len(report["slowest"]) == ui_trace.MAX_SLOW
    assert len(report["transitions"]) == ui_trace.MAX_TRANSITIONS
    assert report["transitions"][-1]["input_action"] == "prev_tab"
    assert report["transitions"][-1]["input_age_ms"] > 0


def test_no_typed_content_mouse_coordinates_or_invalid_context_in_report():
    trace, dashboard = ui_trace.UITrace(), app(tab="/private/job/SECRET", width=None, height="SECRET")
    for mode, event in [("command", ("SECRET", None)), ("main", ("paste", "SECRET")),
                        ("main", ("mouse", (0, 123456789, 987654321, 0, 0)))]:
        dashboard.mode = mode
        trace.input(dashboard, event)
        trace.measure(dashboard, "feedback", lambda: None)
    text = json.dumps(trace.report())
    assert "SECRET" not in text and "123456789" not in text and "987654321" not in text
    assert trace.report()["maximum_geometry"] == {"width": 0, "height": 0}


def test_idle_wait_is_not_reported_as_slow_processing(monkeypatch):
    trace = ui_trace.UITrace()
    start = trace.started
    monkeypatch.setattr(ui_trace.time, "perf_counter_ns", lambda: start + 100_000_000)
    trace.record(app(), "input_wait", start, ui_trace.time.thread_time_ns(), ui_trace._context(app()))
    assert trace.report()["phases"]["input_wait"]["max_ms"] == 100
    assert not trace.report()["slowest"]


def test_capture_reserves_private_file_and_writes_only_after_exit(tmp_path):
    path, dashboard = tmp_path / "trace.json", app()
    with pytest.raises(RuntimeError, match="operation failed"):
        with ui_trace.capture(str(path), dashboard) as trace:
            assert path.stat().st_mode & 0o777 == 0o600
            assert dashboard.ui_trace is trace
            for _ in range(10):
                ui_trace.timed(dashboard, "compose", lambda: None)
            assert path.read_bytes() == b""
            raise RuntimeError("operation failed")
    assert dashboard.ui_trace is None
    assert json.loads(path.read_text())["phases"]["compose"]["calls"] == 10
    with pytest.raises(ui_trace.TraceError):
        with ui_trace.capture(str(path)):
            pytest.fail("existing file must not be overwritten")


def test_capture_refuses_symlink_and_missing_parent(tmp_path):
    target = tmp_path / "target"
    target.write_text("retain")
    link = tmp_path / "link"
    link.symlink_to(target)
    for path in (link, tmp_path / "missing" / "trace"):
        with pytest.raises(ui_trace.TraceError):
            with ui_trace.capture(str(path)):
                pytest.fail("unexpected trace creation")
    assert target.read_text() == "retain"


@pytest.mark.parametrize("mode", [["--once"], ["--watch"], ["--json"], ["--gpu-check"],
                                  ["--doctor"], ["run", "refresh"], ["--write-config"]])
def test_trace_requires_interactive_mode(mode):
    with pytest.raises(SystemExit) as error:
        cli.parse(["--ui-trace", "trace.json", *mode])
    assert error.value.code == 2


def test_no_trace_means_direct_call():
    assert ui_trace.timed(app(), "compose", lambda x: x + 1, 6) == 7


def test_cli_keeps_one_trace_across_profile_switch(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(cli.Config, "load", lambda *_: cli.Config({"profiles": {"second": {}}}))
    sessions = []
    def build(*_):
        dashboard = app(switch_profile="second" if not sessions else "")
        session = SimpleNamespace(app=dashboard, views=None, store=None, actions=None,
                                  sampler=SimpleNamespace(start=lambda: None), close=lambda: None)
        sessions.append(session)
        return session
    def run(dashboard, *_):
        ui_trace.timed(dashboard, "compose", lambda: None)
    monkeypatch.setattr(cli, "build", build)
    monkeypatch.setattr(cli.screen, "run_curses", run)
    path = tmp_path / "trace.json"
    assert cli.main(["--fake", "--ui-trace", str(path)]) == 0
    assert len(sessions) == 2
    assert sessions[0].app.ui_trace is sessions[1].app.ui_trace
    assert json.loads(path.read_text())["phases"]["compose"]["calls"] == 2


def test_cli_trace_creation_failure_precedes_session_start(tmp_path, monkeypatch, capsys):
    path = tmp_path / "existing.json"
    path.write_text("retain")
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(cli, "build", lambda *_: pytest.fail("failed trace started a backend"))
    assert cli.main(["--ui-trace", str(path)]) == 1
    assert path.read_text() == "retain"
    assert "Cannot create UI trace" in capsys.readouterr().err


def test_cli_trace_rejects_nonterminal_before_creating_file(tmp_path, monkeypatch, capsys):
    path = tmp_path / "trace.json"
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: False)
    monkeypatch.setattr(cli, "build", lambda *_: pytest.fail("nonterminal trace started a backend"))
    assert cli.main(["--ui-trace", str(path)]) == 1
    assert not path.exists()
    assert "requires an interactive terminal" in capsys.readouterr().err
