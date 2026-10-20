"""Bounded cluster operations for the explicit Operations workbench.

Preparation is read only. Scheduler changes and terminal handoffs need a reviewed,
one-use plan, and recheck the allocation immediately before the action.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import math
import os
import re
import stat
import time


def _field(key, label, default="", *, required=False, choices=None):
    item = dict(key=key, label=label, default=default, required=required)
    if choices:
        item["choices"] = list(choices)
    return item


SPECIFICATIONS = [
    dict(key="storage", title="Storage readiness", group="Cluster", proposal="P01",
         summary="Check free space, free inodes, and user/group/project quota without scanning files.",
         fields=[_field("path", "Path on the cluster", ".", required=True),
                 _field("quota_tool", "Quota tool", "quota", choices=("quota", "lfs")),
                 _field("quota_kind", "Quota kind", "user", choices=("user", "group", "project")),
                 _field("quota_user", "Quota user/group/project ID (blank: current user)"),
                 _field("min_free_gib", "Minimum free GiB", "1"),
                 _field("min_free_inodes", "Minimum free inodes", "1000")]),
    dict(key="pending-edit", title="Edit pending job", group="Cluster", proposal="P02",
         summary="Review one pending-job field and apply only if the job is unchanged.",
         fields=[_field("job_id", "Job ID (blank: selected)"),
                 _field("field", "Field", "TimeLimit", choices=("TimeLimit", "Partition", "QOS", "Account", "Nice", "Dependency", "BeginTime")),
                 _field("value", "New value", required=True)]),
    dict(key="array-throttle", title="Array concurrency", group="Cluster", proposal="P03",
         summary="Change an existing array's task throttle; zero removes the throttle.",
         fields=[_field("job_id", "Array or task ID (blank: selected)"), _field("limit", "Maximum concurrent tasks (0: unlimited)", "0", required=True)]),
    dict(key="reservations", title="Reservation timeline", group="Cluster", proposal="P04",
         summary="Inspect reservation windows, resources, and access restrictions.",
         fields=[_field("hours", "Hours to show", "48", required=True)]),
    dict(key="licenses", title="License inventory", group="Cluster", proposal="P05",
         summary="Inspect Slurm license availability, reservations, and remote servers.",
         fields=[_field("name", "License name filter (blank: all)")]),
    dict(key="slurm-doctor", title="Slurm service doctor", group="Cluster", proposal="A05",
         summary="Test controller, accounting, partitions, and selected configuration evidence.", fields=[]),
    dict(key="batch-script", title="Retrieve batch script", group="Cluster", proposal="A20",
         summary="Read the scheduler's retained script; optionally review a new local output file.",
         fields=[_field("job_id", "Job ID (blank: selected)"), _field("output", "Save path (blank: preview only)")]),
    dict(key="rerun-job", title="Rerun cancelled job", group="Cluster", proposal="QoL",
         summary="Restart one retained cancelled batch job with Slurm's stored launch settings.",
         fields=[_field("job_id", "Cancelled job or exact array task ID (blank: selected)")]),
    dict(key="allocation-shell", title="Allocation shell or attach", group="Cluster", proposal="A19",
         summary="Open one shell step in a running allocation, or attach to a selected numeric step.",
         fields=[_field("job_id", "Job ID (blank: selected)"),
                 _field("mode", "Action", "shell", choices=("shell", "attach")),
                 _field("step_id", "Exact numeric step ID (attach only)"),
                 _field("shell", "Shell path", "/bin/bash"),
                 _field("node", "Exact allocation node (shell only; blank: scheduler chooses)"),
                 _field("task", "Exact task rank (attach only; blank: all step output)"),
                 _field("display", "Terminal display", "embedded", choices=("embedded", "fullscreen"))]),
]
FEATURES = {spec["key"] for spec in SPECIFICATIONS}
_JOB = re.compile(r"[0-9]{1,20}(?:_[0-9]{1,10})?(?:\+[0-9]{1,6})?\Z")
_KEY = re.compile(r"(?:^|\s)([A-Za-z][A-Za-z0-9_./-]*)=")
_NULL = {"", "(null)", "unknown", "none", "n/a", "none assigned"}
_ACTIVE = {"PENDING", "RUNNING", "CONFIGURING", "COMPLETING", "SUSPENDED"}
_EDIT_FIELDS = {"TimeLimit", "Partition", "QOS", "Account", "Nice", "Dependency", "BeginTime"}
_MAX_SCRIPT = 1 << 20


def _ops():
    from . import operations
    return operations


def _text(value, label, *, empty=False, limit=4096):
    if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError(f"{label} must be bounded text without control characters")
    if not empty and not value.strip():
        raise ValueError(f"{label} is required")
    return value.strip()


def _integer(value, label, low, high):
    text = _text(value, label)
    if not re.fullmatch(r"[0-9]+", text, flags=re.ASCII) or len(text) > 12:
        raise ValueError(f"{label} must be an integer between {low} and {high}")
    number = int(text)
    if not low <= number <= high:
        raise ValueError(f"{label} must be between {low} and {high}")
    return number


def _job_id(params, ctx):
    value = _text(params.get("job_id") or ctx.selected or "", "Job ID")
    if not _JOB.fullmatch(value):
        raise ValueError("Use one exact job ID or array task ID; ranges and job steps are not accepted")
    return value


def parse_records(text, marker):
    """Read scontrol one-line records, preserving values that contain spaces.

    Duplicate keys and records without their expected marker are errors, because
    ambiguity must never select a different job or hide an unavailable field.
    """
    if not isinstance(text, str) or len(text.encode("utf-8")) > 1 << 20 or "\x00" in text:
        raise ValueError("Invalid or oversized scheduler response")
    records = []
    # A few Slurm releases wrap scontrol output despite -o. A record begins at
    # its marker, not at every physical line.
    starts = list(re.finditer(r"(?:^|\s)" + re.escape(marker) + r"=", text))
    for index, start in enumerate(starts):
        segment = text[start.start():starts[index + 1].start() if index + 1 < len(starts) else len(text)]
        matches = list(_KEY.finditer(segment))
        record = {}
        for i, match in enumerate(matches):
            key = match.group(1)
            if key in record:
                raise ValueError(f"Ambiguous duplicate scheduler field: {key}")
            value = segment[match.end():matches[i + 1].start() if i + 1 < len(matches) else len(segment)].strip()
            record[key] = value
        records.append(record)
    if not records and text.strip() and not any(x in text.lower() for x in ("no reservations", "no licenses", "no reservation", "no license")):
        raise ValueError(f"Scheduler did not return {marker} records")
    return records


def _job(ctx, job_id):
    text = _ops().command(ctx, ["scontrol", "show", "job", "-o", job_id], timeout=5)
    records = parse_records(text, "JobId")
    matches = [r for r in records if r.get("JobId") == job_id or
               (str(r.get("ArrayJobId", "")) + "_" + str(r.get("ArrayTaskId", ""))) == job_id or
               (str(r.get("HetJobId", "")) + "+" + str(r.get("HetJobOffset", ""))) == job_id]
    if len(matches) != 1:
        raise ValueError("The scheduler did not return the exact selected job")
    record = matches[0]
    _identity(record)
    return record


def _identity(record, *, running=False):
    submit, user = record.get("SubmitTime", ""), record.get("UserId", "")
    if submit.lower() in _NULL or user.lower() in _NULL:
        raise ValueError("Submit time and owner are required to verify job identity")
    result = {key: record.get(key, "") for key in ("JobId", "ArrayJobId", "HetJobId", "HetJobOffset", "SubmitTime", "UserId", "Restarts", "RestartCnt")}
    # A parent's remaining task range changes as tasks finish. It is not its
    # identity. A numeric child index is stable and must be pinned.
    task = record.get("ArrayTaskId", "")
    result["ArrayTaskId"] = task if task.isascii() and task.isdigit() else ""
    if running:
        start = record.get("StartTime", "")
        if start.lower() in _NULL:
            raise ValueError("The running attempt has no verifiable start time")
        result["StartTime"] = start
    return result


def _owned(ctx, record):
    expected = str(getattr(ctx.slurm, "user", ""))
    owner = record.get("UserId", "").split("(", 1)[0]
    if not expected or owner != expected:
        raise ValueError("This action requires a job owned by the configured Slurm user")


def _state(record):
    return record.get("JobState", "").split("+", 1)[0].upper()


def _check_identity(ctx, payload, *, running=False, pending=False):
    record = _job(ctx, payload.get("read_id", payload["job_id"]))
    _owned(ctx, record)
    if _identity(record, running=running) != payload["identity"]:
        raise ValueError("Job identity or attempt changed; prepare a new review")
    if pending and _state(record) != "PENDING":
        raise ValueError("The job is no longer pending; no update was sent")
    if running and _state(record) != "RUNNING":
        raise ValueError("The allocation is no longer running; no terminal was opened")
    return record


def parse_df(text):
    """Parse POSIX df -P blocks/inodes, including wrapped filesystem names."""
    if not isinstance(text, str) or len(text) > 65536:
        raise ValueError("Invalid df response")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    pending = ""
    results = []
    for line in lines[1:]:
        parts = (pending + " " + line).split()
        if len(parts) == 1:
            pending = parts[0]
            continue
        pending = ""
        if len(parts) < 6 or not parts[4].endswith("%"):
            continue
        def number(value):
            return int(value) if re.fullmatch(r"[0-9]{1,20}", value, re.ASCII) else None
        total, used, free = (number(x) for x in parts[1:4])
        percent = number(parts[4][:-1])
        if total is None or used is None or free is None:
            continue
        results.append(dict(filesystem=parts[0], total=total, used=used, free=free,
                            percent=percent, mount=" ".join(parts[5:])))
    if len(results) != 1:
        raise ValueError("df did not return one supported filesystem record")
    return results[0]


def parse_quota(text):
    """Read conventional quota -w rows; unknown formats retain raw evidence."""
    results, pending = [], ""
    for line in text.splitlines():
        tokens = (pending + " " + line.strip()).split()
        pending = ""
        if len(tokens) == 1 and (tokens[0].startswith("/") or ":/" in tokens[0]):
            pending = tokens[0]
            continue
        if not tokens or not (tokens[0].startswith("/") or ":/" in tokens[0]):
            continue
        # Numeric columns are blocks used/soft/hard, then files used/soft/hard.
        # Grace fields (e.g. 6days, expired) are deliberately skipped.
        values = [int(x.rstrip("*")) for x in tokens[1:]
                  if re.fullmatch(r"[0-9]{1,20}\*?", x, re.ASCII)]
        if len(values) != 6:
            continue
        block_used, block_soft, block_hard, inode_used, inode_soft, inode_hard = values
        over = (block_hard > 0 and block_used >= block_hard or
                inode_hard > 0 and inode_used >= inode_hard)
        soft = (block_soft > 0 and block_used >= block_soft or
                inode_soft > 0 and inode_used >= inode_soft)
        results.append(dict(filesystem=tokens[0], blocks_used=block_used, blocks_soft=block_soft,
                            blocks_hard=block_hard, inodes_used=inode_used, inodes_soft=inode_soft,
                            inodes_hard=inode_hard, hard_limit_reached=bool(over), soft_limit_reached=bool(soft)))
    return results


def _storage(params, ctx):
    path = _text(params.get("path", "."), "Path")
    tool, kind = params.get("quota_tool", "quota"), params.get("quota_kind", "user")
    if tool not in {"quota", "lfs"} or kind not in {"user", "group", "project"}:
        raise ValueError("Choose quota or lfs, and user, group, or project quota")
    if kind != "user" and not params.get("quota_user"):
        raise ValueError("Group and project quota checks require an explicit name or ID")
    quota_user = _text(params.get("quota_user") or str(ctx.slurm.user), "Quota user", limit=128)
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.@-]{0,127}", quota_user):
        raise ValueError("Invalid quota user")
    value = _text(params.get("min_free_gib", "1"), "Minimum free GiB", limit=20)
    try:
        minimum = float(value)
    except ValueError:
        raise ValueError("Minimum free GiB must be a finite nonnegative number") from None
    if not math.isfinite(minimum) or not 0 <= minimum <= 1e12:
        raise ValueError("Minimum free GiB must be a finite nonnegative number")
    min_inodes = _integer(params.get("min_free_inodes", "1000"), "Minimum free inodes", 0, 10 ** 12 - 1)
    data, warnings, rows = {"path": path, "quota_user": quota_user, "quota_kind": kind, "quota_tool": tool}, [], []
    for measurement, option in (("space", "-Pk"), ("inodes", "-Pi")):
        try:
            output = _ops().command(ctx, ["df", option, "--", path], timeout=5, limit=65536)
            value = parse_df(output)
            data[measurement] = value
            rows.append(f"{measurement.title()}: {value['free']:,} {'KiB' if measurement == 'space' else 'inodes'} free on {value['mount']} ({value['filesystem']})")
        except (ValueError, RuntimeError, OSError) as exc:
            _ops().checkpoint(ctx)
            warnings.append(f"{measurement.title()} unavailable: {exc}")
    try:
        flag = {"user": "-u", "group": "-g", "project": "-p" if tool == "lfs" else "-P"}[kind]
        argv = ["lfs", "quota", flag, quota_user, path] if tool == "lfs" else ["quota", "-w", flag, quota_user]
        quota = _ops().command(ctx, argv, timeout=5, limit=65536)
        records = parse_quota(quota)
        data.update(quota=records, quota_raw=quota)
        matching = [r for r in records if r["filesystem"] in {data.get("space", {}).get("filesystem"), data.get("space", {}).get("mount"), path if tool == "lfs" else None}]
        data["quota_for_path"] = matching
        if matching:
            for entry in matching:
                rows.append(f"Quota {entry['filesystem']}: blocks {entry['blocks_used']:,}/{entry['blocks_hard'] or 'unlimited'} hard; files {entry['inodes_used']:,}/{entry['inodes_hard'] or 'unlimited'} hard")
        else:
            warnings.append("Quota for this filesystem is unknown. Review the raw quota evidence or use the site's filesystem-specific quota tool.")
        if quota.strip():
            rows.extend(["Quota evidence:"] + quota.splitlines()[:30])
    except (ValueError, RuntimeError, OSError) as exc:
        _ops().checkpoint(ctx)
        warnings.append(f"{kind.title()} quota unavailable: {exc}")
    blocked = data.get("space", {}).get("free", math.inf) < minimum * 1024 ** 2 or data.get("inodes", {}).get("free", math.inf) < min_inodes
    blocked = blocked or any(q["hard_limit_reached"] for q in data.get("quota_for_path", []))
    soft = any(q["soft_limit_reached"] for q in data.get("quota_for_path", []))
    if soft:
        warnings.append("A soft quota is reached. Grace expiry can prevent writes before the hard limit.")
    rows.append("This check does not write files. Directory permissions, ACLs, other quota kinds, and future usage still apply. Select each applicable quota kind separately.")
    status = "blocked" if blocked else "partial" if warnings or soft else "ok"
    return _ops().report("storage", "Storage threshold failed" if blocked else "Storage evidence collected", status=status, rows=rows, data=data, warnings=warnings)


def _edit_value(field, value):
    if field not in _EDIT_FIELDS:
        raise ValueError("Unsupported pending-job field")
    value = _text(value, "New value", limit=512)
    if field in {"Partition", "Account", "QOS"} and not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.@,-]*", value):
        raise ValueError(f"Invalid {field} name")
    if field == "Nice" and not re.fullmatch(r"-?[0-9]{1,9}", value):
        raise ValueError("Nice must be an integer; Slurm controls permission for negative values")
    if field == "TimeLimit" and not (value.upper() in {"UNLIMITED", "INFINITE"} or re.fullmatch(r"(?:[0-9]{1,6}-)?[0-9]{1,6}(?::[0-9]{1,2}){0,2}", value)):
        raise ValueError("TimeLimit must use Slurm's minutes or [days-]hours:minutes:seconds format")
    if field == "Dependency" and not re.fullmatch(r"(?:0|singleton|(?:after|afterany|afterok|afternotok|aftercorr|afterburstbuffer):[0-9_+?:,a-z]+)", value):
        raise ValueError("Use a Slurm dependency expression, singleton, or 0 to clear dependencies")
    if field == "BeginTime" and not re.fullmatch(r"[A-Za-z0-9:+_.-]+", value):
        raise ValueError("Use a Slurm begin-time expression such as now+1hour or an ISO timestamp")
    return value


def _pending_edit(params, ctx):
    selected = _text(params.get("job_id") or ctx.selected or "", "Job ID")
    compressed = re.fullmatch(r"([0-9]{1,20})_\[([0-9,:%-]{1,4096})\]", selected)
    if compressed:
        from .arrays import parse_range
        parse_range(compressed[2])
        job_id = compressed[1]
    else:
        job_id = _job_id(params, ctx)
    field = params.get("field", "TimeLimit")
    value = _edit_value(field, params.get("value", ""))
    record = _job(ctx, job_id)
    _owned(ctx, record)
    if compressed and record.get("ArrayJobId") != job_id:
        raise ValueError("The selected range is not a verified array")
    if _state(record) != "PENDING":
        raise ValueError("Only a currently pending job can use this editor")
    array_scope = _pending_array_scope(ctx, record, selected)
    target = array_scope["target"] if array_scope else job_id
    old_key = ("BeginTime" if "BeginTime" in record else "EligibleTime") if field == "BeginTime" else field
    # BeginTime is not always exposed; in that case scheduler identity is still
    # checked but no invented old value is displayed as authoritative.
    old = record.get(old_key)
    payload = dict(job_id=job_id, identity=_identity(record), field=field, value=value,
                   old_key=old_key, old=old, array_scope=array_scope,
                   argv=["scontrol", "update", f"JobId={target}", f"{field}={value}"])
    plan = _ops().prepare_plan(ctx, "pending-edit", params, payload)
    rows = [f"Job: {job_id}; owner: {record['UserId']}; submitted: {record['SubmitTime']}", f"{field}: {old if old is not None else 'not exposed'} -> {value}"]
    if array_scope:
        rows.append(f"Exact pending array scope: {target}; {len(array_scope['tasks'])} tasks. Running tasks are never targeted.")
    rows.append("Slurm policy and permissions apply. Apply rechecks the job and the original field.")
    return _ops().report("pending-edit", f"Review {field} for pending job {job_id}", status="review", rows=rows, data=payload, plan=plan)


def _pending_array_scope(ctx, record, selected):
    parent = record.get("ArrayJobId")
    if not parent or re.fullmatch(r"[0-9]+_[0-9]+", selected):
        return None
    # A parent update also affects running siblings. Read the controller's
    # expanded live scope, then target only the exact reviewed task indices.
    raw = _ops().command(ctx, ["squeue", "--noheader", "--array", "--jobs=" + parent,
                               "--format=%i|%T"], limit=256 << 10)
    tasks = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        values = line.strip().split("|")
        match = re.fullmatch(re.escape(parent) + r"_([0-9]{1,10})", values[0]) if len(values) == 2 else None
        if not match:
            raise ValueError("Array membership is unavailable or ambiguous; select an exact pending task")
        if values[1] != "PENDING":
            raise ValueError("Array contains non-pending tasks; edit an exact pending task instead of its parent")
        tasks.append(int(match[1]))
        if len(tasks) > 4096:
            raise ValueError("Pending edit is limited to 4096 exact array tasks; select a smaller scope")
    if not tasks or len(tasks) != len(set(tasks)):
        raise ValueError("Array membership is empty or ambiguous; prepare a new review")
    tasks.sort()
    if "_[" in selected:
        from .arrays import parse_range
        requested = parse_range(selected.split("_", 1)[1])
        chosen = [task for task in tasks if any(part["start"] <= task <= part["end"] and (task - part["start"]) % part["step"] == 0 for part in requested)]
        if not chosen:
            raise ValueError("The selected pending range no longer contains pending tasks")
        tasks = chosen
    suffix = ",".join(map(str, tasks))
    return {"parent": parent, "selected": selected, "tasks": tasks, "target": parent + "_[" + suffix + "]"}


def _throttle_value(record):
    raw = record.get("ArrayTaskThrottle")
    if raw is not None:
        return _integer(raw, "Current array throttle", 0, 2 ** 31 - 1)
    task = record.get("ArrayTaskId", "")
    if "%" in task:
        return _integer(task.rsplit("%", 1)[1], "Current array throttle", 0, 2 ** 31 - 1)
    return None


def _array_throttle(params, ctx):
    selected_text = _text(params.get("job_id") or ctx.selected or "", "Array ID")
    compressed = re.fullmatch(r"([0-9]{1,20})_(\[[^\]]{1,4096}\])", selected_text)
    if compressed:
        from .arrays import parse_range
        parse_range(compressed.group(2))  # Bounded arithmetic ranges, never task expansion.
        selected = compressed.group(1)
    else:
        selected = _job_id(params, ctx)
    limit = _integer(params.get("limit", "0"), "Array throttle", 0, 2 ** 31 - 1)
    read_id, record = _array_record(ctx, selected)
    _owned(ctx, record)
    parent = record.get("ArrayJobId", "")
    if not re.fullmatch(r"[0-9]{1,20}", parent):
        raise ValueError("The selected job is not a verified Slurm array")
    if record.get("ArrayJobId") != parent or _state(record) not in _ACTIVE:
        raise ValueError("The array is no longer active or its parent identity is unavailable")
    old = _throttle_value(record)
    payload = dict(job_id=parent, read_id=read_id, identity=_identity(record), limit=limit, old=old,
                   argv=["scontrol", "update", f"JobId={parent}", f"ArrayTaskThrottle={limit}"])
    plan = _ops().prepare_plan(ctx, "array-throttle", params, payload)
    return _ops().report("array-throttle", f"Review concurrency for array {parent}", status="review", rows=[f"Array {parent}; selected record {selected_text}", f"Current throttle: {'unlimited' if old == 0 else old if old is not None else 'not exposed'}", f"New throttle: {'unlimited (0)' if limit == 0 else limit}", "Reducing the limit does not cancel running tasks. Slurm applies the limit to new starts."], data=payload, plan=plan)


def _array_record(ctx, selected):
    """Read one exact child instead of a potentially huge parent response."""
    if "_" in selected:
        return selected, _job(ctx, selected)
    raw = _ops().command(ctx, ["squeue", "--noheader", "--jobs=" + selected, "--format=%i"], limit=256 << 10)
    from .arrays import parse_range
    candidates = []
    for line in raw.splitlines():
        match = re.fullmatch(re.escape(selected) + r"_([0-9]{1,10}|\[[0-9,:%-]{1,4096}\])", line.strip())
        if match:
            candidates.append(selected + "_" + str(parse_range(match[1])[0]["start"]))
        elif line.strip() and line.strip() != selected:
            raise ValueError("Array query returned unrelated or malformed identities")
    if not candidates:
        # A plain job remains a normal validation error, never a mutation.
        return selected, _job(ctx, selected)
    read_id = candidates[0]
    record = _job(ctx, read_id)
    if record.get("ArrayJobId") != selected:
        raise ValueError("Selected task no longer belongs to this array")
    return read_id, record


def _timestamp(value, zone):
    if not value or value.lower() in _NULL or value.upper() in {"UNLIMITED", "INFINITE"}:
        return None
    try:
        value = dt.datetime.fromisoformat(value)
        return value if value.tzinfo else value.replace(tzinfo=zone)
    except (ValueError, TypeError):
        return None


def reservation_timeline(records, now, hours, width=36):
    end = now + dt.timedelta(hours=hours)
    output = []
    for record in records:
        start = _timestamp(record.get("StartTime"), now.tzinfo)
        finish = _timestamp(record.get("EndTime"), now.tzinfo)
        if finish and finish <= now or start and start >= end:
            continue
        known = start is not None and finish is not None and finish >= start
        if known:
            left = max(0, min(width - 1, int((start - now).total_seconds() / (hours * 3600) * width)))
            right = max(left + 1, min(width, math.ceil((finish - now).total_seconds() / (hours * 3600) * width)))
            band = " " * left + "=" * (right - left) + " " * (width - right)
        else:
            band = "?" * width
        output.append(dict(record, timeline="|" + band + "|", window_known=known))
    return sorted(output, key=lambda record: (record.get("StartTime", ""), record.get("ReservationName", "")))


def _reservations(params, ctx):
    hours = _integer(params.get("hours", "48"), "Timeline hours", 1, 24 * 366)
    text = _ops().command(ctx, ["scontrol", "show", "reservation", "-o"], timeout=5)
    records = parse_records(text, "ReservationName")
    warnings = []
    try:
        clock = _ops().command(ctx, ["date", "+%Y-%m-%dT%H:%M:%S%z"], timeout=3, limit=256).strip()
        # date's %z emits -0700; Python 3.10 fromisoformat requires -07:00.
        # Parse the exact requested format on every supported Python version.
        now = dt.datetime.strptime(clock, "%Y-%m-%dT%H:%M:%S%z")
        if now.tzinfo is None:
            raise ValueError("scheduler date has no time zone")
    except (ValueError, RuntimeError, OSError):
        _ops().checkpoint(ctx)
        now = dt.datetime.now().astimezone()
        warnings.append("Scheduler time zone unavailable. Timeline uses Raven's local time zone; inspect the source timestamps.")
    visible = reservation_timeline(records, now, hours)
    rows = [f"Window: {now.isoformat(timespec='seconds')} + {hours} hours"]
    for record in visible:
        rows.extend([f"{record['ReservationName']} {record['timeline']} {record.get('State', '')}",
                     f"  {record.get('StartTime', '?')} -> {record.get('EndTime', '?')}; nodes={record.get('NodeCnt', '?')} cores={record.get('CoreCnt', '?')}",
                     f"  Users={record.get('Users', '?')} Accounts={record.get('Accounts', '?')} Partition={record.get('PartitionName', '?')}",
                     f"  Licenses={record.get('Licenses', '(none exposed)')} Flags={record.get('Flags', '?')}"])
    if not visible:
        rows.append("No visible reservations overlap this window.")
    rows.append("Reservation visibility does not establish access. User, account, group, flags, and site policy determine eligibility.")
    return _ops().report("reservations", f"{len(visible)} reservation windows", status="partial" if warnings else "ok", rows=rows, data=dict(reservations=visible, all_count=len(records), hours=hours), warnings=warnings)


def _licenses(params, ctx):
    name = _text(params.get("name", ""), "License filter", empty=True, limit=128)
    text = _ops().command(ctx, ["scontrol", "show", "licenses", "-o"], timeout=5)
    records = parse_records(text, "LicenseName")
    records = [r for r in records if not name or name.casefold() in r["LicenseName"].casefold()]
    rows, warnings = [], []
    for record in records:
        counts = {}
        for field in ("Total", "Used", "Free", "Reserved", "LastConsumed", "LastDeficit"):
            value = record.get(field)
            if value is not None:
                counts[field] = int(value) if re.fullmatch(r"[0-9]{1,20}", value, re.ASCII) else None
        record["counts"] = counts
        rows.append(f"{record['LicenseName']}: total={record.get('Total', '?')} used={record.get('Used', '?')} free={record.get('Free', '?')} reserved={record.get('Reserved', '?')}")
        rows.append(f"  remote={record.get('Remote', '?')} server={record.get('Server', '(not exposed)')} updated={record.get('LastUpdate', '(not exposed)')}")
        if any(value is None for value in counts.values()) or "Free" not in counts:
            warnings.append(f"{record['LicenseName']}: availability contains unknown values")
    if not rows:
        rows.append("No visible Slurm licenses match this filter.")
    rows.append("Counts describe licenses managed by Slurm. External license-server state may differ; this operation does not reserve licenses.")
    return _ops().report("licenses", f"{len(records)} Slurm license records", status="partial" if warnings else "ok", rows=rows, data={"licenses": records}, warnings=warnings)


def _diagnosis(text):
    lower = text.lower()
    rules = [(('authentication', 'munge', 'invalid credential'), "Authentication: check MUNGE keys, credentials, and host clock synchronization."),
             (('resolve', 'unknown host', 'name or service'), "Name resolution: verify controller/database DNS and the selected cluster profile."),
             (('connection refused', 'unable to contact', 'socket timed out', 'timed out'), "Connectivity: verify service address, firewall, and daemon availability."),
             (('permission denied', 'access denied', 'not authorized'), "Access: this account or site policy does not permit this query."),
             (('not found',), "Installation: the selected host cannot find the required Slurm client command.")]
    return next((message for needles, message in rules if any(needle in lower for needle in needles)), "No automatic diagnosis. Review the command evidence and cluster service logs with the site administrator.")


def _doctor(params, ctx):
    config_keys = {"ClusterName", "SlurmctldHost", "ControlMachine", "SlurmctldPort", "SlurmdPort", "AuthType", "AccountingStorageType", "AccountingStorageHost", "AccountingStoragePort", "JobAcctGatherType", "JobAcctGatherFrequency", "AccountingStorageTRES", "SelectType", "SelectTypeParameters", "SchedulerType", "SlurmctldTimeout", "SlurmdTimeout"}
    commands = [("Client", ["scontrol", "--version"]), ("Controller", ["scontrol", "ping"]),
                ("Configuration", ["scontrol", "show", "config"]),
                ("Partitions", ["sinfo", "-h", "-o", "%P|%a|%l|%D|%t"]),
                ("Accounting", ["sacctmgr", "-n", "-P", "ping"])]
    rows, warnings, probes = [], [], []
    for label, argv in commands:
        try:
            text = _ops().command(ctx, argv, timeout=4, limit=262144)
            if label == "Configuration":
                parsed = {}
                for line in text.splitlines():
                    key, separator, value = line.partition("=")
                    if separator and key.strip() in config_keys:
                        parsed[key.strip()] = value.strip()
                if not parsed:
                    raise ValueError("no supported configuration evidence returned")
                text = "\n".join(f"{key} = {value}" for key, value in sorted(parsed.items()))
                if parsed.get("AccountingStorageType", "").lower() in {"accounting_storage/none", "none"}:
                    warnings.append("Accounting storage is disabled. Historical records and retained batch scripts may be unavailable.")
                if parsed.get("JobAcctGatherType", "").lower() in {"jobacct_gather/none", "none"}:
                    warnings.append("Job accounting collection is disabled. Live CPU and memory evidence may be unavailable.")
            bad = label in {"Controller", "Accounting"} and (
                not re.search(r"\bUP\b", text.upper()) or bool(re.search(r"\bDOWN\b", text.upper())))
            if bad:
                warnings.append(("A controller is down or its health could not be determined. A working backup can still serve the cluster."
                                 if label == "Controller" else "Accounting is down or its health could not be determined from the response."))
            probes.append(dict(name=label, argv=argv, status="warning" if bad else "ok", output=text))
            rows.extend([f"{label}: {'CHECK' if bad else 'OK'}"] + ["  " + line for line in text.splitlines()[:40]])
        except (ValueError, RuntimeError, OSError) as exc:
            _ops().checkpoint(ctx)
            message = str(exc)
            probes.append(dict(name=label, argv=argv, status="unavailable", error=message))
            warnings.append(f"{label}: {message}")
            rows.extend([f"{label}: UNAVAILABLE — {message}", "  " + _diagnosis(message)])
    rows.append("Doctor runs read-only client queries. It does not restart daemons, read secret configuration files, or change cluster settings.")
    return _ops().report("slurm-doctor", "Cluster service evidence collected", status="partial" if warnings else "ok", rows=rows, data={"probes": probes}, warnings=warnings)


def _historical_job(ctx, job_id):
    output = _ops().command(ctx, ["sacct", "-X", "-n", "-P", "-j", job_id, "--starttime=1970-01-01", "--format=JobID,User,Submit,Start,State,Cluster"], timeout=5)
    matches = []
    for line in output.splitlines():
        parts = line.split("|")
        if len(parts) == 7 and parts[-1] == "":
            parts.pop()
        if len(parts) == 6 and parts[0] == job_id:
            matches.append(dict(zip(("JobId", "UserId", "SubmitTime", "StartTime", "JobState", "Cluster"), parts)))
    if len(matches) != 1:
        raise ValueError("Accounting cannot uniquely identify the selected historical job")
    _identity(matches[0])
    return matches[0]


def _retained_identity(ctx, job_id):
    try:
        return "controller", _job(ctx, job_id)
    except (ValueError, RuntimeError, OSError) as exc:
        try:
            return "accounting", _historical_job(ctx, job_id)
        except (ValueError, RuntimeError, OSError) as historical:
            raise ValueError(f"Cannot verify retained job: controller: {exc}; accounting: {historical}") from None


def _retained_script(ctx, job_id, source):
    argv = ["scontrol", "write", "batch_script", job_id, "-"] if source == "controller" else ["sacct", "-n", "-j", job_id, "--batch-script"]
    text = _ops().command(ctx, argv, timeout=5, limit=_MAX_SCRIPT)
    # sacct may place a descriptive heading before the original shebang. Never
    # save its unavailable/error text as if it were a batch script.
    lines = text.splitlines(keepends=True)
    first = next((index for index, line in enumerate(lines) if line.startswith("#!")), None)
    if first is None or first > 5 or "\x00" in text:
        raise ValueError("Slurm did not return an identifiable retained batch script; accounting retention may be disabled")
    if first and not all(not line.strip() or re.match(r"(?:Batch Script|JobID|[-=]+)", line, re.I) for line in lines[:first]):
        raise ValueError("Unrecognized retained-script wrapper; no file was saved")
    return "".join(lines[first:])


def _new_output(path):
    path = os.path.abspath(os.path.expanduser(_text(path, "Output path")))
    parent = os.path.realpath(os.path.dirname(path))
    path = os.path.join(parent, os.path.basename(path))
    if os.path.lexists(path):
        raise ValueError("Output already exists; choose a new filename")
    st = os.stat(parent)
    if not stat.S_ISDIR(st.st_mode):
        raise ValueError("Output parent must be an existing directory")
    return path, [st.st_dev, st.st_ino]


def _batch_script(params, ctx):
    job_id = _job_id(params, ctx)
    source, record = _retained_identity(ctx, job_id)
    script = _retained_script(ctx, job_id, source)
    digest = hashlib.sha256(script.encode("utf-8")).hexdigest()
    rows = [f"Job {job_id}; source={source}; submitted={record['SubmitTime']}", f"SHA256 {digest}", "Script preview (first 200 lines):"] + script.splitlines()[:200]
    data = dict(job_id=job_id, source=source, identity=_identity(record), sha256=digest, bytes=len(script.encode("utf-8")), script=script)
    plan = None
    if params.get("output"):
        _ops().local_only(ctx)
        output, parent_identity = _new_output(params["output"])
        payload = {key: value for key, value in data.items() if key != "script"}
        payload.update(output=output, parent_identity=parent_identity)
        plan = _ops().prepare_plan(ctx, "batch-script", params, payload)
        rows.append(f"Save after review: {output} (new file only; mode 0600)")
    return _ops().report("batch-script", f"Retained batch script for {job_id}", status="review" if plan else "ok", rows=rows, data=data, plan=plan)


def _rerun_source(ctx, job_id):
    """The selected accounting attempt is evidence, never permission to target a newer one."""
    records = [row for row in ctx.jobs + ctx.finished if str(row.get("id", "")) == job_id]
    if not records:
        raise ValueError("Select the cancelled job from the current Recents or History list")
    keys = ("id", "state", "submit", "start", "end", "user", "account", "cluster")
    sources = [{key: row.get(key, "") for key in keys} for row in records]
    if any(source != sources[0] for source in sources[1:]):
        raise ValueError("The selected job has ambiguous or changed attempt records; refresh the job list")
    source = sources[0]
    if not isinstance(source["state"], str) or not re.fullmatch(r"CANCELLED(?:\+| by [0-9]{1,20})?", source["state"], flags=re.ASCII):
        raise ValueError("Rerun applies to one cancelled job; its current source attempt is no longer cancelled")
    for key in ("submit", "end"):
        _rerun_time(source[key], "Selected job " + key)
    if source["start"] and str(source["start"]).lower() not in _NULL:
        _rerun_time(source["start"], "Selected job start")
    return source


def _rerun_time(value, label):
    value = _text(value, label, limit=64)
    try:
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})?", value, flags=re.ASCII):
            raise ValueError("not an ISO timestamp")
        dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(label + " must contain a verifiable scheduler timestamp") from exc
    return value


def _rerun_record(ctx, job_id, source):
    """Reject every ID form for which native requeue can broaden the request."""
    if not re.fullmatch(r"[1-9][0-9]{0,19}(?:_[0-9]{1,10})?", job_id, flags=re.ASCII):
        raise ValueError("Rerun needs one regular job or exact numeric array task, without ranges or heterogeneous components")
    try:
        record = _job(ctx, job_id)
    except (ValueError, RuntimeError, OSError) as exc:
        _ops().checkpoint(ctx)
        raise ValueError("Slurm no longer exposes a verifiable retained job. Exact rerun is unavailable; no launch script was rerun: " + _ops().clean(exc, 400)) from exc
    _owned(ctx, record)
    if record.get("JobState") != "CANCELLED":
        raise ValueError("The retained job is no longer fully cancelled; refresh and select its current attempt")
    batch_flag = record.get("BatchFlag", "")
    if not re.fullmatch(r"[0-9]{1,10}", batch_flag, flags=re.ASCII) or not 0 < int(batch_flag) <= 4294967295:
        raise ValueError("Rerun requires a retained batch job, not an interactive allocation")
    if record.get("Requeue") != "1":
        raise ValueError("This job or site disables requeue; Raven will not change that policy or reconstruct a submission")
    for key in ("HetJobId", "HetJobIdSet", "HetJobOffset", "PackJobId", "PackJobIdSet", "PackJobOffset"):
        if key in record and record[key].lower() not in _NULL:
            raise ValueError("Heterogeneous jobs cannot be rerun individually: Slurm can restart all components")
    parent, separator, task = job_id.partition("_")
    if separator:
        if record.get("ArrayJobId") != parent or record.get("ArrayTaskId") != task:
            raise ValueError("Slurm did not return the exact selected array task")
    elif any(record.get(key, "").lower() not in _NULL for key in ("ArrayJobId", "ArrayTaskId")):
        raise ValueError("Choose an explicit ARRAY_TASK ID; a bare array ID can restart the whole array")
    if not re.fullmatch(r"[1-9][0-9]{0,19}", record.get("JobId", ""), flags=re.ASCII):
        raise ValueError("Slurm returned an invalid allocation ID")
    for key in ("SubmitTime", "EndTime"):
        _rerun_time(record.get(key, ""), "Retained job " + key)
    for selected, retained in (("submit", "SubmitTime"), ("start", "StartTime"), ("end", "EndTime")):
        left, right = source[selected], record.get(retained, "")
        if selected == "start" and str(left).lower() in _NULL and right.lower() in _NULL:
            continue
        if left != right:
            raise ValueError("The controller job does not match the selected cancelled attempt; refresh the job list")
    for key in ("Restarts", "RestartCnt"):
        if key in record and not re.fullmatch(r"[0-9]{1,10}", record[key], flags=re.ASCII):
            raise ValueError("Slurm returned an invalid restart counter")
    if not any(key in record for key in ("Restarts", "RestartCnt")):
        raise ValueError("A retained restart counter is required to distinguish the selected job attempt")
    if source["user"] and source["user"] != record["UserId"].split("(", 1)[0]:
        raise ValueError("The selected job owner does not match its retained launch")
    if source["account"] and source["account"] != record.get("Account"):
        raise ValueError("The selected job account does not match its retained launch")
    if source["cluster"]:
        cluster = record.get("Cluster", "")
        if not cluster:
            config = _ops().command(ctx, ["scontrol", "show", "config"], timeout=5)
            matches = re.findall(r"^\s*ClusterName\s*=\s*(\S+)\s*$", config, re.M)
            if len(matches) == 1:
                cluster = matches[0]
        if cluster != source["cluster"]:
            raise ValueError("The selected job belongs to a different or unverified cluster")
    return record


def _rerun_evidence(record):
    # Keep unknown site/version request fields too. The only exclusions are
    # display clocks and scheduling observations, not launch configuration.
    volatile = {"RunTime", "LastSchedEval", "EligibleTime", "AccrueTime", "Priority"}
    return {key: value for key, value in record.items() if key not in volatile}


def _rerun_job(params, ctx):
    job_id = _job_id(params, ctx)
    source = _rerun_source(ctx, job_id)
    record = _rerun_record(ctx, job_id, source)
    warnings, script_digest, preview = [], "", []
    try:
        script = _retained_script(ctx, job_id, "controller")
        script_digest = hashlib.sha256(script.encode("utf-8")).hexdigest()
        preview = [_ops().clean(line, 512) for line in script[:8192].splitlines()[:40]]
    except (ValueError, RuntimeError, OSError) as exc:
        _ops().checkpoint(ctx)
        warnings.append("Stored script preview is unavailable: " + _ops().clean(exc, 300)
                        + ". Requeue uses Slurm's retained launch; Raven will not read the original script path.")
    # Script retrieval is another RPC. Pin the complete reviewed request after
    # it, and reject an attempt/configuration change during preparation.
    current = _rerun_record(ctx, job_id, source)
    if _rerun_evidence(current) != _rerun_evidence(record):
        raise ValueError("The retained launch changed during inspection; prepare a new review")
    payload = dict(job_id=job_id, source=source, identity=_identity(current),
                   evidence=_rerun_evidence(current), script_sha256=script_digest,
                   argv=["scontrol", "requeue", job_id])
    plan = _ops().prepare_plan(ctx, "rerun-job", {"job_id": job_id}, payload)
    rows = [f"Rerun exactly job {job_id}; owner={current['UserId']}; cancelled={current['EndTime']}",
            "Command: " + " ".join(payload["argv"]),
            "Slurm keeps the same job ID and starts a new attempt from its retained batch script, arguments, exported environment and job settings.",
            "Raven does not execute a batch launcher, resubmit the group, or use your current shell environment.",
            "Current dependency state: " + current.get("Dependency", "not exposed"),
            "Dependencies are not rewritten or restored to an earlier graph. Unsatisfied dependencies can keep this attempt pending.",
            "Work directory: " + current.get("WorkDir", "not exposed"),
            "Standard output: " + current.get("StdOut", "not exposed"),
            "Standard error: " + current.get("StdErr", "not exposed"),
            "Existing output paths are reused. Slurm's open-mode policy can append to or truncate existing logs.",
            "External inputs, executables and other files are not snapshots; their current contents will be used."]
    if script_digest:
        rows.extend(["Retained batch script SHA256: " + script_digest, "Stored script preview (bounded):", *preview])
    return _ops().report("rerun-job", f"Review rerun of cancelled job {job_id}", status="review", rows=rows,
                         data={"job_id": job_id, "source": source, "script_sha256": script_digest,
                               "script_preview": preview, "dependency": current.get("Dependency", ""),
                               "stdout": current.get("StdOut", ""), "stderr": current.get("StdErr", "")},
                         warnings=warnings, plan=plan)


def _apply_rerun(ctx, payload):
    job_id = payload["job_id"]
    source = _rerun_source(ctx, job_id)
    if source != payload["source"]:
        raise ValueError("The selected cancelled attempt changed after review")
    record = _rerun_record(ctx, job_id, source)
    if _rerun_evidence(record) != payload["evidence"]:
        raise ValueError("The retained launch settings or attempt changed after review")
    if payload["script_sha256"]:
        script = _retained_script(ctx, job_id, "controller")
        if hashlib.sha256(script.encode("utf-8")).hexdigest() != payload["script_sha256"]:
            raise ValueError("The retained batch script changed after review")
        record = _rerun_record(ctx, job_id, source)
        if _rerun_evidence(record) != payload["evidence"]:
            raise ValueError("The retained launch changed before dispatch; no rerun was sent")
    # Rebuild the only allowed mutation argv; never replay arbitrary plan argv.
    output = _ops().command(ctx, ["scontrol", "requeue", job_id], timeout=8, limit=65536)
    data = {"job_id": job_id, "accepted": True, "confirmed": False, "rerun_dispatched": True,
            "output": output, "previous_attempt": source}
    warnings = []
    rows = [f"Slurm accepted requeue of {job_id}. The original job ID is retained.", output or "No scheduler message."]
    try:
        after = _job(ctx, job_id)
        _owned(ctx, after)
        # SubmitTime changes on requeue. A strictly advanced restart counter is
        # stronger evidence than observing a transient PENDING/RUNNING state.
        counter = "Restarts" if "Restarts" in record else "RestartCnt"
        value = after.get(counter, "")
        confirmed = (after.get("JobId") == record["JobId"]
                     and after.get("ArrayJobId", "") == record.get("ArrayJobId", "")
                     and after.get("ArrayTaskId", "") == record.get("ArrayTaskId", "")
                     and re.fullmatch(r"[0-9]{1,10}", value, flags=re.ASCII) is not None
                     and int(value) > int(record[counter]))
        data.update(confirmed=confirmed, observed_state=_state(after), observed_restarts=value)
        rows.append(f"Readback: state={_state(after)}; restart count={value or 'not exposed'}")
        if not confirmed:
            warnings.append("Requeue was accepted, but a new attempt is not yet confirmed. Refresh this job; do not retry automatically.")
    except (ValueError, RuntimeError, OSError) as exc:
        warnings.append("Requeue was accepted, but readback is unavailable: " + _ops().clean(exc, 400)
                        + ". Inspect the job before another attempt; do not retry automatically.")
    return _ops().report("rerun-job", "Job rerun confirmed" if data["confirmed"] else "Rerun accepted; confirmation needed",
                         status="ok" if data["confirmed"] else "partial", rows=rows, data=data, warnings=warnings)


def _steps(ctx, job_id):
    output = _ops().command(ctx, ["scontrol", "show", "step", "-o", job_id], timeout=5, limit=65536)
    steps = []
    for record in parse_records(output, "StepId"):
        step_id = record.get("StepId", "")
        if re.fullmatch(re.escape(job_id) + r"\.[0-9]{1,10}", step_id) and record.get("State", "").upper() == "RUNNING":
            steps.append(step_id)
    return sorted(set(steps), key=lambda value: int(value.rsplit(".", 1)[1]))


def _allocation_shell(params, ctx):
    _ops().local_only(ctx)
    job_id = _job_id(params, ctx)
    record = _job(ctx, job_id)
    _owned(ctx, record)
    if _state(record) != "RUNNING":
        raise ValueError("The selected allocation must be running")
    mode = params.get("mode", "shell")
    allocation_id = record.get("JobId", "")
    if not re.fullmatch(r"[0-9]{1,20}", allocation_id):
        raise ValueError("The scheduler did not expose the numeric allocation ID required for terminal attachment")
    if mode not in {"shell", "attach"}:
        raise ValueError("Mode must be shell or attach")
    display = params.get("display", "embedded")
    if display not in {"embedded", "fullscreen"}:
        raise ValueError("Terminal display must be embedded or fullscreen")
    node = _text(params.get("node", ""), "Node", empty=True, limit=253)
    task = _text(params.get("task", ""), "Task", empty=True, limit=12)
    if node and (mode != "shell" or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,252}", node)):
        raise ValueError("Choose one exact allocation node for a shell; node selection is not an sattach task selector")
    if task and mode != "attach":
        raise ValueError("Task selection applies only to an existing numeric step attachment")
    if node:
        _allocation_node(ctx, record, node)
    warnings = []
    try:
        steps = _steps(ctx, allocation_id)
    except (ValueError, RuntimeError, OSError) as exc:
        _ops().checkpoint(ctx)
        if mode == "attach":
            raise
        steps = []
        warnings.append(f"Existing step inventory is unavailable: {exc}. The reviewed shell will create a new step.")
    if mode == "attach":
        step = _text(params.get("step_id", ""), "Step ID", limit=64)
        if re.fullmatch(r"[0-9]{1,10}", step):
            step = allocation_id + "." + step
        if step not in steps:
            raise ValueError("Choose a running numeric step from this allocation: " + (", ".join(steps) or "none available"))
        argv = ["sattach", step]
        if task:
            task = str(_integer(task, "Task", 0, 999999999))
            _attachment_task(ctx, step, task)
            argv = ["sattach", f"--input-filter={task}", f"--output-filter={task}", step]
    elif mode == "shell":
        shell = _text(params.get("shell", "/bin/bash"), "Shell path", limit=4096)
        if not shell.startswith("/") or shell.endswith("/"):
            raise ValueError("Shell must be an absolute executable path on the compute node")
        # --overlap prevents waiting behind the batch step; --immediate bounds a
        # resource/launch failure. No unbounded salloc or new allocation is made.
        argv = ["srun", f"--jobid={allocation_id}", "--overlap", "--exact", "--nodes=1", "--ntasks=1", "--cpus-per-task=1", "--immediate=5", "--pty", shell]
        if node:
            argv.insert(-2, "--nodelist=" + node)
        step = ""
    payload = dict(job_id=job_id, allocation_id=allocation_id, identity=_identity(record, running=True), mode=mode, step_id=step, argv=argv,
                   node=node, task=task, display=display)
    plan = _ops().prepare_plan(ctx, "allocation-shell", params, payload)
    rows = [f"Running allocation {job_id}; start={record['StartTime']}; owner={record['UserId']}", "Command: " + " ".join(argv), "Running numeric steps: " + (", ".join(steps) or "none"),
            "Apply opens this session in Raven's Terminal pane." if display == "embedded" else "Apply suspends Raven's terminal UI. Exit the shell or detach to return."]
    if mode == "shell":
        rows.append("This shell shares allocation resources. It does not modify the batch script or create a new allocation.")
    else:
        rows.append("sattach joins the selected step's input/output. Only attach when interactive input is appropriate for that application.")
    return _ops().report("allocation-shell", f"Review {mode} for allocation {job_id}", status="review", rows=rows, data={"steps": steps, **payload}, warnings=warnings, plan=plan)


def _allocation_node(ctx, record, node):
    """Expand one reviewed allocation's hostlist, never the cluster inventory."""
    nodelist = _text(record.get("NodeList", ""), "Allocation node list", limit=8192)
    if nodelist.lower() in _NULL or nodelist.startswith("-"):
        raise ValueError("The allocation node list is unavailable")
    output = _ops().command(ctx, ["scontrol", "show", "hostnames", nodelist], timeout=5, limit=1 << 18)
    nodes = output.splitlines()
    if len(nodes) > 16384 or node not in nodes:
        raise ValueError("The selected node is not in this allocation")


def _attachment_task(ctx, step, task):
    output = _ops().command(ctx, ["scontrol", "show", "step", "-o", step], timeout=5)
    records = [r for r in parse_records(output, "StepId") if r.get("StepId") == step]
    if len(records) != 1 or records[0].get("State", "").upper() != "RUNNING":
        raise ValueError("The exact attachment step is no longer running")
    count = records[0].get("Tasks", records[0].get("NumTasks", ""))
    if not count.isascii() or not count.isdigit() or len(count) > 10 or int(task) >= int(count):
        raise ValueError("The step does not expose a valid task count for that exact rank")


_RUN = {"storage": _storage, "pending-edit": _pending_edit, "array-throttle": _array_throttle,
        "reservations": _reservations, "licenses": _licenses, "slurm-doctor": _doctor,
        "batch-script": _batch_script, "allocation-shell": _allocation_shell, "rerun-job": _rerun_job}


def run(feature, params, ctx):
    if feature not in _RUN:
        raise ValueError("Unknown cluster operation")
    return _RUN[feature](params, ctx)


def _save_script(ctx, payload):
    _ops().local_only(ctx)
    source, record = _retained_identity(ctx, payload["job_id"])
    if source != payload["source"] or _identity(record) != payload["identity"]:
        raise ValueError("Retained job identity or source changed; prepare a new review")
    script = _retained_script(ctx, payload["job_id"], source).encode("utf-8")
    if hashlib.sha256(script).hexdigest() != payload["sha256"]:
        raise ValueError("Retained script changed; prepare a new review")
    parent, filename = os.path.split(payload["output"])
    directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
    try:
        st = os.fstat(directory)
        if [st.st_dev, st.st_ino] != payload["parent_identity"]:
            raise ValueError("Output directory changed; prepare a new review")
        descriptor = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=directory)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(script)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            os.unlink(filename, dir_fd=directory)
            raise
    finally:
        os.close(directory)
    return _ops().report("batch-script", "Retained script saved", rows=[payload["output"], "SHA256 " + payload["sha256"]], data={"output": payload["output"], "sha256": payload["sha256"]})


def apply(feature, plan, ctx):
    if feature not in {"pending-edit", "array-throttle", "batch-script", "allocation-shell", "rerun-job"}:
        raise ValueError("This operation is read only")
    payload = _ops().validate_plan(plan, ctx, feature)
    if feature == "batch-script":
        return _save_script(ctx, payload)
    if feature == "rerun-job":
        return _apply_rerun(ctx, payload)
    if feature == "pending-edit":
        record = _check_identity(ctx, payload, pending=True)
        reviewed_scope = payload.get("array_scope")
        selected = reviewed_scope["selected"] if reviewed_scope else payload["job_id"]
        if _pending_array_scope(ctx, record, selected) != reviewed_scope:
            raise ValueError("Pending array membership changed after review; no update was sent")
        if record.get(payload["old_key"]) != payload["old"]:
            raise ValueError("The pending-job field changed after review; prepare again")
        # Validate again defensively even though the plan is digest-bound.
        _edit_value(payload["field"], payload["value"])
    elif feature == "array-throttle":
        record = _check_identity(ctx, payload)
        if record.get("ArrayJobId") != payload["job_id"] or _state(record) not in _ACTIVE:
            raise ValueError("The array is no longer active")
        if _throttle_value(record) != payload["old"]:
            raise ValueError("Array throttle changed after review; prepare again")
    else:
        _ops().local_only(ctx)
        record = _check_identity(ctx, payload, running=True)
        if payload["mode"] == "attach" and payload["step_id"] not in _steps(ctx, payload["allocation_id"]):
            raise ValueError("The reviewed step is no longer running")
        if payload.get("node"):
            _allocation_node(ctx, record, payload["node"])
        if payload.get("task"):
            _attachment_task(ctx, payload["step_id"], payload["task"])
        return _ops().report(feature, "Terminal handoff ready", rows=[" ".join(payload["argv"])], data={"terminal_argv": payload["argv"], "terminal_identity": payload, "job_id": payload["job_id"], "identity": payload["identity"]})
    applied_at = time.time()
    output = _ops().command(ctx, payload["argv"], timeout=8)
    data = {"job_id": payload["job_id"], "output": output, "accepted": True, "confirmed": False}
    rows = [" ".join(payload["argv"]), output or "Slurm accepted the update."]
    warnings = []
    try:
        after = _job(ctx, payload.get("read_id", payload["job_id"]))
        if _identity(after) != payload["identity"]:
            raise ValueError("The job attempt changed before confirmation")
        if feature == "array-throttle":
            actual = _throttle_value(after)
            confirmed = actual == payload["limit"]
        else:
            actual = after.get(payload["field"], after.get(payload["old_key"]))
            confirmed = _same_value(payload["field"], payload["value"], actual, reference=applied_at)
        data.update(observed=actual, confirmed=confirmed, observed_state=_state(after))
        rows.append(f"Readback: {actual if actual is not None else 'not exposed'}; state={_state(after)}")
        if not confirmed:
            warnings.append("The update was accepted, but readback cannot confirm the requested value. Inspect the job before making another change; do not retry automatically.")
    except (ValueError, RuntimeError, OSError) as exc:
        warnings.append(f"The update was accepted, but confirmation is unavailable: {exc}. Inspect current state before another change.")
    return _ops().report(feature, "Scheduler update confirmed" if data["confirmed"] else "Scheduler accepted update; confirmation needed", status="ok" if data["confirmed"] else "partial", rows=rows, data=data, warnings=warnings)


def _same_value(field, expected, observed, *, reference=None):
    if observed is None:
        return False
    if field == "TimeLimit":
        from .model import secs
        def limit(value):
            if value.upper() in {"UNLIMITED", "INFINITE"}:
                return "unlimited"
            return int(value) * 60 if value.isascii() and value.isdigit() else secs(value)
        wanted, actual = limit(expected), limit(observed)
        return wanted is not None and actual is not None and wanted == actual
    if field == "Dependency" and expected == "0":
        return observed.lower() in _NULL or observed == "0"
    if field == "Dependency":
        from .deps import normalized_dependency as _normalized_dependency
        return _normalized_dependency(expected) == _normalized_dependency(observed)
    if field == "BeginTime":
        if expected == observed:
            return True
        try:
            return dt.datetime.fromisoformat(expected) == dt.datetime.fromisoformat(observed)
        except ValueError:
            match = re.fullmatch(r"now(?:\+([0-9]+)(seconds?|minutes?|hours?|days?|weeks?))?", expected, re.I)
            if not match:
                return False
            try:
                actual = dt.datetime.fromisoformat(observed).timestamp()
            except (ValueError, OverflowError):
                return False
            scale = {"second": 1, "minute": 60, "hour": 3600, "day": 86400, "week": 604800}
            seconds = int(match[1]) * scale[match[2].lower().rstrip("s")] if match[1] else 0
            wanted = (time.time() if reference is None else reference) + seconds
            # Slurm's minute-granularity scheduling may round a requested
            # BeginTime. This tolerance concerns readback, never identity.
            return abs(actual - wanted) <= 60
    return expected == observed


def revalidate_terminal(ctx, identity):
    """Root calls this in a worker immediately before its terminal handoff."""
    _ops().local_only(ctx)
    _ops().checkpoint(ctx)
    record = _check_identity(ctx, identity, running=True)
    if identity["mode"] == "attach" and identity["step_id"] not in _steps(ctx, identity["allocation_id"]):
        raise ValueError("The reviewed step is no longer running")
    if identity.get("node"):
        _allocation_node(ctx, record, identity["node"])
    if identity.get("task"):
        _attachment_task(ctx, identity["step_id"], identity["task"])
    return list(identity["argv"])


# Managed Raven entry points also protect direct adapter callers.
from .operations import managed_run as _managed_run, managed_apply as _managed_apply
run = _managed_run(run)
apply = _managed_apply(apply)
