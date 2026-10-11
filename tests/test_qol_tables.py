"""Table quality-of-life controls preserve exact identities and typed values."""
from __future__ import annotations

import copy
from datetime import datetime
import json
from types import SimpleNamespace

import pytest

from tower import clock, layout as L, table_sort, table_tools, table_ui
from tower.config import Config
from tower.controller import App
from tower.model import Finished, Health, Job, Live, Node, Partition, Store
from tower.views import JOB_COLS, Views


@pytest.fixture
def dashboard(tmp_path):
    cfg = Config({"log_lines": 0, "animations": False, "clipboard": {"tools": False, "osc52": False},
                  "partitions": ["main", "gpu"]})
    store = Store(state_dir=str(tmp_path / "state"))
    store.jobs = [Job("10", "small", "main", "RUNNING", cpus=2, mem_req="8G", elapsed="00:15:00", hosts=["n1"], user="alex"),
                  Job("2", "large", "gpu", "RUNNING", cpus=16, gpus=2, mem_req="32G", elapsed="03:00:00", hosts=["n2"], user="sam"),
                  Job("123_10", "waiting", "gpu", "PENDING", cpus=8, mem_req="16G", user="sam")]
    store.live = {"10": Live(rss=0, avg=0), "2": Live(rss=8 * 1024 ** 3, avg=.251)}
    store.group = list(store.jobs)
    store.finished = [Finished("90", "bad", "FAILED", cpus=8, elapsed="01:00:00", req_mem=16 * 1024 ** 3, rss=0,
                               start="2026-10-01T00:00:00", end="2026-10-01T23:59:59", partition="gpu"),
                      Finished("91", "good", "COMPLETED", cpus=16, elapsed="02:00:00", cpu_time=3600, req_mem=32 * 1024 ** 3,
                               end="2026-10-02T00:00:00", partition="main")]
    store.nodes = {"n1": Node("n1", "MIXED", cpus=32, alloc=2, load=0, mem_total=32768, mem_free=32768),
                   "n2": Node("n2", "DRAIN", cpus=64, alloc=16, load=None, mem_total=65536, mem_free=0, gres="gpu:a100:4")}
    store.partitions = [Partition("main", "up", nodes=2), Partition("gpu", "up", nodes=1)]
    store.health = {"nodes": Health("nodes", last_ok=clock.now(), errors=1, error="last observed problem")}
    app = App(store, None, None, cfg, "alex", interactive=False)
    if not hasattr(app, "table_tools_state"):
        table_tools.initialize(app)
    views = Views(L.Glyphs(False), cfg)
    app.views_ref = views
    views.compose(store.snapshot(), app, 160, 40)
    return store, app, views


def command(app, *args):
    app.command_ok = True
    assert table_tools.run_command(app, list(args))
    assert app.command_ok, app.message


@pytest.mark.parametrize("text,field,operator,value", [
    ("cpus>=8", "cpus", ">=", 8), ("gpu!=0", "gpus", "!=", 0),
    ("memory>16GiB", "memory", ">", 16 * 1024 ** 3), ("rss=0B", "rss", "=", 0),
    ("cpu_eff<30%", "cpu_eff", "<", .3), ("mem_eff<=0.5", "mem_eff", "<=", .5),
    ("elapsed>=2h30m", "elapsed", ">=", 9000), ("time=01:02:03", "elapsed", "=", 3723),
    ("nodes==2", "nodes", "=", 2), ("priority>1e3", "priority", ">", 1000),
])
def test_comparisons_parse_units_without_evaluating_code(text, field, operator, value):
    actual = table_tools.parse_rule(text)
    assert actual[:3] == (field, operator, value)
    assert table_tools.validate_numeric([actual]) == [actual]


@pytest.mark.parametrize("text", ["cpus>-1", "cpus>nan", "cpus>inf", "cpus>1e9999", "cpu_eff>2%%",
                                  "memory>hello", "memory>__import__('os')", "unknown>=1", "elapsed>=UNLIMITED",
                                  "cpus", "cpus>=True", "nodes=1;quit", "cpus>" + "1" * 65])
def test_invalid_filters_are_rejected(text):
    with pytest.raises(ValueError):
        table_tools.parse_rule(text)


def test_numeric_filters_use_unrounded_values_zero_and_unknown(dashboard):
    store, app, views = dashboard
    command(app, "where", "jobs", "cpus>=8", "memory>16GiB", "cpu_eff<25.2%")
    assert [row["id"] for row in views.job_rows(store.snapshot(), app)] == ["2"]
    command(app, "where", "jobs", "clear")
    command(app, "where", "jobs", "rss=0B", "cpu_eff=0%")
    assert [row["id"] for row in views.job_rows(store.snapshot(), app)] == ["10"]
    assert not table_ui.matches(app, "jobs", store.jobs[2], store.snapshot())
    command(app, "where", "history", "rss=0B")
    assert table_ui.matches(app, "history", store.finished[0], store.snapshot())


def test_invalid_multifilter_command_is_atomic(dashboard):
    _, app, _ = dashboard
    command(app, "where", "jobs", "cpus>=8")
    before = copy.deepcopy(app.table_tools_state["numeric"])
    table_tools.run_command(app, ["where", "jobs", "memory>16G", "cpus>nan"])
    assert not app.command_ok
    assert app.table_tools_state["numeric"] == before


@pytest.mark.parametrize("table", [key for key in table_sort.TABLE_KEYS if key != "recent"])
def test_columns_all_tables_reorder_resize_hide_restore(dashboard, table):
    _, app, _ = dashboard
    cols = table_ui.definitions(table)
    keys = [column.key for column in cols]
    resized = next(key for key in keys if key != "progress")
    optional = next(column.key for column in cols if column.key not in table_ui.REQUIRED and column.key != resized)
    assert table_ui.run_command(app, ["columns", table, "order", ",".join(reversed(keys))])
    assert table_ui.run_command(app, ["columns", table, "width", resized, "12"])
    assert table_ui.run_command(app, ["columns", table, "hide", optional])
    shown = table_ui.columns(app, table, cols)
    assert [column.key for column in shown] == [key for key in reversed(keys) if key != optional]
    sized = next(column for column in shown if column.key == resized)
    assert sized.lo == sized.hi == 12
    state = json.loads(json.dumps(table_ui.save(app)))
    table_ui.initialize(app)
    table_ui.restore(app, state)
    assert app.table_state["order"][table] == list(reversed(keys))
    assert app.table_state["widths"][table][resized] == 12
    assert app.table_state["hidden"][table] == [optional]


@pytest.mark.parametrize("width", ["1", "121", "-1", "NaN", "True"])
def test_invalid_width_is_atomic(dashboard, width):
    _, app, _ = dashboard
    before = copy.deepcopy(app.table_state)
    table_ui.run_command(app, ["columns", "jobs", "width", "name", width])
    assert not app.command_ok and app.table_state == before


def test_column_overlay_keyboard_order_width_visibility(dashboard):
    _, app, views = dashboard
    table_ui.run_command(app, ["columns", "jobs"])
    table_ui.handle_key(app, "down")  # JOBID follows the leading six-cell PROG.
    table_ui.handle_key(app, "right")
    assert app.table_state["order"]["jobs"][:3] == ["progress", "name", "id"]
    table_ui.handle_key(app, "+")
    assert app.table_state["widths"]["jobs"]["id"] >= 6
    table_ui.handle_key(app, "a")
    assert "id" not in app.table_state["widths"]["jobs"]
    table_ui.handle_key(app, "home")
    table_ui.handle_key(app, "space")
    assert "progress" in app.table_state["hidden"]["jobs"]
    overlay = table_ui.overlay(views, app.store.snapshot(), app, 100, 30)
    assert "width" in "\n".join(L.row_text(row) for _, _, row in overlay)
    table_ui.handle_key(app, "r")
    assert app.table_state["order"]["jobs"] == []
    table_ui.handle_key(app, "enter")
    assert app.mode == "main"


def test_sort_editor_reorders_removes_and_preserves_selected_id(dashboard):
    store, app, views = dashboard
    table_sort.set_sort(app, "jobs", "name", "asc")
    table_sort.set_sort(app, "jobs", "cpus", "desc")
    views.compose(store.snapshot(), app, 160, 40)
    app.cursor["jobs"] = app.visible_ids.index("10")
    app.sync_selection()
    command(app, "sorteditor", "jobs")
    table_tools.handle_key(app, "right")
    assert table_sort.chain(app, "jobs") == [("cpus", "desc"), ("name", "asc")]
    assert app.selected_id == "10"
    table_tools.handle_key(app, "space")
    assert table_sort.chain(app, "jobs")[1] == ("name", "desc")
    table_tools.handle_key(app, "d")
    assert table_sort.chain(app, "jobs") == [("cpus", "desc")]
    table_tools.handle_key(app, "a")
    assert app.table_tools_state["modal"] == "headers"
    table_tools.handle_key(app, "down")  # Skip PROG and add JOBID as the next priority.
    table_tools.handle_key(app, "enter")
    assert table_sort.chain(app, "jobs")[-1] == ("id", "asc")
    table_tools.handle_key(app, "esc")


@pytest.mark.parametrize("table", list(table_sort.TABLE_KEYS))
def test_keyboard_headers_cycle_each_scope(dashboard, table):
    store, app, views = dashboard
    command(app, "headers", table)
    table_tools.overlay(views, store.snapshot(), app, 120, 30)
    column = table_tools.focused_column(app)
    assert column[0] == table
    for expected in ("asc", "desc", None):
        table_tools.handle_key(app, "enter")
        assert dict(table_sort.chain(app, table)).get(column[1]) == expected
    table_tools.handle_key(app, "esc")
    assert app.mode == "main" and table_tools.focused_column(app) is None


def test_interactive_filter_builder_and_mouse_chip_removal(dashboard):
    store, app, views = dashboard
    command(app, "filters", "jobs")
    app.table_tools_state["cursor"] = table_tools.FILTER_FIELDS.index("cpus")
    table_tools.handle_key(app, "enter")
    for key in ">=8":
        table_tools.handle_key(app, key)
    table_tools.handle_key(app, "enter")
    assert table_tools.numeric_rules(app, "jobs")[0][:3] == ("cpus", ">=", 8)
    table_tools.handle_key(app, "esc")
    app.table_state["facets"]["jobs"] = {"partition": "gpu"}
    row = table_ui.chips(app, "jobs", 160)
    assert "partition=gpu" in L.row_text(row)
    app.table_chip_y = 6
    tab, cells = app.table_chip_cells
    key, x0, x1 = cells[0]
    assert table_tools.handle_mouse(app, 6, x0)
    assert app.table_state["facets"]["jobs"] == {}
    assert len(table_tools.numeric_rules(app, "jobs")) == 1
    table_ui.chips(app, "jobs", 160)
    assert table_tools.handle_mouse(app, 6, app.table_chip_cells[1][0][1])
    assert table_tools.numeric_rules(app, "jobs") == []


def test_independent_table_text_filters(dashboard):
    _, app, _ = dashboard
    table_tools.set_filter_text(app, "jobs", "large")
    table_tools.set_filter_text(app, "history", "bad")
    table_tools.set_filter_text(app, "recent", "good")
    assert [table_tools.filter_text(app, table) for table in ("jobs", "history", "recent")] == ["large", "bad", "good"]


def test_saved_picker_loads_columns_filters_and_sorts(dashboard):
    store, app, views = dashboard
    table_tools.set_filter_text(app, "jobs", "large")
    table_ui.run_command(app, ["columns", "jobs", "width", "name", "12"])
    table_ui.run_command(app, ["columns", "jobs", "order", "id,cpus,name"])
    command(app, "where", "jobs", "cpus>=8")
    table_ui.run_command(app, ["savedview", "jobs", "save", "GPU view"])
    table_tools.set_filter_text(app, "jobs", "")
    command(app, "where", "jobs", "clear")
    command(app, "viewpicker", "history")
    overlay = table_tools.overlay(views, store.snapshot(), app, 160, 30)
    assert "GPU view" in "\n".join(L.row_text(row) for _, _, row in overlay)
    table_tools.handle_key(app, "enter")
    assert app.tab == "jobs"
    assert table_tools.filter_text(app, "jobs") == "large"
    assert app.table_state["widths"]["jobs"]["name"] == 12
    assert table_tools.numeric_rules(app, "jobs")[0][:3] == ("cpus", ">=", 8)


@pytest.mark.parametrize("args", [["2026-10-02", "2026-10-01"], ["2026-02-30", "2026-03-01"], ["bad"], ["2026-10-01", "2026-10-02", "extra"]])
def test_invalid_history_range_is_atomic(dashboard, args):
    _, app, _ = dashboard
    before = copy.deepcopy(app.table_tools_state)
    table_tools.run_command(app, ["historyrange"] + args)
    assert not app.command_ok
    assert app.table_tools_state == before


def test_history_dates_are_inclusive_and_upper_bound_exclusive(dashboard, monkeypatch):
    store, app, _ = dashboard
    monkeypatch.setattr(clock, "now", lambda: datetime(2026, 10, 5, 12).timestamp())
    command(app, "historyrange", "2026-10-01", "2026-10-01")
    assert table_tools.history_matches(app, store.finished[0])
    assert not table_tools.history_matches(app, store.finished[1])
    command(app, "historyrange", "all")
    assert all(table_tools.history_matches(app, item) for item in store.finished)
    command(app, "historyrange", "today")
    assert "2026-10-05" in table_tools.date_label(app)
    command(app, "historyrange", "yesterday")
    assert "2026-10-04" in table_tools.date_label(app)


def test_recents_limits_window_and_temporary_expansion(dashboard, monkeypatch):
    store, app, _ = dashboard
    monkeypatch.setattr(clock, "now", lambda: datetime(2026, 10, 2, 1).timestamp())
    command(app, "recents", "10")
    assert table_tools.recent_limit(app) == 10
    command(app, "recents", "window", "2h")
    assert table_tools.recent_matches(app, store.finished[1])
    assert table_tools.recent_matches(app, store.finished[0])
    command(app, "recents", "window", "30m")
    assert not table_tools.recent_matches(app, store.finished[0])
    assert table_tools.recent_matches(app, store.jobs[0])
    command(app, "recents", "expand")
    assert table_tools.recent_limit(app) == 25
    command(app, "recents", "collapse")
    assert table_tools.recent_limit(app) == 10
    before = copy.deepcopy(app.table_tools_state["recents"])
    table_tools.run_command(app, ["recents", "window", "inf"])
    assert not app.command_ok and app.table_tools_state["recents"] == before


def test_viewport_paging_has_one_context_row(dashboard):
    _, app, _ = dashboard
    table_tools.record_page(app, "jobs", 7)
    table_tools.record_page(app, "history", 18)
    assert table_tools.page_size(app, "jobs") == 6
    assert table_tools.page_size(app, "history") == 17
    table_tools.record_page(app, "jobs", 1)
    assert table_tools.page_size(app, "jobs") == 1


def test_marked_manager_sees_hidden_missing_and_finished_ids(dashboard):
    store, app, _ = dashboard
    app.marks = {"10", "2", "90", "missing"}
    app.visible_ids = ["10"]
    app.recent_ids = []
    command(app, "marked", "hidden")
    rows = table_tools.marked_rows(app, store.snapshot())
    assert {row[0] for row in rows} == {"2", "90", "missing"}
    table_tools.handle_key(app, "c")
    assert app.marks == {"10"}
    table_tools.handle_key(app, "esc")
    app.marks.add("90")
    command(app, "marked", "finished")
    table_tools.handle_key(app, "enter")
    assert app.detail_id == "90" and app.mode == "details"


def test_freeze_is_immutable_session_only_and_resumes_accumulated_changes(dashboard):
    store, app, _ = dashboard
    command(app, "freeze", "on")
    frozen = table_tools.snapshot(app, store.snapshot())
    store.jobs[0].name = "renamed after sample"
    store.jobs = [store.jobs[1]]
    store.live["10"].rss = 100
    assert frozen["jobs"][0].name == "small"
    assert frozen["live"]["10"].rss == 0
    assert "2 queue changes" in table_tools.freeze_status(app, store.snapshot())
    assert "freeze" not in table_tools.save(app)
    command(app, "freeze", "off")
    assert table_tools.snapshot(app, store.snapshot())["jobs"] == store.jobs
    assert "2 queue changes accumulated" in app.message


def test_job_action_menu_keeps_exact_accounting_id(dashboard, monkeypatch):
    _, app, _ = dashboard
    copied = []
    monkeypatch.setattr("tower.clipboard.copy", lambda text, *args, **kwargs: copied.append(text) or "copied")
    command(app, "jobactions", "90")
    app.table_tools_state["cursor"] = 2
    table_tools.handle_key(app, "enter")
    assert copied == ["90"]
    command(app, "jobactions", "91")
    app.table_tools_state["cursor"] = 1
    table_tools.handle_key(app, "enter")
    assert app.detail_id == "91" and app.mode == "details"


@pytest.mark.parametrize("ascii_", [False, True])
@pytest.mark.parametrize("dimensions", [(20, 8), (80, 24), (160, 50)])
@pytest.mark.parametrize("modal", ["sorteditor", "headers", "filters", "viewpicker", "marked", "jobactions", "node"])
def test_overlays_fit_terminal_and_mouse_uses_real_placement(dashboard, ascii_, dimensions, modal):
    store, app, views = dashboard
    views.set_ascii(ascii_)
    width, height = dimensions
    command(app, modal, *({"jobactions": ["90"], "node": ["n2"]}.get(modal, [])))
    placements = table_tools.overlay(views, store.snapshot(), app, width, height)
    assert all(0 <= y < height and 0 <= x < width and x + L.vlen(L.row_text(row)) <= width for y, x, row in placements)
    assert table_tools.handle_mouse(app, 0, 0)
    assert app.mode == "table_tools"


def test_nodes_and_partition_rows_have_real_drill_targets(dashboard):
    store, app, views = dashboard
    app.tab = "nodes"
    rows, hits = views.my_nodes(store.snapshot(), app, 160, 40)
    y, kind, name = next(hit for hit in hits if hit[1] == "node_row")
    assert name in L.row_text(rows[y])
    assert table_tools.handle_click_hit(app, y, 4, hits)
    assert app.table_tools_state["node"] == name
    overlay = table_tools.overlay(views, store.snapshot(), app, 160, 40)
    text = "\n".join(L.row_text(row) for _, _, row in overlay)
    assert name in text and "Measurement age" in text
    table_tools.handle_key(app, "esc")
    app.tab = "cluster"
    rows, hits = views.cluster_tab(store.snapshot(), app, 160, 40)
    y, _, name = next(hit for hit in hits if hit[1] == "partition_row")
    assert table_tools.handle_click_hit(app, y, 4, hits)
    assert app.tab == "jobs" and app.table_state["facets"]["jobs"]["partition"] == name


def test_group_user_summary_drills_to_exact_user_and_back_restores(dashboard):
    store, app, views = dashboard
    app.tab = "group"
    rows, hits = views.group_tab(store.snapshot(), app, 160, 40)
    y, _, user = next(hit for hit in hits if hit[1] == "user_drill")
    assert user in L.row_text(rows[y])
    assert table_tools.handle_click_hit(app, y, 4, hits)
    assert app.table_state["facets"]["group"]["user"] == user
    _, hits = views.group_tab(store.snapshot(), app, 160, 40)
    assert {jid for _, kind, jid in hits if kind == "group"} == {job.id for job in store.group if job.user == user}


def test_commands_keep_independent_filters_after_restart(dashboard):
    store, app, views = dashboard
    app.run_command("filter large")
    app.enter_tab("history")
    app.run_command("filter bad")
    assert app.filter == "bad"
    app.enter_tab("jobs")
    assert app.filter == "large"
    app.save()
    restored = App(store, None, None, app.cfg, "alex", interactive=False)
    assert table_tools.filter_text(restored, "jobs") == "large"
    assert table_tools.filter_text(restored, "history") == "bad"


def test_recents_real_controller_limit_filter_and_history_range(dashboard, monkeypatch):
    from tower import recent_history
    store, app, views = dashboard
    store.finished.extend(Finished(str(100 + index), f"older-{index}", "COMPLETED", end="2026-10-01T12:00:00") for index in range(30))
    app.run_command("recents 10")
    # The count is a preview minimum. Native Jobs layout admits the complete
    # visible Recents page, preserving accounting order before its own sort.
    count = max(10, recent_history.initialize(app).page)
    expected = [record.id for record in store.finished[:count]]
    assert [record.id for record in app.recent_jobs(store.snapshot())] == expected
    assert len(expected) == count
    app.filter = "no active job"
    assert [record.id for record in app.recent_jobs(store.snapshot())] == expected
    table_tools.set_filter_text(app, "recent", "older-29")
    assert [record.id for record in app.recent_jobs(store.snapshot())] == ["129"]
    monkeypatch.setattr(clock, "now", lambda: datetime(2026, 10, 5, 12).timestamp())
    app.run_command("historyrange 2026-10-02 2026-10-02")
    app.enter_tab("history")
    assert [record.id for record in app.history_jobs()] == ["91"]


def test_actual_chip_click_removes_only_drawn_rule(dashboard):
    store, app, views = dashboard
    app.run_command("where jobs cpus>=8 memory>16GiB")
    app.table_state["facets"]["jobs"] = {"partition": "gpu"}
    rows, hits = views.compose(store.snapshot(), app, 160, 40)
    first = next(cell for cell in app.table_chip_cells[1] if cell[0] == "numeric:0")
    assert "cpus>=8" in L.row_text(rows[app.table_chip_y])[first[1]:first[2]]
    app.click(app.table_chip_y, first[1], hits)
    assert [rule[0] for rule in table_tools.numeric_rules(app, "jobs")] == ["memory"]
    # Repeated input against the same displayed chip never removes another rule.
    assert table_tools.handle_mouse(app, app.table_chip_y, first[1])
    assert [rule[0] for rule in table_tools.numeric_rules(app, "jobs")] == ["memory"]
    assert app.table_state["facets"]["jobs"] == {"partition": "gpu"}


def test_source_filter_end_and_immediate_toggle_never_targets_hidden_source(dashboard):
    store, app, views = dashboard
    store.health.update({"jobs": Health("jobs"), "finished": Health("finished")})
    app.enter_tab("sources")
    app.run_command("filter nodes")
    views.compose(store.snapshot(), app, 160, 40)
    assert app.source_ids == ["nodes"]
    before = {name: health.enabled for name, health in store.health.items()}
    app.handle("end")
    app.handle_action("source_toggle")
    assert store.health["nodes"].enabled is not before["nodes"]
    assert all(store.health[name].enabled == before[name] for name in ("jobs", "finished"))


def test_user_drill_is_exact_and_back_restores_filters(dashboard):
    from tower.navigation_ui import back
    store, app, views = dashboard
    store.group.append(Job("999", "similar username", "main", "RUNNING", user="samuel"))
    app.enter_tab("group")
    views.compose(store.snapshot(), app, 160, 40)
    app.run_command("drill user sam")
    _, hits = views.group_tab(store.snapshot(), app, 160, 40)
    assert {jid for _, kind, jid in hits if kind == "group"} == {"2", "123_10"}
    assert back(app)
    assert app.tab == "group"
    assert "user" not in app.table_state["facets"].get("group", {})


@pytest.mark.parametrize("table", ["nodes", "cluster"])
def test_drill_buttons_keep_panel_keyboard_scrolling(dashboard, table):
    store, app, views = dashboard
    if table == "nodes":
        store.nodes = {f"n{index:03}": Node(f"n{index:03}", "MIXED", cpus=32, alloc=2) for index in range(90)}
    else:
        store.partitions = [Partition(f"p{index:03}", "up", nodes=1) for index in range(90)]
        app.cfg.data["partitions"] = [partition.name for partition in store.partitions]
    app.enter_tab(table)
    app.run_command("density comfortable")
    rows, hits = views.compose(store.snapshot(), app, 100, 24)
    assert not app.layout_state.interactive_panels[table + ":main"]
    app.handle("end")
    assert app.layout_state.scroll[table + ":main"] > 0
    rows, hits = views.compose(store.snapshot(), app, 100, 24)
    assert any(kind == "sort_header" for _, kind, _ in hits)
    assert any(kind == ("node_row" if table == "nodes" else "partition_row") for _, kind, _ in hits)
    app.handle("home")
    assert app.layout_state.scroll[table + ":main"] == 0


def test_group_user_drill_buttons_do_not_displace_sticky_headers(dashboard):
    store, app, views = dashboard
    store.group = [Job(str(index), f"group-{index}", "main", "RUNNING", user=f"user{index % 10}") for index in range(90)]
    app.enter_tab("group")
    app.run_command("density comfortable")
    views.compose(store.snapshot(), app, 160, 30)
    app.handle("end")
    rows, hits = views.compose(store.snapshot(), app, 160, 30)
    assert app.selected_id == app.group_ids[-1]
    headers = [hit for hit in hits if hit[1] == "sort_header"]
    assert headers and any(payload[:2] == ("group", "id") for _, _, payload in headers)
    assert all(L.row_text(rows[y])[payload[2]:payload[3]].strip() for y, _, payload in headers)


@pytest.mark.parametrize("ascii_", [False, True])
@pytest.mark.parametrize("density", ["compact", "comfortable"])
def test_cluster_map_cells_click_exact_node_at_transformed_coordinates(dashboard, ascii_, density):
    from tower.model import NodeCell
    store, app, views = dashboard
    store.nodemap = {name: NodeCell(name, ["main"], "idle", cpus=64) for name in ("map-first", "map-next", "map-third")}
    app.enter_tab("nodes")
    app.nodes_view = "map"
    views.set_ascii(ascii_)
    app.run_command("density " + density)
    rows, hits = views.compose(store.snapshot(), app, 160, 35)
    cells = [hit for hit in hits if hit[1] == "node_cell"]
    assert cells
    y, _, (name, left, right) = cells[-1]
    assert 0 <= left < right <= 160
    assert name[:8] in L.row_text(rows[y])[left:right]
    app.click(y, left, hits)
    assert app.mode == "table_tools" and app.table_tools_state["node"] == name
    assert table_tools.selected_record(app, "nodes", store.snapshot()).name == name


def test_filter_builder_previews_draft_without_mutating_settings(dashboard):
    store, app, views = dashboard
    app.run_command("filters jobs")
    app.table_tools_state["cursor"] = table_tools.FILTER_FIELDS.index("cpus")
    app.handle("enter")
    for key in ">=8":
        app.handle(key)
    text = "\n".join(L.row_text(row) for _, _, row in table_tools.overlay(views, store.snapshot(), app, 160, 35))
    assert "2 cached records match filters (preview" in text
    assert table_tools.numeric_rules(app, "jobs") == []
    app.handle("esc")
    assert table_tools.numeric_rules(app, "jobs") == []


def test_history_range_restores_accounting_lookback_after_restart(dashboard, monkeypatch):
    store, app, _ = dashboard
    monkeypatch.setattr(clock, "now", lambda: datetime(2026, 10, 5, 12).timestamp())
    app.run_command("historyrange 2026-09-01 2026-09-02")
    requested = app.analytics_days_value()
    assert requested > 7
    app.save()
    restored = App(store, None, None, app.cfg, "alex", interactive=False)
    assert restored.analytics_days_value() == requested
    assert "2026-09-01" in table_tools.date_label(restored)


def _action_backend(store):
    from tower.actions import Actions
    calls = []
    def invoke(action):
        def operation(ids):
            calls.append((action, list(ids)))
            return True, "accepted by test backend"
        return operation
    scheduler = SimpleNamespace(**{action: invoke(action) for action in ("cancel", "hold", "release", "requeue", "top")})
    return Actions(scheduler, store), calls


@pytest.mark.parametrize("transition", ["finished", "state", "start", "submit"])
def test_confirmation_revalidates_complete_scope_and_aborts_atomically(dashboard, transition):
    from dataclasses import replace
    store, app, _ = dashboard
    actions, calls = _action_backend(store)
    app.actions = actions
    prior = Job("guarded", "guarded", "main", "PENDING" if transition == "state" else "RUNNING",
                start="" if transition == "state" else "2026-10-01T12:00:00", submit="2026-10-01T11:00:00")
    store.jobs.append(prior)
    app.marks = {"10", "guarded"}
    action = "hold" if transition == "state" else "cancel"
    targets = [prior] if action == "hold" else [store.jobs[0], prior]
    app.confirm = {"action": action, "jobs": targets}
    app.mode = "confirm"
    if transition == "finished":
        store.jobs = [job for job in store.jobs if job.id != prior.id]
    elif transition == "state":
        store.jobs = [replace(job, state="RUNNING") if job.id == prior.id else job for job in store.jobs]
    elif transition == "start":
        store.jobs = [replace(job, start="2026-10-02T12:00:00") if job.id == prior.id else job for job in store.jobs]
    else:
        store.jobs = [replace(job, submit="2026-10-02T11:00:00") if job.id == prior.id else job for job in store.jobs]
    app.handle("y")
    assert not calls
    assert not app.command_ok and "Action aborted" in app.message
    assert app.marks == {"10", "guarded"}
    assert [job.id for job in app.confirm["jobs"]] == [job.id for job in targets]


def test_frozen_action_menu_preserves_action_and_never_acts_on_departed_job(dashboard):
    store, app, _ = dashboard
    actions, calls = _action_backend(store)
    app.actions = actions
    app.run_command("freeze on")
    app.run_command("jobactions 2")
    items = app.table_tools_state["action_items"]
    app.table_tools_state["cursor"] = next(index for index, item in enumerate(items) if item[0] == "cancel")
    store.jobs = [job for job in store.jobs if job.id != "2"]
    app.handle("enter")
    assert calls == []
    assert app.mode == "main" and not app.command_ok
    assert "no such job" in app.message


def test_confirmation_runs_current_records_after_benign_sample_changes(dashboard):
    from dataclasses import replace
    store, app, _ = dashboard
    actions, calls = _action_backend(store)
    app.actions = actions
    prior = store.jobs[0]
    app.confirm = {"action": "cancel", "jobs": [prior]}
    store.jobs[0] = replace(prior, name="new display name", elapsed="00:20:00")
    app.finish_confirm(True)
    assert calls == [("cancel", [prior.id])]
    assert app.command_ok


@pytest.mark.parametrize("table", [[], {}, 42, True, None])
def test_corrupt_saved_view_table_is_rejected_before_navigation(dashboard, table):
    _, app, _ = dashboard
    app.table_state["views"]["bad table"] = {"tab": table}
    before = copy.deepcopy(app.table_state)
    table_ui.run_command(app, ["savedview", "jobs", "load", "bad table"])
    assert not app.command_ok
    assert app.tab == "jobs" and app.table_state == before
