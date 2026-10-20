"""Read-only queue model discovery and experimental scenario receipts."""
from __future__ import annotations

import re

from . import operations as O
from .evidence import digest
from .slurm_simulator import simulate

SPECIFICATIONS = [
    {"key": "queue-model", "title": "Observable queue model", "group": "Planning", "proposal": "B-01",
     "summary": "Inspect permitted scheduler configuration or simulate an explicitly bounded queue-policy subset.",
     "fields": [{"key": "action", "label": "Action", "default": "discover", "choices": ["discover", "simulate"]},
                {"key": "path", "label": "Model JSON (simulate)"}]},
]


def discover(ctx):
    if ctx.replay:
        raise ValueError("Configuration discovery requires a live scheduler; use a recorded explicit model for replay")
    if ctx.verified_execution:
        raise ValueError("Configuration discovery belongs to the captured scheduler connection, not a compute task")
    observations, errors, raw_hashes = {}, {}, {}
    commands = {"config": ["scontrol", "show", "config"], "nodes": ["scontrol", "show", "nodes", "-o"],
                "partitions": ["scontrol", "show", "partitions", "-o"],
                "reservations": ["scontrol", "show", "reservations", "-o"]}
    for source, argv in commands.items():
        O.checkpoint(ctx)
        try:
            raw = O.command(ctx, argv, timeout=4, limit=1 << 20)
            raw_hashes[source] = digest({"argv": argv, "stdout": raw})
            if source == "config":
                keys = {"ClusterName", "SchedulerType", "SchedulerParameters", "PriorityType", "PriorityDecayHalfLife", "PriorityWeightFairShare", "SelectType", "SelectTypeParameters", "PreemptType"}
                values = {}
                for line in raw.splitlines():
                    match = re.fullmatch(r"\s*([A-Za-z][A-Za-z0-9_]*)\s*=\s*(.*?)\s*", line)
                    if match and match[1] in keys:
                        if match[1] in values:
                            raise ValueError("Ambiguous duplicated scheduler configuration key")
                        values[match[1]] = match[2]
                observations[source] = values
            else:
                from .ops_cluster import parse_records
                identity = {"nodes": "NodeName", "partitions": "PartitionName", "reservations": "ReservationName"}[source]
                rows = parse_records(raw, identity)
                if len(rows) > 256:
                    raise ValueError("Configuration discovery exceeds the 256-record inspection limit")
                observations[source] = rows
        except Exception as exc:
            # Partial discovery never implies an absent policy. Cancellation
            # still exits through the existing operation checkpoint boundary.
            O.checkpoint(ctx)
            errors[source] = O.clean(exc)
    return {"schema": "raven.queue-discovery/v1", "observations": observations, "errors": errors,
            "source_digests": raw_hashes, "commands": commands,
            "unknown": ["Other users' hidden demand and future submissions", "Account associations, historical usage and hierarchical fairshare", "QOS/account GrpTRES and MaxSubmitJobs unless supplied separately", "Unreported reservations, licenses, topology and site plugins"],
            "model_ready": False,
            "summary": "Observed configuration is evidence for a reviewed model, not automatic certification of supported scheduling semantics."}


def run(feature, params, ctx):
    if feature != "queue-model":
        raise ValueError("Unknown queue model operation")
    if params.get("action", "discover") == "discover":
        result = discover(ctx)
        rows = [result["summary"], *[source + ": " + str(value) for source, value in result["observations"].items()],
                *[source + " unavailable: " + error for source, error in result["errors"].items()]]
        return O.report(feature, "Observed scheduler configuration", status="partial", rows=rows, data=result, warnings=result["unknown"])
    result = simulate(O.read_json(ctx, params.get("path", ""), limit=8 << 20), cancel=ctx.cancel)
    rows = [result["summary"], f"Jobs started: {len(result['timeline'])}; blocked: {len(result['blocked'])}; completion coverage: {result['completion_coverage']:.1%}",
            f"Model makespan: {result['makespan_seconds']:.3f} s; placement operations: {result['placement_operations']}",
            f"Runtime factor: {result['parameters']['runtime_factor']:.3f}; calibration attempts: {result['parameters']['calibration_samples']}",
            "Hidden-demand sensitivity: " + str(result["hidden_state_sensitivity"])]
    rows += [f"{item['trace_id']} ({item['origin']}): {item['samples']} held-out starts; MAE={item['start_mae_seconds']} s; coverage={item['coverage']}; ranking={item['ranking_accuracy']}" for item in result["validation"]]
    rows += [f"{item['id']}: start {item['start']:.3f}; end {item['end']:.3f}; wait {item['wait_seconds']:.3f} s; nodes {','.join(item['nodes'])}" for item in result["timeline"][:200]]
    if len(result["timeline"]) > 200:
        rows.append("First 200 starts displayed; the bounded result receipt contains every modeled job.")
    rows += [key + ": " + value for key, value in result["blocked"].items()]
    return O.report(feature, "Experimental queue scenario", status="partial", rows=rows, data=result, warnings=result["limits"])
