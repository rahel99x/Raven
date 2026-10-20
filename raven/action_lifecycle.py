"""One dispatch and receipt lifecycle for reviewed operations.

The operation plan and adapter contracts remain in ``operations``. This module
owns admission and durable outcomes, not scheduler-specific validation. A
receipt saying ``finished`` records that the adapter returned; its nested
report still distinguishes accepted, partial, uncertain and completed work.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Callable
from pathlib import Path
import time


@dataclass(frozen=True)
class Services:
    validate_plan: Callable
    atomic_json: Callable
    canonical: Callable
    clean: Callable
    REPORT_SCHEMA: str


def execute(feature, plan, ctx, callback, *, services):
    """Capture a review, claim it once, revalidate, then publish its outcome.

    Uses the existing worker lane of its caller; no executor or retry loop is
    created here. Cancellation after an adapter returns does not discard the
    outcome of a mutation that might already have succeeded.
    """
    O = services

    # The UI/editor must not be able to retarget a queued action by changing a
    # dict between validation, durable admission and dispatch.
    captured = copy.deepcopy(plan)
    O.validate_plan(captured, ctx, feature)
    if callback is None:
        raise ValueError("This operation is read-only.")
    scope = copy.deepcopy(ctx.scope)
    receipt = None
    if ctx.state_dir:
        from .services_io import private_directory
        directory = private_directory(Path(ctx.state_dir) / "operations-receipts")
        receipt = directory / (captured["digest"] + ".result.json")
        try:
            O.atomic_json(directory / (captured["digest"] + ".intent.json"), {
                "schema": "raven.operation-receipt/v1", "state": "started",
                "feature": feature, "scope": scope, "plan_digest": captured["digest"],
                "started": time.time(), "result_path": str(receipt)})
        except FileExistsError as exc:
            raise ValueError("This plan was already attempted. Read its receipt and inspect current state before retrying.") from exc
    try:
        # Disk admission can block. Expiry, cancellation, connection and job
        # identity must still be valid at dispatch, not just before the write.
        O.validate_plan(captured, ctx, feature)
        value = callback(feature, copy.deepcopy(captured), ctx)
        if not isinstance(value, dict) or value.get("schema") != O.REPORT_SCHEMA or value.get("feature") != feature:
            raise ValueError("The adapter did not return a valid action receipt; inspect current state.")
        O.canonical(value)
    except BaseException as exc:
        if receipt is not None:
            try:
                O.atomic_json(receipt, {
                    "schema": "raven.operation-receipt/v1", "state": "error",
                    "feature": feature, "scope": scope, "plan_digest": captured["digest"],
                    "finished": time.time(), "error": O.clean(exc),
                    "reconciliation": "The action may have partially completed. Inspect current state before a new review."})
            except (OSError, ValueError):
                pass  # The durable intent still identifies the unresolved attempt.
        raise
    if receipt is not None:
        try:
            O.atomic_json(receipt, {
                "schema": "raven.operation-receipt/v1", "state": "finished",
                "feature": feature, "scope": scope, "plan_digest": captured["digest"],
                "finished": time.time(), "result": value})
            value["receipt_path"] = str(receipt)
        except (OSError, ValueError) as exc:
            value.setdefault("warnings", []).append("Action result could not be saved: " + O.clean(exc)
                + ". Its durable intent remains; copy this report before closing.")
    else:
        value.setdefault("warnings", []).append("State storage is disabled; copy this action report before closing.")
    return value
