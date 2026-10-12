"""Explicit, reviewed Slurm batches with durable intent and exact receipts.

The offline workflow planner remains review-only. This module prepares separate
concrete plans after upstream scheduler IDs exist; it never edits planner seals,
automatically retries, infers a successful submission, or executes a script.
"""
from __future__ import annotations

from contextlib import contextmanager
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import time
import uuid

from . import submission

MAX_NODES = 64
MAX_BYTES = 16 << 20
MAX_ATTEMPTS = 16
REVIEW_SCHEMA = "raven.execution-review/v1"
RECEIPT_SCHEMA = "raven.execution-receipt/v1"
_JOB_ID = re.compile(r"[1-9][0-9]{0,19}\Z")
_PLAN_FIELDS = ("schema", "script", "workdir", "argv", "command", "overrides",
                "script_sha256", "parameters", "inputs", "outputs", "resources",
                "directives", "issues", "valid")
_UNCERTAIN = {"launching", "unknown"}
_SCOPE_FIELDS = {"schema", "backend", "connection_host", "configured_host", "profile", "user", "uid", "cluster"}


def validate_scope(scope):
    """A connection identity uses existing configuration, never scheduler probes."""
    if not isinstance(scope, dict) or set(scope) != _SCOPE_FIELDS or scope.get("schema") != "raven.execution-scope/v1":
        raise ValueError("execution connection scope is unknown; prepare a fresh review in the original connection")
    for key in _SCOPE_FIELDS - {"uid"}:
        value = scope[key]
        if (not isinstance(value, str) or len(value) > 256 or value and not value.isprintable()
                or key in {"backend", "connection_host", "user"} and not value):
            raise ValueError("execution connection scope is unknown; prepare a fresh review in the original connection")
    if isinstance(scope["uid"], bool) or not isinstance(scope["uid"], int) or not 0 <= scope["uid"] <= 4294967295:
        raise ValueError("execution owner scope is unknown; prepare a fresh review in the original connection")
    return scope


def require_scope(review, expected_scope):
    """Reject a copied/legacy receipt before applying another cluster's job IDs."""
    validate_scope(expected_scope)
    original = validate_scope(review.get("scope"))
    if original != expected_scope:
        raise ValueError("execution connection, profile, or owner scope differs; use the original connection or prepare a fresh review")
    return original


def _encoded(value):
    submission._metadata_bound(value, depth_limit=32)
    data = json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False,
                      separators=(",", ":")).encode()
    if len(data) > MAX_BYTES:
        raise ValueError("execution metadata exceeds the 16 MiB budget")
    return data


def _review_digest(review):
    return hashlib.sha256(_encoded({key: value for key, value in review.items()
                                  if key != "review_id"})).hexdigest()


def validate_review(review):
    if not isinstance(review, dict) or review.get("schema") != REVIEW_SCHEMA:
        raise ValueError("not a supported execution review")
    if review.get("kind") not in {"workflow", "scaling"}:
        raise ValueError("execution kind must be workflow or scaling")
    if review.get("review_id") != _review_digest(review):
        raise ValueError("execution review changed; prepare and review again")
    if "scope" in review:
        validate_scope(review["scope"])
    nodes = review.get("nodes")
    if not isinstance(nodes, list) or not 1 <= len(nodes) <= MAX_NODES:
        raise ValueError(f"execution requires 1..{MAX_NODES} reviewed nodes")
    seen = set()
    for node in nodes:
        if not isinstance(node, dict) or not isinstance(node.get("id"), str) or node["id"] in seen:
            raise ValueError("execution nodes require unique IDs")
        deps = node.get("depends_on")
        if not isinstance(deps, list) or len(deps) != len(set(deps)) or any(dep not in seen for dep in deps):
            raise ValueError("execution dependencies must precede their children")
        plan = node.get("plan")
        if not isinstance(plan, dict) or not plan.get("valid") or plan.get("plan_id") != submission._digest(plan):
            raise ValueError("execution contains an invalid or changed reviewed plan")
        if review["kind"] == "workflow":
            if (plan.get("submittable") is not False or plan.get("workflow_orchestration") != "review_only"
                    or plan.get("workflow_node_id") != node["id"] or plan.get("symbolic_dependencies") != deps):
                raise ValueError("workflow execution needs unchanged sealed planner plans")
        elif deps or any(key in plan for key in ("workflow_node_id", "workflow_orchestration", "symbolic_dependencies")):
            raise ValueError("scaling execution cannot contain workflow dependencies")
        seen.add(node["id"])
    return review


def prepare_review(kind, recipe, *, workdir=None, source="", scope=None):
    """Prepare a bounded batch without contacting Slurm or writing state."""
    if kind == "workflow":
        from . import workflow
        # Reject the execution budget before inspecting any batch scripts.
        normalized = workflow._validate(recipe)
        if len(normalized["nodes"]) > MAX_NODES:
            raise ValueError(f"explicit execution is limited to {MAX_NODES} jobs; split the reviewed batch")
        plans = workflow.plans(recipe, workdir=workdir)
        nodes = [{"id": plan["workflow_node_id"], "label": plan["workflow_node_id"],
                  "depends_on": list(plan["symbolic_dependencies"]), "plan": plan} for plan in plans]
        metadata = {"name": recipe.get("name", "workflow")}
    elif kind == "scaling":
        from . import scaling
        _, _, _, repeats, _, _, _, configurations = scaling._recipe(recipe, workdir)
        if repeats * len(configurations) > MAX_NODES:
            raise ValueError(f"explicit execution is limited to {MAX_NODES} jobs; split the reviewed batch")
        planned = scaling.plan(recipe, workdir=workdir)
        if not planned.get("valid"):
            raise ValueError("; ".join(item["message"] for item in planned.get("issues", [])) or "invalid scaling recipe")
        nodes = [{"id": f"{run['configuration']}-r{run['repeat']}",
                  "label": f"{run['configuration']} / repeat {run['repeat']}",
                  "depends_on": [], "plan": run["submission"]} for run in planned["runs"]]
        metadata = {key: planned[key] for key in ("experiment_id", "mode", "baseline", "repeats")}
    else:
        raise ValueError("orchestrate workflow FILE | scaling FILE")
    if len(nodes) > MAX_NODES:
        raise ValueError(f"explicit execution is limited to {MAX_NODES} jobs; split the reviewed batch")
    review = {"schema": REVIEW_SCHEMA, "kind": kind, "source": str(source)[:4096],
              "nodes": nodes, "metadata": metadata}
    if scope is not None:
        review["scope"] = copy.deepcopy(validate_scope(scope))
    review["review_id"] = _review_digest(review)
    return validate_review(review)


def _directory(path):
    target = Path(path).expanduser().absolute()
    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(target, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    return target, fd


def _save(receipt, path):
    data = _encoded(receipt) + b"\n"
    path = Path(path).expanduser().absolute()
    directory, directory_fd = _directory(path.parent)
    temporary = ".execution-" + uuid.uuid4().hex + ".tmp"
    try:
        try:
            current = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
            if not stat.S_ISREG(current.st_mode):
                raise ValueError("receipt destination must be a regular file")
        except FileNotFoundError:
            pass
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                     0o600, dir_fd=directory_fd)
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        os.close(directory_fd)
    return str(directory / path.name)


@contextmanager
def _locked(path):
    """One writer per receipt, including separate Raven processes."""
    import fcntl
    path = Path(path).expanduser().absolute()
    _, directory_fd = _directory(path.parent)
    fd = None
    try:
        fd = os.open(path.name + ".lock", os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
                     0o600, dir_fd=directory_fd)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("execution lock must be a regular file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("another Raven process is updating this execution") from exc
        yield
    finally:
        if fd is not None:
            os.close(fd)
        os.close(directory_fd)


def _validate_receipt(receipt):
    if not isinstance(receipt, dict) or receipt.get("schema") != RECEIPT_SCHEMA:
        raise ValueError("not a supported execution receipt")
    review = validate_review(receipt.get("review"))
    if not isinstance(receipt.get("execution_id"), str) or not re.fullmatch(r"[0-9a-f]{32}", receipt["execution_id"]):
        raise ValueError("receipt requires its unique execution identity")
    nodes = receipt.get("nodes")
    if not isinstance(nodes, list) or len(nodes) != len(review["nodes"]):
        raise ValueError("receipt inventory differs from the reviewed batch")
    scheduler_ids = {}
    for node, planned in zip(nodes, review["nodes"]):
        if not isinstance(node, dict) or node.get("id") != planned["id"]:
            raise ValueError("receipt node order differs from its review")
        if node.get("state") not in {"planned", "launching", "unknown", "accepted", "rejected", "not_submitted",
                                      "pending", "running", "completed", "failed"}:
            raise ValueError("receipt contains an unknown node state")
        attempts = node.get("attempts")
        if not isinstance(attempts, list) or len(attempts) > MAX_ATTEMPTS:
            raise ValueError("receipt exceeds its retry history budget")
        for index, attempt in enumerate(attempts, 1):
            if (not isinstance(attempt, dict) or attempt.get("state") not in {"launching", "unknown", "accepted", "rejected", "not_submitted"}
                    or not isinstance(attempt.get("command"), str) or not attempt["command"].isprintable()
                    or len(attempt["command"]) > submission.MAX_COMMAND_BYTES
                    or not isinstance(attempt.get("workdir"), str) or not attempt["workdir"].isprintable()):
                raise ValueError("receipt contains malformed attempt evidence")
            expected_overrides = _execution_overrides(planned["plan"], planned, scheduler_ids,
                                                       execution_id=receipt["execution_id"], attempt=index)
            expected_argv = ["--parsable", *expected_overrides,
                             "--chdir=" + planned["plan"]["workdir"], planned["plan"]["script"]]
            if (attempt["command"] != shlex.join(["sbatch", *expected_argv])
                    or attempt["workdir"] != planned["plan"]["workdir"]
                    or attempt.get("script_sha256") != planned["plan"]["script_sha256"]):
                raise ValueError("attempt command, directory, hash, or receipt marker differs from the reviewed execution")
            if index < len(attempts) and attempt["state"] not in {"rejected", "not_submitted"}:
                raise ValueError("an accepted or uncertain attempt cannot be retried")
            attempt_job = attempt.get("job_id")
            if attempt_job is not None and (not isinstance(attempt_job, str) or not _JOB_ID.fullmatch(attempt_job)):
                raise ValueError("attempt requires a concrete scheduler ID")
            if attempt["state"] == "accepted" and attempt_job is None:
                raise ValueError("accepted attempt requires a scheduler receipt")
        if node["state"] == "planned" and attempts and attempts[-1]["state"] not in {"rejected", "not_submitted"}:
            raise ValueError("an attempted or uncertain node cannot become unattempted")
        job = node.get("job_id")
        if job is not None:
            if (not isinstance(job, str) or not _JOB_ID.fullmatch(job) or job in scheduler_ids.values()
                    or node["state"] not in {"accepted", "pending", "running", "completed", "failed"}
                    or not attempts or attempts[-1]["state"] != "accepted" or attempts[-1].get("job_id") != job):
                raise ValueError("receipt requires distinct concrete scheduler IDs")
            scheduler_ids[node["id"]] = job
        elif node["state"] in {"accepted", "pending", "running", "completed", "failed"}:
            raise ValueError("observed or accepted nodes need a concrete scheduler ID")
        if node["state"] in {"launching", "unknown", "rejected", "not_submitted"} and (not attempts or attempts[-1]["state"] != node["state"]):
            raise ValueError("node state differs from its durable attempt evidence")
    _encoded(receipt)
    return receipt


def load(path):
    from .planning_io import load_json
    return _validate_receipt(load_json(path, max_bytes=MAX_BYTES, max_depth=32))


def create_receipt(review, state_dir):
    """Reserve a private durable execution inventory before dispatching work."""
    validate_review(review)
    if not state_dir:
        raise ValueError("execution requires a persistent private state directory")
    path = str(Path(state_dir).expanduser().absolute() / "executions" / (uuid.uuid4().hex + ".json"))
    receipt = {"schema": RECEIPT_SCHEMA, "execution_id": Path(path).stem, "review": copy.deepcopy(review),
               "created": time.time(), "updated": time.time(), "status": "prepared",
               "nodes": [{"id": node["id"], "state": "planned", "job_id": None, "attempts": []}
                         for node in review["nodes"]]}
    with _locked(path):
        _save(receipt, path)
    return dict(receipt, receipt_path=path)


def _fresh_base(planned):
    reviewed = planned["plan"]
    fresh = submission.prepare(reviewed["script"], workdir=reviewed["workdir"], overrides=reviewed["overrides"],
                               parameters=reviewed["parameters"], inputs=reviewed["inputs"], outputs=reviewed["outputs"])
    if not fresh["valid"] or any(fresh[key] != reviewed[key] for key in _PLAN_FIELDS):
        raise ValueError(f"{planned['id']}: local preflight changed since review; prepare the batch again")
    return fresh


def _execution_overrides(base, planned, scheduler_ids, *, execution_id=None, attempt=1):
    """Pure reconstruction used for both submission and historical integrity."""
    dependencies = []
    for parent in planned["depends_on"]:
        jid = scheduler_ids.get(parent)
        if not isinstance(jid, str) or not _JOB_ID.fullmatch(jid):
            raise ValueError(f"{planned['id']}: missing concrete scheduler receipt for {parent}")
        dependencies.append(jid)
    overrides = list(base["overrides"])
    if dependencies:
        overrides.append("--dependency=afterok:" + ":".join(dependencies))
    if execution_id is not None:
        if not isinstance(execution_id, str) or not re.fullmatch(r"[0-9a-f]{32}", execution_id):
            raise ValueError("concrete execution needs a unique receipt identity")
        if isinstance(attempt, bool) or not isinstance(attempt, int) or not 1 <= attempt <= MAX_ATTEMPTS:
            raise ValueError("concrete execution needs a bounded attempt number")
        marker = "raven-execution:" + execution_id + ":" + planned["id"] + ":" + str(attempt)
        comment = base["resources"].get("comment", "")
        overrides.append("--comment=" + (comment + " " if comment else "") + marker)
    return overrides


def concrete_plan(planned, scheduler_ids, *, execution_id=None, attempt=1):
    """Construct a new plan from exact reviewed fields and real parent IDs."""
    base = _fresh_base(planned)
    if not planned["depends_on"] and execution_id is None:
        return base
    overrides = _execution_overrides(base, planned, scheduler_ids, execution_id=execution_id, attempt=attempt)
    return submission.prepare(base["script"], workdir=base["workdir"], overrides=overrides,
                              parameters=base["parameters"], inputs=base["inputs"], outputs=base["outputs"])


def _cancelled(cancel):
    return bool(cancel and cancel())


def execute(review, slurm, state_dir, *, confirmed=False, receipt_path=None, cancel=None, progress=None, expected_scope=None):
    """Submit each node at most once after confirmation; stop on uncertainty.

    Existing receipts resume only unattempted nodes. A durable launching intent
    precedes every scheduler call, so process death cannot permit a blind retry.
    Cancellation prevents later submissions; already accepted jobs keep running.
    """
    if confirmed is not True:
        raise ValueError("execution requires explicit confirmation of this reviewed batch")
    validate_review(review)
    if expected_scope is not None:
        require_scope(review, expected_scope)
    if not state_dir:
        raise ValueError("execution requires a persistent private state directory")
    path = str(Path(receipt_path).expanduser().absolute()) if receipt_path else str(
        Path(state_dir).expanduser().absolute() / "executions" / (uuid.uuid4().hex + ".json"))
    with _locked(path):
        if receipt_path:
            receipt = load(path)
            if receipt["review"] != review:
                raise ValueError("resume review differs from the stored receipt")
        else:
            receipt = {"schema": RECEIPT_SCHEMA, "execution_id": Path(path).stem, "review": copy.deepcopy(review),
                       "created": time.time(), "updated": time.time(), "status": "prepared",
                       "nodes": [{"id": node["id"], "state": "planned", "job_id": None, "attempts": []}
                                 for node in review["nodes"]]}
            _save(receipt, path)
        if any(node["state"] in _UNCERTAIN for node in receipt["nodes"]):
            raise ValueError("an outcome is unknown; verify its scheduler receipt before resuming")
        if any(node["state"] in {"rejected", "not_submitted"} for node in receipt["nodes"]):
            raise ValueError("a submission failed; explicitly review a node retry before resuming")
        by_id = {node["id"]: node for node in receipt["nodes"]}
        if any(node["state"] == "planned" and any(by_id[parent]["state"] == "failed" for parent in planned["depends_on"])
               for planned, node in zip(review["nodes"], receipt["nodes"])):
            raise ValueError("an observed upstream job failed; prepare a new reviewed batch before launching its descendants")
        if not any(node["state"] == "planned" for node in receipt["nodes"]):
            return dict(receipt, receipt_path=path)
        # Fail changed scripts before any new scheduler action, including resume.
        for planned, node in zip(review["nodes"], receipt["nodes"]):
            if _cancelled(cancel):
                receipt["status"], receipt["updated"] = "cancelled", time.time()
                _save(receipt, path)
                return dict(receipt, receipt_path=path)
            if node["state"] == "planned":
                _fresh_base(planned)
        scheduler_ids = {node["id"]: node["job_id"] for node in receipt["nodes"] if node["job_id"]}
        for planned, node in zip(review["nodes"], receipt["nodes"]):
            if node["state"] != "planned":
                continue
            if _cancelled(cancel):
                receipt["status"] = "cancelled"
                break
            if len(node["attempts"]) >= MAX_ATTEMPTS:
                raise ValueError("node reached the 16-attempt retry budget")
            plan = concrete_plan(planned, scheduler_ids, execution_id=receipt["execution_id"], attempt=len(node["attempts"]) + 1)
            if not plan["valid"]:
                raise ValueError("concrete scheduler preflight failed")
            attempt = {"started": time.time(), "state": "launching", "command": plan["command"],
                       "workdir": plan["workdir"], "script_sha256": plan["script_sha256"], "job_id": None}
            node["attempts"].append(attempt)
            node["state"] = "launching"
            receipt["status"], receipt["updated"] = "submitting", time.time()
            _save(receipt, path)
            if progress:
                progress({"node": node["id"], "status": "submitting", "receipt_path": path,
                          "position": 1 + sum(bool(item["job_id"]) for item in receipt["nodes"]), "total": len(receipt["nodes"])})
            if _cancelled(cancel):
                node["state"] = attempt["state"] = "not_submitted"
                attempt["output"] = "cancelled before scheduler call"
                receipt["status"] = "cancelled"
                _save(receipt, path)
                break
            result = submission.submit(plan, slurm, passport_directory=str(Path(path).parent / "passports"))
            # Persist only bounded receipt fields; immutable passports live separately.
            attempt.update({"ended": time.time(), "state": result["state"], "job_id": result.get("job_id"),
                            "output": str(result.get("error") or result.get("output") or "")[:16384],
                            "passport_path": result.get("passport_path")})
            jid = result.get("job_id")
            if result.get("ok") and (not isinstance(jid, str) or not _JOB_ID.fullmatch(jid) or jid in scheduler_ids.values()):
                attempt["state"] = "unknown"
                attempt["output"] = "scheduler receipt is invalid or duplicates another node; inspect the scheduler"
                jid = None
            node["state"] = attempt["state"]
            node["job_id"] = jid if node["state"] == "accepted" else None
            receipt["updated"] = time.time()
            if node["state"] != "accepted":
                receipt["status"] = "blocked"
                _save(receipt, path)
                break
            scheduler_ids[node["id"]] = jid
            _save(receipt, path)
        else:
            receipt["status"] = "submitted"
        receipt["updated"] = time.time()
        _save(receipt, path)
    return dict(receipt, receipt_path=path)


def retry(receipt_path, node_id, *, confirmed=False, expected_review_id=None, expected_scope=None):
    """Allow a reviewed retry only when the scheduler definitively rejected it."""
    if confirmed is not True:
        raise ValueError("retry requires explicit confirmation")
    with _locked(receipt_path):
        receipt = load(receipt_path)
        if expected_scope is not None:
            require_scope(receipt["review"], expected_scope)
        if expected_review_id is not None and receipt["review"]["review_id"] != expected_review_id:
            raise ValueError("execution review changed after confirmation was displayed")
        node = next((item for item in receipt["nodes"] if item["id"] == node_id), None)
        if node is None or node["state"] not in {"rejected", "not_submitted"}:
            raise ValueError("only definitively rejected or not-submitted nodes can be retried; unknown outcomes need verified recovery")
        if len(node["attempts"]) >= MAX_ATTEMPTS:
            raise ValueError("node reached the 16-attempt retry budget")
        descendants = {node_id}
        for planned, current in zip(receipt["review"]["nodes"], receipt["nodes"]):
            if any(parent in descendants for parent in planned["depends_on"]):
                descendants.add(planned["id"])
                if current["job_id"]:
                    raise ValueError("retry would change dependencies of an already submitted descendant")
        planned = next(item for item in receipt["review"]["nodes"] if item["id"] == node_id)
        _fresh_base(planned)
        node["state"] = "planned"
        receipt["status"], receipt["updated"] = "prepared", time.time()
        _save(receipt, receipt_path)
    return dict(receipt, receipt_path=str(receipt_path))


def recover(receipt_path, node_id, job_id, slurm, *, confirmed=False, expected_review_id=None, expected_scope=None):
    """Bind an unknown intent only to an exact accounting submit line and cwd."""
    if confirmed is not True or not isinstance(job_id, str) or not _JOB_ID.fullmatch(job_id):
        raise ValueError("recovery requires confirmation and a concrete scheduler ID")
    with _locked(receipt_path):
        receipt = load(receipt_path)
        if expected_scope is not None:
            require_scope(receipt["review"], expected_scope)
        if expected_review_id is not None and receipt["review"]["review_id"] != expected_review_id:
            raise ValueError("execution review changed after confirmation was displayed")
        node = next((item for item in receipt["nodes"] if item["id"] == node_id), None)
        if node is None or node["state"] not in _UNCERTAIN or not node["attempts"]:
            raise ValueError("only an unknown or interrupted launching intent can be recovered")
        if any(item.get("job_id") == job_id for item in receipt["nodes"]):
            raise ValueError("scheduler ID is already assigned to another node")
        info = slurm.submit_info(job_id)
        attempt = node["attempts"][-1]
        try:
            exact = (info.get("id") == job_id and info.get("workdir") == attempt["workdir"]
                     and shlex.split(info.get("submit_line", "")) == shlex.split(attempt["command"]))
        except (ValueError, AttributeError):
            exact = False
        if not exact:
            raise ValueError("accounting does not verify the exact reviewed submit command and working directory; recovery refused")
        attempt["recovery"] = {"verified": time.time(), "job_id": job_id, "submit_line": info["submit_line"]}
        attempt["state"], attempt["job_id"] = "accepted", job_id
        node["state"], node["job_id"] = "accepted", job_id
        receipt["status"], receipt["updated"] = "prepared", time.time()
        _save(receipt, receipt_path)
    return dict(receipt, receipt_path=str(receipt_path))


def collect(receipt_path, snapshot, *, expected_scope=None):
    """Record observed scheduler results and measured scaling data, never estimates."""
    from .model import secs, terminal_state, TERMINAL_STATES
    with _locked(receipt_path):
        receipt = load(receipt_path)
        if expected_scope is not None:
            require_scope(receipt["review"], expected_scope)
        observed = {str(getattr(job, "id", "")): job for job in snapshot.get("finished", [])}
        observed.update({str(getattr(job, "id", "")): job for job in snapshot.get("jobs", [])})
        records = []
        for planned, node in zip(receipt["review"]["nodes"], receipt["nodes"]):
            job = observed.get(node["job_id"])
            if job is None:
                continue
            state = terminal_state(getattr(job, "state", ""))
            if state in TERMINAL_STATES:
                node["state"] = "completed" if state == "COMPLETED" else "failed"
            elif state in {"RUNNING", "R", "COMPLETING", "CG"}:
                node["state"] = "running"
            elif state in {"PENDING", "PD", "CONFIGURING", "CF"}:
                node["state"] = "pending"
            else:
                continue
            observation = {"job_id": node["job_id"], "state": state, "observed": time.time(),
                           "elapsed": getattr(job, "elapsed", ""), "exit": getattr(job, "exit", ""),
                           "cpus": getattr(job, "cpus", None), "gpus": getattr(job, "gpus", None),
                           "start": getattr(job, "start", ""), "end": getattr(job, "end", "")}
            previous = node.get("observation")
            if previous and any(previous.get(key) != observation.get(key) for key in ("state", "start", "end")):
                node["observation_history"] = (node.get("observation_history", []) + [previous])[-8:]
            if state in TERMINAL_STATES and state != "COMPLETED":
                node["censored"] = "a failed accounting attempt was observed"
            node["observation"] = observation
        for planned, node in zip(receipt["review"]["nodes"], receipt["nodes"]):
            observation = node.get("observation")
            metadata = planned["plan"].get("parameters", {}).get("scaling")
            if metadata:
                observation = observation or {}
                records.append({"job_id": node["job_id"], "state": "CENSORED" if node.get("censored") else observation.get("state", node["state"].upper()),
                                "observed_state": observation.get("state"),
                                "runtime_seconds": secs(observation.get("elapsed", "")),
                                "script_sha256": planned["plan"]["script_sha256"],
                                **{key: metadata[key] for key in ("workers", "repeat", "parameters", "problem_size")},
                                "cpus": observation.get("cpus"), "gpus": observation.get("gpus")})
        receipt["collection"] = {"observed": time.time(), "records": records,
                                 "missing": [node["id"] for node in receipt["nodes"] if node.get("job_id") and not node.get("observation")]}
        if receipt["review"]["kind"] == "scaling":
            from .scaling import analyze
            metadata = receipt["review"]["metadata"]
            receipt["collection"]["analysis"] = analyze(records, mode=metadata["mode"], baseline=metadata["baseline"])
        if all(node["state"] in {"completed", "failed"} for node in receipt["nodes"]):
            receipt["status"] = "finished"
        receipt["updated"] = time.time()
        _save(receipt, receipt_path)
    return dict(receipt, receipt_path=str(receipt_path))
