"""Frozen job menus never resolve a new target from stale terminal coordinates."""
import pytest

from tower import job_group_menu as M, job_groups as G, layout as L, manual_job_groups as B
from tower.config import Config
from tower.controller import App
from tower.model import Finished, Job, Store
from tower.views import Views


@pytest.fixture
def dashboard(monkeypatch):
    store = Store(persist=False)
    store.jobs = [Job(str(jid), "individual-" + str(jid), "cpu", "RUNNING",
                      submit=f"2026-10-09T10:{jid:02}:00", cluster="local")
                  for jid in range(1, 13)]
    cfg = Config({"animations": False, "startup_animation": False, "log_lines": 0})
    app = App(store, None, None, cfg, "tester", interactive=False)
    app.width, app.height = 130, 36
    app.views_ref = Views(L.Glyphs(False), cfg)
    monkeypatch.setattr(app, "save", lambda: None)
    yield app, store
    if app.research:
        app.research.close()


def frame(app, store):
    return app.views_ref.compose(store.snapshot(), app, app.width, app.height)


def source(app, store, pointed="1", selected="1", ids=None):
    ids = tuple(ids or (record.id for record in store.jobs or store.finished))
    return dict(scope=app.tab, ids=ids, pointed=pointed, selected=selected,
                tokens=B.selection_tokens(store.snapshot(), ids))


def group(app, store, ids=("7", "8")):
    result = B.create(app, store.snapshot(), ids)
    assert result.changed
    return result.group_id


def paint(app):
    return M.overlay(app.views_ref, {}, app, app.width, app.height)


def action_index(app, action, group_id=None):
    return next(index for index, item in enumerate(M.initialize(app)["items"])
                if item["action"] == action and (group_id is None or item.get("group_id") == group_id))


def choose(app, action, group_id=None):
    M.initialize(app)["cursor"] = action_index(app, action, group_id)
    assert M.handle_key(app, "enter")


def test_only_pointed_marked_or_active_row_opens_and_retains_unrelated_marks(dashboard):
    app, store = dashboard
    app.marks = {"1", "2", "other-list"}
    assert not M.open_menu(app, source(app, store, pointed="3"))
    assert app.mode == "main" and app.marks == {"1", "2", "other-list"}
    assert M.open_menu(app, source(app, store))
    assert M.initialize(app)["context"]["targets"] == ("1", "2")
    assert [item["action"] for item in M.initialize(app)["items"]] == ["create", "cancel"]
    choose(app, "create")
    assert app.marks == {"other-list"}
    assert G.registry(app).ensure(store.snapshot()).for_job("1").kind == "manual"


def test_active_unmarked_single_job_does_not_absorb_unrelated_marks(dashboard):
    app, store = dashboard
    gid = group(app, store)
    app.marks = {"2", "3"}
    assert M.open_menu(app, source(app, store))
    state = M.initialize(app)
    assert state["context"]["targets"] == ("1",)
    assert all(item["action"] != "create" for item in state["items"])
    choose(app, "add", gid)
    assert app.marks == {"2", "3"}
    assert G.registry(app).ensure(store.snapshot()).for_job("1").members == ("1", "7", "8")


def test_add_options_exclude_noops_and_insertion_is_naturally_ordered(dashboard):
    app, store = dashboard
    gid = group(app, store, ("1", "2", "3"))
    app.marks = {"7", "8", "9"}
    M.open_menu(app, source(app, store, "7", "7"))
    choose(app, "add", gid)
    app.marks = {"4", "5", "6"}
    M.open_menu(app, source(app, store, "4", "4"))
    choose(app, "add", gid)
    assert G.registry(app).ensure(store.snapshot()).for_job("1").members == tuple(map(str, range(1, 10)))
    app.marks = {"1", "4"}
    M.open_menu(app, source(app, store))
    assert all(item.get("group_id") != gid for item in M.initialize(app)["items"])


def test_ungroup_exists_only_for_grouped_jobs_and_collapsed_group_is_whole(dashboard):
    app, store = dashboard
    gid = group(app, store, ("1", "2", "3"))
    app.table_state["groups"] = True
    G.fold(app, gid, True)
    assert M.open_menu(app, source(app, store))
    assert gid in M.initialize(app)["context"]["ungroup_groups"]
    choose(app, "ungroup")
    index = G.registry(app).ensure(store.snapshot())
    assert all(index.for_job(jid) is None for jid in ("1", "2", "3"))
    assert M.open_menu(app, source(app, store))
    assert all(item["action"] != "ungroup" for item in M.initialize(app)["items"])


def test_expanded_group_ungroups_only_exact_marked_members(dashboard):
    app, store = dashboard
    group(app, store, ("1", "2", "3", "4"))
    app.marks = {"1", "3"}
    M.open_menu(app, source(app, store))
    choose(app, "ungroup")
    index = G.registry(app).ensure(store.snapshot())
    assert index.for_job("1") is None and index.for_job("3") is None
    assert index.for_job("2").members == ("2", "4")


@pytest.mark.parametrize("change", ["missing", "attempt", "origin"])
def test_source_changes_reject_action_atomically(dashboard, change):
    app, store = dashboard
    app.marks = {"1", "2"}
    M.open_menu(app, source(app, store))
    if change == "missing":
        store.jobs.pop(0)
    elif change == "attempt":
        store.jobs[0].submit = "2026-10-10T10:01:00"
    else:
        app.tab = "history"
    choose(app, "create")
    assert app.mode == "main" and app.marks == {"1", "2"}
    assert not G.registry(app).ensure(store.snapshot()).groups
    assert "changed" in app.message.lower()


def test_destination_changed_after_menu_open_is_not_replaced(dashboard):
    app, store = dashboard
    gid = group(app, store)
    M.open_menu(app, source(app, store))
    store.jobs[6].submit = "2026-10-10T10:07:00"
    choose(app, "add", gid)
    assert G.registry(app).ensure(store.snapshot()).for_job("1") is None
    assert "changed" in app.message.lower()


def test_menu_selection_remains_frozen_when_marks_change(dashboard):
    app, store = dashboard
    app.marks = {"1", "2"}
    M.open_menu(app, source(app, store))
    app.marks = {"3", "4"}
    choose(app, "create")
    index = G.registry(app).ensure(store.snapshot())
    assert index.for_job("1").members == ("1", "2")
    assert index.for_job("3") is None and app.marks == {"3", "4"}


def test_hover_paint_wheel_and_arrow_navigation_never_fetch_or_infer(dashboard, monkeypatch):
    app, store = dashboard
    group(app, store)
    app.marks = {"1", "2"}
    M.open_menu(app, source(app, store))
    monkeypatch.setattr(store, "snapshot", lambda: pytest.fail("Pointer copied Store"))
    monkeypatch.setattr(G.registry(app), "ensure", lambda *_: pytest.fail("Pointer inferred groups"))
    paint(app)
    hit = M.initialize(app)["hits"][0]
    for _ in range(100):
        assert M.handle_mouse(app, hit[0], hit[1], "motion")
        M.handle_key(app, "down")
        M.handle_mouse(app, hit[0], hit[1], "wheel-up")
        paint(app)
    assert app.mode == M.MODE and not app.table_state.get("manual_groups", {}).get("detached")


def test_orphan_release_hover_and_mismatched_release_never_activate(dashboard):
    app, store = dashboard
    app.marks = {"1", "2"}
    M.open_menu(app, source(app, store))
    paint(app)
    first, last = M.initialize(app)["hits"][0], M.initialize(app)["hits"][-1]
    M.handle_mouse(app, first[0], first[1], "release")
    M.handle_mouse(app, first[0], first[1], "motion")
    M.handle_mouse(app, first[0], first[1], "press")
    M.handle_mouse(app, last[0], last[1], "release")
    assert app.mode == M.MODE and app.marks == {"1", "2"}
    M.handle_mouse(app, first[0], first[1], "press")
    M.handle_mouse(app, first[0], first[1], "release")
    assert app.mode == "main" and not app.marks


@pytest.mark.parametrize("change", ["resize", "tab", "scroll"])
def test_old_painted_mouse_and_f8_controls_are_rejected(dashboard, change):
    app, store = dashboard
    app.marks = {"1", "2"}
    M.open_menu(app, source(app, store))
    paint(app)
    hit = M.initialize(app)["hits"][0]
    control = M.controls(app)[0]
    M.handle_mouse(app, hit[0], hit[1], "press")
    if change == "resize":
        app.width -= 1
    elif change == "tab":
        app.tab = "history"
    else:
        M._scroll(app, 1)
    assert M.controls(app) == ()
    M.handle_mouse(app, hit[0], hit[1], "release")
    M.run_command(app, control.action[1].split())
    assert app.mode == M.MODE and app.marks == {"1", "2"}


def test_previous_menu_semantic_action_cannot_activate_reopened_menu(dashboard):
    app, store = dashboard
    app.marks = {"1", "2"}
    M.open_menu(app, source(app, store))
    paint(app)
    control = M.controls(app)[0]
    M.handle_key(app, "esc")
    M.open_menu(app, source(app, store))
    paint(app)
    assert M.run_command(app, control.action[1].split())
    assert app.mode == M.MODE and app.marks == {"1", "2"}


def test_history_export_keeps_exact_frozen_ids_and_existing_exporter(dashboard):
    app, store = dashboard
    store.finished = [Finished(record.id, record.name, state="COMPLETED", submit=record.submit)
                      for record in store.jobs]
    store.jobs = []
    app.tab = "history"
    app.history_all_records = list(store.finished)
    app.marks = {"1", "2"}
    M.open_menu(app, source(app, store))
    app.marks = {"3"}
    choose(app, "export")
    assert app.mode == "history_log_menu"
    assert app.history_log_export_state["jobs"] == ("1", "2")
    assert app.marks == {"3"}


@pytest.mark.parametrize("ascii_", [True, False])
@pytest.mark.parametrize("width,height", [(1, 1), (2, 2), (5, 5), (20, 8), (40, 12), (130, 36)])
def test_menu_rows_and_hits_fit_every_terminal(dashboard, ascii_, width, height):
    app, store = dashboard
    group(app, store)
    app.marks = {"1", "2"}
    M.open_menu(app, source(app, store))
    app.width, app.height = width, height
    app.views_ref.g = L.Glyphs(ascii_)
    rows = paint(app)
    assert rows
    assert all(0 <= y < height and 0 <= x < width and x + L.vlen(L.row_text(row)) <= width
               for y, x, row in rows)
    assert all(0 <= y < height and 0 <= left < right <= width
               for y, left, right, _ in M.initialize(app)["hits"])


def test_keyboard_cancel_and_navigation_do_not_change_groups(dashboard):
    app, store = dashboard
    group(app, store)
    app.marks = {"1", "2"}
    M.open_menu(app, source(app, store))
    M.handle_key(app, "end")
    assert M.initialize(app)["items"][M.initialize(app)["cursor"]]["action"] == "cancel"
    M.handle_key(app, "tab")
    assert M.initialize(app)["cursor"] == 0
    M.handle_key(app, "btab")
    assert M.initialize(app)["items"][M.initialize(app)["cursor"]]["action"] == "cancel"
    M.handle_key(app, "enter")
    assert app.mode == "main" and app.marks == {"1", "2"}


def test_changed_page_tick_closes_without_snapshot(dashboard, monkeypatch):
    app, store = dashboard
    M.open_menu(app, source(app, store))
    app.tab = "research"
    monkeypatch.setattr(store, "snapshot", lambda: pytest.fail("Tick copied Store"))
    M.tick(app)
    assert app.mode == "main"


def test_f8_graph_uses_current_semantic_menu_action(dashboard):
    from tower import interaction
    app, store = dashboard
    app.marks = {"1", "2"}
    M.open_menu(app, source(app, store))
    rows = paint(app)
    interaction.publish(app, [], [], app.width, app.height, overlays=rows)
    assert M.handle_key(app, "f8")
    assert app.interaction_state["active"]
    controls = M.controls(app)
    app.interaction_state["focused"] = controls[0].id
    assert M.handle_key(app, "enter")
    assert app.mode == "main"
    assert G.registry(app).ensure(store.snapshot()).for_job("1").members == ("1", "2")


def test_many_automatic_groups_are_available_without_rendering_every_option(dashboard, monkeypatch):
    app, store = dashboard
    store.jobs.extend(Job(f"{base}_{task}", "array", "cpu", "RUNNING",
                          submit="2026-10-09T12:00:00", cluster="local")
                      for base in range(10000, 10300) for task in (1, 2))
    expected = G.registry(app).ensure(store.snapshot())
    assert len(expected.groups) == 300
    assert M.open_menu(app, source(app, store))
    destinations = [item for item in M.initialize(app)["items"] if item["action"] == "add"]
    assert len(destinations) == 3
    assert len(M.initialize(app)["destinations"]) == 300
    monkeypatch.setattr(store, "snapshot", lambda: pytest.fail("Scrolling copied Store"))
    monkeypatch.setattr(G.registry(app), "ensure", lambda *_: pytest.fail("Expansion inferred groups"))
    choose(app, "more")
    assert sum(item["action"] == "add" for item in M.initialize(app)["items"]) == 300
    paint(app)
    assert len(M.initialize(app)["hits"]) < app.height
    M.handle_key(app, "end")
    paint(app)
    state = M.initialize(app)
    assert state["top"] > 0
    assert state["items"][state["hits"][-1][3]]["action"] == "cancel"
    for _ in range(5):
        M.handle_mouse(app, 0, 0, "wheel-down")
    assert state["cursor"] == len(state["items"]) - 1
    M.handle_key(app, "home")
    paint(app)
    assert state["top"] == 0


def test_context_group_and_destination_labels_disambiguate_duplicate_names(dashboard):
    app, store = dashboard
    first, second = group(app, store, ("3", "4")), group(app, store, ("5", "6"))
    registry = G.registry(app)
    registry.manual._labels[first] = registry.manual._labels[second] = "Same name"
    registry.manual.revision += 1
    assert M.open_menu(app, source(app, store))
    labels = [item["label"] for item in M.initialize(app)["items"] if item["action"] == "add"]
    assert labels == ["Add to Same name (3..4)", "Add to Same name (5..6)"] or labels == [
        "Add to Same name (5..6)", "Add to Same name (3..4)"]


def test_repeated_cosmetic_overlay_keeps_one_rail_and_preserves_other_panes(dashboard):
    from tower import scrollbars
    app, store = dashboard
    M.open_menu(app, source(app, store))
    scrollbars.begin_frame(app)
    other = scrollbars.register(app, "other-pane", (2, 2, 10, 20), 50, 8, 0, 0, lambda *_: None)
    for _ in range(scrollbars.MAX_PANES * 2):
        paint(app)
    staged = scrollbars.initialize(app)["staged"]
    assert len(staged) == 2
    assert staged[0] is other
    assert staged[1].key == M.PANE


def test_close_releases_frozen_record_evidence(dashboard):
    app, store = dashboard
    group(app, store)
    M.open_menu(app, source(app, store))
    assert M.initialize(app)["context"]
    M.close(app)
    assert M.initialize(app)["context"] is None
    assert M.initialize(app)["items"] == ()
    assert M.initialize(app)["destinations"] == ()
    assert M.initialize(app)["prefix"] == M.initialize(app)["suffix"] == ()


def _five_groups(app, store, count=5):
    return [group(app, store, (str(jid), str(jid + 1))) for jid in (3, 5, 7, 9, 11)[:count]]


@pytest.mark.parametrize("count", range(6))
def test_compact_menu_shows_three_newest_groups_and_cancel_last(dashboard, count):
    app, store = dashboard
    groups = _five_groups(app, store, count)
    assert M.open_menu(app, source(app, store))
    state = M.initialize(app)
    displayed = [item["group_id"] for item in state["items"] if item["action"] == "add"]
    assert displayed == list(reversed(groups))[:3]
    assert any(item["action"] == "more" for item in state["items"]) == (count > 3)
    assert state["items"][-1]["action"] == "cancel"
    if count > 3:
        choose(app, "more")
        assert [item["group_id"] for item in state["items"] if item["action"] == "add"] == list(reversed(groups))
        assert state["items"][-1]["action"] == "cancel"
        assert all(item["action"] != "more" for item in state["items"])


def test_recency_uses_latest_submission_not_name_creation_or_highest_jobid(dashboard):
    app, store = dashboard
    # The low-ID batch was submitted last, despite being grouped first.
    store.jobs[2].submit = "2026-10-10T10:03:00"
    groups = _five_groups(app, store)
    assert M.open_menu(app, source(app, store))
    state = M.initialize(app)
    assert [item["group_id"] for item in state["items"] if item["action"] == "add"] == [groups[0], groups[4], groups[3]]


def test_unknown_submission_uses_natural_jobid_order(dashboard):
    app, store = dashboard
    for record in store.jobs:
        record.submit = ""
    groups = _five_groups(app, store)
    assert M.open_menu(app, source(app, store))
    assert [item["group_id"] for item in M.initialize(app)["items"] if item["action"] == "add"] == list(reversed(groups))[:3]


def test_more_invalidates_old_mouse_and_semantic_actions_and_preserves_targets(dashboard):
    app, store = dashboard
    _five_groups(app, store)
    app.marks = {"1", "2"}
    assert M.open_menu(app, source(app, store))
    paint(app)
    state = M.initialize(app)
    old_token, frozen = state["token"], state["context"]
    more_index = action_index(app, "more")
    old_controls = M.controls(app)
    hit = next(hit for hit in state["hits"] if hit[-1] == more_index)
    M.handle_mouse(app, hit[0], hit[1], "press")
    M.handle_mouse(app, hit[0], hit[1], "release")
    assert state["expanded"] and state["token"] > old_token
    assert state["context"] is frozen and frozen["targets"] == ("1", "2")
    assert state["cursor"] == more_index
    assert M.controls(app) == ()
    paint(app)
    M.handle_mouse(app, hit[0], hit[1], "release")
    for control in old_controls:
        M.run_command(app, control.action[1].split())
    assert app.mode == M.MODE and app.marks == {"1", "2"}
    assert G.registry(app).ensure(store.snapshot()).for_job("1") is None


def test_more_keeps_open_time_evidence_when_jobs_or_marks_change(dashboard):
    app, store = dashboard
    groups = _five_groups(app, store)
    assert M.open_menu(app, source(app, store))
    app.marks = {"2"}
    store.jobs[2].submit = "2026-10-10T10:03:00"
    choose(app, "more")
    choose(app, "add", groups[0])
    assert app.mode == "main" and app.marks == {"2"}
    assert G.registry(app).ensure(store.snapshot()).for_job("1") is None
    assert "changed" in app.message.lower()


def test_reopening_restores_compact_list_and_end_reaches_cancel(dashboard):
    app, store = dashboard
    _five_groups(app, store)
    assert M.open_menu(app, source(app, store))
    choose(app, "more")
    choose(app, "cancel")
    assert M.open_menu(app, source(app, store))
    assert not M.initialize(app)["expanded"]
    app.width, app.height = 20, 6
    M.handle_key(app, "end")
    paint(app)
    state = M.initialize(app)
    assert state["items"][state["hits"][-1][-1]]["action"] == "cancel"
    M.handle_key(app, "enter")
    assert app.mode == "main"
