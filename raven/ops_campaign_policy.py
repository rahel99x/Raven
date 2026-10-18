"""Campaign shadow comparisons and scoped, reviewed local plan controls."""
from __future__ import annotations
import copy
from contextlib import contextmanager

from . import campaign_common as C, campaign_policy as P, operations as O
from .evidence import digest, loads
from . import config_trust, state_ownership

SPECIFICATIONS = [{"key": "campaign-policy", "title": "Campaign submission policy", "group": "Campaigns", "proposal": "B-02",
    "summary": "Compare owned campaign shadow policies; pause, cancel or override the local reviewed plan.",
    "fields": [{"key": "path", "label": "Campaign policy JSON", "required": True},
               {"key": "action", "label": "Action", "choices": ["compare", "pause", "resume", "cancel", "override"], "default": "compare"},
               {"key": "policy", "label": "Override policy", "choices": list(P.POLICIES), "default": "fifo"}]}]


def _state_path(ctx, review_id):
    if not ctx.state_dir:
        raise ValueError("Campaign controls require persistent private state")
    return C.directory(ctx, "policy") / (review_id + ".json")


def _load_control(ctx, review_id):
    initial = {"schema": "raven.campaign-policy-control/v1", "review_id": review_id, "scope": ctx.scope,
               "status": "ready", "policy": None, "revision": 0}
    value = initial
    if ctx.state_dir:
        try:
            raw, _ = config_trust.read(_state_path(ctx, review_id), private=True, limit=64 << 10)
            value = loads(raw)
        except FileNotFoundError:
            pass
    if (not isinstance(value, dict) or set(value) != set(initial) or value["schema"] != initial["schema"] or
            value["review_id"] != review_id or value["scope"] != ctx.scope or value["status"] not in {"ready", "paused", "cancelled"} or
            value["policy"] not in (None, *P.POLICIES) or type(value["revision"]) is not int or not 0 <= value["revision"] <= 10**9):
        raise ValueError("Campaign control state is invalid or belongs to a different scope")
    return value



@contextmanager
def _control_lock(path):
    """Controls require private trusted paths and local flock/replace semantics."""
    kind = state_ownership.filesystem_kind(path.parent)
    if kind not in state_ownership.LOCAL_FILESYSTEMS:
        raise ValueError(f"Campaign controls are read-only on {kind}; choose a local state directory to change them")
    with config_trust.directory_fd(path.parent, create=True, private=True):
        with C.locked(path):
            # Reuse the existing lock lifecycle while checking its owner/mode,
            # including a pre-existing lock left by another installation.
            config_trust.read(str(path) + ".lock", private=True, limit=64 << 10)
            yield


def controlled_report(report, ctx):
    """Apply interface-owned controls after the worker returns its pure analysis."""
    if getattr(ctx, "verified_execution", None):
        return report
    result = report["data"]
    if result["scope"] != ctx.scope:
        raise ValueError("Campaign review belongs to a different connection or owner")
    control = _load_control(ctx, result["review_id"])
    policy = control["policy"] or result["selected_policy"]
    result["selected_policy"] = policy
    result["shadow_decisions"] = result["policy_previews"][policy] if control["status"] == "ready" else []
    result["eligible_nodes"] = [row["id"] for row in result["shadow_decisions"]]
    result["control"] = control
    if control["status"] != "ready":
        result["status"] = control["status"]
    report["rows"] = _lines(result)
    return report


def _lines(result):
    rows = [result["summary"], "Local plan: " + result["status"] + "; selected policy: " + result["selected_policy"],
            "Shadow candidates: " + str(result["shadow_decisions"])]
    if result["blocked_reason"]:
        rows.append(result["blocked_reason"])
    for item in result["comparisons"]:
        rows.append(item["policy"] + ": " + str(item["metrics"]))
        rows.append("  Gains/regressions versus FIFO: " + str(item["deltas_vs_fifo"]))
        rows.append("  Runtime forecast error sensitivity: " + str(item["forecast_error_sensitivity"]))
    return rows + result["limits"]


def run(feature, params, ctx):
    document = O.read_json(ctx, params["path"], limit=8 << 20)
    if params.get("action", "compare") == "compare":
        result = P.analyze(document, cancel=ctx.cancel)
        report = O.report(feature, result["summary"], status="partial", rows=_lines(result), data=result, warnings=result["limits"])
        return controlled_report(report, ctx)
    if ctx.verified_execution:
        raise ValueError("Campaign controls belong to the captured interface state, not a compute task")
    O.local_only(ctx)
    P.validate(document)
    if document["review"]["scope"] != ctx.scope:
        raise ValueError("Campaign review belongs to a different connection or owner")
    review_id = document["review"]["review_id"]
    control = _load_control(ctx, review_id)
    if control["status"] == "cancelled":
        raise ValueError("This local plan is cancelled; prepare a new immutable review to continue")
    payload = {"source_digest": digest(document), "review_id": review_id, "previous": control,
               "action": params["action"], "policy": params.get("policy", "fifo")}
    plan = O.prepare_plan(ctx, feature, params, payload)
    return O.report(feature, "Review local campaign plan control", rows=["Action: " + params["action"],
        "This changes only the local shadow plan. Existing scheduler jobs and execution receipts remain authoritative."], data=payload, plan=plan)


def apply(feature, plan, ctx):
    if ctx.verified_execution:
        raise ValueError("Campaign controls belong to the captured interface state, not a compute task")
    O.local_only(ctx)
    payload = O.validate_plan(plan, ctx, feature)
    document = O.read_json(ctx, plan["params"]["path"], limit=8 << 20)
    P.validate(document)
    if digest(document) != payload["source_digest"] or document["review"]["scope"] != ctx.scope:
        raise ValueError("Campaign source changed after review")
    path = _state_path(ctx, payload["review_id"])
    with _control_lock(path):
        current = _load_control(ctx, payload["review_id"])
        if current != payload["previous"]:
            raise ValueError("Campaign control changed after review")
        updated = copy.deepcopy(current)
        if updated["revision"] >= 10**9:
            raise ValueError("Campaign control revision limit reached; prepare a new immutable review")
        updated["revision"] += 1
        action = payload["action"]
        if action == "override":
            if payload["policy"] not in P.POLICIES:
                raise ValueError("Unsupported policy override")
            updated["policy"] = payload["policy"]
        elif action in {"pause", "resume", "cancel"}:
            updated["status"] = {"pause": "paused", "resume": "ready", "cancel": "cancelled"}[action]
        else:
            raise ValueError("Unsupported campaign control")
        C.atomic(path, updated)
    return O.report(feature, "Local campaign control recorded", rows=[str(updated), "No scheduler actions were executed."], data=updated)


from .operations import managed_run as _managed_run, managed_apply as _managed_apply
run = _managed_run(run)
apply = _managed_apply(apply)
