"""Release probes exercise retained software paths without qualifying hardware."""

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "raven_trace_qualification", ROOT / "scripts" / "qualify_trace_workspace.py"
)
Q = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(Q)
RUNNER_SPEC = importlib.util.spec_from_file_location(
    "raven_trace_bounded_runner", ROOT / "scripts" / "qualify_flagship.py"
)
RUNNER = importlib.util.module_from_spec(RUNNER_SPEC)
RUNNER_SPEC.loader.exec_module(RUNNER)


def test_json_commands_preserve_stderr_without_corrupting_stdout():
    result = RUNNER.run(
        [
            sys.executable,
            "-c",
            "import sys; print('{\"verified\":true}'); print('owner notice', file=sys.stderr)",
        ],
        separate_streams=True,
    )
    assert json.loads(result["stdout"]) == {"verified": True}
    assert result["stderr"] == "owner notice\n"
    merged = RUNNER.run(
        [
            sys.executable,
            "-c",
            "import sys; print('stdout'); print('stderr', file=sys.stderr)",
        ]
    )
    assert isinstance(merged, str) and "stdout" in merged and "stderr" in merged


def test_separate_streams_share_one_budget_and_failure_reaps_owned_child(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(RUNNER, "MAX_OUTPUT", 1000)
    pid_file = tmp_path / "pid"
    with pytest.raises(ValueError, match="output budget"):
        RUNNER.run(
            [
                sys.executable,
                "-c",
                (
                    "import os,sys,time; "
                    "open(sys.argv[1],'w').write(str(os.getpid())); "
                    "sys.stdout.write('x'*600); sys.stdout.flush(); "
                    "sys.stderr.write('y'*600); sys.stderr.flush(); time.sleep(30)"
                ),
                str(pid_file),
            ],
            separate_streams=True,
        )
    assert not Path("/proc", pid_file.read_text()).exists()
    assert (
        RUNNER.run([sys.executable, "-c", "print('{}')"], separate_streams=True)[
            "stdout"
        ]
        == "{}\n"
    )


def test_failed_json_command_keeps_actual_stderr_diagnostic():
    with pytest.raises(ValueError, match="exit diagnosis"):
        RUNNER.run(
            [
                sys.executable,
                "-c",
                "import sys; print('{}'); print('exit diagnosis',file=sys.stderr); sys.exit(7)",
            ],
            separate_streams=True,
        )


def test_standalone_cleanup_requires_owner_confirmation_and_helper_exit(tmp_path):
    directory = tmp_path / "raven" / "allocations"
    directory.mkdir(parents=True)
    path = directory / "owned.json"
    value = {
        "schema": "raven.allocation-receipt/v1",
        "site_kind": "desktop",
        "state": "light",
        "cleanup_uncertain": False,
        "helper_pid": 2147483647,
        "helper_start": "fixture-lifetime",
    }
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    assert Q._standalone_cleanup(tmp_path)["helper_lifetime_exited"] is True
    value["cleanup_uncertain"] = True
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="confirm worker cleanup"):
        Q._standalone_cleanup(tmp_path)
    value.update(
        cleanup_uncertain=False,
        helper_pid=os.getpid(),
        helper_start=Path(f"/proc/{os.getpid()}/stat")
        .read_text()
        .rsplit(")", 1)[1]
        .split()[19],
    )
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="helper survived"):
        Q._standalone_cleanup(tmp_path)
    (directory / "owned.claim").write_text("pending")
    with pytest.raises(ValueError, match="reconciled ownership receipt"):
        Q._standalone_cleanup(tmp_path)


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
