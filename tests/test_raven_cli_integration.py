"""Managed launch boundaries exercised through the existing dashboard builder."""
from unittest.mock import Mock

import pytest

from raven import cli
from raven.config import Config
from raven.raven_config import resolve, to_raven_config
from raven.raven_execution import process_router, configure_process, reset_process


def resolved(tmp_path, monkeypatch, site="desktop"):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("RAVEN_CONFIG", raising=False)
    monkeypatch.delenv("RAVEN_CONFIG", raising=False)
    return resolve(site=site, system_policy_paths=[])


def test_process_admission_restores_previous_router_even_on_parse_error(tmp_path, monkeypatch):
    value = resolved(tmp_path, monkeypatch)
    previous = process_router()
    configure_process({"managed": True, "test": "outer"})
    outer = process_router()
    try:
        with pytest.raises(SystemExit):
            cli.main(["--unknown-raven-test"], raven=value)
        assert process_router() is outer
    finally:
        reset_process(previous)


def test_trace_execution_rejected_before_recorder_import(tmp_path, monkeypatch, capsys):
    value = resolved(tmp_path, monkeypatch)
    dispatch = Mock(side_effect=AssertionError("must not import execution route"))
    monkeypatch.setattr(cli, "_dispatch_main", dispatch)
    assert cli.main(["trace", "record", "--pid", "123"], raven=value) == 3
    assert "interface host" in capsys.readouterr().out
    dispatch.assert_not_called()


def test_managed_connection_cannot_retarget_admitted_worker(tmp_path, monkeypatch, capsys):
    value = resolved(tmp_path, monkeypatch)
    build = Mock(side_effect=AssertionError("must reject before backend"))
    monkeypatch.setattr(cli, "build", build)
    assert cli.main(["--host", "other.example", "--once"], raven=value) == 1
    assert "cannot retarget" in capsys.readouterr().err
    build.assert_not_called()


def test_managed_legacy_profile_cannot_override_policy(tmp_path, monkeypatch, capsys):
    value = resolved(tmp_path, monkeypatch)
    assert cli.main(["--profile", "untrusted", "--once"], raven=value) == 1
    assert "--site" in capsys.readouterr().err


def test_raven_state_uses_separate_root(tmp_path, monkeypatch):
    value = resolved(tmp_path, monkeypatch)
    cfg = Config(to_raven_config(value))
    path = cli.scoped_state_dir(cli.Backend(), cfg, "alice")
    assert path.startswith(str(tmp_path / "state" / "raven"))
    assert "/profiles/raven/" in path


def test_light_builder_has_no_allocator_start_and_no_plugins(tmp_path, monkeypatch):
    value = resolved(tmp_path, monkeypatch)
    cfg = Config(to_raven_config(value))
    cfg.set("raven.managed", True)
    cfg.set("collection_cache_dir", str(tmp_path / "host-cache"))
    discover = Mock(side_effect=AssertionError("managed plugins cannot import arbitrary code"))
    monkeypatch.setattr(cli.plugins, "discover", discover)
    session = cli.build(cli.parse(["--fake", "--no-state", "--once"]), cfg)
    try:
        assert session.app.raven_allocation_manager.snapshot()["state"] == "light"
        assert session.app.raven_allocation_manager._process is None
        assert session.app.raven_execution.managed
        assert session.sampler.collection["slurm_enabled"] is False
        assert session.backend.ledger is not None
        from raven.raven_diagnostics import collect
        assert collect(session.app)["collection"]["scope"] == "user-host-connection"
        assert session.app.worker_scheduler.max_queue == cfg["raven"]["resources"]["light"]["max_worker_queue"]
        assert session.app.worker_scheduler.multi_limit == cfg["raven"]["resources"]["light"]["max_workers"]
    finally:
        session.close()
    discover.assert_not_called()


def test_fixture_and_real_connection_never_share_observation_identity():
    cfg = Config({"raven": {"managed": True}})
    actual = cli.forecast_scope(cli.Backend(), cfg, "alice")
    first = cli.forecast_scope(cli.FakeBackend("alice"), cfg, "alice")
    second = cli.forecast_scope(cli.FakeBackend("alice"), cfg, "alice")
    assert actual["backend"] == "local"
    assert first["backend"] == second["backend"] == "demo"
    assert first != second and first != actual


def test_fixture_scope_stays_stable_for_one_backend_instance():
    cfg = Config({"raven": {"managed": True}})
    backend = cli.FakeBackend("alice")
    assert cli.forecast_scope(backend, cfg, "alice") == cli.forecast_scope(backend, cfg, "alice")


@pytest.mark.parametrize('requested_sessions, effective_sessions', [(1, 1), (8, 2)])
def test_managed_partial_preferences_keep_legacy_shell_and_layout(tmp_path, monkeypatch, requested_sessions, effective_sessions):
    import json
    value = resolved(tmp_path, monkeypatch)
    source = tmp_path / 'legacy.json'
    source.write_text(json.dumps({'terminal': {'shell': '/bin/sh', 'login': False,
        'history_lines': 321, 'max_sessions': requested_sessions}, 'workspace': {'split': 63},
        'plugins': ['/tmp/unqualified-plugin'], 'gpu_sampling': True}))
    value = resolve(str(source), system_policy_paths=[])
    observed = {}
    original_build = cli.build
    def build(args, cfg, **kwargs):
        cfg.set('collection_cache_dir', str(tmp_path / 'host-cache'))
        session = original_build(args, cfg, **kwargs)
        try:
            observed.update(session.app.cfg.data)
        finally:
            session.close()
        raise ValueError('built configuration checked and session closed')
    monkeypatch.setattr(cli, 'build', build)
    assert cli.main(['--fake', '--no-state', '--once'], raven=value) == 1
    assert observed['terminal']['shell'] == '/bin/sh'
    assert observed['terminal']['history_lines'] == 321
    assert observed['terminal']['max_sessions'] == effective_sessions
    assert observed['workspace']['split'] == 63
    assert observed['workspace']['density'] == 'comfortable'
    assert observed['plugins'] == [] and observed['gpu_sampling'] is False


@pytest.mark.parametrize("tab", ["jobs", "history", "nodes"])
def test_live_dashboard_primes_visible_snapshot_before_sampler_start(tmp_path, monkeypatch, tab):
    from raven.remote import LocalFiles
    from raven.sampler import Sampler
    from raven.slurm import FakeBackend

    value = resolved(tmp_path, monkeypatch, site="slurm")
    monkeypatch.setattr("raven.sampler.time.time", lambda: 1000.)
    monkeypatch.setattr("random.SystemRandom.uniform", lambda self, first, last: last)
    # Exercise the real non-demo launch path, retaining its 20% phase jitter,
    # while an in-process scheduler fixture keeps this test independent of Slurm.
    monkeypatch.setattr(cli, "make_backend", lambda args, cfg, user: (FakeBackend(user), LocalFiles(), None, user))
    starts = []
    def start(sampler):
        assert sampler._collection_demand["tab"] == tab
        assert sampler.due("jobs", 1000.)
        visible = "partitions" if tab == "nodes" else "finished"
        assert sampler.due(visible, 1000.)
        assert sampler.health("jobs").calls == 0
        assert sampler.collection["jitter_fraction"] == .2
        starts.append(sampler)
    monkeypatch.setattr(Sampler, "start", start)
    monkeypatch.setattr(cli.screen, "run_watch", lambda *args, **kwargs: None)

    assert cli.main(["--no-state", "--no-plugins", "--watch", "--tab", tab], raven=value) == 0
    assert len(starts) == 1
