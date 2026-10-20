"""Durable, exact-attempt completion exports using the shared data exporter.

There is no daemon and no scheduler query here. The job supervisor, an existing
dashboard worker, or an explicit reconciliation command supplies worker time.
An advisory lock belongs to one request, never the whole project during export.
Frozen plans make recovery after publication idempotent across processes.
"""
from __future__ import annotations

from contextlib import contextmanager
import bisect
import copy
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time
import uuid

from . import projects, reporting as R

SCHEMA = "raven.auto-export/v1"
MAX_PENDING = 1024
MAX_PER_RUN = 16
MAX_STATE_BYTES = 4 << 20
MAX_ATTEMPTS = 3
_KEY = re.compile(r"[0-9a-f]{64}\Z")
_FINAL = R.TERMINAL - {"UNKNOWN"}


def _json_bytes(value):
    return R._encoded(value, MAX_STATE_BYTES)


def _read(parent, name, default=None):
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    except FileNotFoundError:
        return default
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_STATE_BYTES:
            raise ValueError("automatic export metadata is not a bounded regular file")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(MAX_STATE_BYTES + 1)
        after = os.fstat(fd)
        named = os.stat(name, dir_fd=parent, follow_symlinks=False)
        signature = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if len(raw) > MAX_STATE_BYTES or signature(before) != signature(after) or signature(after) != signature(named):
            raise ValueError("automatic export metadata changed during read")
        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ValueError("duplicate automatic export metadata key")
                result[key] = value
            return result
        value = json.loads(raw, object_pairs_hook=pairs)
        _json_bytes(value)
        return value
    finally:
        os.close(fd)


def _write(parent, name, value, *, exclusive=False):
    raw = _json_bytes(value)
    temporary = "." + uuid.uuid4().hex + ".tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive:
            os.link(temporary, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
        else:
            os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
    finally:
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass


def _child(parent, name, create=False):
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
        except FileExistsError:
            pass
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)


@contextmanager
def _scope(project, create=False):
    root, root_fd = projects._root(str(project))
    opened = [root_fd]
    try:
        raven_fd = _child(root_fd, ".raven", create)
        opened.append(raven_fd)
        queue_fd = _child(raven_fd, "auto-exports", create)
        opened.append(queue_fd)
        children = {}
        for name in ("pending", "done", "claims", "launches"):
            children[name] = _child(queue_fd, name, create)
            opened.append(children[name])
        yield root, root_fd, queue_fd, children
    finally:
        for fd in reversed(opened):
            os.close(fd)


@contextmanager
def _claim(parent, name, *, wait=False):
    fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=parent)
    locked = False
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("automatic export claim must be a regular file")
        until = time.monotonic() + (2 if wait else 0)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except BlockingIOError:
                if time.monotonic() >= until:
                    break
                time.sleep(.01)
        yield locked
    finally:
        os.close(fd)


def _inventory(root_fd, run_id):
    if not isinstance(run_id, str) or not projects._IDENT.fullmatch(run_id):
        raise ValueError("an exact run ID is required for automatic export")
    run_fd = projects._directory(root_fd, "runs/" + run_id)
    try:
        value = projects.validate_inventory(_read(run_fd, "run.json"))
        if value["run_id"] != run_id:
            raise ValueError("automatic export run identity mismatch")
        return value
    finally:
        os.close(run_fd)


def _identity(project, inventory):
    launch = inventory.get("metadata", {}).get("raven_launch", {})
    return {"project": project, "run_id": inventory["run_id"], "attempt": inventory["attempt"],
            "job_id": inventory.get("job_id"), "cluster": launch.get("cluster", ""),
            "submission_token": launch.get("submission_token", ""),
            "script_sha256": inventory.get("provenance", {}).get("script_sha256", "")}


def _request(project, run_id, request):
    if not isinstance(request, dict):
        raise ValueError("automatic export settings must be an object")
    value = copy.deepcopy(request)
    # Reusable preferences deliberately have no transient subject. Capturing an
    # already-bound UI request is allowed only for this exact subject.
    if value.get("project") not in (None, project):
        raise ValueError("automatic export settings belong to another project")
    if value.get("runs") not in (None, [], [run_id]):
        raise ValueError("capture each automatic export attempt separately")
    if value.get("attempts"):
        if value["attempts"] != [{"project": project, "run_id": run_id}]:
            raise ValueError("automatic export settings contain another attempt")
        value.pop("attempts")
    value.update(project=project, runs=[run_id])
    value.setdefault("include", ["all"])
    value.setdefault("layout", "category")
    value.setdefault("destination", {"mode": "project", "path": "", "host": ""})
    _json_bytes(value)
    return value


def _key(identity, request):
    return hashlib.sha256(_json_bytes({"identity": identity, "request": request})).hexdigest()


def _public(state):
    return {key: copy.deepcopy(value) for key, value in state.items()
            if key not in {"plan", "request"}}


def pending_status(project):
    """Read the bounded pending index without creating files or doing work."""
    try:
        with _scope(project) as (_, _, _, folders):
            values = []
            with os.scandir(folders["pending"]) as entries:
                for examined, entry in enumerate(entries):
                    if examined >= projects.MAX_DIRECTORY_ENTRIES:
                        raise ValueError("automatic export pending directory exceeds inspection budget")
                    if not entry.name.endswith(".json") or not _KEY.fullmatch(entry.name[:-5]):
                        continue
                    if len(values) >= MAX_PENDING:
                        raise ValueError("automatic export queue exceeds its bounded index")
                    state = _read(folders["pending"], entry.name)
                    if state is not None:
                        values.append(_public(_validate(state, entry.name[:-5])))
            return sorted(values, key=lambda item: (item["created_at"], item["key"]))
    except FileNotFoundError:
        return []


def _queue_count(folder, pattern):
    count = 0
    with os.scandir(folder) as entries:
        for examined, entry in enumerate(entries):
            if examined >= projects.MAX_DIRECTORY_ENTRIES:
                raise ValueError("automatic export directory exceeds its inspection budget")
            if re.fullmatch(pattern, entry.name):
                count += 1
                if count >= MAX_PENDING:
                    return count
    return count


def capture_launch(project, token, request):
    """Index a captured request before submission; never infer acceptance.

    The existing launch intent is the authority for any eventual job/run ID.
    This small recovery index avoids a scan of historical launch directories.
    """
    if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{32}", token):
        raise ValueError("automatic export requires the exact submission token")
    if not isinstance(request, dict):
        raise ValueError("automatic export settings must be an object")
    with _scope(project, create=True) as (root, _, queue_fd, folders):
        value = {"schema": "raven.launch-auto-export/v1", "project": root,
                 "token": token, "request": copy.deepcopy(request), "created_at": time.time()}
        with _claim(queue_fd, "capture.lock", wait=True) as owned:
            if not owned:
                raise ValueError("automatic export settings are busy; retry capture")
            previous = _read(folders["launches"], token + ".json")
            if previous:
                if previous.get("request") != request or previous.get("project") != root:
                    raise ValueError("launch automatic export request changed")
                return previous
            if _queue_count(folders["launches"], r"[0-9a-f]{32}\.json") >= MAX_PENDING:
                raise ValueError("launch automatic export queue is full; reconcile pending submissions")
            _write(folders["launches"], token + ".json", value, exclusive=True)
        return value


def _rotated(folder, names):
    """Durable round-robin position prevents active prefixes starving work."""
    names = sorted(names)
    cursor = _read(folder, ".cursor.json", {})
    last = cursor.get("last", "") if isinstance(cursor, dict) else ""
    if not isinstance(last, str):
        raise ValueError("invalid automatic export queue cursor")
    offset = bisect.bisect_right(names, last)
    return names[offset:] + names[:offset]


def _launch_inventory(project, root_fd, folders, name):
    value = _read(folders["launches"], name)
    if value is None:
        return None, None
    if (value.get("schema") != "raven.launch-auto-export/v1" or value.get("project") != project
            or value.get("token") != name[:-5] or not isinstance(value.get("request"), dict)):
        raise ValueError("invalid launch automatic export request")
    try:
        launches_fd = projects._directory(root_fd, ".raven/launches")
    except FileNotFoundError:
        return value, None
    try:
        launch = _read(launches_fd, name)
    finally:
        os.close(launches_fd)
    if not isinstance(launch, dict) or launch.get("schema") != "raven.launch/v1" or launch.get("token") != value["token"]:
        return value, None
    run_id = launch.get("run_id") or launch.get("observed_run_id")
    if not run_id:
        return value, None
    try:
        inventory = _inventory(root_fd, run_id)
    except FileNotFoundError:
        return value, None
    if _identity(project, inventory)["submission_token"] != value["token"]:
        raise ValueError("launch recovery found a different execution attempt")
    return value, inventory


def launch_candidates(project):
    """Read unresolved launch requests, including unknown scheduler acceptance.

    Known exact identities let the existing dashboard observations reconcile
    pre-start cancellation. Missing identities remain visible, never inferred.
    """
    try:
        with _scope(project) as (root, root_fd, _, folders):
            names = []
            with os.scandir(folders["launches"]) as entries:
                for examined, entry in enumerate(entries):
                    if examined >= projects.MAX_DIRECTORY_ENTRIES:
                        raise ValueError("automatic export launch directory exceeds inspection budget")
                    if re.fullmatch(r"[0-9a-f]{32}\.json", entry.name):
                        names.append(entry.name)
                        if len(names) > MAX_PENDING:
                            raise ValueError("launch automatic export queue exceeds its bounded index")
            results = []
            for name in names:
                value, inventory = _launch_inventory(root, root_fd, folders, name)
                if value is None:
                    continue
                results.append({"schema": value["schema"], "token": value["token"],
                    "created_at": value["created_at"], "status": "pending",
                    "identity": _identity(root, inventory) if inventory else None,
                    "reason": "Waiting for verified execution or terminal scheduler evidence" if inventory else
                              "Submission acceptance or exact run identity is unresolved; reconcile the launch receipt, never resubmit automatically"})
            return sorted(results, key=lambda value: (value["created_at"], value["token"]))
    except FileNotFoundError:
        return []


def _resolve_launches(project, root_fd, folders, deadline, evidence):
    names = []
    with os.scandir(folders["launches"]) as entries:
        for examined, entry in enumerate(entries):
            if examined >= projects.MAX_DIRECTORY_ENTRIES:
                raise ValueError("automatic export launch directory exceeds inspection budget")
            if re.fullmatch(r"[0-9a-f]{32}\.json", entry.name):
                names.append(entry.name)
                if len(names) > MAX_PENDING:
                    raise ValueError("launch automatic export queue exceeds its bounded index")
    last = None
    try:
        for name in _rotated(folders["launches"], names):
            if deadline.is_set():
                return
            last = name
            value, inventory = _launch_inventory(project, root_fd, folders, name)
            if value is None or inventory is None:
                continue
            run_id = inventory["run_id"]
            is_array = inventory.get("metadata", {}).get("raven_launch", {}).get("array_id") and "_" not in str(inventory.get("job_id", ""))
            if is_array:
                # Each task owns its attempt. A cancelled parent receipt may
                # still provide useful pre-start evidence without task samples.
                terminal = evidence.get(run_id)
                if not _matches_terminal_evidence(inventory, terminal):
                    continue
                if terminal["state"] == "COMPLETED":
                    # Per-task requests were captured by their supervisors.
                    # The parent is a submission receipt, not another attempt.
                    try:
                        os.unlink(name, dir_fd=folders["launches"])
                    except FileNotFoundError:
                        pass
                    continue
            capture(Path(project) / "runs" / run_id, value["request"])
            try:
                os.unlink(name, dir_fd=folders["launches"])
            except FileNotFoundError:
                pass
    finally:
        if last is not None:
            _write(folders["launches"], ".cursor.json", {"last": last})


def _ensure_run_index(root_fd, run_id, key):
    """Called under capture.lock; repair capture interrupted after queue write."""
    run_fd = projects._directory(root_fd, "runs/" + run_id)
    try:
        index = _read(run_fd, "auto-export.json", {"schema": "raven.auto-export-index/v1", "keys": []})
        keys = index.get("keys") if isinstance(index, dict) else None
        if (not isinstance(keys, list) or len(keys) > MAX_PER_RUN
                or any(not isinstance(item, str) or not _KEY.fullmatch(item) for item in keys)):
            raise ValueError("invalid run automatic export index")
        if key not in keys:
            if len(keys) >= MAX_PER_RUN:
                raise ValueError("this attempt already has 16 automatic export requests")
            _write(run_fd, "auto-export.json", dict(index, keys=[*keys, key]))
    finally:
        os.close(run_fd)


def capture(run, request):
    """Freeze a request before application execution; repeated capture is safe.

    ``run`` is an existing ``PROJECT/runs/RUN_ID`` directory. Preferences can be
    unbound; UI requests may name only this same attempt. No export occurs here.
    """
    run = Path(run).absolute()
    if run.parent.name != "runs":
        raise ValueError("automatic export needs an exact PROJECT/runs/RUN_ID directory")
    with _scope(run.parent.parent, create=True) as (project, root_fd, queue_fd, folders):
        inventory = _inventory(root_fd, run.name)
        identity = _identity(project, inventory)
        resolved = _request(project, run.name, request)
        key = _key(identity, resolved)
        filename = key + ".json"
        with _claim(queue_fd, "capture.lock", wait=True) as owned:
            if not owned:
                raise ValueError("automatic export settings are busy; retry capture")
            for folder in ("done", "pending"):
                previous = _read(folders[folder], filename)
                if previous is not None:
                    _validate(previous, key)
                    _ensure_run_index(root_fd, run.name, key)
                    return _public(previous)
            count = _queue_count(folders["pending"], r"[0-9a-f]{64}\.json")
            if count >= MAX_PENDING:
                raise ValueError("automatic export queue is full; reconcile or archive pending requests")
            run_fd = projects._directory(root_fd, "runs/" + run.name)
            try:
                index = _read(run_fd, "auto-export.json", {"schema": "raven.auto-export-index/v1", "keys": []})
                keys = index.get("keys") if isinstance(index, dict) else None
                if not isinstance(keys, list) or any(not isinstance(item, str) or not _KEY.fullmatch(item) for item in keys):
                    raise ValueError("invalid run automatic export index")
                if len(keys) >= MAX_PER_RUN:
                    raise ValueError("this attempt already has 16 automatic export requests")
                state = {"schema": SCHEMA, "key": key, "identity": identity, "request": resolved,
                         "status": "pending", "created_at": time.time(), "tries": 0,
                         "reason": "Waiting for application completion or verified terminal accounting",
                         "outputs": []}
                # Pending itself is the recovery index: a crash before updating
                # the per-run convenience index cannot orphan the request.
                _write(folders["pending"], filename, state, exclusive=True)
                _write(run_fd, "auto-export.json", dict(index, keys=[*keys, key]))
                return _public(state)
            finally:
                os.close(run_fd)


def _validate(state, key):
    if (not isinstance(state, dict) or state.get("schema") != SCHEMA or state.get("key") != key
            or not isinstance(state.get("identity"), dict) or not isinstance(state.get("request"), dict)
            or _key(state["identity"], state["request"]) != key):
        raise ValueError("automatic export request identity or settings changed")
    if state.get("status") not in {"pending", "running", "complete", "partial", "failed"}:
        raise ValueError("invalid automatic export state")
    if type(state.get("tries")) is not int or not 0 <= state["tries"] <= MAX_ATTEMPTS:
        raise ValueError("invalid automatic export retry count")
    return state


def read_status(run):
    """Bounded status lookup for a background details reader; never a renderer."""
    run = Path(run).absolute()
    try:
        with _scope(run.parent.parent) as (project, root_fd, _, folders):
            inventory = _inventory(root_fd, run.name)
            run_fd = projects._directory(root_fd, "runs/" + run.name)
            try:
                index = _read(run_fd, "auto-export.json", {})
            finally:
                os.close(run_fd)
            keys = index.get("keys", [])
            if not isinstance(keys, list) or len(keys) > MAX_PER_RUN:
                raise ValueError("invalid automatic export index")
            # A crash can publish the recovery request before its convenience
            # index. The bounded pending index remains authoritative.
            for pending in pending_status(project):
                if pending["identity"] == _identity(project, inventory) and pending["key"] not in keys:
                    keys.append(pending["key"])
                    if len(keys) > MAX_PER_RUN:
                        raise ValueError("this attempt exceeds its automatic export request budget")
            values = []
            for key in keys:
                if not isinstance(key, str) or not _KEY.fullmatch(key):
                    raise ValueError("invalid automatic export key")
                value = _read(folders["done"], key + ".json") or _read(folders["pending"], key + ".json")
                if value:
                    _validate(value, key)
                    if value["identity"] != _identity(project, inventory):
                        raise ValueError("automatic export belongs to a changed attempt")
                    values.append(_public(value))
            return values
    except FileNotFoundError:
        return []


class _Deadline:
    def __init__(self, seconds, cancel):
        self.until = time.monotonic() + seconds
        self.cancel = cancel

    def is_set(self):
        return time.monotonic() >= self.until or bool(self.cancel and (
            self.cancel.is_set() if hasattr(self.cancel, "is_set") else self.cancel()))

    __call__ = is_set


def _terminal(inventory, evidence):
    if inventory.get("state") in _FINAL:
        return True
    return _matches_terminal_evidence(inventory, evidence or inventory.get("metadata", {}).get("scheduler_outcome"))


def _matches_terminal_evidence(inventory, scheduler):
    if not isinstance(scheduler, dict) or scheduler.get("state") not in _FINAL:
        return False
    launch = inventory.get("metadata", {}).get("raven_launch", {})
    if (scheduler.get("job_id") != inventory.get("job_id") or not inventory.get("job_id")
            or scheduler.get("cluster", "") != launch.get("cluster", "")
            or scheduler.get("attempt") != inventory.get("attempt")):
        return False
    observed_submit, recorded_submit = scheduler.get("submit_time"), inventory.get("submit")
    if any(isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value)
           for value in (observed_submit, recorded_submit)):
        return False
    # Slurm commonly exposes whole-second submission timestamps. This tolerance
    # is only for that precision, never for absent or guessed identity fields.
    return abs(observed_submit - recorded_submit) < 1


def _retain_scheduler(root_fd, inventory, evidence):
    if not evidence or not _matches_terminal_evidence(inventory, evidence):
        return inventory
    value = {"schema": "raven.scheduler-outcome/v1", **{
        key: evidence[key] for key in ("state", "job_id", "cluster", "attempt", "submit_time", "observed_at", "source")
        if key in evidence}}
    run_fd = projects._directory(root_fd, "runs/" + inventory["run_id"])
    try:
        # A separate observation cannot overwrite concurrent application finish
        # publication. Application status remains unknown after missing cleanup.
        _write(run_fd, "scheduler-outcome.json", value)
        return inventory
    finally:
        os.close(run_fd)


def process_pending(project, *, run_ids=None, deadline_seconds=10., limit=4,
                    cancel=None, terminal_evidence=None, retry=False):
    """Perform bounded pending work, returning statuses without scheduler IO.

    A local terminal report is sufficient; absent cleanup needs terminal
    evidence matching cluster, job, restart attempt and submission time. A
    cancelled/deadline-limited export remains pending and never changes the
    workload outcome. Three attempts bound automatic failure retries.
    """
    if (isinstance(deadline_seconds, bool) or not isinstance(deadline_seconds, (float, int))
            or not math.isfinite(deadline_seconds) or not 0 < deadline_seconds <= 300):
        raise ValueError("automatic export worker deadline must be between 0 and 300 seconds")
    if type(limit) is not int or not 1 <= limit <= 64:
        raise ValueError("automatic export worker limit must be between 1 and 64")
    selected = set(run_ids) if run_ids is not None else None
    if selected is not None and (len(selected) > MAX_PENDING or any(not isinstance(item, str) or not projects._IDENT.fullmatch(item) for item in selected)):
        raise ValueError("automatic export run selection is invalid")
    evidence = terminal_evidence or {}
    if not isinstance(evidence, dict) or len(evidence) > MAX_PENDING:
        raise ValueError("terminal evidence must be a bounded mapping of exact run IDs")
    deadline = _Deadline(float(deadline_seconds), cancel)
    results = []
    try:
        context = _scope(project)
        scope = context.__enter__()
    except FileNotFoundError:
        return []
    try:
        root, root_fd, queue_fd, folders = scope
        last_key = None
        # Unknown submissions must not spend the entire worker slice before
        # already-captured completion requests get a chance to publish.
        launch_deadline = _Deadline(min(1., float(deadline_seconds) / 4), deadline)
        _resolve_launches(root, root_fd, folders, launch_deadline, evidence)
        keys = []
        with os.scandir(folders["pending"]) as entries:
            for examined, entry in enumerate(entries):
                if examined >= projects.MAX_DIRECTORY_ENTRIES:
                    raise ValueError("automatic export pending directory exceeds inspection budget")
                if entry.name.endswith(".json") and _KEY.fullmatch(entry.name[:-5]):
                    keys.append(entry.name[:-5])
                    if len(keys) > MAX_PENDING:
                        raise ValueError("automatic export queue exceeds its bounded index")
        for key in _rotated(folders["pending"], keys):
            if deadline.is_set() or len(results) >= limit:
                break
            last_key = key
            with _claim(folders["claims"], key + ".lock") as owned:
                if not owned:
                    continue
                filename = key + ".json"
                state = _read(folders["pending"], filename)
                if state is None:
                    continue
                _validate(state, key)
                completed = _read(folders["done"], filename)
                if completed is not None:
                    _validate(completed, key)
                    if completed["status"] not in {"complete", "partial"}:
                        raise ValueError("invalid published automatic export receipt")
                    os.unlink(filename, dir_fd=folders["pending"])
                    os.fsync(folders["pending"])
                    results.append(_public(completed))
                    continue
                run_id = state["identity"]["run_id"]
                if selected is not None and run_id not in selected:
                    continue
                if not retry and (state["tries"] >= MAX_ATTEMPTS or state.get("retry_after", 0) > time.time()):
                    continue
                inventory = _inventory(root_fd, run_id)
                if _identity(root, inventory) != state["identity"]:
                    state.update(status="failed", tries=MAX_ATTEMPTS, reason="Run identity changed; review the original request")
                    _write(folders["pending"], filename, state)
                    results.append(_public(state))
                    continue
                with _claim(queue_fd, "capture.lock", wait=True) as indexed:
                    if not indexed:
                        continue
                    _ensure_run_index(root_fd, state["identity"]["run_id"], key)
                supplied = evidence.get(run_id)
                if supplied is None:
                    run_fd = projects._directory(root_fd, "runs/" + run_id)
                    try:
                        supplied = _read(run_fd, "scheduler-outcome.json")
                    finally:
                        os.close(run_fd)
                if not _terminal(inventory, supplied):
                    continue
                if evidence.get(run_id):
                    inventory = _retain_scheduler(root_fd, inventory, evidence[run_id])
                from . import data_export
                if retry and state["tries"] >= MAX_ATTEMPTS:
                    state["tries"] = 0
                state["tries"] += 1
                try:
                    if not state.get("plan") or any(source.get('available') and source.get('watermark_hash')!='full-prefix-sha256/v1'
                        for attempt in state.get('plan',{}).get('attempts',[]) for source in attempt.get('sources',[])):
                        state["plan"] = data_export.plan_export(state["request"])
                    # A worker slice limits discovery, not the size of a run
                    # that can ever finish. Size one bounded export allowance
                    # from its captured bytes; external cancellation still wins.
                    captured_bytes=sum(source.get('watermark',{}).get('size',0)
                        for attempt in state['plan'].get('attempts',[]) for source in attempt.get('sources',[]))
                    deadline.until=max(deadline.until,time.monotonic()+min(300.,max(5.,captured_bytes/(512*1024))))
                    state.update(status="running", started_at=time.time(), reason="Exporting retained observations")
                    _write(folders["pending"], filename, state)
                    receipt = data_export.export_data(state["plan"], cancel=deadline)
                    outcome = receipt.get("status", "failed")
                    state.update(receipt=receipt, outputs=receipt.get("outputs", []), updated_at=time.time())
                    if outcome in {"complete", "partial"}:
                        state.update(status=outcome, reason="Published retained observations" if outcome == "complete" else "Published with documented coverage gaps")
                        _write(folders["done"], filename, state, exclusive=True)
                        os.unlink(filename, dir_fd=folders["pending"])
                        os.fsync(folders["pending"])
                        results.append(_public(state))
                        continue
                    if outcome == "cancelled":
                        state['tries']=max(0,state['tries']-1)
                        state.update(status="pending", reason="Export paused at its time/cancellation limit; no failure attempt was consumed")
                    else:
                        state.update(status="failed", reason="Export failed; inspect receipt details")
                except (OSError, ValueError, RuntimeError) as exc:
                    state.update(status="failed", reason=str(exc)[:4096], updated_at=time.time())
                state["retry_after"] = time.time() + min(300, 5 * 2 ** state["tries"])
                _write(folders["pending"], filename, state)
                results.append(_public(state))
        return results
    finally:
        try:
            if locals().get("last_key") is not None:
                _write(folders["pending"], ".cursor.json", {"last": last_key})
        finally:
            context.__exit__(None, None, None)
