"""Observed launch identity survives queue departure and accounting updates."""

from dataclasses import replace

import pytest

from raven.job_groups import Registry
from raven.model import Finished, Job, Store


def pipeline():
    return [
        Job("100", "prepare", "cpu", "RUNNING", submit="2026-10-08T12:00:00",
            user="alex", account="lab", workdir="/project", command="/project/prepare.sh"),
        Job("101", "train", "cpu", "PENDING", submit="2026-10-08T12:00:01",
            user="alex", account="lab", workdir="/project", command="/project/train.sh",
            dependency="afterok:100"),
        Job("102", "evaluate", "cpu", "PENDING", submit="2026-10-08T12:00:02",
            user="alex", account="lab", workdir="/project", command="/project/evaluate.sh",
            dependency="afterany:101"),
    ]


def completed(job, state="COMPLETED", *, provenance=True):
    return Finished(job.id, job.name, state, submit=job.submit, workdir=job.workdir,
                    user=job.user if provenance else "", account=job.account if provenance else "",
                    end="2026-10-08T12:10:00")


@pytest.mark.parametrize("terminal", ["COMPLETED", "CANCELLED", "FAILED"])
def test_pipeline_fold_survives_cleared_dependencies_departure_and_accounting(terminal):
    jobs = pipeline()
    unrelated = replace(jobs[0], id="900", name="unrelated", command="/other/run.sh")
    store, registry = Store(persist=False), Registry()
    store.apply_jobs([*jobs, unrelated])
    original = registry.ensure(store.snapshot()).for_job("100")
    assert original.kind == "dependency"
    assert registry.fold(original.id, True)
    history = []
    for index, job in enumerate(jobs):
        remaining = [replace(record, state="RUNNING", dependency="") if position == index + 1 else record
                     for position, record in enumerate(jobs) if position > index]
        store.apply_jobs([*remaining, unrelated])
        snapshot = store.snapshot()
        assert job.id in snapshot["departed_jobs"]
        group = registry.ensure(snapshot).for_job(job.id)
        assert group.id == original.id and group.members == ("100", "101", "102")
        history.append(completed(job, terminal))
        store.apply_finished(history)
        group = registry.ensure(store.snapshot()).for_job(job.id)
        assert group.id == original.id and registry.is_collapsed(group)
        assert registry.index.for_job("900") is None
    assert {record.state for record in store.snapshot()["finished"]} == {terminal}


def test_satisfied_dependency_retention_does_not_change_waiting_state():
    first, second = pipeline()[:2]
    registry = Registry()
    original = registry.ensure({"jobs": [first, second]}).for_job(first.id)
    running = replace(second, state="RUNNING", dependency="")
    group = registry.ensure({"jobs": [first, running]}).for_job(first.id)
    assert group.id == original.id
    assert running.dependency == "" and running.state == "RUNNING"


@pytest.mark.parametrize("changes", [
    {"submit": "2026-10-08T12:00:05"}, {"submit": ""},
    {"user": "other"}, {"account": "other-lab"}, {"cluster": "other-cluster"},
    {"workdir": "/other-project"}, {"workdir": ""},
    {"dependency": "afterok:999"},
])
def test_changed_submission_scope_or_dependency_does_not_inherit_old_edge(changes):
    first, second = pipeline()[:2]
    registry = Registry()
    assert registry.ensure({"jobs": [first, second]}).groups
    current = replace(second, **{"state": "RUNNING", "dependency": "", **changes})
    assert not registry.ensure({"jobs": [first, current]}).groups


def test_new_dependency_replaces_the_observed_predecessor():
    first, second = pipeline()[:2]
    different = replace(first, id="103", name="other-preparation", command="/project/other.sh")
    registry = Registry()
    assert registry.ensure({"jobs": [first, second, different]}).for_job(first.id).members == ("100", "101")
    current = replace(second, dependency="afterok:103")
    index = registry.ensure({"jobs": [first, current, different]})
    assert index.for_job(first.id) is None
    assert index.for_job(second.id).members == ("103", "101")


@pytest.mark.parametrize("changes", [
    {"submit": "2026-10-08T12:00:05"}, {"submit": ""},
    {"user": "other"}, {"account": "other-lab"}, {"workdir": ""},
])
def test_accounting_type_transition_does_not_inherit_another_submission_edge(changes):
    first, second = pipeline()[:2]
    registry = Registry()
    assert registry.ensure({"jobs": [first, second]}).groups
    record = replace(completed(second), **changes)
    assert not registry.ensure({"jobs": [first], "finished": [record]}).groups


def test_repeated_legacy_accounting_omissions_survive_command_backfill():
    first = pipeline()[0]
    second = replace(first, id="101")
    registry = Registry()
    original = registry.ensure({"jobs": [first, second]}).for_job(first.id)
    record = completed(first, provenance=False)
    snapshot = {"jobs": [second], "finished": [record]}
    assert registry.ensure(snapshot).for_job(first.id).id == original.id
    record.command = first.command
    assert registry.ensure(snapshot).for_job(first.id).id == original.id
    record.comment = "accounting was amended"
    assert registry.ensure(snapshot).for_job(first.id).id == original.id


@pytest.mark.parametrize("changes", [
    {"submit": "2026-10-08T12:00:05"}, {"workdir": ""},
    {"user": "other"}, {"account": "other-lab"},
])
def test_accounting_identity_corrections_invalidate_retained_provenance(changes):
    first = pipeline()[0]
    second = replace(first, id="101")
    registry = Registry()
    assert registry.ensure({"jobs": [first, second]}).groups
    record = completed(first, provenance=False)
    snapshot = {"jobs": [second], "finished": [record]}
    assert registry.ensure(snapshot).groups
    snapshot["finished"] = [replace(record, **changes)]
    assert not registry.ensure(snapshot).groups


@pytest.mark.parametrize("field", ["user", "account"])
def test_explicit_accounting_provenance_clear_is_not_treated_as_an_omission(field):
    first = pipeline()[0]
    second = replace(first, id="101")
    registry = Registry()
    assert registry.ensure({"jobs": [first, second]}).groups
    record = completed(first)
    snapshot = {"jobs": [second], "finished": [record]}
    assert registry.ensure(snapshot).groups
    setattr(record, field, "")
    assert not registry.ensure(snapshot).groups


@pytest.mark.parametrize("field", ["WorkDir", "UserId", "Account"])
def test_deleted_authoritative_details_are_not_revived_by_accounting_omission(field):
    first = pipeline()[0]
    second = replace(first, id="101")
    registry = Registry()
    assert registry.ensure({"jobs": [first, second]}).groups
    record = completed(first, provenance=False)
    if field == "WorkDir":
        record.workdir = ""
    details = {"WorkDir": "/project", "UserId": "alex", "Account": "lab"}
    snapshot = {"jobs": [second], "finished": [record], "details": {first.id: details}}
    assert registry.ensure(snapshot).groups
    del details[field]
    assert not registry.ensure(snapshot).groups
