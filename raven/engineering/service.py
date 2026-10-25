"""Finite service dispatch shared by installed commands and reviewed UI forms.

Only this compiled registry selects Python functions. Input documents cannot
load plugins or turn descriptive extensions into executable configuration.
"""

from __future__ import annotations

import inspect

from .. import science_files as SF
from ..operation_json import canonical

from ._json import MAX_REQUEST, request as request

MAX_RESULT = 4 << 20
SERVICES = {
    "experiments": ("create", "connect", "link", "load", "summarize"),
    "capsules": ("capture", "verify", "compare", "relaunch_draft"),
    "labs": (
        "load",
        "save",
        "defaults",
        "defaults_for_workspace",
        "inventory_from_workspace",
    ),
    "qualification": ("prepare", "load", "collect"),
    "provenance": ("build", "write", "load", "compare", "from_export"),
    "performance": ("analyze", "read_recordings"),
    "incidents": ("capture", "load"),
    "diagnosis": ("propose",),
    "optimization": (
        "prepare",
        "analyze",
        "apply",
        "status",
        "reconcile",
        "review_cancel",
        "cancel",
    ),
    "recovery": ("prepare", "apply", "status", "workflow"),
    "reliability": ("evaluate",),
}
# These actions publish state or submit/cancel work. The UI always reviews them;
# CLI users explicitly choose the verb and native mutations additionally need
# the native review digest. No mutation is invoked by a read or a render.
WRITES = frozenset(
    {
        ("experiments", "create"),
        ("experiments", "connect"),
        ("experiments", "link"),
        ("capsules", "capture"),
        ("labs", "save"),
        ("qualification", "prepare"),
        ("provenance", "write"),
        ("incidents", "capture"),
        ("optimization", "apply"),
        ("optimization", "cancel"),
        ("optimization", "reconcile"),
        ("recovery", "prepare"),
        ("recovery", "apply"),
    }
)
COMPUTE_ACTIONS = frozenset(
    {
        ("performance", "analyze"),
        ("performance", "read_recordings"),
        ("optimization", "analyze"),
    }
)


def result_status(value):
    """Preserve incomplete/error outcomes in CLI exits and UI feedback."""
    if not isinstance(value, dict):
        return "ok"
    if "execution_receipt" in value and isinstance(value.get("result"), dict):
        value = value["result"]
    statuses = {str(value.get(key, "")).lower() for key in ("status", "state")}
    cancellation = value.get("cancellation")
    if isinstance(cancellation, dict):
        statuses.update(
            str(cancellation.get(key, "")).lower() for key in ("status", "state")
        )
    if (
        statuses.intersection({"regression", "failed", "error", "denied"})
        or value.get("compatible") is False
    ):
        return "error"
    if "deferred" in statuses:
        return "deferred"
    if (
        statuses.intersection(
            {"unknown", "incomplete", "partial", "unverified", "unqualified"}
        )
        or value.get("partial") is True
    ):
        return "partial"
    return "ok"


def callback(domain, action):
    """Finite literal imports make extension ownership auditable statically."""
    if domain not in SERVICES or action not in SERVICES[domain]:
        raise ValueError(
            "Unknown engineering service or action; use raven engineering catalog"
        )
    if domain == "experiments":
        from . import experiments as module
    elif domain == "capsules":
        from . import capsules as module
    elif domain == "labs":
        from . import labs as module
    elif domain == "qualification":
        from . import qualification as module
    elif domain == "provenance":
        from . import provenance as module
    elif domain == "performance":
        from . import performance as module
    elif domain == "incidents":
        from . import incidents as module
    elif domain == "diagnosis":
        from . import diagnosis as module
    elif domain == "optimization":
        from . import optimization as module
    elif domain == "recovery":
        from . import recovery as module
    else:
        from . import reliability as module
    return getattr(module, action)


def describe():
    return {
        "schema": "raven.engineering-services/v1",
        "services": {name: list(actions) for name, actions in SERVICES.items()},
        "limits": {"request_bytes": MAX_REQUEST, "result_bytes": MAX_RESULT},
        "qualification": "Native format tests are distinct from live site qualification.",
    }


def execute(domain, action, arguments, *, dependencies=None, cancel=None):
    if domain not in SERVICES or action not in SERVICES[domain]:
        raise ValueError(
            "Unknown engineering service or action; use raven engineering catalog"
        )
    if not isinstance(arguments, dict) or len(canonical(arguments)) > MAX_REQUEST:
        raise ValueError("Use a bounded engineering arguments object")
    reserved = {"cancel"}
    if domain in {"qualification", "optimization", "recovery"}:
        reserved.add("scheduler")
    if any(
        not isinstance(key, str) or key.startswith("_") for key in arguments
    ) or reserved.intersection(arguments):
        raise ValueError(
            "Runtime dependency and private arguments cannot be supplied by JSON; bind a scheduler connection with an arguments envelope"
        )
    SF.cancelled(cancel)
    function = callback(domain, action)
    signature = inspect.signature(function)
    supplied = dict(arguments)
    for key, value in (dependencies or {}).items():
        if key in signature.parameters:
            if key in supplied:
                raise ValueError(
                    "Runtime dependency cannot be supplied by JSON: " + key
                )
            supplied[key] = value
    if "cancel" in signature.parameters:
        supplied["cancel"] = cancel
    try:
        signature.bind(**supplied)
    except TypeError as exc:
        raise ValueError(
            "Invalid " + domain + " " + action + " arguments: " + str(exc)
        ) from exc
    value = function(**supplied)
    SF.cancelled(cancel)
    if len(canonical(value)) > MAX_RESULT:
        raise ValueError(
            "Engineering result exceeds its bounded response; narrow the request"
        )
    return value
