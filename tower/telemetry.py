"""Read-only telemetry capability evidence, independent of the terminal UI.

Polling a collector is not the same as sampling its upstream producer. This
module deliberately does not infer a measured collection period from defaults,
repeated values, or the host on which Tower happens to run.
"""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
import math
import re
import time

SCHEMA = "tower.telemetry/v1"
CONFIG_TTL = 120.0
MAX_CONNECTIONS = 8
MAX_OUTPUT = 262144
TIMEOUT = 4.0
_CONFIG_KEYS = frozenset(("ClusterName", "JobAcctGatherType", "JobAcctGatherFrequency",
    "JobAcctGatherParams", "AccountingStorageTRES", "CgroupPlugin", "ProctrackType", "TaskPlugin"))
_JOB_KEYS = frozenset(("JobId", "ArrayJobId", "ArrayTaskId", "HetJobId", "HetJobOffset", "SubmitTime", "StartTime",
    "JobState", "AcctGatherFrequency", "AcctGatherFreq", "JobAcctGatherFrequency", "RestartCnt"))
_EMPTY = frozenset(("", "(null)", "none", "n/a", "unknown", "not set"))
_JOB_ID = re.compile(r"[0-9]{1,20}(?:_[0-9]{1,20})?(?:\+[0-9]{1,8})?\Z")


def clean(value, limit=1024):
    """Bound untrusted scheduler text and strip terminal control characters."""
    return "".join(c for c in str(value)[:limit] if c.isprintable())


def validate_job_id(value):
    if value is None or value == "":
        return ""
    if not isinstance(value, str) or not _JOB_ID.fullmatch(value):
        raise ValueError("Choose one exact Slurm job or array-task ID (not an array range or step).")
    return value


def _number(value):
    if type(value) not in (int, float):
        return None
    try:
        return float(value) if math.isfinite(value) and 0 <= value < 1e12 else None
    except OverflowError:
        return None


def _bounded_output(output):
    if not isinstance(output, str):
        raise ValueError("The scheduler returned non-text output.")
    if len(output) > MAX_OUTPUT:
        raise ValueError("Scheduler response exceeds the 256 KiB inspection limit.")
    return output


def parse_config(output):
    """Extract an allowlist; do not publish arbitrary site configuration."""
    values = {}
    for line in _bounded_output(output).splitlines():
        key, sep, value = line.partition("=")
        key = key.strip()
        if sep and key in _CONFIG_KEYS:
            # Conflicting duplicate fields are untrustworthy, not last-wins.
            text = clean(value.strip())
            values[key] = text if key not in values or values[key] == text else "unknown"
    return values


def parse_job(output):
    values = {}
    raw = _bounded_output(output)
    if len(re.findall(r"(?:^|\s)JobId=", raw)) != 1:
        raise ValueError("Expected exactly one scheduler job record.")
    for match in re.finditer(r"(?:^|\s)([A-Za-z][A-Za-z0-9]*)=(\S*)", raw):
        key, value = match.groups()
        if key in _JOB_KEYS:
            text = clean(value)
            values[key] = text if key not in values or values[key] == text else "unknown"
    return values


def parse_frequency(value):
    """Parse Slurm's task/energy/network/filesystem frequency syntax.

    Zero is retained: it means no periodic accounting for that category.
    Malformed or duplicate categories are unknown, never silently defaulted.
    """
    if not isinstance(value, str) or len(value) > 1024 or value.lower().strip() in _EMPTY:
        return {}
    text = value.strip()
    if text.isascii() and text.isdigit():
        return {"task": int(text)} if len(text) <= 9 else {}
    result = {}
    for item in text.split(","):
        key, sep, number = item.strip().partition("=")
        if key not in ("task", "energy", "network", "filesystem"):
            continue
        valid = sep and number.isascii() and number.isdigit() and len(number) <= 9
        result[key] = int(number) if valid and key not in result else None
    return result


def matches_job(details, job_id, expected=None):
    """Reject another job/attempt, including allocation-ID array responses."""
    actual = details.get("JobId", "")
    if "+" in job_id:
        leader, offset = job_id.split("+", 1)
        identity_ok = (actual == job_id or
                       details.get("HetJobId") == leader and details.get("HetJobOffset") == offset)
    elif "_" in job_id:
        base, index = job_id.split("_", 1)
        identity_ok = (actual == job_id or
                       details.get("ArrayJobId") == base and details.get("ArrayTaskId") == index)
    else:
        identity_ok = actual == job_id
    if not identity_ok:
        return False
    for field, key in (("submit", "SubmitTime"), ("start", "StartTime")):
        wanted = (expected or {}).get(field)
        # A pending job's predicted start can change and is not an attempt ID.
        if field == "start" and (expected or {}).get("state") in ("PENDING", "CONFIGURING", "", None):
            continue
        if wanted and wanted != details.get(key):
            return False
    return True


def build_report(config, job, *, job_id="", expected=None, intervals=None, health=None,
                 errors=(), observed_at=None, config_at=None, config_cached=False,
                 gpu_enabled=None, gpu_observation=None, live_observation=None):
    """Return a bounded, serializable report from evidence already collected."""
    config = {k: clean(v) for k, v in config.items() if k in _CONFIG_KEYS}
    job = {k: clean(v) for k, v in job.items() if k in _JOB_KEYS}
    intervals, health = intervals or {}, health or {}
    configured = parse_frequency(config.get("JobAcctGatherFrequency", "")).get("task")
    override_key = next((key for key in ("AcctGatherFrequency", "AcctGatherFreq", "JobAcctGatherFrequency") if key in job), None)
    override = parse_frequency(job.get(override_key, "")) if override_key else {}
    requested = override.get("task") if "task" in override else None
    if requested is not None:
        basis = "Job-requested task interval; individual steps may override it."
    elif override_key and job[override_key].lower() in _EMPTY:
        requested = configured
        basis = "No job override reported; configured task interval applies unless a step overrides it."
    elif override_key and "task" not in override and override:
        requested = configured
        basis = "Job override covers other categories; task interval inherits configuration unless a step overrides it."
    else:
        basis = "Job/step override is not exposed or is invalid; effective collection interval is unknown."
    plugin = config.get("JobAcctGatherType", "unknown")
    supported = plugin in ("jobacct_gather/linux", "jobacct_gather/cgroup")
    disabled = plugin in ("jobacct_gather/none", "none")
    task_capability = "disabled" if disabled else "configured" if supported else "unknown"
    if disabled:
        basis = "Task accounting is disabled by JobAcctGatherType."
    elif requested == 0:
        basis = "Periodic task accounting is disabled; Slurm may collect a final termination sample."
    cgroup_v2 = (plugin == "jobacct_gather/cgroup" and config.get("CgroupPlugin") == "cgroup/v2")
    rows = []

    def add(name, source, capability, producer_seconds=None, note="", observation=None):
        source_health = health.get(source, {})
        if not isinstance(source_health, dict):
            source_health = {}
        rows.append({"name": name, "source": source, "capability": capability,
                     "tower_interval_seconds": _number(intervals.get(source)),
                     "producer_interval_seconds": producer_seconds,
                     "last_success_at": _number(source_health.get("last_ok")),
                     "backoff_seconds": _number(source_health.get("backoff")),
                     "enabled": source_health.get("enabled") if type(source_health.get("enabled")) is bool else None,
                     "error": clean(source_health.get("error", "")),
                     "observation": observation, "note": note})

    add("CPU time / efficiency", "live", task_capability, requested if not disabled else None,
        basis + " CPU rate needs at least two changing CPU-time observations.", live_observation)
    add("Resident memory (RSS)", "live", task_capability, requested if not disabled else None,
        basis + " MaxRSS is a peak, so an unchanged value can be correct.", live_observation)
    add("Virtual memory", "live", "unsupported" if cgroup_v2 else "unknown", None,
        "cgroup/v2 does not report virtual-memory counters; zero must not be read as measured usage."
        if cgroup_v2 else "Tower does not plot virtual memory. cgroup/v2 cannot report it; the compute-node cgroup version may not be exposed here.")
    add("GPU utilization / device memory", "gpu", "disabled" if gpu_enabled is False else "observed" if gpu_observation else "unknown", None,
        "Tower uses its device collector independently of Slurm task accounting. Device support, allocation mapping, permissions, and producer timing can differ.", gpu_observation)
    add("GPU trace file", "trace", "unknown", None,
        "Polling reads a job-linked file; new samples arrive only when its producer writes them. No write interval is inferred.")
    add("Project-reported metrics", "research", "unknown", None,
        "The Tower interval shown is the baseline file-reader interval; selected metric demand may read faster. Writer frequency and timestamps determine freshness. Missing values are not zero.")
    attempt = dict(expected or {})
    for field, key in (("submit", "SubmitTime"), ("start", "StartTime"), ("state", "JobState")):
        if job.get(key):
            attempt[field] = job[key]
    if config.get("ClusterName"):
        attempt["cluster"] = config["ClusterName"]
    return {"schema": SCHEMA, "status": ("partial" if config or job else "unavailable") if errors else "ok",
            "job_id": job_id, "attempt": attempt,
            "observed_at": time.time() if observed_at is None else observed_at,
            "config_at": config_at, "config_cached": bool(config_cached),
            "cluster": config.get("ClusterName", "unknown"), "config": config, "job": job,
            "configured_task_seconds": configured, "job_task_seconds": requested,
            "job_frequency_field": override_key, "metrics": rows,
            "errors": [clean(e) for e in list(errors)[:8]],
            "notes": ["A Tower read interval is not a guarantee of new upstream measurements.",
                      "Configured/job-requested intervals are evidence, not measured producer cadence.",
                      "Last successful read describes collector health, not the age of every metric value.",
                      "Backoff, queued work, remote latency, and job completion can delay reads."]}


class Inspector:
    """Bounded configuration cache; all scheduler I/O belongs in a worker."""
    def __init__(self, *, ttl=CONFIG_TTL, clock=time.monotonic):
        self.ttl, self.clock = ttl, clock
        self.cache = OrderedDict()

    def inspect(self, backend, *, scope, job_id="", expected=None, refresh=False,
                intervals=None, health=None, **observations):
        job_id = validate_job_id(job_id)
        now, errors = self.clock(), []
        def observe(argv):
            # This inspector is an explicit selected-job read. Use the same
            # collection owner and its finite fresh-validation lane; routine
            # sampler burst exhaustion must not bypass or replace that owner.
            if getattr(backend, "ledger", None) is not None:
                return backend.command(argv, TIMEOUT, MAX_OUTPUT, None)
            return backend.run(argv, timeout=TIMEOUT)[0]
        cached = self.cache.get(scope)
        use_cache = bool(not refresh and cached and 0 <= now - cached[0] < self.ttl)
        if use_cache:
            config, config_at, cached_error = deepcopy(cached[1]), cached[2], cached[3]
            self.cache.move_to_end(scope)
            if cached_error:
                errors.append(cached_error)
        else:
            config, config_at, error = {}, time.time(), ""
            try:
                output = observe(["scontrol", "show", "config"])
                config = parse_config(output)
                if not config:
                    raise ValueError("No telemetry configuration fields were exposed.")
            except Exception as exc:
                error = "Configuration unavailable: " + clean(exc)
                errors.append(error)
            self.cache[scope] = (self.clock(), config, config_at, error)
            self.cache.move_to_end(scope)
            while len(self.cache) > MAX_CONNECTIONS:
                self.cache.popitem(last=False)
        job = {}
        if job_id:
            try:
                output = observe(["scontrol", "show", "job", "-o", job_id])
                job = parse_job(output)
                if not matches_job(job, job_id, expected):
                    raise ValueError("The scheduler returned another job attempt; refresh the job list before inspecting again.")
                if (expected or {}).get("cluster") and config.get("ClusterName") not in (None, "unknown", expected["cluster"]):
                    raise ValueError("The selected job belongs to a different cluster.")
            except Exception as exc:
                job = {}
                errors.append("Job evidence unavailable: " + clean(exc))
        return build_report(config, job, job_id=job_id, expected=expected, intervals=intervals,
                            health=health, errors=errors, config_at=config_at, config_cached=use_cache,
                            **observations)


def report_lines(report):
    """Plain, readable evidence for both the modal and clipboard export."""
    from .metric_sampling import format_interval
    duration = lambda value: "unknown" if value is None else "disabled (0s)" if value == 0 else format_interval(value, ascii_=True)
    lines = [f"Job: {report.get('job_id') or 'none (cluster capabilities)'} | Cluster: {report.get('cluster', 'unknown')}"]
    attempt = report.get("attempt", {})
    if attempt.get("submit") or attempt.get("start"):
        lines.append(f"Attempt: submitted {attempt.get('submit') or '?'} | started {attempt.get('start') or '?'}")
    stamp = report.get("observed_at")
    lines.append("Snapshot: " + (time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(stamp)) if _number(stamp) else "unknown"))
    lines.append("Configuration: " + ("cached (up to 120s); Refresh bypasses cache" if report.get("config_cached") else "requested for this inspection"))
    lines.append("Configured Slurm task collection: " + duration(report.get("configured_task_seconds")))
    lines.append("Job-requested/inherited task collection: " + duration(report.get("job_task_seconds")))
    for metric in report.get("metrics", []):
        lines.append("")
        lines.append(metric["name"] + " | " + metric["capability"])
        label = "Tower baseline read: " if metric["source"] == "research" else "Tower read: "
        lines.append(label + duration(metric["tower_interval_seconds"]) +
                     " | Producer setting: " + duration(metric["producer_interval_seconds"]))
        last_ok = metric.get("last_success_at")
        age = max(0, report.get("observed_at", time.time()) - last_ok) if last_ok else None
        lines.append("Last successful Tower read: " + (format_interval(age, ascii_=True) + " before snapshot" if age is not None else "unavailable"))
        if metric.get("enabled") is False:
            lines.append("This Tower collector is disabled.")
        if metric.get("backoff_seconds"):
            lines.append("Collector backoff: " + duration(metric["backoff_seconds"]))
        if metric.get("error"):
            lines.append("Collector error: " + metric["error"])
        lines.append(metric["note"])
    lines += ["", "Configuration evidence"]
    lines += [key + " = " + value for key, value in sorted(report.get("config", {}).items())]
    if report.get("job"):
        lines += ["", "Job evidence"] + [key + " = " + value for key, value in sorted(report["job"].items())]
    lines += ["", "Interpretation"] + list(report.get("notes", []))
    if report.get("errors"):
        lines += ["", "Unavailable evidence"] + list(report["errors"])
    return [clean(line, 2048) for line in lines]
