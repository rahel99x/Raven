"""Per-list presentation preserves actual IDs, scoped counts, and cheap redraws."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from raven import history_browser, job_groups, job_list_controls, pane_drag, table_ui
from raven.config import Config
from raven.controller import App
from raven.layout import Glyphs, row_text, vlen
from raven.model import Finished, Job, Store
from raven.views import Views


CONTEXTS = ("jobs", "recent", "history", "group", "history:analytics",
            "history:research", "history:log", "history:deps", "analytics:advisor")
SECONDARY_LISTS = ("recent", "history", "history:analytics", "history:research",
                   "history:log", "history:deps", "analytics:advisor")


def job(jid, state="RUNNING"):
    return Job(str(jid), "encoder-seed", "local", state, user="alex", account="lab",
               cluster="local", submit="2026-10-12T10:00:00", cpus=2)


def finished(jid, minute):
    return Finished(str(jid), "encoder-seed", "COMPLETED", user="alex", account="lab",
                    cluster="local", submit="2026-10-12T10:00:00",
                    end=f"2026-10-12T12:{minute:02d}:00")


def ids(records):
    return [record.id for record in records]


def batches():
    return [job("10_0"), job("20_0"), job("30_0", "PENDING"), job("40"),
            job("10_1", "PENDING"), job("20_1"), job("30_1", "PENDING"),
            job("10_2", "PENDING")]


@pytest.fixture
def app(monkeypatch):
    cfg = Config({"animations": False, "startup_animation": False, "gpu_sampling": False,
                  "smooth_scrolling": False, "log_lines": 0})
    result = App(Store(persist=False), None, None, cfg, "alex", interactive=False)
    result.views_ref = Views(Glyphs(False), cfg)
    result.saved_preferences = []
    monkeypatch.setattr(result, "save", lambda: result.saved_preferences.append(table_ui.save(result)))
    yield result
    if result.research:
        result.research.close()


def frame(app, width=150, height=40):
    return app.views_ref.compose(app.store.snapshot(), app, width, height)


def populate(app):
    app.store.apply_jobs(batches())
    app.store.apply_finished([finished("52_2", 5), finished("71_1", 4),
                              finished("52_1", 3), finished("71_0", 2),
                              finished("52_0", 1)])
    app.store.group = batches()


@pytest.mark.parametrize("context", CONTEXTS)
def test_modes_and_batch_ranks_persist_independently_for_each_listing(app, context):
    before = {other: job_list_controls.fingerprint(app, other) for other in CONTEXTS if other != context}
    app.run_command(f"joblist {context} sort-max")
    assert job_list_controls.fingerprint(app, context) == ("grouped", "max")
    assert app.saved_preferences
    restored = SimpleNamespace()
    table_ui.initialize(restored)
    table_ui.restore(restored, app.saved_preferences[-1])
    assert job_list_controls.fingerprint(restored, context) == ("grouped", "max")
    assert {other: job_list_controls.fingerprint(app, other) for other in before} == before
    app.run_command(f"joblist {context} sequential")
    table_ui.restore(restored, app.saved_preferences[-1])
    assert job_list_controls.mode(restored, context) == "sequential"
    assert {other: job_list_controls.fingerprint(app, other) for other in before} == before
    app.run_command(f"joblist {context} sort-min")
    assert job_list_controls.fingerprint(app, context) == ("grouped", "min")


def test_corrupt_or_unknown_per_list_preferences_are_not_restored(app):
    table_ui.restore(app, {"job_list_modes": {"history": "grouped", "recent": [], "deps": "sequential",
                                           "history:log": "bad", "jobs": "sequential"},
                          "job_list_sorts": {"history:log": "max", "group": False,
                                             "nodes": "min", "history": {}}})
    assert app.table_state["job_list_modes"] == {"history": "grouped", "jobs": "sequential"}
    assert app.table_state["job_list_sorts"] == {"history:log": "max"}
    table_ui.restore(app, {"job_list_modes": [], "job_list_sorts": "max"})
    assert app.table_state["job_list_modes"] == app.table_state["job_list_sorts"] == {}


def test_legacy_global_group_toggle_overrides_local_modes_but_keeps_ranks_and_recents(app):
    populate(app)
    app.run_command("joblist history grouped")
    app.run_command("joblist jobs sort-max")
    app.run_command("jobgroups off")
    assert job_list_controls.mode(app, "history") == "sequential"
    assert job_list_controls.fingerprint(app, "jobs") == ("sequential", "max")
    assert job_list_controls.mode(app, "recent") == "grouped"
    assert ids(app.history_jobs()) == ["52_2", "71_1", "52_1", "71_0", "52_0"]
    app.run_command("jobgroups on")
    assert job_list_controls.mode(app, "history") == "grouped"
    assert job_list_controls.fingerprint(app, "jobs") == ("grouped", "max")
    assert job_list_controls.mode(app, "recent") == "grouped"
    assert ids(app.history_jobs()) == ["52_2", "52_1", "52_0", "71_1", "71_0"]


@pytest.mark.parametrize("context", CONTEXTS)
@pytest.mark.parametrize("rank,expected", [
    ("min", ["30_0", "30_1", "40", "10_0", "10_1", "10_2", "20_0", "20_1"]),
    ("max", ["20_0", "20_1", "40", "10_0", "10_1", "10_2", "30_0", "30_1"]),
])
def test_batch_rank_keeps_whole_groups_contiguous_and_preserves_exact_records(app, context, rank, expected):
    records = batches()
    app.run_command(f"joblist {context} sort-{rank}")
    snap = dict(app.store.snapshot(), jobs=records, group=records)
    result = job_groups.project_records(app, snap, records, context)
    assert ids(result) == expected
    originals = {record.id: record for record in records}
    assert all(record is originals[record.id] for record in result)
    group = job_groups.registry(app).index.for_job("10_0")
    job_groups.fold(app, group.id, True)
    result = job_groups.project_records(app, snap, records, context)
    assert ids(result) == [jid for jid in expected if jid not in ("10_1", "10_2")]
    assert all(record is originals[record.id] for record in result)


@pytest.mark.parametrize("context", SECONDARY_LISTS)
def test_secondary_list_ranks_use_only_admitted_records_without_fetching_siblings(app, context):
    records = batches()
    app.run_command(f"joblist {context} sort-max")
    # The queue knows a second running member of 20, but this listing admits
    # only 20_0. Both admitted singleton blocks therefore precede larger 10.
    admitted = [record for record in records if record.id != "20_1"]
    snap = dict(app.store.snapshot(), jobs=records, group=records)
    result = job_groups.project_records(app, snap, admitted, context)
    assert ids(result) == ["20_0", "40", "10_0", "10_1", "10_2", "30_0", "30_1"]
    assert "20_1" not in ids(result)
    app.run_command(f"joblist {context} sort-min")
    assert ids(job_groups.project_records(app, snap, admitted, context)) == [
        "30_0", "30_1", "20_0", "40", "10_0", "10_1", "10_2"]


def test_group_rank_uses_account_queue_counts_independently_of_personal_queue(app):
    records = batches()
    admitted = [record for record in records if record.id != "20_1"]
    app.run_command("joblist group sort-max")
    snap = dict(app.store.snapshot(), jobs=[job("40") for _ in range(10)], group=records)
    assert ids(job_groups.project_records(app, snap, admitted, "group")) == [
        "20_0", "40", "10_0", "10_1", "10_2", "30_0", "30_1"]
    app.run_command("joblist group sort-min")
    assert ids(job_groups.project_records(app, snap, admitted, "group")) == [
        "30_0", "30_1", "40", "10_0", "10_1", "10_2", "20_0"]


@pytest.mark.parametrize("context", CONTEXTS)
def test_sequential_exposes_individual_exact_ids_without_touching_shared_folds(app, context):
    records = batches()
    snap = dict(app.store.snapshot(), jobs=records, group=records)
    app.run_command(f"joblist {context} grouped")
    job_groups.project_records(app, snap, records, context)
    group = job_groups.registry(app).index.for_job("10_0")
    job_groups.fold(app, group.id, True)
    folded = list(app.table_state["collapsed"])
    app.run_command(f"joblist {context} sequential")
    assert job_groups.project_records(app, snap, records, context) == records
    assert not app.job_group_metadata[context]
    assert app.table_state["collapsed"] == folded


def test_history_grouped_and_sequential_keep_completion_order_and_exact_selection(app):
    populate(app)
    app.tab = "history"
    frame(app)
    assert ids(app.history_jobs()) == ["52_2", "52_1", "52_0", "71_1", "71_0"]
    app.selected_id = "52_0"
    app.cursor["history"] = ids(app.history_jobs()).index("52_0")
    app.run_command("joblist history sequential")
    frame(app)
    assert ids(app.history_jobs()) == ["52_2", "71_1", "52_1", "71_0", "52_0"]
    assert app.selected_id == "52_0" and app.target_ids() == ["52_0"]
    app.run_command("joblist history sort-min")
    frame(app)
    assert ids(app.history_jobs()) == ["71_1", "71_0", "52_2", "52_1", "52_0"]
    assert app.selected_id == "52_0" and app.target_ids() == ["52_0"]


@pytest.mark.parametrize("context", ["history", "group", "history:analytics", "history:research",
                                     "history:log", "history:deps"])
@pytest.mark.parametrize("legacy_groups", [True, False])
def test_return_to_folded_group_preserves_exact_selected_child_and_shared_fold(app, context, legacy_groups, monkeypatch):
    populate(app)
    app.tab = context.removeprefix("history:")
    app.table_state["groups"] = legacy_groups
    chosen = "10_2" if context == "group" else "52_0"
    app.run_command(f"joblist {context} grouped")
    frame(app)
    group = job_groups.registry(app).index.for_job(chosen)
    assert job_groups.fold(app, group.id, True)
    app.run_command(f"joblist {context} sequential")
    frame(app)
    if context.startswith("history:"):
        monkeypatch.setattr(app, "open_log", lambda jid: setattr(app, "log_job", jid))
        assert history_browser.activate(app, chosen)
    else:
        app.selected_id = chosen
        visible = app.group_ids if context == "group" else ids(app.history_jobs())
        app.cursor[context] = visible.index(chosen)
    frame(app)
    assert app.selected_id == chosen
    folds = list(app.table_state["collapsed"])
    app.run_command(f"joblist {context} grouped")
    frame(app)
    assert app.selected_id == chosen
    if context.startswith("history:"):
        visible = [item.record.id for item in history_browser.initialize(app)["items"]]
    else:
        visible = app.group_ids if context == "group" else ids(app.history_jobs())
    assert chosen in visible
    assert app.table_state["collapsed"] == folds


@pytest.mark.parametrize("context", ["history", "group", "history:analytics"])
def test_sequential_ungroup_targets_only_selected_child_not_hidden_shared_group(app, context):
    from raven import job_group_menu, job_selection, manual_group_ui
    populate(app)
    app.tab = context.removeprefix("history:")
    chosen = "10_2" if context == "group" else "52_0"
    siblings = ("10_0", "10_1") if context == "group" else ("52_1", "52_2")
    app.run_command(f"joblist {context} grouped")
    frame(app)
    group = job_groups.registry(app).index.for_job(chosen)
    assert job_groups.fold(app, group.id, True)
    app.run_command(f"joblist {context} sequential")
    frame(app)
    if context.startswith("history:"):
        assert history_browser.activate(app, chosen)
    else:
        app.selected_id = chosen
        visible = app.group_ids if context == "group" else ids(app.history_jobs())
        app.cursor[context] = visible.index(chosen)
    frame(app)
    focused = job_selection.context(app)
    assert focused["scope"] == context and focused["selected"] == chosen
    focused["pointed"] = chosen
    assert job_group_menu.open_menu(app, focused)
    assert app.job_group_menu_state["context"]["ungroup_groups"] == {}
    job_group_menu.close(app)
    assert manual_group_ui.run_action(app, "ungroup", explicit=True)
    index = job_groups.registry(app).ensure(app.store.snapshot())
    assert index.for_job(chosen) is None
    assert index.for_job(siblings[0]).members == siblings


@pytest.mark.parametrize("context", ["jobs", "recent", "history", "group", "history:analytics"])
def test_manual_create_in_sequential_makes_new_group_visible_in_its_listing(app, context):
    from raven import job_selection
    populate(app)
    app.tab = "jobs" if context == "recent" else context.removeprefix("history:")
    app.run_command(f"joblist {context} sequential")
    frame(app)
    members = ("10_0", "20_0") if context in ("jobs", "group") else ("52_2", "71_1")
    chosen = members[0]
    if context.startswith("history:"):
        assert history_browser.activate(app, chosen)
    else:
        app.selected_id = chosen
        visible = (app.visible_ids + app.recent_ids if app.tab == "jobs" else
                   app.group_ids if app.tab == "group" else ids(app.history_jobs()))
        app.cursor[app.tab] = visible.index(chosen)
    frame(app)
    assert job_selection.context(app)["scope"] == context
    app.marks = set(members)
    app.run_command("jobgroup create")
    assert job_list_controls.mode(app, context) == "grouped"
    frame(app)
    group = job_groups.registry(app).index.for_job(chosen)
    assert group.kind == "manual" and set(group.members) == set(members)
    assert job_groups.registry(app).is_collapsed(group)
    meta = job_groups.metadata_for_record(app, context, app.selected_id)
    assert meta.header and meta.collapsed and meta.group.id == group.id
    assert not app.marks


def button(rows, hits, ident, width, height):
    y, control = next((y, value) for y, kind, value in hits
                      if kind == "control" and value.get("id") == ident)
    assert 0 <= y < height and 0 <= control["left"] < control["right"] <= width
    assert row_text(rows[y])[control["left"]:control["right"]].strip()
    return y, control


def click(app, size, ident):
    rows, hits = frame(app, *size)
    if ident.startswith("joblist:analytics:advisor:"):
        # At 80 columns the live-job section follows a scrollable historical
        # recommendation table. Reach it through the existing Details scroll.
        for _ in range(20):
            if any(kind == "control" and value.get("id") == ident for _, kind, value in hits):
                break
            app.run_command("focus details")
            app.run_command("panel-scroll down")
            rows, hits = frame(app, *size)
    y, control = button(rows, hits, ident, *size)
    app.click(y, control["left"], hits, button="press")
    app.click(y, control["left"], hits, button="release")


@pytest.mark.parametrize("ascii_", [False, True])
@pytest.mark.parametrize("size", [(80, 24), (120, 30), (200, 50)])
@pytest.mark.parametrize("context", ["history", "group", "history:analytics", "history:research",
                                     "history:log", "history:deps", "analytics:advisor"])
def test_list_buttons_are_painted_and_dispatch_on_each_job_list(app, context, size, ascii_):
    populate(app)
    app.views_ref.g = Glyphs(ascii_)
    app.tab = context.removeprefix("history:")
    if context == "analytics:advisor":
        app.tab, app.analytics_view = "analytics", "advisor"
    for action in ("sequential", "sort-max", "grouped", "sort-min"):
        click(app, size, f"joblist:{context}:{action}")
        if action.startswith("sort-"):
            assert job_list_controls.fingerprint(app, context) == ("grouped", action[5:])
        else:
            assert job_list_controls.mode(app, context) == action
    rows, _ = frame(app, *size)
    assert all(vlen(row_text(row)) <= size[0] for row in rows)


@pytest.mark.parametrize("ascii_", [False, True])
@pytest.mark.parametrize("size", [(80, 24), (120, 30), (200, 50)])
def test_jobs_mode_and_recents_sort_buttons_use_their_own_list(app, size, ascii_):
    populate(app)
    app.views_ref.g = Glyphs(ascii_)
    click(app, size, "joblist:jobs:sequential")
    assert job_list_controls.mode(app, "jobs") == "sequential"
    assert job_list_controls.mode(app, "recent") == "grouped"
    click(app, size, "joblist:jobs:grouped")
    assert job_list_controls.mode(app, "jobs") == "grouped"
    frame(app, *size)
    app.selected_id = app.recent_ids[0]
    app.cursor["jobs"] = len(app.visible_ids)
    for action in ("sort-max", "sort-min"):
        click(app, size, f"joblist:recent:{action}")
        assert job_list_controls.fingerprint(app, "recent") == ("grouped", action[5:])
        assert job_list_controls.batch_mode(app, "jobs") == "min"


@pytest.mark.parametrize("tab", history_browser.TABS)
def test_browser_reuses_projection_on_unchanged_redraw_without_fetching_sources(app, tab, monkeypatch):
    populate(app)
    app.tab, app.width, app.height, app.body_origin = tab, 160, 50, 6
    app.run_command(f"joblist history:{tab} sort-max")
    snap = app.store.snapshot()
    monkeypatch.setattr(app.store, "snapshot", lambda: pytest.fail("Browser redraw fetched another snapshot"))
    monkeypatch.setattr(app, "open_log", lambda *_: pytest.fail("Browser redraw opened a log"))
    native_calls = []

    def render():
        pane_drag.begin_frame(app, 160, 50)
        return history_browser.wrap_render(app.views_ref, snap, app, 160, 40,
            lambda width, height: (native_calls.append((width, height)) or [[("CONTENT", "")]], []))

    with patch.object(job_groups, "project_records", wraps=job_groups.project_records) as project:
        render()
        first_items = history_browser.initialize(app)["items"]
        inference_count = job_groups.registry(app).inference_count
        for _ in range(3):
            render()
        assert project.call_count == 1
        assert history_browser.initialize(app)["items"] is first_items
        assert job_groups.registry(app).inference_count == inference_count
    assert len(native_calls) == 4


@pytest.mark.parametrize("tab", history_browser.TABS)
def test_browser_reorders_in_place_state_changes_without_stale_cached_batch_rank(app, tab):
    app.store.apply_jobs(batches())
    app.tab, app.width, app.height, app.body_origin = tab, 160, 50, 6
    app.run_command(f"joblist history:{tab} sort-max")

    def render_ids():
        pane_drag.begin_frame(app, 160, 50)
        history_browser.wrap_render(app.views_ref, app.store.snapshot(), app, 160, 40,
                                    lambda width, height: ([[('CONTENT', '')]], []))
        return [item.record.id for item in history_browser.initialize(app)["items"]]

    assert set(render_ids()[:2]) == {"20_0", "20_1"}
    for record in app.store.jobs:
        if record.id.startswith("10_"):
            record.state = "RUNNING"
    assert set(render_ids()[:3]) == {"10_0", "10_1", "10_2"}
