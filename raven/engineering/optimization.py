"""Reviewed, bounded resource experiments using native scheduler submissions.

This compiles an explicit repeated design; it does not predict an optimal
allocation or silently spend an account. Statistics reuse Raven's existing
independent-unit estimators. Unknown submission outcomes retain their budget
reservation until an operator reconciles an exact scheduler identity.
"""
from __future__ import annotations

import copy
import hashlib
import math
import random
import re
import statistics
import time

from .. import science_files, science_stats
from ..runtime_contracts import integer, number, object_, sequence, text
from . import _storage as storage

SCHEMA = "raven.resource-experiment/v1"
PLAN = "raven.resource-experiment-plan/v1"
RESULT = "raven.resource-experiment-results/v1"
_RESOURCE_KEYS = {"cpus", "nodes", "gpus", "memory_mib", "wall_seconds"}
_TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "NODE_FAIL", "OUT_OF_MEMORY", "PREEMPTED", "EVICTED", "REMOVED", "SUCCEEDED", "DONE", "EXIT", "C", "F", "X"}


def _digest(value):
    return hashlib.sha256(storage.encoded(value)).hexdigest()


def submission_digest(submission):
    """Bind explicitly verified effective-request evidence to planned bytes."""
    if not isinstance(submission, dict):
        raise ValueError("Submission must be an object")
    return _digest(submission)


def _quantity(value, kind):
    value = str(value)
    if kind == "cpu":
        if re.fullmatch(r"[0-9]+m", value):
            return int(value[:-1]) / 1000
        if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value):
            return float(value)
    else:
        match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([KMGTE]i?|)", value)
        if match:
            unit = match[2]
            power = "KMGTPE".find(unit[:1]) + 1 if unit else 0
            return float(match[1]) * (1024 if unit.endswith("i") else 1000) ** power
    raise ValueError("Resource experiments require explicit finite CPU/memory quantities")


def reservation(submission, *, _script_bytes=None):
    """Return conservative declared reservation bounds, never measured billing.

    Kubernetes includes init-container maxima, restartable sidecars and job
    completions; a deadline and CPU/memory limits are required. Batch requests
    use conservative per-node bounds. Native overrides are refused because
    they can invalidate the reviewed resource ceiling.
    """
    if not isinstance(submission, dict):
        raise ValueError("Submission must be an object")
    if "manifest" not in submission:
        if submission.get("native_options"):
            raise ValueError("Resource experiments do not accept native_options that can override reviewed bounds")
        cpus = integer(submission.get("cpus"), "explicit CPUs", 1, 1000000)
        nodes = integer(submission.get("nodes", 1), "nodes", 1, 1000000)
        gpus = integer(submission.get("gpus", 0), "GPUs", 0, 1000000)
        memory = number(submission.get("memory_mib"), "explicit memory MiB", 1, 1e12)
        wall = number(submission.get("wall_seconds"), "explicit wall seconds", 1, 1e9)
        # A script can submit an array, add tasks, request extra GPUs or reset
        # wall time through embedded native directives. Their precedence
        # differs by scheduler, so they cannot share a portable budget proof.
        # Existing ordinary launch preserves directives; bounded experiments
        # require resource-free scripts and one explicit reviewed request.
        raw = _script_bytes
        if raw is None:
            _, raw = science_files.snapshot(submission["script"], limit=1 << 20, keep=True)
        if re.search(rb"(?m)^\s*#(?:SBATCH|PBS|BSUB)\b", raw):
            raise ValueError("Bounded experiments require scripts without scheduler directives; declare resources in each variant")
        return dict(cpu_seconds=cpus * wall, gpu_seconds=gpus * wall,
                    reservation_seconds=nodes * wall, memory_bytes=memory * nodes * 1048576,
                    kind="declared total CPU/GPU request and conservative per-node memory bound; site rounding and billing are unqualified")
    manifest = submission["manifest"]
    spec = manifest.get("spec", {}) if isinstance(manifest, dict) else {}
    wall = integer(spec.get("activeDeadlineSeconds"), "Kubernetes activeDeadlineSeconds", 1, 10**9)
    completions = integer(spec.get("completions", 1), "Kubernetes completions", 1, 1000000)
    parallel = integer(spec.get("parallelism", 1), "Kubernetes parallelism", 1, 1000000)
    # backoffLimit=0 prevents unbudgeted replacement attempts. Pods can still
    # be replaced after infrastructure loss; charge prediction is never made.
    if spec.get("backoffLimit", 6) != 0:
        raise ValueError("Bounded Kubernetes trials require backoffLimit=0")
    pod = spec.get("template", {}).get("spec", {})
    if pod.get("restartPolicy") != "Never":
        raise ValueError("Bounded Kubernetes trials require restartPolicy=Never")
    containers = sequence(pod.get("containers"), "Kubernetes containers", 64, 1)
    init = sequence(pod.get("initContainers", []), "Kubernetes init containers", 64)
    cpu, memory, gpu = 0., 0., 0.
    resource_sets = []
    for container in containers + init:
        if not isinstance(container, dict):
            raise ValueError("Each Kubernetes container must be an object")
        limits = container.get("resources", {}).get("limits", {})
        resource_sets.append((limits, True))
        requests = container.get("resources", {}).get("requests", {})
        if not isinstance(limits, dict) or not isinstance(requests, dict):
            raise ValueError("Kubernetes limits and requests must be objects")
        for key in ("cpu", "memory"):
            if key in requests and _quantity(requests[key], key) > _quantity(limits.get(key, ""), key):
                raise ValueError("Kubernetes requests cannot exceed their limits")
    overhead = pod.get("overhead", {})
    if overhead:
        if not isinstance(overhead, dict):
            raise ValueError("Kubernetes pod overhead must be an object")
        resource_sets.append((overhead, False))
    for limits, mandatory in resource_sets:
        c = _quantity(limits.get("cpu", "" if mandatory else "0"), "cpu")
        m = _quantity(limits.get("memory", "" if mandatory else "0"), "memory")
        if (mandatory and (c <= 0 or m <= 0)) or not math.isfinite(c + m):
            raise ValueError("Kubernetes experiments need positive CPU and memory limits")
        gpu_keys = [k for k in limits if k.endswith("/gpu") or k.startswith(("gpu.intel.com/", "nvidia.com/mig-"))]
        unknown = {k for k in limits if "/" in k} - set(gpu_keys)
        if unknown:
            raise ValueError("Unclassified extended resources cannot establish a portable GPU budget: " + ", ".join(sorted(unknown)))
        g = sum(integer(v, "GPU extended-resource count", 0, 1000000) for k, v in limits.items() if k in gpu_keys)
        # Summing all init containers is conservative even for sidecars.
        cpu += c; memory += m; gpu += g
    # activeDeadlineSeconds is job-level elapsed time. parallelism bounds
    # simultaneous resources; completions accounts for serial task count.
    slots = max(completions, parallel)
    return dict(cpu_seconds=cpu * slots * wall, gpu_seconds=gpu * slots * wall,
                reservation_seconds=slots * wall, memory_bytes=memory * parallel,
                kind="declared Kubernetes limits; admission mutation and infrastructure replacement remain site qualification limits")


def _workload(submission, *, _source=None):
    normalized = copy.deepcopy(submission)
    if "manifest" in normalized:
        manifest = normalized["manifest"]
        manifest.get("metadata", {}).pop("name", None)
        spec = manifest.get("spec", {})
        spec.pop("activeDeadlineSeconds", None)
        pod = spec.get("template", {}).get("spec", {})
        for container in pod.get("containers", []) + pod.get("initContainers", []):
            image = container.get("image", "")
            if not re.fullmatch(r".+@sha256:[0-9a-f]{64}", image):
                raise ValueError("Comparable Kubernetes trials require digest-pinned container images")
            container.pop("resources", None)
    else:
        for key in _RESOURCE_KEYS | {"name"}:
            normalized.pop(key, None)
        record = _source
        if record is None:
            record, _ = science_files.snapshot(normalized["script"], limit=1 << 20)
        normalized["script"] = record["sha256"]
    return _digest(normalized)


def prepare(document, scheduler=None):
    """Compile a randomized repeated trial list and explicit charge ceiling."""
    storage.encoded(document)
    object_(document, {"schema", "id", "variants", "repetitions", "budget", "equivalence"},
            {"seed", "baseline"}, "resource experiment")
    if document["schema"] != SCHEMA:
        raise ValueError("Expected " + SCHEMA)
    experiment_id = storage.name(document["id"])
    reps = integer(document["repetitions"], "repetitions", 3, 10)
    seed = integer(document.get("seed", 0), "randomization seed", 0, 2**32 - 1)
    equivalence = object_(document["equivalence"], {"semantic", "reference", "absolute", "relative", "unit"}, name="scientific equivalence")
    for key in ("semantic", "unit"):
        text(equivalence[key], key, 256)
    for key in ("reference", "absolute", "relative"):
        number(equivalence[key], key, -1e100 if key == "reference" else 0, 1e100)
    budget = object_(document["budget"], {"max_jobs", "cpu_seconds", "gpu_seconds", "reservation_seconds", "max_concurrent"}, name="experiment budget")
    maximum = integer(budget["max_jobs"], "maximum jobs", 1, 64)
    integer(budget["max_concurrent"], "maximum concurrent trials", 1, 8)
    for key in ("cpu_seconds", "gpu_seconds", "reservation_seconds"):
        number(budget[key], "budget " + key, 0, 1e15)
    variants, seen, workload, sources, contents = [], set(), None, {}, {}
    for item in sequence(document["variants"], "variants", 8, 2):
        object_(item, {"id", "submission"}, name="resource variant")
        ident = storage.name(item["id"])
        if ident in seen:
            raise ValueError("Resource variant IDs must be unique")
        seen.add(ident)
        source = None
        raw = None
        if "script" in item["submission"]:
            path = item["submission"]["script"]
            if path not in sources:
                record, raw = science_files.snapshot(path, limit=1 << 20, keep=True)
                sources[path], contents[path] = record, raw
            source, raw = sources[path], contents[path]
        bounded = reservation(item["submission"], _script_bytes=raw)
        signature = _workload(item["submission"], _source=source)
        if workload is not None and signature != workload:
            raise ValueError("Variants must preserve workload, account and placement policy; vary resources only")
        workload = signature
        variants.append(dict(id=ident, submission=copy.deepcopy(item["submission"]), reservation=bounded))
    if len(variants) * reps > maximum:
        raise ValueError("Complete repeated design exceeds max_jobs; no partial design will be submitted")
    baseline = document.get("baseline", variants[0]["id"])
    if baseline not in seen:
        raise ValueError("Baseline must name a declared variant")
    totals = {key: sum(v["reservation"][key] * reps for v in variants)
              for key in ("cpu_seconds", "gpu_seconds", "reservation_seconds")}
    if any(totals[k] > budget[k] for k in totals):
        raise ValueError("Complete repeated design exceeds its declared resource budget")
    trials = [dict(id=f"{v['id']}-r{r + 1}", variant=v["id"], repetition=r + 1,
                   submission=copy.deepcopy(v["submission"]), reservation=v["reservation"])
              for r in range(reps) for v in variants]
    random.Random(seed).shuffle(trials)
    if scheduler is not None:
        for trial in trials:
            trial["native_plan"] = scheduler.review("submit", submission=trial["submission"])
            if "script" in trial["submission"]:
                native_digest = trial["native_plan"].get("script_sha256")
                if native_digest is not None and native_digest != sources[trial["submission"]["script"]]["sha256"]:
                    raise ValueError("Resource experiment script changed during native review")
    plan = dict(schema=PLAN, id=experiment_id, document=copy.deepcopy(document), baseline=baseline,
                workload_sha256=workload, budget=copy.deepcopy(budget), reservation=totals,
                trials=trials, sources=list(sources.values()),
                review=["Every trial is a separate explicit submission; no automatic retry.",
                        "Budget bounds requested resources, not actual billing or queue delay.",
                        "Comparisons require matching scientific output and independently identified completed attempts."],
                qualification="Experimental resource study; improved runtime or cost is not established before comparable measured trials.")
    plan["digest"] = _digest(plan)
    return plan


def _verify(plan, review_digest, *, check_sources=True):
    storage.encoded(plan)
    object_(plan, {"schema", "id", "document", "baseline", "workload_sha256", "budget", "reservation",
                   "trials", "sources", "review", "qualification", "digest"}, name="resource experiment plan")
    if not isinstance(plan, dict) or plan.get("schema") != PLAN or plan.get("digest") != review_digest:
        raise ValueError("Review the exact resource experiment plan before applying it")
    value = {k: v for k, v in plan.items() if k != "digest"}
    if _digest(value) != review_digest:
        raise ValueError("Reviewed resource experiment changed")
    # Recompile the design to prevent forged derived budgets or trial lists.
    if not check_sources:
        # Historical analysis retains the reviewed script digest and does not
        # need the original script or checkpoints to remain on disk.
        return
    expected = prepare(plan["document"])
    for key in ("id", "workload_sha256", "budget", "reservation", "sources", "baseline"):
        if expected[key] != plan[key]:
            raise ValueError("Reviewed resource sources or budget changed")
    original = [{k: v for k, v in t.items() if k != "native_plan"} for t in plan["trials"]]
    if original != expected["trials"]:
        raise ValueError("Reviewed resource trials changed")
    for trial in plan["trials"]:
        if "native_plan" in trial:
            native = trial["native_plan"]
            if (not isinstance(native, dict) or native.get("action") != "submit"
                    or native.get("submission") != trial["submission"]):
                raise ValueError("Native trial action or submission differs from its reviewed resource bounds")
    for source in plan["sources"]:
        science_files.verify(source)


def _design_digest(plan):
    """Stable across review-token refreshes, distinct for code or connection changes."""
    connections = [{"scheduler": t["native_plan"].get("scheduler"), "scope": t["native_plan"].get("scope")}
                   for t in plan["trials"] if "native_plan" in t]
    if connections and any(item != connections[0] for item in connections):
        raise ValueError("A resource study cannot mix scheduler connection scopes")
    return _digest(dict(document=plan["document"], workload=plan["workload_sha256"],
                        sources=[{k: s[k] for k in ("path", "sha256", "size")} for s in plan["sources"]],
                        connection=connections[0] if connections else None))


def status(project, experiment_id):
    with storage.project_store(project, "optimization") as fd:
        return storage.read(fd, storage.name(experiment_id) + ".json")


def apply(plan, scheduler, *, project, review_digest, trial_id=None):
    """Claim one trial durably, then issue its reviewed native submission once."""
    _verify(plan, review_digest)
    with storage.locked(project, "optimization") as fd:
        name = plan["id"] + ".json"
        try:
            journal = storage.read(fd, name)
        except FileNotFoundError:
            journal = dict(schema="raven.resource-experiment-state/v1", id=plan["id"], digest=review_digest,
                           design_digest=_design_digest(plan), trials={})
        if journal.get("design_digest") != _design_digest(plan):
            raise ValueError("Experiment ID already belongs to a different reviewed design")
        trials = journal["trials"]
        if trial_id is None:
            trial_id = next((t["id"] for t in plan["trials"] if t["id"] not in trials), None)
        chosen = next((t for t in plan["trials"] if t["id"] == trial_id), None)
        if chosen is None or trial_id in trials:
            raise ValueError("Trial is unavailable or already claimed; inspect its receipt instead of resubmitting")
        if sum(t.get("state") not in {"terminal", "not_submitted"} for t in trials.values()) >= plan["budget"]["max_concurrent"]:
            raise ValueError("Concurrent trial budget exhausted; reconcile exact completed attempts first")
        native = chosen.get("native_plan")
        if not isinstance(native, dict):
            raise ValueError("Prepare this design with the selected scheduler before reviewing submission")
        if native.get("expires_seconds") is not None and not 0 <= time.time() - native.get("created", 0) <= native["expires_seconds"]:
            raise ValueError("Native trial review expired; prepare and review this same design again")
        entry = dict(state="issuing", trial_id=trial_id, claimed_at=time.time(), review_digest=review_digest, reservation=chosen["reservation"])
        trials[trial_id] = entry
        storage.write(fd, name, journal)
        try:
            receipt = scheduler.apply(native, native["digest"])
        except BaseException as exc:
            entry.update(state="unknown", error=f"{type(exc).__name__}: {exc}")
            storage.write(fd, name, journal)
            raise
        entry.update(state="accepted" if receipt.get("status") == "accepted" else "unknown", receipt=receipt)
        storage.write(fd, name, journal)
        return copy.deepcopy(entry)


def reconcile(project, experiment_id, scheduler, *, bindings=None):
    """Refresh exact retained identities; explicit bindings resolve native-ID-only receipts.

    Supplying a binding is an operator assertion about the original submission,
    verified against the native scheduler. Job-number coincidence alone never
    releases an unknown trial's concurrency reservation.
    """
    from ..schedulers.core import scope_digest, validate_identity
    bindings = {} if bindings is None else bindings
    if not isinstance(bindings, dict) or len(bindings) > 64:
        raise ValueError("Bindings must name at most 64 trial identities")
    with storage.locked(project, "optimization") as fd:
        name = storage.name(experiment_id) + ".json"
        journal = storage.read(fd, name)
        if set(bindings) - journal["trials"].keys():
            raise ValueError("Binding names a trial that was never claimed")
        for key, entry in journal["trials"].items():
            receipt = entry.get("receipt", {})
            identity = bindings.get(key, entry.get("identity", receipt.get("identity")))
            if identity is None:
                entry["reconciliation"] = "Exact attempt binding required; no automatic resubmission or budget release"
                continue
            identity = validate_identity(identity)
            if receipt.get("native_id") and identity["native_id"] != receipt["native_id"]:
                raise ValueError("Binding differs from the accepted native job ID")
            recorded_scope = receipt.get("scope")
            expected_scope = scope_digest(recorded_scope) if isinstance(recorded_scope, dict) else recorded_scope
            if expected_scope and identity["scope"] != expected_scope:
                raise ValueError("Binding differs from the accepted scheduler scope")
            if receipt.get("scheduler") and identity["scheduler"] != receipt["scheduler"]:
                raise ValueError("Binding differs from the accepted native scheduler")
            prior = entry.get("identity")
            if prior is not None and identity != prior:
                raise ValueError("A trial cannot be rebound to a different attempt")
            job = getattr(scheduler, "fresh_detail", scheduler.detail)(identity)
            if job.get("identity") != identity:
                raise ValueError("Scheduler returned a different attempt")
            owner = getattr(scheduler, "scope", {}).get("user")
            if owner and job.get("user") != owner:
                raise ValueError("Trial attempt is not owned by the selected scheduler login")
            entry.update(identity=identity, scheduler_state=job.get("state"), reconciled_at=time.time())
            if str(job.get("state", "")).upper() in _TERMINAL:
                entry["state"] = "terminal"
            entry["reconciliation"] = "Exact scheduler attempt verified"
        storage.write(fd, name, journal)
        return copy.deepcopy(journal)


def review_cancel(project, experiment_id, trial_id, scheduler):
    journal = status(project, experiment_id)
    entry = journal["trials"].get(trial_id)
    if not entry or not entry.get("identity"):
        raise ValueError("Bind and verify the exact trial attempt before cancellation")
    if entry.get("state") == "terminal":
        raise ValueError("This trial is already terminal; no cancellation is needed")
    return scheduler.review("cancel", identity=entry["identity"])


def cancel(plan, scheduler, *, project, experiment_id, trial_id, review_digest):
    """Apply a separately reviewed cancellation without releasing its budget."""
    with storage.locked(project, "optimization") as fd:
        name = storage.name(experiment_id) + ".json"
        journal = storage.read(fd, name)
        entry = journal["trials"].get(trial_id)
        if (not entry or not entry.get("identity") or not isinstance(plan, dict)
                or plan.get("action") != "cancel" or plan.get("identity") != entry["identity"]):
            raise ValueError("Cancellation must identify the exact bound trial attempt")
        if plan.get("digest") != review_digest:
            raise ValueError("Review digest differs from the exact trial cancellation")
        if entry.get("cancellation") is not None:
            raise ValueError("Cancellation already has an issuance receipt; reconcile native state")
        entry["cancellation"] = dict(state="issuing", reviewed=review_digest, claimed_at=time.time())
        storage.write(fd, name, journal)
        try:
            receipt = scheduler.apply(plan, review_digest)
        except BaseException as exc:
            entry["cancellation"].update(state="unknown", error=f"{type(exc).__name__}: {exc}")
            storage.write(fd, name, journal)
            raise
        entry["cancellation"].update(state=receipt.get("status", "unknown"), receipt=receipt)
        storage.write(fd, name, journal)
        return copy.deepcopy(entry)


def analyze(document):
    """Compare recorded repetitions, preserving missing/failed trials and units."""
    object_(document, {"schema", "plan", "observations"}, name="resource results")
    if document["schema"] != RESULT:
        raise ValueError("Expected " + RESULT)
    plan = document["plan"]
    _verify(plan, plan.get("digest"), check_sources=False)
    by_id = {t["id"]: t for t in plan["trials"]}
    groups = {t["variant"]: [] for t in plan["trials"]}
    seen, attempts, excluded, accounting = set(), set(), [], []
    eq = plan["document"]["equivalence"]
    tolerance = max(eq["absolute"], eq["relative"] * abs(eq["reference"]))
    for row in sequence(document["observations"], "trial observations", 64):
        object_(row, {"trial_id", "identity", "outcome", "runtime_seconds", "queue_seconds", "cpu_seconds", "gpu_seconds", "output"},
                {"controls"}, name="trial observation")
        key = row["trial_id"]
        if key not in by_id or key in seen:
            raise ValueError("Observations must identify unique planned trials")
        seen.add(key)
        from ..schedulers.core import validate_identity
        identity = validate_identity(row["identity"])
        attempt = _digest(identity)
        if attempt in attempts:
            raise ValueError("Independent repetitions cannot reuse a scheduler attempt")
        attempts.add(attempt)
        if row["outcome"] not in {"completed", "scientific_failed", "infrastructure_failed", "cancelled"}:
            raise ValueError("Preserve completed, scientific_failed, infrastructure_failed or cancelled outcomes")
        values = {}
        for metric in ("runtime_seconds", "queue_seconds", "cpu_seconds", "gpu_seconds"):
            values[metric] = None if row[metric] is None else number(row[metric], metric, 0, 1e15)
        accounting.append(values)
        output = row["output"]
        if output is not None:
            object_(output, {"semantic", "unit", "value", "sha256"}, name="scientific output")
            number(output["value"], "scientific output value", -1e100, 1e100)
            if not isinstance(output["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", output["sha256"]):
                raise ValueError("Scientific output requires its exact SHA-256")
        controls = row.get("controls")
        supported = False
        if controls is not None:
            object_(controls, {"identity", "submission_sha256", "evidence"}, name="effective request evidence")
            text(controls["evidence"], "effective resource evidence", 4096)
            supported = (_identity_digest(controls["identity"]) == attempt
                         and controls["submission_sha256"] == _digest(by_id[key]["submission"]))
        compatible = (row["outcome"] == "completed" and output is not None and supported
                      and output["semantic"] == eq["semantic"] and output["unit"] == eq["unit"]
                      and abs(output["value"] - eq["reference"]) <= tolerance)
        if not compatible:
            excluded.append(dict(trial_id=key, identity=identity, outcome=row["outcome"], measurements=values,
                                 controls_supported=supported, output_available=output is not None,
                                 reason="Outcome, scientific equivalence or effective resource evidence differs/is missing; retained, excluded from comparable estimates"))
            continue
        groups[by_id[key]["variant"]].append(dict(id=attempt, **values))
    summary, comparisons = {}, []
    for variant, records in groups.items():
        summary[variant] = dict(comparable_trials=len(records), **{
            metric: dict(unit="seconds", samples=len(vals), mean=statistics.fmean(vals) if vals else None)
            for metric in ("runtime_seconds", "queue_seconds", "cpu_seconds", "gpu_seconds")
            for vals in [[r[metric] for r in records if r[metric] is not None]]})
    for variant, records in groups.items():
        if variant == plan["baseline"]:
            continue
        for metric in ("runtime_seconds", "queue_seconds", "cpu_seconds", "gpu_seconds"):
            control = [{"id": r["id"], "value": r[metric]} for r in groups[plan["baseline"]] if r[metric] is not None]
            treatment = [{"id": r["id"], "value": r[metric]} for r in records if r[metric] is not None]
            result = science_stats.compare(dict(schema="raven.statistics/v1", design="independent", unit="scheduler attempt", control=control, treatment=treatment)) if control and treatment else {"interval": None, "warnings": ["No comparable measured samples in one or both variants"]}
            comparisons.append(dict(variant=variant, baseline=plan["baseline"], metric=metric, unit="seconds", estimate_kind="observed association; not causal or predictive", statistics=result))
    return dict(schema="raven.resource-experiment-analysis/v1", id=plan["id"], plan_digest=plan["digest"],
                groups=summary, comparisons=comparisons, excluded=excluded, missing=sorted(set(by_id) - seen),
                accounting={metric: dict(unit="seconds", known_sum=math.fsum(vals), known_attempts=len(vals),
                                         unknown_attempts=len(accounting) - len(vals), includes_failed_attempts=True)
                            for metric in ("runtime_seconds", "queue_seconds", "cpu_seconds", "gpu_seconds")
                            for vals in [[row[metric] for row in accounting if row[metric] is not None]]},
                assumptions=["Independent repeated scheduler attempts; randomization does not remove site interference.",
                             "Queue, runtime and resource consumption retain separate measurements; missing counters stay unknown.",
                             "Effective-request evidence is explicitly supplied; its digest binds declared bytes, not hardware truth or native accounting attestation.",
                             "Intervals are exploratory and not corrected for multiple metric/variant comparisons."],
                qualification="Measured comparisons only; no automatic recommendation, submission or claim of optimum")


def _identity_digest(value):
    from ..schedulers.core import validate_identity
    return _digest(validate_identity(value))
