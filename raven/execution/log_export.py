"""Bounded reviewed log export admission, below worker adapter composition."""

from __future__ import annotations
import hashlib
from .policy import _json
from .admission import apply_admission


def large_log_export(router, payload, cancel=None):
    from .. import operation_support as O

    blocked = router.require("log-export", worker_operation="artifact-publish")
    if blocked is not None:
        return {"status": "deferred", "message": blocked["summary"], "export_path": ""}
    worker = router.worker()
    identity = hashlib.sha256(
        _json(worker.identity, 64 << 10, "Worker identity")
    ).hexdigest()
    ctx = O.Context(
        scope={"artifact_target": identity},
        execution=router,
        cancel=cancel,
        state_dir=str(payload.get("state_dir") or ""),
    )
    plan = O.prepare_plan(ctx, "log-export", {}, payload)
    plan["raven_worker"] = identity
    plan["digest"] = O.digest(
        {key: value for key, value in plan.items() if key != "digest"}
    )
    callback = apply_admission("log-export", plan, ctx)
    from ..action_lifecycle import execute

    report = execute("log-export", plan, ctx, callback, services=O.action_services())
    return report["data"]
