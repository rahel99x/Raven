"""Document rails move cached viewports without changing selected source data."""
from types import SimpleNamespace

import pytest

from raven import (analysis_ui as A, analytics_document as D, chart_interaction as C,
                   layout as L, log_tools as T, log_workbench as W,
                   research_views as R, scrollbars as S, scrolling)
from raven.config import Config
from raven.controller import App
from raven.logs import LogSession
from raven.model import Job, Store
from raven.remote import LocalFiles
from raven.research import ResearchHub
from raven.views import Views


@pytest.fixture
def clock(monkeypatch):
    value = [100.0]
    monkeypatch.setattr(scrolling.time, "monotonic", lambda: value[0])
    return value


@pytest.fixture
def app():
    cfg = Config({"startup_animation": False})
    store = Store(persist=False)
    store.jobs = [Job("77", "training", "gpu", "RUNNING", cpus=4, gpus=2)]
    value = App(store, None, None, cfg, "tester", interactive=False)
    value.width, value.height = 100, 28
    value.selected_id = "77"
    value.interactive = True
    value.research = ResearchHub(cfg)
    views = Views(L.Glyphs(False), cfg)
    value.views_ref = views
    yield value, views
    value.research.close()


def pane(app, key):
    return next(item for item in S.publish(app, app.width, app.height) if item.key == key)


def jump(app, item, bottom=True):
    y, left, _ = item.header
    assert S.handle_mouse(app, y, left + (2 if bottom else 0), button="left")


def render_modal(module, app, views):
    S.begin_frame(app)
    scrolling.begin_frame(app)
    rows = module.overlay(views, app.store.snapshot() if hasattr(app, "store") else {},
                          app, app.width, app.height)
    scrolling.finish_frame(app)
    return rows


@pytest.mark.parametrize("ascii_", [False, True])
def test_native_series_buttons_keep_job_and_paint_exact_pid_endpoint(app, clock, ascii_):
    application, views = app
    application.tab, application.analytics_job = "analytics", "77"
    views.set_ascii(ascii_)
    C.begin_frame(application, 100, 28)
    rows = [[(f"row {index}", "")] for index in range(80)]

    def draw():
        S.begin_frame(application)
        scrolling.begin_frame(application)
        painted, page = D.prepare(application, "77", "first", 100, 22, 3, 77)
        result = D.finish(application, rows, 3, painted, page, 100, C.mark(application), views.g)
        scrolling.finish_frame(application)
        return result, pane(application, "analytics:series-document")

    _, initial = draw()
    jump(application, initial)
    assert D.initialize(application)["top"] == initial.limit
    clock[0] += .017
    visible, motion = draw()
    assert 0 < motion.painted < motion.target and scrolling.active(application)
    assert f"row {motion.painted + 3}" in L.row_text(visible[3])
    clock[0] += .3
    _, settled = draw()
    assert settled.painted == settled.target == settled.limit
    assert application.analytics_job == application.selected_id == "77"
    jump(application, settled, bottom=False)
    clock[0] += .3
    _, home = draw()
    assert home.painted == home.target == 0


def test_research_metrics_reserve_rail_and_keep_navigation_and_cached_hits(app, clock, monkeypatch):
    application, views = app
    application.tab, application.research_job_id = "research", "77"
    result = {"status": "ready", "path": "/reported.jsonl", "series": {
        f"metric-{index}": [{"t": stamp, "value": index + stamp} for stamp in range(5)]
        for index in range(40)}}
    monkeypatch.setattr(application.research, "request", lambda context: result)

    def draw():
        S.begin_frame(application)
        C.begin_frame(application, application.width, application.height)
        scrolling.begin_frame(application)
        rows, hits = R.render(views, application.store.snapshot(), application, 100, 22)
        scrolling.finish_frame(application)
        return rows, hits, pane(application, "research:document")

    rows, hits, initial = draw()
    original_navigation = rows[:application.research_nav_rows]
    assert all(item.rect.right <= 99 for item in C.publish(application, 100, 28))
    jump(application, initial)
    clock[0] += .3
    rows, hits, end = draw()
    assert end.painted == end.limit
    assert rows[:application.research_nav_rows] == original_navigation
    assert any(kind == "research_metric" and key == "metric-39" for _, kind, key in hits)
    assert application.research_job_id == application.selected_id == "77"
    with monkeypatch.context() as guard:
        guard.setattr(application.research, "request", lambda *args: pytest.fail("pointer requested Research"))
        guard.setattr(application.store, "snapshot", lambda: pytest.fail("pointer requested snapshot"))
        jump(application, end, bottom=False)


def test_fitting_24_cell_research_metric_keeps_live_controls_and_full_width(app, monkeypatch):
    from raven import metric_live
    application, views = app
    application.tab, application.research_job_id = "research", "77"
    application.width, application.height = 24, 60
    result = {"status": "ready", "path": "/reported.jsonl", "series": {
        "loss": [{"t": stamp, "value": float(stamp)} for stamp in range(5)]}}
    monkeypatch.setattr(application.research, "request", lambda context: result)
    C.begin_frame(application, 24, 60)
    S.begin_frame(application)
    rows, _ = R.render(views, application.store.snapshot(), application, 24, 60)
    C.publish(application, 24, 60)
    item = pane(application, "research:document")
    assert item.limit == 0 and not S.descriptors(application)
    assert metric_live.initialize(application)["records"]
    assert any("30s" in L.row_text(row) and "1s" in L.row_text(row) for row in rows)
    assert all(L.vlen(L.row_text(row)) <= 24 for row in rows if "Live" in L.row_text(row))


@pytest.mark.parametrize("modal,stride", [("timeline", 2), ("chart_events", 3)])
def test_analysis_event_bars_preserve_cursor_then_keyboard_reveals_it(app, clock, monkeypatch, modal, stride):
    application, views = app
    application.mode = "analysis"
    state = A.initialize(application)
    state.update(modal=modal, cursor=0, chart_job="77")
    events = [{"number": index + 1, "t": float(index), "kind": "phase", "job": "77", "text": f"event {index}"}
              for index in range(60)]
    monkeypatch.setattr(A, "timeline_events", lambda *args: events)
    monkeypatch.setattr(A, "chart_events", lambda *args: events)
    render_modal(A, application, views)
    initial = pane(application, "analysis:document")
    jump(application, initial)
    clock[0] += .3
    rows = render_modal(A, application, views)
    end = pane(application, "analysis:document")
    assert end.painted == end.limit and state["cursor"] == 0
    assert f"event {59}" in "\n".join(L.row_text(row) for _, _, row in rows)
    event_links = [value for _, _, value in state["control_hits"] if value.get("group") in ("timeline_events", "chart_events")]
    assert event_links and all(0 <= value["event_index"] < len(events) for value in event_links)
    assert any(value["id"] == "analysis-navigation:back" for _, _, value in state["control_hits"])
    A.handle_key(application, "down")
    clock[0] += .3
    render_modal(A, application, views)
    selected = pane(application, "analysis:document")
    assert state["cursor"] == 1
    assert selected.painted <= stride < selected.painted + selected.page


def log_app(width=80, height=26):
    application = SimpleNamespace(logs=LogSession(files=LocalFiles()), tab="log", mode="main", log_job="77",
                                  project_state={}, width=width, height=height, cfg={}, animations_enabled=True,
                                  theme="default", selected_id="77", message="")
    application.say = lambda text: setattr(application, "message", text)
    W.initialize(application)
    application.log_entries = lambda: W.visible_entries(application, application.logs.entries)
    return application


def test_catalog_bar_can_leave_selected_file_without_recentring_or_metadata_io(clock, monkeypatch):
    application = log_app()
    application.logs.browser = True
    application.logs.entries = [dict(id=str(index), path=f"/declared/{index}.log", label=f"file {index}", group="Workers")
                                for index in range(80)]
    monkeypatch.setattr(W, "_metadata", lambda *args: {})
    views = SimpleNamespace(g=L.Glyphs(False))

    def draw():
        S.begin_frame(application)
        rows, hits = W.render_browser(views, {}, application, 80, 20, [], [])
        return rows, hits, pane(application, "logs:catalog")

    _, _, initial = draw()
    jump(application, initial)
    clock[0] += .3
    rows, hits, end = draw()
    assert end.painted == end.limit and application.logs.browser_cursor == 0
    assert any(kind == "log_file" and key == "79" for _, kind, key in hits)
    assert all(0 <= y < len(rows) for y, _, _ in hits)
    with monkeypatch.context() as guard:
        guard.setattr(W, "_metadata", lambda *args: pytest.fail("pointer requested metadata"))
        jump(application, end, bottom=False)
    W.handle_key(application, "down")
    application.logs.browser_cursor = 1  # Native controller selects catalog entries.
    clock[0] += .3
    _, hits, focused = draw()
    assert any(kind == "log_file" and key == "1" for _, kind, key in hits)
    assert focused.painted == 0


@pytest.mark.parametrize("view", ["json", "fold", "split", "diff"])
def test_alternate_log_bars_publish_painted_exact_rows_and_preserve_raw_source(clock, monkeypatch, view):
    application = log_app(100, 30)
    application.logs.path = "/declared/current.log"
    state = W._state(application)
    state["view"] = view
    sources = [dict(path=f"/declared/{side}.log", label=f"side {side}", size=100,
                    lines=[f'{{"row": {index}, "side": {side}}}' for index in range(160)], file_identity=(side, 1))
               for side in range(2 if view in ("split", "diff") else 1)]
    monkeypatch.setattr(W, "_alternate", lambda app: {"sources": sources})
    views = SimpleNamespace(g=L.Glyphs(False))
    render_modal(W, application, views)
    initial = pane(application, "logs:workbench")
    jump(application, initial)
    clock[0] += .017
    render_modal(W, application, views)
    motion = pane(application, "logs:workbench")
    assert 0 < motion.painted < motion.target
    assert min(value[2] for value in state["mouse_rows"].values()) == motion.painted
    clock[0] += .3
    final_rows = render_modal(W, application, views)
    end = pane(application, "logs:workbench")
    assert end.painted == end.limit and state["cursor"] == 0
    assert application.logs.path == "/declared/current.log" and application.logs.cursor is None
    # Its own box hides the old raw log pane, and does not hide its own rail.
    S.publish(application, application.width, application.height, overlays=final_rows)
    assert end.layer == 1 and S.descriptors(application)
    with monkeypatch.context() as guard:
        guard.setattr(W, "_alternate", lambda *args: pytest.fail("pointer requested source"))
        assert S.handle_mouse(application, end.rect.top, end.rect.right - 1, button="wheel-up")


@pytest.mark.parametrize("mode", ["log_tools_page", "log_tools_results", "log_tools_marks"])
def test_nonoverflow_log_tools_still_publish_cached_text_region_without_scroll_controls(mode):
    application = log_app()
    application.mode = mode
    state = T.initialize(application)
    source = {"path": "/declared/exact.log", "label": "stdout"}
    row = dict(line=1, offset=0, text="exact line", raw=b"exact line\n", source=source, before=[], after=[], name="one")
    state["page"] = dict(rows=[row], start=0, end=11, size=11, complete=True, partial=False)
    state["page_source"] = source
    state["results"] = dict(matches=[row], query="exact", reports=[])
    state["marks"] = [row]
    render_modal(T, application, SimpleNamespace(g=L.Glyphs(False)))
    item = pane(application, "log-tools:document")
    assert item.count == 1 and item.limit == 0
    assert not S.descriptors(application) and not S.feedback(application)


@pytest.mark.parametrize("all_file", [False, True])
def test_log_tools_copy_captures_toolbar_destination_before_worker_runs(monkeypatch, all_file):
    from raven import log_copy
    application = log_app()
    application.mode = "log_tools_page"
    application.cfg["clipboard"] = {"destination": "yank", "osc52": False, "tools": False}
    state = T.initialize(application)
    source = {"path": "/declared/exact.log", "label": "stdout"}
    state["page"] = {"rows": [{"raw": b"exact\r\n"}], "snapshot": {"ident": (1, 2)}}
    state["page_source"] = source
    pending, received = [], []
    monkeypatch.setattr(T, "_task", lambda app, label, fn, complete, **kwargs: pending.append(fn))
    monkeypatch.setattr(T.log_scan, "snapshot", lambda *args: {"ident": (1, 2)})
    helper = "copy_full_log" if all_file else "copy_log_selection"
    monkeypatch.setattr(log_copy, helper, lambda *args, **kwargs: received.append(kwargs))
    T._copy_page(application, all_file=all_file)
    application.cfg["clipboard"]["destination"] = "copy"
    pending[0](None, None)
    assert received[0]["destination"] == "yank"
    assert received[0]["use_osc52"] is received[0]["use_tools"] is False


@pytest.mark.parametrize("mode", ["log_tools_page", "log_tools_results", "log_tools_marks"])
@pytest.mark.parametrize("ascii_", [False, True])
def test_log_tools_bar_preserves_selection_bytes_and_mode_local_cursor(clock, mode, ascii_):
    application = log_app()
    application.mode = mode
    state = T.initialize(application)
    source = {"path": "/declared/exact.err", "label": "stderr"}
    rows = [dict(line=index + 1, offset=index * 10, text=f"source row {index}", raw=f"row {index}\r\n".encode())
            for index in range(140)]
    state["page"] = dict(rows=rows, start=0, end=1400, size=1400, complete=True, partial=False)
    state["page_source"] = source
    state["results"] = dict(matches=[dict(row, source=source, before=[], after=[]) for row in rows], query="row", reports=[])
    state["marks"] = [dict(row, source=source, name=f"mark {index}") for index, row in enumerate(rows)]
    state["selection"] = (2, 5)
    chosen_bytes = tuple(row["raw"] for row in rows[2:6])
    views = SimpleNamespace(g=L.Glyphs(ascii_))
    render_modal(T, application, views)
    initial = pane(application, "log-tools:document")
    jump(application, initial)
    clock[0] += .017
    render_modal(T, application, views)
    motion = pane(application, "log-tools:document")
    assert 0 < motion.painted < motion.limit
    assert min(value[0] for value in state["mouse_rows"].values()) == motion.painted
    clock[0] += .3
    render_modal(T, application, views)
    end = pane(application, "log-tools:document")
    assert end.painted == end.limit
    assert state["selection"] == (2, 5)
    assert tuple(row["raw"] for row in state["page"]["rows"][2:6]) == chosen_bytes
    assert state["page_cursor"] == state["result_cursor"] == state["mark_cursor"] == 0
    T.handle_key(application, "down")
    clock[0] += .3
    render_modal(T, application, views)
    focused = pane(application, "log-tools:document")
    assert focused.painted == 0


def test_analysis_sample_guide_keeps_original_occupied_curve_ink():
    rows = [[("title", "dim")], [("           ⡇       ", "fg:#aabbcc")]]
    # The selected timestamp maps to the occupied x=11 cell.
    points = [{"t": 0., "value": 0.}, {"t": 1., "value": 1.}]
    selected = {"t": 2. / 19, "value": 1.}
    plotted = A._crosshair(rows, 20, 1, points, selected, (0., 1.), False, (1, 10, 2, 20))
    assert plotted[1] == [(char, "fg:#aabbcc") for char in "           ⡇       "]
