"""Native identity, review, bounded transport and Kubernetes UID regressions."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit

import pytest

from raven.schedulers import NAMES, create, scope_digest, validate_identity
from raven.schedulers.core import MAX_INPUT_BYTES, stdin_command
from raven.schedulers.kubernetes import KubernetesScheduler
from raven.schedulers.slurm import SlurmScheduler
from raven.slurm import Backend
from raven.transport_errors import CommandError


class FixtureBackend:
    def __init__(self, handler):
        self.handler, self.calls = handler, []

    def inspect_command(self, argv, timeout, limit, cancel, *, depth=0):
        self.calls.append(list(argv))
        return self.handler(argv)


def kubernetes_job(client, *, uid="uid-1", completed=False):
    return {"apiVersion": "batch/v1", "kind": "Job", "metadata": {
        "name": "experiment", "namespace": "lab", "uid": uid,
        "creationTimestamp": "2026-10-25T00:00:00Z",
        "labels": {"raven.dev/login": client._owner_label("alice")},
        "annotations": {"raven.dev/owner": "alice"}},
        "spec": {"parallelism": 2, "template": {"spec": {
            "restartPolicy": "Never", "containers": [{"name": "main", "image": "example:v1", "resources": {
                "requests": {"cpu": "500m", "memory": "256Mi"},
                "limits": {"cpu": "1", "memory": "1Gi", "nvidia.com/gpu": "1"}}}]}}},
        "status": {"active": 0 if completed else 1,
                   "conditions": [{"type": "Complete", "status": "True"}] if completed else []}}


def kube_client(tmp_path, handler=None, **kwargs):
    backend = FixtureBackend(handler or (lambda argv: "{}"))
    client = KubernetesScheduler(backend, {"cluster": "research", "context": "campus", "namespace": "lab",
                                 "uri": "https://cluster.example", "user": "alice"},
                                 journal_dir=tmp_path / "actions", **kwargs)
    return client, backend


def test_native_registry_is_explicit_and_does_not_run_processes():
    assert NAMES == ("slurm", "kubernetes", "lsf", "openpbs", "pbspro", "htcondor", "flux")
    backend = FixtureBackend(lambda argv: pytest.fail("constructor performed discovery I/O"))
    for name in NAMES:
        client = create(name, backend, {"cluster": "fixture", "user": "alice"})
        assert client.capabilities()["scheduler"] == name
        assert client.capabilities()["limits"]["max_inflight"] == 1
    with pytest.raises(ValueError, match="Unknown scheduler"):
        create("oracle", backend)


def test_identity_preserves_native_id_and_scope_without_slurm_numeric_assumption():
    value = {"scheduler": "openpbs", "scope": "campus", "native_id": "17[3].server", "attempt": "qtime=123/run=2"}
    assert validate_identity(value) == value
    result = validate_identity(value)
    result["native_id"] = "other"
    assert value["native_id"] == "17[3].server"
    assert scope_digest({"cluster": "a", "user": "alice"}) != scope_digest({"cluster": "b", "user": "alice"})
    assert scope_digest({"cluster": "a", "user": "alice", "account": "lab-x"}) == scope_digest({"cluster": "a", "user": "alice", "account": "lab-y"})
    assert scope_digest({"cluster": "a", "schedd": "one"}) != scope_digest({"cluster": "a", "schedd": "two"})
    with pytest.raises(ValueError):
        validate_identity({**value, "action": "cancel"})


def test_shared_transport_read_cache_metrics_and_failure_request_count(tmp_path):
    client, backend = kube_client(tmp_path, lambda argv: "one")
    assert client.read(["fixture"]) == "one"
    assert client.read(["fixture"]) == "one"
    assert len(backend.calls) == 1
    assert client.stats["cache_hits"] == 1
    assert client.stats["requests"] == 1
    assert client.stats["bytes"] == 3
    backend.handler = lambda argv: (_ for _ in ()).throw(CommandError("offline"))
    with pytest.raises(CommandError):
        client.read(["fixture"], fresh=True)
    assert client.stats["requests"] == 2
    assert client.stats["failed_requests"] == 1


def test_read_serializes_inflight_native_commands(tmp_path):
    active = peak = 0
    lock = threading.Lock()

    def read(argv):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(active, peak)
        time.sleep(.02)
        with lock:
            active -= 1
        return "{}"

    client, _ = kube_client(tmp_path, read)
    threads = [threading.Thread(target=client.read, args=(["fixture", str(index)],)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert peak == 1


def test_stdin_transport_keeps_shell_substitution_literal_and_exact():
    from raven.bounded_command import execute
    text = "#!/bin/bash\nprintf '%s' '$(touch /tmp/should-never-exist)'\n"
    assert execute(Backend(), stdin_command(["cat"], text), 2, 65536) == text
    with pytest.raises(ValueError, match="96 KiB"):
        stdin_command(["cat"], "x" * (MAX_INPUT_BYTES + 1))


def test_kubernetes_records_preserve_quantities_and_native_requests_vs_limits(tmp_path):
    client, backend = kube_client(tmp_path)
    job = kubernetes_job(client)
    backend.handler = lambda argv: json.dumps({"items": [job], "metadata": {}})
    rows = client.jobs()
    assert len(rows) == 1 and rows[0]["state"] == "RUNNING"
    assert rows[0]["identity"]["uid"] == "uid-1"
    assert rows[0]["resources"]["containers"][0]["resources"]["requests"]["cpu"] == "500m"
    assert rows[0]["resources"]["containers"][0]["resources"]["limits"]["cpu"] == "1"
    assert rows[0]["cpus"] is None and rows[0]["nodes"] is None
    command = backend.calls[-1]
    assert "--context=campus" in command and "--namespace=lab" in command
    assert "--server=https://cluster.example" in command
    uri = next(arg.removeprefix("--raw=") for arg in command if arg.startswith("--raw="))
    assert urlsplit(uri).path == "/apis/batch/v1/namespaces/lab/jobs"
    assert parse_qs(urlsplit(uri).query) == {"labelSelector": ["raven.dev/login=" + client._owner_label("alice")], "limit": ["10000"]}
    assert not client.jobs(history=True)


def test_kubernetes_history_is_retained_completed_jobs_not_invented_accounting(tmp_path):
    client, backend = kube_client(tmp_path)
    backend.handler = lambda argv: json.dumps({"items": [kubernetes_job(client, completed=True)], "metadata": {}})
    assert client.jobs() == []
    assert client.jobs(history=True)[0]["state"] == "COMPLETED"
    assert "deleted-job accounting without an external source" in client.capabilities()["missing"]


def test_kubernetes_uid_precondition_and_durable_once_only_cancel(tmp_path):
    client, backend = kube_client(tmp_path)
    job = kubernetes_job(client)
    backend.handler = lambda argv: json.dumps(job) if argv[0] == "kubectl" else '{"status":"Success"}'
    target = client._record(job)["identity"]
    plan = client.review("cancel", identity=target)
    body = json.loads(plan["argv"][4])
    assert body["preconditions"] == {"uid": "uid-1"}
    assert any("/namespaces/lab/jobs/experiment" in arg for arg in plan["argv"])
    assert client.apply(plan, plan["digest"])["status"] == "accepted"
    another = KubernetesScheduler(backend, client.scope, journal_dir=tmp_path / "actions")
    with pytest.raises(ValueError, match="already attempted"):
        another.apply(plan, plan["digest"])
    assert len([call for call in backend.calls if call[0] == "sh"]) == 1
    persisted = json.loads((tmp_path / "actions" / (plan["digest"] + ".receipt.json")).read_text())
    assert persisted["status"] == "accepted" and persisted["identity"]["uid"] == "uid-1"


def test_kubernetes_recreated_job_cannot_receive_old_cancel(tmp_path):
    client, backend = kube_client(tmp_path)
    first = kubernetes_job(client)
    backend.handler = lambda argv: json.dumps(first)
    target = client._record(first)["identity"]
    plan = client.review("cancel", identity=target)
    backend.handler = lambda argv: json.dumps(kubernetes_job(client, uid="uid-2"))
    with pytest.raises(ValueError, match="UID differs"):
        client.apply(plan, plan["digest"])
    assert not any(call[0] == "sh" for call in backend.calls)


def test_uncertain_kubernetes_cancel_consumes_token_and_does_not_retry(tmp_path):
    client, backend = kube_client(tmp_path)
    job = kubernetes_job(client)

    def handler(argv):
        if argv[0] == "sh":
            raise CommandError("transport timed out after request; outcome unknown")
        return json.dumps(job)

    backend.handler = handler
    plan = client.review("cancel", identity=client._record(job)["identity"])
    receipt = client.apply(plan, plan["digest"])
    assert receipt["status"] == "unknown" and "reconcile" in receipt["recovery"]
    with pytest.raises(ValueError, match="already attempted"):
        client.apply(plan, plan["digest"])
    assert len([call for call in backend.calls if call[0] == "sh"]) == 1


def test_kubernetes_submission_preserves_manifest_and_returned_uid(tmp_path):
    client, backend = kube_client(tmp_path)
    job = kubernetes_job(client)
    manifest = copy.deepcopy(job)
    manifest.pop("status")
    for field in ("uid", "creationTimestamp"):
        manifest["metadata"].pop(field)
    backend.handler = lambda argv: json.dumps(job)
    plan = client.review("submit", submission={"manifest": manifest})
    payload = json.loads(client._submit_argv(plan["submission"], None)[4])
    assert payload["spec"] == manifest["spec"]
    assert payload["metadata"]["namespace"] == "lab"
    receipt = client.apply(plan, plan["digest"])
    assert receipt["native_id"] == "experiment" and receipt["identity"]["uid"] == "uid-1"


def test_review_requires_digest_scope_and_private_durable_action_journal(tmp_path):
    client, backend = kube_client(tmp_path)
    job = kubernetes_job(client)
    backend.handler = lambda argv: json.dumps(job)
    plan = client.review("cancel", identity=client._record(job)["identity"])
    with pytest.raises(ValueError, match="digest"):
        client.apply(plan, "not the digest")
    changed = copy.deepcopy(plan)
    changed["argv"].append("--force")
    with pytest.raises(ValueError, match="digest"):
        client.apply(changed, plan["digest"])
    client.journal_dir = None
    with pytest.raises(ValueError, match="durable action journal"):
        client.apply(plan, plan["digest"])
    assert not any(call[0] == "sh" for call in backend.calls)


def test_kubernetes_rejects_ambient_targets_owner_changes_and_unsupported_hold(tmp_path):
    client, backend = kube_client(tmp_path)
    job = kubernetes_job(client)
    backend.handler = lambda argv: json.dumps(job)
    target = client._record(job)["identity"]
    with pytest.raises(ValueError, match="unavailable"):
        client.review("hold", identity=target)
    client = KubernetesScheduler(backend, {key: value for key, value in client.scope.items() if key != "uri"})
    with pytest.raises(ValueError, match="Pin the Kubernetes"):
        client._action("cancel", client._record(job))
    client = KubernetesScheduler(backend, {key: value for key, value in client.scope.items() if key != "context"})
    with pytest.raises(ValueError, match="explicit context"):
        client.jobs()


@pytest.mark.parametrize("version", ["23.11.11", "24.05.8", "24.11.7", "26.05.5"])
def test_slurm_reuses_native_recorded_json_field_parsers(version):
    fixture = Path(__file__).parent / "fixtures/slurm/recorded" / version / "final/running-squeue-json.stdout.txt"
    if not fixture.exists():
        fixture = fixture.parent.parent / fixture.name
    document = json.loads(fixture.read_text())
    owner = document["jobs"][0]["user_name"]
    backend = FixtureBackend(lambda argv: json.dumps(document))
    client = SlurmScheduler(backend, {"cluster": "raven-ci", "user": owner})
    rows = client.jobs()
    assert rows and all(row["identity"]["scheduler"] == "slurm" for row in rows)
    assert all("/restart:" in row["identity"]["attempt"] for row in rows)
    assert all(row["cpus"] > 0 for row in rows)
    assert "--clusters" in backend.calls[0] and "--user" in backend.calls[0]


def test_slurm_script_change_prevents_mutation_and_exact_bytes_are_streamed(tmp_path):
    script = ["#!/bin/bash\n#SBATCH --job-name=test\nprintf '%s' 'hello'\n"]
    backend = FixtureBackend(lambda argv: script[0] if argv[0] == "sh" and argv[3] == "raven-script" else "17;raven-ci\n")
    client = SlurmScheduler(backend, {"cluster": "raven-ci", "user": "alice"}, journal_dir=tmp_path / "actions")
    plan = client.review("submit", submission={"script": "/shared/job.sh", "cpus": 2, "memory_mib": 1024, "wall_seconds": 125})
    assert "--time=2:05" in plan["argv"]
    script[0] += "# changed\n"
    with pytest.raises(ValueError, match="script changed"):
        client.apply(plan, plan["digest"])
    plan = client.review("submit", submission={"script": "/shared/job.sh"})
    receipt = client.apply(plan, plan["digest"])
    assert receipt["native_id"] == "17"
    assert backend.calls[-1][4] == script[0]
    assert backend.calls[-1][-1] == "--parsable"


def test_script_read_enforces_recording_size_and_text_fidelity_before_review(tmp_path):
    backend = FixtureBackend(lambda argv: "x" * (MAX_INPUT_BYTES + 1))
    client = SlurmScheduler(backend, {"cluster": "fixture", "user": "alice"})
    with pytest.raises(ValueError, match="96 KiB"):
        client.review("submit", submission={"script": "/shared/job.sh"})
    backend.handler = lambda argv: "invalid \ufffd text"
    with pytest.raises(ValueError, match="unambiguous UTF-8"):
        client.review("submit", submission={"script": "/shared/job.sh"})


def test_native_transport_cannot_publish_oversized_or_nontext_observations(tmp_path):
    client, backend = kube_client(tmp_path, lambda argv: "x" * 101, max_bytes=100)
    with pytest.raises(CommandError, match="bounded text contract"):
        client.read(["fixture"])
    backend.handler = lambda argv: b"bytes"
    with pytest.raises(CommandError, match="bounded text contract"):
        client.read(["fixture"])


def test_mutating_connection_scope_cannot_reuse_old_native_identity(tmp_path):
    client, backend = kube_client(tmp_path)
    client.scope["uri"] = "https://different-cluster.example"
    with pytest.raises(ValueError, match="create a new connection"):
        client.read(["fixture"])
    assert backend.calls == []


def test_write_only_kubernetes_submission_cannot_retarget_a_mutated_scope(tmp_path):
    client, backend = kube_client(tmp_path)
    job = kubernetes_job(client)
    manifest = copy.deepcopy(job)
    manifest.pop("status")
    for key in ("uid", "creationTimestamp"):
        manifest["metadata"].pop(key)
    plan = client.review("submit", submission={"manifest": manifest})
    client.scope["uri"] = "https://different-cluster.example"
    for operation in (lambda: client.review("submit", submission={"manifest": manifest}),
                      lambda: client.apply(plan, plan["digest"]),
                      client.capabilities,
                      lambda: client.identity("experiment", "uid-1")):
        with pytest.raises(ValueError, match="scope changed"):
            operation()
    assert backend.calls == []
    assert not (tmp_path / "actions").exists()


def test_duplicate_native_attempts_cannot_double_count_resource_totals(tmp_path):
    client, backend = kube_client(tmp_path)
    job = kubernetes_job(client)
    backend.handler = lambda argv: json.dumps({"items": [job, job]})
    with pytest.raises(ValueError, match="duplicate exact native attempt"):
        client.jobs()


def test_native_receipt_output_preview_is_bounded_and_persisted(tmp_path):
    client, backend = kube_client(tmp_path)
    job = kubernetes_job(client)
    backend.handler = lambda argv: json.dumps(job) if argv[0] == "kubectl" else "x" * 20000
    plan = client.review("cancel", identity=client._record(job)["identity"])
    receipt = client.apply(plan, plan["digest"])
    assert len(receipt["output"]) == 16384 and receipt["output_bytes"] == 20000
    assert receipt["output_truncated"] is True
    assert json.loads((tmp_path / "actions" / (plan["digest"] + ".receipt.json")).read_text()) == receipt


def test_slurm_explicit_cpu_totals_and_inherited_controls_cannot_multiply_trials(tmp_path, monkeypatch):
    client = SlurmScheduler(Backend(), {"cluster": "fixture", "user": "alice"})
    argv = client._submission({"script": "/shared/run.sh", "cpus": 8, "nodes": 2})
    assert "--ntasks=2" in argv and "--ntasks-per-node=1" in argv and "--cpus-per-task=4" in argv
    with pytest.raises(ValueError, match="divide evenly"):
        client._submission({"script": "/shared/run.sh", "cpus": 3, "nodes": 2})
    monkeypatch.setenv("SBATCH_ARRAY_INX", "1-10000")
    monkeypatch.setenv("SBATCH_NTASKS", "10000")
    command = client._submit_argv({"script": "/shared/run.sh", "cpus": 1}, "#!/bin/bash\n")
    # Substitute the receiving executable, leaving the production fixed shell
    # program and positional native argv transport intact.
    command[5:] = [sys.executable, "-c", "import json,os,sys; print(json.dumps({'array':os.getenv('SBATCH_ARRAY_INX'),'tasks':os.getenv('SBATCH_NTASKS'),'script':sys.stdin.read()}))"]
    from raven.bounded_command import execute
    observed = json.loads(execute(Backend(), command, 2, 65536))
    assert observed == {"array": None, "tasks": None, "script": "#!/bin/bash\n"}


def test_slurm_history_is_bounded_before_scheduler_execution():
    backend = FixtureBackend(lambda argv: '{"jobs":[]}')
    client = SlurmScheduler(backend, {"user": "alice"})
    assert client.jobs(history=True) == []
    argv = backend.calls[-1]
    assert "--starttime=now-2days" in argv and "--allocations" in argv and "--duplicates" in argv
    assert argv[-2:] == ["--user", "alice"]


def test_slurm_mutation_requires_an_explicit_native_cluster():
    backend = FixtureBackend(lambda argv: pytest.fail("No native I/O should precede explicit cluster selection"))
    client = SlurmScheduler(backend, {"user": "alice"})
    with pytest.raises(ValueError, match="actual Slurm ClusterName"):
        client.review("submit", submission={"script": "/shared/run.sh"})


def test_transport_endpoint_mutation_cannot_retarget_a_write_only_submission(tmp_path):
    client, backend = kube_client(tmp_path)
    backend.host = "first.example"
    # Capture this declared transport on a fresh client, then alter the port.
    client = KubernetesScheduler(backend, client.scope, journal_dir=tmp_path / "actions")
    job = kubernetes_job(client)
    manifest = copy.deepcopy(job)
    manifest.pop("status")
    for key in ("uid", "creationTimestamp"):
        manifest["metadata"].pop(key)
    plan = client.review("submit", submission={"manifest": manifest})
    backend.host = "different.example"
    with pytest.raises(ValueError, match="transport changed"):
        client.apply(plan, plan["digest"])
    assert backend.calls == []


def test_slurm_source_cluster_is_bound_even_with_default_endpoint():
    fixture = Path(__file__).parent / "fixtures/slurm/recorded/26.05.5/final/running-squeue-json.stdout.txt"
    document = json.loads(fixture.read_text())
    owner = document["jobs"][0]["user_name"]
    backend = FixtureBackend(lambda argv: json.dumps(document))
    client = SlurmScheduler(backend, {"user": owner})
    assert client.jobs()[0]["identity"]["native_cluster"] == "raven-ci"
    wrong = SlurmScheduler(backend, {"cluster": "different-site", "user": owner})
    with pytest.raises(ValueError, match="native cluster differs"):
        wrong.jobs()


@pytest.mark.parametrize("mutation", [
    lambda job: job.update(metadata=None),
    lambda job: job.update(spec=None),
    lambda job: job.update(status=None),
    lambda job: job["metadata"].update(annotations=None),
    lambda job: job["spec"].update(template=None),
    lambda job: job["spec"]["template"].update(spec=None),
    lambda job: job["spec"]["template"]["spec"].update(containers=[None]),
    lambda job: job["status"].update(conditions=[None]),
])
def test_malformed_kubernetes_record_is_a_diagnosed_value_error(tmp_path, mutation):
    client, _ = kube_client(tmp_path)
    raw = kubernetes_job(client)
    mutation(raw)
    with pytest.raises(ValueError):
        client._record(raw)


@pytest.mark.parametrize("reply_change", ["name", "owner", "malformed"])
def test_kubernetes_submission_reply_must_match_reviewed_name_and_owner(tmp_path, reply_change):
    client, backend = kube_client(tmp_path)
    original = kubernetes_job(client)
    manifest = copy.deepcopy(original)
    manifest.pop("status")
    for key in ("uid", "creationTimestamp"):
        manifest["metadata"].pop(key)
    plan = client.review("submit", submission={"manifest": manifest})
    reply = copy.deepcopy(original)
    if reply_change == "name":
        reply["metadata"]["name"] = "unrelated"
    elif reply_change == "owner":
        reply["metadata"]["annotations"].clear()
        reply["metadata"]["labels"].clear()
    else:
        reply["metadata"] = None
    backend.handler = lambda argv: json.dumps(reply)
    receipt = client.apply(plan, plan["digest"])
    assert receipt["status"] == "unknown"
    assert "identity" not in receipt and "native_id" not in receipt
    with pytest.raises(ValueError, match="already attempted"):
        client.apply(plan, plan["digest"])


def test_observe_reads_one_exact_authoritative_attempt(tmp_path):
    client, backend = kube_client(tmp_path)
    backend.handler = lambda argv: json.dumps(kubernetes_job(client))
    first = client.observe("experiment")
    backend.handler = lambda argv: json.dumps(kubernetes_job(client, uid="uid-2"))
    second = client.observe("experiment")
    assert first["identity"]["uid"] == "uid-1" and second["identity"]["uid"] == "uid-2"
    assert len(backend.calls) == 2


def test_slurm_native_exact_cluster_json_prefix_is_verified_not_arbitrarily_stripped():
    fixture = Path(__file__).parent / "fixtures/slurm/recorded/26.05.5/final/running-squeue-json.stdout.txt"
    document = fixture.read_text()
    owner = json.loads(document)["jobs"][0]["user_name"]
    backend = FixtureBackend(lambda argv: "CLUSTER: raven-ci\n" + document)
    client = SlurmScheduler(backend, {"cluster": "raven-ci", "user": owner})
    assert client.jobs()[0]["identity"]["native_cluster"] == "raven-ci"
    for prefix in ("CLUSTER: unrelated\n", "diagnostic preamble\n", "CLUSTER: raven-ci\nCLUSTER: raven-ci\n"):
        backend.handler = lambda argv, prefix=prefix: prefix + document
        client._cache.clear()
        with pytest.raises(ValueError):
            client.jobs()
    multi = SlurmScheduler(backend, {"cluster": "raven-ci,other", "user": owner})
    with pytest.raises(ValueError, match="One exact Slurm ClusterName"):
        multi.jobs()


def test_malformed_security_fields_produce_value_errors_not_type_errors(tmp_path):
    with pytest.raises(ValueError, match="Unknown scheduler"):
        validate_identity({"scheduler": [], "scope": "a", "native_id": "1", "attempt": "a"})
    client, backend = kube_client(tmp_path)
    with pytest.raises(ValueError, match="scope must be an object"):
        KubernetesScheduler(backend, [])
    job = kubernetes_job(client)
    job["status"]["conditions"] = [{"type": "Complete", "status": {}}]
    with pytest.raises(ValueError, match="condition fields"):
        client._record(job)


def test_kubernetes_cancel_cannot_delete_completed_retained_history(tmp_path):
    client, backend = kube_client(tmp_path)
    job = kubernetes_job(client, completed=True)
    backend.handler = lambda argv: json.dumps(job)
    with pytest.raises(ValueError, match="Only an active Kubernetes Job"):
        client.review("cancel", identity=client._record(job)["identity"])
    assert not (tmp_path / "actions").exists()
