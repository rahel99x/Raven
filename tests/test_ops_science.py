from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import threading
from types import SimpleNamespace

import pytest

from raven import ops_science as ops, science_files as sf, science_probe
from raven.operations import Context


def dump(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    return str(path)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def ctx(tmp_path):
    return Context(scope={"profile": "test", "cluster": "local"}, state_dir=str(tmp_path), cancel=threading.Event())


def environment(tmp_path):
    lock = tmp_path/"requirements.lock"
    lock.write_text("some-package==1.2.3 --hash=sha256:" + "a"*64 + "\n")
    doc = {"schema": "raven.environment/v1", "python": platform.python_version(), "platform": platform.system(),
           "machine": platform.machine(), "packages": {"some_package": "1.2.3"}, "lockfile": {"path": lock.name, "sha256": sha(lock)}}
    return {"manifest": dump(tmp_path/"environment.json", doc), "destination": str(tmp_path/"bundle")}, doc


def reuse(tmp_path):
    dependencies = []
    identity = {"project": "hydrology", "inputs_sha256": {}}
    for role in ("code", "environment", "parameters", "input"):
        path = tmp_path/(role+".txt")
        path.write_text(role+" data\n")
        dependencies.append({"path": path.name, "sha256": sha(path), "role": role})
        if role != "input":
            identity[role+"_sha256"] = sha(path)
        else:
            identity["inputs_sha256"][path.name] = sha(path)
    output = tmp_path/"answer.dat"
    output.write_text("scientific output\n")
    policy_path, results_path = tmp_path/"policy.json", tmp_path/"results.json"
    dump(policy_path, {"schema": "raven.acceptance/v1", "cases": [{"id": "run1", "expected": {"score": {"value": 1, "absolute": .01, "relative": 0}}}]})
    dump(results_path, {"schema": "raven.results/v1", "runs": [{"id": "run1", "metrics": {"score": 1}}]})
    doc = {"schema": "raven.reuse/v1", "identity": identity, "dependencies": dependencies,
           "outputs": [{"path": output.name, "sha256": sha(output), "target": "results/output.dat"}],
           "acceptance": {"path": policy_path.name, "sha256": sha(policy_path)}, "results": {"path": results_path.name, "sha256": sha(results_path)}}
    params = {"manifest": dump(tmp_path/"reuse.json", doc), "identity": dump(tmp_path/"identity.json", identity), "destination": str(tmp_path/"reused")}
    return params, doc


def test_environment_inspection_does_not_write_then_reviewed_bundle_is_no_clobber(tmp_path, ctx):
    params, _ = environment(tmp_path)
    result = ops.run("environment", params, ctx)
    assert not Path(params["destination"]).exists()
    assert result["plan"]
    applied = ops.apply("environment", result["plan"], ctx)
    assert applied["status"] == "ok"
    script = Path(params["destination"])/"recreate.sh"
    assert script.stat().st_mode & 0o100
    assert "--require-hashes --no-deps" in script.read_text()
    assert subprocess.run(["sh", "-n", str(script)], capture_output=True).returncode == 0
    with pytest.raises(ValueError, match="new directory"):
        ops.apply("environment", result["plan"], ctx)


def test_environment_without_destination_is_read_only(tmp_path, ctx):
    params, _ = environment(tmp_path)
    params.pop("destination")
    assert "plan" not in ops.run("environment", params, ctx)


@pytest.mark.parametrize("badline", ["-r /etc/passwd", "--index-url https://evil.invalid", "-e .", "pkg @ file:///tmp/pkg", "pkg==1", "pkg>=1 --hash=sha256:"+"a"*64])
def test_environment_rejects_unpinned_or_option_lock_entries(tmp_path, ctx, badline):
    params, doc = environment(tmp_path)
    lock = tmp_path/"requirements.lock"
    lock.write_text(badline+"\n")
    doc["lockfile"]["sha256"] = sha(lock)
    dump(Path(params["manifest"]), doc)
    with pytest.raises(ValueError):
        ops.run("environment", params, ctx)


@pytest.mark.parametrize("target", ["environment.json", "requirements.lock"])
def test_environment_revalidates_sources_before_bundle_write(tmp_path, ctx, target):
    params, _ = environment(tmp_path)
    plan = ops.run("environment", params, ctx)["plan"]
    with (tmp_path/target).open("a") as stream:
        stream.write("\n")
    with pytest.raises(ValueError, match="changed"):
        ops.apply("environment", plan, ctx)
    assert not Path(params["destination"]).exists()


def test_reuse_requires_scientific_acceptance_and_copies_all_verified_outputs(tmp_path, ctx):
    params, _ = reuse(tmp_path)
    reviewed = ops.run("reuse", params, ctx)
    assert not Path(params["destination"]).exists()
    output = ops.apply("reuse", reviewed["plan"], ctx)
    assert output["status"] == "ok"
    assert (Path(params["destination"])/"results/output.dat").read_bytes() == (tmp_path/"answer.dat").read_bytes()
    receipt = json.loads((Path(params["destination"])/".raven-receipt.json").read_text())
    assert receipt["acceptance"]["passed"] == receipt["acceptance"]["total"] == 1
    assert not (Path(params["destination"])/".raven-incomplete").exists()


@pytest.mark.parametrize("target", ["code.txt", "environment.txt", "parameters.txt", "input.txt", "answer.dat", "policy.json", "results.json", "reuse.json", "identity.json"])
def test_every_reviewed_reuse_source_is_bound_against_toctou(tmp_path, ctx, target):
    params, _ = reuse(tmp_path)
    plan = ops.run("reuse", params, ctx)["plan"]
    with (tmp_path/target).open("a") as stream:
        stream.write(" ")
    with pytest.raises(ValueError, match="changed"):
        ops.apply("reuse", plan, ctx)
    assert not Path(params["destination"]).exists()


@pytest.mark.parametrize("field", ["code_sha256", "environment_sha256", "parameters_sha256", "project"])
def test_reuse_full_requested_identity_must_match(tmp_path, ctx, field):
    params, doc = reuse(tmp_path)
    wanted = dict(doc["identity"])
    wanted[field] = "changed" if field == "project" else "a"*64
    dump(Path(params["identity"]), wanted)
    with pytest.raises(ValueError, match="identity"):
        ops.run("reuse", params, ctx)


def test_reuse_failed_or_missing_acceptance_cannot_pass(tmp_path, ctx):
    params, doc = reuse(tmp_path)
    path = tmp_path/"results.json"
    dump(path, {"schema": "raven.results/v1", "runs": []})
    doc["results"]["sha256"] = sha(path)
    dump(Path(params["manifest"]), doc)
    with pytest.raises(ValueError, match="acceptance failed"):
        ops.run("reuse", params, ctx)


@pytest.mark.parametrize("target", ["../outside", "/tmp/outside", ".", "", "dir/", ".raven-receipt.json"])
def test_reuse_destinations_cannot_escape_or_collide_with_metadata(tmp_path, ctx, target):
    params, doc = reuse(tmp_path)
    doc["outputs"][0]["target"] = target
    dump(Path(params["manifest"]), doc)
    with pytest.raises(ValueError):
        ops.run("reuse", params, ctx)


def test_reuse_overlapping_targets_rejected(tmp_path, ctx):
    params, doc = reuse(tmp_path)
    doc["outputs"].append(dict(doc["outputs"][0], target="results/output.dat/nested"))
    dump(Path(params["manifest"]), doc)
    with pytest.raises(ValueError, match="overlap"):
        ops.run("reuse", params, ctx)


@pytest.mark.parametrize("feature", ["environment", "reuse"])
@pytest.mark.parametrize("mode", ["remote", "replay", "cancelled"])
def test_local_operations_fail_closed_outside_target_connection(tmp_path, ctx, feature, mode):
    params, _ = (environment if feature == "environment" else reuse)(tmp_path)
    if mode == "remote":
        ctx = replace(ctx, files=SimpleNamespace(remote=True))
    elif mode == "replay":
        ctx = replace(ctx, replay=True)
    else:
        ctx.cancel.set()
    with pytest.raises(ValueError):
        ops.run(feature, params, ctx)
    assert not Path(params["destination"]).exists()


@pytest.mark.parametrize("source", [b'{"a":1,"a":2}', b'{"a":1e999}', b'{"a":NaN}', b'\xff', b'['*1000+b']'*1000])
def test_metadata_parser_rejects_nonfinite_duplicate_deep_or_bad_utf8(tmp_path, source):
    path = tmp_path/"invalid.json"
    path.write_bytes(source)
    with pytest.raises((ValueError, UnicodeError)):
        sf.json_file(path)


def test_snapshot_rejects_symlink_fifo_and_oversize(tmp_path):
    path = tmp_path/"data"
    path.write_bytes(b"abc")
    alias = tmp_path/"alias"
    alias.symlink_to(path)
    with pytest.raises(OSError):
        sf.snapshot(alias)
    with pytest.raises(ValueError):
        sf.snapshot(path, limit=2)
    fifo = tmp_path/"fifo"
    os.mkfifo(fifo)
    with pytest.raises(ValueError):
        sf.snapshot(fifo)


def test_copy_source_changed_during_stream_is_not_published(tmp_path, monkeypatch):
    source = tmp_path/"source"
    source.write_bytes(b"original data")
    record, _ = sf.snapshot(source)
    original_read = sf.os.read
    changed = False
    def read(fd, size):
        nonlocal changed
        value = original_read(fd, size)
        if value and not changed:
            changed = True
            source.write_bytes(b"modified data")
        return value
    monkeypatch.setattr(sf.os, "read", read)
    target = tmp_path/"copied"
    with pytest.raises(ValueError, match="changed"):
        sf.publish(target, copies=[(record, "result")])
    assert not target.exists()


class Scheduler:
    def __init__(self):
        self.jobs = {
            "10": dict(JobId="10", UserId="alice(1000)", SubmitTime="2026-01-01T01:00:00", StartTime="Unknown", RestartCnt="0", JobState="PENDING", Dependency="afterok:5", NumNodes="1", NodeList="n1"),
            "11": dict(JobId="11", UserId="alice(1000)", SubmitTime="2026-01-01T02:00:00", StartTime="Unknown", RestartCnt="0", JobState="PENDING", Dependency="afterok:5", NumNodes="1", NodeList="n1"),
            "5": dict(JobId="5", UserId="alice(1000)", SubmitTime="2026-01-01T00:00:00", StartTime="2026-01-01T00:01:00", RestartCnt="0", JobState="COMPLETED", Dependency="(null)"),
            "6": dict(JobId="6", UserId="alice(1000)", SubmitTime="2026-01-01T00:00:00", StartTime="2026-01-01T00:01:00", RestartCnt="0", JobState="COMPLETED", Dependency="(null)")}
        self.calls = []
        self.fail_job = ""
        self.probe = None
    def run(self, argv, timeout=8):
        self.calls.append(argv)
        if argv[0] == "srun":
            return self.probe, .01
        if argv[:3] == ["scontrol", "show", "job"]:
            return " ".join(f"{key}={value}" for key, value in self.jobs[argv[-1]].items()), .01
        if argv[:2] == ["scontrol", "update"]:
            jid = argv[2].split("=", 1)[1]
            if jid == self.fail_job:
                raise RuntimeError("simulated timeout")
            self.jobs[jid]["Dependency"] = argv[3].split("=", 1)[1]
            return "", .01
        raise AssertionError(argv)


def scheduler_context(ctx):
    backend = Scheduler()
    return replace(ctx, slurm=SimpleNamespace(b=backend, user="alice")), backend


def repair(tmp_path, backend, ids=("10",)):
    rows = [{"job_id": jid, "submit_time": backend.jobs[jid]["SubmitTime"], "dependency": "afterok:6"} for jid in ids]
    return {"manifest": dump(tmp_path/"repair.json", {"schema": "raven.dependency-repair/v1", "repairs": rows})}


def test_dependency_repair_preserves_successful_jobs_and_confirms_pending_updates(tmp_path, ctx):
    ctx, backend = scheduler_context(ctx)
    params = repair(tmp_path, backend, ("10", "11"))
    plan = ops.run("dependency-repair", params, ctx)["plan"]
    assert not any(call[1] == "update" for call in backend.calls)
    result = ops.apply("dependency-repair", plan, ctx)
    assert result["status"] == "ok"
    assert backend.jobs["5"]["JobState"] == backend.jobs["6"]["JobState"] == "COMPLETED"
    assert [call[2] for call in backend.calls if call[1] == "update"] == ["JobId=10", "JobId=11"]


def test_dependency_partial_failure_stops_and_retains_precise_outcomes(tmp_path, ctx):
    ctx, backend = scheduler_context(ctx)
    params = repair(tmp_path, backend, ("10", "11"))
    plan = ops.run("dependency-repair", params, ctx)["plan"]
    backend.fail_job = "11"
    result = ops.apply("dependency-repair", plan, ctx)
    assert result["status"] == "partial"
    assert [row["status"] for row in result["data"]["outcomes"]] == ["confirmed", "uncertain"]


@pytest.mark.parametrize("key,value", [("RestartCnt", "1"), ("SubmitTime", "2027-01-01T00:00:00"), ("Dependency", "afterany:6"), ("UserId", "bob(1001)")])
def test_dependency_change_after_review_prevents_any_update(tmp_path, ctx, key, value):
    ctx, backend = scheduler_context(ctx)
    plan = ops.run("dependency-repair", repair(tmp_path, backend), ctx)["plan"]
    backend.jobs["10"][key] = value
    with pytest.raises(ValueError, match="changed"):
        ops.apply("dependency-repair", plan, ctx)
    assert not any(call[1] == "update" for call in backend.calls)


@pytest.mark.parametrize("state", ["RUNNING", "COMPLETED", "FAILED"])
def test_only_pending_jobs_can_be_repaired(tmp_path, ctx, state):
    ctx, backend = scheduler_context(ctx)
    backend.jobs["10"]["JobState"] = state
    with pytest.raises(ValueError, match="pending"):
        ops.run("dependency-repair", repair(tmp_path, backend), ctx)


def test_dependency_indirect_cycle_is_rejected(tmp_path, ctx):
    ctx, backend = scheduler_context(ctx)
    backend.jobs["6"].update(JobState="PENDING", Dependency="afterok:11")
    backend.jobs["11"]["Dependency"] = "afterok:10"
    with pytest.raises(ValueError, match="cycle"):
        ops.run("dependency-repair", repair(tmp_path, backend), ctx)


@pytest.mark.parametrize("value", ["afterok:1,afterany:2?afterok:3", "afterwhatever:1", "afterok:1;touch", "afterok:1 2", "afterok:", "afterok:1_2_3", "singleton:1"])
def test_invalid_dependency_syntax(value):
    with pytest.raises(ValueError):
        ops._dependency(value)


def test_long_dependency_cycle_check_avoids_python_recursion():
    graph = {str(i): [str(i+1)] for i in range(10000)}
    ops._cycles(graph)
    graph["10000"] = ["0"]
    with pytest.raises(ValueError, match="cycle"):
        ops._cycles(graph)


def test_implicit_singleton_edges_cannot_be_certified_as_acyclic(tmp_path, ctx):
    ctx, backend = scheduler_context(ctx)
    backend.jobs["6"].update(JobState="PENDING", Dependency="singleton")
    with pytest.raises(ValueError, match="implicit"):
        ops.run("dependency-repair", repair(tmp_path, backend), ctx)


def test_placement_reads_existing_processes_without_affinity_mutation(tmp_path, ctx):
    ctx, backend = scheduler_context(ctx)
    backend.jobs["10"].update(JobState="RUNNING", StartTime="2026-01-01T01:01:00", NumCPUs="8", AllocTRES="cpu=8,gres/gpu=1")
    sample = {"job_id": "10", "node": "n1", "warnings": [], "processes": [{"pid": 123, "cpu_allowed": "0-7", "numa_pages": {"0": 32}, "gpu_visibility": {"CUDA_VISIBLE_DEVICES": "0"}}]}
    backend.probe = "0: RAVEN_PLACEMENT " + json.dumps(sample)
    result = ops.run("placement", {"job_id": "10"}, ctx)
    assert result["status"] == "ok"
    argv = next(call for call in backend.calls if call[0] == "srun")
    assert "--overlap" in argv
    assert "sched_setaffinity" not in argv[argv.index("-c")+1]
    assert not any(call[:2] == ["scontrol", "update"] for call in backend.calls)


def test_placement_discards_requeued_attempt(ctx):
    ctx, backend = scheduler_context(ctx)
    backend.jobs["10"].update(JobState="RUNNING")
    backend.probe = "RAVEN_PLACEMENT " + json.dumps({"job_id": "10", "node": "n1", "warnings": [], "processes": []})
    original = backend.run
    def run(argv, timeout=8):
        value = original(argv, timeout)
        if argv[0] == "srun":
            backend.jobs["10"]["RestartCnt"] = "1"
        return value
    backend.run = run
    with pytest.raises(ValueError, match="changed"):
        ops.run("placement", {"job_id": "10"}, ctx)


@pytest.mark.parametrize("output", ["", "RAVEN_PLACEMENT {}", 'RAVEN_PLACEMENT {"job_id":"11","node":"n1"}', 'RAVEN_PLACEMENT {"job_id":"10","node":"n1","processes":{},"warnings":[]}'])
def test_placement_rejects_unattributed_or_malformed_reply(output):
    with pytest.raises(ValueError):
        science_probe.parse(output, "10")


def test_placement_probe_source_compiles_and_has_resource_budgets():
    compile(science_probe.SOURCE, "placement-probe", "exec")
    assert "32 * 1048576" in science_probe.SOURCE
    assert "candidates[:256]" in science_probe.SOURCE
    assert "members = set(listpids())" in science_probe.SOURCE


@pytest.mark.parametrize("reference", ["docker://example/image:latest", "docker://example/image", "docker://example/image@sha256:123", "docker://example/image@sha256:"+"A"*64])
def test_environment_refuses_mutable_or_malformed_container_identity(tmp_path, ctx, reference):
    params, doc = environment(tmp_path)
    doc["container"] = {"kind": "oci", "reference": reference}
    dump(Path(params["manifest"]), doc)
    with pytest.raises(ValueError, match="digest"):
        ops.run("environment", params, ctx)


@pytest.mark.parametrize("container_kind", ["oci", "sif"])
def test_container_recipe_is_digest_pinned_and_python_fragments_compile(tmp_path, ctx, container_kind):
    import shlex
    params, doc = environment(tmp_path)
    if container_kind == "oci":
        doc["container"] = {"kind": "oci", "reference": "docker://example/image@sha256:"+"a"*64}
    else:
        image = tmp_path/"image.sif"
        image.write_bytes(b"test SIF bytes; not executed")
        doc["container"] = {"kind": "sif", "path": image.name, "sha256": sha(image)}
    doc["native"] = {"libc": "glibc", "minimum_version": "2.28"}
    dump(Path(params["manifest"]), doc)
    plan = ops.run("environment", params, ctx)["plan"]
    ops.apply("environment", plan, ctx)
    script = Path(params["destination"])/"recreate.sh"
    assert "apptainer exec --cleanenv" in script.read_text()
    assert subprocess.run(["sh", "-n", str(script)], capture_output=True).returncode == 0
    for line in script.read_text().splitlines():
        tokens = shlex.split(line)
        if "-c" in tokens:
            compile(tokens[tokens.index("-c")+1], "recipe-check", "exec")


def test_sif_change_after_review_cancels_recreation_bundle(tmp_path, ctx):
    params, doc = environment(tmp_path)
    image = tmp_path/"image.sif"
    image.write_bytes(b"version one")
    doc["container"] = {"kind": "sif", "path": image.name, "sha256": sha(image)}
    dump(Path(params["manifest"]), doc)
    plan = ops.run("environment", params, ctx)["plan"]
    image.write_bytes(b"version two")
    with pytest.raises(ValueError, match="changed"):
        ops.apply("environment", plan, ctx)


def test_runtime_probe_reports_compute_host_separately_from_local_environment(tmp_path, ctx):
    ctx, backend = scheduler_context(ctx)
    backend.jobs["10"].update(JobState="RUNNING")
    params, _ = environment(tmp_path)
    params["probe_job_id"] = "10"
    evidence = {"job_id": "10", "node": "n1", "python": "3.10.1", "platform": "Linux", "machine": "aarch64",
                "libc": ["glibc", "2.31"], "packages": {"some-package": None}, "warnings": []}
    backend.probe = "0: RAVEN_RUNTIME " + json.dumps(evidence)
    result = ops.run("environment", params, ctx)
    assert result["data"]["compute_hosts"][0]["python"] == "3.10.1"
    assert result["data"]["actual"]["python"] == platform.python_version()
    assert result["data"]["compute_hosts"][0]["differences"]


def test_generated_recipe_refuses_tampered_lock_without_creating_venv(tmp_path, ctx):
    params, _ = environment(tmp_path)
    ops.apply("environment", ops.run("environment", params, ctx)["plan"], ctx)
    bundle = Path(params["destination"])
    (bundle/"requirements.lock").write_text("malicious changed contents\n")
    target = tmp_path/"venv"
    process = subprocess.run(["sh", str(bundle/"recreate.sh"), str(target)], capture_output=True, text=True)
    assert process.returncode != 0
    assert "Lockfile digest mismatch" in process.stderr
    assert not target.exists()


def test_dependency_change_during_output_copy_cannot_publish_success(tmp_path, ctx, monkeypatch):
    params, _ = reuse(tmp_path)
    plan = ops.run("reuse", params, ctx)["plan"]
    original = sf.publish
    def publish(*args, **kwargs):
        final = kwargs["final_check"]
        def changed():
            (tmp_path/"input.txt").write_text("new input while copying")
            final()
        kwargs["final_check"] = changed
        return original(*args, **kwargs)
    monkeypatch.setattr(sf, "publish", publish)
    with pytest.raises(ValueError, match="changed"):
        ops.apply("reuse", plan, ctx)
    assert not Path(params["destination"]).exists()


def test_reuse_requested_input_identity_cannot_be_omitted_or_substituted(tmp_path, ctx):
    params, doc = reuse(tmp_path)
    wanted = dict(doc["identity"], inputs_sha256={"input.txt": "a"*64})
    dump(Path(params["identity"]), wanted)
    with pytest.raises(ValueError, match="identity"):
        ops.run("reuse", params, ctx)
    # Changing both declarations does not make the actual input bytes match.
    doc["identity"] = wanted
    dump(Path(params["manifest"]), doc)
    with pytest.raises(ValueError, match="scientific input"):
        ops.run("reuse", params, ctx)


def test_duplicate_scheduler_identity_cannot_shadow_a_different_record(ctx):
    ctx, backend = scheduler_context(ctx)
    backend.run = lambda *_: ("JobId=10 UserId=alice(1000) JobState=RUNNING JobId=10 UserId=bob(1001)", 0)
    with pytest.raises(ValueError, match="ambiguous"):
        ops._details(ctx, "10")


@pytest.mark.parametrize("logical,extra", [("10_2", {"ArrayJobId": "10", "ArrayTaskId": "2"}),
                                           ("10+1", {"HetJobId": "10", "HetJobOffset": "1"})])
def test_scheduler_raw_identity_preserves_exact_array_and_heterogeneous_selection(ctx, logical, extra):
    ctx, backend = scheduler_context(ctx)
    backend.jobs[logical] = dict(backend.jobs["10"], JobId="12", **extra)
    assert ops._details(ctx, logical)["JobId"] == "12"
