"""Published cell geometry, captured resize ownership, and aligned job tables."""
from types import SimpleNamespace

import pytest

from tower import column_resize as C, layout as L, table_sort, table_ui
from tower.config import Config
from tower.controller import App
from tower.model import Finished, Job, Store
from tower.views import Views
from tower.text_selection import _slice


@pytest.fixture
def simple():
    app = SimpleNamespace(tab="jobs", mode="main", width=100, height=20,
                          toolbar_state={}, interaction_state={}, sort={}, reverse={},
                          saves=0, messages=[])
    table_ui.initialize(app)
    app.save = lambda: setattr(app, "saves", app.saves + 1)
    app.say = app.messages.append
    rows = [[(" " * app.width, "")]] * app.height
    hits = [(4, "sort_header", ("jobs", "id", 4, 9)),
            (4, "sort_header", ("jobs", "name", 11, 31)),
            (4, "sort_header", ("jobs", "st", 33, 35))]
    C.publish(app, rows, hits, app.width, app.height)
    return app, rows, hits


@pytest.fixture
def dashboard():
    cfg = Config({"log_lines": 0, "animations": False, "startup_animation": False})
    store = Store(persist=False)
    store.jobs = [Job("1001", "界界 e\u0301 research simulation", "main", "RUNNING", cpus=4)]
    store.finished = [Finished("999", "finished 界 experiment", "COMPLETED", cpus=4),
                      Finished("998", "old 界 failed", "FAILED", cpus=2)]
    app = App(store, None, None, cfg, "reader", interactive=False)
    views = Views(L.Glyphs(False), cfg)
    app.views_ref = views
    app.job_panel_defer_content = True
    app.workspace_main_usable_height = 40
    return app, views


def test_header_grip_and_buffer_do_not_take_sort_cells(simple):
    app, rows, _ = simple
    name = next(grip for grip in C.initialize(app)["grips"] if grip.column == "name")
    assert (name.left, name.right, name.width) == (31, 33, 20)
    assert L.row_text(rows[4])[31] == "│"
    assert not C.handle_mouse(app, 4, 30, "press")
    assert C.handle_mouse(app, 4, 32, "press")
    assert C.active(app)
    assert all(descriptor["rect"][1] < descriptor["rect"][3] for descriptor in C.descriptors(app))


def test_drag_crosses_other_panes_and_commits_only_once(simple):
    app, _, _ = simple
    assert C.handle_mouse(app, 4, 31, "press")
    for offset in range(1, 26):
        assert C.handle_mouse(app, 10, 31 + offset, "motion")
    assert app.table_state["widths"]["jobs"]["name"] == 45
    assert app.saves == 0
    assert "name" not in table_ui.save(app)["widths"]["jobs"]
    assert C.handle_mouse(app, 19, 56, "release")
    assert app.saves == 1 and not C.active(app)
    assert table_ui.save(app)["widths"]["jobs"]["name"] == 45
    assert not C.handle_mouse(app, 19, 56, "release")


@pytest.mark.parametrize("existing", [None, 18])
@pytest.mark.parametrize("abort", ["esc", "right", "tab", "resize", "modal", "gone"])
def test_cancel_restores_exact_preference_and_consumes_release(simple, existing, abort):
    app, rows, hits = simple
    if existing is not None:
        app.table_state["widths"]["jobs"] = {"name": existing}
    C.handle_mouse(app, 4, 31, "press")
    C.handle_mouse(app, 4, 46, "drag")
    if abort == "esc":
        assert C.handle_key(app, "esc")
    elif abort == "right":
        assert C.handle_mouse(app, 2, 80, "right")
    elif abort == "tab":
        app.tab = "history"
        C.handle_mouse(app, 4, 50, "motion")
    elif abort == "resize":
        app.width = 60
        C.publish(app, rows, hits, 60, app.height)
    elif abort == "modal":
        app.mode = "confirm"
        C.publish(app, rows, hits, app.width, app.height)
    else:
        C.publish(app, rows, [], app.width, app.height)
    assert not C.active(app)
    assert app.table_state["widths"]["jobs"].get("name") == existing
    assert app.saves == 0
    assert C.handle_mouse(app, 2, 80, "release")


def test_no_drag_does_not_install_width_or_save(simple):
    app, _, _ = simple
    C.handle_mouse(app, 4, 31, "press")
    C.handle_mouse(app, 4, 31, "release")
    assert app.table_state["widths"]["jobs"] == {} and app.saves == 0


def test_fresh_press_recovers_a_lost_release_without_stealing_new_gesture(simple):
    app, _, _ = simple
    C.handle_mouse(app, 4, 31, "press")
    C.handle_mouse(app, 4, 47, "motion")
    assert not C.handle_mouse(app, 1, 70, "press")
    assert not C.active(app) and app.table_state["widths"]["jobs"] == {}
    assert not C.handle_mouse(app, 1, 70, "release")
    assert app.saves == 0
    C.handle_mouse(app, 4, 31, "press")
    C.handle_mouse(app, 4, 47, "drag")
    C.handle_mouse(app, 4, 31, "release")
    assert app.table_state["widths"]["jobs"] == {} and app.saves == 0


def test_reset_is_per_column_and_respects_other_widths_and_marks(simple):
    app, _, _ = simple
    app.table_state["widths"]["jobs"] = {"name": 33, "id": 7}
    app.marks = {"1001"}
    assert C.handle_mouse(app, 4, 31, "right")
    assert app.table_state["widths"]["jobs"] == {"id": 7}
    assert app.marks == {"1001"} and app.saves == 1
    assert C.handle_mouse(app, 4, 31, "right")
    assert app.saves == 1


def test_extreme_motion_is_bounded_and_modifiers_do_not_start_resize(simple):
    app, _, _ = simple
    assert not C.handle_mouse(app, 4, 31, "press", shift=True)
    C.handle_mouse(app, 4, 31, "press")
    C.handle_mouse(app, 4, -1000000, "drag")
    assert app.table_state["widths"]["jobs"]["name"] == 2
    C.handle_mouse(app, 4, 1000000, "drag")
    assert 2 <= app.table_state["widths"]["jobs"]["name"] <= 84
    assert C.handle_mouse(app, 4, 31, "wheel-down")
    C.cancel(app)


def test_progress_is_fixed_and_hidden_or_tiny_frames_have_no_grips(simple):
    app, rows, _ = simple
    hits = [(4, "sort_header", ("jobs", "progress", 4, 10))]
    C.publish(app, rows, hits, 100, 20)
    assert not C.initialize(app)["grips"]
    C.publish(app, rows, [(4, "sort_header", ("jobs", "name", 4, 100))], 100, 20)
    assert not C.initialize(app)["grips"]
    C.publish(app, rows, hits, 100, None)
    assert not C.initialize(app)["grips"]


def test_hover_does_not_resize_or_persist(simple):
    app, _, _ = simple
    for index in range(5000):
        assert not C.handle_mouse(app, 4, index % 100, "motion")
    assert app.table_state["widths"] == {} and app.saves == 0


@pytest.mark.parametrize("ascii_", [False, True])
def test_narrow_custom_heading_is_clipped_before_next_header(ascii_):
    cells = []
    rows, _ = L.table([L.Column("wide", "界界HEADER", 2, 2), L.Column("id", "ID", 2, 2)],
                      [{"wide": "界界", "id": "42"}], 14, ascii_=ascii_, header_cells=cells)
    assert cells == [("wide", 1, 3), ("id", 5, 7)]
    assert _slice(rows[0], 5, 7) == "ID"
    assert all(L.vlen(L.row_text(row)) <= 14 for row in rows)


@pytest.mark.parametrize("width", [20, 40, 80, 120, 180, 340])
@pytest.mark.parametrize("ascii_", [False, True])
def test_recent_fields_align_with_exact_live_columns(dashboard, width, ascii_):
    app, views = dashboard
    views.set_ascii(ascii_)
    rows, hits = views.jobs_tab(app.store.snapshot(), app, None, width, 40)
    jobs = {value[1]: value[2:] for _, kind, value in hits if kind == "sort_header" and value[0] == "jobs"}
    recents = {value[1]: value[2:] for _, kind, value in hits if kind == "sort_header" and value[0] == "recent"}
    for live_key, recent_key in C.RECENT_ALIASES.items():
        if live_key in jobs:
            assert recents[recent_key] == jobs[live_key]
    assert all(L.vlen(L.row_text(row)) <= width for row in rows)
    if "progress" in jobs:
        for y, kind, _ in hits:
            if kind == "recent":
                assert not _slice(rows[y], *jobs["progress"]).strip()


def test_recent_respects_reordered_columns_and_shares_resized_job_name(dashboard):
    app, views = dashboard
    app.table_state["order"]["jobs"] = ["progress", "name", "id", "cpus", "st"]
    app.table_state["widths"]["jobs"] = {"name": 42}
    rows, hits = views.jobs_tab(app.store.snapshot(), app, None, 240, 40)
    name_cells = [(value[0], value[2:]) for _, kind, value in hits if kind == "sort_header" and value[1] == "name"]
    assert name_cells[0][1] == name_cells[1][1]
    assert name_cells[0][1][1] - name_cells[0][1][0] == 42
    assert app.table_state["widths"].get("recent", {}) == {}


def test_dropped_recent_tags_remain_visible_without_changing_aligned_widths(dashboard):
    app, views = dashboard
    app.store.tags = {"999": {"tags": ["cohort"]}}
    rows, hits = views.jobs_tab(app.store.snapshot(), app, None, 90, 40)
    recent_y = next(y for y, kind, jid in hits if kind == "recent" and jid == "999")
    assert "cohort" in L.row_text(rows[recent_y])
    names = [value[2:] for _, kind, value in hits if kind == "sort_header" and value[1] == "name"]
    assert names[0] == names[1]
    app.table_state["hidden"]["jobs"] = ["tags"]
    rows, hits = views.jobs_tab(app.store.snapshot(), app, None, 90, 40)
    recent_y = next(y for y, kind, jid in hits if kind == "recent" and jid == "999")
    assert "cohort" not in L.row_text(rows[recent_y])


def test_recent_grip_uses_canonical_jobs_field(simple):
    app, rows, _ = simple
    C.publish(app, rows, [(4, "sort_header", ("recent", "elapsed", 11, 31)),
                          (4, "sort_header", ("recent", "cpus", 33, 36))], 100, 20)
    C.handle_mouse(app, 4, 31, "press")
    C.handle_mouse(app, 4, 38, "release")
    assert app.table_state["widths"]["jobs"]["time"] == 27
    assert "recent" not in app.table_state["widths"]


def test_final_fitted_column_retains_one_cell_resize_cap(simple):
    app, rows, _ = simple
    C.publish(app, rows, [(4, "sort_header", ("jobs", "name", 11, 99))], 100, 20)
    grip, = C.initialize(app)["grips"]
    assert (grip.left, grip.right) == (98, 99)
    assert C.handle_mouse(app, 4, 98, "press")
    assert C.handle_mouse(app, 4, 58, "release")
    assert app.table_state["widths"]["jobs"]["name"] == 48


def test_column_origin_changes_cancel_a_stale_capture(simple):
    app, rows, hits = simple
    C.handle_mouse(app, 4, 31, "press")
    C.handle_mouse(app, 4, 46, "motion")
    shifted = [(y, kind, (table, key, left + 1, right + 1)) for y, kind, (table, key, left, right) in hits]
    C.publish(app, rows, shifted, app.width, app.height)
    assert not C.active(app) and "name" not in app.table_state["widths"]["jobs"]


def test_real_pointer_resize_updates_both_panes_and_never_sorts_or_switches(dashboard, monkeypatch):
    app, views = dashboard
    saved = []
    monkeypatch.setattr(app, "save", lambda: saved.append(True))
    rows, hits = views.compose(app.store.snapshot(), app, 180, 40)
    grip = next(grip for grip in C.initialize(app)["grips"] if grip.table == "jobs" and grip.column == "name")
    original = table_sort.chain(app, "jobs")
    app.click(grip.y, grip.left, hits, button="press")
    assert C.active(app)
    for delta in (2, 12, 25, 10):
        app.click(2, grip.left + delta, app.last_hits, button="drag")
        rows, hits = views.compose(app.store.snapshot(), app, 180, 40)
        assert C.active(app)
        assert app.tab == "jobs" and table_sort.chain(app, "jobs") == original
    app.click(1, grip.left + 10, hits, button="release")
    assert saved == [True]
    assert app.table_state["widths"]["jobs"]["name"] == grip.width + 10
    rows, hits = views.compose(app.store.snapshot(), app, 180, 40)
    positions = [value[2:] for _, kind, value in hits if kind == "sort_header" and value[1] == "name"]
    assert len(positions) == 2 and positions[0] == positions[1]
    assert all(L.vlen(L.row_text(row)) <= 180 for row in rows)


@pytest.mark.parametrize("extreme", [-10000, 10000])
def test_real_pointer_resize_can_reverse_from_both_clamps(dashboard, extreme):
    app, views = dashboard
    _, hits = views.compose(app.store.snapshot(), app, 180, 40)
    grip = next(grip for grip in C.initialize(app)["grips"] if grip.column == "name")
    app.click(grip.y, grip.left, hits, button="press")
    app.click(grip.y, extreme, hits, button="motion")
    rows, hits = views.compose(app.store.snapshot(), app, 180, 40)
    assert C.active(app)
    assert all(L.vlen(L.row_text(row)) <= 180 for row in rows)
    app.click(grip.y, grip.left + 3, hits, button="drag")
    _, hits = views.compose(app.store.snapshot(), app, 180, 40)
    assert C.active(app)
    app.click(grip.y, grip.left + 3, hits, button="release")
    assert app.table_state["widths"]["jobs"]["name"] == grip.width + 3


@pytest.mark.parametrize("command, field, value", [
    ("columns recent width elapsed 24", "widths", {"time": 24}),
    ("columns recent order name,id,state", "order", ["name", "id", "st"]),
    ("columns recent hide ce", "hidden", ["eff"]),
])
def test_recent_column_commands_configure_the_shared_queue_layout(dashboard, command, field, value):
    app, _ = dashboard
    app.run_command(command)
    assert app.table_state[field]["jobs"] == value
    assert "recent" not in app.table_state[field]


def test_recent_only_columns_report_unavailable_instead_of_saving_unused_values(dashboard):
    app, _ = dashboard
    app.run_command("columns recent width rss 20")
    assert "History" in app.message
    assert app.table_state["widths"] == {}


@pytest.mark.parametrize("width", [20, 40, 80, 160])
def test_group_controls_precede_table_and_keep_jump_header_and_rows_separate(dashboard, width):
    app, views = dashboard
    rows, hits = views.jobs_tab(app.store.snapshot(), app, None, width, 40)
    buttons = [(y, value) for y, kind, value in hits if kind == "control" and value.get("group") == "grouping-toolbar"]
    assert [value["id"] for _, value in buttons] == ["grouping:auto", "grouping:undo", "grouping:clear", "grouping:expand"]
    heading = min(y for y, kind, _ in hits if kind == "sort_header")
    assert max(y for y, _ in buttons) == heading - 2
    assert all(y >= heading + 1 for y, kind, _ in hits if kind == "job")
    assert len(rows) <= 40
    assert all(0 <= value["left"] < value["right"] <= width for _, value in buttons)


@pytest.mark.parametrize("table", ["jobs", "history", "group", "nodes", "cluster", "sources"])
def test_all_primary_table_headers_support_width_capture(simple, table):
    app, rows, _ = simple
    app.tab = table
    C.publish(app, rows, [(4, "sort_header", (table, "name", 11, 31))], 100, 20)
    C.handle_mouse(app, 4, 31, "press")
    C.handle_mouse(app, 4, 36, "release")
    assert app.table_state["widths"][table]["name"] == 25


def test_auto_group_preference_persists_and_invalidates_table_fingerprint(simple):
    app, _, _ = simple
    original = table_ui.fingerprint(app, "jobs")
    app.table_state["auto_groups"] = False
    assert table_ui.fingerprint(app, "jobs") != original
    saved = table_ui.save(app)
    table_ui.initialize(app)
    table_ui.restore(app, saved)
    assert app.table_state["auto_groups"] is False
