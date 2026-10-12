"""Real viewport adapters retain exact data while wheel frames are interpolated."""
from pathlib import Path
from types import SimpleNamespace
import curses
import json

import pytest

from raven import analysis_ui, clipboard, layout as L, screen, scrolling
from raven.config import Config
from raven.controller import App
from raven.metrics import MetricReader
from raven.model import Job, Store
from raven.views import Views


def frame(app, views, *, width=120, height=28):
    app.width = width
    scrolling.begin_frame(app)
    rows, hits = views.compose(app.store.snapshot(), app, width, height)
    scrolling.finish_frame(app)
    app.last_hits = hits
    return rows, hits


def text(rows):
    return "\n".join(L.row_text(row) for row in rows)


@pytest.fixture
def clock(monkeypatch):
    value = [100.0]
    monkeypatch.setattr(scrolling.time, "monotonic", lambda: value[0])
    return value


@pytest.fixture
def log_document(tmp_path, clock):
    cfg = Config({"log_lines": 0, "startup_animation": False,
                  "clipboard": {"osc52": False, "tools": True}})
    store = Store(state_dir=str(tmp_path / "state"))
    job = Job("77", "exact log", "cpu", "RUNNING")
    store.apply_jobs([job])
    source = tmp_path / "job-77.stdout"
    original = [f"original_{index:04}\tpayload 界\r\n".encode() for index in range(600)]
    source.write_bytes(b"".join(original))
    store.details["77"] = {"StdOut": str(source)}
    app = App(store, None, None, cfg, "tester", interactive=True)
    views = Views(L.Glyphs(False), cfg)
    app.views_ref = views
    frame(app, views)
    app.open_log("77")
    frame(app, views)
    yield app, views, source, original
    if app.research is not None:
        app.research.close()


def wheel(app, direction, count=1):
    flag = curses.BUTTON4_PRESSED if direction < 0 else curses.BUTTON5_PRESSED
    event = ("mouse", (0, 4, app.body_origin + 4, 0, flag))
    for _ in range(count):
        screen._apply_input(app, event, app.last_hits, curses)


@pytest.mark.parametrize("width,height,ascii_", [(120, 28, False), (60, 20, True), (160, 40, False)])
def test_plain_logs_paint_intermediate_rows_then_exact_endpoint(log_document, clock, width, height, ascii_):
    app, views, source, original = log_document
    views.g = L.Glyphs(ascii_)
    _, initial_hits = frame(app, views, width=width, height=height)
    before = min(int(key) for _, kind, key in initial_hits if kind == "log_line")
    wheel(app, -1, 12)
    target = app.logs.top
    assert target < before
    clock[0] += .017
    rows, hits = frame(app, views, width=width, height=height)
    painted = min(int(key) for _, kind, key in hits if kind == "log_line")
    assert target < painted < before and scrolling.active(app)
    assert app.logs.top == target  # Paint has not replaced the logical offset.
    for y, kind, index in hits:
        if kind == "log_line":
            assert f"original_{int(index):04}" in L.row_text(rows[y])
    clock[0] += .25
    _, hits = frame(app, views, width=width, height=height)
    assert min(int(key) for _, kind, key in hits if kind == "log_line") == target
    assert not scrolling.active(app)


def test_keyboard_after_wheel_makes_log_cursor_visible_and_visual_bytes_exact(log_document, clock, monkeypatch):
    app, views, source, original = log_document
    wheel(app, -1, 20)
    clock[0] += .015
    frame(app, views)
    assert scrolling.active(app)
    screen._apply_input(app, ("down", None), app.last_hits, curses)
    rows, hits = frame(app, views)
    assert not scrolling.active(app)
    visible = {int(index) for _, kind, index in hits if kind == "log_line"}
    assert app.logs.cursor in visible
    selected = app.logs.cursor
    screen._apply_input(app, ("v", None), hits, curses)
    screen._apply_input(app, ("down", None), hits, curses)
    rows, hits = frame(app, views)
    assert app.logs.selection_anchor == selected and app.logs.selection_end == selected + 1
    assert {selected, selected + 1} <= {int(index) for _, kind, index in hits if kind == "log_line"}
    assert app.logs.selection_bytes(app.logs.buffers[str(source)]) == b"".join(original[selected:selected + 2])
    assert any("◆" in L.row_text(row) for row in rows)


def test_log_wheel_reversal_preserves_each_move_and_resize_snaps_safe_visible_range(log_document, clock):
    app, views, source, original = log_document
    _, hits = frame(app, views)
    start = min(int(index) for _, kind, index in hits if kind == "log_line")
    wheel(app, -1, 20)
    clock[0] += .04
    _, hits = frame(app, views)
    first = min(int(index) for _, kind, index in hits if kind == "log_line")
    assert app.logs.top < first < start
    old_cursor = app.logs.cursor
    wheel(app, 1, 12)
    assert app.logs.cursor == old_cursor + 36
    clock[0] += .017
    rows, hits = frame(app, views, width=52, height=18)
    assert not scrolling.active(app)  # The resized viewport is a new geometry.
    visible = [(y, int(index)) for y, kind, index in hits if kind == "log_line"]
    assert visible and all(0 <= y < 18 and 0 <= index < len(original) for y, index in visible)
    assert app.logs.cursor in {index for _, index in visible}
    assert all(L.vlen(L.row_text(row)) <= 52 for row in rows)


@pytest.mark.parametrize("preference", ["reader", "animations", "smoothscroll"])
def test_motion_preferences_changed_during_log_easing_take_effect_on_next_frame(log_document, clock, preference):
    app, views, source, original = log_document
    wheel(app, -1, 12)
    clock[0] += .017
    frame(app, views)
    assert scrolling.active(app)
    if preference == "reader":
        app.theme = "reader"
    elif preference == "animations":
        app.cfg.set("animations", False)
    else:
        assert scrolling.run_command(app, ["smoothscroll", "off"])
    clock[0] += .017
    _, hits = frame(app, views)
    assert not scrolling.active(app)
    assert app.logs.cursor in {int(index) for _, kind, index in hits if kind == "log_line"}


def test_copy_all_during_scroll_animation_still_exports_entire_original_file(log_document, clock, monkeypatch):
    app, views, source, original = log_document
    copied = []
    def copy_file(path, **kwargs):
        data = Path(path).read_bytes()
        copied.append(data)
        return {"methods": ["test clipboard"], "warnings": [], "bytes": len(data), "text": True}
    monkeypatch.setattr(clipboard, "copy_file", copy_file)
    wheel(app, -1, 15)
    clock[0] += .016
    frame(app, views)
    assert scrolling.active(app)
    screen._apply_input(app, ("Y", None), app.last_hits, curses)
    assert not scrolling.active(app)
    assert app.research.pending is not None
    app.research.pending[0].result(timeout=3)
    app.research.poll_task()
    assert copied == [b"".join(original)]
    assert b"original_0000" in copied[0] and b"original_0599" in copied[0]


@pytest.fixture
def research_document(tmp_path, clock):
    cfg = Config({"log_lines": 0, "startup_animation": False})
    store = Store(persist=False)
    job = Job("7", "reported metrics", "cpu", "RUNNING")
    store.apply_jobs([job])
    metrics = tmp_path / "metrics.jsonl"
    metrics.write_text("".join(json.dumps({"t": index + 1, "metrics": {f"metric_{metric:02}": metric + index / 10 for metric in range(64)}}) + "\n" for index in range(8)))
    result = MetricReader().read(metrics)
    app = App(store, None, None, cfg, "tester", interactive=True)
    views = Views(L.Glyphs(False), cfg)
    app.views_ref = views
    app.tab, app.research_job_id, app.research_view = "research", "7", "experiment"
    app.research = SimpleNamespace(settings=cfg["research"],
        context=lambda snap, app: {"job": job, "jid": "7", "generation": 1},
        request=lambda context: result)
    return app, views, result


def assert_metric_hit_alignment(rows, hits):
    for y, kind, metric in hits:
        if kind == "research_metric":
            assert metric in L.row_text(rows[y])


@pytest.mark.parametrize("width,height", [(120, 28), (60, 20), (180, 48)])
def test_virtualized_experiment_rasterizes_only_painted_cards_during_wheel_motion(research_document, clock, monkeypatch, width, height):
    app, views, result = research_document
    rasterized = []
    original = analysis_ui.chart_rows
    def chart(*args, **kwargs):
        rasterized.append(args[5])
        return original(*args, **kwargs)
    monkeypatch.setattr(analysis_ui, "chart_rows", chart)
    rows, hits = frame(app, views, width=width, height=height)
    assert len(rasterized) <= (height + 8) // 9 + 1
    assert_metric_hit_alignment(rows, hits)
    assert app.research_rows > height * 5
    wheel(app, 1, 40)
    target = app.research_scroll
    assert target == 120
    clock[0] += .016
    rasterized.clear()
    rows, hits = frame(app, views, width=width, height=height)
    painted = scrolling.published_position(app, "research:document", target)
    assert 0 < painted < target and scrolling.active(app)
    assert app.research_scroll == target
    assert len(rasterized) <= (height + 8) // 9 + 1
    assert rasterized and "metric_" in text(rows)
    assert_metric_hit_alignment(rows, hits)
    # Visible content must be the nearby painted cards, not placeholder rows
    # reserved for the future target's offscreen cards.
    assert min(int(name.removeprefix("metric_")) for name in rasterized) <= painted // 9
    clock[0] += .25
    rasterized.clear()
    rows, hits = frame(app, views, width=width, height=height)
    assert not scrolling.active(app)
    assert scrolling.published_position(app, "research:document", target) == target
    assert len(rasterized) <= (height + 8) // 9 + 1
    assert_metric_hit_alignment(rows, hits)


def test_research_keyboard_end_while_easing_renders_last_metric_immediately(research_document, clock, monkeypatch):
    app, views, result = research_document
    frame(app, views)
    wheel(app, 1, 30)
    clock[0] += .016
    frame(app, views)
    assert scrolling.active(app)
    screen._apply_input(app, ("end", None), app.last_hits, curses)
    rows, hits = frame(app, views)
    assert not scrolling.active(app)
    assert "metric_63" in text(rows)
    assert_metric_hit_alignment(rows, hits)


def test_research_repeated_targets_reversal_and_endpoint_keep_virtualized_frames_nonempty(research_document, clock, monkeypatch):
    app, views, result = research_document
    rasterized = []
    original = analysis_ui.chart_rows
    def chart(*args, **kwargs):
        rasterized.append(args[5])
        return original(*args, **kwargs)
    monkeypatch.setattr(analysis_ui, "chart_rows", chart)
    frame(app, views, height=32)
    for direction, count, delay in [(1, 25, .017), (1, 20, .03), (-1, 15, .02), (-1, 8, .03), (1, 80, .017), (1, 1, .24)]:
        wheel(app, direction, count)
        logical = app.research_scroll
        clock[0] += delay
        rasterized.clear()
        rows, hits = frame(app, views, height=32)
        assert app.research_scroll == logical
        assert 1 <= len(rasterized) <= 5
        assert "metric_" in text(rows)
        assert_metric_hit_alignment(rows, hits)
    clock[0] += .3
    frame(app, views, height=32)
    assert not scrolling.active(app)
