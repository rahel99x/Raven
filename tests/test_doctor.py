"""Diagnostics must help a fresh install without touching a real workload."""
import json

import pytest

from raven import doctor
from raven.cli import main, parse
from raven.config import Config
from raven.slurm import CommandError


def test_demo_doctor_does_not_need_commands_or_build_a_session(monkeypatch, capsys):
    monkeypatch.setattr(doctor.shutil, "which", lambda _: None)
    monkeypatch.setattr("raven.cli.build", lambda *_: pytest.fail("doctor must not start sampling"))
    assert main(["--doctor", "--fake", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["ready"] and result["mode"] == "demo"
    assert all(c["name"] not in doctor.REQUIRED for c in result["checks"])


def test_local_doctor_distinguishes_required_and_optional_tools(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "/usr/bin/" + name if name in doctor.REQUIRED else None)
    result = doctor.diagnose(Config())
    assert result["ready"]
    assert sum(c["status"] == "warning" for c in result["checks"]) >= len(doctor.OPTIONAL)
    monkeypatch.setattr(doctor.shutil, "which", lambda _: None)
    result = doctor.diagnose(Config())
    assert not result["ready"]
    assert {c["name"] for c in result["checks"] if c["status"] == "error"} == set(doctor.REQUIRED)
    assert "--fake" in doctor.render(result)


def test_remote_doctor_probes_read_only_without_connecting_to_user_cluster(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda _: "/usr/bin/ssh")
    calls = []

    def run(self, argv, timeout):
        calls.append((self, argv, timeout))
        return "\n".join(doctor.REQUIRED), 0.01

    monkeypatch.setattr(doctor.SshBackend, "run", run)
    result = doctor.diagnose(Config({"ssh_user": "tester"}), host="login.example")
    assert result["ready"] and result["mode"] == "remote"
    backend, argv, timeout = calls[0]
    assert backend.host == "login.example" and not backend.control
    assert argv[:2] == ["sh", "-c"] and "command -v" in argv[2] and timeout == 8
    assert len(calls) == 1

    def fail(*args, **kwargs):
        raise CommandError("SSH connection unavailable")

    monkeypatch.setattr(doctor.SshBackend, "run", fail)
    result = doctor.diagnose(Config(), host="login.example")
    assert not result["ready"]
    assert any(c["name"] == "SSH connection" and c["status"] == "error" for c in result["checks"])


@pytest.mark.parametrize("args", [["--speed", "0"], ["--interval", "-1"], ["--days", "nan"], ["--timeout", "inf"], ["--width", "-20"]])
def test_invalid_numeric_options_fail_before_sampling(args):
    with pytest.raises(SystemExit) as error:
        parse(args)
    assert error.value.code == 2


def test_config_creation_preserves_existing_user_settings(tmp_path):
    path = tmp_path / "config.json"
    Config.write_default(str(path))
    assert Config.load(str(path))["intervals"]["jobs"] == 2
    path.write_text('{"account":"keep-me"}')
    with pytest.raises(FileExistsError):
        Config.write_default(str(path))
    assert json.loads(path.read_text()) == {"account": "keep-me"}


def test_config_errors_are_actionable_without_a_traceback(tmp_path, capsys):
    path = tmp_path / "broken.json"
    path.write_text("[]")
    assert main(["--config", str(path), "--doctor", "--fake"]) == 1
    assert "JSON object" in capsys.readouterr().err
    assert main(["--config", str(tmp_path / "missing.json"), "--doctor"]) == 1
    assert "configuration:" in capsys.readouterr().err


def test_write_config_never_overwrites_user_file(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert main(["--write-config"]) == 0
    path = capsys.readouterr().out.strip()
    assert Config.load(path)["history_days"] == 2
    assert main(["--write-config"]) == 1
    assert "File exists" in capsys.readouterr().err
