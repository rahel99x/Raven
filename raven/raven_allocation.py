"""Nonblocking Light-side allocation manager and generation-bound worker proxy."""
from __future__ import annotations

from collections import deque
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import threading
import time

from .framing import Decoder as FramedDecoder, FramePolicy, finite_constant, frame, unique_object, write_pending
from .raven_allocation_adapters import VerifiedReuse, config_data, prepare

MAX_FRAME = 1024 * 1024
MAX_QUEUE = 8 * 1024 * 1024
ACTIVE = frozenset(("preparing", "pending", "starting", "heavy", "draining", "reconciling"))


def _validate(value, depth=0, budget=None):
    if budget is None:
        budget = [20000]
    budget[0] -= 1
    if depth > 16 or budget[0] < 0:
        raise ValueError("Allocation message exceeds structure limits")
    if isinstance(value, str):
        if len(value) > MAX_FRAME:
            raise ValueError("Allocation message string exceeds its limit")
        value.encode("utf-8")
    elif isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("Allocation message keys must be strings")
            key.encode("utf-8")
            _validate(item, depth + 1, budget)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _validate(item, depth + 1, budget)
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Allocation message numbers must be finite")
    elif value is not None and not isinstance(value, (bool, int, float)):
        raise ValueError("Allocation message contains an unsupported value")


POLICY = FramePolicy("Allocation", MAX_FRAME, MAX_FRAME + 65540)


def encode(message):
    _validate(message)
    data = json.dumps(message, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
    return frame(data, POLICY)


def _parse(raw):
    try:
        value = json.loads(raw, parse_constant=finite_constant, object_pairs_hook=unique_object)
        _validate(value)
        return value
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ValueError("Invalid allocation JSON frame") from exc


class Decoder(FramedDecoder):
    def __init__(self):
        super().__init__(POLICY, _parse)
        self.data = self.buffer  # Retain the published buffer attribute.


def plan(config, reuse=None):
    return prepare(config, reuse)


class WorkerProxy:
    def __init__(self, manager):
        self.manager = manager
        self.generation = manager.generation
        self.tasks = {}
        self._events = deque(maxlen=128)

    @property
    def ready(self):
        return self.generation == self.manager.generation and self.manager._state.get("state") == "heavy" and not self.manager._cancelled

    @property
    def status(self):
        return "closed" if self.generation != self.manager.generation else "ready" if self.ready else self.manager._state.get("state", "light")

    @property
    def identity(self):
        return self.manager._state.get("identity", {}) if self.generation == self.manager.generation else {}

    @property
    def resources(self):
        return self.manager._state.get("resources", {}) if self.generation == self.manager.generation else {}

    @property
    def operations(self):
        return self.manager._state.get("operations", {}) if self.generation == self.manager.generation else {}

    def submit(self, operation, inputs, context=None, budget=None, task_id=None):
        with self.manager._lock:
            if not self.ready:
                raise RuntimeError("Heavy execution is not ready; no local fallback is permitted")
            task_id = task_id or secrets.token_hex(16)
            if not isinstance(task_id, str) or len(task_id) > 128 or task_id in self.tasks:
                raise ValueError("Task identity is invalid or has already been used")
            if sum(task.get("status") not in ("cancelled", "failed", "acknowledged") for task in self.tasks.values()) >= 8:
                raise RuntimeError("Heavy task admission queue is full; acknowledge committed results before continuing")
            message = {"op": "submit", "task_id": task_id, "operation": operation, "inputs": inputs,
                       "context": context or {}, "budget": budget}
            self.tasks = {**self.tasks, task_id: {"task_id": task_id, "status": "submitted", "generation": self.manager.generation}}
            if not self.manager._send(message):
                self.tasks.pop(task_id, None)
                raise RuntimeError(self.manager._state.get("error") or "Heavy request queue is full")
            # Retain bounded transaction state; never evict a live task.
            if len(self.tasks) > 128:
                for old_id, task in tuple(self.tasks.items()):
                    if len(self.tasks) <= 128:
                        break
                    if task.get("status") in ("acknowledged", "cancelled", "failed"):
                        self.tasks.pop(old_id, None)
            return task_id

    def cancel(self, task_id):
        return self.generation == self.manager.generation and task_id in self.tasks and self.manager._send({"op": "cancel_task", "task_id": task_id})

    def ack(self, task_id):
        with self.manager._lock:
            accepted = self.generation == self.manager.generation and task_id in self.tasks and self.manager._send({"op": "ack", "task_id": task_id})
            if accepted:
                self.tasks = {**self.tasks, task_id: {key: value for key, value in self.tasks[task_id].items() if key != "result"}}
                self._events = deque((event for event in self._events if event.get("id", event.get("task_id")) != task_id), maxlen=128)
            return accepted

    def poll(self):
        self.manager.tick()
        with self.manager._lock:
            events = list(self._events)
            self._events.clear()
        return events

    def _event(self, event):
        task_id = event.get("task_id", event.get("id"))
        if task_id:
            old = self.tasks.get(task_id, {"task_id": task_id})
            status = event.get("status", event.get("op", "unknown"))
            self.tasks = {**self.tasks, task_id: {**old, **event, "task_id": task_id, "status": status}}
            if status == "acknowledged":
                self.tasks[task_id].pop("result", None)
        self._events.append(event)

    def close(self):
        return self.manager.release(generation=self.generation)


class AllocationManager:
    """One reviewed request and one helper lifetime; constructing it is read-only."""
    def __init__(self, config, *, state_dir=None, placement_verifier=None):
        self.config = config_data(config)
        self.policy_digest = getattr(config, "digest", None) or hashlib.sha256(json.dumps(self.config, sort_keys=True).encode()).hexdigest()
        self.state_dir = os.fspath(state_dir or self.config.get("storage", {}).get("state_dir", "~/.local/state/raven"))
        self.generation = 0
        self.request_id = ""
        self._state = {"state": "light", "mode": "light", "generation": 0, "request_id": "", "error": "", "cleanup_uncertain": False,
                       "target": self.config.get("site", {}).get("kind", "slurm"), "identity": {}, "resources": {}}
        self.worker = WorkerProxy(self)
        self._socket = None
        self._process = None
        self._queue = deque()
        self._queued = 0
        self._decoder = Decoder()
        self._lock = threading.RLock()
        self._closed = False
        self._cancelled = False
        self._sequence = 0
        self._placement_verifier = placement_verifier
        self._verification_pool = None
        self._verification = None
        self._verify_cancel = threading.Event()
        self._reuse_future = None
        self._reuse_receipt = None
        self._reuse_cancel = threading.Event()
        self.collection_backend = None
        self._recovery_future = None
        self._recovery_checked = False
        self._recovery_next = 0.
        self._recovery_reset = False
        self._recovery_failures = 0
        self._recovery_cancel = threading.Event()

    def plan(self, reuse=None):
        return prepare(self.config, reuse)

    def set_placement_verifier(self, callback):
        if callback is not None and not callable(callback):
            raise TypeError("Placement verifier must be a governed source callback")
        self._placement_verifier = callback

    def snapshot(self):
        return dict(self._state)

    def readiness(self):
        """UI readiness hints; tick retains all protocol and ownership actions."""
        import selectors
        if not self._lock.acquire(blocking=False):
            return (), False
        try:
            if self._socket is None or self._closed:
                return (), False
            mask = selectors.EVENT_READ | (selectors.EVENT_WRITE if self._queue else 0)
            return ((self._socket, mask),), self._decoder.has_complete_frame
        finally:
            self._lock.release()

    def _wake_on_completion(self, future):
        from .ui_wakeup import UIWakeup
        wakeup = getattr(self, "ui_wakeup", None)
        if type(wakeup) is UIWakeup:
            future.add_done_callback(wakeup.notify)

    def request_reuse_review(self, job_id):
        """Start one fresh, finite owned-allocation read; never run it on the UI."""
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("Allocation settings are busy; retry after saving finishes")
        try:
            return self._request_reuse_review(job_id)
        finally:
            self._lock.release()

    def _request_reuse_review(self, job_id):
        import re
        if self._closed or self._state["state"] in ACTIVE:
            raise RuntimeError("Return to Light before reviewing another allocation")
        if not isinstance(job_id, str) or not re.fullmatch(r"[0-9]{1,20}", job_id):
            raise ValueError("Reuse requires a numeric allocation JobID")
        if self.config.get("site", {}).get("kind") != "slurm":
            raise ValueError("Desktop workers do not reuse Slurm allocations")
        if getattr(self.collection_backend, "ledger", None) is None:
            raise ValueError("Reuse requires the governed collection owner")
        if self._reuse_future is not None:
            if self._state.get("reuse_review", {}).get("job_id") == job_id:
                return self._state["reuse_review"]["token"]
            raise RuntimeError("Another allocation review is still running")
        from .raven_allocation_reuse import review
        if self._verification_pool is None:
            self._verification_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="raven-placement")
        token = secrets.token_hex(16)
        reviewed_plan = self.plan()
        self._reuse_receipt = None
        self._reuse_cancel = threading.Event()
        future = self._verification_pool.submit(review, self.collection_backend, job_id, reviewed_plan, self._reuse_cancel)
        self._reuse_future = (future, token, self.generation, reviewed_plan["digest"])
        self._wake_on_completion(future)
        self._state = {**self._state, "reuse_review": {"state": "reviewing", "token": token, "job_id": job_id, "generation": self.generation}}
        return token

    # Explicit public alias for programmatic callers; it also never blocks.
    review_reuse = request_reuse_review

    def _finish_reuse_review(self):
        if not self._reuse_future or not self._reuse_future[0].done():
            return
        future, token, generation, digest = self._reuse_future
        self._reuse_future = None
        current = self._state.get("reuse_review", {})
        if self._closed or generation != self.generation or current.get("token") != token:
            return
        try:
            receipt = future.result()
            if self.plan()["digest"] != digest:
                raise ValueError("Configuration changed during allocation review")
            reviewed = self.plan(receipt)
            expires = time.monotonic() + max(0, 5 - (time.time() - receipt.observed_at))
            self._reuse_receipt = (token, receipt, generation, expires)
            value = {**current, "state": "ready", "plan": reviewed, "expires_at": expires, "error": ""}
        except Exception as exc:
            value = {**current, "state": "error", "error": str(exc)[:1000]}
        self._state = {**self._state, "reuse_review": value}

    def _resolve_reuse(self, reuse):
        if not isinstance(reuse, str):
            return reuse
        if self._reuse_receipt is None:
            raise ValueError("Allocation reuse needs a fresh reviewed token")
        token, receipt, generation, expires = self._reuse_receipt
        if reuse != token or generation != self.generation or time.monotonic() >= expires:
            raise ValueError("Allocation reuse review expired or changed; obtain a fresh review")
        return receipt

    def request(self, reuse=None, plan=None):
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("Allocation settings are busy; retry after saving finishes")
        try:
            return self._request(reuse, plan)
        finally:
            self._lock.release()

    def _request(self, reuse=None, plan=None):
        if self._closed:
            raise RuntimeError("Allocation manager is closed")
        if self._state["state"] in ACTIVE:
            return self.request_id
        reuse = self._resolve_reuse(reuse)
        reviewed = self.plan(reuse)
        if plan is not None and (not isinstance(plan, dict) or plan.get("digest") != reviewed["digest"]):
            raise ValueError("Allocation review changed; inspect the current plan before requesting compute")
        if self._process is not None and self._process.poll() is None:
            raise RuntimeError("Previous allocation helper is still draining")
        if self._verification is not None and not self._verification[0].done():
            raise RuntimeError("Previous placement verification is still draining")
        admission = None
        if self.config.get("schema") == "raven.config/v1":
            from .raven_collection import admit_allocation_launch
            admission = admit_allocation_launch(self.collection_backend, reviewed)
        self.generation += 1
        self._reuse_cancel.set()
        self.request_id = secrets.token_hex(16)
        self._cancelled = False
        self._verify_cancel = threading.Event()
        self._sequence = 0
        self._decoder = Decoder()
        self.worker = WorkerProxy(self)
        parent, child = socket.socketpair()
        try:
            self._process = subprocess.Popen([sys.executable, "-c", "import runpy,sys;sys.path.insert(0,sys.argv.pop(1));runpy.run_module('raven.raven_allocation_helper',run_name='__main__')",
                str(Path(__file__).resolve().parent.parent), "--fd", str(child.fileno())],
                pass_fds=(child.fileno(),), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                close_fds=True, start_new_session=True)
            parent.setblocking(False)
            self._socket = parent
        except BaseException:
            parent.close()
            raise
        finally:
            child.close()
        self._state = {**reviewed, "state": "preparing", "mode": "light", "request_id": self.request_id,
                       "generation": self.generation, "identity": {}, "job_id": "", "host": "", "error": "", "cleanup_uncertain": False,
                       "launch_admission": admission}
        if not self._send({"op": "start", "config": self.config, "plan": reviewed, "state_dir": self.state_dir, "policy_digest": self.policy_digest}):
            self._uncertain("Allocation helper request could not be delivered; inspect before retrying")
        return self.request_id

    def _send(self, message):
        with self._lock:
            if self._socket is None or self._closed:
                return False
            message = {**message, "generation": self.generation, "request_id": self.request_id}
            try:
                data = encode(message)
            except (ValueError, TypeError, OverflowError):
                self._state = {**self._state, "error": "Allocation command exceeds its protocol budget or is invalid"}
                return False
            if self._queued + len(data) > MAX_QUEUE:
                self._state = {**self._state, "error": "Allocation command queue is full; command was not accepted"}
                return False
            self._queue.append(memoryview(data))
            self._queued += len(data)
            self._flush()
            return self._socket is not None

    def _flush(self):
        if self._socket is None:
            return
        try:
            for size in write_pending(self._queue, self._socket.send):
                self._queued -= size
        except OSError:
            self._disconnect("Allocation helper connection was lost; cleanup is uncertain")

    def _uncertain(self, error):
        if self._state["state"] != "light":
            if self._state["state"] == "reconciling" and self._state.get("error"):
                error = self._state["error"]
            self._state = {**self._state, "state": "reconciling", "mode": "light", "cleanup_uncertain": True, "error": error}
        for task_id, task in tuple(self.worker.tasks.items()):
            if task.get("status") not in ("committed", "acknowledged", "cancelled", "failed"):
                self.worker.tasks = {**self.worker.tasks, task_id: {**task, "status": "failed", "error": "Execution connection lost; external outcome may be uncertain"}}

    def _disconnect(self, error):
        if self._socket is not None:
            self._socket.close()
            self._socket = None
        self._queue.clear()
        self._queued = 0
        self._uncertain(error)

    def _apply(self, message):
        if message.get("generation") != self.generation or message.get("request_id") != self.request_id:
            return False
        sequence = message.get("sequence", 0)
        if not isinstance(sequence, int) or sequence <= self._sequence:
            return False
        self._sequence = sequence
        op = message.get("op")
        if op == "state":
            state = message.get("state", {})
            if not isinstance(state, dict) or state.get("state") not in ACTIVE | {"light"}:
                return False
            if self._cancelled and state.get("state") in ("pending", "starting", "heavy"):
                state = {**state, "state": "draining", "mode": "light"}
            self._state = {**self._state, **state}
        elif op == "worker":
            event = message.get("event", {})
            if not isinstance(event, dict):
                return False
            if event.get("op") == "placement_probe":
                self._start_verification(event.get("candidate", {}))
            self.worker._event(event)
        return True

    def _start_verification(self, candidate):
        if self._cancelled:
            self._send({"op": "placement", "error": "Request was cancelled before placement verification"})
            return
        if self._verification is not None:
            self._send({"op": "placement", "error": "A placement verification is already active"})
            return
        if self._placement_verifier is None:
            self._send({"op": "placement", "error": "No governed placement verifier is configured; Heavy remains unavailable"})
            return
        if self._verification_pool is None:
            self._verification_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="raven-placement")
        generation, request_id = self.generation, self.request_id
        future = self._verification_pool.submit(self._placement_verifier, candidate, cancel=self._verify_cancel)
        self._verification = (future, generation, request_id)
        self._wake_on_completion(future)

    def _finish_verification(self):
        if not self._verification or not self._verification[0].done():
            return
        future, generation, request_id = self._verification
        self._verification = None
        if (generation, request_id) != (self.generation, self.request_id) or self._cancelled:
            return
        try:
            self._send({"op": "placement", "records": future.result()})
        except Exception as exc:
            self._send({"op": "placement", "error": str(exc)[:1000]})

    def retry_cleanup(self, generation=None):
        """Resume one bounded cleanup cycle; never allocate or resubmit a job."""
        with self._lock:
            if self._closed or generation is not None and generation != self.generation:
                return False
            if not self._state.get("cleanup_uncertain") or self._recovery_future is not None:
                return False
            self._recovery_reset = True
            self._recovery_failures = 0
            self._recovery_next = 0.
            return True

    def _tick_recovery(self):
        if self._closed:
            return
        now = time.time()
        if self._recovery_future is not None:
            future, generation, request_id = self._recovery_future
            if not future.done():
                return
            self._recovery_future = None
            if (generation, request_id) != (self.generation, self.request_id):
                return
            self._recovery_checked = True
            try:
                result = future.result()
                if result is None and self._state.get("cleanup_uncertain"):
                    result = {"state": "reconciling", "mode": "light", "cleanup_uncertain": True,
                              "error": "The ownership receipt is unavailable; elapsed time cannot establish cleanup",
                              "recovery": {"phase": "paused", "next_retry": 0.,
                                           "next_action": "Restore the original private receipt and choose Retry cleanup"}}
                self._recovery_failures = 0
            except Exception as exc:
                self._recovery_failures += 1
                result = {"state": "reconciling", "mode": "light", "cleanup_uncertain": True,
                          "error": "Allocation recovery: " + str(exc)[:1000],
                          "recovery": {"phase": "paused" if self._recovery_failures >= 8 else "owner unavailable",
                                       "attempts": self._recovery_failures, "next_retry": now + 30.,
                                       "next_action": "Wait for the existing owner or inspect the private receipt; no PID was signalled"}}
            if result is not None:
                self._state = {**self._state, **result}
                self._recovery_next = max(now + 1., result.get("recovery", {}).get("next_retry", now + 30.))
            return
        if self._socket is not None or self._process is not None and self._process.poll() is None:
            return
        if self._verification is not None or self._reuse_future is not None:
            return
        if self._recovery_checked and not self._state.get("cleanup_uncertain"):
            return
        if now < self._recovery_next or self._state.get("recovery", {}).get("phase") == "paused" and not self._recovery_reset:
            return
        if self.collection_backend is None and not self._state.get("cleanup_uncertain"):
            return
        from .raven_allocation_recovery import reconcile
        if self._verification_pool is None:
            self._verification_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="raven-placement")
        future = self._verification_pool.submit(reconcile, self.state_dir, self.plan(), self.collection_backend,
                                                self._recovery_cancel, reset=self._recovery_reset)
        self._recovery_reset = False
        self._recovery_future = (future, self.generation, self.request_id)
        self._wake_on_completion(future)

    def tick(self):
        if not self._lock.acquire(blocking=False):
            return False
        try:
            before = self._state
            self._finish_reuse_review()
            self._finish_verification()
            self._flush()
            changed = False
            if self._socket is not None:
                try:
                    for message in self._decoder.take():
                        changed = self._apply(message) or changed
                    budget = 262144
                    while budget:
                        try:
                            data = self._socket.recv(min(65536, budget))
                        except (BlockingIOError, InterruptedError):
                            break
                        if not data:
                            self._disconnect("Allocation helper exited without a confirmed cleanup receipt")
                            break
                        budget -= len(data)
                        self._decoder.feed(data)
                        for message in self._decoder.take():
                            changed = self._apply(message) or changed
                except (OSError, ValueError, TypeError, OverflowError):
                    self._disconnect("Allocation helper protocol failed; cleanup is uncertain")
            if self._process is not None and self._process.poll() is not None and self._state["state"] != "light":
                self._uncertain("Allocation helper stopped; inspect the exact request before taking recovery action")
            self._tick_recovery()
            return changed or before != self._state
        finally:
            self._lock.release()

    def cancel(self, generation=None):
        if generation is not None and generation != self.generation:
            return False
        if self._state["state"] not in ACTIVE:
            return False
        if self._cancelled:
            return True
        self._cancelled = True
        self._verify_cancel.set()
        self._reuse_cancel.set()
        self._state = {**self._state, "state": "draining", "mode": "light"}
        accepted = self._send({"op": "cancel"})
        if not accepted:
            self._uncertain("Allocation cancellation could not reach its owner; inspect the exact request")
        return accepted

    def release(self, generation=None):
        return self.cancel(generation)

    def shutdown(self):
        if self._closed:
            return
        self.cancel()
        self._verify_cancel.set()
        self._reuse_cancel.set()
        if self._socket is not None:
            self._send({"op": "shutdown"})
            self._socket.close()
            self._socket = None
        self._closed = True
        self._recovery_cancel.set()
        if self._verification_pool is not None:
            self._verification_pool.shutdown(wait=False, cancel_futures=True)
        if self._process is not None:
            try:
                self._process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._process.terminate()
                try:
                    self._process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    self._uncertain("Allocation helper cleanup is still pending; no unrelated allocation was cancelled")
                    threading.Thread(target=self._process.wait, name="raven-allocation-reaper", daemon=True).start()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.shutdown()
