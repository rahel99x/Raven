"""Real table/document rails preserve source selection and viewport geometry."""
from types import SimpleNamespace

import pytest

from raven import advisor, chart_interaction as C, history_browser as H, layout as L, recent_history as R, scrollbars as S, scrolling
from raven.config import Config
from raven.controller import App
from raven.model import Finished, Health, Job, Store
from raven.remote import LocalFiles
from raven.views import Views


@pytest.fixture
def raven(tmp_path):
    cfg = Config({"animations": False, "smooth_scrolling": False, "startup_animation": False,
                  "log_lines": 0, "workspace": {"density": "compact", "split": 50}})
    store = Store(persist=False)
    store.jobs = [Job(str(i), f"job-{i:03}", "cpu", "RUNNING", cpus=4, user="test")
                  for i in range(1, 61)]
    store.group = list(store.jobs)
    store.finished = [Finished(str(i), f"past-{i:03}", "COMPLETED", cpus=4,
                               elapsed="00:10:00") for i in range(101, 451)]
    store.health = {f"source{i:02}": Health(f"source{i:02}") for i in range(60)}
    app = App(store, None, None, cfg, "test", interactive=False)
    app.files = app.logs.files = LocalFiles()
    views = Views(L.Glyphs(False), cfg, files=app.files)
    app.views_ref = views
    app.table_state["groups"] = False
    value = SimpleNamespace(app=app, store=store, views=views, tmp_path=tmp_path)
    yield value
    if app.research:
        app.research.close()


def draw(raven, width=160, height=42):
    rows, hits = raven.views.compose(raven.store.snapshot(), raven.app, width, height)
    return rows, hits


def pane(raven, key):
    return next(item for item in S.initialize(raven.app)["panes"] if item.key == key)


def forbidden(*args, **kwargs):
    raise AssertionError("Scrollbar input fetched source observations or persisted preferences")


@pytest.mark.parametrize("tab,key", [("jobs", "jobs"), ("history", "history"),
                                     ("group", "group"), ("sources", "sources")])
def test_table_jump_preserves_source_marks_and_reaches_last_visible_row(raven, monkeypatch, tab, key):
    raven.app.enter_tab(tab)
    draw(raven)
    selected, marks = raven.app.selected_id, {"2", "3"}
    raven.app.marks = marks.copy()
    item = pane(raven, key)
    assert item.header and item.rect.top > item.header[0]
    with monkeypatch.context() as patch:
        patch.setattr(raven.store, "snapshot", forbidden)
        patch.setattr(raven.app, "save", forbidden)
        assert S.activate(raven.app, key, "bottom")
    assert raven.app.selected_id == selected and raven.app.marks == marks
    rows, hits = draw(raven)
    item = pane(raven, key)
    assert item.painted == item.limit and item.target == item.limit
    assert raven.app.selected_id == selected and raven.app.marks == marks
    assert all(L.vlen(L.row_text(row)) <= 160 for row in rows)
    identifiers = {value for _, kind, value in hits if kind in ("job", "fin", "group", "source")}
    expected = (raven.app.visible_ids[-1] if key == "jobs" else
                raven.app.history_jobs()[-1].id if key == "history" else
                raven.app.group_ids[-1] if key == "group" else raven.app.source_ids[-1])
    assert expected in identifiers
    assert S.activate(raven.app, key, "top")
    draw(raven)
    assert pane(raven, key).painted == 0


def test_recent_bottom_admits_older_observations_at_render_and_keeps_current_details(raven, monkeypatch):
    draw(raven)
    app, state = raven.app, R.initialize(raven.app)
    source, admitted = app.selected_id, len(app.recent_ids)
    item = pane(raven, "recent")
    assert item.count > admitted
    with monkeypatch.context() as patch:
        patch.setattr(raven.store, "snapshot", forbidden)
        patch.setattr(raven.app, "save", forbidden)
        assert S.activate(app, "recent", "bottom")
    assert len(app.recent_ids) == admitted and state.target > admitted
    _, hits = draw(raven)
    item = pane(raven, "recent")
    assert item.painted == item.limit
    assert "450" in {value for _, kind, value in hits if kind == "recent"}
    assert app.selected_id == source


@pytest.mark.parametrize("dock", ["left", "right", "top", "bottom"])
def test_history_dock_has_independent_smooth_scroll_and_nonoverlapping_jump_handle(raven, dock):
    raven.app.enter_tab("analytics")
    H._view(raven.app)["dock"] = dock
    rows, _ = draw(raven)
    app = raven.app
    selected = app.analytics_job
    item = pane(raven, "history-browser:analytics")
    y, left, _ = item.header
    handle = next(value for _, kind, value in H.initialize(app)["frame"]["hits"]
                  if kind == "control" and value["id"].endswith(":drag"))
    assert handle["left"] >= 4
    assert S.handle_mouse(app, y, left + 2, "left")
    rows, _ = draw(raven)
    item = pane(raven, "history-browser:analytics")
    assert item.painted == item.limit
    assert app.analytics_job == selected
    assert H.initialize(app)["frame"]["page"] == item.page
    assert any(H.initialize(app)["items"][-1].record.id in L.row_text(row) for row in rows)
    assert all(L.vlen(L.row_text(row)) <= 160 for row in rows)


@pytest.mark.parametrize("density", ["comfortable", "focused"])
def test_workspace_history_uses_actual_main_page_once(raven, density):
    raven.app.enter_tab("history")
    raven.app.layout_state.density = density
    _, hits = draw(raven)
    item = pane(raven, "history")
    visible = [hit for hit in hits if hit[1] == "fin"]
    assert len(visible) == item.page
    assert not any(p.key == "workspace:history:main" and p.limit
                   for p in S.initialize(raven.app)["panes"])
    assert S.activate(raven.app, "history", "bottom")
    _, hits = draw(raven)
    assert raven.app.history_jobs()[-1].id in {value for _, kind, value in hits if kind == "fin"}


def test_native_advisor_keeps_all_names_and_running_jobs_reachable_without_changing_source(raven, monkeypatch):
    app = raven.app
    app.enter_tab("analytics")
    app.analytics_view = "advisor"
    derived = []
    original = advisor.advise_running
    def advise(job, *args, **kwargs):
        derived.append(job.id)
        return original(job, *args, **kwargs)
    monkeypatch.setattr(advisor, "advise_running", advise)
    draw(raven, 160, 28)
    assert not derived, "Offscreen running advice must not be derived on the first table page"
    key = "analytics:document:advisor"
    selected = app.analytics_job
    item = pane(raven, key)
    assert item.count > 300
    assert S.activate(app, key, "bottom")
    rows, _ = draw(raven, 160, 28)
    assert pane(raven, key).painted == pane(raven, key).limit
    assert app.analytics_job == selected
    assert "job-060" in L.to_text(rows, 160)
    assert "60" in derived and len(derived) <= pane(raven, key).page + 8
    app.move("home")
    draw(raven, 160, 28)
    assert pane(raven, key).painted == 0


def test_workspace_table_layout_probe_does_not_reset_pid_every_frame(raven, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(scrolling.time, "monotonic", lambda: now[0])
    raven.app.interactive = True
    raven.app.cfg.set("animations", True)
    raven.app.cfg.set("smooth_scrolling", True)
    raven.app.scrolling_state = None
    raven.app.enter_tab("history")
    raven.app.layout_state.density = "comfortable"
    draw(raven)
    assert S.activate(raven.app, "history", "bottom")
    draw(raven)
    now[0] += .03
    draw(raven)
    item = pane(raven, "history")
    assert 0 < item.painted < item.target
    now[0] += .3
    draw(raven)
    item = pane(raven, "history")
    assert item.painted == item.target == item.limit


@pytest.mark.parametrize("width", [120, 200])
@pytest.mark.parametrize("density", ["compact", "comfortable"])
def test_inline_metric_curves_do_not_overlap_the_details_scrollbar_gutter(raven, width, density):
    app = raven.app
    app.layout_state.density = density
    app.run_command("jobpanel analytics job")
    raven.store.jobs[0].gpus = 2
    for index in range(20):
        raven.store.record("1", {"k": "live", "t": index, "cpu": index / 20,
                                  "rss": 1024 ** 3, "eff": .5})
        raven.store.record("1", {"k": "gpu", "t": index,
                                  "gpu": {"nodeA:0": [index * 5, 512, 1024]}})
    draw(raven, width, 70)
    item = pane(raven, "workspace:jobs:details")
    plots = [plot for plot in C.initialize(app)["plots"] if plot.kind == "metric"]
    assert plots
    assert all(plot.visible.right <= item.rect.right - 1 for plot in plots)


def test_inline_advisor_keeps_every_retained_running_job_reachable(raven):
    raven.store.finished = raven.store.finished[:20]
    app = raven.app
    app.run_command("jobpanel analytics advisor")
    draw(raven, 200, 50)
    assert S.activate(app, "workspace:jobs:details", "bottom")
    rows, _ = draw(raven, 200, 50)
    assert "60 / job-060" in L.to_text(rows, 200)


def test_primary_log_scrollbar_keeps_range_and_original_prefixes(raven, monkeypatch):
    path = raven.tmp_path / "source.log"
    path.write_text("".join(f"AB source line {index:03}\n" for index in range(300)))
    raven.store.details["1"] = {"StdOut": str(path), "WorkDir": str(raven.tmp_path)}
    app = raven.app
    app.open_log("1")
    draw(raven)
    app.logs.selection_anchor = 280
    app.logs.selection_end = 282
    selected, log_job = (app.logs.selection_anchor, app.logs.selection_end), app.log_job
    with monkeypatch.context() as patch:
        patch.setattr(raven.store, "snapshot", forbidden)
        assert S.activate(app, "logs:document", "top")
    rows, _ = draw(raven)
    assert pane(raven, "logs:document").painted == 0
    assert selected == (app.logs.selection_anchor, app.logs.selection_end)
    assert app.log_job == log_job
    assert "AB source line 000" in L.to_text(rows, 160)


@pytest.mark.parametrize("width,height", [(1, 1), (2, 8), (10, 14), (40, 18), (80, 24), (160, 42)])
def test_scrollbar_staging_never_leaks_geometry_outside_terminal(raven, width, height):
    rows, _ = draw(raven, width, height)
    assert len(rows) == height
    assert all(L.vlen(L.row_text(row)) <= width for row in rows)
    for item in S.initialize(raven.app)["panes"]:
        assert 0 <= item.rect.top < item.rect.bottom <= height - 1
        assert 0 <= item.rect.left < item.rect.right <= width
        if item.header:
            assert 0 <= item.header[0] < height - 1
            assert 0 <= item.header[1] < item.header[2] <= width
