"""Dashboard admission stays nonblocking and cannot bypass worker verification."""
from copy import deepcopy
import json
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from raven import raven_config as C, raven_launch as L, raven_ui as R
from raven.raven_execution import ExecutionRouter


class Manager:
    def __init__(self):
        self.value = {"state": "light", "mode": "light", "generation": 0,
                      "request_id": "", "error": "", "cleanup_uncertain": False}
        self.worker = SimpleNamespace(ready=False, submit=Mock())
        self.calls = []
        self.request_error = None
        self.digest = "unchanged"

    def snapshot(self):
        return deepcopy(self.value)

    def tick(self):
        return False

    def plan(self):
        return {"digest": self.digest, "site_kind": "slurm"}

    def request(self, *, plan=None, reuse=None):
        self.calls.append(("request", plan, reuse))
        if self.request_error:
            raise ValueError(self.request_error)
        self.value.update(state="pending", mode="light", error="",
                          generation=self.value["generation"] + 1, request_id="owned")
        return "owned"

    def request_reuse_review(self, job):
        self.calls.append(("review", job))
        token = "receipt-" + str(len(self.calls))
        self.value["reuse_review"] = {"token": token, "state": "reviewing"}
        return token

    def cancel(self, generation=None):
        self.calls.append(("cancel", generation))
        self.value.update(state="draining", mode="light")
        self.worker.ready = False
        return True

    def shutdown(self):
        self.calls.append(("shutdown",))


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for key in ("RAVEN_CONFIG", "RAVEN_PROFILE", "RAVEN_STATE_DIR", "RAVEN_SITE_POLICY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(C, "SYSTEM_POLICIES", ())


def app(manager=None, reuse=None):
    manager = manager or Manager()
    resolved = SimpleNamespace(data={"allocation": {"pending_timeout_seconds": 120}})
    manager.background_startup = L.BackgroundHeavyStartup(manager, resolved, reuse)
    result = SimpleNamespace(mode="main", tab="jobs", width=80, height=24,
                             selected_id="science-42", messages=[], cfg={"raven": {}})
    result.say = result.messages.append
    result.fail = result.messages.append
    R.bind(result, manager)
    return result, manager


def test_interactive_dashboard_opens_before_admission_or_verification(monkeypatch):
    from raven import cli
    manager = Manager()
    monkeypatch.setattr(L, "_allocation", lambda _: manager)
    monkeypatch.setattr(L, "configure_placement", lambda *args: None)
    monkeypatch.setattr(L.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(L, "_wait_heavy", Mock(side_effect=AssertionError("UI cannot wait")))

    def dashboard(arguments, **kwargs):
        assert not manager.worker.ready and not manager.calls
        assert kwargs["raven_allocation"] is manager
        assert manager.background_startup.phase == "armed"
        return 7

    monkeypatch.setattr(cli, "main", dashboard)
    assert L.heavy_main(["--site", "desktop"]) == 7
    assert manager.background_startup.phase == "closed"
    assert manager.calls == [("shutdown",)]


@pytest.mark.parametrize("restriction", [{"deny_heavy": True}, {"require_account": True}])
def test_policy_denial_is_visible_inside_light_dashboard_without_launch(tmp_path, monkeypatch, restriction):
    from raven import cli
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"schema": C.POLICY_SCHEMA, "restrictions": restriction}))
    monkeypatch.setattr(L.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(L, "configure_placement", lambda *args: None)
    monkeypatch.setattr("raven.raven_allocation.subprocess.Popen", Mock(side_effect=AssertionError("policy must prevent launch")))

    def dashboard(arguments, **kwargs):
        manager = kwargs["raven_allocation"]
        a = SimpleNamespace(mode="main", tab="jobs", width=80, height=24, cfg={"raven": {}})
        R.bind(a, manager)
        R.tick(a)
        assert R.status(a)["mode"] == "light" and R.status(a)["error"]
        assert manager.background_startup.retryable
        assert manager._process is None
        return 0

    monkeypatch.setattr(cli, "main", dashboard)
    assert L.heavy_main(["--site-policy", str(policy)]) == 0


@pytest.mark.parametrize("arguments", [["--once"], ["--json"], ["--csv"],
    ["--report"], ["--eval", "n_running"], ["--wait-for", "n_running == 0"],
    ["--run", "raven inspect"], ["--gpu-check"], ["run", "raven", "inspect"]])
def test_one_shot_modes_keep_readiness_guarantee(arguments, monkeypatch):
    monkeypatch.setattr(L.sys.stdout, "isatty", lambda: True)
    assert not L._background_dashboard(arguments)


def test_explicit_watch_also_opens_without_waiting(monkeypatch):
    monkeypatch.setattr(L.sys.stdout, "isatty", lambda: False)
    assert L._background_dashboard(["--watch"])
    assert not L._background_dashboard(["--watch", "--once"])
    assert not L._background_dashboard([], json_mode=True)


def test_queued_to_verified_transition_preserves_context_and_gates_execution():
    a, manager = app()
    router = ExecutionRouter({"managed": True}, lambda: manager.worker)
    assert R.status(a)["state"] == "preparing"
    assert manager.calls == []
    assert router.dispatch("log-search", {"text": "x", "needle": "x"})["status"] == "deferred"
    R.tick(a)
    assert R.status(a)["state"] == "pending" and R.status(a)["mode"] == "light"
    assert "Heavy allocation queued" in R.summary(a)
    assert ("cancel", "Cancel request") in R._choices(a)
    assert router.require("log-search") is not None
    manager.value.update(state="starting")
    R.tick(a)
    assert "verifying worker" in R.summary(a)
    assert router.require("log-search") is not None
    manager.value.update(state="heavy")
    manager.worker.ready = True
    R.tick(a)
    assert R.status(a)["mode"] == "heavy"
    assert manager.background_startup.phase == "ready"
    assert router.require("log-search") is None
    assert a.mode == "main" and a.selected_id == "science-42"
    manager.worker.submit.assert_not_called()


def test_failure_keeps_dashboard_usable_and_retry_requires_review():
    a, manager = app()
    manager.request_error = "Invalid account labx: allocation denied"
    R.tick(a)
    assert a.mode == "main" and a.selected_id == "science-42"
    assert R.status(a)["error"] == manager.request_error
    assert ("retry", "Retry Heavy") in R._choices(a)
    R.run_command(a, ["raven", "retry"])
    assert a.raven_ui_state["review"]["action"] == "retry"
    assert len(manager.calls) == 1
    manager.request_error = None
    R.run_command(a, ["raven", "accept"])
    assert len(manager.calls) == 1  # accepted intent; next event-loop tick submits
    R.tick(a)
    assert len(manager.calls) == 2 and R.status(a)["state"] == "pending"
    assert not R.status(a)["error"]


def test_cancel_before_first_tick_never_submits_and_is_retryable():
    a, manager = app()
    R.run_command(a, ["raven", "cancel"])
    R.run_command(a, ["raven", "accept"])
    R.tick(a)
    assert not manager.calls
    assert R.status(a)["state"] == "light"
    assert manager.background_startup.retryable


def test_cancel_racing_grant_uses_same_owned_generation():
    a, manager = app()
    R.tick(a)
    R.run_command(a, ["raven", "cancel"])
    manager.value.update(state="heavy")
    manager.worker.ready = True
    R.tick(a)
    R.run_command(a, ["raven", "accept"])
    assert manager.calls[-1] == ("cancel", 1)
    assert not manager.worker.ready
    assert manager.background_startup.phase == "cancelled"
    assert R.status(a)["mode"] == "light"


def test_retry_cannot_bypass_uncertain_cleanup_or_changed_settings():
    a, manager = app()
    R.tick(a)
    manager.value.update(state="reconciling", cleanup_uncertain=True, error="Uncertain ownership")
    R.tick(a)
    assert not manager.background_startup.retryable
    with pytest.raises(ValueError, match="confirmed cleanup"):
        manager.background_startup.retry()
    manager.value.update(state="light", cleanup_uncertain=False)
    R.tick(a)
    R.run_command(a, ["raven", "retry"])
    R.run_command(a, ["raven", "accept"])
    manager.digest = "changed"
    R.tick(a)
    assert "settings changed" in R.status(a)["error"]
    assert len(manager.calls) == 1


def test_reuse_waits_for_matching_receipt_and_never_substitutes_new_allocation():
    a, manager = app(reuse="123")
    R.tick(a)
    assert manager.calls == [("review", "123")]
    assert "verifying allocation" in R.summary(a)
    manager.value["reuse_review"] = {"token": "stale", "state": "ready", "plan": {"digest": "other"}}
    R.tick(a)
    assert len(manager.calls) == 1
    manager.value["reuse_review"] = {"token": "receipt-1", "state": "ready", "plan": {"digest": "reuse"}}
    R.tick(a)
    assert manager.calls[-1] == ("request", {"digest": "reuse"}, "receipt-1")
    assert R.status(a)["state"] == "pending"


@pytest.mark.parametrize("cancel", [True, False])
def test_cancelled_or_closed_reuse_review_can_never_start_worker(cancel):
    a, manager = app(reuse="123")
    R.tick(a)
    if cancel:
        R.run_command(a, ["raven", "cancel"])
        R.run_command(a, ["raven", "accept"])
    else:
        R.shutdown(a)
    manager.value["reuse_review"] = {"token": "receipt-1", "state": "ready", "plan": {"digest": "reuse"}}
    R.tick(a)
    assert not any(call[0] == "request" for call in manager.calls)


def test_timeout_cancels_only_owned_request_and_waits_for_cleanup(monkeypatch):
    now = [10.0]
    monkeypatch.setattr(L.time, "monotonic", lambda: now[0])
    a, manager = app()
    R.tick(a)
    now[0] = 191.0
    R.tick(a)
    assert manager.calls[-1] == ("cancel", None)
    assert R.status(a)["mode"] == "light" and "timed out" in R.status(a)["error"]
    assert not manager.background_startup.retryable
    manager.value.update(state="light")
    R.tick(a)
    assert manager.background_startup.retryable


def test_closing_dashboard_disarms_not_yet_started_request():
    a, manager = app()
    R.shutdown(a)
    R.tick(a)
    assert manager.calls == [("shutdown",)]


def test_real_desktop_worker_uses_same_dashboard_from_light_to_ready_and_release(tmp_path):
    """Exercise the actual helper/verification protocol, without any scheduler."""
    resolved = C.resolve(site="desktop", system_policy_paths=[], cli_overrides={
        "resources": {"heavy": {"memory_mib": 512, "max_workers": 2, "wall_seconds": 30}},
        "storage": {"state_dir": str(tmp_path / "worker-state"),
                    "coordination_dir": str(tmp_path / "coordination")}})
    manager = L.configure_placement(resolved, L._allocation(resolved))
    a, _ = app(manager)
    assert manager._process is None and R.status(a)["mode"] == "light"

    def until(predicate):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            R.tick(a)
            if predicate():
                return
            time.sleep(.005)
        pytest.fail(str(R.status(a)))

    try:
        until(lambda: manager.worker.ready)
        assert manager.snapshot()["identity"]["verified"] is True
        assert R.status(a)["mode"] == "heavy" and a.selected_id == "science-42"
        R.run_command(a, ["raven", "release"])
        R.run_command(a, ["raven", "accept"])
        assert R.status(a)["mode"] == "light"
        until(lambda: manager.snapshot()["state"] == "light")
        assert not manager.snapshot()["cleanup_uncertain"]
        assert a.selected_id == "science-42"
    finally:
        R.shutdown(a)
        manager.collection_backend.close()
