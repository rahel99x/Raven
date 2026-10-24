"""Scientific producer -> verified trace -> indexed counter observations."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from raven.tracing import metrics_overlay as M
from raven.tracing.capture import PythonCapture, Recorder
from raven.tracing.protocol import read_trace
from raven.tracing.workspace_analysis import counter_lens
from raven.tracing.workspace_index import TraceIndex


@contextmanager
def registered(project):
    recorder = Recorder(project, job_id="21", attempt="a1", background_flush=False)
    capture = PythonCapture(recorder, legacy=True, scope="project")
    assert M.register(capture)
    try:
        yield capture
    finally:
        capture.stop()
        recorder.close()


def record(name="residual", value=0.25, **extra):
    return {"t": time.time(), "metrics": {name: value}, **extra}


def test_hook_queues_without_encoding_or_filesystem_work_and_has_same_thread_identity(
    tmp_path, monkeypatch
):
    with registered(tmp_path) as capture:
        original = capture.recorder.emit

        def fail(*args, **kwargs):
            pytest.fail("the scientific producer performed journal encoding/I/O")

        monkeypatch.setattr(capture.recorder, "emit", fail)
        client = SimpleNamespace(path=str(tmp_path / "metrics.jsonl"))
        try:
            assert (
                M.publish(
                    client,
                    record(phase="solve", step=3),
                    kind="residual",
                    unit="relative",
                )
                == "emitted"
            )
        finally:
            monkeypatch.setattr(capture.recorder, "emit", original)
        thread = capture.local.identity
        capture.recorder.flush()
        result = read_trace(capture.recorder.path)
        point = next(row for row in result["events"] if row["kind"] == "counter")
        assert point["thread"] == thread
        assert any(
            row["thread"] == thread and row["kind"] == "thread_start"
            for row in result["events"]
        )
        assert point["data"]["values"] == {"residual": 0.25}
        assert point["data"]["units"] == {"residual": "relative"}
        assert point["data"]["phase"] == "solve"
        assert point["data"]["step"] == 3
        assert result["identity"]["attempt"] == "a1"


def test_rate_policy_coalesces_explicitly_and_does_not_invent_points(tmp_path):
    with registered(tmp_path) as capture:
        client = SimpleNamespace(path=str(tmp_path / "metrics.jsonl"))
        assert M.publish(client, record(value=1), kind="gauge", unit="K") == "emitted"
        for value in range(20):
            assert (
                M.publish(client, record(value=value), kind="gauge", unit="K")
                == "coalesced"
            )
        time.sleep(0.06)
        assert M.publish(client, record(value=30), kind="gauge", unit="K") == "emitted"
        capture.recorder.flush()
        events = [
            row
            for row in read_trace(capture.recorder.path)["events"]
            if row["kind"] == "counter"
        ]
        assert [row["data"]["values"]["residual"] for row in events] == [1, 30]
        assert events[-1]["data"]["overlay"]["coalesced_since_previous"] == 20
        coverage = capture.recorder.coverage["scientific_metrics"]
        assert coverage["accepted"] == 22
        assert coverage["coalesced"] == 20
        assert coverage["emitted"] == 2


def test_ambiguous_or_no_longer_owned_capture_is_never_borrowed(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    client = SimpleNamespace(path=str(tmp_path / "metrics.jsonl"))
    with registered(tmp_path) as first, registered(other) as second:
        assert M.readiness()["available"] is False
        assert M.publish(client, record(), kind="gauge", unit="") == "ambiguous"
        assert first.recorder.coverage["scientific_metrics"]["emitted"] == 0
        assert second.recorder.coverage["scientific_metrics"]["emitted"] == 0
        second.stop()
        assert M.readiness()["available"] is True
        first.recorder._fork_disabled = True
        try:
            assert M.publish(client, record(), kind="gauge", unit="") == "inactive"
        finally:
            first.recorder._fork_disabled = False


def test_inventory_and_progress_names_are_bounded_across_reconfiguration(tmp_path):
    with registered(tmp_path) as capture:
        client = SimpleNamespace(path=str(tmp_path / "metrics.jsonl"))
        for index in range(62):
            assert (
                M.publish(client, record(name="m" + str(index)), kind="gauge", unit="")
                == "emitted"
            )
        progress = {
            "t": time.time(),
            "metrics": {},
            "progress": {"completed": 1, "total": 2, "unit": "steps"},
        }
        assert M.publish(client, progress, kind="progress", unit="") == "name-budget"
        for index in range(62, 64):
            assert (
                M.publish(client, record(name="m" + str(index)), kind="gauge", unit="")
                == "emitted"
            )
        client.path = str(tmp_path / "another-metrics.jsonl")
        assert (
            M.publish(client, record(name="m0"), kind="gauge", unit="") == "name-budget"
        )
        assert capture.recorder.coverage["scientific_metrics"]["dropped"] == 2


def _subprocess(project, program):
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    for key in (
        "RAVEN_RUN_DIR",
        "RAVEN_METRICS_FILE",
        "RAVEN_METRICS_OWNER_PID",
        "RANK",
        "OMPI_COMM_WORLD_RANK",
        "PMI_RANK",
        "SLURM_PROCID",
    ):
        environment.pop(key, None)
    child = subprocess.Popen(
        [sys.executable, "-c", program, str(project)],
        env=environment,
        cwd=project,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = child.communicate(timeout=20)
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGKILL)
        child.communicate()
        raise
    result = subprocess.CompletedProcess(child.args, child.returncode, stdout, stderr)
    assert result.returncode == 0, result.stderr
    return result


def test_actual_python_capture_microapi_to_index_counter_lens_end_to_end(tmp_path):
    script = tmp_path / "science.py"
    script.write_text(
        "import raven,threading,time\n"
        "@raven.measure('loss',kind='objective',unit='J')\n"
        "def energy(): return .125\n"
        "with raven.phase('solve'):\n"
        " assert energy()==.125\n"
        " assert raven.metric('residual',.5,kind='residual',step=1,unit='relative')==.5\n"
        " assert raven.progress(1,2,unit='iterations')==1\n"
        " for index in range(2):\n"
        "  thread=threading.Thread(target=lambda i=index:raven.metric('worker.'+str(i),i,unit='count'))\n"
        "  thread.start();thread.join()\n"
    )
    result = _subprocess(
        tmp_path,
        "import json,runpy,sys\nfrom pathlib import Path\n"
        "from raven import client\nfrom raven.tracing.capture import Recorder,PythonCapture\n"
        "root=Path(sys.argv[1]);client.configure(path=root/'metrics.jsonl',flush_interval=.05)\n"
        "recorder=Recorder(root,job_id='21',attempt='a1',journal_format='blocks')\n"
        "capture=PythonCapture(recorder,scope='project',roots=[root],legacy=sys.version_info<(3,12))\n"
        "try:\n capture.start();runpy.run_path(str(root/'science.py'),run_name='__main__');client._shutdown()\n"
        "finally:\n capture.stop();recorder.close()\n"
        "print(json.dumps({'manifest':str(recorder.path)}))\n",
    )
    manifest = json.loads(result.stdout)["manifest"]
    with TraceIndex(tmp_path / "derived.sqlite") as index:
        for _ in range(200):
            if index.refresh([manifest], expected={"job_id": "21", "attempt": "a1"})[
                "caught_up"
            ]:
                break
        query = index.query(limit=2000)
        lens = counter_lens(query)
    names = {row["name"] for row in lens["series"]}
    assert {
        "loss",
        "residual",
        "progress.completed",
        "progress.total",
        "progress.fraction",
        "worker.0",
        "worker.1",
    } <= names
    residual = next(row for row in lens["series"] if row["name"] == "residual")
    assert residual["unit"] == "relative"
    assert residual["points"][0]["value"] == "0.5"
    assert any(row["name"] == "solve" for row in lens["phases"])
    workers = [row for row in lens["series"] if row["name"].startswith("worker.")]
    assert len({row["thread"] for row in workers}) == 2
    assert all(
        row["clock_id"] == query["sources"][0]["clock"]["id"] for row in lens["series"]
    )
    assert query["identity"]["attempt"] == "a1"
    metadata = query["sources"][0]["coverage"]["scientific_metrics"]
    assert (
        metadata["emitted"] == 6
    )  # Progress contributes three values in one observation.
    assert len(lens["series"]) == 8
    assert metadata["status"] == "stopped"
    original = [
        json.loads(line)
        for line in (tmp_path / "metrics.jsonl").read_text().splitlines()
    ]
    assert any(row.get("metrics", {}).get("loss") == 0.125 for row in original)
    assert all(
        "mono_ns" not in row and "producer_stream_id" not in row for row in original
    )


def test_standalone_metrics_imports_no_tracing_or_optional_dependencies(tmp_path):
    result = _subprocess(
        tmp_path,
        "import json,sys\nfrom pathlib import Path\nfrom raven import client\n"
        "client.configure(path=Path(sys.argv[1])/'metrics.jsonl',flush_interval=.05)\n"
        "assert client.metric('loss',.25,unit='J')==.25\nclient._shutdown()\n"
        "assert not any(name.startswith('raven.tracing') for name in sys.modules)\n"
        "assert not any(name in sys.modules for name in ('torch','numpy','pyte'))\n"
        "print('standalone-unchanged')\n",
    )
    assert result.stdout.strip() == "standalone-unchanged"
    assert json.loads((tmp_path / "metrics.jsonl").read_text())["metrics"] == {
        "loss": 0.25
    }


def test_isolated_launch_runtime_connects_vendored_client_to_same_owned_recording(
    tmp_path,
):
    package = Path(__file__).resolve().parents[1] / "raven"
    (tmp_path / "raven.py").write_text((package / "client.py").read_text())
    result = _subprocess(
        tmp_path,
        "import json,sys,types\nfrom pathlib import Path\n"
        f"package=types.ModuleType('_raven_launch_runtime');package.__path__=[{str(package)!r}]\n"
        "package.__package__='_raven_launch_runtime';sys.modules[package.__name__]=package\n"
        "from _raven_launch_runtime.tracing.capture import Recorder,PythonCapture\n"
        "from _raven_launch_runtime.tracing import metrics_overlay\n"
        "root=Path(sys.argv[1]);recorder=Recorder(root,job_id='21',attempt='private-a1')\n"
        "capture=PythonCapture(recorder,scope='project',roots=[root],legacy=sys.version_info<(3,12));capture.start()\n"
        "import raven\nassert raven.__raven_vendored__\n"
        "raven.configure(path=root/'metrics.jsonl',flush_interval=.05)\n"
        "assert raven.metric('residual',.125,kind='residual',unit='relative')==.125\n"
        "raven._shutdown();capture.stop();recorder.close()\n"
        "assert not metrics_overlay.readiness()['available']\n"
        "print(json.dumps({'manifest':str(recorder.path)}))\n",
    )
    result = read_trace(json.loads(result.stdout)["manifest"])
    points = [row for row in result["events"] if row["kind"] == "counter"]
    assert len(points) == 1
    assert points[0]["data"]["values"] == {"residual": 0.125}
    assert result["identity"]["attempt"] == "private-a1"
    assert result["manifest"]["coverage"]["scientific_metrics"]["status"] == "stopped"


def test_failed_capture_start_does_not_register_or_replace_existing_profiler(tmp_path):
    old = sys.getprofile()
    profiler = lambda *args: None
    recorder = Recorder(tmp_path, job_id="21", attempt="a1", background_flush=False)
    capture = PythonCapture(recorder, legacy=True)
    sys.setprofile(profiler)
    try:
        with pytest.raises(ValueError, match="Existing Python profiler"):
            capture.start()
        assert sys.getprofile() is profiler
        assert capture not in M._BINDINGS
        assert "epoch" in M.readiness()["historical_jsonl"]
    finally:
        capture.stop()
        sys.setprofile(old)
        recorder.close()


def test_optional_hook_failures_preserve_science_metrics_and_capture_teardown(tmp_path):
    result = _subprocess(
        tmp_path,
        "import json,sys\nfrom pathlib import Path\nfrom raven import client\n"
        "from raven.tracing.capture import Recorder,PythonCapture\n"
        "from raven.tracing import metrics_overlay as overlay\n"
        "root=Path(sys.argv[1]);client.configure(path=root/'metrics.jsonl',flush_interval=.05)\n"
        "recorder=Recorder(root,job_id='21',attempt='a1');capture=PythonCapture(recorder,legacy=True)\n"
        "def fail(*args,**kwargs): raise RuntimeError('fixture optional overlay failure')\n"
        "capture.start();overlay.publish=fail\n"
        "assert client.metric('loss',.25,unit='J')==.25\nclient._shutdown()\n"
        "overlay.unregister=fail;capture.stop();assert sys.getprofile() is None\n"
        "assert not overlay.readiness()['available'];recorder.close()\n"
        "print('original-outcome-preserved')\n",
    )
    assert result.stdout.strip() == "original-outcome-preserved"
    row = json.loads((tmp_path / "metrics.jsonl").read_text())
    assert row["metrics"] == {"loss": 0.25}
    metadata = json.loads((tmp_path / "metrics.meta.json").read_text())
    assert any(
        "trace overlay failed" in reason
        for reason in metadata["rank_policy"]["coverage"]
    )


def test_registration_error_does_not_replace_scientific_outcome_or_existing_trace(
    tmp_path,
):
    result = _subprocess(
        tmp_path,
        "import sys\nfrom pathlib import Path\nfrom raven.tracing.capture import Recorder,PythonCapture\n"
        "from raven.tracing import metrics_overlay as overlay\n"
        "root=Path(sys.argv[1]);recorder=Recorder(root,job_id='21',attempt='a1')\n"
        "capture=PythonCapture(recorder,legacy=True)\n"
        "def fail(*args,**kwargs): raise RuntimeError('fixture registration failure')\n"
        "overlay.register=fail\ncapture.start()\n"
        "assert sys.getprofile() is capture.profile\n"
        "assert recorder.coverage['scientific_metrics']['status']=='unavailable'\n"
        "capture.stop();assert sys.getprofile() is None;recorder.close()\n"
        "print('capture-preserved')\n",
    )
    assert result.stdout.strip() == "capture-preserved"


@pytest.mark.skipif(not hasattr(os, "fork"), reason="POSIX owned fork qualification")
def test_fork_child_cannot_publish_into_parent_capture(tmp_path):
    result = _subprocess(
        tmp_path,
        "import json,os,sys\nfrom pathlib import Path\nfrom raven import client\n"
        "from raven.tracing.capture import Recorder,PythonCapture\n"
        "from raven.tracing import metrics_overlay as overlay\n"
        "root=Path(sys.argv[1]);client.configure(path=root/'metrics.jsonl',all_ranks=True,flush_interval=.05)\n"
        "recorder=Recorder(root,job_id='21',attempt='a1');capture=PythonCapture(recorder,legacy=True)\n"
        "capture.start();client.metric('parent.before',1)\n"
        "pid=os.fork()\n"
        "if pid==0:\n"
        " assert not overlay.readiness()['available']\n"
        " client.configure(path=root/'child-metrics.jsonl',all_ranks=True,flush_interval=.05)\n"
        " client.metric('child.only',2);client._shutdown();os._exit(0)\n"
        "_,status=os.waitpid(pid,0);assert os.waitstatus_to_exitcode(status)==0\n"
        "client.metric('parent.after',3);client._shutdown();capture.stop();recorder.close()\n"
        "print(json.dumps({'manifest':str(recorder.path)}))\n",
    )
    manifest = json.loads(result.stdout)["manifest"]
    points = [row for row in read_trace(manifest)["events"] if row["kind"] == "counter"]
    names = {name for row in points for name in row["data"]["values"]}
    assert names == {"parent.before", "parent.after"}
    assert all(
        row["data"]["source_pid"] == read_trace(manifest)["identity"]["pid"]
        for row in points
    )
    child_streams = list(tmp_path.glob("child-metrics.rank-0-pid-*.jsonl"))
    assert len(child_streams) == 1
    assert json.loads(child_streams[0].read_text())["metrics"] == {"child.only": 2.0}
