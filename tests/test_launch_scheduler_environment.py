"""Submission previews preserve Slurm environment precedence over saved defaults."""
from pathlib import Path

import pytest

from raven import launch, launch_settings


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("RAVEN_CONFIG", raising=False)
    script = tmp_path / "job.sh"
    script.write_text("#!/bin/bash\ntrue\n")
    return script


def preview(project, defaults, environ, *options):
    launch_settings.save_settings(project.parent, {"launch": {"sbatch_defaults": defaults}})
    before = set(project.parent.rglob("*"))
    result = launch.resolve_plan([str(project), "--check", *options], environ=environ)
    assert set(project.parent.rglob("*")) == before
    return result


@pytest.mark.parametrize("variable,option,value,default", [
    ("SBATCH_NTASKS_PER_NODE", "ntasks-per-node", "4", "1"),
    ("SBATCH_NTASKS_PER_CORE", "ntasks-per-core", "2", "1"),
    ("SBATCH_NTASKS_PER_SOCKET", "ntasks-per-socket", "8", "1"),
    ("SBATCH_MEM_PER_GPU", "mem-per-gpu", "32G", "4G"),
    ("SBATCH_CPUS_PER_GPU", "cpus-per-gpu", "8", "1"),
    ("SBATCH_GPUS_PER_NODE", "gpus-per-node", "a100:2", "1"),
    ("SBATCH_GPUS_PER_TASK", "gpus-per-task", "1", "2"),
    ("SBATCH_GPU_BIND", "gpu-bind", "closest", "none"),
    ("SBATCH_CONSTRAINT", "constraint", "gpu&fast", "cpu"),
    ("SBATCH_THREADS_PER_CORE", "threads-per-core", "1", "2"),
    ("SBATCH_WAIT_ALL_NODES", "wait-all-nodes", "0", "1"),
    ("SBATCH_TIMEMIN", "time-min", "00:10:00", "00:01:00"),
    ("SBATCH_ACCTG_FREQ", "acctg-freq", "task=30", "task=60"),
    ("SBATCH_OPEN_MODE", "open-mode", "append", "truncate"),
    ("SBATCH_RESERVATION", "reservation", "lab-reservation", "default"),
    ("SBATCH_DISTRIBUTION", "distribution", "block:block", "cyclic"),
])
def test_recognized_environment_without_script_directive_blocks_generated_override(project, variable, option, value, default):
    result = preview(project, {option: default}, {variable: value})
    assert result["effective_options"][option] == value
    assert result["option_sources"][option] == "environment " + variable
    assert not any(token.startswith("--" + option + "=") for token in result["sbatch_options"])


@pytest.mark.parametrize("value,enabled", [("", True), ("yes", True), ("YES", True), ("2", True), ("-1", True), ("0", False), ("no", False), ("false", False), ("true", False)])
def test_environment_flag_native_boolean_semantics_preserve_explicit_disabled_choice(project, value, enabled):
    project.write_text("#!/bin/bash\n#SBATCH --overcommit\ntrue\n")
    result = preview(project, {"overcommit": True}, {"SBATCH_OVERCOMMIT": value})
    assert result["effective_options"]["overcommit"] is enabled
    assert result["option_sources"]["overcommit"] == "environment SBATCH_OVERCOMMIT"
    assert "--overcommit" not in result["sbatch_options"]


@pytest.mark.parametrize("variable,option,value,expected", [
    ("SBATCH_EXCLUSIVE", "exclusive", "", True),
    ("SBATCH_EXCLUSIVE", "exclusive", "user", "user"),
    ("SBATCH_EXCLUSIVE", "exclusive", "mcs", "mcs"),
    ("SBATCH_EXCLUSIVE", "exclusive", "topo", "topo"),
    ("SBATCH_EXCLUSIVE", "exclusive", "oversubscribe", "oversubscribe"),
    ("SBATCH_NO_KILL", "no-kill", "", True),
    ("SBATCH_NO_KILL", "no-kill", "off", False),
    ("SBATCH_GET_USER_ENV", "get-user-env", "", True),
    ("SBATCH_GET_USER_ENV", "get-user-env", "60L", "60L"),
    ("SBATCH_NICE", "nice", "", True),
    ("SBATCH_NICE", "nice", "100", "100"),
    ("SBATCH_DEBUG", "verbose", "3", 3),
])
def test_optional_environment_arguments_do_not_use_boolean_flag_rules(project, variable, option, value, expected):
    result = preview(project, {option: "7" if option in {"nice", "get-user-env"} else True}, {variable: value})
    assert result["effective_options"][option] == expected
    assert not any(token == "--" + option or token.startswith("--" + option + "=") for token in result["sbatch_options"])


@pytest.mark.parametrize("variable,value", [
    ("SBATCH_MEM_PER_GPU", ""), ("SBATCH_NTASKS_PER_NODE", ""),
    ("SBATCH_EXCLUSIVE", "yes"), ("SBATCH_NO_KILL", "1"),
    ("SBATCH_GET_USER_ENV", "bad"), ("SBATCH_NICE", "nice"),
    ("SBATCH_NTASKS_PER_NODE", 4), ("SBATCH_MEM_PER_GPU", None),
    ("SBATCH_GPUS_PER_NODE", "1\n--wrap=evil"),
    ("SBATCH_CONSTRAINT", "a\x00b"), ("SBATCH_PARTITION", "x" * 8193),
])
def test_invalid_inherited_values_fail_preview_before_writes(project, variable, value):
    before = set(project.parent.rglob("*"))
    with pytest.raises(ValueError, match=variable):
        launch.resolve_plan([str(project), "--check"], environ={variable: value})
    assert set(project.parent.rglob("*")) == before


@pytest.mark.parametrize("value", ["", "yes", "1", "-1"])
def test_enabled_unsupported_wait_environment_is_rejected(project, value):
    with pytest.raises(ValueError, match="submission mode is unsupported"):
        launch.resolve_plan([str(project), "--check"], environ={"SBATCH_WAIT": value})


@pytest.mark.parametrize("value", ["0", "no", "false"])
def test_disabled_unsupported_wait_flag_cannot_change_submission_mode(project, value):
    result = launch.resolve_plan([str(project), "--check"], environ={"SBATCH_WAIT": value})
    assert "wait" not in result["effective_options"]


@pytest.mark.parametrize("defaults,environ,blocked", [
    ({"mem": "1G"}, {"SBATCH_MEM_PER_GPU": "16G"}, "mem"),
    ({"mem-per-cpu": "1G"}, {"SBATCH_MEM_PER_NODE": "16G"}, "mem-per-cpu"),
    ({"cpus-per-task": "1"}, {"SBATCH_CPUS_PER_GPU": "8"}, "cpus-per-task"),
    ({"oversubscribe": True}, {"SBATCH_EXCLUSIVE": "user"}, "oversubscribe"),
    ({"no-requeue": True}, {"SBATCH_REQUEUE": "yes"}, "no-requeue"),
])
def test_conflicting_resource_defaults_do_not_override_stronger_alternate_option(project, defaults, environ, blocked):
    result = preview(project, defaults, environ)
    assert not any(token == "--" + blocked or token.startswith("--" + blocked + "=") for token in result["sbatch_options"])


def test_command_line_still_overrides_environment_and_script_without_changing_native_tokens(project):
    project.write_text("#!/bin/bash\n#SBATCH --ntasks-per-node=2\ntrue\n")
    result = preview(project, {"ntasks-per-node": "1"}, {"SBATCH_NTASKS_PER_NODE": "4"}, "--sbatch=--ntasks-per-node=8")
    assert result["effective_options"]["ntasks-per-node"] == "8"
    assert result["option_sources"]["ntasks-per-node"] == "command line"
    assert result["sbatch_options"].count("--ntasks-per-node=8") == 1
    assert all(token == "--ntasks-per-node=8" or token.startswith("--job-name=") for token in result["sbatch_options"])
    assert result["sbatch_header"] == ["#SBATCH --ntasks-per-node=2"]


def test_unknown_site_environment_is_not_fabricated_into_effective_scheduler_settings(project):
    result = preview(project, {"qos": "normal"}, {"SBATCH_SITE_PLUGIN_HINT": "private", "SOMETHING": "ignored"})
    assert "site-plugin-hint" not in result["effective_options"]
    assert result["effective_options"]["qos"] == "normal"


def test_conflicting_environment_aliases_fail_instead_of_depending_on_dict_order(project):
    for environ in ({"SBATCH_EXPORT": "ALL", "SBATCH_EXPORT_ENV": "NONE"}, {"SBATCH_EXPORT_ENV": "NONE", "SBATCH_EXPORT": "ALL"}):
        with pytest.raises(ValueError, match="Conflicting environment aliases"):
            launch.resolve_plan([str(project), "--check"], environ=environ)
