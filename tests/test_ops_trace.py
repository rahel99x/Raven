import shlex
import threading

import pytest

from tower.operations import Context
from tower.ops_trace import run, apply, SPECIFICATIONS
from tower.trace_recorder import Recorder


def context(**kwargs):
    return Context(selected="123", scope={"cluster": "local"}, **kwargs)


def test_capture_plan_does_not_run_script_and_quotes_paths(tmp_path):
    script = tmp_path / "bad $(command).py"
    script.write_text("raise RuntimeError('must not run')")
    report = run("trace-capture", {"project": str(tmp_path), "script": str(script),
        "attempt": "submit/start", "job_id": "123"}, context())
    assert shlex.split(report["rows"][1]) == report["data"]["argv"]
    assert not (tmp_path / ".tower").exists()
    assert "No command was executed. Copy this into the batch launch script." in report["rows"]


def test_trace_inspection_binds_job_attempt_cluster(tmp_path):
    with Recorder(tmp_path, job_id="123", attempt="a") as recorder:
        recorder.emit("call", {"function": "work"})
        recorder.emit("return", {"function": "work"})
    report = run("trace-inspect", {"path": str(recorder.path), "attempt": "a"}, context())
    assert report["data"]["complete"]
    for params, ctx in [({"attempt": "wrong"}, context()), ({"job_id": "999"}, context()),
                        ({}, Context(scope={"cluster": "other"}))]:
        with pytest.raises(ValueError, match="match"):
            run("trace-inspect", {"path": str(recorder.path), **params}, ctx)


def test_project_multistream_causal_analysis_joins_explicit_edges(tmp_path):
    with Recorder(tmp_path, job_id="123", attempt="a", background_flush=False) as first:
        with first.span("prepare", span_id="prepare") as origin:
            pass
    with Recorder(tmp_path, job_id="123", attempt="a", background_flush=False) as second:
        with second.span("consume", span_id="consume") as target:
            pass
        second.link(origin, target)
    result = run("causal-delay", {"path": str(tmp_path), "job_id": "123", "attempt": "a"}, context())
    assert result["data"]["analysis_status"] == "COMPLETE", result
    assert len(result["data"]["sources"]) == 2
    assert result["data"]["source_complete"]


def test_project_collective_auditor_matches_ranks(tmp_path):
    for rank in (0, 1):
        with Recorder(tmp_path, job_id="123", attempt="a", background_flush=False,
                      identity_extra={"pid": 1000 + rank}) as recorder:
            data = {"communicator": "world", "generation": "1", "sequence": 1, "operation": "allreduce",
                    "members": [0, 1], "rank": rank, "dtype": "float32", "count": 128}
            recorder.emit("collective", {**data, "phase": "enter"})
            recorder.emit("collective", {**data, "phase": "complete"})
    result = run("collective-audit", {"path": str(tmp_path), "job_id": "123", "attempt": "a"}, context())
    assert result["data"]["analysis_status"] == "MATCH", result


def test_multistream_requires_exact_attempt(tmp_path):
    with Recorder(tmp_path, job_id="123", attempt="a"):
        pass
    with pytest.raises(ValueError, match="exact attempt"):
        run("causal-delay", {"path": str(tmp_path)}, context())


def test_cancelled_and_remote_inspection_do_not_read(tmp_path):
    event = threading.Event()
    event.set()
    with pytest.raises(ValueError, match="cancelled"):
        run("trace-inspect", {"path": str(tmp_path / "missing")}, context(cancel=event))
    remote = type("Remote", (), {"remote": True})()
    with pytest.raises(ValueError, match="target host"):
        run("trace-inspect", {"path": str(tmp_path / "missing")}, context(files=remote))


def test_readonly_trace_features_cannot_apply():
    with pytest.raises(ValueError, match="read-only"):
        apply("causal-delay", {}, context())
    assert {s["key"] for s in SPECIFICATIONS} == {
        "trace-inspect", "trace-capture", "trace-snapshot", "causal-delay", "collective-audit"}


def test_mpi_capture_plan_binds_library_and_shared_generation(tmp_path):
    program, library = tmp_path / "app", tmp_path / "library.so"
    program.write_text("program placeholder")
    library.write_text("library placeholder")
    result = run("trace-capture", {"project": str(tmp_path), "script": str(program), "adapter": "mpi",
        "mpi_library": str(library), "mpi_generation": "world-1", "attempt": "submit/start"}, context())
    argv = result["data"]["argv"]
    assert argv[:3] == ["tower", "trace", "native"]
    assert "LD_PRELOAD=" + str(library) in argv
    assert "TOWER_MPI_COMM_GENERATION=world-1" in argv
