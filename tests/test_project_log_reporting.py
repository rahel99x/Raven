"""The copyable producer indexes multiple log locations without touching logs."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

import pytest

from raven.log_catalog import build_catalog


TEMPLATE = Path(__file__).resolve().parents[1] / "examples" / "project-template"


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "Project with spaces"
    shutil.copytree(TEMPLATE, root)
    spec = importlib.util.spec_from_file_location("project_log_reporting", root / "reporting.py")
    producer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(producer)
    run = producer.begin_run(root, "attempt-1", name="stable-work", script="experiment.py", job_id="12345_7")
    return root, producer, run


def index(run):
    return json.loads((run / "logs.json").read_text())


def test_begin_run_publishes_private_log_index_with_real_identity_and_relative_paths(project):
    _, _, run = project
    document = index(run)
    assert document["schema"] == "raven.logs/v1"
    assert document["run_id"] == "attempt-1" and document["job_id"] == "12345_7"
    assert {row["path"] for row in document["logs"]} == {"logs/stdout.log", "logs/stderr.log"}
    assert json.loads((run / "run.json").read_text())["paths"]["log_index"] == "logs.json"
    assert stat.S_IMODE((run / "logs.json").stat().st_mode) == 0o600


def test_registers_relative_and_external_files_with_group_metadata_and_preserves_content(project, tmp_path):
    _, producer, run = project
    rank = run / "logs" / "rank.log"
    rank.write_text("training evidence\n")
    external = tmp_path / "Batch stderr.log"
    external.write_text("scheduler failure evidence\n")
    before = {path: path.read_bytes() for path in (rank, external)}
    producer.register_log(run, "training.rank-0", "logs/rank.log", label="Rank 0", group="Training")
    producer.register_log(run, "scheduler.stderr", external, label="Launch errors", group="Scheduler",
                          description="The scheduler's actual stderr location")
    document = index(run)
    assert document["logs"][-1] == {"id": "scheduler.stderr", "path": str(external),
        "label": "Launch errors", "group": "Scheduler", "description": "The scheduler's actual stderr location"}
    assert {path: path.read_bytes() for path in before} == before
    assert not list(run.glob(".logs.json.*"))


def test_produced_index_is_read_by_native_catalog_for_exact_failed_job(project, tmp_path):
    _, producer, run = project
    batch_stdout = tmp_path / "batch-12345_7.out"
    batch_stderr = tmp_path / "batch-12345_7.err"
    batch_stdout.write_text("actual scheduler stdout\n")
    batch_stderr.write_text("actual scheduler stderr\n")
    worker = run / "logs" / "rank-0.log"
    worker.write_text("actual failed worker evidence\n")
    producer.register_log(run, "rank-0", "logs/rank-0.log", group="Workers", label="Rank 0")
    producer.register_log(run, "batch.stderr", batch_stderr, group="Scheduler", label="Launch stderr")
    producer.finish_run(run, state="FAILED", runtime_seconds=2, exit_code=1)
    catalog = build_catalog("12345_7", str(batch_stdout), str(batch_stderr), manifest_file=str(run / "logs.json"))
    assert catalog["status"] == "ready" and catalog["messages"] == []
    paths = [entry["path"] for entry in catalog["entries"]]
    assert set(paths) == {str(batch_stdout), str(batch_stderr), str(run / "logs/stdout.log"),
                          str(run / "logs/stderr.log"), str(worker)}
    assert len(paths) == len(set(paths))
    rank = next(entry for entry in catalog["entries"] if entry["path"] == str(worker))
    assert rank["group"] == "Workers" and rank["label"] == "Rank 0"
    wrong = build_catalog("99999", "", "", manifest_file=str(run / "logs.json"))
    assert wrong["entries"] == []
    assert any("does not match" in message for message in wrong["messages"])


def test_log_registration_is_available_for_failed_runs_and_never_rewrites_summary(project):
    _, producer, run = project
    producer.finish_run(run, state="FAILED", runtime_seconds=1.5, exit_code=1)
    before = (run / "summary.json").read_bytes()
    producer.register_log(run, "diagnostics", "logs/diagnostics.log", group="Failure evidence")
    assert index(run)["logs"][-1]["id"] == "diagnostics"
    assert (run / "summary.json").read_bytes() == before
    assert not (run / "logs" / "diagnostics.log").exists()


def test_local_execution_does_not_fabricate_a_scheduler_identity(tmp_path):
    root = tmp_path / "copied"
    shutil.copytree(TEMPLATE, root)
    env = {key: value for key, value in os.environ.items() if not key.startswith(("SLURM_", "RAVEN_"))}
    env.pop("PYTHONPATH", None)
    result = subprocess.run([sys.executable, "-S", "experiment.py", "--run-id", "local",
                             "--steps", "2", "--terms-per-step", "8"], cwd=root, env=env,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    document = index(root / "runs" / "local")
    assert "job_id" not in document and document["run_id"] == "local"
    assert len(document["logs"]) == 2


@pytest.mark.parametrize("kwargs", [
    {"id": "application.stdout", "path": "logs/another.log"},
    {"id": "another", "path": "logs/stdout.log"},
    {"id": "bad/id", "path": "logs/another.log"},
    {"id": "../bad", "path": "logs/another.log"},
    {"id": "a" * 129, "path": "logs/another.log"},
    {"id": "another", "path": "logs/*.log"},
    {"id": "another", "path": "logs/other.log\n"},
    {"id": "another", "path": "logs\\other.log"},
    {"id": "another", "path": "~/other.log"},
    {"id": "another", "path": "a" * 4097},
    {"id": "another", "path": "logs/other.log", "label": "\x1b[31munsafe"},
    {"id": "another", "path": "logs/other.log", "group": "a" * 161},
    {"id": "another", "path": "logs/other.log", "description": "a" * 513},
])
def test_invalid_registration_preserves_existing_index(project, kwargs):
    _, producer, run = project
    before = (run / "logs.json").read_bytes()
    with pytest.raises(ValueError):
        producer.register_log(run, **kwargs)
    assert (run / "logs.json").read_bytes() == before


def test_absolute_alias_of_registered_relative_path_is_refused(project):
    _, producer, run = project
    before = (run / "logs.json").read_bytes()
    with pytest.raises(ValueError, match="unique"):
        producer.register_log(run, "duplicate.path", run / "logs/stdout.log")
    assert (run / "logs.json").read_bytes() == before


def test_explicit_relative_sibling_locations_are_resolved_from_manifest_directory(project):
    root, producer, run = project
    sibling = root / "runs" / "shared-worker.log"
    sibling.write_text("another exact location\n")
    producer.register_log(run, "sibling-worker", "../shared-worker.log", group="Workers")
    assert index(run)["logs"][-1]["path"] == "../shared-worker.log"
    before = (run / "logs.json").read_bytes()
    with pytest.raises(ValueError, match="unique"):
        producer.register_log(run, "same-worker", str(sibling))
    assert (run / "logs.json").read_bytes() == before


@pytest.mark.parametrize("kind", ["file-symlink", "directory-symlink", "fifo", "directory"])
def test_existing_nonregular_paths_and_symlink_ancestors_are_refused_without_opening(project, tmp_path, kind):
    _, producer, run = project
    path = run / "logs" / "unsafe"
    if kind == "file-symlink":
        path.symlink_to(run / "logs/stdout.log")
    elif kind == "directory-symlink":
        path.symlink_to(tmp_path, target_is_directory=True)
        path = path / "not-created.log"
    elif kind == "fifo":
        os.mkfifo(path)
    else:
        path.mkdir()
    before = (run / "logs.json").read_bytes()
    with pytest.raises(ValueError):
        producer.register_log(run, "unsafe", str(path))
    assert (run / "logs.json").read_bytes() == before


@pytest.mark.parametrize("change", [
    lambda doc: doc.update(job_id="99999"),
    lambda doc: doc.update(run_id="another-run"),
    lambda doc: doc.update(schema="raven.logs/v9"),
    lambda doc: doc.update(extra="unsupported"),
    lambda doc: doc["logs"].append(dict(doc["logs"][0])),
    lambda doc: doc["logs"][0].update(label=""),
    lambda doc: doc["logs"][0].update(group=""),
])
def test_tampered_existing_index_is_rejected_before_publication(project, change):
    _, producer, run = project
    document = index(run)
    change(document)
    (run / "logs.json").write_text(json.dumps(document))
    before = (run / "logs.json").read_bytes()
    with pytest.raises(ValueError):
        producer.register_log(run, "another", "logs/another.log")
    assert (run / "logs.json").read_bytes() == before


def test_maximum_log_count_is_bounded_without_silently_dropping_entries(project):
    _, producer, run = project
    for number in range(254):
        producer.register_log(run, f"rank-{number}", f"logs/rank-{number}.log", group="Workers")
    assert len(index(run)["logs"]) == 256
    before = (run / "logs.json").read_bytes()
    with pytest.raises(ValueError, match="bounded"):
        producer.register_log(run, "overflow", "logs/overflow.log")
    assert (run / "logs.json").read_bytes() == before


def test_duplicate_json_keys_do_not_replace_the_previous_index(project):
    _, producer, run = project
    (run / "logs.json").write_text('{"schema":"raven.logs/v1","logs":[],"logs":[]}')
    before = (run / "logs.json").read_bytes()
    with pytest.raises(ValueError, match="duplicate"):
        producer.register_log(run, "another", "logs/another.log")
    assert (run / "logs.json").read_bytes() == before


def test_encoded_log_index_byte_limit_preserves_all_prior_entries(project):
    _, producer, run = project
    for number in range(254):
        before = (run / "logs.json").read_bytes()
        try:
            producer.register_log(run, f"rank-{number}", f"logs/rank-{number}.log",
                                  label="Worker", group="Training", description="𝑥" * 512)
        except ValueError as exc:
            assert "262144-byte" in str(exc)
            assert (run / "logs.json").read_bytes() == before
            assert len(index(run)["logs"]) == number + 2
            assert len(before) <= 262144
            break
    else:
        pytest.fail("UTF-8 log metadata exceeded the native byte budget without rejection")
