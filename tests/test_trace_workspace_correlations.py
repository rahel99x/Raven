"""Declared MPI semantics over real journals, not live MPI qualification."""

from copy import deepcopy
import json
from urllib.request import Request, urlopen

import pytest

from raven.tracing.workspace_correlations import correlations


def fixture(*, ranks=2, operation="barrier", count=0, reduction="sum", root=0):
    sources, markers = [], []
    for rank in range(ranks):
        source = {
            "id": f"s{rank}",
            "identity": {
                "job_id": "42",
                "attempt": "one",
                "cluster": "site",
                "node": f"node{rank}",
                "boot_id": f"boot{rank}",
                "pid": 100 + rank,
                "process_start": "start",
                "stream": f"rank{rank}",
                "rank": rank,
            },
            "clock": {"id": f"clock{rank}"},
        }
        sources.append(source)
        for sequence, phase in enumerate(("enter", "complete"), 1):
            stamp = sequence * 100 + rank
            markers.append(
                dict(
                    id=f"s{rank}:{phase}",
                    source_id=f"s{rank}",
                    clock_id=f"clock{rank}",
                    start_ns=stamp,
                    end_ns=stamp,
                    duration_ns=0,
                    kind="collective",
                    name="collective",
                    thread="main",
                    first_seq=sequence,
                    last_seq=sequence,
                    data={
                        "communicator": "MPI_COMM_WORLD",
                        "generation": "world-1",
                        "sequence": 7,
                        "members": list(range(ranks)),
                        "rank": rank,
                        "operation": operation,
                        "count": count,
                        "dtype": "int32",
                        "root": root,
                        "reduction": reduction,
                        "phase": phase,
                    },
                )
            )
    return {
        "schema": "raven.trace-workspace-query/v1",
        "intervals": [],
        "markers": markers,
        "sources": sources,
        "caught_up": True,
        "truncated": False,
        "identity": {"job_id": "42", "attempt": "one", "cluster": "site"},
    }


def edges(result):
    return {(row["source_id"], row["target_id"]) for row in result["links"]}


def test_barrier_audits_existing_signatures_and_links_all_enters_before_returns():
    query = fixture()
    original = deepcopy(query)
    result = correlations(query)
    assert edges(result) == {
        (f"s{a}:enter", f"s{b}:complete") for a in range(2) for b in range(2)
    }
    assert result["audit"]["qualified_groups"] == 1 and not result["unknown"]
    assert all(
        row["relation"] == "precedes" and "Barrier" in row["provenance"]
        for row in result["links"]
    )
    assert query == original


def test_broadcast_is_data_dependency_not_an_implicit_barrier():
    result = correlations(fixture(operation="bcast", count=1))
    assert edges(result) == {
        ("s0:enter", "s0:complete"),
        ("s1:enter", "s1:complete"),
        ("s0:enter", "s1:complete"),
    }
    assert ("s1:enter", "s0:complete") not in edges(result)
    assert all("no implicit barrier" in row["provenance"] for row in result["links"])


@pytest.mark.parametrize("operation", ["bcast", "allreduce"])
def test_zero_count_never_invents_cross_rank_synchronization(operation):
    result = correlations(fixture(operation=operation, count=0))
    assert edges(result) == {("s0:enter", "s0:complete"), ("s1:enter", "s1:complete")}
    assert any(
        row["kind"] == "unsupported-collective-data-dependency"
        for row in result["unknown"]
    )


def test_supported_positive_sum_allreduce_has_input_to_result_dependencies():
    result = correlations(fixture(operation="allreduce", count=1))
    assert len(result["links"]) == 4 and not result["unknown"]
    assert all("Allreduce SUM" in row["provenance"] for row in result["links"])
    result = correlations(fixture(operation="allreduce", count=1, reduction="max"))
    assert len(result["links"]) == 2
    assert result["unknown"]


@pytest.mark.parametrize(
    "change",
    [
        "missing-rank",
        "generation",
        "cluster",
        "signature",
        "owner",
        "thread",
        "replay",
        "reverse",
        "rank",
    ],
)
def test_missing_or_ambiguous_lifetimes_do_not_supply_collective_edges(change):
    query = fixture()
    if change == "missing-rank":
        query["markers"] = query["markers"][:2]
    elif change == "generation":
        for row in query["markers"][2:]:
            row["data"]["generation"] = "other-lifetime"
    elif change == "cluster":
        query["sources"][1]["identity"]["cluster"] = "other-site"
    elif change == "signature":
        query["markers"][2]["data"]["operation"] = "bcast"
    elif change == "owner":
        query["markers"][1]["source_id"] = "s1"
    elif change == "thread":
        query["markers"][1]["thread"] = "other-thread"
    elif change == "replay":
        extra = deepcopy(query["markers"][0])
        extra.update(
            id="duplicate-entry", first_seq=3, last_seq=3, start_ns=201, end_ns=201
        )
        query["markers"].append(extra)
    elif change == "reverse":
        query["markers"][1].update(first_seq=0, last_seq=0)
    elif change == "rank":
        query["sources"][0]["identity"]["rank"] = 99
    result = correlations(query)
    assert not result["links"] and result["unknown"]
    assert all("deadlock" not in row["reason"].lower() for row in result["unknown"])


def test_loss_partial_query_and_boundaries_are_explicit():
    query = fixture()
    query["truncated"] = True
    result = correlations(query)
    assert not result["links"] and result["unknown"]
    query["truncated"] = False
    gap = deepcopy(query["markers"][0])
    gap.update(
        id="gap", kind="gap", data={}, first_seq=0, last_seq=0, start_ns=0, end_ns=0
    )
    query["markers"].append(gap)
    result = correlations(query)
    assert not result["links"] and result["unknown"]


@pytest.mark.parametrize(
    "semantic", [{"communicator": "MPI_COMM_SELF"}, {"intercommunicator": True}]
)
def test_unsupported_communicator_semantics_are_not_descriptive_extensions(semantic):
    query = fixture()
    for row in query["markers"]:
        row["data"].update(semantic)
    result = correlations(query)
    assert not result["links"]
    assert any(
        row["kind"] == "unsupported-collective-communicator"
        for row in result["unknown"]
    )


def test_fanout_is_result_bounded_and_omissions_are_exact():
    result = correlations(fixture(ranks=200), limit=7)
    assert len(result["links"]) == 7 and result["omitted"] == 200 * 200 - 7
    assert any(row["kind"] == "correlation-edge-budget" for row in result["unknown"])
    with pytest.raises(ValueError):
        correlations(fixture(), limit=0)


def test_report_number_does_not_become_a_versioned_launch_contract():
    query = fixture()
    query["markers"] = []
    for identifier, kind in (("host", "host"), ("device", "gpu_kernel")):
        query["intervals"].append(
            dict(
                id=identifier,
                source_id="s0",
                clock_id="clock0",
                kind=kind,
                name=identifier,
                start_ns=10,
                end_ns=20,
                duration_ns=10,
                data={"correlation_id": "7", "external_id": "3"},
            )
        )
    result = correlations(query)
    assert not result["links"]
    assert result["unknown"][0]["kind"] == "unqualified-activity-correlation"
    assert "versioned typed" in result["unknown"][0]["reason"]


def test_actual_journal_markers_reach_terminal_projection_and_browser_causal_api(
    tmp_path,
):
    from raven.tracing.capture import Recorder
    from raven.tracing.workspace_browser import start
    from raven.tracing.workspace_index import TraceIndex
    from raven.tracing.workspace_model import project

    paths = []
    # These are real integrity journals carrying simulated two-node MPI
    # observations. This qualifies the software bridge, not a live MPI run.
    for rank in range(2):
        with Recorder(
            tmp_path,
            job_id="42",
            attempt="one",
            cluster="site",
            identity_extra={
                "node": f"node{rank}",
                "boot_id": f"boot{rank}",
                "rank": rank,
            },
            background_flush=False,
        ) as recorder:
            for phase in ("enter", "complete"):
                recorder.emit(
                    "collective",
                    {
                        "communicator": "MPI_COMM_WORLD",
                        "generation": "same-world",
                        "sequence": 1,
                        "members": [0, 1],
                        "rank": rank,
                        "operation": "barrier",
                        "phase": phase,
                        "dtype": "byte",
                        "count": 0,
                        "root": 0,
                        "reduction": "sum",
                    },
                )
        paths.append(recorder.path)
    with TraceIndex(tmp_path / "private.sqlite") as index:
        catalog = index.refresh(paths)
        assert catalog["caught_up"]
        query = index.query(limit=100)
        collective = [row for row in query["markers"] if row["kind"] == "collective"]
        selected = next(
            row["id"] for row in collective if row["data"]["phase"] == "enter"
        )
        projected = project(query)
        assert (
            len(
                [
                    row
                    for row in projected["intervals"]
                    if row["kind"] == "collective" and row["point"]
                ]
            )
            == 4
        )
        with start(index) as viewer:
            request = Request(
                viewer.origin + "/api/analysis?selected_id=" + selected,
                headers={"Authorization": "Bearer " + viewer.token},
            )
            with urlopen(request, timeout=3) as response:
                result = json.loads(response.read())
        causal = result["result"]["causal_slice"]
        assert len(causal["edges"]) >= 2
        assert all(
            row["source_id"] in {point["id"] for point in collective}
            and row["target_id"] in {point["id"] for point in collective}
            for row in causal["edges"]
        )
        assert any(
            row["timing"]["availability"] == "unavailable" for row in causal["edges"]
        )
        assert not causal["cycles"]
