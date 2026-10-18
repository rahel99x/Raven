import copy
import json
from pathlib import Path

from hypothesis import given, strategies as st
from jsonschema import Draft202012Validator
import pytest

from raven.protocol_contracts import CONTROL, worker_control, worker_control_schema


def control(operation):
    values = {"session_id": "session", "generation": 1, "major": 1, "minor": 0,
              "nonce": "a" * 64, "build_digest": "b" * 64, "config": {},
              "task": {}, "id": "task", "result_digest": "c" * 64, "records": None, "error": None}
    return {"op": operation, **{key: values[key] for key in CONTROL[operation]}}


def runtime_valid(document):
    try:
        worker_control(document)
        return True
    except ValueError:
        return False


def test_generated_contract_schema_has_no_drift_or_remote_refs():
    path = Path(__file__).parents[1] / "docs/schemas/worker-control.v1.schema.json"
    assert json.loads(path.read_text()) == worker_control_schema()
    assert '"$ref"' not in path.read_text()


@pytest.mark.parametrize("operation", CONTROL)
@given(st.sampled_from([None, False, True, -1, 0, 1, 2, -1.0, 1.0, 1.5, "", "new", [], {}, {"description": "new minor field"}]), st.integers(0, 12))
def test_runtime_and_independent_schema_agree(operation, replacement, selected):
    document = control(operation)
    fields = [*document, "extensions", "unknown_action"]
    document[fields[selected % len(fields)]] = replacement
    assert runtime_valid(document) == Draft202012Validator(worker_control_schema()).is_valid(document)


@pytest.mark.parametrize("operation", CONTROL)
def test_descriptive_extension_is_explicit_and_does_not_change_action(operation):
    original = control(operation)
    extended = {**original, "extensions": {"display_description": "new description", "minor_hint": 3}}
    assert worker_control(extended) == extended
    assert all(extended[key] == value for key, value in original.items())
    with pytest.raises(ValueError):
        worker_control({**original, "execute_as_user": "other"})


@pytest.mark.parametrize("major", [0, 2, -1, True, "1"])
def test_unknown_major_never_migrates_action_semantics(major):
    value = {**control("hello"), "major": major}
    with pytest.raises(ValueError):
        worker_control(value)
    assert not Draft202012Validator(worker_control_schema()).is_valid(value)


def test_generated_schema_is_local_dependency_free_runtime(monkeypatch):
    import sys
    assert "jsonschema" not in worker_control.__globals__
    original = control("hello")
    value = copy.deepcopy(original)
    worker_control(value)
    assert value == original  # No implicit migration or caller mutation.
