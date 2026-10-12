"""Bounded deterministic replay of declared, nonpreemptive resource DAGs.

This models fixed work and dependency structure, not arbitrary program execution,
MPI semantics, changing rank counts, or scheduler waiting time.
"""
from __future__ import annotations

import heapq
import math
import statistics

from .runtime_contracts import identifier, number, object_, sequence, text, unique_names
from .operations import digest

MAX_EVENTS = 20000


def validate(document):
    object_(document, {"schema", "workload", "resources", "events", "domain"}, {"placement", "routes", "observations"})
    if document["schema"] != "raven.execution-model/v1":
        raise ValueError("Expected raven.execution-model/v1")
    text(document["workload"], "immutable workload identity", 512)
    resources = document["resources"]
    if not isinstance(resources, dict) or not 1 <= len(resources) <= 256:
        raise ValueError("Declare 1..256 resources")
    for key, resource in resources.items():
        identifier(key)
        object_(resource, {"kind", "rate"}, {"latency"}, "resource")
        if not isinstance(resource["kind"], str) or resource["kind"] not in {"cpu", "network", "io"}:
            raise ValueError("Resource kind must be cpu, network or io")
        number(resource["rate"], "resource rate", positive=True)
        number(resource.get("latency", 0), "resource latency")
    domain = document["domain"]
    object_(domain, {"minimum_rate_ratio", "maximum_rate_ratio", "fixed_workload"}, {"max_validation_relative_error"})
    low = number(domain["minimum_rate_ratio"], "minimum rate ratio", positive=True)
    high = number(domain["maximum_rate_ratio"], "maximum rate ratio", positive=True)
    if not low <= 1 <= high or domain["fixed_workload"] is not True:
        raise ValueError("Model domain must contain baseline and explicitly require fixed_workload=true")
    if "max_validation_relative_error" in domain:
        number(domain["max_validation_relative_error"], "validation acceptance threshold", 0, 1)
    placement = document.get("placement", {})
    if not isinstance(placement, dict) or len(placement) > 4096:
        raise ValueError("Invalid rank placement")
    for rank, resource in placement.items():
        identifier(rank, "rank")
        if not isinstance(resource, str) or resources.get(resource, {}).get("kind") != "cpu":
            raise ValueError("Rank placement must reference CPU resources")
    routes = document.get("routes", {})
    if not isinstance(routes, dict) or len(routes) > 16384:
        raise ValueError("Invalid route map")
    for route, resource in routes.items():
        if not isinstance(route, str) or len(route.split("->")) != 2 or any(resources.get(part, {}).get("kind") != "cpu" for part in route.split("->")):
            raise ValueError("Routes must join declared CPU resource names with ->")
        if not isinstance(resource, str) or resources.get(resource, {}).get("kind") != "network":
            raise ValueError("Routes must reference network resources")
    events = sequence(document["events"], "events", MAX_EVENTS, 1)
    names = set()
    for event in events:
        object_(event, {"id", "kind", "work", "depends"}, {"resource", "rank", "source", "destination"}, "event")
        name = identifier(event["id"], "event ID")
        if name in names:
            raise ValueError("Duplicate event ID")
        names.add(name)
        number(event["work"], "event work")
        unique_names(event["depends"], "dependencies", 256)
        if not isinstance(event["kind"], str) or event["kind"] not in {"cpu", "network", "io"}:
            raise ValueError("Invalid event kind")
        _resource(event, resources, placement, routes)
    if any(dep not in names for event in events for dep in event["depends"]):
        raise ValueError("Dependency refers to an absent event")
    return document


def _resource(event, resources, placement, routes):
    kind = event["kind"]
    if "resource" in event:
        if set(event) & {"rank", "source", "destination"}:
            raise ValueError("Explicit resources cannot also declare rank routing")
        resource = event["resource"]
    elif kind == "cpu" and set(event) & {"rank", "source", "destination"} == {"rank"}:
        resource = placement.get(identifier(event["rank"], "rank"))
    elif kind == "network" and set(event) & {"rank", "source", "destination"} == {"source", "destination"}:
        source = placement.get(identifier(event["source"], "source rank"))
        destination = placement.get(identifier(event["destination"], "destination rank"))
        resource = routes.get(f"{source}->{destination}")
    else:
        raise ValueError("Event requires an explicit resource or supported rank routing")
    if not isinstance(resource, str) or resources.get(resource, {}).get("kind") != kind:
        raise ValueError("Missing or mismatched event resource/route")
    return resource


def _simulate(document, scenario, cancel=None):
    object_(scenario, set(), {"rates", "placement"}, "scenario")
    resources = document["resources"]
    rates = scenario.get("rates", {})
    if not isinstance(rates, dict) or set(rates) - set(resources):
        raise ValueError("Scenario rates must reference existing resources")
    for key, rate in rates.items():
        number(rate, "scenario rate", positive=True)
        ratio = rate / resources[key]["rate"]
        if not document["domain"]["minimum_rate_ratio"] <= ratio <= document["domain"]["maximum_rate_ratio"]:
            raise ValueError("Scenario is outside the declared calibrated rate domain")
    placement = dict(document.get("placement", {}))
    changes = scenario.get("placement", {})
    if not isinstance(changes, dict) or set(changes) - set(placement):
        raise ValueError("Cannot change rank count or add unknown ranks")
    for rank, resource in changes.items():
        if not isinstance(resource, str) or resources.get(resource, {}).get("kind") != "cpu":
            raise ValueError("Scenario placement requires declared CPU resources")
        placement[rank] = resource
    events = {event["id"]: event for event in document["events"]}
    degree, children = {}, {key: [] for key in events}
    for key, event in events.items():
        degree[key] = len(event["depends"])
        for dependency in event["depends"]:
            children[dependency].append(key)
    ready = [(0.0, key) for key, count in degree.items() if not count]
    heapq.heapify(ready)
    free, ends, timeline, busy = {}, {}, [], {}
    while ready:
        if cancel is not None and cancel.is_set():
            raise ValueError("Simulation cancelled")
        earliest, key = heapq.heappop(ready)
        event = events[key]
        resource = _resource(event, resources, placement, document.get("routes", {}))
        start = max(earliest, free.get(resource, 0))
        duration = event["work"] / rates.get(resource, resources[resource]["rate"]) + resources[resource].get("latency", 0)
        end = start + duration
        if not math.isfinite(end) or end > 1e100:
            raise ValueError("Simulation duration exceeds numerical bounds")
        free[resource] = ends[key] = end
        busy[resource] = busy.get(resource, 0) + duration
        timeline.append({"id": key, "resource": resource, "start": start, "end": end, "dependency_ready": earliest})
        for child in children[key]:
            degree[child] -= 1
            if not degree[child]:
                heapq.heappush(ready, (max(ends[dep] for dep in events[child]["depends"]), child))
    if len(ends) != len(events):
        raise ValueError("Execution graph contains a dependency cycle")
    return {"seconds": max(ends.values()), "timeline": timeline, "resource_busy_seconds": busy}


def simulate(document, scenario=None, *, cancel=None):
    """Replay a fixed DAG, calibrate a scalar factor, and report held-out residuals."""
    validate(document)
    scenario = {} if scenario is None else scenario
    baseline = _simulate(document, {}, cancel)
    observations = sequence(document.get("observations", []), "observations", 64)
    measured, ratios = [], []
    for observation in observations:
        object_(observation, {"id", "scenario", "actual_seconds", "role"})
        identifier(observation["id"], "observation ID")
        actual = number(observation["actual_seconds"], "actual duration", positive=True)
        if not isinstance(observation["role"], str) or observation["role"] not in {"calibration", "validation"}:
            raise ValueError("Observation role must be calibration or validation")
        modeled = _simulate(document, observation["scenario"], cancel)["seconds"]
        if not modeled:
            raise ValueError("A zero-duration graph cannot calibrate positive observations")
        measured.append((observation, modeled))
        if observation["role"] == "calibration":
            ratios.append(actual / modeled)
    if len({row["id"] for row in observations}) != len(observations):
        raise ValueError("Observation IDs must be unique")
    factor = statistics.median(ratios) if ratios else 1.0
    number(factor, "calibration factor", positive=True)
    candidate = _simulate(document, scenario, cancel)
    threshold = document["domain"].get("max_validation_relative_error")
    residuals = []
    for observation, modeled in measured:
        predicted = number(modeled * factor, "calibrated observation duration")
        relative = number((predicted - observation["actual_seconds"]) / observation["actual_seconds"],
                          "relative validation error", low=-1e100)
        residuals.append({"id": observation["id"], "role": observation["role"], "predicted_seconds": predicted,
                          "actual_seconds": observation["actual_seconds"], "residual_seconds": observation["actual_seconds"] - predicted,
                          "relative_error": relative,
                          "accepted": abs(relative) <= threshold if observation["role"] == "validation" and threshold is not None else None})
    held_out = [abs(row["relative_error"]) for row in residuals if row["role"] == "validation"]
    failures = [row["id"] for row in residuals if row["accepted"] is False]
    if threshold is None:
        state, reason = "unqualified", "No acceptance threshold is declared."
    elif not held_out:
        state, reason = "unqualified", "No separate held-out observations are available."
    elif failures:
        state, reason = "failed", "At least one held-out observation exceeds the declared acceptance threshold; predictions are withdrawn."
    else:
        state, reason = "passed", "All declared held-out observations meet the declared acceptance threshold."
    model_digest = digest(document)
    definition_digest = digest({key: value for key, value in document.items() if key != "observations"})
    calibration_digest = digest([row for row in observations if row["role"] == "calibration"])
    validation_revision = digest([row for row in observations if row["role"] == "validation"])
    candidate_seconds = number(candidate["seconds"] * factor, "calibrated candidate duration")
    candidate_placement = {**document.get("placement", {}), **scenario.get("placement", {})}
    candidate_rates = {key: scenario.get("rates", {}).get(key, row["rate"]) for key, row in document["resources"].items()}
    direct = []
    for row in observations:
        if row["role"] != "validation":
            continue
        placement = {**document.get("placement", {}), **row["scenario"].get("placement", {})}
        rates = {key: row["scenario"].get("rates", {}).get(key, value["rate"]) for key, value in document["resources"].items()}
        if placement == candidate_placement and rates == candidate_rates:
            direct.append(row["id"])
    # Additional held-out evidence revises the qualification of this same
    # prediction. A changed model, fit, policy or scenario starts a new identity.
    prediction_id = digest({"model_definition_digest": definition_digest, "calibration_digest": calibration_digest,
                            "scenario": scenario, "algorithm": "serial-dag-scalar/v1"})
    return {"schema": "raven.execution-prediction/v1", "prediction_id": prediction_id,
            "model_digest": model_digest, "scenario": scenario, "algorithm": "serial-dag-scalar/v1",
            "model_definition_digest": definition_digest, "calibration_digest": calibration_digest,
            "validation_revision": validation_revision,
            "baseline_seconds": number(baseline["seconds"] * factor, "calibrated baseline duration"),
            "predicted_seconds": None if state == "failed" else candidate_seconds,
            "diagnostic_model_seconds": candidate_seconds,
            "prediction_status": "withdrawn" if state == "failed" else "available" if state == "passed" else "unqualified",
            "validation": {"status": state, "reason": reason, "acceptance_threshold": threshold,
                           "held_out_count": len(held_out), "failed_observations": failures},
            "applicability": {"within_declared_domain": True, "fixed_workload": document["workload"],
                              "direct_validation_observations": direct,
                              "basis": "Declared fixed-workload rates and routes only; unmeasured scenarios are not independently validated."},
            "calibration_factor": factor, "calibrated": bool(ratios), "validated": state == "passed",
            "validation_max_relative_error": max(held_out) if held_out else None, "residuals": residuals,
            "timeline": candidate["timeline"], "timeline_units": "uncalibrated model seconds",
            "resource_busy_seconds": candidate["resource_busy_seconds"],
            "limitations": ["Fixed workload, dependencies and rank count; no control-flow or scheduler-wait prediction.",
                            "Each resource is one serial nonpreemptive server; declared routes do not model packet-level contention.",
                            "Scalar calibration does not establish confidence intervals or validity for unmeasured placements.",
                            "Calibration/held-out role labels and independence are supplied by the project; Raven does not certify them."]}
