"""Budgeted next-measurement advice; efficacy remains an experimental question."""

from __future__ import annotations

import copy
from decimal import Decimal

from .. import evidence as E

DEFAULT_CANDIDATES = (
    {
        "id": "scheduler-states",
        "title": "Record explicit scheduling states",
        "targets": ["cpu", "waiting"],
        "estimated_overhead_percent": 2.0,
        "estimated_bytes": 2 << 20,
        "information_value": 3.0,
        "method": "Review an available scheduling-event collector for this exact attempt; source permissions and clock coverage must be qualified.",
    },
    {
        "id": "device-activity",
        "title": "Connect a device activity report",
        "targets": ["device"],
        "estimated_overhead_percent": 3.0,
        "estimated_bytes": 4 << 20,
        "information_value": 3.0,
        "method": "Select and validate a supported GPU activity report or qualified device collector; host calls alone cannot provide device timing.",
    },
    {
        "id": "clock-calibration",
        "title": "Declare verified cross-node clock calibration",
        "targets": ["clocks"],
        "estimated_overhead_percent": 0.1,
        "estimated_bytes": 4096,
        "information_value": 1.0,
        "method": "Provide a measured clock mapping with uncertainty and validity bounds before comparing cross-node endpoint times.",
    },
    {
        "id": "coverage",
        "title": "Inspect recording loss and reader progress",
        "targets": ["coverage"],
        "estimated_overhead_percent": 0.0,
        "estimated_bytes": 0,
        "information_value": 5.0,
        "method": "Inspect retained source errors, quota/loss markers and committed/read sequence counts; narrow the query or allow retained reading to catch up.",
    },
)


def propose(report, *, candidates=None, budget=None):
    """Choose informative declared measurements under explicit additive estimates.

    This deterministic greedy set-cover heuristic is O(CT), bounded to 128
    candidates and five targets. It is not an optimum or measured gain.
    No collector, job, submission, SSH connection, or configuration is changed.
    """
    E.canonical(report)
    if not isinstance(report, dict) or report.get("schema") != "raven.performance-investigation/v1":
        raise ValueError("A performance investigation is required")
    if budget is not None and not isinstance(budget, dict):
        raise ValueError("Diagnosis budget must be an object")
    budget = {"max_overhead_percent": 5.0, "max_bytes": 8 << 20, **(budget or {})}
    E.keys(budget, {"max_overhead_percent", "max_bytes"}, label="diagnosis budget")
    overhead_limit = E.finite(budget["max_overhead_percent"], "overhead budget", 0)
    if overhead_limit > 100:
        raise ValueError("Overhead budget cannot exceed 100 percent")
    E.integer(budget["max_bytes"], "diagnosis byte budget", 1 << 30)
    needed = set()
    facts = []
    for section, target in (
        ("execution", "cpu"),
        ("waiting", "waiting"),
        ("device_activity", "device"),
    ):
        value = report.get(section)
        if not isinstance(value, dict) or type(value.get("available")) is not bool:
            raise ValueError("Investigation availability must be explicit")
        if not value["available"]:
            needed.add(target)
        facts.append(
            {
                "source": section,
                "available": value["available"],
                "reason": value.get("missing_reason"),
            }
        )
    if report.get("partial"):
        needed.add("coverage")
    if len(report.get("clock_envelopes", [])) > 1:
        needed.add("clocks")
    candidates = copy.deepcopy(list(DEFAULT_CANDIDATES) if candidates is None else candidates)
    if not isinstance(candidates, list) or len(candidates) > 128:
        raise ValueError("Choose at most 128 declared measurement candidates")
    available, seen = [], set()
    for item in candidates:
        E.keys(
            item,
            {
                "id",
                "title",
                "targets",
                "estimated_overhead_percent",
                "estimated_bytes",
                "information_value",
                "method",
            },
            label="measurement candidate",
        )
        for name in ("id", "title", "method"):
            E.text(item[name], name, 2048)
        if item["id"] in seen:
            raise ValueError("Duplicate measurement candidate identity")
        seen.add(item["id"])
        targets = item["targets"]
        if (
            not isinstance(targets, list)
            or len(targets) > 8
            or any(not isinstance(value, str) for value in targets)
            or len(targets) != len(set(targets))
            or set(targets) - {"cpu", "waiting", "device", "clocks", "coverage"}
        ):
            raise ValueError("Measurement targets must name known evidence gaps")
        E.finite(item["estimated_overhead_percent"], "candidate overhead", 0)
        E.integer(item["estimated_bytes"], "candidate bytes", 1 << 30)
        E.finite(item["information_value"], "candidate information value", 0)
        available.append(item)
    # Decimal preserves an exact user-declared budget at floating boundaries.
    remaining = Decimal(str(overhead_limit))
    bytes_left = budget["max_bytes"]
    missing = set(needed)
    selected = []
    while available and missing:
        eligible = [
            item
            for item in available
            if missing.intersection(item["targets"])
            and Decimal(str(item["estimated_overhead_percent"])) <= remaining
            and item["estimated_bytes"] <= bytes_left
        ]
        if not eligible:
            break

        def score(item):
            cost = 1 + item["estimated_overhead_percent"] + item["estimated_bytes"] / (1 << 20)
            return (
                -len(missing.intersection(item["targets"])) * item["information_value"] / cost,
                item["id"],
            )

        chosen = min(eligible, key=score)
        available.remove(chosen)
        remaining -= Decimal(str(chosen["estimated_overhead_percent"]))
        bytes_left -= chosen["estimated_bytes"]
        missing.difference_update(chosen["targets"])
        selected.append({**chosen, "review_required": True, "execution": "not-run"})
    return {
        "schema": "raven.adaptive-diagnosis/v1",
        "identity": copy.deepcopy(report.get("identity", {})),
        "status": "experimental advice; no efficacy or deployment qualification",
        "facts": facts,
        "evidence_digest": report.get("evidence_digest"),
        "hypotheses": [
            {
                "target": key,
                "claim": "A missing measurement may distinguish competing explanations; no bottleneck is established.",
            }
            for key in sorted(needed)
        ],
        "proposed_measurements": selected,
        "unresolved_targets": sorted(missing),
        "budget": budget,
        "estimated_cost": {
            "overhead_percent": float(Decimal(str(overhead_limit)) - remaining),
            "bytes": budget["max_bytes"] - bytes_left,
        },
        "limitations": [
            "Costs and information values are declared estimates, not measurements.",
            "The additive heuristic is conservative only if supplied estimates bound actual combined overhead; interaction costs remain unqualified.",
            "Missing CPU/GPU evidence does not establish poor efficiency, waiting, or a need to collect every source.",
        ],
    }
