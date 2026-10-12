"""Telemetry inspection cannot freeze, misattribute jobs, or click through."""
from dataclasses import replace
from types import SimpleNamespace
import threading
import time

import pytest

from raven import interaction, layout as L, telemetry, telemetry_ui as U, workbench
from raven.actions import Actions
from raven.config import Config
from raven.controller import App
from raven.model import Health, Job, Live, Store
from raven.research import ResearchHub
from raven.slurm import Slurm
from raven.worker_scheduler import WorkerScheduler


class Backend:
    def __init__(self):
        self.calls = []

    def run(self, command, timeout=8):
        self.calls.append((command, timeout, threading.current_thread().name))
        if command == ["scontrol", "show", "config"]:
            return "ClusterName = science\nJobAcctGatherType = jobacct_gather/cgroup\nJobAcctGatherFrequency = 30", .1
        job_id = command[-1]
        return f"JobId={job_id} SubmitTime=2026-01-01T01:00:00 StartTime=2026-01-01T01:05:00 JobState=RUNNING AcctGatherFrequency=task=5", .1


class Hub:
    def __init__(self):
        self.pending = None
        self.starts = 0

    def refresh_interval(self):
        return 2.5

    def start_task(self, function, callback):
        if self.pending:
            return False
        self.pending = function, callback
        self.starts += 1
        return True

    def complete(self, override=None):
        function, callback = self.pending
        self.pending = None
        callback(function() if override is None else override)


@pytest.fixture
def app(tmp_path):
    backend = Backend()
    store = Store(persist=False)
    store.jobs = [Job("17", "simulation", "cpu", "RUNNING", submit="2026-01-01T01:00:00", start="2026-01-01T01:05:00", cluster="science"),
                  Job("18", "other", "cpu", "RUNNING", submit="2026-01-01T01:00:00", start="2026-01-01T01:05:00", cluster="science")]
    cfg = Config({"animations": False, "log_lines": 0})
    value = App(store, None, Actions(Slurm(backend, "tester"), store), cfg, "tester", interactive=True)
    value.research = Hub()
    value.selected_id = "17"
    value.visible_ids = ["17", "18"]
    value.state_dir = str(tmp_path)
    value.width, value.height = 100, 30
    return value


def render(app, *, width=100, height=30, ascii_=False):
    app.width, app.height = width, height
    return U.overlay(SimpleNamespace(g=L.Glyphs(ascii_)), {}, app, width, height)


def complete(app):
    assert U.open_inspector(app)
    app.research.complete()
    return app.telemetry_state


def test_registration_and_no_scheduler_work_during_open_or_render(app):
    assert "telemetry" in workbench.command_names()
    app.run_command("telemetry")
    assert app.mode == "telemetry" and app.telemetry_state["running"]
    for _ in range(100):
        render(app)
    assert app.actions.slurm.b.calls == []
    app.research.complete()
    assert len(app.actions.slurm.b.calls) == 2
    for _ in range(100):
        render(app)
    assert len(app.actions.slurm.b.calls) == 2
    assert app.tab == "jobs"


def test_effective_native_polling_and_health_captured_without_snapshot(app):
    calls = []
    app.sampler = SimpleNamespace(gpu_sampling=True, sampling_interval=lambda source, jid, attempt: calls.append((source, jid, attempt)) or .5)
    app.store.health["live"] = Health("live", last_ok=123, backoff=10, error="temporary")
    app.store.live["17"] = Live(t=123)
    state = complete(app)
    assert calls == [(source, "17", "2026-01-01T01:00:00|2026-01-01T01:05:00") for source in ("live", "gpu", "trace")]
    cpu = state["report"]["metrics"][0]
    assert cpu["raven_interval_seconds"] == .5
    assert cpu["producer_interval_seconds"] == 5
    assert cpu["backoff_seconds"] == 10 and cpu["error"] == "temporary"
    assert state["report"]["metrics"][-1]["raven_interval_seconds"] == 2.5


def test_explicit_job_does_not_change_underlying_selection(app):
    U.run_command(app, ["telemetry", "18"])
    app.research.complete()
    assert app.telemetry_state["report"]["job_id"] == "18"
    assert app.selected_id == "17" and app.tab == "jobs"


def test_refresh_stays_pinned_and_bypasses_configuration_cache(app):
    state = complete(app)
    app.selected_id = "18"
    U.run_command(app, ["telemetry", "refresh"])
    app.research.complete()
    assert state["report"]["job_id"] == "17"
    assert not state["report"]["config_cached"]
    assert len(app.actions.slurm.b.calls) == 4


def test_reopen_uses_bounded_config_cache_but_refreshes_job_evidence(app):
    state = complete(app)
    U.handle_key(app, "esc")
    U.open_inspector(app)
    app.research.complete()
    assert state["report"]["config_cached"]
    assert len(app.actions.slurm.b.calls) == 3


def test_duplicate_activation_does_not_queue(app):
    U.open_inspector(app)
    for _ in range(30):
        U.open_inspector(app, refresh=True)
    assert app.research.starts == 1 and app.actions.slurm.b.calls == []


def test_busy_worker_reports_retry_without_stealing_task(app):
    app.research.pending = (lambda: None, lambda result: None)
    old = app.research.pending
    U.open_inspector(app)
    state = app.telemetry_state
    assert app.research.pending is old
    assert "busy" in state["error"] and not state["running"]
    assert app.mode == "telemetry" and state["report"] is None


def test_late_callback_after_close_does_not_reopen_or_publish(app):
    U.open_inspector(app)
    U.handle_key(app, "esc")
    app.tab = "history"
    app.research.complete()
    assert app.mode == "main" and app.tab == "history"
    assert app.telemetry_state["report"] is None


def test_old_request_cannot_replace_new_job(app):
    U.open_inspector(app, "17")
    old_function, old_callback = app.research.pending
    U.open_inspector(app, "18")
    assert "busy" in app.telemetry_state["error"]
    old_callback(old_function())
    assert app.telemetry_state["job_id"] == "18"
    assert app.telemetry_state["report"] is None


@pytest.mark.parametrize("field,value", [("start", "2026-01-01T02:00:00"), ("submit", "2026-01-01T02:00:00"), ("cluster", "other")])
def test_changed_attempt_rejects_completion(app, field, value):
    U.open_inspector(app)
    app.store.jobs[0] = replace(app.store.jobs[0], **{field: value})
    app.research.complete()
    assert "attempt changed" in app.telemetry_state["error"]
    assert app.telemetry_state["report"] is None


def test_connection_change_rejects_completion(app):
    U.open_inspector(app)
    app.actions.slurm.b = Backend()
    app.research.complete()
    assert "Connection changed" in app.telemetry_state["error"]
    assert app.telemetry_state["report"] is None


def test_discovered_cluster_enriches_unknown_queue_cluster_without_false_staleness(app):
    # Ordinary squeue rows do not include a cluster field. A successful
    # scontrol configuration read adds evidence, not an identity change.
    app.store.jobs[0] = replace(app.store.jobs[0], cluster="")
    state = complete(app)
    assert state["report"]["attempt"]["cluster"] == "science"
    assert not state["error"]


def test_previously_known_cluster_cannot_disappear_while_inspection_runs(app):
    U.open_inspector(app)
    app.store.jobs[0] = replace(app.store.jobs[0], cluster="")
    app.research.complete()
    assert "attempt changed" in app.telemetry_state["error"]
    assert app.telemetry_state["report"] is None


def test_newly_known_conflicting_cluster_cannot_publish_old_connection_evidence(app):
    app.store.jobs[0] = replace(app.store.jobs[0], cluster="")
    U.open_inspector(app)
    app.store.jobs[0] = replace(app.store.jobs[0], cluster="different-site")
    app.research.complete()
    assert "does not match" in app.telemetry_state["error"]
    assert app.telemetry_state["report"] is None


def test_unrelated_selection_change_keeps_explicit_report_identity(app):
    U.open_inspector(app)
    app.selected_id = "18"
    app.research.complete()
    assert app.telemetry_state["report"]["job_id"] == "17"
    assert not app.telemetry_state["error"]


@pytest.mark.parametrize("value", [RuntimeError("lost connection"), {}, [], 7])
def test_exception_and_bad_result_are_visible(app, value):
    U.open_inspector(app)
    app.research.complete(value)
    assert app.telemetry_state["error"]
    assert not app.telemetry_state["running"]


def test_invalid_command_replay_and_missing_connection_never_issue_reads(app):
    for args in (["telemetry", "17", "18"], ["telemetry", "--help"], ["telemetry", "17_[1-5]"]):
        assert U.run_command(app, args)
        assert app.mode == "main" and not app.command_ok
    app.replay = True
    assert not U.open_inspector(app)
    app.replay = None
    app.actions = None
    assert not U.open_inspector(app)
    assert app.research.pending is None


@pytest.mark.parametrize("width,height", [(0, 0), (1, 1), (3, 2), (4, 3), (8, 6), (15, 9), (25, 12), (50, 18), (120, 40)])
@pytest.mark.parametrize("ascii_", [False, True])
def test_overlay_fits_small_terminals_and_unicode(app, width, height, ascii_):
    state = complete(app)
    state["lines"].append("Unicode 研究 αβ café 👩 and long " + "x" * 300)
    rows = render(app, width=width, height=height, ascii_=ascii_)
    for y, x, row in rows:
        text = L.row_text(row)
        assert 0 <= y < height and 0 <= x < width
        assert L.vlen(text) + x <= width
        if ascii_:
            assert text.isascii()
    for y, _, hit in state["control_hits"]:
        assert 0 <= y < height and 0 <= hit["left"] < hit["right"] <= width


def test_button_controls_map_exactly_to_rendered_semantic_targets(app):
    state = complete(app)
    rows = render(app)
    assert [hit[2]["action"] for hit in state["control_hits"]] == [("command", "telemetry " + command) for command in ("refresh", "copy", "close")]
    graph = interaction.publish(app, [], [], app.width, app.height, overlays=rows)
    assert {c.id for c in graph.controls} >= {"telemetry:refresh", "telemetry:copy", "telemetry:close"}
    for y, _, hit in state["control_hits"]:
        control = graph.at(y, hit["left"])
        assert control is not None and control.action == hit["action"]


def test_keyboard_selection_copy_and_right_click_reset(app, monkeypatch):
    copied = []
    monkeypatch.setattr("raven.clipboard.copy", lambda text, *args, **kwargs: copied.append(text) or "copied")
    state = complete(app)
    render(app)
    U.handle_key(app, "v")
    U.handle_key(app, "down")
    U.handle_key(app, "down")
    U.handle_key(app, "y")
    assert copied[-1] == "\n".join(state["lines"][:3]) + "\n"
    U.handle_mouse(app, -100, -100, button="right")
    assert state["anchor"] is None
    U.handle_key(app, "y")
    assert copied[-1] == "\n".join(state["lines"]) + "\n"


def test_mouse_shift_selection_and_press_controls(app):
    state = complete(app)
    render(app)
    y, left, _, index = state["line_hits"][2]
    U.handle_mouse(app, y, left, button="left", shift=True)
    assert state["anchor"] == 0 and state["cursor"] == index
    y, left, _, _ = state["hits"][2]
    U.handle_mouse(app, y, left, button="press")
    assert app.mode == "main"


def test_arrow_and_page_navigation_remain_bounded(app):
    state = complete(app)
    render(app, width=45, height=12)
    for _ in range(100):
        U.handle_key(app, "down")
    assert state["cursor"] == len(state["lines"]) - 1
    rows = render(app, width=45, height=12)
    assert state["top"] > 0
    U.handle_key(app, "home")
    render(app, width=45, height=12)
    assert state["cursor"] == 0 and state["top"] == 0
    U.handle_key(app, "pgdn")
    assert state["cursor"] == state["page"]
    U.handle_key(app, "end")
    assert state["cursor"] == len(state["lines"]) - 1


def test_tab_arrows_enter_activate_only_chosen_button(app):
    state = complete(app)
    U.handle_key(app, "tab")
    U.handle_key(app, "right")
    U.handle_key(app, "right")
    U.handle_key(app, "enter")
    assert app.mode == "main"
    assert len(app.actions.slurm.b.calls) == 2


def test_clicking_evidence_restores_content_keys_after_button_traversal(app, monkeypatch):
    copied = []
    monkeypatch.setattr("raven.clipboard.copy", lambda text, *args, **kwargs: copied.append(text) or "copied")
    state = complete(app)
    rows = render(app)
    interaction.publish(app, [], [], app.width, app.height, overlays=rows)
    app.handle("f8")
    assert app.interaction_state["active"]
    y, left, _, index = state["line_hits"][0]
    app.click(y, left, [], button="left")
    assert not app.interaction_state["active"]
    app.handle("v")
    app.handle("down")
    app.handle("y")
    assert state["cursor"] == index + 1
    assert copied[-1] == "\n".join(state["lines"][index:index + 2]) + "\n"


@pytest.mark.parametrize("button", ["left", "press", "motion", "drag", "release", "right", "double", "wheel-up", "wheel-down"])
def test_controller_modal_owns_pointer_and_prevents_tab_job_graph_clickthrough(app, button):
    state = complete(app)
    rows = render(app)
    app.tab_hits = [(2, 0, 100, "research")]
    app.marks = {"17"}
    hits = [(2, "job", "18"), (2, "tab", "research")]
    interaction.publish(app, [], hits, app.width, app.height, overlays=rows)
    app.click(2, 2, hits, button=button)
    assert app.mode == "telemetry" and app.tab == "jobs"
    assert app.selected_id == "17" and app.marks == {"17"}


@pytest.mark.parametrize("mode", ["single", "multi"])
def test_shared_real_worker_completes_without_ui_wait_or_nested_worker_deadlock(app, mode):
    scheduler = WorkerScheduler(workers=3, mode=mode, lanes=("research", "source"))
    hub = ResearchHub(app.cfg, slurm=app.actions.slurm, worker_scheduler=scheduler)
    app.research = hub
    try:
        assert U.open_inspector(app)
        deadline = time.monotonic() + 3
        while hub.pending and time.monotonic() < deadline:
            hub.poll_task()
            time.sleep(.001)
        assert not hub.pending
        assert app.telemetry_state["report"]["job_id"] == "17"
        assert all(thread_name != threading.current_thread().name for _, _, thread_name in app.actions.slurm.b.calls)
    finally:
        hub.close()
        scheduler.shutdown(wait=True, cancel_futures=True)


def test_nonmodal_handlers_do_not_intercept(app):
    assert not U.handle_key(app, "g")
    assert not U.handle_mouse(app, 2, 2)
    assert render(app) is None
    assert not U.run_command(app, ["unrelated"])


def test_scripted_inspection_returns_structured_report_without_unpublished_work(app):
    app.interactive = False
    app.run_command("telemetry 17")
    assert app.research.pending is None
    assert app.research_result["schema"] == telemetry.SCHEMA
    assert app.research_result["job_id"] == "17"
    assert app.command_ok and len(app.actions.slurm.b.calls) == 2


def test_scripted_inspection_reports_unavailable_evidence_and_failure(app):
    app.interactive = False
    def fail(*args, **kwargs):
        raise RuntimeError("remote timed out")
    app.actions.slurm.b.run = fail
    app.run_command("telemetry 17")
    assert app.research_result["status"] == "unavailable"
    assert "remote timed out" in app.research_result["errors"][0]
    assert not app.command_ok


def test_wrapped_connection_change_rejects_old_read(app):
    class Wrapper:
        def __init__(self, inner):
            self.inner = inner
        def run(self, *args, **kwargs):
            return self.inner.run(*args, **kwargs)
    app.actions.slurm.b = Wrapper(app.actions.slurm.b)
    U.open_inspector(app)
    app.actions.slurm.b.inner = Backend()
    app.research.complete()
    assert "Connection changed" in app.telemetry_state["error"]
