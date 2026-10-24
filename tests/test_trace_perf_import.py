"""Native samples remain observations with verified report-local timestamps."""

from __future__ import annotations

from pathlib import Path

import pytest

from raven.tracing import perf_import as P
from raven.tracing.protocol import discover_trace, read_trace
from raven.tracing.workspace_index import TraceIndex
from raven.tracing.workspace_model import project, sample_statistics

REPORT = """# perf script --ns -F comm,pid,tid,cpu,time,period,event,ip,sym,dso
science 42 43 [002] 1.000000017: 99 cpu_core/cycles/:u: 7f20 child+0x10 (/tmp/science)
    7f20 child+0x10 (/tmp/science)
    7e00 parent (/tmp/science)

science 42/43 [003] 1.000000127: 10 task-clock: 7f40 leaf (/tmp/science)
    7e00 parent (/tmp/science)

"""


def imported(tmp_path, report=REPORT, **kwargs):
    source = tmp_path / "perf.txt"
    source.write_text(report)
    root = tmp_path / "project"
    root.mkdir()
    receipt = P.import_perf_script(
        source, project=root, job_id="42", attempt="attempt-a", node="node-a", **kwargs
    )
    return receipt, root, source


def read(path):
    result = read_trace(path)
    assert result["status"] in {"COMPLETE", "PARTIAL"}, result
    return result


def test_actual_report_to_verified_journal_index_and_sample_projection(tmp_path):
    receipt, root, _ = imported(tmp_path)
    assert receipt["samples"] == 2
    assert receipt["status"] == "COMPLETE" and receipt["registered"]
    assert list(map(Path, discover_trace(root, "42", "attempt-a"))) == [
        Path(receipt["path"])
    ]
    result = read(receipt["path"])
    samples = [row for row in result["events"] if row["kind"] == "cpu_sample"]
    assert [row["mono_ns"] for row in samples] == [1_000_000_017, 1_000_000_127]
    assert samples[0]["data"]["frames"] == [
        {"name": "parent", "ip": "7e00", "dso": "/tmp/science"},
        {"name": "child+0x10", "ip": "7f20", "dso": "/tmp/science"},
    ]
    assert samples[0]["data"]["weight_unit"] == "event-count"
    assert samples[1]["data"]["weight_unit"] == "ns"
    assert result["events"][-1]["mono_ns"] == 1_000_000_127
    assert samples[0]["data"]["source_pid"] == 42
    assert result["identity"]["pid"] != 42
    with TraceIndex(tmp_path / "index.sqlite") as index:
        for _ in range(20):
            if index.refresh([receipt["path"]], max_seconds=0.1)["caught_up"]:
                break
        query = index.query()
        assert not query["intervals"]
        assert (
            len([row for row in query["markers"] if row["kind"] == "cpu_sample"]) == 2
        )
        view = project(query, "cpu")
        assert len(view["intervals"]) == 4
        assert all(
            row["duration_ns"] == 0 and row["point"] for row in view["intervals"]
        )
        stats = sample_statistics(query)
        assert stats["observations"] == 2
        assert {row["weight_unit"] for row in stats["groups"]} == {"event-count", "ns"}


def test_partial_stack_and_lost_records_remain_explicit(tmp_path):
    report = (
        REPORT
        + "PERF_RECORD_LOST 5 lost events\nscience 42 43 [004] 1.000000227: cycles: unknown-symbol\n"
    )
    receipt, _, _ = imported(tmp_path, report, complete=False)
    assert receipt["status"] == "PARTIAL"
    assert receipt["partial_stacks"] == 1 and receipt["gaps"] == 1
    result = read(receipt["path"])
    assert result["manifest"]["state"] == "PARTIAL"
    assert any(row["kind"] == "gap" for row in result["events"])
    assert result["events"][-1]["mono_ns"] == 1_000_000_227


@pytest.mark.parametrize(
    "changes",
    [
        dict(job_id=None),
        dict(attempt=""),
        dict(node="bad\nnode"),
        dict(max_samples=True),
        dict(complete="yes"),
        dict(clock={"schema": "other"}),
    ],
)
def test_bad_contract_refused_before_creating_recordings(tmp_path, changes):
    path, root = tmp_path / "perf.txt", tmp_path / "project"
    path.write_text(REPORT)
    root.mkdir()
    options = dict(project=root, job_id="42", attempt="a")
    options.update(changes)
    with pytest.raises(ValueError):
        P.import_perf_script(path, **options)
    assert not (root / ".raven").exists()


@pytest.mark.parametrize(
    "report,match",
    [
        (REPORT.replace("1.000000127", "0.000000127"), "monotonic report order"),
        (REPORT.replace("1.000000017", "1.0000000171"), "integer nanoseconds"),
        ("x" * 16385, "line exceeds"),
        ("not a perf sample\n", "no supported stack"),
    ],
)
def test_invalid_report_never_registers_a_success(tmp_path, report, match):
    source, root = tmp_path / "perf.txt", tmp_path / "project"
    source.write_text(report)
    root.mkdir()
    with pytest.raises(ValueError, match=match):
        P.import_perf_script(source, project=root, job_id="42", attempt="a")
    assert list(discover_trace(root, "42", "a")) == []


def test_sample_budget_is_not_silent_truncation(tmp_path):
    with pytest.raises(ValueError, match="sample count"):
        imported(tmp_path, max_samples=1)
    assert list(discover_trace(tmp_path / "project", "42", "attempt-a")) == []


def test_repeat_import_preserves_original_and_does_not_duplicate_sources(tmp_path):
    receipt, root, path = imported(tmp_path)
    before = Path(receipt["path"]).read_bytes()
    with pytest.raises(FileExistsError):
        P.import_perf_script(
            path, project=root, job_id="42", attempt="attempt-a", node="node-a"
        )
    assert Path(receipt["path"]).read_bytes() == before
    assert len(discover_trace(root, "42", "attempt-a")) == 1


def test_uint64_time_is_exact_and_not_float_rounded():
    row = next(
        P.samples(["science 42/43 [3] 18446744073.709551615: cycles: 7f child (app)\n"])
    )
    assert row["sample_time_ns"] == (1 << 64) - 1
    with pytest.raises(ValueError, match="integer nanoseconds"):
        list(
            P.samples(
                ["science 42/43 [3] 18446744073.709551616: cycles: 7f child (app)\n"]
            )
        )


def test_partial_frames_are_bounded_without_claiming_a_complete_stack():
    report = (
        "science 42/43 [3] 1.000000000: cycles: 7f child (app)\n"
        + " 7f recursive (app)\n" * 3000
    )
    row = next(P.samples(report.splitlines()))
    assert row["stack_truncated"] and row["stack_status"] == "partial"
    assert len(row["frames"]) <= 2048
