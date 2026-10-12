"""Cross-feature pointer and publication regressions for grouped job details."""
from types import SimpleNamespace

import pytest

from raven import chart_interaction as charts, job_groups, job_panels, layout as L, screen
from raven.config import Config
from raven.controller import App
from raven.model import Finished, Job, Store
from raven.views import Views


MOUSE = SimpleNamespace(BUTTON1_CLICKED=1, BUTTON1_PRESSED=2, BUTTON1_RELEASED=4,
    BUTTON1_DOUBLE_CLICKED=8, BUTTON3_CLICKED=16, BUTTON3_PRESSED=32,
    BUTTON4_PRESSED=64, BUTTON5_PRESSED=128, REPORT_MOUSE_POSITION=256, BUTTON_SHIFT=512)


@pytest.fixture
def workspace():
    cfg = Config({"animations": False, "startup_animation": False, "log_lines": 0,
                  "smooth_scrolling": False, "workspace": {"density": "compact", "split": 50}})
    store = Store(persist=False)
    store.jobs = [Job("71_1", "training", "gpu", "RUNNING", cpus=8),
                  Job("71_2", "training", "gpu", "PENDING", reason="Dependency",
                      dependency="afterok:71_1")]
    store.finished = [Finished("81_1", "archived training", "COMPLETED", cpus=8,
                               end="2026-10-09T11:00:00"),
                      Finished("81_2", "archived training", "FAILED", cpus=8,
                               end="2026-10-09T10:00:00")]
    for record in store.jobs + store.finished:
        for index in range(40):
            store.record(record.id, {"k": "live", "t": 1000. + index,
                                    "cpu": .3 + index / 100, "rss": (index + 1) * 1024**2})
    app = App(store, None, None, cfg, "tester", interactive=True)
    views = Views(L.Glyphs(False), cfg)
    app.views_ref = views
    yield app, views, store
    if app.research:
        app.research.close()


def pointer(app, y, x, bits):
    screen._apply_input(app, ("mouse", (0, x, y, 0, bits)), app.last_hits, MOUSE)


def render(workspace, tab, width, ascii_):
    app, views, store = workspace
    app.tab = tab
    views.set_ascii(ascii_)
    job_panels.initialize(app).update(mode="analytics", analytics_view="job")
    rows, hits = views.compose(store.snapshot(), app, width, 52)
    assert all(L.vlen(L.row_text(row)) <= width for row in rows)
    assert all(0 <= y < 52 for y, _, _ in hits)
    return rows, hits


@pytest.mark.parametrize("tab", ["jobs", "history"])
@pytest.mark.parametrize("width", [160, 240])
@pytest.mark.parametrize("ascii_", [False, True])
def test_passive_crossings_of_grouped_rows_details_and_graphs_never_activate(workspace, monkeypatch, tab, width, ascii_):
    app, _, store = workspace
    render(workspace, tab, width, ascii_)
    selected = app.selected_id
    group = job_groups.registry(app).index.for_job(selected)
    assert group is not None
    assert job_groups.fold(app, group.id, True)
    _, hits = render(workspace, tab, width, ascii_)
    plots = charts.initialize(app)["plots"]
    assert plots and any(plot.key[1] == selected for plot in plots)
    row = next(y for y, kind, jid in hits if kind in ("job", "fin") and jid == selected)
    buttons = [(y, value[1]) for y, kind, value in hits if kind == "job_panel_tab"]
    assert buttons
    plot = plots[0]
    points = [(row, 4), (plot.visible.top, plot.visible.left + 2),
              (plot.visible.bottom - 1, plot.visible.right - 2), *buttons]
    app.marks = {selected}

    def forbidden(*args, **kwargs):
        pytest.fail("Passive pointer motion fetched a snapshot, opened a log, or changed a tab")

    monkeypatch.setattr(store, "snapshot", forbidden)
    monkeypatch.setattr(app, "open_log", forbidden)
    monkeypatch.setattr(app, "enter_tab", forbidden)
    for _ in range(12):
        for y, x in points:
            pointer(app, y, x, MOUSE.REPORT_MOUSE_POSITION)
            assert app.tab == tab and app.mode == "main"
            assert app.selected_id == selected and app.marks == {selected}
            assert app.job_panel_state["mode"] == "analytics"
    assert not app.quit and not app.confirm


@pytest.mark.parametrize("tab", ["jobs", "history"])
def test_new_member_publication_preserves_collapsed_identity_and_held_graph(workspace, tab):
    app, _, store = workspace
    render(workspace, tab, 240, False)
    selected = app.selected_id
    group = job_groups.registry(app).index.for_job(selected)
    assert job_groups.fold(app, group.id, True)
    render(workspace, tab, 240, False)
    original = next(plot for plot in charts.initialize(app)["plots"] if plot.key[2] == "cpu-rate")
    start, end = original.visible.left + 3, original.visible.right - 4
    y = original.visible.top + 1
    pointer(app, y, start, MOUSE.BUTTON1_PRESSED)
    assert charts.active(app)
    if tab == "jobs":
        store.jobs.append(Job("71_3", "training", "gpu", "PENDING", reason="Resources"))
    else:
        store.finished.append(Finished("81_3", "archived training", "TIMEOUT", cpus=8,
                                       end="2026-10-09T09:00:00"))
    store.record(selected, {"k": "live", "t": 1041., "cpu": .9, "rss": 80 * 1024**2})
    render(workspace, tab, 240, False)
    current = job_groups.registry(app).index.for_job(selected)
    assert current.id == group.id and len(current.members) == 3
    assert job_groups.registry(app).is_collapsed(current)
    assert app.selected_id == selected and charts.active(app)
    pointer(app, y, end, MOUSE.REPORT_MOUSE_POSITION | MOUSE.BUTTON1_PRESSED)
    pointer(app, y, end, MOUSE.BUTTON1_RELEASED)
    assert not charts.active(app) and charts.bounds(app, original.key)
    assert app.tab == tab and app.mode == "main" and app.selected_id == selected
    pointer(app, y, end, MOUSE.BUTTON3_PRESSED)
    assert charts.bounds(app, original.key) is None
