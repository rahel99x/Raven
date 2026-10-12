"""Public commands, saved preferences, and keyboard fallback behavior."""
import copy

import pytest

from raven import job_groups, layout, table_ui
from raven.config import Config
from raven.controller import App
from raven.model import Job, Store
from raven.views import Views


@pytest.fixture
def dashboard(monkeypatch):
    store = Store(persist=False)
    store.jobs = [Job(str(jid), "batch", "cpu", "RUNNING",
                      submit="2026-10-09T10:00:00", cluster="local")
                  for jid in (10, 20, 30)]
    cfg = Config({"startup_animation": False, "animations": False, "log_lines": 0})
    app = App(store, None, None, cfg, "tester", interactive=False)
    app.views_ref = Views(layout.Glyphs(False), cfg)
    monkeypatch.setattr(app, "save", lambda: None)
    yield app, store
    if app.research:
        app.research.close()


def frame(app, store):
    return app.views_ref.compose(store.snapshot(), app, 180, 45)


def index(app, store):
    return job_groups.registry(app).ensure(store.snapshot())


def test_public_create_persists_exact_attempts_and_reset_restores_deduction(dashboard):
    app, store = dashboard
    frame(app, store)
    app.marks = {"10", "20"}
    app.run_command("jobgroup create")
    group = index(app, store).for_job("10")
    assert group is not None and group.kind == "manual"
    assert job_groups.registry(app).is_collapsed(group)
    saved = table_ui.save(app)
    assert len(saved["manual_groups"]["groups"]) == 1
    table_ui.initialize(app)
    table_ui.restore(app, saved)
    assert index(app, store).for_job("20").id == group.id
    frame(app, store)
    app.run_command("jobgroup ungroup")
    assert index(app, store).for_job("10") is None
    assert len(table_ui.save(app)["manual_groups"]["detached"]) == 2
    app.run_command("jobgroup reset")
    clean = table_ui.save(app)["manual_groups"]
    assert clean == {"version": 1, "groups": [], "detached": []}
    assert group.id not in app.table_state["collapsed"]


def test_create_with_grouping_hidden_enables_new_collapsed_group(dashboard):
    app, store = dashboard
    app.table_state["groups"] = False
    frame(app, store)
    app.marks = {"10", "20"}
    app.handle("g")
    assert app.table_state["groups"]
    assert job_groups.registry(app).is_collapsed(index(app, store).for_job("10"))


def test_invalid_commands_keep_rows_marks_and_grouping_unchanged(dashboard):
    app, store = dashboard
    frame(app, store)
    before = table_ui.save(app)
    app.marks = {"10"}
    for command in ("jobgroup create", "jobgroup create extra", "jobgroup ungroup extra", "jobgroup reset extra"):
        app.run_command(command)
        assert table_ui.save(app) == before
        assert app.marks == {"10"}


def test_custom_g_binding_is_not_hijacked_by_grouping(dashboard):
    app, store = dashboard
    frame(app, store)
    app.marks = {"10", "20"}
    app.keymap["g"] = "help"
    app.handle("g")
    assert app.mode == "help"
    assert index(app, store).for_job("10") is None


def test_uppercase_u_clears_marks_without_ungrouping(dashboard):
    app, store = dashboard
    frame(app, store)
    app.marks = {"10", "20"}
    app.handle("g")
    group = index(app, store).for_job("10")
    frame(app, store)
    app.marks = {"10", "20"}
    app.handle("U")
    assert app.marks == set()
    assert index(app, store).for_job("10") == group


def test_saved_group_preferences_are_detached_from_caller_owned_mutables(dashboard):
    app, store = dashboard
    frame(app, store)
    app.marks = {"10", "20"}
    app.handle("g")
    saved = table_ui.save(app)
    expected = copy.deepcopy(saved["manual_groups"])
    saved["manual_groups"]["groups"][0]["members"].clear()
    assert table_ui.save(app)["manual_groups"] == expected
    saved["manual_groups"] = expected
    table_ui.restore(app, saved)
    expected["groups"].clear()
    assert table_ui.save(app)["manual_groups"]["groups"]


@pytest.mark.parametrize("mark,clear", [("a", "U"), ("mark all", "unmark")])
def test_explicit_remarking_rebinds_new_attempt_without_an_intermediate_frame(dashboard, mark, clear):
    app, store = dashboard
    frame(app, store)
    send = app.handle if mark == "a" else app.run_command
    send(mark)
    old = app.job_selection_state["mark_tokens"]["10"]
    store.jobs[0].submit = "2026-10-10T10:00:00"
    frame(app, store)
    assert app.job_selection_state["mark_tokens"]["10"] == old
    send(clear)
    send(mark)
    assert app.job_selection_state["mark_tokens"]["10"] != old
    app.handle("g")
    assert index(app, store).for_job("10").kind == "manual"
