"""Fail-open automatic Python capture scoped to an immutable launch attempt."""
from __future__ import annotations

import atexit
import fcntl
import os
import re
import stat
import struct
import sys
import time
from pathlib import Path

from . import reporting as R
from .launch_trace import _diagnostic, _stop_capture
from .research_reporting import _lock

_ACTIVE = None
_FORK_REGISTERED = False
_AUDIT_REGISTERED = False
_AUDIT_SEEN = set()
MAX_INDEX = 512


def _process(*, forked=False):
    original = getattr(sys, "orig_argv", sys.argv)
    role, launcher = "main", Path(sys.executable).name
    joined = " ".join(str(x) for x in original[:8])
    if any(name in joined for name in ("multiprocessing.spawn", "multiprocessing.forkserver", "multiprocessing.resource_tracker", "--multiprocessing-fork")) or forked:
        role = "helper"
    elif "torchrun" in joined or "torch.distributed.run" in joined:
        role, launcher = "launcher", "torchrun"
    elif "-m" in original:
        role = "module"
    elif "-c" in original:
        role = "command"
    elif not sys.argv or sys.argv[0] in ("", "-"):
        role = "interactive"
    value = {"pid": os.getpid(), "parent_pid": os.getppid(), "role": role,
             "launcher": launcher, "interpreter": ".".join(map(str, sys.version_info[:3]))}
    for rank, local, size, source in (("RANK", "LOCAL_RANK", "WORLD_SIZE", "torchrun"),
                                      ("OMPI_COMM_WORLD_RANK", "OMPI_COMM_WORLD_LOCAL_RANK", "OMPI_COMM_WORLD_SIZE", "mpirun"),
                                      ("PMI_RANK", "MPI_LOCALRANKID", "PMI_SIZE", "mpi"),
                                      ("SLURM_PROCID", "SLURM_LOCALID", "SLURM_NTASKS", "srun")):
        raw = os.environ.get(rank, "")
        if raw.isascii() and raw.isdecimal() and len(raw) <= 9:
            value["rank"] = int(raw)
            value["launcher"] = source
            if role != "helper":
                value["role"] = "rank"
            for key, variable in (("local_rank", local), ("world_size", size)):
                raw = os.environ.get(variable, "")
                if raw.isascii() and raw.isdecimal() and len(raw) <= 9 and (key != "world_size" or int(raw) > 0):
                    value[key] = int(raw)
            break
    return value


def _index(run):
    path = R._under(run, "trace/index.json", create_parent=True)
    value = R._read_json(path) if path.exists() else {"schema": "raven.trace-index/v1", "sources": []}
    if value.get("schema") != "raven.trace-index/v1" or not isinstance(value.get("sources"), list) or len(value["sources"]) > MAX_INDEX:
        raise ValueError("Invalid or oversized launch trace index")
    return path, value


def record_gap(run, reason, *, process=None, status="unavailable"):
    """Retain bounded observations; an unobserved child never gets a fake PID."""
    try:
        with _lock(run):
            path, value = _index(run)
            process = _process() if process is None else dict(process)
            # Distinct observed processes/ranks keep distinct receipts. Only
            # repeated observations of the same known identity (or the same
            # explicitly unknown child launch) are coalesced.
            identity_keys = ("node", "pid", "process_start", "rank", "local_rank", "parent_pid", "role", "launcher")
            signature = tuple(process.get(key) for key in identity_keys), reason[:2048]
            for entry in value["sources"]:
                prior = entry.get("process", {})
                if entry.get("schema") == "raven.trace-gap/v1" and (tuple(prior.get(key) for key in identity_keys), entry.get("reason")) == signature:
                    entry["count"] = min(1000000, entry.get("count", 1) + 1)
                    break
            else:
                if len(value["sources"]) < MAX_INDEX:
                    value["sources"].append({"schema": "raven.trace-gap/v1", "run_id": run.name,
                        "status": status, "reason": reason[:2048], "process": process, "count": 1})
                else:
                    value["omitted_gaps"] = min(1000000, value.get("omitted_gaps", 0) + 1)
            R._atomic_json(path, value, replace=True)
    except Exception:
        _diagnostic(run, reason[:2048])


class _RunBudget:
    """One fixed-size, locked counter shared by all descendants of this run.

    Reservations precede writes; a crash can conservatively consume capacity,
    but cannot make another process exceed the aggregate byte budget.
    """
    def __init__(self, run, settings):
        from .raven_limits import TRACE_PROCESSES, TRACE_RUN_BYTES
        self.limit = int(settings.get("trace_run_bytes", TRACE_RUN_BYTES))
        maximum = int(settings.get("trace_processes", TRACE_PROCESSES))
        path = R._under(run, ".raven-runtime/trace-budget")
        self.fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(self.fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size != 16:
            self.close()
            raise ValueError("Automatic trace budget is not a private fixed-size file")
        locked = False
        try:
            self._acquire()
            locked = True
            used, processes = struct.unpack("!QQ", os.pread(self.fd, 16, 0))
            if processes >= maximum:
                raise ValueError("Automatic trace process budget exhausted")
            if os.pwrite(self.fd, struct.pack("!QQ", used, processes + 1), 0) != 16:
                raise OSError("Incomplete trace budget reservation")
        except BaseException:
            self.close()
            raise
        finally:
            if locked and self.fd is not None:
                fcntl.flock(self.fd, fcntl.LOCK_UN)

    def _acquire(self):
        """A stopped peer cannot indefinitely stall the research program."""
        deadline = time.monotonic() + .2
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Shared trace budget is busy; recording stops and the application continues") from None
                time.sleep(min(.01, remaining))

    def reserve(self, size):
        self._acquire()
        try:
            used, processes = struct.unpack("!QQ", os.pread(self.fd, 16, 0))
            if used + size > self.limit:
                return False
            if os.pwrite(self.fd, struct.pack("!QQ", used + size, processes), 0) != 16:
                raise OSError("Incomplete trace byte reservation")
            return True
        finally:
            fcntl.flock(self.fd, fcntl.LOCK_UN)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


class _Attachment:
    def __init__(self, run, plan, *, chained, chain_error, forked=False, automatic=True):
        self.pid, self.run, self.plan = os.getpid(), run, plan
        self.capture = self.recorder = self.budget = None
        self.closed = False
        self.chained, self.chain_error = chained, chain_error
        self.automatic = automatic
        self.process = _process(forked=forked)

    def start(self):
        from .trace_recorder import PythonCapture, Recorder, register_trace
        self.run, manifest = verified_run(self.run, self.plan)
        settings = self.plan.get("settings", {})
        adapter = settings.get("trace_adapter") or "python-monitoring"
        if adapter not in {"python-monitoring", "python-legacy-profile"}:
            raise ValueError("Selected adapter requires its native/cooperative integration; automatic attachment covers Python only")
        if sys.version_info < (3, 12) and adapter != "python-legacy-profile":
            raise ValueError("Python 3.10/3.11 requires explicit --trace-adapter python-legacy-profile consent")
        self.budget = _RunBudget(self.run, settings)
        identity = {key: value for key, value in self.process.items() if key != "pid"}
        identity["run_id"] = self.run.name
        self.recorder = Recorder(self.run, job_id=manifest.get("job_id", "local"), attempt=self.run.name,
            cluster=os.environ.get("SLURM_CLUSTER_NAME", "local"), identity_extra=identity,
            max_bytes=int(settings.get("trace_process_bytes", 64 << 20)), journal_format="blocks",
            reserve_bytes=self.budget.reserve,
            coverage={"adapter": "python-profile" if adapter == "python-legacy-profile" else "python-sys-monitoring",
                "scope": "Observed Python frames in this process within " + settings.get("trace_scope", "project") + " scope; native execution, GPU kernels, and children bypassing startup hooks are unknown",
                "automatic": self.automatic, "chained": self.chained, "chain_error": self.chain_error[:2048],
                "observed_pythonpath": os.environ.get("PYTHONPATH", "")[:8192],
                "helpers_default": settings.get("trace_helpers", False),
                "limitations": "Legacy profile cannot distinguish yields/returns or exception unwind" if adapter == "python-legacy-profile" else "Frames already active before attachment are unknown"})
        self.capture = PythonCapture(self.recorder, legacy=adapter == "python-legacy-profile",
            scope=settings.get("trace_scope", "project"),
            roots=(self.plan["project_root"], str(Path(self.plan["script"]).parent)))
        self.capture.start()
        register_trace(self.plan["project_root"], self.recorder.path)
        with _lock(self.run):
            path, value = _index(self.run)
            if len(value["sources"]) + (1 if self.automatic else 2) > MAX_INDEX:
                raise ValueError("Automatic trace index is full")
            value["sources"].append({"path": str(self.recorder.path.relative_to(self.run)),
                "schema": "raven.trace/v1", "stream": self.recorder.stream, "run_id": self.run.name,
                "status": "recording", "reason": "", "process": self.process,
                "chained": self.chained, "chain_error": self.chain_error[:256]})
            if not self.automatic:
                # Preserve the explicit hook's existing journal attachment for
                # report/export consumers while sharing the recorder itself.
                value["sources"].append({"path": str((self.recorder.directory / "events.jsonl").relative_to(self.run)),
                    "schema": "raven.trace-event/v1", "stream": self.recorder.stream,
                    "run_id": self.run.name, "status": "recording", "reason": ""})
            R._atomic_json(path, value, replace=True)

    def close(self):
        if self.closed or self.pid != os.getpid():
            return
        self.closed = True
        try:
            if self.capture is not None:
                _stop_capture(self.capture, self.recorder, self.run)
            if self.recorder is not None:
                self.recorder.close()
                with _lock(self.run):
                    path, value = _index(self.run)
                    for source in value["sources"]:
                        if source.get("stream") == self.recorder.stream:
                            source.update(status="complete" if self.recorder.state == "CLOSED" else "partial",
                                reason="; ".join(self.recorder.warnings), process=self.process,
                                events=self.recorder.committed_seq)
                    R._atomic_json(path, value, replace=True)
        except Exception as exc:
            _diagnostic(self.run, "Optional trace finalization failed: " + str(exc))
        finally:
            if self.budget is not None:
                self.budget.close()


def _finish():
    if _ACTIVE is not None:
        _ACTIVE.close()


def _after_fork():
    global _ACTIVE, _AUDIT_SEEN
    previous = _ACTIVE
    _ACTIVE, _AUDIT_SEEN = None, set()
    if previous is None or previous.closed:
        return
    try:
        # Recorder's earlier registered child hook has closed inherited journal
        # descriptors. No parent lock or parent queue is touched here.
        if previous.capture is not None:
            previous.capture.stop()
        if previous.budget is not None:
            previous.budget.close()
        auto_attach(chained=previous.chained, chain_error=previous.chain_error, forked=True)
    except Exception as exc:
        _diagnostic(previous.run, "Forked Python attachment unavailable: " + str(exc))


def _multiprocessing_ready(_owner):
    # multiprocessing clears inherited finalizers before running this callback.
    # Its os._exit shutdown does not run normal atexit handlers.
    if _ACTIVE is not None and _ACTIVE.pid == os.getpid():
        from multiprocessing.util import Finalize
        _ACTIVE.process["role"] = "helper"
        Finalize(None, _finish, exitpriority=20)


def _audit(event, args):
    active = _ACTIVE
    if active is None or active.closed or active.pid != os.getpid() or event not in {"subprocess.Popen", "os.exec", "os.posix_spawn"}:
        return
    try:
        executable, argv = args[:2]
        if not isinstance(argv, (list, tuple)):
            return
        words = [os.fsdecode(x) for x in argv[:64] if isinstance(x, (str, bytes))]
        if os.environ.get("RAVEN_RUNTIME_BUNDLE") in words or os.environ.get("RAVEN_REPORT_HELPER") in words:
            return
        name = Path(os.fsdecode(executable)).name
        python = re.fullmatch(r"python(?:[0-9]+(?:\.[0-9]+)?)?", name) is not None
        options = []
        for word in words[1:]:
            if not word.startswith("-") or word in {"-c", "-m", "--"}:
                break
            options.append(word)
        reason = ""
        if python and any(any(flag in word[1:] for flag in ("I", "S", "E")) for word in options):
            reason = "Observed child launch disables environment/site startup (-I, -E, or -S); automatic tracing is unavailable"
        child_env = args[3] if event == "subprocess.Popen" else args[2]
        site = os.environ.get("RAVEN_TRACE_SITE", "")
        if python and isinstance(child_env, dict) and site and site not in child_env.get("PYTHONPATH", "").split(os.pathsep):
            reason = "Observed child launch replaces PYTHONPATH without the Raven startup directory; automatic tracing is unavailable"
        if not python and name not in {"torchrun", "srun", "mpirun", "mpiexec"}:
            reason = "Observed child executable is not identified as a Python interpreter; native frames are unavailable and startup-hook coverage is unverified"
        if not reason:
            return
        signature = name, reason
        if signature in _AUDIT_SEEN or len(_AUDIT_SEEN) >= 128:
            return
        _AUDIT_SEEN.add(signature)
        record_gap(active.run, reason, process={"parent_pid": os.getpid(), "role": "child", "launcher": name}, status="unverified")
    except Exception:
        pass  # An audit observer must never reject the application's operation.


def verified_run(run, plan):
    """Prove this immutable launch owns this run before any recording write."""
    from .launch_runtime import plan_inventory
    if not isinstance(plan, dict) or plan.get("schema") != "raven.launch-plan/v1":
        raise ValueError("Automatic trace requires a Raven launch plan")
    root = R._directory(plan["project_root"])
    requested = Path(run)
    if not requested.is_absolute():
        raise ValueError("Automatic trace run must be an absolute launch-owned path")
    directory = R._under(root, "runs/" + requested.name)
    if str(directory) != str(requested):
        raise ValueError("Automatic trace run is outside its launch project")
    R._directory(directory)
    manifest = R._read_json(R._under(directory, "run.json"))
    if not isinstance(manifest, dict) or manifest.get("schema") != "raven.run/v1" or manifest.get("run_id") != directory.name:
        raise ValueError("Run identity disagrees with the launch environment")
    automation = manifest.get("metadata", {}).get("automation", {})
    inventory = plan_inventory(plan)
    if (automation.get("project_root") != str(root) or automation.get("plan") != inventory
            or automation.get("filesystem_id") != plan.get("filesystem_id")):
        raise ValueError("Run does not belong to this frozen launch plan")
    return directory, manifest


def auto_attach(*, chained=False, chain_error="", forked=False):
    global _ACTIVE, _FORK_REGISTERED, _AUDIT_REGISTERED
    if _ACTIVE is not None and _ACTIVE.pid == os.getpid() and not _ACTIVE.closed:
        return _ACTIVE
    if not os.environ.get("RAVEN_RUN_DIR") or not os.environ.get("RAVEN_LAUNCH_PLAN") or os.environ.get("RAVEN_TRACE_SKIP_AUTO") == "1":
        return None
    run = Path(os.environ["RAVEN_RUN_DIR"])
    attachment = None
    diagnostic_run = None
    try:
        plan = R._read_json(os.environ["RAVEN_LAUNCH_PLAN"])
        if not isinstance(plan, dict) or plan.get("trace") is not True:
            return None
        from .launch_settings import _launch
        _launch(plan.get("settings", {}))
        run, _manifest = verified_run(run, plan)
        if run.name != os.environ.get("RAVEN_RUN_ID", run.name):
            raise ValueError("Automatic trace attempt identity changed")
        diagnostic_run = run
        attachment = _Attachment(run, plan, chained=chained, chain_error=chain_error, forked=forked)
        attachment.start()
        _ACTIVE = attachment
        os.environ["RAVEN_TRACE_ATTACHED"] = str(os.getpid())
        atexit.register(_finish)
        if not _FORK_REGISTERED and hasattr(os, "register_at_fork"):
            os.register_at_fork(after_in_child=_after_fork)
            _FORK_REGISTERED = True
        if not _AUDIT_REGISTERED:
            sys.addaudithook(_audit)
            _AUDIT_REGISTERED = True
        from multiprocessing.util import register_after_fork
        register_after_fork(attachment, _multiprocessing_ready)
        return attachment
    except Exception as exc:
        if attachment is not None:
            if attachment.recorder is not None:
                try:
                    attachment.recorder._fail("PARTIAL", "Optional Python attachment did not start completely: " + str(exc))
                except Exception:
                    pass
            attachment.close()
        reason = "Automatic Python attachment unavailable: " + str(exc)
        if diagnostic_run is None:
            _diagnostic(None, reason)
        else:
            record_gap(diagnostic_run, reason, process=_process(forked=forked))
        return None
