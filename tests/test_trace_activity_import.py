"""Actual imported journals preserve device semantics, identity and bounds."""

import hashlib
import json
from pathlib import Path

import pytest

from raven.tracing import activity_import as A
from raven.tracing.protocol import discover_trace, read_json, read_trace


def report(tmp_path, events, **metadata):
    path = tmp_path / "report.json"
    path.write_text(json.dumps({**metadata, "traceEvents": events}))
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    result = A.import_activity(
        path, project=project, job_id="42", attempt="attempt-one"
    )
    snapshot = read_trace(result["manifest"])
    assert snapshot["status"] == "COMPLETE"
    assert snapshot["caught_up"]
    return result, snapshot, project


def event(
    name="kernel", category="kernel", *, stamp=10, duration=4, pid=123, tid=9, **args
):
    return {
        "ph": "X",
        "ts": stamp,
        "dur": duration,
        "name": name,
        "cat": category,
        "pid": pid,
        "tid": tid,
        "args": args,
    }


def activities(snapshot):
    return [row["data"] for row in snapshot["events"] if row["kind"] == "activity"]


def test_imported_real_journal_keeps_gpu_host_copy_clock_and_source_identity(tmp_path):
    result, snapshot, project = report(
        tmp_path,
        [
            event("cudaLaunchKernel", "cuda_runtime", stamp=4, duration=2),
            event(
                "solve",
                "kernel",
                device=0,
                context=1,
                stream=7,
                correlation=17,
                node="node-a",
                device_uuid="GPU-A",
                vendor="nvidia",
            ),
            event(
                "copy",
                "gpu_memcpy",
                stamp=11,
                duration=1,
                device=0,
                stream=8,
                node="node-a",
                device_uuid="GPU-A",
            ),
        ],
    )
    host, kernel, copy = activities(snapshot)
    assert [row["activity_kind"] for row in (host, kernel, copy)] == [
        "host",
        "gpu_kernel",
        "gpu_copy",
    ]
    assert host["duration_ns"] == 2000
    assert kernel["start_ns"] == 10_000 and kernel["end_ns"] == 14_000
    assert kernel["context"] == "1" and kernel["queue"] == "7"
    assert kernel["correlation_id"] == "17"
    assert kernel["device"]["uuid"] == "GPU-A"
    assert kernel["source_pid"] == "123" and kernel["source_tid"] == "9"
    assert kernel["source_node"] == "node-a"
    assert "not verified live process ownership" in kernel["provenance"]["identity"]
    assert result["capture_qualified"] is False
    assert snapshot["covered"]["clock"]["kind"] == "producer-report"
    assert list(map(Path, discover_trace(project, "42", "attempt-one"))) == [
        Path(result["manifest"])
    ]
    assert Path(result["manifest"]).stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "topology",
    [
        [("node-a", 0, "GPU-A", "nvidia")],
        [("node-a", 0, "GPU-A", "nvidia"), ("node-a", 1, "GPU-B", "nvidia")],
        [("node-a", 0, "GPU-A", "nvidia"), ("node-b", 0, "GPU-B", "amd")],
        [
            ("node-a", 0, "GPU-A", "nvidia"),
            ("node-b", 0, "GPU-B", "amd"),
            ("node-b", 1, "GPU-C", "intel"),
            ("node-c", 0, "GPU-D", "apple"),
        ],
    ],
)
def test_device_topology_uses_node_uuid_not_local_ordinal(tmp_path, topology):
    _, snapshot, _ = report(
        tmp_path,
        [
            event("solve", node=node, device=ordinal, device_uuid=uuid, vendor=vendor)
            for node, ordinal, uuid, vendor in topology
        ],
    )
    found = [row["device"] for row in activities(snapshot)]
    assert len({row["id"] for row in found}) == len(topology)
    assert {(row["node"], row["uuid"], row["vendor"]) for row in found} == {
        (node, uuid, vendor) for node, _, uuid, vendor in topology
    }


def test_partition_and_unknown_physical_identity_do_not_merge_process_devices(tmp_path):
    _, snapshot, _ = report(
        tmp_path,
        [
            event(
                device=0, device_uuid="GPU-A", partition="instance-one", node="node-a"
            ),
            event(
                device=0, device_uuid="GPU-A", partition="instance-two", node="node-a"
            ),
            event(device=0, pid=100),
            event(device=0, pid=200),
        ],
    )
    found = [row["device"] for row in activities(snapshot)]
    assert len({row["id"] for row in found}) == 4
    assert all(row["identity_scope"] == "producer-report-process" for row in found[2:])
    assert found[2]["node"] == "unknown" and found[2]["uuid"] is None


def test_device_visibility_remapping_preserves_physical_identity(tmp_path):
    _, snapshot, _ = report(
        tmp_path,
        [
            event(device=0, device_uuid="GPU-A", node="node-a", pid=100),
            event(device=3, device_uuid="GPU-A", node="node-a", pid=200),
        ],
    )
    left, right = [row["device"] for row in activities(snapshot)]
    assert left["id"] == right["id"]
    assert (left["ordinal"], right["ordinal"]) == (0, 3)


def test_metadata_can_follow_events_without_losing_node_or_device_names(tmp_path):
    _, snapshot, _ = report(
        tmp_path,
        [
            event(device=0),
            {"ph": "M", "name": "raven_node", "pid": 123, "args": {"node": "worker-a"}},
            {
                "ph": "M",
                "name": "process_name",
                "pid": 123,
                "args": {"name": "rank zero"},
            },
            {
                "ph": "M",
                "name": "thread_name",
                "pid": 123,
                "tid": 9,
                "args": {"name": "main"},
            },
        ],
        deviceProperties=[
            {"id": 0, "uuid": "GPU-A", "name": "Example GPU", "vendor": "amd"}
        ],
    )
    row = activities(snapshot)[0]
    assert row["source_node"] == "worker-a"
    assert (
        row["source_process_name"] == "rank zero"
        and row["source_thread_name"] == "main"
    )
    assert row["device"]["name"] == "Example GPU"


def test_import_preserves_nanoseconds_above_javascript_integer_precision(tmp_path):
    source = tmp_path / "report.json"
    source.write_text(
        '{"traceEvents":[{"ph":"X","name":"precise","cat":"kernel",'
        '"ts":9007199254740.993,"dur":0.007,"pid":1,"tid":1}]}'
    )
    project = tmp_path / "project"
    project.mkdir()
    receipt = A.import_activity(source, project=project, job_id="42", attempt="one")
    snapshot = read_trace(receipt["manifest"])
    assert snapshot["status"] == "COMPLETE"
    row = activities(snapshot)[0]
    assert row["start_ns"] == (1 << 53) + 1
    assert row["duration_ns"] == 7
    assert row["source_ts_us"] == "9007199254740.993"
    assert row["timestamp_rounding_bound_ns"] == 0
    assert row["measurement_uncertainty_ns"] is None
    assert snapshot["events"][-1]["mono_ns"] == row["end_ns"]


def test_fractional_nanosecond_source_precision_is_retained_and_labelled(tmp_path):
    source = tmp_path / "report.json"
    source.write_text(
        '[{"ph":"X","name":"tiny","cat":"kernel","ts":0.0001,"dur":0.0014}]'
    )
    project = tmp_path / "project"
    project.mkdir()
    receipt = A.import_activity(source, project=project, job_id="42", attempt="one")
    row = activities(read_trace(receipt["manifest"]))[0]
    assert row["source_ts_us"] == "0.0001" and row["source_duration_us"] == "0.0014"
    assert row["timestamp_rounding_bound_ns"] == 1
    assert row["duration_ns"] == row["end_ns"] - row["start_ns"]


def test_nested_producer_boundaries_are_paired_across_interleaved_threads(tmp_path):
    _, snapshot, _ = report(
        tmp_path,
        [
            {"ph": "B", "ts": 1, "name": "outer", "cat": "cpu_op", "pid": 1, "tid": 1},
            {"ph": "B", "ts": 2, "name": "inner", "cat": "cpu_op", "pid": 1, "tid": 1},
            event("parallel", "cpu_op", stamp=2, duration=5, pid=1, tid=2),
            {"ph": "E", "ts": 3, "pid": 1, "tid": 1},
            {"ph": "E", "ts": 9, "pid": 1, "tid": 1},
        ],
    )
    rows = {row["name"]: row for row in activities(snapshot)}
    assert rows["outer"]["duration_ns"] == 8000 and rows["outer"]["depth"] == 0
    assert rows["inner"]["duration_ns"] == 1000 and rows["inner"]["depth"] == 1
    assert rows["parallel"]["source_tid"] == "2"


def test_unmatched_or_unsupported_events_are_reported_without_invented_durations(
    tmp_path,
):
    receipt, snapshot, _ = report(
        tmp_path,
        [
            event("cudaLaunchKernel", "cuda_runtime"),
            {"ph": "B", "ts": 20, "name": "unfinished", "pid": 1, "tid": 1},
            {"ph": "E", "ts": 30, "pid": 2, "tid": 2},
            {"ph": "P", "name": "sample"},
        ],
    )
    assert [row["name"] for row in activities(snapshot)] == ["cudaLaunchKernel"]
    assert receipt["ignored"] == {"P": 1, "unmatched_end": 1, "unmatched_begin": 1}
    assert receipt["warnings"]


def test_flows_respect_global_local_scope_and_never_use_temporal_proximity(tmp_path):
    receipt, snapshot, _ = report(
        tmp_path,
        [
            {
                "ph": "s",
                "ts": 1,
                "name": "launch",
                "cat": "ac2g",
                "pid": 1,
                "tid": 1,
                "id": 7,
            },
            {
                "ph": "f",
                "ts": 2,
                "name": "launch",
                "cat": "ac2g",
                "pid": 0,
                "tid": 3,
                "id": 7,
            },
            {"ph": "s", "ts": 3, "name": "local", "pid": 1, "id2": {"local": 9}},
            {"ph": "f", "ts": 4, "name": "local", "pid": 2, "id2": {"local": 9}},
        ],
    )
    flows = [row["data"] for row in snapshot["events"] if row["kind"] == "flow"]
    assert receipt["counts"]["flows"] == 4
    assert flows[0]["flow_id"] == flows[1]["flow_id"]
    assert flows[2]["flow_id"] != flows[3]["flow_id"]
    assert flows[0]["flow_phase"] == "start" and flows[1]["flow_phase"] == "end"


def test_counter_and_generic_span_do_not_claim_cpu_or_gpu_utilization(tmp_path):
    _, snapshot, _ = report(
        tmp_path,
        [
            event("phase", "application_phase"),
            {
                "ph": "C",
                "ts": 3,
                "name": "memory",
                "args": {"used": 123.5, "note": "ignored"},
            },
        ],
    )
    assert activities(snapshot)[0]["activity_kind"] == "cooperative"
    counter = next(
        row["data"] for row in snapshot["events"] if row["kind"] == "counter"
    )
    assert counter["values"] == {"used": "123.5"}
    assert counter["units"] == "producer-defined"


@pytest.mark.parametrize(
    "raw,match",
    [
        ('{"traceEvents":[],"traceEvents":[]}', "duplicate"),
        ('{"traceEvents":[{"ph":"X","ts":NaN,"dur":1}]}', "finite"),
        ('{"traceEvents":[{"ph":"X","ts":-1,"dur":1}]}', "nonnegative"),
        ('{"traceEvents":[{"ph":"X","ts":1,"dur":-1}]}', "nonnegative"),
        ('{"traceEvents":[{"ph":"X","ts":9223372036854776,"dur":1}]}', "signed-64"),
        ('{"traceEvents":[{"ph":"X","ts":1e999999999,"dur":1}]}', "exponent"),
        ('{"traceEvents":[{"ph":"X","ts":1e999,"dur":1}]}', "signed-64"),
        ('{"traceEvents":[{"ph":"X","ts":1,"dur":1,"pid":1e999}]}', "identifier"),
        (
            '{"traceEvents":[{"ph":"X","ts":1,"dur":1,"cat":"kernel","args":{"device":1e999}}]}',
            "integer",
        ),
        ('{"traceEvents":[{"ph":"X","ts":1,"dur":1,"args":[]}]}', "args"),
        ('{"traceEvents":[{"ph":"X","ts":1,"dur":1},]}', "Incomplete"),
        ('{"traceEvents":[]} trailing', "Trailing"),
        ('{"traceEvents":[[]]}', "objects"),
    ],
)
def test_invalid_reports_fail_before_creating_a_trace(tmp_path, raw, match):
    source = tmp_path / "bad.json"
    source.write_text(raw)
    project = tmp_path / "project"
    project.mkdir()
    with pytest.raises(ValueError, match=match):
        A.import_activity(source, project=project, job_id="42", attempt="one")
    assert not (project / ".raven").exists()


def test_path_bytes_depth_and_event_limits_are_enforced(tmp_path, monkeypatch):
    source = tmp_path / "report.json"
    source.write_text(json.dumps([event()]))
    project = tmp_path / "project"
    project.mkdir()
    link = tmp_path / "linked.json"
    link.symlink_to(source)
    with pytest.raises(
        (ValueError, OSError), match="symlink|symbolic|regular|director"
    ):
        A.import_activity(link, project=project, job_id="42", attempt="one")
    source.write_text(" " * 4097)
    with pytest.raises(ValueError, match="byte budget"):
        A.import_activity(
            source, project=project, job_id="42", attempt="one", max_bytes=4096
        )
    # The outer event array is streamed; one event value may nest at most 32.
    source.write_text("[" * 34 + "0" + "]" * 34)
    with pytest.raises(ValueError, match="nesting"):
        A.import_activity(source, project=project, job_id="42", attempt="one")
    source.write_text(json.dumps([event(), event()]))
    monkeypatch.setattr(A, "MAX_EVENTS", 1)
    with pytest.raises(ValueError, match="events"):
        A.import_activity(source, project=project, job_id="42", attempt="one")
    assert not (project / ".raven").exists()


def test_duplicate_import_never_overwrites_or_duplicates_registered_evidence(tmp_path):
    receipt, _, project = report(tmp_path, [event()])
    manifest = Path(receipt["manifest"])
    before = hashlib.sha256(manifest.read_bytes()).hexdigest()
    with pytest.raises(FileExistsError):
        A.import_activity(
            tmp_path / "report.json",
            project=project,
            job_id="42",
            attempt="attempt-one",
        )
    assert hashlib.sha256(manifest.read_bytes()).hexdigest() == before
    assert list(map(Path, discover_trace(project, "42", "attempt-one"))) == [manifest]


def test_journal_budget_failure_is_not_registered_as_a_success(tmp_path, monkeypatch):
    source = tmp_path / "report.json"
    source.write_text(json.dumps([event(stamp=n) for n in range(12)]))
    project = tmp_path / "project"
    project.mkdir()
    original = A._ReportRecorder

    def limited(*args, **kwargs):
        return original(*args, **{**kwargs, "max_bytes": 4096})

    monkeypatch.setattr(A, "_ReportRecorder", limited)
    with pytest.raises(ValueError, match="journal storage budget"):
        A.import_activity(source, project=project, job_id="42", attempt="one")
    assert discover_trace(project, "42", "one") == []
    manifest = next((project / ".raven" / "traces").glob("*/manifest.json"))
    assert read_json(manifest)["state"] == "ERROR"


def test_registration_contention_retains_imported_evidence_and_recovery_command(
    tmp_path, monkeypatch
):
    from raven.tracing import capture

    def busy(*args):
        raise TimeoutError("Registry is busy; retry later")

    monkeypatch.setattr(capture, "register_trace", busy)
    receipt, snapshot, project = report(tmp_path, [event()])
    assert snapshot["status"] == "COMPLETE"
    assert receipt["registered"] is False
    assert "Registry is busy" in receipt["registration_error"]
    assert receipt["registration_argv"] == [
        "raven",
        "trace",
        "register",
        "--project",
        str(project),
        receipt["manifest"],
    ]
    assert discover_trace(project, "42", "attempt-one") == []


def test_runtime_device_metadata_can_enrich_but_not_contradict_physical_identity(
    tmp_path,
):
    source = tmp_path / "report.json"
    source.write_text(
        json.dumps(
            {
                "deviceProperties": [{"id": 0, "uuid": "GPU-A"}],
                "traceEvents": [event(device=0)],
            }
        )
    )
    project = tmp_path / "project"
    project.mkdir()
    with pytest.raises(ValueError, match="conflicts"):
        A.import_activity(
            source,
            project=project,
            job_id="42",
            attempt="one",
            device_metadata=[{"id": 0, "uuid": "GPU-B"}],
        )
    assert not (project / ".raven").exists()
    result = A.import_activity(
        source,
        project=project,
        job_id="42",
        attempt="one",
        node="node-a",
        device_metadata=[{"id": 0, "uuid": "GPU-A", "vendor": "nvidia"}],
    )
    assert activities(read_trace(result["manifest"]))[0]["device"]["vendor"] == "nvidia"


def test_global_profiler_markers_keep_unknown_identity_instead_of_importer_pid(
    tmp_path,
):
    _, snapshot, _ = report(
        tmp_path,
        [
            event(category="cpu_op"),
            {
                "ph": "i",
                "s": "g",
                "name": "Record Window End",
                "ts": 20,
                "pid": "",
                "tid": "",
            },
        ],
    )
    marker = next(
        row["data"] for row in snapshot["events"] if row["kind"] == "activity_marker"
    )
    assert marker["source_pid"] == "unknown" and marker["source_tid"] == "unknown"
    assert marker["producer_scope"] == "g"


def test_owned_profiler_session_shares_clock_across_reports_and_rejects_epoch_change(
    tmp_path,
):
    from raven.tracing.workspace_index import TraceIndex

    clock = {
        "schema": "raven.trace-clock/v1",
        "kind": "producer-session",
        "id": "framework-relative",
        "session_id": "one-owned-runtime",
        "producer": {
            "node": "compute-a",
            "boot_id": "boot-a",
            "pid": 123,
            "process_start": "100",
        },
    }
    project = tmp_path / "project"
    project.mkdir()
    paths = []
    for number in range(2):
        source = tmp_path / f"window-{number}.json"
        source.write_text(
            json.dumps(
                {
                    "baseTimeNanoseconds": 1000,
                    "traceEvents": [event(stamp=10 + number * 10)],
                }
            )
        )
        receipt = A.import_activity(
            source, project=project, job_id="42", attempt="one", clock=clock
        )
        clock = receipt["clock"]
        paths.append(receipt["manifest"])
    with TraceIndex(tmp_path / "private.sqlite") as index:
        catalog = index.refresh(paths)
        assert catalog["caught_up"]
        assert len(catalog["bounds"]) == 1
        assert all(
            source["clock"]["alignment"] == "producer-session"
            for source in catalog["sources"]
        )
    source.write_text(
        json.dumps({"baseTimeNanoseconds": 2000, "traceEvents": [event(stamp=40)]})
    )
    with pytest.raises(ValueError, match="epoch changed"):
        A.import_activity(
            source, project=project, job_id="42", attempt="one", clock=clock
        )
    assert len(discover_trace(project, "42", "one")) == 2


def test_report_import_does_not_accept_an_unvalidated_clock_override(tmp_path):
    source = tmp_path / "report.json"
    source.write_text(json.dumps([event()]))
    with pytest.raises(ValueError, match="producer session"):
        A.import_activity(
            source,
            project=tmp_path,
            job_id="42",
            attempt="one",
            clock={"kind": "monotonic", "id": "unproved-shared-clock"},
        )
    assert not (tmp_path / ".raven").exists()
