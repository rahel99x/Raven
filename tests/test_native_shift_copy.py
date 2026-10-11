"""Native Shift ranges and Advisor drags retain exact painted pane ownership."""
import pytest

from tower import clipboard, history_browser, text_selection as T
from tower.config import Config
from tower.controller import App
from tower.layout import Glyphs
from tower.model import Finished, Health, Job, Live, Store
from tower.screen import _FrameCache
from tower.views import Views


@pytest.mark.parametrize("reverse", [False, True])
def test_source_shift_click_uses_rendered_range_instead_of_legacy_screen_rows(monkeypatch, reverse):
    config = Config({"animations": False, "startup_animation": False, "smooth_scrolling": False})
    store = Store(persist=False)
    store.health = {name: Health(name, last_ok=1) for name in ("jobs", "live", "gpu")}
    app = App(store, None, None, config, "test", interactive=False)
    app.views_ref = Views(Glyphs(False), config)
    app.enter_tab("sources")
    frame = _FrameCache()
    draw = lambda: frame.rebuild(app, app.views_ref, store, None, 140, 35)
    copies = []
    monkeypatch.setattr(clipboard, "copy", lambda text, *a, **kw: copies.append(text) or "copied")
    draw()
    rows = [y for y, kind, _ in app.last_hits if kind == "source"]
    start, end = (rows[-1], rows[0]) if reverse else (rows[0], rows[-1])
    app.click(start, 8, app.last_hits)
    draw()
    app.click(end, 8, app.last_hits, shift=True)
    assert app.sel_anchor is None
    assert T.initialize(app)["selection"]["key"] == "sources"
    expected = T.selection_text(app)
    app.handle("y")
    assert copies == [expected]
    assert copies[0].count("\n") == len(rows)
    assert "File" not in copies[0] and "Tower" not in copies[0]


@pytest.mark.parametrize("reverse", [False, True])
def test_log_catalog_shift_range_excludes_the_adjacent_job_history(tmp_path, monkeypatch, reverse):
    from test_log_selection_ui import SelectionDashboard
    dashboard = SelectionDashboard(tmp_path, monkeypatch)
    app, frame = dashboard.app, _FrameCache()
    app.logs.browser = True
    history_browser._view(app)["dock"] = "right"
    copies = []
    monkeypatch.setattr(clipboard, "copy", lambda text, *a, **kw: copies.append(text) or "copied")
    draw = lambda: frame.rebuild(app, dashboard.views, dashboard.store, None, 140, 36)
    try:
        draw()
        rows = [y for y, kind, _ in app.last_hits if kind == "log_file"]
        start, end = (rows[-1], rows[0]) if reverse else (rows[0], rows[-1])
        app.click(start, 8, app.last_hits)
        draw()
        app.click(end, 8, app.last_hits, shift=True)
        assert app.sel_anchor is None
        assert T.initialize(app)["selection"]["key"] == "logs:catalog"
        expected = T.selection_text(app)
        app.handle("y")
        assert copies == [expected]
        assert copies[0].count("\n") == len(rows)
        assert "stdout-77.log" in copies[0] and "stderr-77.log" in copies[0]
        assert "failed experiment" not in copies[0] and "File" not in copies[0]
    finally:
        dashboard.close()


@pytest.mark.parametrize("tab", ["jobs", "analytics"])
def test_real_advisor_document_drag_survives_selection_sync_and_repaint(monkeypatch, tab):
    config = Config({"animations": False, "startup_animation": False,
                     "gpu_sampling": False, "smooth_scrolling": False})
    store = Store(persist=False)
    store.jobs = [Job("41", "running-job", "cpu", "RUNNING", cpus=4,
                      mem_req="8G", elapsed="00:10:00")]
    store.finished = [Finished("21", "finished-family", "COMPLETED", cpus=4,
                               elapsed="00:10:00", rss=1024 ** 3,
                               req_mem=8 * 1024 ** 3, cpu_time=100)]
    store.live = {"41": Live(rss=1024 ** 3, avg=.1)}
    app = App(store, None, None, config, "tester", interactive=False)
    views = app.views_ref = Views(Glyphs(False), config)
    if tab == "jobs":
        app.run_command("jobpanel analytics advisor")
    else:
        app.enter_tab("analytics")
        app.analytics_view = "advisor"
    frame = _FrameCache()
    copies = []
    monkeypatch.setattr(clipboard, "copy", lambda text, *a, **kw: copies.append(text) or "copied")
    draw = lambda: frame.rebuild(app, views, store, None, 180, 50)
    try:
        draw()
        state = T.initialize(app)
        # A historical recommendation is plain document text beside native
        # job selectors. Use its actual published pane and real input route.
        needle = "Memory peak" if tab == "jobs" else "core-hours spent"
        candidates = [(pane, y) for pane in state["panes"]
                      if not pane.key.startswith("frame:")
                      for y in range(pane.rect.top, pane.rect.bottom - 1)
                      if needle in (T._line(state, pane, y) or "")]
        assert candidates
        pane, y = candidates[0]
        x = pane.rect.left + 4
        app.click(y, x, app.last_hits, button="press")
        app.click(y + 1, x, app.last_hits, button="drag")
        app.click(y + 1, x, app.last_hits, button="release")
        expected = T.selection_text(app)
        assert expected is not None and expected.count("\n") == 2
        assert needle in expected
        draw()
        app.handle("y")
        assert copies == [expected]
        assert "running-job" not in copies[0] and "File" not in copies[0]
    finally:
        if app.research:
            app.research.close()
