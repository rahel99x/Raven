"""Recorder guards exclude themselves while retaining application stdlib calls."""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import threading
import weakref
from pathlib import Path

import pytest


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("guarded", [False, True])
def test_all_scope_guards_preserve_exact_application_boundaries(
    tmp_path, legacy, guarded
):
    if not legacy and sys.version_info < (3, 12):
        pytest.skip("sys.monitoring requires CPython3.12+")
    path = tmp_path / "science.py"
    path.write_text(
        "def call(value): return (value*17+3)%101\n"
        "def work():\n"
        " total=0\n"
        " for index in range(2000): total+=call(index)\n"
        + (
            " assert not capture.local.busy\n"
            " with guard():\n"
            "  assert capture.local.busy\n"
            "  with guard(): assert capture.local.busy\n"
            "  assert capture.local.busy\n"
            " assert not capture.local.busy\n"
            " try:\n"
            "  with guard(): raise RuntimeError('owned guard exception')\n"
            " except RuntimeError: pass\n"
            " assert not capture.local.busy\n"
            " recorder.emit('note',{'name':'direct application observation'})\n"
            " with contextlib.nullcontext(): threading.current_thread()\n"
            if guarded
            else ""
        )
        + " return total\n"
    )
    program = (
        "import contextlib,json,sys,threading\nfrom pathlib import Path\n"
        "from raven.tracing.capture import Recorder,PythonCapture,_internal_callbacks\n"
        "from raven.tracing.protocol import read_trace\n"
        "root=Path(sys.argv[1]);path=root/'science.py'\n"
        "recorder=Recorder(root,journal_format='blocks')\n"
        f"capture=PythonCapture(recorder,legacy={legacy!r},scope='all')\n"
        "namespace={'guard':_internal_callbacks,'recorder':recorder,'capture':capture,'contextlib':contextlib,'threading':threading}\n"
        "exec(compile(path.read_text(),str(path),'exec'),namespace)\n"
        "capture.start()\n"
        "try: answer=namespace['work']()\n"
        "finally: capture.stop();recorder.close()\n"
        "value=read_trace(recorder.path)\n"
        "print(json.dumps({'answer':answer,'status':value['status'],'events':value['events'],'dropped':value['manifest']['dropped']}))\n"
    )
    environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    result = subprocess.run(
        [sys.executable, "-c", program, str(tmp_path)],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert value["status"] == "COMPLETE"
    assert value["answer"] == 100066
    assert value["dropped"] == 0
    events = value["events"]
    calls = [row for row in events if row["kind"] in {"call", "resume"}]
    returns = [row for row in events if row["kind"] in {"return", "yield", "unwind"}]
    assert sum(row["data"].get("function") == "call" for row in calls) == 2000
    assert sum(row["data"].get("function") == "call" for row in returns) == 2000
    stacks = {}
    for event in events:
        if event["kind"] not in {"call", "resume", "return", "yield", "unwind"}:
            continue
        frame = (event["data"].get("file"), event["data"].get("function"))
        stack = stacks.setdefault(event["thread"], [])
        if event["kind"] in {"call", "resume"}:
            stack.append(frame)
        else:
            assert stack and stack[-1] == frame, (event, stack[-4:])
            stack.pop()
    assert all(not stack for stack in stacks.values())
    names = {row["data"].get("function") for row in calls}
    if guarded:
        assert "current_thread" in names

        # Python 3.10 code objects have co_name but no co_qualname. Verify
        # exact code boundaries using file/line as well as the available name;
        # an unrelated __enter__ must never satisfy the stdlib-call assertion.
        def boundary(method):
            code = method.__code__
            return (
                code.co_filename,
                code.co_firstlineno,
                getattr(code, "co_qualname", code.co_name),
            )

        def recorded(row):
            data = row["data"]
            return (data.get("file"), data.get("line"), data.get("function"))

        observed_calls = [recorded(row) for row in calls]
        observed_returns = [recorded(row) for row in returns]
        for method in (
            contextlib.nullcontext.__enter__,
            contextlib.nullcontext.__exit__,
            threading.current_thread,
        ):
            assert observed_calls.count(boundary(method)) == 1
            assert observed_returns.count(boundary(method)) == 1
        forbidden = {
            boundary(method)
            for method in (
                contextlib._GeneratorContextManager.__enter__,
                contextlib._GeneratorContextManager.__exit__,
                contextlib.contextmanager(lambda: None),
                weakref.WeakSet.__iter__,
            )
        }
        assert not forbidden.intersection(observed_calls)
        assert not any(
            Path(row["data"]["file"]).is_relative_to(
                Path(__file__).resolve().parents[1] / "raven"
            )
            for row in calls
        )
        assert not any(
            name
            and (
                "GeneratorContextManager" in name
                or name == "contextmanager.<locals>.helper"
                or name == "WeakSet.__iter__"
            )
            for name in names
        )
    else:
        assert names == {"work", "call"}
        # Code dictionaries add two metadata records in monitoring+async mode.
        assert len(events) == 4007
