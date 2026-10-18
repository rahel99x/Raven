"""Adversarial legacy records preserve facts without fabricating coverage."""
from types import SimpleNamespace

import pytest
from hypothesis import given, strategies as st

from raven.model import Job, Store
from raven.sampler import Sampler
from raven.slurm import CommandError, ParseDiagnostics, Slurm, parse_jobs, parse_kv, parse_sacct


def row(jid="7", name="train", state="PENDING", cpus="4"):
    return "|".join([jid, name, "main", state, "00:00:00", "01:00:00", "1", cpus,
                     "N/A", "(Priority)", "1G", "N/A", "2026-10-01T12:00:00", "Priority", "1"])


class Backend:
    def __init__(self, text):
        self.text = text
    def run(self, command, timeout):
        return self.text, 0


def test_kv_keeps_spaces_and_embedded_assignments_without_invented_quoting():
    fields = parse_kv("JobId=7 JobName=a training run UserId=alice(1000) Reason=bad dependency code=4 "
                      "RunTime=00:00:01 TimeLimit=01:00:00 Command=env EXPERIMENT=a=b python train.py --x=a=b "
                      "WorkDir=/scratch/my run StdOut=/scratch/my run/out")
    assert fields["JobName"] == "a training run"
    assert fields["Reason"] == "bad dependency code=4"
    assert fields["Command"] == "env EXPERIMENT=a=b python train.py --x=a=b"
    assert fields["WorkDir"] == "/scratch/my run"
    assert fields["StdOut"] == "/scratch/my run/out"
    assert fields["TimeLimit"] == "01:00:00"
    assert "code" not in fields


@pytest.mark.parametrize("text", ["JobId=7 JobId=8", "JobId=7\nJobId=8", "JobId=7\0"])
def test_ambiguous_identity_is_not_usable(text):
    assert "JobId" not in parse_kv(text)


def test_duplicate_lease_fields_never_use_last_value():
    report = ParseDiagnostics("details")
    fields = parse_kv("JobId=7 TimeLimit=00:00:10 Command=env TimeLimit=99:00:00", report)
    assert "TimeLimit" not in fields
    assert report.ambiguous == 1


@pytest.mark.parametrize("corrupt", ["7|short", row(cpus="NaN"), row(cpus="inf"), row(cpus="1.5"),
                                      row(name="bad|shift"), row(jid="--danger"), row(state="")])
def test_bad_queue_is_not_successful_empty_queue(corrupt):
    slurm = Slurm(Backend(corrupt), "alice")
    with pytest.raises(CommandError, match="last trustworthy snapshot"):
        slurm.jobs()
    assert slurm.parse_diagnostics["jobs"]["total_errors"] == 1


def test_partial_queue_keeps_missing_jobs_and_all_invalid_keeps_last_snapshot():
    backend = Backend(row("7"))
    slurm = Slurm(backend, "alice")
    store = Store(persist=False)
    sampler = Sampler(slurm, store, {}, [], workers=1)
    try:
        sampler.run_source("jobs")
        backend.text = row("8") + "\n7|truncated"
        sampler.run_source("jobs")
        assert {job.id for job in store.jobs} == {"7", "8"}
        health = sampler.health("jobs")
        assert "1 rejected" in health.error and health.backoff == 0
        assert not store.departed_jobs
        backend.text = "7|truncated"
        sampler.run_source("jobs")
        assert {job.id for job in store.jobs} == {"7", "8"}
        assert health.backoff > 0
        backend.text = ""
        sampler.run_source("jobs")
        assert store.jobs == [] and not health.error
    finally:
        sampler.shutdown()


def test_duplicate_queue_ids_do_not_choose_arbitrary_row():
    report = ParseDiagnostics("jobs")
    assert parse_jobs(row() + "\n" + row(name="other"), report) == []
    assert report.rejected == 1


@given(st.text(alphabet="abcdefghijklmnopqrstuvwxyz 0123456789_-=", min_size=1, max_size=100))
def test_free_text_without_field_boundary_survives(value):
    value = value.strip()
    assert parse_kv("Command=" + value + " WorkDir=/tmp/run")["Command"] == value


@given(st.lists(st.integers(min_value=1, max_value=999999), min_size=0, max_size=40, unique=True))
def test_whitespace_and_row_order_do_not_change_queue_facts(ids):
    text = "\n".join(row(str(jid)) for jid in ids)
    first = parse_jobs(text)
    second = parse_jobs("\n\n" + "\n\n".join(reversed(text.splitlines())) + "\n")
    assert {job.id: (job.cpus, job.state) for job in first} == {job.id: (job.cpus, job.state) for job in second}


def test_helper_rejects_ambiguous_or_missing_lease_before_worker_admission():
    from raven.raven_allocation_helper import Supervisor
    supervisor = object.__new__(Supervisor)
    calls = []
    supervisor.client = SimpleNamespace(provide_placement=lambda *args, **kwargs: calls.append((args, kwargs)))
    supervisor.cancelled = False
    supervisor.request_id = "request"
    supervisor.generation = 1
    supervisor.plan = {"resources": {"wall_seconds": 100}}
    supervisor.handle({"request_id": "request", "generation": 1, "op": "placement",
                       "records": {"job_record": "JobId=7 TimeLimit=00:00:10 TimeLimit=99:00:00 RunTime=00:00:01"}})
    assert calls and "lease fields" in calls[0][1]["error"]
