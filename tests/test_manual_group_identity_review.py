"""Independent lifecycle and cache audit for presentation-only manual groups."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from raven import job_groups, manual_job_groups as manual
from raven.model import Finished, Job


SUBMIT = "2026-10-09T09:00:00"
LATER = "2026-10-09T10:00:00"


def app():
    return SimpleNamespace(table_state={"groups": True, "collapsed": []})


def record(jid, *, submit=SUBMIT, cluster="alpha"):
    return Job(jid, "train", "gpu", "RUNNING", submit=submit, cluster=cluster,
               user="researcher", account="science", workdir="/project", command="run.sh")


def snapshot(*records):
    return {"jobs": list(records), "finished": [], "details": {}}


def members(index, jid):
    group = index.for_job(jid)
    return set(group.members) if group else set()


def test_group_survives_queue_departure_and_accounting_record_replacement():
    owner = app()
    first, second = record("101"), record("109")
    snap = snapshot(first, second)
    created = manual.create(owner, snap, [first.id, second.id])
    assert created.changed
    registry = job_groups.registry(owner)
    departed = replace(first)
    snap.update(jobs=[second], departed_jobs={first.id: departed})
    assert members(registry.ensure(snap), first.id) == {first.id, second.id}
    # Accounting may omit the cluster already proven for this submission.
    finished = Finished(first.id, first.name, "COMPLETED", submit=first.submit)
    snap.update(departed_jobs={}, finished=[finished])
    assert registry.ensure(snap).for_job(first.id).id == created.group_id


@pytest.mark.parametrize("replacement", [False, True])
def test_detached_identity_does_not_leak_to_reused_scheduler_id(replacement):
    owner = app()
    first, second, third = (record("500_" + str(index)) for index in range(1, 4))
    snap = snapshot(first, second, third)
    registry = job_groups.registry(owner)
    assert len(members(registry.ensure(snap), first.id)) == 3
    assert manual.detach(owner, snap, [first.id]).changed
    assert registry.ensure(snap).for_job(first.id) is None
    if replacement:
        snap["jobs"][0] = replace(first, submit=LATER)
    else:
        first.submit = LATER
    assert members(registry.ensure(snap), first.id) == {first.id, second.id, third.id}


@pytest.mark.parametrize("field,value", [
    ("name", "other application"),
    ("user", "someone_else"),
    ("account", "other_account"),
    ("cluster", "other_cluster"),
    ("start", LATER),
    ("submit", LATER),
])
def test_unknown_submission_identity_rechecks_every_signature_field(field, value):
    owner = app()
    first, second = record("11", submit=""), record("90", submit="")
    snap = snapshot(first, second)
    assert manual.create(owner, snap, [first.id, second.id]).changed
    registry = job_groups.registry(owner)
    assert len(members(registry.ensure(snap), first.id)) == 2
    setattr(first, field, value)
    assert registry.ensure(snap).for_job(first.id) is None
    assert registry.index.for_job(second.id) is None


@pytest.mark.parametrize("detached", [False, True])
@pytest.mark.parametrize("clusters", [("beta",), ("", "beta")])
def test_session_choices_recheck_details_cluster_without_forgetting_omissions(detached, clusters):
    owner = app()
    records = [record("500_" + str(index), submit="", cluster="") for index in range(1, 4)]
    first, second, third = records
    snap = snapshot(*records)
    snap["details"] = {item.id: {"Cluster": "alpha"} for item in records}
    registry = job_groups.registry(owner)
    if detached:
        assert manual.detach(owner, snap, [first.id]).changed
    else:
        assert manual.create(owner, snap, [first.id, second.id]).changed
    before = registry.ensure(snap)
    original = before.for_job(first.id)
    if detached:
        assert original is None
    else:
        assert original is not None and original.kind == "manual"

    for cluster in clusters:
        # Only supplemental provenance changes: the exact record objects and
        # every field in their ambiguous-attempt signatures remain unchanged.
        for details in snap["details"].values():
            details["Cluster"] = cluster
        current = registry.ensure(snap)
        if not cluster:
            if detached:
                assert current.for_job(first.id) is None
            else:
                assert current.for_job(first.id).id == original.id
                assert members(current, first.id) == {first.id, second.id}
        else:
            # A contradictory cluster invalidates either kind of override;
            # the now-unclaimed records can resume ordinary array grouping.
            resumed = current.for_job(first.id)
            assert resumed is not None and resumed.kind == "array"
            assert members(current, first.id) == {first.id, second.id, third.id}


@pytest.mark.parametrize("detached", [False, True])
def test_ambiguous_record_replacement_never_inherits_session_decision(detached):
    owner = app()
    first, second, third = (record("500_" + str(index), submit="") for index in range(1, 4))
    snap = snapshot(first, second, third)
    registry = job_groups.registry(owner)
    if detached:
        assert manual.detach(owner, snap, [first.id]).changed
    else:
        assert manual.create(owner, snap, [first.id, second.id]).changed
    snap["jobs"][0] = replace(first)
    result = registry.ensure(snap)
    assert result.for_job(first.id).kind == "array"
    assert (second.id in members(result, first.id)) is detached


def test_saved_group_survives_cold_start_with_exact_identity_only():
    owner = app()
    first, second = record("11"), record("90")
    snap = snapshot(first, second)
    created = manual.create(owner, snap, [first.id, second.id])
    restored = app()
    restored.table_state["manual_groups"] = manual.validate_state(owner.table_state["manual_groups"])
    registry = job_groups.registry(restored)
    fresh = snapshot(replace(first), replace(second))
    assert registry.ensure(fresh).for_job(first.id).id == created.group_id
    fresh["jobs"][0].cluster = "unrelated_cluster"
    assert registry.ensure(fresh).for_job(first.id) is None


def test_ordinary_state_updates_do_not_rebuild_manual_or_automatic_indices():
    owner = app()
    first, second = record("11"), record("90")
    snap = snapshot(first, second)
    assert manual.create(owner, snap, [first.id, second.id]).changed
    registry = job_groups.registry(owner)
    previous = registry.ensure(snap)
    counts = registry.inference_count, registry.manual.overlay_count
    for state in ("RUNNING", "COMPLETING", "COMPLETED"):
        first.state = state
        first.elapsed = "20:00"
        first.reason = "Resources"
        current = registry.ensure(dict(snap, jobs=list(snap["jobs"])))
        assert current is previous
        assert (registry.inference_count, registry.manual.overlay_count) == counts
        assert job_groups.status_counts([first])["running" if state != "COMPLETED" else "completed"] == 1


def test_manual_override_preserves_unrelated_automatic_group_and_members():
    owner = app()
    originals = [record(f"{batch}_{index}") for batch in (400, 500, 600) for index in range(1, 5)]
    snap = snapshot(*originals)
    registry = job_groups.registry(owner)
    before = registry.ensure(snap)
    untouched = before.for_job("600_1")
    assert manual.create(owner, snap, ["400_2", "500_2"]).changed
    after = registry.ensure(snap)
    assert after.for_job("600_1") is untouched
    assert members(after, "400_1") == {"400_1", "400_3", "400_4"}
    assert members(after, "500_1") == {"500_1", "500_3", "500_4"}
    assert members(after, "400_2") == {"400_2", "500_2"}
    # A new array member must join the automatic remainder, never the manual pair.
    snap["jobs"].append(record("400_5"))
    updated = registry.ensure(snap)
    assert members(updated, "400_1") == {"400_1", "400_3", "400_4", "400_5"}
    assert members(updated, "400_2") == {"400_2", "500_2"}


def test_reload_replaces_overlay_without_mutating_old_published_index():
    owner = app()
    snap = snapshot(record("11"), record("90"))
    assert manual.create(owner, snap, ["11", "90"]).changed
    registry = job_groups.registry(owner)
    before = registry.ensure(snap)
    owner.table_state["manual_groups"] = {"version": 1, "groups": [], "detached": []}
    current = job_groups.registry(owner).ensure(snap)
    assert current.for_job("11") is None
    assert members(before, "11") == {"11", "90"}


def test_disappeared_records_release_identity_references():
    owner = app()
    snap = snapshot(record("11"), record("90"))
    assert manual.create(owner, snap, ["11", "90"]).changed
    registry = job_groups.registry(owner)
    registry.ensure(snapshot())
    assert not registry.manual._identities
    # Preferences survive absence, so later accounting of this exact attempt works.
    assert members(registry.ensure(snap), "11") == {"11", "90"}


def test_rejected_stale_selection_is_atomic_for_all_existing_groups():
    owner = app()
    snap = snapshot(record("11"), record("90"), record("700_1"), record("700_2"))
    assert manual.create(owner, snap, ["11", "90"]).changed
    registry = job_groups.registry(owner)
    before = registry.ensure(snap)
    saved = manual.validate_state(owner.table_state["manual_groups"])
    result = manual.create(owner, snap, ["11", "missing"])
    assert not result.changed
    assert owner.table_state["manual_groups"] == saved
    assert registry.ensure(snap) is before
    rejected = manual.detach(owner, snap, ["11"], ["array:does-not-exist"])
    assert not rejected.changed
    assert owner.table_state["manual_groups"] == saved
    assert registry.ensure(snap) is before


@pytest.mark.parametrize("blank_source", ["finished", "departed_jobs"])
def test_missing_cluster_cannot_hide_conflicting_provenance_between_sources(blank_source):
    owner = app()
    old = record("11", cluster="alpha")
    current = replace(old, cluster="beta")
    omitted = replace(old, cluster="")
    snap = snapshot(current, record("90", cluster="beta"))
    snap["group"] = [old]
    snap[blank_source] = {old.id: omitted} if blank_source == "departed_jobs" else [omitted]
    assert manual.selection_tokens(snap)[old.id] is None
    assert not manual.create(owner, snap, ["11", "90"]).changed
    assert "manual_groups" not in owner.table_state


@pytest.mark.parametrize("with_registry", [False, True])
@pytest.mark.parametrize("mutation", ["in_place_submit", "in_place_start", "replacement",
                                     "details", "malformed_submit_with_valid_details", "duplicate_cluster"])
def test_cached_provenance_matches_fresh_tokens_after_source_mutation(with_registry, mutation):
    first, second = record("11", submit=""), record("90")
    snap = snapshot(first, second)
    if mutation == "malformed_submit_with_valid_details":
        first.submit = "invalid-date"
        snap["details"][first.id] = {"SubmitTime": SUBMIT}
    if mutation == "duplicate_cluster":
        first.submit = SUBMIT
        snap["group"] = [replace(first)]
        snap["finished"] = [replace(first, cluster="")]
    registry, cache = job_groups.Registry(), manual.SelectionTokenCache()

    def cached():
        if with_registry:
            registry.ensure(snap)
        return cache.update(snap, [first.id, second.id], registry=registry if with_registry else None)

    before = cached()
    if mutation == "in_place_submit":
        first.submit = LATER
    elif mutation == "in_place_start":
        first.start = LATER
    elif mutation in ("replacement", "malformed_submit_with_valid_details"):
        snap["jobs"][0] = replace(first)
    elif mutation == "details":
        snap["details"][first.id] = {"SubmitTime": SUBMIT}
    elif mutation == "duplicate_cluster":
        snap["group"][0].cluster = "other_cluster"
    actual = cached()
    expected = manual.selection_tokens(snap, [first.id, second.id])
    assert actual == expected
    assert actual != before
    assert cache.build_count == 2


@pytest.mark.parametrize("with_registry", [False, True])
def test_cached_provenance_reuses_unchanged_new_snapshots_and_follows_requested_subset(with_registry):
    first, second = record("11"), record("90")
    registry, cache = job_groups.Registry(), manual.SelectionTokenCache()
    snap = snapshot(first, second)

    def cached(source, ids):
        if with_registry:
            registry.ensure(source)
        return cache.update(source, ids, registry=registry if with_registry else None)

    before = cached(snap, [first.id, second.id])
    first.state = "COMPLETED"
    first.elapsed = "01:00:00"
    assert cached(snapshot(first, second), [second.id, first.id]) is before
    assert cache.build_count == 1
    assert cached(snapshot(first, second), [first.id]) == {first.id: before[first.id]}
    assert cache.build_count == 2
    assert cached(snapshot(first, second), [first.id, "missing"])["missing"] is None


def test_cached_provenance_does_not_retain_removed_records_as_current():
    first, second = record("11"), record("90")
    registry, cache = job_groups.Registry(), manual.SelectionTokenCache()
    snap = snapshot(first, second)
    registry.ensure(snap)
    cache.update(snap, [first.id, second.id], registry=registry)
    snap["jobs"] = [second]
    registry.ensure(snap)
    assert cache.update(snap, [first.id, second.id], registry=registry)[first.id] is None
