"""Real isolated processes, delta-debug oracles and reviewed Slurm receipts."""
import copy
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from tower import diagnostic_trials as driver
from tower import ops_investigation as ops
from tower.operations import Context


def write(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    return str(path)


def recipe(tmp_path, program=None, kind="minimize"):
    code = tmp_path/"oracle.py"
    code.write_text(program or "from pathlib import Path\nimport sys\ns=Path(sys.argv[1]).read_text()\nprint('TARGET' if 'needle' in s else 'CLEAR')\nsys.exit(7 if 'needle' in s else 0)\n")
    input_file = tmp_path/"input.txt"
    input_file.write_text("first\nsecond\nneedle\nthird\nfourth\n")
    files = [{"name": p.name, "path": str(p), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in (code, input_file)]
    result = {"schema": "tower.diagnostic-campaign/v1", "id": "diagnosis", "kind": kind, "mode": "local-diagnostic",
              "command": [str(Path(sys.executable).resolve()), "{file:oracle.py}", "{input}"], "files": files,
              "limits": {"max_trials": 40, "trial_seconds": 2, "total_seconds": 20, "cpus": 1, "memory_mb": 256, "output_bytes": 8192, "repeats": 2},
              "oracle": {"exit_code": 7, "contains": "TARGET"}}
    if kind == "minimize":
        result["input"] = "input.txt"
    else:
        result["command"] = result["command"][:-1]
        result["cases"] = [{"id": "case1", "arguments": []}]
    return result


def prepare(tmp_path, document, ctx, feature=None, destination="work"):
    feature = feature or ("minimize-reproducer" if document["kind"] == "minimize" else "diagnostic-campaign")
    params = {"manifest": write(tmp_path/"recipe.json", document), "destination": str(tmp_path/destination), "action": "launch"}
    return ops.run(feature, params, ctx), params


@pytest.fixture
def ctx(tmp_path):
    return Context(scope={"cluster": "test", "user": "researcher"}, state_dir=str(tmp_path/"state"), cancel=threading.Event())


@pytest.fixture(autouse=True)
def synchronous_driver_validation(monkeypatch):
    # Algorithm tests run the identical driver synchronously to inspect each
    # outcome. Separate async tests exercise real detached launch/status/cancel.
    monkeypatch.setattr(ops, "_launch_local", lambda recipe, root, ctx: driver.run(recipe, root, cancel=ctx.cancel))


def test_real_reproducer_minimizes_exact_failure_with_durable_receipts(tmp_path, ctx):
    doc = recipe(tmp_path)
    reviewed, params = prepare(tmp_path, doc, ctx)
    root = Path(params["destination"])
    assert not root.exists()
    result = ops.apply("minimize-reproducer", reviewed["plan"], ctx)
    assert result["data"]["result"]["state"] == "reduced"
    assert result["data"]["result"]["one_minimal"] is True
    assert (root/"minimal-input.txt").read_bytes() == b"needle\n"
    assert (tmp_path/"input.txt").read_text().startswith("first\n")
    assert (root/"run.claim.json").exists()
    assert len(list((root/"trials").glob("*/intent.json"))) == len(result["data"]["result"]["trials"])
    with pytest.raises((ValueError, FileExistsError)):
        driver.run(doc, root)
    with pytest.raises(ValueError):
        ops.apply("minimize-reproducer", reviewed["plan"], ctx)


@pytest.mark.parametrize("source", ["recipe.json", "oracle.py", "input.txt"])
def test_changed_review_inputs_never_execute(tmp_path, ctx, source):
    doc = recipe(tmp_path)
    reviewed, params = prepare(tmp_path, doc, ctx)
    with (tmp_path/source).open("a") as stream:
        stream.write("\n")
    with pytest.raises(ValueError, match="changed"):
        ops.apply("minimize-reproducer", reviewed["plan"], ctx)
    assert not Path(params["destination"]).exists()


def test_wrong_crash_signature_does_not_reduce_original(tmp_path, ctx):
    doc = recipe(tmp_path, "import sys\nprint('OTHER FAILURE')\nsys.exit(7)\n")
    reviewed, _ = prepare(tmp_path, doc, ctx)
    result = ops.apply("minimize-reproducer", reviewed["plan"], ctx)["data"]["result"]
    assert result["state"] == "original-not-confirmed"
    assert result["reduced_bytes"] == result["original_bytes"]
    assert result["one_minimal"] is False


@pytest.mark.parametrize("program,expected", [("import time\ntime.sleep(10)\n", "timeout"), ("print('x'*100000)\n", "output-limit"), ("import os,signal\nos.kill(os.getpid(),signal.SIGTERM)\n", "finished")])
def test_timeout_output_and_signal_are_not_failure_evidence(tmp_path, ctx, program, expected):
    doc = recipe(tmp_path, program)
    doc["limits"].update(trial_seconds=.15 if expected == "timeout" else 2, total_seconds=3, repeats=1)
    reviewed, _ = prepare(tmp_path, doc, ctx)
    result = ops.apply("minimize-reproducer", reviewed["plan"], ctx)["data"]["result"]
    assert result["state"] == "original-not-confirmed"
    assert result["trials"][0]["state"] == expected


def test_nondeterministic_repeated_oracle_stops_without_false_minimality(tmp_path, ctx):
    program = "from pathlib import Path\nimport sys\nx=int(Path.cwd().name)%2\nprint('TARGET' if x else 'CLEAR')\nsys.exit(7 if x else 0)\n"
    doc = recipe(tmp_path, program)
    reviewed, _ = prepare(tmp_path, doc, ctx)
    result = ops.apply("minimize-reproducer", reviewed["plan"], ctx)["data"]["result"]
    assert result["state"] == "original-not-confirmed" and not result["one_minimal"]
    assert len(result["trials"]) == 2


def test_budget_exhaustion_preserves_confirmed_candidate_and_no_more_trials(tmp_path, ctx):
    doc = recipe(tmp_path)
    doc["limits"].update(max_trials=2, repeats=2)
    reviewed, _ = prepare(tmp_path, doc, ctx)
    result = ops.apply("minimize-reproducer", reviewed["plan"], ctx)["data"]["result"]
    assert result["state"] == "inconclusive" and not result["one_minimal"]
    assert len(result["trials"]) == 2
    assert result["reduced_bytes"] == result["original_bytes"]


def test_campaign_executes_each_case_and_persists_measured_result(tmp_path, ctx):
    doc = recipe(tmp_path, "import sys,json\nfrom pathlib import Path\nPath(sys.argv[1]).write_text(json.dumps({'metric':float(sys.argv[2])**2}))\nprint('TARGET')\nsys.exit(7)\n", kind="campaign")
    doc["cases"] = [{"id": str(i), "arguments": ["{result}", str(i)]} for i in range(3)]
    reviewed, params = prepare(tmp_path, doc, ctx)
    result = ops.apply("diagnostic-campaign", reviewed["plan"], ctx)["data"]["result"]
    assert result["state"] == "completed" and len(result["trials"]) == 6
    assert json.loads((Path(params["destination"])/"trials"/"0004"/"result.json").read_text())["metric"] == 4


def test_real_standalone_driver_does_not_require_importable_tower(tmp_path, ctx):
    doc = recipe(tmp_path, "print('TARGET')\nraise SystemExit(7)\n", kind="campaign")
    reviewed, params = prepare(tmp_path, doc, ctx)
    # Normal apply stages and executes; reuse the staged driver in an independently staged workspace.
    ops.apply("diagnostic-campaign", reviewed["plan"], ctx)
    import shutil
    root = tmp_path/"standalone"
    root.mkdir()
    old = Path(params["destination"])
    for name in ("driver.py", "recipe.json", "execution.json"):
        shutil.copyfile(old/name, root/name)
    shutil.copytree(old/"sources", root/"sources")
    process = subprocess.run([sys.executable, "-I", "-S", str(root/"driver.py"), str(root/"recipe.json"), str(root)], capture_output=True, text=True, timeout=10)
    assert process.returncode == 0, process.stderr
    assert json.loads(process.stdout)["state"] == "completed"


@pytest.mark.parametrize("result", [(True, "812", "812"), (False, "transport timeout", None), (True, "accepted but unavailable", None)])
def test_native_slurm_submission_durable_no_retry_even_if_uncertain(tmp_path, ctx, result):
    calls = []
    slurm = SimpleNamespace(b=SimpleNamespace(), submit=lambda argv, cwd: (calls.append((argv, cwd)) or result))
    ctx = replace(ctx, slurm=slurm)
    doc = recipe(tmp_path)
    doc["mode"] = "slurm"
    reviewed, params = prepare(tmp_path, doc, ctx)
    report = ops.apply("minimize-reproducer", reviewed["plan"], ctx)
    assert len(calls) == 1
    assert report["data"]["result"]["state"] == ("accepted" if result[1] == "812" else "unknown")
    root = Path(params["destination"])
    assert (root/"launch.intent.json").exists() and (root/"submission.result.json").exists()
    assert not (root/"run.claim.json").exists()
    assert subprocess.run(["sh", "-n", str(root/"diagnostic.sbatch")], capture_output=True).returncode == 0
    with pytest.raises(ValueError):
        ops.apply("minimize-reproducer", reviewed["plan"], ctx)
    assert len(calls) == 1


@pytest.mark.parametrize("restriction", ["remote", "replay", "cancelled", "scope"])
def test_review_lifecycle_never_executes_in_wrong_context(tmp_path, ctx, restriction):
    reviewed, params = prepare(tmp_path, recipe(tmp_path), ctx)
    if restriction == "remote":
        ctx = replace(ctx, files=SimpleNamespace(remote=True))
    elif restriction == "replay":
        ctx = replace(ctx, replay=True)
    elif restriction == "scope":
        ctx = replace(ctx, scope={"cluster": "other"})
    else:
        ctx.cancel.set()
    with pytest.raises(ValueError):
        ops.apply("minimize-reproducer", reviewed["plan"], ctx)
    assert not Path(params["destination"]).exists()


@pytest.mark.parametrize("field,value", [("max_trials", 0), ("max_trials", True), ("trial_seconds", float("nan")), ("cpus", 1000), ("output_bytes", 10), ("repeats", 100), ("memory_mb", -1)])
def test_invalid_execution_limits_refused(tmp_path, field, value):
    doc = recipe(tmp_path)
    doc["limits"][field] = value
    with pytest.raises(ValueError):
        driver.validate(doc)


@pytest.mark.parametrize("argv", [["python3", "file"], ["/bin/echo", "{file:missing}"], ["/bin/echo", "x{input}"], ["/bin/echo", "bad\x00"]])
def test_ambiguous_arguments_and_sources_refused(tmp_path, argv):
    doc = recipe(tmp_path)
    doc["command"] = argv
    with pytest.raises(ValueError):
        driver.validate(doc)


def test_delta_debug_empty_and_nonmonotonic_oracles():
    assert driver.minimize([1, 2], lambda values: True)["items"] == []
    result = driver.minimize([1, 2, 3, 4], lambda values: 2 in values and 4 in values)
    assert result["items"] == [2, 4] and result["one_minimal"]
    result = driver.minimize([1], lambda values: None)
    assert result["state"] == "original-not-confirmed" and not result["one_minimal"]


def test_cancellation_interrupts_active_child_and_still_publishes_trial_receipt(tmp_path, ctx, monkeypatch):
    doc = recipe(tmp_path, "import time\ntime.sleep(30)\n", kind="campaign")
    reviewed, params = prepare(tmp_path, doc, ctx)
    original = driver._execute
    def execute(*args, **kwargs):
        timer = threading.Timer(.2, ctx.cancel.set)
        timer.start()
        try:
            return original(*args, **kwargs)
        finally:
            timer.cancel()
    monkeypatch.setattr(driver, "_execute", execute)
    result = ops.apply("diagnostic-campaign", reviewed["plan"], ctx)["data"]["result"]
    assert result["state"] == "partial"
    assert result["trials"][0]["state"] == "cancelled"
    assert (Path(params["destination"])/"trials"/"0000"/"result.json.receipt").exists()


def test_trial_cannot_modify_source_then_claim_scientific_success(tmp_path, ctx):
    program = "from pathlib import Path\nPath(__file__).write_text('changed')\nprint('TARGET')\nraise SystemExit(7)\n"
    doc = recipe(tmp_path, program, kind="campaign")
    reviewed, params = prepare(tmp_path, doc, ctx)
    with pytest.raises(ValueError, match="source changed"):
        ops.apply("diagnostic-campaign", reviewed["plan"], ctx)
    root = Path(params["destination"])
    assert (root/"error.json").exists() and (root/"run.claim.json").exists()
    assert not (root/"result.json").exists()


def test_simultaneous_apply_executes_only_one_campaign(tmp_path, ctx):
    from concurrent.futures import ThreadPoolExecutor
    doc = recipe(tmp_path, "print('TARGET')\nraise SystemExit(7)\n", kind="campaign")
    reviewed, params = prepare(tmp_path, doc, ctx)
    def apply():
        try:
            return ops.apply("diagnostic-campaign", reviewed["plan"], ctx)
        except (ValueError, FileExistsError) as exc:
            return exc
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: apply(), range(2)))
    assert sum(isinstance(result, dict) for result in results) == 1
    receipt = json.loads((Path(params["destination"])/"result.json").read_text())
    assert len(receipt["trials"]) == doc["limits"]["repeats"]


def test_offline_examples_measure_real_numerical_difference_and_all_views(tmp_path, ctx):
    import runpy
    examples = Path(__file__).parents[1]/"examples"/"scientific-investigation"
    prepare_example = runpy.run_path(str(examples/"prepare_demo.py"))["prepare"]
    collect = runpy.run_path(str(examples/"collect_numerical.py"))["collect"]
    root = prepare_example(tmp_path/"demo")
    params = {"manifest": str(root/"campaign.json"), "destination": str(tmp_path/"measured"), "action": "launch"}
    reviewed = ops.run("diagnostic-campaign", params, ctx)
    ops.apply("diagnostic-campaign", reviewed["plan"], ctx)
    measured = collect(params["destination"])
    numerical_path = write(root/"numerical.json", measured)
    report = ops.run("numerical-stability", {"manifest": numerical_path}, ctx)
    by_config = {row["configuration"]: row for row in report["data"]["measurements"]}
    assert by_config["naive"]["mean"] == 0 and by_config["naive"]["state"] == "failed"
    assert by_config["compensated"]["mean"] == 1 and by_config["compensated"]["state"] == "observed-pass"
    for feature, name in [("recommendation", "recommendation.json"), ("investigation-plan", "planner.json"),
                          ("scientific-impact", "lineage.json"), ("regression-investigation", "regression.json")]:
        assert ops.run(feature, {"manifest": str(root/name)}, ctx)["rows"]
