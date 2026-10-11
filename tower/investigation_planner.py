"""Budgeted mixed information-action planning from explicit, auditable models."""
from __future__ import annotations

from .science_contracts import canonical, integer, number, obj, rows, text

KINDS = {"configuration", "repeat", "trace", "scientific-validation", "placement", "numerical-audit"}


def plan(document):
    canonical(document)
    obj(document, {"schema", "budget", "actions", "known_evidence"}, {"committed", "risk_limit"})
    if document["schema"] != "tower.investigation-plan/v1":
        raise ValueError("Expected tower.investigation-plan/v1")
    budget = obj(document["budget"], {"seconds", "resource_seconds", "trials"}, label="budget")
    available = {key: number(budget[key], key, 0, 1e12) for key in ("seconds", "resource_seconds")}
    available["trials"] = integer(budget["trials"], "trials", 0, 256)
    known = set()
    for key in rows(document["known_evidence"], "known evidence"):
        if text(key) in known:
            raise ValueError("Duplicate known evidence")
        known.add(key)
    risk_limit = number(document.get("risk_limit", 0), "risk_limit", 0, 1)
    committed = set()
    for entry in rows(document.get("committed", []), "committed actions", 256):
        obj(entry, {"id", "seconds", "resource_seconds"}, label="committed action")
        key = text(entry["id"])
        if key in committed:
            raise ValueError("Duplicate committed action")
        committed.add(key)
        available["trials"] -= 1
        for field in ("seconds", "resource_seconds"):
            available[field] -= number(entry[field], field, 0, 1e12)
    if any(value < 0 for value in available.values()):
        raise ValueError("Committed work already exceeds the declared budget")
    candidates, excluded, seen = [], [], set()
    for action in rows(document["actions"], "actions", 2048):
        obj(action, {"id", "kind", "seconds", "resource_seconds", "queue_seconds", "decision_gain", "success_probability", "scientific_risk", "requires", "model"}, label="action")
        key = text(action["id"])
        if key in seen or action["kind"] not in KINDS:
            raise ValueError("Actions require unique IDs and supported kinds")
        seen.add(key)
        text(action["model"], "model provenance", 1024)
        seconds = number(action["seconds"], "seconds", .001, 1e9)
        resource = number(action["resource_seconds"], "resource_seconds", 0, 1e12)
        queue = number(action["queue_seconds"], "queue_seconds", 0, 1e9)
        gain = number(action["decision_gain"], "decision_gain", 0, 1)
        probability = number(action["success_probability"], "success_probability", 0, 1)
        risk = number(action["scientific_risk"], "scientific_risk", 0, 1)
        required = {text(item) for item in rows(action["requires"], "requires", 256)}
        reason = "already committed" if key in committed else "missing evidence" if required - known else "risk limit" if risk > risk_limit else None
        if reason:
            excluded.append({"id": key, "reason": reason, "missing": sorted(required-known)})
            continue
        utility = gain * probability * (1-risk)
        candidates.append({**action, "total_seconds": seconds+queue, "utility": utility,
                           "score": utility/(seconds+queue), "resource_seconds": resource})
    candidates.sort(key=lambda row: (-row["score"], row["resource_seconds"], row["id"]))
    selected = []
    for action in candidates:
        if action["utility"] <= 0:
            excluded.append({"id": action["id"], "reason": "no declared decision benefit"})
        elif available["trials"] < 1 or action["total_seconds"] > available["seconds"] or action["resource_seconds"] > available["resource_seconds"]:
            excluded.append({"id": action["id"], "reason": "remaining budget"})
        else:
            selected.append(action)
            available["seconds"] -= action["total_seconds"]
            available["resource_seconds"] -= action["resource_seconds"]
            available["trials"] -= 1
    return {"selected": selected, "excluded": excluded, "remaining": available,
            "method": "deterministic greedy declared decision gain per predicted wall second",
            "limits": ["Declared model values are not calibrated probabilities unless their producer establishes that.",
                       "Queue time and scientific risk are predictions, not scheduler or correctness guarantees.",
                       "The plan is sequential, does not execute actions, and is not claimed globally optimal."]}
