"""Standalone client publication preserves user files and the reporting contract."""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from raven import research_reporting as reporting


def config(root):
    return json.loads((root / ".raven/config.json").read_text())


def test_vendor_publishes_exact_source_and_preserves_project_configuration(tmp_path):
    (tmp_path / ".raven").mkdir()
    path = tmp_path / ".raven/config.json"
    path.write_text(json.dumps({"site": "lab", "nested": {"value": [1, 2]}}))
    metadata = reporting.vendor_client(tmp_path)
    source = (Path(reporting.__file__).with_name("client.py")).read_bytes()
    assert (tmp_path / "raven.py").read_bytes() == source
    assert metadata == {"api_version": 1, "path": "raven.py", "sha256": hashlib.sha256(source).hexdigest()}
    assert config(tmp_path) == {"site": "lab", "nested": {"value": [1, 2]}, "metrics_client": metadata}
    assert (tmp_path / "raven.py").stat().st_mode & 0o777 == 0o600
    assert reporting.vendor_client(tmp_path) == metadata


def test_changed_client_requires_force_and_keeps_config(tmp_path):
    reporting.vendor_client(tmp_path)
    destination = tmp_path / "raven.py"
    destination.write_text("# researcher's changes\n")
    before = (tmp_path / ".raven/config.json").read_bytes()
    with pytest.raises(ValueError, match="--force"):
        reporting.vendor_client(tmp_path)
    assert destination.read_text() == "# researcher's changes\n"
    assert (tmp_path / ".raven/config.json").read_bytes() == before
    reporting.vendor_client(tmp_path, force=True)
    assert destination.read_bytes() == Path(reporting.__file__).with_name("client.py").read_bytes()


def test_untracked_source_requires_force_but_matching_registered_old_source_upgrades(tmp_path):
    old_source = b"API_VERSION = 0\n"
    destination = tmp_path / "raven.py"
    destination.write_bytes(old_source)
    with pytest.raises(ValueError, match="--force"):
        reporting.vendor_client(tmp_path)
    (tmp_path / ".raven/config.json").write_text(json.dumps({
        "metrics_client": {"path": "raven.py", "api_version": 0,
                           "sha256": hashlib.sha256(old_source).hexdigest()}}))
    assert reporting.vendor_client(tmp_path)["api_version"] == 1
    assert destination.read_bytes() != old_source


@pytest.mark.parametrize("kind", ["destination", "parent", "config", "config-parent"])
def test_vendor_refuses_symlinks_even_with_force(tmp_path, kind):
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "source.py"
    target.write_text("untouched")
    root = tmp_path / "project"
    root.mkdir()
    relative = "raven.py"
    if kind == "destination":
        (root / "raven.py").symlink_to(target)
    elif kind == "parent":
        (root / "app").symlink_to(outside, target_is_directory=True)
        relative = "app/raven.py"
    elif kind == "config":
        (root / ".raven").mkdir()
        (root / ".raven/config.json").symlink_to(target)
    else:
        (root / ".raven").symlink_to(outside, target_is_directory=True)
    with pytest.raises((OSError, ValueError)):
        reporting.vendor_client(root, relative, force=True)
    assert target.read_text() == "untouched"
    assert not (outside / "raven.py").exists()


@pytest.mark.parametrize("path", ["../outside.py", "/outside.py", "part/../raven.py", "raven.json"])
def test_vendor_requires_python_path_inside_project(tmp_path, path):
    with pytest.raises(ValueError):
        reporting.vendor_client(tmp_path, path)
    assert not (tmp_path / ".raven/config.json").exists()


@pytest.mark.parametrize("existing", [False, True])
def test_failed_metadata_publication_restores_previous_source(tmp_path, monkeypatch, existing):
    destination = tmp_path / "raven.py"
    if existing:
        destination.write_text("# old\n")
    (tmp_path / ".raven").mkdir()
    path = tmp_path / ".raven/config.json"
    path.write_text('{"keep": true}\n')
    before = path.read_bytes()
    def fail(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(reporting.R, "_atomic_json", fail)
    with pytest.raises(OSError, match="disk full"):
        reporting.vendor_client(tmp_path, force=True)
    assert path.read_bytes() == before
    assert destination.read_text() == "# old\n" if existing else not destination.exists()
    assert not list(tmp_path.glob(".raven-client-*"))


def test_invalid_configuration_is_not_overwritten(tmp_path):
    (tmp_path / ".raven").mkdir()
    path = tmp_path / ".raven/config.json"
    path.write_text("[]\n")
    with pytest.raises(TypeError, match="JSON object"):
        reporting.vendor_client(tmp_path)
    assert path.read_text() == "[]\n"
    assert not (tmp_path / "raven.py").exists()


def test_vendor_admission_precedes_mutation(tmp_path, monkeypatch):
    def refuse(command):
        assert command == "vendor-client"
        raise ValueError("admission refused")
    monkeypatch.setattr(reporting, "_managed_driver_admission", refuse)
    with pytest.raises(ValueError, match="admission refused"):
        reporting.vendor_client(tmp_path)
    assert not list(tmp_path.iterdir())


def test_vendor_cli_accepts_project_path_and_reports_identity(tmp_path, capsys):
    assert reporting.main(["vendor-client", "--root", str(tmp_path), "--path", "app/raven.py"]) == 0
    metadata = json.loads(capsys.readouterr().out)
    assert metadata["path"] == "app/raven.py"
    assert config(tmp_path)["metrics_client"] == metadata


def test_vendored_copy_runs_without_installed_package_and_emits_valid_metrics(tmp_path):
    reporting.vendor_client(tmp_path)
    program = tmp_path / "application.py"
    program.write_text('''import raven
import sys
assert raven.__raven_vendored__ is True
assert "raven.slurm" not in sys.modules and "curses" not in sys.modules
@raven.measure("loss", kind="objective")
def calculate():
    return 0.25
assert calculate() == 0.25
with raven.phase("solve"):
    raven.metric("residual", 0.01, kind="residual", step=1)
raven.progress(1, 2, unit="steps")
''')
    target = tmp_path / "metrics.jsonl"
    env = {key: value for key, value in os.environ.items() if not key.startswith("RAVEN_")}
    env["RAVEN_METRICS_FILE"] = str(target)
    result = subprocess.run([sys.executable, "-S", str(program)], cwd=tmp_path, env=env,
                            capture_output=True, text=True, timeout=15, check=False)
    assert result.returncode == 0, result.stderr
    from raven.metrics import _record
    records = [_record(json.loads(line)) for line in target.read_text().splitlines()]
    values = {name: value for record in records for name, value in record["metrics"].items()}
    assert values["loss"] == 0.25
    assert values["residual"] == 0.01
    assert values["phase.solve.seconds"] >= 0
    assert any(record.get("progress", {}).get("completed") == 1 for record in records)


def test_vendored_copy_is_inert_without_reporting_environment(tmp_path):
    reporting.vendor_client(tmp_path)
    program = tmp_path / "application.py"
    program.write_text('''import raven
import threading
before = threading.active_count()
@raven.measure("loss")
def calculate():
    return 2
assert calculate() == 2
with raven.phase("solve"):
    raven.metric("loss", 2)
raven.progress(1, 2)
assert threading.active_count() == before
''')
    env = {key: value for key, value in os.environ.items() if not key.startswith("RAVEN_")}
    result = subprocess.run([sys.executable, "-S", str(program)], cwd=tmp_path, env=env,
                            capture_output=True, text=True, timeout=15, check=False)
    assert result.returncode == 0, result.stderr
    assert not list(tmp_path.glob("metrics*"))


@pytest.mark.parametrize("name", ["solve_client.py", "pool.py"])
def test_micro_api_examples_emit_measured_values_from_vendored_client(tmp_path, name):
    reporting.vendor_client(tmp_path)
    source = Path(__file__).resolve().parents[1] / "examples/automated-reporting" / name
    program = tmp_path / name
    program.write_bytes(source.read_bytes())
    target = tmp_path / "metrics.jsonl"
    env = {key: value for key, value in os.environ.items() if not key.startswith("RAVEN_")}
    env["RAVEN_METRICS_FILE"] = str(target)
    result = subprocess.run([sys.executable, "-S", str(program)], cwd=tmp_path, env=env,
                            capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr
    from raven.metrics import _record, read_meta
    records = [_record(json.loads(line)) for line in target.read_text().splitlines()]
    values = {key: value for record in records for key, value in record["metrics"].items()}
    metadata = read_meta(str(target))
    if name == "solve_client.py":
        assert 3 < values["pi_estimate"] < 4
        assert 0 < values["absolute_error"] < 0.01
        assert metadata["kinds"]["absolute_error"] == "error"
        assert metadata["coalesced"]["absolute_error"] > 0
        assert "step=8 " in result.stdout
    else:
        assert values["sum_of_squares"] == sum(index * index for index in range(80000))
        assert values["items_per_second"] > 0
        assert "helper_value" not in values
        assert metadata["kinds"]["items_per_second"] == "throughput"
    assert any(record.get("progress", {}).get("completed") == 8 for record in records)
