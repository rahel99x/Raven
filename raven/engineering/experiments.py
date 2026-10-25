"""Continuous scientific identities linking existing, exact run attempts.

This is an index, not another job or run store. Raven run inventories remain the
authority for execution identity. External scheduler identities are recorded as
explicit observations and never manufacture a Raven run or imply live access.
"""

from __future__ import annotations

import copy
import hashlib
import math
import re
import time

from .. import projects, run_home
from . import _storage as S

SCHEMA = "raven.experiment/v1"
MAX_ATTEMPTS = 4096
_RELATIONS = {"submission", "retry", "checkpoint", "array", "resume", "recovery"}
_METRICS = {"cpu_seconds", "gpu_seconds", "allocated_cpu_seconds", "allocated_gpu_seconds",
            "memory_byte_seconds", "elapsed_seconds"}


def _digest(value):
    return hashlib.sha256(S.encoded(value)).hexdigest()


def _id(value):
    S.name(value)
    if len(value) > 128:
        raise ValueError("Experiment identity must be at most 128 ASCII characters")
    return value


def retained_identity(binding):
    """The existing Run Home identity plus its explicit scheduler restart.

    CPU usage, lifecycle state, and other changing observations do not change
    this identity. Shared consumers must use this helper instead of dropping
    the restart component when joining retained exports and experiment links.
    """
    if not isinstance(binding, dict) or not isinstance(binding.get("launch") or {}, dict):
        raise ValueError("Retained run identity requires a run binding and launch object")
    restart = (binding.get("launch") or {}).get("restart_count", 0)
    if type(restart) is not int or not 0 <= restart <= 2147483646:
        raise ValueError("Retained scheduler restart count must be a bounded nonnegative integer")
    return list(run_home.identity(binding)) + [restart]


def _association(binding, scheduler_identity, *, execution=None):
    """Reject known conflicts without promoting caller links to attestation."""
    if scheduler_identity is None:
        return
    if str(binding.get("job_id") or "") != str(scheduler_identity["native_id"]):
        raise ValueError("Scheduler identity belongs to another run")
    launch = binding.get("launch") or {}
    recorded_cluster, native_cluster = launch.get("cluster"), scheduler_identity.get("native_cluster")
    if recorded_cluster and native_cluster and recorded_cluster != native_cluster:
        raise ValueError("Scheduler native cluster differs from the retained run cluster")
    known_slurm = execution == "slurm" or bool((launch.get("submission_token") or recorded_cluster)
                                               and binding.get("job_id") not in (None, "", "local"))
    if known_slurm and scheduler_identity["scheduler"] != "slurm":
        raise ValueError("A retained Slurm run cannot be associated with another scheduler family")
    if execution == "local":
        raise ValueError("Standalone execution has no native scheduler attempt association")
    if scheduler_identity["scheduler"] == "slurm" and "restart_count" in launch:
        match = re.fullmatch(r".+/restart:([0-9]+|unknown)", scheduler_identity["attempt"])
        if match and match.group(1) != "unknown" and int(match.group(1)) != launch["restart_count"]:
            raise ValueError("Scheduler restart differs from the retained run attempt")


def _text(value, label, maximum=256, *, empty=False):
    if (not isinstance(value, str) or not (0 if empty else 1) <= len(value) <= maximum
            or any(not ch.isprintable() for ch in value)):
        raise ValueError(label + " must be bounded printable text")
    return value


def _revision(document, expected):
    if expected is not None and (type(expected) is not int or document["revision"] != expected):
        raise ValueError("Experiment changed since review; reload it before saving")


def _validate(value):
    if (not isinstance(value, dict) or value.get("schema") != SCHEMA
            or set(value) != {"schema", "experiment_id", "title", "revision", "created", "updated", "attempts"}
            or type(value["revision"]) is not int or value["revision"] < 1):
        raise ValueError("Invalid continuous experiment document")
    _id(value["experiment_id"])
    _text(value["title"], "Experiment title", empty=True)
    for key in ("created", "updated"):
        if type(value[key]) not in (int, float) or not math.isfinite(value[key]) or value[key] < 0:
            raise ValueError("Experiment timestamps must be finite")
    attempts = value["attempts"]
    if not isinstance(attempts, list) or len(attempts) > MAX_ATTEMPTS:
        raise ValueError("Experiment exceeds its bounded attempt inventory")
    seen = set()
    native = set()
    for attempt in attempts:
        if not isinstance(attempt, dict) or set(attempt) != {"id", "relation", "parent", "source", "identity",
                "scheduler_identity", "run", "state", "resources", "provenance", "usage", "observed"}:
            raise ValueError("Invalid experiment attempt record")
        if not isinstance(attempt["id"], str) or not re.fullmatch(r"[0-9a-f]{64}", attempt["id"]) or attempt["id"] in seen:
            raise ValueError("Experiment attempts need unique immutable identities")
        if attempt["relation"] not in _RELATIONS or (attempt["parent"] is not None and attempt["parent"] not in seen):
            raise ValueError("Attempt ancestry must reference an earlier linked attempt")
        seen.add(attempt["id"])
        if attempt["source"] not in {"raven-inventory", "external-scheduler-observation"}:
            raise ValueError("Unknown attempt identity source")
        _text(attempt["state"], "Observed attempt state", 128)
        if (type(attempt["observed"]) not in (int, float) or not math.isfinite(attempt["observed"])
                or attempt["observed"] < 0):
            raise ValueError("Attempt observation time must be finite")
        if not isinstance(attempt["resources"], dict) or not isinstance(attempt["provenance"], dict):
            raise ValueError("Attempt resources and provenance must be objects")
        if attempt["scheduler_identity"] is not None:
            from ..schedulers.core import validate_identity
            normalized = validate_identity(attempt["scheduler_identity"])
            key = _digest(normalized)
            if key in native:
                raise ValueError("A scheduler attempt cannot be charged twice")
            native.add(key)
        if attempt["source"] == "raven-inventory":
            if not isinstance(attempt["run"], dict) or not isinstance(attempt["identity"], list) or len(attempt["identity"]) != 7:
                raise ValueError("Raven attempts require their exact retained run identity")
            exact = retained_identity(attempt["run"])
            if exact != attempt["identity"]:
                raise ValueError("Retained attempt binding and identity disagree")
            _association(attempt["run"], attempt["scheduler_identity"])
        elif attempt["identity"] is not None or attempt["run"] is not None or attempt["scheduler_identity"] is None:
            raise ValueError("External observations cannot manufacture Raven run inventory bindings")
        expected_key = (_digest({"scheduler": attempt["scheduler_identity"]}) if attempt["scheduler_identity"]
                        else _digest({"raven_run": attempt["identity"]}))
        if attempt["id"] != expected_key:
            raise ValueError("Experiment attempt digest differs from its immutable identity")
        _usage(attempt["usage"])
    S.encoded(value)
    return value


def create(project, experiment_id, title="", *, expected_revision=None):
    """Create a private index without touching existing run inventories."""
    _id(experiment_id)
    _text(title, "Experiment title", empty=True)
    if expected_revision is not None:
        raise ValueError("Creating an experiment does not replace an existing revision")
    now = time.time()
    document = dict(schema=SCHEMA, experiment_id=experiment_id, title=title,
                    revision=1, created=now, updated=now, attempts=[])
    with S.locked(project, "experiments") as fd:
        return S.write(fd, experiment_id + ".json", document, exclusive=True)


def load(project, experiment_id):
    _id(experiment_id)
    with S.project_store(project, "experiments") as fd:
        document = _validate(S.read(fd, experiment_id + ".json"))
    if document["experiment_id"] != experiment_id:
        raise ValueError("Experiment identity differs from the requested filename")
    return document


def _usage(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - (_METRICS | {"source", "scope", "complete"}):
        raise ValueError("Accounting uses explicit allocation or step counters and known units")
    _text(value.get("source"), "Accounting source", 1024)
    if value.get("scope") not in {"allocation", "step"} or type(value.get("complete")) is not bool:
        raise ValueError("Accounting needs an explicit allocation/step scope and completeness flag")
    for key in _METRICS & value.keys():
        number = value[key]
        if number is not None and (type(number) not in (int, float) or not math.isfinite(number) or not 0 <= number <= 1e30):
            raise ValueError("Accounting counters must be finite, nonnegative values or unknown")
    return copy.deepcopy(value)


def _attempt(binding, scheduler_identity, usage, relation, parent):
    if relation not in _RELATIONS:
        raise ValueError("Unsupported experiment attempt relationship")
    if scheduler_identity is not None:
        from ..schedulers.core import validate_identity
        scheduler_identity = validate_identity(scheduler_identity)
    if binding is not None:
        if not isinstance(binding, dict):
            raise ValueError("A Raven run binding must be an object")
        result = run_home.load(binding)
        current, inventory = result["selected"]["binding"], result["inventory"]
        exact = retained_identity(current)
        # A native observation can supplement identity; it cannot contradict
        # the run inventory's job or cluster association.
        if scheduler_identity:
            execution = ((result.get("report") or {}).get("plan") or {}).get("execution")
            execution = execution or inventory.get("metadata", {}).get("automation", {}).get("execution")
            _association(current, scheduler_identity, execution=execution)
        original = {key: copy.deepcopy(current.get(key)) for key in
                    ("project_root", "project_id", "run_id", "attempt", "job_id", "experiment_id", "launch", "automation_identity")}
        source, state = "raven-inventory", inventory["state"]
        resources, provenance = inventory.get("resources", {}), inventory.get("provenance", {})
    else:
        if scheduler_identity is None:
            raise ValueError("An external attempt needs an exact scheduler identity")
        exact, original = None, None
        source, state, resources, provenance = "external-scheduler-observation", "UNKNOWN", {}, {}
    key = _digest({"scheduler": scheduler_identity}) if scheduler_identity else _digest({"raven_run": exact})
    return dict(id=key, relation=relation, parent=parent, source=source, identity=exact,
                scheduler_identity=copy.deepcopy(scheduler_identity), run=original, state=state,
                resources=copy.deepcopy(resources), provenance=copy.deepcopy(provenance),
                usage=_usage(usage), observed=time.time())


def link(project, experiment_id, binding=None, *, scheduler_identity=None,
         run_id=None, parent=None, relation="submission", usage=None, expected_revision=None):
    """Link or refresh one exact attempt; retries remain separate billable work.

    ``binding=None`` explicitly records an external scheduler observation.
    Such an identity is not labelled a verified Raven run or a live connection.
    ``usage.scope=step`` is retained for inspection but excluded from totals;
    mixing job summary and step counters would double-count the same work.
    """
    _id(experiment_id)
    if run_id is not None:
        if binding is not None:
            raise ValueError("Choose a run ID or an explicit binding, not both")
        binding = projects.select_run(str(project), run_id)["binding"]
    attempt = _attempt(binding, scheduler_identity, usage, relation, parent)
    with S.locked(project, "experiments") as fd:
        document = _validate(S.read(fd, experiment_id + ".json"))
        if document["experiment_id"] != experiment_id:
            raise ValueError("Experiment identity differs from the requested filename")
        _revision(document, expected_revision)
        prior = next((item for item in document["attempts"] if item["id"] == attempt["id"]), None)
        if prior:
            for key in ("identity", "scheduler_identity", "run", "parent", "relation", "source"):
                if prior[key] != attempt[key]:
                    raise ValueError("An existing attempt cannot be rebound or given different ancestry")
            if usage is None:
                attempt["usage"] = prior["usage"]
            document["attempts"][document["attempts"].index(prior)] = attempt
        else:
            if len(document["attempts"]) >= MAX_ATTEMPTS:
                raise ValueError("Experiment attempt limit reached; start a related experiment")
            if parent is not None and parent not in {item["id"] for item in document["attempts"]}:
                raise ValueError("Parent must be an earlier linked attempt")
            # The same Raven run cannot acquire a second charge identity when
            # native scheduler evidence is attached after its first link.
            if attempt["identity"] is not None and any(item["identity"] == attempt["identity"] for item in document["attempts"]):
                raise ValueError("Run already linked; preserve its original scheduler identity")
            document["attempts"].append(attempt)
        document["revision"] += 1
        document["updated"] = time.time()
        return S.write(fd, experiment_id + ".json", _validate(document))


def connect(project, experiment_id, run_id, *, title=""):
    """Create the experiment if needed and link a verified existing run once.

    The first Run Home connection is one atomic, reviewed project edit. Existing
    experiment titles, scheduler evidence, accounting, and retry/checkpoint
    ancestry are preserved. Concurrent connections merge through the same
    namespace lock; a second empty-index or overwrite step is unnecessary.
    """
    _id(experiment_id)
    _text(title, "Experiment title", empty=True)
    binding = projects.select_run(str(project), run_id)["binding"]
    attempt = _attempt(binding, None, None, "submission", None)
    with S.locked(project, "experiments") as fd:
        created = False
        try:
            document = _validate(S.read(fd, experiment_id + ".json"))
        except FileNotFoundError:
            now = time.time()
            document = dict(schema=SCHEMA, experiment_id=experiment_id, title=title,
                            revision=1, created=now, updated=now, attempts=[])
            created = True
        if document["experiment_id"] != experiment_id:
            raise ValueError("Experiment identity differs from the requested filename")
        prior = next((item for item in document["attempts"] if item["identity"] == attempt["identity"]), None)
        if prior:
            if prior["run"] != attempt["run"] or prior["source"] != "raven-inventory":
                raise ValueError("An existing attempt cannot be rebound to a changed inventory")
            for key in ("state", "resources", "provenance", "observed"):
                prior[key] = attempt[key]
        else:
            if len(document["attempts"]) >= MAX_ATTEMPTS:
                raise ValueError("Experiment attempt limit reached; start a related experiment")
            document["attempts"].append(attempt)
        if not created:
            document["revision"] += 1
        document["updated"] = time.time()
        return S.write(fd, experiment_id + ".json", _validate(document))


def summarize(document=None, *, project=None, experiment_id=None):
    """Sum allocation counters once per exact attempt, including failures.

    Known subtotals carry their coverage. Missing values stay unknown; a
    partial subtotal is never presented as a complete experiment total.
    """
    if document is None:
        if project is None or experiment_id is None:
            raise ValueError("Choose an experiment document or its project and identity")
        document = load(project, experiment_id)
    elif project is not None or experiment_id is not None:
        raise ValueError("Choose an experiment document or a project identity, not both")
    _validate(document)
    attempts = document["attempts"]
    totals = {}
    for metric in sorted(_METRICS):
        covered = [item["usage"] for item in attempts if item["usage"] is not None
                   and item["usage"]["scope"] == "allocation" and item["usage"].get(metric) is not None]
        totals[metric] = dict(known_sum=sum(item[metric] for item in covered) if covered else None,
                              covered_attempts=len(covered), total_attempts=len(attempts),
                              complete=bool(attempts) and len(covered) == len(attempts) and all(item["complete"] for item in covered),
                              unit="byte*s" if metric == "memory_byte_seconds" else "s")
    states = {}
    for item in attempts:
        states[item["state"]] = states.get(item["state"], 0) + 1
    return dict(experiment_id=document["experiment_id"], revision=document["revision"],
                attempts=len(attempts), states=states, totals=totals,
                accounting="Allocation summaries only; step counters are retained and excluded to avoid double counting.",
                external_observations=sum(item["source"] == "external-scheduler-observation" for item in attempts))
