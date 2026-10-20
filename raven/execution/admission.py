"""Policy-only operation admission through the declared worker client port."""

import hashlib
from .policy import (
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
        from ..operation_json import digest

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


from contextlib import contextmanager
import os
import time
from .policy import source_limit, source_read, _SOURCE_BUDGET


def cli_admission(argv, router=None):
    """Guard early legacy commands before their recorder/driver is imported."""
    router = router or router_for()
    if not router.managed or not argv:
        return None
    # A child's --help after argparse.REMAINDER is not help for Raven.
    # Accept only the unambiguous parser-only forms, before any payload.
    if len(argv) <= 3 and argv[-1] in {"--help", "-h"} and "--" not in argv:
        return None
    if list(argv) in (
        ["trace", "native", "--sdk-header"],
        ["trace", "mpi", "--source"],
    ):
        return None
    if needs_job_local(argv):
        return job_local_admission(argv, router)
    if argv[0] == "trace" and argv[1:2] not in (
        ["register"],
        ["inspect"],
        ["export"],
        ["summary"],
        ["capabilities"],
        ["cleanup-runtime"],
    ):
        return deferred(
            "trace-cli",
            "Use the admitted Trace operation or an explicitly verified job-local instrumentation launcher; this CLI cannot execute on the interface host.",
            supported=False,
        )
    if argv[0] in {"diagnostic", "diagnostic-trials"}:
        return deferred(
            "diagnostic-campaign",
            "Review this diagnostic against its explicit allocation or desktop worker; interface-host execution is disabled.",
            supported=False,
        )
    if argv[0] == "report" and argv[1:2] in (
        ["exec"],
        ["submit"],
        ["passport"],
        ["aggregate"],
    ):
        return deferred(
            "report-" + argv[1],
            "This reporting driver needs an explicit governed execution target; bounded report metadata commands remain available.",
            supported=False,
        )
    return None


def needs_job_local(argv):
    """Whether a legacy explicit launcher needs a governed owner before dispatch.

    Reporting submission obtains the owner but does not require compute. Child
    arguments after ``--`` never become parser help or change this decision.
    """
    if not isinstance(argv, (list, tuple)) or len(argv) < 2:
        return False
    if len(argv) <= 3 and argv[-1] in {"--help", "-h"} and "--" not in argv:
        return False
    if list(argv) in (
        ["trace", "native", "--sdk-header"],
        ["trace", "mpi", "--source"],
    ):
        return False
    return (
        argv[0] == "report"
        and argv[1] in {"exec", "submit", "passport", "aggregate"}
        or argv[0] == "trace"
        and argv[1] in {"run", "native"}
    )


def job_local_admission(argv, router=None):
    router = router or router_for()
    if not router.managed:
        return None
    command = ":".join(argv[:2])
    policy, backend = router.policy, router.source_backend
    try:
        if policy.get("connection", {}).get("transport") != "local":
            raise AdmissionError(
                "Job-local drivers require the actual local Slurm connection; SSH/frontend paths are not substituted"
            )
        if backend is None or getattr(backend, "ledger", None) is None:
            raise AdmissionError(
                "No governed scheduler source is available for this explicit driver"
            )
        capabilities = policy.get("execution", {}).get("capabilities", ())
        if command == "report:submit":
            if "scheduler-action" not in capabilities:
                raise AdmissionError("The site policy disables scheduler actions")
            return None
        if "instrumentation" not in capabilities:
            raise AdmissionError("The site policy disables job-local instrumentation")
        from ..raven_resources import placement_probe, verify_placement, limits

        configuration = {
            "context": {"cluster_id": policy.get("site", {}).get("cluster_id", "")},
            "resources": limits({"cpus": 1, "memory_mib": 96}),
        }
        candidate = placement_probe(configuration, purpose="instrumentation")
        candidate["probe_id"] = hashlib.sha256(
            _json(candidate, 64 << 10, "Local process identity")
        ).hexdigest()
        previous = router.job_local_receipt
        if (
            previous
            and previous["expires"] >= time.monotonic()
            and previous["candidate"] == candidate
            and previous["backend"] is backend
        ):
            return None
        from ..collection.ledger import make_placement_verifier

        records = make_placement_verifier(backend, purpose="instrumentation")(candidate)
        identity = verify_placement(
            configuration, candidate, records, purpose="instrumentation"
        )
        router.job_local_receipt = {
            "candidate": candidate,
            "identity": identity,
            "backend": backend,
            "expires": time.monotonic() + 2.0,
        }
        return None
    except (ValueError, OSError, RuntimeError) as exc:
        result = deferred(
            command, "Job-local launch deferred: " + str(exc)[:400], supported=True
        )
        result["data"]["prepared_invocation"] = list(argv)
        result["data"]["required_target"] = (
            "same invocation inside a verified owned Slurm job/step"
        )
        return result


@contextmanager
def reporting_submission(argv):
    """Preserve report submit's exact environment/intent under scheduler quota."""
    router = router_for()
    if not router.managed:
        yield
        return
    blocked = job_local_admission(["report", "submit"], router)
    if blocked is not None:
        raise AdmissionError(blocked["summary"])
    from ..collection.ledger import classify

    descriptor = classify(argv)
    if descriptor is None or descriptor.family != "submit":
        raise AdmissionError(
            "Reporting submission requires one exact sbatch invocation"
        )
    with router.source_backend.ledger.admit(descriptor, argv, validation=True):
        yield


def helper_admission(app, text, metadata=None):
    """Generated PTY helpers cannot smuggle analysis around worker admission."""
    router = router_for(app)
    if not router.managed:
        return None
    metadata = metadata or {}
    if metadata.get("helper_kind") == "user-paste":
        return None
    if (
        metadata.get("helper_kind") == "shell-hook"
        and metadata.get("session_kind", "local") == "local"
        and isinstance(text, str)
        and len(text.encode()) <= 64 << 10
    ):
        return None
    value = deferred(
        "terminal-helper",
        "Generated helpers need a declared operation and admitted execution target; ordinary shell input remains available.",
        supported=False,
    )
    value["reason"] = value["summary"]
    return value


def research_command_route(command, args, router=None):
    router = router or router_for()
    if not router.managed or command not in {"validate", "passport"}:
        return None
    # Displaying one bounded provenance document is ordinary Light evidence.
    if command == "passport" and list(args[:1]) == ["show"] and len(args) == 2:
        from ..provenance import load

        available = source_limit(256 << 10)
        token = _SOURCE_BUDGET.set({"remaining": available, "files": 1})
        try:
            document = load(os.path.expanduser(args[1]))
        finally:
            used = available - _SOURCE_BUDGET.get()["remaining"]
            _SOURCE_BUDGET.reset(token)
            source_read(max(0, used))
        return document, 0
    if command == "passport" and list(args[:1]) == ["capture"]:
        blocked = job_local_admission(["report", "passport"], router)
        return None if blocked is None else (blocked, 2)
    blocked = router.require(command, worker_operation="research-command")
    if blocked is not None:
        return blocked, 2
    receipt = router.dispatch(
        "research-command", {"command": command, "args": list(args)}
    )
    if receipt.get("status") == "deferred":
        return receipt, 2
    return receipt["result"]["output"], receipt["result"]["exit_code"]
