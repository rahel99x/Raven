"""Sequential evidence -> reviewed trial -> measured finding regression tests."""
import copy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys
import threading

import pytest

from tower import diagnostic_trials, investigation_workflow as W, operations as O, ops_investigation as adapter
from tower.derived_results import ResultCache


def write(path, value):
    path.write_text(json.dumps(value))
    return str(path)


def setup(tmp_path):
    identity = {"cluster": "test", "job_id": "12", "attempt": "producer-attempt-1"}
    source = {"schema": "tower.numerical-stability/v1", "metrics": [{"name": "answer", "reference": 1, "absolute": 0, "relative": 0}],
              "runs": [{"id": "pilot", "configuration": "baseline", "metrics": {"answer": 1}}]}
    planner = {"schema": "tower.investigation-plan/v1", "budget": {"seconds": 20, "resource_seconds": 30, "trials": 1},
               "known_evidence": ["forged"], "actions": [{"id": "accuracy", "kind": "scientific-validation", "seconds": 5,
                 "resource_seconds": 5, "queue_seconds": 0, "decision_gain": 1, "success_probability": 1,
                 "scientific_risk": 0, "requires": ["baseline"], "model": "declared pilot estimate"}]}
    script = tmp_path/"measure.py"
    script.write_text("import json,sys\nfrom pathlib import Path\nPath(sys.argv[1]).write_text(json.dumps({'answer': float(sys.argv[2])}))\nprint('MEASURED')\n")
    recipe = {"schema": "tower.diagnostic-campaign/v1", "id": "accuracy", "kind": "campaign", "mode": "local-diagnostic",
              "command": [str(Path(sys.executable).resolve()), "{file:measure.py}"],
              "files": [{"name": "measure.py", "path": "measure.py", "sha256": hashlib.sha256(script.read_bytes()).hexdigest()}],
              "limits": {"max_trials": 4, "trial_seconds": 2, "total_seconds": 10, "cpus": 1, "memory_mb": 256, "output_bytes": 4096, "repeats": 2},
              "oracle": {"exit_code": 0, "contains": "MEASURED"},
              "cases": [{"id": "correct", "arguments": ["{result}", "1"]}, {"id": "incorrect", "arguments": ["{result}", "2"]}]}
    document = {"schema": W.SCHEMA, "identity": identity,
                "sources": [{"id": "baseline", "operation": "numerical-stability", "path": "baseline.json"}],
                "planner": "planner.json", "diagnostics": [{"id": "accuracy", "operation": "diagnostic-campaign",
                                                               "manifest": "recipe.json", "metrics": source["metrics"]}]}
    for name, value in [("baseline.json", source), ("planner.json", planner), ("recipe.json", recipe), ("workflow.json", document)]:
        write(tmp_path/name, value)
    params = {"manifest": str(tmp_path/"workflow.json"), "action": "inspect", "diagnostic_id": "accuracy", "destination": str(tmp_path/"run")}
    ctx = O.Context(scope={"cluster": "test", "user": "researcher"}, state_dir=str(tmp_path/"state"), cancel=threading.Event())
    return document, params, ctx


@pytest.fixture(autouse=True)
def cache(monkeypatch):
    cache = ResultCache()
    monkeypatch.setattr(W, "RESULTS", cache)
    return cache


def test_form_registration_and_inspection_are_real_and_read_only(tmp_path):
    doc, params, ctx = setup(tmp_path)
    assert O.specification("investigation-workflow")["group"] == "Investigation"
    result = O.run("investigation-workflow", params, ctx)
    assert "plan" not in result
    assert result["data"]["findings"][0]["state"] == "observed-pass"
    assert result["data"]["suggested"][0]["id"] == "accuracy"
    assert result["data"]["stages"][-1] == "updated-findings"
    assert not (tmp_path/"run").exists()


def test_changed_evidence_recomputes_only_changed_analysis(tmp_path, cache):
    doc, params, ctx = setup(tmp_path)
    before = W.run(params, ctx)
    stats = cache.stats()
    W.run(params, ctx)
    assert cache.stats()["misses"] == stats["misses"]
    source = json.loads((tmp_path/"baseline.json").read_text())
    source["runs"][0]["metrics"]["answer"] = 2
    write(tmp_path/"baseline.json", source)
    after = W.run(params, ctx)
    assert after["data"]["revision"] != before["data"]["revision"]
    assert after["data"]["findings"][0]["state"] == "failed"
    assert cache.stats()["misses"] == stats["misses"]+1


@pytest.mark.parametrize("kind", ["absent", "unknown"])
def test_missing_evidence_cannot_satisfy_declared_planner_prerequisites(tmp_path, kind):
    doc, params, ctx = setup(tmp_path)
    if kind == "absent":
        (tmp_path/"baseline.json").unlink()
    else:
        value = json.loads((tmp_path/"baseline.json").read_text())
        value["runs"][0]["metrics"] = {}
        write(tmp_path/"baseline.json", value)
    result = W.run(params, ctx)
    assert result["data"]["missing"][0]["id"] == "baseline"
    assert result["data"]["suggested"] == []
    with pytest.raises(ValueError, match="not admitted"):
        W.run(dict(params, action="review-diagnostic"), ctx)


@pytest.mark.parametrize("contents", [None, [], {"unexpected": 1}])
def test_malformed_present_source_is_not_reported_as_missing(tmp_path, contents):
    doc, params, ctx = setup(tmp_path)
    write(tmp_path/"baseline.json", contents)
    with pytest.raises(ValueError):
        W.run(params, ctx)


@pytest.mark.parametrize("change", ["cluster", "job", "attempt", "missing-anchor", "duplicate"])
def test_exact_selected_scheduler_identity(tmp_path, change):
    doc, params, ctx = setup(tmp_path)
    anchor = {"submit": "2026-01-01T00:00:00", "start": "2026-01-01T00:00:01", "user": "researcher"}
    doc["scheduler"] = dict(anchor)
    job = {"id": "12", "cluster": "test", **anchor}
    ctx = replace(ctx, selected="12", jobs=(job,))
    if change == "cluster":
        doc["identity"]["cluster"] = "other"
    elif change == "job":
        doc["identity"]["job_id"] = "13"
    elif change == "attempt":
        job["start"] = "2026-02-01T00:00:00"
    elif change == "missing-anchor":
        del doc["scheduler"]
    else:
        ctx = replace(ctx, finished=(job,))
    write(Path(params["manifest"]), doc)
    with pytest.raises(ValueError):
        W.run(params, ctx)


def test_source_changed_mid_analysis_and_missing_file_appearing_are_rejected(tmp_path, monkeypatch):
    doc, params, ctx = setup(tmp_path)
    original = W.analyze
    changed = False
    def analyze(operation, document, ctx, **kwargs):
        nonlocal changed
        result = original(operation, document, ctx, **kwargs)
        if not changed:
            changed = True
            write(tmp_path/"baseline.json", {"changed": True})
        return result
    monkeypatch.setattr(W, "analyze", analyze)
    with pytest.raises(ValueError, match="changed"):
        W.run(params, ctx)


@pytest.mark.parametrize("source", ["workflow.json", "baseline.json", "planner.json", "recipe.json", "measure.py"])
def test_changed_review_sources_block_execution(tmp_path, source):
    doc, params, ctx = setup(tmp_path)
    reviewed = W.run(dict(params, action="review-diagnostic"), ctx)
    path = tmp_path/source
    if source.endswith(".json"):
        value = json.loads(path.read_text())
        value["changed"] = True
        write(path, value)
    else:
        path.write_text("raise RuntimeError('changed')")
    with pytest.raises(ValueError):
        W.apply(reviewed["plan"], ctx)
    assert not (tmp_path/"run").exists()


def test_real_reviewed_campaign_returns_measured_findings_without_external_conversion(tmp_path, monkeypatch):
    doc, params, ctx = setup(tmp_path)
    monkeypatch.setattr(adapter, "_launch_local", lambda recipe, root, ctx: diagnostic_trials.run(recipe, root, cancel=ctx.cancel))
    review = O.run("investigation-workflow", dict(params, action="review-diagnostic"), ctx)
    assert not (tmp_path/"run").exists()
    result = O.apply("investigation-workflow", review["plan"], ctx)
    assert result["data"]["diagnostic"]["data"]["result"]["state"] == "completed"
    status = O.run("investigation-workflow", dict(params, action="status"), ctx)
    updated = status["data"]["updated_findings"][0]
    assert updated["state"] == "failed"
    assert {row["configuration"]: row["state"] for row in updated["result"]["measurements"]} == {"correct": "observed-pass", "incorrect": "failed"}
    assert updated["provenance"]["workspace"] == params["destination"]
    with pytest.raises(ValueError, match="attempted|claimed"):
        O.apply("investigation-workflow", review["plan"], ctx)
    (tmp_path/"run"/"trials"/"0000"/"result.json").unlink()
    status = O.run("investigation-workflow", dict(params, action="status"), ctx)
    rows = status["data"]["updated_findings"][0]["result"]["measurements"]
    assert rows[0]["state"] == "unknown"
    assert rows[0]["missing"] == ["trial-0"]


def test_recommendation_expiry_is_not_cached(tmp_path, monkeypatch):
    _, _, ctx = setup(tmp_path)
    calls = []
    def evaluate(document):
        calls.append(1)
        return {"state": "hypothesis" if len(calls) == 1 else "expired"}
    monkeypatch.setitem(adapter._READERS, "recommendation", ("", "", evaluate))
    assert W.analyze("recommendation", {}, ctx)["state"] == "hypothesis"
    assert W.analyze("recommendation", {}, ctx)["state"] == "expired"


def test_cancellation_before_inspection_never_reads_files(tmp_path, monkeypatch):
    _, params, ctx = setup(tmp_path)
    ctx.cancel.set()
    with pytest.raises(ValueError, match="cancelled"):
        O.run("investigation-workflow", params, ctx)


@pytest.mark.parametrize("change", ["attempt", "unbound", "recipe"])
def test_status_cannot_borrow_another_attempt_or_unbound_workspace(tmp_path, monkeypatch, change):
    doc, params, ctx = setup(tmp_path)
    monkeypatch.setattr(adapter, "_launch_local", lambda recipe, root, ctx: diagnostic_trials.run(recipe, root, cancel=ctx.cancel))
    review = W.run(dict(params, action="review-diagnostic"), ctx)
    W.apply(review["plan"], ctx)
    if change == "attempt":
        doc["identity"]["attempt"] = "second"
        write(Path(params["manifest"]), doc)
    elif change == "unbound":
        path = tmp_path/"run"/".tower-receipt.json"
        receipt = json.loads(path.read_text())
        del receipt["workflow_binding"]
        write(path, receipt)
    else:
        path = tmp_path/"recipe.json"
        recipe = json.loads(path.read_text())
        recipe["id"] = "different"
        write(path, recipe)
    with pytest.raises(ValueError, match="not bound|different recipe"):
        W.run(dict(params, action="status"), ctx)


def test_missing_source_appearing_during_inspection_is_a_revision_change(tmp_path, monkeypatch):
    _, params, ctx = setup(tmp_path)
    original, source = W.analyze, (tmp_path/"baseline.json").read_text()
    (tmp_path/"baseline.json").unlink()
    def analyze(operation, document, ctx, **kwargs):
        result = original(operation, document, ctx, **kwargs)
        (tmp_path/"baseline.json").write_text(source)
        return result
    monkeypatch.setattr(W, "analyze", analyze)
    with pytest.raises(ValueError, match="changed"):
        W.run(params, ctx)


@pytest.mark.parametrize("kind", ["duplicate-source", "source-operation", "duplicate-diagnostic", "diagnostic-operation", "unknown-link", "unexpected-field", "too-many-sources"])
def test_manifest_validation_stays_bounded_and_strict(tmp_path, kind):
    doc, params, ctx = setup(tmp_path)
    if kind == "duplicate-source":
        doc["sources"] *= 2
    elif kind == "source-operation":
        doc["sources"][0]["operation"] = []
    elif kind == "duplicate-diagnostic":
        doc["diagnostics"] *= 2
    elif kind == "diagnostic-operation":
        doc["diagnostics"][0]["operation"] = {}
    elif kind == "unknown-link":
        doc["diagnostics"][0]["id"] = "not-declared"
    elif kind == "unexpected-field":
        doc["execute_automatically"] = True
    else:
        doc["sources"] = [dict(doc["sources"][0], id=str(i)) for i in range(17)]
    write(Path(params["manifest"]), doc)
    with pytest.raises(ValueError):
        W.run(params, ctx)


def test_optional_intent_uses_existing_compiler_without_executing_commands(tmp_path, monkeypatch):
    doc, params, ctx = setup(tmp_path)
    examples = Path(__file__).resolve().parents[1]/"examples"/"runtime"
    doc["intent"] = {"manifest": str(examples/"diagnostic-intent.json"), "capabilities": str(examples/"site-capabilities.json")}
    write(Path(params["manifest"]), doc)
    monkeypatch.setattr(adapter, "_launch_local", lambda *args: pytest.fail("inspection executed a command"))
    result = W.run(params, ctx)
    assert result["data"]["compiled"]["schema"] == "tower.diagnostic-plan/v1"
    assert result["data"]["compiled"]["executable"] is False


def test_schema_and_generated_demo_match_runtime_contract(tmp_path):
    import importlib.util
    import jsonschema
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("demo", root/"examples/scientific-investigation/prepare_demo.py")
    demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo)
    destination = demo.prepare(tmp_path/"demo")
    document = json.loads((destination/"workflow.json").read_text())
    schema = json.loads((root/"docs/schemas/investigation-workflow.schema.json").read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(document, schema)
    result = O.run("investigation-workflow", {"manifest": str(destination/"workflow.json")}, O.Context(scope={"cluster": "synthetic"}))
    assert result["data"]["suggested"][0]["id"] == "accuracy-audit"


def test_expiry_between_review_and_apply_blocks_execution(tmp_path, monkeypatch):
    doc, params, ctx = setup(tmp_path)
    clock = [100.0]
    from tower import science_contracts
    monkeypatch.setattr(science_contracts.time, "time", lambda: clock[0])
    evidence = {"x": 1}
    recommendation = {"schema": "tower.recommendation-review/v1", "contract": {
        "id": "policy", "identity": doc["identity"], "created": 90, "expires": 120,
        "evidence": [{"id": "pilot", "sha256": O.digest(evidence)}],
        "preconditions": [{"source": "pilot", "path": ["x"], "op": "eq", "value": 1}],
        "acceptance": [{"source": "trial", "path": ["answer"], "op": "eq", "value": 1}], "intervention": "measure"},
        "snapshot": {"identity": doc["identity"], "evidence": [{"id": "pilot", "data": evidence, "sha256": O.digest(evidence), "complete": True}]}}
    write(tmp_path/"baseline.json", recommendation)
    doc["sources"][0]["operation"] = "recommendation"
    write(Path(params["manifest"]), doc)
    reviewed = W.run(dict(params, action="review-diagnostic"), ctx)
    clock[0] = 121
    with pytest.raises(ValueError, match="expired or changed"):
        W.apply(reviewed["plan"], ctx)
    assert not (tmp_path/"run").exists()


def test_campaign_budget_exhaustion_preserves_all_unexecuted_measurements(tmp_path):
    doc, params, ctx = setup(tmp_path)
    root = tmp_path/"partial"
    root.mkdir()
    recipe = json.loads((tmp_path/"recipe.json").read_text())
    recipe["cases"] = [{"id": str(i), "arguments": ["{result}", "1"]} for i in range(30)]
    recipe["limits"]["repeats"] = 10
    recipe["limits"]["max_trials"] = 10
    diagnostic_trials.validate(recipe)
    write(root/"recipe.json", recipe)
    outcome = {"recipe_sha256": O.digest(recipe), "trials": [], "state": "partial"}
    write(root/"result.json", outcome)
    result = W._measurements(doc["diagnostics"][0], {"workspace": str(root), "result": outcome, "recipe_sha256": O.digest(recipe)}, ctx, doc["identity"])
    assert result[0]["state"] == "unknown"
    assert len(result[0]["result"]["measurements"]) == 30
    assert sum(len(row["missing"]) for row in result[0]["result"]["measurements"]) == 300


@pytest.mark.parametrize("state", ["partial", "interrupted", "starting-or-unknown"])
def test_nested_execution_warning_is_preserved(tmp_path, monkeypatch, state):
    doc, params, ctx = setup(tmp_path)
    del doc["diagnostics"][0]["metrics"]
    write(Path(params["manifest"]), doc)
    recipe = json.loads((tmp_path/"recipe.json").read_text())
    nested = O.report("diagnostic-campaign", "Incomplete", status="warning", rows=["Incomplete"], data={
        "state": state, "recipe_sha256": O.digest(recipe), "result": None,
        "workflow_binding": {"schema": "tower.investigation-binding/v1", "identity": doc["identity"], "diagnostic_id": "accuracy"}})
    monkeypatch.setattr(adapter, "run", lambda *args: nested)
    result = W.run(dict(params, action="status"), ctx)
    assert result["status"] == "warning"


def test_selected_scheduler_attempt_is_part_of_shared_analysis_key(tmp_path, cache):
    _, _, ctx = setup(tmp_path)
    source = json.loads((tmp_path/"baseline.json").read_text())
    job = {"id": "12", "submit": "first", "start": "first", "user": "researcher", "cluster": "test"}
    selected = replace(ctx, selected="12", jobs=(job,))
    W.analyze("numerical-stability", source, selected)
    before = cache.stats()["misses"]
    changed = replace(selected, jobs=({**job, "start": "requeued"},))
    W.analyze("numerical-stability", source, changed)
    assert cache.stats()["misses"] == before+1
