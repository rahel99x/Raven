"""Lazy worker publication, exact identities, and resource evidence semantics."""
from __future__ import annotations

import copy
import json
import math
from threading import Event, current_thread
from types import SimpleNamespace

import pytest

from raven import job_panels as J, layout as L, quick_advisor as Q
from raven.config import Config
from raven.controller import App
from raven.model import Finished, Job, Live, Step, Store
from raven.remote import LocalFiles
from raven.research import ResearchHub
from raven.views import Views

GIB = 1024 ** 3


@pytest.fixture
def dashboard():
    cfg, store = Config(), Store(persist=False)
    store.jobs = [Job("900", "exact-workload", "cpu", "RUNNING", cpus=8,
                      mem_req="8Gn", elapsed="00:10:00", limit="01:00:00"),
                  Job("901", "different-job", "cpu", "RUNNING", cpus=2)]
    app = App(store, None, None, cfg, "test", interactive=True)
    views = Views(L.Glyphs(False), cfg, files=LocalFiles())
    app.views_ref, app.logs.files = views, views.files
    app.selected_id = "900"
    release = []
    result = SimpleNamespace(app=app, store=store, views=views, release=release)
    result.render = lambda width=70: J.render(views, store.snapshot(), app, app.job_record(app.selected_id), width, 40)
    yield result
    for event in release:
        event.set()
    if app.research:
        app.research.close()


def ready(dashboard):
    future = dashboard.app.research.future
    future.result(timeout=3)
    dashboard.app.research.poll_task()
    state = Q.initialize(dashboard.app)
    assert state["status"] == "ok", state
    return state["result"]


def evidence(job=None, *, series=None, live=None, history=None, details=None, **extra):
    return dict(job=job or Job("900", "workload", "cpu", "RUNNING", cpus=8,
                               nodes=1, mem_req="8Gn", elapsed="00:10:00", limit="01:00:00"),
                series=series or [], live=live, history=history or [], details=details or {},
                steps=[], trace=[], gpu=[], metadata={}, captured_at=1000, **extra)


def section(result, name):
    return next(item for item in result["sections"] if item["name"] == name)


def text(result):
    return json.dumps(result)


def rates(values, *, rss=GIB, spacing=30):
    return [dict(t=index * spacing + 1, k="live", cpu=value, eff=value, rss=rss)
            for index, value in enumerate(values)]


def test_render_restore_and_all_other_modes_never_compute(dashboard, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Implicit Quick Advisor computation")
    monkeypatch.setattr(Q, "capture", forbidden)
    monkeypatch.setattr(Q, "analyze", forbidden)
    J.restore(dashboard.app, {"mode": "quick", "quick": {"status": "ok", "job": "900"}})
    for _ in range(12):
        rows, hits = dashboard.render()
        dashboard.app.tick()
        assert "Analyze this job" in L.to_text(rows, 70)
        assert Q.initialize(dashboard.app)["status"] == "idle"
    assert dashboard.app.research is None
    assert J.save(dashboard.app)["mode"] == "quick"
    assert "quick" not in J.save(dashboard.app)


def test_explicit_button_starts_one_shared_worker_and_publishes_only_on_ui_thread(dashboard, monkeypatch):
    entered, release = Event(), Event()
    dashboard.release.append(release)
    original = Q.capture
    calls = []
    ui_thread = current_thread()
    def held(store, jid, event):
        assert current_thread() is not ui_thread
        calls.append((jid, current_thread().name))
        entered.set()
        assert release.wait(3)
        return original(store, jid, event)
    monkeypatch.setattr(Q, "capture", held)
    assert J.run_command(dashboard.app, ["jobpanel", "quick"])
    assert entered.wait(1)
    state, hub = Q.initialize(dashboard.app), dashboard.app.research
    future = hub.future
    for _ in range(20):
        rows, hits = dashboard.render()
        assert "Reading the job's recorded evidence" in L.to_text(rows, 70)
        assert state["status"] == "loading" and state["result"] is None
        assert hub.future is future
        assert any(value[0] == ("quick_cancel", "900") for _, kind, value in hits if kind == "job_panel_action")
    release.set()
    future.result(timeout=3)
    assert state["status"] == "loading"  # Worker never mutates UI state.
    hub.poll_task()
    assert state["status"] == "ok" and state["result"]["job"] == "900"
    assert len(calls) == 1 and calls[0][0] == "900"
    assert calls[0][1].startswith("raven-research")


@pytest.mark.parametrize("transition", ["job", "off", "tab", "attempt", "accounting"])
def test_stale_worker_result_is_discarded(dashboard, monkeypatch, transition):
    entered, release = Event(), Event()
    dashboard.release.append(release)
    original = Q.capture
    def held(store, jid, event):
        entered.set()
        assert release.wait(3)
        return original(store, jid, event)
    monkeypatch.setattr(Q, "capture", held)
    J.run_command(dashboard.app, ["jobpanel", "quick"])
    assert entered.wait(1)
    future = dashboard.app.research.future
    if transition == "job":
        dashboard.app.selected_id = "901"
    elif transition == "off":
        J.run_command(dashboard.app, ["jobpanel", "off"])
    elif transition == "tab":
        dashboard.app.tab = "analytics"
    elif transition == "attempt":
        dashboard.store.jobs[0].start = "2026-10-08T10:00:00"
    else:
        dashboard.store.finished = [Finished("900", "exact-workload", "COMPLETED", elapsed="00:10:00", cpus=8)]
        dashboard.store.jobs = dashboard.store.jobs[1:]
    J.tick(dashboard.app)
    release.set()
    future.result(timeout=3)
    dashboard.app.research.poll_task()
    state = Q.initialize(dashboard.app)
    assert state["status"] == "idle" and state["result"] is None


def test_refresh_uses_new_data_only_after_explicit_selection(dashboard):
    dashboard.store.series["900"].extend(rates([.2] * 12))
    J.run_command(dashboard.app, ["jobpanel", "quick"])
    before = copy.deepcopy(ready(dashboard))
    dashboard.store.series["900"].append(dict(t=1000, k="live", cpu=1.0, rss=7 * GIB))
    for _ in range(5):
        dashboard.render()
        J.tick(dashboard.app)
    assert Q.initialize(dashboard.app)["result"] == before
    assert J._content_action(dashboard.app, ("quick_refresh", "900"))
    after = ready(dashboard)
    assert after["sample_count"] == before["sample_count"] + 1
    assert "7.0 GB" in text(after)
    assert not J._content_action(dashboard.app, ("quick_refresh", "901"))


def test_busy_worker_keeps_only_one_cancelable_ui_request(dashboard):
    hub = dashboard.app.research = ResearchHub(dashboard.app.cfg, dashboard.views.files)
    entered, release = Event(), Event()
    dashboard.release.append(release)
    def busy():
        entered.set()
        assert release.wait(3)
        return "done"
    assert hub.start_task(busy, lambda value: None)
    assert entered.wait(1)
    first = hub.future
    for _ in range(50):
        J.run_command(dashboard.app, ["jobpanel", "quick"])
        J.tick(dashboard.app)
        assert hub.future is first and Q.initialize(dashboard.app)["status"] == "queued"
    release.set()
    first.result(timeout=3)
    hub.poll_task()
    J.tick(dashboard.app)
    assert hub.future is not first
    ready(dashboard)


def test_cancel_does_not_publish_error_or_start_another_task(dashboard, monkeypatch):
    entered, release = Event(), Event()
    dashboard.release.append(release)
    def held(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        raise RuntimeError("Canceled result must remain detached")
    monkeypatch.setattr(Q, "capture", held)
    J.run_command(dashboard.app, ["jobpanel", "quick"])
    assert entered.wait(1)
    future = dashboard.app.research.future
    assert J._content_action(dashboard.app, ("quick_cancel", "900"))
    release.set()
    with pytest.raises(RuntimeError):
        future.result(timeout=3)
    dashboard.app.research.poll_task()
    for _ in range(8):
        J.tick(dashboard.app)
        dashboard.render()
    assert Q.initialize(dashboard.app)["status"] == "idle"
    assert dashboard.app.research.future is future


def test_background_error_has_retry_action(dashboard, monkeypatch):
    def failed(*args, **kwargs):
        raise OSError("state file unavailable")
    monkeypatch.setattr(Q, "capture", failed)
    J.run_command(dashboard.app, ["jobpanel", "quick"])
    with pytest.raises(OSError):
        dashboard.app.research.future.result(timeout=3)
    dashboard.app.research.poll_task()
    rows, hits = dashboard.render()
    assert "Analysis unavailable: state file unavailable" in L.to_text(rows, 70)
    assert any(value[0] == ("quick_refresh", "900") for _, kind, value in hits if kind == "job_panel_action")


@pytest.mark.parametrize("ascii_,width", [(False, 70), (True, 70), (False, 12), (True, 1), (True, 0)])
def test_idle_loading_and_ready_rows_can_fit_narrow_and_ascii(dashboard, ascii_, width):
    dashboard.views.set_ascii(ascii_)
    J.restore(dashboard.app, {"mode": "quick"})
    for status in ("idle", "loading", "ok", "error"):
        state = Q.initialize(dashboard.app)
        state.update(status=status, job="900", result=Q.analyze(evidence()) if status == "ok" else None)
        rows, hits = dashboard.render(width)
        clipped = [L.clip_row(row, width) for row in rows]
        rendered = L.to_text(clipped, width)
        assert rendered.isascii() if ascii_ else True
        assert all(0 <= value[1] < value[2] <= width for _, kind, value in hits if kind == "job_panel_action")


def test_no_data_is_a_valid_report_not_zero_use():
    result = Q.analyze(evidence())
    assert section(result, "CPU")["status"] == "unknown"
    assert section(result, "Memory")["status"] == "unknown"
    assert result["sample_count"] == 0
    assert "missing sample does not mean idle CPU" in text(result)
    assert "CPU request may be larger" not in text(result)


def test_many_idle_samples_support_a_burst_preserving_cpu_test():
    result = Q.analyze(evidence(series=rates([.2] * 16), live=Live(avg=.2)))
    cpu = section(result, "CPU")
    assert "Benchmark 3 CPUs" in cpu["action"]
    assert "validate throughput" in cpu["action"]
    assert "P90 20%" in " ".join(cpu["facts"])


def test_low_mean_with_busy_phases_does_not_recommend_core_reduction():
    result = Q.analyze(evidence(series=rates([.1] * 12 + [.95] * 4), live=Live(avg=.2)))
    cpu = section(result, "CPU")
    assert "bursty" in cpu["title"]
    assert "Benchmark" not in cpu["action"]


def test_a_rare_busy_peak_is_preserved_even_when_p90_is_low():
    result = Q.analyze(evidence(series=rates([.1] * 99 + [.95]), live=Live(avg=.2)))
    cpu = section(result, "CPU")
    assert "P90 10%" in " ".join(cpu["facts"])
    assert "bursty" in cpu["title"] and "Benchmark" not in cpu["action"]


def test_one_low_snapshot_does_not_support_core_reduction():
    cpu = section(Q.analyze(evidence(series=rates([.01]), live=Live(avg=.01))), "CPU")
    assert "Benchmark" not in cpu["action"] and cpu["status"] == "unknown"


def test_invalid_cpu_scope_above_one_hundred_percent_is_flagged():
    cpu = section(Q.analyze(evidence(series=rates([1.5] * 12), live=Live(avg=1.5))), "CPU")
    assert "scope needs review" in cpu["title"]
    assert "invalid" in cpu["action"]


@pytest.mark.parametrize("memory,nodes", [("8Gn", 1), ("8Gn", 4), ("2Gc", 4)])
def test_task_rss_never_establishes_aggregate_free_headroom(memory, nodes):
    job = Job("900", "workload", "cpu", "RUNNING", cpus=8, nodes=nodes,
              mem_req=memory, elapsed="00:10:00", limit="01:00:00")
    result = Q.analyze(evidence(job, series=rates([.5] * 12, rss=GIB)))
    memory_section = section(result, "Memory")
    assert "not total job memory" in " ".join(memory_section["facts"])
    assert "headroom" in memory_section["title"]
    assert "Keep the memory request" in memory_section["action"]
    assert "--mem=" not in text(result)
    assert "memory flag is withheld" in text(result)


def test_per_node_pressure_uses_per_node_request_not_aggregate_allocation():
    job = Job("900", "workload", "cpu", "RUNNING", cpus=32, nodes=4, mem_req="8Gn")
    result = Q.analyze(evidence(job, series=rates([.5] * 12, rss=7 * GIB)))
    memory = section(result, "Memory")
    assert memory["status"] == "risk"
    assert "88% of the 8.0 GB node allocation" in " ".join(memory["facts"])
    assert "32.0 GB over 4 node(s)" in " ".join(memory["facts"])


def test_multinode_finished_accounting_total_memory_has_no_task_fraction():
    job = Finished("900", "workload", "COMPLETED", elapsed="00:10:00", cpus=32,
                   nodes=4, req_mem=32 * GIB, rss=7 * GIB, cpu_time=1000)
    facts = " ".join(section(Q.analyze(evidence(job)), "Memory")["facts"])
    assert "Per-node allocation is unknown" in facts
    assert "% of" not in facts


@pytest.mark.parametrize("rss", [0, GIB])
def test_out_of_memory_state_overrides_missing_or_small_task_peak(rss):
    job = Finished("900", "workload", "OUT_OF_MEMORY", elapsed="00:10:00",
                   cpus=8, req_mem=8 * GIB, rss=rss)
    memory = section(Q.analyze(evidence(job)), "Memory")
    assert memory["status"] == "risk" and "memory" in memory["title"]
    assert "doubled" not in memory["action"]


def test_memory_growth_is_descriptive_without_predicting_physical_requirement():
    samples = rates([.5] * 16)
    for index, sample in enumerate(samples):
        sample["rss"] = (1 + index / 8) * GIB
    result = Q.analyze(evidence(series=samples))
    assert "Early to recent task RSS" in text(result)
    assert "no extrapolation" in text(result)
    assert "physical memory need" in text(result)


def test_time_history_filters_incompatible_allocations_and_preserves_worst_duration():
    job = Finished("900", "workload", "COMPLETED", elapsed="00:10:00", cpus=8, nodes=1, limit="01:00:00")
    history = [Finished("1", "workload", "COMPLETED", elapsed="00:20:00", cpus=8, nodes=1),
               Finished("2", "workload", "COMPLETED", elapsed="10:00:00", cpus=16, nodes=2),
               Finished("3", "workload", "FAILED", elapsed="20:00:00", cpus=8, nodes=1)]
    result = Q.analyze(evidence(job, history=history))
    assert result["compatible_runs"] == 1
    wall = section(result, "Wall time")
    assert "00:30:00" in wall["action"]
    assert "Equal names do not prove equal input" in " ".join(wall["facts"])


def test_near_wall_limit_has_warning_without_fabricated_eta():
    job = Job("900", "workload", "cpu", "RUNNING", elapsed="00:59:00", limit="01:00:00")
    wall = section(Q.analyze(evidence(job)), "Wall time")
    assert wall["status"] == "risk"
    assert "do not establish remaining work" in wall["action"]


def test_sparse_observation_span_is_not_claimed_as_continuous_coverage():
    result = Q.analyze(evidence(series=rates([.1, .2, .3], spacing=250)))
    assert result["observation_span"] == 500
    assert math.isclose(result["observation_fraction"], 500 / 600)
    assert "span is not continuous coverage" in text(result)
    assert "unobserved" in text(result)


def test_gpu_samples_and_job_trace_are_not_mixed_into_one_average():
    job = Job("900", "workload", "gpu", "RUNNING", gpus=2)
    payload = evidence(job, series=[dict(t=1, k="gpu", gpu={"node:0": [10, 800, 1000]})])
    payload["trace"] = [dict(t=1, index=0, util=90, mem=900)]
    gpu = section(Q.analyze(payload), "GPU")
    facts = " ".join(gpu["facts"])
    assert "utilization mean 10%" in facts and "Job trace mean 90%" in facts
    assert "memory peak 80%" in facts
    assert "fewer GPUs" in gpu["action"]


def test_latest_gpu_snapshot_is_usable_without_claiming_a_history():
    from raven.model import GpuSample
    payload = evidence(Job("900", "workload", "gpu", "RUNNING", gpus=1))
    payload["gpu"] = [GpuSample("node", 0, util=75, used=800, total=1000)]
    gpu = section(Q.analyze(payload), "GPU")
    assert "latest published GPU snapshot" in " ".join(gpu["facts"])
    assert "utilization mean 75%" in " ".join(gpu["facts"])


def test_pending_attempt_does_not_borrow_retained_resource_observations():
    job = Job("900", "workload", "cpu", "PENDING", cpus=8, mem_req="8Gn")
    payload = evidence(job, series=rates([.9] * 20), live=Live(avg=.9, rss=7 * GIB))
    result = Q.analyze(payload)
    assert result["sample_count"] == 0
    assert section(result, "CPU")["status"] == "unknown"
    assert section(result, "Memory")["status"] == "unknown"
    assert "pending" in text(result)


def test_requeued_attempt_excludes_previous_attempt_samples():
    from raven.model import stamp
    start = "2026-10-08T10:00:00"
    epoch = stamp(start)
    job = Job("900", "workload", "cpu", "RUNNING", start=start, cpus=8)
    result = Q.analyze(evidence(job, series=[dict(t=epoch - 60, k="live", cpu=.99),
                                           dict(t=epoch + 30, k="live", cpu=.1)]))
    assert result["sample_count"] == 1
    assert "P90 10%" in text(result)
    assert "outside this attempt" in text(result)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1, None, "invalid", True])
def test_nonfinite_negative_and_malformed_samples_are_missing_not_crashes(bad):
    payload = evidence(series=[dict(t=1, k="live", cpu=bad, rss=bad),
                               dict(t=2, k="gpu", gpu={"x": [bad, bad, bad]})],
                       live=Live(avg=bad, rss=bad))
    result = Q.analyze(payload)
    assert section(result, "CPU")["status"] == "unknown"
    assert section(result, "Memory")["status"] == "unknown"
    assert "NaN" not in text(result) and "Infinity" not in text(result)


def test_inspector_rank_imbalance_and_exit_are_included():
    payload = evidence(Finished("900", "workload", "FAILED", elapsed="00:10:00", cpus=8, exit="1:0"))
    payload["steps"] = [Step("900.0", cpu_time=800, ntasks=8, min_cpu=20)]
    payload["metadata"] = {"note": "Review input batches"}
    context = section(Q.analyze(payload), "Context")
    facts = " ".join(context["facts"])
    assert "slowest rank CPU time is 20%" in facts
    assert "Accounting exit: 1:0" in facts and "Review input batches" in facts
    assert "application or node failure" in context["action"]


@pytest.mark.parametrize("source", ["tasks", "nodes", "steps"])
def test_multitask_live_cpu_scope_withholds_core_reduction(source):
    payload = evidence(series=rates([.1] * 20), live=Live(avg=.1))
    if source == "tasks":
        payload["details"] = {"NumTasks": "8"}
    elif source == "nodes":
        payload["job"].nodes = 2
    else:
        payload["steps"] = [Step("900.batch", cpu_time=60, ntasks=1), Step("900.0", cpu_time=120, ntasks=8)]
    cpu = section(Q.analyze(payload), "CPU")
    assert "aggregate job use" in cpu["title"]
    assert "before testing fewer CPUs" in cpu["action"] and "Benchmark" not in cpu["action"]


def test_live_rank_imbalance_does_not_divide_sstat_avecpu_by_task_count_twice():
    payload = evidence()
    payload["steps"] = [Step("900.0", cpu_time=100, ntasks=8, min_cpu=20)]
    context = section(Q.analyze(payload), "Context")
    assert "slowest rank CPU time is 20%" in " ".join(context["facts"])


def test_capture_reads_persistent_tail_without_store_series_io_or_ui_thread(dashboard, tmp_path, monkeypatch):
    store = dashboard.store
    store.persist, store.state_dir = True, str(tmp_path)
    directory = tmp_path / "series"
    directory.mkdir()
    (directory / "900.jsonl").write_text(json.dumps({"t": 1, "k": "live", "cpu": .7}) + "\n" +
                                       "truncated json\n" + json.dumps({"t": 2, "k": "live", "rss": GIB}) + "\n")
    store.series["900"].append(dict(t=3, k="live", cpu=.8))
    def forbidden(*args, **kwargs):
        raise AssertionError("series_of holds Store lock during disk reads")
    monkeypatch.setattr(store, "series_of", forbidden)
    J.run_command(dashboard.app, ["jobpanel", "quick"])
    result = ready(dashboard)
    assert result["sample_count"] == 3
    assert "900" not in store._series_loaded


def test_capture_limits_history_and_keeps_exact_job_data(dashboard):
    store = dashboard.store
    store.finished = [Finished(str(index), "exact-workload", "COMPLETED", cpus=8)
                      for index in range(1000, 1400)]
    store.details["900"] = {"WorkDir": "/exact/900"}
    store.details["901"] = {"WorkDir": "/wrong/901"}
    payload = Q.capture(store, "900")
    assert len(payload["history"]) == Q.MAX_HISTORY
    assert payload["details"] == {"WorkDir": "/exact/900"}
    assert "256 of 400" in " ".join(payload["warnings"])
    payload["job"].name = "mutation"
    assert store.jobs[0].name == "exact-workload"


def test_compute_supports_cooperative_cancellation():
    event = Event()
    event.set()
    assert Q.analyze(evidence(), event=event) is None


@pytest.mark.parametrize("elapsed,limit", [("nan", "inf"), ("inf", "nan"), ("-1", "-1"),
                                          ("1e308", "1e308"), (None, None)])
def test_invalid_or_unbounded_accounting_durations_are_missing_evidence(elapsed, limit):
    job = Finished("900", "workload", "COMPLETED", elapsed=elapsed, limit=limit, cpus=8, cpu_time=100)
    result = Q.analyze(evidence(job))
    assert "unknown/unlimited" in text(result)
    assert "NaN" not in text(result) and "Infinity" not in text(result)


def test_persistent_reader_releases_store_lock_before_file_read(dashboard, tmp_path, monkeypatch):
    import builtins
    entered, release = Event(), Event()
    dashboard.release.append(release)
    store = dashboard.store
    store.persist, store.state_dir = True, str(tmp_path)
    directory = tmp_path / "series"
    directory.mkdir()
    path = directory / "900.jsonl"
    path.write_text(json.dumps(dict(t=1, k="live", cpu=.2)) + "\n")
    original = builtins.open
    class Held:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.stream.close()
        def fileno(self):
            return self.stream.fileno()
        def seek(self, offset):
            return self.stream.seek(offset)
        def read(self, count):
            assert current_thread().name.startswith("raven-research")
            entered.set()
            assert release.wait(3)
            return self.stream.read(count)
    def open_file(target, *args, **kwargs):
        stream = original(target, *args, **kwargs)
        return Held(stream) if str(target) == str(path) else stream
    monkeypatch.setattr(builtins, "open", open_file)
    J.run_command(dashboard.app, ["jobpanel", "quick"])
    assert entered.wait(1)
    assert store.lock.acquire(timeout=.1)
    try:
        store.jobs.append(Job("902", "publication-during-IO", "cpu", "PENDING"))
    finally:
        store.lock.release()
    assert any(job.id == "902" for job in store.snapshot()["jobs"])
    dashboard.render()
    release.set()
    ready(dashboard)


def test_bounded_disk_tail_and_large_in_memory_series_are_reported(dashboard, tmp_path, monkeypatch):
    store = dashboard.store
    store.persist, store.state_dir, store.series_keep = True, str(tmp_path), 20
    directory = tmp_path / "series"
    directory.mkdir()
    (directory / "900.jsonl").write_text("".join(json.dumps(dict(t=index, k="live", cpu=.2)) + "\n"
                                                for index in range(100)))
    monkeypatch.setattr(Q, "MAX_SERIES_BYTES", 256)
    monkeypatch.setattr(Q, "MAX_SERIES", 3)
    store.series["900"].extend(rates([.3] * 10))
    payload = Q.capture(store, "900")
    assert len(payload["series"]) == 3
    assert "bounded tail" in " ".join(payload["warnings"])
    assert "3 of 10 in-memory" in " ".join(payload["warnings"])


def test_cpu_interval_mean_uses_each_rate_for_its_preceding_interval():
    mean, span = Q._timed([(1, 0.0), (11, 1.0), (21, 0.0)])
    assert mean == .5 and span == 20
    mean, span = Q._timed([(1, 0.0), (11, 1.0), (31, 0.0)])
    assert math.isclose(mean, 1 / 3) and span == 30


def test_nonregular_series_does_not_block_the_shared_worker(dashboard, tmp_path):
    import os
    store = dashboard.store
    store.persist, store.state_dir = True, str(tmp_path)
    directory = tmp_path / "series"
    directory.mkdir()
    os.mkfifo(directory / "900.jsonl")
    J.run_command(dashboard.app, ["jobpanel", "quick"])
    result = ready(dashboard)
    assert "must be a regular file" in text(result)


def test_history_quick_advisor_is_explicit_and_reads_the_finished_job(dashboard, monkeypatch):
    app = dashboard.app
    app.tab = "history"
    dashboard.store.finished = [Finished("700", "past-run", "FAILED", cpus=2, elapsed="00:02:00", exit="1:0")]
    app.selected_id = "700"
    J.initialize(app)["mode"] = "quick"
    rows, _ = dashboard.render()
    assert "Analyze this job" in L.to_text(rows, 70)
    assert app.research is None
    original, requested = Q.capture, []
    def record(store, jid, event):
        requested.append(jid)
        return original(store, jid, event)
    monkeypatch.setattr(Q, "capture", record)
    assert J.run_command(app, ["jobpanel", "quick"])
    result = ready(dashboard)
    assert requested == ["700"] and result["job"] == "700"
    assert app.tab == "history" and app.selected_id == "700"
