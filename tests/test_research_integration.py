"""Research workflows through the terminal, palette, and actual CLI boundary."""
import json
from pathlib import Path
import shlex
import subprocess
import threading
import time

import pytest

from raven import cli, layout as L
from raven.config import Config
from raven.metrics import write_metric
from raven.model import Finished, Job
from raven.provenance import capture
from raven.research import RESEARCH_VIEWS
from raven.submission import prepare


def finish_research_command(app):
    pending = app.research.pending
    if pending:
        pending[0].result(timeout=5)
        app.research.poll_task()


def is_submission(command):
    tokens = shlex.split(command[2]) if command[:2] == ["sh", "-c"] else command
    return "sbatch" in tokens and "--test-only" not in tokens


@pytest.fixture
def research_files(tmp_path):
    script = tmp_path / "train model.sbatch"
    script.write_text("#!/bin/bash\n#SBATCH --time=00:10:00\n#SBATCH --output=slurm-%A_%a.out\necho simulation\n")
    metrics = tmp_path / "metrics.jsonl"
    for i in range(12):
        write_metric(metrics, {"loss": 1 / (i + 1), "accuracy": i / 12}, step=i, phase="训练",
                     completed=i + 1, total=20, unit="steps", t=100 + i * 5)
    output = tmp_path / "résult.json"
    output.write_text('{"accuracy":0.8}')
    contract = tmp_path / "outputs.json"
    contract.write_text(json.dumps({"version": 1, "outputs": [{"path": output.name, "format": "json", "required_keys": ["accuracy"]}]}))
    stdout = tmp_path / "stdout.txt"
    stdout.write_text("Starting training\n\x1b[31mRuntimeError: CUDA out of memory\x1b[0m\n")
    cfg = tmp_path / "config.json"
    cfg.write_text("{}")
    return {"root": tmp_path, "script": script, "metrics": metrics, "contract": contract, "stdout": stdout, "config": cfg}


@pytest.fixture
def research_dashboard(research_files):
    session = cli.build(cli.parse(["--fake", "--no-state", "--no-plugins"]), Config())
    app, store, hub = session.app, session.store, session.app.research
    session.app.tab = "research"
    store.jobs = [Job("900_[0-19]", "模型", "main", "PENDING"), Job("900_3", "模型", "main", "RUNNING", elapsed="00:02:00")]
    store.finished = [Finished("900_2", "模型", "FAILED", elapsed="00:01:00", exit="1:0")]
    store.details["900_2"] = {"StdOut": str(research_files["stdout"]), "StdErr": str(research_files["stdout"])}
    app.research_job_id = "900_2"
    hub.configure(metrics_file=str(research_files["metrics"]), contract=str(research_files["contract"]), workdir=str(research_files["root"]))
    hub.passport = capture(research_files["root"], script=str(research_files["script"]), parameters={"name": "模型"})
    hub.plan = prepare(research_files["script"], workdir=research_files["root"], overrides=["--job-name=模型 train"])
    yield session, research_files
    session.close()


@pytest.mark.parametrize("ascii_", [False, True])
@pytest.mark.parametrize("width,height", [(0, 0), (1, 1), (24, 10), (40, 16), (104, 32), (160, 50)])
def test_all_six_research_views_fit_and_keep_external_text_control_free(research_dashboard, ascii_, width, height):
    session, _ = research_dashboard
    app, hub = session.app, session.app.research
    session.views.set_ascii(ascii_)
    app._ascii_cfg = ascii_
    for view, _ in RESEARCH_VIEWS:
        app.research_view, app.research_scroll = view, 0
        result = hub.request(hub.context(session.store.snapshot(), app), wait=True, force=True)
        assert result.get("status") not in {"loading", "error", "empty"}, (view, result)
        rows, hits = session.views.compose(session.store.snapshot(), app, width, height, session.actions)
        assert len(rows) == height
        assert all(L.vlen(L.row_text(row)) <= width for row in rows)
        assert all(0 <= y < max(0, height - 1) for y, _, _ in hits)
        text = "\n".join(L.row_text(row) for row in rows)
        assert all(char.isprintable() or char == "\n" for char in text)
        if ascii_:
            assert text.isascii(), (view, text)
    if width >= 40 and height >= 16:
        assert any(hit[3] == "research" for hit in app.tab_hits)


def test_research_palette_view_keys_array_selection_scroll_and_mouse_hits(research_dashboard):
    session, files = research_dashboard
    app, hub = session.app, session.app.research
    app.enter_tab("research")
    app.research_view = "experiment"
    app.handle("right")
    assert app.research_view == "arrays"
    app.handle("left")
    assert app.research_view == "experiment"
    app.run_command("view arrays")
    assert app.command_ok and app.research_view == "arrays"
    session.store.jobs += [Job(f"{1000 + i}_[0-99]", f"array{i}", "main", "PENDING") for i in range(20)]
    hub.request(hub.context(session.store.snapshot(), app), wait=True, force=True)
    session.views.compose(session.store.snapshot(), app, 120, 32, session.actions)
    for _ in range(20):
        app.handle("down")
    rows, hits = session.views.compose(session.store.snapshot(), app, 120, 32, session.actions)
    assert app.cursor["research"] == 20
    assert any(key == "1019" for _, kind, key in hits if kind == "research_array"), L.to_text(rows, 120)
    app.handle("enter")
    assert app.research_array_open
    app.handle("pgdn")
    assert app.research_task_offset > 0
    app.handle("home")
    rows, hits = session.views.compose(session.store.snapshot(), app, 120, 32, session.actions)
    hit = next((hit for hit in hits if hit[1] == "research_array"), None)
    assert hit is not None
    app.click(hit[0], 3, hits)
    assert app.research_groups[app.cursor["research"]]["id"] == hit[2]
    app.run_command("view experiment")
    hub.request(hub.context(session.store.snapshot(), app), wait=True)
    session.views.compose(session.store.snapshot(), app, 120, 20, session.actions)
    app.handle("pgdn")
    assert app.research_scroll > 0
    app.handle("home")
    assert app.research_scroll == 0
    assert app.palette_complete("prep") == "prepare "
    app.mode, app.palette_edit = "palette", "metrics " + shlex.quote(str(files["metrics"]))
    app.handle("enter")
    assert app.mode == "main" and app.command_ok


def test_passport_comparison_is_visible_in_research(research_dashboard):
    session, files = research_dashboard
    from raven.provenance import save
    left = capture(files["root"], script=str(files["script"]), parameters={"size": 1})
    right = capture(files["root"], script=str(files["script"]), parameters={"size": 2})
    left_path = save(left, files["root"] / "passports")
    right_path = save(right, files["root"] / "passports")
    session.app.run_command("passport compare " + shlex.join([str(left_path), str(right_path)]))
    finish_research_command(session.app)
    session.app.research.request(session.app.research.context(session.store.snapshot(), session.app), wait=True)
    rows, _ = session.views.compose(session.store.snapshot(), session.app, 160, None, session.actions)
    text = L.to_text(rows, 160)
    assert "/parameters/size" in text and "1" in text and "2" in text


@pytest.mark.parametrize("command", ["prepare", "metric", "validate", "passport"])
def test_offline_cli_commands_never_create_a_scheduler_backend(research_files, monkeypatch, capsys, command):
    files = research_files
    def forbidden(*args, **kwargs):
        pytest.fail("offline research command created a scheduler backend")
    monkeypatch.setattr(cli, "make_backend", forbidden)
    if command == "prepare":
        words = ["prepare", str(files["script"]), "--workdir", str(files["root"]), "--account", "submit-account", "--job-name", "a training model"]
    elif command == "metric":
        words = ["metric", str(files["metrics"]), "--value", "loss=0.2", "--step", "13"]
    elif command == "validate":
        words = ["validate", str(files["contract"]), str(files["root"])]
    else:
        words = ["passport", "capture", str(files["script"]), "--workdir", str(files["root"]), "--output-dir", str(files["root"] / "passports")]
    result = cli.main(["--config", str(files["config"]), "--no-state", "--no-plugins", "run", *words])
    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    if command == "prepare":
        assert payload["valid"]
        assert payload["resources"]["account"] == "submit-account"
        assert payload["resources"]["job_name"] == "a training model"
        assert payload["argv"][-1] == str(files["script"])


def test_offline_cli_invalid_plan_and_remote_rejection_still_never_build_backend(research_files, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail("offline rejection started a scheduler backend")
    monkeypatch.setattr(cli, "make_backend", forbidden)
    assert cli.main(["--config", str(research_files["config"]), "run", "prepare", str(research_files["root"] / "missing.sbatch")]) == 1
    capsys.readouterr()
    assert cli.main(["--config", str(research_files["config"]), "--host", "not-a-real-cluster", "run", "prepare", str(research_files["script"])]) == 1
    assert "local" in capsys.readouterr().out.lower()


def test_cli_keeps_submission_flags_quotes_and_alias_glyph_overrides(research_files):
    words = ["--account", "monitor-account", "--unicode", "run", "prepare", str(research_files["script"]),
             "--account", "submit account", "--job-name", "a model's training", "--ascii", "--fake"]
    args = cli.parse(words)
    assert args.account == "monitor-account"
    assert args.ascii is True
    assert args.fake
    assert shlex.split(args.run) == ["prepare", str(research_files["script"]), "--account", "submit account", "--job-name", "a model's training"]
    assert cli.parse(["--ascii", "run", "prepare", str(research_files["script"]), "--unicode"]).ascii is False


def test_submit_and_array_retry_require_confirmation_and_never_mutate_before_yes(research_dashboard, monkeypatch):
    session, files = research_dashboard
    app = session.app
    calls = []
    original = session.backend.call
    def recording(command, *args, **kwargs):
        calls.append(list(command))
        return original(command, *args, **kwargs)
    monkeypatch.setattr(session.backend, "call", recording)
    app.run_command("submit " + shlex.join([str(files["script"]), "--workdir", str(files["root"])]))
    finish_research_command(app)
    assert app.mode == "confirm" and app.confirm["action"] == "submit"
    assert calls == []
    app.handle("n")
    assert app.mode == "main" and calls == []
    session.store.jobs = [Job("900_2", "running", "main", "RUNNING")]
    session.store.finished = [Finished("900_[0-9]", "failed", "FAILED"), Finished("900_1", "done", "COMPLETED")]
    app.interactive = False
    app.run_command("array retry 900 " + shlex.join([str(files["script"]), "--workdir", str(files["root"]), "--limit", "3"]))
    assert app.mode == "confirm" and app.command_ok
    assert app.confirm["plan"]["array_retry"]["indices"] == "0,3-9%3"
    assert calls == []
    app.finish_confirm(False)
    assert calls == []
    app.run_command("array retry 900 " + shlex.join([str(files["script"]), "--workdir", str(files["root"]), "--indices", "2"]))
    assert not app.command_ok and app.mode != "confirm" and calls == []


@pytest.mark.parametrize("explicit_directory", [False, True])
def test_cli_submission_preview_exit_and_yes_execution(research_files, monkeypatch, capsys, explicit_directory):
    files = research_files
    original_build = cli.build
    session = original_build(cli.parse(["--fake", "--no-state", "--no-plugins"]), Config())
    session.app.interactive = False
    monkeypatch.setattr(cli, "build", lambda *args: session)
    calls = []
    original = session.backend.call
    monkeypatch.setattr(session.backend, "call", lambda command, *args, **kwargs: calls.append(list(command)) or original(command, *args, **kwargs))
    argv = ["--config", str(files["config"]), "--fake", "--no-state", "--no-plugins", "run", "submit", str(files["script"]),
            "--workdir", str(files["root"])]
    directory = files["root"] / ("submission-passports" if explicit_directory else ".raven/passports")
    if explicit_directory:
        argv += ["--passport-dir", str(directory)]
    assert cli.main(argv) == 3
    output = capsys.readouterr().out
    assert "add --yes" in output and str(files["script"]) in output
    assert not any(is_submission(command) for command in calls)
    # A fresh session avoids reusing closed executors and keeps every invocation independent.
    session = original_build(cli.parse(["--fake", "--no-state", "--no-plugins"]), Config())
    session.app.interactive = False
    calls.clear()
    original = session.backend.call
    monkeypatch.setattr(session.backend, "call", lambda command, *args, **kwargs: calls.append(list(command)) or original(command, *args, **kwargs))
    assert cli.main(argv + ["--yes"]) == 0
    submitted = [command for command in calls if is_submission(command)]
    assert len(submitted) == 1
    assert str(files["script"]) in " ".join(submitted[0])
    receipt = json.loads(capsys.readouterr().out)
    assert Path(receipt["passport_path"]).parent == directory
    assert Path(receipt["passport_path"]).is_file()


def test_installed_alias_forwards_ascii_override_and_live_account(tmp_path):
    from scripts.install_shell import shell_block
    root = tmp_path / "a raven checkout"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    launcher = scripts / "raven"
    launcher.write_text("#!/usr/bin/env python3\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n")
    launcher.chmod(0o755)
    block = tmp_path / "alias.bash"
    block.write_text(shell_block(root))
    command = "shopt -s expand_aliases\nsource " + shlex.quote(str(block)) + "\nCARC_ACCOUNT='live account'\neval \"raven --ascii --job-name 'two words'\"\n"
    result = subprocess.run(["bash", "--noprofile", "--norc", "-c", command], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    arguments = json.loads(result.stdout)
    assert arguments.index("--unicode") < arguments.index("--ascii")
    assert arguments[arguments.index("--account") + 1] == "live account"
    assert arguments[-2:] == ["--job-name", "two words"]


def test_interactive_preflight_stays_responsive_and_has_only_one_background_operation(research_dashboard, monkeypatch):
    session, files = research_dashboard
    from raven import submission
    entered, released = threading.Event(), threading.Event()
    original = submission.prepare
    def slow_prepare(*args, **kwargs):
        entered.set()
        assert released.wait(3), "test worker was not released"
        return original(*args, **kwargs)
    monkeypatch.setattr(submission, "prepare", slow_prepare)
    app = session.app
    try:
        start = time.perf_counter()
        app.run_command("prepare " + shlex.join([str(files["script"]), "--workdir", str(files["root"])]))
        assert time.perf_counter() - start < .5
        assert entered.wait(1)
        assert app.tab == "research" and app.research_view == "submit"
        app.handle("right")
        chosen_view = app.research_view
        assert chosen_view == "predict"
        session.views.compose(session.store.snapshot(), app, 80, 20, session.actions)
        app.run_command("prepare " + shlex.quote(str(files["script"])))
        assert not app.command_ok and "still running" in app.message
    finally:
        released.set()
        finish_research_command(app)
    assert app.research.plan["valid"]
    assert app.research_view == chosen_view


def test_research_rendering_and_array_drilldown_never_query_scheduler(research_dashboard):
    session, _ = research_dashboard
    app, hub = session.app, session.app.research
    before = list(session.backend.calls)
    for view, _ in RESEARCH_VIEWS:
        app.research_view = view
        hub.request(hub.context(session.store.snapshot(), app), wait=True, force=True)
        session.views.compose(session.store.snapshot(), app, 120, 30, session.actions)
        if view == "arrays":
            app.handle("enter")
            session.views.compose(session.store.snapshot(), app, 120, 30, session.actions)
    assert session.backend.calls == before


def test_array_task_pages_clamp_at_actual_coverage(research_dashboard):
    session, _ = research_dashboard
    app, hub = session.app, session.app.research
    app.research_view = "arrays"
    hub.request(hub.context(session.store.snapshot(), app), wait=True, force=True)
    session.views.compose(session.store.snapshot(), app, 120, 40, session.actions)
    app.handle("enter")
    for _ in range(3):
        app.handle("pgdn")
    session.views.compose(session.store.snapshot(), app, 120, 40, session.actions)
    assert app.research_task_offset == 0  # Twenty known tasks fit on one page.
