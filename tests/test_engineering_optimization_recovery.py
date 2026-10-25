"""Budget, independent-unit, ownership and restart-evidence integration checks."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from raven.engineering import optimization as O, recovery as R


def identity(number="1", attempt="2026-10-25T00:00:00/restart:0"):
    return dict(scheduler="slurm", scope="test-scope", native_id=number, attempt=attempt)


class Scheduler:
    def __init__(self):
        self.applied = []
        self.scope = dict(user="alice")
        self.failure = None
        self.record = dict(identity=identity(), user="alice", state="FAILED", native_state="NODE_FAIL")

    def review(self, action, identity=None, submission=None):
        plan = dict(action=action, submission=submission, identity=identity,
                    created=time.time(), expires_seconds=300)
        if submission and "script" in submission:
            plan["script_sha256"] = hashlib.sha256(Path(submission["script"]).read_bytes()).hexdigest()
        plan["digest"] = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
        return plan

    def apply(self, plan, review_digest):
        assert plan["digest"] == review_digest
        self.applied.append(plan)
        if self.failure:
            raise self.failure
        return dict(status="accepted", native_id="1", scope="test-scope")

    def detail(self, requested):
        return copy.deepcopy(self.record)


@pytest.fixture
def experiment(tmp_path):
    script = tmp_path / "work.sh"
    script.write_text("#!/bin/bash\nprintf '%s\\n' 'same science'\n")
    return dict(schema=O.SCHEMA, id="study", repetitions=3, seed=7,
                equivalence=dict(semantic="energy", reference=1., absolute=.01, relative=0., unit="J"),
                budget=dict(max_jobs=6, cpu_seconds=180, gpu_seconds=0, reservation_seconds=60, max_concurrent=1),
                variants=[dict(id="two", submission=dict(script=str(script), cpus=2, nodes=1, memory_mib=64, wall_seconds=10)),
                          dict(id="four", submission=dict(script=str(script), cpus=4, nodes=1, memory_mib=64, wall_seconds=10))])


def test_budget_complete_design_and_reproducible_order(experiment):
    plan = O.prepare(experiment)
    assert plan["reservation"] == dict(cpu_seconds=180, gpu_seconds=0, reservation_seconds=60)
    assert len(plan["trials"]) == 6
    assert [r["id"] for r in O.prepare(experiment)["trials"]] == [r["id"] for r in plan["trials"]]
    experiment["budget"]["cpu_seconds"] = 179
    with pytest.raises(ValueError, match="resource budget"):
        O.prepare(experiment)


@pytest.mark.parametrize("bad", [True, float("inf"), -1])
def test_invalid_resource_budgets_rejected(experiment, bad):
    experiment["budget"]["cpu_seconds"] = bad
    with pytest.raises(ValueError):
        O.prepare(experiment)


def test_scientific_workload_and_directives_cannot_bypass_bounds(experiment, tmp_path):
    other = tmp_path / "different.sh"
    other.write_text("#!/bin/bash\necho 'different science'\n")
    experiment["variants"][1]["submission"]["script"] = str(other)
    with pytest.raises(ValueError, match="preserve workload"):
        O.prepare(experiment)
    other.write_text("#!/bin/bash\n#SBATCH --array=1-999\necho 'science'\n")
    with pytest.raises(ValueError, match="without scheduler directives"):
        O.prepare(experiment)


def test_apply_claim_and_concurrency_survive_refresh(experiment, tmp_path):
    scheduler = Scheduler()
    plan = O.prepare(experiment, scheduler)
    first = O.apply(plan, scheduler, project=tmp_path, review_digest=plan["digest"])
    assert first["state"] == "accepted"
    assert len(scheduler.applied) == 1
    refreshed = O.prepare(experiment, scheduler)
    assert refreshed["digest"] != plan["digest"]
    with pytest.raises(ValueError, match="Concurrent"):
        O.apply(refreshed, scheduler, project=tmp_path, review_digest=refreshed["digest"])
    O.reconcile(tmp_path, "study", scheduler, bindings={first["trial_id"]: identity()})
    second = O.apply(refreshed, scheduler, project=tmp_path, review_digest=refreshed["digest"])
    assert second["trial_id"] != first["trial_id"]
    assert len(scheduler.applied) == 2
    with pytest.raises(ValueError, match="already claimed"):
        O.apply(refreshed, scheduler, project=tmp_path, review_digest=refreshed["digest"], trial_id=first["trial_id"])


def test_timeout_is_never_replayed_or_budget_released(experiment, tmp_path):
    scheduler = Scheduler()
    scheduler.failure = TimeoutError("controller outcome unknown")
    plan = O.prepare(experiment, scheduler)
    with pytest.raises(TimeoutError):
        O.apply(plan, scheduler, project=tmp_path, review_digest=plan["digest"])
    state = O.status(tmp_path, "study")
    entry = next(iter(state["trials"].values()))
    assert entry["state"] == "unknown"
    assert "TimeoutError" in entry["error"]
    scheduler.failure = None
    with pytest.raises(ValueError, match="Concurrent"):
        O.apply(plan, scheduler, project=tmp_path, review_digest=plan["digest"])
    assert len(scheduler.applied) == 1
    state = O.reconcile(tmp_path, "study", scheduler)
    assert next(iter(state["trials"].values()))["state"] == "unknown"


def test_script_and_budget_revalidated_before_issue(experiment, tmp_path):
    scheduler = Scheduler()
    plan = O.prepare(experiment, scheduler)
    Path(experiment["variants"][0]["submission"]["script"]).write_text("#!/bin/sh\necho changed\n")
    with pytest.raises(ValueError, match="changed"):
        O.apply(plan, scheduler, project=tmp_path, review_digest=plan["digest"])
    assert scheduler.applied == []
    assert not (tmp_path / ".raven").exists()


def observations(plan):
    return [dict(trial_id=trial["id"], identity=identity(str(i + 1)), outcome="completed",
                 runtime_seconds=(10 if trial["variant"] == "two" else 7) + i / 10,
                 queue_seconds=1, cpu_seconds=None, gpu_seconds=None,
                 controls=dict(identity=identity(str(i + 1)), submission_sha256=O._digest(trial["submission"]), evidence="Native post-admission request matches the pinned trial"),
                 output=dict(semantic="energy", unit="J", value=1., sha256="a" * 64))
            for i, trial in enumerate(plan["trials"])]


def test_measured_analysis_preserves_units_missing_and_failed(experiment):
    plan = O.prepare(experiment)
    rows = observations(plan)
    rows[0]["outcome"] = "scientific_failed"
    rows.pop()
    report = O.analyze(dict(schema=O.RESULT, plan=plan, observations=rows))
    assert len(report["missing"]) == 1 and len(report["excluded"]) == 1
    assert all(c["unit"] == "seconds" for c in report["comparisons"])
    assert all(g["cpu_seconds"]["mean"] is None for g in report["groups"].values())
    assert all(c["statistics"]["interval"] is None for c in report["comparisons"] if c["metric"] == "gpu_seconds")


def test_historical_results_survive_original_script_removal(experiment):
    plan = O.prepare(experiment)
    Path(experiment["variants"][0]["submission"]["script"]).unlink()
    report = O.analyze(dict(schema=O.RESULT, plan=plan, observations=observations(plan)))
    assert report["groups"]["two"]["comparable_trials"] == 3


def test_attempt_duplicate_cannot_inflate_confidence(experiment):
    plan = O.prepare(experiment)
    rows = observations(plan)
    rows[1]["identity"] = rows[0]["identity"]
    with pytest.raises(ValueError, match="Independent repetitions"):
        O.analyze(dict(schema=O.RESULT, plan=plan, observations=rows))


def test_missing_controls_and_failed_outputs_remain_unknown_not_fabricated(experiment):
    plan = O.prepare(experiment)
    rows = observations(plan)
    rows[0].pop("controls")
    rows[1].update(outcome="infrastructure_failed", output=None, cpu_seconds=5)
    report = O.analyze(dict(schema=O.RESULT, plan=plan, observations=rows))
    assert len(report["excluded"]) == 2
    assert report["accounting"]["cpu_seconds"] == dict(unit="seconds", known_sum=5., known_attempts=1, unknown_attempts=5, includes_failed_attempts=True)


def test_kubernetes_limits_init_sidecars_and_no_unbounded_retries():
    manifest = dict(apiVersion="batch/v1", kind="Job", metadata=dict(name="trial"), spec=dict(
        activeDeadlineSeconds=10, completions=2, parallelism=1, backoffLimit=0,
        template=dict(spec=dict(restartPolicy="Never", containers=[dict(name="work", image="x@sha256:" + "a" * 64,
            resources=dict(limits=dict(cpu="500m", memory="64Mi", **{"amd.com/gpu": 1})))],
            initContainers=[dict(name="init", resources=dict(limits=dict(cpu="1", memory="32Mi")))]))))
    result = O.reservation(dict(manifest=manifest))
    assert result["cpu_seconds"] == 30 and result["gpu_seconds"] == 20
    assert result["memory_bytes"] == 96 * 1048576
    manifest["spec"]["backoffLimit"] = 1
    with pytest.raises(ValueError, match="backoffLimit"):
        O.reservation(dict(manifest=manifest))


@pytest.fixture
def recovery(tmp_path):
    script = tmp_path / "restart.sh"
    script.write_text("#!/bin/bash\necho restarted\n")
    return dict(schema=R.SCHEMA, id="retry-one", source=dict(identity=identity(), owner="alice", state="NODE_FAIL"),
                failure=dict(kind="node_failure", evidence="Slurm terminal NODE_FAIL accounting record"),
                submission=dict(script=str(script), cpus=2, memory_mib=64, wall_seconds=60), mode="restart")


def test_recovery_exact_attempt_owner_and_once(recovery, tmp_path):
    scheduler = Scheduler()
    plan = R.prepare(recovery, scheduler)
    scheduler.record["user"] = "bob"
    with pytest.raises(ValueError, match="owner changed"):
        R.apply(plan, scheduler, project=tmp_path, review_digest=plan["digest"])
    assert not (tmp_path / ".raven").exists()
    scheduler.record["user"] = "alice"
    result = R.apply(plan, scheduler, project=tmp_path, review_digest=plan["digest"])
    assert result["state"] == "accepted" and len(scheduler.applied) == 1
    with pytest.raises(ValueError, match="issuance receipt"):
        R.apply(plan, scheduler, project=tmp_path, review_digest=plan["digest"])


@pytest.mark.parametrize("kind,state", [("timeout", "TIMEOUT"), ("out_of_memory", "OUT_OF_MEMORY"), ("application_failure", "FAILED"), ("cancelled", "CANCELLED")])
def test_policy_and_application_failures_require_explicit_correction(recovery, kind, state):
    recovery["source"]["state"] = state
    recovery["failure"]["kind"] = kind
    with pytest.raises(ValueError, match="correction"):
        R.prepare(recovery)
    recovery["correction"] = "Reviewed the application and corrected its time/resource/output configuration"
    assert R.prepare(recovery)["document"]["failure"]["kind"] == kind


def checkpoint(tmp_path):
    path = tmp_path / "state.ckpt"
    path.write_text("scientific state")
    compatible = dict(application="solver-v1", format="checkpoint-v2", runtime="python3")
    return dict(path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest(), complete=True,
                source_identity=identity(), validation=dict(passed=True, method="application checksum and schema", evidence="solver verified every field"),
                compatibility=dict(expected=compatible, actual=compatible))


def test_checkpoint_wrapper_checks_bytes_on_compute_and_preserves_directives(recovery, tmp_path):
    script = Path(recovery["submission"]["script"])
    script.write_text("#!/bin/bash\n#SBATCH --time=00:01:00\nprintf '%s\\n' \"$RAVEN_CHECKPOINT\" > '" + str(tmp_path / "ran") + "'\n")
    recovery.update(mode="resume", checkpoint=checkpoint(tmp_path))
    scheduler = Scheduler()
    plan = R.prepare(recovery, scheduler, project=tmp_path)
    wrapper = Path(plan["submission"]["script"])
    raw = wrapper.read_text()
    assert raw.startswith("#!/bin/bash\n#SBATCH --time=00:01:00\nexport RAVEN_CHECKPOINT=")
    env = dict(os.environ, PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"])
    run = subprocess.run(["bash", str(wrapper)], capture_output=True, text=True, env=env)
    assert run.returncode == 0 and (tmp_path / "ran").exists()
    (tmp_path / "ran").unlink()
    Path(recovery["checkpoint"]["path"]).write_text("changed after review")
    run = subprocess.run(["bash", str(wrapper)], capture_output=True, text=True, env=env)
    assert run.returncode == 65 and not (tmp_path / "ran").exists()
    assert "checkpoint changed" in run.stderr


def test_checkpoint_compatibility_and_source_are_required(recovery, tmp_path):
    recovery.update(mode="resume", checkpoint=checkpoint(tmp_path))
    recovery["checkpoint"]["source_identity"] = identity("2")
    with pytest.raises(ValueError, match="exact source attempt"):
        R.prepare(recovery, project=tmp_path)
    recovery["checkpoint"]["source_identity"] = identity()
    recovery["checkpoint"]["compatibility"]["actual"] = dict(application="other", format="checkpoint-v2", runtime="python3")
    with pytest.raises(ValueError, match="compatibility differs"):
        R.prepare(recovery, project=tmp_path)


def test_preemption_is_not_node_failure(recovery):
    recovery["source"]["state"] = "PREEMPTED"
    with pytest.raises(ValueError, match="does not support"):
        R.prepare(recovery)
    recovery["failure"]["kind"] = "preemption"
    assert "Policy interruption" in R.prepare(recovery)["recommendation"]


def test_workflow_invalidation_and_active_ownership():
    report = R.workflow(dict(nodes=[dict(id="a", dependencies=[], state="COMPLETED", outputs_validated=True),
        dict(id="b", dependencies=["a"], state="FAILED", outputs_validated=False),
        dict(id="c", dependencies=["b"], state="COMPLETED", outputs_validated=True),
        dict(id="d", dependencies=["a"], state="RUNNING", outputs_validated=False),
        dict(id="e", dependencies=["d"], state="PENDING", outputs_validated=False)]))
    assert {k: v["action"] for k, v in report["nodes"].items()} == dict(a="reuse", b="review-rerun", c="review-rerun", d="running", e="blocked")
    with pytest.raises(ValueError, match="acyclic"):
        R.workflow(dict(nodes=[dict(id="a", dependencies=["b"], state="FAILED", outputs_validated=False),
                              dict(id="b", dependencies=["a"], state="FAILED", outputs_validated=False)]))


def test_cancellation_is_separate_review_and_keeps_reservation(experiment, tmp_path):
    scheduler = Scheduler()
    plan = O.prepare(experiment, scheduler)
    first = O.apply(plan, scheduler, project=tmp_path, review_digest=plan["digest"])
    scheduler.record["state"] = scheduler.record["native_state"] = "RUNNING"
    O.reconcile(tmp_path, "study", scheduler, bindings={first["trial_id"]: identity()})
    cancel = O.review_cancel(tmp_path, "study", first["trial_id"], scheduler)
    receipt = O.cancel(cancel, scheduler, project=tmp_path, experiment_id="study", trial_id=first["trial_id"], review_digest=cancel["digest"])
    assert receipt["cancellation"]["state"] == "accepted"
    assert receipt["state"] == "accepted"
    with pytest.raises(ValueError, match="already has"):
        O.cancel(cancel, scheduler, project=tmp_path, experiment_id="study", trial_id=first["trial_id"], review_digest=cancel["digest"])


def test_expired_native_review_does_not_claim_or_issue(experiment, tmp_path):
    scheduler = Scheduler()
    plan = O.prepare(experiment, scheduler)
    for trial in plan["trials"]:
        trial["native_plan"]["created"] = time.time() - 301
    plan["digest"] = O._digest({k: v for k, v in plan.items() if k != "digest"})
    with pytest.raises(ValueError, match="review expired"):
        O.apply(plan, scheduler, project=tmp_path, review_digest=plan["digest"])
    assert scheduler.applied == []
    with pytest.raises(FileNotFoundError):
        O.status(tmp_path, "study")


def test_identity_binding_cannot_release_another_owners_budget(experiment, tmp_path):
    scheduler = Scheduler()
    plan = O.prepare(experiment, scheduler)
    first = O.apply(plan, scheduler, project=tmp_path, review_digest=plan["digest"])
    scheduler.record["user"] = "bob"
    with pytest.raises(ValueError, match="not owned"):
        O.reconcile(tmp_path, "study", scheduler, bindings={first["trial_id"]: identity()})
    assert O.status(tmp_path, "study")["trials"][first["trial_id"]]["state"] == "accepted"


def test_source_read_is_deduplicated_across_repeated_variants(experiment, monkeypatch):
    original = O.science_files.snapshot
    calls = []
    def record(path, **kwargs):
        calls.append(path)
        return original(path, **kwargs)
    monkeypatch.setattr(O.science_files, "snapshot", record)
    O.prepare(experiment)
    assert calls == [experiment["variants"][0]["submission"]["script"]]


def test_refresh_cannot_mix_changed_code_under_same_experiment_id(experiment, tmp_path):
    scheduler = Scheduler()
    plan = O.prepare(experiment, scheduler)
    first = O.apply(plan, scheduler, project=tmp_path, review_digest=plan["digest"])
    O.reconcile(tmp_path, "study", scheduler, bindings={first["trial_id"]: identity()})
    Path(experiment["variants"][0]["submission"]["script"]).write_text("#!/bin/sh\necho 'changed workload'\n")
    changed = O.prepare(experiment, scheduler)
    with pytest.raises(ValueError, match="different reviewed design"):
        O.apply(changed, scheduler, project=tmp_path, review_digest=changed["digest"])
    assert len(scheduler.applied) == 1


def test_resource_study_uses_real_native_review_and_durable_claim(experiment, tmp_path):
    from raven.schedulers.slurm import SlurmScheduler
    class Backend:
        def __init__(self):
            self.submissions = []
        def run(self, argv, timeout):
            assert timeout > 0
            if argv[:2] == ["sh", "-c"] and argv[3] == "raven-script":
                return Path(argv[4]).read_text(), ""
            if argv[:2] == ["sh", "-c"] and argv[3] == "raven-input":
                assert argv[5] == "sbatch" and "--cpus-per-task=" in " ".join(argv[5:])
                self.submissions.append(argv)
                return "123;local\n", ""
            raise AssertionError(argv)
    backend = Backend()
    scheduler = SlurmScheduler(backend, {"cluster": "local", "user": "alice"}, journal_dir=str(tmp_path / "native-journal"))
    plan = O.prepare(experiment, scheduler)
    result = O.apply(plan, scheduler, project=tmp_path, review_digest=plan["digest"])
    assert result["receipt"]["native_id"] == "123"
    assert len(backend.submissions) == 1
    assert len(list((tmp_path / "native-journal").glob("*.claimed"))) == 1
    assert "same science" in backend.submissions[0][4]
    # Core receipts retain the full scope dictionary; exact job identities use
    # its digest. This must work through the real review/apply contract, not a
    # fixture that invents a digest-shaped receipt scope.
    exact = dict(scheduler="slurm", scope=scheduler.scope_key, native_id="123", attempt="2026-10-25T00:00:00/restart:0")
    scheduler.fresh_detail = lambda selected: dict(identity=selected, user="alice", state="COMPLETED", native_state="COMPLETED")
    journal = O.reconcile(tmp_path, "study", scheduler, bindings={result["trial_id"]: exact})
    assert journal["trials"][result["trial_id"]]["state"] == "terminal"


def test_recovery_timeout_outcome_cannot_be_replayed(recovery, tmp_path):
    scheduler = Scheduler()
    scheduler.failure = TimeoutError("native submission outcome unknown")
    plan = R.prepare(recovery, scheduler)
    with pytest.raises(TimeoutError):
        R.apply(plan, scheduler, project=tmp_path, review_digest=plan["digest"])
    assert R.status(tmp_path, "retry-one")["state"] == "unknown"
    scheduler.failure = None
    with pytest.raises(ValueError, match="issuance receipt"):
        R.apply(plan, scheduler, project=tmp_path, review_digest=plan["digest"])
    assert len(scheduler.applied) == 1


def test_large_checkpoint_is_rejected_before_local_hashing(recovery, tmp_path, monkeypatch):
    item = checkpoint(tmp_path)
    with open(item["path"], "wb") as stream:
        stream.truncate(9 << 20)
    recovery.update(mode="resume", checkpoint=item)
    snapshots = []
    original = R.science_files.snapshot
    def track(path, **kwargs):
        snapshots.append(kwargs["limit"])
        return original(path, **kwargs)
    monkeypatch.setattr(R.science_files, "snapshot", track)
    with pytest.raises(ValueError, match="Heavy checkpoint campaign"):
        R.prepare(recovery, project=tmp_path)
    assert all(limit <= R.MAX_LOCAL_INSPECTION for limit in snapshots)


def test_recovery_cancel_before_hashing_or_issuance(recovery, tmp_path):
    import threading
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(ValueError, match="cancelled"):
        R.prepare(recovery, project=tmp_path, cancel=cancelled)
    assert not (tmp_path / ".raven").exists()


@pytest.mark.parametrize("redirect", ["cancel", "different-account"])
def test_recovery_nested_native_action_cannot_redirect_reviewed_restart(recovery, tmp_path, redirect):
    scheduler = Scheduler()
    plan = R.prepare(recovery, scheduler)
    if redirect == "cancel":
        plan["native_plan"] = scheduler.review("cancel", identity=identity())
    else:
        changed = dict(plan["submission"], account="other-lab")
        plan["native_plan"] = scheduler.review("submit", submission=changed)
    plan["digest"] = R._digest({k: v for k, v in plan.items() if k != "digest"})
    with pytest.raises(ValueError, match="action or submission differs"):
        R.apply(plan, scheduler, project=tmp_path, review_digest=plan["digest"])
    assert scheduler.applied == [] and not (tmp_path / ".raven").exists()


def test_resource_native_action_must_be_submission_even_with_same_fields(experiment, tmp_path):
    scheduler = Scheduler()
    plan = O.prepare(experiment, scheduler)
    plan["trials"][0]["native_plan"]["action"] = "cancel"
    plan["digest"] = O._digest({k: v for k, v in plan.items() if k != "digest"})
    with pytest.raises(ValueError, match="action or submission differs"):
        O.apply(plan, scheduler, project=tmp_path, review_digest=plan["digest"])
    assert scheduler.applied == [] and not (tmp_path / ".raven").exists()


@pytest.mark.parametrize("directive", [
    "#SBATCH --ntasks=8", "#SBATCH --array=1-999", "#SBATCH hetjob",
    "#PBS -l select=4:ncpus=16", "#PBS -J 1-999", "#BSUB -n 64", "#BSUB -J trial[1-999]",
])
def test_native_trial_directives_reject_before_real_adapter_review(experiment, directive):
    from raven.schedulers.slurm import SlurmScheduler
    class NoSchedulerIO:
        def run(self, argv, timeout):
            raise AssertionError("Unsafe trial reached native review: " + repr(argv))
    path = Path(experiment["variants"][0]["submission"]["script"])
    path.write_text("#!/bin/bash\n" + directive + "\necho science\n")
    scheduler = SlurmScheduler(NoSchedulerIO(), {"cluster": "local", "user": "alice"})
    with pytest.raises(ValueError, match="without scheduler directives"):
        O.prepare(experiment, scheduler)


def test_recovery_native_host_cannot_run_different_bytes_at_the_same_path(recovery, tmp_path):
    scheduler = Scheduler()
    original = scheduler.review
    def wrong_host(*args, **kwargs):
        plan = original(*args, **kwargs)
        plan["script_sha256"] = "f" * 64
        return plan
    scheduler.review = wrong_host
    with pytest.raises(ValueError, match="Native recovery script differs"):
        R.prepare(recovery, scheduler)
    assert scheduler.applied == [] and not (tmp_path / ".raven").exists()


def test_recovery_rehashed_native_byte_mismatch_cannot_bypass_apply(recovery, tmp_path):
    scheduler = Scheduler()
    plan = R.prepare(recovery, scheduler)
    plan["native_plan"]["script_sha256"] = "f" * 64
    plan["digest"] = R._digest({k: v for k, v in plan.items() if k != "digest"})
    with pytest.raises(ValueError, match="Native recovery script differs"):
        R.apply(plan, scheduler, project=tmp_path, review_digest=plan["digest"])
    assert scheduler.applied == [] and not (tmp_path / ".raven").exists()
