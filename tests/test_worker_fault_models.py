"""Real local worker lifecycle models and isolated process faults, not Slurm CI."""
import copy
import os
import signal
import sys
import time

from hypothesis import settings
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule
import pytest

from raven.raven_protocol import ProtocolError
from raven.raven_worker_client import WorkerClient


def config():
    return {"site_kind": "desktop", "desktop_explicit": True, "context": {},
            "resources": {"memory_mib": 512, "cpus": 1, "max_tasks": 1, "wall_seconds": 30}}


def until(client, predicate):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        client.poll()
        if predicate():
            return
        time.sleep(.005)
    pytest.fail(f"Local worker did not settle: {client.status}: {client.error}")


class CommittedWorkerMachine(RuleBasedStateMachine):
    def __init__(self):
        super().__init__()
        self.client = WorkerClient(config())
        self.acknowledged = False
        self.closed = False
        self.requests = []
        original = self.client._queue
        def capture(message):
            self.requests.append(copy.deepcopy(message))
            original(message)
        self.client._queue = capture
        self.client.start()
        try:
            until(self.client, lambda: self.client.ready)
            inputs = {"document": {"schema": "raven.statistics/v1", "design": "independent", "unit": "seed",
                      "control": [{"id": str(i), "value": i} for i in range(3)],
                      "treatment": [{"id": str(i + 3), "value": i + 3} for i in range(3)]}}
            self.task_id = self.client.submit("statistics", inputs, {"attempt": "owned-attempt"})
            self.original_request = copy.deepcopy(self.requests[-1])
            until(self.client, lambda: self.client.tasks[self.task_id]["status"] == "committed")
            self.receipt = copy.deepcopy(self.client.tasks[self.task_id]["receipt"])
            self.result = copy.deepcopy(self.client.result(self.task_id))
        except BaseException:
            self.client.close()
            raise

    @precondition(lambda self: not self.closed)
    @rule()
    def retry_after_lost_ack(self):
        sequence = self.client.sequence
        self.client._queue(copy.deepcopy(self.original_request))
        until(self.client, lambda: self.client.sequence > sequence)
        # Same process/completion/request receipt, not a second accepted effect.
        assert self.client.tasks[self.task_id]["receipt"] == self.receipt

    @precondition(lambda self: not self.closed)
    @rule()
    def acknowledge_or_repeat_ack(self):
        sequence = self.client.sequence
        assert self.client.ack(self.task_id)
        until(self.client, lambda: self.client.sequence > sequence)
        self.acknowledged = True

    @precondition(lambda self: not self.closed)
    @rule()
    def stale_generation_cannot_replace_commit(self):
        before = copy.deepcopy(self.client.tasks[self.task_id])
        with pytest.raises(ProtocolError):
            self.client._consume({"op": "cancelled", "session_id": self.client.session_id,
                                  "generation": self.client.generation - 1, "id": self.task_id,
                                  "seq": self.client.sequence + 1, "worker_id": self.client.worker_id})
        assert self.client.tasks[self.task_id] == before

    @rule()
    def disconnect_preserves_verified_result(self):
        self.client.close()
        self.closed = True

    @invariant()
    def commit_identity_and_retention_are_stable(self):
        entry = self.client.tasks[self.task_id]
        assert entry["receipt"] == self.receipt
        assert len(self.client.tasks) == 1
        if self.acknowledged:
            assert self.client.result_bytes == 0 and entry["result"] is None
        else:
            assert entry["result"] == self.result

    def teardown(self):
        self.client.close()


TestCommittedWorkerModel = CommittedWorkerMachine.TestCase
TestCommittedWorkerModel.settings = settings(max_examples=5, stateful_step_count=12, deadline=None)


@pytest.mark.parametrize("stop_first", [False, True])
def test_child_death_mid_frame_is_uncertain_without_replay(stop_first):
    program = "import os,time;os.write(1,b'\\x00\\x00\\x00\\x64{');time.sleep(30)"
    client = WorkerClient(config(), launch_argv=[sys.executable, "-c", program])
    try:
        client.start()
        until(client, lambda: bool(client.decoder.buffer))
        if stop_first:
            os.kill(client.process.pid, signal.SIGSTOP)
            assert client.poll() == []
            assert not client.ready
        os.kill(client.process.pid, signal.SIGKILL)
        until(client, lambda: client.status == "failed")
        assert "truncated" in client.error
        assert not client.ready and client.tasks == {}
    finally:
        client.close()
    assert client.process.poll() is not None
