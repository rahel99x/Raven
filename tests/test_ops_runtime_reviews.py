import copy
import json
from pathlib import Path
import shutil

import pytest

from tower.operations import Context
from tower.operations import digest
from tower import ops_runtime
from tower import runtime_control


EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "runtime"


def context(**kwargs):
    return Context(scope={"backend": "local", "cluster": "fixture"}, **kwargs)


@pytest.mark.parametrize("feature,params", [
    ("execution-simulation", {"manifest": "execution-model.json", "scenario": '{"rates":{"fabric":10}}'}),
    ("diagnostic-recipe", {"manifest": "diagnostic-intent.json", "capabilities": "site-capabilities.json"}),
    ("information-query", {"manifest": "query-source.json", "query": "summary-query.json"}),
    ("scientific-field", {"manifest": "scientific-field.json"}),
])
def test_real_examples_render_through_operations(feature, params):
    params = {key: str(EXAMPLES / value) if key in {"manifest", "capabilities", "query"} else value for key, value in params.items()}
    result = ops_runtime.run(feature, params, context())
    assert result["feature"] == feature
    assert result["rows"]
    assert "plan" not in result
    json.dumps(result, allow_nan=False)


@pytest.fixture
def runtime_request(tmp_path):
    mailbox = tmp_path / "mailbox"
    configuration = json.loads((EXAMPLES / "control-config.json").read_text())
    runtime_control.initialize(mailbox, configuration)
    data = json.loads((EXAMPLES / "runtime-request.json").read_text())
    data["mailbox"] = str(mailbox)
    path = tmp_path / "request.json"
    path.write_text(json.dumps(data))
    return path, data, runtime_control.RuntimeControl(mailbox, data["attempt"])


def test_review_does_not_queue_and_apply_waits_for_application(runtime_request):
    path, data, control = runtime_request
    review = ops_runtime.run("runtime-steering", {"manifest": str(path)}, context())
    assert not control.status()["requests"]
    applied = ops_runtime.apply("runtime-steering", review["plan"], context())
    assert applied["data"]["status"] == "pending"
    assert ops_runtime.apply("runtime-steering", review["plan"], context())["data"]["id"] == data["request_id"]
    control.safe_point("epoch", lambda *args: {"confirmed": True})
    assert control.status()["requests"][0]["status"] == "applied"


@pytest.mark.parametrize("change", ["source", "scope", "epoch", "mailbox", "plan", "replay"])
def test_apply_rejects_changed_review_evidence(runtime_request, change):
    path, data, control = runtime_request
    review = ops_runtime.run("runtime-steering", {"manifest": str(path)}, context())
    ctx = context()
    if change == "source":
        path.write_text(path.read_text() + " ")
    elif change == "scope":
        ctx = Context(scope={"backend": "another"})
    elif change == "epoch":
        control.submit("earlier", data["command"], data["parameters"], expected_epoch=0)
        control.safe_point("epoch", lambda *args: {})
    elif change == "mailbox":
        shutil.rmtree(control.directory)
        runtime_control.initialize(control.directory, json.loads((EXAMPLES / "control-config.json").read_text()))
    elif change == "plan":
        review["plan"]["payload"]["document"]["parameters"]["value"] = .5
    elif change == "replay":
        ctx = context(replay=True)
    with pytest.raises(ValueError):
        ops_runtime.apply("runtime-steering", review["plan"], ctx)


@pytest.mark.parametrize("key,value", [("ttl", 0), ("request_id", "bad\nID"), ("expected_epoch", False), ("parameters", {"value": 200})])
def test_invalid_action_cannot_even_prepare_review(runtime_request, key, value):
    path, data, control = runtime_request
    data[key] = value
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        ops_runtime.run("runtime-steering", {"manifest": str(path)}, context())
    assert not control.status()["requests"]


def test_io_review_does_not_assert_application_arrivals(runtime_request):
    path, data, control = runtime_request
    document = json.loads((EXAMPLES / "io-request.json").read_text())
    document["mailbox"] = data["mailbox"]
    path.write_text(json.dumps(document))
    review = ops_runtime.run("io-coordination", {"manifest": str(path)}, context())
    assert not control.status()["permits"]
    result = ops_runtime.apply("io-coordination", review["plan"], context())
    assert result["data"]["state"] == "waiting"
    assert result["data"]["arrivals"] == []
    accepted = control.permit(document["request_id"], participants=document["participants"], arrived=document["participants"],
                               lease_seconds=document["lease_seconds"], wait_seconds=document["wait_seconds"], fallback=document["fallback"])
    assert accepted["state"] == "granted"


def test_io_read_only_overview(runtime_request):
    path, data, control = runtime_request
    path.write_text(json.dumps({"schema": "tower.io-request/v1", "mailbox": data["mailbox"], "attempt": data["attempt"]}))
    result = ops_runtime.run("io-coordination", {"manifest": str(path)}, context())
    assert "plan" not in result


def test_scenario_duplicate_keys_refused():
    with pytest.raises(ValueError, match="Duplicate"):
        ops_runtime.run("execution-simulation", {"manifest": str(EXAMPLES / "execution-model.json"), "scenario": '{"rates":{},"rates":{}}'}, context())


@pytest.mark.parametrize("qualification", ["passed", "failed", "unqualified"])
def test_prediction_record_retains_exact_review_and_qualification(tmp_path, qualification):
    model = json.loads((EXAMPLES / "execution-model.json").read_text())
    if qualification == "failed":
        model["observations"][-1]["actual_seconds"] = 100
    elif qualification == "unqualified":
        model["domain"].pop("max_validation_relative_error")
    source = tmp_path / "source.json"
    source.write_text(json.dumps(model))
    destination = tmp_path / "record"
    scenario = {"rates": {"fabric": 10}}
    result = ops_runtime.run("execution-simulation", {"manifest": str(source), "scenario": json.dumps(scenario),
                                                     "destination": str(destination)}, context())
    assert not destination.exists()
    assert result["data"]["validation"]["status"] == qualification
    assert result["status"] == ("ok" if qualification == "passed" else "warning")
    if qualification == "failed":
        assert "withdrawn" in result["summary"]
        assert any("WITHDRAWN" in row for row in result["rows"])
        assert result["data"]["predicted_seconds"] is None
    recorded = ops_runtime.apply("execution-simulation", result["plan"], context())
    assert (destination / "model.json").read_bytes() == source.read_bytes()
    assert json.loads((destination / "scenario.json").read_text()) == scenario
    prediction = json.loads((destination / "prediction.json").read_text())
    assert prediction == result["data"]
    receipt = json.loads((destination / ".tower-receipt.json").read_text())
    assert receipt == recorded["data"]["receipt"]
    assert receipt["prediction_digest"] == digest(prediction)
    assert receipt["validation"]["status"] == qualification
    assert receipt["prediction_id"] == prediction["prediction_id"]
    assert not (destination / ".tower-incomplete").exists()
    with pytest.raises(ValueError, match="new directory"):
        ops_runtime.apply("execution-simulation", result["plan"], context())


@pytest.mark.parametrize("change", ["source", "scope", "plan", "replay", "destination"])
def test_prediction_publication_refuses_changed_review(tmp_path, change):
    source = tmp_path / "source.json"
    source.write_bytes((EXAMPLES / "execution-model.json").read_bytes())
    destination = tmp_path / "record"
    result = ops_runtime.run("execution-simulation", {"manifest": str(source), "destination": str(destination)}, context())
    ctx = context()
    if change == "source":
        source.write_text(source.read_text() + " ")
    elif change == "scope":
        ctx = Context(scope={"backend": "another"})
    elif change == "plan":
        result["plan"]["payload"]["prediction"]["validated"] = False
    elif change == "replay":
        ctx = context(replay=True)
    else:
        destination.mkdir()
        (destination / "user-file").write_text("preserve")
    with pytest.raises(ValueError):
        ops_runtime.apply("execution-simulation", result["plan"], ctx)
    if change == "destination":
        assert (destination / "user-file").read_text() == "preserve"
        assert not (destination / "prediction.json").exists()
    else:
        assert not destination.exists()
