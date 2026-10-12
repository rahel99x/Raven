"""Recents modes preserve batch layout and exact actions through completions."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from raven import job_groups, recents_ui, recent_history, table_ui, table_sort
from raven.config import Config
from raven.controller import App
from raven.layout import Glyphs, row_text
from raven.model import Finished, Job, Store
from raven.views import Views


def finished(jid, minute=1, state="COMPLETED"):
    return Finished(str(jid), "train", state, submit="2026-10-12T10:00:00",
                    end=f"2026-10-12T12:{minute:02d}:00", cluster="local")


def ids(records):
    return [record.id for record in records]


@pytest.fixture
def app(monkeypatch):
    cfg = Config({"animations": False, "startup_animation": False, "gpu_sampling": False,
                  "smooth_scrolling": False, "log_lines": 0})
    result = App(Store(persist=False), None, None, cfg, "reader", interactive=False)
    result.views_ref = Views(Glyphs(False), cfg)
    monkeypatch.setattr(result, "save", lambda: None)
    yield result
    if result.research:
        result.research.close()


def frame(app, width=150, height=40):
    return app.views_ref.compose(app.store.snapshot(), app, width, height)


def select(app, jid):
    app.selected_id = jid
    app.cursor["jobs"] = (app.visible_ids + app.recent_ids).index(jid)


def test_interleaved_completions_are_contiguous_only_in_grouped(app):
    records = [finished("52_2", 5), finished("71_1", 4, "CANCELLED"),
               finished("52_1", 3, "FAILED"), finished("71_0", 2), finished("52_0", 1)]
    app.store.apply_finished(records)
    assert ids(app.recent_jobs()) == ["52_2", "52_1", "52_0", "71_1", "71_0"]
    group = job_groups.registry(app).index.for_job("52_0")
    job_groups.fold(app, group.id, True)
    assert ids(app.recent_jobs()) == ["52_2", "71_1", "71_0"]
    folds = list(app.table_state["collapsed"])
    app.run_command("recents sequential")
    assert ids(app.recent_jobs()) == ids(records)
    assert not app.job_group_metadata["recent"]
    assert app.table_state["collapsed"] == folds
    assert app.table_state["groups"] and app.table_state["auto_groups"]
    app.run_command("recents grouped")
    assert ids(app.recent_jobs()) == ["52_2", "71_1", "71_0"]


def test_new_completion_keeps_selected_collapsed_representative_and_group_rank(app):
    app.store.apply_finished([finished("71_0", 4), finished("52_1", 3), finished("52_0", 1), finished("71_1", 0)])
    frame(app)
    select(app, "52_1")
    group_id = job_groups.registry(app).index.for_job("52_1").id
    assert job_groups.fold(app, group_id, True)
    frame(app)
    app.store.apply_finished([finished("52_2", 5), *app.store.finished])
    frame(app)
    assert app.recent_ids == ["52_1", "71_0", "71_1"]
    assert app.selected_id == "52_1" and app.target_ids() == ["52_1"]
    meta = job_groups.metadata_for_record(app, "recent", "52_1")
    assert meta.header and meta.representative_id == "52_1"
    assert ids(meta.records) == ["52_2", "52_1", "52_0"]
    assert meta.stats.counts["completed"] == 3
    job_groups.fold(app, group_id, False)
    frame(app)
    assert app.recent_ids == ["52_2", "52_1", "52_0", "71_0", "71_1"]
    assert app.selected_id == "52_1"


def test_mode_switch_keeps_selected_child_without_changing_shared_folds_or_marks(app):
    app.store.apply_finished([finished("52_2", 5), finished("71", 4), finished("52_1", 3), finished("52_0", 1)])
    frame(app)
    group_id = job_groups.registry(app).index.for_job("52_1").id
    assert job_groups.fold(app, group_id, True)
    app.run_command("recents sequential")
    frame(app)
    select(app, "52_0")
    app.marks = {"52_0", "71"}
    before = table_ui.save(app)
    app.run_command("recents grouped")
    frame(app)
    assert app.selected_id == "52_0"
    assert app.recent_ids == ["52_0", "71"]
    assert app.marks == {"52_0", "71"}
    assert app.table_state["collapsed"] == before["collapsed"]
    assert table_ui.save(app)["manual_groups"] == before["manual_groups"]


@pytest.mark.parametrize("departed", [False, True])
def test_mode_change_retains_selected_older_loaded_record(app, departed):
    if departed:
        app.store.apply_jobs([Job(str(i), "job", "cpu", "RUNNING") for i in range(100)])
        app.store.apply_jobs([])
    else:
        app.store.apply_finished([finished(str(i), i % 60) for i in range(100)])
    frame(app)
    recent_history.initialize(app).target = 100
    frame(app)
    chosen = app.recent_ids[-1]
    select(app, chosen)
    app.run_command("recents sequential")
    frame(app)
    assert app.selected_id == chosen
    assert app.recent_ids[app.cursor["jobs"]] == chosen
    assert app.target_ids() == [chosen]


def test_header_sort_is_honored_within_whole_blocks_and_mode_resets_only_recents(app):
    app.store.apply_finished([finished("52_2", 5), finished("71_1", 4), finished("52_1", 3), finished("71_0", 2)])
    table_sort.set_sort(app, "recent", "id", "asc")
    table_sort.set_sort(app, "history", "name", "desc")
    assert ids(app.recent_jobs()) == ["52_1", "52_2", "71_0", "71_1"]
    app.run_command("recents sequential")
    assert ids(app.recent_jobs()) == ["52_2", "71_1", "52_1", "71_0"]
    assert table_sort.chain(app, "recent") is None
    assert table_sort.chain(app, "history") == [("name", "desc")]
    table_sort.set_sort(app, "recent", "id", "asc")
    assert ids(app.recent_jobs()) == ["52_1", "52_2", "71_0", "71_1"]


def test_mode_persists_and_invalid_values_fall_back_to_grouped(app):
    assert recents_ui.mode(app) == "grouped"
    app.run_command("recents sequential")
    saved = table_ui.save(app)
    restored = SimpleNamespace()
    table_ui.initialize(restored)
    table_ui.restore(restored, saved)
    assert recents_ui.mode(restored) == "sequential"
    restored.table_state["recents_mode"] = {}
    assert recents_ui.mode(restored) == "grouped"


@pytest.mark.parametrize("ascii_", [False, True])
@pytest.mark.parametrize("size", [(80, 24), (120, 30), (200, 50)])
def test_mode_buttons_click_without_resizing_or_losing_full_wheel_rect(app, ascii_, size):
    app.views_ref.g = Glyphs(ascii_)
    app.store.apply_jobs([Job("active", "live", "cpu", "RUNNING")])
    app.store.apply_finished([finished("52_0", 5), finished("71", 4), finished("52_1", 3)])
    rows, hits = frame(app, *size)
    if size == (80, 24):
        # The compact vertical budget shows one focused table at a time.
        select(app, app.recent_ids[0])
        rows, hits = frame(app, *size)
    controls = {value["id"]: (y, value) for y, kind, value in hits
                if kind == "control" and value.get("group") == "recents-modes"}
    assert set(controls) == {"recents-mode:grouped", "recents-mode:sequential"}
    y, button = controls["recents-mode:sequential"]
    assert row_text(rows[y])[button["left"]:button["right"]].strip()
    state = recent_history.initialize(app)
    ratio = state.ratio
    assert state.rect.x <= button["right"] - 1 < state.rect.x + state.rect.width
    app.click(y, button["left"], hits, button="press")
    app.click(y, button["left"], hits, button="release")
    assert recents_ui.mode(app) == "sequential"
    assert state.ratio == ratio and not app.pane_drag_state["capture"]


def test_legacy_recents_commands_still_dispatch(app):
    app.run_command("recents 25")
    assert app.table_tools_state["recents"]["count"] == 25
    app.run_command("recents window 2h")
    assert app.table_tools_state["recents"]["window"] == 7200
    app.run_command("recents expand")
    assert app.table_tools_state["expand_recent"]
    assert recents_ui.mode(app) == "grouped"


def test_grouped_recents_visibility_is_independent_of_other_tables(app):
    app.store.apply_finished([finished("52_0", 5), finished("71", 4), finished("52_1", 3)])
    app.table_state["groups"] = False
    frame(app)
    assert app.recent_ids == ["52_0", "52_1", "71"]
    assert job_groups.metadata_for_record(app, "recent", "52_0") is not None
    app.run_command("recents sequential")
    assert ids(app.recent_jobs()) == ["52_0", "71", "52_1"]
    app.run_command("recents grouped")
    assert ids(app.recent_jobs()) == ["52_0", "52_1", "71"]
    assert not app.table_state["groups"]


def test_grouped_recents_share_one_frame_inference_check_when_jobs_grouping_is_off(app):
    app.store.apply_finished([finished("52_0", 5), finished("71", 4), finished("52_1", 3)])
    app.table_state["groups"] = False
    registry = job_groups.registry(app)
    with patch.object(registry, "ensure", wraps=registry.ensure) as ensure:
        frame(app)
        assert ensure.call_count == 1


def test_departed_groups_show_accounting_until_real_terminal_states_arrive(app):
    jobs = [Job("52_0", "train", "cpu", "RUNNING"), Job("52_1", "train", "cpu", "PENDING")]
    app.store.apply_jobs(jobs)
    frame(app)
    group = job_groups.registry(app).index.for_job("52_0")
    assert job_groups.fold(app, group.id, True)
    app.store.apply_jobs([])
    frame(app)
    meta = job_groups.metadata_for_record(app, "recent", app.recent_ids[0])
    assert meta.stats.counts["accounting"] == 2
    assert meta.stats.counts["running"] == meta.stats.counts["pending"] == 0
    assert "awaiting accounting" in job_groups.summary(meta.stats)
    assert jobs[0].state == "RUNNING" and jobs[1].state == "PENDING"
    app.store.apply_finished([Finished("52_0", "train", "COMPLETED"), Finished("52_1", "train", "FAILED")])
    frame(app)
    meta = job_groups.metadata_for_record(app, "recent", app.recent_ids[0])
    assert meta.stats.counts["completed"] == meta.stats.counts["failed"] == 1
    assert not meta.stats.counts.get("accounting", 0)


def test_sequential_keyboard_does_not_change_invisible_shared_folds(app):
    app.store.apply_finished([finished("52_0", 5), finished("52_1", 4), finished("52_2", 3)])
    frame(app)
    group = job_groups.registry(app).index.for_job("52_0")
    assert job_groups.fold(app, group.id, True)
    app.run_command("recents sequential")
    frame(app)
    select(app, "52_0")
    assert not table_ui.handle_key(app, "right")
    assert job_groups.registry(app).is_collapsed(group)
    app.run_command("recents grouped")
    app.table_state["groups"] = False
    frame(app)
    assert table_ui.handle_key(app, "right")
    assert not job_groups.registry(app).is_collapsed(group)
    assert not app.table_state["groups"]


def test_sequential_ungroup_detaches_only_the_selected_member(app):
    from raven import manual_group_ui
    app.store.apply_finished([finished("52_0", 5), finished("52_1", 4), finished("52_2", 3)])
    frame(app)
    group = job_groups.registry(app).index.for_job("52_0")
    assert job_groups.fold(app, group.id, True)
    app.run_command("recents sequential")
    frame(app)
    select(app, "52_0")
    assert manual_group_ui.run_action(app, "ungroup", explicit=True)
    index = job_groups.registry(app).ensure(app.store.snapshot())
    assert index.for_job("52_0") is None
    assert index.for_job("52_1").members == ("52_1", "52_2")


def test_sequential_context_menu_does_not_promote_member_to_hidden_group(app):
    from raven import job_group_menu, job_selection
    app.store.apply_finished([finished("52_0", 5, "CANCELLED"), finished("52_1", 4)])
    frame(app)
    group = job_groups.registry(app).index.for_job("52_0")
    assert job_groups.fold(app, group.id, True)
    app.run_command("recents sequential")
    frame(app)
    select(app, "52_0")
    context = job_selection.context(app)
    context["pointed"] = "52_0"
    assert job_group_menu.open_menu(app, context)
    assert app.job_group_menu_state["context"]["ungroup_groups"] == {}
    assert any(item["action"] == "rerun" for item in app.job_group_menu_state["items"])


def test_grouped_prefix_does_not_admit_all_siblings_or_repeat_inference(app):
    app.store.apply_finished([finished("52_" + str(i), 1) for i in range(1000)])
    with patch.object(table_ui, "matches", wraps=table_ui.matches) as matches:
        assert len(app.recent_jobs()) == 5
        assert matches.call_count == 5
    registry = job_groups.registry(app)
    count = registry.inference_count
    app.recent_jobs()
    assert registry.inference_count == count
    assert len(recent_history.initialize(app).loaded) == 5


@pytest.mark.parametrize("ascii_", [False, True])
@pytest.mark.parametrize("density", ["comfortable", "compact"])
@pytest.mark.parametrize("size", [(120, 30), (140, 40), (200, 50)])
def test_wrapped_grouping_toolbar_stays_above_queue_headers(app, ascii_, density, size):
    app.views_ref.g = Glyphs(ascii_)
    app.layout_state.density = density
    app.store.apply_jobs([Job("301", "running", "cpu", "RUNNING"), Job("302", "queued", "cpu", "PENDING")])
    app.store.apply_finished([finished("52_2", 5), finished("71_1", 4), finished("52_1", 3), finished("71_0", 2)])
    rows, hits = frame(app, *size)
    controls = [(y, value) for y, kind, value in hits if kind == "control" and value.get("group") == "grouping-toolbar"]
    headers = [y for y, kind, value in hits if kind == "sort_header" and value[0] == "jobs"]
    assert len(controls) == 7 and headers
    assert max(y for y, _ in controls) < min(headers)
    for y, value in controls:
        assert row_text(rows[y])[value["left"]:value["right"]].strip()
    assert all(y > max(headers) for y, kind, _ in hits if kind == "job")
    assert any(kind == "recent" for _, kind, _ in hits)
