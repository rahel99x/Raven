"""Pointer feedback uses published frames; document actions retain fresh data."""
from collections import deque
import curses
from types import SimpleNamespace

import pytest

from raven import interaction, layout as L, screen, toolbar
from raven.config import Config
from raven.controller import App
from raven.model import Job, Store
from raven.views import TABS, Views


@pytest.fixture
def dashboard():
    cfg = Config({"animations": False, "startup_animation": False, "log_lines": 0})
    store = Store(persist=False)
    store.jobs = [Job(str(index + 1), "train-" + str(index), "cpu", "RUNNING", cpus=4)
                  for index in range(30)]
    app = App(store, None, None, cfg, "test", interactive=True)
    views = Views(L.Glyphs(False), cfg)
    app.views_ref = views
    cache = screen._FrameCache()
    cache.rebuild(app, views, store, None, 160, 40)
    yield SimpleNamespace(app=app, views=views, store=store, cache=cache, cfg=cfg)
    if app.research:
        app.research.close()


def mouse_report(x, y, state=curses.REPORT_MOUSE_POSITION):
    return "mouse", (0, x, y, 0, state)


def test_hover_feedback_does_not_tick_snapshot_recompose_or_read_files(dashboard, monkeypatch):
    app, views, cache = dashboard.app, dashboard.views, dashboard.cache
    before = tuple(tuple(row) for row in app.last_rows)
    graph = app.interaction_state["graph"]

    def forbidden(*args, **kwargs):
        raise AssertionError("Passive feedback rebuilt the document or performed IO")

    monkeypatch.setattr(app, "tick", forbidden)
    monkeypatch.setattr(dashboard.store, "snapshot", forbidden)
    monkeypatch.setattr(views, "compose", forbidden)
    monkeypatch.setattr(views, "overlay", forbidden)
    monkeypatch.setattr(views.files, "stat", forbidden)
    monkeypatch.setattr(views.files, "tail", forbidden)
    controls = [control for control in graph.controls if control.group == "job"]
    assert len(controls) >= 2
    for control in controls[:2]:
        y, x = control.rect.top, control.rect.left
        screen._apply_input(app, mouse_report(x, y), cache.hits, curses)
        rows, overlays, bar = cache.feedback(app, views)
        assert app.interaction_state["hovered"] == control.id
        assert rows[y] != cache.rows[y]
        assert L.row_text(rows[y]) == L.row_text(cache.rows[y])
        assert app.interaction_state["graph"] is graph
        assert tuple(tuple(row) for row in app.last_rows) == before
    assert not app.marks and app.selected_id == "1"


@pytest.mark.parametrize("mode", ["help", "palette", "confirm", "execution", "project_runs"])
def test_passive_motion_can_be_coalesced_over_every_modal(mode):
    app = SimpleNamespace(mode=mode, tab="jobs", keymap={})
    assert screen._batchable_input(app, mouse_report(7, 8), curses)
    assert not screen._batchable_input(app, ("down", None), curses)


@pytest.mark.parametrize("state", [curses.BUTTON1_PRESSED, curses.BUTTON1_CLICKED,
    curses.BUTTON1_DOUBLE_CLICKED, curses.BUTTON1_RELEASED, curses.BUTTON3_PRESSED,
    curses.BUTTON3_CLICKED, curses.BUTTON4_PRESSED, curses.BUTTON5_PRESSED])
def test_deliberate_gestures_always_rebuild_the_document(dashboard, state):
    effects = screen._InputEffects()
    effects.record(dashboard.app, mouse_report(2, 9, state), curses)
    assert effects.document


@pytest.mark.parametrize("capture", ["slider", "jobs", "probe"])
def test_motion_without_button_bits_rebuilds_when_a_gesture_is_captured(dashboard, capture):
    app = dashboard.app
    if capture == "slider":
        app.toolbar_state["dragging"] = True
    elif capture == "jobs":
        app.job_selection_state["capture"] = {"anchor": "1"}
    else:
        app.mode = "terminal_probe"
    effects = screen._InputEffects()
    effects.record(app, mouse_report(3, 11), curses)
    assert effects.document


def test_continuous_hover_never_moves_maintenance_or_animation_deadlines(dashboard, monkeypatch):
    cache, app, views = dashboard.cache, dashboard.app, dashboard.views
    cache.next_maintenance, cache.next_animation = 10.2, 10.08
    for now in (10.0, 10.02, 10.04, 10.06):
        monkeypatch.setattr(screen.time, "monotonic", lambda: now)
        screen._apply_input(app, mouse_report(5, 11), cache.hits, curses)
        cache.feedback(app, views)
        assert not cache.due(app, 160, 40)
        assert (cache.next_maintenance, cache.next_animation) == (10.2, 10.08)
    assert cache.due(app, 160, 40, now=10.08)
    cache.next_animation = float("inf")
    assert not cache.due(app, 160, 40, now=10.19)
    assert cache.wait_ms(now=10.1) == 100
    assert cache.due(app, 160, 40, now=10.2)
    assert cache.due(app, 80, 40, now=10.1)


def test_due_rebuild_publishes_background_jobs_before_the_next_action(dashboard, monkeypatch):
    app, cache, store = dashboard.app, dashboard.cache, dashboard.store
    initial = tuple(key for _, kind, key in cache.hits if kind == "job")
    store.jobs = [Job("700", "new-worker-publication", "cpu", "RUNNING", cpus=2)]
    assert cache.due(app, 160, 40, now=cache.next_maintenance)
    cache.rebuild(app, dashboard.views, store, None, 160, 40)
    current = tuple(key for _, kind, key in cache.hits if kind == "job")
    assert initial and current == ("700",)
    assert app.selected_id == "700"
    y = next(y for y, kind, _ in cache.hits if kind == "job")
    screen._apply_input(app, mouse_report(5, y, curses.BUTTON1_CLICKED), cache.hits, curses)
    assert app.selected_id == "700"


def test_menu_motion_updates_only_toolbar_and_removes_old_pointer_feedback(dashboard, monkeypatch):
    app, cache, views = dashboard.app, dashboard.cache, dashboard.views
    app.run_command("menu Help")
    cache.rebuild(app, views, dashboard.store, None, 160, 40)
    content = cache.content
    menu_hits = list(app.toolbar_state["menu_hits"])
    assert len(menu_hits) >= 2

    def forbidden(*args, **kwargs):
        raise AssertionError("Menu hover rebuilt a report")

    monkeypatch.setattr(views, "compose", forbidden)
    monkeypatch.setattr(views, "overlay", forbidden)
    monkeypatch.setattr(app, "tick", forbidden)
    for y, left, _, key in menu_hits[:2]:
        screen._apply_input(app, mouse_report(left, y), cache.hits, curses)
        _, overlays, _ = cache.feedback(app, views)
        assert cache.content is content
        assert app.interaction_state["hovered"] == "toolbar:menu:3:" + key
        hovered_rows = {row_y for row_y, _, row in overlays
                        if any(interaction.POINTER_STYLE in style for _, style in row)}
        assert hovered_rows == {y}
    screen._apply_input(app, mouse_report(150, 35), cache.hits, curses)
    cache.feedback(app, views)
    assert app.toolbar_state["menu"] is None
    assert not cache.toolbar


def test_legacy_details_stays_visible_beneath_menu_and_after_cached_hover_close(dashboard, monkeypatch):
    app, cache, views, store = dashboard.app, dashboard.cache, dashboard.views, dashboard.store
    store.details["1"] = {"JobState": "RUNNING", "WorkDir": "/exact/job-one"}
    app.detail_id, app.mode = "1", "details"
    cache.rebuild(app, views, store, None, 160, 40)
    underlying = tuple((y, x, tuple(row)) for y, x, row in cache.content)
    assert underlying and any("/exact/job-one" in L.row_text(row) for _, _, row in underlying)
    app.run_command("menu File")
    cache.rebuild(app, views, store, None, 160, 40)
    assert tuple((y, x, tuple(row)) for y, x, row in cache.content) == underlying
    snapshot, content = cache.snapshot, cache.content

    def forbidden(*args, **kwargs):
        raise AssertionError("Legacy menu hover rebuilt or read the document")

    for target, name in ((app, "tick"), (store, "snapshot"), (views, "compose"),
                         (views, "overlay"), (views.files, "stat"), (views.files, "tail")):
        monkeypatch.setattr(target, name, forbidden)
    y, left, _, _ = app.toolbar_state["menu_hits"][0]
    screen._apply_input(app, mouse_report(left, y), cache.hits, curses)
    _, opened_overlays, _ = cache.feedback(app, views)
    assert len(opened_overlays) > len(underlying)
    screen._apply_input(app, mouse_report(150, 35), cache.hits, curses)
    _, closed_overlays, _ = cache.feedback(app, views)
    assert app.mode == "details" and app.detail_id == "1"
    assert app.toolbar_state["menu"] is None
    assert cache.snapshot is snapshot and cache.content is content
    assert tuple((y, x, tuple(row)) for y, x, row in closed_overlays) == underlying


def test_differential_painter_restores_removed_overlay_and_only_changes_affected_rows():
    erased, painted = [], []
    window = SimpleNamespace(erase=lambda: erased.append(True))

    def paint(y, x, row, width, height):
        painted.append((y, x, tuple(row)))

    painter = screen._DifferentialPainter(window, paint)
    base = [[("base zero", "")], [("AB界e\u0301", "cyan")], [("bottom", "")]]
    overlay = [(1, 2, [("MENU", "bold")])]
    assert painter.draw(base, overlay, 12, 3) == (0, 1, 2)
    assert len(erased) == 1
    painted.clear()
    assert painter.draw(base, overlay, 12, 3) == ()
    assert not painted
    assert painter.draw(base, [], 12, 3) == (1,)
    assert len(erased) == 1
    assert painted[0][0:2] == (1, 2)
    assert L.vlen(L.row_text(painted[0][2])) == 4
    assert "界e\u0301" in L.row_text(painted[0][2])
    assert painter.draw(base, [], 8, 3) == (0, 1, 2)
    assert len(erased) == 2
    painter.invalidate()
    assert painter.draw(base, [], 8, 3) == (0, 1, 2)
    assert len(erased) == 3


def test_differential_toolbar_masks_row_zero_overlay_changes():
    calls = []
    painter = screen._DifferentialPainter(SimpleNamespace(erase=lambda: None),
        lambda y, x, row, width, height: calls.append((y, x, L.row_text(row))))
    base, bar = [[("hidden", "")], [("body", "")]], [("File View", "accent")]
    painter.draw(base, [(0, 1, [("modal", "")])], 12, 2, bar=bar)
    assert calls[0] == (0, 0, "File View   ")
    calls.clear()
    assert painter.draw(base, [(0, 1, [("another modal", "")])], 12, 2, bar=bar) == ()
    assert not calls


@pytest.mark.parametrize("tab", [name for name, _ in TABS])
def test_every_page_reuses_its_published_document_for_toolbar_hover(dashboard, monkeypatch, tab):
    app, views, cache = dashboard.app, dashboard.views, dashboard.cache
    app.tab = tab
    cache.rebuild(app, views, dashboard.store, None, 160, 40)
    graph = app.interaction_state["graph"]
    target = graph.get("toolbar:menu:2")
    assert target is not None

    def forbidden(*args, **kwargs):
        raise AssertionError("A page hover performed document work")

    monkeypatch.setattr(app, "tick", forbidden)
    monkeypatch.setattr(dashboard.store, "snapshot", forbidden)
    monkeypatch.setattr(views, "compose", forbidden)
    monkeypatch.setattr(views, "overlay", forbidden)
    screen._apply_input(app, mouse_report(target.rect.left, target.rect.top), cache.hits, curses)
    _, _, bar = cache.feedback(app, views)
    assert any(interaction.POINTER_STYLE in style for _, style in bar)
    assert app.tab == tab
    assert app.interaction_state["graph"] is graph


class LoopWindow:
    def __init__(self):
        self.erased = 0
        self.frames = 0
        self.timeouts = []

    def getmaxyx(self):
        return 40, 160

    def timeout(self, value):
        self.timeouts.append(value)

    def keypad(self, value):
        pass

    def erase(self):
        self.erased += 1

    def addstr(self, *args):
        pass

    def noutrefresh(self):
        self.frames += 1


def test_actual_loop_cached_hover_then_queued_click_gets_one_fresh_document(dashboard, monkeypatch):
    app, views, store = dashboard.app, dashboard.views, dashboard.store
    counts = dict(compose=0, snapshot=0, tick=0)
    for target, name in ((views, "compose"), (store, "snapshot"), (app, "tick")):
        original = getattr(target, name)

        def observed(*args, _name=name, _original=original, **kwargs):
            counts[_name] += 1
            return _original(*args, **kwargs)

        monkeypatch.setattr(target, name, observed)
    for name in ("raw", "curs_set", "mousemask", "mouseinterval", "doupdate", "set_escdelay"):
        monkeypatch.setattr(curses, name, lambda *args: None)
    monkeypatch.setattr(curses, "has_colors", lambda: False)
    monkeypatch.setattr(screen.time, "monotonic", lambda: 0)
    monkeypatch.setattr(screen, "_mouse_reporting", lambda enabled: None)
    window = LoopWindow()
    monkeypatch.setattr(curses, "wrapper", lambda callback: callback(window))
    row = next(y for y, kind, _ in app.last_hits if kind == "job")
    events = deque([mouse_report(6, row), None, mouse_report(6, row + 1),
                    mouse_report(6, row + 1, curses.BUTTON1_CLICKED), ("q", None)])
    observed_feedback = []

    def read(*args):
        assert events
        event = events.popleft()
        # The first passive endpoint can now be caught up before the first
        # paint. Observe the next hover itself, before its queued click, rather
        # than assuming that cached feedback requires a second terminal paint.
        if event == mouse_report(6, row + 1):
            observed_feedback.append(dict(counts))
        return event

    monkeypatch.setattr(screen, "_read_input", read)
    screen.run_curses(app, views, None, store, None, app.cfg)
    assert not events and app.quit
    assert observed_feedback and observed_feedback[0]["compose"] == 1
    assert observed_feedback[0]["tick"] == 1
    assert counts["compose"] == 3  # Initial, fresh queued click, selected row.
    assert window.erased == 1


def test_actual_loop_services_worker_publications_during_uninterrupted_hover(dashboard, monkeypatch):
    app, views, store = dashboard.app, dashboard.views, dashboard.store
    clock, rebuilt = [0.0], []
    original = views.compose

    def compose(*args, **kwargs):
        rebuilt.append(clock[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(views, "compose", compose)
    for name in ("raw", "curs_set", "mousemask", "mouseinterval", "doupdate", "set_escdelay"):
        monkeypatch.setattr(curses, name, lambda *args: None)
    monkeypatch.setattr(curses, "has_colors", lambda: False)
    monkeypatch.setattr(screen.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(screen, "_mouse_reporting", lambda enabled: None)
    window = LoopWindow()
    monkeypatch.setattr(curses, "wrapper", lambda callback: callback(window))
    events = deque()
    for index in range(6):
        events.extend(("hover", None))
    events.append("quit")

    def read(*args):
        assert events
        value = events.popleft()
        if value == "hover":
            clock[0] += .05
            if clock[0] >= .15:
                store.jobs = [Job("800", "published-during-hover", "cpu", "RUNNING")]
            return mouse_report(5, 10)
        if value == "quit":
            return "q", None
        return None

    monkeypatch.setattr(screen, "_read_input", read)
    screen.run_curses(app, views, None, store, None, app.cfg)
    assert len(rebuilt) == 2
    assert rebuilt == [0, .2]
    assert app.selected_id == "800"
    assert window.frames >= 6
    assert window.erased == 1


def test_actual_loop_repaints_unchanged_style_names_after_a_theme_change(dashboard, monkeypatch):
    app = dashboard.app
    original = app.handle

    def handle(key):
        if key == "t":
            app.set_theme("high")
        else:
            original(key)

    monkeypatch.setattr(app, "handle", handle)
    for name in ("raw", "curs_set", "mousemask", "mouseinterval", "doupdate", "set_escdelay"):
        monkeypatch.setattr(curses, name, lambda *args: None)
    monkeypatch.setattr(curses, "has_colors", lambda: False)
    monkeypatch.setattr(screen.time, "monotonic", lambda: 0)
    monkeypatch.setattr(screen, "_mouse_reporting", lambda enabled: None)
    window = LoopWindow()
    monkeypatch.setattr(curses, "wrapper", lambda callback: callback(window))
    events = deque([("t", None), ("q", None)])
    monkeypatch.setattr(screen, "_read_input", lambda *args: events.popleft())
    screen.run_curses(app, dashboard.views, None, dashboard.store, None, app.cfg)
    assert app.theme == "high"
    assert window.erased == 2
