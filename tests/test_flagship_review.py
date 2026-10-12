"""Independent regressions for cross-module flagship integration failures."""
from pathlib import Path
from types import SimpleNamespace
import os
import sys
import threading
import time

import pytest

from raven import flagship_commands, operations as O, ops_ui
from raven.platform_session import GovernedBackend
from raven.shared_collection import SharedQueryCache
from raven.slurm import Backend, CommandError
from test_flagship_platform import policy
from test_operations_sequences import operation_dashboard


@pytest.mark.parametrize("index,task", list(enumerate(flagship_commands.TASKS)))
def test_task_catalog_buttons_work_from_keyboard(operation_dashboard, index, task):
    app = operation_dashboard.app
    app.run_command("workbench all")
    operation_dashboard.draw()
    app.ops_state.update(focus="buttons", button=index)
    app.handle("enter")
    assert app.ops_state["catalog_task"] == task.key
    assert app.mode == "operations"
    assert all(spec["key"] in task.operations for spec in ops_ui._catalog_specs(app.ops_state))


def wrapper(tmp_path, backend=None):
    return GovernedBackend(backend or Backend(), policy(.5), SharedQueryCache(tmp_path / "cache"),
                           {"cluster": "test", "user": "test"})


def test_action_validation_collects_fresh_state_and_mutation_invalidates_cache(tmp_path):
    class Stateful:
        state = "PENDING"
        def run(self, argv, timeout):
            if "update" in argv:
                self.state = "HELD"
                return "updated", .001
            return self.state, .001
    inner = Stateful()
    wrapped = wrapper(tmp_path, inner)
    ctx = O.Context(slurm=SimpleNamespace(b=wrapped), cancel=threading.Event())
    argv = ["scontrol", "show", "job", "81"]
    assert wrapped.run(argv)[0] == "PENDING"
    inner.state = "RUNNING"
    assert wrapped.run(argv)[0] == "PENDING"  # ordinary sampler freshness contract
    assert O.command(ctx, argv) == "RUNNING"  # review must inspect current identity
    O.command(ctx, ["scontrol", "update", "JobId=81", "Hold=1"])
    assert wrapped.run(argv)[0] == "HELD"


def test_governed_command_preserves_live_cancellation(tmp_path, monkeypatch):
    executable = tmp_path / "squeue"
    marker = tmp_path / "entered"
    executable.write_text("#!" + sys.executable + "\nimport pathlib,time\n"
                          "pathlib.Path(" + repr(str(marker)) + ").write_text('entered')\ntime.sleep(10)\n")
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ.get("PATH", ""))
    wrapped = wrapper(tmp_path)
    stop, finished = threading.Event(), threading.Event()
    ctx = O.Context(slurm=SimpleNamespace(b=wrapped), cancel=stop)
    errors = []
    def run():
        try:
            O.command(ctx, ["squeue"], timeout=2)
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()
    thread = threading.Thread(target=run)
    thread.start()
    try:
        until = time.monotonic() + 2
        while not marker.exists() and time.monotonic() < until:
            assert not finished.wait(.01), errors
        assert marker.exists()
        stop.set()
        assert finished.wait(1), "The governed wrapper abandoned the caller's cancellation event"
        assert len(errors) == 1 and "cancel" in str(errors[0]).lower()
    finally:
        stop.set()
        thread.join(4)
        assert not thread.is_alive()


def test_governed_unknown_command_enforces_limit_before_process_finishes(tmp_path):
    marker = tmp_path / "should-not-complete"
    wrapped = wrapper(tmp_path)
    ctx = O.Context(slurm=SimpleNamespace(b=wrapped), cancel=threading.Event())
    program = "import pathlib,time; print('x'*100000,flush=True); time.sleep(.2); pathlib.Path(" + repr(str(marker)) + ").touch()"
    with pytest.raises(CommandError, match="output exceeded"):
        O.command(ctx, [sys.executable, "-c", program], timeout=2, limit=64)
    assert not marker.exists(), "An output overflow must stop collection before subsequent child effects"


def test_governed_large_queue_is_not_limited_by_one_evidence_string(tmp_path):
    class LargeQueue:
        calls = 0
        def run(self, argv, timeout):
            self.calls += 1
            return "a" * 70000, .001
    inner = LargeQueue()
    wrapped = wrapper(tmp_path, inner)
    assert len(wrapped.run(["squeue"])[0]) == 70000
    assert len(wrapped.run(["squeue"])[0]) == 70000
    assert inner.calls == 1
