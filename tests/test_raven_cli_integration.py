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
    resolved_context = []
    original_build = cli.build
    def build(args, cfg, **kwargs):
        cfg.set('collection_cache_dir', str(tmp_path / 'host-cache'))
        session = original_build(args, cfg, **kwargs)
        try:
            observed.update(session.app.cfg.data)
            resolved_context.append(session.app.raven_resolved)
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
    assert resolved_context == [value]
    assert resolved_context[0] is value


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


def test_setup_rendered_account_save_updates_real_cli_profile_without_allocating(tmp_path, monkeypatch):
    """Exercise the bootstrap, governed discovery, background save, and mouse path."""
    import copy
    import json
    import os
    import pwd
    import threading

    from raven import setup_ui, workbench
    from raven.remote import LocalFiles
    from raven.sampler import Sampler

    resolved(tmp_path, monkeypatch, site="slurm")
    username = pwd.getpwuid(os.getuid()).pw_name
    source = tmp_path / "profiles.json"
    original = {
        "schema": "raven.config/v1",
        "profile": "campus",
        "ui": {"theme": "amber"},
        "storage": {"coordination_dir": str(tmp_path / "host-cache")},
        "profiles": {
            "campus": {"allocation": {"account": "personal", "partition": "cpu"}},
            "other-campus": {"allocation": {"account": "other-lab", "partition": "gpu"}},
        },
    }
    source.write_text(json.dumps(original))
    value = resolve(str(source), system_policy_paths=[])
    calls, worker_threads, visited = [], [], []
    main_thread = threading.get_ident()

    class SchedulerFixture:
        def run(self, command, timeout=8.):
            calls.append(list(command))
            worker_threads.append(threading.get_ident())
            if command[0] == "sacctmgr":
                assert "name=" + username in command
                return (f"{username}|personal|campus|personal||normal|normal\n"
                        f"{username}|personal|campus|lab-x|cpu|normal|normal\n"), .001
            if command == ["sinfo", "--noheader", "--format=%P"]:
                return "cpu*\n", .001
            if command == ["scontrol", "show", "config"]:
                return "ClusterName = campus\n", .001
            raise AssertionError("Unexpected scheduler command: " + repr(command))

    backend = SchedulerFixture()
    monkeypatch.setattr(cli, "make_backend", lambda args, cfg, user: (backend, LocalFiles(), None, username))
    # The live sampler is unrelated to this explicit Setup flow. The real
    # ResearchHub and its governed background worker remain enabled.
    monkeypatch.setattr(Sampler, "start", lambda _: None)
    launch = Mock(side_effect=AssertionError("Saving Setup must not launch an allocation or process"))
    monkeypatch.setattr("raven.raven_allocation.subprocess.Popen", launch)

    def screen(app, views, sampler, store, actions, cfg, **kwargs):
        assert isinstance(cfg, Config)
        assert app.raven_resolved is value
        assert not app.demo and not app.research.demo
        assert app.actions.slurm.b.ledger is not None
        app.width, app.height = 120, 48

        def paint():
            snapshot = store.snapshot()
            views.compose(snapshot, app, app.width, app.height)
            views.overlay(snapshot, app, app.width, app.height)

        def click(command):
            paint()
            y, _, control = next(hit for hit in app.setup_ui_state["control_hits"]
                                 if hit[2]["action"] == ("command", command))
            assert workbench.handle_mouse(app, y, control["left"])

        def click_action(command):
            index = next(index for index, item in enumerate(setup_ui._actions(app))
                         if item[1] == command)
            click("setup action " + str(index))

        def finish_background():
            assert app.research.pending is not None
            app.research.pending[0].result(timeout=5)
            app.research.poll_task()
            assert app.research.pending is None
            assert not app.setup_ui_state["busy"]

        app.run_command("menu Setup")
        paint()
        y, left, _, _ = next(hit for hit in app.toolbar_state["menu_hits"] if hit[3] == "setup-connections")
        assert workbench.handle_mouse(app, y, left)
        paint()
        y, left, _, _ = next(hit for hit in app.toolbar_state["menu_hits"] if hit[3] == "setup-allocation")
        assert workbench.handle_mouse(app, y, left)
        assert app.mode == "setup"
        finish_background()
        discovery = app.setup_ui_state["discovery"]
        assert discovery["username"] == username
        assert discovery["cluster"] == "campus"
        assert not discovery["errors"]
        assert len(calls) == 3
        assert all(thread != main_thread for thread in worker_threads)
        before = copy.deepcopy(app.raven_allocation_manager.config)

        click_action("setup accounts")
        lab_index = next(index for index, row in enumerate(discovery["accounts"]) if row["account"] == "lab-x")
        click("setup account " + str(lab_index))
        click("setup edit cpus_per_task")
        assert workbench.handle_key(app, "ctrl-u")
        assert workbench.handle_key(app, "4")
        assert workbench.handle_key(app, "enter")
        assert app.raven_allocation_manager.config == before
        assert json.loads(source.read_text()) == original

        click_action("setup save")
        finish_background()
        assert "Saved allocation settings" in app.message
        assert "No allocation was requested" in app.message
        allocation = cfg.get("raven.allocation")
        assert allocation["account"] == "lab-x" and allocation["cpus_per_task"] == 4
        assert app.raven_allocation_manager.config["allocation"] == allocation
        assert app.raven_resolved.data["allocation"] == allocation
        assert cfg.raven_resolved is app.raven_resolved
        # Allocation preferences do not silently retarget the active Group /
        # Budget observations, whose account was captured by the sampler.
        assert cfg.get("account") == sampler.account == "personal"
        reloaded = resolve(str(source), system_policy_paths=[])
        assert reloaded.data["allocation"] == allocation
        expected = copy.deepcopy(original)
        expected["profiles"]["campus"]["allocation"].update(account="lab-x", cpus_per_task=4)
        assert json.loads(source.read_text()) == expected
        assert app.raven_allocation_manager.snapshot()["state"] == "light"
        assert app.raven_allocation_manager._process is None
        assert not app.setup_ui_state["dirty"]
        assert len(calls) == 3
        visited.append(True)

    monkeypatch.setattr(cli.screen, "run_watch", screen)
    assert cli.main(["--no-state", "--no-plugins", "--watch"], raven=value) == 0
    assert visited == [True]
    launch.assert_not_called()
