"""Foreground-owned Raven allocation supervisor, isolated from the Light UI."""

from __future__ import annotations

from collections import deque
import argparse
import math
import os
from pathlib import Path
import selectors
import shutil
import signal
import socket
import sys
import time
import subprocess

from .allocation import Decoder, MAX_QUEUE, encode
from .allocation_adapters import (
    VerifiedReuse,
    build_launch,
    prepare,
    scrub_launch_environment,
)


from .allocation_ownership import OwnershipRecord, boot_identity, process_start


class Supervisor:
    def __init__(self, channel):
        self.channel = channel
        channel.setblocking(False)
        self.selector = selectors.DefaultSelector()
        self.selector.register(channel, selectors.EVENT_READ)
        self.decoder = Decoder()
        self.out = deque()
        self.out_bytes = 0
        self.generation = None
        self.request_id = ""
        self.sequence = 0
        self.client = None
        self.plan = None
        self.record = None
        self.state = {
            "state": "light",
            "mode": "light",
            "cleanup_uncertain": False,
            "error": "",
        }
        self.started = time.monotonic()
        self.ready_at = 0
        self.last_work = self.started
        self.cancelled = False
        self.stopping = False
        self.done = False
        self.disconnect = False
        self.retained = False
        self.last_summary = 0
        self._summary_frame = None
        self.close_requested = False
        self.subreaper = False
        self.lease_deadline = None

    def emit(self, op, **value):
        self.sequence += 1
        try:
            data = encode(
                {
                    "op": op,
                    "generation": self.generation,
                    "request_id": self.request_id,
                    "sequence": self.sequence,
                    **value,
                }
            )
        except (ValueError, TypeError, OverflowError) as exc:
            event = value.get("event", {})
            task_id = (
                event.get("id", event.get("task_id"))
                if isinstance(event, dict)
                else None
            )
            if op != "worker" or not isinstance(task_id, str):
                raise
            # A representation failure belongs to this task, not to the
            # allocation that may own other accepted work.
            data = encode(
                {
                    "op": "worker",
                    "generation": self.generation,
                    "request_id": self.request_id,
                    "sequence": self.sequence,
                    "event": {
                        "op": "failed",
                        "id": task_id,
                        "error": "Task result cannot be relayed within its protocol budget: "
                        + str(exc)[:256],
                    },
                }
            )
            if self.client is not None:
                try:
                    self.client.ack(task_id)
                except (ValueError, RuntimeError, OSError):
                    pass
        if op == "state" and self._summary_frame is not None:
            for index, pending in enumerate(self.out):
                if pending is self._summary_frame:
                    self.out_bytes -= len(pending)
                    del self.out[index]
                    break
        if self.out_bytes + len(data) > MAX_QUEUE + (2 << 20 if op == "state" else 0):
            # A lost UI cannot leave unlimited results or allocated work running.
            self.stopping, self.cancelled = True, True
            self.state["error"] = "UI result channel exceeded its bounded queue"
            return
        pending = memoryview(data)
        self.out.append(pending)
        if op == "state":
            self._summary_frame = pending
        self.out_bytes += len(data)

    def publish(self, state, **extra):
        self.state = {
            **self.state,
            **extra,
            "state": state,
            "mode": "heavy" if state == "heavy" else "light",
            "generation": self.generation,
            "request_id": self.request_id,
        }
        if self.record is not None:
            receipt = {
                key: self.state.get(key)
                for key in (
                    "state",
                    "request_id",
                    "generation",
                    "site_kind",
                    "transport",
                    "ssh_target",
                    "ssh_options",
                    "target",
                    "ownership",
                    "reuse",
                    "identity",
                    "job_id",
                    "step_id",
                    "allocation_observation",
                    "host",
                    "error",
                    "cleanup_uncertain",
                    "request_name",
                    "connection_binding",
                    "created_at",
                    "owner_uid",
                    "cluster_id",
                    "cleanup_on_loss",
                )
            }
            receipt.update(
                helper_pid=os.getpid(),
                helper_start=process_start(os.getpid()),
                helper_host=socket.gethostname(),
                helper_boot=boot_identity(),
                updated_at=time.time(),
            )
            try:
                self.record.write(receipt)
            except (OSError, ValueError) as exc:
                self.state.update(
                    error="Ownership receipt could not be saved: " + str(exc),
                    cleanup_uncertain=True,
                )
                self.stopping = self.cancelled = True
        self.emit("state", state=self.state)

    def start(self, message):
        if self.client is not None or self.generation is not None:
            return
        self.generation = message.get("generation")
        self.request_id = message.get("request_id")
        if (
            type(self.generation) is not int
            or self.generation < 1
            or not isinstance(self.request_id, str)
            or len(self.request_id) != 32
        ):
            raise ValueError("Invalid allocation request identity")
        plan = message.get("plan", {})
        reuse = plan.get("reuse")
        if reuse:
            reuse = VerifiedReuse(
                **{**reuse, "node_names": tuple(reuse.get("node_names", ()))}
            )
        checked = prepare(
            message["config"], reuse, observed_cluster=plan.get("cluster_id")
        )
        if checked["digest"] != plan.get("digest"):
            raise ValueError("Reviewed allocation plan changed before launch")
        self.plan = checked
        message_timeout = plan.get("message_timeout_seconds", 10)
        if type(message_timeout) is not int or not 1 <= message_timeout <= 600:
            raise ValueError("Invalid scheduler message timeout")
        self.plan = {**checked, "message_timeout_seconds": message_timeout}
        checked = self.plan
        from .allocation_identity import connection_binding

        self.state.update(
            {
                "created_at": time.time(),
                "cleanup_on_loss": True,
                "request_name": (
                    "raven-heavy-"
                    if checked["ownership"] == "dedicated-allocation"
                    else "raven-step-"
                    if checked["ownership"] == "owned-step-only"
                    else ""
                )
                + self.request_id
                if checked["ownership"] != "local-worker"
                else "",
                "connection_binding": connection_binding(checked),
                **checked,
                "identity": {},
                "job_id": reuse.job_id if reuse else "",
                "step_id": "",
                "allocation_observation": {},
                "host": "",
                "cleanup_uncertain": False,
            }
        )
        try:
            self.record = OwnershipRecord(
                message["state_dir"], checked, self.request_id
            )
            self.state["receipt_path"] = str(self.record.path)
        except (OSError, ValueError) as exc:
            self.publish("reconciling", error=str(exc), cleanup_uncertain=True)
            self.done = True
            return
        self.publish("preparing")
        if self.stopping:
            self.done = True
            return
        launch_environment = scrub_launch_environment(cpus=checked["resources"]["cpus"])
        first_executable = build_launch(
            checked,
            [sys.executable, "-m", "raven.raven_worker"],
            request_id=self.request_id,
        )[0]
        if shutil.which(first_executable, path=launch_environment.get("PATH")) is None:
            self.publish(
                "light",
                error="Required launch executable is unavailable: " + first_executable,
                cleanup_uncertain=False,
            )
            self.done = True
            return
        from .worker_client import WorkerClient

        context = {
            "cluster_id": checked["cluster_id"],
            "transport": checked["transport"],
            "host": checked["target"] if checked["transport"] == "ssh" else "",
            **{
                key: checked["requested_resources"].get(key, "")
                for key in ("account", "partition", "qos")
            },
        }
        if checked["transport"] != "ssh":
            context["owner_uid"] = checked["owner_uid"]
        if reuse:
            context.update(
                allocation_id=reuse.job_id,
                submit_time=reuse.submit_time,
                start_time=reuse.start_time,
                owner_uid=reuse.owner_uid,
            )
        config = {
            "site_kind": checked["site_kind"],
            "desktop_explicit": checked["site_kind"] == "desktop",
            "session_id": self.request_id,
            "generation": self.generation,
            "policy_digest": message["policy_digest"],
            "context": context,
            "resources": checked["resources"],
        }
        launcher = str(Path(__file__).with_name("allocation_exec.py"))
        self.client = WorkerClient(
            config,
            build_launch=lambda argv: [
                sys.executable,
                launcher,
                str(os.getpid()),
                *build_launch(checked, argv, request_id=self.request_id),
            ],
            launch_env=launch_environment,
            observe_allocation=checked["ownership"] == "dedicated-allocation",
        )
        self.client.cleanup_timeout = max(35.0, float(message_timeout) + 5.0)
        try:
            self.client.start()
        except (OSError, ValueError, RuntimeError) as exc:
            self.publish(
                "light",
                error="Worker launch failed before allocation admission: " + str(exc),
                cleanup_uncertain=False,
            )
            self.done = True
            return
        self.started = time.monotonic()
        self.publish("pending" if checked["site_kind"] == "slurm" else "starting")

    def handle(self, message):
        if message.get("op") == "start":
            self.start(message)
            return
        if (
            message.get("generation") != self.generation
            or message.get("request_id") != self.request_id
        ):
            return
        op = message.get("op")
        if op in ("cancel", "shutdown", "release"):
            self.cancelled = self.stopping = True
            self.publish("draining", error=self.state.get("error", ""))
            return
        if self.client is None or self.cancelled:
            return
        if op == "placement":
            records = message.get("records")
            if isinstance(records, dict) and not message.get("error"):
                from ..slurm import parse_kv
                from .allocation_reuse import duration

                fields = parse_kv(records.get("job_record", ""))
                try:
                    remaining = duration(fields["TimeLimit"]) - duration(
                        fields["RunTime"]
                    )
                except (KeyError, ValueError):
                    self.client.provide_placement(
                        error="Allocation lease fields are missing, ambiguous or invalid"
                    )
                    return
                if remaining <= 1:
                    self.client.provide_placement(
                        error="Allocation expires before worker admission"
                    )
                    return
                self.lease_deadline = time.monotonic() + min(
                    self.plan["resources"]["wall_seconds"], remaining - 1
                )
            self.client.provide_placement(message.get("records"), message.get("error"))
        elif op == "submit":
            if not self.client.ready or self.state["state"] != "heavy":
                self.emit(
                    "worker",
                    event={
                        "op": "failed",
                        "id": message.get("task_id"),
                        "error": "Heavy admission closed before this task was accepted",
                    },
                )
                return
            try:
                remaining = (self.lease_deadline or time.monotonic()) - time.monotonic()
                if remaining < 1:
                    raise ValueError(
                        "Allocation lease is draining; insufficient time to admit another task"
                    )
                budget = message.get("budget")
                if budget is not None and not isinstance(budget, dict):
                    raise ValueError("Task budget must be an object")
                budget = dict(budget or {})
                declared_wall = budget.get("wall_seconds")
                if declared_wall is not None and (
                    isinstance(declared_wall, bool)
                    or not isinstance(declared_wall, (int, float))
                    or not math.isfinite(declared_wall)
                    or declared_wall > remaining
                ):
                    raise ValueError(
                        "Task walltime budget exceeds the remaining allocation lease"
                    )
                if declared_wall is None:
                    budget["wall_seconds"] = min(
                        remaining, self.plan["resources"]["wall_seconds"]
                    )
                self.client.submit(
                    message["operation"],
                    message["inputs"],
                    message.get("context") or {},
                    budget=budget,
                    task_id=message["task_id"],
                )
                self.last_work = time.monotonic()
            except (OSError, ValueError, RuntimeError) as exc:
                self.emit(
                    "worker",
                    event={
                        "op": "failed",
                        "id": message.get("task_id"),
                        "error": str(exc)[:2000],
                    },
                )
        elif op == "cancel_task":
            self.client.cancel(message.get("task_id"))
        elif op == "ack":
            self.client.ack(message.get("task_id"))
        elif op == "retain":
            self.retained = bool(message.get("retained"))

    def collect(self):
        if self.client is None:
            return
        events = self.client.poll()
        if (
            not self.ready_at
            and not self.cancelled
            and self.state["state"] in ("pending", "starting")
        ):
            # This observation can identify the current foreground request in
            # the queue while pending. It does not change admission, verified
            # identity, placement validation, or cleanup confirmation.
            job_id = self.client.observed_allocation_id
            if job_id != self.state.get("job_id", ""):
                observation = {}
                if job_id:
                    observation = {
                        "allocation_id": job_id,
                        "source": "owned-salloc-stderr",
                        "request_id": self.request_id,
                        "generation": self.generation,
                        "cluster_id": self.plan["cluster_id"],
                        "transport": self.plan["transport"],
                    }
                    if self.plan["transport"] != "ssh":
                        observation["owner_uid"] = self.plan["owner_uid"]
                self.publish(
                    self.state["state"],
                    job_id=job_id,
                    allocation_observation=observation,
                )
        for event in events:
            if event.get("op") == "placement_probe":
                candidate = event.get("candidate") or {}
                self.publish(
                    "starting",
                    job_id=str(
                        candidate.get(
                            "allocation_id",
                            candidate.get("job_id", self.state.get("job_id", "")),
                        )
                    ),
                    step_id=str(candidate.get("step_id", "")),
                )
            self.emit("worker", event=event)
        now = time.monotonic()
        if self.client.ready and not self.cancelled:
            if not self.ready_at:
                self.ready_at = self.last_work = now
                if self.lease_deadline is None:
                    self.lease_deadline = now + self.plan["resources"]["wall_seconds"]
                identity = self.client.identity
                self.publish(
                    "heavy",
                    identity=identity,
                    job_id=identity.get("allocation_id", identity.get("job_id", "")),
                    step_id=identity.get("step_id", ""),
                    allocation_observation={},
                    host=identity.get("host", ""),
                    resources=self.client.resources,
                    operations=self.client.operations,
                    error="",
                    cleanup_uncertain=False,
                )
            remaining = max(0, self.lease_deadline - now)
            active = any(
                task.get("status") in ("prepared", "accepted", "running")
                for task in self.client.tasks.values()
            )
            if active or self.retained:
                self.last_work = now
            idle_limit = self.plan["idle_release_seconds"]
            if remaining <= 0 or (idle_limit and now - self.last_work >= idle_limit):
                self.cancelled = self.stopping = True
                self.publish(
                    "draining",
                    error="Worker lease expired"
                    if remaining <= 0
                    else "Idle compute released",
                )
            elif now - self.last_summary >= 1:
                self.last_summary = now
                # Remaining-time publications do not rewrite a filesystem receipt.
                self.state = {
                    **self.state,
                    "remaining_seconds": round(remaining),
                    "active_tasks": sum(
                        task.get("status") in ("prepared", "accepted", "running")
                        for task in self.client.tasks.values()
                    ),
                }
                self.emit("state", state=self.state)
        elif not self.cancelled and self.client.status == "failed":
            self.cancelled = self.stopping = True
            self.publish(
                "draining",
                error=self.client.error or "Worker startup or connection failed",
            )
        elif (
            not self.ready_at
            and now - self.started > self.plan["pending_timeout_seconds"]
        ):
            self.cancelled = self.stopping = True
            self.publish(
                "draining", error="Allocation request exceeded its pending deadline"
            )

    def drain(self):
        if self.close_requested:
            return
        self.close_requested = True
        if self.client is not None:
            # Runs only in this isolated helper; collection permits and UI never
            # wait on an allocation or a permanent worker lifecycle.
            self.client.close(timeout=1)
            process = self.client.process
            code = process.poll() if process is not None else None
            local = self.plan["site_kind"] == "desktop"
            confirmed = process is None or (code is not None and (local or code == 0))
            rejected = (
                code is not None
                and self.client.allocation_rejected
                and not self.client.observed_allocation_id
            )
            relinquished = (
                code is not None
                and self.client.allocation_relinquished
                and not self.client.forced_stop
            )
            no_step = (
                code is not None
                and 0 < code < 255
                and self.plan["ownership"] == "owned-step-only"
                and not self.ready_at
                and not self.client.worker_id
                and not self.client.forced_stop
                and any(
                    reason in self.client.diagnostics.lower()
                    for reason in (
                        "unable to create step",
                        "requested nodes are busy",
                        "unable to allocate resources",
                        "immediate allocation impossible",
                    )
                )
            )
            confirmed = confirmed or rejected or relinquished or no_step
            if confirmed:
                error = (
                    self.client.allocation_rejection
                    if rejected
                    else self.state.get("error", "")
                )
                self.publish(
                    "light",
                    cleanup_uncertain=False,
                    error=error,
                    cleanup_basis="submission rejected before allocation existed"
                    if rejected
                    else "owned step exited before worker startup"
                    if no_step
                    else "salloc confirmed allocation relinquish"
                    if relinquished
                    else "owned local process exited"
                    if local
                    else "owned foreground Slurm command completed successfully",
                )
            else:
                self.publish(
                    "reconciling",
                    cleanup_uncertain=True,
                    error=self.state.get("error")
                    or "Owned worker stopped but allocation cleanup could not be confirmed; inspect the request receipt",
                )
        elif self.state["state"] not in ("light", "reconciling"):
            self.publish("light", cleanup_uncertain=False)
        self.done = True

    def flush(self):
        budget = 65536
        while self.out and budget:
            try:
                count = self.channel.send(self.out[0][:budget])
            except (BlockingIOError, InterruptedError):
                break
            except OSError:
                self.disconnect = self.stopping = self.cancelled = True
                self.out.clear()
                self.out_bytes = 0
                break
            if not count:
                break
            self.out_bytes -= count
            budget -= count
            self.out[0] = self.out[0][count:]
            if not self.out[0]:
                self.out.popleft()

    def run(self):
        client_fds = set()
        try:
            while True:
                for message in self.decoder.take():
                    self.handle(message)
                if self.stopping:
                    self.drain()
                if not self.done:
                    self.collect()
                self.flush()
                if self.done and (not self.out or self.disconnect):
                    break
                # The client's selector remains the sole pipe reader. Mirror
                # its readiness interests here so allocation commands, output,
                # process exit and leases can wake the existing helper owner.
                for fd in client_fds:
                    try:
                        self.selector.unregister(fd)
                    except (KeyError, ValueError, OSError):
                        pass
                client_fds.clear()
                buffered = self.decoder.has_complete_frame
                if self.client is not None:
                    descriptors, pending = self.client.readiness()
                    buffered = buffered or pending
                    for fileobj, events in descriptors:
                        try:
                            key = self.selector.register(fileobj, events, "worker")
                            client_fds.add(key.fd)
                        except (KeyError, ValueError, OSError):
                            pass
                self.selector.modify(
                    self.channel,
                    selectors.EVENT_READ | (selectors.EVENT_WRITE if self.out else 0),
                )
                now = time.monotonic()
                deadlines = [
                    self.started
                    + (self.plan["pending_timeout_seconds"] if self.request_id else 10)
                ]
                if self.ready_at:
                    deadlines = [self.last_summary + 1, self.lease_deadline or now + 1]
                    if self.plan["idle_release_seconds"] and not self.retained:
                        deadlines.append(
                            self.last_work + self.plan["idle_release_seconds"]
                        )
                delay = 0 if buffered else max(0, min(1, min(deadlines) - now))
                for _key, _events in self.selector.select(delay):
                    if _key.data == "worker" or not _events & selectors.EVENT_READ:
                        continue
                    try:
                        data = self.channel.recv(65536)
                    except (BlockingIOError, InterruptedError):
                        continue
                    if not data:
                        self.disconnect = self.stopping = self.cancelled = True
                        break
                    self.decoder.feed(data)
                if not self.request_id and time.monotonic() - self.started > 10:
                    break
        except (OSError, ValueError, RuntimeError, TypeError, KeyError) as exc:
            if self.request_id:
                self.publish("draining", error=str(exc)[:2000])
                self.stopping = self.cancelled = True
                self.drain()
                self.flush()
        finally:
            if self.client is not None:
                self.client.close(timeout=0.5)
            if self.record:
                self.record.close()
            if self.subreaper:
                deadline = time.monotonic() + 0.25
                while time.monotonic() < deadline:
                    try:
                        while os.waitpid(-1, os.WNOHANG)[0]:
                            pass
                    except ChildProcessError:
                        break
                    time.sleep(0.005)
            self.selector.close()
            self.channel.close()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "--remote-launch":
        command = argv[1:]
        cleanup_timeout = 35
        if command and command[0].startswith("--cleanup-timeout="):
            cleanup_timeout = int(command.pop(0).split("=", 1)[1])
            if not 5 <= cleanup_timeout <= 605:
                raise SystemExit("Invalid remote cleanup timeout")
        if (
            not command
            or command[0] not in ("salloc", "srun")
            or not all(isinstance(arg, str) and "\0" not in arg for arg in command)
        ):
            raise SystemExit("Unsupported remote allocation adapter command")
        return remote_launch(command, cleanup_timeout=cleanup_timeout)
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--fd", type=int, required=True)
    args = parser.parse_args(argv)
    supervisor = Supervisor(socket.socket(fileno=args.fd))
    if sys.platform.startswith("linux"):
        try:
            import ctypes

            supervisor.subreaper = ctypes.CDLL(None).prctl(36, 1, 0, 0, 0) == 0
        except (ImportError, AttributeError, OSError):
            pass

    def stop(*_args):
        supervisor.cancelled = supervisor.stopping = True

    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, stop)
    supervisor.run()


def remote_launch(command, *, cleanup_timeout=35):
    """Own remote salloc until SSH EOF has been translated into cleanup.

    SSH without a PTY supplies no terminal hang-up. This parent forwards the
    bounded worker input stream and explicitly hangs up its own child session.
    """
    shim = [
        sys.executable,
        str(Path(__file__).with_name("allocation_exec.py")),
        str(os.getpid()),
        *command,
    ]
    process = subprocess.Popen(
        shim,
        stdin=subprocess.PIPE,
        close_fds=True,
        start_new_session=True,
        env=scrub_launch_environment(),
    )
    selector = selectors.DefaultSelector()
    os.set_blocking(0, False)
    os.set_blocking(process.stdin.fileno(), False)
    selector.register(0, selectors.EVENT_READ, "input")
    pending = deque()
    queued = 0
    stopping = False
    deadline = None

    def stop(*_):
        nonlocal stopping
        stopping = True

    old_handlers = {
        sig: signal.signal(sig, stop)
        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)
    }
    try:
        while process.poll() is None:
            if stopping and deadline is None:
                # Close only the remote command's input and signal only the
                # session this parent created. salloc can now revoke/relinquish.
                try:
                    selector.unregister(0)
                except KeyError:
                    pass
                try:
                    selector.unregister(process.stdin)
                except KeyError:
                    pass
                process.stdin.close()
                try:
                    os.killpg(process.pid, signal.SIGHUP)
                except ProcessLookupError:
                    pass
                deadline = time.monotonic() + cleanup_timeout
            if deadline is not None and time.monotonic() >= deadline:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                break
            for key, _ in selector.select(
                min(0.25, max(0.0, deadline - time.monotonic())) if deadline else 0.25
            ):
                if key.data == "input":
                    raw = os.read(0, 65536)
                    if not raw:
                        stopping = True
                        break
                    if queued + len(raw) > MAX_QUEUE:
                        stopping = True
                        break
                    pending.append(memoryview(raw))
                    queued += len(raw)
                    if process.stdin not in [
                        item.fileobj for item in selector.get_map().values()
                    ]:
                        selector.register(
                            process.stdin, selectors.EVENT_WRITE, "output"
                        )
                else:
                    try:
                        count = os.write(key.fd, pending[0])
                    except (BrokenPipeError, OSError):
                        stopping = True
                        break
                    queued -= count
                    pending[0] = pending[0][count:]
                    if not pending[0]:
                        pending.popleft()
                    if not pending:
                        selector.unregister(process.stdin)
        return process.wait(timeout=2)
    finally:
        selector.close()
        if not process.stdin.closed:
            process.stdin.close()
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    raise SystemExit(main())
