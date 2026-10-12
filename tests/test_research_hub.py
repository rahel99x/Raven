"""The research worker cannot multiply reads or publish obsolete contexts."""
from __future__ import annotations

import threading
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from raven.model import Finished, Job
from raven.research import ResearchHub, clean


def context(hub, view="experiment", jid="1", *, job=None, snap=None):
    return {"view": view, "jid": jid, "job": job,
            "snap": snap or {}, "settings": dict(hub.settings), "generation": hub.generation}


def test_repeated_frames_and_job_changes_do_not_queue_more_readers():
    hub = ResearchHub({})
    entered, release = threading.Event(), threading.Event()
    calls = []

    def read(selected):
        calls.append(selected["jid"])
        entered.set()
        assert release.wait(2)
        return {"status": "ok", "selected": selected["jid"]}

    hub._read = read
    first, second = context(hub), context(hub, jid="2")
    try:
        hub.request(first)
        assert entered.wait(1)
        worker = hub.future
        for _ in range(100):
            hub.request(first, force=True)
            hub.request(second, force=True)
        assert calls == ["1"] and hub.future is worker
        release.set()
        worker.result(timeout=2)
        assert hub.current(second)["status"] == "loading"
        assert hub.request(second, wait=True)["selected"] == "2"
        assert calls == ["1", "2"]
    finally:
        release.set()
        hub.close()


def test_configuration_change_discards_inflight_old_generation():
    hub = ResearchHub({})
    entered, release = threading.Event(), threading.Event()

    def read(selected):
        entered.set()
        assert release.wait(2)
        return {"status": "ok", "path": selected["settings"].get("metrics_file")}

    hub._read = read
    old = context(hub)
    try:
        hub.request(old)
        assert entered.wait(1)
        hub.configure(metrics_file="new.jsonl")
        new = context(hub)
        release.set()
        hub.future.result(timeout=2)
        assert not hub.cache
        assert hub.request(new, wait=True)["path"] == "new.jsonl"
        assert hub.current(old)["status"] == "loading"
        assert len(hub.cache) == 1
    finally:
        release.set()
        hub.close()


def test_close_discards_running_result_and_prevents_new_submissions():
    hub = ResearchHub({})
    entered, release = threading.Event(), threading.Event()

    def read(selected):
        entered.set()
        assert release.wait(2)
        return {"status": "ok"}

    hub._read = read
    selected = context(hub)
    try:
        hub.request(selected)
        assert entered.wait(1)
        worker = hub.future
        hub.close()
        release.set()
        worker.result(timeout=2)
        assert not hub.cache
        assert hub.request(selected, wait=True, force=True)["status"] == "loading"
        assert hub.future is worker
    finally:
        release.set()
        hub.close()


def test_cache_bound_and_fresh_results_reuse():
    hub = ResearchHub({})
    calls = []
    hub._read = lambda selected: calls.append(selected["jid"]) or {"status": "ok", "selected": selected["jid"]}
    try:
        for i in range(40):
            selected = context(hub, jid=str(i))
            assert hub.request(selected, wait=True)["selected"] == str(i)
            assert len(hub.cache) <= 16
        selected = context(hub, jid="39")
        assert hub.request(selected, wait=True)["selected"] == "39"
        assert len(calls) == 40
        assert hub.request(selected, wait=True, force=True)["selected"] == "39"
        assert len(calls) == 41
    finally:
        hub.close()


@pytest.mark.parametrize("error", [OSError("missing output"), ValueError("malformed stream"), TypeError("wrong type"), RuntimeError("adapter failed")])
def test_reader_errors_are_published_and_do_not_disable_future_reads(error):
    hub = ResearchHub({})

    def read(selected):
        raise error

    hub._read = read
    selected = context(hub)
    try:
        result = hub.request(selected, wait=True)
        assert result["status"] == "error" and str(error) in result["summary"]
        hub._read = lambda selected: {"status": "ok"}
        assert hub.request(selected, wait=True, force=True)["status"] == "ok"
    finally:
        hub.close()


def test_selected_jobs_do_not_fall_back_to_an_unrelated_record_when_present():
    hub = ResearchHub({})
    running = Job(id="1", name="x", partition="p", state="RUNNING")
    finished = Finished(id="2", state="FAILED")
    app = SimpleNamespace(selected_id="1", research_job_id="2", research_view="evidence")
    try:
        selected = hub.context({"jobs": [running], "finished": [finished]}, app)
        assert selected["jid"] == "2" and selected["job"] is finished
    finally:
        hub.close()


def test_explicit_missing_selection_never_analyzes_a_different_job():
    hub = ResearchHub({})
    other = Job(id="1", name="x", partition="p", state="RUNNING")
    app = SimpleNamespace(selected_id="1", research_job_id="999", research_view="evidence")
    try:
        selected = hub.context({"jobs": [other], "finished": []}, app)
        assert selected["jid"] == "999" and selected["job"] is None
        assert hub.request(selected, wait=True)["status"] == "empty"
    finally:
        hub.close()


def test_evidence_reads_only_selected_job_paths_with_bounded_tail_requests():
    class Files:
        remote = False

        def __init__(self):
            self.requests = []

        def tail(self, path, count):
            self.requests.append((path, count))
            if path.endswith(".err"):
                return b"CUDA out of memory\n", 1_000_000
            return b"progress\n", 9

    files = Files()
    hub = ResearchHub({}, files)
    snap = {"details": {"1": {"StdOut": "chosen.out", "StdErr": "chosen.err"}, "2": {"StdOut": "unrelated.out"}},
            "events": [{"job": "2", "text": "disk quota exceeded"}]}
    selected = context(hub, "evidence", job=Finished(id="1", state="FAILED"), snap=snap)
    try:
        result = hub.request(selected, wait=True)
        assert files.requests == [("chosen.out", 131072), ("chosen.err", 131072)]
        assert result["job_id"] == "1"
        assert not any(h["key"] == "quota" for h in result["hypotheses"])
        assert any("bounded stderr tail" in s for s in result["limitations"])
    finally:
        hub.close()


def test_unavailable_evidence_paths_remain_explicit_without_directory_scans():
    class Files:
        remote = False

        def tail(self, path, count):
            raise OSError("output not yet present")

        def listdir(self, path):
            pytest.fail("evidence must not scan directories")

    hub = ResearchHub({}, Files())
    selected = context(hub, "evidence", job=Finished(id="1", state="COMPLETED"), snap={"details": {"1": {"StdErr": "chosen.err"}}})
    try:
        result = hub.request(selected, wait=True)
        assert "StdOut path not available" in result["limitations"]
        assert any("output not yet present" in s for s in result["limitations"])
        assert "outputs remain unverified" in result["summary"]
    finally:
        hub.close()


def test_real_log_tail_preserves_bytes_and_read_bound(tmp_path):
    raw = b"x" * 500_000 + "🚀\xff".encode("utf-8")
    path = tmp_path / "chosen log.err"
    path.write_bytes(raw)
    hub = ResearchHub({})
    try:
        tail, size = hub._tail(str(path), 8)
        assert size == len(raw) and tail == raw[-8:]
        assert hub._tail(str(path), 0) == (b"", len(raw))
    finally:
        hub.close()


@pytest.mark.parametrize("kind", ["fifo", "device", "symlink", "directory"])
def test_special_log_paths_are_rejected_without_blocking_the_worker(tmp_path, kind):
    path = tmp_path / "unsafe.err"
    if kind == "fifo":
        os.mkfifo(path)
    elif kind == "symlink":
        target = tmp_path / "real.err"
        target.write_text("hello")
        path.symlink_to(target)
    elif kind == "directory":
        path.mkdir()
    else:
        path = "/dev/null"
    # A child deadline makes this regression safe even if a future change
    # accidentally reintroduces a blocking FIFO open.
    source = """import sys
from raven.research import ResearchHub
hub = ResearchHub({})
try:
    hub._tail(sys.argv[1], 128)
except (OSError, ValueError):
    pass
else:
    raise SystemExit('special log path was accepted')
finally:
    hub.close()
"""
    result = subprocess.run([sys.executable, "-c", source, str(path)], capture_output=True, text=True, timeout=3)
    assert result.returncode == 0, result.stderr


def test_changed_log_tail_is_unverified_instead_of_interpreted(monkeypatch):
    from raven import artifacts
    hub = ResearchHub({})
    monkeypatch.setattr(artifacts, "read_local_tail", lambda path, max_bytes: (b"CUDA out of memory", 50, False))
    selected = context(hub, "evidence", job=Finished(id="1", state="COMPLETED"), snap={"details": {"1": {"StdErr": "changed.err"}}})
    try:
        result = hub.request(selected, wait=True)
        assert not any(h["key"] == "gpu_oom" for h in result["hypotheses"])
        assert any("changed during inspection" in s for s in result["limitations"])
    finally:
        hub.close()


def test_evidence_scheduler_details_run_only_in_background_on_explicit_view():
    threads = []

    class Slurm:
        def details(self, jid):
            threads.append((threading.current_thread().name, jid))
            return {"JobId": jid}

    hub = ResearchHub({}, demo=True, slurm=Slurm())
    selected = context(hub, job=Finished(id="1", state="FAILED"))
    try:
        hub.request(selected, wait=True)
        assert not threads
        selected["view"] = "evidence"
        assert hub.request(selected, wait=True)["job_id"] == "1"
        assert len(threads) == 1 and threads[0][0].startswith("raven-research")
    finally:
        hub.close()


def test_passport_comparisons_preserve_the_rendered_schema():
    hub = ResearchHub({})
    hub.passport_diff = [{"path": "/parameters/steps", "left": 10, "right": 20}]
    try:
        result = hub.request(context(hub, "passport"), wait=True)
        assert result["status"] == "ok" and result["differences"] == hub.passport_diff
        hub.passport_diff = []
        result = hub.request(context(hub, "passport"), wait=True, force=True)
        assert result["status"] == "ok" and result["differences"] == []
    finally:
        hub.close()


def test_reader_error_summaries_are_bounded():
    hub = ResearchHub({})

    def read(selected):
        raise ValueError("x" * 1_000_000)

    hub._read = read
    try:
        result = hub.request(context(hub), wait=True)
        assert result["status"] == "error" and len(result["summary"]) <= 4096
    finally:
        hub.close()


def render_result(view, result, *, ascii_=False):
    from raven.layout import Glyphs
    from raven.research_views import render

    class Hub:
        def context(self, snap, app):
            return {"job": None}

        def request(self, selected):
            return result

    app = SimpleNamespace(research_view=view, research=Hub(), research_scroll=0, research_rows=0)
    rows, _ = render(SimpleNamespace(g=Glyphs(ascii_)), {}, app, 80, 30)
    return "\n".join("".join(segment[0] for segment in row) for row in rows)


@pytest.mark.parametrize("ascii_", [False, True])
@pytest.mark.parametrize("values,times", [([-1e308, 1e308], [1, 2]), ([1, 2], [1, 1e308])])
def test_accepted_extreme_finite_metrics_cannot_crash_terminal_render(values, times, ascii_):
    result = {"status": "ok", "path": "selected.jsonl", "records": 2,
              "series": {"stress": [{"t": t, "value": v} for t, v in zip(times, values)]}}
    rendered = render_result("experiment", result, ascii_=ascii_)
    assert "stress" in rendered
    if ascii_:
        assert all(ord(c) < 128 for c in rendered)


def test_counter_evidence_is_inspectable_when_not_also_in_support():
    result = {"status": "warning", "summary": "Scheduler records disagree",
              "evidence": [{"id": "E2", "source": "scheduler", "location": "job 1 details state", "text": "Details state differs: FAILED"}],
              "hypotheses": [{"name": "Historical exception", "confidence": "weak", "support": [], "contradictions": ["E2"], "next_checks": []}]}
    rendered = render_result("evidence", result)
    assert "Details state differs: FAILED" in rendered


def test_job_token_only_expands_literal_documented_placeholder():
    hub = ResearchHub({"research": {"workdir": "/scratch/example"}})
    selected = context(hub, jid="123_4")
    try:
        assert hub._path("metrics-{job_id}.jsonl", selected) == "/scratch/example/metrics-123_4.jsonl"
        assert hub._path("metrics-{job_id.__class__}.jsonl", selected) == "/scratch/example/metrics-{job_id.__class__}.jsonl"
    finally:
        hub.close()


def test_clean_never_emits_terminal_controls_and_ascii_mode_is_ascii():
    value = "\x1b[31mname\x00\n\u202e🚀"
    cleaned = clean(value)
    assert all(c.isprintable() for c in cleaned)
    assert all(ord(c) < 128 for c in clean(value, ascii_=True))


@pytest.mark.parametrize("interval", [float("nan"), float("inf"), -1, 0, .5, True, "5"])
def test_invalid_poll_intervals_are_rejected_before_pool_creation(interval):
    with pytest.raises(ValueError, match="research.interval"):
        ResearchHub({"research": {"interval": interval}})
