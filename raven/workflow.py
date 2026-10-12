"""Bounded, offline dependency planning with explicit runtime estimates.

The schedule assumes every dependency succeeds and unlimited scheduler capacity.
It is a conditional estimate, never a queue forecast or an orchestration engine.
Preparing individual submission plans does not submit or execute any script.
"""
from __future__ import annotations

from collections import deque
import copy
import json
import math
import os
from pathlib import Path
import re

from . import clock

MAX_NODES = 512
MAX_EDGES = 4096
MAX_DURATION = 1e12
MAX_INTEGER = 2147483647
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_SCENARIOS = ("lower", "estimate", "upper")
_RESOURCE_KEYS = {"cpus_per_task", "ntasks", "ntasks_per_node", "nodes", "gpus",
                  "mem", "time", "partition", "account", "qos", "constraint"}


def _text(value, label, limit=256):
    if not isinstance(value, str) or not value or len(value) > limit or not all(c.isprintable() for c in value):
        raise ValueError(f"{label} must be a nonempty printable string of at most {limit} characters")
    return value


def _number(value, label, *, positive=True, limit=MAX_DURATION):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or abs(value) > limit or not math.isfinite(value) or (value <= 0 if positive else value < 0)):
        comparator = "positive" if positive else "nonnegative"
        raise ValueError(f"{label} must be a finite {comparator} number no greater than {limit:g}")
    return float(value)


def _parameters(value, label):
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    pending = [(value, 0)]
    count, characters = 0, 0
    while pending:
        item, depth = pending.pop()
        count += 1
        if count > 2048 or depth > 8:
            raise ValueError(f"{label} exceeds the 2048-value or eight-level limit")
        if isinstance(item, dict):
            if len(item) > 2048:
                raise ValueError(f"{label} exceeds the 2048-value limit")
            for key, child in item.items():
                _text(key, label + " key", 128)
                characters += len(key)
                pending.append((child, depth + 1))
        elif isinstance(item, list):
            if len(item) > 2048:
                raise ValueError(f"{label} exceeds the 2048-value limit")
            pending.extend((child, depth + 1) for child in item)
        elif isinstance(item, str):
            if len(item) > 4096 or not all(c.isprintable() for c in item):
                raise ValueError(f"{label} contains an oversized or nonprintable string")
            characters += len(item)
        elif item is None or isinstance(item, bool):
            pass
        elif isinstance(item, (int, float)):
            if abs(item) > 1e15 or not math.isfinite(item):
                raise ValueError(f"{label} contains an unbounded or nonfinite number")
        else:
            raise ValueError(f"{label} must contain only finite JSON values")
        if characters > 128 * 1024:
            raise ValueError(f"{label} exceeds the 128 KiB text limit")
    return copy.deepcopy(value)


def _resources(value, label):
    if not isinstance(value, dict) or set(value) - _RESOURCE_KEYS:
        raise ValueError(f"{label} accepts only {', '.join(sorted(_RESOURCE_KEYS))}")
    result = dict(value)
    for key in {"cpus_per_task", "ntasks", "ntasks_per_node", "nodes", "gpus"} & result.keys():
        amount = result[key]
        minimum = 0 if key == "gpus" else 1
        if isinstance(amount, bool) or not isinstance(amount, int) or not minimum <= amount <= MAX_INTEGER:
            raise ValueError(f"{label}.{key} must be an integer in {minimum}..{MAX_INTEGER}")
    if "ntasks" in result and "ntasks_per_node" in result:
        raise ValueError(f"{label}: choose ntasks or ntasks_per_node, not both")
    for key in {"mem", "time", "partition", "account", "qos", "constraint"} & result.keys():
        _text(result[key], f"{label}.{key}", 256)
    if "mem" in result and not re.fullmatch(r"\d+(?:\.\d+)?[KMGTkmgt]?", result["mem"]):
        raise ValueError(f"{label}.mem needs a Slurm memory amount such as 16G")
    if "time" in result:
        # The existing preflight owns the full Slurm option grammar; this purely
        # lexical helper has no I/O or scheduler calls.
        from .submission import _time
        if not _time(result["time"]):
            raise ValueError(f"{label}.time needs a valid Slurm time expression")
    return result


def _validate(recipe):
    keys = {"version", "kind", "nodes", "name", "label", "description", "deadline", "deadline_seconds"}
    if not isinstance(recipe, dict) or set(recipe) - keys:
        raise ValueError("workflow must be a JSON object with version, kind, nodes, and optional name/label/description/deadline")
    if type(recipe.get("version")) is not int or recipe.get("version") != 1 or recipe.get("kind") != "raven.workflow":
        raise ValueError("workflow needs version 1 and kind 'raven.workflow'")
    nodes = recipe.get("nodes")
    if not isinstance(nodes, list) or not 1 <= len(nodes) <= MAX_NODES:
        raise ValueError(f"workflow needs 1..{MAX_NODES} nodes")
    result = {"version": 1, "kind": "raven.workflow", "nodes": []}
    for key in ("name", "label", "description"):
        if key in recipe:
            result[key] = _text(recipe[key], key, 4096 if key == "description" else 256)
    if "deadline" in recipe and "deadline_seconds" in recipe:
        raise ValueError("choose an absolute deadline or relative deadline_seconds, not both")
    for key in ("deadline", "deadline_seconds"):
        if key in recipe:
            result[key] = _number(recipe[key], key, positive=False, limit=1e15 if key == "deadline" else MAX_DURATION)
    ids, edges = set(), 0
    for index, node in enumerate(nodes):
        label = f"nodes[{index}]"
        if not isinstance(node, dict) or set(node) - {"id", "label", "script", "depends_on", "runtime_seconds", "duration", "resources", "parameters"}:
            raise ValueError(f"{label} accepts id, label, script, depends_on, runtime_seconds/duration, resources, and parameters")
        jid = node.get("id")
        if not isinstance(jid, str) or not _ID.fullmatch(jid):
            raise ValueError(f"{label}.id needs 1..64 ASCII letters, digits, dots, underscores, or dashes")
        if jid in ids:
            raise ValueError(f"duplicate workflow node id: {jid}")
        ids.add(jid)
        normalized = {"id": jid, "label": _text(node.get("label", jid), label + ".label"),
                      "resources": _resources(node.get("resources", {}), label + ".resources")}
        dependencies = node.get("depends_on", [])
        if not isinstance(dependencies, list) or len(dependencies) > MAX_NODES:
            raise ValueError(f"{label}.depends_on must be a list with at most {MAX_NODES} node IDs")
        if any(not isinstance(dep, str) or not _ID.fullmatch(dep) for dep in dependencies):
            raise ValueError(f"{label}.depends_on accepts only workflow node IDs; external job IDs and dependency operators are unsupported")
        if len(set(dependencies)) != len(dependencies):
            raise ValueError(f"{label}.depends_on contains duplicate dependencies")
        edges += len(dependencies)
        if edges > MAX_EDGES:
            raise ValueError(f"workflow exceeds the {MAX_EDGES}-edge limit")
        normalized["depends_on"] = list(dependencies)
        if "runtime_seconds" in node and "duration" in node:
            raise ValueError(f"{label}: choose runtime_seconds or duration, not both")
        if "runtime_seconds" in node:
            estimate = _number(node["runtime_seconds"], label + ".runtime_seconds")
            normalized["duration"] = dict.fromkeys(_SCENARIOS, estimate)
        elif "duration" in node:
            duration = node["duration"]
            if not isinstance(duration, dict) or set(duration) != set(_SCENARIOS):
                raise ValueError(f"{label}.duration needs exactly lower, estimate, and upper")
            normalized["duration"] = {key: _number(duration[key], label + ".duration." + key) for key in _SCENARIOS}
            values = normalized["duration"]
            if not values["lower"] <= values["estimate"] <= values["upper"]:
                raise ValueError(f"{label}.duration must satisfy lower <= estimate <= upper")
        else:
            normalized["duration"] = dict.fromkeys(_SCENARIOS, None)
        if "script" in node:
            normalized["script"] = _text(node["script"], label + ".script", 4096)
        if "parameters" in node:
            normalized["parameters"] = _parameters(node["parameters"], label + ".parameters")
        result["nodes"].append(normalized)
    for node in result["nodes"]:
        for dependency in node["depends_on"]:
            if dependency not in ids:
                raise ValueError(f"node {node['id']} depends on unknown node {dependency}; external dependencies are unsupported")
    return result


def _graph(recipe):
    nodes = {node["id"]: node for node in recipe["nodes"]}
    children = {jid: [] for jid in nodes}
    indegree = {}
    for jid, node in nodes.items():
        indegree[jid] = len(node["depends_on"])
        for parent in node["depends_on"]:
            children[parent].append(jid)
    ready = deque(jid for jid in nodes if indegree[jid] == 0)
    order = []
    while ready:
        jid = ready.popleft()
        order.append(jid)
        for child in children[jid]:
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
    if len(order) != len(nodes):
        blocked = [jid for jid in nodes if indegree[jid]][:12]
        raise ValueError("workflow contains a dependency cycle; blocked nodes include " + ", ".join(blocked))
    return nodes, children, order


def load(path):
    """Read bounded JSON and reject malformed graphs before any script inspection."""
    from .planning_io import load_json
    recipe = _validate(load_json(path))
    _graph(recipe)
    # Keep the user-facing schema: unknown durations are omitted, so callers may
    # validate or analyze a loaded recipe again without inventing estimates.
    for node in recipe["nodes"]:
        if node["duration"]["estimate"] is None:
            node.pop("duration")
    return recipe


def _counts(resources):
    nodes = resources.get("nodes", 1)
    tasks = resources.get("ntasks")
    if tasks is None and "ntasks_per_node" in resources:
        tasks = resources["ntasks_per_node"] * nodes
    if tasks is None and nodes == 1:
        tasks = 1
    cpus = resources.get("cpus_per_task")
    return {"cpus": cpus * tasks if cpus is not None and tasks is not None else None,
            "gpus": resources.get("gpus", 0), "nodes": nodes}


def _option_count(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,10}", value):
        return None
    result = int(value)
    return result if 1 <= result <= MAX_INTEGER else None


def _envelope(results):
    totals = {key: dict.fromkeys(_SCENARIOS, 0.0) for key in ("core_hours", "gpu_hours", "node_hours")}
    warnings = []
    for item in results:
        counts = _counts(item["resources"])
        item["resource_counts"] = counts
        for output, resource in (("core_hours", "cpus"), ("gpu_hours", "gpus"), ("node_hours", "nodes")):
            for scenario in _SCENARIOS:
                count, duration = counts[resource], item["duration"][scenario]
                # Zero GPUs consume zero GPU-hours even for unknown durations.
                if count == 0:
                    continue
                if count is None or duration is None:
                    totals[output][scenario] = None
                elif totals[output][scenario] is not None:
                    totals[output][scenario] += count * duration / 3600
        if counts["cpus"] is None:
            warnings.append(f"{item['id']}: CPU allocation is unknown; declare cpus_per_task and multi-node ntasks/ntasks_per_node")
    events = []
    timing_complete = all(item["earliest_start"]["estimate"] is not None and item["earliest_finish"]["estimate"] is not None for item in results)
    cpu_complete = all(item["resource_counts"]["cpus"] is not None for item in results)
    for item in results:
        start, end = item["earliest_start"]["estimate"], item["earliest_finish"]["estimate"]
        if start is None or end is None:
            continue
        counts = item["resource_counts"]
        amounts = (1, counts["cpus"] or 0, counts["gpus"], counts["nodes"])
        events.append((start, amounts))
        events.append((end, tuple(-amount for amount in amounts)))
    events.sort(key=lambda event: event[0])
    active, peaks, i = [0] * 4, [0] * 4, 0
    # Aggregate simultaneous boundaries: a completed job frees its resources
    # before successors start. This avoids artificial endpoint overlap.
    while i < len(events):
        t = events[i][0]
        while i < len(events) and events[i][0] == t:
            active = [current + change for current, change in zip(active, events[i][1])]
            i += 1
        peaks = [max(peak, current) for peak, current in zip(peaks, active)]
    for index, key in enumerate(("peak_concurrent_jobs", "peak_cpus", "peak_gpus", "peak_requested_nodes")):
        complete = timing_complete and (cpu_complete if key == "peak_cpus" else True)
        totals[key] = peaks[index] if complete else None
        totals["known_" + key + "_lower_bound"] = peaks[index]
    totals["complete"] = timing_complete and cpu_complete
    totals["schedule"] = "nominal earliest-start schedule; unlimited scheduler capacity"
    totals["resource_basis"] = "recipe declarations only; missing GPUs are zero, single-node task count defaults to one, CPU requests remain unknown unless declared"
    return totals, warnings


def analyze(recipe, *, now=None):
    """Calculate conditional interval schedules, slack, paths, and resource costs.

    Missing duration estimates stay unknown, including downstream completion and
    global slack. All three timing scenarios satisfy every AND-success dependency.
    """
    normalized = _validate(recipe)
    nodes, children, order = _graph(normalized)
    timestamp = clock.now() if now is None else now
    timestamp = _number(timestamp, "now", positive=False, limit=1e15)
    results = {}
    layers = []
    for jid in order:
        node = nodes[jid]
        item = copy.deepcopy(node)
        parents = node["depends_on"]
        layer = max((results[parent]["layer"] for parent in parents), default=-1) + 1
        while len(layers) <= layer:
            layers.append([])
        layers[layer].append(jid)
        item["layer"] = layer
        start, finish = {}, {}
        for scenario in _SCENARIOS:
            predecessors = [results[parent]["earliest_finish"][scenario] for parent in parents]
            start[scenario] = None if any(value is None for value in predecessors) else max(predecessors, default=0.0)
            duration = node["duration"][scenario]
            finish[scenario] = start[scenario] + duration if start[scenario] is not None and duration is not None else None
            if finish[scenario] is not None and finish[scenario] <= start[scenario]:
                raise ValueError(f"node {jid}: positive {scenario} duration collapses at floating-point time precision; this estimate range cannot be scheduled accurately")
        item.update(earliest_start=start, earliest_finish=finish, latest_start={}, latest_finish={}, slack={}, critical=False,
                    estimate_source="declared" if node["duration"]["estimate"] is not None else "unknown")
        results[jid] = item
    makespan = {}
    for scenario in _SCENARIOS:
        finishes = [item["earliest_finish"][scenario] for item in results.values()]
        makespan[scenario] = None if any(value is None for value in finishes) else max(finishes)
    for jid in reversed(order):
        item = results[jid]
        for scenario in _SCENARIOS:
            successors = [results[child]["latest_start"][scenario] for child in children[jid]]
            finish = None if makespan[scenario] is None or any(value is None for value in successors) else min(successors, default=makespan[scenario])
            duration = item["duration"][scenario]
            start = finish - duration if finish is not None and duration is not None else None
            if start is not None and start >= finish:
                raise ValueError(f"node {jid}: latest {scenario} interval collapses at floating-point time precision; this estimate range cannot be scheduled accurately")
            earliest = item["earliest_start"][scenario]
            slack = max(0.0, start - earliest) if start is not None and earliest is not None else None
            item["latest_finish"][scenario], item["latest_start"][scenario], item["slack"][scenario] = finish, start, slack
        item["critical"] = item["slack"]["estimate"] is not None and math.isclose(item["slack"]["estimate"], 0, abs_tol=1e-8)
    critical_nodes = [jid for jid in order if results[jid]["critical"]]
    critical_edges = [[parent, jid] for jid in order for parent in nodes[jid]["depends_on"]
                      if results[parent]["critical"] and results[jid]["critical"]
                      and math.isclose(results[parent]["earliest_finish"]["estimate"], results[jid]["earliest_start"]["estimate"], rel_tol=0, abs_tol=1e-8)]
    path = []
    if makespan["estimate"] is not None:
        position = {jid: index for index, jid in enumerate(order)}
        tail = max(order, key=lambda jid: (results[jid]["earliest_finish"]["estimate"], -position[jid]))
        while True:
            path.append(tail)
            parents = nodes[tail]["depends_on"]
            if not parents:
                break
            tail = max(parents, key=lambda jid: (results[jid]["earliest_finish"]["estimate"], -position[jid]))
        path.reverse()
    ordered_results = [results[jid] for jid in order]
    envelope, warnings = _envelope(ordered_results)
    unknown = [jid for jid in order if results[jid]["duration"]["estimate"] is None]
    if unknown:
        warnings.append("missing runtime estimates: " + ", ".join(unknown[:12]) + (" ..." if len(unknown) > 12 else ""))
    completion_at = {key: timestamp + value if value is not None else None for key, value in makespan.items()}
    if any(value is not None and value <= timestamp for value in completion_at.values()):
        warnings.append("some absolute completion timestamps exceed floating-point clock resolution; use the relative makespan instead")
        completion_at = {key: value if value is None or value > timestamp else None for key, value in completion_at.items()}
    deadline = None
    if "deadline" in normalized or "deadline_seconds" in normalized:
        if "deadline" in normalized:
            at = normalized["deadline"]
            available = at - timestamp
        else:
            available = normalized["deadline_seconds"]
            at = timestamp + available
            if available > 0 and at <= timestamp:
                at = None
                warnings.append("absolute deadline timestamp exceeds floating-point clock resolution; relative deadline_seconds remains exact")
        slack = {key: available - value if value is not None else None for key, value in makespan.items()}
        feasible = (True if makespan["upper"] is not None and makespan["upper"] <= available else
                    False if makespan["lower"] is not None and makespan["lower"] > available else None)
        deadline = {"at": at, "remaining_seconds": available, "slack": slack, "feasible": feasible,
                    "estimate_feasible": None if makespan["estimate"] is None else makespan["estimate"] <= available,
                    "slack_interval": {"lower": slack["upper"], "estimate": slack["estimate"], "upper": slack["lower"]},
                    "basis": "conditional on immediate capacity, declared runtime estimates, and successful predecessors"}
    return {"kind": "raven.workflow-analysis", "version": 1, "status": "incomplete" if unknown else "ok",
            "name": normalized.get("name", normalized.get("label", "Workflow")), "now": timestamp,
            "nodes": ordered_results, "order": order, "layers": layers,
            "parallel_frontier": [jid for jid in order if not nodes[jid]["depends_on"]],
            "critical_path": path, "critical_nodes": critical_nodes, "critical_edges": critical_edges,
            "makespan": makespan, "completion_at": completion_at, "deadline": deadline,
            "resource_envelope": envelope, "warnings": warnings,
            "scenario_semantics": "lower/estimate/upper select duration scenarios; latest-start and node-slack values are scenario results, not uncertainty bounds",
            "assumptions": ["All dependencies are AND-success (afterok) relationships.",
                            "All jobs have immediate, unlimited scheduler capacity; queue delays and reservations are excluded.",
                            "Durations are explicit estimates, not measurements or calibrated confidence intervals.",
                            "Resource totals reflect recipe declarations rather than script directives or measured allocations.",
                            "Dependency-depth layers describe structure; they are not simultaneous launch batches.",
                            "This is a review-only planner; no workflow jobs are submitted or orchestrated."]}


def plans(recipe, *, workdir=None):
    """Prepare all individual scripts for review, preserving symbolic dependencies.

    Graph errors and missing script declarations fail before any script reads. A
    preflight error prevents returning partial results. No fake Slurm dependency
    IDs are injected; launching each node independently does not run a workflow.
    """
    normalized = _validate(recipe)
    nodes, _, order = _graph(normalized)
    missing = [jid for jid in order if "script" not in nodes[jid]]
    if missing:
        raise ValueError("submission plans need script declarations for every node: " + ", ".join(missing[:12]))
    if workdir is None:
        root = Path.cwd()
    else:
        try:
            root = Path(_text(os.fspath(workdir), "workdir", 4096)).expanduser()
        except TypeError as exc:
            raise ValueError("workdir must be a printable path") from exc
    from .submission import prepare, _digest
    prepared = []
    failures = []
    cache = {}
    for jid in order:
        node = nodes[jid]
        script = Path(node["script"]).expanduser()
        if not script.is_absolute():
            script = root / script
        overrides = ["--" + key.replace("_", "-") + "=" + str(value) for key, value in node["resources"].items()
                     if not (key == "gpus" and value == 0)]
        # Repeated scripts with identical declarations are common in experiment
        # DAGs. Reuse one preflight snapshot rather than rereading the same file
        # hundreds of times. Submission still revalidates its digest later.
        key = (os.path.abspath(script), tuple(overrides), json.dumps(node.get("parameters"), sort_keys=True, allow_nan=False))
        if key not in cache:
            cache[key] = prepare(script, workdir=root, overrides=overrides, parameters=node.get("parameters"))
        plan = copy.deepcopy(cache[key])
        if not plan["valid"]:
            messages = [issue["message"] for issue in plan["issues"] if issue["level"] == "error"]
            failures.append(jid + ": " + "; ".join(messages[:3]))
        if plan["resources"].get("dependency"):
            failures.append(jid + ": script contains a scheduler dependency; workflow planning accepts only symbolic recipe dependencies")
        effective = plan["resources"]
        ambiguous = {"array", "cpus_per_gpu", "ntasks_per_gpu", "ntasks_per_core", "ntasks_per_socket",
                     "gpus_per_node", "gpus_per_socket", "gpus_per_task"} & effective.keys()
        if ambiguous:
            failures.append(jid + ": script contains allocation modes outside the workflow resource model; use explicit task/CPU/node/GPU totals instead of " + ", ".join(sorted(ambiguous)))
        script_gpu_request = (any(key in effective for key in ("gpus", "gpus_per_node", "gpus_per_task", "gpus_per_socket"))
                              or re.search(r"(?:^|,)gpu(?::|=|$)", effective.get("gres", ""), flags=re.I))
        if node["resources"].get("gpus", 0) == 0 and script_gpu_request:
            failures.append(jid + ": recipe models zero GPUs but the script requests GPU resources; declare the total GPUs in the recipe or remove that script request")
        expected = _counts(node["resources"])
        actual_nodes = effective.get("nodes", "1")
        if actual_nodes != str(expected["nodes"]):
            failures.append(jid + ": script node allocation differs from the recipe envelope; declare an explicit matching nodes count")
        if expected["cpus"] is not None:
            tasks = _option_count(effective.get("ntasks"))
            if tasks is None and "ntasks_per_node" in effective:
                per_node = _option_count(effective["ntasks_per_node"])
                tasks = per_node * expected["nodes"] if per_node is not None else None
            if tasks is None and expected["nodes"] == 1:
                tasks = 1
            per_task = _option_count(effective.get("cpus_per_task", "1"))
            actual_cpus = per_task * tasks if per_task is not None and tasks is not None else None
            if actual_cpus != expected["cpus"]:
                failures.append(jid + ": script CPU/task allocation differs from the recipe envelope; declare matching ntasks or ntasks_per_node")
        plan.update(workflow_node_id=jid, symbolic_dependencies=list(node["depends_on"]),
                    workflow_orchestration="review_only", requires_workflow_orchestration=bool(node["depends_on"]),
                    submittable=False)
        plan["plan_id"] = _digest(plan)
        prepared.append(plan)
    if failures:
        raise ValueError("workflow script preflight failed; no plans returned: " + " | ".join(failures[:8]))
    return prepared
