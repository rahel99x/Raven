"""Independent regression checks at the UI and command-line action boundaries."""
from __future__ import annotations

import json
import os
import shlex
import threading
from pathlib import Path

import pytest

from raven import cli, research_commands
from raven.config import Config
from raven.research import ResearchHub
from raven.submission import prepare


@pytest.fixture(autouse=True)
def clear_inherited_batch_options(monkeypatch):
    for name in os.environ:
        if name.startswith("SBATCH_"):
            monkeypatch.delenv(name)


@pytest.fixture
def batch_files(tmp_path):
    script = tmp_path / "a user's training.sbatch"
    script.write_text("#!/bin/bash\n#SBATCH --output=log-%j.txt\n#SBATCH --time=00:10:00\necho simulation\n")
    config = tmp_path / "config.json"
    config.write_text("{}")
    return {"root": tmp_path, "script": script, "config": config}


@pytest.fixture
def session(batch_files):
    result = cli.build(cli.parse(["--fake", "--no-state", "--no-plugins"]), Config())
    yield result
    result.close()


def command(name, files, *args):
    return shlex.join([name, str(files["script"]), "--workdir", str(files["root"]), *args])


def batch_argv(cmd):
    words = shlex.split(cmd[2]) if cmd[:2] == ["sh", "-c"] else list(cmd)
    return words[words.index("sbatch"):] if "sbatch" in words else None


def finish_pending(app):
    future = app.research.pending[0]
    future.result(timeout=5)
    app.tick()


def test_background_preparation_publishes_state_only_during_ui_tick(session, batch_files, monkeypatch):
    app = session.app
    entered, release = threading.Event(), threading.Event()
    worker_threads, ui_messages = [], []
    original = research_commands.offline_result
    original_say = app.say

    def blocked(cmd, args):
        worker_threads.append(threading.get_ident())
        entered.set()
        assert release.wait(3)
        return original(cmd, args)

    def record_message(text):
        ui_messages.append((threading.get_ident(), text))
        return original_say(text)

    monkeypatch.setattr(research_commands, "offline_result", blocked)
    monkeypatch.setattr(app, "say", record_message)
    ui_thread = threading.get_ident()
    app.run_command(command("prepare", batch_files))
    assert entered.wait(3)
    assert app.research_result is None and app.research.plan is None
    assert app.tab == "research" and app.research_view == "submit"
    app.tick()  # Rendering while the worker is busy must remain nonblocking.
    assert app.research_result is None
    release.set()
    app.research.pending[0].result(timeout=5)
    assert app.research_result is None and app.research.plan is None
    app.tick()
    assert app.research_result["valid"]
    assert app.research.plan["script"] == str(batch_files["script"])
    assert app.tab == "research"
    assert worker_threads == [worker_threads[0]] and worker_threads[0] != ui_thread
    assert all(thread == ui_thread for thread, _ in ui_messages)


def test_busy_worker_does_not_queue_additional_commands(session, batch_files, monkeypatch):
    app = session.app
    entered, release = threading.Event(), threading.Event()
    calls = []
    original = research_commands.offline_result

    def blocked(cmd, args):
        calls.append(cmd)
        entered.set()
        assert release.wait(3)
        return original(cmd, args)

    monkeypatch.setattr(research_commands, "offline_result", blocked)
    app.run_command(command("prepare", batch_files))
    assert entered.wait(3)
    app.run_command(command("prepare", batch_files, "--mem=32G"))
    assert not app.command_ok and "still running" in app.message
    release.set()
    finish_pending(app)
    assert calls == ["prepare"]
    assert "mem" not in app.research.plan["resources"]


def test_unexpected_worker_exception_cannot_crash_ui_tick(session, batch_files, monkeypatch):
    app = session.app

    def broken(*args):
        raise RuntimeError("unexpected adapter failure")

    monkeypatch.setattr(research_commands, "offline_result", broken)
    app.run_command(command("prepare", batch_files))
    future = app.research.pending[0]
    with pytest.raises(RuntimeError, match="adapter failure"):
        future.result(timeout=5)
    app.tick()
    assert not app.command_ok
    assert "adapter failure" in app.message
    assert app.mode == "main"


def test_closed_hub_discards_completed_worker_callback(batch_files):
    hub = ResearchHub(Config())
    called = []
    entered, release = threading.Event(), threading.Event()

    def running():
        entered.set()
        return release.wait(3)

    try:
        assert hub.start_task(running, called.append)
        assert entered.wait(3)
        future = hub.pending[0]
        hub.close()
        release.set()
        future.result(timeout=5)
        hub.poll_task()
        assert called == []
    finally:
        release.set()
        hub.close()


def test_background_submission_updates_ui_and_audit_only_on_ui_tick(session, batch_files, monkeypatch):
    app = session.app
    app.research.plan = prepare(batch_files["script"], batch_files["root"])
    threads, events = [], []
    original_call, original_event = session.backend.call, session.store.event
    ui_thread = threading.get_ident()

    def record_call(cmd, *args):
        if batch_argv(list(cmd)):
            threads.append(threading.get_ident())
        return original_call(cmd, *args)

    def record_event(*args, **kwargs):
        events.append(threading.get_ident())
        return original_event(*args, **kwargs)

    monkeypatch.setattr(session.backend, "call", record_call)
    monkeypatch.setattr(session.store, "event", record_event)
    app.run_command("submit")
    assert app.mode == "confirm" and not threads
    # Keep generated passports in the test fixture without changing HOME.
    app.confirm["passport_directory"] = str(batch_files["root"] / "passports")
    app.handle("y")
    app.research.pending[0].result(timeout=5)
    assert threads and all(thread != ui_thread for thread in threads)
    assert app.research_result is None and app.research.passport is None
    assert events == []
    app.tick()
    assert app.research_result["ok"] and app.research.passport is not None
    assert events and all(thread == ui_thread for thread in events)
    assert app.mode == "main"


def test_second_confirmation_while_submit_is_running_cannot_queue_a_second_job(session, batch_files, monkeypatch):
    app = session.app
    app.research.plan = prepare(batch_files["script"], batch_files["root"])
    entered, release = threading.Event(), threading.Event()
    calls = []
    original = session.backend.call

    def blocked(cmd, *args):
        args_ = batch_argv(list(cmd))
        if args_ and "--test-only" not in args_:
            calls.append(args_)
            entered.set()
            assert release.wait(3)
        return original(cmd, *args)

    monkeypatch.setattr(session.backend, "call", blocked)
    app.run_command("submit")
    app.confirm["passport_directory"] = str(batch_files["root"] / "passports")
    app.handle("y")
    assert entered.wait(3)
    app.run_command("submit")
    assert not app.command_ok and "still running" in app.message
    assert app.mode == "main"
    app.handle("y")
    assert app.mode == "main" and len(calls) == 1
    release.set()
    finish_pending(app)
    assert len(calls) == 1
    for _ in range(3):
        app.tick()
    assert len(calls) == 1


@pytest.mark.parametrize("response", ["accepted without a receipt", "Submitted batch job 10001\nSubmitted batch job 10002"])
def test_unknown_submission_response_never_retries_on_ticks_or_extra_yes_keys(session, batch_files, monkeypatch, response):
    app = session.app
    app.interactive = False
    calls = []
    original = session.backend.call

    def uncertain(cmd, *args):
        args_ = batch_argv(list(cmd))
        result = original(cmd, *args)
        if args_ and "--test-only" not in args_:
            calls.append(args_)
            return True, response
        return result

    monkeypatch.setattr(session.backend, "call", uncertain)
    before = len(session.backend.spec)
    app.run_command(command("submit", batch_files, "--passport-dir", str(batch_files["root"] / "passports")))
    assert app.mode == "confirm" and not calls
    app.handle("y")
    assert app.research_result["state"] == "unknown"
    assert app.research_result["submitted"] is True
    assert not app.command_ok
    assert "inspect the queue" in app.message
    assert len(session.backend.spec) == before + 1
    for _ in range(5):
        app.tick()
        app.handle("y")
    assert len(calls) == 1 and len(session.backend.spec) == before + 1


def test_script_edited_while_confirmation_is_open_is_not_submitted(session, batch_files, monkeypatch):
    app = session.app
    app.interactive = False
    calls = []
    original = session.backend.call

    def record(cmd, *args):
        calls.append(list(cmd))
        return original(cmd, *args)

    monkeypatch.setattr(session.backend, "call", record)
    app.run_command(command("submit", batch_files, "--passport-dir", str(batch_files["root"] / "passports")))
    assert app.mode == "confirm" and not calls
    batch_files["script"].write_text(batch_files["script"].read_text() + "echo edited\n")
    app.handle("y")
    assert not app.command_ok
    assert app.research_result["state"] == "not_submitted"
    assert "changed" in app.message
    assert calls == []


def test_cli_uncertain_submission_returns_failure_without_an_automatic_retry(batch_files, monkeypatch, capsys):
    argv = ["--config", str(batch_files["config"]), "--fake", "--no-state", "--no-plugins", "run", "submit",
            str(batch_files["script"]), "--workdir", str(batch_files["root"]), "--passport-dir",
            str(batch_files["root"] / "passports"), "--yes"]
    session = cli.build(cli.parse(argv), Config())
    calls = []
    before = len(session.backend.spec)
    original = session.backend.call

    def uncertain(cmd, *args):
        args_ = batch_argv(list(cmd))
        result = original(cmd, *args)
        if args_ and "--test-only" not in args_:
            calls.append(args_)
            return True, "accepted without a job ID"
        return result

    monkeypatch.setattr(cli, "build", lambda *args: session)
    monkeypatch.setattr(session.backend, "call", uncertain)
    assert cli.main(argv) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["state"] == "unknown" and result["submitted"] is True
    assert "inspect the queue" in result["error"]
    assert len(calls) == 1 and len(session.backend.spec) == before + 1


@pytest.mark.parametrize("tokens,target", [
    (["--comment", "--parameter=literal"], "parameter"),
    (["--job-name", "--workdir=literal"], "workdir"),
    (["--output", "--declared-output=literal"], "output"),
    (["--comment", "--passport-dir=literal"], "passport_dir"),
])
def test_workbench_options_cannot_steal_sbatch_option_values(batch_files, tokens, target):
    script, opts, overrides, parameters = research_commands.script_args([str(batch_files["script"]), *tokens])
    assert script == str(batch_files["script"])
    assert overrides == tokens
    assert not getattr(opts, target)
    assert parameters == {}


@pytest.mark.parametrize("tokens,attribute,default", [
    (["--comment", "--yes"], "yes", False),
    (["--job-name", "--fake"], "fake", False),
    (["--comment", "--config=/untrusted/config"], "config", None),
    (["--output", "--record=/untrusted/recording"], "record", ""),
])
def test_global_raven_options_cannot_steal_sbatch_option_values(batch_files, tokens, attribute, default):
    args = cli.parse(["run", "submit", str(batch_files["script"]), *tokens])
    assert getattr(args, attribute) == default
    assert shlex.split(args.run) == ["submit", str(batch_files["script"]), *tokens]


@pytest.mark.parametrize("literal,attribute,default", [("--yes", "yes", False), ("--fake", "fake", False),
                                                       ("--record=/untrusted/path", "record", "")])
def test_command_separator_prevents_extraction_of_literal_raven_flags(batch_files, literal, attribute, default):
    # Everything after -- belongs to the command; --yes must never become an
    # implicit confirmation for a literal, unsupported batch argument.
    args = cli.parse(["run", "submit", str(batch_files["script"]), "--", literal])
    assert getattr(args, attribute) == default
    assert shlex.split(args.run) == ["submit", str(batch_files["script"]), "--", literal]


@pytest.mark.parametrize("words,attribute,default", [
    (["metric", "metrics.jsonl", "--phase", "--fake"], "fake", False),
    (["metric", "metrics.jsonl", "--unit", "--no-state"], "no_state", False),
    (["metric", "metrics.jsonl", "--value", "--yes"], "yes", False),
    (["passport", "capture", "batch.sh", "--output-dir", "--config=/tmp/a-literal-directory"], "config", None),
    (["passport", "capture", "batch.sh", "--input", "--record=/tmp/a-literal-input"], "record", ""),
    (["passport", "capture", "batch.sh", "--env", "--yes"], "yes", False),
])
def test_nonbatch_research_command_values_cannot_be_extracted_as_raven_flags(words, attribute, default):
    args = cli.parse(["run", *words])
    assert getattr(args, attribute) == default
    assert shlex.split(args.run) == words


def test_offline_metric_with_flaglike_phase_writes_literal_data_without_backend(batch_files, monkeypatch, capsys):
    monkeypatch.setattr(cli, "build", lambda *a: pytest.fail("metric writer constructed a scheduler session"))
    path = batch_files["root"] / "metrics.jsonl"
    assert cli.main(["--config", str(batch_files["config"]), "run", "metric", str(path), "--value=loss=0.25", "--phase=--fake"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["phase"] == "--fake"
    assert json.loads(path.read_text())["phase"] == "--fake"


def test_cli_prepare_preserves_all_safe_batch_flag_boundaries_without_backend(batch_files, monkeypatch, capsys):
    monkeypatch.setattr(cli, "build", lambda *a: pytest.fail("preparation constructed a scheduler session"))
    flags = ["--account", "submit account", "--job-name", "a user's name; $(touch should-not-exist)",
             "--export=ALL,NOTE=a;b", "--array=0-99:3%4", "-c8", "--mem", "8G", "-p", "main"]
    result = cli.main(["--config", str(batch_files["config"]), "--account", "monitor-account", "run", "prepare",
                       str(batch_files["script"]), "--workdir", str(batch_files["root"]), *flags])
    assert result == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["overrides"] == flags
    assert plan["resources"]["account"] == "submit account"
    assert shlex.split(plan["command"]) == ["sbatch", *plan["argv"]]
    assert not (batch_files["root"] / "should-not-exist").exists()


@pytest.mark.parametrize("mode", ["host", "profile", "replay"])
def test_remote_and_replay_offline_refusal_precedes_session_or_file_io(batch_files, monkeypatch, capsys, mode):
    monkeypatch.setattr(cli, "build", lambda *a: pytest.fail("offline refusal constructed a scheduler session"))
    from raven import submission
    monkeypatch.setattr(submission, "_read_script", lambda *a: pytest.fail("remote offline command read local script"))
    args = ["--config", str(batch_files["config"])]
    if mode == "host":
        args += ["--host", "unreachable.example"]
    elif mode == "profile":
        batch_files["config"].write_text(json.dumps({"profiles": {"remote": {"host": "unreachable.example"}}}))
        args += ["--profile", "remote"]
    else:
        args += ["--replay", str(batch_files["root"] / "does-not-exist-recording")]
    assert cli.main([*args, "run", "prepare", str(batch_files["script"])]) == 1
    assert "local" in capsys.readouterr().out.lower()


def test_remote_palette_preparation_does_not_queue_worker(session, batch_files, monkeypatch):
    app = session.app
    app.files.remote = True
    monkeypatch.setattr(app.research, "start_task", lambda *a: pytest.fail("remote prepare queued a local worker"))
    app.run_command(command("prepare", batch_files))
    assert not app.command_ok and "local" in app.message
    assert app.mode == "main"
