"""Release probes exercise retained software paths without qualifying hardware."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "raven_trace_qualification", ROOT / "scripts" / "qualify_trace_workspace.py"
)
Q = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(Q)


def test_real_source_probe_preserves_private_lifecycle_and_qualification_limits(
    tmp_path,
):
    result = Q.probe(tmp_path / "probe", expected_package=ROOT)
    assert {
        "verified-journal",
        "cpu-stack-intersection",
        "sample-share",
        "owner-capacity",
        "MPI-protocol-parse",
        "terminal-render",
        "viewer-close",
        "index-revalidation",
        "portable-activity-import",
    } <= set(result["checks"])
    assert result["hardware_qualified"] is False and result["scheduler_calls"] == 0
    assert Path(result["manifest"]).is_file()
    assert (tmp_path / "probe" / "private.sqlite").stat().st_mode & 0o077 == 0


def test_wrong_import_prefix_fails_before_creating_qualification_files(tmp_path):
    target = tmp_path / "uncreated"
    with pytest.raises(ValueError, match="outside its selected"):
        Q.probe(target, expected_package=tmp_path / "wrong-installation")
    assert not target.exists()


def test_existing_evidence_report_is_never_overwritten(tmp_path, monkeypatch):
    report = tmp_path / "report.json"
    report.write_text("retained evidence\n")
    monkeypatch.setattr(
        Q,
        "qualify",
        lambda **kwargs: pytest.fail("existing output started qualification"),
    )
    with pytest.raises(SystemExit) as caught:
        Q.main(["--output", str(report)])
    assert caught.value.code == 2
    assert report.read_text() == "retained evidence\n"
