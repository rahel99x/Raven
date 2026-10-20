"""Timeline controls preserve source identity, pane focus and render budgets."""
from types import SimpleNamespace

import pytest

from raven import job_panels, layout as L, trace_timeline as T, trace_timeline_ui as U, trace_ui
from test_job_panels import dashboard
from test_trace_timeline import snapshot


def ready(rows=None):
    source = snapshot(rows or [("call", "outer", 0), ("call", "inner", 20), ("return", "inner", 40), ("return", "outer", 100)])
    source["timeline"] = T.build(source)
    source["timeline_index"] = T.index(source["timeline"])
    return {"result": source, "view": "timeline", "paint": None}


def app():
    messages = []
    return SimpleNamespace(say=messages.append), messages


def test_zoom_pan_fit_and_click_selection_use_integer_source_time():
    state, (owner, messages) = ready(), app()
    assert U.command(owner, state, ["zoom-in"])
    view = U.initialize(state)
    assert U._window(state["result"]["timeline"], view) == (25, 75)
    U.command(owner, state, ["pan-right"])
    assert U._window(state["result"]["timeline"], view) == (41, 91)
    U.command(owner, state, ["frame", "a-stream", "python:2"])
    assert view["selected"] == "python:2"
    U.command(owner, state, ["focus"])
    assert U._window(state["result"]["timeline"], view) == (16, 44)
    U.command(owner, state, ["fit"])
    assert view["window"] is None
    assert not messages


def test_search_is_case_insensitive_and_thread_selection_is_independent():
    state = ready([("call", "outer", 0), ("call", "WorkerSolve", 2, "worker"),
                   ("return", "WorkerSolve", 8, "worker"), ("return", "outer", 10)])
    owner, messages = app()
    U.command(owner, state, ["find", "solve"])
    assert "No matching" in messages[-1]
    U.command(owner, state, ["thread-next"])
    U.command(owner, state, ["find", "sOlVe"])
    assert U.initialize(state)["selected"] == "python:2"
    assert U.initialize(state)["thread"] == "worker"
    U.command(owner, state, ["find"])
    assert U.initialize(state)["query"] == ""


def test_stale_stream_or_evicted_interval_click_is_rejected():
    state, (owner, messages) = ready(), app()
    U.command(owner, state, ["frame", "different-stream", "python:1"])
    assert U.initialize(state)["selected"] is None
    U.command(owner, state, ["frame", "a-stream", "python:999"])
    assert U.initialize(state)["selected"] is None
    assert len(messages) == 2 and all("no longer" in message for message in messages)


@pytest.mark.parametrize("width", [1, 7, 8, 22, 40, 80])
@pytest.mark.parametrize("ascii_only", [True, False])
def test_wrapped_chart_controls_and_bar_hits_remain_inside_the_terminal(width, ascii_only):
    state = ready([("call", "solver_界\x1b[2J", 0), ("return", "solver_界\x1b[2J", 100)])
    views = SimpleNamespace(g=L.Glyphs(ascii_only))
    rows, hits = U.render(views, state, "42", width)
    rows, hits = trace_ui._wrap(rows, hits, width)
    assert all(L.vlen(L.row_text(row)) <= width for row in rows)
    assert "\x1b" not in L.to_text(rows, width)
    assert all(0 <= value[1] < value[2] <= width for _, _, value in hits)
    assert all(0 <= y < len(rows) for y, _, _ in hits)


def test_completed_view_has_no_live_claim_and_keeps_selected_details():
    state, (owner, _) = ready(), app()
    U.command(owner, state, ["frame", "a-stream", "python:2"])
    rows, _ = U.render(SimpleNamespace(g=L.Glyphs(False)), state, "42", 80)
    text = L.to_text(rows, 80)
    assert "Selected: inner" in text and "20ns wall time" in text
    assert "solver.py:12" in text and "live window" not in text


def test_paint_never_builds_or_reads_the_trace(monkeypatch):
    state = ready()
    def forbidden(*args, **kwargs):
        raise AssertionError("paint rebuilt source evidence")
    monkeypatch.setattr(T, "build", forbidden)
    monkeypatch.setattr(T, "index", forbidden)
    U.render(SimpleNamespace(g=L.Glyphs(False)), state, "42", 80)


def test_shortcuts_act_only_in_focused_timeline_content(dashboard, monkeypatch):
    owner = dashboard.app
    dashboard.render()
    job_panels._activate(owner, "trace")
    panels = job_panels.initialize(owner)
    panels["focus"] = "content"
    calls = []
    monkeypatch.setattr(trace_ui, "run_command", lambda app, args: calls.append(args))
    assert job_panels.handle_key(owner, "+")
    assert calls == [["trace", "zoom-in"]]
    trace_ui.initialize(owner)["view"] = "events"
    assert not trace_ui.handle_key(owner, "+")
    trace_ui.initialize(owner)["view"] = "timeline"
    from raven.workspace_layout import initialize as layout_state
    layout_state(owner).focus = "main"
    assert not trace_ui.handle_key(owner, "+")


def test_source_switch_resets_timeline_selection(dashboard):
    owner = dashboard.app
    state = trace_ui.initialize(owner)
    state["timeline_view"].update(selected="python:3", thread="old", window=(1, 4), query="old")
    trace_ui.cancel(owner, clear=True)
    assert U.initialize(state) == {"window": None, "thread": None, "selected": None, "query": ""}


def test_live_retention_keeps_absolute_fixed_window_and_reports_evicted_selection():
    state, (owner, _) = ready(), app()
    U.command(owner, state, ["frame", "a-stream", "python:2"])
    U.command(owner, state, ["focus"])
    saved = U.initialize(state)["window"]
    later = ready([("call", "later", 50), ("return", "later", 150)])["result"]
    state["result"] = later
    rows, _ = U.render(SimpleNamespace(g=L.Glyphs(False)), state, "42", 80)
    assert U.initialize(state)["window"] == saved
    assert "left the retained window" in L.to_text(rows, 80)


def cpu_ready():
    state = ready()
    source = state["result"]
    for position, event in enumerate(source["events"]):
        event["data"]["cpu_observation"] = dict(schema="raven.cpu-placement/v1", logical_cpu=position,
            socket_id=position // 2, core_id=position % 2, sample_start_ns=event["mono_ns"] + 1,
            sample_end_ns=event["mono_ns"] + 3, source="linux-sched_getcpu")
    source["timeline"] = T.build(source)
    source["timeline_index"] = T.index(source["timeline"])
    return state


def test_cpu_core_tracks_show_hardware_hierarchy_points_and_query_brackets():
    state, (owner, _) = cpu_ready(), app()
    U.command(owner, state, ["cores"])
    assert U.initialize(state)["thread"] is None
    rows, hits = U.render(SimpleNamespace(g=L.Glyphs(False)), state, "42", 80)
    text = L.to_text(rows, 80)
    assert "Socket 0 / core 0 / logical CPU 0" in text
    assert "Socket 1 / core 1 / logical CPU 3" in text
    point = next(value for _, _, value in hits if value[0][1]["command"].endswith("cpu:2"))
    U.command(owner, state, point[0][1]["command"].split()[1:])
    assert U.initialize(state)["thread"] is None  # Selecting a point preserves All threads.
    rows, _ = U.render(SimpleNamespace(g=L.Glyphs(False)), state, "42", 80)
    text = L.to_text(rows, 80)
    assert "Selected CPU observation: inner" in text
    assert "Query bracket: 21ns - 23ns" in text
    assert "not an exact scheduling transition" in " ".join(L.row_text(row) for row in rows)
    assert state["result"]["timeline"]["end_ns"] == 103


def test_absent_cpu_observations_offer_future_capture_without_guessing_a_core():
    state, (owner, _) = ready(), app()
    U.command(owner, state, ["cores"])
    rows, hits = U.render(SimpleNamespace(g=L.Glyphs(True)), state, "42", 80)
    text = L.to_text(rows, 80)
    assert "not-recorded" in text and "--trace-cpu-placement" in text
    assert not any("trace frame" in value[0][1]["command"] for _, _, value in hits)


def test_cpu_core_mode_switches_threads_and_persists_across_source_reset():
    state, (owner, _) = cpu_ready(), app()
    U.command(owner, state, ["cores"])
    U.command(owner, state, ["thread-next"])
    assert U.initialize(state)["thread"] == "main"
    U.command(owner, state, ["thread-next"])
    assert U.initialize(state)["thread"] is None
    U.command(owner, state, ["next"])
    assert U.initialize(state)["selected"] == "cpu:1"
    U.reset(state)
    assert U.initialize(state) == {"window": None, "thread": None, "selected": None, "query": "", "track": "cores"}


@pytest.mark.parametrize("initial,identifier,expected,details", [
    ("calls", "cpu:2", "cores", "Selected CPU observation: inner"),
    ("cores", "python:2", "calls", "Selected: inner"),
])
def test_cross_track_frame_command_selects_the_recorded_type(initial, identifier, expected, details):
    state, (owner, messages) = cpu_ready(), app()
    U.command(owner, state, [initial])
    U.command(owner, state, ["frame", "a-stream", identifier])
    assert U.initialize(state)["track"] == expected
    assert U.initialize(state)["selected"] == identifier
    rows, _ = U.render(SimpleNamespace(g=L.Glyphs(False)), state, "42", 80)
    assert details in L.to_text(rows, 80)
    assert not messages


@pytest.mark.parametrize("initial,identifier", [("calls", "cpu:2"), ("cores", "python:2")])
def test_cross_track_frame_command_keeps_source_identity_guard(initial, identifier):
    state, (owner, messages) = cpu_ready(), app()
    U.command(owner, state, [initial])
    before = dict(U.initialize(state))
    U.command(owner, state, ["frame", "another-stream", identifier])
    assert U.initialize(state) == before
    assert messages and "no longer" in messages[-1]


def test_stacks_summary_to_timeline_command_clears_stale_summary_mode():
    state, (owner, _) = ready(), app()
    state.update(summary=True, view="summary")
    U.command(owner, state, ["cores"])
    assert state["view"] == "timeline" and state["summary"] is False


def test_automatic_publication_of_another_stream_cannot_reuse_selected_sequence(dashboard, monkeypatch):
    from threading import Lock
    owner = dashboard.app
    dashboard.render()
    job_panels._activate(owner, "trace")
    context = {"key": ("test-context",)}
    monkeypatch.setattr(trace_ui, "_context", lambda app: context)
    state = trace_ui.initialize(owner)
    state.update(context=context, result=ready()["result"], path="old-path")
    state["timeline_view"].update(selected="python:2", window=(1, 3), thread="old")
    queued = []
    hub = SimpleNamespace(lock=Lock(), closed=False, pending=None, future=None,
        start_task=lambda work, done: queued.append(done) or True, cancel_task=lambda done: None)
    monkeypatch.setattr(job_panels, "_worker", lambda *args: hub)
    trace_ui.tick(owner)
    source = ready()["result"]
    source["identity"]["stream"] = "another-stream"
    queued[0](dict(sources=["new-path"], processes=[], index=0, snapshot=source,
                   reader=None, path="new-path", verified=True, discovered=True))
    assert state["timeline_view"]["selected"] is None
    assert state["timeline_view"]["window"] is None


def test_core_visualization_preference_saves_without_process_identity(dashboard):
    state = trace_ui.initialize(dashboard.app)
    state["timeline_view"].update(track="cores", selected="cpu:3", thread="old", window=(1, 2))
    saved = trace_ui.save(dashboard.app)
    assert saved == {"track": "cores"}
    restored = SimpleNamespace()
    trace_ui.restore(restored, saved)
    assert trace_ui.initialize(restored)["timeline_view"] == {
        "window": None, "thread": None, "selected": None, "query": "", "track": "cores"}


def test_unchanged_verified_prefix_reuses_worker_prepared_model(tmp_path, monkeypatch):
    from threading import Event
    from raven.trace_recorder import Recorder
    context = dict(remote=False, root=str(tmp_path), job_id="123", submit="submit", start="start", cluster="local", run_id="")
    with Recorder(tmp_path, job_id="123", attempt="submit/start", cluster="local", background_flush=False) as recorder:
        recorder.emit("call", {"function": "solve", "frame": "solve"})
        recorder.emit("return", {"frame": "solve"})
        recorder.flush()
        first = trace_ui._read(context, "", 0, "", None, Event())
        def forbidden(*args, **kwargs):
            raise AssertionError("Unchanged verified data rebuilt the visual model")
        monkeypatch.setattr(T, "build", forbidden)
        second = trace_ui._read(context, "", 0, first["path"], first["reader"], Event())
        assert second["snapshot"]["timeline"] is first["snapshot"]["timeline"]
        assert second["snapshot"]["timeline_index"] is first["snapshot"]["timeline_index"]
