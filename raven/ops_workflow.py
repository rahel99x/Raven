"""Registry adapter for staged investigation workflows and diagnostic ports."""

from __future__ import annotations

from . import investigation_workflow
from .ops_investigation import _field

SPECIFICATIONS = [
    {
        "key": "investigation-workflow",
        "title": "Staged job investigation",
        "group": "Investigation",
        "proposal": "Consolidation",
        "summary": "Join evidence, findings, missing information, diagnostic planning, reviewed execution and updated measurements.",
        "fields": [
            _field("manifest", "Workflow manifest", required=True),
            _field(
                "action",
                "Stage",
                "inspect",
                choices=["inspect", "review-diagnostic", "status"],
            ),
            _field("diagnostic_id", "Diagnostic ID (review or status)"),
            _field("destination", "Diagnostic workspace (new for review)"),
        ],
    }
]


def run(feature, params, ctx):
    if feature != "investigation-workflow":
        raise ValueError("Unknown staged investigation operation")
    return investigation_workflow.run(params, ctx)


def apply(feature, plan, ctx):
    if feature != "investigation-workflow":
        raise ValueError("Unknown staged investigation operation")
    return investigation_workflow.apply(plan, ctx)
