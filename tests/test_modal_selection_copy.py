"""Report copy keys respect explicit rows without losing complete exports."""
from types import SimpleNamespace
import json

import pytest

from tower import clipboard, layout as L, operations, ops_ui, telemetry_ui, text_selection
from tower.config import Config
from tower.controller import App
from tower.model import Job, Store
from tower.screen import _FrameCache
from tower.views import Views


@pytest.fixture(params=["operations", "telemetry"])
def report_ui(request, monkeypatch):
    cfg = Config({"animations": False, "startup_animation": False, "gpu_sampling": False})
    store = Store(persist=False)
    store.apply_jobs([Job("41", "PRIVATE BACKGROUND JOB", "cpu", "RUNNING")])
    app = App(store, None, None, cfg, "tester", interactive=False)
    views = Views(L.Glyphs(False), cfg)
    app.views_ref = views
    module = ops_ui if request.param == "operations" else telemetry_ui
    app.mode = request.param
    state = module.initialize(app)
    lines = ["report first", "chosen second", "chosen third", "report fourth"]
    if request.param == "operations":
        result = operations.report("slurm-doctor", lines[0], rows=lines[1:],
                                   data={"extra": "complete structured evidence"})
        state.update(view="report", feature="slurm-doctor", result=result, generation=1)
        complete = json.dumps(result, indent=2, ensure_ascii=True, allow_nan=False) + "\n"
    else:
        state.update(lines=lines, generation=1)
        complete = "\n".join(lines) + "\n"
    copies = []
    monkeypatch.setattr(clipboard, "copy", lambda text, *args, **kwargs: copies.append(text) or "copied")
    cache = _FrameCache()

    def draw(width=110, height=34):
        cache.rebuild(app, views, store, None, width, height)

    def click(index, *, shift=False, button="left", occurrence=0):
        y, left, _, _ = [hit for hit in state["line_hits"] if hit[-1] == index][occurrence]
        app.click(y, left + 1, app.last_hits, button=button, shift=shift)
        return y, left + 1

    draw()
    yield SimpleNamespace(app=app, state=state, lines=lines, copies=copies,
                          complete=complete, draw=draw, click=click, module=module)
    if app.research:
        app.research.close()


@pytest.mark.parametrize("button", ["left", "press"])
def test_explicit_single_report_row_yanks_only_that_line(report_ui, button):
    d = report_ui
    y, x = d.click(1, button=button)
    if button == "press":
        d.app.click(y, x, d.app.last_hits, button="release")
    d.draw()
    d.app.handle("y")
    assert d.copies == ["chosen second\n"]
    assert d.app.mode in ("operations", "telemetry")
    assert "PRIVATE BACKGROUND JOB" not in d.copies[-1]


@pytest.mark.parametrize("start,end", [(1, 3), (3, 1)])
def test_report_shift_range_copies_exact_source_lines_in_order(report_ui, start, end):
    d = report_ui
    d.click(start)
    d.draw()
    d.click(end, shift=True)
    d.draw()
    d.app.handle("y")
    assert d.copies == ["\n".join(d.lines[1:4]) + "\n"]


def test_keyboard_row_navigation_copies_the_chosen_line_without_visual_mode(report_ui):
    d = report_ui
    d.app.handle("down")
    d.draw()
    d.app.handle("y")
    assert d.copies == ["chosen second\n"]


@pytest.mark.parametrize("method", ["key", "button", "command"])
def test_explicit_full_report_copy_ignores_native_line_range(report_ui, method):
    d = report_ui
    d.click(1)
    d.draw()
    d.click(2, shift=True)
    d.draw()
    if method == "key":
        d.app.handle("Y")
    elif method == "button":
        y, _, hit = next(hit for hit in d.state["control_hits"] if hit[2]["id"].endswith(":copy"))
        d.app.click(y, hit["left"], d.app.last_hits, button="press")
        d.app.click(y, hit["left"], d.app.last_hits, button="release")
    else:
        d.app.run_command(("ops" if d.app.mode == "operations" else "telemetry") + " copy")
    assert d.copies == [d.complete]


def test_explicit_modal_row_replaces_carried_rendered_selection(report_ui):
    d = report_ui
    d.app.handle("v")
    d.app.handle("down")
    assert text_selection.selected(d.app)
    d.draw()
    d.click(2)
    assert not text_selection.selected(d.app)
    d.app.handle("y")
    assert d.copies == ["chosen third\n"]


def test_full_report_key_replaces_rendered_range_too(report_ui):
    d = report_ui
    d.app.handle("v")
    d.app.handle("down")
    assert text_selection.selected(d.app)
    d.app.handle("Y")
    assert d.copies == [d.complete]


def test_wrapped_report_row_copies_the_complete_original_line(report_ui):
    d = report_ui
    long_line = "unicode 界 τ / " * 9
    d.lines[1] = long_line
    if d.app.mode == "operations":
        d.state["result"]["rows"][0] = long_line
    d.state["wrapped"] = None
    d.draw(width=55, height=38)
    assert len([hit for hit in d.state["line_hits"] if hit[-1] == 1]) > 1
    d.click(1, occurrence=1)
    d.app.handle("y")
    assert d.copies == [long_line + "\n"]


def test_fresh_click_after_modal_right_click_copies_one_new_line(report_ui):
    d = report_ui
    d.click(1)
    d.draw()
    d.click(3, shift=True)
    y, left, _, _ = next(hit for hit in d.state["line_hits"] if hit[-1] == 2)
    d.app.click(y, left, d.app.last_hits, button="right")
    assert d.state["anchor"] is None and not d.state["row_selected"]
    d.draw()
    d.click(2)
    d.app.handle("y")
    assert d.copies == ["chosen third\n"]


def test_moving_the_pointer_cannot_expand_a_single_report_copy(report_ui):
    d = report_ui
    d.click(1)
    d.draw()
    y, left, _, _ = next(hit for hit in d.state["line_hits"] if hit[-1] == 3)
    for button in ("motion", "drag", "release"):
        d.app.click(y, left + 1, d.app.last_hits, button=button)
    d.app.handle("y")
    assert d.copies == ["chosen second\n"]
