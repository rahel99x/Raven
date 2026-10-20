"""Cooperative control over an application's existing gather/broadcast transport.

The communicator follows mpi4py's Get_rank/Get_size/gather/bcast interface.
Only rank zero accesses node-local SQLite. All members must call methods in the
same order from their application control thread. Transport failures belong to
the application's MPI/process-group failure policy; Raven creates no extra ranks,
listeners, scheduler calls, or worker threads.
"""
from __future__ import annotations

import time

from .operation_support import canonical, digest
from .runtime_contracts import identifier, integer, number
from .runtime_control import RuntimeControl


class CollectiveControlError(RuntimeError):
    def __init__(self, message, receipt=None):
        super().__init__(message)
        self.receipt = receipt


class CollectiveRuntime:
    def __init__(self, communicator, mailbox, attempt, *, initial_epoch=0):
        self.comm = communicator
        self.rank = integer(communicator.Get_rank(), "rank", 0, 4095)
        self.size = integer(communicator.Get_size(), "group size", 1, 4096)
        if self.rank >= self.size:
            raise ValueError("Rank is outside the communicator")
        self.attempt = attempt
        self.epoch = initial_epoch
        self.sequence = 0
        self.control = RuntimeControl(mailbox, attempt) if self.rank == 0 else None
        self._agree("initialize", {"attempt": attempt, "epoch": initial_epoch})
        def inspect():
            identifier(attempt, "exact attempt")
            integer(initial_epoch, "restored parameter epoch", 0, 2**53)
            actual = self.control.status()["epochs"][attempt]
            if actual != initial_epoch:
                raise ValueError("Application parameter state must be restored to the mailbox epoch before collective control starts")
            return {"epoch": actual}
        self._root(inspect)

    def _agree(self, operation, details):
        header = {"sequence": self.sequence, "operation": operation, "attempt": self.attempt, "details": details}
        headers = self.comm.gather(header, root=0)
        error = None
        if self.rank == 0:
            if len(headers) != self.size or any(value != headers[0] for value in headers):
                error = "Collective control calls differ in operation, sequence, attempt or parameters"
        error = self.comm.bcast(error, root=0)
        self.sequence += 1
        if error:
            raise CollectiveControlError(error)

    def _root(self, action):
        reply = None
        if self.rank == 0:
            try:
                result = action()
                if len(canonical(result)) > 262144:
                    raise ValueError("Collective control reply exceeds 256 KiB")
                reply = {"ok": True, "result": result}
            except Exception as exc:
                reply = {"ok": False, "error": str(exc)[:1024]}
        reply = self.comm.bcast(reply, root=0)
        if not reply["ok"]:
            raise CollectiveControlError(reply["error"])
        return reply["result"]

    def safe_point(self, name, prepare, commit):
        """Stage side-effect-free changes on every rank, then commit and acknowledge.

        prepare(command, parameters, request_id) returns a local staged object.
        commit(staged) applies it and returns bounded JSON. A failed preparation
        rejects without application. A failed commit is uncertain on all ranks;
        it is never replayed automatically. This is not a distributed transaction
        rollback facility for arbitrary scientific application state.
        """
        self._agree("safe-point", {"name": name, "epoch": self.epoch})
        request = self._root(lambda: self.control.poll(name))
        if request is None:
            return None
        if request["expected_epoch"] != self.epoch:
            receipt = self._root(lambda: self.control.mark_uncertain(request["id"], request["claim"], {"reason": "rank parameter epoch differs"}))
            raise CollectiveControlError("Application state differs from the requested parameter epoch", receipt)
        staged = None
        try:
            staged = prepare(request["command"], request["parameters"], request["id"])
            local = {"rank": self.rank, "ok": True}
        except Exception as exc:
            local = {"rank": self.rank, "ok": False, "error": str(exc)[:256]}
        preparations = self.comm.gather(local, root=0)
        failures = [row for row in preparations if not row["ok"]] if self.rank == 0 else None
        ready = self.comm.bcast(not failures if self.rank == 0 else None, root=0)
        if not ready:
            return self._root(lambda: self.control.acknowledge(request["id"], request["claim"], applied=False,
                                                               result={"reason": "rank preparation failed", "failures": failures[:16], "failed_ranks": len(failures)}))
        try:
            result = commit(staged)
            if len(canonical(result)) > 4096:
                raise ValueError("Per-rank application result exceeds 4 KiB")
            local = {"rank": self.rank, "ok": True, "result_digest": digest(result)}
        except Exception as exc:
            local = {"rank": self.rank, "ok": False, "error": str(exc)[:256]}
        outcomes = self.comm.gather(local, root=0)
        def acknowledge():
            failures = [row for row in outcomes if not row["ok"]]
            result = {"ranks": self.size, "outcomes_digest": digest(outcomes), "failed_ranks": len(failures), "failures": failures[:16]}
            if failures:
                return self.control.mark_uncertain(request["id"], request["claim"], result)
            return self.control.acknowledge(request["id"], request["claim"], applied=True, result=result)
        receipt = self._root(acknowledge)
        if receipt["status"] != "applied":
            raise CollectiveControlError("A rank could not confirm application; reconcile all ranks before continuing", receipt)
        self.epoch = request["expected_epoch"] + 1
        return receipt

    def permit(self, request_id, *, lease_seconds=30, wait_seconds=30, fallback="skip"):
        self._agree("io-permit", {"id": request_id, "lease": lease_seconds, "wait": wait_seconds, "fallback": fallback})
        participants = [f"rank{rank}" for rank in range(self.size)]
        return self._root(lambda: self.control.permit(request_id, participants=participants, arrived=participants,
                                                     lease_seconds=lease_seconds, wait_seconds=wait_seconds, fallback=fallback))

    def release(self, request_id, token, *, completed=True):
        self._agree("io-release", {"id": request_id, "token": token})
        outcomes = self.comm.gather(completed is True, root=0)
        receipt = self._root(lambda: self.control.release(request_id, token) if all(outcomes) else self.control.quarantine_permit(request_id, token))
        if receipt["state"] != "released":
            raise CollectiveControlError("Not every rank confirmed I/O completion; permit remains quarantined", receipt)
        return receipt

    def renew(self, request_id, token, seconds):
        self._agree("io-renew", {"id": request_id, "token": token, "seconds": seconds})
        return self._root(lambda: self.control.renew(request_id, token, seconds))


class TorchDistributedCommunicator:
    """Adapter for an already initialized torch.distributed process group.

    Set the CUDA device before using NCCL object collectives. A separate existing
    Gloo control group can avoid GPU object-collective overhead when available.
    This adapter does not initialize networking or create a process group.
    """
    def __init__(self, distributed, group=None):
        self.distributed = distributed
        self.group = group
        if not distributed.is_initialized():
            raise ValueError("Initialize the application's distributed process group first")
        # torch dst/src use global ranks; subgroup leaders need their global ID.
        self.root = distributed.get_global_rank(group, 0) if group is not None else 0

    def Get_rank(self):
        return self.distributed.get_rank(self.group)

    def Get_size(self):
        return self.distributed.get_world_size(self.group)

    def bcast(self, value, root=0):
        if root != 0:
            raise ValueError("Raven collective control uses rank zero")
        values = [value]
        self.distributed.broadcast_object_list(values, src=self.root, group=self.group)
        return values[0]

    def gather(self, value, root=0):
        if root != 0:
            raise ValueError("Raven collective control uses rank zero")
        values = [None] * self.Get_size() if self.Get_rank() == 0 else None
        self.distributed.gather_object(value, object_gather_list=values, dst=self.root, group=self.group)
        return values


class TorchMetrics:
    """Bounded optional CUDA event timing and allocator metrics, without synchronize.

    Supply the already imported torch module and the application's own device.
    At most max_pending timing pairs are retained. Unready full queues skip new
    measurements and report skipped samples. These are GPU stream intervals and
    allocator bytes, not device utilization or kernel instruction traces.
    """
    def __init__(self, torch, device, *, max_pending=2, clock=time.monotonic):
        self.torch, self.device, self.clock = torch, device, clock
        self.limit = integer(max_pending, "pending CUDA timing pairs", 1, 16)
        self.pending = []
        self.active = None
        self.skipped = 0
        self.enabled = str(device).startswith("cuda") and torch.cuda.is_available()

    def begin(self):
        if not self.enabled:
            return False
        if self.active is not None:
            raise ValueError("Finish the previous timing interval before beginning another")
        if len(self.pending) >= self.limit:
            self.skipped += 1
            return False
        with self.torch.cuda.device(self.device):
            start = self.torch.cuda.Event(enable_timing=True)
            end = self.torch.cuda.Event(enable_timing=True)
            start.record()
        self.active = (start, end)
        return True

    def end(self):
        if self.active is None:
            return False
        with self.torch.cuda.device(self.device):
            self.active[1].record()
        self.pending.append(self.active)
        self.active = None
        return True

    def sample(self):
        result = {"cuda_available": self.enabled, "skipped_timing_samples": self.skipped, "pending_timing_samples": len(self.pending)}
        if not self.enabled:
            return result
        durations, remaining = [], []
        for start, end in self.pending:
            if end.query():
                value = start.elapsed_time(end)
                number(value, "CUDA stream interval milliseconds", high=1e12)
                durations.append(value / 1000)
            else:
                remaining.append((start, end))
        self.pending = remaining
        result.update({"gpu_stream_seconds": durations, "gpu_allocated_bytes": self.torch.cuda.memory_allocated(self.device),
                       "gpu_reserved_bytes": self.torch.cuda.memory_reserved(self.device), "gpu_peak_allocated_bytes": self.torch.cuda.max_memory_allocated(self.device),
                       "pending_timing_samples": len(remaining)})
        return result
