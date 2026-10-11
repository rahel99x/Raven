"""Shared task labels and commands for the terminal, help and automation.

Descriptors contain data only. Opening a task never starts a collector or an
external action; execution still goes through the operation review contract.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Task:
    key: str
    title: str
    description: str
    operations: tuple[str, ...]


TASKS = (
    Task("run", "Run", "Prepare, inspect and control an exact execution attempt.",
         ("pending-edit", "array-throttle", "allocation-shell", "placement", "batch-script", "trace-capture", "runtime-steering", "io-coordination", "diagnostic-recipe")),
    Task("investigate", "Investigate", "Follow a failure or delay to its supporting evidence.",
         ("trace-inspect", "trace-snapshot", "causal-delay", "collective-audit", "bottlenecks", "incidents", "evidence", "investigation-timeline", "recommendation", "investigation-plan", "minimize-reproducer")),
    Task("compare", "Compare", "Test a performance or scientific hypothesis with a bounded experiment.",
         ("statistics", "energy", "numerical-stability", "regression-investigation", "regression-sweep", "diagnostic-campaign", "execution-simulation", "scientific-field", "information-query")),
    Task("results", "Results", "Inspect outputs, acceptance, lineage and justified reuse.",
         ("acceptance", "reuse", "checkpoint", "staging", "scientific-impact", "research-portable", "environment", "workflow-engine")),
    Task("connect", "Connect", "Connect a project or qualify an optional site integration.",
         ("project-guide", "site-policy", "site-telemetry", "adapter", "qualification", "support-bundle", "clusters", "slurm-doctor", "storage", "supervisor")),
)


def task(key):
    return next((item for item in TASKS if item.key == key), None)


def operation_keys(key):
    value = task(key)
    return value.operations if value else ()


def command_rows():
    """The same descriptors are used by toolbar buttons and generated guides."""
    return [{"key": item.key, "label": item.title, "command": "workbench " + item.key,
             "description": item.description, "operations": list(item.operations)} for item in TASKS]
