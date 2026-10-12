"""Forms and execution reviews remain navigable and require explicit activation."""
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from raven import execution_ui, orchestrator, submission
from raven.layout import Glyphs, row_text, vlen


class Worker:
    def __init__(self):
        self.pending = None
        self.plan = None

    def start_task(self, function, completion):
        if self.pending:
            return False
        self.pending = (function, completion)
        return True

    def complete(self):
        function, completion = self.pending
        self.pending = None
        try:
            value = function()
        except Exception as exc:
            value = exc
        completion(value)


class Scheduler:
    def __init__(self):
        self.b = SimpleNamespace()
        self.calls = []

    def submit(self, argv, workdir):
        self.calls.append((list(argv), workdir))
        return True, str(100 + len(self.calls)), str(100 + len(self.calls))


@pytest.fixture
def app(tmp_path, monkeypatch):
    for key in os.environ:
        if key.startswith("SBATCH_"):
            monkeypatch.delenv(key)
    messages = []
    value = SimpleNamespace(mode="main", interactive=True, research=Worker(), replay=None,
                            user="tester", profile_name="carc",
                            state_dir=str(tmp_path / "state"), actions=SimpleNamespace(slurm=Scheduler()),
                            files=SimpleNamespace(remote=False), sampler=None, command_ok=True,
                            store=SimpleNamespace(snapshot=lambda: {"jobs": [], "finished": []}),
                            say=lambda message: messages.append((True, message)),
                            fail=lambda message: messages.append((False, message)), messages=messages)
    execution_ui.initialize(value)
    return value


@pytest.fixture
def script(tmp_path):
    path = tmp_path / "batch job.sh"
    path.write_text("#!/bin/bash\n#SBATCH --cpus-per-task=4\n#SBATCH --mem=8G\ntrue\n")
    return path


def form(app, script):
    assert execution_ui.run_command(app, ["preflight", str(script), "--workdir", str(script.parent)])
    assert app.execution_state["running"]
    app.research.complete()
    assert app.mode == "execution"
    execution_ui.overlay(SimpleNamespace(g=Glyphs(False)), {}, app, 120, 35)
    return app.execution_state


def review(app, script, tmp_path):
    path = tmp_path / "workflow.json"
    path.write_text(json.dumps({"version": 1, "kind": "raven.workflow", "nodes": [
        {"id": "first", "script": str(script), "resources": {"cpus_per_task": 4}},
        {"id": "second", "script": str(script), "depends_on": ["first"], "resources": {"cpus_per_task": 4}},
    ]}))
    execution_ui.run_command(app, ["orchestrate", "workflow", str(path), "--workdir", str(tmp_path)])
    app.research.complete()
    assert app.mode == "execution"
    execution_ui.overlay(SimpleNamespace(g=Glyphs(False)), {}, app, 120, 35)
    return app.execution_state


def test_inactive_overlay_returns_none_and_does_not_hide_legacy_panels(app):
    for mode in ("main", "details", "help", "confirm", "palette"):
        app.mode = mode
        assert execution_ui.overlay(SimpleNamespace(g=Glyphs(False)), {}, app, 120, 40) is None
        assert execution_ui.handle_key(app, "enter") is False


def test_preflight_opens_an_editable_local_form_without_scheduler_calls(app, script):
    state = form(app, script)
    assert state["values"]["script"] == str(script)
    assert state["values"]["cpus"] == "4"
    assert state["values"]["mem"] == "8G"
    assert state["plan"]["valid"]
    assert app.actions.slurm.calls == []


def test_keyboard_editor_handles_spaces_in_paths_and_cursor_edits(app, script):
    state = form(app, script)
    execution_ui.handle_key(app, "enter")
    execution_ui.handle_key(app, "end")
    execution_ui.handle_key(app, "space")
    execution_ui.handle_key(app, "x")
    execution_ui.handle_key(app, "left")
    execution_ui.handle_key(app, "backspace")
    assert state["edit"] == str(script) + "x"
    execution_ui.handle_key(app, "backspace")
    execution_ui.handle_key(app, "esc")
    assert state["values"]["script"] == str(script)
    assert app.mode == "execution"


def test_edited_values_need_validation_before_submission_review(app, script):
    state = form(app, script)
    execution_ui.handle_key(app, "down")
    execution_ui.handle_key(app, "down")
    execution_ui.handle_key(app, "enter")
    execution_ui.handle_key(app, "ctrl-u")
    execution_ui.handle_key(app, "8")
    execution_ui.handle_key(app, "enter")
    assert state["values"]["cpus"] == "8"
    assert state["plan"] is None
    execution_ui.handle_key(app, "s")
    assert app.mode == "execution" and not app.messages[-1][0]
    execution_ui.handle_key(app, "p")
    app.research.complete()
    assert state["plan"]["valid"]
    assert "--cpus-per-task=8" in state["plan"]["argv"]
    execution_ui.handle_key(app, "s")
    assert app.mode == "confirm"
    assert app.confirm["action"] == "submit"
    assert app.actions.slurm.calls == []


def test_invalid_fields_show_preflight_errors_and_cannot_submit(app, script):
    state = form(app, script)
    state["values"]["mem"] = "invalid-memory"
    state["plan"] = None
    execution_ui.handle_key(app, "p")
    app.research.complete()
    assert not state["plan"]["valid"]
    execution_ui.handle_key(app, "s")
    assert app.mode == "execution"
    assert not app.actions.slurm.calls


def test_preflight_command_and_errors_have_a_scrollable_detail_view(app, script):
    state = form(app, script)
    state["values"]["mem"] = "bad"
    execution_ui.handle_key(app, "p")
    app.research.complete()
    execution_ui.handle_key(app, "d")
    execution_ui.handle_key(app, "end")
    rows = execution_ui.overlay(SimpleNamespace(g=Glyphs(False)), {}, app, 50, 10)
    text = "\n".join(row_text(row) for _, _, row in rows)
    assert "ERROR" in text or "memory amount" in text
    assert state["detail_scroll"] > 0
    execution_ui.handle_key(app, "esc")
    assert app.mode == "execution" and not state["detail"]


def test_command_wrap_preserves_wide_characters_and_display_width():
    value = "sbatch --chdir=/tmp/研究 --mem=8G /tmp/研究/run.sh"
    rows = execution_ui._wrap(value, 14)
    assert "".join(rows) == value
    assert all(vlen(row) <= 14 for row in rows)


def test_form_preserves_declared_parameters_inputs_outputs_and_other_flags(app, script, tmp_path):
    source = tmp_path / "input.txt"
    source.write_text("data")
    app.research.plan = submission.prepare(script, workdir=tmp_path, overrides=["--account=science"],
                                           parameters={"seed": 4}, inputs=[str(source)], outputs=["result.txt"])
    execution_ui.run_command(app, ["preflight"])
    state = app.execution_state
    state["values"]["cpus"] = "8"
    execution_ui.handle_key(app, "p")
    app.research.complete()
    assert state["plan"]["parameters"] == {"seed": 4}
    assert state["plan"]["inputs"] == [str(source)]
    assert state["plan"]["outputs"] == ["result.txt"]
    assert "--account=science" in state["plan"]["argv"]


def test_sealed_workflow_cannot_be_opened_as_an_ordinary_submission_form(app, script, tmp_path):
    state = review(app, script, tmp_path)
    app.research.plan = state["review"]["nodes"][0]["plan"]
    app.mode = "main"
    execution_ui.run_command(app, ["preflight"])
    assert app.mode == "main"
    assert not app.messages[-1][0]
    assert not app.actions.slurm.calls


def test_orchestration_review_does_not_submit_on_enter_and_defaults_to_cancel(app, script, tmp_path):
    state = review(app, script, tmp_path)
    execution_ui.handle_key(app, "enter")
    assert state["detail"] and not app.actions.slurm.calls
    execution_ui.handle_key(app, "tab")
    assert state["focus"] == "cancel" and not state["detail"]
    execution_ui.handle_key(app, "enter")
    assert app.mode == "main"
    assert not app.actions.slurm.calls
    assert not (tmp_path / "state").exists()


def test_two_tabs_and_enter_explicitly_submit_reviewed_batch_once(app, script, tmp_path):
    state = review(app, script, tmp_path)
    execution_ui.handle_key(app, "tab")
    execution_ui.handle_key(app, "tab")
    execution_ui.handle_key(app, "enter")
    assert state["running"] and not app.actions.slurm.calls
    assert Path(state["receipt_path"]).is_file()
    app.research.complete()
    assert len(app.actions.slurm.calls) == 2
    assert "--dependency=afterok:101" in app.actions.slurm.calls[1][0]
    assert state["receipt"]["status"] == "submitted"
    execution_ui.handle_key(app, "enter")
    assert not state["running"] and len(app.actions.slurm.calls) == 2


def test_receipt_and_last_path_restore_without_automatic_resume(app, script, tmp_path):
    state = review(app, script, tmp_path)
    receipt = orchestrator.create_receipt(state["review"], tmp_path / "state")
    state["receipt_path"] = receipt["receipt_path"]
    saved = execution_ui.save(app)
    del app.execution_state
    execution_ui.restore(app, saved)
    assert app.execution_state["receipt_path"] == receipt["receipt_path"]
    assert not app.actions.slurm.calls
    execution_ui.run_command(app, ["execution"])
    app.research.complete()
    assert app.execution_state["receipt"]["status"] == "prepared"
    assert not app.actions.slurm.calls


def test_resume_requires_fresh_confirmation(app, script, tmp_path):
    state = review(app, script, tmp_path)
    receipt = orchestrator.create_receipt(state["review"], tmp_path / "state")
    state["receipt_path"] = receipt["receipt_path"]
    execution_ui.run_command(app, ["execution", "resume"])
    assert state["pending_action"] == ("resume",)
    assert not app.actions.slurm.calls
    execution_ui.handle_key(app, "esc")
    assert app.mode == "main" and not app.actions.slurm.calls


@pytest.mark.parametrize("height", [6, 7, 8])
def test_small_terminal_confirmation_is_visible_or_disabled_before_activation(app, script, tmp_path, height):
    state = review(app, script, tmp_path)
    rendered = execution_ui.overlay(SimpleNamespace(g=Glyphs(True)), {}, app, 120, height)
    text = "\n".join(row_text(row) for _, _, row in rendered)
    visible = state["confirm_visible"]
    assert visible == ("[Confirm]" in text and "Submit 2 reviewed jobs once" in text and "receipt markers" in text)
    execution_ui.handle_key(app, "tab")
    execution_ui.handle_key(app, "tab")
    execution_ui.handle_key(app, "enter")
    if visible:
        assert app.research.pending and state["running"]
        app.research.complete()
        assert len(app.actions.slurm.calls) == 2
    else:
        assert not app.research.pending and not app.actions.slurm.calls
        assert "resize" in app.messages[-1][1]


def test_unrendered_action_cannot_be_confirmed(app, script, tmp_path):
    state = review(app, script, tmp_path)
    state["confirm_visible"] = False
    execution_ui.handle_key(app, "tab")
    execution_ui.handle_key(app, "tab")
    execution_ui.handle_key(app, "enter")
    assert not app.research.pending and not app.actions.slurm.calls
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize("size", [(120, 7), (120, 8), (40, 8), (20, 8)])
def test_recovery_summary_retains_actual_target_and_job_before_enabling_confirmation(app, script, tmp_path, size):
    state = review(app, script, tmp_path)
    state.update(pending_action=("recover", "second", "777"), index=0)
    rows = execution_ui.overlay(SimpleNamespace(g=Glyphs(True)), {}, app, *size)
    text = "\n".join(row_text(row) for _, _, row in rows)
    if state["confirm_visible"]:
        assert "Target node: second" in text and "Scheduler job: 777" in text and "[Confirm]" in text
    else:
        assert "[Confirm]" not in text


def test_ascii_workbench_escapes_unicode_paths_and_nodes_without_changing_commands(app, tmp_path):
    directory = tmp_path / "研究"
    directory.mkdir()
    script = directory / "作業.sh"
    script.write_text("#!/bin/bash\ntrue\n")
    state = form(app, script)
    original = state["plan"]["command"]
    state["detail"] = True
    rows = execution_ui.overlay(SimpleNamespace(g=Glyphs(True)), {}, app, 160, 45)
    text = "\n".join(row_text(row) for _, _, row in rows)
    assert text.isascii() and "\\u7814" in text
    assert state["plan"]["command"] == original and "研究" in original
    state.update(view="review", pending_action=("retry", "節点"), detail=False,
                 review={"kind": "workflow", "nodes": [{"id": "節点", "depends_on": [], "plan": state["plan"]}]})
    rows = execution_ui.overlay(SimpleNamespace(g=Glyphs(True)), {}, app, 160, 45)
    text = "\n".join(row_text(row) for _, _, row in rows)
    assert text.isascii() and "\\u7bc0" in text
    assert state["review"]["nodes"][0]["id"] == "節点"


def test_background_cancellation_event_and_close_stop_later_submission(app, script, tmp_path):
    state = review(app, script, tmp_path)
    execution_ui.handle_key(app, "tab")
    execution_ui.handle_key(app, "tab")
    execution_ui.handle_key(app, "enter")
    execution_ui.close(app)
    assert state["cancel"].is_set()
    app.research.complete()
    assert state["receipt"]["status"] == "cancelled"
    assert not app.actions.slurm.calls


def test_collect_uses_snapshot_without_new_scheduler_calls(app, script, tmp_path):
    state = review(app, script, tmp_path)
    receipt = orchestrator.create_receipt(state["review"], tmp_path / "state")
    state.update(receipt=receipt, receipt_path=receipt["receipt_path"], view="receipt", pending_action=None)
    execution_ui.handle_key(app, "c")
    app.research.complete()
    assert state["receipt"]["collection"]["records"] == []
    assert not app.actions.slurm.calls


@pytest.mark.parametrize("restricted", ["remote", "replay"])
def test_remote_or_replay_cannot_open_local_execution_forms(app, script, restricted):
    if restricted == "remote":
        app.files.remote = True
    else:
        app.replay = object()
    execution_ui.run_command(app, ["preflight", str(script)])
    assert app.mode == "main"
    assert not app.research.pending and not app.actions.slurm.calls


@pytest.mark.parametrize("ascii_", [False, True])
@pytest.mark.parametrize("size", [(1, 1), (5, 3), (20, 8), (40, 12), (80, 24), (160, 50)])
def test_form_and_review_render_within_terminal_bounds(app, script, tmp_path, ascii_, size):
    width, height = size
    views = SimpleNamespace(g=Glyphs(ascii_))
    form(app, script)
    for field in (0, 6):
        app.execution_state["field"] = field
        rows = execution_ui.overlay(views, {}, app, width, height)
        for y, x, row in rows:
            assert 0 <= y < height and 0 <= x < width
            assert x + vlen(row_text(row)) <= width
    state = review(app, script, tmp_path)
    for detail in (False, True):
        state["detail"] = detail
        rows = execution_ui.overlay(views, {}, app, width, height)
        for y, x, row in rows:
            assert 0 <= y < height and 0 <= x < width
            assert x + vlen(row_text(row)) <= width
