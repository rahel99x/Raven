"""Claim-level reverse lineage and conservative revalidation proposal planning."""
from __future__ import annotations

from collections import deque
import heapq

from .science_contracts import canonical, number, obj, rows, text


def analyze(document, *, cancel=None):
    def check():
        if cancel is not None and cancel.is_set():
            raise ValueError("Scientific lineage analysis cancelled")

    check()
    canonical(document)
    obj(document, {"schema", "nodes", "changed", "actions"})
    if document["schema"] != "raven.scientific-lineage/v1":
        raise ValueError("Expected raven.scientific-lineage/v1")
    nodes, reverse, unresolved_inputs = {}, {}, {}
    for node in rows(document["nodes"], "nodes", 10000):
        check()
        obj(node, {"id", "kind", "dependencies", "complete"}, label="lineage node")
        key = text(node["id"])
        if key in nodes or node["kind"] not in {"artifact", "attempt", "claim", "policy", "validation"} or not isinstance(node["complete"], bool):
            raise ValueError("Lineage nodes need unique IDs, known kinds and boolean coverage")
        deps = rows(node["dependencies"], "dependencies", 256)
        if len(set(map(text, deps))) != len(deps) or key in deps:
            raise ValueError("Duplicate or self-referential dependency")
        nodes[key] = node
    indegree = {key: 0 for key in nodes}
    for key, node in nodes.items():
        check()
        unresolved_inputs[key] = sorted(set(node["dependencies"]) - nodes.keys())
        for dependency in node["dependencies"]:
            reverse.setdefault(dependency, []).append(key)
            if dependency in nodes:
                indegree[key] += 1
    queue = deque(key for key, count in indegree.items() if not count)
    order = []
    while queue:
        check()
        key = queue.popleft()
        order.append(key)
        for child in reverse.get(key, ()):
            indegree[child] -= 1
            if not indegree[child]:
                queue.append(child)
    if len(order) != len(nodes):
        raise ValueError("Scientific lineage must be acyclic")
    changed = {text(key) for key in rows(document["changed"], "changed nodes", 10000)}
    if changed - nodes.keys():
        raise ValueError("Changed roots must be explicit known nodes")
    affected, uncertain = set(changed), set()
    witnesses = {key: {"root": key, "parent": None, "distance": 0} for key in changed}
    for key in order:
        check()
        node = nodes[key]
        if not node["complete"] or unresolved_inputs[key] or any(dep in uncertain for dep in node["dependencies"]):
            uncertain.add(key)
        for dep in node["dependencies"]:
            if dep in affected:
                affected.add(key)
                # Parent links reconstruct the evidence path without quadratic copies.
                proposed = {"root": witnesses[dep]["root"], "parent": dep,
                            "distance": witnesses[dep]["distance"] + 1}
                if key not in witnesses or proposed["distance"] < witnesses[key]["distance"]:
                    witnesses[key] = proposed
    claims = {key for key, node in nodes.items() if node["kind"] == "claim" and key in affected | uncertain}
    required_claims = claims - uncertain
    needed = set(required_claims)
    pending = list(required_claims)
    while pending:
        check()
        for parent in nodes[pending.pop()]["dependencies"]:
            if parent in nodes and parent not in needed:
                needed.add(parent)
                pending.append(parent)
    required_nodes = affected & needed
    actions, seen = [], set()
    for action in rows(document["actions"], "revalidation actions", 2048):
        check()
        obj(action, {"id", "covers", "requires", "cost", "method"}, label="revalidation action")
        key = text(action["id"])
        if key in seen:
            raise ValueError("Duplicate revalidation action")
        seen.add(key)
        text(action["method"], "validation method", 1024)
        covers = {text(item) for item in rows(action["covers"], "covers", 256, 1)}
        requires = {text(item) for item in rows(action["requires"], "requires", 256)}
        if (covers | requires) - nodes.keys() or covers & requires:
            raise ValueError("Actions require known, disjoint covered and prerequisite nodes")
        cost = number(action["cost"], "action cost", .000001, 1e12)
        actions.append({**action, "covers": sorted(covers), "requires": sorted(requires), "cost": cost})
    # Index each incidence once. Covering a node updates only the actions
    # mentioning it; no iteration rebuilds the whole affected/required sets.
    covered, selected = set(), []
    covering, waiting = {}, {}
    blocked, gains, revisions = [], [], []
    prohibited, chosen = set(), set()
    heap = []
    blocking = affected | uncertain
    for index, action in enumerate(actions):
        check()
        covers, requires = set(action["covers"]), set(action["requires"])
        blocked.append(len(requires & blocking))
        gains.append(len(covers & required_nodes))
        revisions.append(0)
        if covers & uncertain:
            prohibited.add(index)
        for key in covers & required_nodes:
            covering.setdefault(key, []).append(index)
        for key in requires & blocking:
            waiting.setdefault(key, []).append(index)

    def offer(index):
        if index not in chosen and index not in prohibited and not blocked[index] and gains[index]:
            action = actions[index]
            heapq.heappush(heap, (-gains[index] / action["cost"], action["id"], index, revisions[index]))

    for index in range(len(actions)):
        offer(index)
    outstanding = len(required_claims)
    while heap and outstanding:
        check()
        _, _, index, revision = heapq.heappop(heap)
        if index in chosen or revisions[index] != revision:
            continue
        chosen.add(index)
        best = actions[index]
        selected.append(best)
        touched = set()
        for key in best["covers"]:
            if key in covered:
                continue
            covered.add(key)
            if key in required_claims:
                outstanding -= 1
            for affected_action in covering.get(key, ()):
                gains[affected_action] -= 1
                touched.add(affected_action)
            for dependent in waiting.get(key, ()):
                blocked[dependent] -= 1
                touched.add(dependent)
        for affected_action in touched:
            revisions[affected_action] += 1
            offer(affected_action)
    return {"affected": sorted(affected), "uncertain": sorted(uncertain),
            "claims": sorted(claims), "witnesses": witnesses,
            "missing_dependencies": {key: value for key, value in unresolved_inputs.items() if value},
            "proposed_actions": selected, "unresolved_claims": sorted(claims-covered),
            "cost": sum(action["cost"] for action in selected),
            "method": "greedy dependency-ordered coverage; no global minimality claim",
            "limit": "This is a revalidation proposal. No claim becomes valid until its declared checks actually pass; dependency impact alone does not prove a wrong scientific result."}
