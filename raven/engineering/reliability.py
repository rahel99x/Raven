"""Measured reliability budgets; absence of measurements never becomes a pass."""

from __future__ import annotations

import math

SCHEMA = "raven.engineering-reliability/v1"
DEFAULTS = {
    "startup_seconds": 2.0,
    "input_p95_ms": 50.0,
    "render_p95_ms": 25.0,
    "process_rss_mib": 256.0,
    "python_peak_mib": 128.0,
    "scheduler_requests_per_minute": 60.0,
    "reconnect_seconds": 10.0,
    "recording_freshness_seconds": 2.0,
}


def evaluate(document):
    """Evaluate comparable observed values, retaining units and source coverage.

    This service consumes measurements, never a simulated performance claim.
    Defaults are acceptance targets, not assertions about a particular site.
    """
    if not isinstance(document, dict) or set(document) - {
        "schema",
        "measurements",
        "budgets",
        "environment",
        "revision",
    }:
        raise ValueError("Use measurements, budgets, environment and revision fields")
    if document.get("schema", SCHEMA) != SCHEMA:
        raise ValueError("Expected " + SCHEMA)
    values, overrides = document.get("measurements", {}), document.get("budgets", {})
    if not isinstance(values, dict) or not isinstance(overrides, dict):
        raise ValueError("Measurements and budgets must be objects")
    if (values.keys() | overrides.keys()) - DEFAULTS.keys():
        raise ValueError(
            "Unknown reliability metric; units must be explicit in its name"
        )
    budgets = {**DEFAULTS, **overrides}
    for key, value in budgets.items():
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError("Reliability budget must be finite and positive: " + key)
    rows = []
    for key, budget in budgets.items():
        measurement = values.get(key)
        if measurement is None:
            rows.append(
                dict(metric=key, budget=budget, value=None, status="unmeasured")
            )
            continue
        if not isinstance(measurement, dict) or set(measurement) != {
            "value",
            "source",
            "samples",
        }:
            raise ValueError("Every measurement needs value, source and samples")
        value, count, source = (
            measurement["value"],
            measurement["samples"],
            measurement["source"],
        )
        if (
            type(value) not in (int, float)
            or not math.isfinite(value)
            or value < 0
            or type(count) is not int
            or not 1 <= count <= 10**9
            or not isinstance(source, str)
            or not 0 < len(source) <= 4096
        ):
            raise ValueError(
                "Use finite observations, a real source and a positive sample count"
            )
        rows.append(
            dict(
                metric=key,
                budget=budget,
                value=value,
                source=source,
                samples=count,
                status="pass" if value <= budget else "regression",
            )
        )
    statuses = {row["status"] for row in rows}
    return dict(
        schema=SCHEMA,
        revision=document.get("revision", "unrecorded"),
        environment=document.get("environment", {}),
        metrics=rows,
        status="regression"
        if "regression" in statuses
        else "incomplete"
        if "unmeasured" in statuses
        else "pass",
        limits=[
            "Targets are configurable release criteria, not live site qualification.",
            "Process RSS and Python allocations measure different things and are reported separately.",
        ],
    )
