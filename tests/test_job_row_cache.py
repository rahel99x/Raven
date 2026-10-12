"""Native prepared rows retain current observations and independent mappings."""
from dataclasses import fields, replace

import pytest

from raven import clock, job_groups, job_row_cache as C, layout as L, table_sort
from raven.actions import Actions
from raven.config import Config
from raven.controller import App
from raven.job_progress import Source
from raven.model import GpuSample, Job, Live, Store
from raven.plugins import PluginAPI
from raven.views import Views


@pytest.fixture
def case():
    cfg = Config({"animations": True})
    store = Store(persist=False)
    store.jobs = [Job("41", "training", "gpu", "RUNNING", elapsed="0:10:00", limit="1:00:00",
                      cpus=4, gpus=1, mem_req="8G", hosts=["node1"])]
    store.live["41"] = Live(avg=.4, rate=.7, rss=2 << 30)
    store.gpu["41"] = [GpuSample("node1", 0, 80, 512, 2048)]
    store.tags["41"] = {"tags": ["train"], "note": "current", "pinned": False}
    app = App(store, None, None, cfg, "test", interactive=False)
    views = Views(L.Glyphs(False), cfg)
    app.views_ref = views
    snap = store.snapshot()
    progress = {"41": Source(.4, "reported", "steps")}
    rows = views.job_rows(snap, app)
    key, cached = C.lookup(views, snap, app, None, progress, table_sort.chain(app, "jobs"))
    assert key is not None and cached is None
    C.remember(views, app, key, rows)
    yield app, views, snap, progress, rows
    if app.research:
        app.research.close()


def lookup(case):
    app, views, snap, progress, rows = case
    return C.lookup(views, snap, app, None, progress, table_sort.chain(app, "jobs"))


def test_unchanged_source_reuses_rows_without_leaking_mutable_maps(case):
    app, views, snap, progress, rows = case
    key, cached = lookup(case)
    assert cached == rows and cached is not rows
    assert cached[0]["job"] is snap["jobs"][0]
    cached[0]["info"] = "modified presentation"
    cached[0]["_styles"]["flags"] = "wrong"
    again = lookup(case)[1]
    assert again[0]["info"] != "modified presentation"
    assert again[0]["_styles"].get("flags") != "wrong"


@pytest.mark.parametrize("field", [value.name for value in fields(Job)])
def test_every_native_job_field_correction_is_observed_in_place(case, field):
    job = case[2]["jobs"][0]
    value = getattr(job, field)
    if isinstance(value, list):
        value.append("node2")
    else:
        setattr(job, field, value + 1 if isinstance(value, int) else value + " changed")
    assert lookup(case)[1] is None


@pytest.mark.parametrize("field", ["avg", "rate", "rss"])
def test_live_usage_corrections_invalidate_in_place(case, field):
    sample = case[2]["live"]["41"]
    setattr(sample, field, getattr(sample, field) + 1)
    assert lookup(case)[1] is None


@pytest.mark.parametrize("change", ["gpu-util", "gpu-node", "gpu-index", "gpu-vendor", "gpu-uuid", "gpu-bdf", "gpu-partition", "gpu-mean", "tag", "note", "pin", "progress"])
def test_telemetry_tags_and_progress_corrections_remain_current(case, change):
    snap, progress = case[2:4]
    if change.startswith("gpu-") and change != "gpu-mean":
        field = change[4:]
        sample = snap["gpu"]["41"][0]
        value = getattr(sample, field)
        setattr(sample, field, value + "other" if isinstance(value, str) else value + 1)
    elif change == "gpu-mean":
        snap["gpu_mean"]["41:node1:0"] = 3
    elif change == "tag":
        snap["tags"]["41"]["tags"].append("new")
    elif change == "progress":
        progress["41"] = Source(.8, "reported", "steps")
    else:
        snap["tags"]["41"]["pinned" if change == "pin" else "note"] = True if change == "pin" else "new note"
    assert lookup(case)[1] is None


@pytest.mark.parametrize("change", ["facets", "numeric", "cascade", "filter", "selected", "marks", "theme", "threshold", "ascii"])
def test_table_and_display_preferences_remain_current(case, change):
    app, views = case[:2]
    if change == "facets":
        app.table_state["facets"]["jobs"] = {"partition": "cpu"}
    elif change == "numeric":
        app.table_tools_state["numeric"]["jobs"] = [("cpus", ">", 10, "10")]
    elif change == "cascade":
        table_sort.set_sort(app, "jobs", "id", "desc")
    elif change == "filter":
        app.filter = "none"
    elif change == "selected":
        app.selected_id = "another"
    elif change == "marks":
        app.marks.add("41")
    elif change == "theme":
        app.theme = "darcula"
    elif change == "threshold":
        views.th["cpu"] = .9
    else:
        views.g = L.Glyphs(True)
    assert lookup(case)[1] is None


def test_replaced_equal_job_refreshes_exact_row_record_reference(case):
    snap = case[2]
    original = snap["jobs"][0]
    snap["jobs"][0] = replace(original)
    assert lookup(case)[1] is None


def test_pending_wait_invalidates_at_compact_display_boundary(case, monkeypatch):
    app, views, snap, progress, rows = case
    job = snap["jobs"][0]
    job.state, job.submit = "PENDING", "2026-10-08T12:00:00"
    started = clock.now()
    monkeypatch.setattr(C, "stamp", lambda value: started)
    monkeypatch.setattr(clock, "now", lambda: started + 61)
    key, cached = lookup(case)
    C.remember(views, app, key, rows)
    monkeypatch.setattr(clock, "now", lambda: started + 119)
    assert lookup(case)[1] is not None
    monkeypatch.setattr(clock, "now", lambda: started + 120)
    assert lookup(case)[1] is None


def test_custom_callbacks_keep_existing_behavior_instead_of_inferred_dependencies(case):
    app, views, snap, progress, rows = case
    assert C.lookup(views, snap, app, object(), progress, None) == (None, None)
    plugin = views.plugins = PluginAPI()
    plugin.flag(lambda *args: "dynamic")
    assert lookup(case) == (None, None)


def test_cascading_sort_maps_are_independent(case):
    app, views, snap, progress, rows = case
    table_sort.set_sort(app, "jobs", "id", "asc")
    key, cached = lookup(case)
    rows = [{"job": snap["jobs"][0], "_sort": {"id": "41"}, "_styles": {}}]
    C.remember(views, app, key, rows)
    lookup(case)[1][0]["_sort"]["id"] = "wrong"
    assert lookup(case)[1][0]["_sort"]["id"] == "41"


def test_native_actions_reuse_rows_and_expire_marks_on_time(case, monkeypatch):
    app, views, snap, progress, rows = case
    actions = Actions(None, app.store)
    monkeypatch.setattr(clock, "now", lambda: 1000)
    actions.marks["41"] = ("cancelling", 1000)
    first = views.job_rows(snap, app, actions)
    assert "CANCELLING" in first[0]["info"]
    cached = C.lookup(views, snap, app, actions, {}, table_sort.chain(app, "jobs"))[1]
    assert cached is not None and "CANCELLING" in cached[0]["info"]
    monkeypatch.setattr(clock, "now", lambda: 1121)
    after = views.job_rows(snap, app, actions)
    assert "CANCELLING" not in after[0]["info"] and not actions.marks


def test_native_hold_acknowledgement_invalidates_action_flag(case, monkeypatch):
    app, views, snap, progress, rows = case
    actions = Actions(None, app.store)
    job = snap["jobs"][0]
    job.state = "PENDING"
    monkeypatch.setattr(clock, "now", lambda: 1000)
    actions.marks["41"] = ("holding", 1000)
    assert "HOLDING" in views.job_rows(snap, app, actions)[0]["info"]
    job.reason = "JobHeldUser"
    acknowledged = views.job_rows(snap, app, actions)[0]
    assert "HOLDING" not in acknowledged["info"]
    assert "held" in acknowledged["flags"] and not actions.marks


@pytest.mark.parametrize("kind", ["instance", "class", "subclass"])
def test_modified_action_callbacks_are_never_cached(case, monkeypatch, kind):
    app, views, snap, progress, rows = case
    actions = Actions(None, app.store)
    if kind == "instance":
        actions.mark = lambda job: "custom"
    elif kind == "class":
        monkeypatch.setattr(Actions, "mark", lambda self, job: "custom")
    else:
        class Custom(Actions):
            pass
        actions = Custom(None, app.store)
    assert C.lookup(views, snap, app, actions, progress, None) == (None, None)


def test_group_projection_and_progress_animations_remain_outside_row_cache(case):
    app, views, snap, progress, rows = case
    first = snap["jobs"][0]
    first.id = "41_0"
    second = replace(first, id="41_1")
    snap["jobs"] = [first, second]
    app.table_state["groups"] = True
    expanded = views.job_rows(snap, app)
    assert len(expanded) == 2
    group = expanded[0]["_group"].group
    job_groups.fold(app, group.id, True)
    collapsed = views.job_rows(snap, app)
    assert len(collapsed) == 1 and collapsed[0]["_group"].collapsed
    assert collapsed[0]["_progress_animation"] is None
    assert collapsed[0]["progress"] == ""
    job_groups.fold(app, group.id, False)
    reopened = views.job_rows(snap, app)
    assert len(reopened) == 2
    assert all(row["_progress_animation"] == "clock" and row["progress"] for row in reopened)


def test_shared_rows_are_immutable_and_do_not_allocate_full_mutable_copies(case):
    app, views, snap, progress, rows = case
    key, first = C.lookup(views, snap, app, None, progress, table_sort.chain(app, "jobs"), shared=True)
    second = C.lookup(views, snap, app, None, progress, table_sort.chain(app, "jobs"), shared=True)[1]
    assert first is second and first[0]["job"] is snap["jobs"][0]
    with pytest.raises(TypeError):
        first[0]["info"] = "bad cached display"
    with pytest.raises(TypeError):
        first[0]["_styles"]["flags"] = "bad cached style"
    visible = C.copy_visible(first[:1])
    visible[0]["_mark"] = "selected"
    visible[0]["_styles"]["flags"] = "yellow"
    assert "_mark" not in first[0]
    assert first[0]["_styles"].get("flags") != "yellow"
    assert lookup(case)[1][0]["_styles"].get("flags") != "yellow"


def test_internal_table_copies_only_visible_rows_and_does_not_fetch_sources(case, monkeypatch):
    import builtins
    app, views, snap, progress, rows = case
    snap["jobs"] = [replace(snap["jobs"][0], id=str(index), name=f"job-{index}") for index in range(1000)]
    app.job_panel_state["mode"] = "off"
    app.table_state["groups"] = False
    views.compose(snap, app, 160, 48)
    views.compose(snap, app, 160, 48)
    cached_before = app.job_rows_cache_state
    counts = []
    original = C._copy_rows
    def copy_rows(rows):
        counts.append(len(rows))
        return original(rows)
    def forbid(*args, **kwargs):
        pytest.fail("Cached table rendering must use the supplied publication")
    monkeypatch.setattr(C, "_copy_rows", copy_rows)
    monkeypatch.setattr(app.store, "snapshot", forbid)
    monkeypatch.setattr(app.store, "render_snapshot", forbid)
    monkeypatch.setattr(builtins, "open", forbid)
    rendered, hits = views.compose(snap, app, 160, 48)
    visible = [hit for hit in hits if hit[1] == "job"]
    assert visible and len(app.visible_ids) == 1000
    assert app.job_rows_cache_state is cached_before
    assert counts and max(counts) <= 48
    assert sum(counts) == len(visible)
    cached_rows = app.job_rows_cache_state[2]
    assert all("_mark" not in row for row in cached_rows)


@pytest.mark.parametrize("kind", ["instance", "class", "subclass"])
@pytest.mark.parametrize("surface", ["compose", "jobs_tab"])
def test_internal_table_preserves_legacy_row_builder_overrides(case, monkeypatch, kind, surface):
    app, views, snap, progress, rows = case
    native = Views.job_rows
    calls = []
    def legacy(self, snap, app, actions=None):
        calls.append(actions)
        result = native(self, snap, app, actions)
        result[0]["info"] = "legacy row customization"
        return result
    if kind == "instance":
        monkeypatch.setattr(views, "job_rows", lambda snap, app, actions=None: legacy(views, snap, app, actions))
    elif kind == "class":
        monkeypatch.setattr(Views, "job_rows", legacy)
    else:
        class LegacyViews(Views):
            job_rows = legacy
        views = LegacyViews(views.g, views.cfg)
        app.views_ref = views
    app.job_panel_state["mode"] = "off"
    actions = Actions(None, app.store)
    if surface == "compose":
        rendered, hits = views.compose(snap, app, 320, 48, actions=actions)
    else:
        rendered, hits = views.jobs_tab(snap, app, actions, 320, 48)
    assert calls == [actions]
    assert app.visible_ids == ["41"]
    assert any(hit[1] == "job" and hit[2] == "41" for hit in hits)
    assert "legacy row customization" in L.to_text(rendered, 320)


@pytest.mark.parametrize("change", ["correct", "replace", "append", "remove", "filter", "sort", "group"])
def test_shared_native_rows_match_mutable_api_after_changes(case, change):
    app, views, snap, progress, rows = case
    first = snap["jobs"][0]
    first.id = "41_0"
    snap["jobs"].append(replace(first, id="41_1", name="other"))
    views.job_rows(snap, app, shared=True)
    if change == "correct":
        first.name, first.cpus = "corrected", 8
    elif change == "replace":
        snap["jobs"][0] = replace(first, cpus=32)
    elif change == "append":
        snap["jobs"].append(replace(first, id="41_2", cpus=64))
    elif change == "remove":
        snap["jobs"].pop(0)
    elif change == "filter":
        app.filter = "other"
    elif change == "sort":
        table_sort.set_sort(app, "jobs", "id", "desc")
    else:
        app.table_state["groups"] = True
        expanded = views.job_rows(snap, app, shared=True)
        group = expanded[0]["_group"].group
        job_groups.fold(app, group.id, True)
    shared = views.job_rows(snap, app, shared=True)
    expected = views.job_rows(snap, app)
    assert shared == expected
    for row in shared:
        assert row["job"] is next(job for job in snap["jobs"] if job.id == row["id"])
    if change == "group":
        job_groups.fold(app, group.id, False)
        assert views.job_rows(snap, app, shared=True) == views.job_rows(snap, app)
        assert len(views.job_rows(snap, app, shared=True)) == 2
