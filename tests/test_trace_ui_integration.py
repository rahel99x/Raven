import threading

import pytest

from tower.trace_recorder import Recorder
from tower.trace_ui import _read


def context(tmp_path, **values):
    return dict(remote=False, root=str(tmp_path), job_id="123", submit="submit", start="start",
                cluster="local", run_id="", **values)


def test_real_worker_subscription_tracks_committed_stacks_incrementally(tmp_path):
    with Recorder(tmp_path, job_id="123", attempt="submit/start", background_flush=False) as recorder:
        recorder.emit("call", {"function": "solve", "file": "main.py", "line": 8, "frame": "solve"}, thread="thread")
        recorder.flush()
        first = _read(context(tmp_path), "", 0, "", None, threading.Event())
        assert first["verified"] and first["sources"] == [str(recorder.path)]
        assert first["snapshot"]["stacks"][0]["frames"][0]["function"] == "solve"
        recorder.emit("return", {"frame": "solve"}, thread="thread")
        recorder.flush()
        second = _read(context(tmp_path), "", 0, first["path"], first["reader"], threading.Event())
        assert second["reader"] is first["reader"]
        assert second["snapshot"]["read_seq"] == first["snapshot"]["read_seq"] + 1
        assert not second["snapshot"]["stacks"][0]["frames"]


def test_same_job_old_attempt_never_auto_attaches(tmp_path):
    with Recorder(tmp_path, job_id="123", attempt="older/attempt") as old:
        pass
    with Recorder(tmp_path, job_id="123", attempt="submit/start") as current:
        pass
    result = _read(context(tmp_path), "", 0, "", None, threading.Event())
    assert result["sources"] == [str(current.path)]
    explicit = _read(context(tmp_path), str(old.path), 0, "", None, threading.Event())
    assert not explicit["verified"]


def test_actual_project_run_id_binds_without_scheduler_timestamp(tmp_path):
    with Recorder(tmp_path, job_id="123", attempt="project-run", identity_extra={"run_id": "project-run"}) as recorder:
        pass
    ctx = context(tmp_path)
    ctx.update(submit="", start="", run_id="project-run")
    result = _read(ctx, "", 0, "", None, threading.Event())
    assert result["verified"] and result["path"] == str(recorder.path)


def test_cross_job_explicit_attachment_is_rejected(tmp_path):
    with Recorder(tmp_path, job_id="999", attempt="submit/start") as recorder:
        pass
    with pytest.raises(ValueError, match="different job"):
        _read(context(tmp_path), str(recorder.path), 0, "", None, threading.Event())
