"""Exercise the source-checkout migration helpers against isolated user roots."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


@pytest.fixture
def desktop(tmp_path):
    home = tmp_path / "desktop home"
    config = home / "config files"
    state = home / "state files"
    temporary = tmp_path / "temporary files"
    for directory in (home, config, state, temporary):
        directory.mkdir(mode=0o700)
    old_config = config / "previous-app"
    old_state = state / "previous-app"
    old_config.mkdir(mode=0o700)
    old_state.mkdir(mode=0o700)
    (old_config / "config.json").write_text(json.dumps({
        "theme": "mono", "host": "", "account": "local-slurm",
        "profiles": {"desktop": {"host": "", "account": "desktop-slurm"}},
    }))
    (old_state / "groups.json").write_text('{"schema":"raven.groups/v1","jobs":["123"]}')
    (old_state / "history.jsonl").write_text('{"schema":"raven.event/v1"}\n')
    for name, filename in (("runtime", "worker.pid"), ("ssh", "private-key")):
        directory = old_state / name
        directory.mkdir(mode=0o700)
        (directory / filename).write_text("fixture only")
    environment = os.environ.copy()
    environment.update(HOME=str(home), XDG_CONFIG_HOME=str(config),
                       XDG_STATE_HOME=str(state), XDG_CACHE_HOME=str(home / "cache"),
                       TMPDIR=str(temporary), PYTHONNOUSERSITE="1")
    for name in ("RAVEN_CONFIG", "RAVEN_PROFILE", "RAVEN_STATE_DIR", "RAVEN_SITE_POLICY"):
        environment.pop(name, None)
    return {"root": tmp_path, "home": home, "config": config, "state": state,
            "temporary": temporary, "old_config": old_config, "old_state": old_state,
            "new_config": config / "raven" / "config.json", "env": environment}


def run(desktop, script, *arguments, expected=0, scripts=SCRIPTS, source_defaults=True):
    arguments = list(map(str, arguments))
    if source_defaults and script == "migrate-config.sh" and not any(
            option in arguments for option in ("--help", "-h", "--plan-file")):
        if "--source-config" not in arguments:
            arguments += ["--source-config", str(desktop["old_config"])]
        if "--include-state" in arguments and "--source-state" not in arguments:
            arguments += ["--source-state", str(desktop["old_state"])]
    result = subprocess.run([str(scripts / script), *map(str, arguments)],
                            cwd=desktop["root"], env=desktop["env"],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == expected, result.stderr + result.stdout
    return result


def files(root):
    return {str(path.relative_to(root)): path.read_bytes()
            for path in root.rglob("*") if path.is_file()}


def save(desktop, *arguments):
    plan_path = desktop["root"] / "private plans" / "reviewed plan.json"
    result = run(desktop, "migrate-config.sh", *arguments, "--save-plan", plan_path)
    return plan_path, json.loads(result.stdout)


def apply(desktop, plan_path):
    return json.loads(run(desktop, "migrate-config.sh", "--apply", "--plan-file", plan_path).stdout)


@pytest.mark.parametrize("script", ["migrate-config.sh", "rollback-config.sh"])
def test_help_is_available_without_install_or_user_configuration(desktop, script):
    before = files(desktop["root"])
    result = run(desktop, script, "--help")
    assert "Usage:" in result.stdout
    assert files(desktop["root"]) == before
    assert os.access(SCRIPTS / script, os.X_OK)


def test_explicit_source_preview_selects_config_only_and_leaves_user_files_unchanged(desktop):
    before = files(desktop["root"])
    value = json.loads(run(desktop, "migrate-config.sh").stdout)
    assert [(item["category"], item["path"]) for item in value["files"]] == [("config", "config.json")]
    assert value["destination_config"] == str(desktop["new_config"])
    assert value["converted_config"]["site"]["kind"] == "slurm"
    assert value["converted_config"]["profile"] == "default-slurm"
    assert not Path(value["sources"]["state"]["root"]).exists()
    assert files(desktop["root"]) == before
    assert not list(desktop["temporary"].iterdir())
    assert not desktop["new_config"].parent.exists()


def test_new_plan_requires_explicit_source_directories(desktop):
    before = files(desktop["root"])
    result = run(desktop, "migrate-config.sh", source_defaults=False, expected=2)
    assert "requires --source-config" in result.stderr
    result = run(desktop, "migrate-config.sh", "--source-config", desktop["old_config"],
                 "--include-state", source_defaults=False, expected=2)
    assert "requires --source-state" in result.stderr
    assert files(desktop["root"]) == before
    assert not list(desktop["temporary"].iterdir())


def test_saved_plan_is_private_and_replays_after_temporary_inventory_is_removed(desktop):
    original_config = files(desktop["old_config"])
    original_state = files(desktop["old_state"])
    plan_path, value = save(desktop)
    assert plan_path.stat().st_mode & 0o777 == 0o600
    assert plan_path.parent.stat().st_mode & 0o777 == 0o700
    assert not Path(value["sources"]["state"]["root"]).exists()
    result = apply(desktop, plan_path)
    assert result["state"] == "complete" and result["files"] == 2
    identity = {entry["path"]: Path(entry["path"]).stat().st_ino
                for entry in json.loads(Path(result["journal"]).read_text())["outputs"]}
    repeated = apply(desktop, plan_path)
    assert repeated["journal"] == result["journal"]
    assert {path: Path(path).stat().st_ino for path in identity} == identity
    assert files(desktop["old_config"]) == original_config
    assert files(desktop["old_state"]) == original_state
    assert not (desktop["home"] / ".bashrc").exists()


def test_interrupted_publication_resumes_against_same_saved_plan(desktop):
    plan_path, value = save(desktop)
    # Simulate process death after config publication but before its completion
    # record. The built-in engine persists publication intent before linking.
    program = """
import json
import sys
from raven import raven_migration as migration
value = json.loads(migration._read(sys.argv[1], migration.MAX_PLAN)[0])
writer = migration._journal_write
def interrupt(path, journal):
    if any(item['path'] == value['destination_config'] for item in journal['outputs']):
        raise RuntimeError('simulated process death')
    writer(path, journal)
migration._journal_write = interrupt
try:
    migration.apply(value)
except RuntimeError:
    raise SystemExit(75)
"""
    interrupted = subprocess.run([sys.executable, "-c", program, str(plan_path)],
                                 cwd=ROOT, env=desktop["env"],
                                 capture_output=True, text=True, timeout=30)
    assert interrupted.returncode == 75, interrupted.stderr
    assert desktop["new_config"].exists()
    journal = Path(value["destination_state"]) / "migrations" / (value["id"] + ".json")
    pending = json.loads(journal.read_text())
    assert pending["state"] == "applying" and pending["pending"]["path"] == str(desktop["new_config"])
    preview = json.loads(run(desktop, "rollback-config.sh", "--journal", journal).stdout)
    assert preview["state"] == "applying" and preview["conflicts"] == 0
    result = apply(desktop, plan_path)
    assert result["state"] == "complete" and result["files"] == 2
    assert "pending" not in json.loads(journal.read_text())


def test_state_and_history_each_require_explicit_opt_in(desktop):
    state = json.loads(run(desktop, "migrate-config.sh", "--include-state").stdout)
    assert {(item["category"], item["path"]) for item in state["files"]} == {
        ("config", "config.json"), ("state", "groups.json")}
    history = json.loads(run(desktop, "migrate-config.sh", "--include-state", "--include-history").stdout)
    assert {item["path"] for item in history["files"]} == {"config.json", "groups.json", "history.jsonl"}
    assert {item["path"] for item in history["sources"]["state"]["skipped"]} == {"runtime", "ssh"}
    run(desktop, "migrate-config.sh", "--include-history", expected=2)
    run(desktop, "migrate-config.sh", "--source-state", desktop["old_state"], expected=2)


def test_explicit_paths_and_spaced_checkout_path_are_safe(desktop):
    source_alias = desktop["root"] / "Raven source checkout"
    source_alias.symlink_to(ROOT, target_is_directory=True)
    destination_config = desktop["root"] / "new config files" / "my config.json"
    destination_state = desktop["root"] / "new state files"
    plan_path, value = save(desktop, "--include-state",
                            "--source-config", desktop["old_config"],
                            "--source-state", desktop["old_state"],
                            "--destination-config", destination_config,
                            "--destination-state", destination_state)
    result = json.loads(run(desktop, "migrate-config.sh", "--apply", "--plan-file", plan_path,
                            scripts=source_alias / "scripts").stdout)
    assert result["state"] == "complete"
    assert destination_config.exists()
    assert value["destination_state"] == str(destination_state)
    preview = run(desktop, "rollback-config.sh", "--journal", result["journal"],
                  scripts=source_alias / "scripts")
    assert json.loads(preview.stdout)["conflicts"] == 0


def test_apply_requires_saved_plan_and_rejects_ambiguous_choices(desktop):
    before = files(desktop["root"])
    result = run(desktop, "migrate-config.sh", "--apply", expected=2)
    assert "requires a reviewed --plan-file" in result.stderr
    plan_path, _ = save(desktop)
    for extra in (("--include-state",), ("--source-config", desktop["old_config"]),
                  ("--destination-config", desktop["new_config"]), ("--save-plan", plan_path)):
        run(desktop, "migrate-config.sh", "--apply", "--plan-file", plan_path, *extra, expected=2)
    run(desktop, "migrate-config.sh", "--plan", "--apply", "--plan-file", plan_path, expected=2)
    assert not desktop["new_config"].exists()
    assert all(files(desktop["root"])[path] == content for path, content in before.items())


def test_new_saved_plan_can_be_explicitly_applied_in_one_command(desktop):
    plan_path = desktop["root"] / "private plans" / "automated.json"
    value = json.loads(run(desktop, "migrate-config.sh", "--apply", "--save-plan", plan_path).stdout)
    assert value["state"] == "complete"
    assert plan_path.stat().st_mode & 0o777 == 0o600


def test_save_plan_refuses_to_overwrite_and_does_not_apply(desktop):
    plan_path, _ = save(desktop)
    saved = plan_path.read_bytes()
    result = run(desktop, "migrate-config.sh", "--apply", "--save-plan", plan_path, expected=2)
    assert "File exists" in result.stderr
    assert plan_path.read_bytes() == saved
    assert not desktop["new_config"].exists()
    assert not list(desktop["temporary"].iterdir())


def test_changed_source_fails_before_import_and_preserves_originals(desktop):
    plan_path, _ = save(desktop)
    target = desktop["old_config"] / "config.json"
    target.write_text('{"theme":"dark"}')
    result = run(desktop, "migrate-config.sh", "--apply", "--plan-file", plan_path, expected=2)
    assert "Legacy source changed" in result.stderr
    assert not (desktop["state"] / "raven" / "legacy-imports").exists()
    assert target.read_text() == '{"theme":"dark"}'


def test_existing_raven_configuration_survives_apply_and_rollback(desktop):
    desktop["new_config"].parent.mkdir(mode=0o700)
    original = b'{"schema":"raven.config/v1","ui":{"theme":"dark"}}\n'
    desktop["new_config"].write_bytes(original)
    plan_path, value = save(desktop, "--include-state")
    assert value["conflicts"] and value["converted_config"] is None
    result = apply(desktop, plan_path)
    assert desktop["new_config"].read_bytes() == original
    rollback = json.loads(run(desktop, "rollback-config.sh", "--journal", result["journal"], "--apply").stdout)
    assert rollback["state"] == "rolled-back"
    assert desktop["new_config"].read_bytes() == original


def test_rollback_preview_is_read_only_then_rollback_is_repeatable(desktop):
    plan_path, _ = save(desktop, "--include-state")
    result = apply(desktop, plan_path)
    before = files(desktop["root"])
    preview = json.loads(run(desktop, "rollback-config.sh", "--journal", result["journal"]).stdout)
    assert preview["preview"] and preview["conflicts"] == 0
    assert {item["status"] for item in preview["outputs"]} == {"unchanged"}
    assert files(desktop["root"]) == before
    rollback = json.loads(run(desktop, "rollback-config.sh", "--journal", result["journal"], "--apply").stdout)
    assert rollback["state"] == "rolled-back" and rollback["removed"] == 3
    assert not desktop["new_config"].exists()
    repeat = json.loads(run(desktop, "rollback-config.sh", "--journal", result["journal"], "--apply").stdout)
    assert repeat["removed"] == 0
    assert plan_path.exists() and Path(result["journal"]).exists()
    assert files(desktop["old_config"])
    run(desktop, "migrate-config.sh", "--apply", "--plan-file", plan_path, expected=2)


def test_modified_output_reports_conflict_without_removing_any_outputs(desktop):
    plan_path, _ = save(desktop, "--include-state")
    result = apply(desktop, plan_path)
    desktop["new_config"].write_bytes(desktop["new_config"].read_bytes() + b" ")
    before = files(desktop["root"])
    preview = run(desktop, "rollback-config.sh", "--journal", result["journal"], expected=2)
    assert json.loads(preview.stdout)["conflicts"] == 1
    rollback = run(desktop, "rollback-config.sh", "--journal", result["journal"], "--apply", expected=2)
    assert "rollback refuses to remove" in rollback.stderr
    assert files(desktop["root"]) == before
    run(desktop, "migrate-config.sh", "--apply", "--plan-file", plan_path, expected=2)
    assert files(desktop["root"]) == before


def test_rollback_preview_reports_unauthorized_journal_output(desktop):
    plan_path, _ = save(desktop)
    result = apply(desktop, plan_path)
    journal_path = Path(result["journal"])
    journal = json.loads(journal_path.read_text())
    journal["outputs"][0]["path"] = str(desktop["old_config"] / "config.json")
    journal_path.write_text(json.dumps(journal))
    before = files(desktop["root"])
    preview = run(desktop, "rollback-config.sh", "--journal", journal_path, expected=2)
    assert json.loads(preview.stdout)["conflicts"] == 1
    actual = run(desktop, "rollback-config.sh", "--journal", journal_path, "--apply", expected=2)
    assert "not authorized by its plan" in actual.stderr
    assert files(desktop["root"]) == before


def test_explicit_journal_is_required(desktop):
    before = files(desktop["root"])
    result = run(desktop, "rollback-config.sh", "--apply", expected=2)
    assert "--journal is required" in result.stderr
    run(desktop, "rollback-config.sh", "--journal", desktop["root"] / "missing.json", expected=2)
    assert files(desktop["root"]) == before
