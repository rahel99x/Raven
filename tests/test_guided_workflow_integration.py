"""Entry points and shared UI routing for setup and reviewed job launching."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from raven import raven_config, raven_launch, toolbar, workbench, screen
from raven.config import Config
from raven.controller import App
from raven.layout import Glyphs, row_text, vlen
from raven.model import Store


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for name in ("RAVEN_CONFIG", "RAVEN_PROFILE", "RAVEN_SITE_POLICY", "RAVEN_STATE_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(raven_config, "SYSTEM_POLICIES", ())


@pytest.mark.parametrize("entry", [raven_launch.light_main, raven_launch.heavy_main])
def test_setup_routes_nested_flags_before_config_or_allocation(monkeypatch, entry):
    from raven import onboarding
    setup = Mock(return_value=0)
    monkeypatch.setattr(onboarding, "run_setup", setup)
    monkeypatch.setattr(raven_launch, "resolve", Mock(side_effect=AssertionError("bootstrap config read")))
    monkeypatch.setattr(raven_launch, "_allocation", Mock(side_effect=AssertionError("allocation requested")))
    assert entry(["--config", "/missing.json", "--site", "labx", "setup", "--help"]) == 0
    setup.assert_called_once_with(["--help"], config_path="/missing.json", site="labx")


def test_setup_global_json_is_forwarded(monkeypatch):
    from raven import onboarding
    setup = Mock(return_value=0)
    monkeypatch.setattr(onboarding, "run_setup", setup)
    assert raven_launch.light_main(["--json", "setup", "--check"]) == 0
    setup.assert_called_once_with(["--check", "--json"], config_path=None, site=None)


def test_explicit_getting_started_does_not_request_heavy(monkeypatch):
    from raven import cli
    monkeypatch.setattr(raven_launch, "_allocation", Mock(side_effect=AssertionError("setup cannot allocate")))
    dashboard = Mock(return_value=0)
    monkeypatch.setattr(cli, "main", dashboard)
    assert raven_launch.heavy_main(["--getting-started"]) == 0
    assert dashboard.call_args.args[0] == ["--getting-started"]
    assert dashboard.call_args.kwargs["raven"].data["execution"]["startup"] == "light"


@pytest.mark.parametrize("prefix", [["--account", "labx"], ["--host", "cluster"], ["--plan"], ["--write-config"], ["--site-policy", "/policy.json"]])
def test_setup_does_not_silently_ignore_bootstrap_overrides(monkeypatch, prefix):
    from raven import onboarding
    setup = Mock(side_effect=AssertionError("unsupported prefix cannot be ignored"))
    monkeypatch.setattr(onboarding, "run_setup", setup)
    # --write-config has an optional operand; '=' makes the boundary explicit.
    if prefix == ["--write-config"]:
        prefix = ["--write-config=/unused.json"]
    assert raven_launch.light_main([*prefix, "setup", "--check"]) == 2


@pytest.mark.parametrize("entry", [raven_launch.light_main, raven_launch.heavy_main])
def test_fresh_interactive_setup_opens_light_before_any_heavy_request(monkeypatch, entry):
    from raven import cli, onboarding
    monkeypatch.setattr(onboarding, "should_offer_first_run", lambda *args, **kwargs: True)
    monkeypatch.setattr(raven_launch, "_allocation", Mock(side_effect=AssertionError("setup must precede allocation")))
    start = Mock(return_value=0)
    monkeypatch.setattr(cli, "main", start)
    assert entry([]) == 0
    assert start.call_args.args[0] == ["--getting-started"]
    assert start.call_args.kwargs["raven"].data["execution"]["startup"] == "light"
    assert start.call_args.kwargs["raven_allocation"] is None


def test_explicit_profile_reconnect_rebuilds_light_session_and_keeps_privacy(monkeypatch, tmp_path):
    from raven import cli
    from raven.setup_profiles import ProfileSetup
    first = raven_config.resolve()
    profiles = ProfileSetup(first)
    saved = profiles.save(profiles.stage("Standalone", "standalone"))
    sessions = []

    def run(argv, *, raven, raven_allocation):
        sessions.append((list(argv), raven, raven_allocation))
        if len(sessions) == 1:
            raise cli._Reconnect(saved["path"], saved["profile"])
        return 7

    monkeypatch.setattr(cli, "_managed_main", run)
    assert cli.main(["--getting-started", "--no-state", "--no-plugins"], raven=first) == 7
    assert sessions[1][0] == ["--no-state", "--no-plugins"]
    assert sessions[1][1].data["site"]["kind"] == "desktop"
    assert sessions[1][1].data["execution"]["startup"] == "light"
    assert sessions[1][2] is None


def test_demo_restarts_light_with_simulated_input_and_no_user_state(monkeypatch):
    from raven import cli
    first = raven_config.resolve()
    sessions = []

    def run(argv, *, raven, raven_allocation):
        sessions.append((list(argv), raven, raven_allocation))
        if len(sessions) == 1:
            raise cli._OpenDemo()
        return 0

    monkeypatch.setattr(cli, "_managed_main", run)
    assert cli.main([], raven=first) == 0
    assert sessions[1][0] == ["--fake", "--no-state", "--no-plugins"]
    assert sessions[1][1].data["execution"]["startup"] == "light"
    assert sessions[1][2] is None


def test_profile_reconnect_retains_explicit_extra_site_policy(tmp_path):
    import json
    from raven import cli
    from raven.setup_profiles import ProfileSetup
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"schema": raven_config.POLICY_SCHEMA,
                                  "restrictions": {"allow_desktop": False}}))
    initial = raven_config.resolve(site_policy_path=str(policy))
    setup = ProfileSetup(initial)
    saved = setup.save(setup.stage("LabX", "local-slurm", heavy={"account": "labx"}))
    new = cli._reconnect_config(initial, cli._Reconnect(saved["path"], saved["profile"]))
    assert any(item.get("rules", {}).get("allow_desktop") is False for item in new.restrictions)
    from raven.setup_config import AllocationSetup
    allocation = AllocationSetup(new)
    updated = allocation.save(allocation.stage({"cpus_per_task": 3}))
    assert updated["resolved"].extra_policy_path == str(policy)


def test_explicit_policy_remains_when_it_also_belongs_to_original_profile(tmp_path):
    import json
    from raven import cli
    policy = tmp_path / "shared-policy.json"
    policy.write_text(json.dumps({"schema": raven_config.POLICY_SCHEMA,
                                  "restrictions": {"deny_heavy": True}}))
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"schema": raven_config.SCHEMA, "profile": "one",
        "profiles": {"one": {"site": {"policy_file": str(policy)}},
                     "two": {"site": {"policy_file": ""}}}}))
    initial = raven_config.resolve(str(config), site="one", site_policy_path=str(policy))
    for updated in (cli._reconnect_config(initial, cli._Reconnect(str(config), "two")),
                    raven_config.reload_config(initial, site="two")):
        assert updated.extra_policy_path == str(policy)
        assert "heavy-analysis" not in updated.data["execution"]["capabilities"]


def test_fresh_heavy_setup_can_choose_billing_before_required_account_policy(monkeypatch, tmp_path):
    import json
    from raven import cli, onboarding
    policy = tmp_path / "mandatory.json"
    policy.write_text(json.dumps({"schema": raven_config.POLICY_SCHEMA,
                                  "restrictions": {"require_account": True}}))
    monkeypatch.setattr(raven_config, "SYSTEM_POLICIES", (str(policy),))
    read_file = raven_config.read_file
    # The fixture models an administrator-owned policy without requiring root
    # ownership of the test runner's private temporary directory.
    monkeypatch.setattr(raven_config, "read_file", lambda path, **kwargs: read_file(path))
    monkeypatch.setattr(onboarding, "should_offer_first_run", lambda *args, **kwargs: True)
    monkeypatch.setattr(raven_launch, "_allocation", Mock(side_effect=AssertionError("no allocation")))
    dashboard = Mock(return_value=0)
    monkeypatch.setattr(cli, "main", dashboard)
    assert raven_launch.heavy_main([]) == 0
    assert dashboard.call_args.args[0] == ["--getting-started"]


def test_launch_and_setup_are_discoverable_in_menus_and_palette():
    app = App(Store(persist=False), None, None, Config(), "student")
    known = set(app.commands())
    assert {"launchjob", "gettingstarted", "profiles", "connectdata", "runhome", "refreshsettings"} <= known
    assert toolbar.menu_items(app, "File")[0].command == "launchjob"
    assert len(toolbar.menu_items(app, "Setup")) == 5
    assert "profiles" in {item.command for item in toolbar.submenu_items(app, "connections")}
    assert "gettingstarted" in {item.command for item in toolbar.submenu_items(app, "installation")}
    assert workbench.INSPECTION_MODES["launch_job"] == "launch_wizard_ui"
    assert workbench.INSPECTION_MODES["onboarding"] == "onboarding_ui"


def test_primary_launch_button_opens_only_the_wizard(monkeypatch):
    from raven import launch_wizard_ui
    app = App(Store(persist=False), None, None, Config(), "student")
    opened = Mock()
    monkeypatch.setattr(launch_wizard_ui, "open_launch", opened)
    views = SimpleNamespace(g=Glyphs(True))
    row = toolbar.render_bar(views, app, 120)
    assert vlen(row_text(row)) == 120
    hit = next(hit for hit in app.toolbar_state["hits"] if hit[3] == "launch-job")
    assert toolbar.handle_mouse(app, hit[0], hit[1])
    opened.assert_called_once_with(app)


def test_toolbar_keyboard_labels_explain_the_visible_actions():
    from raven import interaction
    app = App(Store(persist=False), None, None, Config(), "student")
    toolbar.render_bar(SimpleNamespace(g=Glyphs(True)), app, 80)
    labels = {control.id: control.label for control in interaction._toolbar_controls(app)}
    assert labels["toolbar:menu:0"] == "File"
    assert labels["toolbar:launch-job:None"] == "Launch job"
    assert labels["toolbar:refresh-settings:None"].startswith("Refresh settings: Queue:")


def test_launch_receipt_opens_the_continuous_exact_run_home(monkeypatch):
    from raven import launch_wizard_ui, run_home_ui
    app = App(Store(persist=False), None, None, Config(), "student")
    launch_wizard_ui.open_launch(app)
    receipt = {"project_root": "/project", "run_id": "trial-a2", "job_id": "123", "token": "unique-attempt"}
    app.launch_wizard_state.update(stage="done", receipt=receipt)
    opened = Mock()
    monkeypatch.setattr(run_home_ui, "open_receipt", opened)
    assert workbench.run_command(app, ["launchjob", "action", "open-run"])
    opened.assert_called_once_with(app, receipt)
    assert opened.call_args.args[1] is not receipt


def test_run_again_enters_review_flow_without_writing_project_defaults(monkeypatch):
    from raven import launch_wizard_ui
    app = App(Store(persist=False), None, None, Config(), "student")
    app.mode = "run_home"
    draft = {"project": "/project", "script": "batch.sh", "captured_plan": {
        "effective_options": {"account": "lab-x"}, "settings": {"recordings": {}}}}
    assert launch_wizard_ui.open_from_run(app, draft)
    state = app.launch_wizard_state
    assert app.mode == "launch_job" and state["stage"] == "resources"
    assert state["review"] is None and state["receipt"] is None
    assert not state["busy"] and state["pending"] is None and not state["attempted"]
    assert state["draft"]["captured_plan"] == draft["captured_plan"]
    state["draft"]["captured_plan"]["effective_options"]["account"] = "another-lab"
    assert draft["captured_plan"]["effective_options"]["account"] == "lab-x"
    save = Mock(side_effect=AssertionError("must not overwrite project defaults"))
    monkeypatch.setattr(launch_wizard_ui, "_save_recipe", save)
    workbench.run_command(app, ["launchjob", "action", "save-recipe"])
    assert "prior run" in state["error"]
    save.assert_not_called()


@pytest.mark.parametrize("mode,module_name", [("onboarding", "onboarding_ui"), ("launch_job", "launch_wizard_ui"), ("launch_setup", "data_export_ui"), ("connect_data", "connect_data_ui")])
def test_paste_is_routed_to_the_visible_form(monkeypatch, mode, module_name):
    from importlib import import_module
    app = App(Store(persist=False), None, None, Config(), "student")
    app.mode = mode
    paste = Mock()
    monkeypatch.setattr(import_module("raven." + module_name), "paste", paste)
    screen._apply_input(app, ("paste", "/a project/job.sh"), [], SimpleNamespace())
    paste.assert_called_once_with(app, "/a project/job.sh")
