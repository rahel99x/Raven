"""Column headers order complete observations without changing job identities."""
from __future__ import annotations

import pytest

from raven import layout as L, table_sort
from raven.config import Config
from raven.controller import App
from raven.model import Finished, Health, Job, Live, Node, Partition, Store
from raven.views import JOB_COLS, Views


@pytest.fixture
def dashboard():
    cfg = Config({"log_lines": 0, "clipboard": {"osc52": False, "tools": False}})
    store = Store(persist=False)
    app = App(store, None, None, cfg, "reader", interactive=False)
    views = Views(L.Glyphs(False), cfg)
    app.views_ref = views
    return store, app, views


def _headers(hits, table):
    return [(y, payload) for y, kind, payload in hits
            if kind == "sort_header" and payload[0] == table]


def _body_ids(hits, kind):
    return [key for _, actual, key in hits if actual == kind]


def test_job_cascade_orders_all_rows_before_paging_and_keeps_raw_ids(dashboard):
    store, app, views = dashboard
    store.jobs = [Job("20", "beta", "main", "RUNNING", cpus=2),
                  Job("100", "alpha", "main", "RUNNING", cpus=2),
                  Job("3_10", "alpha", "main", "RUNNING", cpus=8),
                  Job("3_2", "alpha", "main", "RUNNING", cpus=8),
                  Job("2", "alpha", "main", "RUNNING", cpus=2)]
    for key, direction in (("name", "asc"), ("cpus", "desc"), ("id", "asc")):
        table_sort.set_sort(app, "jobs", key, direction)
    records = views.job_rows(store.snapshot(), app)
    # Sort Min places the one-job batches before the two-task array. The
    # column cascade orders equal-size batches and members within the array.
    assert [r["id"] for r in records] == ["2", "100", "20", "3_2", "3_10"]
    rows, hits = views.jobs_tab(store.snapshot(), app, None, 160, 5)
    assert app.visible_ids == ["2", "100", "20", "3_2", "3_10"]
    assert _body_ids(hits, "job") == app.visible_ids[:len(_body_ids(hits, "job"))]
    assert "2" in L.row_text(rows[next(y for y, kind, _ in hits if kind == "job")])


def test_removing_column_restores_its_original_ties_and_preserves_other_sorts(dashboard):
    store, app, views = dashboard
    store.jobs = [Job("10", "same", "main", "RUNNING", cpus=2),
                  Job("2", "same", "main", "RUNNING", cpus=8),
                  Job("20", "same", "main", "RUNNING", cpus=4)]
    table_sort.set_sort(app, "jobs", "name", "asc")
    table_sort.set_sort(app, "jobs", "cpus", "desc")
    assert [r["id"] for r in views.job_rows(store.snapshot(), app)] == ["2", "20", "10"]
    table_sort.set_sort(app, "jobs", "cpus", "off")
    assert [r["id"] for r in views.job_rows(store.snapshot(), app)] == ["10", "2", "20"]
    assert table_sort.chain(app, "jobs") == [("name", "asc")]
    table_sort.set_sort(app, "jobs", "name", "off")
    assert [r["id"] for r in views.job_rows(store.snapshot(), app)] == ["10", "2", "20"]


def test_job_measurements_sort_before_rounding_and_unknowns_follow_real_zero(dashboard):
    store, app, views = dashboard
    store.jobs = [Job("1", "unknown", "main", "RUNNING", cpus=1, mem_req="1G"),
                  Job("2", "larger", "main", "RUNNING", cpus=1, mem_req="1G"),
                  Job("3", "smaller", "main", "RUNNING", cpus=1, mem_req="1G"),
                  Job("4", "zero", "main", "RUNNING", cpus=1, mem_req="1G")]
    store.live = {"2": Live(rate=.5004, avg=.5004, rss=104),
                  "3": Live(rate=.5003, avg=.5003, rss=103),
                  "4": Live(rate=0, avg=0, rss=0)}
    table_sort.set_sort(app, "jobs", "cpu%", "asc")
    result = views.job_rows(store.snapshot(), app)
    assert [r["id"] for r in result] == ["4", "3", "2", "1"]
    assert result[1]["cpu%"] == result[2]["cpu%"] == "50"
    table_sort.set_sort(app, "jobs", "cpu%", "desc")
    assert [r["id"] for r in views.job_rows(store.snapshot(), app)] == ["2", "3", "4", "1"]
    table_sort.set_sort(app, "jobs", "cpu%", "off")
    table_sort.set_sort(app, "jobs", "mem%", "asc")
    assert [r["id"] for r in views.job_rows(store.snapshot(), app)] == ["4", "3", "2", "1"]


def test_pinned_rows_remain_first_without_polluting_jobid_sort(dashboard):
    store, app, views = dashboard
    store.jobs = [Job("100", "third", "main", "RUNNING"),
                  Job("10", "second", "main", "RUNNING"),
                  Job("2", "first", "main", "RUNNING")]
    store.tags = {"100": {"pinned": True}}
    table_sort.set_sort(app, "jobs", "id", "asc")
    result = views.job_rows(store.snapshot(), app)
    assert [r["id"] for r in result] == ["100", "2", "10"]
    assert all(r["id"] == r["job"].id for r in result)


@pytest.mark.parametrize("ascii_", [False, True])
@pytest.mark.parametrize("width", [20, 40, 80, 160, 340])
def test_visible_jobs_headers_have_exact_nonoverlapping_bounds(dashboard, ascii_, width):
    store, app, views = dashboard
    views.set_ascii(ascii_)
    store.jobs = [Job("12", "界界", "main", "RUNNING", cpus=2)]
    table_sort.set_sort(app, "jobs", "id", "asc")
    rows, hits = views.jobs_tab(store.snapshot(), app, None, width, 10)
    headers = _headers(hits, "jobs")
    assert headers and any(payload[1] == "id" for _, payload in headers)
    assert all(0 <= left < right <= width for _, (_, _, left, right) in headers)
    assert all(right <= other_left for (_, (_, _, _, right)), (_, (_, _, other_left, _))
               in zip(headers, headers[1:]))
    first_job_row = next(y for y, kind, _ in hits if kind == "job")
    assert all(y == first_job_row - 1 for y, _ in headers)
    assert L.vlen(L.row_text(rows[first_job_row - 1])) <= width
    assert all(L.vlen(L.row_text(rows[y])) <= width for y, kind, _ in hits if kind == "job")
    if width == 340:
        assert {payload[1] for _, payload in headers} == {column.key for column in JOB_COLS}
    first = headers[0][1]
    assert first[2] >= 1  # The selection/mark gutter is not a sortable header.


def test_group_cascade_orders_full_source_before_viewport_and_includes_jobid(dashboard):
    store, app, views = dashboard
    app.tab = "group"
    store.group = [Job("100", "z", "main", "RUNNING", user="same", cpus=2),
                   Job("10", "a", "main", "RUNNING", user="same", cpus=4),
                   Job("2", "b", "main", "RUNNING", user="same", cpus=4),
                   Job("1", "first", "main", "RUNNING", user="other", cpus=8)]
    table_sort.set_sort(app, "group", "user", "asc")
    table_sort.set_sort(app, "group", "cpus", "desc")
    table_sort.set_sort(app, "group", "id", "asc")
    rows, hits = views.group_tab(store.snapshot(), app, 180, 10)
    assert app.group_ids == ["1", "2", "10", "100"]
    assert _body_ids(hits, "group")[0] == "1"
    assert any(payload[1] == "id" for _, payload in _headers(hits, "group"))
    assert "sorted by user asc, then cpus desc, then id asc" in L.to_text(rows, 180)


def test_sources_sort_raw_latency_and_publish_display_order_for_toggle(dashboard):
    store, app, views = dashboard
    app.tab = "sources"
    store.health = {"z": Health("z", calls=1, latency_ms=.49),
                    "a": Health("a", calls=1, latency_ms=.40),
                    "zero": Health("zero", calls=1, latency_ms=0),
                    "unknown": Health("unknown")}
    table_sort.set_sort(app, "sources", "latency", "asc")
    _, hits = views.sources_tab(store.snapshot(), app, 160, 20)
    assert app.source_ids == ["zero", "a", "z", "unknown"]
    assert _body_ids(hits, "source") == app.source_ids
    assert {payload[1] for _, payload in _headers(hits, "sources")} == {
        "name", "state", "every", "last", "latency", "calls", "errors", "backoff", "error"}
    table_sort.set_sort(app, "sources", "latency", "off")
    views.sources_tab(store.snapshot(), app, 160, 20)
    assert app.source_ids == ["z", "a", "zero", "unknown"]


def test_nodes_sort_unrounded_measurements_and_preserve_zero_unknown_distinction(dashboard):
    store, app, views = dashboard
    app.tab = "nodes"
    store.nodes = {"unknown": Node("unknown", cpus=8, mem_total=1024),
                   "larger": Node("larger", cpus=8, load=.49, mem_total=1024, mem_free=0),
                   "smaller": Node("smaller", cpus=8, load=.40, mem_total=1024, mem_free=512),
                   "zero": Node("zero", cpus=8, load=0, mem_total=1024, mem_free=1024)}
    table_sort.set_sort(app, "nodes", "load", "asc")
    rows, hits = views.my_nodes(store.snapshot(), app, 180, 18)
    names = [name for row in rows for name in store.nodes if name in L.row_text(row)]
    assert names == ["zero", "smaller", "larger", "unknown"]
    assert any(payload[1] == "load" for _, payload in _headers(hits, "nodes"))
    table_sort.set_sort(app, "nodes", "load", "off")
    table_sort.set_sort(app, "nodes", "mem", "desc")
    rows, _ = views.my_nodes(store.snapshot(), app, 180, 18)
    names = [name for row in rows for name in store.nodes if name in L.row_text(row)]
    assert names == ["larger", "smaller", "zero", "unknown"]


def test_cluster_numeric_duration_sort_handles_unlimited_and_unknown(dashboard):
    store, app, views = dashboard
    store.partitions = [Partition("long", limit="2-00:00:00", nodes=100),
                        Partition("short", limit="12:00:00", nodes=2),
                        Partition("unlimited", limit="UNLIMITED", nodes=10),
                        Partition("unknown", limit="Unknown", nodes=0)]
    app.cfg.data["partitions"] = [part.name for part in store.partitions]
    table_sort.set_sort(app, "cluster", "limit", "desc")
    rows, hits = views.cluster_tab(store.snapshot(), app, 180, 20)
    names = [name for row in rows for name in ("long", "short", "unlimited", "unknown")
             if name in L.row_text(row)]
    assert names == ["unlimited", "long", "short", "unknown"]
    assert any(payload[1] == "limit" for _, payload in _headers(hits, "cluster"))


def test_mouse_jobid_header_cycles_ascending_descending_and_source_order(dashboard):
    store, app, views = dashboard
    store.jobs = [Job("10", "a", "main", "RUNNING"),
                  Job("2", "b", "main", "RUNNING"),
                  Job("100", "c", "main", "RUNNING")]
    for expected in (["2", "10", "100"], ["100", "10", "2"], ["10", "2", "100"]):
        rows, hits = views.jobs_tab(store.snapshot(), app, None, 140, 12)
        y, (_, _, left, _) = next(item for item in _headers(hits, "jobs") if item[1][1] == "id")
        app.click(y, left, hits)
        assert [r["id"] for r in views.job_rows(store.snapshot(), app)] == expected


@pytest.mark.parametrize("tab", ["jobs", "history", "group", "nodes", "sources", "cluster"])
@pytest.mark.parametrize("width,height", [(40, 20), (100, 30), (160, 40)])
def test_composed_table_headers_remain_clickable_after_panel_layout(dashboard, tab, width, height):
    store, app, views = dashboard
    store.jobs = [Job("10", "界 alpha", "main", "RUNNING", cpus=2, hosts=["node10"]),
                  Job("2", "beta", "main", "RUNNING", cpus=4, hosts=["node2"])]
    store.finished = [Finished("100", "previous", "COMPLETED", cpus=2),
                      Finished("20", "failed", "FAILED", cpus=4)]
    store.group = list(store.jobs)
    store.nodes = {"node10": Node("node10", cpus=2, load=1),
                   "node2": Node("node2", cpus=4, load=2)}
    store.partitions = [Partition("main", nodes=10)]
    store.health = {"jobs": Health("jobs", calls=2, last_ok=1),
                    "live": Health("live", calls=1, last_ok=1)}
    app.tab = tab
    rows, hits = views.compose(store.snapshot(), app, width, height)
    headers = _headers(hits, tab)
    assert headers
    for y, (_, _, left, right) in headers:
        assert 0 <= y < height - 1
        assert 0 <= left < right <= width
        assert L.vlen(L.row_text(rows[y])) <= width
    column = "id" if tab in ("jobs", "history", "group") else "name"
    y, (_, _, left, right) = next(item for item in headers if item[1][1] == column)
    app.click(y, (left + right - 1) // 2, hits)
    assert table_sort.chain(app, tab) == [(column, "asc")]
    rows, _ = views.compose(store.snapshot(), app, width, height)
    assert len(rows) == height and all(L.vlen(L.row_text(row)) <= width for row in rows)


def test_recent_headers_and_sorting_are_independent_from_history(dashboard):
    store, app, views = dashboard
    store.jobs = [Job("1", "active", "main", "RUNNING")]
    store.finished = [Finished("100", "old", "FAILED"), Finished("2", "new", "COMPLETED")]
    table_sort.set_sort(app, "history", "id", "desc")
    rows, hits = views.compose(store.snapshot(), app, 160, 40)
    headers = _headers(hits, "recent")
    assert headers
    y, (_, _, left, right) = next(item for item in headers if item[1][1] == "id")
    app.click(y, (left + right - 1) // 2, hits)
    assert table_sort.chain(app, "recent") == [("id", "asc")]
    assert table_sort.chain(app, "history") == [("id", "desc")]
    assert [record.id for record in app.recent_jobs(store.snapshot())] == ["2", "100"]


def test_source_selection_survives_live_reordering_and_toggle_uses_same_source(dashboard):
    store, app, views = dashboard
    app.tab = "sources"
    store.health = {"a": Health("a", calls=1), "b": Health("b", calls=10)}
    table_sort.set_sort(app, "sources", "calls", "asc")
    views.compose(store.snapshot(), app, 160, 32)
    app.handle("down")
    views.compose(store.snapshot(), app, 160, 32)
    assert app.source_ids[app.cursor["sources"]] == "b"
    store.health["b"].calls = 0
    views.compose(store.snapshot(), app, 160, 32)
    assert app.source_ids == ["b", "a"]
    assert app.cursor["sources"] == 0
    app.handle("x")
    assert not store.health["b"].enabled and store.health["a"].enabled


def test_group_selection_survives_live_reordering_and_log_uses_same_job(dashboard, tmp_path):
    store, app, views = dashboard
    app.tab = "group"
    store.group = [Job("1", "one", "main", "RUNNING", cpus=1),
                   Job("2", "two", "main", "RUNNING", cpus=10)]
    path = tmp_path / "job-two.log"
    path.write_text("the selected second job\n")
    store.details["2"] = {"StdOut": str(path)}
    table_sort.set_sort(app, "group", "cpus", "asc")
    views.compose(store.snapshot(), app, 160, 32)
    app.handle("down")
    views.compose(store.snapshot(), app, 160, 32)
    assert app.selected_id == "2"
    store.group[1].cpus = 0
    views.compose(store.snapshot(), app, 160, 32)
    assert app.group_ids == ["2", "1"]
    assert app.cursor["group"] == 0 and app.selected_id == "2"
    app.handle("l")
    views.compose(store.snapshot(), app, 160, 32)
    assert app.log_job == "2" and app.logs.path == str(path)
    assert "the selected second job" in L.to_text(app.last_rows, 160)


@pytest.mark.parametrize("tab", ["group", "sources"])
def test_intervening_arrow_remains_attached_to_its_job_or_source_on_refresh(dashboard, tab):
    store, app, views = dashboard
    app.tab = tab
    if tab == "group":
        store.group = [Job("1", "one", "main", "RUNNING", cpus=1),
                       Job("2", "two", "main", "RUNNING", cpus=10)]
        table_sort.set_sort(app, tab, "cpus", "asc")
    else:
        store.health = {"a": Health("a", calls=1), "b": Health("b", calls=10)}
        table_sort.set_sort(app, tab, "calls", "asc")
    views.compose(store.snapshot(), app, 160, 32)
    app.handle("down")
    views.compose(store.snapshot(), app, 160, 32)
    app.handle("up")  # Its new target has not been drawn before the refresh.
    if tab == "group":
        store.group[1].cpus = 0
    else:
        store.health["b"].calls = 0
    views.compose(store.snapshot(), app, 160, 32)
    assert app.cursor[tab] == 1
    if tab == "group":
        assert app.selected_id == "1"
        app.handle("l")
        assert app.log_job == "1"
    else:
        assert app.source_ids[app.cursor[tab]] == "a"
        app.handle("x")
        assert not store.health["a"].enabled and store.health["b"].enabled


def test_group_reanchor_does_not_override_an_independently_changed_cursor(dashboard):
    store, app, views = dashboard
    app.tab = "group"
    store.group = [Job("1", "one", "main", "RUNNING", cpus=1),
                   Job("2", "two", "main", "RUNNING", cpus=10)]
    table_sort.set_sort(app, "group", "cpus", "asc")
    views.compose(store.snapshot(), app, 160, 32)
    app.handle("down")
    views.compose(store.snapshot(), app, 160, 32)
    assert app.selected_id == "2"
    app.cursor["group"] = 0  # A restored cursor no longer points at that selection.
    store.group[1].cpus = 0
    views.compose(store.snapshot(), app, 160, 32)
    assert app.cursor["group"] == 0 and app.selected_id == "2"


def test_sources_legacy_reverse_changes_order_and_matches_header(dashboard):
    store, app, views = dashboard
    app.tab = "sources"
    store.health = {"a": Health("a"), "b": Health("b")}
    app.sort["sources"] = "name"
    rows, _ = views.compose(store.snapshot(), app, 160, 32)
    assert app.source_ids == ["a", "b"]
    assert "SOURCE ^" in L.to_text(rows, 160)
    app.handle("S")
    rows, _ = views.compose(store.snapshot(), app, 160, 32)
    assert app.source_ids == ["b", "a"]
    assert "SOURCE v" in L.to_text(rows, 160)
    assert app.source_ids[app.cursor["sources"]] == "a"
