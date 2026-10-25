"""Native binding, explicit reads and real local probe execution (not site qualification)."""

import copy
import hashlib
import json
import os
from pathlib import Path
import threading

import pytest

from raven import evidence as E
from raven.engineering import labs, provenance, qualification
from raven.schedulers.core import scope_digest


def workspace():
    return {"schema": labs.SCHEMA, "name": "LabX experiments", "sites": [{
        "id": "carc", "scheduler": "slurm", "login": "alice",
        "scope": {"cluster": "carc", "user": "alice", "host": "carc-login"},
        "accounts": ["LabX", "personal"], "association_source": "sacctmgr association read, 2026-10-25",
        "job_defaults": {"account": "LabX", "cpus": 6, "memory_mib": 32768},
        "heavy_defaults": {"account": "personal", "cpus": 1, "memory_mib": 512}}]}


class Client:
    scheduler = "slurm"

    def __init__(self, scope=None):
        self.scope = scope or workspace()["sites"][0]["scope"]
        self.scope_key = scope_digest(self.scope)
        self.calls = []
        self.rows = []

    def jobs(self, *, user=""):
        self.calls.append(("jobs", user))
        return self.rows

    def detail(self, identity):
        self.calls.append(("detail", identity))
        return {"identity": identity, "user": self.scope["user"], "state": "COMPLETED"}

    def fresh_detail(self, identity):
        return self.detail(identity)

    def review(self, action, *, submission):
        self.calls.append((action, submission))
        return {"action": action, "submission": submission, "digest": E.digest(submission)}


def identity(client=None):
    client = client or Client()
    return {"scheduler": client.scheduler, "scope": client.scope_key,
            "native_id": "123", "attempt": "2026-10-25T00:00:00"}


@pytest.fixture(autouse=True)
def fixture_native_compute_lookup(monkeypatch):
    """Synthetic native lookup keeps local file tests distinct from live sites."""
    def observation(config, observed_id):
        if not observed_id:
            return {"status": "unavailable", "reason": "No native job ID"}
        return {"status": "observed", "identity": {"scheduler": config["scheduler"],
                "scope": scope_digest(config["scope"]), "native_id": observed_id,
                "attempt": "2026-10-25T00:00:00"}, "user": config["scope"]["user"],
                "source_sha256": "a" * 64, "source": "synthetic native observation; no scheduler calls", "observed_at": 1000}

    monkeypatch.setattr(qualification, "_observe_native", observation)


def test_lab_save_does_no_network_preserves_billing_and_resource_purposes(tmp_path):
    value = labs.save(tmp_path, workspace())
    assert labs.load(tmp_path) == value
    assert labs.defaults(value, "carc")["resources"]["account"] == "LabX"
    assert labs.defaults(value, "carc", purpose="heavy")["resources"]["account"] == "personal"
    assert labs.defaults(value, "carc", account="")["billing"].startswith("scheduler default")
    with pytest.raises(ValueError, match="associated"):
        labs.defaults(value, "carc", account="alice")
    state = tmp_path / ".raven/engineering/labs/workspace.json"
    assert state.stat().st_mode & 0o777 == 0o600
    assert state.parent.stat().st_mode & 0o777 == 0o700


def test_lab_compare_and_swap_rejects_unreviewed_overwrite(tmp_path):
    before = labs.save(tmp_path, workspace())
    changed = workspace()
    changed["name"] = "Updated LabX"
    with pytest.raises(ValueError, match="digest"):
        labs.save(tmp_path, changed)
    after = labs.save(tmp_path, changed, expected_digest=before["digest"])
    with pytest.raises(ValueError, match="changed"):
        labs.save(tmp_path, workspace(), expected_digest=before["digest"])
    assert labs.load(tmp_path) == after


@pytest.mark.parametrize("mutate", [
    lambda doc: doc["sites"][0].update(login="other"),
    lambda doc: doc["sites"][0].update(association_source=""),
    lambda doc: doc["sites"][0]["scope"].update(token="secret"),
    lambda doc: doc["sites"][0]["job_defaults"].update(cpus=True),
    lambda doc: doc["sites"][0]["heavy_defaults"].update(account="other-lab"),
    lambda doc: doc["sites"].append(copy.deepcopy(doc["sites"][0])),
])
def test_lab_rejects_ambiguous_account_authorities(mutate):
    value = workspace()
    mutate(value)
    with pytest.raises(ValueError):
        labs.validate(value)


def test_lab_inventory_is_explicit_scoped_bounded_and_does_not_query_disconnected_sites():
    client = Client()
    client.rows = [{"id": str(index), "state": "RUNNING"} for index in range(3)]
    report = labs.inventory(workspace(), {"carc": client}, max_records=2)
    assert client.calls == [("jobs", "alice")]
    assert report["records"] == 2 and report["sites"][0]["omitted"] == 1
    client.calls.clear()
    assert labs.inventory(workspace(), {})["sites"][0]["status"] == "disconnected"
    assert not client.calls
    wrong = Client({"user": "alice", "cluster": "other"})
    assert labs.inventory(workspace(), {"carc": wrong})["sites"][0]["status"] == "blocked"
    assert not wrong.calls
    stopped = threading.Event()
    stopped.set()
    assert labs.inventory(workspace(), {"carc": client}, cancel=stopped)["sites"][0]["status"] == "deferred"
    assert not client.calls


def test_lab_missing_reads_do_not_create_state_and_symlinks_are_refused(tmp_path):
    with pytest.raises(FileNotFoundError):
        labs.load(tmp_path)
    assert not (tmp_path / ".raven").exists()
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / ".raven").symlink_to(outside, target_is_directory=True)
    with pytest.raises((OSError, ValueError)):
        labs.save(tmp_path, workspace())
    assert not list(outside.iterdir())


def test_compute_probe_prepare_is_review_only_and_compute_checks_are_real_local(tmp_path, monkeypatch):
    client = Client()
    prepared = qualification.prepare(tmp_path, client, {
        "scratch": str(tmp_path), "worker": False, "trace": True, "imports": ["raven"]})
    assert [row[0] for row in client.calls] == ["submit"]  # review(), never apply()
    assert prepared["review"]["submission"]["cpus"] == 1
    assert "srun" in Path(prepared["script"]).read_text()
    monkeypatch.setenv("SLURM_JOB_ID", "123")
    result = qualification.execute(prepared["config"])
    assert result["status"] == "passed", result
    assert {row["name"] for row in result["checks"]} >= {"import:raven", "filesystem", "metrics-recording", "trace-recording", "process-counters"}
    assert next(row for row in result["checks"] if row["name"] == "trace-recording")["detail"]["retained_events"] >= 2
    receipt = qualification.collect(tmp_path, prepared["id"], client, identity(client))
    assert receipt["identity_verified"] is True
    assert receipt["status"] == "passed"
    assert qualification.collect(tmp_path, prepared["id"], client, identity(client)) == receipt
    assert not list(tmp_path.glob("raven-qualification-*"))


def test_probe_failed_import_retains_actual_error_and_does_not_fake_gpu_or_worker_success(tmp_path, monkeypatch):
    client = Client()
    prepared = qualification.prepare(tmp_path, client, {"scratch": str(tmp_path), "worker": False,
                                                       "trace": False, "imports": ["missing_raven_qualification_module"]})
    monkeypatch.setenv("SLURM_JOB_ID", "123")
    result = qualification.execute(prepared["config"])
    assert result["status"] == "failed"
    assert "ModuleNotFoundError" in result["checks"][0]["error"]
    assert "worker-startup" not in {row["name"] for row in result["checks"]}
    assert qualification.collect(tmp_path, prepared["id"], client, identity(client))["status"] == "failed"


def test_probe_wrong_attempt_or_nonce_rejected(tmp_path, monkeypatch):
    client = Client()
    prepared = qualification.prepare(tmp_path, client, {"scratch": str(tmp_path), "worker": False, "trace": False})
    monkeypatch.setenv("SLURM_JOB_ID", "other")
    qualification.execute(prepared["config"])
    with pytest.raises(ValueError, match="another native job"):
        qualification.collect(tmp_path, prepared["id"], client, identity(client))
    path = Path(prepared["config"]["result_path"])
    value = json.loads(path.read_text())
    value.update(observed_job_id="123", nonce="wrong")
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="nonce"):
        qualification.collect(tmp_path, prepared["id"], client, identity(client))


def test_native_missing_linkage_remains_unverified_and_unsupported_features_explicit(tmp_path):
    client = Client()
    client.scheduler = "htcondor"
    prepared = qualification.prepare(tmp_path, client, {"scratch": str(tmp_path)})
    result = qualification.execute(prepared["config"])
    assert result["status"] == "partial"
    unsupported = {row["name"] for row in result["checks"] if row["status"] == "unavailable"}
    assert unsupported == {"trace-recording", "worker-startup"}
    assert qualification.collect(tmp_path, prepared["id"], client, identity(client))["status"] == "unverified"


def test_kubernetes_probe_requires_explicit_image_and_mounts(tmp_path):
    client = Client()
    client.scheduler = "kubernetes"
    prepared = qualification.prepare(tmp_path, client)
    assert prepared["status"] == "needs-manifest" and prepared["review"] is None
    assert not client.calls


@pytest.mark.parametrize("config", [{"resources": {"cpus": 100}}, {"imports": ["os;evil"]},
                                    {"python": "python"}, {"gpu": "yes"}, {"scratch": "/tmp/../etc"}])
def test_compute_probe_rejects_unbounded_or_ambiguous_config(tmp_path, config):
    with pytest.raises(ValueError):
        qualification.prepare(tmp_path, Client(), config)
    assert not (tmp_path / ".raven").exists()


def output():
    return provenance.build(identity(), "Training loss", [{"epoch": 1, "loss": .5}, {"epoch": 2, "loss": None}],
                            units={"epoch": "count", "loss": "ratio"},
                            calculation={"name": "reported values", "version": "1", "parameters": {}},
                            sources=[{"id": "metrics", "sha256": "a" * 64, "format": "raven-jsonl", "version": "1"}])


def test_provenance_retains_nulls_units_filters_and_immutable_source(tmp_path):
    value = output()
    saved = provenance.write(tmp_path, "loss-plot", value)
    assert provenance.load(tmp_path, "loss-plot") == saved
    assert saved["data"][1]["loss"] is None
    assert saved["coverage"]["complete"] is None
    assert saved["clock"]["uncertainty"] is None
    with pytest.raises(ValueError, match="exists"):
        provenance.write(tmp_path, "loss-plot", value)
    value["data"][0]["loss"] = .25
    with pytest.raises(ValueError, match="digest"):
        provenance.validate(value)


@pytest.mark.parametrize("field,value", [("units", {"epoch": "count", "loss": "percent"}),
                                        ("filters", [{"field": "phase", "operator": "eq", "value": "training"}]),
                                        ("calculation", {"name": "moving mean", "version": "1", "parameters": {"width": 3}})])
def test_plot_comparison_blocks_silent_semantic_changes(field, value):
    before = output()
    after = copy.deepcopy(before)
    after.pop("digest")
    after[field] = value
    comparison = provenance.compare(before, after)
    assert comparison["compatible"] is False
    assert comparison["blockers"]


def test_plot_comparison_requires_clock_domain_and_known_uncertainty_for_alignment():
    before = output()
    report = provenance.compare(before, before)
    assert report["compatible"] is True and report["timing_alignment_verified"] is False
    assert len(report["warnings"]) == 2
    before.pop("digest")
    before["clock"] = {"domain": "node-a/boot-a", "unit": "ns", "uncertainty": 100}
    assert provenance.compare(before, before)["timing_alignment_verified"] is True


@pytest.mark.parametrize("mutate", [
    lambda value: value["data"][0].update(loss=float("nan")),
    lambda value: value["data"][0].update(secret=1),
    lambda value: value["coverage"].update(complete=True, omitted=1),
    lambda value: value["clock"].update(uncertainty=-1),
    lambda value: value["sources"][0].update(sha256="unknown"),
    lambda value: value["sources"].append(copy.deepcopy(value["sources"][0])),
])
def test_provenance_rejects_bad_or_false_data(mutate):
    value = output()
    value.pop("digest")
    mutate(value)
    with pytest.raises(ValueError):
        provenance.validate(value)


def export_bundle(tmp_path, *, job="123"):
    bundle = tmp_path / "export"
    bundle.mkdir()
    raw = ("project,run_id,attempt,cluster,job_id,submit_time,metric,value,unit,timestamp,clock\n"
           f"/experiment,run-a1,1,carc,{job},2026-10-25,loss,0.5,ratio,1,unix-epoch-seconds UTC\n"
           f"/experiment,run-a1,1,carc,{job},2026-10-25,throughput,100,count/s,2,unix-epoch-seconds UTC\n").encode()
    (bundle / "metrics.csv").write_bytes(raw)
    manifest = {"schema": "raven.export-manifest/v1", "export_id": "fixture",
                "files": [{"path": "metrics.csv", "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}]}
    encoded = json.dumps(manifest).encode()
    (bundle / "manifest.json").write_bytes(encoded)
    (bundle / "COMPLETE.json").write_text(json.dumps({"schema": "raven.export-complete/v1", "export_id": "fixture",
                                                     "manifest_sha256": hashlib.sha256(encoded).hexdigest()}))
    return bundle


def from_export(bundle, **options):
    params = {"fields": ["timestamp", "value"], "units": {"timestamp": "s", "value": "ratio"},
              "calculation": {"name": "reported samples", "version": "1", "parameters": {}},
              "filters": [{"field": "metric", "operator": "eq", "value": "loss"}]}
    params.update(options)
    return provenance.from_export(str(bundle), "metrics.csv", identity(), "Loss", **params)


def test_research_companion_verifies_existing_export_applies_filters_preserves_original(tmp_path):
    bundle = export_bundle(tmp_path)
    original = {path.name: path.read_bytes() for path in bundle.iterdir()}
    result = from_export(bundle)
    assert result["data"] == [{"timestamp": 1.0, "value": .5}]
    assert result["coverage"]["omitted"] == 1 and result["coverage"]["complete"] is None
    assert result["filters"][0]["value"] == "loss"
    assert result["sources"][1]["sha256"] == hashlib.sha256(original["metrics.csv"]).hexdigest()
    assert original == {path.name: path.read_bytes() for path in bundle.iterdir()}


@pytest.mark.parametrize("problem", ["marker", "csv", "job", "units", "traversal", "symlink"])
def test_research_companion_refuses_ambiguous_or_changed_exports(tmp_path, problem):
    bundle = export_bundle(tmp_path, job="456" if problem == "job" else "123")
    options = {}
    if problem == "marker":
        value = json.loads((bundle / "COMPLETE.json").read_text())
        value["manifest_sha256"] = "a" * 64
        (bundle / "COMPLETE.json").write_text(json.dumps(value))
    elif problem == "csv":
        with (bundle / "metrics.csv").open("ab") as stream:
            stream.write(b"changed")
    elif problem == "units":
        options["units"] = {"timestamp": "s", "value": "percent"}
    elif problem == "traversal":
        with pytest.raises(ValueError):
            provenance.from_export(bundle, "../metrics.csv", identity(), "wrong", fields=["x"], units={"x": "s"},
                                   calculation={"name": "identity", "version": "1", "parameters": {}})
        return
    elif problem == "symlink":
        original = bundle / "metrics.csv"
        original.rename(bundle / "outside.csv")
        original.symlink_to(bundle / "outside.csv")
    with pytest.raises((OSError, ValueError)):
        from_export(bundle, **options)


def test_native_probe_review_uses_real_local_bounded_transport_for_all_batch_formats(tmp_path):
    from raven.schedulers import create
    from raven.slurm import Backend

    # These reviews read our scripts using the real bounded local file
    # transport. They issue no scheduler commands and qualify no live site.
    for name in ("slurm", "lsf", "openpbs", "pbspro", "htcondor", "flux"):
        client = create(name, Backend(), {"cluster": "test", "user": "alice",
                                        **({"uri": "local:///tmp/flux-test"} if name == "flux" else {})})
        result = qualification.prepare(tmp_path, client, {"worker": False, "trace": False, "scratch": str(tmp_path)})
        assert result["review"]["action"] == "submit"
        assert result["review"]["script_sha256"] == result["script_sha256"]
        if name == "htcondor":
            text = Path(result["script"]).read_text()
            assert text.startswith("universe = vanilla\n")
            assert "queue 1" in text and "periodic_remove" in text
            assert "wall_seconds" not in result["review"]["submission"]
        elif name in {"lsf", "flux"}:
            assert result["unverified_limits"]


def test_lab_serializable_inventory_opens_selected_connections_and_closes_failures(tmp_path, monkeypatch):
    from raven import scheduler_cli

    labs.save(tmp_path, workspace())
    opened, closed = [], []
    instance = Client()

    def connect(descriptor, **_):
        opened.append(descriptor)
        return instance

    monkeypatch.setattr(scheduler_cli, "client", connect)
    monkeypatch.setattr(scheduler_cli, "close", lambda item: closed.append(item))
    report = labs.inventory_from_workspace(tmp_path)
    assert report["sites"][0]["status"] == "available"
    assert opened[0]["scope"]["user"] == "alice"
    assert closed == [instance]


def kube_manifest(project):
    return {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": "raven-probe", "namespace": "lab"},
            "spec": {"template": {"spec": {"restartPolicy": "Never", "containers": [{
                "name": "probe", "image": "site/raven:qualified", "volumeMounts": [{"name": "project", "mountPath": str(project)}]}],
                "volumes": [{"name": "project", "persistentVolumeClaim": {"claimName": "lab-project"}}]}}}}


@pytest.mark.parametrize("mismatch", [None, "namespace", "job_uid", "missing-pod-uid"])
def test_kubernetes_probe_matches_job_uid_not_pod_uid_or_name_alone(tmp_path, monkeypatch, mismatch):
    client = Client({"cluster": "test", "user": "alice", "context": "test", "namespace": "lab", "uri": "https://cluster.example"})
    client.scheduler = "kubernetes"
    prepared = qualification.prepare(tmp_path, client, {"scratch": str(tmp_path), "worker": False, "trace": False},
                                     manifest=kube_manifest(tmp_path))
    spec = prepared["review"]["submission"]["manifest"]["spec"]
    assert spec["activeDeadlineSeconds"] == 120
    container = spec["template"]["spec"]["containers"][0]
    assert container["resources"]["limits"] == {"cpu": "1", "memory": "512Mi"}
    fields = {row["name"]: row["valueFrom"]["fieldRef"]["fieldPath"] for row in container["env"]}
    assert fields["RAVEN_QUALIFICATION_JOB_UID"] == "metadata.labels['batch.kubernetes.io/controller-uid']"
    assert fields["RAVEN_QUALIFICATION_POD_UID"] == "metadata.uid"
    monkeypatch.setenv("RAVEN_QUALIFICATION_JOB_NAME", "raven-probe")
    monkeypatch.setenv("RAVEN_QUALIFICATION_NAMESPACE", "wrong" if mismatch == "namespace" else "lab")
    monkeypatch.setenv("RAVEN_QUALIFICATION_JOB_UID", "wrong" if mismatch == "job_uid" else "job-controller-uid")
    monkeypatch.setenv("RAVEN_QUALIFICATION_POD_UID", "" if mismatch == "missing-pod-uid" else "distinct-pod-uid")
    qualification.execute(prepared["config"])
    native = {"scheduler": "kubernetes", "scope": client.scope_key, "native_id": "raven-probe",
              "attempt": "job-controller-uid", "uid": "job-controller-uid", "namespace": "lab"}
    if mismatch in {"namespace", "job_uid"}:
        with pytest.raises(ValueError, match="namespace or Job UID"):
            qualification.collect(tmp_path, prepared["id"], client, native)
    else:
        result = qualification.collect(tmp_path, prepared["id"], client, native)
        assert result["identity_verified"] is (mismatch is None)
        assert result["status"] == ("passed" if mismatch is None else "unverified")


def test_kubernetes_probe_prepare_real_adapter_uses_explicit_job_without_submission(tmp_path):
    from raven.schedulers import create
    from raven.slurm import Backend

    client = create("kubernetes", Backend(), {"cluster": "test", "user": "alice", "context": "test", "namespace": "lab", "uri": "https://cluster.example"})
    result = qualification.prepare(tmp_path, client, {"scratch": str(tmp_path)}, manifest=kube_manifest(tmp_path))
    assert result["review"]["action"] == "submit"
    assert not client.stats["requests"]


def probe_result(tmp_path, monkeypatch, *, scheduler="slurm"):
    client = Client()
    client.scheduler = scheduler
    prepared = qualification.prepare(tmp_path, client, {"scratch": str(tmp_path), "worker": False, "trace": False})
    monkeypatch.setenv("SLURM_JOB_ID", "123")
    monkeypatch.setenv("LSB_JOBID", "123")
    qualification.execute(prepared["config"])
    return client, prepared


def test_qualification_rejects_foreign_owner_and_old_report_for_new_native_attempt(tmp_path, monkeypatch):
    client, prepared = probe_result(tmp_path, monkeypatch)
    client.fresh_detail = lambda selected: {"identity": selected, "user": "bob", "state": "COMPLETED"}
    with pytest.raises(ValueError, match="owner"):
        qualification.collect(tmp_path, prepared["id"], client, identity(client))
    client.fresh_detail = lambda selected: {"identity": selected, "user": "alice", "state": "COMPLETED"}
    changed = dict(identity(client), attempt="2030-01-01/restart:99")
    with pytest.raises(ValueError, match="native attempt"):
        qualification.collect(tmp_path, prepared["id"], client, changed)


def test_id_only_compute_report_is_unverified_even_when_all_checks_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(qualification, "_observe_native", lambda *_: {"status": "unavailable", "reason": "native compute commands denied"})
    client, prepared = probe_result(tmp_path, monkeypatch)
    result = qualification.collect(tmp_path, prepared["id"], client, identity(client))
    assert result["result"]["status"] == "passed"
    assert result["status"] == "unverified" and result["identity_verified"] is False
    assert result["result"]["native_observation"]["reason"] == "native compute commands denied"


def test_qualification_collection_can_recover_from_nonterminal_and_retains_unverified_limits(tmp_path, monkeypatch):
    client, prepared = probe_result(tmp_path, monkeypatch)
    client.fresh_detail = lambda selected: {"identity": selected, "user": "alice", "state": "RUNNING"}
    first = qualification.collect(tmp_path, prepared["id"], client, identity(client))
    assert first["status"] == "partial"
    client.fresh_detail = lambda selected: {"identity": selected, "user": "alice", "state": "COMPLETED"}
    second = qualification.collect(tmp_path, prepared["id"], client, identity(client))
    assert second["status"] == "passed"
    assert qualification.load(tmp_path, prepared["id"])["latest_receipt"]["status"] == "passed"
    other = tmp_path / "lsf"
    other.mkdir()
    lsf, prepared_lsf = probe_result(other, monkeypatch, scheduler="lsf")
    result = qualification.collect(other, prepared_lsf["id"], lsf, identity(lsf))
    assert result["status"] == "partial" and result["unverified_limits"]


def test_lab_inventory_rejects_equal_scope_different_native_scheduler():
    client = Client()
    client.scheduler = "openpbs"
    result = labs.inventory(workspace(), {"carc": client})
    assert result["sites"][0]["status"] == "blocked"
    assert not client.calls


def test_numeric_filters_preserve_exact_large_integers_and_reject_incompatible_ranges():
    assert not provenance._matches({"sequence": "9007199254740993"}, provenance._filters([
        {"field": "sequence", "operator": "eq", "value": 9007199254740992}]))
    assert provenance._matches({"sequence": "9007199254740993"}, provenance._filters([
        {"field": "sequence", "operator": "eq", "value": 9007199254740993}]))
    assert provenance._numeric("9007199254740993") == 9007199254740993
    assert type(provenance._numeric("9007199254740993")) is int
    with pytest.raises(ValueError, match="compatible types"):
        provenance._filters([{"field": "sequence", "operator": "range", "value": [0, "2"]}])


def test_legacy_export_does_not_promote_arbitrary_caller_native_identity_to_verified_lineage(tmp_path):
    bundle = export_bundle(tmp_path)
    value = from_export(bundle)
    assert value["status"] == "partial" and value["association"]["status"] == "unverified"
    assert value["association"]["source_identity"]["cluster"] == "carc"
    assert value["association"]["source_identity"]["run_id"] == "run-a1"
    assert provenance.compare(value, value)["native_association_verified"] is False
    with pytest.raises(ValueError, match="source units"):
        from_export(bundle, units={"timestamp": "ns", "value": "ratio"})


def test_compute_result_write_refuses_intermediate_namespace_symlinks(tmp_path, monkeypatch):
    client = Client()
    prepared = qualification.prepare(tmp_path, client, {"scratch": str(tmp_path), "worker": False, "trace": False})
    folder = tmp_path / ".raven/engineering/qualification"
    folder.rename(folder.with_name("original"))
    outside = tmp_path / "outside"
    outside.mkdir()
    folder.symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("SLURM_JOB_ID", "123")
    with pytest.raises((OSError, ValueError)):
        qualification.execute(prepared["config"])
    assert not list(outside.iterdir())
