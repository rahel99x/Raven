"""Manual grouping is an exact, reversible presentation choice, not inference."""
import copy
from dataclasses import replace
import json
import random
from types import SimpleNamespace

import pytest

from raven import job_groups as G
from raven import manual_job_groups as M
from raven.model import Finished, Job


def job(jid, **kwargs):
    fields = dict(submit="2026-10-08T12:00:00", workdir="/project", user="alex",
                  account="lab", command="/project/train.sh", cluster="carc")
    fields.update(kwargs)
    return Job(str(jid), "experiment", "main", "RUNNING", **fields)


def app():
    return SimpleNamespace(table_state={"groups": True, "collapsed": []}, tab="jobs",
                           selected_id=None, cursor={"jobs": 0, "history": 0})


def index(value, records, **kwargs):
    return G.registry(value).ensure({"jobs": records, **kwargs})


def test_group_exact_selection_overrides_auto_without_claiming_other_members():
    value, records = app(), [job(i) for i in range(100, 106)]
    original = index(value, records).for_job("100")
    result = M.create(value, {"jobs": records}, ["100", "102"])
    assert result.changed and result.job_ids == ("100", "102")
    current = index(value, records)
    assert current.for_job("100").kind == "manual"
    assert current.for_job("100").members == ("100", "102")
    assert current.for_job("101").id == original.id
    assert current.for_job("101").members == ("101", "103", "104", "105")
    assert records[0].__dict__ == job(100).__dict__


def test_detach_one_then_multiple_expanded_members_preserves_exact_remainder():
    value, records = app(), [job(i) for i in range(100, 105)]
    snap = {"jobs": records}
    made = M.create(value, snap, [record.id for record in records])
    assert M.detach(value, snap, ["102"]).changed
    assert index(value, records).for_job("102") is None
    assert index(value, records).groups[made.group_id].members == ("100", "101", "103", "104")
    assert M.detach(value, snap, ["100", "104"]).changed
    assert index(value, records).groups[made.group_id].members == ("101", "103")
    assert not any(index(value, records).for_job(jid) for jid in ("100", "102", "104"))


@pytest.mark.parametrize("manual", [False, True])
def test_whole_group_detach_includes_members_outside_filtered_projection(manual):
    value, records = app(), [job(i) for i in range(100, 105)]
    snap = {"jobs": records}
    if manual:
        M.create(value, snap, [record.id for record in records])
    group = index(value, records).for_job("100")
    G.project_records(value, snap, records[:1])
    assert M.detach(value, snap, group_ids=[group.id]).changed
    assert not index(value, records).groups
    assert len(value.table_state["manual_groups"]["detached"]) == len(records)


def test_whole_manual_group_detaches_persisted_absent_members_too():
    value, records = app(), [job(i) for i in range(100, 104)]
    made = M.create(value, {"jobs": records}, [record.id for record in records])
    result = M.detach(value, {"jobs": records[:2]}, group_ids=[made.group_id])
    assert result.job_ids == ("100", "101", "102", "103")
    assert not index(value, records).groups


def test_regroup_reverses_exclusion_and_moves_members_without_nested_groups():
    value, records = app(), [job(i) for i in range(100, 106)]
    snap = {"jobs": records}
    old = M.create(value, snap, ["100", "101", "102"])
    other = M.create(value, snap, ["103", "104", "105"])
    assert M.detach(value, snap, ["100"]).changed
    new = M.create(value, snap, ["100", "101", "103", "104"])
    current = index(value, records)
    assert current.groups[new.group_id].members == ("100", "101", "103", "104")
    assert old.group_id not in current.groups and other.group_id not in current.groups
    assert current.for_job("102") is None and current.for_job("105") is None
    assert not value.table_state["manual_groups"]["detached"]


def test_single_remaining_member_does_not_automatically_rejoin_another_launch():
    value, records = app(), [job(i) for i in range(100, 105)]
    snap = {"jobs": records}
    M.create(value, snap, ["100", "101"])
    M.detach(value, snap, ["100"])
    current = index(value, records)
    assert current.for_job("100") is None and current.for_job("101") is None
    assert current.for_job("102").members == ("102", "103", "104")


def test_preferences_survive_restart_and_live_to_history_transition():
    value, records = app(), [job(100), job(900)]
    result = M.create(value, {"jobs": records}, ["100", "900"])
    fresh = app()
    fresh.table_state["manual_groups"] = json.loads(json.dumps(value.table_state["manual_groups"]))
    finished = [Finished(record.id, submit=record.submit, cluster=record.cluster) for record in records]
    current = index(fresh, [], finished=finished)
    assert current.groups[result.group_id].members == ("100", "900")
    M.detach(fresh, {"finished": finished}, ["100"])
    another = app()
    another.table_state["manual_groups"] = fresh.table_state["manual_groups"]
    assert not index(another, records).groups


@pytest.mark.parametrize("field,new", [("submit", "2026-10-09T12:00:00"), ("cluster", "fedora")])
def test_reused_ids_never_inherit_group_or_detached_preferences(field, new):
    value, records = app(), [job(100), job(101), job(102)]
    snap = {"jobs": records}
    M.create(value, snap, ["100", "101"])
    M.detach(value, snap, ["100"])
    replacements = [replace(record, **{field: new}) for record in records]
    current = index(value, replacements)
    assert current.for_job("100").kind == "burst"
    assert current.for_job("100").members == ("100", "101", "102")


def test_inplace_submit_correction_invalidates_manual_overlay_even_array_inference_unchanged():
    value, records = app(), [job("52_0"), job("52_1"), job("52_2")]
    M.create(value, {"jobs": records}, ["52_0", "52_1"])
    records[0].submit = "2026-10-09T12:00:00"
    assert index(value, records).for_job("52_0").kind == "array"
    assert index(value, records).for_job("52_1") is None


def test_accounting_omitted_cluster_uses_same_submission_provenance():
    value, records = app(), [job(100), job(900)]
    made = M.create(value, {"jobs": records}, ["100", "900"])
    finished = [Finished(record.id, submit=record.submit) for record in records]
    assert index(value, [], finished=finished).groups[made.group_id].members == ("100", "900")


def test_empty_snapshots_preserve_preferences_but_do_not_render_phantom_groups():
    value, records = app(), [job(100), job(900)]
    made = M.create(value, {"jobs": records}, ["100", "900"])
    assert not index(value, []).groups
    assert not index(value, records[:1]).groups
    assert index(value, records).groups[made.group_id].members == ("100", "900")


def test_duplicate_selection_and_repeat_create_are_idempotent():
    value, records = app(), [job(100), job(900)]
    snap = {"jobs": records}
    first = M.create(value, snap, ["900", "100", "100"])
    revision = value.manual_job_groups_revision
    again = M.create(value, snap, ["100", "900"])
    assert not again.changed and again.group_id == first.group_id
    assert value.manual_job_groups_revision == revision
    assert len(value.table_state["manual_groups"]["groups"]) == 1


@pytest.mark.parametrize("ids", [[], ["100"], ["100", "100"], ["100", "missing"], "100", None])
def test_invalid_or_stale_create_is_atomic(ids):
    value = app()
    before = copy.deepcopy(value.table_state)
    result = M.create(value, {"jobs": [job(100), job(900)]}, ids)
    assert not result.changed and value.table_state == before


def test_stale_detach_is_atomic_even_when_other_targets_are_valid():
    value, records = app(), [job(100), job(101)]
    snap = {"jobs": records}
    M.create(value, snap, ["100", "101"])
    before = copy.deepcopy(value.table_state)
    assert not M.detach(value, snap, ["100", "missing"]).changed
    assert value.table_state == before
    assert not M.detach(value, snap, ["100"], ["missing"]).changed
    assert value.table_state == before


def test_unknown_submission_group_is_session_only_and_record_identity_safe():
    value, records = app(), [job(100, submit=""), job(900, submit="")]
    snap = {"jobs": records}
    made = M.create(value, snap, ["100", "900"])
    assert made.changed and "session" in made.message
    assert not value.table_state["manual_groups"]["groups"]
    assert index(value, records).groups[made.group_id].members == ("100", "900")
    registry = G.registry(value)
    before = registry.manual.overlay_count
    assert index(value, list(records)).groups[made.group_id].members == ("100", "900")
    assert registry.manual.overlay_count == before
    assert not index(value, [replace(record) for record in records]).groups
    assert not index(app(), records).groups


def test_mixed_stable_and_ambiguous_group_never_persists_partial_membership():
    value, records = app(), [job(100), job(900, submit="")]
    made = M.create(value, {"jobs": records}, ["100", "900"])
    assert made.changed and not value.table_state["manual_groups"]["groups"]
    assert made.group_id in index(value, records).groups


def test_unknown_structural_member_detach_is_session_only():
    value, records = app(), [job("52_0", submit=""), job("52_1", submit=""), job("52_2", submit="")]
    assert M.detach(value, {"jobs": records}, ["52_0"]).changed
    assert index(value, records).for_job("52_0") is None
    assert index(value, records).for_job("52_1").members == ("52_1", "52_2")
    assert not value.table_state["manual_groups"]["detached"]
    replaced = [replace(record) for record in records]
    assert index(value, replaced).for_job("52_0").members == ("52_0", "52_1", "52_2")


def test_unchanged_overlay_cache_avoids_reinference_and_identity_parsing(monkeypatch):
    value, records = app(), [job(i) for i in range(100, 105)]
    M.create(value, {"jobs": records}, ["100", "104"])
    registry = G.registry(value)
    before, count, calls = registry.index, registry.inference_count, registry.manual.overlay_count
    monkeypatch.setattr(registry.manual, "identity", lambda *args: pytest.fail("cached frame parsed identity"))
    for _ in range(100):
        assert index(value, records) is before
    assert registry.inference_count == count and registry.manual.overlay_count == calls


def test_status_changes_update_summary_without_recomputing_overlay():
    value, records = app(), [job(100), job(900)]
    snap = {"jobs": records}
    made = M.create(value, snap, ["100", "900"])
    G.fold(value, made.group_id, True)
    before = index(value, records)
    records[0].state = "COMPLETED"
    assert index(value, records) is before
    G.project_records(value, snap, records)
    meta = G.metadata_for_record(value, "history", "100")
    assert meta.stats.counts["completed"] == meta.stats.counts["running"] == 1


def test_group_mutation_invalidates_render_frame_index():
    value, records = app(), [job(100), job(900)]
    snap = {"jobs": records}
    with G.frame(value, snap):
        assert not G.frame_index(value, snap).groups
        made = M.create(value, snap, ["100", "900"])
        assert made.group_id in G.frame_index(value, snap).groups


@pytest.mark.parametrize("bad", [None, [], True, {}, {"version": True}, {"version": 2}, {"version": "1"}])
def test_invalid_preference_root_is_rejected(bad):
    assert M.validate_state(bad) == {"version": 1, "groups": [], "detached": []}


@pytest.mark.parametrize("key", [None, [], ["1", "", ""], ["1", "2026-02-30T12:00:00", ""],
                                   ["../1", "2026-10-08T12:00:00", ""],
                                   ["1", "2026-10-08T12:00:00", "bad\ncluster"],
                                   ["1", "session:1", ""], [[], {}, None]])
def test_unsafe_preference_identity_is_rejected(key):
    result = M.validate_state({"version": 1, "groups": [{"id": "manual:" + "a" * 20, "members": [key]}], "detached": [key]})
    assert not result["groups"] and not result["detached"]


def test_preference_conflicts_have_deterministic_unique_membership():
    a, b, c = [[str(jid), "2026-10-08T12:00:00", "carc"] for jid in range(3)]
    result = M.validate_state({"version": 1, "groups": [
        {"id": "manual:" + "a" * 20, "members": [a, a, b]},
        {"id": "manual:" + "b" * 20, "members": [b, c]},
    ], "detached": [c, c]})
    assert result["groups"] == [{"id": "manual:" + "a" * 20, "members": [a, b]}]
    assert result["detached"] == [c]


def test_preference_bounds_and_mutation_limit_are_atomic(monkeypatch):
    monkeypatch.setattr(M, "MAX_IDENTITIES", 3)
    value, records = app(), [job(i) for i in range(100, 105)]
    snap = {"jobs": records}
    M.create(value, snap, ["100", "101"])
    before = copy.deepcopy(value.table_state)
    assert not M.create(value, snap, ["103", "104"]).changed
    assert value.table_state == before
    assert not M.create(value, snap, ["100", "101", "102", "103"]).changed
    assert value.table_state == before
    data = {"version": 1, "detached": [[record.id, record.submit, record.cluster] for record in records]}
    assert len(M.validate_state(data)["detached"]) == 3


def test_group_count_limit_does_not_partially_steal_existing_members(monkeypatch):
    monkeypatch.setattr(M, "MAX_GROUPS", 1)
    value, records = app(), [job(i) for i in range(100, 105)]
    snap = {"jobs": records}
    M.create(value, snap, ["100", "101", "102"])
    before = copy.deepcopy(value.table_state)
    assert not M.create(value, snap, ["102", "103"]).changed
    assert value.table_state == before


def test_reset_restores_auto_groups_and_clears_only_manual_fold_preferences():
    value, records = app(), [job(i) for i in range(100, 105)]
    snap = {"jobs": records}
    made = M.create(value, snap, ["100", "101"])
    G.fold(value, made.group_id, True)
    value.table_state["collapsed"].append("array:52")
    M.detach(value, snap, ["102"])
    revision = value.manual_job_groups_revision
    assert M.reset(value).changed
    assert value.manual_job_groups_revision == revision + 1
    assert value.table_state["collapsed"] == ["array:52"]
    assert value.table_state["manual_groups"] == M.validate_state(None)
    assert index(value, records).for_job("100").members == tuple(record.id for record in records)
    assert not M.reset(value).changed


def test_reset_clears_session_groups_and_unknown_detach_tombstones():
    value, records = app(), [job("52_0", submit=""), job("52_1", submit=""), job("52_2", submit="")]
    snap = {"jobs": records}
    M.create(value, snap, ["52_0", "52_1"])
    M.detach(value, snap, ["52_0"])
    assert M.reset(value).changed
    assert not G.registry(value).manual.session_groups
    assert not G.registry(value).manual.session_detached
    assert index(value, records).for_job("52_0").members == ("52_0", "52_1", "52_2")


def test_selection_tokens_are_immutable_and_do_not_query_store():
    records = [job(100), job(900, submit="")]
    before = M.selection_tokens({"jobs": records})
    assert before["100"] == ("100", records[0].submit, "carc", None)
    assert before["900"][3] == id(records[1])
    records[0].submit = "2026-10-09T12:00:00"
    records[1].start = "2026-10-08T13:00:00"
    after = M.selection_tokens({"jobs": records})
    assert before["100"] != after["100"] and before["900"] != after["900"]


@pytest.mark.parametrize("changes", [dict(submit="2026-10-09T12:00:00"), dict(cluster="fedora")])
def test_conflicting_old_history_attempts_block_create_and_detach_atomically(changes):
    value, records = app(), [job(100), job(101)]
    snap = {"jobs": records}
    made = M.create(value, snap, ["100", "101"])
    before = copy.deepcopy(value.table_state)
    conflict = {**snap, "finished": [replace(records[0], **changes)]}
    assert M.selection_tokens(conflict)["100"] is None
    assert not M.create(value, conflict, ["100", "101"]).changed
    assert not M.detach(value, conflict, ["100"]).changed
    assert not M.detach(value, conflict, group_ids=[made.group_id]).changed
    assert value.table_state == before


def test_duplicate_same_attempt_sources_are_not_false_conflicts():
    records = [job(100), job(101)]
    snap = {"jobs": records, "group": [replace(record) for record in records],
            "finished": [Finished(record.id, submit=record.submit) for record in records]}
    assert all(M.selection_tokens(snap).values())
    assert M.create(app(), snap, ["100", "101"]).changed


def test_random_edit_sequence_never_duplicates_or_loses_observed_records():
    value, records = app(), [job(i) for i in range(100, 125)]
    snap = {"jobs": records}
    rng = random.Random(314159)
    for _ in range(100):
        selected = [record.id for record in rng.sample(records, rng.randint(2, 8))]
        if rng.random() < .6:
            M.create(value, snap, selected)
        else:
            M.detach(value, snap, selected)
        current = index(value, records)
        all_members = [jid for group in current.groups.values() for jid in group.members]
        assert len(all_members) == len(set(all_members))
        assert set(all_members) <= {record.id for record in records}
        assert all(len(group.members) >= 2 for group in current.groups.values())
        projected = G.project_records(value, snap, records)
        assert [record.id for record in projected] == [record.id for record in records]
