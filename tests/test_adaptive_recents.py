"""Recents budgets and large-history navigation use published exact records."""
from unittest.mock import patch
from types import SimpleNamespace

import pytest

from raven import recent_history as recents, table_tools, table_ui
from raven.config import Config
from raven.controller import App
from raven.model import Finished, Job, Live, Store, stamp
from raven.workspace_layout import Rect


@pytest.fixture
def app():
    result = App(Store(persist=False), None, None, Config(), "reader", ascii_=True)
    result.visible_ids = ["active"]
    result.recent_ids = []
    return result


def observation(count=50):
    return dict(jobs=[Job("active", "live", "cpu", "RUNNING")],
                finished=[Finished(str(1000 + index), name=f"job-{index:04d}", state="COMPLETED",
                                   end="2026-10-08T12:00:00") for index in range(count)],
                departed_jobs={}, live={}, tags={})


def activate(app, snap, height=14):
    queue, page = recents.allocate(app, height, len(snap["jobs"]))
    loaded = recents.records(app, snap)
    app.recent_ids = [record.id for record in loaded]
    recents.publish(app, loaded, page, len(snap["jobs"]), rect=Rect(10, 10, 50, page + 2))
    return queue, page, loaded


def select_recent(app, index):
    app.cursor["jobs"] = len(app.visible_ids) + index
    app.selected_id = app.recent_ids[index]


def test_legacy_preview_remains_exact_preference_until_adaptive_layout(app):
    snap = observation(100)
    assert len(recents.records(app, snap)) == 5
    app.table_tools_state["recents"]["count"] = 10
    assert len(recents.records(app, snap)) == 10
    app.table_tools_state["expand_recent"] = True
    assert len(recents.records(app, snap)) == 25


def test_larger_main_height_and_divider_admit_more_history(app):
    snap = observation(100)
    _, small_page, small = activate(app, snap, 14)
    _, big_page, big = activate(app, snap, 40)
    assert big_page > small_page and len(big) > len(small)
    recents.resize(app, 90)
    more_queue, even_larger = recents.allocate(app, 40, 50)
    assert more_queue >= 1 and even_larger > round(36 * .35)


@pytest.mark.parametrize("height", range(0, 20))
def test_allocation_stays_within_actual_main_budget(app, height):
    queue, recent = recents.allocate(app, height, 100)
    assert queue >= 0 and recent >= 0
    # A table's rule and heading are rendered only for an admitted data row.
    required = queue + recent + (2 if queue else 0) + (2 if recent else 0)
    assert required <= height or height < 2


def test_empty_queue_gives_recents_full_height(app):
    queue, recent = recents.allocate(app, 30, 0)
    assert (queue, recent) == (0, 28)


def test_tiny_main_keeps_the_focused_section_and_its_table_heading(app):
    queue, recent = recents.allocate(app, 4, 10)
    assert (queue, recent) == (2, 0)
    state = recents.initialize(app)
    state.loaded = [Finished("1000")]
    app.cursor["jobs"] = 10
    queue, recent = recents.allocate(app, 4, 10)
    assert (queue, recent) == (0, 2)


def test_drag_reserves_chosen_queue_space_even_when_only_one_job_remains(app):
    queue, recent = recents.allocate(app, 40, 1)
    assert queue == 1 and recent == 35
    recents.resize(app, 20)
    queue, recent = recents.allocate(app, 40, 1)
    assert (queue, recent) == (29, 7)


def test_default_unfiltered_preview_does_not_traverse_ten_thousand_records(app):
    snap = observation(10_000)
    with patch("raven.table_query.matches", wraps=table_ui.matches) as matches:
        result = recents.records(app, snap)
    assert [record.id for record in result] == [str(1000 + index) for index in range(5)]
    assert matches.call_count == 5


def test_prefix_growth_keeps_same_job_actions_between_frames(app):
    snap = observation(100)
    _, page, loaded = activate(app, snap, 10)
    select_recent(app, len(loaded) - 1)
    old_id = app.selected_id
    assert recents.navigate(app, "down")
    assert app.selected_id == str(int(old_id) + 1)
    assert len(app.recent_ids) > len(loaded)
    assert app.recent_ids[app.cursor["jobs"] - 1] == app.selected_id
    assert app.top["recent"] > 0
    assert recents.initialize(app).page == page


def test_new_completion_preserves_matching_selected_candidate_at_preview_boundary(app):
    snap = observation(100)
    _, page, loaded = activate(app, snap, 10)
    select_recent(app, len(loaded) - 1)
    selected = app.selected_id
    snap["finished"].insert(0, Finished("new-finish", "new-run", "COMPLETED"))
    refreshed = recents.records(app, snap)
    assert selected in [record.id for record in refreshed]
    assert len(refreshed) > len(loaded)
    assert recents.initialize(app).page == page


def test_removed_or_now_filtered_selected_job_is_not_reintroduced(app):
    snap = observation(100)
    _, _, loaded = activate(app, snap, 10)
    select_recent(app, len(loaded) - 1)
    selected = app.selected_id
    snap["finished"] = [record for record in snap["finished"] if record.id != selected]
    assert selected not in [record.id for record in recents.records(app, snap)]
    assert recents.candidate_limit(app) == 5


def test_growing_prefix_skips_members_of_a_collapsed_launch(app):
    snap = observation(1000)
    _, page, loaded = activate(app, snap, 10)
    state = recents.initialize(app)
    state.projector = lambda snap_, raw: raw[:1] + [record for record in raw if int(record.id) >= 1500]
    projected = state.projector(snap, loaded)
    app.recent_ids = [record.id for record in projected]
    recents.publish(app, projected, page, 1)
    select_recent(app, 0)
    assert recents.navigate(app, "down")
    assert app.selected_id == "1500"
    assert not any(str(identifier) in app.recent_ids for identifier in range(1001, 1500))


def test_end_keeps_collapsed_group_navigation_at_actual_representatives(app):
    snap = observation(1000)
    _, page, loaded = activate(app, snap, 10)
    state = recents.initialize(app)
    state.projector = lambda snap_, raw: raw[:1] + raw[-1:]
    projected = state.projector(snap, loaded)
    app.recent_ids = [record.id for record in projected]
    recents.publish(app, projected, page, 1)
    select_recent(app, 0)
    assert recents.navigate(app, "end")
    assert app.selected_id == "1999"
    assert app.recent_ids == ["1000", "1999"]


def test_keyboard_crosses_back_into_queue_but_mouse_stays_within_recents(app):
    activate(app, observation(10), 10)
    select_recent(app, 0)
    assert recents.navigate(app, "up", wheel=True)
    assert app.selected_id == "1000"
    assert recents.navigate(app, "up")
    assert app.selected_id == "active" and app.cursor["jobs"] == 0


def test_wheel_is_bounded_to_exact_absolute_recents_rect(app):
    activate(app, observation(20), 14)
    select_recent(app, 0)
    assert not recents.handle_mouse(app, 9, 12, button="wheel-down")
    assert not recents.handle_mouse(app, 11, 60, button="wheel-down")
    assert recents.handle_mouse(app, 11, 12, button="wheel-down")
    assert app.selected_id == "1001"


def test_mouse_focus_uses_current_recents_window_when_queue_selected(app):
    activate(app, observation(100), 14)
    select_recent(app, 8)
    app.top["recent"] = 4
    app.cursor["jobs"], app.selected_id = 0, "active"
    assert recents.handle_mouse(app, 11, 12, button="wheel-down")
    assert app.selected_id == "1005"


def test_end_loads_all_matching_history_and_home_returns_first_recent(app):
    snap = observation(10_000)
    activate(app, snap, 14)
    select_recent(app, 0)
    assert recents.navigate(app, "end")
    assert app.selected_id == "10999"
    assert len(app.recent_ids) == 10_000
    assert recents.initialize(app).exhausted
    assert recents.navigate(app, "home")
    assert app.selected_id == "1000" and app.top["recent"] == 0


def test_filters_find_old_history_before_applying_preview_size(app):
    snap = observation(100)
    table_tools.set_filter_text(app, "recent", "job-0099")
    assert [record.id for record in recents.records(app, snap)] == ["1099"]


def test_pending_precedes_accounting_without_duplicates_or_active_rows(app):
    snap = observation(10)
    snap["departed_jobs"] = {"1000": Job("1000", "departed", "cpu", "RUNNING"),
                              "active": snap["jobs"][0]}
    snap["finished"].insert(0, Finished("active", name="duplicate-live"))
    result = recents.records(app, snap)
    assert isinstance(result[0], Job) and result[0].id == "1000"
    assert len({record.id for record in result}) == len(result)
    assert "active" not in [record.id for record in result]


def test_tag_filter_cascading_jobid_sort_and_empty_reset(app):
    snap = observation(20)
    snap["tags"] = {str(1000 + index): {"tags": ["keep"]} for index in range(2, 20)}
    table_tools.set_filter_text(app, "recent", "#keep")
    from raven.table_sort import set_sort
    set_sort(app, "recent", "id", "desc")
    assert [record.id for record in recents.records(app, snap)] == ["1006", "1005", "1004", "1003", "1002"]
    set_sort(app, "recent", "id", "off")
    assert [record.id for record in recents.records(app, snap)] == ["1002", "1003", "1004", "1005", "1006"]


def test_large_index_skips_repeat_filter_work_but_amended_content_invalidates(app):
    snap = observation(1000)
    activate(app, snap)
    state = recents.initialize(app)
    state.target = 500
    with patch("raven.table_query.matches", wraps=table_ui.matches) as matches:
        first = recents.records(app, snap)
        initial_count = matches.call_count
        assert initial_count == 1000
        assert recents.records(app, snap) == first
        assert matches.call_count == initial_count
        snap["finished"][0].name = "amended same-ID accounting"
        recents.records(app, snap)
        assert matches.call_count == initial_count * 2


def test_large_history_does_not_repeat_cascading_sort_without_data_changes(app):
    snap = observation(1000)
    activate(app, snap)
    from raven.table_sort import set_sort
    set_sort(app, "recent", "name", "desc")
    set_sort(app, "recent", "id", "asc")
    recents.records(app, snap)
    recents.initialize(app).target = 500
    with patch("raven.table_sort.sort_rows", wraps=table_ui.sort_rows) as sort_rows:
        first = recents.records(app, snap)
        second = recents.records(app, snap)
        assert first == second and first is not second
        assert sort_rows.call_count == 1
        snap["finished"][0].name = "zzzz amended"
        refreshed = recents.records(app, snap)
        assert refreshed[0].id == "1000"
        assert sort_rows.call_count == 2


def test_large_index_refreshes_when_tags_or_active_jobs_change(app):
    snap = observation(1000)
    activate(app, snap)
    recents.initialize(app).target = 500
    table_tools.set_filter_text(app, "recent", "#keep")
    # A settings change resets the window; deliberately enlarge it afterward.
    recents.records(app, snap)
    recents.initialize(app).target = 500
    assert recents.records(app, snap) == []
    snap["tags"]["1999"] = {"tags": ["keep"]}
    assert [record.id for record in recents.records(app, snap)] == ["1999"]
    snap["jobs"].append(Job("1999", "active again", "cpu", "RUNNING"))
    assert recents.records(app, snap) == []


def test_large_index_refreshes_when_live_numeric_samples_change(app):
    snap = observation(1000)
    activate(app, snap)
    app.table_tools_state["numeric"]["recent"] = [table_tools.parse_rule("cpu_eff>50%")]
    snap["live"]["1999"] = Live(avg=.4)
    recents.records(app, snap)
    recents.initialize(app).target = 500
    assert recents.records(app, snap) == []
    snap["live"]["1999"].avg = .9
    assert [record.id for record in recents.records(app, snap)] == ["1999"]


def test_large_window_cache_expires_after_inclusive_boundary(app, monkeypatch):
    snap = observation(500)
    ended = stamp("2026-10-08T12:00:00")
    monkeypatch.setattr(recents.clock, "now", lambda: ended + 5)
    app.table_tools_state["recents"]["window"] = 10
    activate(app, snap)
    recents.initialize(app).target = 500
    assert len(recents.records(app, snap)) == 500
    monkeypatch.setattr(recents.clock, "now", lambda: ended + 10)
    assert len(recents.records(app, snap)) == 500
    monkeypatch.setattr(recents.clock, "now", lambda: ended + 10.01)
    assert recents.records(app, snap) == []


def test_user_keymap_is_respected_and_modal_or_other_tabs_are_untouched(app):
    activate(app, observation(100), 14)
    select_recent(app, 0)
    app.keymap["j"] = "down"
    assert recents.handle_key(app, "j") and app.selected_id == "1001"
    app.mode = "confirm"
    assert not recents.handle_key(app, "j")
    app.mode, app.tab = "main", "history"
    assert not recents.handle_key(app, "j")


def test_details_keyboard_focus_is_preserved_but_wheel_targets_recents(app):
    activate(app, observation(100), 14)
    select_recent(app, 0)
    app.layout_state.focus = "details"
    app.job_panel_state["focus"] = "content"
    assert not recents.handle_key(app, "down")
    assert app.selected_id == "1000"
    assert recents.handle_mouse(app, 11, 12, button="wheel-down")
    assert app.selected_id == "1001"
    assert app.layout_state.focus == "main"
    assert app.job_panel_state["focus"] == ""


@pytest.mark.parametrize("data", [{"ratio": True}, {"ratio": 9}, {"ratio": 91},
                                   {"ratio": 50, "manual": 1}, {"version": 2, "ratio": 50}, []])
def test_invalid_saved_split_is_ignored_without_restoring_observations(app, data):
    recents.restore(app, data)
    assert recents.initialize(app).ratio == 35


def test_saved_split_restores_only_layout_preferences(app):
    recents.resize(app, 70)
    saved = recents.save(app)
    assert saved == {"version": 1, "ratio": 70, "manual": True}
    other = SimpleNamespace()
    recents.restore(other, saved)
    restored = recents.initialize(other)
    assert restored.ratio == 70 and restored.manual_split
    assert not restored.enabled and restored.loaded == [] and restored.observation == {}
