"""Native-format contract fixtures, not live scheduler qualification."""

from __future__ import annotations

import copy
import json
import subprocess
import sys

import pytest

from raven import evidence as E
from raven.schedulers import create
from raven.schedulers.batch_common import memory_bytes, stdin_submission
from raven.schedulers.flux import canonical_job_id
from raven.schedulers.pbs import submission_time
from raven.transport_errors import CommandError


PBS = {
    "timestamp": 1792929601,
    "pbs_version": "23.06.06",
    "pbs_server": "pbs.example",
    "Jobs": {
        "123[4].pbs.example": {
            "Job_Name": "experiment",
            "Job_Owner": "alice@login.example",
            "job_state": "R",
            "queue": "workq",
            "Account_Name": "LabX",
            "qtime": "Sun Oct 25 12:00:01 2026",
            "run_count": 2,
            "Resource_List": {"ncpus": 8, "nodect": 1, "mem": "8gb", "ngpus": 1},
            "resources_used": {"cput": "00:01:02", "mem": "32kb"},
        }
    },
}
CONDOR = [
    {
        "ClusterId": 123,
        "ProcId": 4,
        "GlobalJobId": "schedd.example#123.4#1792929501",
        "Owner": "alice",
        "QDate": 1792929501,
        "NumJobStarts": 2,
        "JobStatus": 2,
        "JobBatchName": "experiment",
        "RequestCpus": 2,
        "RequestMemory": 2048,
        "RequestGpus": 1,
        "AccountingGroup": "LabX",
    }
]
LSF = {
    "COMMAND": "bjobs",
    "JOBS": 1,
    "RECORDS": [
        {
            "JOBID": "123",
            "JOBINDEX": "4",
            "USER": "alice",
            "STAT": "RUN",
            "QUEUE": "normal",
            "JOB_NAME": "experiment",
            "SUBMIT_TIME": "2026/10/25 12:00:01",
            "NALLOC_SLOT": "8",
            "PROJ_NAME": "LabX",
            "EXEC_HOST": "8*compute01",
        }
    ],
}
FLUX = {
    "jobs": [
        {
            "id": 58,
            "jobid": "ƒ21",
            "userid": 1000,
            "username": "alice",
            "t_submit": 1792929501.125,
            "state": "RUN",
            "status": "RUN",
            "name": "experiment",
            "ncores": 8,
            "nnodes": 2,
            "bank": "LabX",
            "queue": "batch",
            "urgency": 16,
            "exception": {"occurred": False},
        }
    ]
}
DOCUMENTS = {
    "openpbs": PBS,
    "pbspro": PBS,
    "htcondor": CONDOR,
    "lsf": LSF,
    "flux": FLUX,
}
MUTATIONS = {
    "qdel",
    "qhold",
    "qrls",
    "condor_rm",
    "condor_hold",
    "condor_release",
    "bkill",
    "bstop",
    "bresume",
    "cancel",
}


class NativeFixture:
    def __init__(self, scheduler):
        self.scheduler = scheduler
        self.document = copy.deepcopy(DOCUMENTS[scheduler])
        self.calls = []
        self.script = (
            "#!/bin/sh\n# native directives remain unchanged\nprintf 'hello\\n'\n"
        )
        self.fail_mutation = False
        self.empty_queue = False

    def inspect_command(self, argv, timeout, limit, cancel=None, *, depth=0):
        self.calls.append(list(argv))
        if argv[0] == "qselect":
            return "".join(
                job_id + "\n" for job_id in self.document["Jobs"] if "[]" not in job_id
            )
        if argv[0] == "sh":
            if "cat --" in argv[2]:
                return self.script
            if self.fail_mutation:
                raise CommandError("Command timed out; reconcile native state")
            return {
                "openpbs": "124.pbs.example\n",
                "pbspro": "124.pbs.example\n",
                "htcondor": "124.0 - 124.2\n",
                "lsf": "Job <124> is submitted to queue <normal>.\n",
                "flux": "ƒ21\n",
            }[self.scheduler]
        if any(item in MUTATIONS for item in argv):
            if self.fail_mutation:
                raise CommandError("Command timed out; reconcile native state")
            return ""
        document = self.document
        if argv[0] == "qstat":
            ids = [value for value in argv if value in document["Jobs"]]
            if ids:
                document = {
                    **document,
                    "Jobs": {
                        key: value
                        for key, value in document["Jobs"].items()
                        if key in ids
                    },
                }
        if (
            self.scheduler == "flux"
            and "jobs" in argv
            and not any(item.startswith("--count") for item in argv)
        ):
            document = document["jobs"][0]
        if self.scheduler == "htcondor" and argv[0] == "condor_q" and self.empty_queue:
            document = []
        raw = json.dumps(document)
        if len(raw.encode()) > limit:
            raise CommandError("Command output exceeded the inspection limit")
        return raw


def adapter(name, tmp_path=None, **limits):
    backend = NativeFixture(name)
    scope = {"cluster": "lab-site", "user": "alice"}
    if name == "flux":
        scope["uri"] = "local:///run/flux/lab-instance"
    if name == "htcondor":
        scope["schedd"] = "schedd.example"
    kwargs = {"journal_dir": str(tmp_path / "journal")} if tmp_path else {}
    return create(name, backend, scope, **kwargs, **limits), backend


@pytest.mark.parametrize("name", DOCUMENTS)
def test_native_format_jobs_details_units_and_no_implicit_mutation(name):
    scheduler, backend = adapter(name)
    (job,) = scheduler.jobs()
    assert job["user"] == "alice"
    assert job["state"] == "RUNNING"
    assert job["account"] == "LabX"
    assert job["identity"]["scheduler"] == name
    assert job["identity"]["scope"] == scheduler.scope_key
    assert scheduler.detail(job["identity"]) == job
    assert not any(any(item in MUTATIONS for item in argv) for argv in backend.calls)
    assert scheduler.capabilities()["limits"]["max_inflight"] == 1
    assert scheduler.capabilities()["qualification"].startswith("fixture-tested")


@pytest.mark.parametrize("name", DOCUMENTS)
def test_review_apply_changes_one_exact_job_and_token_cannot_be_replayed(
    name, tmp_path
):
    scheduler, backend = adapter(name, tmp_path)
    (job,) = scheduler.jobs()
    plan = scheduler.review("cancel", job["identity"])
    assert plan["target_user"] == "alice"
    assert not any(any(item in MUTATIONS for item in argv) for argv in backend.calls)
    receipt = scheduler.apply(plan, plan["digest"])
    assert receipt["status"] == "accepted"
    assert receipt["identity"] == job["identity"]
    mutations = [
        argv for argv in backend.calls if any(item in MUTATIONS for item in argv)
    ]
    assert len(mutations) == 1
    with pytest.raises(ValueError, match="already attempted"):
        scheduler.apply(plan, plan["digest"])
    assert (
        len([argv for argv in backend.calls if any(item in MUTATIONS for item in argv)])
        == 1
    )


@pytest.mark.parametrize("name", DOCUMENTS)
def test_failed_action_consumes_review_without_automatic_retry(name, tmp_path):
    scheduler, backend = adapter(name, tmp_path)
    (job,) = scheduler.jobs()
    plan = scheduler.review("cancel", job["identity"])
    backend.fail_mutation = True
    result = scheduler.apply(plan, plan["digest"])
    assert result["status"] == "unknown"
    assert "reconcile" in result["error"]
    assert "token is consumed" in result["recovery"]
    with pytest.raises(ValueError, match="already attempted"):
        scheduler.apply(plan, plan["digest"])


@pytest.mark.parametrize("name", DOCUMENTS)
def test_owner_change_between_review_and_apply_is_refused(name, tmp_path):
    scheduler, backend = adapter(name, tmp_path)
    (job,) = scheduler.jobs()
    plan = scheduler.review("cancel", job["identity"])
    if name in {"openpbs", "pbspro"}:
        backend.document["Jobs"][job["id"]]["Job_Owner"] = "bob@login.example"
    elif name == "htcondor":
        backend.document[0]["Owner"] = "bob"
    elif name == "lsf":
        backend.document["RECORDS"][0]["USER"] = "bob"
    else:
        backend.document["jobs"][0]["username"] = "bob"
    with pytest.raises(ValueError, match="ownership|owner"):
        scheduler.apply(plan, plan["digest"])
    assert not any(any(item in MUTATIONS for item in argv) for argv in backend.calls)


@pytest.mark.parametrize("name", DOCUMENTS)
def test_attempt_change_between_review_and_apply_is_refused(name, tmp_path):
    scheduler, backend = adapter(name, tmp_path)
    (job,) = scheduler.jobs()
    plan = scheduler.review("cancel", job["identity"])
    if name in {"openpbs", "pbspro"}:
        backend.document["Jobs"][job["id"]]["run_count"] += 1
    elif name == "htcondor":
        backend.document[0]["NumJobStarts"] += 1
    elif name == "lsf":
        backend.document["RECORDS"][0]["SUBMIT_TIME"] = "2026/10/25 12:00:02"
    else:
        backend.document["jobs"][0]["t_submit"] += 1
    with pytest.raises(ValueError, match="missing, stale|attempt"):
        scheduler.apply(plan, plan["digest"])
    assert not any(any(item in MUTATIONS for item in argv) for argv in backend.calls)


@pytest.mark.parametrize("name", DOCUMENTS)
def test_scope_mismatch_does_not_query_or_change_a_job(name):
    scheduler, backend = adapter(name)
    (job,) = scheduler.jobs()
    identity = {**job["identity"], "scope": "other-cluster"}
    count = len(backend.calls)
    with pytest.raises(ValueError, match="scope"):
        scheduler.detail(identity)
    assert len(backend.calls) == count


@pytest.mark.parametrize("name", DOCUMENTS)
def test_native_output_byte_bound_is_enforced(name):
    scheduler, _ = adapter(name, max_bytes=32)
    with pytest.raises(CommandError, match="output exceeded"):
        scheduler.jobs()


@pytest.mark.parametrize("name", DOCUMENTS)
def test_unknown_submission_fields_and_native_options_are_not_ignored(name):
    scheduler, _ = adapter(name)
    for extra in ({"surprise": True}, {"native_options": ["--dangerous"]}):
        with pytest.raises(ValueError):
            scheduler._submission({"script": "/project/job", **extra})


@pytest.mark.parametrize("name", DOCUMENTS)
def test_submission_streams_verified_bytes_and_preserves_file(name, tmp_path):
    scheduler, backend = adapter(name, tmp_path)
    settings = {
        "script": "/project/job",
        "cpus": 4,
        "account": "LabX",
        "name": "experiment",
    }
    plan = scheduler.review("submit", submission=settings)
    original = backend.script
    receipt = scheduler.apply(plan, plan["digest"])
    assert receipt["status"] == "accepted"
    assert receipt["native_id"] in {"124.pbs.example", "124.0", "124", "58"}
    invocation = backend.calls[-1]
    assert invocation[0] == "sh"
    assert original in invocation
    assert "/project/job" not in invocation
    assert backend.script == original
    assert not any("native directives" in str(value) for value in plan.values())


@pytest.mark.parametrize("name", DOCUMENTS)
def test_changed_script_is_not_submitted(name, tmp_path):
    scheduler, backend = adapter(name, tmp_path)
    plan = scheduler.review("submit", submission={"script": "/project/job"})
    backend.script += "\nprintf 'changed'\n"
    with pytest.raises(ValueError, match="script changed"):
        scheduler.apply(plan, plan["digest"])
    assert not list((tmp_path / "journal").glob("*.claimed"))


def test_pbs_server_identity_subjobs_and_memory_are_preserved_without_double_counting():
    scheduler, backend = adapter("openpbs")
    backend.document["Jobs"]["123[].pbs.example"] = {"job_state": "B"}
    (job,) = scheduler.jobs()
    assert job["id"] == "123[4].pbs.example"
    assert job["resources"]["memory"] == {"requested": 8 * 1024**3, "unit": "B"}
    assert "-t" in backend.calls[-1]
    assert job["native"]["owner"] == "alice@login.example"
    assert memory_bytes("123kb") == 123 * 1024
    with pytest.raises(ValueError, match="explicit native unit"):
        memory_bytes(8192)


def test_pbs_missing_restart_count_stays_unknown_and_disables_action():
    scheduler, backend = adapter("openpbs")
    del backend.document["Jobs"]["123[4].pbs.example"]["run_count"]
    (job,) = scheduler.jobs()
    assert job["identity"]["attempt"].endswith("run_count=unknown")
    assert job["native"]["run_count"] is None
    with pytest.raises(ValueError, match="authoritative precision"):
        scheduler.review("cancel", job["identity"])


def test_pbs_native_ctime_identity_has_seconds_year_and_no_guessed_timezone():
    assert submission_time("Sun Oct 25 12:00:01 2026") == "Sun Oct 25 12:00:01 2026"
    assert submission_time("Sun Oct  4 12:00:01 2026") == "Sun Oct  4 12:00:01 2026"
    assert submission_time(1792929601) == "1792929601"
    for value in ("Oct 25 12:00", "Sun Oct 25 12:00:01", "Sun Feb 30 12:00:01 2026", 0):
        with pytest.raises(ValueError):
            submission_time(value)


def test_pbs_empty_user_selection_never_falls_back_to_all_user_polling():
    scheduler, backend = adapter("openpbs")
    backend.document["Jobs"] = {}
    assert scheduler.jobs() == []
    assert backend.calls == [["qselect", "-u", "alice", "-T"]]


def test_pbs_history_excludes_active_jobs_and_keeps_finished_state():
    scheduler, backend = adapter("openpbs")
    assert scheduler.jobs(history=True) == []
    job = backend.document["Jobs"]["123[4].pbs.example"]
    job["job_state"] = "F"
    job["Exit_status"] = 17
    scheduler._cache.clear()
    (finished,) = scheduler.jobs(history=True)
    assert finished["state"] == "FAILED" and finished["exit_code"] == 17
    assert backend.calls[0] == ["qselect", "-u", "alice", "-T", "-x"]


def test_pbs_user_selected_bulk_reads_have_bounded_argv_chunks():
    scheduler, backend = adapter("openpbs", max_records=600)
    sample = next(iter(backend.document["Jobs"].values()))
    backend.document["Jobs"] = {
        f"{index}.pbs.example": copy.deepcopy(sample) for index in range(1, 601)
    }
    jobs = scheduler.jobs()
    assert len(jobs) == 600
    reads = [argv for argv in backend.calls if argv[0] == "qstat"]
    assert len(reads) == 3
    assert all(len(argv[5:]) <= 256 for argv in reads)
    assert all(job["user"] == "alice" for job in jobs)


def test_pbs_total_resource_overrides_are_distributed_without_multiplication():
    scheduler, _ = adapter("pbspro")
    argv = scheduler._submission(
        {
            "script": "/project/job.sh",
            "cpus": 8,
            "nodes": 2,
            "memory_mib": 4096,
            "gpus": 2,
            "wall_seconds": 61,
        }
    )
    assert "select=2:ncpus=4:mem=2048mb:ngpus=1" in argv
    assert "walltime=00:01:01" in argv
    with pytest.raises(ValueError, match="divide evenly"):
        scheduler._submission({"script": "/project/job.sh", "nodes": 2, "cpus": 3})


def test_htcondor_schedd_global_id_restart_and_atomic_owner_constraint():
    scheduler, backend = adapter("htcondor")
    backend.document[0]["RequestCpus"] = 0.5
    (job,) = scheduler.jobs()
    assert job["resources"]["cpu"]["requested"] == 0.5
    assert job["resources"]["memory"]["unit"] == "MiB"
    plan = scheduler.review("cancel", job["identity"])
    assert plan["argv"][:3] == ["condor_rm", "-name", "schedd.example"]
    constraint = plan["argv"][-1]
    assert 'GlobalJobId == "schedd.example#123.4#1792929501"' in constraint
    assert 'Owner == "alice"' in constraint
    assert "NumJobStarts == 2" in constraint
    assert "ProcId == 4" in constraint


def test_htcondor_completed_job_detail_falls_back_to_history():
    scheduler, backend = adapter("htcondor")
    identity = scheduler.jobs()[0]["identity"]
    backend.empty_queue = True
    scheduler._cache.clear()
    assert scheduler.detail(identity)["id"] == "123.4"
    assert backend.calls[-1][0] == "condor_history"


def test_htcondor_transport_host_is_distinct_from_schedd_and_pool():
    scheduler = create(
        "htcondor",
        NativeFixture("htcondor"),
        {
            "cluster": "site",
            "user": "alice",
            "host": "login.example",
            "schedd": "submit.example",
            "pool": "manager.example",
        },
    )
    scheduler.jobs()
    command = scheduler.backend.calls[0]
    assert command[:5] == [
        "condor_q",
        "-name",
        "submit.example",
        "-pool",
        "manager.example",
    ]
    assert "login.example" not in command


def test_htcondor_submission_range_is_bounded_and_not_expanded():
    scheduler, _ = adapter("htcondor", max_records=4)
    assert scheduler._parse_submission("7.2 - 7.4\n") == {
        "native_id": "7.2",
        "native_range": {"first": "7.2", "last": "7.4"},
        "jobs": 3,
    }
    for value in ("7.2 - 8.3", "7.0 - 7.4", "7.3 - 7.2", "7.*", "7.1\n8.1"):
        with pytest.raises(ValueError):
            scheduler._parse_submission(value)


def test_lsf_allocated_slots_and_low_precision_identity_remain_honest():
    scheduler, backend = adapter("lsf")
    backend.document["RECORDS"][0]["SUBMIT_TIME"] = "Oct 25 12:00"
    (job,) = scheduler.jobs()
    assert job["identity"]["attempt"] == "submit=Oct 25 12:00"
    assert job["resources"]["cpu"] == {"allocated": 8, "unit": "lsf-slot"}
    assert job["resources"]["memory"]["requested"] is None
    with pytest.raises(ValueError, match="authoritative precision"):
        scheduler.review("cancel", job["identity"])
    with pytest.raises(ValueError, match="memory_mib has no portable equivalent"):
        scheduler._submission({"script": "/project/job.sh", "memory_mib": 1024})
    with pytest.raises(ValueError, match="gpus has no portable equivalent"):
        scheduler._submission({"script": "/project/job.sh", "gpus": 2})


def test_lsf_pending_hold_does_not_suspend_running_jobs():
    scheduler, backend = adapter("lsf")
    (job,) = scheduler.jobs()
    with pytest.raises(ValueError, match="pending"):
        scheduler.review("hold", job["identity"])
    backend.document["RECORDS"][0]["STAT"] = "PEND"
    plan = scheduler.review("hold", job["identity"])
    assert plan["argv"] == ["bstop", "123[4]"]
    backend.document["RECORDS"][0]["STAT"] = "PSUSP"
    plan = scheduler.review("release", job["identity"])
    assert plan["argv"] == ["bresume", "123[4]"]


def test_flux_exact_broker_unsigned_ids_and_aliases():
    scheduler, backend = adapter("flux")
    (job,) = scheduler.jobs()
    assert job["id"] == "58"
    assert backend.calls[0][:4] == [
        "flux",
        "proxy",
        "local:///run/flux/lab-instance",
        "flux",
    ]
    assert (
        canonical_job_id("ƒ21")
        == canonical_job_id("f21")
        == canonical_job_id(58)
        == "58"
    )
    assert canonical_job_id((1 << 64) - 1) == str((1 << 64) - 1)
    assert scheduler._parse_submission("ƒ21\n") == {"native_id": "58"}
    with pytest.raises(ValueError, match="unsigned 64-bit"):
        canonical_job_id(1 << 64)
    backend.document["jobs"][0]["jobid"] = "ƒ22"
    scheduler._cache.clear()
    with pytest.raises(ValueError, match="disagree"):
        scheduler.jobs()


def test_flux_no_unpinned_mutations_and_no_false_memory_equivalence():
    scheduler = create(
        "flux", NativeFixture("flux"), {"cluster": "flux", "user": "alice"}
    )
    assert scheduler.capabilities()["operations"] == ["queue", "history", "detail"]
    with pytest.raises(ValueError, match="Pin the concrete"):
        scheduler.review("submit", submission={"script": "/project/job.sh"})
    pinned, _ = adapter("flux")
    with pytest.raises(ValueError, match="memory_mib has no portable equivalent"):
        pinned._submission({"script": "/project/job.sh", "memory_mib": 4096})
    argv = pinned._submission(
        {
            "script": "/project/job.sh",
            "nodes": 2,
            "cpus": 8,
            "gpus": 2,
            "wall_seconds": 121,
        }
    )
    assert (
        "--nslots=2" in argv
        and "--cores-per-slot=4" in argv
        and "--gpus-per-slot=1" in argv
    )
    assert "--time-limit=121s" in argv


@pytest.mark.parametrize("name", DOCUMENTS)
@pytest.mark.parametrize(
    "native_id", ["--all", "*", "123[1-4]", "1;touch /tmp/unsafe", "123\n456"]
)
def test_native_identity_cannot_expand_into_switches_ranges_or_shell(name, native_id):
    scheduler, backend = adapter(name)
    identity = scheduler.jobs()[0]["identity"]
    count = len(backend.calls)
    with pytest.raises(ValueError):
        scheduler.detail({**identity, "native_id": native_id})
    assert len(backend.calls) == count


def test_stdin_submission_shell_preserves_literal_payload_and_argument_boundaries(
    tmp_path,
):
    marker = tmp_path / "must-not-exist"
    payload = '#!/bin/sh\nprintf "$(touch ' + str(marker) + ')"\n'
    literal_arg = 'quote " ; $(touch ' + str(marker) + ")"
    native = [
        sys.executable,
        "-c",
        "import json,sys; print(json.dumps([sys.stdin.read(),sys.argv[1:]]))",
        literal_arg,
    ]
    result = subprocess.run(
        stdin_submission(native, payload), capture_output=True, text=True, check=True
    )
    assert json.loads(result.stdout) == [payload, [literal_arg]]
    assert not marker.exists()
    with pytest.raises(ValueError, match="96 KiB"):
        stdin_submission(["cat"], "x" * (97 << 10))


@pytest.mark.parametrize("name", DOCUMENTS)
def test_native_parser_record_bound_is_checked_before_publication(name):
    scheduler, backend = adapter(name, max_records=1)
    if name in {"openpbs", "pbspro"}:
        backend.document["Jobs"]["124.pbs.example"] = copy.deepcopy(
            next(iter(backend.document["Jobs"].values()))
        )
    elif name == "htcondor":
        backend.document += copy.deepcopy(backend.document)
    else:
        key = "RECORDS" if name == "lsf" else "jobs"
        backend.document[key] += copy.deepcopy(backend.document[key])
    with pytest.raises(ValueError, match="bound"):
        scheduler.jobs()


def test_fixture_documents_are_finite_bounded_json():
    for document in DOCUMENTS.values():
        assert E.loads(E.canonical(document)) == document


@pytest.mark.parametrize("name", DOCUMENTS)
def test_optional_metric_errors_keep_job_and_other_available_measurements(name):
    scheduler, backend = adapter(name)
    if name in {"openpbs", "pbspro"}:
        backend.document["Jobs"]["123[4].pbs.example"]["Resource_List"]["mem"] = "4kw"
    elif name == "htcondor":
        backend.document[0]["RequestMemory"] = {
            "expression": "ifThenElse(SiteLimit, 1024, undefined)"
        }
    elif name == "lsf":
        backend.document["RECORDS"][0]["NALLOC_SLOT"] = "site-unavailable"
    else:
        backend.document["jobs"][0]["ngpus"] = "site-unavailable"
    (job,) = scheduler.jobs()
    assert job["state"] == "RUNNING"
    assert job["native"]["identity_verified"]
    (diagnostics,) = job["native"]["resource_diagnostics"]
    assert diagnostics["field"] and diagnostics["error"]
    assert diagnostics["native_value"] is not None
    if name != "lsf":
        assert job["cpus"] is not None
    if name in {"openpbs", "pbspro", "htcondor"}:
        assert job["resources"]["memory"]["requested"] is None
    # Optional counter extensions do not authorize execution, but they also do
    # not conceal an otherwise verified exact job from reviewed cancellation.
    assert scheduler.review("cancel", job["identity"])["action"] == "cancel"


@pytest.mark.parametrize("name", DOCUMENTS)
def test_critical_identity_errors_still_reject_a_native_job(name):
    scheduler, backend = adapter(name)
    if name in {"openpbs", "pbspro"}:
        backend.document["Jobs"]["123[4].pbs.example"]["qtime"] = "unknown"
    elif name == "htcondor":
        backend.document[0]["GlobalJobId"] = "unknown"
    elif name == "lsf":
        backend.document["RECORDS"][0]["JOBINDEX"] = "1-4"
    else:
        backend.document["jobs"][0]["t_submit"] = "unknown"
    with pytest.raises(ValueError):
        scheduler.jobs()


@pytest.mark.parametrize("name", DOCUMENTS)
def test_native_observation_targets_one_exact_job_without_queue_scan(name):
    scheduler, backend = adapter(name)
    (job,) = scheduler.jobs()
    backend.calls.clear()
    observed = scheduler.observe(job["id"])
    assert observed["identity"] == job["identity"]
    assert len(backend.calls) == 1
    assert job["id"] in backend.calls[0]
    assert backend.calls[0][0] != "qselect"


@pytest.mark.parametrize("name", DOCUMENTS)
def test_native_observation_refuses_other_job_and_other_owner(name):
    scheduler, backend = adapter(name)
    (job,) = scheduler.jobs()
    # The fixture returns the original record for an unexpected selection;
    # verify that trusting the native command alone cannot authorize it.
    with pytest.raises(ValueError, match="another job"):
        scheduler.observe(
            "999.pbs.example"
            if name in {"openpbs", "pbspro"}
            else "999.0"
            if name == "htcondor"
            else "999"
        )
    if name in {"openpbs", "pbspro"}:
        backend.document["Jobs"][job["id"]]["Job_Owner"] = "bob@login.example"
    elif name == "htcondor":
        backend.document[0]["Owner"] = "bob"
    elif name == "lsf":
        backend.document["RECORDS"][0]["USER"] = "bob"
    else:
        backend.document["jobs"][0]["username"] = "bob"
    with pytest.raises(ValueError, match="owned by"):
        scheduler.observe(job["id"])
