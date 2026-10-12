"""Shared Research feature vocabulary and honest pre-launch readiness.

This registry declares integration opportunities. Selecting a feature never
creates observations, issues scheduler probes, or applies Operations actions.
"""
from __future__ import annotations

import difflib

# Registry order is also the documented order for the explicit ``all`` selector.
FEATURES = (
    {"name": "experiment", "label": "Experiment", "aliases": ("ex",), "source": "experiment", "description": "Lifecycle and actual application measurements", "prerequisites": "A compatible metric producer for scientific values and progress", "adapters": ("native-jsonl", "report-helper"), "cost": "metadata", "exports": ("application", "summary")},
    {"name": "arrays", "label": "Arrays", "aliases": ("arr",), "source": "arrays", "description": "Exact array and task identity", "prerequisites": "A declared array manifest for sample-to-task meaning", "adapters": ("array-manifest",), "cost": "metadata", "exports": ("research", "summary")},
    {"name": "evidence", "label": "Evidence", "aliases": ("ev",), "source": "evidence", "description": "Retained observations, provenance, and coverage", "prerequisites": "Actual retained application or scheduler observations", "adapters": ("native-jsonl", "scheduler-observer"), "cost": "metadata", "exports": ("application", "summary", "research")},
    {"name": "artifacts", "label": "Artifacts", "aliases": ("ar",), "source": "artifacts", "description": "Declared outputs and contracts", "prerequisites": "Explicit artifact paths or an output contract", "adapters": ("artifact-contract", "report-helper"), "cost": "declared-files", "exports": ("artifacts",)},
    {"name": "passport", "label": "Passport", "aliases": ("pa",), "source": "passport", "description": "Bounded source identity and explicitly selected inputs", "prerequisites": "Explicit inputs and environment selections for scientific provenance", "adapters": ("passport",), "cost": "declared-files", "exports": ("research",)},
    {"name": "submit", "label": "Submit", "aliases": ("su",), "source": "submit", "description": "Prepared submission plan and actual receipt", "prerequisites": "Scheduler acceptance is verified only at submission", "adapters": ("launch-plan",), "cost": "metadata", "exports": ("research", "summary")},
    {"name": "resources", "label": "Resources", "aliases": ("resource", "re", "predict"), "source": "predict", "description": "Native resource predictions and comparable history", "prerequisites": "An explicit workload query and sufficient comparable measurements", "adapters": ("native-predict",), "cost": "declared-files", "exports": ("research",)},
    {"name": "forecast", "label": "Forecast", "aliases": ("f",), "source": "forecast", "description": "Retained pending-time predictions", "prerequisites": "A governed observer while the job is pending; missed predictions cannot be backfilled", "adapters": ("scheduler-observer", "native-forecast"), "cost": "observer", "exports": ("research",)},
    {"name": "blockers", "label": "Blockers", "aliases": ("b",), "source": "blockers", "description": "Observed pending reasons and visible scheduler context", "prerequisites": "A governed observer while pending and authorized scheduler visibility", "adapters": ("scheduler-observer", "native-blockers"), "cost": "observer", "exports": ("research",)},
    {"name": "tradeoffs", "label": "Tradeoffs", "aliases": ("t",), "source": "tradeoffs", "description": "Explicit candidates and comparable measurements", "prerequisites": "Declared alternatives and measured comparable runs; queue outcomes when relevant", "adapters": ("native-tradeoffs",), "cost": "declared-files", "exports": ("research",)},
    {"name": "scaling", "label": "Scaling", "aliases": ("sc",), "source": "scaling", "description": "Measured runtime against declared workload sizes and workers", "prerequisites": "Worker count, problem size, repeats, and comparable completed runs", "adapters": ("native-scaling",), "cost": "declared-files", "exports": ("research", "summary")},
    {"name": "workflow", "label": "Workflow", "aliases": ("w",), "source": "workflow", "description": "Original dependencies and declared workflow recipes", "prerequisites": "A declared workflow recipe for scientific dependency meaning", "adapters": ("native-workflow", "slurm-dependency"), "cost": "metadata", "exports": ("research",)},
    {"name": "operations", "label": "Operations", "aliases": ("op",), "source": "operations", "description": "Existing reviewed receipts; never automatic actions", "prerequisites": "Supported Operations receipts or manifests from explicitly reviewed actions", "adapters": ("operations-receipt",), "cost": "declared-files", "exports": ("research",)},
)
FEATURE_REGISTRY = {item["name"]: item for item in FEATURES}
ALIASES = {alias: item["name"] for item in FEATURES for alias in (item["name"], *item["aliases"])}
DEFAULT_FEATURES = ("experiment", "evidence")


def normalize_features(names):
    """Normalize explicit vocabulary once, preserving first-selection order."""
    result = []
    for value in names:
        if not isinstance(value, str) or not value or len(value) > 128:
            raise ValueError("Research selectors must be nonempty names of at most 128 characters")
        if value == "all":
            selected = FEATURE_REGISTRY
        elif value in ALIASES:
            selected = (ALIASES[value],)
        else:
            match = difflib.get_close_matches(str(value), ALIASES, n=1, cutoff=.5)
            hint = " Did you mean '" + match[0] + "'?" if match else ""
            raise ValueError("Unknown Research feature '" + str(value) + "'. Choose: " + ", ".join(FEATURE_REGISTRY) + "." + hint)
        for name in selected:
            if name not in result:
                result.append(name)
    return result


def readiness(features, settings, *, execution="slurm", gpu=False, trace=False, effective_options=None):
    rows = [{"feature": "run", "label": "Run records", "status": "ready", "reason": "Exact lifecycle and source snapshot; filesystem/runtime rechecked at job start."},
            {"feature": "logs", "label": "Logs", "status": "ready", "reason": "Original Slurm stdout/stderr destinations are preserved and registered." if execution == "slurm" else "Inherited local stdout/stderr streams are preserved; automatic run-local log copies are unavailable."},
            {"feature": "runtime", "label": "Reporting runtime", "status": "will-verify", "reason": "The pinned helper and Python 3.10+ must be visible on the execution host."}]
    bindings = settings.get("research_sources", {})
    for name in features:
        item = FEATURE_REGISTRY[name]
        status, reason = "needs-input", item["prerequisites"]
        if name in ("experiment", "evidence", "submit"):
            status = "ready"
            reason = item["description"] + "; " + item["prerequisites"] + "."
        if name == "arrays" and execution == "local":
            status, reason = "unsupported", "Local execution has no Slurm array identity."
        elif name == "arrays" and effective_options is not None and not effective_options.get("array"):
            status, reason = "needs-input", "Request an array with --sbatch=--array=... or a #SBATCH --array directive; sample-to-task meaning also requires a declared array manifest."
        elif name == "arrays":
            status, reason = "will-verify", "Actual Slurm array/task identity at job start; sample mapping requires a declared array manifest."
        elif name == "submit" and execution == "local":
            status, reason = "unsupported", "Local execution retains its launch plan; it has no Slurm submission receipt."
        elif name == "workflow":
            status, reason = "needs-input", "Original scheduler dependency expressions are retained verbatim. Scientific workflow structure requires a declared native recipe."
        if name != "arrays" and (name in bindings or item["source"] in bindings):
            status, reason = "will-verify", "Declared native source; its schema, attempt identity, and evidence must validate before use. " + item["prerequisites"]
        elif name == "artifacts" and settings.get("artifacts"):
            status, reason = "will-verify", "Only explicitly declared output paths will be registered; existence/content verified after execution."
        elif name == "passport":
            status, reason = "ready", "Script digest and declared provenance retained; no arbitrary environment dump or dataset hashing."
        rows.append({"feature": name, "label": item["label"], "status": status, "reason": reason})
    if gpu:
        rows.append({"feature": "gpu", "label": "GPU", "status": "will-verify", "reason": "Verify assigned devices and supported provider at job start; device counters are not per-process accounting. Multi-node coverage may be partial."})
    if settings.get("cpu_memory", True):
        rows.append({"feature": "cpu-memory", "label": "CPU/memory", "status": "will-verify", "reason": "Supported local supervisor/process evidence only; remote MPI ranks are outside this collector's scope."})
    if trace:
        adapter = settings.get("trace_adapter", "")
        status = "will-verify" if adapter else "needs-input"
        reason = "Source RAVEN_SHELL_HELPER after activation and use raven_exec for the application. " + ("Adapter " + adapter + " is verified inside the application runtime." if adapter else "Choose a supported adapter; a shell wrapper cannot capture arbitrary application stacks.")
        if adapter in {"native-cooperative", "mpi"}:
            status = "needs-input"
            reason = ("The raven_exec helper currently wraps supported Python scripts. "
                      "Native or MPI tracing requires the existing cooperative SDK or prepared MPI adapter; "
                      "this launch does not retrofit binary instrumentation or add rank wrappers.")
        rows.append({"feature": "trace", "label": "Stack tracing", "status": status, "reason": reason})
    return rows
