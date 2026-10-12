"""Terminal mouse reports remain data, including fragmented legacy reports."""
from collections import deque
import curses

import pytest

from raven import chart_interaction, metric_live, pane_drag, screen, scrollbars
from raven.config import Config
from raven.controller import App
from raven.layout import Glyphs
from raven.model import Job, Store
from raven.views import TABS, Views


class Window:
    def __init__(self, text=""):
        self.values = deque(text)

    def get_wch(self):
        if self.values:
            return self.values.popleft()
        raise curses.error("empty")

    def timeout(self, value):
        pass

    def keypad(self, enabled):
        pass


def report(protocol, code, x=81, y=24):
    if protocol == "sgr":
        return f"\x1b[<{code};{x + 1};{y + 1}M"
    if protocol == "urxvt":
        return f"\x1b[{code + 32};{x + 1};{y + 1}M"
    return "\x1b[M" + "".join(chr(value) for value in (code + 32, x + 33, y + 33))


@pytest.mark.parametrize("protocol", ["sgr", "urxvt", "x10"])
@pytest.mark.parametrize("code,flag", [(0, "BUTTON1_PRESSED"), (2, "BUTTON3_PRESSED"),
    (3, "BUTTON1_RELEASED"), (32, "BUTTON1_PRESSED"), (35, "REPORT_MOUSE_POSITION"),
    (64, "BUTTON4_PRESSED"), (65, "BUTTON5_PRESSED")])
def test_protocols_decode_coordinates_as_data_and_preserve_next_command(protocol, code, flag, monkeypatch):
    monkeypatch.setattr(screen.time, "monotonic", lambda: 0)
    reader = screen._InputReader(Window(report(protocol, code) + "q"))
    event = reader.read(curses)
    assert event[0] == "mouse"
    assert event[1][:4] == (0, 81, 24, 0)
    assert event[1][4] & getattr(curses, flag)
    assert event[1].held == (None if code & 64 else code & 3 < 3)
    assert not reader.queue
    assert reader.read(curses) == ("q", None)


@pytest.mark.parametrize("protocol", ["urxvt", "x10"])
def test_slow_fragmented_legacy_report_keeps_its_prefix_and_never_replays_research_key(protocol, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(screen.time, "monotonic", lambda: now[0])
    window = Window("\x1b[")
    reader = screen._InputReader(window)
    assert reader.read(curses) is None
    now[0] = .1
    assert reader.read(curses) == (None, None)
    events = []
    for char in report(protocol, 35)[2:]:
        window.values.append(char)
        event = reader.read(curses)
        if event is not None and event[0] is not None:
            events.append(event)
        now[0] += .05
    assert events == [("mouse", (0, 81, 24, 0, curses.REPORT_MOUSE_POSITION))]
    assert not reader.queue
    window.values.append("q")
    assert reader.read(curses) == ("q", None)


def test_timed_out_sgr_suffix_retains_pointer_update_without_losing_following_command(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(screen.time, "monotonic", lambda: now[0])
    window = Window("\x1b[<35;82")
    reader = screen._InputReader(window)
    assert reader.read(curses) is None
    now[0] = .1
    assert reader.read(curses) == (None, None)
    window.values.extend(";25Mq")
    assert reader.read(curses) == ("mouse", (0, 81, 24, 0, curses.REPORT_MOUSE_POSITION))
    assert reader.read(curses) == ("q", None)
    assert not reader.queue


@pytest.mark.parametrize("sequence", ["\x1b[99r", "\x1b[12;81r", "\x1b[?1003h", "\x1b[99~"])
def test_unknown_csi_never_dispatches_its_final_letter_as_a_shortcut(sequence, monkeypatch):
    monkeypatch.setattr(screen.time, "monotonic", lambda: 0)
    reader = screen._InputReader(Window(sequence + "q"))
    assert reader.read(curses) == (None, None)
    assert not reader.queue
    assert reader.read(curses) == ("q", None)


@pytest.mark.parametrize("code", [128, 129, 130, 255])
def test_unsupported_extra_mouse_buttons_are_not_reinterpreted_as_left_clicks(code, monkeypatch):
    monkeypatch.setattr(screen.time, "monotonic", lambda: 0)
    reader = screen._InputReader(Window(report("sgr", code) + "q"))
    assert reader.read(curses) == ("mouse", None)
    assert reader.read(curses) == ("q", None)


@pytest.mark.parametrize("tab", [name for name, _ in TABS])
def test_legacy_hover_coordinates_cannot_activate_any_page_shortcut(tab, monkeypatch):
    monkeypatch.setattr(screen.time, "monotonic", lambda: 0)
    cfg = Config({"animations": False, "startup_animation": False, "gpu_sampling": False})
    store = Store(persist=False)
    store.jobs = [Job("7", "training", "gpu", "RUNNING", cpus=4)]
    app = App(store, None, None, cfg, "test", interactive=False)
    app.tab = tab
    views = Views(Glyphs(False), cfg)
    app.views_ref = views
    cache = screen._FrameCache()
    cache.rebuild(app, views, store, None, 160, 40)
    reader = screen._InputReader(Window(report("x10", 35) * 25))
    calls = []
    app.handle = calls.append
    try:
        for _ in range(25):
            event = reader.read(curses)
            assert event[0] == "mouse"
            effects = screen._InputEffects()
            effects.record(app, event, curses)
            screen._apply_input(app, event, cache.hits, curses)
            assert not effects.document
            assert app.tab == tab and not app.marks and not app.quit
        assert not calls
    finally:
        if app.research:
            app.research.close()


def test_held_left_motion_without_capture_keeps_document_cached():
    app = App(Store(persist=False), None, None, Config(), "test", interactive=False)
    effects = screen._InputEffects()
    event = ("mouse", (0, 80, 15, 0, curses.REPORT_MOUSE_POSITION | curses.BUTTON1_PRESSED))
    effects.record(app, event, curses)
    assert not effects.document


def test_protocol_metadata_distinguishes_proven_no_button_from_unspecified_curses_motion():
    report_ = screen.MouseReport((0, 81, 24, 0, curses.REPORT_MOUSE_POSITION), held=False)
    assert report_ == (0, 81, 24, 0, curses.REPORT_MOUSE_POSITION)
    assert report_.held is False
    assert getattr(tuple(report_), "held", None) is None


@pytest.mark.parametrize("flag", [curses.BUTTON1_CLICKED, curses.BUTTON1_DOUBLE_CLICKED,
    curses.BUTTON1_RELEASED, curses.BUTTON3_CLICKED, curses.BUTTON3_PRESSED,
    curses.BUTTON4_PRESSED, curses.BUTTON5_PRESSED])
def test_completed_actions_with_position_flag_are_never_coalesced(flag):
    app = App(Store(persist=False), None, None, Config(), "test", interactive=False)
    event = ("mouse", (0, 80, 15, 0, flag | curses.REPORT_MOUSE_POSITION))
    if flag in (curses.BUTTON4_PRESSED, curses.BUTTON5_PRESSED):
        assert screen._batchable_input(app, event, curses)
    else:
        assert not screen._batchable_input(app, event, curses)
    effects = screen._InputEffects()
    effects.record(app, event, curses)
    assert effects.document


@pytest.fixture
def graph_dashboard():
    cfg = Config({"animations": False, "startup_animation": False, "gpu_sampling": False})
    store = Store(persist=False)
    store.jobs = [Job(str(jid), "training-" + str(jid), "cpu", "RUNNING", cpus=4)
                  for jid in range(7, 107)]
    for index in range(20):
        store.record("7", {"k": "live", "t": 180.0 + index, "cpu": index / 25,
                           "rss": 1024 ** 3})
    app = App(store, None, None, cfg, "test", interactive=False)
    app.job_panel_state.update(mode="analytics", analytics_view="job")
    views = Views(Glyphs(False), cfg)
    app.views_ref = views
    cache = screen._FrameCache()
    cache.rebuild(app, views, store, None, 190, 90)
    yield app, cache, store
    if app.research:
        app.research.close()


@pytest.mark.parametrize("protocol", ["sgr", "urxvt", "x10"])
@pytest.mark.parametrize("owner", ["jobs", "chart", "metric", "toolbar", "divider", "scrollbar", "text", "dock"])
def test_proven_unheld_hover_finishes_lost_release_without_changing_page_or_marks(graph_dashboard, owner, protocol, monkeypatch):
    monkeypatch.setattr(screen.time, "monotonic", lambda: 0)
    app, cache, _ = graph_dashboard
    if owner == "jobs":
        y, _, _ = next(hit for hit in cache.hits if hit[1] == "job")
        app.click(y, 2, cache.hits, button="press")
        assert app.job_selection_state["capture"]
    elif owner == "chart":
        plot = next(plot for plot in app.chart_interaction_state["plots"] if plot.key[0] == "resource-series")
        app.click(plot.visible.top + 1, plot.visible.left + 3, cache.hits, button="press")
        assert chart_interaction.active(app)
    elif owner == "metric":
        control = metric_live.initialize(app)["records"][0]
        app.click(control.slider.top, control.slider.right - 1, cache.hits, button="press")
        assert metric_live.active(app)
    elif owner == "toolbar":
        y, left, _, _, _ = next(hit for hit in app.toolbar_state["hits"] if hit[3] == "track")
        app.click(y, left, cache.hits, button="press")
        assert app.toolbar_state["dragging"]
    elif owner == "divider":
        divider = next(item for item in app.pane_drag_state["dividers"].values() if item.axis == "vertical")
        assert pane_drag.handle_mouse(app, divider.y + divider.height // 2, divider.x, button="press")
        assert pane_drag.active(app)
    elif owner == "scrollbar":
        pane = next(pane for pane in app.scrollbar_state["panes"] if pane.key == "jobs")
        start, _, _ = scrollbars._thumb(pane)
        assert scrollbars.handle_mouse(app, start, pane.rect.right - 1, button="press")
        assert app.scrollbar_state["capture"]
    elif owner == "text":
        app.text_selection_state.update(capture={"key": "existing"},
                                        selection={"key": "existing", "anchor": 1, "end": 4})
    else:
        app.history_browser_state["drag"] = {"context": "existing"}
    app.marks = {"7", "8"}
    monkeypatch.setattr(app, "click", lambda *a, **k: pytest.fail("unheld hover resumed a stale gesture"))
    monkeypatch.setattr(app, "run_command", lambda *a, **k: pytest.fail("unheld hover activated a command"))
    reader = screen._InputReader(Window(report(protocol, 35)))
    screen._apply_input(app, reader.read(curses), cache.hits, curses)
    assert app.tab == "jobs" and app.marks == {"7", "8"}
    for name in ("job_selection_state", "text_selection_state", "chart_interaction_state",
                 "metric_live_state", "pane_drag_state", "scrollbar_state"):
        assert not getattr(app, name).get("capture")
    assert not app.toolbar_state["dragging"] and not app.history_browser_state["drag"]
    if owner == "text":
        assert app.text_selection_state["selection"]["end"] == 4


def test_generic_curses_motion_keeps_omitted_held_bit_drag_compatibility(graph_dashboard):
    app, cache, _ = graph_dashboard
    control = metric_live.initialize(app)["records"][0]
    app.click(control.slider.top, control.slider.left, cache.hits, button="press")
    event = ("mouse", (0, control.slider.right - 1, control.slider.top, 0, curses.REPORT_MOUSE_POSITION))
    screen._apply_input(app, event, cache.hits, curses)
    assert metric_live.active(app)


def test_coalescing_retains_final_held_job_endpoint_before_unheld_report(graph_dashboard, monkeypatch):
    monkeypatch.setattr(screen.time, "monotonic", lambda: 0)
    app, cache, _ = graph_dashboard
    job_rows = [hit for hit in cache.hits if hit[1] == "job"]
    first_y, _, first_id = job_rows[0]
    last_y, _, last_id = job_rows[1]
    app.click(first_y, 2, cache.hits, button="press")
    held = screen.MouseReport((0, 2, last_y, 0, curses.REPORT_MOUSE_POSITION | curses.BUTTON1_PRESSED), held=True)
    window = Window(report("sgr", 35))
    pending = screen._consume_input_batch(app, window, curses, cache.hits, ("mouse", held))
    if pending is not None:
        screen._apply_input(app, pending, cache.hits, curses)
    assert app.marks == {first_id, last_id}
    assert app.selected_id == last_id and not app.job_selection_state["capture"]


@pytest.mark.parametrize("invalid", ["negative-x", "outside-x", "stale-selection", "stale-hit-map", "unpublished-hit-map"])
def test_double_click_cannot_bypass_rejected_row_activation(graph_dashboard, invalid, monkeypatch):
    app, cache, _ = graph_dashboard
    y, _, key = next(hit for hit in cache.hits if hit[1] == "job")
    x, hits = 2, cache.hits
    if invalid == "negative-x":
        x = -1
    elif invalid == "outside-x":
        x = app.width
    elif invalid == "stale-selection":
        app.selected_id = "8"
    elif invalid == "stale-hit-map":
        hits = [(hy, kind, "8" if kind == "job" and hy == y else value) for hy, kind, value in hits]
    else:
        app.interaction_state["published_hit_token"] = None
    monkeypatch.setattr(app, "handle", lambda *a: pytest.fail("rejected double-click activated Enter"))
    screen._apply_input(app, ("mouse", (0, x, y, 0, curses.BUTTON1_DOUBLE_CLICKED)), hits, curses)
    assert app.mode == "main" and app.tab == "jobs"


def test_invalid_sgr_quarantine_survives_a_long_gap(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(screen.time, "monotonic", lambda: now[0])
    window = Window("\x1b[<35;" + "1" * 25)
    reader = screen._InputReader(window)
    assert reader.read(curses) == (None, None)
    assert reader.read(curses) is None
    assert reader.discard_mouse
    now[0] = .1
    assert reader.read(curses) is None
    window.values.extend(";82;25Mq")
    assert reader.read(curses) == ("q", None)
    assert not reader.queue


@pytest.mark.parametrize("protocol", ["sgr", "urxvt", "x10"])
@pytest.mark.parametrize("delay", [.05, .12, .8])
@pytest.mark.parametrize("x,y", [(81, 24), (24, 81), (81, 81)])
def test_bare_escape_timeout_recovers_delayed_mouse_packet_without_dispatching_bracket_or_coordinates(protocol, delay, x, y, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(screen.time, "monotonic", lambda: now[0])
    sequence = report(protocol, 35, x, y)
    window = Window(sequence[:1])
    reader = screen._InputReader(window)
    assert reader.read(curses) is None
    now[0] = delay
    assert reader.read(curses) == ("esc", None)
    window.values.extend(sequence[1:] + "q")
    assert reader.read(curses) == ("mouse", (0, x, y, 0, curses.REPORT_MOUSE_POSITION))
    assert not reader.queue
    assert reader.read(curses) == ("q", None)


@pytest.mark.parametrize("text", ["q", "r", "x", "[q", "[r"])
def test_real_keyboard_input_after_bare_escape_is_preserved(text, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(screen.time, "monotonic", lambda: now[0])
    window = Window("\x1b")
    reader = screen._InputReader(window)
    assert reader.read(curses) is None
    now[0] = .1
    assert reader.read(curses) == ("esc", None)
    window.values.extend(text)
    assert [reader.read(curses) for _ in text] == [(char, None) for char in text]


@pytest.mark.parametrize("prefix", ["\x1b[", "\x1b[<35;", "\x1b[M"])
@pytest.mark.parametrize("key,name", [(curses.KEY_MOUSE, "mouse"), (curses.KEY_RESIZE, "resize"),
                                    (curses.KEY_UP, "up")])
def test_native_curses_event_resolves_partial_raw_prefix_without_losing_following_key(prefix, key, name, monkeypatch):
    monkeypatch.setattr(screen.time, "monotonic", lambda: 0)
    mouse = (0, 81, 24, 0, curses.REPORT_MOUSE_POSITION)
    monkeypatch.setattr(curses, "getmouse", lambda: mouse)
    window = Window(prefix)
    reader = screen._InputReader(window)
    assert reader.read(curses) is None
    window.values.extend((key, "q"))
    assert reader.read(curses) == (name, mouse if name == "mouse" else None)
    assert reader.read(curses) == ("q", None)


@pytest.mark.parametrize("protocol", ["sgr", "urxvt", "x10"])
def test_c1_csi_mouse_introducer_has_same_safe_semantics_as_escape_bracket(protocol, monkeypatch):
    monkeypatch.setattr(screen.time, "monotonic", lambda: 0)
    window = Window("\x9b" + report(protocol, 35)[2:] + "q")
    reader = screen._InputReader(window)
    assert reader.read(curses) == ("mouse", (0, 81, 24, 0, curses.REPORT_MOUSE_POSITION))
    assert reader.read(curses) == ("q", None)


@pytest.mark.parametrize("protocol", ["sgr", "urxvt", "x10"])
@pytest.mark.parametrize("protection", ["recent-pointer", "capture"])
def test_pointer_context_grace_keeps_slow_mouse_header_from_emitting_escape(protocol, protection, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(screen.time, "monotonic", lambda: now[0])
    window = Window(report(protocol, 35) if protection == "recent-pointer" else "")
    reader = screen._InputReader(window)
    if protection == "recent-pointer":
        assert reader.read(curses)[0] == "mouse"
    else:
        reader.protect_escape = True
    sequence = report(protocol, 35)
    window.values.extend(sequence[:1])
    assert reader.read(curses) is None
    now[0] = .12
    assert reader.read(curses) is None
    window.values.extend(sequence[1:] + "q")
    assert reader.read(curses) == ("mouse", (0, 81, 24, 0, curses.REPORT_MOUSE_POSITION))
    assert reader.read(curses) == ("q", None)


@pytest.mark.parametrize("protection", ["recent-pointer", "capture"])
def test_real_escape_in_pointer_context_is_bounded_and_still_cancels_selection(protection, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(screen.time, "monotonic", lambda: now[0])
    window = Window(report("sgr", 35) if protection == "recent-pointer" else "")
    reader = screen._InputReader(window)
    if protection == "recent-pointer":
        assert reader.read(curses)[0] == "mouse"
    else:
        reader.protect_escape = True
    window.values.append("\x1b")
    assert reader.read(curses) is None
    now[0] = .199
    assert reader.read(curses) is None
    now[0] = .201
    event = reader.read(curses)
    assert event == ("esc", None)
    app = App(Store(persist=False), None, None, Config(), "test", interactive=False)
    app.marks = {"7", "8"}
    screen._apply_input(app, event, [], curses)
    assert not app.marks


def test_old_pointer_does_not_delay_keyboard_only_escape(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(screen.time, "monotonic", lambda: now[0])
    window = Window(report("sgr", 35))
    reader = screen._InputReader(window)
    assert reader.read(curses)[0] == "mouse"
    now[0] = 1.0
    window.values.append("\x1b")
    assert reader.read(curses) is None
    now[0] = 1.031
    assert reader.read(curses) == ("esc", None)


@pytest.mark.parametrize("x,y", [(120, 9), (9, 120), (200, 200)])
def test_extended_x10_coordinates_are_payload_even_when_they_equal_c1_csi(x, y, monkeypatch):
    monkeypatch.setattr(screen.time, "monotonic", lambda: 0)
    reader = screen._InputReader(Window(report("x10", 35, x, y) + "q"))
    assert reader.read(curses) == ("mouse", (0, x, y, 0, curses.REPORT_MOUSE_POSITION))
    assert reader.read(curses) == ("q", None)
