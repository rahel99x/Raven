"""Captured launch results are inspectable without becoming a submit action."""
import copy
from pathlib import Path

import pytest

from raven import launch, launch_results, launch_runtime, projects, reporting
from raven.research import ResearchHub


@pytest.fixture
def recorded(tmp_path):
    script = tmp_path / "batch.sh"
    script.write_text("#!/bin/bash\nprintf 'application result\\n'\n")
    plan = launch.resolve_plan([str(script), "--project", str(tmp_path), "--local", "--research", "submit", "scaling"], environ={})
    receipt = launch_runtime.execute_plan(plan, environ={"PATH": "/usr/bin:/bin"})
    run = Path(receipt["run_dir"])
    bound = projects.select_run(str(tmp_path), run.name)["binding"]
    report = reporting._read_json(run / "reports" / "submit.json")
    return run, bound, report


def test_actual_launch_is_readable_in_submit_without_mutating_prepared_action(recorded, monkeypatch):
    run, binding, report = recorded
    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("reading launch results must not run a command"))
    hub = ResearchHub({})
    previous = {"explicit_prepared_plan": "retained"}
    hub.plan = previous
    try:
        result = hub._read({"view": "submit", "snap": {}, "binding": binding,
                            "settings": {"submit_file": str(run / "reports" / "submit.json")}})
        assert result["display_only"] is True
        assert result["launch_report"]["receipt"]["application_exit_code"] == 0
        assert result["launch_report"]["receipt"]["scheduler_outcome"] == "unknown"
        assert hub.plan is previous
        assert launch_results.validate(report, binding) is report
    finally:
        hub.close()


@pytest.mark.parametrize("key,value", [("run_id", "other-run"), ("job_id", "10"), ("attempt", 2),
                                      ("cluster", "other-cluster"), ("token", "0" * 32)])
def test_report_for_another_exact_attempt_is_rejected(recorded, key, value):
    _, binding, report = recorded
    report = copy.deepcopy(report)
    report["receipt"][key] = value
    with pytest.raises(ValueError, match="attempt|token|cluster"):
        launch_results.validate(report, binding)


@pytest.mark.parametrize("ascii_", [False, True])
@pytest.mark.parametrize("width", [20, 80])
def test_results_wrap_and_preserve_readiness_explanation(recorded, width, ascii_):
    _, _, report = recorded
    rows = launch_results.rows(report, width, ascii_=ascii_)
    assert all(len(text) <= width for row in rows for text, _ in row)
    text = "".join(text.strip() for row in rows for text, _ in row)
    assert "Capturedlaunch" in text.replace(" ", "")
    assert "Scaling" in text and "needs input" in text
    assert "Viewingitdoesnotsubmitanotherjob." in text.replace(" ", "")
