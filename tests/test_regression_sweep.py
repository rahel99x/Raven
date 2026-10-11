"""Actual Git snapshots and measured bounded regression trials, no checkout mutation."""
from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import threading
from types import SimpleNamespace

import pytest

from tower import diagnostic_trials as driver
from tower import ops_investigation as ops
from tower.operations import Context


@pytest.fixture(autouse=True)
def synchronous_driver_validation(monkeypatch):
    monkeypatch.setattr(ops, "_launch_local", lambda recipe, root, ctx: driver.run(recipe, root, cancel=ctx.cancel))


def git(repo, *argv):
    result = subprocess.run(["git", "-c", "user.name=Tower test", "-c", "user.email=tower@example.invalid", "-C", str(repo), *argv], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def sweep(tmp_path):
    repo = tmp_path/"project"
    repo.mkdir()
    git(repo, "init", "-q")
    revisions = []
    for name, delay in (("baseline", .002), ("regressed", .08), ("recovered", .002)):
        (repo/"measure.py").write_text(f"import json,sys,time\nfrom pathlib import Path\nstart=time.perf_counter()\ntime.sleep({delay})\nseconds=time.perf_counter()-start\nPath(sys.argv[1]).write_text(json.dumps({{'seconds':seconds,'scientific_valid':True}}))\nprint('SCIENTIFIC_CHECK_OK')\n# {name}\n")
        git(repo, "add", "measure.py")
        git(repo, "commit", "-qm", name)
        revisions.append({"id": name, "commit": git(repo, "rev-parse", "HEAD")})
    recipe = {"schema": "tower.diagnostic-campaign/v1", "id": "git-sweep", "mode": "local-diagnostic", "kind": "regression",
              "command": [str(Path(sys.executable).resolve()), "{repo:measure.py}", "{result}"], "files": [],
              "limits": {"max_trials": 40, "trial_seconds": 3, "total_seconds": 45, "cpus": 1, "memory_mb": 256, "output_bytes": 16384, "repeats": 4},
              "oracle": {"exit_code": 0, "contains": "SCIENTIFIC_CHECK_OK"},
              "repository": {"path": str(repo), "commits": revisions, "baseline": "baseline", "minimum_change": .03, "metric": "result_seconds", "confidence": .95}}
    path = tmp_path/"sweep.json"
    path.write_text(json.dumps(recipe))
    params = {"manifest": str(path), "destination": str(tmp_path/"sweep-work"), "action": "launch"}
    ctx = Context(scope={"cluster": "test"}, cancel=threading.Event(), state_dir=str(tmp_path/"state"))
    return repo, recipe, params, ctx


def test_real_git_regression_sweep_preserves_dirty_checkout_and_handles_recovery(sweep):
    repo, recipe, params, ctx = sweep
    head = git(repo, "rev-parse", "HEAD")
    (repo/"measure.py").write_text("uncommitted user edits\n")
    (repo/"untracked.txt").write_text("preserve this\n")
    index_before = (repo/".git"/"index").read_bytes()
    reviewed = ops.run("regression-sweep", params, ctx)
    assert not Path(params["destination"]).exists()
    result = ops.apply("regression-sweep", reviewed["plan"], ctx)["data"]["result"]
    assert result["state"] == "completed"
    assert result["first_observed_regression"] == "regressed"
    assert result["nonmonotonic"]
    assert len(result["trials"]) == 12
    assert all(row["measurement"]["scientific_valid"] for row in result["trials"])
    assert git(repo, "rev-parse", "HEAD") == head
    assert (repo/"measure.py").read_text() == "uncommitted user edits\n"
    assert (repo/"untracked.txt").read_text() == "preserve this\n"
    assert (repo/".git"/"index").read_bytes() == index_before
    assert [row["revision"] for row in result["trials"][:6]] == ["baseline", "regressed", "recovered", "recovered", "regressed", "baseline"]


def test_mutable_refs_tree_objects_and_symlink_archives_refused(sweep):
    repo, recipe, params, ctx = sweep
    with pytest.raises(ValueError, match="commit hash"):
        driver.archive_bytes(repo, "HEAD")
    with pytest.raises(ValueError, match="commit"):
        driver.archive_bytes(repo, git(repo, "rev-parse", "HEAD^{tree}"))
    (repo/"unsafe").symlink_to("/etc/passwd")
    git(repo, "add", "unsafe")
    git(repo, "commit", "-qm", "symlink")
    with pytest.raises(ValueError, match="links"):
        driver.archive_bytes(repo, git(repo, "rev-parse", "HEAD"))


def test_archive_limit_and_cancel_are_enforced_before_execution(sweep):
    repo, recipe, params, ctx = sweep
    commit = recipe["repository"]["commits"][0]["commit"]
    with pytest.raises(ValueError, match="budget"):
        driver.archive_bytes(repo, commit, limit=10)
    ctx.cancel.set()
    with pytest.raises(ValueError, match="cancelled"):
        driver.archive_bytes(repo, commit, cancel=ctx.cancel)


def test_budget_interruption_leaves_unmeasured_versions_inconclusive(sweep):
    repo, recipe, params, ctx = sweep
    recipe["limits"]["max_trials"] = 4
    Path(params["manifest"]).write_text(json.dumps(recipe))
    reviewed = ops.run("regression-sweep", params, ctx)
    result = ops.apply("regression-sweep", reviewed["plan"], ctx)["data"]["result"]
    assert result["state"] == "partial" and len(result["trials"]) == 4
    assert all(row["state"] == "inconclusive" for row in result["comparisons"])


def test_scientific_failure_is_not_endorsed_as_speedup(sweep):
    repo, recipe, params, ctx = sweep
    program = (repo/"measure.py").read_text().replace("'scientific_valid':True", "'scientific_valid':False")
    (repo/"measure.py").write_text(program)
    git(repo, "add", "measure.py")
    git(repo, "commit", "-qm", "science invalid")
    recipe["repository"]["commits"] = [recipe["repository"]["commits"][0], {"id": "bad", "commit": git(repo, "rev-parse", "HEAD")}]
    Path(params["manifest"]).write_text(json.dumps(recipe))
    reviewed = ops.run("regression-sweep", params, ctx)
    result = ops.apply("regression-sweep", reviewed["plan"], ctx)["data"]["result"]
    assert result["comparisons"][0]["state"] == "inconclusive"
    assert all(not row["scientific_valid"] for row in result["measurements"]["bad"])


def test_slurm_snapshot_staging_is_complete_without_running_work(sweep):
    repo, recipe, params, ctx = sweep
    recipe["mode"] = "slurm"
    Path(params["manifest"]).write_text(json.dumps(recipe))
    calls = []
    ctx = replace(ctx, slurm=SimpleNamespace(b=SimpleNamespace(), submit=lambda argv, cwd: (calls.append(argv) or (True, "234", "234"))))
    reviewed = ops.run("regression-sweep", params, ctx)
    result = ops.apply("regression-sweep", reviewed["plan"], ctx)
    assert len(calls) == 1 and result["data"]["result"]["state"] == "accepted"
    root = Path(params["destination"])
    assert len(list((root/"revisions").glob("*.tar"))) == 3
    assert not (root/"run.claim.json").exists()


@pytest.mark.parametrize("path", ["../escape", "/absolute"])
def test_archive_extraction_cannot_escape_private_workdir(tmp_path, path):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        member = tarfile.TarInfo(path)
        member.size = 1
        archive.addfile(member, io.BytesIO(b"x"))
    with pytest.raises(ValueError, match="outside"):
        driver.extract_snapshot(stream.getvalue(), tmp_path/"checkout")


def test_bootstrap_abstains_zero_variance_and_is_deterministic():
    repository = {"baseline": "a", "commits": [{"id": "a", "commit": "a"*40}, {"id": "b", "commit": "b"*40}], "minimum_change": .1, "confidence": .95}
    measured = {"a": [{"value": 1, "scientific_valid": True}]*3, "b": [{"value": 3, "scientific_valid": True}]*3}
    assert driver.regression_summary(repository, measured)["comparisons"][0]["state"] == "inconclusive"
    measured["a"] = [{"value": x, "scientific_valid": True} for x in [1, 1.1, .9, 1.05]]
    measured["b"] = [{"value": x, "scientific_valid": True} for x in [3, 3.1, 2.9, 3.05]]
    assert driver.regression_summary(repository, measured) == driver.regression_summary(repository, measured)
    assert driver.regression_summary(repository, measured)["comparisons"][0]["state"] == "regression"


def test_cancelled_sweep_keeps_receipt_and_unknown_versions(sweep, monkeypatch):
    repo, recipe, params, ctx = sweep
    reviewed = ops.run("regression-sweep", params, ctx)
    original = driver._execute
    def execute(*args, **kwargs):
        ctx.cancel.set()
        return original(*args, **kwargs)
    monkeypatch.setattr(driver, "_execute", execute)
    result = ops.apply("regression-sweep", reviewed["plan"], ctx)["data"]["result"]
    assert result["state"] == "partial"
    assert result["trials"][0]["state"] == "cancelled"
    assert all(row["state"] == "inconclusive" for row in result["comparisons"])
    assert (Path(params["destination"])/"run.claim.json").exists()


@pytest.mark.parametrize("data", [b'{"scientific_valid":false,"scientific_valid":true}', b'{"seconds":NaN}', b'{"seconds":Infinity}'])
def test_untrusted_measurement_json_cannot_hide_invalid_evidence(data):
    with pytest.raises(ValueError):
        driver._json(data)
