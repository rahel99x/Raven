"""Cleanup cannot infer scheduler absence on a different connection or cluster."""

import copy
from pathlib import Path
from types import SimpleNamespace

import pytest

from raven.launch_scheduler_context import capture, configured
from raven.raven_config import DEFAULTS
from raven.runtime_cleanup import _confirmed_absent


def execution(response="", error=None, cluster="alpha"):
    policy = copy.deepcopy(DEFAULTS)
    policy["site"]["cluster_id"] = cluster
    context = configured(policy)
    calls = []

    def command(argv, *, timeout, limit, cancel):
        calls.append((argv, timeout, limit, cancel))
        if error is not None:
            raise error
        return response.get(argv[0], "") if isinstance(response, dict) else response

    backend = SimpleNamespace(
        ledger=object(),
        scope={"connection": context["connection"], "user": context["user"]},
        command=command,
    )
    router = SimpleNamespace(source_backend=backend, policy=policy)
    return router, calls


def candidates(router, count):
    context = capture(router.source_backend, router.policy)
    return [
        (
            {"status": "preserved"},
            Path(str(index)),
            1,
            {"job_id": str(index), "cluster": "alpha", "scheduler_context": context},
        )
        for index in range(1, count + 1)
    ]


def test_confirmations_batch_once_through_governed_fresh_reader_and_cap_ids():
    router, calls = execution()
    rows = candidates(router, 129)
    absent = _confirmed_absent(rows, router)
    assert absent == {str(index) for index in range(1, 129)}
    assert len(calls) == 1
    argv, timeout, limit, cancel = calls[0]
    assert argv[0] == "squeue" and "--clusters=alpha" in argv
    assert (
        len(
            next(value for value in argv if value.startswith("--jobs="))
            .split("=", 1)[1]
            .split(",")
        )
        == 128
    )
    assert timeout == 5 and limit == 65536 and cancel is None
    assert "capped at 128" in rows[-1][0]["reason"]


@pytest.mark.parametrize("mismatch", ["connection", "cluster", "user", "missing"])
def test_unknown_or_changed_scheduler_context_never_queries_or_deletes(mismatch):
    router, calls = execution()
    rows = candidates(router, 1)
    receipt = rows[0][3]
    if mismatch == "missing":
        receipt.pop("scheduler_context")
    else:
        receipt["scheduler_context"] = dict(
            receipt["scheduler_context"], **{mismatch: "different"}
        )
    assert _confirmed_absent(rows, router) == set()
    assert (
        calls == [] and "matching retained scheduler connection" in rows[0][0]["reason"]
    )


def test_deferred_record_budget_failure_preserves_every_wrapper():
    from raven.raven_collection import Deferred

    router, calls = execution(error=Deferred("rolling controller budget exhausted"))
    rows = candidates(router, 2)
    assert _confirmed_absent(rows, router) == set()
    assert len(calls) == 1
    assert all(
        "rolling controller budget exhausted" in row["reason"] for row, *_ in rows
    )


def test_active_array_task_and_malformed_output_are_not_absence_proof():
    router, calls = execution(response="1_[2-99]\n")
    rows = candidates(router, 2)
    assert _confirmed_absent(rows, router) == {"2"}
    assert "still lists" in rows[0][0]["reason"]
    router, _ = execution(response="unknown-job-shape\n")
    rows = candidates(router, 2)
    assert _confirmed_absent(rows, router) == set()
    assert all("unrecognized job identity" in row["reason"] for row, *_ in rows)


def test_default_cluster_is_verified_once_before_batched_absence():
    router, calls = execution(
        response={"scontrol": "ClusterName = alpha\n", "squeue": "2\n"}, cluster=""
    )
    rows = candidates(router, 2)
    assert _confirmed_absent(rows, router) == {"1"}
    assert len(calls) == 2 and calls[0][0] == ["scontrol", "show", "config"]
    assert "--clusters=alpha" in calls[1][0]


@pytest.mark.parametrize(
    "response",
    ["ClusterName = beta\n", "", "ClusterName = alpha\nClusterName = beta\n"],
)
def test_default_cluster_mismatch_or_unknown_preserves_every_wrapper(response):
    router, calls = execution(response={"scontrol": response}, cluster="")
    rows = candidates(router, 2)
    assert _confirmed_absent(rows, router) == set()
    assert len(calls) == 1 and calls[0][0][0] == "scontrol"
    assert all("preserved" in row["reason"] for row, *_ in rows)


def test_runtime_observed_cluster_can_complete_an_empty_submission_receipt():
    router, calls = execution()
    rows = candidates(router, 1)
    rows[0][3].update(cluster="", observed_cluster="alpha")
    assert _confirmed_absent(rows, router) == {"1"}
    assert len(calls) == 1
    rows[0][3].update(cluster="beta")
    assert _confirmed_absent(rows, router) == set()
    assert len(calls) == 1


def test_configured_context_is_pure_and_tracks_slurm_configuration_selectors(
    monkeypatch,
):
    import socket
    import subprocess

    policy = copy.deepcopy(DEFAULTS)
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: pytest.fail("No subprocess in preview")
    )
    first = configured(policy, environ={"SLURM_CONF": "/cluster/alpha.conf"})
    second = configured(policy, environ={"SLURM_CONF": "/cluster/beta.conf"})
    assert first["cluster"] == "" and first["connection"] != second["connection"]
    monkeypatch.setattr(socket, "gethostname", lambda: "another-submission-host")
    assert (
        configured(policy, environ={"SLURM_CONF": "/cluster/alpha.conf"})["connection"]
        != first["connection"]
    )


def test_cleanup_confirmation_consumes_the_actual_shared_ledger(tmp_path):
    from raven.connection_scope import configured_collection_scope
    from raven.platform_session import wrap_backend
    from raven.raven_collection import classify

    policy = copy.deepcopy(DEFAULTS)
    policy["site"]["cluster_id"] = "alpha"
    calls = []

    class Raw:
        def run(self, argv, timeout):
            calls.append(argv)
            return "", 0.001

    backend = wrap_backend(
        Raw(),
        {
            "raven": {"collection": {"action_validation_requests_per_minute": 1}},
            "collection_cache_dir": str(tmp_path / "cache"),
        },
        configured_collection_scope(policy),
        None,
    )
    router = SimpleNamespace(source_backend=backend, policy=policy)
    rows = candidates(router, 2)
    assert _confirmed_absent(rows, router) == {"1", "2"}
    assert len(calls) == 1
    # A second distinct batch cannot bypass the same rolling validation debit.
    rows = candidates(router, 3)
    assert _confirmed_absent(rows, router) == set()
    assert len(calls) == 1
    assert all("rolling validation" in row["reason"] for row, *_ in rows)
    assert classify(calls[0]).endpoint == "controller"


def test_cli_launch_retains_original_connection_without_a_collection_probe(
    tmp_path, monkeypatch
):
    from raven import launch

    policy = copy.deepcopy(DEFAULTS)
    seen = {}

    def main(argv, **kwargs):
        seen.update(kwargs)
        return 17

    monkeypatch.setattr(launch, "main", main)
    assert (
        launch.dispatch(
            ["launch", "job.sh", "--check"], resolved=SimpleNamespace(data=policy)
        )
        == 17
    )
    assert seen["scheduler_context"] == configured(policy)
    assert seen["connection"] is policy["connection"]


def test_cleanup_inventory_stops_before_materializing_an_unbounded_directory():
    from raven.runtime_cleanup import _entries

    consumed = []

    class Endless:
        def iterdir(self):
            while True:
                consumed.append(None)
                yield Path(str(len(consumed)))

    with pytest.raises(ValueError, match="bounded inventory"):
        _entries(Endless(), 64)
    assert len(consumed) == 65
