"""Fresh Heavy execution owner. No dashboard, sampler, terminal, or UI imports.

The worker accepts only fixed Raven analysis operations. Its stdio pipe belongs
exclusively to the versioned protocol. Placement failure never falls back local.
"""

from __future__ import annotations

from collections import deque, OrderedDict
import argparse
import hashlib
import os
import resource
import selectors
import signal
import socket
import subprocess
import sys
import time
import uuid

from ..protocol_contracts import worker_control
from .protocol import (
    MAJOR,
    MINOR,
    MAX_QUEUE,
    Decoder,
    ProtocolError,
    build_digest,
    canonical,
    digest,
    encode,
    worker_argv,
)
from .resources import (
    apply_limits,
    child_environment,
    limits,
    placement_probe,
    task_limits,
    verify_placement,
)


def _stop(process):
    if process is not None and process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _policy_check(config):
    from ..raven_config import worker_policy_check

    checked = worker_policy_check(config)
    return config if checked is None else checked


def _manifest():
    from ..raven_execution import OPERATIONS

    return {
        name: {
            "version": value.get("version", 1),
            "budget": value["budget"],
            "targets": value.get("targets", []),
            "side_effects": value.get("side_effects", "none"),
        }
        for name, value in OPERATIONS.items()
    }


def _parent_death(expected_parent):
    if (
        not isinstance(expected_parent, int)
        or expected_parent <= 1
        or os.getppid() != expected_parent
    ):
        raise ProtocolError("Analysis supervisor is no longer its launch parent")
    if sys.platform.startswith("linux"):
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
            raise OSError(
                ctypes.get_errno(), "Could not bind analysis to its supervisor lifetime"
            )
        if os.getppid() != expected_parent:
            raise ProtocolError("Analysis supervisor exited during startup")


def task_main(expected_parent):
    """One admitted task in a fresh, bounded process; all dispatch is named."""
    _parent_death(expected_parent)
    decoder = Decoder()
    while True:
        data = os.read(0, 65536)
        if not data:
            raise ProtocolError("Task request ended before its complete frame")
        decoder.feed(data)
        values = decoder.take(1)
        if values:
            request = values[0]
            break
    budget = request["budget"]
    try:
        enforcement = apply_limits(budget)
        from ..raven_execution import DEFAULT_BUDGET, OPERATIONS, execute_operation

        operation = request["operation"]
        if operation not in OPERATIONS or request.get("version") != OPERATIONS[
            operation
        ].get("version", 1):
            raise ValueError("Unsupported Heavy operation or version")
        result = execute_operation(
            operation,
            request["inputs"],
            context=request["context"],
            budget={
                key: value for key, value in budget.items() if key in DEFAULT_BUDGET
            },
            cancelled=lambda: False,
            verified_execution=request.get("verified_execution"),
        )
        if len(canonical(result)) > budget["output_bytes"]:
            raise ValueError("Analysis result exceeds its admitted output budget")
        reply = {
            "op": "task_result",
            "id": request["id"],
            "result": result,
            "enforcement": enforcement,
        }
    except BaseException as exc:
        reply = {
            "op": "task_failed",
            "id": request.get("id", ""),
            "error": (type(exc).__name__ + ": " + str(exc))[:2000],
        }
    raw = memoryview(encode(reply))
    while raw:
        wrote = os.write(1, raw)
        raw = raw[wrote:]
    return 0


class Worker:
    def __init__(self, input_fd=0, output_fd=1):
        self.input_fd, self.output_fd = input_fd, output_fd
        os.set_blocking(input_fd, False)
        os.set_blocking(output_fd, False)
        self.selector = selectors.DefaultSelector()
        self.selector.register(input_fd, selectors.EVENT_READ, ("owner", None))
        self.decoder = Decoder()
        self.output = deque()
        self.output_bytes = 0
        self.config = None
        self.session = ""
        self.generation = 0
        self.worker_id = uuid.uuid4().hex
        self.seq = 0
        self.ready = False
        self.shutting_down = False
        self.started = time.monotonic()
        self.tasks = {}
        self.retired = OrderedDict()
        self.receipt_bytes = 0
        self.probe = None
        self.identity = {}
        self.output_registered = False
        for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            signal.signal(
                signum, lambda _signum, _frame: setattr(self, "shutting_down", True)
            )

    def emit(self, op, **payload):
        self.seq += 1
        value = {
            "op": op,
            "session_id": self.session,
            "generation": self.generation,
            "worker_id": self.worker_id,
            "seq": self.seq,
            **payload,
        }
        raw = encode(value)
        if self.output_bytes + len(raw) > MAX_QUEUE:
            self.shutting_down = True
            raise ProtocolError(
                "Worker owner channel is backpressured beyond its budget"
            )
        self.output.append(memoryview(raw))
        self.output_bytes += len(raw)
        if not self.output_registered:
            self.selector.register(
                self.output_fd, selectors.EVENT_WRITE, ("output", None)
            )
            self.output_registered = True

    def flush(self):
        budget = 65536
        while self.output and budget > 0:
            try:
                count = os.write(self.output_fd, self.output[0][:budget])
            except BlockingIOError:
                return
            if not count:
                raise BrokenPipeError("Worker owner stopped reading")
            self.output_bytes -= count
            budget -= count
            self.output[0] = self.output[0][count:]
            if not self.output[0]:
                self.output.popleft()
        if not self.output and self.output_registered:
            self.selector.unregister(self.output_fd)
            self.output_registered = False

    def hello(self, message):
        if self.config is not None:
            raise ProtocolError("Worker handshake cannot be replaced")
        # Correlate a rejected startup with its waiting owner without admitting
        # any work. Build/protocol mismatches must remain visible diagnostics,
        # rather than appearing to be an unrelated generation's publication.
        config = message.get("config")
        if not isinstance(config, dict):
            raise ProtocolError("Missing Heavy policy configuration")
        session, generation = config.get("session_id"), config.get("generation")
        if (
            not isinstance(session, str)
            or not 1 <= len(session) <= 128
            or type(generation) is not int
            or generation < 1
        ):
            raise ProtocolError("Invalid worker session generation")
        self.session, self.generation = session, generation
        worker_control(message)
        if (
            type(message.get("major")) is not int
            or message.get("major") != MAJOR
            or type(message.get("minor")) is not int
            or message["minor"] < 0
        ):
            raise ProtocolError("Worker protocol major version mismatch")
        if message.get("build_digest") != build_digest():
            raise ProtocolError(
                "Worker executable/build digest differs from the requester"
            )
        nonce = message.get("nonce")
        if (
            not isinstance(nonce, str)
            or len(nonce) != 64
            or any(c not in "0123456789abcdef" for c in nonce)
        ):
            raise ProtocolError("Missing unpredictable launch binding")
        if (
            not isinstance(config.get("policy_digest"), str)
            or len(config["policy_digest"]) != 64
            or any(char not in "0123456789abcdef" for char in config["policy_digest"])
        ):
            raise ProtocolError("Missing versioned policy digest")
        config = _policy_check(dict(config))
        config["resources"] = limits(config.get("resources"))
        if config["resources"]["memory_mib"] < 128:
            raise ValueError("Heavy needs 128 MiB including supervisor reservation")
        if config["resources"]["enforcement"] == "hard":
            raise ValueError(
                "Owned hard cgroup enforcement is unavailable in this portable worker adapter"
            )
        self.config = config
        self.nonce = nonce
        self.operations = _manifest()
        task_limits(
            {}, config["resources"]
        )  # Check aggregate memory reservation before readiness.
        current_hard = resource.getrlimit(resource.RLIMIT_AS)[1]
        supervisor_as = 64 << 20
        if current_hard != resource.RLIM_INFINITY:
            supervisor_as = min(supervisor_as, current_hard)
        resource.setrlimit(resource.RLIMIT_AS, (supervisor_as, current_hard))
        if config.get("site_kind") == "desktop":
            if config.get("desktop_explicit") is not True:
                raise ValueError("Desktop Heavy requires explicit desktop policy")
            self.identity = {
                "site_kind": "desktop",
                "host": socket.gethostname(),
                "uid": os.getuid(),
                "pid": os.getpid(),
                "verified": True,
                "verification": "explicit approved desktop policy",
            }
            self.become_ready()
        elif config.get("site_kind") == "slurm":
            self.probe = placement_probe(config)
            self.probe["probe_id"] = uuid.uuid4().hex
            self.probe_started = time.monotonic()
            self.emit("placement_probe", candidate=self.probe)
        else:
            raise ValueError(
                "Heavy execution requires an explicit desktop or Slurm site"
            )

    def become_ready(self):
        self.ready = True
        self.emit(
            "hello",
            major=MAJOR,
            minor=MINOR,
            nonce=self.nonce,
            build_digest=build_digest(),
            policy_digest=self.config["policy_digest"],
            identity=self.identity,
            resources=self.config["resources"],
            operations=self.operations,
            enforcement={
                "mechanism": "rlimit-process-group",
                "hard_rss": False,
                "cpu_percentage": False,
                "native_threads": "per-task admitted CPUs",
                "placement": self.identity["verification"],
            },
        )
        del self.nonce

    def admit(self, task):
        if not self.ready or self.shutting_down:
            raise ValueError("Heavy worker is not ready for admission")
        if not isinstance(task, dict):
            raise ValueError("Invalid Heavy task")
        task_id = task.get("id")
        if not isinstance(task_id, str) or not 1 <= len(task_id) <= 128:
            raise ValueError("Invalid task identity")
        task_hash = digest(task)
        for old_id, old in tuple(self.tasks.items()):
            if len(self.tasks) < 128:
                break
            if old.get("process") is None and (
                old.get("acknowledged")
                or old.get("status") in {"failed", "cancelled", "uncertain"}
            ):
                self.tasks.pop(old_id)
                self.receipt_bytes -= old.get("retained_bytes", 0)
                self.retired[old_id] = old.get("request_digest")
                while len(self.retired) > 4096:
                    self.retired.popitem(last=False)
        if task_id in self.retired:
            raise ValueError(
                "Task identity was retired; create a new exact task identity"
            )
        if task_id in self.tasks:
            previous = self.tasks[task_id]
            if previous["request_digest"] != task_hash:
                raise ValueError("Task ID was reused with different dependencies")
            if previous.get("acknowledged"):
                self.emit(
                    "task_status",
                    id=task_id,
                    status=previous["status"],
                    receipt=previous.get("receipt"),
                )
            else:
                self.emit(
                    previous["status"],
                    id=task_id,
                    receipt=previous.get("receipt"),
                    result=previous.get("result"),
                )
            return
        if len(self.tasks) >= 1024:
            raise ValueError(
                "Worker task identity ledger is full; start a new generation"
            )
        if (
            sum(item.get("process") is not None for item in self.tasks.values())
            >= self.config["resources"]["max_tasks"]
        ):
            raise ValueError("Heavy task concurrency budget is full")
        if self.receipt_bytes >= MAX_QUEUE - self.config["resources"]["output_bytes"]:
            raise ValueError(
                "Acknowledge committed results before submitting more tasks"
            )
        _policy_check(self.config)  # Mandatory policy tightening stops new admission.
        operation = task.get("operation")
        if (
            operation not in self.operations
            or type(task.get("version")) is not int
            or task.get("version") != self.operations[operation]["version"]
        ):
            raise ValueError("Unsupported Heavy operation/version")
        inputs, context = task.get("inputs"), task.get("context")
        if not isinstance(context, dict) or len(canonical(context)) > 65536:
            raise ValueError("Task context must be an immutable bounded object")
        if task.get("input_digest") != digest(inputs):
            raise ValueError("Task input digest mismatch")
        if len(canonical(inputs)) > self.config["resources"]["input_bytes"]:
            raise ValueError("Task input exceeds its admitted byte budget")
        mutation = operation in {
            "artifact-publish",
            "evidence-index-update",
            "allocation-health-probe",
            "wasi-analysis-execute",
            "process-profile-capture",
        }
        expected_effects = self.operations[operation]["side_effects"]
        if task.get("side_effects", "none") != expected_effects:
            raise ValueError(
                "Task side-effect contract differs from the declared operation"
            )
        if task.get("cancel_policy", "terminate") != (
            "terminate-uncertain" if mutation else "terminate"
        ):
            raise ValueError(
                "Task cancellation policy differs from its side-effect contract"
            )
        if mutation:
            plan = inputs.get("plan") if isinstance(inputs, dict) else None
            if (
                not isinstance(plan, dict)
                or not plan.get("digest")
                or context.get("reviewed_plan_digest") != plan["digest"]
            ):
                raise ValueError(
                    "Artifact publication needs its exact reviewed plan digest"
                )
        elif expected_effects != "none":
            raise ValueError("No worker adapter for these external side effects")
        budget = task_limits(task.get("budget"), self.config["resources"])
        contract = self.operations[operation]
        target = (
            "desktop"
            if self.identity["site_kind"] == "desktop"
            else "allocated-compute"
        )
        if target not in contract["targets"]:
            raise ValueError(
                "Operation has no pure adapter for this execution placement"
            )
        for key, ceiling in contract["budget"].items():
            if key in budget:
                budget[key] = min(budget[key], ceiling)
        verified_execution = self.identity
        if operation in {"allocation-health", "allocation-health-probe"}:
            # Resource grants are supervisor-owned, never task/context claims.
            verified_execution = {
                **self.identity,
                "verified_resources": {
                    "cpus": min(int(budget["cpus"]), len(os.sched_getaffinity(0))),
                    "memory_mib": int(budget["memory_mib"]),
                },
            }
        request = {**task, "budget": budget, "verified_execution": verified_execution}
        raw = encode(request)
        entry = {
            "request_digest": task_hash,
            "context_digest": digest(context),
            "status": "accepted",
            "result": None,
            "process": None,
            "started": time.monotonic(),
            "deadline": time.monotonic() + budget["wall_seconds"],
            "decoder": Decoder(),
            "request": memoryview(raw),
            "stderr": bytearray(),
            "budget": budget,
            "cancelled": False,
            "payload": None,
            "mutation": mutation,
        }
        self.tasks[task_id] = entry
        self.emit(
            "accepted",
            id=task_id,
            context_digest=entry["context_digest"],
            request_digest=task_hash,
        )
        try:
            import threading

            if (
                sys.platform.startswith("linux")
                and hasattr(os, "fork")
                and threading.active_count() == 1
            ):
                # A controlled single-threaded fork server amortizes imports,
                # while every operation still receives fresh rlimits and pipes.
                from ..raven_execution import _PURE

                if operation in _PURE:
                    import importlib

                    importlib.import_module("raven." + _PURE[operation][0])
                from .fork_task import TaskProcess

                process = TaskProcess(child_environment(budget["cpus"]), task_main)
            else:
                process = subprocess.Popen(
                    worker_argv() + ["--task", "--parent-pid", str(os.getpid())],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    close_fds=True,
                    start_new_session=True,
                    env=child_environment(budget["cpus"]),
                )
            entry["process"] = process
            entry["pid"] = process.pid
            if hasattr(os, "pidfd_open"):
                try:
                    exit_fd = os.pidfd_open(process.pid, 0)
                    self.selector.register(
                        exit_fd, selectors.EVENT_READ, ("task_exit", task_id)
                    )
                    entry["exit_fd"] = exit_fd
                except OSError:
                    pass
            for stream, mode, label in (
                (process.stdin, selectors.EVENT_WRITE, "task_input"),
                (process.stdout, selectors.EVENT_READ, "task_output"),
                (process.stderr, selectors.EVENT_READ, "task_error"),
            ):
                os.set_blocking(stream.fileno(), False)
                self.selector.register(stream, mode, (label, task_id))
            entry["status"] = "running"
            self.emit(
                "running",
                id=task_id,
                context_digest=entry["context_digest"],
                pid=process.pid,
            )
        except BaseException:
            self.finish(
                task_id, "failed", error="Could not launch the admitted analysis child"
            )
            raise

    def finish(self, task_id, status, *, result=None, error=None, enforcement=None):
        entry = self.tasks[task_id]
        result_bytes = canonical(result)
        receipt = {
            "schema": "raven.worker-result/v1",
            "task_id": task_id,
            "session_id": self.session,
            "generation": self.generation,
            "worker_id": self.worker_id,
            "context_digest": entry["context_digest"],
            "request_digest": entry["request_digest"],
            "policy_digest": self.config["policy_digest"],
            "execution": self.identity,
            "status": status,
            "process_pid": entry.get("pid"),
            "completed_unix": time.time(),
            "elapsed_seconds": time.monotonic() - entry["started"],
            "result_digest": hashlib.sha256(result_bytes).hexdigest(),
            "enforcement": enforcement or {},
            "error": error or "",
        }
        entry["status"], entry["receipt"], entry["result"] = status, receipt, result
        entry["retained_bytes"] = len(result_bytes)
        self.receipt_bytes += entry["retained_bytes"]
        self.emit(status, id=task_id, result=result, receipt=receipt, error=error or "")

    def handle(self, message):
        op = message.get("op")
        if op == "hello":
            return self.hello(message)
        worker_control(message)
        if (
            message.get("session_id") != self.session
            or message.get("generation") != self.generation
        ):
            raise ProtocolError(
                "Worker message belongs to a different execution generation"
            )
        if op == "placement":
            if self.ready or self.probe is None:
                raise ValueError("Worker is not awaiting placement verification")
            if message.get("error"):
                raise ValueError(
                    "Placement unavailable: " + str(message["error"])[:1000]
                )
            self.identity = verify_placement(
                self.config, self.probe, message.get("records")
            )
            self.become_ready()
        elif op == "admit":
            self.admit(message.get("task"))
        elif op == "cancel":
            entry = self.tasks.get(message.get("id"))
            if entry and entry.get("process") is not None:
                entry["cancelled"] = True
                _stop(entry["process"])
        elif op == "ack":
            entry = self.tasks.get(message.get("id"))
            if (
                not entry
                or not entry.get("receipt")
                or message.get("result_digest") != entry["receipt"]["result_digest"]
            ):
                raise ValueError(
                    "Result acknowledgement does not match a committed receipt"
                )
            self.receipt_bytes -= entry.get("retained_bytes", 0)
            entry["retained_bytes"] = 0
            entry["result"] = None
            entry["acknowledged"] = True
            self.emit("acknowledged", id=message["id"], receipt=entry["receipt"])
        elif op == "status":
            entry = self.tasks.get(message.get("id"))
            self.emit(
                "task_status",
                id=message.get("id"),
                status=entry["status"] if entry else "unknown",
                receipt=entry.get("receipt") if entry else None,
            )
        elif op == "shutdown":
            self.shutting_down = True
        else:
            raise ProtocolError("Unknown worker control operation")

    def unregister(self, stream):
        try:
            self.selector.unregister(stream)
        except (KeyError, ValueError):
            pass
        stream.close()

    def task_io(self, key, label, task_id):
        entry = self.tasks[task_id]
        if label == "task_exit":
            self.selector.unregister(key.fileobj)
            os.close(key.fd)
            entry.pop("exit_fd", None)
            return
        if label == "task_input":
            try:
                count = os.write(key.fd, entry["request"][:65536])
                entry["request"] = entry["request"][count:]
            except BrokenPipeError:
                entry["request"] = memoryview(b"")
            if not entry["request"]:
                self.unregister(key.fileobj)
            return
        try:
            data = os.read(key.fd, 65536)
        except BlockingIOError:
            return
        if not data:
            self.unregister(key.fileobj)
            return
        if label == "task_error":
            entry["stderr"].extend(data[: max(0, 8192 - len(entry["stderr"]))])
        else:
            try:
                entry["decoder"].feed(data)
                messages = entry["decoder"].take(2)
                if messages:
                    if (
                        entry["payload"] is not None
                        or len(messages) != 1
                        or messages[0].get("id") != task_id
                    ):
                        raise ProtocolError("Analysis returned an invalid transaction")
                    entry["payload"] = messages[0]
            except (ValueError, TypeError):
                entry["cancelled"] = True
                entry["failure"] = "Analysis returned malformed or oversized output"
                _stop(entry["process"])

    def maintain(self):
        now = time.monotonic()
        if self.config is None and now - self.started > 15:
            raise ProtocolError("Worker handshake timed out")
        if (
            self.config is not None
            and now - self.started > self.config["resources"]["wall_seconds"]
        ):
            self.shutting_down = True
        if self.probe is not None and not self.ready and now - self.probe_started > 60:
            raise ProtocolError("Fresh placement validation timed out")
        for task_id, entry in list(self.tasks.items()):
            process = entry.get("process")
            if process is None:
                continue
            if self.shutting_down or now > entry["deadline"]:
                entry["cancelled"] = True
                if now > entry["deadline"]:
                    entry["failure"] = "Analysis wall-time budget expired"
                _stop(process)
            code = process.poll()
            if code is None:
                continue
            # Drain final pipe data after reaping before committing the result.
            for stream, label in (
                (process.stdout, "task_output"),
                (process.stderr, "task_error"),
            ):
                if not stream.closed:
                    for _ in range(20):
                        key = self.selector.get_key(stream)
                        self.task_io(key, label, task_id)
                        if stream.closed:
                            break
            for stream in (process.stdin, process.stdout, process.stderr):
                if not stream.closed:
                    self.unregister(stream)
            exit_fd = entry.pop("exit_fd", None)
            if exit_fd is not None:
                self.selector.unregister(exit_fd)
                os.close(exit_fd)
            entry["process"] = None
            payload = entry.get("payload") or {}
            if entry["cancelled"]:
                status = (
                    "uncertain"
                    if entry.get("mutation")
                    else "failed"
                    if entry.get("failure")
                    else "cancelled"
                )
                self.finish(
                    task_id,
                    status,
                    error=entry.get(
                        "failure",
                        "Cancelled by owner; inspect any partial artifact before retrying"
                        if entry.get("mutation")
                        else "Cancelled by owner",
                    ),
                )
            elif code == 0 and payload.get("op") == "task_result":
                result = payload.get("result")
                if len(canonical(result)) > entry["budget"]["output_bytes"]:
                    self.finish(
                        task_id,
                        "uncertain" if entry.get("mutation") else "failed",
                        error="Analysis result exceeded output budget",
                    )
                else:
                    self.finish(
                        task_id,
                        "committed",
                        result=result,
                        enforcement=payload.get("enforcement"),
                    )
            else:
                self.finish(
                    task_id,
                    "uncertain" if entry.get("mutation") else "failed",
                    error=payload.get("error")
                    or f"Analysis process exited with status {code}",
                )
            for field in ("decoder", "request", "stderr", "payload"):
                entry.pop(field, None)

    def run(self):
        try:
            while True:
                for message in self.decoder.take(16):
                    try:
                        self.handle(message)
                    except (
                        ValueError,
                        TypeError,
                        KeyError,
                        OSError,
                        ImportError,
                    ) as exc:
                        task = message.get("task")
                        task_id = (
                            task.get("id", message.get("id"))
                            if isinstance(task, dict)
                            else message.get("id")
                        )
                        if not isinstance(task_id, str) or len(task_id) > 128:
                            task_id = None
                        self.emit("error", error=str(exc)[:2000], id=task_id)
                        if not self.ready or isinstance(exc, ProtocolError):
                            self.shutting_down = True
                if self.shutting_down:
                    for entry in self.tasks.values():
                        _stop(entry.get("process"))
                self.maintain()
                if (
                    self.shutting_down
                    and not self.output
                    and not any(entry.get("process") for entry in self.tasks.values())
                ):
                    break
                now = time.monotonic()
                deadlines = (
                    [self.started + 15]
                    if self.config is None
                    else [self.started + self.config["resources"]["wall_seconds"]]
                )
                if self.probe is not None and not self.ready:
                    deadlines.append(self.probe_started + 60)
                deadlines.extend(
                    entry["deadline"]
                    for entry in self.tasks.values()
                    if entry.get("process") is not None
                )
                # Pipe EOF wakes completion; no 20 Hz timer is needed when idle.
                wait = max(0.0, min(deadlines) - now)
                fallback_reap = any(
                    entry.get("process") and "exit_fd" not in entry
                    for entry in self.tasks.values()
                )
                for key, _mask in self.selector.select(
                    min(wait, 0.05) if fallback_reap else wait
                ):
                    label, task_id = key.data
                    if label == "output":
                        self.flush()
                    elif label == "owner":
                        data = os.read(self.input_fd, 65536)
                        if data:
                            self.decoder.feed(data)
                        else:
                            self.selector.unregister(self.input_fd)
                            self.shutting_down = True
                    else:
                        self.task_io(key, label, task_id)
        finally:
            for entry in self.tasks.values():
                process = entry.get("process")
                if process:
                    _stop(process)
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
            self.selector.close()
        return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", action="store_true")
    parser.add_argument("--parent-pid", type=int)
    args = parser.parse_args(argv)
    try:
        return task_main(args.parent_pid) if args.task else Worker().run()
    except (ValueError, TypeError, OSError, ImportError, KeyError) as exc:
        # No nonce/config/environment is printed, even when handshake fails.
        sys.stderr.write("Raven worker: " + type(exc).__name__ + "\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
