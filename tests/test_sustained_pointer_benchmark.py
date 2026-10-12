"""The optional profiling experiment cannot consume deliberate input."""
from collections import deque
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture(scope="module")
def benchmark():
    path = Path(__file__).resolve().parents[1] / "scripts" / "benchmark_sustained_pointer.py"
    spec = importlib.util.spec_from_file_location("tower_sustained_pointer_benchmark", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MOUSE = SimpleNamespace(BUTTON1_CLICKED=1, BUTTON1_PRESSED=2, BUTTON1_RELEASED=4,
                       BUTTON1_DOUBLE_CLICKED=8, BUTTON3_CLICKED=16, BUTTON3_PRESSED=32,
                       BUTTON4_PRESSED=64, BUTTON5_PRESSED=128, REPORT_MOUSE_POSITION=256)


def motion(index, bits=256):
    return "mouse", (0, index, 10, 0, bits)


def fixture(events):
    pending, queued, applied, timeout = deque(events), deque(), [], []
    app = SimpleNamespace(mode="main")
    window = SimpleNamespace(timeout=timeout.append)
    screen = SimpleNamespace(
        _read_input=lambda *_: pending.popleft() if pending else None,
        _motion_report=lambda bits, _: bool(bits & MOUSE.REPORT_MOUSE_POSITION),
        _apply_input=lambda _, event, *_args: applied.append(event),
        _INPUT_READERS={id(window): SimpleNamespace(queue=queued)},
    )
    return SimpleNamespace(app=app, window=window, screen=screen, pending=pending,
                           queued=queued, applied=applied, timeout=timeout)


@pytest.mark.parametrize("event", [("q", None), ("resize", (52, 320)), ("paste", "research"),
                                    motion(5, 2), motion(5, 4), motion(5, 1), motion(5, 64)])
def test_profiling_drain_preserves_first_key_resize_paste_or_button_order(benchmark, event):
    first, latest, future = motion(1), motion(2), motion(9)
    f = fixture([first, latest, event, future])
    assert benchmark.diagnostic_drain_motion(f.screen, f.app, f.window, MOUSE, [], seconds=1) == 2
    assert f.applied == [latest]
    assert f.queued[0] is event
    assert list(f.pending) == [future]


@pytest.mark.parametrize("owner", ["chart_interaction_state", "metric_live_state", "text_selection_state",
                                   "job_selection_state", "scrollbar_state", "pane_drag_state"])
def test_profiling_drain_never_reads_while_an_existing_gesture_owns_input(benchmark, owner):
    event = motion(1)
    f = fixture([event])
    setattr(f.app, owner, {"capture": object()})
    assert benchmark.diagnostic_drain_motion(f.screen, f.app, f.window, MOUSE, []) == 0
    assert list(f.pending) == [event] and not f.timeout and not f.applied


def test_profiling_drain_preserves_pending_event_and_respects_report_budget(benchmark):
    f = fixture([motion(index) for index in range(10)])
    assert benchmark.diagnostic_drain_motion(f.screen, f.app, f.window, MOUSE, [], pending=True) == 0
    assert len(f.pending) == 10 and not f.timeout
    assert benchmark.diagnostic_drain_motion(f.screen, f.app, f.window, MOUSE, [], limit=3, seconds=1) == 3
    assert f.applied == [motion(2)] and len(f.pending) == 7


@pytest.mark.parametrize("option,value", [
    ("--rates", "0"), ("--rates", "-1"), ("--rates", "1001"),
    ("--rates", "50,"), ("--rates", "50,50"),
    ("--seconds", "0"), ("--seconds", "-1"), ("--seconds", "nan"),
    ("--seconds", "inf"), ("--seconds", "11"),
    ("--points", "0"), ("--points", "10001"),
    ("--jobs", "2"), ("--jobs", "1001"),
    ("--delta", "0"), ("--delta", "31"), ("--delta", "nan"),
])
def test_public_arguments_fail_before_opening_a_pty(benchmark, monkeypatch, tmp_path, option, value):
    calls = []
    monkeypatch.setattr(benchmark, "run", lambda *_: calls.append(True))
    source = Path(__file__).resolve().parents[1]
    with pytest.raises(SystemExit) as error:
        benchmark.main(["--source-root", str(source), "--output", str(tmp_path / "result.json"),
                        option, value])
    assert error.value.code == 2 and not calls


def test_public_arguments_require_existing_output_directory(benchmark, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(benchmark, "run", lambda *_: calls.append(True))
    source = Path(__file__).resolve().parents[1]
    with pytest.raises(SystemExit) as error:
        benchmark.main(["--source-root", str(source), "--output", str(tmp_path / "missing" / "result.json")])
    assert error.value.code == 2 and not calls


def test_valid_bounded_arguments_preserve_requested_comparison(benchmark, monkeypatch, tmp_path):
    calls = []
    def run(args, rate):
        calls.append((rate, args.seconds, args.points, args.jobs, args.delta))
        return {"rate_hz": rate}
    monkeypatch.setattr(benchmark, "run", run)
    source = Path(__file__).resolve().parents[1]
    output = tmp_path / "result.json"
    assert benchmark.main(["--source-root", str(source), "--output", str(output), "--rates", "250,1000",
                           "--seconds", "3", "--points", "4000", "--jobs", "3", "--delta", "5"]) == 0
    assert calls == [(250, 3., 4000, 3, 5.), (1000, 3., 4000, 3, 5.)]
    assert output.is_file()


def test_shared_live_delta_skips_retained_ineligible_and_other_job_controls(benchmark):
    entries = {("live", "7", "cpu"): {"delta": 30},
               ("live", "7", "retired"): {"delta": 30},
               ("live", "8", "cpu"): {"delta": 30}}
    configured = []
    def set_delta(_, key, value):
        configured.append(key)
        entries[key]["delta"] = value
        return True
    metric = SimpleNamespace(initialize=lambda _: {"entries": entries},
                             set_enabled=lambda _, key, value: key[-1] != "retired",
                             set_delta=set_delta)
    assert benchmark.configure_live(SimpleNamespace(selected_id="7"), metric, 5) == [5]
    assert configured == [("live", "7", "cpu")]


def test_live_fixture_requires_at_least_one_eligible_control(benchmark):
    metric = SimpleNamespace(initialize=lambda _: {"entries": {("live", "7", "cpu"): {"delta": 30}}},
                             set_enabled=lambda *_: False)
    with pytest.raises(ValueError, match="no eligible"):
        benchmark.configure_live(SimpleNamespace(selected_id="7"), metric, 5)


@pytest.mark.parametrize("interactive", [False, True])
def test_fixture_seeds_the_actual_sorted_job_and_renders_its_4000_point_history(benchmark, interactive):
    from tower import chart_interaction as C, layout as L, metric_live
    from tower.config import Config
    from tower.controller import App
    from tower.model import Job, Store
    from tower.views import Views
    cfg = Config({"animations": False, "log_lines": 0, "series_keep": 9000})
    store = Store(persist=False, series_keep=9000)
    jobs = [Job("10", "first-source", "cpu", "RUNNING", submit="s", start="r"),
            Job("20", "displayed-source", "cpu", "RUNNING", submit="s", start="r")]
    store.apply_jobs(jobs)
    app = App(store, None, None, cfg, "test", interactive=interactive)
    app.selected_id = "10"
    app.cursor["jobs"] = 1
    views = Views(L.Glyphs(False), cfg)
    app.views_ref = views
    try:
        selected = benchmark.prepare_fixture_job(
            app, store.snapshot(), lambda: views.compose(store.snapshot(), app, 320, 52))
        assert selected.id == app.selected_id == app.analytics_job == "20"
        for index in range(4000):
            store.record(selected.id, {"k": "live", "t": float(index), "cpu": .5, "rss": 1000.})
            store.record(selected.id, {"k": "gpu", "t": float(index), "gpu": {"0": [50.]}})
        rows, _ = views.compose(store.snapshot(), app, 320, 52)
        plots = C.initialize(app)["plots"]
        assert plots and all(plot.key[1] == selected.id for plot in plots)
        assert len(store.series[selected.id]) == 8000
        assert not store.series.get("10")
        assert "4000 cpu samples, 4000 gpu samples" in L.to_text(rows, 320)
        # Real curses uses the virtualized interactive document. A previous
        # empty source must not suppress the first published chart bands or
        # require an extra refresh before Live controls exist.
        assert benchmark.configure_live(app, metric_live, 1)
    finally:
        if app.research:
            app.research.close()


def test_diagnostic_state_does_not_record_terminal_prefix_or_pasted_contents(benchmark):
    import json
    reader = SimpleNamespace(escape="private-terminal-bytes", pasting=True, queue=[("paste", "private-paste")],
                             raw_values=["private-coordinate"], text=["private-paste"], pending=True)
    state = benchmark.input_state(SimpleNamespace(mode="main", tab="jobs", quit=False), reader)
    assert state["reader"] == {"escape_length": 22, "pasting": True, "queued_events": 1,
                               "raw_values": 1, "pending_utf8_bytes": 0,
                               "discard_mouse": False, "discard_csi": False, "pending": True}
    assert "private" not in json.dumps(state)


@pytest.mark.parametrize("initial,expected", [("multi", {250: "single", 500: "multi"}),
                                             ("single", {250: "multi", 500: "single"})])
def test_actual_command_switch_plan_visits_opposite_mode_and_returns(benchmark, initial, expected):
    assert benchmark.switch_plan(initial, 750) == expected


def test_switch_fixture_requires_enough_time_and_reports_before_opening_pty(benchmark, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(benchmark, "run", lambda *_: calls.append(True))
    source = Path(__file__).resolve().parents[1]
    with pytest.raises(SystemExit) as error:
        benchmark.main(["--source-root", str(source), "--output", str(tmp_path / "result.json"),
                        "--switch-workers", "--seconds", ".1"])
    assert error.value.code == 2 and not calls


def test_toolbar_switch_uses_published_cell_coordinates_and_complete_press_release(benchmark):
    from tower import screen
    raw = benchmark.switch_input("single", "toolbar", (0, 50, 60)).decode("ascii")
    packets = [packet for packet in raw.split("\x1b") if packet]
    press = screen._SGR_MOUSE.fullmatch("\x1b" + packets[0])
    release = screen._SGR_MOUSE.fullmatch("\x1b" + packets[1])
    assert press is not None and release is not None
    assert press.groups() == ("0", "56", "1", "M")
    assert release.groups() == ("0", "56", "1", "m")
    assert benchmark.switch_input("single", "command") == b":workers single\r"


def test_toolbar_control_requires_switch_and_unknown_control_is_rejected(benchmark, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(benchmark, "run", lambda *_: calls.append(True))
    source = Path(__file__).resolve().parents[1]
    for options in (["--switch-control", "toolbar"], ["--switch-workers", "--switch-control", "unknown"]):
        with pytest.raises(SystemExit) as error:
            benchmark.main(["--source-root", str(source), "--output", str(tmp_path / "result.json"), *options])
        assert error.value.code == 2 and not calls


@pytest.mark.parametrize("previous", [None, '{"button": [0, 10, 20]}'])
@pytest.mark.parametrize("fail_write", [False, True])
def test_toolbar_metadata_marker_is_complete_before_visible(benchmark, tmp_path, monkeypatch, previous, fail_write):
    import json
    marker = tmp_path / "pointer.control.json"
    if previous is not None:
        marker.write_text(previous)
    original = Path.write_text
    observations = []
    def interrupted_write(path, text, *args, **kwargs):
        original(path, text[:1], *args, **kwargs)
        observations.append(marker.read_text() if marker.exists() else None)
        assert observations[-1] == previous
        if fail_write:
            raise OSError("Interrupted control publication")
        return original(path, text, *args, **kwargs)
    monkeypatch.setattr(Path, "write_text", interrupted_write)
    value = {"button": [0, 50, 60]}
    if fail_write:
        with pytest.raises(OSError, match="Interrupted control"):
            benchmark.publish_marker(marker, value)
        assert (marker.read_text() if marker.exists() else None) == previous
    else:
        benchmark.publish_marker(marker, value)
        assert json.loads(marker.read_text()) == value
    assert observations == [previous]
    assert not marker.with_name(marker.name + ".tmp").exists()


def test_paint_and_flush_age_use_latest_dispatched_pointer_not_unconsumed_input(benchmark):
    sent = {1: 1., 2: 1.01, 3: 1.02}
    dispatched = [{'seq': 1, 'at': 1.005}, {'seq': 3, 'at': 1.025}]
    paints = [{'start': 1.004, 'ms': 1}, {'start': 1.015, 'ms': 5}, {'start': 1.03, 'ms': 2}]
    flushes = [{'start': 1.02, 'ms': 4}, {'start': 1.033, 'ms': 3}]
    assert benchmark.latest_pointer_age(paints, dispatched, sent) == pytest.approx([20, 12])
    assert benchmark.latest_pointer_age(flushes, dispatched, sent) == pytest.approx([24, 16])


def test_managed_variant_is_forwarded_to_measurement(benchmark, monkeypatch, tmp_path):
    called = []
    monkeypatch.setattr(benchmark, 'run', lambda args, rate: called.append(args.managed) or {'rate_hz': rate})
    source = Path(__file__).resolve().parents[1]
    assert benchmark.main(['--source-root', str(source), '--output', str(tmp_path / 'managed.json'),
                           '--managed', '--rates', '50']) == 0
    assert called == [True]


@pytest.mark.parametrize('count', [6, 100])
def test_queue_fixture_preserves_exact_requested_retained_count(benchmark, count):
    from tower.slurm import FakeBackend, Slurm
    backend = FakeBackend()
    backend.spec = benchmark.fixture_specs(count)
    jobs = Slurm(backend, backend.user).jobs()
    assert len(jobs) == count
    assert len({job.id for job in jobs}) == count
    assert sum(not job.pending for job in jobs) == count - (count + 4) // 5
