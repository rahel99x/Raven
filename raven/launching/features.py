"""Shared Research feature vocabulary and honest pre-launch readiness.

This registry declares integration opportunities. Selecting a feature never
creates observations, issues scheduler probes, or applies Operations actions.
"""

from __future__ import annotations

import difflib
import ast
import os
from pathlib import Path
import re
import shlex

# Registry order is also the documented order for the explicit ``all`` selector.
FEATURES = (
    {
        "name": "experiment",
        "label": "Experiment",
        "aliases": ("ex",),
        "source": "experiment",
        "description": "Lifecycle and actual application measurements",
        "prerequisites": "A compatible metric producer for scientific values and progress",
        "adapters": ("native-jsonl", "report-helper"),
        "cost": "metadata",
        "exports": ("application", "summary"),
    },
    {
        "name": "arrays",
        "label": "Arrays",
        "aliases": ("arr",),
        "source": "arrays",
        "description": "Exact array and task identity",
        "prerequisites": "A declared array manifest for sample-to-task meaning",
        "adapters": ("array-manifest",),
        "cost": "metadata",
        "exports": ("research", "summary"),
    },
    {
        "name": "evidence",
        "label": "Evidence",
        "aliases": ("ev",),
        "source": "evidence",
        "description": "Retained observations, provenance, and coverage",
        "prerequisites": "Actual retained application or scheduler observations",
        "adapters": ("native-jsonl", "scheduler-observer"),
        "cost": "metadata",
        "exports": ("application", "summary", "research"),
    },
    {
        "name": "artifacts",
        "label": "Artifacts",
        "aliases": ("ar",),
        "source": "artifacts",
        "description": "Declared outputs and contracts",
        "prerequisites": "Explicit artifact paths or an output contract",
        "adapters": ("artifact-contract", "report-helper"),
        "cost": "declared-files",
        "exports": ("artifacts",),
    },
    {
        "name": "passport",
        "label": "Passport",
        "aliases": ("pa",),
        "source": "passport",
        "description": "Bounded source identity and explicitly selected inputs",
        "prerequisites": "Explicit inputs and environment selections for scientific provenance",
        "adapters": ("passport",),
        "cost": "declared-files",
        "exports": ("research",),
    },
    {
        "name": "submit",
        "label": "Submit",
        "aliases": ("su",),
        "source": "submit",
        "description": "Prepared submission plan and actual receipt",
        "prerequisites": "Scheduler acceptance is verified only at submission",
        "adapters": ("launch-plan",),
        "cost": "metadata",
        "exports": ("research", "summary"),
    },
    {
        "name": "resources",
        "label": "Resources",
        "aliases": ("resource", "re", "predict"),
        "source": "predict",
        "description": "Native resource predictions and comparable history",
        "prerequisites": "An explicit workload query and sufficient comparable measurements",
        "adapters": ("native-predict",),
        "cost": "declared-files",
        "exports": ("research",),
    },
    {
        "name": "forecast",
        "label": "Forecast",
        "aliases": ("f",),
        "source": "forecast",
        "description": "Retained pending-time predictions",
        "prerequisites": "A governed observer while the job is pending; missed predictions cannot be backfilled",
        "adapters": ("scheduler-observer", "native-forecast"),
        "cost": "observer",
        "exports": ("research",),
    },
    {
        "name": "blockers",
        "label": "Blockers",
        "aliases": ("b",),
        "source": "blockers",
        "description": "Observed pending reasons and visible scheduler context",
        "prerequisites": "A governed observer while pending and authorized scheduler visibility",
        "adapters": ("scheduler-observer", "native-blockers"),
        "cost": "observer",
        "exports": ("research",),
    },
    {
        "name": "tradeoffs",
        "label": "Tradeoffs",
        "aliases": ("t",),
        "source": "tradeoffs",
        "description": "Explicit candidates and comparable measurements",
        "prerequisites": "Declared alternatives and measured comparable runs; queue outcomes when relevant",
        "adapters": ("native-tradeoffs",),
        "cost": "declared-files",
        "exports": ("research",),
    },
    {
        "name": "scaling",
        "label": "Scaling",
        "aliases": ("sc",),
        "source": "scaling",
        "description": "Measured runtime against declared workload sizes and workers",
        "prerequisites": "Worker count, problem size, repeats, and comparable completed runs",
        "adapters": ("native-scaling",),
        "cost": "declared-files",
        "exports": ("research", "summary"),
    },
    {
        "name": "workflow",
        "label": "Workflow",
        "aliases": ("w",),
        "source": "workflow",
        "description": "Original dependencies and declared workflow recipes",
        "prerequisites": "A declared workflow recipe for scientific dependency meaning",
        "adapters": ("native-workflow", "slurm-dependency"),
        "cost": "metadata",
        "exports": ("research",),
    },
    {
        "name": "operations",
        "label": "Operations",
        "aliases": ("op",),
        "source": "operations",
        "description": "Existing reviewed receipts; never automatic actions",
        "prerequisites": "Supported Operations receipts or manifests from explicitly reviewed actions",
        "adapters": ("operations-receipt",),
        "cost": "declared-files",
        "exports": ("research",),
    },
)
FEATURE_REGISTRY = {item["name"]: item for item in FEATURES}
ALIASES = {
    alias: item["name"]
    for item in FEATURES
    for alias in (item["name"], *item["aliases"])
}
DEFAULT_FEATURES = ("experiment", "evidence")


def normalize_features(names):
    """Normalize explicit vocabulary once, preserving first-selection order."""
    result = []
    for value in names:
        if not isinstance(value, str) or not value or len(value) > 128:
            raise ValueError(
                "Research selectors must be nonempty names of at most 128 characters"
            )
        if value == "all":
            selected = FEATURE_REGISTRY
        elif value in ALIASES:
            selected = (ALIASES[value],)
        else:
            match = difflib.get_close_matches(str(value), ALIASES, n=1, cutoff=0.5)
            hint = " Did you mean '" + match[0] + "'?" if match else ""
            raise ValueError(
                "Unknown Research feature '"
                + str(value)
                + "'. Choose: "
                + ", ".join(FEATURE_REGISTRY)
                + "."
                + hint
            )
        for name in selected:
            if name not in result:
                result.append(name)
    return result


def readiness(
    features,
    settings,
    *,
    execution="slurm",
    gpu=False,
    trace=False,
    effective_options=None,
    metric_client=None,
):
    rows = [
        {
            "feature": "run",
            "label": "Run records",
            "status": "ready",
            "reason": "Exact lifecycle and source snapshot; filesystem/runtime rechecked at job start.",
        },
        {
            "feature": "logs",
            "label": "Logs",
            "status": "ready",
            "reason": "Original Slurm stdout/stderr destinations are preserved and registered."
            if execution == "slurm"
            else "Inherited local stdout/stderr streams are preserved; automatic run-local log copies are unavailable.",
        },
        {
            "feature": "runtime",
            "label": "Reporting runtime",
            "status": "will-verify",
            "reason": "The pinned helper and Python 3.10+ must be visible on the execution host.",
        },
    ]
    bindings = settings.get("research_sources", {})
    for name in features:
        item = FEATURE_REGISTRY[name]
        status, reason = "needs-input", item["prerequisites"]
        if name in ("experiment", "evidence", "submit"):
            status = "ready"
            reason = item["description"] + "; " + item["prerequisites"] + "."
        if name == "arrays" and execution == "local":
            status, reason = (
                "unsupported",
                "Local execution has no Slurm array identity.",
            )
        elif (
            name == "arrays"
            and effective_options is not None
            and not effective_options.get("array")
        ):
            status, reason = (
                "needs-input",
                "Request an array with --sbatch=--array=... or a #SBATCH --array directive; sample-to-task meaning also requires a declared array manifest.",
            )
        elif name == "arrays":
            status, reason = (
                "will-verify",
                "Actual Slurm array/task identity at job start; sample mapping requires a declared array manifest.",
            )
        elif name == "submit" and execution == "local":
            status, reason = (
                "unsupported",
                "Local execution retains its launch plan; it has no Slurm submission receipt.",
            )
        elif name == "workflow":
            status, reason = (
                "needs-input",
                "Original scheduler dependency expressions are retained verbatim. Scientific workflow structure requires a declared native recipe.",
            )
        if name != "arrays" and (name in bindings or item["source"] in bindings):
            status, reason = (
                "will-verify",
                "Declared native source; its schema, attempt identity, and evidence must validate before use. "
                + item["prerequisites"],
            )
        elif name == "artifacts" and settings.get("artifacts"):
            status, reason = (
                "will-verify",
                "Only explicitly declared output paths will be registered; existence/content verified after execution.",
            )
        elif name == "passport":
            status, reason = (
                "ready",
                "Script digest and declared provenance retained; no arbitrary environment dump or dataset hashing.",
            )
        rows.append(
            {
                "feature": name,
                "label": item["label"],
                "status": status,
                "reason": reason,
            }
        )
    if gpu:
        rows.append(
            {
                "feature": "gpu",
                "label": "GPU",
                "status": "will-verify",
                "reason": "Verify assigned devices and supported provider at job start; device counters are not per-process accounting. Multi-node coverage may be partial.",
            }
        )
    if settings.get("cpu_memory", True):
        rows.append(
            {
                "feature": "cpu-memory",
                "label": "CPU/memory",
                "status": "will-verify",
                "reason": "Supported local supervisor/process evidence only; remote MPI ranks are outside this collector's scope.",
            }
        )
    if trace:
        adapter = settings.get("trace_adapter") or "python-monitoring"
        status = "will-verify"
        reason = (
            "Automatic attachment for Python processes inheriting this job's environment; no script hook is required. "
            "Python monitoring requires 3.12+; Python 3.10/3.11 requires explicit python-legacy-profile consent. "
            "Application interpreter and scope are verified at runtime. raven_exec is an optional command override; "
            "isolated interpreters and replaced PYTHONPATH can prevent attachment."
        )
        if adapter in {"native-cooperative", "mpi"}:
            status = "needs-input"
            reason = (
                "The raven_exec helper currently wraps supported Python scripts. "
                "Native or MPI tracing requires the existing cooperative SDK or prepared MPI adapter; "
                "this launch does not retrofit binary instrumentation or add rank wrappers."
            )
        rows.append(
            {
                "feature": "trace",
                "label": "Stack tracing",
                "status": status,
                "reason": reason,
            }
        )
        if settings.get("trace_cpu_placement"):
            rows.append(
                {
                    "feature": "trace-cpu-placement",
                    "label": "CPU placement",
                    "status": "will-verify",
                    "reason": "Optional Linux sched_getcpu observations on the application host, grouped by known socket/core/logical CPU. Each point describes one event-time query; continuous CPU residency and unobserved cores remain unknown. Unsupported hosts keep recording without CPU placement.",
                }
            )
    if metric_client is not None:
        append_metrics_readiness(rows, metric_client)
    return rows


def append_metrics_readiness(rows, evidence):
    """A bounded source declaration is never proof that measurements exist."""
    targets = evidence.get("targets", [])
    if targets:
        rows.append(
            {
                "feature": "metrics-client",
                "label": "Metrics",
                "status": "will-verify",
                "reason": "@raven client detected in "
                + ", ".join(targets)
                + ". Imports were inspected without execution; actual measurements are verified after launch.",
            }
        )
    if evidence.get("warnings"):
        rows.append(
            {
                "feature": "metrics-client",
                "label": "Metrics source check",
                "status": "needs-input",
                "reason": "; ".join(evidence["warnings"]),
            }
        )
    return rows


def detect_metric_client(script_path, project_root=None, *, working_directory=None):
    """Inspect only immediate literal Python targets, never shell-expand or execute.

    At most 32 files and 256 KiB are inspected. Imports inside conditional code
    count as declarations; dynamic launchers and unreadable targets remain unknown.
    Symlinks, devices and paths outside the declared project are not followed.
    """
    from ..tracing.protocol import regular_open

    script = Path(script_path).absolute()
    root = Path(project_root).absolute() if project_root else script.parent
    workdir = Path(working_directory).absolute() if working_directory else root
    result = {"targets": [], "warnings": []}
    budget = 256 << 10

    def read(path, limit):
        nonlocal budget
        path.relative_to(root)
        descriptor = regular_open(path)
        with os.fdopen(descriptor, "rb") as source:
            raw = source.read(min(limit, budget) + 1)
        if len(raw) > min(limit, budget):
            raise ValueError("Source exceeds the bounded readiness scan")
        budget -= len(raw)
        return raw.decode("utf-8")

    try:
        source = read(script, 128 << 10)
    except (OSError, ValueError, UnicodeError) as exc:
        return dict(
            result, warnings=["Metrics source readiness unavailable: " + str(exc)]
        )
    candidates = []
    if script.suffix == ".py" or re.search(r"^#![^\n]*\bpython(?:[0-9.]+)?\b", source):
        candidates.append((script, source))
    else:
        # Punctuation separates commands without executing substitutions,
        # expanding variables, reading shell includes or traversing directories.
        lexer = shlex.shlex(source, posix=True, punctuation_chars=";&|(){}")
        lexer.whitespace_split = True
        try:
            tokens = list(lexer)
        except ValueError:
            return dict(
                result,
                warnings=[
                    "Shell syntax prevented a bounded Python-target readiness scan."
                ],
            )
        for index, token in enumerate(tokens[:-1]):
            if not re.fullmatch(
                r"python(?:[0-9]+(?:\.[0-9]+)?)?", os.path.basename(token)
            ):
                continue
            after = index + 1
            while after < len(tokens) and tokens[after] in {
                "-u",
                "-B",
                "-E",
                "-s",
                "-P",
                "-I",
                "-S",
            }:
                after += 1
            if after >= len(tokens):
                continue
            target = tokens[after]
            if target.startswith("-") or any(char in target for char in "$`*?[]{}"):
                continue
            path = Path(os.path.abspath(workdir / target))
            if path not in [entry[0] for entry in candidates]:
                candidates.append((path, None))
            if len(candidates) >= 32:
                result["warnings"].append(
                    "Metrics readiness inspected at most 32 immediate Python targets; other producers are unknown."
                )
                break
    for path, text in candidates:
        try:
            text = read(path, 64 << 10) if text is None else text
            tree = ast.parse(text)
        except (
            OSError,
            ValueError,
            UnicodeError,
            SyntaxError,
            RecursionError,
            MemoryError,
        ):
            continue
        if any(
            isinstance(node, ast.Import)
            and any(alias.name in {"raven", "raven.client"} for alias in node.names)
            or isinstance(node, ast.ImportFrom)
            and node.module in {"raven", "raven.client"}
            for node in ast.walk(tree)
        ):
            result["targets"].append(str(path.relative_to(root)))
            vendored = path.parent / "raven.py"
            if vendored != path and vendored.is_file():
                result["warnings"].append(
                    "A local raven.py beside "
                    + str(path.relative_to(root))
                    + " takes import precedence; verify its vendor-client version and checksum before launch."
                )
    return result
