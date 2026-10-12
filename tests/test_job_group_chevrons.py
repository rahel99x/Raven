"""Painted group arrows route one explicit fold action across real job panes."""
import pytest

from raven import history_browser, job_groups, layout as L
from raven.config import Config
from raven.controller import App
from raven.model import Finished, Job, Store
from raven.views import Views


@pytest.fixture
def raven():
    store = Store(persist=False)
    store.jobs = [Job("600_1", "train", "gpu", "RUNNING", cpus=8, gpus=1),
                  Job("600_2", "train", "gpu", "RUNNING", cpus=16, gpus=2),
                  Job("600_3", "train", "gpu", "PENDING", reason="Dependency", dependency="afterok:600_2"),
                  Job("600_4", "train", "gpu", "PENDING", reason="DependencyNeverSatisfied"),
                  Job("900", "independent", "cpu", "RUNNING")]
    store.finished = [Finished("500_1", "train", "COMPLETED", cpus=8),
                      Finished("500_2", "train", "FAILED", cpus=16)]
    store.group = list(store.jobs)
    cfg = Config({"animations": False, "startup_animation": False, "log_lines": 0,
                  "smooth_scrolling": False, "workspace": {"density": "compact"}})
    app = App(store, None, None, cfg, "test", interactive=False)
    views = Views(L.Glyphs(False), cfg)
    app.views_ref = views
    yield app, views, store
    if app.research:
        app.research.close()


def frame(raven, tab="jobs", *, ascii_=False, width=280, height=58):
    app, views, store = raven
    app.tab = "jobs" if tab == "recent" else "analytics" if tab == "analytics:advisor" else tab
    if tab == "analytics:advisor":
        app.analytics_view = "advisor"
    views.set_ascii(ascii_)
    return views.compose(store.snapshot(), app, width, height)


def table_control(hits, context, gid):
    return next((y, value) for y, kind, value in hits if kind == "control"
                and value["id"].startswith("jobgroup:" + context + ":" + gid))


def dock_control(hits, tab, gid):
    return next((y, value) for y, kind, value in hits if kind == "control"
                and value["id"] == "history:" + tab + ":group:" + gid)


def is_closed(app, gid):
    registry = job_groups.registry(app)
    return registry.is_collapsed(registry.index.groups[gid])


def gesture(app, hit, hits):
    y, control = hit
    app.click(y, control["left"], hits, button="press")
    app.click(y, control["left"], hits, button="release")


@pytest.mark.parametrize("context", ["jobs", "recent", "history", "group", "deps", "analytics:advisor"])
@pytest.mark.parametrize("ascii_", [False, True])
def test_painted_table_chevron_closes_and_opens_once_without_selecting_a_batch(raven, context, ascii_, monkeypatch):
    app, _, _ = raven
    gid = "array:500" if context in ("recent", "history") else "array:600"
    rows, hits = frame(raven, context, ascii_=ascii_)
    hit = table_control(hits, context, gid)
    y, control = hit
    assert L.row_text(rows[y])[control["left"]] == (">" if ascii_ else "▸")
    assert control["action"] == ("command", "jobgroup close " + gid)
    app.marks = {"600_2", "500_2"}
    calls = []
    original = app.run_command
    monkeypatch.setattr(app, "run_command", lambda text: (calls.append(text), original(text))[1])
    gesture(app, hit, hits)
    assert calls == ["jobgroup close " + gid]
    assert is_closed(app, gid)
    assert app.marks == {"600_2", "500_2"}
    assert app.mode == "main" and not app.confirm
    assert app.selected_id is None or app.selected_id in {"600_1", "600_2", "600_3", "600_4", "500_1", "500_2", "900"}
    # Repeated input against the still-painted close action is idempotent.
    gesture(app, hit, hits)
    assert is_closed(app, gid)
    rows, hits = frame(raven, context, ascii_=ascii_)
    hit = table_control(hits, context, gid)
    y, control = hit
    assert L.row_text(rows[y])[control["left"]] == ("v" if ascii_ else "▾")
    assert control["action"] == ("command", "jobgroup open " + gid)
    previous = len(calls)
    gesture(app, hit, hits)
    assert calls[previous:] == ["jobgroup open " + gid]
    assert not is_closed(app, gid)
    assert app.marks == {"600_2", "500_2"}


@pytest.mark.parametrize("tab", history_browser.TABS)
@pytest.mark.parametrize("dock", ["left", "right", "top", "bottom"])
def test_each_job_browser_dock_uses_the_same_explicit_chevron_actions(raven, tab, dock, monkeypatch):
    app, _, _ = raven
    app.tab = tab
    history_browser._view(app)["dock"] = dock
    rows, hits = frame(raven, tab)
    hit = dock_control(hits, tab, "array:600")
    y, control = hit
    assert L.row_text(rows[y])[control["left"]] == "▸"
    assert control["action"] == ("command", "jobgroup close array:600")
    calls = []
    original = app.run_command
    monkeypatch.setattr(app, "run_command", lambda text: (calls.append(text), original(text))[1])
    gesture(app, hit, hits)
    assert is_closed(app, "array:600") and calls == ["jobgroup close array:600"]
    rows, hits = frame(raven, tab)
    hit = dock_control(hits, tab, "array:600")
    assert L.row_text(rows[hit[0]])[hit[1]["left"]] == "▾"
    assert hit[1]["action"] == ("command", "jobgroup open array:600")
    gesture(app, hit, hits)
    assert not is_closed(app, "array:600")
    assert calls == ["jobgroup close array:600", "jobgroup open array:600"]
    assert app.tab == tab and app.mode == "main"


@pytest.mark.parametrize("context", ["jobs", "recent", "history", "group", "deps", "analytics:advisor"])
def test_passive_motion_and_orphan_drag_release_never_fold_or_read_sources(raven, context, monkeypatch):
    app, _, store = raven
    _, hits = frame(raven, context)
    gid = "array:500" if context in ("recent", "history") else "array:600"
    y, control = table_control(hits, context, gid)
    selected, marks, tab = app.selected_id, set(app.marks), app.tab
    monkeypatch.setattr(store, "snapshot", lambda: pytest.fail("Passive group hover fetched a source snapshot"))
    monkeypatch.setattr(app, "run_command", lambda text: pytest.fail("Passive group event executed " + text))
    for button in ("motion", "drag", "release") * 3:
        app.click(y, control["left"], hits, button=button)
    assert not is_closed(app, gid)
    assert (app.selected_id, app.marks, app.tab) == (selected, marks, tab)


@pytest.mark.parametrize("tab", history_browser.TABS)
def test_browser_passive_motion_and_orphan_release_cannot_toggle_groups(raven, tab, monkeypatch):
    app, _, store = raven
    _, hits = frame(raven, tab)
    y, control = dock_control(hits, tab, "array:600")
    selected = app.selected_id
    monkeypatch.setattr(store, "snapshot", lambda: pytest.fail("Passive browser event fetched a snapshot"))
    monkeypatch.setattr(app, "run_command", lambda text: pytest.fail("Passive browser event executed " + text))
    for button in ("motion", "drag", "release") * 3:
        app.click(y, control["left"], hits, button=button)
    assert not is_closed(app, "array:600") and app.selected_id == selected


@pytest.mark.parametrize("context,kind,jid", [("jobs", "job", "600_2"), ("recent", "recent", "500_2"),
                                             ("history", "fin", "500_2"), ("group", "group", "600_2"),
                                             ("deps", "dep", "600_2")])
def test_clicking_an_ordinary_job_cell_selects_exact_job_without_folding(raven, context, kind, jid):
    app, _, _ = raven
    _, hits = frame(raven, context)
    y = next(y for y, actual_kind, value in hits if actual_kind == kind and value == jid)
    app.click(y, 12, hits, button="press")
    app.click(y, 12, hits, button="release")
    assert app.selected_id == jid
    assert not is_closed(app, "array:500" if jid.startswith("500") else "array:600")
    assert not app.marks


@pytest.mark.parametrize("tab", history_browser.TABS)
def test_browser_row_text_selects_exact_job_without_folding(raven, tab, monkeypatch):
    app, _, _ = raven
    opened = []
    monkeypatch.setattr(app, "open_log", lambda jid: opened.append(jid))
    _, hits = frame(raven, tab)
    y, control = next((y, value) for y, kind, value in hits if kind == "control"
                      and value["id"] == "history:" + tab + ":job:600_2")
    app.click(y, control["left"] + 4, hits, button="press")
    app.click(y, control["left"] + 4, hits, button="release")
    assert app.selected_id == "600_2" and not is_closed(app, "array:600")
    if tab == "log":
        assert opened == ["600_2"]


@pytest.mark.parametrize("stale", ["resized", "other-tab", "replacement-frame"])
def test_stale_group_hit_maps_cannot_execute_an_action(raven, stale, monkeypatch):
    app, _, _ = raven
    _, old_hits = frame(raven)
    y, control = table_control(old_hits, "jobs", "array:600")
    if stale == "resized":
        frame(raven, width=180)
    elif stale == "other-tab":
        frame(raven, "history")
    else:
        job_groups.fold(app, "array:600", True)
        frame(raven)
    before = list(app.table_state["collapsed"])
    monkeypatch.setattr(app, "run_command", lambda text: pytest.fail("Stale group hit executed " + text))
    app.click(y, control["left"], old_hits, button="press")
    app.click(y, control["left"], old_hits, button="release")
    assert app.table_state["collapsed"] == before


def test_collapsed_summary_does_not_turn_representative_resources_into_batch_totals(raven):
    app, views, store = raven
    _, hits = frame(raven)
    gesture(app, table_control(hits, "jobs", "array:600"), hits)
    rows = views.job_rows(store.snapshot(), app)
    summary = next(row for row in rows if row["id"] == "600_1")
    assert summary["st"] == "GRP" and "4 records" in summary["name"]
    assert all(summary[field] == "" for field in ("progress", "cpus", "gpu", "cpu%", "mem%", "time"))
    assert "2" in summary["info"] and "never" in job_groups.summary(summary["_group"].records).lower()
