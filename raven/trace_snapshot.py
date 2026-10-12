"""Reviewed, node-local, nonblocking Python stack snapshots for owned jobs.

This adapter never stops a target, adds privileges, changes ptrace policy or
falls back to a debugger. A snapshot is non-atomic evidence, not an event trace.
"""
from __future__ import annotations

import getpass
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import stat
import time

from . import operations as O
from . import trace_protocol as T
from .reporting import _directory
from .trace_recorder import Recorder

FEATURE = "trace-snapshot"
MAX_OUTPUT = 1 << 20
MAX_THREADS = 256
MAX_FRAMES = 256
_JOB = re.compile(r"[0-9]{1,20}(?:_[0-9]{1,10})?\Z", re.ASCII)


def _text(value, name, limit=4096, optional=False):
    if not isinstance(value, str) or len(value) > limit or not value.isprintable() and value:
        raise ValueError(f"{name} must be bounded printable text")
    value = value.strip()
    if not value and not optional:
        raise ValueError(f"Set {name}")
    return value


def _local_user():
    uid = os.getuid()
    try:
        name = pwd.getpwuid(uid).pw_name
    except KeyError as exc:
        raise ValueError("Current user identity cannot be resolved") from exc
    if os.geteuid() != uid or getpass.getuser() != name:
        raise ValueError("Use Raven directly as the job owner, without switched user credentials")
    return name, uid


def _project(path):
    root = _directory(Path(path).absolute())
    info = root.stat()
    return str(root), [info.st_dev, info.st_ino, info.st_uid]


def _binary():
    found = shutil.which("py-spy")
    if not found:
        raise ValueError("py-spy is not installed on this node; install it in your user environment to enable Python snapshots")
    try:
        path = Path(found).resolve(strict=True)
        info = path.stat()
    except OSError as exc:
        raise ValueError("The installed py-spy executable cannot be inspected") from exc
    if not stat.S_ISREG(info.st_mode) or not os.access(path, os.X_OK):
        raise ValueError("py-spy must be an executable regular file")
    return str(path), [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_mode, info.st_uid]


def _process(pid, uid):
    """Read a bounded /proc stat through a descriptor for this PID instance."""
    try:
        directory = os.open(f"/proc/{pid}", os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
        try:
            info = os.fstat(directory)
            if info.st_uid != uid:
                raise ValueError("The target process is not owned by the current Unix user")
            fd = os.open("stat", os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory)
            with os.fdopen(fd, "rb") as stream:
                raw = stream.read(65537)
        finally:
            os.close(directory)
    except OSError as exc:
        raise ValueError("The target process exited or its /proc identity is unavailable") from exc
    try:
        if len(raw) > 65536:
            raise ValueError
        text = raw.decode("utf-8", "replace")
        fields = text.rsplit(")", 1)[1].split()
        if text.split(" ", 1)[0] != str(pid) or fields[0] in {"Z", "X", "x"}:
            raise ValueError
        start = fields[19]
        if not re.fullmatch(r"[0-9]{1,20}", start, re.ASCII):
            raise ValueError
    except (IndexError, ValueError) as exc:
        raise ValueError("The target process has no stable live /proc start identity") from exc
    return dict(start=start, uid=info.st_uid, device=info.st_dev, inode=info.st_ino)


def _job(ctx, job_id, user, uid):
    candidates = [record for record in ctx.jobs if str(record.get("id", "")) == job_id]
    if len(candidates) != 1 or str(candidates[0].get("state", "")).upper() != "RUNNING":
        raise ValueError("Select one exact currently running job from this connection")
    record = candidates[0]
    configured = str(getattr(ctx.slurm, "user", "") or "")
    if configured != user or record.get("user") not in (None, "", user):
        raise ValueError("Snapshots are limited to jobs owned by the current Unix and Slurm user")
    from .ops_cluster import parse_records
    details = parse_records(O.command(ctx, ["scontrol", "show", "job", "-o", job_id], timeout=5, limit=MAX_OUTPUT), "JobId")
    if len(details) != 1:
        raise ValueError("The scheduler returned ambiguous job identity")
    detail = details[0]
    actual = detail.get("JobId", "")
    if not re.fullmatch(r"[0-9]{1,20}", actual, re.ASCII):
        raise ValueError("The scheduler must resolve a single numeric job identity")
    aliases = {actual}
    if "ArrayJobId" in detail or "ArrayTaskId" in detail:
        if (not re.fullmatch(r"[0-9]{1,20}", detail.get("ArrayJobId", ""), re.ASCII)
                or not re.fullmatch(r"[0-9]{1,10}", detail.get("ArrayTaskId", ""), re.ASCII)):
            raise ValueError("An array snapshot requires a resolved single numeric task, not an aggregate parent or range")
        aliases.add(detail["ArrayJobId"] + "_" + detail["ArrayTaskId"])
    if job_id not in aliases or detail.get("JobState", "").split("+", 1)[0] != "RUNNING":
        raise ValueError("The requested job is not the exact running scheduler task")
    if detail.get("UserId") != f"{user}({uid})":
        raise ValueError("The live scheduler job is not owned by the current Unix user")
    for source, target in (("submit", "SubmitTime"), ("start", "StartTime")):
        value = detail.get(target, "")
        if not value or value.lower() in {"unknown", "n/a", "none", "(null)"}:
            raise ValueError("The scheduler did not provide the running submit/start attempt identity")
        if record.get(source) and record[source] != value:
            raise ValueError("The job attempt changed; refresh and prepare a new review")
    identity = {key: detail.get(key, "") for key in ("JobId", "ArrayJobId", "ArrayTaskId", "SubmitTime", "StartTime", "Restarts", "UserId")}
    return actual, identity


def _membership(ctx, job_id, actual, pid):
    output = O.command(ctx, ["scontrol", "listpids", job_id], timeout=5, limit=MAX_OUTPUT)
    lines = [line.split() for line in output.splitlines() if line.strip()]
    if not lines or len(set(lines[0])) != len(lines[0]) or "PID" not in lines[0] or "JOBID" not in lines[0]:
        raise ValueError("scontrol listpids did not provide unambiguous PID and JOBID columns on this node")
    header, matches = lines[0], []
    for row in lines[1:]:
        if len(row) != len(header):
            raise ValueError("Malformed node-local Slurm process membership response")
        value = dict(zip(header, row))
        if value["PID"] == str(pid):
            matches.append(value)
    if len(matches) != 1 or matches[0]["JOBID"] != actual:
        raise ValueError("PID is not uniquely verified in this job by node-local scontrol listpids; run Raven on the process node")


def prepare(params, ctx):
    O.local_only(ctx)
    O.checkpoint(ctx)
    job_id = _text(params.get("job_id") or str(ctx.selected or ""), "job_id", 64)
    if not _JOB.fullmatch(job_id):
        raise ValueError("Use one exact job ID or numeric array task, without ranges, steps or wildcards")
    raw_pid = _text(params.get("pid", ""), "pid", 10)
    if not re.fullmatch(r"[0-9]+", raw_pid, re.ASCII) or not 1 <= int(raw_pid) <= 2**31 - 1:
        raise ValueError("PID must be a positive decimal process ID")
    pid = int(raw_pid)
    project, project_identity = _project(_text(params.get("project", ""), "project"))
    binary, binary_identity = _binary()
    user, uid = _local_user()
    actual, job_identity = _job(ctx, job_id, user, uid)
    process = _process(pid, uid)
    _membership(ctx, job_id, actual, pid)
    if _process(pid, uid) != process:
        raise ValueError("PID identity changed while checking job membership")
    known_attempt = job_identity["SubmitTime"] + "/" + job_identity["StartTime"]
    requested = _text(params.get("attempt", ""), "attempt", 256, optional=True)
    if requested and requested != known_attempt:
        raise ValueError("Snapshot attempt must equal this job's verified submit/start identity")
    payload = dict(job_id=job_id, actual_job_id=actual, pid=pid, user=user, uid=uid, process=process,
                   job=job_identity, project=project, project_identity=project_identity,
                   binary=binary, binary_identity=binary_identity, attempt=known_attempt)
    plan = O.prepare_plan(ctx, FEATURE, params, payload)
    return O.report(FEATURE, "Review one nonblocking Python stack snapshot", plan=plan, data=payload,
                    rows=[f"Job {job_id}, local PID {pid}, owner {user}, process start {process['start']}",
                          f"Save observed stacks in {project}/.raven/traces", f"Executable: {binary}",
                          "Capture Python frames once, without --native, process suspension, locals or debugger fallback."],
                    warnings=["Nonblocking snapshots are non-atomic and may be incomplete or inaccurate; they do not recover earlier execution.",
                              "Kernel ptrace policy and Python/runtime compatibility can deny this read; Raven will not change permissions."])


def _snapshot(raw, pid):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate py-spy JSON key")
            result[key] = value
        return result
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_OUTPUT:
        raise ValueError("py-spy snapshot exceeds its 1 MiB output budget")
    try:
        value = json.loads(raw, object_pairs_hook=pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite py-spy JSON")))
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("py-spy did not return supported stack JSON") from exc
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_THREADS:
        raise ValueError(f"Snapshot must contain 1–{MAX_THREADS} Python threads")
    result, seen = [], set()
    for thread in value:
        if not isinstance(thread, dict) or type(thread.get("pid")) is not int or thread["pid"] != pid:
            raise ValueError("py-spy returned a different or unverified target PID")
        tid = thread.get("thread_id")
        frames = thread.get("frames")
        if type(tid) is not int or not 0 < tid < 1 << 64 or tid in seen:
            raise ValueError("py-spy returned an invalid or duplicated thread identity")
        seen.add(tid)
        if not isinstance(frames, list) or len(frames) > MAX_FRAMES:
            raise ValueError(f"Snapshot exceeds {MAX_FRAMES} frames per thread")
        stack = []
        for index, frame in enumerate(reversed(frames)):
            if (not isinstance(frame, dict) or not isinstance(frame.get("name"), str)
                    or not isinstance(frame.get("filename"), str) or type(frame.get("line")) is not int
                    or not -(1 << 31) <= frame["line"] < 1 << 31):
                raise ValueError("py-spy returned an unsupported frame structure")
            stack.append(dict(function=O.clean(frame["name"], 256) or "<unknown>",
                              file=O.clean(frame["filename"], 1024) or "<unknown>",
                              line=max(0, frame["line"]), frame=f"{tid}:{index}"))
        data = {"frames": stack}
        T.validate_event(dict(kind="stack_checkpoint", mono_ns=0, thread=str(tid), data=data))
        result.append(dict(thread=str(tid), frames=stack))
    if not any(thread["frames"] for thread in result):
        raise ValueError("No Python frames were observed; this process/runtime may be unsupported or between Python calls")
    return result


def apply(plan, ctx):
    O.local_only(ctx)
    payload = O.validate_plan(plan, ctx, FEATURE)
    user, uid = _local_user()
    if (user, uid) != (payload["user"], payload["uid"]):
        raise ValueError("The current user changed; prepare a new snapshot review")
    if _project(payload["project"]) != (payload["project"], payload["project_identity"]):
        raise ValueError("The destination project changed; prepare a new review")
    if _binary() != (payload["binary"], payload["binary_identity"]):
        raise ValueError("The py-spy executable changed; prepare a new review")
    actual, identity = _job(ctx, payload["job_id"], user, uid)
    if actual != payload["actual_job_id"] or identity != payload["job"]:
        raise ValueError("The live job attempt changed; prepare a new review")
    _membership(ctx, payload["job_id"], actual, payload["pid"])
    if _process(payload["pid"], uid) != payload["process"]:
        raise ValueError("PID exited or was reused; prepare a new snapshot review")
    before = time.monotonic_ns()
    raw = O.command(ctx, [payload["binary"], "dump", "--pid", str(payload["pid"]), "--json", "--nonblocking"],
                    timeout=5, limit=MAX_OUTPUT)
    after = time.monotonic_ns()
    if _process(payload["pid"], uid) != payload["process"]:
        raise ValueError("PID changed during capture; snapshot discarded")
    _membership(ctx, payload["job_id"], actual, payload["pid"])
    if _binary() != (payload["binary"], payload["binary_identity"]):
        raise ValueError("py-spy changed during capture; snapshot discarded")
    stacks = _snapshot(raw, payload["pid"])
    O.checkpoint(ctx)
    coverage = dict(adapter="py-spy-nonblocking-snapshot", mode="snapshot", complete=False,
                    scope="Point-in-time non-atomic observed Python frames; no historical, native or GPU coverage",
                    capture_start_ns=before, capture_end_ns=after)
    extra = dict(pid=payload["pid"], process_start=payload["process"]["start"],
                 slurm_submit=identity["SubmitTime"], slurm_start=identity["StartTime"])
    with Recorder(payload["project"], job_id=payload["job_id"], attempt=payload["attempt"],
                  cluster=str(ctx.scope.get("cluster") or "local"), coverage=coverage, identity_extra=extra,
                  max_bytes=4 << 20) as recorder:
        for thread in stacks:
            O.checkpoint(ctx)
            if not recorder.emit("stack_checkpoint", {"frames": thread["frames"]}, thread=thread["thread"]):
                raise ValueError("Snapshot journal could not store all observed frames; inspect its incomplete manifest")
    rows = [f"Saved {len(stacks)} observed Python thread stacks for job {payload['job_id']} / PID {payload['pid']}", str(recorder.path)]
    for thread in stacks:
        if len(rows) >= 256:
            rows.append("Additional observed frames are available in the Trace panel and saved journal.")
            break
        rows.append(f"Thread {thread['thread']}: {len(thread['frames'])} frames")
        rows.extend(f"  {frame['function']} ({frame['file']}:{frame['line']})" for frame in thread["frames"][:max(0, 256 - len(rows))])
    return O.report(FEATURE, "Python stack snapshot saved", status="warning", rows=rows,
                    data=dict(path=str(recorder.path), identity=recorder.identity, threads=len(stacks), coverage=coverage),
                    warnings=["This is a non-atomic, best-effort Python snapshot, not a continuous or lossless trace.",
                              "Native extension frames, GPU work, earlier execution and unobserved threads are outside this snapshot's coverage."])
