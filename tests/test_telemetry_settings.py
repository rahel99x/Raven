"""Real collectors and durable preferences agree with live telemetry controls."""
import copy
import json
from types import SimpleNamespace

import pytest

from raven import raven_config as C, refresh_rate, telemetry_settings as T
from raven.config import Config
from raven.model import Store
from raven.sampler import Sampler
from raven.telemetry_profiles import DEFAULTS, FIELDS, VALUES, validate_custom, value_fraction


@pytest.fixture
def session(tmp_path):
    samplers = []
    def make(*, heavy=True, document=None, policies=(), profile=None):
        path = tmp_path / "config.json"
        if document is not None:
            path.write_text(json.dumps(document))
        resolved = C.resolve(config_path=str(path) if path.exists() else None, site=profile,
                             env={"HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path)},
                             system_policy_paths=list(policies))
        cfg = Config(C.to_raven_config(resolved))
        sampler = Sampler(SimpleNamespace(), Store(persist=False), cfg["intervals"], [],
                          collection=cfg["raven"]["collection"])
        samplers.append(sampler)
        app = SimpleNamespace(cfg=cfg, sampler=sampler, raven_resolved=resolved,
                              raven_ui_state={"snapshot": {"state": "heavy" if heavy else "light",
                                                           "mode": "heavy" if heavy else "light"}})
        # A configured destination need not already exist.
        if not resolved.source:
            app.config_path = str(path)
        refresh_rate.initialize(app)
        return app
    yield make
    for sampler in samplers:
        sampler.shutdown()


def test_heavy_presets_change_real_collectors_without_replacement(session):
    app = session()
    sampler, pool = app.sampler, app.sampler.pool
    for preset in ("default", "medium", "high", "ultra", "default"):
        T.apply_settings(app, preset)
        settings = VALUES[preset]
        assert sampler.effective_interval("jobs") == settings["queue_seconds"]
        assert sampler.effective_interval("live") == settings["live_seconds"]
        assert sampler.effective_interval("trace") == settings["trace_seconds"]
        assert sampler.effective_interval("finished") == settings["history_seconds"]
        assert app.sampler is sampler and sampler.pool is pool
    assert T.settings_snapshot(app)["custom"] == DEFAULTS["custom"]


def test_custom_survives_other_presets_and_validation_is_atomic(session):
    app = session()
    custom = {**DEFAULTS["custom"], "queue_seconds": 3.2, "live_seconds": .8}
    T.apply_settings(app, "custom", custom)
    T.apply_settings(app, "ultra")
    assert T.settings_snapshot(app)["custom"] == custom
    T.apply_settings(app, "custom")
    assert app.sampler.effective_interval("jobs") == 3.2
    before = T.settings_snapshot(app)
    with pytest.raises(ValueError):
        T.apply_settings(app, "custom", {**custom, "live_seconds": float("nan")})
    assert T.settings_snapshot(app) == before


def test_light_slider_and_heavy_transition_preserve_distinct_policies(session):
    app = session(heavy=False)
    assert app.sampler.effective_interval("jobs") == 60
    refresh_rate.set_multiplier(app, 50)
    assert app.sampler.effective_interval("jobs") == pytest.approx(6)
    assert app.sampler.effective_interval("live") == 60
    T.apply_settings(app, "high")
    assert app.sampler.effective_interval("jobs") == 60
    app.raven_ui_state["snapshot"] = {"state": "heavy", "mode": "heavy"}
    T.sync(app)
    assert app.sampler.effective_interval("jobs") == 1
    app.raven_ui_state["snapshot"] = {"state": "reconciling", "mode": "light", "cleanup_uncertain": True}
    T.sync(app)
    assert app.sampler.effective_interval("jobs") == 60
    assert T.settings_snapshot(app)["preset"] == "high"


def test_site_limits_remain_visible_and_cannot_be_evaded_by_slider(session, tmp_path):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({"schema": C.POLICY_SCHEMA, "restrictions": {
        "floors": {"collection.queue_seconds": 3, "collection.live_seconds": 4},
        "ceilings": {"collection.controller_requests_per_minute": 7}}}))
    app = session(policies=[path])
    T.apply_settings(app, "ultra")
    refresh_rate.set_multiplier(app, 50)
    assert app.sampler.effective_interval("jobs") == 3
    assert app.sampler.effective_interval("live") == 4
    assert app.sampler.collection["controller_requests_per_minute"] == 7
    assert any("site" in limit for limit in T.settings_snapshot(app)["limits"])


def test_snapshot_and_typing_need_no_file_io_and_cached_profiles_are_bounded(session, monkeypatch):
    app = session()
    def fail(*args, **kwargs):
        raise AssertionError("Rendering must not read or write a file")
    monkeypatch.setattr("builtins.open", fail)
    monkeypatch.setattr("os.open", fail)
    for preset in VALUES:
        T.apply_settings(app, preset)
        for position in range(1, 51):
            assert T.candidate_cadence(app, "jobs", position) > 0
        for field in FIELDS:
            assert value_fraction(field["key"], field["default"]) == 0
            assert value_fraction(field["key"], field["ultra"]) == 1
        T.settings_snapshot(app)
    assert len(app.telemetry_settings_state["effective_cache"]) <= 128


@pytest.mark.parametrize("bad", [True, float("nan"), float("inf"), -1, 0, "1", 10 ** 500])
def test_invalid_custom_values_rejected_by_model_and_config(bad):
    with pytest.raises(ValueError):
        validate_custom({**DEFAULTS["custom"], "queue_seconds": bad})
    with pytest.raises(ValueError):
        C.validate({"schema": C.SCHEMA, "telemetry": {"custom": {"queue_seconds": bad}}}, partial=True)


def test_save_preserves_unrelated_config_and_reloads_saved_custom(session, tmp_path):
    app = session(document={"schema": C.SCHEMA, "allocation": {"account": "labX"}})
    custom = {**DEFAULTS["custom"], "queue_seconds": 2.25}
    T.apply_settings(app, "custom", custom)
    request = T.prepare_save(app)
    path = tmp_path / "config.json"
    # An independent allocation edit need not prevent telemetry saving.
    document = json.loads(path.read_text())
    document["allocation"]["partition"] = "workstation"
    path.write_text(json.dumps(document))
    result = T.save_settings(request)
    T.saved(app, request, result)
    data = json.loads(path.read_text())
    assert data["allocation"] == {"account": "labX", "partition": "workstation"}
    assert data["telemetry"] == {"preset": "custom", "custom": custom}
    resolved = C.resolve(config_path=str(path), env={"HOME": str(tmp_path)}, system_policy_paths=[])
    assert resolved.data["telemetry"] == data["telemetry"]
    T.apply_settings(app, "high")
    T.save_settings(T.prepare_save(app))
    assert json.loads(path.read_text())["telemetry"]["preset"] == "high"


def test_save_conflicting_telemetry_leaves_external_file_intact(session, tmp_path):
    app = session(document={"schema": C.SCHEMA})
    T.apply_settings(app, "high")
    request = T.prepare_save(app)
    path = tmp_path / "config.json"
    external = json.dumps({"schema": C.SCHEMA, "telemetry": {"preset": "ultra"}})
    path.write_text(external)
    with pytest.raises(ValueError, match="changed"):
        T.save_settings(request)
    assert path.read_text() == external


def test_save_named_profile_preserves_other_users_and_parent_preferences(session, tmp_path):
    document = {"schema": C.SCHEMA, "profiles": {
        "lab": {"allocation": {"account": "labX"}}, "personal": {"allocation": {"account": "alice"}}}}
    app = session(document=document, profile="lab")
    T.apply_settings(app, "medium")
    T.save_settings(T.prepare_save(app))
    output = json.loads((tmp_path / "config.json").read_text())
    assert "telemetry" not in output
    assert output["profiles"]["personal"] == document["profiles"]["personal"]
    assert output["profiles"]["lab"]["allocation"]["account"] == "labX"
    assert output["profiles"]["lab"]["telemetry"]["preset"] == "medium"


def test_queue_slider_remains_smooth_for_ultra_and_displays_idle_cadence(session, monkeypatch):
    app = session()
    T.apply_settings(app, "ultra")
    values = [T.candidate_cadence(app, "jobs", n) for n in range(1, 51)]
    assert all(left > right for left, right in zip(values, values[1:]))
    assert values[0] == .5 and values[-1] == pytest.approx(.1)
    app.sampler._collection_activity = 0
    monkeypatch.setattr("time.monotonic", lambda: 1000.)
    assert T.candidate_cadence(app, "jobs", 1) == app.sampler.effective_interval("jobs")


def test_account_setup_before_heavy_uses_updated_admission_context(session, tmp_path):
    path = tmp_path / "account-policy.json"
    path.write_text(json.dumps({"schema": C.POLICY_SCHEMA, "restrictions": {"require_account": True}}))
    app = session(heavy=False, policies=[path])
    T.settings_snapshot(app)
    app.cfg.set("raven.allocation.account", "LabX")
    app.raven_ui_state["snapshot"] = {"state": "heavy", "mode": "heavy"}
    T.sync(app)
    assert app.sampler.effective_interval("jobs") == 5
    assert T.initialize(app)["baseline"]["allocation"]["account"] == "LabX"


def test_site_recommended_preset_can_be_saved_without_false_conflict(session, tmp_path):
    path = tmp_path / "recommendation.json"
    path.write_text(json.dumps({"schema": C.POLICY_SCHEMA, "recommendations": {
        "telemetry": {"preset": "high", "custom": {"queue_seconds": 3}}}}))
    app = session(document={"schema": C.SCHEMA}, policies=[path])
    assert T.settings_snapshot(app)["preset"] == "high"
    T.apply_settings(app, "medium")
    T.save_settings(T.prepare_save(app))
    assert json.loads((tmp_path / "config.json").read_text())["telemetry"]["preset"] == "medium"
