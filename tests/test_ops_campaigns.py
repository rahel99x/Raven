"""Campaign contracts, stale review rejection, and durable launch boundaries."""
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from raven import ops_campaigns as ops
from raven import operations
from raven import campaign_common as common


@pytest.fixture
def context(tmp_path):
    calls = []
    def submit(argv, workdir):
        calls.append((argv, workdir))
        return True, str(1000 + len(calls)), str(1000 + len(calls))
    def query(argv, timeout):
        if argv[0] == "squeue":
            return "", 0
        return "77|77|test|2026-01-01T10:00:00|FAILED\n", 0
    slurm = SimpleNamespace(b=SimpleNamespace(run=query), submit=submit)
    ctx = operations.Context(slurm=slurm, state_dir=str(tmp_path / "state"),
                             scope={"cluster": "test", "user": "researcher"},
                             finished=({"id": "77", "start": "2026-01-01T10:00:00", "cluster": "test", "state": "FAILED"},))
    return ctx, calls


def write(path, value):
    path.write_text(json.dumps(value))
    return str(path)


def script(tmp_path):
    path = tmp_path / "job.sh"
    path.write_text("#!/bin/bash\n#SBATCH --cpus-per-task=1\nprintf '%s' \"${RAVEN_TRIAL_PARAMETERS:-$RAVEN_CHECKPOINT}\"\n")
    return path


def recipes(tmp_path):
    batch = script(tmp_path)
    checkpoint = tmp_path / "snapshot.bin"
    checkpoint.write_bytes(b"application-state")
    base = {"script": str(batch), "workdir": str(tmp_path)}
    return {
        "checkpoint": {"schema": "raven.checkpoint-restart/v1", **base,
                       "script_sha256": hashlib.sha256(batch.read_bytes()).hexdigest(),
                       "checkpoint": {"path": str(checkpoint), "complete": True, "sha256": hashlib.sha256(checkpoint.read_bytes()).hexdigest()},
                       "compatibility": {"expected": {"application": "solver-1", "format": "v3", "runtime": "gcc-14"},
                                         "actual": {"application": "solver-1", "format": "v3", "runtime": "gcc-14"}},
                       "source_job": {"id": "77", "start": "2026-01-01T10:00:00", "cluster": "test"}},
        "search": {"schema": "raven.parameter-search/v1", **base, "id": "campaign-a", "budget": 6, "max_parallel": 3,
                   "direction": "minimize", "parameters": {"x": [0, 1, 2], "mode": ["fast", "accurate"]}},
        "packing": {"schema": "raven.packed-tasks/v1", "cpus": 4, "memory_mb": 128, "parallel": 2,
                    "workdir": str(tmp_path), "tasks": [{"id": "manifest", "argv": ["/bin/echo", "value"], "cpus": 1, "memory_mb": 32}]},
        "dask": {"schema": "raven.dask-pool/v1", "id": "pool-a", "cores": 4, "memory_mb": 2048, "max_jobs": 5},
        "heterogeneous": {"schema": "raven.heterogeneous-allocation/v1", "workdir": str(tmp_path),
                          "components": [{"resources": {"nodes": 1, "ntasks": 2, "mem": "2G"}, "argv": ["solver", "--cpu"]},
                                         {"resources": {"nodes": 1, "ntasks": 1, "gpus": 1}, "argv": ["solver", "--gpu"]}]}}


def prepare(feature, recipe, tmp_path, ctx, action=None, **kwargs):
    params = {"manifest": write(tmp_path / (feature + ".json"), recipe),
              "action": action or {"checkpoint": "restart", "search": "launch", "packing": "prepare", "dask": "inspect", "heterogeneous": "prepare"}[feature], **kwargs}
    return ops.run(feature, params, ctx), params


@pytest.mark.parametrize("feature", ops._SCHEMAS)
def test_inspect_is_read_only(feature, context, tmp_path):
    ctx, calls = context
    result, _ = prepare(feature, recipes(tmp_path)[feature], tmp_path, ctx, action="inspect")
    assert result["rows"]
    assert "plan" not in result
    assert calls == []
    assert not Path(ctx.state_dir).exists()


@pytest.mark.parametrize("feature", ["checkpoint", "search", "packing", "heterogeneous"])
def test_review_apply_records_exact_receipt_once(feature, context, tmp_path):
    ctx, calls = context
    result, _ = prepare(feature, recipes(tmp_path)[feature], tmp_path, ctx)
    applied = ops.apply(feature, result["plan"], ctx)
    assert len(calls) == 1
    assert applied["status"] == "ok"
    with pytest.raises(ValueError, match="receipt|evidence|journal"):
        ops.apply(feature, result["plan"], ctx)
    assert len(calls) == 1
    assert Path(calls[0][0][-1]).is_file()


@pytest.mark.parametrize("feature", ["checkpoint", "search", "packing", "heterogeneous"])
def test_changed_manifest_blocks_apply(feature, context, tmp_path):
    ctx, calls = context
    recipe = recipes(tmp_path)[feature]
    result, params = prepare(feature, recipe, tmp_path, ctx)
    recipe["new_revision"] = 2
    write(Path(params["manifest"]), recipe)
    with pytest.raises(ValueError, match="manifest changed"):
        ops.apply(feature, result["plan"], ctx)
    assert calls == []


@pytest.mark.parametrize("feature", ["checkpoint", "search", "packing", "heterogeneous"])
def test_scope_change_blocks_apply(feature, context, tmp_path):
    ctx, calls = context
    result, _ = prepare(feature, recipes(tmp_path)[feature], tmp_path, ctx)
    altered = operations.Context(**{**ctx.__dict__, "scope": {"cluster": "elsewhere"}})
    with pytest.raises(ValueError, match="connection changed"):
        ops.apply(feature, result["plan"], altered)
    assert not calls


@pytest.mark.parametrize("feature", ops._SCHEMAS)
def test_remote_and_replay_refuse_local_substitutes(feature, context, tmp_path):
    ctx, _ = context
    for changes in ({"replay": True}, {"files": SimpleNamespace(remote=True)}):
        altered = operations.Context(**{**ctx.__dict__, **changes})
        with pytest.raises(ValueError, match="recorded|target host"):
            prepare(feature, recipes(tmp_path)[feature], tmp_path, altered)


def test_checkpoint_rejects_modified_data(context, tmp_path):
    ctx, calls = context
    recipe = recipes(tmp_path)["checkpoint"]
    result, _ = prepare("checkpoint", recipe, tmp_path, ctx)
    Path(recipe["checkpoint"]["path"]).write_bytes(b"different snapshot")
    with pytest.raises(ValueError, match="SHA-256"):
        ops.apply("checkpoint", result["plan"], ctx)
    assert not calls


@pytest.mark.parametrize("damage", ["incomplete", "digest", "compatibility", "source", "cluster", "script"])
def test_checkpoint_contract_rejects_unsafe_restart(damage, context, tmp_path):
    ctx, calls = context
    recipe = recipes(tmp_path)["checkpoint"]
    if damage == "incomplete":
        recipe["checkpoint"]["complete"] = False
    elif damage == "digest":
        recipe["checkpoint"]["sha256"] = "0" * 64
    elif damage == "compatibility":
        recipe["compatibility"]["actual"]["runtime"] = "incompatible"
    elif damage == "source":
        recipe["source_job"]["start"] = "reused attempt"
    elif damage == "cluster":
        recipe["source_job"]["cluster"] = "other"
    else:
        recipe["script_sha256"] = "0" * 64
    with pytest.raises(ValueError):
        prepare("checkpoint", recipe, tmp_path, ctx)
    assert not calls


def test_running_checkpoint_source_blocks_second_copy(context, tmp_path):
    ctx, _ = context
    row = {**ctx.finished[0], "state": "RUNNING"}
    ctx = operations.Context(**{**ctx.__dict__, "jobs": (row,), "finished": ()})
    with pytest.raises(ValueError, match="still active"):
        prepare("checkpoint", recipes(tmp_path)["checkpoint"], tmp_path, ctx)


@pytest.mark.parametrize("queue,accounting", [("77|RUNNING", ""), ("", "77|77|test|later attempt|FAILED"),
                                               ("", ""), ("", "77|77|other|2026-01-01T10:00:00|FAILED"),
                                               ("", "77|77|test|2026-01-01T10:00:00|RUNNING")])
def test_checkpoint_rechecks_live_source_before_restart(queue, accounting, context, tmp_path):
    ctx, calls = context
    review, _ = prepare("checkpoint", recipes(tmp_path)["checkpoint"], tmp_path, ctx)
    ctx.slurm.b.run = lambda argv, timeout: (queue if argv[0] == "squeue" else accounting, 0)
    with pytest.raises(ValueError, match="source"):
        ops.apply("checkpoint", review["plan"], ctx)
    assert not calls


def test_checkpoint_array_logical_id_matches_distinct_raw_allocation(context, tmp_path):
    ctx, calls = context
    recipe = recipes(tmp_path)["checkpoint"]
    recipe["source_job"]["id"] = "100_2"
    ctx.slurm.b.run = lambda argv, timeout: ("" if argv[0] == "squeue" else "100_2|102|test|2026-01-01T10:00:00|FAILED\n", 0)
    result, _ = prepare("checkpoint", recipe, tmp_path, ctx)
    applied = ops.apply("checkpoint", result["plan"], ctx)
    assert len(calls) == 1
    assert applied["data"]["source_attempt"]["raw_id"] == "102"


def test_script_change_between_review_and_apply(context, tmp_path):
    ctx, calls = context
    recipe = recipes(tmp_path)["search"]
    result, _ = prepare("search", recipe, tmp_path, ctx)
    Path(recipe["script"]).write_text("#!/bin/sh\nexit 3\n")
    with pytest.raises(ValueError, match="settings changed|evidence changed"):
        ops.apply("search", result["plan"], ctx)
    assert not calls


def test_search_persistence_budgets_adaptation_and_separate_failures(context, tmp_path):
    ctx, calls = context
    recipe = recipes(tmp_path)["search"]
    first, _ = prepare("search", recipe, tmp_path, ctx, count="3")
    trials = first["data"]["proposals"]
    assert all(p["strategy"] == "explore" for p in trials)
    applied = ops.apply("search", first["plan"], ctx)
    assert len(calls) == 3
    blocked, _ = prepare("search", recipe, tmp_path, ctx, count="3")
    assert "plan" not in blocked and not blocked["data"]["proposals"]
    recipe["observations"] = [{"trial": trials[0]["id"], "status": "success", "metric": 5},
                              {"trial": trials[1]["id"], "status": "infrastructure_failed"},
                              {"trial": trials[2]["id"], "status": "scientific_failed"}]
    next_review, _ = prepare("search", recipe, tmp_path, ctx, count="3")
    assert [p["strategy"] for p in next_review["data"]["proposals"]] == ["exploit", "explore", "exploit"]
    assert {p["id"] for p in trials}.isdisjoint({p["id"] for p in next_review["data"]["proposals"]})
    ops.apply("search", next_review["plan"], ctx)
    exhausted, _ = prepare("search", recipe, tmp_path, ctx, count="3")
    assert not exhausted["data"]["proposals"]
    assert len(calls) == 6
    assert Path(applied["data"]["journal"]).is_file()


def test_search_active_scheduler_jobs_still_count_after_scientific_outcome(context, tmp_path):
    ctx, calls = context
    recipe = recipes(tmp_path)["search"]
    recipe["max_parallel"] = 1
    review, _ = prepare("search", recipe, tmp_path, ctx)
    ops.apply("search", review["plan"], ctx)
    recipe["observations"] = [{"trial": review["data"]["proposals"][0]["id"], "status": "success", "metric": 1}]
    active_ctx = operations.Context(**{**ctx.__dict__, "jobs": ({"id": "1001", "state": "RUNNING"},)})
    blocked, _ = prepare("search", recipe, tmp_path, active_ctx)
    assert not blocked["data"]["proposals"]


def test_search_extreme_float_ranges_have_deterministic_finite_distance(context, tmp_path):
    ctx, _ = context
    recipe = recipes(tmp_path)["search"]
    recipe.update(parameters={"x": [-1e308, 0.0, 1e308], "flag": [True, False]}, budget=6)
    first, _ = prepare("search", recipe, tmp_path, ctx)
    trial = first["data"]["proposals"][0]
    ops.apply("search", first["plan"], ctx)
    recipe["observations"] = [{"trial": trial["id"], "status": "success", "metric": 1.0}]
    second, _ = prepare("search", recipe, tmp_path, ctx)
    repeated, _ = prepare("search", recipe, tmp_path, ctx)
    assert second["data"]["proposals"] == repeated["data"]["proposals"]
    assert second["data"]["proposals"][0]["strategy"] == "exploit"


def test_unknown_search_launch_is_never_repeated(context, tmp_path):
    ctx, calls = context
    def uncertain(argv, cwd):
        calls.append((argv, cwd))
        raise TimeoutError("connection timed out after acceptance")
    ctx.slurm.submit = uncertain
    recipe = recipes(tmp_path)["search"]
    review, _ = prepare("search", recipe, tmp_path, ctx, count="3")
    result = ops.apply("search", review["plan"], ctx)
    assert len(calls) == 1
    assert result["status"] == "warning"
    assert result["data"]["trials"][0]["state"] == "unknown"
    next_review, _ = prepare("search", recipe, tmp_path, ctx, count="3")
    unknown = result["data"]["trials"][0]["trial"]
    assert unknown not in {row["id"] for row in next_review["data"]["proposals"]}
    assert len(next_review["data"]["proposals"]) == 2


def test_checkpoint_script_rechecks_snapshot_when_queued_job_starts(context, tmp_path):
    import subprocess
    ctx, calls = context
    recipe = recipes(tmp_path)["checkpoint"]
    review, _ = prepare("checkpoint", recipe, tmp_path, ctx)
    ops.apply("checkpoint", review["plan"], ctx)
    generated = calls[0][0][-1]
    accepted = subprocess.run(["bash", generated], capture_output=True, timeout=5)
    assert accepted.returncode == 0
    Path(recipe["checkpoint"]["path"]).write_bytes(b"mutated while pending")
    refused = subprocess.run(["bash", generated], capture_output=True, timeout=5)
    assert refused.returncode == 65
    assert b"changed since review" in refused.stderr


@pytest.mark.parametrize("flags", [["--nodes=2"], ["--mem=100G"], ["--array=0-4"], ["--wrap=echo hi"]])
def test_packing_site_overrides_cannot_break_resource_accounting(flags, context, tmp_path):
    ctx, _ = context
    recipe = recipes(tmp_path)["packing"]
    recipe["sbatch"] = flags
    with pytest.raises(ValueError, match="permits only"):
        prepare("packing", recipe, tmp_path, ctx)


def test_packing_site_options_and_intentional_new_run(context, tmp_path):
    ctx, calls = context
    recipe = recipes(tmp_path)["packing"]
    recipe.update(id="run-a", sbatch=["--account=research", "--partition=compute"])
    first, _ = prepare("packing", recipe, tmp_path, ctx)
    ops.apply("packing", first["plan"], ctx)
    assert "#SBATCH --account research" in Path(calls[0][0][-1]).read_text()
    recipe["id"] = "run-b"
    second, _ = prepare("packing", recipe, tmp_path, ctx)
    ops.apply("packing", second["plan"], ctx)
    assert len(calls) == 2 and calls[0][0][-1] != calls[1][0][-1]


def test_heterogeneous_hidden_sbatch_environment_is_rejected(context, tmp_path, monkeypatch):
    ctx, calls = context
    monkeypatch.setenv("SBATCH_NTASKS", "1000")
    with pytest.raises(ValueError, match="inherited sbatch"):
        prepare("heterogeneous", recipes(tmp_path)["heterogeneous"], tmp_path, ctx)
    assert not calls


@pytest.mark.parametrize("bad", [{"x": [1, 1]}, {"x": [None]}, {"x": list(range(100)), "y": list(range(100))}, {"bad name": [1]}, {}])
def test_search_grid_limits(bad):
    with pytest.raises(ValueError):
        ops._search_candidates({"parameters": bad})


def test_heterogeneous_colon_submission_and_group_launch(context, tmp_path):
    ctx, calls = context
    result, _ = prepare("heterogeneous", recipes(tmp_path)["heterogeneous"], tmp_path, ctx)
    ops.apply("heterogeneous", result["plan"], ctx)
    argv = calls[0][0]
    assert argv.count(":") == 1
    text = Path(argv[-1]).read_text()
    assert "--het-group=0 --mem=2G --nodes=1 --ntasks=2 solver --cpu : --het-group=1 --gpus=1 --nodes=1 --ntasks=1 solver --gpu" in text
    assert "--gpus=1" in argv and "--ntasks=2" in argv


@pytest.mark.parametrize("bad", ["array", "mem_conflict", "missing_tasks", "zero_nodes", "colon", "switch", "none"])
def test_heterogeneous_rejects_invalid_components(bad, context, tmp_path):
    ctx, calls = context
    recipe = recipes(tmp_path)["heterogeneous"]
    component = recipe["components"][0]
    if bad == "array":
        component["resources"]["array"] = "0-4"
    elif bad == "mem_conflict":
        component["resources"]["mem-per-cpu"] = "2G"
    elif bad == "missing_tasks":
        del component["resources"]["ntasks"]
    elif bad == "zero_nodes":
        component["resources"]["nodes"] = 0
    elif bad == "colon":
        component["argv"] += [":"]
    elif bad == "switch":
        component["argv"][0] = "--pty"
    else:
        recipe["components"] = None
    with pytest.raises(ValueError):
        prepare("heterogeneous", recipe, tmp_path, ctx)
    assert not calls


def test_dask_missing_dependency_is_explicit(context, tmp_path, monkeypatch):
    ctx, calls = context
    monkeypatch.setattr(ops.importlib.util, "find_spec", lambda _: None)
    with pytest.raises(ValueError, match="optional dask-jobqueue"):
        prepare("dask", recipes(tmp_path)["dask"], tmp_path, ctx, action="start")
    assert not calls


def test_dask_unresolved_controller_blocks_duplicate_pool(context, tmp_path, monkeypatch):
    ctx, _ = context
    recipe = recipes(tmp_path)["dask"]
    monkeypatch.setattr(ops.importlib.util, "find_spec", lambda _: object())
    root = common.directory(ctx, "dask") / recipe["id"]
    common.atomic(root / "config.json", {"old": True})
    common.atomic(root / "status.json", {"state": "running", "pid": 999999999, "process_start": "bad"})
    with pytest.raises(ValueError, match="unresolved"):
        prepare("dask", recipe, tmp_path, ctx, action="start")


def test_strict_json_and_artifact_identity(tmp_path):
    path = tmp_path / "input.json"
    for raw in ('{"x":1,"x":2}', '{"x":NaN}', '{"x":1e999}'):
        path.write_text(raw)
        with pytest.raises(ValueError):
            common.read(path)
    artifact = tmp_path / "fixed.sh"
    common.artifact(artifact, b"content")
    common.artifact(artifact, b"content")
    with pytest.raises(ValueError, match="changed"):
        common.artifact(artifact, b"different")


def test_file_hash_detects_atomic_replacement(tmp_path, monkeypatch):
    original = tmp_path / "checkpoint"
    replacement = tmp_path / "new"
    original.write_bytes(b"old")
    replacement.write_bytes(b"new")
    actual = common.os.fstat
    calls = 0
    def replace_during_hash(fd):
        nonlocal calls
        calls += 1
        stat = actual(fd)
        if calls == 2:
            replacement.replace(original)
        return stat
    monkeypatch.setattr(common.os, "fstat", replace_during_hash)
    with pytest.raises(ValueError, match="changed"):
        common.file_hash(original)
