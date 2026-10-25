"""Evidence-bound workflow recovery with explicit native scheduler review.

Node failure, wall-time expiry, preemption and application failure are distinct
events. A hash proves retained bytes, not application validity; checkpoint and
output reuse also require explicit validation and compatibility evidence.
"""
from __future__ import annotations

import copy
import hashlib
import os
from pathlib import Path
import re
import shlex
import stat
import time
import uuid

from .. import science_files
from ..runtime_contracts import object_, sequence, text
from . import _storage as storage

SCHEMA = "raven.workflow-recovery/v1"
PLAN = "raven.workflow-recovery-plan/v1"
MAX_LOCAL_INSPECTION = 8 << 20
_TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "NODE_FAIL", "OUT_OF_MEMORY", "PREEMPTED", "EVICTED", "REMOVED", "SUCCEEDED", "DONE", "EXIT", "C", "F", "X"}
_OUTCOMES = {
    "node_failure": {"NODE_FAIL", "BOOT_FAIL"},
    "timeout": {"TIMEOUT", "DEADLINE"},
    "preemption": {"PREEMPTED", "EVICTED"},
    "out_of_memory": {"OUT_OF_MEMORY", "OOMKILLED"},
    "application_failure": {"FAILED", "EXIT", "F"},
    "cancelled": {"CANCELLED", "REMOVED", "X"},
}


class _Inspection:
    """Aggregate small local review budget; larger artifacts use Heavy campaigns."""
    def __init__(self, cancel=None):
        self.remaining = MAX_LOCAL_INSPECTION
        self.deadline = time.monotonic() + 1.
        self.cancel = cancel

    def snapshot(self, path, *, limit, keep=False):
        science_files.cancelled(self.cancel)
        if self.remaining <= 0 or time.monotonic() > self.deadline:
            raise ValueError("Local recovery inspection budget reached (8 MiB / 1 second); use the existing Heavy checkpoint campaign for large artifacts")
        cap = min(limit, self.remaining)
        try:
            record, raw = science_files.snapshot(path, limit=cap, keep=keep, cancel=self.cancel)
        except ValueError as exc:
            if "at most" in str(exc) or "size limit" in str(exc):
                raise ValueError("Local recovery artifact exceeds the remaining 8 MiB inspection budget; use the existing Heavy checkpoint campaign for large artifacts") from exc
            raise
        self.remaining -= record["size"]
        if time.monotonic() > self.deadline:
            raise ValueError("Local recovery inspection exceeded its 1 second budget; use the existing Heavy checkpoint campaign")
        return record, raw

    def verify(self, record):
        actual, _ = self.snapshot(record["path"], limit=record["size"])
        if any(actual[key] != record[key] for key in ("sha256", "size", "signature")):
            raise ValueError("Reviewed recovery source changed: " + record["path"])


def _digest(value):
    return hashlib.sha256(storage.encoded(value)).hexdigest()


def _identity(value):
    from ..schedulers.core import validate_identity
    return validate_identity(value)


def _artifact(item, source, *, checkpoint=False, inspection=None):
    object_(item, {"path", "sha256", "complete", "source_identity", "validation"},
            {"compatibility"}, "checkpoint" if checkpoint else "output")
    if item["complete"] is not True or _identity(item["source_identity"]) != source:
        raise ValueError("Reusable bytes require atomic completion and the exact source attempt")
    expected = item["sha256"]
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("Reusable bytes require an exact SHA-256")
    validation = object_(item["validation"], {"passed", "method", "evidence"}, name="application validation")
    if validation["passed"] is not True:
        raise ValueError("Application output validation must pass before reuse")
    text(validation["method"], "validation method", 512)
    text(validation["evidence"], "validation evidence", 4096)
    record, _ = inspection.snapshot(item["path"], limit=1 << 30)
    if record["sha256"] != expected:
        raise ValueError("Reusable artifact hash differs from the validated bytes")
    if checkpoint:
        compatibility = object_(item.get("compatibility"), {"expected", "actual"}, name="checkpoint compatibility")
        for kind in ("expected", "actual"):
            object_(compatibility[kind], {"application", "format", "runtime"}, name="checkpoint compatibility " + kind)
            for key, value in compatibility[kind].items():
                text(value, "checkpoint " + key, 256)
        if compatibility["actual"] != compatibility["expected"]:
            raise ValueError("Application, format or runtime checkpoint compatibility differs")
    return dict(record=record, validation=copy.deepcopy(validation), source_identity=source,
                compatibility=copy.deepcopy(item.get("compatibility")))


def workflow(document):
    """Plan a bounded dependency DAG; missing output validation never means success.

    This is an explicit execution graph, not a scheduler replacement. Native
    dependency options remain in each reviewed submission; this report alone
    never submits or drops a dependency.
    """
    object_(document, {"nodes"}, name="recovery workflow")
    nodes, descendants = {}, {}
    for row in sequence(document["nodes"], "workflow nodes", 4096, 1):
        object_(row, {"id", "dependencies", "state", "outputs_validated"}, name="workflow node")
        key = storage.name(row["id"])
        if key in nodes or type(row["outputs_validated"]) is not bool:
            raise ValueError("Workflow needs unique nodes and explicit output validation")
        deps = sequence(row["dependencies"], "dependencies", 256)
        for dep in deps:
            storage.name(dep)
            descendants.setdefault(dep, []).append(key)
        if key in deps or len(set(deps)) != len(deps):
            raise ValueError("Workflow cannot have self or duplicate dependencies")
        text(row["state"], "workflow state", 80)
        nodes[key] = copy.deepcopy(row)
    if set(descendants) - nodes.keys():
        raise ValueError("Workflow dependency is missing")
    indegree = {key: len(row["dependencies"]) for key, row in nodes.items()}
    queue = [key for key, degree in indegree.items() if not degree]
    order, results = [], {}
    for key in queue:
        order.append(key)
        row = nodes[key]
        state = row["state"].upper()
        parents = [results[dep]["action"] for dep in row["dependencies"]]
        if any(action in {"blocked", "running"} for action in parents):
            action, reason = "blocked", "A dependency is unresolved or still running"
        elif state in {"COMPLETED", "SUCCEEDED", "DONE"} and row["outputs_validated"] and all(action == "reuse" for action in parents):
            action, reason = "reuse", "Completed outputs explicitly validated; dependencies retain verified reuse"
        elif state in {"RUNNING", "PENDING", "QUEUED", "R", "PD", "Q", "H"}:
            action, reason = "running", "Existing active work keeps its native ownership; do not duplicate"
        else:
            action, reason = "review-rerun", "Failed, missing validation or invalidated dependency; review a new submission"
        results[key] = dict(action=action, reason=reason)
        for child in descendants.get(key, ()):
            indegree[child] -= 1
            if not indegree[child]:
                queue.append(child)
    if len(order) != len(nodes):
        raise ValueError("Workflow dependency graph must be acyclic")
    return dict(schema="raven.workflow-recovery-dag/v1", order=order, nodes=results,
                qualification="Declared dependency and validation evidence; native dependencies are not changed by analysis")


def _resume_script(submission, checkpoint, project, inspection):
    """Reuse the existing checkpoint campaign insertion convention and guard."""
    from ..shell_environment import with_environment as _with_environment
    _, raw = inspection.snapshot(submission["script"], limit=1 << 20, keep=True)
    script = raw.decode("utf-8")
    if not re.match(r"^#!\s*(?:/usr/bin/env\s+)?(?:[^\s]*/)?(?:bash|sh|dash)(?:\s|$)", script):
        raise ValueError("Checkpoint resume requires a Bash/POSIX-shell script")
    if "RAVEN_CHECKPOINT" not in script:
        raise ValueError("Resume script must explicitly consume RAVEN_CHECKPOINT")
    if project is None:
        raise ValueError("Checkpoint resume preparation requires its project to stage a reviewed guard")
    verifier = (
        "import hashlib,os,stat,sys\n"
        "p,expected=sys.argv[1:]\n"
        "fd=os.open(p,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|os.O_NONBLOCK)\n"
        "with os.fdopen(fd,'rb') as f:\n"
        " before=os.fstat(f.fileno()); h=hashlib.sha256(); total=0\n"
        " if not stat.S_ISREG(before.st_mode) or before.st_size>1073741824: sys.exit('invalid checkpoint')\n"
        " for block in iter(lambda:f.read(1048576),b''):\n"
        "  total+=len(block)\n"
        "  if total>1073741824: sys.exit('checkpoint exceeds limit')\n"
        "  h.update(block)\n"
        " after=os.fstat(f.fileno()); named=os.stat(p,follow_symlinks=False)\n"
        " sig=lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns)\n"
        " if sig(before)!=sig(after) or sig(after)!=sig(named) or h.hexdigest()!=expected: sys.exit('checkpoint changed since review')\n"
    )
    environment = {"RAVEN_CHECKPOINT": checkpoint["record"]["path"],
                   "RAVEN_CHECKPOINT_SHA256": checkpoint["record"]["sha256"]}
    guard = "python3 -c " + shlex.quote(verifier) + ' "$RAVEN_CHECKPOINT" "$RAVEN_CHECKPOINT_SHA256" || exit 65\n'
    generated = _with_environment(script, environment, guard).encode("utf-8")
    filename = hashlib.sha256(generated).hexdigest() + ".sh"
    science_files.cancelled(inspection.cancel)
    with storage.locked(project, "recovery-scripts") as fd:
        temporary = "staging-" + uuid.uuid4().hex
        out = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        try:
            with os.fdopen(out, "wb") as stream:
                stream.write(generated)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, filename, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
            except FileExistsError:
                existing = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                with os.fdopen(existing, "rb") as stream:
                    info = os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or stream.read(len(generated) + 1) != generated:
                        raise ValueError("Staged checkpoint wrapper changed or has unsafe ownership")
            os.fsync(fd)
        finally:
            os.unlink(temporary, dir_fd=fd)
    # Derive the canonical project path through the same rooted project helper.
    from ..projects import _root
    root_path, root_fd = _root(str(project))
    os.close(root_fd)
    path = str(Path(root_path) / ".raven" / "engineering" / "recovery-scripts" / filename)
    record, _ = inspection.snapshot(path, limit=2 << 20)
    return dict(submission, script=path), record


def prepare(document, scheduler=None, *, project=None, cancel=None):
    return _prepare(document, scheduler, project=project, cancel=cancel, inspection=_Inspection(cancel))


def _prepare(document, scheduler=None, *, project=None, cancel=None, inspection):
    science_files.cancelled(cancel)
    storage.encoded(document)
    object_(document, {"schema", "id", "source", "failure", "submission", "mode"},
            {"outputs", "checkpoint", "workflow", "correction"}, "workflow recovery")
    if document["schema"] != SCHEMA or document["mode"] not in {"restart", "resume"}:
        raise ValueError("Use workflow-recovery/v1 with restart or resume mode")
    ident = storage.name(document["id"])
    source = object_(document["source"], {"identity", "owner", "state"}, name="recovery source")
    identity = _identity(source["identity"])
    text(source["owner"], "source owner", 256)
    state = text(source["state"], "source final state", 80).upper()
    failure = object_(document["failure"], {"kind", "evidence"}, name="failure evidence")
    if failure["kind"] not in _OUTCOMES:
        raise ValueError("Separate node_failure, timeout, preemption, out_of_memory, application_failure and cancelled outcomes")
    text(failure["evidence"], "failure evidence", 4096)
    if state not in _OUTCOMES[failure["kind"]]:
        raise ValueError("Native source outcome does not support the selected failure class; preserve uncertainty")
    correction = document.get("correction")
    if failure["kind"] in {"timeout", "out_of_memory", "application_failure", "cancelled"}:
        text(correction, "reviewed resource, application or cancellation-policy correction", 4096)
    if document["mode"] == "resume" and "checkpoint" not in document:
        raise ValueError("Resume requires a complete application-compatible checkpoint")
    if document["mode"] == "restart" and "checkpoint" in document:
        raise ValueError("Choose resume to consume a checkpoint; restart starts from declared inputs")
    submission = copy.deepcopy(document["submission"])
    if "script" not in submission:
        # Kubernetes can recover a reviewed explicit Job manifest, but cannot
        # guarantee application checkpoint consumption merely from a file.
        if document["mode"] == "resume":
            raise ValueError("Manifest resume needs an application-specific checkpoint adapter; use a reviewed mounted checkpoint command")
        sources = []
    else:
        record, _ = inspection.snapshot(submission["script"], limit=1 << 20)
        sources = [record]
    outputs = [_artifact(item, identity, inspection=inspection) for item in sequence(document.get("outputs", []), "validated outputs", 64)]
    checkpoint = _artifact(document["checkpoint"], identity, checkpoint=True, inspection=inspection) if "checkpoint" in document else None
    for item in outputs + ([checkpoint] if checkpoint else []):
        sources.append(item["record"])
    dag = workflow(document["workflow"]) if "workflow" in document else None
    recommendation = {
        "node_failure": "Infrastructure interruption: review placement/site health; retry only this verified attempt's unfinished work.",
        "timeout": "Wall-time expiry: review measured runtime, checkpoint cost and a larger valid time request; this is not a node-failure hazard.",
        "preemption": "Policy interruption: review preemption notice and application-safe checkpoint; do not infer hardware failure.",
        "out_of_memory": "Memory exhaustion: validate peak evidence and correct application/request limits before retrying.",
        "application_failure": "General failure is not proof of an application defect; inspect supplied failure evidence and the explicit correction.",
        "cancelled": "Cancellation may be intentional; the supplied policy correction authorizes a separately reviewed new job.",
    }[failure["kind"]]
    plan = dict(schema=PLAN, id=ident, document=copy.deepcopy(document), source=copy.deepcopy(source),
                submission=submission, sources=sources, reusable_outputs=outputs, checkpoint=checkpoint,
                workflow=dag, recommendation=recommendation,
                feedback=["Original job ownership, logs and history remain unchanged.",
                          "Only explicitly validated exact-attempt artifacts are reusable; hashes alone do not validate science.",
                          "A retry is a new charged submission, not a replay of an uncertain previous action."])
    if checkpoint:
        plan["checkpoint_environment"] = {"RAVEN_CHECKPOINT": checkpoint["record"]["path"],
                                            "RAVEN_CHECKPOINT_SHA256": checkpoint["record"]["sha256"]}
        submission, wrapper = _resume_script(submission, checkpoint, project, inspection)
        plan["submission"] = submission
        plan["sources"].append(wrapper)
        plan["feedback"].append("The staged wrapper validates the completed checkpoint again on compute before the application starts.")
    if scheduler is not None:
        current = scheduler.detail(identity)
        _source_matches(source, current)
        plan["native_plan"] = scheduler.review("submit", submission=submission)
        _script_binding(plan)
    plan["digest"] = _digest(plan)
    return plan


def _source_matches(source, current):
    if current.get("identity") != source["identity"] or current.get("user") != source["owner"]:
        raise ValueError("Recovery source attempt or verified owner changed")
    native_state = str(current.get("native_state") or current.get("state", "")).upper()
    if native_state != source["state"].upper():
        raise ValueError("Recovery source state changed; review fresh scheduler evidence")
    if native_state not in _TERMINAL | {"BOOT_FAIL", "DEADLINE", "OOMKILLED"}:
        raise ValueError("Recovery source is still active or unresolved; do not duplicate it")


def _script_binding(plan):
    """The native host must inspect the same reviewed bytes as this project."""
    script = plan["submission"].get("script")
    if script is None:
        return
    records = [record for record in plan["sources"] if record["path"] == script]
    if not records or any(record["sha256"] != records[0]["sha256"] for record in records):
        raise ValueError("Recovery script does not have one exact reviewed source")
    if plan["native_plan"].get("script_sha256") != records[0]["sha256"]:
        raise ValueError("Native recovery script differs from the locally reviewed bytes; use the same verified shared script")


def _fresh_source(source, scheduler):
    # The shared native adapter deliberately caches routine reads. A reviewed
    # recovery boundary must obtain current ownership rather than that cache.
    current = getattr(scheduler, "fresh_detail", scheduler.detail)(source["identity"])
    _source_matches(source, current)
    return current


def apply(plan, scheduler, *, project, review_digest, cancel=None):
    science_files.cancelled(cancel)
    if not isinstance(plan, dict) or plan.get("schema") != PLAN or plan.get("digest") != review_digest:
        raise ValueError("Review the exact recovery plan before submitting")
    if _digest({k: v for k, v in plan.items() if k != "digest"}) != review_digest:
        raise ValueError("Reviewed recovery plan changed")
    native = plan.get("native_plan")
    if (not isinstance(native, dict) or native.get("action") != "submit"
            or native.get("submission") != plan.get("submission")):
        raise ValueError("Native recovery action or submission differs from the reviewed restart")
    _script_binding(plan)
    inspection = _Inspection(cancel)
    fresh = _prepare(plan["document"], project=project, cancel=cancel, inspection=inspection)
    for key in ("id", "source", "submission", "sources", "checkpoint", "reusable_outputs", "workflow"):
        if fresh.get(key) != plan.get(key):
            raise ValueError("Recovery sources or validated outputs changed")
    for record in plan["sources"]:
        inspection.verify(record)
    _fresh_source(plan["source"], scheduler)
    with storage.locked(project, "recovery") as fd:
        name = plan["id"] + ".json"
        try:
            existing = storage.read(fd, name)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            raise ValueError("This recovery already has an issuance receipt; inspect it before a separately reviewed attempt")
        journal = dict(schema="raven.workflow-recovery-state/v1", id=plan["id"], digest=review_digest,
                       source=plan["source"], state="issuing", claimed_at=time.time())
        storage.write(fd, name, journal)
        try:
            receipt = scheduler.apply(native, native["digest"])
        except BaseException as exc:
            journal.update(state="unknown", error=f"{type(exc).__name__}: {exc}")
            storage.write(fd, name, journal)
            raise
        journal.update(state="accepted" if receipt.get("status") == "accepted" else "unknown", receipt=receipt)
        storage.write(fd, name, journal)
        return copy.deepcopy(journal)


def status(project, recovery_id):
    with storage.project_store(project, "recovery") as fd:
        return storage.read(fd, storage.name(recovery_id) + ".json")
