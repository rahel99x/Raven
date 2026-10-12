"""Independent launch evidence, identity safety, and cache budget contracts."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from raven import job_groups as G
from raven.model import Finished, Job


def job(jid, *, name="experiment", submit="2026-10-08T12:00:00", **kwargs):
    return Job(str(jid), name, "main", "RUNNING", submit=submit,
               user=kwargs.pop("user", "alex"), account=kwargs.pop("account", "lab"),
               command=kwargs.pop("command", "/project/train.sh"), **kwargs)


def snapshot(jobs=(), finished=(), departed=(), **kwargs):
    records = list(jobs) + list(finished) + list(departed)
    details = {record.id: {"WorkDir": "/project", "UserId": "alex(1000)",
                            "Account": "lab", "Command": "/project/train.sh"} for record in records}
    details.update(kwargs.pop("details", {}))
    return {"jobs": list(jobs), "finished": list(finished),
            "departed_jobs": {record.id: record for record in departed},
            "details": details, "tags": {}, **kwargs}


def app():
    return SimpleNamespace(table_state={"groups": True, "collapsed": []}, tab="jobs",
                           selected_id=None, cursor={"jobs": 0, "history": 0},
                           last_jobs_ids=[], last_history_ids=[])


@pytest.mark.parametrize("ids,kind", [(["12_0", "12_1"], "array"),
                                     (["12_[2-9%4]", "12_1"], "array"),
                                     (["12+0", "12+1"], "heterogeneous"),
                                     (["12_1+0", "12_2+0"], "array")])
def test_scheduler_evidence_is_strong_without_owner_or_project_fields(ids, kind):
    records = [job(jid, name=f"different-{index}") for index, jid in enumerate(ids)]
    index = G.Registry().ensure({"jobs": records})
    group, = index.groups.values()
    assert group.kind == kind and group.confidence == "certain"
    assert set(group.members) == set(ids)


def test_arrays_with_identical_names_and_numeric_parent_or_steps_never_merge():
    records = [job(jid) for jid in ("12_0", "12_1", "13_0", "13_1", "12", "12.batch", "12.0")]
    index = G.Registry().ensure(snapshot(records))
    assert len(index.groups) == 2
    assert index.by_job["12_0"] != index.by_job["13_0"]
    assert not {"12", "12.batch", "12.0"} & index.by_job.keys()


def test_native_ids_are_separate_across_explicit_scheduler_clusters():
    records = [dict(id="12_0", name="a", cluster="one"), dict(id="12_1", name="a", cluster="two")]
    assert not G.Registry().ensure({"jobs": records}).groups


def test_complete_consecutive_burst_is_likely_and_explains_evidence():
    records = [job(100), job(101, submit="2026-10-08T12:00:10")]
    group, = G.Registry().ensure(snapshot(records)).groups.values()
    assert group.kind == "burst" and group.confidence == "likely"
    assert group.members == ("100", "101")
    assert "10 seconds" in group.reason and "work directory" in group.reason


@pytest.mark.parametrize("field", ["UserId", "Account", "WorkDir", "Command"])
def test_missing_provenance_prevents_same_name_burst_inference(field):
    records = [job(100), job(101)]
    snap = snapshot(records)
    for fields in snap["details"].values():
        fields[field] = ""
    assert not G.Registry().ensure(snap).groups


@pytest.mark.parametrize("field,value", [("UserId", "bea(2000)"), ("Account", "another"),
                                          ("WorkDir", "/another"), ("Command", "/project/train.sh --different")])
def test_different_provenance_prevents_same_name_burst_inference(field, value):
    records = [job(100), job(101)]
    snap = snapshot(records)
    snap["details"]["101"][field] = value
    assert not G.Registry().ensure(snap).groups


@pytest.mark.parametrize("records", [lambda: [job(100), job(101, submit="2026-10-08T12:00:11")],
                                      lambda: [job(100), job(102)],
                                      lambda: [job(100, submit="unknown"), job(101)],
                                      lambda: [job(100, name="generic"), job(101, name="different")]])
def test_separate_submissions_or_incomplete_evidence_are_not_groups(records):
    assert not G.Registry().ensure(snapshot(records())).groups


def test_window_is_anchored_not_a_transitive_chain_of_close_submissions():
    records = [job(100), job(101, submit="2026-10-08T12:00:09"),
               job(102, submit="2026-10-08T12:00:18"), job(103, submit="2026-10-08T12:00:36")]
    index = G.Registry().ensure(snapshot(records))
    assert index.for_job("100").members == ("100", "101", "102")
    assert index.for_job("103") is None


def test_numbered_name_family_requires_three_with_full_common_provenance():
    records = [job(100 + n, name=f"run_{n}") for n in range(3)]
    assert not G.Registry().ensure(snapshot(records[:2])).groups
    group, = G.Registry().ensure(snapshot(records)).groups.values()
    assert group.members == ("100", "101", "102") and group.label == "run"


@pytest.mark.parametrize("marker", ["launch:series-a", "group:series-a"])
def test_explicit_tags_allow_nonconsecutive_ids_and_different_job_names(marker):
    records = [job(100, name="first"), job(500, name="second")]
    snap = snapshot(records)
    snap["tags"] = {record.id: {"tags": [marker, "ordinary-tag"]} for record in records}
    group, = G.Registry().ensure(snap).groups.values()
    assert group.kind == "explicit" and group.confidence == "explicit"


def test_generic_shared_tag_or_conflicting_markers_do_not_group_jobs():
    records = [job(100), job(500)]
    snap = snapshot(records)
    snap["tags"] = {record.id: {"tags": ["train", "launch:a", "launch:b"]} for record in records}
    assert not G.Registry().ensure(snap).groups


def test_explicit_launch_id_is_scoped_by_owner_and_project():
    records = [job(100), job(500)]
    snap = snapshot(records)
    for fields in snap["details"].values():
        fields["RavenLaunchId"] = "same-id"
    snap["details"]["500"]["WorkDir"] = "/another-project"
    assert not G.Registry().ensure(snap).groups
    snap["details"]["500"]["WorkDir"] = "/project"
    assert len(G.Registry().ensure(snap).groups) == 1


def test_lifecycle_union_uses_active_record_and_retains_shared_group_identity():
    first, second = job("12_0"), job("12_1")
    registry = G.Registry()
    index = registry.ensure(snapshot([first, second]))
    original = index.by_job[first.id]
    registry.fold(original, True)
    finished = Finished(first.id, first.name, "COMPLETED", submit=first.submit, workdir="/project")
    index = registry.ensure(snapshot([second], [finished]))
    assert index.by_job[first.id] == index.by_job[second.id] == original
    assert registry.is_collapsed(index.groups[original])
    assert registry.project([second])[0].collapsed
    assert registry.project([finished])[0].collapsed


def test_burst_completion_and_later_backfill_preserve_fold_identity():
    records = [job(101, submit="2026-10-08T12:00:03"), job(102, submit="2026-10-08T12:00:04")]
    registry = G.Registry()
    gid = registry.ensure(snapshot(records)).by_job["101"]
    registry.fold(gid, True)
    finished = Finished("101", "experiment", "COMPLETED", submit=records[0].submit, workdir="/project")
    updated = snapshot([job(100), records[1]], [finished])
    group = registry.ensure(updated).for_job("100")
    assert group.id == gid and registry.is_collapsed(group)
    assert group.members == ("100", "101", "102")


def test_reused_ids_with_new_submit_times_start_new_inferred_launch():
    records = [job(100), job(101)]
    registry = G.Registry()
    old = registry.ensure(snapshot(records)).by_job["100"]
    registry.fold(old, True)
    later = [replace(record, submit="2026-10-08T12:00:05") for record in records]
    current = registry.ensure(snapshot(later)).for_job("100")
    assert current.id != old and not registry.is_collapsed(current)


def test_exact_content_cache_updates_in_place_corrections_but_ignores_live_usage():
    records = [job(100), job(101)]
    snap = snapshot(records)
    registry = G.Registry()
    first = registry.ensure(snap)
    assert registry.ensure(snap) is first and registry.inference_count == 1
    records[0].elapsed = "1:23"
    assert registry.ensure(snap) is first and registry.inference_count == 1
    snap["details"]["101"]["Command"] += " --changed"
    assert not registry.ensure(snap).groups and registry.inference_count == 2


def test_projection_retains_original_identity_and_within_batch_sort_order():
    records = [job("12_0"), job(900), job("12_1"), job(901)]
    snap = snapshot(records)
    value = app()
    out = G.project_records(value, snap, records, "jobs")
    assert out == [records[0], records[2], records[1], records[3]]
    meta = G.metadata_for_record(value, "jobs", "12_0")
    assert meta.header and meta.visible_count == meta.total_count == 2
    assert not G.metadata_for_record(value, "jobs", "12_1").header
    G.fold(value, meta.group.id, True)
    out = G.project_records(value, snap, records, "jobs")
    assert out == [records[0], records[1], records[3]]
    # A filter does not resurrect its hidden sibling. The filtered record is
    # the real representative and reports 1 visible / 2 observed allocations.
    out = G.project_records(value, snap, [records[2], records[3]], "history")
    assert out == [records[2], records[3]]
    filtered = G.metadata_for_record(value, "history", "12_1")
    assert filtered.header and filtered.visible_count == 1 and filtered.total_count == 2


def test_shared_collapse_reanchors_hidden_selected_actual_job_without_fake_ids():
    records = [job("12_0"), job("12_1")]
    value = app()
    value.selected_id, value.cursor["jobs"] = "12_1", 1
    value.last_jobs_ids = [record.id for record in records]
    snap = snapshot(records)
    G.project_records(value, snap, records, "jobs")
    assert G.fold(value, "array:12", True)
    assert value.selected_id == "12_0" and value.cursor["jobs"] == 0
    assert [record.id for record in G.project_records(value, snap, records, "recent")] == ["12_0"]
    assert G.fold(value, "array:12", False)
    assert [record.id for record in G.project_records(value, snap, records, "history")] == ["12_0", "12_1"]


def test_prepared_rows_copy_only_group_presentation_without_mutating_input():
    records = [job("12_0"), job("12_1")]
    rows = [{"job": record, "id": record.id, "info": "source"} for record in records]
    value = app()
    out = G.project_rows(value, rows, snapshot(records))
    assert out[0]["job"] is records[0] and out[0]["id"] == "12_0"
    assert "Array 12" in out[0]["info"] and rows[0]["info"] == "source"
    assert "_group" not in rows[0]


def test_disabled_grouping_skips_deduction_and_removes_stale_presentation(monkeypatch):
    value = app()
    records = [job("12_0"), job("12_1")]
    snap = snapshot(records)
    G.project_records(value, snap, records, "jobs")
    value.table_state["groups"] = False
    monkeypatch.setattr(G.Registry, "ensure", lambda *args: pytest.fail("disabled grouping inferred jobs"))
    assert G.project_records(value, snap, records, "jobs") == records
    assert G.metadata_for_record(value, "jobs", "12_0") is None


def test_large_groups_are_linear_and_cached_without_pairwise_comparison(monkeypatch):
    records = [job(f"12_{index}") for index in range(10000)]
    snap = {"jobs": records}
    registry = G.Registry()
    original = G._evidence
    seen = 0

    def counted(*args, **kwargs):
        nonlocal seen
        seen += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(G, "_evidence", counted)
    first = registry.ensure(snap)
    assert len(first.groups) == 1 and len(first.by_job) == 10000
    assert seen == 10000
    assert registry.ensure(snap) is first and registry.inference_count == 1
    assert seen == 10000  # Unchanged exact fields reuse parsed evidence.
    registry.fold("array:12", True)
    assert len(registry.project(records)) == 1  # Header only; no task expansion.
    value = app()
    assert len(G.project_records(value, snap, records, "jobs", index=first)) == 10000
    assert seen == 10000  # Callers share one inferred index across frame views.


def test_immutable_group_indexes_do_not_leak_mutable_cache_state():
    index = G.Registry().ensure({"jobs": [job("12_0"), job("12_1")]})
    with pytest.raises(TypeError):
        index.by_job["12_0"] = "fake"
    with pytest.raises(TypeError):
        index.groups["new"] = object()


def test_render_frame_shares_one_check_and_never_reuses_stale_mutable_snapshot(monkeypatch):
    records = [job(100), job(101)]
    snap = snapshot(records)
    value = app()
    groups = G.registry(value)
    original = groups.ensure
    calls = 0

    def counted(snapshot):
        nonlocal calls
        calls += 1
        return original(snapshot)

    monkeypatch.setattr(groups, "ensure", counted)
    with G.frame(value, snap) as index:
        assert G.frame_index(value, snap) is index
        G.project_records(value, snap, records, "jobs")
        G.project_records(value, snap, records, "recent")
        assert calls == 1
    assert value.job_groups_frame_index is None
    snap["details"]["101"]["WorkDir"] = "/changed"
    assert not G.frame_index(value, snap).groups and calls == 2


def test_render_frame_clears_index_when_compose_raises():
    value = app()
    with pytest.raises(RuntimeError):
        with G.frame(value, snapshot([job("12_0"), job("12_1")])):
            raise RuntimeError("paint failed")
    assert value.job_groups_frame_index is None


def test_fold_open_close_are_idempotent_for_known_groups():
    value = app()
    snap = snapshot([job("12_0"), job("12_1")])
    G.project_records(value, snap, snap["jobs"], "jobs")
    assert G.fold(value, "array:12", False)
    assert G.fold(value, "array:12", False)
    assert G.fold(value, "array:12", True)
    assert G.fold(value, "array:12", True)
    assert not G.fold(value, "array:999", True)


def test_ordinary_accounting_fastpath_still_detects_later_launch_evidence(monkeypatch):
    records = [Finished(str(100 + index), "train", "COMPLETED") for index in range(1000)]
    snap = {"finished": records}
    groups = G.Registry()
    original = G._evidence
    seen = 0

    def counted(*args, **kwargs):
        nonlocal seen
        seen += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(G, "_evidence", counted)
    assert not groups.ensure(snap).groups and seen == 0
    snap["details"] = {"100": {"RavenLaunchId": "later"}, "101": {"RavenLaunchId": "later"}}
    group, = groups.ensure(snap).groups.values()
    assert group.members == ("100", "101") and seen == 2


@pytest.mark.parametrize("suffix", ["[--]", "[9-1]", "[0-9:0]", "[0-9%0]", "[0,]"])
def test_malformed_array_tokens_are_not_strong_evidence(suffix):
    records = [job("12_" + suffix), job("12_0")]
    assert not G.Registry().ensure({"jobs": records}).groups


def test_bounded_collapsed_registry_retains_the_group_just_closed(monkeypatch):
    records = [job("1_0"), job("1_1"), job("9_0"), job("9_1")]
    groups = G.Registry(["9", "8"])
    groups.ensure({"jobs": records})
    monkeypatch.setattr(G, "MAX_COLLAPSED", 2)
    assert groups.fold("array:1", True)
    assert groups.is_collapsed(groups.index.groups["array:1"])
    assert len(groups.collapsed) == 2


def test_mapping_records_and_in_place_metadata_are_supported():
    from types import MappingProxyType
    first = {"id": "100", "name": "first", "RavenLaunchId": "marker"}
    second = {"id": "500", "name": "second", "RavenLaunchId": "marker"}
    snap = {"jobs": [MappingProxyType(first), MappingProxyType(second)]}
    groups = G.Registry()
    assert len(groups.ensure(snap).groups) == 1
    second["RavenLaunchId"] = "different"
    assert not groups.ensure(snap).groups


@pytest.mark.parametrize("tab,identity_field", [("group", "group_ids"), ("deps", "dep_ids")])
def test_other_job_tables_reanchor_exact_representative_on_fold(tab, identity_field):
    records = [job("12_0"), job("12_1")]
    value = app()
    value.tab, value.selected_id = tab, "12_1"
    value.cursor[tab] = 1
    setattr(value, identity_field, [record.id for record in records])
    snap = snapshot(records)
    G.project_records(value, snap, records, tab)
    assert G.fold(value, "array:12", True)
    assert value.selected_id == "12_0" and value.cursor[tab] == 0


def test_repeated_dependency_paths_count_distinct_jobs_and_preserve_edge_rows():
    records = [job("12_0"), job("12_1")]
    repeated = [records[0], records[1], records[0]]
    snap = snapshot(records)
    value = app()
    assert G.project_records(value, snap, repeated, "deps") == repeated
    meta = G.metadata_for_record(value, "deps", "12_0")
    assert meta.visible_count == meta.total_count == 2
    assert meta.records == tuple(records)
    assert G.fold(value, meta.group.id, True)
    assert G.project_records(value, snap, repeated, "deps") == [records[0], records[0]]


def test_deleting_workdir_evidence_does_not_reuse_stale_provenance():
    records = [job(100), job(101)]
    snap = snapshot(records)
    groups = G.Registry()
    assert len(groups.ensure(snap).groups) == 1
    del snap["details"]["101"]["WorkDir"]
    assert not groups.ensure(snap).groups


def test_accounting_type_transition_can_retain_omitted_owner_and_command():
    records = [job(100), job(101)]
    snap = {"jobs": records, "details": {record.id: {"WorkDir": "/project"} for record in records}}
    groups = G.Registry()
    gid = groups.ensure(snap).for_job("100").id
    completed = Finished("100", records[0].name, "COMPLETED", submit=records[0].submit, workdir="/project")
    snap["jobs"], snap["finished"] = [records[1]], [completed]
    assert groups.ensure(snap).for_job("100").id == gid
