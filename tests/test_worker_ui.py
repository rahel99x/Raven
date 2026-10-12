"""Worker admission preferences remain local, reversible, and precisely clickable."""
from __future__ import annotations

from types import SimpleNamespace
import threading

import pytest

from raven import interaction, layout, toolbar, workbench, worker_ui
from raven.config import Config
from raven.controller import App
from raven.model import Job, Store
from raven.views import Views


class Governor:
    def __init__(self, mode="multi", *, running=0):
        self.value = dict(mode=mode, target=mode, pending=False, running=running,
                          queued=2, draining=0, limit=1 if mode == "single" else 4,
                          target_limit=1 if mode == "single" else 4, generation=0, closed=False)
        self.requests = []

    def status(self):
        return dict(self.value)

    def request_mode(self, target):
        self.requests.append(target)
        pending = target == "single" and self.value["running"] > 1
        self.value.update(target=target, pending=pending,
                          draining=self.value["running"] - 1 if pending else 0,
                          target_limit=1 if target == "single" else 4,
                          generation=self.value["generation"] + 1)
        if not pending:
            self.value.update(mode=target, limit=self.value["target_limit"])


def instance(mode="multi", *, running=0, governor=True):
    app = SimpleNamespace(cfg=Config({"worker_mode": mode}), mode="main", tab="jobs",
                          messages=[], failures=[], quit=False, selected_id="7", marks={"7"})
    app.say = app.messages.append
    app.fail = app.failures.append
    worker_ui.initialize(app)
    if governor:
        worker_ui.bind(app, Governor(mode, running=running))
    return app


def ui(ascii_=False):
    return SimpleNamespace(g=layout.Glyphs(ascii_))


@pytest.mark.parametrize("mode", ["single", "multi"])
def test_command_registration_status_and_saved_preference(mode):
    app = instance()
    assert "workers" in workbench.command_names()
    assert worker_ui.run_command(app, ["workers", mode])
    assert app.cfg["worker_mode"] == mode
    assert worker_ui.save(app) == {"mode": mode}
    assert worker_ui.run_command(app, ["workers", "status"])
    assert mode in app.messages[-1] and "2 queued" in app.messages[-1]
    assert not app.failures


def test_rapid_toggle_uses_latest_desired_mode_and_cancels_a_pending_switch():
    app = instance(running=3)
    worker_ui.run_command(app, ["workers", "toggle"])
    assert worker_ui.status(app)["mode"] == "multi"
    assert worker_ui.status(app)["target"] == "single"
    assert worker_ui.status(app)["pending"]
    assert "waiting for 2 active" in app.messages[-1]
    assert worker_ui.token(app) == ("multi", "single", True, False, 2)
    worker_ui.run_command(app, ["workers", "toggle"])
    assert app.worker_scheduler.requests == ["single", "multi"]
    assert not worker_ui.status(app)["pending"]
    assert app.cfg["worker_mode"] == "multi"
    assert app.mode == "main" and app.tab == "jobs" and app.marks == {"7"}


@pytest.mark.parametrize("explicit", [False, True])
def test_bind_applies_restored_preference_but_retains_explicit_cli_override(explicit):
    app = instance("multi", governor=False)
    if explicit:
        app.cfg.ui_locked_settings = {"worker_mode"}
    worker_ui.restore(app, {"mode": "single"})
    governor = Governor()
    worker_ui.bind(app, governor)
    assert governor.requests == ([] if explicit else ["single"])
    assert worker_ui.status(app)["target"] == ("multi" if explicit else "single")
    assert worker_ui.save(app)["mode"] == ("multi" if explicit else "single")


@pytest.mark.parametrize("explicit", [False, True])
def test_real_app_save_and_restore_preserves_desired_pending_mode(tmp_path, explicit):
    store = Store(state_dir=str(tmp_path), persist=True)
    app = App(store, None, None, Config(), "tester", interactive=False)
    worker_ui.bind(app, Governor(running=3))
    app.run_command("workers single")
    assert worker_ui.status(app)["pending"]
    app.save()
    assert store.load_ui()["workbench"]["worker_ui"] == {"mode": "single"}

    cfg = Config({"worker_mode": "multi"})
    if explicit:
        cfg.ui_locked_settings = {"worker_mode"}
    restored = App(Store(state_dir=str(tmp_path), persist=True), None, None,
                   cfg, "tester", interactive=False)
    governor = Governor()
    worker_ui.bind(restored, governor)
    assert governor.status()["target"] == ("multi" if explicit else "single")
    assert governor.requests == ([] if explicit else ["single"])


def test_scheduler_error_preserves_preference_and_surfaces_the_reason():
    app = instance()

    def unavailable(mode):
        raise RuntimeError("session is closing")

    app.worker_scheduler.request_mode = unavailable
    assert worker_ui.run_command(app, ["workers", "single"])
    assert "session is closing" in app.failures[-1]
    assert app.cfg["worker_mode"] == "multi" and worker_ui.save(app) == {"mode": "multi"}


def test_real_scheduler_pending_switch_returns_before_active_collection_finishes():
    from raven.worker_scheduler import WorkerScheduler

    started, finish = threading.Event(), threading.Event()
    governor = WorkerScheduler(workers=2)
    lane = governor.lane("source")
    app = App(Store(persist=False), None, None, Config(), "tester", interactive=False)
    worker_ui.bind(app, governor)
    calls = []

    def collect():
        calls.append(threading.get_ident())
        started.set()
        assert finish.wait(3)
        return "collected"

    future = lane.submit(collect)
    try:
        assert started.wait(1)
        app.run_command("workers toggle")
        assert future.running() and not finish.is_set()
        status = worker_ui.status(app)
        assert status["mode"] == "multi" and status["target"] == "single"
        assert status["pending"] and status["draining"] == 1
        row = toolbar.render_bar(ui(), app, 120)
        assert "→Single" in layout.row_text(row)
        app.run_command("workers toggle")
        assert worker_ui.status(app)["mode"] == "multi" and not worker_ui.status(app)["pending"]
        assert future.running() and len(calls) == 1
    finally:
        finish.set()
        governor.shutdown()
    assert future.result(timeout=1) == "collected" and len(calls) == 1


@pytest.mark.parametrize("bad", [None, {}, {"mode": "bad", "target": "multi"}])
def test_unavailable_or_invalid_scheduler_fails_without_changing_preferences(bad):
    app = instance(governor=False)
    if bad is not None:
        app.worker_scheduler = SimpleNamespace(status=lambda: bad)
    assert worker_ui.status(app) is None
    assert worker_ui.button(app, 200) is None
    assert worker_ui.run_command(app, ["workers", "toggle"])
    assert "unavailable" in app.failures[-1]
    assert app.cfg["worker_mode"] == "multi"
    rows = toolbar.render_bar(ui(), app, 120)
    assert layout.vlen(layout.row_text(rows)) == 120
    assert not any(hit[3] == "workers" for hit in app.toolbar_state["hits"])
    item = next(item for item in toolbar.menu_items(app, "View") if item.key == "workers-toggle")
    assert "unavailable" in toolbar._blocked(app, item)


def test_closed_scheduler_exposes_status_but_refuses_new_work():
    app = instance()
    app.worker_scheduler.value["closed"] = True
    worker_ui.run_command(app, ["workers", "toggle"])
    assert "stopped" in app.failures[-1] and not app.worker_scheduler.requests
    worker_ui.run_command(app, ["workers", "status"])
    assert "stopped" in app.messages[-1]
    entries = {item.key: item for item in toolbar.menu_items(app, "View")}
    assert toolbar._blocked(app, entries["workers-toggle"])
    assert not toolbar._blocked(app, entries["workers-status"])


@pytest.mark.parametrize("args", [["workers", "bad"], ["workers", "single", "extra"]])
def test_invalid_command_does_not_touch_scheduler(args):
    app = instance()
    assert worker_ui.run_command(app, args)
    assert app.failures and not app.worker_scheduler.requests


@pytest.mark.parametrize("ascii_", [False, True])
@pytest.mark.parametrize("width", [0, 1, 7, 8, 10, 13, 16, 24, 30, 43, 44, 50, 62, 80, 120, 200])
def test_toolbar_mode_and_pending_switch_never_displace_controls(width, ascii_):
    app = instance(running=3)
    geometries = []
    for change in (None, "single", "multi"):
        if change:
            worker_ui.run_command(app, ["workers", change])
        row = toolbar.render_bar(ui(ascii_), app, width)
        assert layout.vlen(layout.row_text(row)) == width
        hits = app.toolbar_state["hits"]
        assert all(0 <= left < right <= width for _, left, right, _, _ in hits)
        assert all(a[2] <= b[1] for a, b in zip(hits, hits[1:]))
        geometries.append(tuple((y, left, right, kind, key) for y, left, right, kind, key in hits))
        if width >= 8:
            assert any(hit[3] == "rate" for hit in hits)
        if width >= 16:
            worker = next(hit for hit in hits if hit[3] == "workers")
            assert worker[2] - worker[1] in (5, 11)
        if ascii_:
            assert layout.row_text(row).isascii()
    assert geometries[0] == geometries[1] == geometries[2]


@pytest.mark.parametrize("button", ["motion", "drag", "release", "right", "middle", "wheel-up", "wheel-down"])
def test_nonprimary_toolbar_reports_cannot_change_worker_mode(button):
    app = instance()
    toolbar.render_bar(ui(), app, 120)
    y, left, *_ = next(hit for hit in app.toolbar_state["hits"] if hit[3] == "workers")
    toolbar.handle_mouse(app, y, left, button=button)
    assert not app.worker_scheduler.requests and app.cfg["worker_mode"] == "multi"


@pytest.mark.parametrize("button", ["left", "press"])
def test_toolbar_switch_preserves_pending_job_review_and_release_is_not_a_second_toggle(button):
    app = instance(running=3)
    review = app.confirm = {"ids": ["7"], "command": "cancel"}
    app.mode = "confirm"
    toolbar.render_bar(ui(), app, 120)
    y, left, *_ = next(hit for hit in app.toolbar_state["hits"] if hit[3] == "workers")
    assert toolbar.handle_mouse(app, y, left, button=button)
    toolbar.handle_mouse(app, y, left, button="release")
    assert app.worker_scheduler.requests == ["single"]
    assert app.mode == "confirm" and app.confirm is review
    assert app.tab == "jobs" and app.marks == {"7"} and app.selected_id == "7"
    item = next(item for item in toolbar.menu_items(app, "View") if item.key == "workers-toggle")
    assert not toolbar._blocked(app, item)
    toolbar._activate(app, item)
    assert app.worker_scheduler.requests == ["single", "multi"] and app.mode == "confirm"


@pytest.mark.parametrize("ascii_", [False, True])
def test_real_published_toolbar_supports_mouse_and_arrow_navigation(ascii_):
    cfg = Config({"animations": False, "startup": {"enabled": False}, "log_lines": 0})
    store = Store(persist=False)
    store.jobs = [Job("7", "test", "cpu", "RUNNING", cpus=4)]
    app = App(store, None, None, cfg, "tester", ascii_=ascii_, interactive=False)
    views = Views(layout.Glyphs(ascii_), cfg)
    app.views_ref, app.selected_id = views, "7"
    worker_ui.bind(app, Governor(running=3))

    def draw():
        views.compose(store.snapshot(), app, 160, 42)
        return interaction.initialize(app)["graph"]

    graph = draw()
    before = graph.get("toolbar:menu:3")
    app.click(before.rect.top, before.rect.left, app.last_hits, button="motion")
    app.handle("f8")
    assert app.interaction_state["focused"] == before.id
    app.handle("right")
    assert app.interaction_state["focused"] == "toolbar:workers:None"
    app.handle("enter")
    assert app.worker_scheduler.requests == ["single"] and app.tab == "jobs"
    assert app.selected_id == "7" and not app.quit
    graph = draw()
    target = graph.get("toolbar:workers:None")
    assert target and target.rect == graph.get(app.interaction_state["focused"]).rect
    app.click(target.rect.top, target.rect.left, app.last_hits, button="left")
    assert app.worker_scheduler.requests == ["single", "multi"]
    assert app.tab == "jobs" and app.job_panel_state["mode"] == "inspector"
    assert toolbar.control_descriptors(app)[5]["id"] == "toolbar:workers"


@pytest.mark.parametrize('ascii_',[False,True])
@pytest.mark.parametrize('managed',[False,True])
def test_eighty_column_toolbar_retains_readable_refresh_and_worker_controls(ascii_,managed):
    app=instance()
    if managed:
        app.cfg.data['raven']={'managed':True,'execution':{'heavy_target':'slurm'}}
    row=toolbar.render_bar(ui(ascii_),app,80)
    text=layout.row_text(row)
    assert 'Updates' in text and 'Multi' in text
    assert 'File' in text and 'Help' in text
    kinds={hit[3] for hit in app.toolbar_state['hits']}
    assert {'workers','copy-mode','rate','minus','plus','track'}<=kinds
    assert ('raven' in kinds)==managed
    assert ('Light' in text)==managed
    assert layout.vlen(text)==80
