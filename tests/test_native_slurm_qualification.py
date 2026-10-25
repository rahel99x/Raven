"""Qualification harness controls; live scheduler results are separate evidence."""

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


WRAPPER = load("native_slurm_qualifier", ROOT / "scripts/qualify_native_slurm.py")
DRIVER = load("native_slurm_driver", ROOT / "scripts/slurm/native_scheduler.py")


def test_plan_never_contacts_docker_or_scheduler(tmp_path, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail("Plan must not contact a process or scheduler")

    monkeypatch.setattr(WRAPPER.qualify.__globals__["subprocess"], "run", forbidden)
    output = tmp_path / "uncreated"
    assert WRAPPER.main(["--plan", "--output", str(output)]) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["scenario"] == "native-scheduler"
    assert document["production_qualified"] is False
    assert not output.exists()


@pytest.mark.parametrize("option", ["--scenario", "--scenario=lifecycle"])
def test_wrapper_cannot_be_retargeted_to_another_scenario(option):
    with pytest.raises(ValueError, match="own disposable scenario"):
        WRAPPER.main([option])


def test_native_driver_rejects_ordinary_host_before_process_or_state(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(DRIVER.os, "getuid", lambda: 12345)
    monkeypatch.setattr(
        DRIVER.subprocess,
        "run",
        lambda *a, **k: pytest.fail("No process on ordinary host"),
    )
    output = tmp_path / "uncreated"
    with pytest.raises(SystemExit) as exc:
        DRIVER.main(["--output", str(output), "--revision", "a" * 40])
    assert exc.value.code == 2
    assert not output.exists()


def test_child_requires_fixture_login_before_project_writes(tmp_path, monkeypatch):
    monkeypatch.setattr(DRIVER.os, "getuid", lambda: 12345)
    with pytest.raises(ValueError, match="disposable fixture"):
        DRIVER.qualify(tmp_path, "a" * 40)
    assert list(tmp_path.iterdir()) == []


def test_source_evidence_hashes_delivered_code_and_driver(tmp_path, monkeypatch):
    monkeypatch.setattr(DRIVER, "SOURCE", tmp_path)
    for path, data in (
        ("raven/__init__.py", "# source\n"),
        ("scripts/slurm/native_scheduler.py", "# driver\n"),
        ("scripts/slurm/entrypoint.sh", "# fixture\n"),
    ):
        file = tmp_path / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(data)
    first = DRIVER.source_evidence()
    assert set(first["files"]) == {
        "raven/__init__.py",
        "scripts/slurm/native_scheduler.py",
        "scripts/slurm/entrypoint.sh",
    }
    (tmp_path / "raven/__init__.py").write_text("# changed\n")
    second = DRIVER.source_evidence()
    assert first["tree_sha256"] != second["tree_sha256"]
    assert first["files"]["raven/__init__.py"] != second["files"]["raven/__init__.py"]


def test_ci_invokes_native_adapter_qualification_with_artifact():
    workflow = (ROOT / ".github/workflows/slurm.yml").read_text()
    assert "scripts/qualify_native_slurm.py --skip-build" in workflow
    assert "slurm-native-${{ matrix.version }}-${{ github.sha }}" in workflow
    assert "raven/schedulers/**" in workflow
    assert "raven/engineering/**" in workflow


def test_accounting_wait_preserves_async_absence_until_actual_records_arrive(
    monkeypatch,
):
    clock = [0]
    monkeypatch.setattr(DRIVER.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        DRIVER.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )

    class Client:
        reads = 0

        def jobs(self, *, history=False):
            assert history is True
            self.reads += 1
            return [] if self.reads < 3 else [{"id": "1"}, {"id": "2"}]

    client = Client()
    assert DRIVER.history_until(client, {"1", "2"}) == [{"id": "1"}, {"id": "2"}]
    assert client.reads == 3 and clock[0] == 2


def test_accounting_wait_does_not_invent_missing_records_at_deadline(monkeypatch):
    clock = [0]
    monkeypatch.setattr(DRIVER.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        DRIVER.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )

    class Client:
        def jobs(self, *, history=False):
            return [{"id": "1"}]

    with pytest.raises(ValueError, match="deadline"):
        DRIVER.history_until(Client(), {"1", "2"}, timeout=2)
    assert clock[0] == 2
