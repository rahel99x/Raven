"""Raven's user-private, host-local scheduler request ledger.

The existing acquisition cache owns read coalescing. This ledger is consulted
only by its producing loader, immediately before a subprocess is started. Its
rolling quotas and live reservations survive UI refresh and mode changes.
These are per-user/host/connection limits, not cluster-wide admission.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import shlex
import re
import time
import uuid
import math
import threading
import tempfile
import stat

from raven import evidence as E
from raven.shared_collection import _host, _process_start as _process_start
from raven.transport_errors import CommandError


DEFAULTS = {
    "queue_seconds": 60.0,
    "starts_seconds": 120.0,
    "history_seconds": 300.0,
    "inventory_seconds": 300.0,
    "details_seconds": 60.0,
    "live_seconds": 60.0,
    "account_seconds": 900.0,
    "log_follow_seconds": 5.0,
    "trace_seconds": 5.0,
    "idle_after_seconds": 300.0,
    "idle_interval_factor": 5.0,
    "jitter_fraction": 0.2,
    "max_inflight": 2,
    "controller_requests_per_minute": 12,
    "controller_burst": 2,
    "accounting_requests_per_minute": 2,
    "statistics_requests_per_minute": 4,
    "action_validation_requests_per_minute": 6,
    "step_launches_per_minute": 6,
    "step_launch_interval_seconds": 10.0,
    "filesystem_metadata_per_minute": 120,
    "file_bytes_per_minute": 16 << 20,
    "log_file_bytes_per_minute": 16 << 20,
    "local_processes_per_minute": 60,
    "max_response_bytes": 4 << 20,
    "max_response_records": 10000,
    "live_batch_jobs": 16,
    "circuit_failures": 3,
    "circuit_seconds": 120.0,
    "live_statistics": "selected-on-demand",
    "gpu": "existing-telemetry",
    "weather_probes": False,
    "slurm_enabled": True,
}
FLOORS = {
    "jobs": "queue_seconds",
    "starts": "starts_seconds",
    "finished": "history_seconds",
    "nodes": "inventory_seconds",
    "partitions": "inventory_seconds",
    "details": "details_seconds",
    "fin_details": "details_seconds",
    "live": "live_seconds",
    "gpu": "live_seconds",
    "trace": "trace_seconds",
    "share": "account_seconds",
    "account": "account_seconds",
    "budget": "account_seconds",
    "weather": "inventory_seconds",
}


def settings(value):
    result = dict(DEFAULTS)
    result.update(value or {})
    if not value or "trace_seconds" not in value:
        result["trace_seconds"] = result["log_follow_seconds"]
    for key, default in DEFAULTS.items():
        if isinstance(default, (int, float)) and not isinstance(default, bool):
            number = result[key]
            if (
                type(number) not in (int, float)
                or not math.isfinite(number)
                or number
                < (
                    0
                    if key == "jitter_fraction"
                    else 0.1
                    if key == "step_launch_interval_seconds"
                    else 0.001
                )
                or type(default) is int
                and (type(number) is not int or number > (1 << 30))
            ):
                raise ValueError("Invalid Raven collection setting: " + key)
    result["max_inflight"] = min(2, int(result["max_inflight"]))
    result["live_batch_jobs"] = min(64, int(result["live_batch_jobs"]))
    return result


def interval(config, source, requested):
    fallback = config.get("log_follow_seconds", 5.0) if source == "trace" else 60.0
    return max(
        float(requested),
        float(config.get(FLOORS.get(source, "details_seconds"), fallback)),
    )


@dataclass(frozen=True)
class Descriptor:
    family: str
    endpoint: str
    source: str
    read_only: bool = True
    cost: int = 1
    subprocess: bool = True


def classify(argv):
    """Classify structured commands; shell wrappers have one exact grammar."""
    if not isinstance(argv, (list, tuple)) or not argv or len(argv) > 512:
        return None
    if any(not isinstance(item, str) or not item or "\0" in item for item in argv):
        return None
    name = os.path.basename(argv[0])
    args = list(argv[1:])
    if name == "id" and args in (["-u"], ["-un"]):
        return Descriptor("configuration", "local", "hostnames")
    if name in (
        "squeue",
        "sacct",
        "sstat",
        "sinfo",
        "sshare",
        "sreport",
        "sacctmgr",
        "scontrol",
        "scancel",
    ) and args in (["--version"], ["--help"]):
        return Descriptor("configuration", "local", "hostnames")
    if name == "sh" and len(args) == 2 and args[0] == "-c":
        try:
            words = shlex.split(args[1])
        except ValueError:
            return None
        if (
            len(words) >= 4
            and words[:1] == ["cd"]
            and words[2] == "&&"
            and args[1]
            == "cd " + shlex.quote(words[1]) + " && " + shlex.join(words[3:])
        ):
            return classify(words[3:])
        return None
    if name in {
        "squeue",
        "sacct",
        "sstat",
        "sinfo",
        "scontrol",
        "sshare",
        "sacctmgr",
    } and args in (["--version"], ["--help"], ["-V"]):
        return Descriptor("cli-capability", "local", "capabilities")
    if name == "squeue":
        source = (
            "starts" if "--start" in args else "account" if "-A" in args else "jobs"
        )
        return Descriptor("queue", "controller", source)
    if name == "sacct":
        return Descriptor(
            "accounting",
            "accounting",
            "details"
            if "-j" in args or any(a.startswith("--jobs=") for a in args)
            else "finished",
        )
    if name == "sstat":
        return Descriptor("statistics", "statistics", "live")
    if name == "sinfo":
        return Descriptor("nodes", "controller", "nodes")
    if name == "sshare":
        return Descriptor("shares", "controller", "share")
    if name == "sreport":
        return Descriptor("budget-report", "accounting", "budget")
    if name == "sacctmgr":
        if "ping" in args and all(arg in ("-n", "-P", "ping") for arg in args):
            return Descriptor("associations", "accounting", "budget")
        if any(arg.lower() in ("show", "list") for arg in args) and not any(
            arg.lower() in ("add", "modify", "delete", "load") for arg in args
        ):
            return Descriptor("associations", "accounting", "budget")
        return None
    if name == "scontrol":
        lower = [arg.lower() for arg in args]
        if len(args) == 4 and lower[:2] == ["write", "batch_script"] and args[3] == "-":
            return Descriptor("job-detail", "controller", "details")
        if lower[:2] == ["show", "hostnames"]:
            return Descriptor("hostnames", "local", "hostnames")
        if lower[:1] in (
            ["show"],
            ["listpids"],
            ["ping"],
            ["version"],
            ["--version"],
            ["-v"],
        ):
            source = (
                "nodes"
                if len(lower) > 1 and lower[1].startswith(("node", "part"))
                else "details"
            )
            return Descriptor("job-detail", "controller", source)
        if lower and lower[0] in (
            "hold",
            "release",
            "requeue",
            "requeuehold",
            "update",
            "top",
            "suspend",
            "resume",
            "notify",
        ):
            return Descriptor("job-action", "controller", "actions", False)
    if name == "scancel":
        return Descriptor("cancel", "controller", "actions", False)
    if name == "sbatch":
        return (
            Descriptor("test-submit", "probes", "actions", False)
            if "--test-only" in args
            else Descriptor("submit", "controller", "actions", False)
        )
    if name in ("salloc", "srun"):
        return (
            Descriptor("allocation", "controller", "actions", False)
            if name == "salloc"
            else Descriptor("step", "steps", "actions", False)
        )
    if name == "sattach":
        return Descriptor("step-attach", "steps", "actions", False)
    return None


_AUTO_CACHE_ROOTS = {}


def cache_directory(configured=""):
    if configured:
        return str(Path(configured).expanduser().absolute())
    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    base = Path(runtime) if runtime and Path(runtime).is_absolute() else Path("/tmp")
    root = base / ("raven-collection-" + str(os.getuid()) + "-" + _host()[:16])
    try:
        root.mkdir(mode=0o700, parents=False, exist_ok=True)
        info = root.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or root.is_symlink()
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise ValueError("Automatic cache root is unavailable")
        return str(root)
    except (OSError, ValueError):
        # Never repair, enter or delete a squatter's path. All instances in
        # this process reuse the same private unpredictable replacement.
        key = os.getpid()
        if key not in _AUTO_CACHE_ROOTS:
            _AUTO_CACHE_ROOTS[key] = tempfile.mkdtemp(
                prefix="raven-collection-", dir="/tmp"
            )
        return _AUTO_CACHE_ROOTS[key]


from raven.hostlist import expand_hostlist


def make_placement_verifier(backend, *, purpose="worker"):
    """A finite worker callback using the parent's exact collection owner.

    Call on a bounded background worker, never the UI thread. Job and step
    reads are admitted; absent cgroup proof adds one exact local listpids read.
    Host-list expansion is local Python.
    The worker remains responsible for checking its kernel placement proof.
    """
    if getattr(backend, "ledger", None) is None:
        raise ValueError("Placement verification requires the managed collection owner")
    if purpose not in ("worker", "instrumentation"):
        raise ValueError("Unknown placement verification purpose")

    def verify(candidate, cancel=None):
        from raven.slurm import parse_kv

        job = str(candidate.get("jobid", candidate.get("job_id", "")))
        step = str(candidate.get("stepid", candidate.get("step_id", "")))
        step_pattern = (
            r"(?:[0-9]+|batch|extern)" if purpose == "instrumentation" else r"[0-9]+"
        )
        if not re.fullmatch(r"[0-9]+", job) or not re.fullmatch(step_pattern, step):
            raise ValueError(
                "Placement verification requires unambiguous allocation and step IDs"
            )
        job_record = backend.command(
            ["scontrol", "show", "job", job, "-o"], 8.0, 65536, cancel
        )
        step_record = backend.command(
            ["scontrol", "show", "step", job + "." + step, "-o"], 8.0, 65536, cancel
        )
        job_fields, step_fields = parse_kv(job_record), parse_kv(step_record)
        records = {
            "job_record": job_record,
            "step_record": step_record,
            "job_nodes": expand_hostlist(job_fields.get("NodeList", "")),
            # NodeList is the host list; Nodes is a count on modern Slurm.
            "step_nodes": expand_hostlist(
                step_fields.get("NodeList", step_fields.get("Nodes", ""))
            ),
            "probe_id": candidate.get("probe_id", ""),
        }
        if candidate.get("cgroup_proof") is not True:
            from raven.placement_pids import parse as parse_pids

            # linuxproc sites expose membership through their local slurmd.
            # This fresh exact read never starts a step or guesses a remote PID.
            try:
                text = backend.command(
                    ["scontrol", "listpids", job + "." + step], 8.0, 65536, cancel
                )
            except (CommandError, OSError) as exc:
                raise ValueError(
                    "Worker process placement is unverified: local Slurm PID membership unavailable: "
                    + str(exc)
                ) from exc
            records["pids"] = parse_pids(text, job, step)
        return records

    return verify


def admit_allocation_launch(backend, plan):
    """Debit a reviewed launch once, without leasing a finite collection slot.

    The allocation helper owns its long-lived processes. Admission occurs just
    before that helper starts, and every scheduler subprocess is charged: a
    dedicated allocation costs one controller request and one step launch.
    Denial never waits. A failed or partly admitted launch retains its debits,
    so retrying or changing modes cannot reset the rolling allowance.
    """
    ledger = getattr(backend, "ledger", None)
    if ledger is None:
        raise ValueError("Allocation launch requires the managed collection owner")
    if not isinstance(plan, dict):
        raise ValueError("Allocation launch requires a structured reviewed plan")
    prefix = plan.get("prefix")
    if (
        not isinstance(prefix, list)
        or len(prefix) > 128
        or any(not isinstance(arg, str) or not arg or "\0" in arg for arg in prefix)
    ):
        raise ValueError("Invalid allocation launch prefix")
    if plan.get("site_kind") == "desktop":
        if prefix or plan.get("ownership") != "local-worker":
            raise ValueError("Desktop launch cannot contain a scheduler prefix")
        requests = [
            (Descriptor("worker-launch", "local", "actions", False), ["worker-launch"])
        ]
    elif plan.get("site_kind") == "slurm":
        if not ledger.config.get("slurm_enabled", True):
            raise Deferred(
                "Scheduler collection and launch are disabled by this profile"
            )
        commands = [
            index for index, arg in enumerate(prefix) if arg in ("salloc", "srun")
        ]
        expected = (
            ["salloc", "srun"]
            if plan.get("ownership") == "dedicated-allocation"
            else ["srun"]
            if plan.get("ownership") == "owned-step-only"
            else []
        )
        if (
            not expected
            or not commands
            or commands[0] != 0
            or [prefix[index] for index in commands] != expected
        ):
            raise ValueError(
                "Allocation launch must use the reviewed scheduler command sequence"
            )
        requests = []
        for start, end in zip(commands, [*commands[1:], len(prefix)]):
            argv = prefix[start:end]
            requests.append((classify(argv), argv))
    else:
        raise ValueError("Allocation launch has an unknown site kind")
    receipts = []
    for descriptor, argv in requests:
        # Explicit launches use the bounded action lane and their own endpoint
        # quota, not the separate allowance reserved for fresh read validation.
        with ledger.admit(descriptor, argv, validation=True):
            receipts.append(
                {"family": descriptor.family, "endpoint": descriptor.endpoint}
            )
    return {"scope": "user-host-connection", "requests": receipts}


def admit_terminal_launch(backend, argv):
    """Charge an exact, freshly reviewed scheduler terminal handoff once.

    Call from the operation worker after attempt validation. The process may
    remain interactive for hours, but its admission reservation is finite.
    Ordinary user-entered shell commands are outside this generated workflow.
    """
    ledger = getattr(backend, "ledger", None)
    if ledger is None:
        raise ValueError(
            "Generated terminal launch requires a managed collection owner"
        )
    descriptor = classify(argv)
    if descriptor is None or descriptor.family not in ("step", "step-attach"):
        raise ValueError(
            "Generated terminal launch must be an exact reviewed step or attach"
        )
    if not ledger.config.get("slurm_enabled", True):
        raise Deferred("Scheduler collection and launch are disabled by this profile")
    with ledger.admit(descriptor, argv, validation=True):
        return {
            "scope": "user-host-connection",
            "family": descriptor.family,
            "endpoint": descriptor.endpoint,
        }


class Deferred(CommandError):
    def __init__(self, message, *, retry_after=5.0):
        super().__init__(message)
        self.retry_after = max(0.1, min(300.0, float(retry_after)))


class Ledger:
    """Admission uses short SQLite transactions, never sleeps with a permit."""

    def __init__(self, cache, scope, config, *, now=time.monotonic):
        # Profile/mode names cannot mint a new allowance for the same endpoint
        # and authority. Read-cache identities still include the complete scope.
        quota_scope = {
            key: value
            for key, value in scope.items()
            if key not in ("profile", "mode", "generation", "policy_digest")
        }
        self.cache, self.scope, self.config, self.now = (
            cache,
            E.digest(quota_scope),
            settings(config),
            now,
        )
        self._config_lock = threading.RLock()
        self.counts = {"admitted": 0, "deferred": 0, "failed": 0}
        with cache._connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS raven_requests (scope TEXT, endpoint TEXT, started REAL, cost INTEGER NOT NULL DEFAULT 1);
                CREATE INDEX IF NOT EXISTS raven_requests_scope ON raven_requests(scope, started);
                CREATE TABLE IF NOT EXISTS raven_active (token TEXT PRIMARY KEY, scope TEXT, lane TEXT, pid INTEGER, process_start TEXT);
                CREATE TABLE IF NOT EXISTS raven_sources (scope TEXT, source TEXT, started REAL, failures INTEGER, until REAL, PRIMARY KEY(scope,source));
                CREATE TABLE IF NOT EXISTS raven_endpoints (scope TEXT, endpoint TEXT, failures INTEGER, until REAL, last_launch REAL, PRIMARY KEY(scope,endpoint));
            """)
            db.execute("BEGIN IMMEDIATE")
            try:
                if "cost" not in {
                    row[1] for row in db.execute("PRAGMA table_info(raven_requests)")
                }:
                    db.execute(
                        "ALTER TABLE raven_requests ADD COLUMN cost INTEGER NOT NULL DEFAULT 1"
                    )
                if "token" not in {
                    row[1] for row in db.execute("PRAGMA table_info(raven_requests)")
                }:
                    db.execute("ALTER TABLE raven_requests ADD COLUMN token TEXT")
                if "last_launch" not in {
                    row[1] for row in db.execute("PRAGMA table_info(raven_endpoints)")
                }:
                    db.execute(
                        "ALTER TABLE raven_endpoints ADD COLUMN last_launch REAL"
                    )
                # Preserve legacy launches before the rolling request cleanup,
                # including site cooldowns longer than the minute quota window.
                db.execute("""
                    INSERT INTO raven_endpoints(scope,endpoint,failures,until,last_launch)
                    SELECT scope,endpoint,0,0,max(started) FROM raven_requests
                    WHERE endpoint='steps' GROUP BY scope,endpoint
                    ON CONFLICT(scope,endpoint) DO UPDATE SET
                    last_launch=max(coalesce(raven_endpoints.last_launch,excluded.last_launch),excluded.last_launch)
                """)
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise

    def reconfigure(self, config):
        """Replace future admission limits without resetting spent or live work.

        Mode changes share the same database, identity, source clocks, failure
        circuits and reservations. An already admitted command keeps its slot
        until it returns, even when the new policy is more restrictive.
        """
        updated = settings(config)
        with self._config_lock:
            self.config = updated

    def _admit(self, descriptor, argv, validation):
        with self._config_lock:
            return self._admit_locked(descriptor, argv, validation)

    def _admit_locked(self, descriptor, argv, validation):
        now, cfg = self.now(), self.config
        source = E.digest({"family": descriptor.family, "argv": list(argv)})
        endpoint = descriptor.endpoint
        lane = "validation" if validation else "source"
        quota = {
            "controller": "controller_requests_per_minute",
            "accounting": "accounting_requests_per_minute",
            "statistics": "statistics_requests_per_minute",
            "steps": "step_launches_per_minute",
            "probes": "step_launches_per_minute",
            "filesystem": "filesystem_metadata_per_minute",
            "file-bytes": "file_bytes_per_minute",
            "log-bytes": "log_file_bytes_per_minute",
            "local": "local_processes_per_minute",
        }[endpoint]
        # Action validation is bounded independently from observational traffic;
        # it still consumes the common process quota and total concurrency.
        bucket = (
            "validation"
            if validation and descriptor.read_only and endpoint != "local"
            else endpoint
        )
        limit = (
            cfg["action_validation_requests_per_minute"]
            if bucket == "validation"
            else cfg[quota]
        )
        token = uuid.uuid4().hex
        with self.cache._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute("DELETE FROM raven_requests WHERE started<=?", (now - 60.0,))
                if (
                    db.execute("SELECT count(*) FROM raven_requests").fetchone()[0]
                    >= 4096
                ):
                    raise Deferred(
                        "Raven collection rolling ledger entry budget exhausted"
                    )
                for old in db.execute(
                    "SELECT token,pid,process_start FROM raven_active"
                ).fetchall():
                    if self.cache._dead(old):
                        db.execute("DELETE FROM raven_active WHERE token=?", (old[0],))
                recovery = db.execute(
                    "SELECT until,last_launch FROM raven_endpoints WHERE scope=? AND endpoint=?",
                    (self.scope, endpoint),
                ).fetchone()
                if endpoint == "steps":
                    # The endpoint timestamp survives minute-window pruning:
                    # mandatory site floors may require a longer cooldown.
                    last_launch = recovery[1] if recovery else None
                    if last_launch is None:
                        # Honor recent debits written before this timestamp
                        # was introduced, including failed launch attempts.
                        last_launch = db.execute(
                            "SELECT max(started) FROM raven_requests WHERE scope=? AND endpoint=?",
                            (self.scope, endpoint),
                        ).fetchone()[0]
                    remaining = (
                        0.0
                        if last_launch is None
                        else last_launch + cfg["step_launch_interval_seconds"] - now
                    )
                    if remaining > 0:
                        retry_after = max(0.1, remaining)
                        raise Deferred(
                            "Raven collection deferred: step launch cooldown active; retry in "
                            + format(retry_after, ".2f")
                            + " seconds",
                            retry_after=retry_after,
                        )
                active = db.execute(
                    "SELECT lane,count(*) FROM raven_active WHERE scope=? GROUP BY lane",
                    (self.scope,),
                ).fetchall()
                counts = dict(active)
                # Preserve one of the starter profile's two command slots for
                # review validation. A cancelled producer keeps its slot until
                # its synchronous backend command actually returns.
                maximum = cfg["max_inflight"]
                if sum(counts.values()) >= maximum or counts.get(lane, 0) >= (
                    1 if validation else max(1, maximum - 1)
                ):
                    raise Deferred(
                        "Raven collection deferred: command lane is occupied"
                    )
                budgets = [(bucket, limit)]
                if descriptor.subprocess:
                    budgets.append(("process", cfg["local_processes_per_minute"]))
                for key, ceiling in budgets:
                    count = db.execute(
                        "SELECT coalesce(sum(cost),0) FROM raven_requests WHERE scope=? AND endpoint=?",
                        (self.scope, key),
                    ).fetchone()[0]
                    cost = descriptor.cost if key == bucket else 1
                    if count + cost > ceiling:
                        raise Deferred(
                            "Raven collection deferred: rolling "
                            + key
                            + " budget exhausted"
                        )
                previous = db.execute(
                    "SELECT started,failures,until FROM raven_sources WHERE scope=? AND source=?",
                    (self.scope, source),
                ).fetchone()
                if not validation and recovery and now < recovery[0]:
                    raise Deferred(
                        "Raven collection deferred: endpoint circuit is recovering"
                    )
                floor = (
                    0.0
                    if validation
                    or endpoint in ("local", "filesystem", "file-bytes", "log-bytes")
                    else cfg["step_launch_interval_seconds"]
                    if endpoint == "steps"
                    else interval(cfg, descriptor.source, 0)
                )
                if previous and now < max(previous[0] + floor, previous[2]):
                    raise Deferred(
                        "Raven collection deferred: source floor or circuit recovery is active"
                    )
                # An endpoint burst limit smooths synchronized different queue
                # and detail requests without counting cached subscribers.
                if not validation and endpoint == "controller":
                    burst = db.execute(
                        "SELECT count(*) FROM raven_requests WHERE scope=? AND endpoint=? AND started>?",
                        (self.scope, bucket, now - 1.0),
                    ).fetchone()[0]
                    if burst >= cfg["controller_burst"]:
                        raise Deferred(
                            "Raven collection deferred: controller burst budget exhausted",
                            retry_after=1.01,
                        )
                if (
                    db.execute("SELECT count(*) FROM raven_sources").fetchone()[0]
                    >= 4096
                    and previous is None
                ):
                    db.execute(
                        "DELETE FROM raven_sources WHERE started<? AND until<?",
                        (now - 86400, now),
                    )
                    if (
                        db.execute("SELECT count(*) FROM raven_sources").fetchone()[0]
                        >= 4096
                    ):
                        raise Deferred(
                            "Raven collection source identity capacity reached"
                        )
                debits = [(self.scope, bucket, now, descriptor.cost)]
                if descriptor.subprocess:
                    debits.append((self.scope, "process", now, 1))
                db.executemany(
                    "INSERT INTO raven_requests(scope,endpoint,started,cost,token) VALUES (?,?,?,?,?)",
                    [(*debit, token) for debit in debits],
                )
                if endpoint == "steps":
                    db.execute(
                        "INSERT INTO raven_endpoints(scope,endpoint,failures,until,last_launch) VALUES (?,?,0,0,?) ON CONFLICT(scope,endpoint) DO UPDATE SET last_launch=excluded.last_launch",
                        (self.scope, endpoint, now),
                    )
                db.execute(
                    "INSERT INTO raven_active VALUES (?,?,?,?,?)",
                    (token, self.scope, lane, *self.cache._owner),
                )
                db.execute(
                    "INSERT OR REPLACE INTO raven_sources VALUES (?,?,?,?,?)",
                    (self.scope, source, now, previous[1] if previous else 0, 0.0),
                )
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                self.counts["deferred"] += 1
                raise
        self.counts["admitted"] += 1
        return token, source, now

    @contextmanager
    def admit(self, descriptor, argv, *, validation=False):
        token, source, begin = self._admit(descriptor, argv, validation)
        failed = False
        reservation = ByteReservation(descriptor.cost)
        try:
            yield reservation
        except BaseException:
            failed = True
            self.counts["failed"] += 1
            raise
        finally:
            with self.cache._connection() as db:
                db.execute("BEGIN IMMEDIATE")
                try:
                    if reservation.used is not None and descriptor.endpoint in {
                        "file-bytes",
                        "log-bytes",
                    }:
                        db.execute(
                            "UPDATE raven_requests SET cost=? WHERE scope=? AND endpoint=? AND token=?",
                            (reservation.used, self.scope, descriptor.endpoint, token),
                        )
                    row = db.execute(
                        "SELECT failures FROM raven_sources WHERE scope=? AND source=?",
                        (self.scope, source),
                    ).fetchone()
                    failures = (row[0] if row else 0) + 1 if failed else 0
                    # Sustained latency also extends the observational floor.
                    duration = max(0.0, self.now() - begin)
                    delay = (
                        min(900.0, 2.0 ** min(9, failures))
                        if failed
                        else duration
                        if duration >= 5
                        else 0.0
                    )
                    if failures >= self.config["circuit_failures"]:
                        delay = max(delay, self.config["circuit_seconds"])
                    endpoint = db.execute(
                        "SELECT failures,last_launch FROM raven_endpoints WHERE scope=? AND endpoint=?",
                        (self.scope, descriptor.endpoint),
                    ).fetchone()
                    consecutive = (endpoint[0] if endpoint else 0) + 1 if failed else 0
                    recovery = (
                        self.now() + self.config["circuit_seconds"]
                        if consecutive >= self.config["circuit_failures"]
                        else 0.0
                    )
                    db.execute(
                        "INSERT OR REPLACE INTO raven_endpoints VALUES (?,?,?,?,?)",
                        (
                            self.scope,
                            descriptor.endpoint,
                            consecutive,
                            recovery,
                            endpoint[1] if endpoint else None,
                        ),
                    )
                    db.execute(
                        "UPDATE raven_sources SET failures=?,until=? WHERE scope=? AND source=?",
                        (failures, self.now() + delay, self.scope, source),
                    )
                    db.execute("DELETE FROM raven_active WHERE token=?", (token,))
                    db.execute("COMMIT")
                except BaseException:
                    db.execute("ROLLBACK")
                    raise

    def report(self):
        with self.cache._connection() as db:
            rates = dict(
                db.execute(
                    "SELECT endpoint,coalesce(sum(cost),0) FROM raven_requests WHERE scope=? AND started>? GROUP BY endpoint",
                    (self.scope, self.now() - 60.0),
                ).fetchall()
            )
            inflight = db.execute(
                "SELECT count(*) FROM raven_active WHERE scope=?", (self.scope,)
            ).fetchone()[0]
        return {
            "scope": "user-host-connection",
            "rates_last_minute": rates,
            "inflight": inflight,
            **self.counts,
        }


class ByteReservation:
    def __init__(self, reserved):
        self.reserved, self.used = reserved, None

    def consumed(self, amount):
        if type(amount) is not int or not 0 <= amount <= self.reserved:
            raise ValueError("File adapter exceeded its reserved byte budget")
        self.used = amount


class GovernedFiles:
    """Typed file reads share metadata/byte admission with the collection owner.

    This is a managed-reader budget, not a restriction on user shell commands.
    Missing-file checks never hide quota errors as an absent artifact.
    """

    def __init__(self, files, backend):
        self.inner, self.ledger = files, backend.ledger
        self.remote = bool(getattr(files, "remote", False))
        self.project_delegate = files if self.remote else None

    @property
    def min_refresh(self):
        """Live file cadence follows the current policy without rebinding readers."""
        return max(
            float(getattr(self.inner, "min_refresh", 0)),
            self.ledger.config["log_follow_seconds"],
        )

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def _metadata(self, method, path):
        try:
            with self.ledger.admit(
                Descriptor(
                    "file-" + method, "filesystem", "trace", subprocess=self.remote
                ),
                [method, str(path)],
            ):
                return getattr(self.inner, method)(path)
        except Deferred as exc:
            raise OSError(str(exc)) from exc

    def stat(self, path):
        return self._metadata("stat", path)

    def snapshot_stat(self, path):
        return self._metadata("snapshot_stat", path)

    def exists(self, path):
        if not path:
            return False
        try:
            self.stat(path)
            return True
        except FileNotFoundError:
            return False

    def read(self, path, offset, length):
        return self._read(path, offset, length, bucket="log-bytes")

    def _read(self, path, offset, length, *, bucket):
        if (
            type(length) is not int
            or type(offset) is not int
            or not 0 <= length <= 2 << 20
            or offset < 0
        ):
            raise OSError(
                "Raven Light file read requires a nonnegative offset and at most 2 MiB"
            )
        if not length:
            return b""
        try:
            with self.ledger.admit(
                Descriptor(
                    "file-read", bucket, "trace", cost=length, subprocess=self.remote
                ),
                ["read", str(path)],
            ) as debit:
                result = self.inner.read(path, offset, length)
                if not isinstance(result, bytes) or len(result) > length:
                    raise OSError("File adapter exceeded the admitted read length")
                debit.consumed(len(result))
                return result
        except Deferred as exc:
            raise OSError(str(exc)) from exc

    def tail(self, path, max_bytes):
        size, _ = self.stat(path)
        length = min(max_bytes, size)
        return self._read(
            path, max(0, size - length), length, bucket="file-bytes"
        ), size

    def project_command(self, argv, timeout):
        """One declared confined project-reader request; cap wire output first."""
        from raven.bounded_command import execute as _backend_command
        from raven.project_wire import classify as classify_project

        descriptor = classify_project(argv) if self.remote else None
        if descriptor is None:
            raise OSError("Raven project adapter received an unclassified helper")
        limit = min(
            4096 if descriptor["action"] != "read" else 2 << 20,
            int(self.ledger.config["max_response_bytes"]),
        )
        upload = descriptor["upload_bytes"]
        with self.ledger.admit(
            Descriptor("project-reader", "file-bytes", "trace", cost=limit + upload),
            ["project-reader", descriptor["identity"]],
        ) as debit:
            start = time.monotonic()
            raw = _backend_command(self.inner.ssh, argv, timeout, limit, None)
            used = len(raw.encode("utf-8"))
            if used > limit:
                raise OSError("Remote project reply exceeds the Light byte budget")
            debit.consumed(used + upload)
            return raw, time.monotonic() - start

    def manifest_signature(self, path):
        """Retain the catalog's no-symlink rule behind typed admission."""
        import stat
        from raven.bounded_command import execute as _backend_command

        try:
            with self.ledger.admit(
                Descriptor(
                    "manifest-stat", "filesystem", "trace", subprocess=self.remote
                ),
                ["manifest-stat", str(path)],
            ):
                if not self.remote:
                    value = os.lstat(path)
                    mode = value.st_mode
                    signature = (
                        value.st_dev,
                        value.st_ino,
                        value.st_size,
                        value.st_mtime_ns,
                        value.st_ctime_ns,
                    )
                else:
                    raw = _backend_command(
                        self.inner.ssh,
                        ["stat", "-c", "%f %d %i %s %Y %Z", "--", path],
                        self.inner.timeout,
                        512,
                        None,
                    )
                    fields = raw.split()
                    if len(fields) != 6:
                        raise OSError("Invalid remote manifest metadata")
                    mode, signature = (
                        int(fields[0], 16),
                        tuple(int(item) for item in fields[1:]),
                    )
                if not stat.S_ISREG(mode):
                    raise OSError(
                        "Log manifest must be a regular file, not a symlink or device"
                    )
                return signature
        except (Deferred, ValueError) as exc:
            raise OSError(str(exc)) from exc

    def listdir(self, path):
        maximum = min(10000, int(self.ledger.config["max_response_records"]))
        try:
            with self.ledger.admit(
                Descriptor(
                    "file-listdir", "filesystem", "trace", subprocess=self.remote
                ),
                ["listdir", str(path)],
            ):
                if not self.remote:
                    with os.scandir(path) as entries:
                        result = []
                        for item in entries:
                            if len(result) >= maximum:
                                raise OSError(
                                    "Directory exceeds the Light entry budget; use a bounded project index"
                                )
                            result.append(item.name)
                else:
                    import base64
                    from raven.bounded_command import execute as _backend_command

                    quoted = shlex.quote(path)
                    script = (
                        f"test -d {quoted} && test -r {quoted} && test -x {quoted} || exit 2; "
                        f"find -- {quoted} -mindepth 1 -maxdepth 1 -printf '%f\\0' "
                        f"| head -z -n {maximum + 1} | base64"
                    )
                    encoded = _backend_command(
                        self.inner.ssh,
                        ["sh", "-c", script],
                        self.inner.timeout,
                        2 << 20,
                        None,
                    )
                    result = (
                        base64.b64decode("".join(encoded.split()), validate=True)
                        .decode("utf-8")
                        .split("\0")
                    )
                    if result and not result[-1]:
                        result.pop()
                if len(result) > maximum:
                    raise OSError(
                        "Directory exceeds the Light entry budget; use a bounded project index"
                    )
                return result
        except Deferred as exc:
            raise OSError(str(exc)) from exc


from raven.local_files import register_confined_wrapper

register_confined_wrapper(GovernedFiles, lambda files: files.inner)


def wrap_files(files, backend):
    return (
        GovernedFiles(files, backend)
        if getattr(backend, "ledger", None) is not None
        else files
    )
