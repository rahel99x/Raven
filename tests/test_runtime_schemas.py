"""Published runtime shapes agree with native validation and preserve its limits."""
import copy
import json
from pathlib import Path

import pytest

jsonschema = pytest.importorskip("jsonschema")
from referencing import Registry, Resource

from tower import diagnostic_recipes, execution_simulator, ops_runtime, runtime_control, science_fields
from tower.operations import Context

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples/runtime"
SCHEMAS = ROOT / "docs/schemas"
PAIRS = {
    "control-config": "runtime-control", "diagnostic-intent": "diagnostic-intent",
    "execution-model": "execution-model", "io-request": "io-request",
    "query-source": "query-source", "runtime-request": "runtime-request",
    "scientific-field": "scientific-field", "site-capabilities": "site-capabilities",
    "summary-query": "summary-query",
}


def example(name):
    return json.loads((EXAMPLES / (name + ".json")).read_text())


@pytest.fixture(scope="module")
def validators():
    documents = {path.stem: json.loads(path.read_text()) for path in SCHEMAS.glob("*.json")}
    registry = Registry().with_resources((document["$id"], Resource.from_contents(document))
                                         for document in documents.values() if "$id" in document)
    result = {}
    for name, schema in PAIRS.items():
        document = documents[schema + ".schema"]
        jsonschema.Draft202012Validator.check_schema(document)
        result[name] = jsonschema.Draft202012Validator(document, registry=registry)
    return result


def native(name, document, tmp_path):
    if name == "control-config":
        return runtime_control.validate_configuration(document)
    if name == "execution-model":
        return execution_simulator.simulate(document)
    if name == "diagnostic-intent":
        return diagnostic_recipes.compile_recipe(document, example("site-capabilities"))
    if name == "site-capabilities":
        return diagnostic_recipes.compile_recipe(example("diagnostic-intent"), document)
    if name == "query-source":
        return diagnostic_recipes.summarize_query(document, example("summary-query"))
    if name == "summary-query":
        return diagnostic_recipes.summarize_query(example("query-source"), document)
    if name == "scientific-field":
        return science_fields.inspect_field(document)
    mailbox = tmp_path / "mailbox"
    runtime_control.initialize(mailbox, example("control-config"))
    document = copy.deepcopy(document)
    document["mailbox"] = str(mailbox)
    path = tmp_path / "request.json"
    path.write_text(json.dumps(document))
    feature = "runtime-steering" if name == "runtime-request" else "io-coordination"
    return ops_runtime.run(feature, {"manifest": str(path)}, Context(scope={"backend": "local", "cluster": "fixture"}))


@pytest.mark.parametrize("name", PAIRS)
def test_published_examples_pass_both_contracts(validators, tmp_path, name):
    document = example(name)
    validators[name].validate(document)
    assert native(name, document, tmp_path)


@pytest.mark.parametrize("name", PAIRS)
@pytest.mark.parametrize("change", ["required", "unknown"])
def test_missing_marker_or_unknown_root_field_is_refused(validators, name, change):
    document = example(name)
    if change == "required":
        del document["schema"]
    else:
        document["unexpected"] = True
    assert not validators[name].is_valid(document)


@pytest.mark.parametrize("name", PAIRS)
def test_checked_in_invalid_example_is_rejected_by_schema_and_native(validators, tmp_path, name):
    path = ROOT / "tests/fixtures/runtime-contracts/invalid" / (name + ".json")
    document = json.loads(path.read_text())
    assert not validators[name].is_valid(document)
    with pytest.raises(ValueError):
        native(name, document, tmp_path)


@pytest.mark.parametrize("name,path,value", [
    ("control-config", ("attempts",), ["same", "same"]),
    ("control-config", ("commands", "learning-rate", "parameters", "value"), {"type": "enum", "choices": []}),
    ("control-config", ("io", "capacity"), True),
    ("diagnostic-intent", ("required",), ["x"] * 33),
    ("diagnostic-intent", ("budget", "seconds"), 604801),
    ("execution-model", ("resources", "node0", "rate"), 0),
    ("execution-model", ("domain", "minimum_rate_ratio"), 2),
    ("execution-model", ("domain", "fixed_workload"), 1),
    ("runtime-request", ("expected_epoch",), True),
    ("runtime-request", ("parameters", "value"), []),
    ("io-request", ("lease_seconds",), 3601),
    ("io-request", ("participants",), []),
    ("scientific-field", ("shape",), [1, 1, 1, 1]),
    ("scientific-field", ("values",), [True]),
    ("summary-query", ("operations",), ["sum", "sum"]),
    ("site-capabilities", ("adapters",), None),
])
def test_local_limits_and_scalar_types(validators, name, path, value):
    document = example(name)
    if name == "site-capabilities":
        value = document["adapters"] * 9
    target = document
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    assert not validators[name].is_valid(document)


@pytest.mark.parametrize("name,change", [
    ("scientific-field", lambda data: data["values"].pop()),
    ("control-config", lambda data: data["commands"]["learning-rate"]["parameters"]["value"].update(minimum=2)),
    ("control-config", lambda data: data["io"].update(capacity=1.0)),
    ("execution-model", lambda data: data["events"][0].update(depends=["absent"])),
    ("diagnostic-intent", lambda data: data.update(approximations={"not-required": ["alternate"]})),
    ("query-source", lambda data: data["rows"][0]["values"].update(duration_s="not numeric")),
])
def test_structural_acceptance_does_not_replace_relational_native_checks(validators, tmp_path, name, change):
    document = example(name)
    change(document)
    validators[name].validate(document)
    with pytest.raises(ValueError):
        native(name, document, tmp_path)


def test_io_inspection_and_request_shapes_are_distinct(validators):
    document = example("io-request")
    inspection = {key: document[key] for key in ("schema", "mailbox", "attempt")}
    validators["io-request"].validate(inspection)
    inspection["fallback"] = "proceed"
    assert not validators["io-request"].is_valid(inspection)


def test_unselected_query_columns_are_preserved_without_reinterpretation(validators, tmp_path):
    source = example("query-source")
    source["rows"][0]["values"]["metadata"] = {"producer": "application-owned"}
    validators["query-source"].validate(source)
    assert native("query-source", source, tmp_path)["summaries"]["duration_s"]["count"] == 3


@pytest.mark.parametrize("threshold,accepted", [(0, True), (1, True), (.05, True),
                                               (True, False), (None, False), (-.01, False), (1.01, False)])
def test_execution_acceptance_policy_matches_native_limits(validators, threshold, accepted):
    document = example("execution-model")
    document["domain"]["max_validation_relative_error"] = threshold
    assert validators["execution-model"].is_valid(document) is accepted
    if accepted:
        assert execution_simulator.simulate(document)["validation"]["acceptance_threshold"] == threshold
    else:
        with pytest.raises(ValueError):
            execution_simulator.simulate(document)
