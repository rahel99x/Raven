"""Cohort task disclosures are exact, idempotent, and isolated in Details."""

import copy
from types import SimpleNamespace

import pytest

from raven import array_disclosure as D, arrays, job_panels as J, layout as L
from raven.config import Config
from raven.controller import App
from raven.model import Finished, Job, Store
from raven.views import Views


@pytest.fixture
def dashboard():
    cfg = Config({"log_lines": 0, "animations": False, "startup_animation": False,
                  "smooth_scrolling": False, "workspace": {"density": "compact", "split": 40}})
    store = Store(persist=False)
    store.jobs = [Job("52_0", "train", "cpu", "RUNNING"),
                  Job("52_1", "train", "cpu", "PENDING"),
                  Job("63_0", "test", "cpu", "RUNNING"),
                  Job("63_1", "test", "cpu", "PENDING")]
    store.finished = [Finished("41_0", "old", "FAILED"), Finished("41_1", "old", "COMPLETED")]
    app = App(store, None, None, cfg, "test", interactive=False)
    views = Views(L.Glyphs(False), cfg)
    app.views_ref = views
    state = SimpleNamespace(app=app, store=store, views=views,
                            groups=arrays.summarize(store.jobs, store.finished))
    def context(snap, target):
        jid = target.research_job_id or target.selected_id
        job = next((record for record in store.jobs + store.finished if record.id == jid), None)
        return {"job": job, "jid": jid, "view": target.research_view, "generation": 1,
                "binding": None, "snap": snap}
    app.research = SimpleNamespace(context=context, request=lambda _: {"status": "ok", "groups": state.groups},
                                  interval=5, generation=1, close=lambda: None)
    app.tab, app.research_view = "research", "arrays"
    state.render = lambda: views.compose(store.snapshot(), app, 240, 64)
    yield state


def button(hits, array_id, *, inline=False):
    for y, kind, value in hits:
        descriptor, left, right = None, None, None
        if kind == "control" and not inline:
            descriptor, left, right = value, value["left"], value["right"]
        elif kind == "job_panel_action" and inline:
            (inner_kind, payload), left, right = value
            if inner_kind == "control":
                descriptor = payload
        if descriptor and descriptor["id"].startswith("research-array-disclosure:") and descriptor["id"].endswith(":" + array_id):
            return y, left, right, descriptor
    pytest.fail("Array disclosure is not visible: " + array_id)


@pytest.mark.parametrize("ascii_", [False, True])
def test_cohort_arrows_open_and_close_once_without_navigating_or_starting_work(dashboard, ascii_):
    app, views = dashboard.app, dashboard.views
    views.set_ascii(ascii_)
    rows, hits = dashboard.render()
    y, x, _, descriptor = button(hits, "52")
    assert L.row_text(rows[y])[x] == ("v" if ascii_ else "▾")
    assert descriptor["action"] == ("command", "array open 52 ''")
    before = app.tab, app.mode, app.selected_id, app.research_job_id
    app.click(y, x, hits, button="press")
    app.click(y, x, hits, button="release")
    assert app.research_array_open
    assert app.research_groups[app.cursor["research"]]["id"] == "52"
    assert (app.tab, app.mode, app.selected_id, app.research_job_id) == before
    app.research_task_offset = 24
    app.run_command(descriptor["action"][1])
    assert app.research_array_open and app.research_task_offset == 24
    rows, hits = dashboard.render()
    y, x, _, descriptor = button(hits, "52")
    assert L.row_text(rows[y])[x] == (">" if ascii_ else "▸")
    assert descriptor["action"] == ("command", "array close 52 ''")
    app.click(y, x, hits, button="press")
    app.click(y, x, hits, button="release")
    assert not app.research_array_open
    assert "task page / offset" not in L.to_text(dashboard.render()[0], 240)


@pytest.mark.parametrize("event", ["motion", "drag", "release", "right"])
def test_passive_and_right_clicks_do_not_toggle_array_tasks(dashboard, event):
    app = dashboard.app
    _, hits = dashboard.render()
    y, x, _, _ = button(hits, "52")
    app.click(y, x, hits, button=event)
    assert not app.research_array_open and app.tab == "research" and app.mode == "main"


def test_clicking_normal_cohort_text_still_selects_without_toggling(dashboard):
    app = dashboard.app
    _, hits = dashboard.render()
    y, _, _, _ = button(hits, "63")
    app.click(y, 8, hits)
    assert app.research_groups[app.cursor["research"]]["id"] == "63"
    assert not app.research_array_open


@pytest.mark.parametrize("tab", ["jobs", "history"])
@pytest.mark.parametrize("ascii_", [False, True])
def test_inline_disclosure_is_exact_and_keeps_full_research_state_separate(dashboard, tab, ascii_):
    app, views = dashboard.app, dashboard.views
    views.set_ascii(ascii_)
    app.tab = tab
    J.initialize(app).update(mode="research", research_view="arrays")
    before = app.research_view, app.research_array_open, app.research_task_offset, app.cursor["research"]
    rows, hits = dashboard.render()
    y, x, _, descriptor = button(hits, "52", inline=True)
    assert L.row_text(rows[y])[x] == ("v" if ascii_ else "▾")
    selected = app.selected_id
    app.click(y, x, hits, button="press")
    app.click(y, x, hits, button="release")
    retained = J.initialize(app)["view_states"]["research:arrays"]
    assert retained["array_open"]
    assert retained["proxy"].research_groups[retained["cursor"]]["id"] == "52"
    assert app.tab == tab and app.mode == "main" and app.selected_id == selected
    assert (app.research_view, app.research_array_open, app.research_task_offset, app.cursor["research"]) == before
    rows, hits = dashboard.render()
    y, x, _, _ = button(hits, "52", inline=True)
    assert L.row_text(rows[y])[x] == (">" if ascii_ else "▸")
    assert "task page / offset" in L.to_text(rows, 240)
    app.click(y, x, hits)
    dashboard.render()
    assert not retained["array_open"]
    assert app.selected_id == selected and app.tab == tab


def test_selected_cohort_survives_publication_insertion_then_closes_if_removed(dashboard):
    app = dashboard.app
    dashboard.render()
    app.run_command("array open 52")
    app.research_task_offset = 24
    selected = app.research_groups[app.cursor["research"]]
    # Simulate a new launch inserted ahead of this cohort without changing
    # any selected allocation or using a positional task-page identity.
    dashboard.groups.insert(0, dict(selected, id="1"))
    dashboard.render()
    assert app.research_groups[app.cursor["research"]]["id"] == "52"
    assert app.research_array_open
    dashboard.groups[:] = [group for group in dashboard.groups if group["id"] != "52"]
    dashboard.render()
    assert not app.research_array_open and app.research_task_offset == 0


def test_cluster_qualified_buttons_and_rows_select_the_exact_duplicate_id(dashboard):
    app = dashboard.app
    base = copy.deepcopy(dashboard.groups[0])
    dashboard.groups = [dict(base, cluster="alpha"), dict(base, cluster="beta")]
    _, hits = dashboard.render()
    targets = [(y, target) for y, kind, target in hits if kind == "research_array"]
    assert [target for _, target in targets] == [(base["id"], "alpha"), (base["id"], "beta")]
    app.click(targets[1][0], 8, hits)
    assert app.cursor["research"] == 1
    app.run_command("array open " + base["id"])
    assert not app.command_ok and not app.research_array_open
    app.run_command("array open " + base["id"] + " beta")
    assert app.command_ok and app.research_array_open and app.cursor["research"] == 1
    app.run_command("array close " + base["id"] + " alpha")
    assert app.command_ok and app.research_array_open and app.cursor["research"] == 1


def test_stale_missing_or_wrong_workspace_commands_do_not_retarget(dashboard):
    app = dashboard.app
    dashboard.render()
    app.run_command("array open 52")
    before = app.cursor["research"], app.research_array_open, app.research_task_offset
    for command in ("array open 999", "array open", "array close 999", "array open 52 wrong-cluster"):
        app.run_command(command)
        assert not app.command_ok
        assert (app.cursor["research"], app.research_array_open, app.research_task_offset) == before
    app.research_view = "experiment"
    app.run_command("array close 52")
    assert not app.command_ok
    assert (app.cursor["research"], app.research_array_open, app.research_task_offset) == before


def test_array_task_commands_use_only_published_data_even_without_a_service(dashboard):
    app = dashboard.app
    dashboard.render()
    app.research = None
    app.run_command("array open 52")
    assert app.command_ok and app.research_array_open
    app.run_command("array close 52")
    assert app.command_ok and not app.research_array_open
