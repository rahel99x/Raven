import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from tower.trace_protocol import discover_trace, read_trace, TraceReader
from tower.trace_native import HEADER


def launch(tmp_path, source, extra=()):
    script = tmp_path / "app.py"
    script.write_text(source)
    result = subprocess.run([sys.executable, "-m", "tower.trace_recorder", "run", "--project", str(tmp_path),
        "--job-id", "123", "--attempt", "submit/start", *extra, "--", str(script)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    paths = discover_trace(tmp_path, "123", "submit/start")
    assert len(paths) == 1
    reader = TraceReader(paths[0], retain=100000)
    snapshot = reader.refresh()
    while not snapshot["caught_up"] and snapshot["status"] != "INVALID":
        snapshot = reader.refresh()
    return snapshot, result


@pytest.mark.skipif(not hasattr(sys, "monitoring"), reason="CPython3.12 monitoring")
def test_real_python_exceptions_generators_async_and_threads(tmp_path):
    result, process = launch(tmp_path, '''
import threading, asyncio
def recursive(n):
    if n: return recursive(n - 1)
    return 3
def generator():
    try:
        yield 1
        yield 2
    finally:
        recursive(2)
def work():
    try: raise ValueError("expected")
    except ValueError: recursive(3)
    g=generator(); next(g); g.close()
async def task():
    await asyncio.sleep(0)
    work()
threads=[threading.Thread(target=work) for _ in range(3)]
for thread in threads: thread.start()
for thread in threads: thread.join()
asyncio.run(task())
''')
    assert result["status"] == "COMPLETE", result["warnings"]
    assert not any("Stack transition" in warning for warning in result["warnings"])
    script_events = [e for e in result["events"] if e["data"].get("file") == str(tmp_path / "app.py")]
    assert len({e["thread"] for e in script_events}) >= 4
    assert {"call", "return", "yield", "resume", "unwind"} <= {e["kind"] for e in script_events}
    assert all(not entry["frames"] for entry in result["stacks"])


def test_legacy_profile_is_explicitly_scoped(tmp_path):
    result, _ = launch(tmp_path, "def f(): return 1\nf()\n", extra=("--legacy-profile",))
    assert result["complete"]
    assert result["covered"]["adapter"] == "python-profile"
    assert "cannot distinguish" in result["covered"]["limitations"]


@pytest.mark.parametrize("adapter", ["profile"] + (["monitoring"] if hasattr(sys, "monitoring") else []))
def test_script_exception_still_finalizes_recorded_evidence(tmp_path, adapter):
    script = tmp_path / "bad.py"
    script.write_text("def fail(): raise RuntimeError('expected')\nfail()\n")
    options = ["--legacy-profile"] if adapter == "profile" else []
    result = subprocess.run([sys.executable, "-m", "tower.trace_recorder", "run", "--project", str(tmp_path),
        "--job-id", "bad", "--attempt", "attempt", *options, "--", str(script)], capture_output=True, text=True, timeout=20)
    assert result.returncode != 0 and "expected" in result.stderr
    trace = read_trace(discover_trace(tmp_path, "bad")[0])
    assert trace["manifest"]["state"] == "CLOSED"
    assert trace["covered"]["adapter"] == ("python-profile" if adapter == "profile" else "python-sys-monitoring")


@pytest.mark.parametrize("compiler,suffix", [("cc", ".c"), ("c++", ".cpp")])
def test_compiled_native_threads_and_fork_use_separate_streams(tmp_path, compiler, suffix):
    if not shutil.which(compiler):
        pytest.skip("C/C++ compiler unavailable")
    (tmp_path / "tower_trace.h").write_text(HEADER)
    source = tmp_path / ("native" + suffix)
    source.write_text('''
#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#include "tower_trace.h"
#include <pthread.h>
#include <sys/wait.h>
static void *work(void *arg) {
    (void)arg;
    tower_trace_emit("span_start", "{\\"span_id\\":\\"work\\",\\"name\\":\\"work\\"}");
    tower_trace_emit("span_end", "{\\"span_id\\":\\"work\\"}");
    return NULL;
}
int main(void) {
    pthread_t thread;
    pthread_create(&thread, NULL, work, NULL);
    pthread_join(thread, NULL);
    pid_t pid=fork();
    if (!pid) { work(NULL); _exit(0); }
    waitpid(pid, NULL, 0);
    work(NULL);
    return 0;
}
''')
    binary = tmp_path / "native"
    compile_result = subprocess.run([compiler, "-Wall", "-Wextra", "-Werror", "-pthread", str(source), "-o", str(binary)], capture_output=True, text=True)
    assert compile_result.returncode == 0, compile_result.stderr
    result = subprocess.run([sys.executable, "-m", "tower.trace_recorder", "native", "--project", str(tmp_path),
        "--job-id", "123", "--attempt", "attempt", "--", str(binary)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    paths = discover_trace(tmp_path, "123", "attempt")
    assert len(paths) == 3
    snapshots = [read_trace(path) for path in paths]
    assert all(s["complete"] for s in snapshots), [s["warnings"] for s in snapshots]
    assert len({s["identity"]["pid"] for s in snapshots}) == 2
    assert all(s["identity"]["process_start"] != "unknown" for s in snapshots)


def test_native_without_sdk_does_not_claim_capture(tmp_path):
    result = subprocess.run([sys.executable, "-m", "tower.trace_recorder", "native", "--project", str(tmp_path),
        "--", sys.executable, "-c", "print('ordinary stdout')"], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0 and result.stdout.strip() == "ordinary stdout"
    assert "no SDK events" in result.stderr


def test_sdk_dump_is_standalone_header(tmp_path):
    result = subprocess.run([sys.executable, "-m", "tower.trace_recorder", "native", "--sdk-header"], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0 and "tower_trace_emit" in result.stdout
    assert not (tmp_path / ".tower").exists()
