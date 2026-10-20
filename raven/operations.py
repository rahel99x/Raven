"""Operation composition and compatibility facade above adapter contracts."""

from __future__ import annotations

from functools import lru_cache
import copy
import time as time  # public compatibility for callers that inject a review clock

from .operation_support import (
    Context as Context,
    LocalFiles as LocalFiles,
    PLAN_SCHEMA as PLAN_SCHEMA,
    REPORT_SCHEMA as REPORT_SCHEMA,
    MAX_DOCUMENT as MAX_DOCUMENT,
    PLAN_LIFETIME as PLAN_LIFETIME,
    cancelled as cancelled,
    checkpoint as checkpoint,
    _identity as _identity,
    canonical as canonical,
    digest as digest,
    clean as clean,
    report as report,
    prepare_plan as prepare_plan,
    validate_plan as validate_plan,
    local_only as local_only,
    _process as _process,
    _backend_command as _backend_command,
    command as command,
    read_bytes as read_bytes,
    read_json as read_json,
    atomic_json as atomic_json,
    managed_run as managed_run,
    managed_apply as managed_apply,
)
from .operation_support import parameters_for
from .distribution_profile import absent, missing_operation, unavailable

MODULES = (
    "ops_cluster",
    "ops_science",
    "ops_campaigns",
    "ops_services",
    "ops_scale",
    "ops_queue_order",
    "ops_workspace",
    "ops_investigation",
    "ops_workflow",
    "ops_platform",
    "ops_runtime",
    "ops_trace",
    "ops_evidence_queries",
    "ops_health",
    "ops_process_profile",
    "ops_queue_models",
    "ops_signal_analysis",
    "ops_campaign_policy",
)


def _modules():
    """Finite adapter composition; absent optional distributions stay explicit."""
    modules = []
    if not absent("raven.operation_adapters.cluster"):
        from .operation_adapters import cluster as ops_cluster

        modules.append(ops_cluster)
    if not absent("raven.operation_adapters.science"):
        from .operation_adapters import science as ops_science

        modules.append(ops_science)
    if not absent("raven.operation_adapters.campaigns"):
        from .operation_adapters import campaigns as ops_campaigns

        modules.append(ops_campaigns)
    if not absent("raven.operation_adapters.services"):
        from .operation_adapters import services as ops_services

        modules.append(ops_services)
    if not absent("raven.operation_adapters.scale"):
        from .operation_adapters import scale as ops_scale

        modules.append(ops_scale)
    if not absent("raven.operation_adapters.queue_order"):
        from .operation_adapters import queue_order as ops_queue_order

        modules.append(ops_queue_order)
    if not absent("raven.operation_adapters.workspace"):
        from .operation_adapters import workspace as ops_workspace

        modules.append(ops_workspace)
    if not absent("raven.operation_adapters.investigation"):
        from .operation_adapters import investigation as ops_investigation

        modules.append(ops_investigation)
    if not absent("raven.operation_adapters.workflow"):
        from .operation_adapters import workflow as ops_workflow

        modules.append(ops_workflow)
    if not absent("raven.operation_adapters.platform"):
        from .operation_adapters import platform as ops_platform

        modules.append(ops_platform)
    if not absent("raven.operation_adapters.runtime"):
        from .operation_adapters import runtime as ops_runtime

        modules.append(ops_runtime)
    if not absent("raven.operation_adapters.trace"):
        from .operation_adapters import trace as ops_trace

        modules.append(ops_trace)
    if not absent("raven.operation_adapters.evidence_queries"):
        from .operation_adapters import evidence_queries as ops_evidence_queries

        modules.append(ops_evidence_queries)
    if not absent("raven.operation_adapters.health"):
        from .operation_adapters import health as ops_health

        modules.append(ops_health)
    if not absent("raven.operation_adapters.process_profile"):
        from .operation_adapters import process_profile as ops_process_profile

        modules.append(ops_process_profile)
    if not absent("raven.operation_adapters.queue_models"):
        from .operation_adapters import queue_models as ops_queue_models

        modules.append(ops_queue_models)
    if not absent("raven.operation_adapters.signal_analysis"):
        from .operation_adapters import signal_analysis as ops_signal_analysis

        modules.append(ops_signal_analysis)
    if not absent("raven.operation_adapters.campaign_policy"):
        from .operation_adapters import campaign_policy as ops_campaign_policy

        modules.append(ops_campaign_policy)
    return tuple(modules)


@lru_cache(maxsize=1)
def _registry():
    entries = {}
    for module in _modules():
        for specification in module.SPECIFICATIONS:
            key = specification["key"]
            if key in entries:
                raise ValueError("Duplicate operation: " + key)
            entries[key] = (module, specification)
    return entries


def catalog():
    return [copy.deepcopy(spec) for _, spec in _registry().values()]


def specification(feature):
    try:
        return copy.deepcopy(_registry()[feature][1])
    except KeyError as exc:
        if missing_operation(feature):
            raise ValueError(unavailable(feature)) from exc
        raise ValueError("Unknown operation: " + clean(feature)) from exc


def parameters(feature, supplied):
    return parameters_for(feature, specification(feature), supplied)


def _complete_routed(feature, result, ctx):
    if feature == "campaign-policy" and result.get("status") != "deferred":
        return _registry()[feature][0].controlled_report(result, ctx)
    return result


def run(feature, params, ctx):
    checkpoint(ctx)
    values = parameters(feature, params)
    from .execution.admission import operation_route

    routed = operation_route(feature, values, ctx)
    if routed is not None:
        return _complete_routed(feature, routed, ctx)
    return _registry()[feature][0].run(feature, values, ctx)


def action_services():
    from .action_lifecycle import Services

    return Services(validate_plan, atomic_json, canonical, clean, REPORT_SCHEMA)


def apply(feature, plan, ctx):
    from .action_lifecycle import execute
    from .execution.admission import apply_admission

    routed = apply_admission(feature, plan, ctx)
    callback = routed or getattr(_registry()[feature][0], "apply", None)
    return execute(feature, plan, ctx, callback, services=action_services())
