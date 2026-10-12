"""Qualification records scoped checks without submitting or silently certifying sites."""
import importlib.util
import json
import os
from pathlib import Path
import sys

import pytest

SPEC = importlib.util.spec_from_file_location("raven_qualify", Path(__file__).resolve().parents[1] / "scripts" / "qualify_flagship.py")
Q = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(Q)


def manifest():
    return {"schema": "raven.site-qualification/v1", "cluster": "lab", "job_id": "123",
            "user": "researcher", "submit_time": "2026-10-10T12:00:00", "start_time": "2026-10-10T12:01:00"}


def test_live_probe_is_exact_read_only_and_does_not_export_identity(tmp_path):
    path = tmp_path / "site.json"
    path.write_text(json.dumps(manifest()))
    calls = []
    def query(argv):
        calls.append(argv)
        return "JobId=123 UserId=researcher(1000) SubmitTime=2026-10-10T12:00:00 StartTime=2026-10-10T12:01:00 JobState=RUNNING WorkDir=/private/data"
    report = Q.check_live(path, query)
    assert calls == [["scontrol", "-M", "lab", "--oneliner", "show", "job", "123"]]
    assert report["state"] == "RUNNING" and len(report["identity_sha256"]) == 64
    assert "researcher" not in json.dumps(report) and "/private" not in json.dumps(report)


def test_array_task_identity_uses_exact_array_fields(tmp_path):
    value = manifest()
    value["job_id"] = "123_7"
    path = tmp_path / "site.json"
    path.write_text(json.dumps(value))
    def query(argv):
        return "JobId=345 ArrayJobId=123 ArrayTaskId=7 UserId=researcher(1000) SubmitTime=2026-10-10T12:00:00 StartTime=2026-10-10T12:01:00 JobState=RUNNING"
    assert Q.check_live(path, query)["state"] == "RUNNING"


@pytest.mark.parametrize("field,value", [("job_id", "123;quit"), ("job_id", "123_[1-7]"), ("job_id", 123),
    ("cluster", "-other"), ("user", "a b"), ("submit_time", "Unknown"), ("start_time", True), ("schema", "other")])
def test_invalid_manifest_never_runs_scheduler_command(tmp_path, field, value):
    document = manifest()
    document[field] = value
    path = tmp_path / "site.json"
    path.write_text(json.dumps(document))
    with pytest.raises(ValueError):
        Q.check_live(path, lambda argv: pytest.fail("invalid identity reached scheduler"))


@pytest.mark.parametrize("mismatch", ["JobId=124", "UserId=other(1000)", "SubmitTime=2026-10-10T11:00:00", "StartTime=2026-10-10T13:00:00"])
def test_changed_attempt_refuses_qualification(tmp_path, mismatch):
    path = tmp_path / "site.json"
    path.write_text(json.dumps(manifest()))
    fields = dict(JobId="123", UserId="researcher(1000)", SubmitTime="2026-10-10T12:00:00", StartTime="2026-10-10T12:01:00")
    key, value = mismatch.split("=", 1)
    fields[key] = value
    with pytest.raises(ValueError, match="identity changed"):
        Q.check_live(path, lambda argv: " ".join(f"{key}={value}" for key, value in fields.items()))


def test_real_subprocess_output_and_deadline_limits(monkeypatch):
    monkeypatch.setattr(Q, "MAX_OUTPUT", 1000)
    with pytest.raises(ValueError, match="output budget"):
        Q.run([sys.executable, "-c", "print('x'*2000)"])
    with pytest.raises(ValueError, match="deadline"):
        Q.run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=.05)
    assert Q.run([sys.executable, "-c", "print('next command')"]).strip() == "next command"


@pytest.mark.parametrize("kind", ["fifo", "symlink", "oversized"])
def test_live_manifest_refuses_special_or_unbounded_sources(tmp_path, kind):
    path = tmp_path / "site.json"
    if kind == "fifo":
        os.mkfifo(path)
    elif kind == "symlink":
        source = tmp_path / "source.json"
        source.write_text(json.dumps(manifest()))
        path.symlink_to(source)
    else:
        path.write_text(" " * 16385)
    with pytest.raises(ValueError):
        Q.check_live(path, lambda argv: pytest.fail("invalid file reached scheduler"))


def test_docs_checker_reports_only_local_missing_paths(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "README.md").write_text("[Policy](docs/policy.md) [Remote](https://example.test/missing) [self](#start)")
    (tmp_path / "CONTRIBUTING.md").write_text("Contribute")
    with pytest.raises(ValueError, match="policy.md"):
        Q.documentation_check(tmp_path)
    (tmp_path / "docs" / "policy.md").write_text("Policy")
    assert Q.documentation_check(tmp_path)["local_links"] == 1


def test_offline_report_retains_external_untested_gates(monkeypatch):
    monkeypatch.setattr(Q, "documentation_check", lambda: {"documents": 2})
    monkeypatch.setattr(Q, "run", lambda *args, **kwargs: "Raven demo")
    monkeypatch.setattr(Q, "terminal_check", lambda *args: {"output_bytes": 1})
    report = Q.qualify()
    assert report["status"] == "qualified-for-recorded-checks"
    assert report["production_qualified"] is False
    checks = {item["id"]: item for item in report["checks"]}
    assert checks["real-pty"]["status"] == "qualified"
    for name in ("installed-wheel", "live-allocation-identity", "physical-gpu", "multi-node", "external-user-study", "scientific-outcomes"):
        assert checks[name]["status"] == "untested"
