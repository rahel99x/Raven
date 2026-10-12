"""Nonblocking, identity-bound client for an owned Raven Heavy worker.

Use ``wait`` only from an existing background lane. UI and allocation loops use
``poll``; this module never starts a dashboard sampler or calls scheduler APIs.
"""
from __future__ import annotations

from collections import deque
import os
import json
import re
import selectors
import signal
import struct
import subprocess
import sys
import time
import uuid

from .raven_protocol import MAJOR, MINOR, MAX_QUEUE, Decoder, ProtocolError, build_digest, canonical, digest, encode, worker_argv


class WorkerClient:
    def __init__(self, config, *, launch_argv=None, build_launch=None, launch_env=None, placement_verifier=None,
                 observe_allocation=False):
        self.config = json.loads(canonical(config))
        self.config.setdefault("session_id", uuid.uuid4().hex)
        self.config.setdefault("generation", 1)
        self.config.setdefault("policy_digest", digest({key: value for key, value in self.config.items() if key not in ("session_id", "generation")}))
        self.session_id = self.config["session_id"]
        self.generation = self.config["generation"]
        self.nonce = os.urandom(32).hex()
        self.launch_argv, self.build_launch, self.launch_env = launch_argv, build_launch, launch_env
        self.placement_verifier = placement_verifier
        self.process = None
        self.selector = selectors.DefaultSelector()
        self.decoder = Decoder()
        self.output = deque()
        self.output_bytes = 0
        self.stderr = bytearray()
        # Enabled only by the foreground allocation supervisor. This is a
        # presentation hint, never worker identity or placement evidence.
        self.observed_allocation_id = ""
        self._allocation_observation_open = bool(observe_allocation and self.config.get("site_kind") == "slurm")
        self._allocation_stderr_offset = 0
        self.ready = False
        self.status = "new"
        self.error = ""
        self.identity = {}
        self.placement = None
        self.resources = {}
        self.operations = {}
        self.tasks = {}
        self.result_bytes = 0
        self.worker_id = None
        self.sequence = 0
        self._events = deque()
        self._closed = False
        self._write_registered = False
        self._stdout_eof = False

    def _message(self, op, **payload):
        return {"op": op, "session_id": self.session_id, "generation": self.generation, **payload}

    def _queue(self, message):
        raw = encode(message)
        if self.output_bytes + len(raw) > MAX_QUEUE:
            raise ProtocolError("Heavy worker input queue exceeds 8 MiB")
        if self.process is None or self._closed or self.process.poll() is not None:
            raise RuntimeError("Heavy worker process is unavailable")
        self.output.append(memoryview(raw))
        self.output_bytes += len(raw)
        if not self._write_registered:
            self.selector.register(self.process.stdin, selectors.EVENT_WRITE, "write")
            self._write_registered = True
        self._flush()

    def start(self):
        if self.process is not None:
            return not self._closed
        command = [sys.executable, "-m", "raven.raven_worker"]
        if self.build_launch is not None:
            command = self.build_launch(command)
        elif self.launch_argv is not None:
            command = self.launch_argv
        else:
            command = worker_argv()
        if not isinstance(command, (list, tuple)) or not command or not all(isinstance(arg, str) and arg and "\0" not in arg for arg in command):
            raise ValueError("Heavy launch must be a structured nonempty argument list")
        hello = {"op": "hello", "major": MAJOR, "minor": MINOR, "nonce": self.nonce,
                 "build_digest": build_digest(), "config": self.config}
        encode(hello)  # Validate before any process starts.
        self._expected_build = hello["build_digest"]
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        close_fds=True, start_new_session=True, env=self.launch_env)
        for stream, label in ((self.process.stdout, "read"), (self.process.stderr, "stderr")):
            os.set_blocking(stream.fileno(), False)
            self.selector.register(stream, selectors.EVENT_READ, label)
        os.set_blocking(self.process.stdin.fileno(), False)
        self.status = "starting"
        self._queue(hello)
        return True

    def _flush(self):
        budget = 65536
        while self.output and budget > 0:
            try:
                written = os.write(self.process.stdin.fileno(), self.output[0][:budget])
            except (BlockingIOError, InterruptedError):
                return
            except BrokenPipeError:
                self._fail("Heavy worker input closed")
                return
            self.output_bytes -= written
            budget -= written
            self.output[0] = self.output[0][written:]
            if not self.output[0]:
                self.output.popleft()
        if not self.output and self._write_registered:
            self.selector.unregister(self.process.stdin)
            self._write_registered = False

    def provide_placement(self, records=None, error=None):
        if self.status != "placement" or self.placement is None:
            raise ValueError("Worker is not awaiting placement validation")
        if records is not None:
            if records.get("probe_id") not in (None, self.placement["probe_id"]):
                raise ValueError("Placement records belong to another worker probe")
            records = {**records, "probe_id": self.placement["probe_id"]}
        self._queue(self._message("placement", records=records, error=error))
        return True

    def _retain_stderr(self, raw):
        self.stderr.extend(raw[:max(0, 16384 - len(self.stderr))])
        if not self._allocation_observation_open:
            return
        while True:
            end = self.stderr.find(b"\n", self._allocation_stderr_offset)
            if end < 0:
                break
            line = bytes(self.stderr[self._allocation_stderr_offset:end]).removesuffix(b"\r")
            self._allocation_stderr_offset = end + 1
            match = re.fullmatch(rb"salloc: (?:Pending job allocation|Granted job allocation) ([1-9][0-9]{0,19})", line)
            if match is None:
                continue
            job_id = match.group(1).decode("ascii")
            if self.observed_allocation_id and job_id != self.observed_allocation_id:
                self.observed_allocation_id = ""
                self._allocation_observation_open = False
                break
            self.observed_allocation_id = job_id
        if len(self.stderr) >= 16384:
            self._allocation_observation_open = False

    def _finish_allocation_observation(self, message):
        # The first worker publication ends launch-output observation, before
        # analysis tasks can produce diagnostics containing unrelated JobIDs.
        self._allocation_observation_open = False
        value = message.get("candidate") if message.get("op") == "placement_probe" else message.get("identity")
        if isinstance(value, dict) and self.observed_allocation_id:
            job_id = value.get("allocation_id", value.get("job_id"))
            if str(job_id) != self.observed_allocation_id:
                self.observed_allocation_id = ""

    def _consume(self, message):
        if message.get("session_id") != self.session_id or message.get("generation") != self.generation:
            raise ProtocolError("Worker publication belongs to another generation")
        worker_id = message.get("worker_id")
        sequence = message.get("seq")
        if not isinstance(worker_id, str) or not worker_id or type(sequence) is not int or sequence != self.sequence + 1:
            raise ProtocolError("Worker publication identity or sequence is invalid")
        if self.worker_id is not None and worker_id != self.worker_id:
            raise ProtocolError("Worker identity changed within an execution generation")
        self.worker_id, self.sequence = worker_id, sequence
        op = message.get("op")
        self._finish_allocation_observation(message)
        if op == "hello":
            if (type(message.get("major")) is not int or message.get("major") != MAJOR or
                    type(message.get("minor")) is not int or message["minor"] < 0 or message.get("nonce") != self.nonce or
                    message.get("build_digest") != self._expected_build or
                    message.get("policy_digest") != self.config["policy_digest"]):
                raise ProtocolError("Worker handshake did not match launch, build, or policy")
            self.ready, self.status = True, "ready"
            self.identity = message.get("identity") or {}
            self.resources = message.get("resources") or {}
            self.operations = message.get("operations") or {}
            message = {key: value for key, value in message.items() if key != "nonce"}
            self.nonce = ""  # Never expose the launch nonce through normal events.
        elif op == "placement_probe":
            if self.ready:
                raise ProtocolError("A ready worker cannot change placement")
            self.placement = message.get("candidate")
            if not isinstance(self.placement, dict) or not self.placement.get("probe_id"):
                raise ProtocolError("Invalid worker placement probe")
            self.status = "placement"
            if self.placement_verifier is not None:
                try:
                    records = self.placement_verifier(dict(self.placement))
                    self.provide_placement(records)
                except (ValueError, OSError, RuntimeError) as exc:
                    self.provide_placement(error=str(exc)[:1000])
        elif op in ("accepted", "running", "committed", "failed", "cancelled", "uncertain", "acknowledged", "task_status"):
            task = self.tasks.get(message.get("id"))
            if task is None:
                raise ProtocolError("Worker result targets an unknown task")
            context_digest = message.get("context_digest") or (message.get("receipt") or {}).get("context_digest")
            if context_digest is not None and context_digest != task["context_digest"]:
                raise ProtocolError("Worker result targets a different exact task context")
            receipt = message.get("receipt")
            if receipt:
                if (receipt.get("session_id") != self.session_id or receipt.get("generation") != self.generation or
                        receipt.get("task_id") != message["id"] or receipt.get("worker_id") != self.worker_id or
                        receipt.get("request_digest") != task["request_digest"]):
                    raise ProtocolError("Worker result receipt identity mismatch")
            if op == "committed":
                if not receipt or receipt.get("result_digest") != digest(message.get("result")):
                    raise ProtocolError("Worker result does not match its committed digest")
                retained = len(canonical(message.get("result")))
                if self.result_bytes - task.get("retained_bytes", 0) + retained > MAX_QUEUE:
                    raise ProtocolError("Committed Heavy results exceed the client retention budget")
                self.result_bytes += retained - task.get("retained_bytes", 0)
                task.update(status="committed", result=message.get("result"), receipt=receipt, retained_bytes=retained)
            elif op == "acknowledged":
                task["acknowledged"] = True
                self.result_bytes -= task.get("retained_bytes", 0)
                task["retained_bytes"] = 0
                task["result"] = None
            elif op == "task_status":
                task["worker_status"] = message.get("status")
            else:
                task.update(status=op, error=message.get("error", ""))
                if op == "running":
                    task["pid"] = message.get("pid")
                if receipt:
                    task["receipt"] = receipt
        elif op == "error":
            self.error = str(message.get("error", "Worker operation failed"))[:2000]
            task = self.tasks.get(message.get("id"))
            if task:
                task.update(status="failed", error=self.error)
            elif not self.ready:
                self.status = "failed"
        else:
            raise ProtocolError("Unknown worker publication")
        self._events.append(message)

    def _fail(self, error):
        self._allocation_observation_open = False
        self.ready = False
        self.status = "failed"
        self.error = str(error)[:2000]
        for task in self.tasks.values():
            if task["status"] in ("prepared", "accepted", "running"):
                task.update(status="uncertain", error="Worker disconnected before a committed result")

    def poll(self):
        if self.process is None or self._closed:
            return []
        try:
            for message in self.decoder.take(32):
                self._consume(message)
            for key, _mask in self.selector.select(0):
                if key.data == "write":
                    self._flush()
                    continue
                try:
                    raw = os.read(key.fd, 65536)
                except (BlockingIOError, InterruptedError):
                    continue
                if not raw:
                    self.selector.unregister(key.fileobj)
                    if key.data == "read":
                        self._stdout_eof = True
                    continue
                if key.data == "stderr":
                    self._retain_stderr(raw)
                else:
                    self.decoder.feed(raw)
            for message in self.decoder.take(32):
                self._consume(message)
            code = self.process.poll()
            if code is not None and self._stdout_eof:
                # A worker can exit with its final result still buffered in the
                # pipe or decoder. Drain it across bounded polls before marking
                # pending tasks uncertain. Complete queued frames remain valid.
                remaining = self.decoder.buffer
                if remaining and (len(remaining) < 4 or len(remaining) < 4 + struct.unpack("!I", remaining[:4])[0]):
                    raise ProtocolError("Heavy worker closed a truncated result frame")
                if not remaining:
                    self._fail(self.error or f"Heavy worker exited with status {code}")
        except (ProtocolError, OSError, ValueError, KeyError) as exc:
            self._fail(str(exc))
            if self.process.poll() is None:
                try:
                    os.killpg(self.process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        events = list(self._events)
        self._events.clear()
        return events

    def submit(self, operation, inputs, context, budget=None, task_id=None):
        if not self.ready or self._closed:
            raise RuntimeError("Heavy worker is not ready")
        task_id = task_id or uuid.uuid4().hex
        if task_id in self.tasks or len(self.tasks) >= 1024:
            raise ValueError("Task identity already exists or the generation ledger is full")
        inputs, context, budget = json.loads(canonical([inputs, context, budget or {}]))
        task = {"id": task_id, "operation": operation, "version": 1,
                "inputs": inputs, "input_digest": digest(inputs), "context": context,
                "budget": budget or {},
                "side_effects": self.operations.get(operation, {}).get("side_effects", "none"),
                "cancel_policy": "terminate-uncertain" if operation == "artifact-publish" else "terminate"}
        self._queue(self._message("admit", task=task))
        self.tasks[task_id] = {"status": "prepared", "result": None, "error": "", "context_digest": digest(context),
                               "request_digest": digest(task), "operation": operation, "acknowledged": False}
        return task_id

    submit_task = submit

    def cancel(self, task_id):
        if task_id not in self.tasks:
            return False
        self._queue(self._message("cancel", id=task_id))
        return True

    def ack(self, task_id):
        task = self.tasks.get(task_id)
        if not task or not task.get("receipt"):
            return False
        if self.process is None or self._closed or self.process.poll() is not None:
            return False  # A validated commit survives its worker's later exit.
        self._queue(self._message("ack", id=task_id, result_digest=task["receipt"]["result_digest"]))
        return True

    def result(self, task_id):
        task = self.tasks.get(task_id)
        return task.get("result") if task and task["status"] == "committed" else None

    def wait(self, task_id, timeout, cancelled=None):
        if not 0 < timeout <= 86400:
            raise ValueError("Analysis wait requires a bounded positive timeout")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.poll()
            task = self.tasks.get(task_id)
            if task is None:
                raise ValueError("Unknown Heavy task")
            if task["status"] == "committed":
                result = task["result"]
                self.ack(task_id)
                return result
            if task["status"] in ("failed", "cancelled", "uncertain"):
                raise RuntimeError(task.get("error") or "Heavy task did not commit")
            if cancelled is not None and cancelled():
                self.cancel(task_id)
                raise RuntimeError("Heavy analysis was cancelled; owned execution is draining")
            time.sleep(.01)
        self.cancel(task_id)
        raise TimeoutError("Heavy analysis deadline expired; owned execution is draining")

    @property
    def diagnostics(self):
        return self.stderr.decode("utf-8", "replace")

    def close(self, timeout=2.0):
        if self._closed:
            return
        if self.process is not None and self.process.poll() is None:
            try:
                self._queue(self._message("shutdown"))
            except (OSError, RuntimeError, ValueError):
                pass
            deadline = time.monotonic() + max(0, min(5, timeout))
            while self.process.poll() is None and time.monotonic() < deadline:
                self.poll()
                time.sleep(.01)
            if self.process.poll() is None:
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.error = "Worker process is still draining after termination"
        self.ready, self.status, self._closed = False, "closed", True
        self.selector.close()
        if self.process is not None:
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                stream.close()
        self.output.clear()
        self.output_bytes = 0

    shutdown = close

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.close()
