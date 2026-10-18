import copy
import json
import os
from pathlib import Path
import socket
import threading
import time

import pytest

from raven import allocation_health as H, operations as O, raven_execution as R
from raven.raven_worker_client import WorkerClient


def identity():
    return {"site_kind": "desktop", "host": socket.gethostname(), "uid": os.getuid(),
            "pid": os.getpid(), "verified": True, "verification": "explicit approved desktop policy"}


def params(tmp_path, **updates):
    return {"tests": "memory,scratch", "bytes": "4096", "repeats": "3", "warmup": "1", "seconds": "1",
            "scratch": str(tmp_path), "destination": str(tmp_path / "receipt"), **updates}


def test_review_does_no_probe_or_write_and_requires_authenticated_execution(tmp_path, monkeypatch):
    from raven import ops_health
    monkeypatch.setattr(H, "probe", lambda *_args, **_kwargs: pytest.fail("review ran probes"))
    ctx = O.Context(scope={"site": "test"}, verified_execution=identity())
    result = ops_health.run("allocation-health", params(tmp_path), ctx)
    assert result["plan"]["payload"]["settings"]["maximum_io_bytes"] == 32768
    assert not list(tmp_path.iterdir())
    with pytest.raises(ValueError, match="authenticated"):
        ops_health.run("allocation-health", params(tmp_path), O.Context(scope={"site": "test"}))


def test_no_adapter_runs_in_light_or_from_caller_context(tmp_path):
    result = O.run("allocation-health", params(tmp_path), O.Context(cfg={"raven": {"managed": True}}))
    assert result["status"] == "deferred"
    with pytest.raises(ValueError, match="authenticated"):
        R.execute_operation("allocation-health", {"params": params(tmp_path)},
                            context={"scope": {"site": "test"}, "verified_execution": identity()})
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("field,value", [("seconds", "nan"), ("seconds", "inf"), ("seconds", "31"),
    ("bytes", str(H.MAX_BYTES + 1)), ("repeats", "1.5"), ("warmup", "-1"), ("seconds", True),
    ("tests", "gpu"), ("tests", "memory,gpu"), ("tests", "memory,memory")])
def test_invalid_or_unqualified_tests_are_rejected(field, value):
    with pytest.raises(ValueError):
        H.settings({field: value})


def test_memory_and_anonymous_scratch_data_verified_cleaned_and_retained(tmp_path):
    from raven import ops_health
    ctx = O.Context(scope={"site": "test"}, verified_execution=identity())
    review = ops_health.run("allocation-health", params(tmp_path), ctx)
    result = ops_health.apply("allocation-health", review["plan"], ctx)
    retained = json.loads((tmp_path / "receipt/health.json").read_text())
    assert result["data"] == retained
    assert retained["status"] == "completed"
    assert len(retained["samples"]) == 8
    assert all(row["verified"] for row in retained["samples"])
    assert all(value["count"] == 3 for value in retained["summary"].values())
    assert retained["comparison"]["status"] == "no-baseline"
    assert [p.name for p in tmp_path.iterdir()] == ["receipt"]
    assert (tmp_path / "receipt/health.json").stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="new directory"):
        ops_health.apply("allocation-health", review["plan"], ctx)


def test_replaced_attempt_or_scratch_refuses_apply_without_probe(tmp_path):
    from raven import ops_health
    ctx = O.Context(scope={"site": "test"}, verified_execution=identity())
    review = ops_health.run("allocation-health", params(tmp_path), ctx)
    changed = dict(identity(), pid=identity()["pid"] + 1)
    with pytest.raises(ValueError, match="allocation changed"):
        ops_health.apply("allocation-health", review["plan"], O.Context(scope=ctx.scope, verified_execution=changed))
    (tmp_path / "old").mkdir()
    with pytest.raises(ValueError, match="scratch directory changed"):
        H.probe({"tests": "scratch", "bytes": 4096}, identity(), {**H.scratch_identity(tmp_path), "inode": 1})


def test_scratch_world_writable_and_symlink_rejected(tmp_path):
    link = tmp_path / "link"
    link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinks"):
        H.scratch_identity(link)
    tmp_path.chmod(0o777)
    with pytest.raises(ValueError, match="not writable"):
        H.scratch_identity(tmp_path)


def test_cancel_and_time_budget_return_partial_without_scientific_job_action(tmp_path):
    cancel = threading.Event()
    cancel.set()
    result = H.probe({"tests": "scratch"}, identity(), H.scratch_identity(tmp_path), cancel=cancel)
    assert result["status"] == "cancelled" and result["samples"] == []
    ticks = iter([0, .2, .3])
    result = H.probe({"tests": "memory", "seconds": .1}, identity(), clock=lambda: next(ticks))
    assert result["status"] == "budget-exhausted" and not list(tmp_path.iterdir())


def test_cohort_and_repeatability_prevent_spurious_causal_claim(tmp_path):
    receipt = H.probe({"tests": "memory", "bytes": 4096}, identity())
    baseline = copy.deepcopy(receipt)
    receipt["summary"]["memory"].update(median_seconds=2, relative_range=.01)
    baseline["summary"]["memory"].update(median_seconds=1, relative_range=.01)
    compared = H.compare(receipt, baseline)
    assert compared["comparisons"][0]["unusual"]
    assert "no causal" in compared["reason"]
    baseline["summary"]["memory"]["relative_range"] = 1
    assert not H.compare(receipt, baseline)["comparisons"][0]["unusual"]
    baseline["cohort"] = "other"
    assert H.compare(receipt, baseline)["status"] == "incomparable"


def test_real_worker_review_probe_persistence_and_spoofed_context(tmp_path):
    client = WorkerClient({"site_kind": "desktop", "desktop_explicit": True, "context": {},
                           "resources": {"memory_mib": 512, "cpus": 1, "max_tasks": 1, "wall_seconds": 40}})
    try:
        client.start()
        until = time.monotonic() + 8
        while not client.ready and time.monotonic() < until:
            client.poll()
            time.sleep(.01)
        assert client.ready, client.error
        router = R.ExecutionRouter({"managed": True}, lambda: client)
        spoofed = router.dispatch("allocation-health", {"params": params(tmp_path)},
                                  {"scope": {"site": "test"}, "verified_execution": {"host": "attacker", "verified": True}})
        assert {key: value for key, value in spoofed["result"]["plan"]["payload"]["execution"].items() if key != "verified_resources"} == client.identity
        assert spoofed["result"]["plan"]["payload"]["execution"]["verified_resources"]["cpus"] == 1
        ctx = O.Context(scope={"site": "test"}, execution=router, state_dir=str(tmp_path / "state"))
        review = O.run("allocation-health", params(tmp_path), ctx)
        assert review["plan"]["payload"]["execution"] == spoofed["result"]["plan"]["payload"]["execution"]
        result = O.apply("allocation-health", review["plan"], ctx)
        assert result["data"]["execution"] == review["plan"]["payload"]["execution"]
        assert result["data"]["status"] == "completed"
        assert (tmp_path / "receipt/health.json").exists()
    finally:
        client.close()


def test_memory_cohorts_can_span_nodes_but_scratch_requires_local_identity(tmp_path):
    value = H.probe({"tests": "memory", "bytes": 4096}, identity())
    other = copy.deepcopy(value)
    other["execution"]["host"] = "another-matching-node"
    assert H.cohort(value) == H.cohort(other)
    value["settings"]["tests"] = ["scratch"]
    other["settings"]["tests"] = ["scratch"]
    assert H.cohort(value) != H.cohort(other)


@pytest.mark.parametrize("summary", [None, [], {"memory": []}, {"memory": {"median_seconds": "wrong"}},
    {"memory": {"median_seconds": 1, "relative_range": "wrong"}}])
def test_malformed_baseline_summary_abstains_safely(summary):
    receipt = H.probe({"tests": "memory", "bytes": 4096}, identity())
    baseline = dict(receipt, summary=summary)
    result = H.compare(receipt, baseline)
    assert not any(row["unusual"] for row in result.get("comparisons", []))


def test_actual_receipt_matches_published_contract():
    import jsonschema
    schema = json.loads((Path(__file__).parents[1] / "docs/schemas/allocation-health.v1.schema.json").read_text())
    receipt = H.probe({"tests": "memory", "bytes": 4096}, identity())
    jsonschema.Draft202012Validator(schema).validate(receipt)
