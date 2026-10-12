"""Moving jobs between display groups preserves exact attempts and ordering."""
import copy
from dataclasses import replace
import json
import random
from types import SimpleNamespace

import pytest

from raven import job_groups as G
from raven import manual_job_groups as M
from raven.model import Finished, Job
from raven.table_sort import _natural


def job(jid, **changes):
    fields = dict(submit="2026-10-08T12:00:00", workdir="/project", user="alex",
                  account="lab", command="/project/train.sh", cluster="carc")
    fields.update(changes)
    return Job(str(jid), "experiment", "main", "RUNNING", **fields)


def app():
    return SimpleNamespace(table_state={"groups": True, "collapsed": []}, tab="jobs",
                           selected_id=None, cursor={"jobs": 0, "history": 0})


def current(owner, snap):
    return G.registry(owner).ensure(snap)


def test_add_ranges_is_natural_and_keeps_manual_identity_label_and_sort():
    owner = app()
    records = [job(jid) for jid in range(1, 13)]
    snap = {"jobs": records}
    made = M.create(owner, snap, ["1", "2", "3"])
    for ids in (["9", "7", "8"], ["6", "4", "5"], ["12", "10", "11"]):
        added = M.add(owner, snap, made.group_id, ids)
        assert added.changed and added.group_id == made.group_id
    group = current(owner, snap).groups[made.group_id]
    assert group.members == tuple(str(jid) for jid in range(1, 13))
    assert group.label == "Manual group 1"
    assert [record.id for record in G.project_records(owner, snap, records[::-1])] == list(group.members)[::-1]


def test_add_lower_id_keeps_label_through_removal_and_restart():
    owner = app()
    snap = {"jobs": [job(jid) for jid in (1, 3, 7, 9)]}
    made = M.create(owner, snap, ["7", "9"])
    assert M.add(owner, snap, made.group_id, ["1", "3"]).changed
    assert M.detach(owner, snap, ["7"]).changed
    restored = app()
    restored.table_state["manual_groups"] = json.loads(json.dumps(owner.table_state["manual_groups"]))
    group = current(restored, snap).groups[made.group_id]
    assert group.members == ("1", "3", "9")
    assert group.label == "Manual group 7"


def test_natural_order_handles_arrays_heterogeneous_steps_and_leading_zeroes():
    owner = app()
    ids = ["3", "10", "2", "02", "9_10", "9_2", "9+12", "9+2", "9.batch", "9.extern"]
    snap = {"jobs": [job(jid) for jid in ids]}
    made = M.create(owner, snap, ["9_10", "10"])
    assert M.add(owner, snap, made.group_id, ids).changed
    members = current(owner, snap).groups[made.group_id].members
    assert members == tuple(sorted(ids, key=lambda value: (_natural(value), value)))
    assert members.index("9_2") < members.index("9_10")
    assert members.index("9+2") < members.index("9+12")
    assert members.index("2") < members.index("3") < members.index("10")


@pytest.mark.parametrize("collapsed", [False, True])
def test_automatic_destination_is_promoted_without_siblings_of_selected_jobs(collapsed):
    owner = app()
    snap = {"jobs": [job(jid) for jid in ("4_1", "4_2", "9_1", "9_2", "9_3")]}
    original = current(owner, snap).for_job("4_1")
    G.fold(owner, original.id, collapsed)
    evidence = M.target_evidence(owner, snap, original.id)
    added = M.add(owner, snap, original.id, ["9_2"], expected=evidence)
    assert added.changed and added.group_id.startswith("manual:")
    index = current(owner, snap)
    group = index.groups[added.group_id]
    assert group.members == ("4_1", "4_2", "9_2")
    assert group.label == original.label
    assert G.registry(owner).is_collapsed(group) is collapsed
    assert index.for_job("9_1").members == ("9_1", "9_3")


def test_manual_move_preserves_untouched_source_and_removes_detached_exclusion():
    owner = app()
    snap = {"jobs": [job(jid) for jid in range(1, 10)]}
    source = M.create(owner, snap, ["1", "2", "3", "4"])
    dest = M.create(owner, snap, ["7", "8"])
    assert M.detach(owner, snap, ["9"]).changed
    assert M.add(owner, snap, dest.group_id, ["2", "4", "9"]).changed
    index = current(owner, snap)
    assert index.groups[source.group_id].members == ("1", "3")
    assert index.groups[dest.group_id].members == ("2", "4", "7", "8", "9")
    assert not owner.table_state["manual_groups"]["detached"]


def test_manual_destination_preserves_saved_absent_members():
    owner = app()
    records = [job(jid) for jid in range(1, 6)]
    made = M.create(owner, {"jobs": records}, ["1", "2", "3", "4"])
    sparse = {"jobs": [records[0], records[1], records[4]]}
    assert M.add(owner, sparse, made.group_id, ["5"]).changed
    assert current(owner, {"jobs": records}).groups[made.group_id].members == ("1", "2", "3", "4", "5")


@pytest.mark.parametrize("manual", [False, True])
def test_redundant_add_is_noop_without_saving_or_promoting(manual):
    owner = app()
    snap = {"jobs": [job("8_1"), job("8_2")]}
    gid = M.create(owner, snap, ["8_1", "8_2"]).group_id if manual else current(owner, snap).for_job("8_1").id
    before = copy.deepcopy(owner.table_state)
    revision = getattr(owner, "manual_job_groups_revision", 0)
    result = M.add(owner, snap, gid, ["8_2", "8_1", "8_1"])
    assert not result.changed and result.group_id == gid
    assert owner.table_state == before
    assert getattr(owner, "manual_job_groups_revision", 0) == revision


@pytest.mark.parametrize("ids", [None, "3", [], ["missing"], ["3", None], ["3", "../4"], ["3", []]])
def test_invalid_or_missing_source_rejects_entire_add(ids):
    owner = app()
    snap = {"jobs": [job(1), job(2), job(3)]}
    made = M.create(owner, snap, ["1", "2"])
    before = copy.deepcopy(owner.table_state)
    assert not M.add(owner, snap, made.group_id, ids).changed
    assert owner.table_state == before


@pytest.mark.parametrize("gid", [None, [], "unknown", "array:404"])
def test_missing_destination_is_atomic(gid):
    owner = app()
    snap = {"jobs": [job(1), job(2), job(3)]}
    before = copy.deepcopy(owner.table_state)
    assert not M.add(owner, snap, gid, ["1"]).changed
    assert owner.table_state == before


@pytest.mark.parametrize("conflicted", ["1", "3"])
@pytest.mark.parametrize("change", [{"submit": "2026-10-09T12:00:00"}, {"cluster": "fedora"}])
def test_conflicting_source_or_destination_attempts_reject_atomically(conflicted, change):
    owner = app()
    records = [job(1), job(2), job(3)]
    snap = {"jobs": records}
    made = M.create(owner, snap, ["1", "2"])
    before = copy.deepcopy(owner.table_state)
    conflict = {**snap, "finished": [replace(next(record for record in records if record.id == conflicted), **change)]}
    assert not M.add(owner, conflict, made.group_id, ["3"]).changed
    assert owner.table_state == before
    if conflicted == "1":
        assert M.target_evidence(owner, conflict, made.group_id) is None


@pytest.mark.parametrize("mutation", ["add", "detach", "attempt", "unknown-replace"])
def test_frozen_destination_rejects_membership_or_attempt_change(mutation):
    owner = app()
    records = [job(jid, submit="" if mutation == "unknown-replace" else "2026-10-08T12:00:00") for jid in range(1, 5)]
    snap = {"jobs": records}
    made = M.create(owner, snap, ["1", "2", "3"])
    expected = M.target_evidence(owner, snap, made.group_id)
    assert expected is not None
    if mutation == "add":
        assert M.add(owner, snap, made.group_id, ["4"]).changed
    elif mutation == "detach":
        assert M.detach(owner, snap, ["3"]).changed
    elif mutation == "attempt":
        records[0].submit = "2026-10-09T12:00:00"
    else:
        records[0] = replace(records[0])
    before = copy.deepcopy(owner.table_state)
    assert not M.add(owner, snap, made.group_id, ["4"], expected=expected).changed
    assert owner.table_state == before


def test_unknown_source_makes_whole_destination_session_only():
    owner = app()
    records = [job(1), job(2), job(3, submit="")]
    snap = {"jobs": records}
    made = M.create(owner, snap, ["1", "2"])
    added = M.add(owner, snap, made.group_id, ["3"])
    assert added.changed and "session" in added.message
    assert not owner.table_state["manual_groups"]["groups"]
    assert current(owner, snap).groups[made.group_id].members == ("1", "2", "3")
    assert made.group_id not in current(app(), snap).groups


def test_destination_accounting_update_keeps_same_label_and_members():
    owner = app()
    records = [job(1), job(2), job(3)]
    snap = {"jobs": records}
    made = M.create(owner, snap, ["1", "2"])
    final = {"jobs": [records[2]], "finished": [Finished(record.id, submit=record.submit, cluster=record.cluster) for record in records[:2]]}
    assert M.add(owner, final, made.group_id, ["3"]).changed
    assert current(owner, final).groups[made.group_id].members == ("1", "2", "3")


def test_capacity_rejection_does_not_steal_source_jobs_or_fold_group(monkeypatch):
    owner = app()
    snap = {"jobs": [job(jid) for jid in range(1, 7)]}
    made = M.create(owner, snap, ["1", "2"])
    assert M.detach(owner, snap, ["6"]).changed
    monkeypatch.setattr(M, "MAX_IDENTITIES", 3)
    before = copy.deepcopy(owner.table_state)
    assert not M.add(owner, snap, made.group_id, ["3"]).changed
    assert owner.table_state == before


def test_group_limit_rejection_is_atomic_for_automatic_promotion(monkeypatch):
    owner = app()
    snap = {"jobs": [job(jid) for jid in ("1_1", "1_2", "8_1", "8_2", "8_3")]}
    M.create(owner, snap, ["8_1", "8_2", "8_3"])
    target = current(owner, snap).for_job("1_1")
    monkeypatch.setattr(M, "MAX_GROUPS", 1)
    before = copy.deepcopy(owner.table_state)
    assert not M.add(owner, snap, target.id, ["8_1"]).changed
    assert owner.table_state == before


def test_bulk_evidence_is_cached_without_scanning_unchanged_pointer_frames(monkeypatch):
    owner = app()
    records = [job(jid) for jid in ("1_1", "1_2", "8_1", "8_2", "8_3")]
    snap = {"jobs": records}
    index = current(owner, snap)
    ids = tuple(index.groups)
    first = M.target_evidences(owner, snap, ids, index=index)
    assert len(first) == 2
    monkeypatch.setattr(M, "selection_tokens", lambda *args: pytest.fail("unchanged frame scanned job provenance"))
    monkeypatch.setattr(G.registry(owner), "ensure", lambda *args: pytest.fail("render evidence repeated inference"))
    for _ in range(100):
        records[0].state = "COMPLETED"
        assert M.target_evidences(owner, snap, ids, index=index) == first
    # The returned dictionary is isolated from internal cached destinations.
    first.clear()
    assert len(M.target_evidences(owner, snap, ids, index=index)) == 2


def test_bulk_evidence_rejects_old_frame_or_incomplete_supplied_tokens():
    owner = app()
    records = [job("1_1"), job("1_2")]
    snap = {"jobs": records}
    index = current(owner, snap)
    ids = tuple(index.groups)
    assert not M.target_evidences(owner, snap, ids, index=index, tokens=M.selection_tokens(snap, ["1_1"]))
    assert not M.target_evidences(owner, {**snap}, ids, index=index)
    assert not M.target_evidences(owner, snap, "invalid", index=index)


def test_bulk_evidence_refreshes_after_reused_id_even_if_auto_group_id_stays():
    owner = app()
    records = [job("1_1"), job("1_2")]
    snap = {"jobs": records}
    before = M.target_evidences(owner, snap, tuple(current(owner, snap).groups))
    records[0].submit = "2026-10-09T12:00:00"
    after = M.target_evidences(owner, snap, tuple(current(owner, snap).groups))
    assert before.keys() == after.keys() and before != after


@pytest.mark.parametrize("bad_label", ["", "\x1b[31mred", "a" * (M.MAX_LABEL + 1), [], None])
def test_saved_label_validation_uses_safe_fallback(bad_label):
    owner = app()
    records = [job(1), job(2)]
    made = M.create(owner, {"jobs": records}, ["1", "2"])
    saved = copy.deepcopy(owner.table_state["manual_groups"])
    saved["groups"][0]["label"] = bad_label
    restored = app()
    restored.table_state["manual_groups"] = saved
    assert current(restored, {"jobs": records}).groups[made.group_id].label == "Manual group 1"


def test_explicit_menu_evidence_includes_all_valid_targets_above_render_budget(monkeypatch):
    owner = app()
    snap = {"jobs": [job(jid) for jid in ("1_1", "1_2", "8_1", "8_2")]}
    index = current(owner, snap)
    monkeypatch.setattr(M, "MAX_GROUPS", 1)
    monkeypatch.setattr(M, "MAX_IDENTITIES", 2)
    assert len(M.target_evidences(owner, snap, tuple(index.groups), index=index)) == 1
    assert len(M.target_evidences(owner, snap, tuple(index.groups), index=index, all_groups=True)) == 2


def test_long_inferred_label_can_be_promoted_without_losing_its_name():
    owner = app()
    records = [job(1), job(2), job(300)]
    records[0].name = records[1].name = "a" * 128
    snap = {"jobs": records}
    group = current(owner, snap).for_job("1")
    assert len(group.label) > 128
    result = M.add(owner, snap, group.id, ["300"])
    assert result.changed and current(owner, snap).groups[result.group_id].label == group.label


def test_random_add_remove_sequence_never_duplicates_or_appends_out_of_order():
    owner = app()
    snap = {"jobs": [job(jid) for jid in range(1, 36)]}
    M.create(owner, snap, ["1", "2"])
    M.create(owner, snap, ["21", "22"])
    rng = random.Random(1729)
    for _ in range(150):
        index = current(owner, snap)
        ids = [str(jid) for jid in rng.sample(range(1, 36), rng.randint(1, 4))]
        if index.groups and rng.random() < .75:
            M.add(owner, snap, rng.choice(tuple(index.groups)), ids)
        else:
            M.detach(owner, snap, ids)
        index = current(owner, snap)
        manual_members = [member for members in G.registry(owner).manual.groups().values() for member in members]
        assert len(manual_members) == len(set(manual_members))
        visible_members = [jid for group in index.groups.values() for jid in group.members]
        assert len(visible_members) == len(set(visible_members))
        for group in index.groups.values():
            if group.kind == "manual":
                assert group.members == tuple(sorted(group.members, key=lambda value: (_natural(value), value)))
        assert [record.id for record in G.project_records(owner, snap, snap["jobs"])] == [str(jid) for jid in range(1, 36)]
