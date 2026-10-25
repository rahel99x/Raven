"""Exact lineage, known accounting coverage, and private execution packages."""

import copy
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import threading
import time

import pytest

from raven import projects
from raven.engineering import _storage as S, capsules as C, experiments as X


def run(project, run_id="a1", *, state="FAILED", attempt=1, job="123", retained=False):
    project.mkdir(exist_ok=True)
    raw = b"#!/bin/sh\nprintf 'science\\n'\n"
    (project / "science.sh").write_bytes(raw)
    directory = project / "runs" / run_id
    directory.mkdir(parents=True)
    inventory = dict(schema="raven.run/v1", project_id="lab", run_id=run_id,
                     experiment_id="old-name", attempt=attempt, state=state, job_id=job,
                     resources=dict(cpus=2, account="LabX", mem_bytes=1024),
                     provenance=dict(script="science.sh", script_sha256=hashlib.sha256(raw).hexdigest()))
    if retained:
        token = "a" * 32
        relative = ".raven/wrappers/" + token + "/payload.sh"
        payload = project / relative
        payload.parent.mkdir(parents=True)
        payload.write_bytes(raw)
        inventory["metadata"] = dict(raven_launch=dict(cluster="local", submission_token=token))
        inventory["paths"] = dict(submit="submit.json")
        report = dict(schema="raven.launch-report/v1",
                      plan=dict(schema="raven.launch-plan/v1", token=token, project_root=str(project),
                                script_sha256=hashlib.sha256(raw).hexdigest(), source_snapshot=relative,
                                effective_options={"cpus-per-task": "2", "account": "LabX"}, option_sources=dict(account="profile"),
                                script_args=["--seed", "13"], execution="slurm"),
                      receipt=dict(run_id=run_id, job_id=job, attempt=attempt, token=token, cluster="local"))
        (directory / "submit.json").write_text(json.dumps(report))
    (directory / "run.json").write_text(json.dumps(inventory))
    return projects.select_run(str(project), run_id)["binding"]


def native(job="123", attempt="2026-10-25/0", scheduler="slurm", scope="site-a"):
    return dict(scheduler=scheduler, scope=scope, native_id=job, attempt=attempt)


def usage(cpu=10, *, scope="allocation", complete=True):
    return dict(cpu_seconds=cpu, allocated_cpu_seconds=20, source="sacct job allocation record", scope=scope, complete=complete)


def test_continuous_identity_failed_attempt_cost_and_unknown_coverage(tmp_path):
    first = run(tmp_path, "a1", state="FAILED", job="123")
    second = run(tmp_path, "a2", state="COMPLETED", attempt=2, job="124")
    X.create(tmp_path, "science", "Lab X convergence")
    one = X.link(tmp_path, "science", first, scheduler_identity=native(), usage=usage())
    two = X.link(tmp_path, "science", second, scheduler_identity=native("124"),
                 relation="retry", parent=one["attempts"][0]["id"], usage=usage(30))
    summary = X.summarize(two)
    assert summary["states"] == {"FAILED": 1, "COMPLETED": 1}
    assert summary["totals"]["cpu_seconds"] == dict(known_sum=40, covered_attempts=2,
                                                   total_attempts=2, complete=True, unit="s")
    assert summary["totals"]["gpu_seconds"]["known_sum"] is None
    assert not summary["totals"]["gpu_seconds"]["complete"]
    # Revisiting an attempt updates evidence rather than adding its cost again.
    again = X.link(tmp_path, "science", second, scheduler_identity=native("124"),
                   relation="retry", parent=one["attempts"][0]["id"], usage=usage(35))
    assert X.summarize(again)["totals"]["cpu_seconds"]["known_sum"] == 45
    assert len(again["attempts"]) == 2
    assert X.load(tmp_path, "science") == again


def test_step_and_allocation_counters_never_mix_and_native_attempts_remain_distinct(tmp_path):
    X.create(tmp_path, "multi-site")
    first = X.link(tmp_path, "multi-site", scheduler_identity=native(), usage=usage())
    X.link(tmp_path, "multi-site", scheduler_identity=native(attempt="2026-10-25/1"),
           parent=first["attempts"][0]["id"], relation="checkpoint", usage=usage(99, scope="step"))
    result = X.link(tmp_path, "multi-site", scheduler_identity=native(scope="site-b"), usage=usage(5, complete=False))
    summary = X.summarize(result)
    assert summary["totals"]["cpu_seconds"]["known_sum"] == 15
    assert summary["totals"]["cpu_seconds"]["covered_attempts"] == 2
    assert not summary["totals"]["cpu_seconds"]["complete"]
    assert summary["external_observations"] == 3
    assert all(item["run"] is None and item["state"] == "UNKNOWN" for item in result["attempts"])


def test_experiment_ancestry_stale_revisions_and_identity_rebinding_refused(tmp_path):
    binding = run(tmp_path)
    X.create(tmp_path, "science")
    with pytest.raises(ValueError, match="Parent"):
        X.link(tmp_path, "science", binding, parent="absent")
    one = X.link(tmp_path, "science", binding, expected_revision=1)
    with pytest.raises(ValueError, match="changed since review"):
        X.link(tmp_path, "science", binding, expected_revision=1)
    with pytest.raises(ValueError, match="already linked"):
        X.link(tmp_path, "science", binding, scheduler_identity=native())
    wrong = copy.deepcopy(binding)
    wrong["job_id"] = "999"
    with pytest.raises(ValueError, match="different run attempt"):
        X.link(tmp_path, "science", wrong)
    assert X.load(tmp_path, "science") == one


def link_when_admitted(project, job, busy=None):
    """Retry only the documented rejection before a state transaction begins.

    This is a metadata test client, never a scheduler mutation retry. Keep the
    production 250 ms admission bound and expose unexpected timeout failures.
    """
    deadline = time.monotonic() + 20
    while True:
        try:
            return X.link(project, "science", scheduler_identity=native(str(job)))
        except TimeoutError as exc:
            if str(exc) != "Engineering state is busy; retry later" or time.monotonic() >= deadline:
                raise
            if busy is not None:
                busy.set()
            time.sleep(0.005)


def test_parallel_links_serialize_without_losing_attempts(tmp_path):
    X.create(tmp_path, "science")
    with ThreadPoolExecutor(max_workers=4) as pool:
        accepted = list(pool.map(lambda i: link_when_admitted(tmp_path, i), range(16)))
    result = X.load(tmp_path, "science")
    assert result["revision"] == 17
    assert len(result["attempts"]) == 16
    assert {row["scheduler_identity"]["native_id"] for row in result["attempts"]} == {str(i) for i in range(16)}
    assert {row["revision"] for row in accepted} == set(range(2, 18))


def test_busy_state_rejects_without_writes_then_explicit_client_retry_succeeds(tmp_path):
    X.create(tmp_path, "science")
    before = X.load(tmp_path, "science")
    busy = threading.Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with S.locked(tmp_path, "experiments"):
            rejected = pool.submit(X.link, tmp_path, "science", scheduler_identity=native("held"))
            with pytest.raises(TimeoutError, match="Engineering state is busy; retry later"):
                rejected.result(timeout=5)
            assert X.load(tmp_path, "science") == before
            retrying = pool.submit(link_when_admitted, tmp_path, "held", busy)
            assert busy.wait(5), "The client must observe a real busy rejection before retrying"
            assert X.load(tmp_path, "science") == before
        accepted = retrying.result(timeout=20)
    assert accepted["revision"] == 2
    assert len(accepted["attempts"]) == 1
    assert accepted["attempts"][0]["scheduler_identity"]["native_id"] == "held"
    assert X.load(tmp_path, "science") == accepted


def test_read_no_creation_and_private_anchored_storage(tmp_path):
    with pytest.raises(FileNotFoundError):
        X.load(tmp_path, "absent")
    assert not (tmp_path / ".raven").exists()
    X.create(tmp_path, "science")
    namespace = tmp_path / ".raven/engineering/experiments"
    assert stat.S_IMODE(namespace.stat().st_mode) == 0o700
    assert stat.S_IMODE((namespace / "science.json").stat().st_mode) == 0o600
    (namespace / "science.json").unlink()
    elsewhere = tmp_path / "outside.json"
    elsewhere.write_text("{}")
    (namespace / "science.json").symlink_to(elsewhere)
    with pytest.raises(OSError):
        X.load(tmp_path, "science")
    with pytest.raises(ValueError, match="private"):
        X.create(tmp_path, "science")
    assert elsewhere.read_text() == "{}"


def test_storage_rejects_shared_namespace_and_stale_directory(tmp_path):
    with S.locked(tmp_path, "test") as fd:
        S.write(fd, "item.json", {"hello": "world"})
    folder = tmp_path / ".raven/engineering/test"
    folder.chmod(0o770)
    with pytest.raises(ValueError, match="private"):
        with S.project_store(tmp_path, "test"):
            pytest.fail("insecure namespace should not open")
    folder.chmod(0o700)
    with pytest.raises(ValueError, match="directory changed"):
        with S.project_store(tmp_path, "test"):
            folder.rename(folder.with_name("moved"))
            folder.mkdir(mode=0o700)


def test_storage_detects_replaced_project_root(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    with pytest.raises(ValueError, match="Project root changed"):
        with S.locked(project, "test") as fd:
            S.write(fd, "item.json", {"test": "snapshot"})
            project.rename(tmp_path / "old-project")
            project.mkdir()


def test_experiment_tampered_identity_or_external_run_claim_is_refused(tmp_path):
    X.create(tmp_path, "science")
    document = X.link(tmp_path, "science", scheduler_identity=native())
    modified = copy.deepcopy(document)
    modified["attempts"][0]["id"] = "a" * 64
    with pytest.raises(ValueError, match="digest"):
        X.summarize(modified)
    modified = copy.deepcopy(document)
    modified["attempts"][0]["run"] = {"run_id": "invented"}
    with pytest.raises(ValueError, match="manufacture"):
        X.summarize(modified)


def test_friendly_run_id_link_preserves_verified_inventory_binding(tmp_path):
    binding = run(tmp_path)
    X.create(tmp_path, "science")
    result = X.link(tmp_path, "science", run_id="a1")
    assert result["attempts"][0]["run"]["run_id"] == "a1"
    with pytest.raises(ValueError, match="not both"):
        X.link(tmp_path, "science", binding, run_id="a1")
    summary = X.summarize(project=tmp_path, experiment_id="science")
    assert summary["attempts"] == 1


def test_full_length_experiment_identifier_has_room_for_json_suffix(tmp_path):
    experiment_id = "x" * 128
    X.create(tmp_path, experiment_id)
    assert X.load(tmp_path, experiment_id)["experiment_id"] == experiment_id
    with pytest.raises(ValueError, match="128"):
        X.create(tmp_path, "y" * 129)


def test_connect_first_run_is_atomic_idempotent_and_preserves_existing_science(tmp_path):
    first = run(tmp_path, "a1", state="FAILED", job="123")
    run(tmp_path, "a2", state="COMPLETED", job="124", attempt=2)
    connected = X.connect(tmp_path, "science", "a1", title="Original title")
    assert connected["revision"] == 1
    assert len(connected["attempts"]) == 1
    repeated = X.connect(tmp_path, "science", "a1", title="Do not overwrite")
    assert len(repeated["attempts"]) == 1
    assert repeated["title"] == "Original title"
    # Native accounting attached through the explicit link path is kept when
    # returning through Run Home's simpler connection action.
    X.create(tmp_path, "native-evidence")
    parent = X.link(tmp_path, "native-evidence", first, scheduler_identity=native(), usage=usage())
    retried = X.link(tmp_path, "native-evidence", run_id="a2", relation="retry", parent=parent["attempts"][0]["id"], usage=usage(30))
    refreshed = X.connect(tmp_path, "native-evidence", "a2")
    assert refreshed["attempts"][1]["relation"] == "retry"
    assert refreshed["attempts"][1]["parent"] == retried["attempts"][1]["parent"]
    X.connect(tmp_path, "native-evidence", "a1")
    final = X.load(tmp_path, "native-evidence")
    assert X.summarize(final)["totals"]["cpu_seconds"]["known_sum"] == 40
    assert len(final["attempts"]) == 2


def test_concurrent_first_connections_do_not_overwrite_other_runs(tmp_path):
    run(tmp_path, "a1", job="1")
    run(tmp_path, "a2", job="2")
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda key: X.connect(tmp_path, "science", key), ["a1", "a2", "a1", "a2"]))
    result = X.load(tmp_path, "science")
    assert len(result["attempts"]) == 2
    assert result["revision"] == 4
    assert {item["run"]["run_id"] for item in result["attempts"]} == {"a1", "a2"}


def test_connect_invalid_or_missing_run_creates_no_state(tmp_path):
    run(tmp_path)
    with pytest.raises(FileNotFoundError):
        X.connect(tmp_path, "science", "absent")
    with pytest.raises(ValueError):
        X.connect(tmp_path, "Scientific title with spaces", "a1")
    assert not (tmp_path / ".raven").exists()


def test_retained_identity_shared_helper_keeps_explicit_restart(tmp_path):
    binding = run(tmp_path, retained=True)
    assert X.retained_identity(binding)[-1] == 0
    changed = copy.deepcopy(binding)
    changed["launch"]["restart_count"] = 2
    assert X.retained_identity(changed)[:-1] == X.retained_identity(binding)[:-1]
    assert X.retained_identity(changed)[-1] == 2
    changed["launch"]["restart_count"] = True
    with pytest.raises(ValueError, match="restart count"):
        X.retained_identity(changed)


def test_retained_native_link_rejects_foreign_cluster_and_scheduler_family(tmp_path):
    binding = run(tmp_path, retained=True)
    X.create(tmp_path, "science")
    wrong_cluster = dict(native(), native_cluster="another-cluster")
    with pytest.raises(ValueError, match="native cluster"):
        X.link(tmp_path, "science", binding, scheduler_identity=wrong_cluster)
    with pytest.raises(ValueError, match="scheduler family"):
        X.link(tmp_path, "science", binding, scheduler_identity=native(scheduler="kubernetes"))
    assert X.load(tmp_path, "science")["attempts"] == []


def test_retained_restart_must_match_when_native_source_knows_it(tmp_path):
    binding = run(tmp_path, retained=True, attempt=3)
    path = tmp_path / "runs/a1/run.json"
    inventory = json.loads(path.read_text())
    inventory["metadata"]["raven_launch"]["restart_count"] = 2
    path.write_text(json.dumps(inventory))
    X.create(tmp_path, "science")
    with pytest.raises(ValueError, match="restart differs"):
        X.link(tmp_path, "science", binding, scheduler_identity=native(attempt="2026-10-25T00:00:00/restart:1"))
    result = X.link(tmp_path, "science", binding, scheduler_identity=native(attempt="2026-10-25T00:00:00/restart:2"))
    assert result["attempts"][0]["identity"][-1] == 2


@pytest.mark.parametrize("document", [{"x": float("nan")}, {"x": float("inf")}, {1: "bad"}, {"x": set()}])
def test_storage_rejects_invalid_json(document):
    with pytest.raises(ValueError):
        S.encoded(document)


def test_capsule_retained_payload_survives_source_edit_and_preserves_effective_settings(tmp_path, monkeypatch):
    binding = run(tmp_path, retained=True)
    (tmp_path / "science.sh").write_text("changed after execution")
    monkeypatch.setattr(C, "_git", lambda *args: (dict(status="unavailable", reason="fixture"), None))
    result = C.capture(tmp_path, binding["run_id"], environment={"OMP_NUM_THREADS": "2"}, modules=["python/3.13"],
                       seeds={"numpy": 13}, inputs=[dict(name="observations", uri="s3://lab/data", version="v17")])
    capsule = C.verify(result["path"])
    assert capsule["script"]["source"] == "retained-launch-payload"
    assert Path(result["path"], "script.sh").read_bytes().startswith(b"#!/bin/sh")
    assert capsule["effective_options"] == {"account": "LabX", "cpus-per-task": "2"}
    assert capsule["arguments"] == ["--seed", "13"]
    assert capsule["inputs"][0]["version"] == "v17"
    assert not Path(result["path"], "observations").exists()
    assert C.compare(result["path"], result["path"])["compatible_for_comparison"]


def test_capsule_unknown_context_stays_unknown_and_comparison_explains_changes(tmp_path, monkeypatch):
    run(tmp_path)
    monkeypatch.setenv("SUPER_SECRET_TOKEN", "must-not-copy")
    monkeypatch.setattr(C, "_git", lambda *args: (dict(status="unavailable", reason="fixture"), None))
    first = C.capture(tmp_path, "a1")
    assert "must-not-copy" not in Path(first["path"], "capsule.json").read_text()
    second = C.capture(tmp_path, "a1", environment={"OMP_NUM_THREADS": "8"}, seeds={"rng": 9})
    comparison = C.compare(first["path"], second["path"])
    assert {row["field"] for row in comparison["changed"]} == {"environment", "seeds", "declared"}
    assert comparison["unknown"] == ["environment", "inputs", "modules", "seeds"]
    assert not comparison["compatible_for_comparison"]
    assert not comparison["matching"]


@pytest.mark.parametrize("options", [
    {"environment": {"API_TOKEN": "danger"}}, {"environment": {"LD_PRELOAD": "a\x1bb"}},
    {"seeds": {"rng": True}}, {"inputs": [{"name": "data", "uri": "/large-data"}]},
    {"inputs": [{"name": "data", "uri": "secret", "sha256": "bad"}]},
    {"modules": ["python/3.13", "python/3.13"]},
])
def test_capsule_declarations_rejected_before_any_files_created(tmp_path, options):
    run(tmp_path)
    with pytest.raises(ValueError):
        C.capture(tmp_path, "a1", **options)
    assert not (tmp_path / ".raven").exists()


def test_changed_or_symlinked_script_refused_without_publication(tmp_path):
    run(tmp_path)
    (tmp_path / "science.sh").write_text("edited")
    with pytest.raises(ValueError, match="script changed"):
        C.capture(tmp_path, "a1")
    original = tmp_path / "science.sh"
    original.unlink()
    outside = tmp_path / "outside.sh"
    outside.write_text("edited")
    original.symlink_to(outside)
    with pytest.raises(ValueError, match="symlinks"):
        C.capture(tmp_path, "a1")
    assert not (tmp_path / ".raven").exists()


def test_capsule_integrity_tampering_incomplete_and_destination_no_clobber(tmp_path, monkeypatch):
    run(tmp_path)
    monkeypatch.setattr(C, "_git", lambda *args: (dict(status="unavailable", reason="fixture"), None))
    result = C.capture(tmp_path, "a1", destination=tmp_path / "export")
    with pytest.raises(ValueError, match="new directory"):
        C.capture(tmp_path, "a1", destination=tmp_path / "export")
    Path(result["path"], "script.sh").write_text("different")
    with pytest.raises(ValueError, match="digest"):
        C.verify(result["path"])
    Path(result["path"], ".raven-incomplete").write_text("interrupted")
    with pytest.raises(ValueError, match="incomplete"):
        C.verify(result["path"])


def test_git_capture_never_runs_repo_clean_filters_and_local_patch_is_explicit(tmp_path):
    run(tmp_path)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "science.sh"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=Fixture", "-c", "user.email=f@example.invalid",
                    "commit", "-qm", "source"], check=True)
    (tmp_path / "science.sh").write_text("edited current checkout")
    (tmp_path / ".gitattributes").write_text("science.sh filter=review\n")
    marker = tmp_path / "untrusted-filter-executed"
    subprocess.run(["git", "-C", str(tmp_path), "config", "filter.review.clean",
                    "touch " + str(marker) + "; cat"], check=True)
    metadata, patch = C._git(str(tmp_path), False, None)
    assert metadata["tracked_diff_status"] == "unknown"
    assert metadata["tracked_diff_included"] is False
    assert patch is None
    assert "capture-time" in metadata["semantics"]
    assert not marker.exists()
    with pytest.raises(ValueError, match="patch_path"):
        C.capture(tmp_path, "a1", include_diff=True)


def test_explicit_patch_is_retained_without_git_filter_or_dataset_reads(tmp_path):
    run(tmp_path)
    patch = tmp_path / "reviewed.patch"
    raw = b"--- a/science.py\n+++ b/science.py\n@@ -1 +1 @@\n-old\n+new\n"
    patch.write_bytes(raw)
    result = C.capture(tmp_path, "a1", patch_path=patch, include_diff=True)
    capsule = C.verify(result["path"])
    assert Path(result["path"], "working-tree.patch").read_bytes() == raw
    assert capsule["git"]["tracked_diff_sha256"] == hashlib.sha256(raw).hexdigest()
    assert capsule["git"]["tracked_diff_status"] == "supplied-patch"
    assert "unverified" in capsule["git"]["tracked_diff_reason"]


def test_failed_capsule_final_check_leaves_no_success_receipt(tmp_path, monkeypatch):
    run(tmp_path)
    monkeypatch.setattr(C, "_git", lambda *args: (dict(status="unavailable", reason="fixture"), None))
    original = C._script
    count = 0
    def changed(result):
        nonlocal count
        count += 1
        if count == 2:
            raise ValueError("fixture source changed")
        return original(result)
    monkeypatch.setattr(C, "_script", changed)
    with pytest.raises(ValueError, match="source changed"):
        C.capture(tmp_path, "a1", destination=tmp_path / "export")
    assert not (tmp_path / "export").exists()


def test_capsule_rerun_uses_existing_launch_review_without_mutating_or_submitting(tmp_path, monkeypatch):
    run(tmp_path, retained=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(C, "_git", lambda *args: (dict(status="unavailable", reason="fixture"), None))
    package = C.capture(tmp_path, "a1", environment={"OMP_NUM_THREADS": "2"},
                        modules=["python/3.13"], seeds={"rng": 7}, inputs=[])
    before = set(tmp_path.rglob("*"))
    result = C.relaunch_draft(package["path"])
    assert result["requires_review"] is True
    assert result["draft"]["project"] == str(tmp_path)
    assert result["draft"]["captured_plan"]["effective_options"] == {"account": "LabX", "cpus-per-task": "2"}
    assert result["declarations"]["modules"] == ["python/3.13"]
    assert result["changes"][0]["field"] == "script_location"
    assert result["unknown"] == []
    assert set(tmp_path.rglob("*")) == before
    assert "raven launch" in result["command"]
    assert "--sbatch=--account=LabX" in result["command"]


def test_capsule_rerun_refuses_external_paths_and_changed_payload(tmp_path, monkeypatch):
    project = tmp_path / "project"
    other = tmp_path / "other"
    other.mkdir()
    run(project, retained=True)
    monkeypatch.setattr(C, "_git", lambda *args: (dict(status="unavailable", reason="fixture"), None))
    package = C.capture(project, "a1", destination=other / "package")
    with pytest.raises(ValueError, match="Copy this verified capsule"):
        C.relaunch_draft(package["path"], project=project)
    Path(package["path"], "script.sh").write_text("replaced")
    with pytest.raises(ValueError, match="digest"):
        C.relaunch_draft(package["path"], project=other)


def test_real_standalone_run_capsule_and_reviewed_relaunch_integration(tmp_path, monkeypatch):
    from raven import launch, launch_runtime
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    script = tmp_path / "real.sh"
    script.write_text("#!/bin/sh\nprintf 'scientific result\\n'\n")
    plan = launch.resolve_plan([str(script), "--project", str(tmp_path), "--local", "--no-cpu-memory"], environ={})
    receipt = launch_runtime.start_local_plan(plan, environ={"PATH": "/usr/bin:/bin"})
    inventory = Path(receipt["run_dir"]) / "run.json"
    until = time.monotonic() + 10
    while time.monotonic() < until:
        if inventory.exists() and json.loads(inventory.read_text()).get("state") == "COMPLETED":
            break
        time.sleep(.02)
    assert json.loads(inventory.read_text())["state"] == "COMPLETED"
    X.create(tmp_path, "real-experiment")
    linked = X.link(tmp_path, "real-experiment", run_id=receipt["run_id"])
    assert linked["attempts"][0]["state"] == "COMPLETED"
    result = C.capture(tmp_path, receipt["run_id"], environment={}, modules=[], seeds={}, inputs=[])
    verified = C.verify(result["path"])
    assert verified["execution"] == "local"
    assert Path(result["path"], "script.sh").read_text() == script.read_text()
    rerun = C.relaunch_draft(result["path"])
    assert rerun["draft"]["execution"] == "local"
    assert rerun["unknown"] == []
    assert "--local" in rerun["command"]
    # Preparing a rerun never starts another process or manufactures a run.
    assert len(list((tmp_path / "runs").iterdir())) == 1
