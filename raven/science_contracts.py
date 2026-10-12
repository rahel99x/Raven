"""Bounded scientific recommendation contracts; no code evaluation or inferred truth."""
from __future__ import annotations

import math
import re
import time

from .operations import canonical, digest


def obj(value, required, optional=(), label="document"):
    if not isinstance(value, dict) or not set(required) <= value.keys() or set(value) - set(required) - set(optional):
        raise ValueError(f"{label}: required {', '.join(sorted(required))}; unknown fields are not accepted")
    return value


def rows(value, name, maximum=4096, minimum=0):
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise ValueError(f"{name} requires {minimum}..{maximum} rows")
    return value


def text(value, name="identifier", maximum=256):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or any(not c.isprintable() for c in value):
        raise ValueError(f"{name} must be nonempty bounded printable text")
    return value


def number(value, name="number", low=-1e100, high=1e100):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    try:
        result = float(value)
    except OverflowError as exc:
        raise ValueError(f"{name} is out of range") from exc
    if not math.isfinite(result) or not low <= result <= high:
        raise ValueError(f"{name} must be within {low}..{high}")
    return result


def integer(value, name, low=0, high=4096):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{name} must be an integer within {low}..{high}")
    return value


def sha(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise ValueError("SHA256 requires 64 lowercase hexadecimal characters")
    return value


def identity(value):
    obj(value, {"cluster", "job_id", "attempt"}, {"code_sha256", "inputs_sha256", "environment_sha256"}, "attempt identity")
    for key, item in value.items():
        sha(item) if key.endswith("sha256") else text(item, key)
    return value


def evidence_index(records):
    index = {}
    for row in rows(records, "evidence", 1024):
        obj(row, {"id", "sha256", "complete", "data"}, label="evidence record")
        key = text(row["id"])
        if key in index or not isinstance(row["complete"], bool) or not isinstance(row["data"], dict):
            raise ValueError("Evidence needs unique IDs, boolean coverage and object data")
        sha(row["sha256"])
        if digest(row["data"]) != row["sha256"]:
            raise ValueError("Evidence bytes do not match their declared digest")
        index[key] = row
    return index


def validate_query(query):
    obj(query, {"source", "path", "op", "value"}, label="query")
    text(query["source"])
    for component in rows(query["path"], "query path", 16, 1):
        text(component)
    if query["op"] not in {"eq", "le", "ge", "lt", "gt"}:
        raise ValueError("Query operators: eq, le, ge, lt, gt")
    if query["op"] == "eq":
        if not isinstance(query["value"], (str, bool, int, float)) or query["value"] is None:
            raise ValueError("Equality requires a scalar value")
        if isinstance(query["value"], str):
            text(query["value"])
        elif not isinstance(query["value"], bool):
            number(query["value"])
    else:
        number(query["value"])
    return query


def query(query, index):
    validate_query(query)
    row = index.get(query["source"])
    if row is None or not row["complete"]:
        return {"query": query, "state": "unknown", "reason": "Missing or incomplete source"}
    value = row["data"]
    for component in query["path"]:
        if not isinstance(value, dict) or component not in value:
            return {"query": query, "state": "unknown", "reason": "Missing value"}
        value = value[component]
    expected = query["value"]
    op = query["op"]
    if op == "eq":
        if isinstance(expected, bool) != isinstance(value, bool):
            passed = False
        else:
            passed = value == expected
    else:
        try:
            value = number(value, "observed value")
        except ValueError:
            return {"query": query, "state": "unknown", "reason": "Value is not finite numeric evidence"}
        passed = {"le": value <= expected, "ge": value >= expected,
                  "lt": value < expected, "gt": value > expected}[op]
    return {"query": query, "state": "pass" if passed else "fail", "observed": value}


def evaluate(document, *, now=None):
    """Evaluate a pinned hypothesis and optionally its measured experiment outcome."""
    canonical(document)
    obj(document, {"schema", "contract", "snapshot"}, {"outcome"})
    if document["schema"] != "raven.recommendation-review/v1":
        raise ValueError("Expected raven.recommendation-review/v1")
    contract = obj(document["contract"], {"id", "identity", "created", "expires", "evidence", "preconditions", "acceptance", "intervention"}, {"prediction", "alternatives"}, "contract")
    text(contract["id"])
    identity(contract["identity"])
    text(contract["intervention"], "intervention", 4096)
    created = number(contract["created"], "created", 0, 1e12)
    expires = number(contract["expires"], "expires", created, created + 366*86400)
    clock = number(time.time() if now is None else now, "current time", 0, 1e12)
    for field in ("preconditions", "acceptance"):
        for item in rows(contract[field], field, 256, 1):
            validate_query(item)
    snapshot = obj(document["snapshot"], {"identity", "evidence"}, label="snapshot")
    identity(snapshot["identity"])
    index = evidence_index(snapshot["evidence"])
    pins = {}
    for pin in rows(contract["evidence"], "pinned evidence", 1024, 1):
        obj(pin, {"id", "sha256"}, label="evidence pin")
        key, expected = text(pin["id"]), sha(pin["sha256"])
        if key in pins:
            raise ValueError("Duplicate evidence pin")
        pins[key] = expected
    if any(item["source"] not in pins for item in contract["preconditions"]):
        raise ValueError("Every precondition source must be pinned")
    reasons = []
    state = "hypothesis"
    if snapshot["identity"] != contract["identity"]:
        state, reasons = "stale", ["Exact execution identity changed"]
    if clock < created - 1 or clock >= expires:
        state, reasons = "expired", ["Contract is outside its declared validity window"]
    for key, expected in pins.items():
        source = index.get(key)
        if source is None or not source["complete"]:
            if state == "hypothesis":
                state = "inconclusive"
            reasons.append(f"Missing or incomplete evidence: {key}")
        elif source["sha256"] != expected:
            if state not in {"expired", "stale"}:
                state = "stale"
            reasons.append(f"Evidence changed: {key}")
    preconditions = [query(item, index) for item in contract["preconditions"]]
    if state == "hypothesis" and any(item["state"] != "pass" for item in preconditions):
        state = "inapplicable" if any(item["state"] == "fail" for item in preconditions) else "inconclusive"
    acceptance = []
    outcome = document.get("outcome")
    if outcome is not None:
        obj(outcome, {"contract_sha256", "experiment_id", "identity", "evidence"}, label="outcome")
        text(outcome["experiment_id"])
        identity(outcome["identity"])
        sha(outcome["contract_sha256"])
        outcome_index = evidence_index(outcome["evidence"])
        if outcome["contract_sha256"] != digest(contract):
            raise ValueError("Outcome belongs to a different contract")
        # The experiment can have a new job ID; its declared build/input context must agree.
        for key in ("cluster", "code_sha256", "inputs_sha256", "environment_sha256"):
            if key in contract["identity"] and outcome["identity"].get(key) != contract["identity"][key]:
                raise ValueError("Experiment context does not match the reviewed contract")
        acceptance = [query(item, outcome_index) for item in contract["acceptance"]]
        if state == "hypothesis":
            states = {item["state"] for item in acceptance}
            state = "refuted" if "fail" in states else "inconclusive" if "unknown" in states else "supported"
    return {"contract_id": contract["id"], "contract_sha256": digest(contract), "state": state,
            "reasons": reasons, "preconditions": preconditions, "acceptance": acceptance,
            "intervention": contract["intervention"], "expires": expires,
            "limit": "Support applies only to the declared checks, workload and experiment; it is not proof of universal correctness or causation."}
