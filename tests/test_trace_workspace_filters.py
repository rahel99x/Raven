"""Resource predicates select verified evidence before count/byte limits."""

import pytest

from raven.tracing.capture import Recorder
from raven.tracing.workspace_index import TraceIndex
from raven.tracing.workspace_model import project


def recording(root):
    root.mkdir(mode=0o700)
    return Recorder(
        root,
        job_id="42",
        attempt="a",
        cluster="local",
        background_flush=False,
        initial_mono_ns=0,
    )


def finish(index, paths):
    for _ in range(100):
        result = index.refresh(paths, max_events=1024, max_seconds=0.1)
        if result["caught_up"]:
            return result
    pytest.fail("Filtered fixture did not finish indexing")


def test_process_node_device_task_rank_and_kind_filters_precede_limit(tmp_path):
    with recording(tmp_path / "p") as rec:
        for position in range(20):
            rec.emit(
                "activity",
                {
                    "activity_kind": "gpu_kernel",
                    "name": "earlier",
                    "start_ns": 100 + position * 10,
                    "end_ns": 105 + position * 10,
                    "source_pid": 10,
                    "source_tid": 1,
                    "source_node": "other-node",
                    "device": {"uuid": "GPU-other", "id": "0"},
                },
                thread="report-thread",
                mono_ns=100 + position * 10,
            )
        rec.emit(
            "activity",
            {
                "activity_kind": "gpu_kernel",
                "name": "selected",
                "start_ns": 500,
                "end_ns": 525,
                "source_pid": 20,
                "source_tid": 7,
                "source_node": "selected-node",
                "source_boot_id": "selected-boot",
                "device": {"uuid": "GPU-selected", "id": "9", "ordinal": 3},
                "task_id": "task-a",
                "source_rank": 2,
            },
            thread="report-thread",
            mono_ns=500,
        )
    with TraceIndex(tmp_path / "index.sqlite") as index:
        catalog = finish(index, [rec.path])
        filters = dict(
            node="selected-node",
            process=20,
            thread="7",
            task="task-a",
            rank=2,
            kind="gpu_kernel",
        )
        result = index.query(**filters, device_id="GPU-selected", limit=1)
        assert [row["name"] for row in result["intervals"]] == ["selected"]
        assert not result["truncated"]
        device = project(result, "gpu")["lanes"][0]["device"]["id"]
        for selector in (device, "GPU-selected", "9", "3"):
            assert (
                index.query(**filters, device_id=selector, limit=1)["intervals"][0][
                    "name"
                ]
                == "selected"
            )
        stats = index.functions(**filters, device_id=device)
        assert stats["functions"][0]["inclusive_ns"] == 25
        batch = index.query_many(
            [
                dict(
                    filters,
                    device_id=device,
                    limit=1,
                    clock_id=catalog["bounds"][0]["clock_id"],
                )
            ]
        )
        assert batch["intervals"][0]["name"] == "selected"
        assert "selected-node" in catalog["dimensions"]["node"]
        assert device in catalog["dimensions"]["device_id"]
        assert "20" in catalog["dimensions"]["process"]


def test_cpu_filter_keeps_only_matching_execution_and_required_call_boundaries(
    tmp_path,
):
    with recording(tmp_path / "p") as rec:
        rec.emit("call", {"frame": "f", "function": "work"}, thread="one", mono_ns=100)
        for stamp, cpu in ((110, 2), (140, 1)):
            rec.emit(
                "activity",
                {
                    "activity_kind": "scheduling",
                    "name": "running",
                    "state": "running",
                    "logical_cpu": cpu,
                    "socket_id": 0,
                    "core_id": cpu,
                    "start_ns": stamp,
                    "end_ns": stamp + 20,
                },
                thread="one",
                mono_ns=stamp,
            )
        rec.emit("return", {"frame": "f"}, thread="one", mono_ns=200)
    with TraceIndex(tmp_path / "index.sqlite") as index:
        finish(index, [rec.path])
        result = index.query(logical_cpu=2, kind="python")
        assert {row["kind"] for row in result["intervals"]} == {"python", "scheduling"}
        assert all(row["data"].get("logical_cpu") != 1 for row in result["intervals"])
        assert result["filter_prerequisites"]
        projection = project(result, "cpu")
        slices = [
            row for row in projection["intervals"] if row["kind"] == "cpu_stack_slice"
        ]
        assert len(slices) == 1 and slices[0]["duration_ns"] == 20
        assert slices[0]["elapsed_ns"] == 100
        stats = index.functions(logical_cpu=2, kind="python")
        assert stats["functions"][0]["inclusive_ns"] == 100
        assert any("whole-call elapsed" in item for item in stats["limitations"])
        assert index.catalog()["dimensions"]["logical_cpu"] == [1, 2]


def test_bounded_facets_do_not_limit_direct_queries_or_hide_omission(tmp_path):
    with recording(tmp_path / "p") as rec:
        for position in range(260):
            rec.emit(
                "activity",
                {
                    "activity_kind": "host",
                    "name": str(position),
                    "source_pid": 10000 + position,
                    "source_tid": position,
                    "start_ns": 100 + position * 10,
                    "end_ns": 105 + position * 10,
                },
                mono_ns=100 + position * 10,
            )
    with TraceIndex(tmp_path / "index.sqlite") as index:
        catalog = finish(index, [rec.path])
        assert len(catalog["dimensions"]["process"]) == 256
        assert "process" in catalog["dimensions_truncated"]
        assert index.query(process=10259, limit=1)["intervals"][0]["name"] == "259"
        for bad in (
            dict(logical_cpu=True),
            dict(node=1),
            dict(device_id="bad\nvalue"),
            dict(kinds=[1]),
            dict(kind="host", kinds=["host"]),
            dict(unknown="x"),
        ):
            with pytest.raises(ValueError):
                index.query(**bad)


def test_batch_common_filters_survive_independent_source_selections(tmp_path):
    sources = []
    for name in ("a", "b"):
        with recording(tmp_path / name) as rec:
            rec.emit(
                "activity",
                {
                    "activity_kind": "host",
                    "name": name,
                    "source_pid": 20,
                    "start_ns": 100,
                    "end_ns": 110,
                },
                mono_ns=100,
            )
        sources.append(rec.path)
    with TraceIndex(tmp_path / "index.sqlite") as index:
        catalog = finish(index, sources)
        requests = [
            dict(
                clock_id=source["clock"]["id"],
                source_ids=[source["id"]],
                process="20",
                kind="host",
                limit=1,
            )
            for source in catalog["sources"]
        ]
        result = index.query_many(requests)
        assert result["filters"] == {"process": "20", "kind": "host", "per_range": True}
        assert len(result["intervals"]) == 2
        assert all(scope["filters"]["source_ids"] for scope in result["ranges"])


def test_task_aliases_share_one_activation_without_merging_distinct_tasks(tmp_path):
    with recording(tmp_path / "p") as rec:
        rec.emit(
            "call",
            {"frame": "f", "function": "task", "task_id": "one"},
            thread="t",
            mono_ns=100,
        )
        rec.emit(
            "return", {"frame": "f", "logical_task_id": "one"}, thread="t", mono_ns=120
        )
    with TraceIndex(tmp_path / "index.sqlite") as index:
        finish(index, [rec.path])
        row = index.query(task="one")["intervals"][0]
        assert row["duration_ns"] == 20 and row["self_ns"] == 20
