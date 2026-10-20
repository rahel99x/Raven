"""Policy-only operation admission through the declared worker client port."""

import hashlib
from .execution_policy import (
    router_for,
    deferred,
    context_data,
    AdmissionError,
    LIGHT_OPERATIONS,
    FILE_OPERATIONS,
    PUBLISH_OPERATIONS,
    _PURE,
    _json,
)


def operation_route(feature, params, ctx):
    """Return None only for admitted Light/legacy handlers; never heavy fallback."""
    router = router_for(ctx)
    if not router.managed:
        return None
    if feature == "campaign-policy" and params.get("action", "compare") != "compare":
        return None  # Bounded interface-owned plan controls, never job submission.
    if (
        feature == "clusters"
        or (feature == "supervisor" and params.get("action", "status") == "start")
        or (feature == "site-telemetry" and params.get("action", "inspect") == "query")
    ):
        return deferred(
            feature,
            "This live collector needs a governed source adapter; a Heavy worker does not grant another polling entitlement.",
            supported=False,
        )
    if feature in LIGHT_OPERATIONS:
        return None
    if feature == "queue-model" and params.get("action", "discover") == "discover":
        return None  # Explicit governed reads on the captured scheduler connection.
    trace = feature in {"trace-inspect", "causal-delay", "collective-audit"}
    simulation_review = feature == "execution-simulation" and bool(
        params.get("destination")
    )
    operation = (
        "trace-file"
        if trace
        else "simulation-review"
        if simulation_review
        else "scientific-inspection"
        if feature in _PURE
        else feature
    )
    blocked = router.require(feature, worker_operation=operation)
    if blocked is not None:
        return blocked
    if not (
        trace or feature in FILE_OPERATIONS or simulation_review or feature in _PURE
    ):
        return deferred(
            feature,
            "This operation has no artifact inspection adapter",
            supported=False,
        )
    if getattr(ctx.files, "remote", False):
        return deferred(
            feature,
            "Artifact paths need an explicit worker-visible mapping; remote paths are not replaced by local files.",
            supported=False,
        )
    inputs = (
        {"operation": feature, "params": params}
        if trace or operation == "scientific-inspection"
        else {"params": params}
    )
    receipt = router.dispatch(operation, inputs, context_data(ctx), cancel=ctx.cancel)
    if receipt.get("status") == "deferred":
        return receipt
    result = receipt["result"]
    result["execution_receipt"] = {
        key: value for key, value in receipt.items() if key != "result"
    }
    if result.get("plan"):
        from .operation_json import digest

        plan = result["plan"]
        worker = router.worker()
        if worker is None:
            raise AdmissionError(
                "Compute became unavailable before the review was bound; inspect again"
            )
        plan["raven_worker"] = hashlib.sha256(
            _json(worker.identity, 64 << 10, "Worker identity")
        ).hexdigest()
        plan["digest"] = digest(
            {key: value for key, value in plan.items() if key != "digest"}
        )
    return result


def apply_admission(feature, plan, ctx):
    router = router_for(ctx)
    if (
        feature == "campaign-policy"
        and isinstance(plan, dict)
        and plan.get("payload", {}).get("action")
        in {"pause", "resume", "cancel", "override"}
    ):
        return None  # The normal reviewed action lifecycle persists local controls.
    if (
        router.managed
        and feature == "supervisor"
        and isinstance(plan, dict)
        and plan.get("payload", {}).get("action") == "start"
    ):
        raise AdmissionError(
            "A persistent collector needs its own governed source adapter before it can start"
        )
    if not router.managed or feature in LIGHT_OPERATIONS:
        return None
    if feature in PUBLISH_OPERATIONS or feature in {
        "evidence-query",
        "allocation-health",
        "wasi-analysis",
        "process-profile",
    }:
        mutation_operation = (
            "process-profile-capture"
            if feature == "process-profile"
            else "wasi-analysis-execute"
            if feature == "wasi-analysis"
            else "allocation-health-probe"
            if feature == "allocation-health"
            else "evidence-index-update"
            if feature == "evidence-query"
            else "artifact-publish"
        )
        blocked = router.require(feature, worker_operation=mutation_operation)
        if blocked is not None:
            raise AdmissionError(blocked["summary"])
        identity = hashlib.sha256(
            _json(router.worker().identity, 64 << 10, "Worker identity")
        ).hexdigest()
        if not isinstance(plan, dict) or plan.get("raven_worker") != identity:
            raise AdmissionError(
                "The reviewed execution target changed; inspect again before publishing"
            )

        def publish(operation, captured, captured_ctx):
            identity_context = context_data(captured_ctx)
            identity_context["reviewed_plan_digest"] = captured["digest"]
            receipt = router.dispatch(
                mutation_operation,
                {"operation": operation, "plan": captured},
                identity_context,
                cancel=captured_ctx.cancel,
            )
            if receipt.get("status") == "deferred":
                raise AdmissionError(receipt["summary"])
            result = receipt["result"]
            result["execution_receipt"] = {
                key: value for key, value in receipt.items() if key != "result"
            }
            return result

        return publish
    raise AdmissionError(
        "This Heavy mutation needs a reviewed target adapter; no local execution fallback is permitted"
    )
