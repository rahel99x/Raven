"""Owned subprocess integration; the fake perf tool is not hardware qualification."""

import errno
import json
import os
import resource
import sys
import time
from pathlib import Path

import pytest

from raven.tracing import perf_capture as C
from raven.tracing.protocol import read_json, read_trace


def app(project, *, seconds=0.08, exit_code=0, body=""):
    counter = project / "application-count"
    script = project / "science.py"
    script.write_text(
        "from pathlib import Path\nimport os,time,sys,resource\n"
        f"p=Path({str(counter)!r}); p.write_text(p.read_text()+'x' if p.exists() else 'x')\n"
        f"Path({str(project / 'application-pid')!r}).write_text(str(os.getpid()))\n"
        + body
        + f"\ntime.sleep({seconds!r}); raise SystemExit({exit_code})\n"
    )
    return [sys.executable, str(script)]


def fake_perf(project, monkeypatch, *, mode="success"):
    executable = project / "fake-perf"
    log = project / "perf-processes.jsonl"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json,os,signal,sys,time\nfrom pathlib import Path\n"
        f"log=Path({str(log)!r}); mode={mode!r}\n"
        "a=sys.argv[1:]; operation=a[0]\n"
        "def option(name): return a[a.index(name)+1]\n"
        "if operation=='record':\n"
        " target=int(option('--pid')); out=Path(option('--output'))\n"
        " with log.open('a') as f: f.write(json.dumps({'pid':os.getpid(),'target':target,'path':str(out)})+'\\n')\n"
        " real=out.name=='samples.data'\n"
        " if mode=='probe-failure' or (mode=='attach-failure' and real):\n"
        "  print('fixture: permission denied',file=sys.stderr); raise SystemExit(1)\n"
        " r,w=map(int,option('--control')[3:].split(','))\n"
        " command=os.read(r,64)\n"
        " if command != b'enable\\n': raise SystemExit(3)\n"
        " out.write_text(json.dumps({'pid':target,'stamp':time.monotonic_ns()}))\n"
        " signal.signal(signal.SIGINT,lambda *args: sys.exit(0))\n"
        " os.write(w,b'ack\\n')\n"
        " if mode=='quota' and real:\n"
        "  with out.open('ab',buffering=0) as f:\n"
        "   while True: f.write(b'x'*2048)\n"
        " while True: time.sleep(.005)\n"
        "elif operation=='script':\n"
        " source=Path(option('--input'))\n"
        " if source.name=='probe.data': raise SystemExit(0)\n"
        " data=json.loads(source.read_text()); pid=data['pid']; stamp=data['stamp']\n"
        " print(f'science {pid}/{pid} [001] {stamp//1000000000}.{stamp%1000000000:09d}: 10 cpu-clock:u:')\n"
        " print('  0000000000000010 solve (science)')\n"
        " print('')\n"
        "else: raise SystemExit(4)\n"
    )
    executable.chmod(0o700)
    monkeypatch.setattr(C, "_find_perf", lambda: str(executable))
    return log


def run(project, command, **kwargs):
    return C.sample_command(
        command, project=project, job_id="17", attempt="a1", max_bytes=1 << 20, **kwargs
    )


def test_actual_missing_tool_optional_executes_original_once_with_exit_status(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(C, "_find_perf", lambda: None)
    result = run(tmp_path, app(tmp_path, exit_code=7))
    assert (tmp_path / "application-count").read_text() == "x"
    assert result["application_exit"] == 7
    assert result["status"] == "unavailable"
    assert "not installed" in result["errors"][0]
    assert read_json(result["receipt"])["application_exit"] == 7
    assert Path(result["receipt"]).stat().st_mode & 0o777 == 0o600


def test_required_missing_tool_does_not_invoke_science(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "_find_perf", lambda: None)
    result = run(tmp_path, app(tmp_path), required=True)
    assert not (tmp_path / "application-count").exists()
    assert result["application_status"] == "not-started"
    assert result["application_exit"] == 2


def test_optional_absent_tool_preserves_stdout_environment_and_signal_status(
    tmp_path, monkeypatch, capfd
):
    monkeypatch.setattr(C, "_find_perf", lambda: None)
    monkeypatch.setenv("RAVEN_SCIENCE_ENV_FIXTURE", "preserved")
    result = run(
        tmp_path,
        [
            sys.executable,
            "-c",
            "import os,signal; print(os.environ['RAVEN_SCIENCE_ENV_FIXTURE'],flush=True); os.kill(os.getpid(),signal.SIGTERM)",
        ],
    )
    assert capfd.readouterr().out == "preserved\n"
    assert result["application_returncode"] == -15
    assert result["application_exit"] == 143


def test_optional_missing_executable_has_durable_launch_failure_not_retry(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(C, "_find_perf", lambda: None)
    result = run(tmp_path, [str(tmp_path / "nonexistent-science")])
    assert result["application_status"] == "launch-failed"
    assert result["application_exit"] == 127
    assert result["application_launches"] == 1
    assert read_json(result["receipt"])["application_exit"] == 127


def test_optional_capture_storage_failure_does_not_block_science(tmp_path, monkeypatch):
    from raven import reporting

    def full(*args, **kwargs):
        raise OSError(errno.ENOSPC, "fixture disk full")

    monkeypatch.setattr(reporting, "_atomic_json", full)
    result = run(tmp_path, app(tmp_path, exit_code=7))
    assert (tmp_path / "application-count").read_text() == "x"
    assert result["application_exit"] == 7
    assert result["receipt"] is None
    assert any("storage unavailable" in row.lower() for row in result["errors"])


def test_gated_child_is_only_target_and_sample_import_preserves_reported_pid(
    tmp_path, monkeypatch
):
    log = fake_perf(tmp_path, monkeypatch)
    result = run(tmp_path, app(tmp_path))
    pid = int((tmp_path / "application-pid").read_text())
    records = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(records) == 2  # The first target is an owned preflight, never science.
    assert records[-1]["target"] == pid == result["application_pid"]
    assert records[0]["target"] != pid
    assert result["application_exit"] == 0
    assert result["capture_prerequisites_verified"] is True
    assert result["status"] == "complete", result
    assert result["recording"]["samples"] == 1
    trace = read_trace(result["recording"]["path"])
    sample = next(row["data"] for row in trace["events"] if row["kind"] == "cpu_sample")
    assert sample["source_pid"] == sample["source_tid"] == pid
    assert sample["weight_unit"] == "ns"
    assert (tmp_path / "application-count").read_text() == "x"
    for record in records:
        with pytest.raises(ProcessLookupError):
            os.kill(record["pid"], 0)


@pytest.mark.parametrize("mode", ["probe-failure", "attach-failure"])
@pytest.mark.parametrize("required", [False, True])
def test_failure_before_release_never_retries_or_duplicates_application(
    tmp_path, monkeypatch, mode, required
):
    fake_perf(tmp_path, monkeypatch, mode=mode)
    result = run(tmp_path, app(tmp_path), required=required)
    assert result["status"] == "unavailable"
    if required:
        assert not (tmp_path / "application-count").exists()
        assert result["application_exit"] == 2
    else:
        assert (tmp_path / "application-count").read_text() == "x"
        assert result["application_exit"] == 0
    assert any("permission" in row for row in result["errors"])


def test_duration_stops_collector_and_science_finishes_normally(tmp_path, monkeypatch):
    fake_perf(tmp_path, monkeypatch)
    before = time.monotonic()
    result = run(tmp_path, app(tmp_path, seconds=0.4), capture_seconds=0.1)
    assert time.monotonic() - before >= 0.4
    assert result["stop_reason"] == "capture-duration"
    assert result["status"] == "partial"
    assert result["application_exit"] == 0
    assert result["recording"]["status"] == "PARTIAL"


def test_bounded_window_is_viewable_while_application_is_still_running(
    tmp_path, monkeypatch
):
    fake_perf(tmp_path, monkeypatch)
    body = (
        "import json\n"
        f"root=Path({str(tmp_path)!r}); deadline=time.monotonic()+3\n"
        "while time.monotonic()<deadline:\n"
        " receipts=list((root/'.raven'/'perf-captures').glob('*/receipt.json'))\n"
        " for path in receipts:\n"
        "  value=json.loads(path.read_text())\n"
        "  if value.get('recording'):\n"
        "   assert value['application_status']=='running'\n"
        "   assert Path(value['recording']['manifest']).is_file()\n"
        "   (root/'live-window-observed').write_text('yes')\n"
        " if (root/'live-window-observed').exists(): break\n"
        " time.sleep(.01)\n"
        "else: raise AssertionError('sample window was withheld until completion')\n"
    )
    result = run(tmp_path, app(tmp_path, seconds=0.01, body=body), capture_seconds=0.1)
    assert result["application_exit"] == 0, result
    assert (tmp_path / "live-window-observed").read_text() == "yes"
    assert result["stop_reason"] == "capture-duration"


def test_late_receipt_write_failure_preserves_original_exit_status(
    tmp_path, monkeypatch
):
    from raven import reporting

    fake_perf(tmp_path, monkeypatch)
    original = reporting._atomic_json

    def late_full(path, value, **kwargs):
        if (
            value.get("schema") == "raven.perf-capture/v1"
            and value.get("application_status") == "completed"
        ):
            raise OSError(errno.ENOSPC, "fixture late disk full")
        return original(path, value, **kwargs)

    monkeypatch.setattr(reporting, "_atomic_json", late_full)
    result = run(tmp_path, app(tmp_path, exit_code=7))
    assert result["application_exit"] == 7
    assert "late disk full" in result["receipt_write_error"]


def test_viewer_interrupt_stops_collector_without_terminating_science(
    tmp_path, monkeypatch
):
    from raven.tracing import perf_capture_process as P

    fake_perf(tmp_path, monkeypatch)
    original, interrupted = P.Collector.drain, False

    def interrupt(collector):
        nonlocal interrupted
        original(collector)
        if (tmp_path / "application-count").exists() and not interrupted:
            interrupted = True
            raise KeyboardInterrupt()

    monkeypatch.setattr(P.Collector, "drain", interrupt)
    command = app(
        tmp_path,
        seconds=0.2,
        body=f"time.sleep(.15)\nPath({str(tmp_path / 'science-finished')!r}).write_text('yes')\n",
    )
    with pytest.raises(KeyboardInterrupt):
        run(tmp_path, command)
    receipt = read_json(
        next((tmp_path / ".raven" / "perf-captures").glob("*/receipt.json"))
    )
    assert receipt["application_status"] == "running-after-viewer-interruption"
    assert receipt["status"] == "partial"
    # Reap the child left running deliberately; it must reach its own conclusion.
    _, status = os.waitpid(receipt["application_pid"], 0)
    assert os.waitstatus_to_exitcode(status) == 0
    assert (tmp_path / "science-finished").read_text() == "yes"


def test_collector_file_quota_does_not_limit_or_kill_application(tmp_path, monkeypatch):
    fake_perf(tmp_path, monkeypatch, mode="quota")
    old_limit = resource.getrlimit(resource.RLIMIT_FSIZE)
    command = app(
        tmp_path,
        seconds=0.2,
        body=f"Path({str(tmp_path / 'science-limit')!r}).write_text(str(resource.getrlimit(resource.RLIMIT_FSIZE)))\n"
        f"Path({str(tmp_path / 'science-output')!r}).write_bytes(b'x'*(256<<10))\n",
    )
    result = C.sample_command(
        command, project=tmp_path, job_id="17", attempt="a1", max_bytes=4096
    )
    assert (tmp_path / "science-limit").read_text() == str(old_limit)
    assert (tmp_path / "science-output").stat().st_size == 256 << 10
    assert (Path(result["receipt"]).parent / "samples.data").stat().st_size <= 4096
    assert result["application_exit"] == 0
    assert result["status"] == "partial"
    assert result["stop_reason"] in {"raw-byte-quota", "collector-exited"}


def test_target_is_not_reaped_while_collector_may_open_pid(tmp_path, monkeypatch):
    from raven.tracing import perf_capture_process as P

    fake_perf(tmp_path, monkeypatch)
    target, gate = P.gated_application(
        [sys.executable, "-c", "raise AssertionError('must not execute')"]
    )
    os.close(gate)
    deadline = time.monotonic() + 3
    while os.waitid(os.P_PID, target.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is None:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    collector = P.Collector(
        C._find_perf(),
        target,
        tmp_path / "target.data",
        frequency=99,
        max_bytes=4096,
        seconds=0.1,
    )
    try:
        assert not collector.ready(target)
        assert target.returncode is None
        assert (
            os.waitid(os.P_PID, target.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT).si_pid
            == target.pid
        )
    finally:
        collector.stop()
        assert target.wait(timeout=3) == 125


@pytest.mark.parametrize(
    "field,value",
    [
        ("frequency", True),
        ("capture_seconds", float("nan")),
        ("max_bytes", 1000),
        ("attempt", "bad\x1b"),
    ],
)
def test_invalid_capture_request_has_no_application_side_effect(tmp_path, field, value):
    args = {"project": tmp_path, "job_id": "17", "attempt": "a1"}
    args[field] = value
    with pytest.raises(ValueError):
        C.sample_command(app(tmp_path), **args)
    assert not (tmp_path / "application-count").exists()
