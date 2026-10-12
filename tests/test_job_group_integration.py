"""Collapsed sorting must retain the exact allocation selected for actions."""
import pytest

from raven import job_groups
from raven.config import Config
from raven.controller import App
from raven.layout import Glyphs
from raven.model import Finished, Job, Store
from raven.views import Views


@pytest.fixture
def dashboard(tmp_path):
    cfg = Config({"log_lines": 0, "clipboard": {"tools": False, "osc52": False}})
    store = Store(state_dir=str(tmp_path / "state"))
    jobs = [Job("101", "zebra", "main", "RUNNING", user="alex", account="lab"),
            Job("102", "aardvark", "main", "RUNNING", user="alex", account="lab"),
            Job("201", "solo", "main", "RUNNING", user="alex", account="lab")]
    finished = [Finished("301", "zebra", "FAILED", end="2026-10-08T12:02:00"),
                Finished("302", "aardvark", "FAILED", end="2026-10-08T12:01:00")]
    store.apply_jobs(jobs)
    store.apply_finished(finished)
    store.group = jobs
    for record in jobs[:2] + finished:
        store.details[record.id] = {"RavenLaunchId": "live" if isinstance(record, Job) else "finished",
                                    "UserId": "alex(1000)", "Account": "lab", "WorkDir": "/project"}
    app = App(store, None, None, cfg, "alex")
    views = Views(Glyphs(False), cfg)
    app.views_ref = views
    return app, views, store


@pytest.mark.parametrize("table,selected", [("jobs", "101"), ("recent", "301"),
                                            ("history", "301"), ("group", "101")])
def test_sort_reveals_selected_explicit_group_member_when_representative_changes(dashboard, table, selected):
    app, views, store = dashboard
    app.enter_tab("jobs" if table == "recent" else table)
    views.compose(store.snapshot(), app, 180, 48)
    if table in ("jobs", "recent"):
        ids, cursor_table = app.visible_ids + app.recent_ids, "jobs"
    elif table == "history":
        ids, cursor_table = app.last_history_ids, "history"
    else:
        ids, cursor_table = app.group_ids, "group"
    app.selected_id, app.cursor[cursor_table] = selected, ids.index(selected)
    group = job_groups.registry(app).index.for_job(selected)
    assert group is not None and group.kind == "explicit"
    assert job_groups.fold(app, group.id, True)
    views.compose(store.snapshot(), app, 180, 48)
    assert app.selected_id == selected
    app.run_command(f"sortby {table} name asc")
    assert app.command_ok
    views.compose(store.snapshot(), app, 180, 48)
    assert app.selected_id == selected
    assert not job_groups.registry(app).is_collapsed(group)
    opened = []
    app.open_log = lambda jid=None: opened.append(jid or app.selected_id)
    app.handle("l")
    assert opened == [selected]


def test_frozen_render_reports_changes_from_live_queue(dashboard):
    app, views, store = dashboard
    views.compose(store.snapshot(), app, 180, 48)
    app.run_command("freeze on")
    assert app.command_ok
    store.apply_jobs(store.jobs + [Job("999", "new live allocation", "main", "RUNNING")])
    views.compose(store.snapshot(), app, 180, 48)
    assert "1 queue changes" in app.freeze_label
    assert "999" not in app.visible_ids


def test_dependency_render_does_not_rebuild_related_set_per_job(dashboard, monkeypatch):
    from raven.deps import DepGraph
    app, views, store = dashboard
    records = [Job("1", "parent", "main", "RUNNING")]
    records += [Job(str(number), "child", "main", "PENDING", dependency="afterok:1")
                for number in range(2, 202)]
    store.apply_jobs(records)
    app.tab = "deps"
    calls = 0
    original = DepGraph.related

    def counted(graph):
        nonlocal calls
        calls += 1
        return original(graph)

    monkeypatch.setattr(DepGraph, "related", counted)
    views.deps_tab(store.snapshot(), app, 120, 24)
    assert calls <= 5, f"Dependency render rebuilt the related set {calls} times for 201 jobs"


def test_deep_dependency_row_keeps_exact_job_id_within_terminal_width(dashboard):
    from raven import layout as L
    app, views, store = dashboard
    records = [Job("1", "root", "main", "RUNNING")]
    records += [Job(str(number), "child", "main", "PENDING", dependency=f"afterok:{number - 1}")
                for number in range(2, 501)]
    store.apply_jobs(records)
    app.tab, app.cursor["deps"] = "deps", 499
    rows, hits = views.deps_tab(store.snapshot(), app, 120, 24)
    selected_row = next(y for y, kind, jid in hits if kind == "dep" and jid == "500")
    assert "500" in L.row_text(L.clip_row(rows[selected_row], 120))
    assert L.vlen(L.row_text(rows[selected_row])) <= 240


def test_repeated_dependency_group_fold_controls_have_distinct_semantic_ids(dashboard):
    from raven import interaction
    app, views, store = dashboard
    records = [Job("1", "first root", "main", "RUNNING"), Job("2", "second root", "main", "RUNNING"),
               Job("12_0", "array", "main", "PENDING", dependency="afterok:1:2"),
               Job("12_1", "array", "main", "PENDING", dependency="afterok:12_0")]
    store.apply_jobs(records)
    app.tab = "deps"
    rows, hits = views.deps_tab(store.snapshot(), app, 120, 30)
    controls = [value for _, kind, value in hits if kind == "control" and value["id"].startswith("jobgroup:deps:")]
    assert len(controls) == 2 and len({control["id"] for control in controls}) == 2
    graph = interaction.publish(app, rows, hits, 120, 30)
    assert all(graph.get(control["id"]) is not None for control in controls)


def test_frozen_group_controls_use_displayed_snapshot_after_live_metadata_changes(dashboard):
    app, views, store = dashboard
    views.compose(store.snapshot(), app, 180, 48)
    app.run_command("freeze on")
    views.compose(store.snapshot(), app, 180, 48)
    group = job_groups.registry(app).index.for_job("101")
    store.details["101"]["WorkDir"] = "/live correction"
    views.compose(store.snapshot(), app, 180, 48)
    app.run_command("jobgroup close " + group.id)
    assert app.command_ok
    views.compose(store.snapshot(), app, 180, 48)
    assert "101" in app.visible_ids and "102" not in app.visible_ids
    app.handle("right")
    views.compose(store.snapshot(), app, 180, 48)
    assert {"101", "102"} <= set(app.visible_ids)


def test_dependency_group_fold_button_preserves_real_mark_indicator(dashboard):
    from raven import layout as L
    app, views, store = dashboard
    records = [Job("1", "root", "main", "RUNNING"),
               Job("12_0", "array", "main", "PENDING", dependency="afterok:1"),
               Job("12_1", "array", "main", "PENDING", dependency="afterok:12_0")]
    store.apply_jobs(records)
    app.tab, app.marks = "deps", {"12_0"}
    rows, hits = views.deps_tab(store.snapshot(), app, 120, 30)
    y = next(y for y, kind, jid in hits if kind == "dep" and jid == "12_0")
    assert views.g.mark in L.row_text(rows[y])
    assert any(row == y and kind == "control" and value["id"].startswith("jobgroup:deps:")
               for row, kind, value in hits)
