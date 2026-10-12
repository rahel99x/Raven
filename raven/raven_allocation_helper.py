"""Foreground-owned Raven allocation supervisor, isolated from the Light UI."""
from __future__ import annotations

from collections import deque
import argparse
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import selectors
import shutil
import signal
import socket
import stat
import sys
import time

from .raven_allocation import Decoder, MAX_QUEUE, encode
from .raven_allocation_adapters import VerifiedReuse, build_launch, prepare, scrub_launch_environment


def process_start(pid):
    try:
        return Path(f"/proc/{int(pid)}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, ValueError, IndexError):
        return "unavailable"


class OwnershipRecord:
    """Per-user/context lock and private receipt; old uncertainty blocks replay."""
    def __init__(self, directory, plan, request_id):
        directory = Path(directory).expanduser() / "allocations"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Allocation receipts need a private directory owned by this user")
        key = hashlib.sha256(json.dumps({"site": plan["site_kind"], "cluster": plan["cluster_id"], "target": plan["target"]}, sort_keys=True).encode()).hexdigest()[:32]
        self.path = directory / (key + ".json")
        lock = directory / (key + ".lock")
        self.fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, 0o600)
        try:
            info = os.fstat(self.fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError("Invalid allocation ownership lock")
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                receipt_fd = os.open(self.path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
            except FileNotFoundError:
                receipt_fd = -1
            if receipt_fd >= 0:
                with os.fdopen(receipt_fd, "rb") as source:
                    meta = os.fstat(source.fileno())
                    if not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid() or meta.st_mode & 0o077 or meta.st_size > 65536:
                        raise ValueError("Invalid prior allocation receipt")
                    previous = json.loads(source.read(65537))
                    if previous.get("state") != "light" or previous.get("cleanup_uncertain"):
                        raise ValueError("A previous allocation request needs reconciliation; inspect its private receipt before starting another")
            self.request_id = request_id
        except BaseException:
            os.close(self.fd)
            self.fd = -1
            raise

    def write(self, value):
        data = (json.dumps({"schema": "raven.allocation-receipt/v1", **value}, allow_nan=False, sort_keys=True) + "\n").encode()
        if len(data) > 65536:
            raise ValueError("Allocation receipt exceeds its budget")
        temporary = self.path.with_name(self.path.name + "." + self.request_id + ".tmp")
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(data)
                output.flush()
            os.replace(temporary, self.path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def close(self):
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


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
        self.state = {"state": "light", "mode": "light", "cleanup_uncertain": False, "error": ""}
        self.started = time.monotonic()
        self.ready_at = 0
        self.last_work = self.started
        self.cancelled = False
        self.stopping = False
        self.done = False
        self.disconnect = False
        self.retained = False
        self.last_summary = 0
        self.close_requested = False
        self.subreaper = False
        self.lease_deadline = None

    def emit(self, op, **value):
        self.sequence += 1
        data = encode({"op": op, "generation": self.generation, "request_id": self.request_id, "sequence": self.sequence, **value})
        if self.out_bytes + len(data) > MAX_QUEUE:
            # A lost UI cannot leave unlimited results or allocated work running.
            self.stopping, self.cancelled = True, True
            self.state["error"] = "UI result channel exceeded its bounded queue"
            return
        self.out.append(memoryview(data))
        self.out_bytes += len(data)

    def publish(self, state, **extra):
        self.state = {**self.state, **extra, "state": state, "mode": "heavy" if state == "heavy" else "light",
                      "generation": self.generation, "request_id": self.request_id}
        if self.record is not None:
            receipt = {key: self.state.get(key) for key in ("state", "request_id", "generation", "target", "ownership", "identity", "job_id", "host", "error", "cleanup_uncertain")}
            receipt.update(helper_pid=os.getpid(), helper_start=process_start(os.getpid()), updated_at=time.time())
            try:
                self.record.write(receipt)
            except (OSError, ValueError) as exc:
                self.state.update(error="Ownership receipt could not be saved: " + str(exc), cleanup_uncertain=True)
                self.stopping = self.cancelled = True
        self.emit("state", state=self.state)

    def start(self, message):
        if self.client is not None or self.generation is not None:
            return
        self.generation = message.get("generation")
        self.request_id = message.get("request_id")
        if type(self.generation) is not int or self.generation < 1 or not isinstance(self.request_id, str) or len(self.request_id) != 32:
            raise ValueError("Invalid allocation request identity")
        plan = message.get("plan", {})
        reuse = plan.get("reuse")
        if reuse:
            reuse = VerifiedReuse(**{**reuse, "node_names": tuple(reuse.get("node_names", ()))})
        checked = prepare(message["config"], reuse)
        if checked["digest"] != plan.get("digest"):
            raise ValueError("Reviewed allocation plan changed before launch")
        self.plan = checked
        self.state.update({**checked, "identity": {}, "job_id": "", "host": "", "cleanup_uncertain": False})
        try:
            self.record = OwnershipRecord(message["state_dir"], checked, self.request_id)
            self.state["receipt_path"] = str(self.record.path)
        except (OSError, ValueError) as exc:
            self.publish("reconciling", error=str(exc), cleanup_uncertain=True)
            self.done = True
            return
        self.publish("preparing")
        launch_environment = scrub_launch_environment(cpus=checked["resources"]["cpus"])
        first_executable = build_launch(checked, [sys.executable, "-m", "raven.raven_worker"])[0]
        if shutil.which(first_executable, path=launch_environment.get("PATH")) is None:
            self.publish("light", error="Required launch executable is unavailable: " + first_executable, cleanup_uncertain=False)
            self.done = True
            return
        from .raven_worker_client import WorkerClient
        context = {"cluster_id": checked["cluster_id"],
                   "transport": checked["transport"], "host": checked["target"] if checked["transport"] == "ssh" else "",
                   **{key: checked["requested_resources"].get(key, "") for key in ("account", "partition", "qos")}}
        if checked["transport"] != "ssh":
            context["owner_uid"] = checked["owner_uid"]
        if reuse:
            context.update(allocation_id=reuse.job_id, submit_time=reuse.submit_time, start_time=reuse.start_time, owner_uid=reuse.owner_uid)
        config = {"site_kind": checked["site_kind"], "desktop_explicit": checked["site_kind"] == "desktop",
                  "session_id": self.request_id, "generation": self.generation, "policy_digest": message["policy_digest"],
                  "context": context, "resources": checked["resources"]}
        launcher = str(Path(__file__).with_name("raven_allocation_exec.py"))
        self.client = WorkerClient(config, build_launch=lambda argv: [sys.executable, launcher, str(os.getpid()), *build_launch(checked, argv)],
                                   launch_env=launch_environment)
        try:
            self.client.start()
        except (OSError, ValueError, RuntimeError) as exc:
            self.publish("light", error="Worker launch failed before allocation admission: " + str(exc), cleanup_uncertain=False)
            self.done = True
            return
        self.started = time.monotonic()
        self.publish("pending" if checked["site_kind"] == "slurm" else "starting")

    def handle(self, message):
        if message.get("op") == "start":
            self.start(message)
            return
        if message.get("generation") != self.generation or message.get("request_id") != self.request_id:
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
                from .slurm import parse_kv
                from .raven_allocation_reuse import duration
                fields = parse_kv(records.get("job_record", ""))
                if fields.get("TimeLimit") and fields.get("RunTime"):
                    remaining = duration(fields["TimeLimit"]) - duration(fields["RunTime"])
                    if remaining <= 1:
                        self.client.provide_placement(error="Allocation expires before worker admission")
                        return
                    self.lease_deadline = time.monotonic() + min(self.plan["resources"]["wall_seconds"], remaining - 1)
            self.client.provide_placement(message.get("records"), message.get("error"))
        elif op == "submit":
            if not self.client.ready or self.state["state"] != "heavy":
                self.emit("worker", event={"op": "failed", "id": message.get("task_id"), "error": "Heavy admission closed before this task was accepted"})
                return
            try:
                remaining = (self.lease_deadline or time.monotonic()) - time.monotonic()
                if remaining < 1:
                    raise ValueError("Allocation lease is draining; insufficient time to admit another task")
                budget = message.get("budget")
                if budget is not None and not isinstance(budget, dict):
                    raise ValueError("Task budget must be an object")
                budget = dict(budget or {})
                declared_wall = budget.get("wall_seconds")
                if declared_wall is not None and (isinstance(declared_wall, bool) or not isinstance(declared_wall, (int, float)) or not math.isfinite(declared_wall) or declared_wall > remaining):
                    raise ValueError("Task walltime budget exceeds the remaining allocation lease")
                if declared_wall is None:
                    budget["wall_seconds"] = min(remaining, self.plan["resources"]["wall_seconds"])
                self.client.submit(message["operation"], message["inputs"], message.get("context") or {},
                                   budget=budget, task_id=message["task_id"])
                self.last_work = time.monotonic()
            except (OSError, ValueError, RuntimeError) as exc:
                self.emit("worker", event={"op": "failed", "id": message.get("task_id"), "error": str(exc)[:2000]})
        elif op == "cancel_task":
            self.client.cancel(message.get("task_id"))
        elif op == "ack":
            self.client.ack(message.get("task_id"))
        elif op == "retain":
            self.retained = bool(message.get("retained"))

    def collect(self):
        if self.client is None:
            return
        for event in self.client.poll():
            if event.get("op") == "placement_probe":
                self.publish("starting")
            self.emit("worker", event=event)
        now = time.monotonic()
        if self.client.ready and not self.cancelled:
            if not self.ready_at:
                self.ready_at = self.last_work = now
                if self.lease_deadline is None:
                    self.lease_deadline = now + self.plan["resources"]["wall_seconds"]
                identity = self.client.identity
                self.publish("heavy", identity=identity, job_id=identity.get("allocation_id", identity.get("job_id", "")),
                             host=identity.get("host", ""), resources=self.client.resources, operations=self.client.operations,
                             error="", cleanup_uncertain=False)
            remaining = max(0, self.lease_deadline - now)
            active = any(task.get("status") in ("prepared", "accepted", "running") for task in self.client.tasks.values())
            if active or self.retained:
                self.last_work = now
            idle_limit = self.plan["idle_release_seconds"]
            if remaining <= 0 or (idle_limit and now - self.last_work >= idle_limit):
                self.cancelled = self.stopping = True
                self.publish("draining", error="Worker lease expired" if remaining <= 0 else "Idle compute released")
            elif now - self.last_summary >= 1:
                self.last_summary = now
                # Remaining-time publications do not rewrite a filesystem receipt.
                self.state = {**self.state, "remaining_seconds": round(remaining), "active_tasks": sum(task.get("status") in ("prepared", "accepted", "running") for task in self.client.tasks.values())}
                self.emit("state", state=self.state)
        elif not self.cancelled and self.client.status == "failed":
            self.cancelled = self.stopping = True
            self.publish("draining", error=self.client.error or "Worker startup or connection failed")
        elif not self.ready_at and now - self.started > self.plan["pending_timeout_seconds"]:
            self.cancelled = self.stopping = True
            self.publish("draining", error="Allocation request exceeded its pending deadline")

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
            if confirmed:
                self.publish("light", cleanup_uncertain=False, cleanup_basis="owned local process exited" if local else "owned foreground Slurm command completed successfully")
            else:
                self.publish("reconciling", cleanup_uncertain=True,
                             error=self.state.get("error") or "Owned worker stopped but allocation cleanup could not be confirmed; inspect the request receipt")
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
                for _key, _events in self.selector.select(.01):
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
                self.client.close(timeout=.5)
            if self.record:
                self.record.close()
            if self.subreaper:
                deadline = time.monotonic() + .25
                while time.monotonic() < deadline:
                    try:
                        while os.waitpid(-1, os.WNOHANG)[0]:
                            pass
                    except ChildProcessError:
                        break
                    time.sleep(.005)
            self.selector.close()
            self.channel.close()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "--remote-launch":
        command = argv[1:]
        if not command or command[0] not in ("salloc", "srun") or not all(isinstance(arg, str) and "\0" not in arg for arg in command):
            raise SystemExit("Unsupported remote allocation adapter command")
        os.execvpe(command[0], command, scrub_launch_environment())
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


if __name__ == "__main__":
    main()
