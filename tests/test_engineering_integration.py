"""Installed entry boundaries and reviewed project workflows, not live sites."""

from dataclasses import replace
import importlib
import json
import threading

import pytest

from raven import operations as O
from raven.engineering import cli, reliability, service
from raven.entry import launcher
from raven.schedulers import NAMES


@pytest.mark.parametrize("entry", [launcher.light_main, launcher.heavy_main])
@pytest.mark.parametrize("domain", ["engineering", "scheduler"])
def test_catalog_never_requests_an_allocation_or_config(
    entry, domain, monkeypatch, capsys
):
    def forbidden(*args, **kwargs):
        pytest.fail(
            "Catalog/help requested an allocation or read dashboard configuration"
        )

    monkeypatch.setattr(launcher, "_allocation", forbidden)
    monkeypatch.setattr(launcher, "resolve", forbidden)
    assert entry([domain, "catalog"]) == 0
    value = json.loads(capsys.readouterr().out)
    assert value["schema"].startswith("raven.")


def test_registry_contains_only_real_public_callbacks():
    for domain, actions in service.SERVICES.items():
        module = importlib.import_module("raven.engineering." + domain)
        for action in actions:
            assert callable(getattr(module, action)), (domain, action)
    assert set(NAMES) == {
        "slurm",
        "kubernetes",
        "lsf",
        "openpbs",
        "pbspro",
        "htcondor",
        "flux",
    }
    assert "oracle" not in NAMES


def test_request_cannot_select_python_imports_or_inject_runtime_dependency():
    with pytest.raises(ValueError, match="Unknown"):
        service.execute("os", "system", {"command": "false"})
    with pytest.raises(ValueError, match="Unknown"):
        service.execute("experiments", "__dict__", {})
    with pytest.raises(ValueError, match="dependency"):
        service.execute(
            "qualification",
            "prepare",
            {"scheduler": {}, "project": "/invalid"},
            dependencies={"scheduler": object()},
        )


@pytest.fixture
def ctx(tmp_path):
    return O.Context(
        scope={"cluster": "lab", "user": "researcher"},
        state_dir=str(tmp_path / "state"),
        cancel=threading.Event(),
    )


def test_experiment_form_reviews_before_creating_and_roundtrips_cli(
    tmp_path, ctx, capsys
):
    project = tmp_path / "project"
    project.mkdir()
    result = O.run(
        "engineering-experiments",
        {
            "action": "create",
            "root": str(project),
            "experiment_id": "science-1",
            "title": "Two resource layouts",
        },
        ctx,
    )
    assert result["plan"] and not (project / ".raven").exists()
    applied = O.apply("engineering-experiments", result["plan"], ctx)
    assert applied["data"]["experiment_id"] == "science-1"
    assert (
        cli.main(
            ["experiments", "summarize", "--project", str(project), "--id", "science-1"]
        )
        == 0
    )
    summary = json.loads(capsys.readouterr().out)
    assert summary["attempts"] == 0


def test_changed_source_or_connection_cannot_apply_review(tmp_path, ctx):
    project = tmp_path / "project"
    project.mkdir()
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"project": str(project), "experiment_id": "a"}))
    result = O.run(
        "engineering-experiments", {"action": "create", "request": str(request)}, ctx
    )
    with pytest.raises(ValueError, match="connection changed"):
        O.apply(
            "engineering-experiments",
            result["plan"],
            replace(ctx, scope={"cluster": "different"}),
        )
    request.write_text(json.dumps({"project": str(project), "experiment_id": "b"}))
    with pytest.raises(ValueError, match="changed"):
        O.apply("engineering-experiments", result["plan"], ctx)
    assert not (project / ".raven").exists()


def test_engineering_workbench_has_finite_task_and_shared_fields():
    from raven.command_registry import task
    from raven.operation_adapters import engineering

    expected = {
        "native-scheduler",
        *("engineering-" + name for name in service.SERVICES),
    }
    assert set(task("engineering").operations) == expected
    assert {row["key"] for row in engineering.SPECIFICATIONS} == expected
    for row in engineering.SPECIFICATIONS:
        assert len(row["fields"]) == len({field["key"] for field in row["fields"]})
        assert all(field.get("label") for field in row["fields"])


def test_native_queue_result_has_readable_rows_and_freshness():
    from raven.operation_adapters.engineering import _rows

    result = {
        "records": [{"id": "3.0", "state": "RUNNING", "name": "model", "user": "alex"}],
        "capabilities": {
            "read_stats": {"requests": 1, "cache_hits": 0, "last_read": 123}
        },
    }
    rows = _rows(result)
    assert any("3.0 | RUNNING | model | alex" in row for row in rows)
    assert any("last successful read: 123" in row for row in rows)


def test_missing_reliability_evidence_never_passes_and_rss_is_separate():
    result = reliability.evaluate({})
    assert result["status"] == "incomplete"
    assert all(item["status"] == "unmeasured" for item in result["metrics"])
    report = reliability.evaluate(
        {
            "measurements": {
                "process_rss_mib": {
                    "value": 512,
                    "samples": 3,
                    "source": "process high water RSS",
                },
                "python_peak_mib": {"value": 1, "samples": 3, "source": "tracemalloc"},
            }
        }
    )
    rows = {row["metric"]: row for row in report["metrics"]}
    assert rows["process_rss_mib"]["status"] == "regression"
    assert rows["python_peak_mib"]["status"] == "pass"
    assert report["status"] == "regression"


@pytest.mark.parametrize("value", [True, -1, float("inf"), float("nan")])
def test_reliability_rejects_invalid_numbers(value):
    with pytest.raises(ValueError):
        reliability.evaluate({"budgets": {"input_p95_ms": value}})


def test_private_output_is_no_clobber_and_rejects_symlink_parents(tmp_path):
    from raven.scheduler_cli import save_new

    path = tmp_path / "review.json"
    save_new(path, {"valid": True})
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        save_new(path, {"valid": False})
    assert json.loads(path.read_text()) == {"valid": True}
    link = tmp_path / "link"
    link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises((ValueError, OSError)):
        save_new(link / "unsafe.json", {})


def test_compute_service_in_light_does_not_fall_back_to_local_work(monkeypatch):
    from raven.execution.policy import configure_process, process_router, reset_process

    previous = process_router()
    configure_process({"managed": True, "profile": "light", "site_kind": "slurm"})
    try:
        monkeypatch.setattr(
            service,
            "execute",
            lambda *a, **k: pytest.fail("Light executed compute locally"),
        )
        result = cli.execute("performance", "analyze", {"query": {}})
        assert result["status"] == "deferred"
    finally:
        reset_process(previous)


def test_invalid_arguments_and_scope_fail_cleanly_before_state_creation(
    tmp_path, capsys
):
    from raven.scheduler_cli import client

    path = tmp_path / "bad.json"
    path.write_text('{"arguments": []}')
    assert cli.main(["experiments", "create", "--request", str(path), "--id", "x"]) == 2
    assert "JSON object" in capsys.readouterr().err
    with pytest.raises(ValueError, match="scope"):
        client({"name": "slurm", "scope": None})
    for arguments in (
        {"project": str(tmp_path), "scheduler": {}},
        {"project": str(tmp_path), "_inspection": False},
    ):
        with pytest.raises(ValueError, match="private arguments"):
            service.execute("qualification", "prepare", arguments)
    assert not (tmp_path / ".raven").exists()


def test_incomplete_reliability_is_not_a_successful_release_gate(tmp_path, capsys, ctx):
    path = tmp_path / "metrics.json"
    path.write_text('{"document": {"measurements": {}}}')
    assert cli.main(["reliability", "evaluate", "--request", str(path)]) == 3
    assert json.loads(capsys.readouterr().out)["status"] == "incomplete"
    result = O.run(
        "engineering-reliability", {"action": "evaluate", "request": str(path)}, ctx
    )
    assert result["status"] == "partial"
    path.write_text(
        json.dumps(
            {
                "document": {
                    "measurements": {
                        "input_p95_ms": {
                            "value": 1000,
                            "samples": 5,
                            "source": "test fixture",
                        }
                    }
                }
            }
        )
    )
    result = O.run(
        "engineering-reliability", {"action": "evaluate", "request": str(path)}, ctx
    )
    assert result["status"] == "error"


def test_shared_shell_composer_preserves_directives_and_literal_values():
    from raven.shell_environment import with_environment
    from raven.operation_adapters.campaigns import _with_environment

    assert _with_environment is with_environment
    script = "#!/bin/bash\n#SBATCH --account=LabX\n#PBS -l select=1\nexec run\n"
    result = with_environment(script, {"RAVEN_CHECKPOINT": "a $(false) ' b"})
    assert result.startswith(
        "#!/bin/bash\n#SBATCH --account=LabX\n#PBS -l select=1\nexport "
    )
    assert result.endswith("exec run\n")
    with pytest.raises(ValueError, match="valid names"):
        with_environment(script, {"PATH; false": "x"})


def test_workbench_renders_real_metrics_missing_sources_and_read_errors():
    from raven.engineering.presentation import rows

    report = {
        "attempts": 2,
        "totals": {"cpu_seconds": {"known_sum": 123.5, "covered_attempts": 1}},
        "device_activity": {"available": False, "missing_reason": "No GPU source"},
        "coverage": {
            "read_errors": [{"read_error": "Temporary I/O error: retry later"}]
        },
    }
    rendered = "\n".join(rows(report))
    assert "123.5" in rendered and "covered attempts: 1" in rendered
    assert (
        "No GPU source" in rendered and "Temporary I/O error: retry later" in rendered
    )
    assert report["totals"]["cpu_seconds"]["known_sum"] == 123.5


def test_large_workbench_result_has_explicit_display_bound():
    from raven.engineering.presentation import MAX_ROWS, rows

    result = {"data": [{"value": i} for i in range(50000)]}
    rendered = rows(result)
    assert len(rendered) <= MAX_ROWS
    assert any("full values retained in Copy report" in line for line in rendered)
    assert len(result["data"]) == 50000


@pytest.mark.parametrize(
    "result",
    [
        {"state": "unknown"},
        {"state": "accepted", "cancellation": {"state": "unknown"}},
        {"result": {"state": "unknown"}, "execution_receipt": {"status": "completed"}},
    ],
)
def test_unknown_native_outcomes_are_preserved_in_cli_and_ui(result):
    assert service.result_status(result) == "partial"
