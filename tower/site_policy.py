"""Explicit site capabilities and bounded collection admission; never inferred permission."""
from __future__ import annotations

import copy
from . import evidence as E

SCHEMA = "tower.site-policy/v1"
CAPABILITIES = "tower.capabilities/v1"


def validate(value):
    E.canonical(value)
    E.keys(value, {"schema", "id", "revision", "sources", "operations", "limits", "allocation_required", "allow_intrusive"}, label="site policy")
    if value["schema"] != SCHEMA:
        raise ValueError("Expected " + SCHEMA)
    for key in ("id", "revision"):
        E.text(value[key], key, 256)
    for key in ("sources", "operations"):
        if not isinstance(value[key], list) or not 1 <= len(value[key]) <= 256:
            raise ValueError(key + " must be an explicit bounded allow-list")
        for item in value[key]:
            E.text(item, key, 256)
        if len(set(value[key])) != len(value[key]):
            raise ValueError("Duplicate " + key)
    for key in ("allocation_required", "allow_intrusive"):
        if type(value[key]) is not bool:
            raise ValueError(key + " must be boolean")
    limits = E.keys(value["limits"], {"min_interval", "max_duration", "max_bytes", "max_records", "max_inflight"}, label="policy limits")
    for key in ("min_interval", "max_duration"):
        E.finite(limits[key], key, .001)
        if limits[key] > 86400:
            raise ValueError("Policy intervals cannot exceed one day")
    for key, upper in (("max_bytes", 64 << 20), ("max_records", 1000000), ("max_inflight", 64)):
        E.integer(limits[key], key, upper)
        if limits[key] < 1:
            raise ValueError(key + " must be positive")
    return copy.deepcopy(value)


def capabilities(value):
    E.canonical(value)
    E.keys(value, {"schema", "source", "version", "operations", "min_interval", "status"}, {"reason", "schema_versions"}, "capabilities")
    if value["schema"] != CAPABILITIES or value["status"] not in ("available", "unsupported", "inaccessible", "unknown"):
        raise ValueError("Invalid capabilities schema or status")
    E.text(value["source"], "capability source", 256)
    E.text(value["version"], "capability version", 256)
    if not isinstance(value["operations"], list) or len(value["operations"]) > 256:
        raise ValueError("Capability operations must be a bounded list")
    for item in value["operations"]:
        E.text(item, "operation", 256)
    if len(set(value["operations"])) != len(value["operations"]):
        raise ValueError("Duplicate capability operation")
    E.finite(value["min_interval"], "source interval", .001)
    if "reason" in value:
        E.text(value["reason"], "capability reason")
    if "schema_versions" in value:
        if not isinstance(value["schema_versions"], list) or len(value["schema_versions"]) > 64:
            raise ValueError("schema_versions must be a bounded list")
        for version in value["schema_versions"]:
            E.text(version, "schema version", 256)
    return copy.deepcopy(value)


def admit(policy, request, observed):
    policy, observed = validate(policy), capabilities(observed)
    E.keys(request, {"source", "operation", "interval", "duration", "max_bytes", "max_records", "allocation", "intrusive"}, label="collection request")
    for key in ("source", "operation"):
        E.text(request[key], key, 256)
    for key in ("interval", "duration"):
        E.finite(request[key], key, .001)
    for key in ("max_bytes", "max_records"):
        E.integer(request[key], key)
        if request[key] < 1:
            raise ValueError(key + " must be positive")
    if type(request["intrusive"]) is not bool:
        raise ValueError("intrusive must be boolean")
    if request["allocation"] is not None:
        E.identity(request["allocation"])
    reasons, limits = [], policy["limits"]
    if request["source"] not in policy["sources"] or request["source"] != observed["source"]:
        reasons.append("Source is not allowed by this policy/capability identity")
    if request["operation"] not in policy["operations"] or request["operation"] not in observed["operations"]:
        reasons.append("Operation is not explicitly supported and allowed")
    if observed["status"] != "available":
        reasons.append("Source capability is " + observed["status"])
    effective = max(limits["min_interval"], observed["min_interval"])
    if request["interval"] < effective:
        reasons.append(f"Interval is below the {effective:g} s site/source floor")
    for name in ("duration", "bytes", "records"):
        key = name if name == "duration" else "max_" + name
        if request[key] > limits["max_" + name]:
            reasons.append(name + " exceeds site budget")
    if policy["allocation_required"] and request["allocation"] is None:
        reasons.append("An exact allocated execution identity is required")
    if request["intrusive"] and not policy["allow_intrusive"]:
        reasons.append("Intrusive collection is not permitted")
    return {"schema": "tower.collection-admission/v1", "allowed": not reasons, "reasons": reasons,
            "policy_digest": E.digest(policy), "capabilities_digest": E.digest(observed),
            "request_digest": E.digest(request), "effective_min_interval": effective,
            "max_inflight": limits["max_inflight"],
            "limits": copy.deepcopy(limits), "request": copy.deepcopy(request)}


class SitePolicy:
    def __init__(self, value):
        self.value = validate(value)

    def admit(self, request, observed):
        return admit(self.value, request, observed)
