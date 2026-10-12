"""Whole-batch queue ordering, exact Heavy identity, and saved mode selection."""
from types import SimpleNamespace

import pytest

from raven import grouping_toolbar, job_groups, table_ui
from raven.config import Config
from raven.controller import App
from raven.layout import Glyphs
from raven.model import Job, Store
from raven.views import Views


def job(jid, state="RUNNING", **changes):
    values = dict(id=str(jid), name="experiment", partition="local", state=state,
                  user="alex", account="lab", workdir="/project", command="/project/train.sh",
                  submit="2026-10-12T12:00:00", cluster="local")
    values.update(changes)
    return Job(**values)


def app():
    return SimpleNamespace(table_state={"groups": True, "collapsed": []}, tab="jobs",
                           selected_id=None, cursor={"jobs": 0}, last_jobs_ids=[])


def ids(records):
    return [record.id for record in records]


def batches():
    a = [job(1)] + [job(n, "PENDING", dependency="afterok:1") for n in range(2, 9)]
    b = [job(9, name="other experiment")]
    c = [job(20, comment="launch:c"), job(21, comment="launch:c")]
    d = [job(30, "PENDING", comment="launch:d"), job(31, "PENDING", comment="launch:d")]
    # The old state-first projection scattered batch A around another batch.
    return a, b, c, d, {"jobs": [a[0], *b, *c, *d, *a[1:]]}


@pytest.mark.parametrize("sort,expected", [
    ("min", [30, 31, 9, *range(1, 9), 20, 21]),
    ("max", [20, 21, 9, *range(1, 9), 30, 31]),
])
def test_running_and_dependency_batches_are_contiguous_in_both_orders(sort, expected):
    value = app()
    value.table_state["jobs_batch_sort"] = sort
    _, _, _, _, snap = batches()
    projected = job_groups.project_records(value, snap, snap["jobs"], "jobs")
    assert ids(projected) == list(map(str, expected))
    group = job_groups.registry(value).index.for_job("1")
    assert group.members == tuple(map(str, range(1, 9)))
    assert job_groups.registry(value).index.for_job("9") is None
    assert grouping_toolbar.execute(value, "collapse", snap).changed
    collapsed = ids(job_groups.project_records(value, snap, snap["jobs"], "jobs"))
    assert collapsed == (["30", "9", "1", "20"] if sort == "min" else ["20", "9", "1", "30"])
    grouping_toolbar.execute(value, "expand", snap)
    assert ids(job_groups.project_records(value, snap, snap["jobs"], "jobs")) == list(map(str, expected))


def test_prepared_rows_keep_batch_order_and_exact_records():
    value = app()
    _, _, _, _, snap = batches()
    rows = [{"job": record, "id": record.id, "info": "native"} for record in snap["jobs"]]
    result = job_groups.project_rows(value, rows, snap)
    assert [row["id"] for row in result] == ["30", "31", "9", *map(str, range(1, 9)), "20", "21"]
    original = {record.id: record for record in snap["jobs"]}
    assert all(row["job"] is original[row["id"]] for row in result)
    assert all("_group" not in row and row["info"] == "native" for row in rows)


def test_filter_uses_full_queue_counts_without_resurrecting_hidden_jobs():
    value = app()
    a, b, c, _, snap = batches()
    filtered = [a[1], c[0], *b]
    assert ids(job_groups.project_records(value, snap, filtered, "jobs")) == ["9", "2", "20"]
    meta = job_groups.metadata_for_record(value, "jobs", "2")
    assert meta.visible_count == 1 and meta.total_count == 8
    value.table_state["jobs_batch_sort"] = "max"
    assert ids(job_groups.project_records(value, snap, filtered, "jobs")) == ["20", "9", "2"]


def test_state_change_reorders_batches_without_stale_inference_cache():
    value = app()
    a, _, _, _, snap = batches()
    job_groups.project_records(value, snap, snap["jobs"], "jobs")
    before = job_groups.registry(value).inference_count
    for record in a:
        record.state = "RUNNING"
    value.table_state["jobs_batch_sort"] = "max"
    assert ids(job_groups.project_records(value, snap, snap["jobs"], "jobs"))[:8] == list(map(str, range(1, 9)))
    assert job_groups.registry(value).inference_count == before


def test_dependency_tree_keeps_its_edge_order():
    value = app()
    _, _, _, _, snap = batches()
    assert job_groups.project_records(value, snap, snap["jobs"], "deps") == snap["jobs"]


@pytest.mark.parametrize("table", ["history", "group", "analytics:advisor"])
def test_other_job_lists_pack_batches_without_applying_queue_rank_by_default(table):
    value = app()
    a, b, c, d, snap = batches()
    assert job_groups.project_records(value, snap, snap["jobs"], table) == a + b + c + d
    value.table_state["job_list_modes"] = {table: "sequential"}
    assert job_groups.project_records(value, snap, snap["jobs"], table) == snap["jobs"]


def heavy_snapshot(record):
    return {"state": "heavy", "job_id": record.id, "identity": {
        "site_kind": "slurm", "verified": True, "allocation_id": record.id,
        "uid": 1000, "cluster_id": record.cluster, "submit_time": record.submit}}


@pytest.mark.parametrize("sort", ["min", "max"])
def test_verified_heavy_is_separate_and_first_even_if_inferred_or_manually_grouped(sort):
    from raven import manual_job_groups
    value = app()
    value.table_state["jobs_batch_sort"] = sort
    records = [job(n) for n in (100, 101, 102)]
    snap = {"jobs": records, "details": {"102": {"UserId": "alex(1000)"}}}
    manual_job_groups.create(value, snap, ids(records))
    value.raven_ui_state = {"snapshot": heavy_snapshot(records[2])}
    result = job_groups.project_records(value, snap, records, "jobs")
    assert ids(result) == ["102", "100", "101"]
    assert job_groups.metadata_for_record(value, "jobs", "102") is None
    index = job_groups.registry(value).ensure(snap)
    assert index.for_job("102") is None and index.for_job("100").members == ("100", "101")
    grouping_toolbar.execute(value, "collapse", snap)
    assert ids(job_groups.project_records(value, snap, records, "jobs")) == ["102", "100"]


@pytest.mark.parametrize("change", [
    {"state": "light"}, {"job_id": "999"},
    {"identity": {"verified": False}}, {"identity": {"cluster_id": "other"}},
    {"identity": {"submit_time": "2026-10-12T11:00:00"}}, {"identity": {"uid": 2000}},
    {"identity": {"site_kind": "desktop"}},
])
def test_same_name_or_conflicting_worker_identity_never_pins_another_job(change):
    value = app()
    value.table_state["jobs_batch_sort"] = "max"
    regular, impostor = job(1, name="ordinary"), job(99, "PENDING", name="ravenh")
    publication = heavy_snapshot(impostor)
    if "identity" in change:
        publication["identity"].update(change["identity"])
    else:
        publication.update(change)
    value.raven_ui_state = {"snapshot": publication}
    snap = {"jobs": [regular, impostor], "details": {"99": {"UserId": "alex(1000)"}}}
    assert ids(job_groups.project_records(value, snap, snap["jobs"], "jobs")) == ["1", "99"]


def test_singletons_and_disabled_grouping_still_honor_selected_sort():
    value = app()
    value.table_state["groups"] = False
    records = [job(1), job(2, "PENDING")]
    snap = {"jobs": records}
    assert ids(job_groups.project_records(value, snap, records, "jobs")) == ["2", "1"]
    value.table_state["jobs_batch_sort"] = "max"
    assert ids(job_groups.project_records(value, snap, records, "jobs")) == ["1", "2"]


@pytest.mark.parametrize("phase", ["pending", "starting"])
def test_owned_pending_allocation_pins_before_worker_admission(phase):
    value = app()
    value.table_state["jobs_batch_sort"] = "max"
    records = [job(1), job(99, "PENDING")]
    observation = dict(allocation_id="99", source="owned-salloc-stderr", request_id="current", generation=2,
                       cluster_id="local", transport="local", owner_uid=1000)
    value.raven_ui_state = {"snapshot": dict(state=phase, identity={}, job_id="99", generation=2,
        request_id="current", ownership="dedicated-allocation", allocation_observation=observation)}
    snap = {"jobs": records, "details": {"99": {"UserId": "alex(1000)"}}}
    assert ids(job_groups.project_records(value, snap, records, "jobs")) == ["99", "1"]
    assert value.raven_ui_state["snapshot"]["identity"] == {}
    observation["generation"] = 1
    assert ids(job_groups.project_records(value, snap, records, "jobs")) == ["1", "99"]


def test_pending_observation_from_previous_request_cannot_pin():
    value = app()
    value.table_state["jobs_batch_sort"] = "max"
    records = [job(1), job(99, "PENDING")]
    observation = dict(allocation_id="99", source="owned-salloc-stderr", request_id="old", generation=2)
    value.raven_ui_state = {"snapshot": dict(state="pending", identity={}, job_id="99", generation=2,
        request_id="new", ownership="dedicated-allocation", allocation_observation=observation)}
    assert ids(job_groups.project_records(value, {"jobs": records}, records, "jobs")) == ["1", "99"]


@pytest.mark.parametrize("state,start", [("RUNNING", "Unknown"), ("RUNNING", "N/A"),
                                          ("PENDING", "2026-10-12T15:00:00")])
def test_verified_heavy_remains_pinned_while_queue_observation_catches_up(state, start):
    value = app()
    value.table_state["jobs_batch_sort"] = "max"
    records = [job(1), job(99, state, start=start)]
    publication = heavy_snapshot(records[1])
    publication["identity"]["start_time"] = "2026-10-12T12:01:00"
    value.raven_ui_state = {"snapshot": publication}
    assert ids(job_groups.project_records(value, {"jobs": records}, records, "jobs")) == ["99", "1"]


def test_numeric_scheduler_owner_mismatch_does_not_pin():
    value = app()
    value.table_state["jobs_batch_sort"] = "max"
    records = [job(1), job(99, "PENDING")]
    value.raven_ui_state = {"snapshot": heavy_snapshot(records[1])}
    snap = {"jobs": records, "details": {"99": {"UserId": "2000"}}}
    assert ids(job_groups.project_records(value, snap, records, "jobs")) == ["1", "99"]


def test_default_mode_and_validated_state_roundtrip():
    value = app()
    table_ui.initialize(value)
    assert value.table_state["jobs_batch_sort"] == "min"
    table_ui.restore(value, {"jobs_batch_sort": ["max"]})
    assert value.table_state["jobs_batch_sort"] == "min"
    before = table_ui.fingerprint(value, "jobs")
    grouping_toolbar.execute(value, "sort-max", {"jobs": []})
    assert table_ui.fingerprint(value, "jobs") != before
    saved = table_ui.save(value)
    other = app()
    table_ui.initialize(other)
    table_ui.restore(other, saved)
    assert other.table_state["jobs_batch_sort"] == "max"
    assert not grouping_toolbar.execute(other, "sort-max", {"jobs": []}).changed


def test_sort_buttons_follow_collapse_and_highlight_current_choice():
    from raven.layout import row_text
    value = app()
    rows, hits = grouping_toolbar.controls(value, Glyphs(False), 180)
    assert [hit[2]["id"] for hit in hits][-3:] == ["grouping:collapse", "grouping:sort-min", "grouping:sort-max"]
    assert any("Sort Min" in text and "sel" in style for row in rows for text, style in row)
    assert "Sort Max" in "".join(row_text(row) for row in rows)


@pytest.mark.parametrize("ascii_", [False, True])
@pytest.mark.parametrize("density", ["compact", "comfortable"])
@pytest.mark.parametrize("queue", [False, True])
def test_all_jobs_controls_are_visible_and_clickable_at_80_by_24(ascii_, density, queue, monkeypatch):
    from raven.layout import row_text
    cfg = Config({"log_lines": 0, "animations": False, "startup_animation": False,
                  "smooth_scrolling": False, "workspace": {"density": density}, "raven": {}})
    store = Store(persist=False)
    if queue:
        store.jobs = [job(1)]
    dashboard = App(store, None, None, cfg, "alex", ascii_=ascii_, interactive=False)
    views = Views(Glyphs(ascii_), cfg)
    dashboard.views_ref = views
    monkeypatch.setattr(dashboard, "save", lambda: None)
    invoked = []
    original = dashboard.run_command
    monkeypatch.setattr(dashboard, "run_command", lambda command: (invoked.append(command), original(command))[1])
    keys = ("auto", "undo", "clear", "expand", "collapse", "sort-min", "sort-max")
    try:
        for key in keys:
            rows, hits = views.compose(store.snapshot(), dashboard, 80, 24)
            controls = {value["id"]: (y, value) for y, kind, value in hits
                        if kind == "control" and value.get("group") == "grouping-toolbar"}
            assert set(controls) == {"grouping:" + item for item in keys}
            assert len({y for y, _ in controls.values()}) == 1
            y, control = controls["grouping:" + key]
            assert 0 <= y < 24 and 0 <= control["left"] < control["right"] <= 80
            assert row_text(rows[y])[control["left"]:control["right"]].strip()
            dashboard.click(y, control["left"], hits, button="press")
            dashboard.click(y, control["left"], hits, button="release")
            assert invoked[-1] == "grouping " + key
        assert dashboard.table_state["jobs_batch_sort"] == "max"
    finally:
        if dashboard.research:
            dashboard.research.close()


def test_real_dashboard_sort_command_reanchors_selected_job_and_persists(tmp_path):
    cfg = Config({"log_lines": 0, "clipboard": {"tools": False, "osc52": False}})
    store = Store(state_dir=str(tmp_path))
    _, _, _, _, snap = batches()
    store.apply_jobs(snap["jobs"])
    dashboard = App(store, None, None, cfg, "alex")
    views = Views(Glyphs(False), cfg)
    dashboard.views_ref = views
    views.compose(store.snapshot(), dashboard, 180, 48)
    dashboard.selected_id = "3"
    dashboard.cursor["jobs"] = dashboard.visible_ids.index("3")
    dashboard.run_command("grouping sort-max")
    assert dashboard.selected_id == "3"
    assert dashboard.visible_ids[dashboard.cursor["jobs"]] == "3"
    assert dashboard.visible_ids[:2] == ["20", "21"]
    reopened = App(Store(state_dir=str(tmp_path)), None, None, cfg, "alex")
    assert reopened.table_state["jobs_batch_sort"] == "max"
