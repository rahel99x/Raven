"""Setup never submits work and only explicit review/save changes configuration."""
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from raven import onboarding as O, onboarding_ui as U, raven_config as C
from raven import layout as L
from raven.config import Config
from raven.controller import App
from raven.model import Store


@pytest.fixture
def resolved(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("RAVEN_CONFIG", raising=False)
    monkeypatch.delenv("RAVEN_PROFILE", raising=False)
    monkeypatch.delenv("RAVEN_STATE_DIR", raising=False)
    monkeypatch.delenv("RAVEN_SITE_POLICY", raising=False)
    monkeypatch.delenv("RAVEN_WORKER_PYTHON", raising=False)
    monkeypatch.setattr(C, "SYSTEM_POLICIES", ())
    return C.resolve()


def test_first_run_is_interactive_bare_and_unconfigured_only(resolved):
    tty = SimpleNamespace(isatty=lambda: True)
    pipe = SimpleNamespace(isatty=lambda: False)
    assert O.should_offer_first_run(resolved, stdin=tty, stdout=tty, env={})
    for argv in (["--fake"], ["--once"], ["doctor"], ["--site", "desktop"], ["--help"], ["--no-state"]):
        assert not O.should_offer_first_run(resolved, argv, stdin=tty, stdout=tty, env={})
    assert not O.should_offer_first_run(resolved, stdin=pipe, stdout=tty, env={})
    assert not O.should_offer_first_run(resolved, stdin=tty, stdout=pipe, env={})
    assert not O.should_offer_first_run(resolved, stdin=tty, stdout=tty, env={"RAVEN_PROFILE": "desktop"})
    configured = C.Resolved(resolved.data, resolved.origins, (), (), source="/config.json")
    assert not O.should_offer_first_run(configured, stdin=tty, stdout=tty, env={})
    heavy = C.Resolved(C.merge(resolved.data, {"execution": {"startup": "heavy"}}), {}, (), ())
    assert O.should_offer_first_run(heavy, stdin=tty, stdout=tty, env={})


def test_cli_check_only_reports_presence_without_writing(resolved, capsys):
    assert O.run_setup(["--check", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["mutations"] == 0
    assert "sbatch" in report["tools"] and "sacctmgr" in report["tools"]
    assert "Unverified" in report["worker_readiness"]
    assert not Path(resolved.data["storage"]["state_dir"]).exists()


def test_cli_review_then_save_single_json_and_preserve_workload_separation(resolved, capsys):
    flags = ["--kind", "local-slurm", "--name", "LabX GPU experiments", "--account", "LabX", "--json"]
    assert O.run_setup(flags + ["--review"]) == 0
    review = json.loads(capsys.readouterr().out)
    path = Path(review["path"])
    assert not path.exists()
    assert review["effective"]["allocation"]["account"] == "LabX"
    assert O.run_setup(flags + ["--save"]) == 0
    saved = json.loads(capsys.readouterr().out)
    assert saved["profile"] == review["profile"]
    document = json.loads(path.read_text())
    assert document["profile"] == saved["profile"]
    assert document["profiles"][saved["profile"]]["allocation"]["account"] == "LabX"
    assert not Path(resolved.data["storage"]["state_dir"]).exists()


def test_ssh_review_never_copies_local_interpreter_or_claims_verified(resolved, capsys):
    assert O.run_setup(["--kind", "ssh-slurm", "--name", "HPC3", "--host", "hpc3", "--review", "--json"]) == 0
    draft = json.loads(capsys.readouterr().out)
    assert draft["effective"]["execution"]["worker_python"] == ""
    assert draft["effective"]["connection"]["host"] == "hpc3"
    assert any("unverified" in line for line in O.preview_lines(draft))


def test_cancelled_interactive_preview_leaves_configuration_absent(resolved, capsys):
    answers = iter(["1", "My workstation", "", "", "", "n"])
    assert O.run_setup([], input_fn=lambda prompt: next(answers)) == 0
    assert "Configuration unchanged" in capsys.readouterr().out
    assert not (Path.home() / "config/raven/config.json").exists()


def test_explicit_progress_can_resume_after_process_exit(resolved, capsys):
    form = O.default_form(resolved)
    form.update(name="LabX", account="labx", cluster="campus")
    path = O.save_progress(resolved, form, False)
    assert Path(path).stat().st_mode & 0o077 == 0
    assert O.load_progress(resolved)["form"] == form
    assert O.run_setup(["--resume", "--review", "--json"]) == 0
    draft = json.loads(capsys.readouterr().out)
    assert draft["effective"]["site"]["cluster_id"] == "campus"
    assert not draft["make_default"]
    assert not Path(draft["path"]).exists()


def test_help_does_not_read_invalid_config(monkeypatch):
    monkeypatch.setattr(C, "resolve", lambda *a, **k: pytest.fail("Help attempted to load configuration"))
    with pytest.raises(SystemExit) as exc:
        O.run_setup(["--help"])
    assert exc.value.code == 0


def test_noninteractive_setup_requires_explicit_action(resolved, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO())
    assert O.run_setup([]) == 2
    assert "needs a terminal" in capsys.readouterr().err


class Background:
    demo = False

    def __init__(self):
        self.pending = None

    def start_task(self, action, completed):
        if self.pending:
            return False
        self.pending = action, completed
        return True

    def finish(self):
        action, done = self.pending
        self.pending = None
        try:
            value = action()
        except Exception as exc:
            value = exc
        done(value)


@pytest.fixture
def app(resolved):
    cfg = Config()
    cfg.set("raven", resolved.as_dict())
    value = App(Store(persist=False), None, None, cfg, "student")
    value.width, value.height = 80, 24
    value.raven_resolved = resolved
    value.research = Background()
    return value


def render(app):
    return U.overlay(SimpleNamespace(g=L.Glyphs(False)), {}, app, app.width, app.height)


def test_welcome_render_and_pointer_do_not_query_or_write(app, monkeypatch):
    monkeypatch.setattr(O, "installation_report", lambda *_: pytest.fail("Paint performed checks"))
    U.open_setup(app)
    for _ in range(3):
        assert render(app)
        U.handle_mouse(app, 12, 20, button="motion")
    assert app.research.pending is None
    assert app.onboarding_ui_state["form"]["kind"] == "local-slurm"
    U.handle_key(app, "esc")
    assert app.mode == "main"


def test_friendly_label_keyboard_and_paste_accept_spaces_without_commands(app):
    U.open_setup(app)
    U.run_command(app, ["gettingstarted", "kind", "local-slurm"])
    U.run_command(app, ["gettingstarted", "edit", "name"])
    U.handle_key(app, "ctrl-u")
    assert U.paste(app, "LabX")
    U.handle_key(app, "space")
    U.handle_key(app, "G")
    assert U.paste(app, "PU")
    assert U.paste(app, "\n:quit")
    U.handle_key(app, "enter")
    assert app.onboarding_ui_state["form"]["name"] == "LabX GPU"
    assert app.mode == "onboarding"


def test_review_save_and_connect_are_three_distinct_actions(app):
    U.open_setup(app)
    U.run_command(app, ["gettingstarted", "kind", "standalone"])
    U.run_command(app, ["gettingstarted", "review"])
    state = app.onboarding_ui_state
    assert state["draft"] is None
    app.research.finish()
    path = Path(state["draft"]["path"])
    assert state["page"] == "review" and not path.exists()
    U.run_command(app, ["gettingstarted", "save"])
    assert not path.exists()
    app.research.finish()
    assert path.exists() and state["page"] == "saved"
    assert app.raven_resolved.data["site"]["kind"] == "slurm"
    assert not app.switch_profile and not app.quit
    U.run_command(app, ["gettingstarted", "connect"])
    assert app.switch_profile == state["saved"]["profile"]
    assert app.switch_profile_config == str(path) and app.quit


@pytest.mark.parametrize("worker_state", ["preparing", "pending", "starting", "heavy", "draining", "reconciling"])
def test_use_profile_blocks_active_heavy(app, worker_state):
    U.open_setup(app)
    app.onboarding_ui_state.update(page="saved", saved={"profile": "labx", "path": "/tmp/config.json"})
    app.raven_allocation_manager = SimpleNamespace(snapshot=lambda: {"state": worker_state})
    U.run_command(app, ["gettingstarted", "connect"])
    assert not app.quit and "Release" in app.onboarding_ui_state["message"]


@pytest.mark.parametrize("launch", [{"busy": "Submitting"}, {"pending": "review"}])
def test_use_profile_blocks_unresolved_workload_launch(app, launch):
    U.open_setup(app)
    app.onboarding_ui_state.update(page="saved", saved={"profile": "labx", "path": "/tmp/config.json"})
    app.launch_wizard_state = launch
    U.run_command(app, ["gettingstarted", "connect"])
    assert not app.quit and "current launch" in app.onboarding_ui_state["message"]


def test_closed_async_review_does_not_reopen_or_save(app):
    U.open_setup(app)
    U.run_command(app, ["gettingstarted", "review"])
    U.handle_key(app, "esc")
    app.research.finish()
    assert app.mode == "main"
    assert not Path(app.onboarding_ui_state["draft"]["path"]).exists()


def test_demo_never_saves_a_profile(app):
    U.open_setup(app)
    U.run_command(app, ["gettingstarted", "review"])
    app.research.finish()
    app.research.demo = True
    U.run_command(app, ["gettingstarted", "save"])
    assert app.research.pending is None
    assert not Path(app.onboarding_ui_state["draft"]["path"]).exists()


def test_demo_button_hands_off_without_discovery_or_configuration_writes(app, monkeypatch):
    monkeypatch.setattr(U, "_service", lambda _: pytest.fail("Demo attempted profile access"))
    U.open_setup(app)
    U.run_command(app, ["gettingstarted", "demo"])
    assert not app.quit and not getattr(app, "start_demo", False)
    render(app)
    y, _, control = next(hit for hit in app.onboarding_ui_state["control_hits"]
                         if hit[2]["action"] == ("command", "gettingstarted open-demo"))
    U.handle_mouse(app, y, control["left"])
    assert app.start_demo and app.quit
    assert app.research.pending is None and not app.switch_profile
    assert not Path(app.raven_resolved.data["storage"]["state_dir"]).exists()


@pytest.mark.parametrize("blocker", ["pending", "heavy", "launch", "job-review", "worker-review"])
def test_demo_does_not_interrupt_compute_or_pending_submission(app, blocker):
    U.open_setup(app)
    U.run_command(app, ["gettingstarted", "demo"])
    if blocker in ("pending", "heavy"):
        app.raven_allocation_manager = SimpleNamespace(snapshot=lambda: {"state": blocker})
    elif blocker == "launch":
        app.launch_wizard_state = {"busy": True}
    elif blocker == "job-review":
        app.execution_state = {"pending_action": {"command": "submit"}}
    else:
        app.raven_ui_state = {"review": {"account": "labx"}}
    U.run_command(app, ["gettingstarted", "open-demo"])
    assert not getattr(app, "start_demo", False) and not app.quit
    assert "before opening the offline demo" in app.onboarding_ui_state["message"]


def test_changed_config_is_rejected_after_review(app):
    U.open_setup(app)
    U.run_command(app, ["gettingstarted", "review"])
    app.research.finish()
    path = Path(app.onboarding_ui_state["draft"]["path"])
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema": C.SCHEMA, "ui": {"theme": "user-choice"}}))
    U.run_command(app, ["gettingstarted", "save"])
    app.research.finish()
    assert json.loads(path.read_text())["ui"]["theme"] == "user-choice"
    assert "changed" in app.onboarding_ui_state["message"]
