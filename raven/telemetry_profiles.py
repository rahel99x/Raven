"""Finite Heavy collection presets shared by configuration and live controls.

Intervals describe reader demand, not the sampling rate of another program's
trace writer. Scheduler accounting and site restrictions remain authoritative.
"""
from __future__ import annotations

import math

PRESETS = ("default", "medium", "high", "ultra", "custom")
_ROWS = (
    ("queue_seconds", "Jobs and queue", "s", (5., 2., 1., .5), .1, 3600., "Scheduler queue and completion detection."),
    ("starts_seconds", "Start estimates", "s", (15., 10., 5., 2.), .5, 3600., "Predicted start times for pending jobs."),
    ("history_seconds", "History and recents", "s", (30., 15., 10., 5.), 1., 86400., "Accounting refresh; Slurm may publish completed records later."),
    ("inventory_seconds", "Nodes and partitions", "s", (60., 30., 20., 15.), 1., 86400., "Cluster inventory and capacity."),
    ("details_seconds", "Job details", "s", (10., 5., 3., 2.), .5, 3600., "Selected job and completed-step details."),
    ("live_seconds", "CPU and memory", "s", (2., 1., .75, .5), .5, 3600., "Selected live job statistics; Slurm's accounting frequency limits new samples."),
    ("account_seconds", "Accounts and fair share", "s", (300., 180., 120., 60.), 5., 86400., "Account observations and fair-share information."),
    ("log_follow_seconds", "Log following", "s", (1., .75, .5, .5), .5, 3600., "Read appended job logs while following them."),
    ("research_seconds", "Research metrics", "s", (1., .75, .5, .5), .5, 3600., "Read application metrics and Research evidence."),
    ("trace_seconds", "GPU and retained traces", "s", (1., .75, .5, .5), .5, 3600., "Read existing GPU/CPU/memory traces; does not change their producer."),
    ("controller_requests_per_minute", "Controller requests", "/min", (180, 240, 360, 600), 1, 3600, "Shared rolling budget for scheduler controller reads."),
    ("controller_burst", "Controller burst", "requests/s", (4, 6, 8, 12), 1, 64, "Short-window controller allowance; must accommodate the selected queue rate."),
    ("accounting_requests_per_minute", "Accounting requests", "/min", (12, 24, 36, 60), 1, 600, "Shared rolling budget for history and accounting reads."),
    ("statistics_requests_per_minute", "Live statistics requests", "/min", (60, 90, 120, 180), 1, 1200, "Shared rolling budget for live job statistics."),
    ("filesystem_metadata_per_minute", "File metadata reads", "/min", (240, 480, 720, 1200), 1, 10000, "Shared rolling budget for trace and log metadata reads."),
    ("file_bytes_per_minute", "File read bytes", "bytes/min", (16777216, 33554432, 50331648, 67108864), 1, 1073741824, "Shared rolling byte budget for existing evidence and logs."),
    ("local_processes_per_minute", "Collection processes", "/min", (300, 420, 600, 900), 1, 10000, "Shared rolling budget for collection subprocesses."),
)
FIELDS = tuple(dict(key=key, label=label, unit=unit, default=values[0], ultra=values[3],
                    min=low, max=high, type="int" if type(values[0]) is int else "float",
                    description=description, category="Budgets" if unit != "s" else "Intervals")
               for key, label, unit, values, low, high, description in _ROWS)
VALUES = {name: {row[0]: row[3][index] for row in _ROWS}
          for index, name in enumerate(PRESETS[:-1])}
DEFAULTS = {"preset": "default", "custom": dict(VALUES["default"])}
BY_KEY = {field["key"]: field for field in FIELDS}


def validate_custom(values, *, partial=False):
    if not isinstance(values, dict) or set(values) - BY_KEY.keys():
        raise ValueError("Custom telemetry settings must contain documented fields only.")
    if not partial and set(values) != BY_KEY.keys():
        raise ValueError("Custom telemetry settings need a value for every field.")
    result = {}
    for key, value in values.items():
        field = BY_KEY[key]
        if (type(value) not in (int, float) or not field["min"] <= value <= field["max"]
                or not math.isfinite(value) or field["type"] == "int" and type(value) is not int):
            raise ValueError(f"{field['label']} must be a finite {field['type']} in {field['min']:g}..{field['max']:g} {field['unit']}.")
        result[key] = value
    return result


def value_fraction(key, value):
    """A bounded log-scale intensity: shorter waits or larger budgets are pink."""
    field = BY_KEY[key]
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        return 0.
    default, ultra = field["default"], field["ultra"]
    return max(0., min(1., math.log(value / default) / math.log(ultra / default)))
