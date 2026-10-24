"""Published trace workspace controls, clock fidelity and terminal interaction."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from raven import layout as L
from raven.tracing import workspace_ui as UI

SECOND = 1_000_000_000


def app():
    messages, saves = [], []
    return (
        SimpleNamespace(say=messages.append, save=lambda: saves.append(True)),
        messages,
        saves,
    )


def test_closed_chunks_follow_until_known_job_completion_and_resume_is_explicit():
    state = {}
    owner, _, _ = app()
    first = snapshot(complete=True)
    first.update(collection_following=True, job_finished=False)
    UI.publish(state, first)
    UI.command(owner, state, ["live"])
    original = UI.query_options(state)
    between = snapshot(complete=True, end=110 * SECOND)
    between.update(collection_following=True, job_finished=False)
    UI.publish(state, between)
    assert state["workspace_view"]["mode"] == "live"
    assert UI.query_options(state)["end_ns"] > original["end_ns"]
    before_completion = UI.query_options(state)
    finished = dict(between, job_finished=True)
    UI.publish(state, finished)
    assert state["workspace_view"]["mode"] == "explore"
    assert UI.query_options(state) == before_completion
    UI.command(owner, state, ["live"])
    UI.publish(state, finished)
    assert state["workspace_view"]["mode"] == "live"


def snapshot(*, complete=False, extra_clock=False, end=100 * SECOND):
    base = (1 << 62) + 31
    identity = dict(job_id="42", attempt="submit/start", cluster="local")
    sources = [
        dict(
            id="source-a",
            identity=dict(identity, node="node-a", pid=101, rank=0),
            status="COMPLETE" if complete else "LIVE",
            coverage=dict(adapter="python-monitoring"),
            clock=dict(id="clock-a"),
        )
    ]
    rows = []
    for index, thread in enumerate(("main", "worker")):
        rows.append(
            dict(
                id=f"source-a:python:{index + 1}",
                source_id="source-a",
                clock_id="clock-a",
                thread=thread,
                depth=0,
                kind="python",
                name=f"{thread}_solve",
                file="/project/solver.py",
                line=12,
                first_seq=index * 2 + 1,
                last_seq=index * 2 + 2,
                lane_id=f"lane-{thread}",
                start_ns=base + 90 * SECOND,
                end_ns=base + 91 * SECOND,
                duration_ns=SECOND,
                self_ns=SECOND // 2,
            )
        )
    rows.append(
        dict(
            id="source-a:gpu:5",
            source_id="source-a",
            clock_id="clock-a",
            thread="main",
            depth=0,
            kind="gpu_kernel",
            name="diffusion_kernel",
            device=dict(uuid="GPU-uuid-a", vendor="nvidia", ordinal=0),
            context="context-1",
            queue="stream-2",
            lane_id="gpu-lane",
            start_ns=base + 92 * SECOND,
            end_ns=base + 93 * SECOND,
            duration_ns=SECOND,
            first_seq=5,
            last_seq=6,
        )
    )
    marker = dict(
        id="source-a:cpu:7",
        source_id="source-a",
        clock_id="clock-a",
        thread="worker",
        kind="cpu_observation",
        name="observed placement",
        start_ns=base + 94 * SECOND,
        end_ns=base + 94 * SECOND + 10,
        duration_ns=10,
        seq=7,
        socket_id=1,
        core_id=3,
        logical_cpu=11,
    )
    bounds = [dict(clock_id="clock-a", start_ns=base, end_ns=base + end)]
    if extra_clock:
        sources.append(
            dict(
                id="source-b",
                identity=dict(identity, node="node-b", pid=101, rank=1),
                status="COMPLETE" if complete else "LIVE",
                clock=dict(id="clock-b"),
            )
        )
        rows.append(
            dict(
                rows[0],
                id="source-b:python:1",
                source_id="source-b",
                clock_id="clock-b",
                lane_id="second-node-lane",
                start_ns=7,
                end_ns=17,
                duration_ns=10,
            )
        )
        bounds.append(dict(clock_id="clock-b", start_ns=7, end_ns=77))
    return dict(
        identity=identity,
        sources=sources,
        bounds=bounds,
        intervals=rows,
        markers=[marker],
        caught_up=True,
        warnings=[],
        truncated=False,
    )


def rendered(state, width=80, ascii_only=True):
    return UI.render(SimpleNamespace(g=L.Glyphs(ascii_only)), state, "42", width)


def text(state, width=80):
    return "\n".join(L.row_text(row) for row in rendered(state, width)[0])


def test_live_delta_fit_and_explore_preserve_integer_clock_and_capture():
    state, (owner, messages, _) = {}, app()
    source = snapshot()
    UI.publish(state, source)
    opts = UI.query_options(state)
    base = source["bounds"][0]["start_ns"]
    assert opts["start_ns"] == base + 90 * SECOND
    assert opts["end_ns"] == base + 100 * SECOND
    UI.command(owner, state, ["delta", "250ms"])
    assert UI.query_options(state)["start_ns"] == base + 100 * SECOND - 250_000_000
    UI.command(owner, state, ["pause"])
    frozen = dict(UI.query_options(state))
    UI.publish(state, snapshot(end=101 * SECOND))
    assert UI.query_options(state) == frozen
    assert (
        state["paused"] is False
    )  # View pause leaves the background/capture path usable.
    UI.command(owner, state, ["fit"])
    assert UI.query_options(state)["start_ns"] == base
    assert UI.query_options(state)["end_ns"] == base + 101 * SECOND
    UI.publish(state, snapshot(end=102 * SECOND))
    assert UI.query_options(state)["end_ns"] == base + 101 * SECOND
    UI.command(owner, state, ["live"])
    assert UI.query_options(state)["end_ns"] == base + 102 * SECOND
    assert not messages


def test_simultaneous_threads_render_and_select_full_recorded_details():
    state, (owner, _, saves) = {}, app()
    UI.publish(state, snapshot())
    result = text(state)
    assert "Thread main" in result and "Thread worker" in result
    rows, hits = rendered(state)
    interval_hit = next(
        value
        for _, _, value in hits
        if value[0][1]["command"].endswith("source-a:python:2")
    )
    UI.command(owner, state, interval_hit[0][1]["command"].split()[2:])
    result = text(state)
    assert "Selected: worker_solve" in result
    assert "1s recorded elapsed" in result
    assert "500ms self elapsed" in result
    assert "/project/solver.py:12" in result
    assert UI.initialize(state)["mode"] == "explore"
    assert saves
    assert all(
        0 <= y < len(rows) and 0 <= value[1] < value[2] <= 80 for y, _, value in hits
    )


def test_completion_keeps_last_live_range_selection_and_expansion():
    state, (owner, _, _) = {}, app()
    UI.publish(state, snapshot())
    UI.command(owner, state, ["frame", "source-a:python:2"])
    UI.command(owner, state, ["live"])
    view = UI.initialize(state)
    view["collapsed"] = ["0123456789abcdef"]
    before = UI.query_options(state)
    UI.publish(state, snapshot(complete=True, end=110 * SECOND))
    assert UI.initialize(state)["selected"] == "source-a:python:2"
    assert UI.initialize(state)["collapsed"] == ["0123456789abcdef"]
    assert UI.initialize(state)["mode"] == "explore"
    assert UI.query_options(state) == before


def test_run_attempt_change_clears_only_source_specific_state():
    state, (owner, _, _) = {}, app()
    first = snapshot()
    UI.publish(state, first)
    UI.command(owner, state, ["delta", "30s"])
    UI.command(owner, state, ["frame", "source-a:python:1"])
    UI.command(owner, state, ["bookmark"])
    UI.command(owner, state, ["projection", "gpu"])
    second = deepcopy(first)
    second["identity"]["attempt"] = "new/attempt"
    UI.publish(state, second)
    view = UI.initialize(state)
    assert (
        view["selected"] is None and view["bookmarks"] == [] and view["window"] is None
    )
    assert view["delta_ns"] == 30 * SECOND and view["projection"] == "gpu"
    assert "Selected:" not in text(state)


def test_independent_clock_domains_have_separate_axes():
    state, (owner, _, _) = {}, app()
    UI.publish(state, snapshot(extra_clock=True))
    assert UI.query_options(state)["clock_id"] == "clock-a"
    assert "2 clock domains" in text(state)
    assert "No cross-node simultaneity is inferred" in text(state)
    UI.command(owner, state, ["clock-next"])
    opts = UI.query_options(state)
    assert opts["clock_id"] == "clock-b"
    assert opts["start_ns"] == 7 and opts["end_ns"] == 77
    assert "Node node-b" in text(state) and "Node node-a" not in text(state)


def test_cpu_points_and_gpu_activity_do_not_relabel_host_elapsed_time():
    state, (owner, _, _) = {}, app()
    UI.publish(state, snapshot())
    UI.command(owner, state, ["projection", "cpu"])
    result = text(state)
    assert "Socket 1" in result and "Core 3" in result and "Logical CPU 11" in result
    assert "not continuous residency" in result
    UI.command(owner, state, ["frame", "source-a:cpu:7"])
    assert "10ns query bracket" in text(state)
    UI.command(owner, state, ["projection", "gpu"])
    result = text(state)
    assert "GPU GPU-uuid-a" in result and "Stream stream-2" in result
    UI.command(owner, state, ["frame", "source-a:gpu:5"])
    assert "Selected: diffusion_kernel" in text(state)
    assert "main_solve" not in result.split("Selected:")[0]


@pytest.mark.parametrize(
    "value",
    ["NaN", "Infinity", "0", "-1", "0.0001ms", "86401s", "1e100000000000000000000000"],
)
def test_invalid_delta_never_mutates_or_crashes_view(value):
    state, (owner, messages, _) = {}, app()
    UI.publish(state, snapshot())
    before = UI.initialize(state)["delta_ns"]
    assert UI.command(owner, state, ["delta", value])
    assert messages and UI.initialize(state)["delta_ns"] == before


@pytest.mark.parametrize("width", [1, 7, 8, 22, 40, 80])
@pytest.mark.parametrize("ascii_only", [True, False])
def test_all_controls_and_unicode_bars_remain_in_their_painted_cells(width, ascii_only):
    state = {}
    value = snapshot()
    value["intervals"][0]["name"] = "solver_界\x1b[2J\u202e"
    UI.publish(state, value)
    rows, hits = rendered(state, width, ascii_only)
    assert all(L.vlen(L.row_text(row)) <= width for row in rows)
    assert "\x1b" not in "\n".join(L.row_text(row) for row in rows)
    assert all(
        0 <= y < len(rows) and 0 <= value[1] < value[2] <= width for y, _, value in hits
    )


def test_group_arrows_collapse_expand_and_bookmarks_are_real_controls():
    state, (owner, _, _) = {}, app()
    UI.publish(state, snapshot())
    rows, hits = rendered(state, ascii_only=False)
    group_hit = next(
        value for _, _, value in hits if "workspace group" in value[0][1]["command"]
    )
    assert any("▾" in L.row_text(row) for row in rows)
    UI.command(owner, state, group_hit[0][1]["command"].split()[2:])
    assert "▸" in "\n".join(
        L.row_text(row) for row in rendered(state, ascii_only=False)[0]
    )
    UI.command(owner, state, ["expand"])
    assert not UI.initialize(state)["collapsed"]
    UI.command(owner, state, ["pause"])
    UI.command(owner, state, ["bookmark"])
    bookmark = deepcopy(UI.initialize(state)["bookmarks"][0])
    UI.command(owner, state, ["pan-left"])
    UI.command(owner, state, ["bookmarks-next"])
    assert UI.initialize(state)["window"] == bookmark["window"]


def test_query_bounds_errors_and_missing_selection_are_truthful():
    state, (owner, messages, _) = {}, app()
    value = snapshot()
    value["truncated"] = True
    state["error"] = "temporary read error: retry later"
    UI.publish(state, value)
    UI.initialize(state)["selected"] = "source-a:missing"
    result = text(state)
    assert "temporary read error: retry later" in result
    assert "Range query reached its event bound" in result
    assert "outside this query" in result
    assert UI.command(owner, state, ["frame", "not-a-current-event"])
    assert messages and "no longer" in messages[-1]
    assert UI.initialize(state)["selected"] == "source-a:missing"


def test_paint_does_not_open_or_reconstruct_recordings(monkeypatch):
    state = {}
    UI.publish(state, snapshot())

    def forbidden(*args, **kwargs):
        raise AssertionError("paint attempted I/O")

    monkeypatch.setattr("builtins.open", forbidden)
    rows, _ = rendered(state)
    assert rows


def test_persisted_malformed_optional_choices_do_not_crash_renderer():
    state = dict(
        workspace_view=dict(
            mode={},
            projection={},
            delta_ns=True,
            query=[],
            lane_page=True,
            collapsed=[{}, "bad"],
            bookmarks=[None, dict(window=[0, 0])],
        )
    )
    UI.publish(state, snapshot())
    rows, _ = rendered(state)
    assert rows
    assert UI.initialize(state)["projection"] == "execution"


def test_legacy_timeline_live_delta_all_threads_and_keyboard_selection():
    from raven.tracing import timeline_ui as old
    from test_trace_timeline_ui import ready

    state = ready(
        [
            ("call", "outer", 0),
            ("call", "worker", 90 * SECOND, "worker"),
            ("return", "worker", 91 * SECOND, "worker"),
            ("return", "outer", 100 * SECOND),
        ]
    )
    owner, messages, _ = app()
    assert old.command(owner, state, ["delta", "250ms"])
    assert old.command(owner, state, ["live"])
    view = old.initialize(state)
    model = state["result"]["timeline"]
    assert old._window(model, view) == (100 * SECOND - 250_000_000, 100 * SECOND)
    old.command(owner, state, ["fit"])
    assert view["mode"] == "explore" and view["window"] is None
    old.command(owner, state, ["threads"])
    rows, _ = old.render(SimpleNamespace(g=L.Glyphs(True)), state, "42", 80)
    content = "\n".join(L.row_text(row) for row in rows)
    assert "Thread main" in content and "Thread worker" in content
    old.command(owner, state, ["thread-next"])
    assert not view["all_threads"] and view["thread"] == "main"
    old.command(owner, state, ["live"])
    old.command(owner, state, ["next"])
    assert view["mode"] == "explore"
    assert not messages


def test_viewport_pan_zoom_and_point_only_clocks_remain_valid_index_requests():
    state, (owner, _, _) = {}, app()
    source = snapshot()
    source["bounds"] = [dict(clock_id="clock-a", start_ns=0, end_ns=100)]
    source["sources"][0]["status"] = "COMPLETE"
    UI.publish(state, source)
    UI.command(owner, state, ["pan-left"])
    options = UI.query_options(state)
    assert options["start_ns"] == 0 and options["end_ns"] == 100
    UI.command(owner, state, ["zoom-out"])
    options = UI.query_options(state)
    assert options["start_ns"] == 0 and options["end_ns"] == 200
    source["bounds"] = [
        dict(clock_id="clock-a", start_ns=(1 << 64) - 1, end_ns=(1 << 64) - 1)
    ]
    UI.publish(state, source)
    UI.command(owner, state, ["fit"])
    assert UI.query_options(state)["end_ns"] == (1 << 64) - 1
    assert rendered(state)[0]


def test_overview_click_queries_exact_range_and_cannot_switch_to_an_unknown_clock():
    import hashlib

    state, (owner, messages, _) = {}, app()
    value = snapshot()
    UI.publish(state, value)
    base = value["bounds"][0]["start_ns"]
    state["workspace_overview"] = dict(
        domains=[
            dict(
                clock_id="clock-a",
                bins=[
                    dict(start_ns=base, end_ns=base + 50 * SECOND, count=1),
                    dict(
                        start_ns=base + 50 * SECOND, end_ns=base + 100 * SECOND, count=3
                    ),
                ],
            )
        ]
    )
    rows, hits = rendered(state)
    assert "Overview density" in "\n".join(L.row_text(row) for row in rows)
    hit = next(
        value for _, _, value in hits if "workspace range" in value[0][1]["command"]
    )
    UI.command(owner, state, hit[0][1]["command"].split()[2:])
    assert UI.query_options(state)["start_ns"] == base
    assert UI.query_options(state)["end_ns"] == base + 50 * SECOND
    before = dict(UI.query_options(state))
    UI.command(
        owner,
        state,
        [
            "range",
            hashlib.sha256(b"different-clock").hexdigest()[:16],
            str(base),
            str(base + SECOND),
        ],
    )
    assert UI.query_options(state) == before and "no longer" in messages[-1]


def test_keyboard_navigation_follows_current_resource_projection():
    state, (owner, _, _) = {}, app()
    UI.publish(state, snapshot())
    UI.command(owner, state, ["projection", "gpu"])
    UI.command(owner, state, ["next"])
    assert UI.initialize(state)["selected"] == "source-a:gpu:5"
    UI.command(owner, state, ["projection", "cpu"])
    UI.command(owner, state, ["next"])
    assert UI.initialize(state)["selected"] == "source-a:cpu:7"


def test_real_recording_index_projection_and_terminal_inspection(tmp_path):
    from raven.tracing.capture import Recorder
    from raven.tracing.workspace_index import TraceIndex
    from raven.tracing.workspace_model import project

    (tmp_path / "project").mkdir()
    with Recorder(
        tmp_path / "project",
        job_id="42",
        attempt="real-attempt",
        background_flush=False,
    ) as recorder:
        for thread in ("main", "worker"):
            recorder.emit(
                "call",
                dict(
                    function="solver_" + thread,
                    frame="f-" + thread,
                    file="/project/solve.py",
                    line=20,
                ),
                thread=thread,
            )
            recorder.emit(
                "return",
                dict(function="solver_" + thread, frame="f-" + thread),
                thread=thread,
            )
    state, (owner, _, _) = {}, app()
    with TraceIndex(tmp_path / "private-index.sqlite") as index:
        catalog = index.refresh([recorder.path], max_events=100, max_seconds=2)
        assert catalog["caught_up"]
        query = index.query()
        query["workspace_projection"] = project(query, "execution")
        query["workspace_projection_name"] = "execution"
        UI.publish(state, query)
        state["workspace_overview"] = index.overview()
        state["workspace_functions"] = index.functions()
        UI.command(owner, state, ["functions"])
        UI.command(owner, state, ["next"])
        selected = UI.initialize(state)["selected"]
        state["workspace_detail"] = index.detail(selected)
        rows, hits = rendered(state)
        result = "\n".join(L.row_text(row) for row in rows)
        assert "solver_main" in result and "solver_worker" in result
        assert "Function totals" in result and "Overview density" in result
        assert "Selected: solver_" in result and "/project/solve.py:20" in result
        assert hits and UI.initialize(state)["mode"] == "explore"


def test_multi_node_view_displays_independent_local_axes_and_fair_live_requests():
    state, (owner, _, _) = {}, app()
    value = snapshot(extra_clock=True)
    UI.publish(state, value)
    UI.command(owner, state, ["projection", "node"])
    requests = UI.query_requests(state)
    assert {request["clock_id"] for request in requests} == {"clock-a", "clock-b"}
    assert sum(request["limit"] for request in requests) <= 2000
    first = next(request for request in requests if request["clock_id"] == "clock-a")
    second = next(request for request in requests if request["clock_id"] == "clock-b")
    assert first["start_ns"] == value["bounds"][0]["start_ns"] + 90 * SECOND
    assert second["start_ns"] == 7 and second["end_ns"] == 77
    content = text(state)
    assert "Node node-a" in content and "Node node-b" in content
    assert (
        "Clock clock-a / local axis" in content
        and "Clock clock-b / local axis" in content
    )
    assert "No cross-node simultaneity is inferred" in content
    assert "independent local windows" in content
    UI.command(owner, state, ["pause"])
    before = UI.query_requests(state)
    newer = snapshot(extra_clock=True, end=110 * SECOND)
    newer["bounds"][1]["end_ns"] = 177
    UI.publish(state, newer)
    assert UI.query_requests(state) == before
    UI.command(owner, state, ["bookmark"])
    saved = deepcopy(UI.initialize(state)["domain_windows"])
    UI.command(owner, state, ["zoom-in"])
    assert UI.initialize(state)["domain_windows"] != saved
    UI.command(owner, state, ["bookmarks-next"])
    assert UI.initialize(state)["domain_windows"] == saved


def test_collapsing_all_groups_is_complete_and_individual_expansion_remains_possible():
    state, (owner, _, _) = {}, app()
    UI.publish(state, snapshot())
    UI.command(owner, state, ["collapse"])
    rows, hits = rendered(state, ascii_only=False)
    assert "▸" in "\n".join(L.row_text(row) for row in rows)
    assert "main_solve" not in "\n".join(L.row_text(row) for row in rows)
    hit = next(
        value for _, _, value in hits if "workspace group" in value[0][1]["command"]
    )
    UI.command(owner, state, hit[0][1]["command"].split()[2:])
    rows, _ = rendered(state, ascii_only=False)
    assert "▾" in "\n".join(L.row_text(row) for row in rows)
    UI.command(owner, state, ["expand"])
    assert not UI.initialize(state)["collapsed"]


def test_compact_toolbar_reveals_advanced_controls_without_hiding_their_actions():
    state, (owner, _, _) = {}, app()
    UI.publish(state, snapshot())
    rows, hits = rendered(state)
    content = "\n".join(L.row_text(row) for row in rows)
    assert (
        "More" in content
        and "Custom window" not in content
        and "Functions" not in content
    )
    more = next(
        value
        for _, _, value in hits
        if value[0][1]["command"].endswith("workspace controls")
    )
    UI.command(owner, state, more[0][1]["command"].split()[2:])
    content = text(state)
    assert "Custom window" in content and "Functions" in content
    UI.command(owner, state, ["controls"])
    assert not UI.initialize(state)["advanced"]


def test_sample_frame_selection_resolves_original_record_without_invented_call_time():
    from raven.tracing.workspace_model import project

    state, (owner, _, _) = {}, app()
    value = snapshot()
    base = value["bounds"][0]["start_ns"]
    sample = dict(
        id="source-a:sample:1",
        source_id="source-a",
        clock_id="clock-a",
        thread="worker",
        kind="cpu_sample",
        name="sample",
        start_ns=base + 95 * SECOND,
        end_ns=base + 95 * SECOND,
        duration_ns=0,
        data=dict(
            logical_cpu=11,
            weight=500,
            weight_unit="ns",
            frames=[
                dict(name="root_fn", ip="0x100", dso="/lib/solver.so"),
                dict(name="leaf_fn", ip="0x200", dso="/lib/solver.so"),
            ],
        ),
    )
    value["markers"].append(sample)
    value["workspace_projection"] = project(value, "execution")
    value["workspace_projection_name"] = "execution"
    UI.publish(state, value)
    frame = next(
        row
        for row in value["workspace_projection"]["intervals"]
        if row.get("kind") == "cpu_sample" and row["name"] == "leaf_fn"
    )
    assert UI.command(owner, state, ["frame", frame["id"]])
    assert UI.detail_request(state) == sample["id"]
    assert UI.symbol_request(state) == dict(event_id=sample["id"], frame_index=1)
    state["workspace_detail"] = sample
    content = text(state)
    assert "Selected: leaf_fn" in content and "per-call duration is unknown" in content
    assert (
        "root_fn" in content
        and "leaf_fn" in content
        and "Sampling weight: 500 ns" in content
    )
    assert "0ns recorded elapsed" not in content
    assert UI.initialize(state)["selected"] == frame["id"]


def test_preferences_roundtrip_preserves_exact_run_bookmarks_and_discards_non_preferences():
    import json

    state, (owner, _, _) = {}, app()
    UI.publish(state, snapshot(extra_clock=True))
    UI.command(owner, state, ["projection", "node"])
    UI.command(owner, state, ["pause"])
    UI.command(owner, state, ["bookmark"])
    UI.command(owner, state, ["controls"])
    saved = json.loads(json.dumps(UI.save_preferences(state)))
    saved["untrusted_extension"] = dict(command="do not execute")
    other = {}
    restored = UI.restore_preferences(other, saved)
    assert "untrusted_extension" not in restored
    assert restored["bookmarks"] == UI.initialize(state)["bookmarks"]
    assert restored["domain_windows"] == UI.initialize(state)["domain_windows"]
    assert restored["advanced"] and restored["all_clocks"]
    assert other.get("workspace_result") is None
    UI.publish(other, snapshot(extra_clock=True))
    assert UI.initialize(other)["bookmarks"] == saved["bookmarks"]
    changed = snapshot(extra_clock=True)
    changed["identity"]["attempt"] = "requeued-attempt"
    UI.publish(other, changed)
    assert UI.initialize(other)["bookmarks"] == []


def test_explicit_explore_range_does_not_jump_as_an_index_rebuilds():
    state, (owner, _, _) = {}, app()
    full = snapshot(complete=True)
    UI.publish(state, full)
    UI.command(owner, state, ["pause"])
    chosen = UI.query_options(state)
    partial = snapshot(complete=True, end=20 * SECOND)
    partial["caught_up"] = False
    UI.publish(state, partial)
    assert UI.query_options(state) == chosen
    assert UI.initialize(state)["mode"] == "explore"


def test_calibrated_reference_is_explicit_and_all_clocks_remain_raw():
    state, (owner, messages, _) = {}, app()
    value = snapshot(extra_clock=True)
    value["aligned_bounds"] = [
        dict(clock_id="reference-clock", start_ns=1000, end_ns=9000)
    ]
    UI.publish(state, value)
    assert UI.query_options(state)["clock_id"] == "clock-a"
    UI.command(owner, state, ["clock", "reference-clock"])
    assert UI.query_options(state)["clock_id"] == "reference-clock"
    assert UI.query_options(state)["start_ns"] == 1000
    assert UI.query_options(state)["end_ns"] == 9000
    assert "Calibrated reference axis" in text(state)
    UI.command(owner, state, ["clocks-all"])
    assert {row["clock_id"] for row in UI.query_requests(state)} == {
        "clock-a",
        "clock-b",
    }
    before = UI.save_preferences(state)
    UI.command(owner, state, ["clock", "unknown"])
    assert "current catalog" in messages[-1]
    assert UI.save_preferences(state) == before


def test_calibrated_inspection_preserves_raw_time_and_uncertainty():
    state, (owner, _, _) = {}, app()
    value = snapshot(complete=True)
    value["aligned_bounds"] = [
        dict(clock_id="reference-clock", start_ns=1000, end_ns=9000)
    ]
    row = value["intervals"][0]
    row.update(
        raw_clock_id=row["clock_id"],
        raw_start_ns=row["start_ns"],
        raw_end_ns=row["end_ns"],
        clock_id="reference-clock",
        start_ns=2000,
        end_ns=3000,
        duration_ns=1000,
        alignment_uncertainty_ns=17,
        alignment_provenance="paired pilot calibration",
    )
    UI.publish(state, value)
    UI.command(owner, state, ["clock", "reference-clock"])
    UI.command(owner, state, ["frame", row["id"]])
    content = text(state)
    assert "Calibration uncertainty: ±17ns" in content
    assert "Original clock clock-a" in content and "paired pilot calibration" in content
    UI.command(owner, state, ["fit"])
    assert UI.query_options(state)["start_ns"] == 1000


def test_reference_fit_unions_native_reference_sources_and_transformed_ranges():
    state, (owner, _, _) = {}, app()
    value = snapshot(complete=True)
    value["bounds"] = [dict(clock_id="reference-clock", start_ns=100, end_ns=500)]
    value["aligned_bounds"] = [
        dict(clock_id="reference-clock", start_ns=1000, end_ns=9000)
    ]
    UI.publish(state, value)
    UI.command(owner, state, ["clock", "reference-clock"])
    UI.command(owner, state, ["fit"])
    assert UI.query_options(state)["start_ns"] == 100
    assert UI.query_options(state)["end_ns"] == 9000


def test_recorded_parent_child_navigation_rejects_independent_activity():
    state, (owner, messages, _) = {}, app()
    value = snapshot(complete=True)
    parent = value["intervals"][0]
    parent.update(start_ns=100, end_ns=1000, duration_ns=900)
    child = dict(
        parent,
        id="source-a:python:child",
        lane_id="child-lane",
        name="child_solve",
        depth=1,
        start_ns=200,
        end_ns=300,
        duration_ns=100,
        first_seq=2,
        last_seq=3,
    )
    unrelated = dict(
        child, id="independent-gpu", kind="gpu_kernel", name="not-a-python-child"
    )
    value["intervals"] = [parent, child, unrelated]
    value["bounds"] = [dict(clock_id="clock-a", start_ns=100, end_ns=1000)]
    UI.publish(state, value)
    UI.command(owner, state, ["frame", parent["id"]])
    UI.command(owner, state, ["child"])
    assert UI.initialize(state)["selected"] == child["id"]
    UI.command(owner, state, ["parent"])
    assert UI.initialize(state)["selected"] == parent["id"]
    UI.command(owner, state, ["frame", unrelated["id"]])
    UI.command(owner, state, ["parent"])
    assert UI.initialize(state)["selected"] == unrelated["id"]
    assert "No unambiguous recorded parent" in messages[-1]


def test_pin_lane_survives_collapse_and_preferences_then_resets_with_attempt():
    state, (owner, _, _) = {}, app()
    value = snapshot()
    UI.publish(state, value)
    UI.command(owner, state, ["frame", "source-a:python:2"])
    UI.command(owner, state, ["pin"])
    UI.command(owner, state, ["collapse"])
    content = text(state)
    assert "Pinned lanes" in content and "worker_solve" in content
    assert "main_solve" not in content.split("Selected:")[0]
    restored = {}
    UI.restore_preferences(restored, UI.save_preferences(state))
    UI.publish(restored, value)
    assert UI.initialize(restored)["pinned"] == ["lane-worker"]
    other = deepcopy(value)
    other["identity"]["attempt"] = "another"
    UI.publish(restored, other)
    assert UI.initialize(restored)["pinned"] == []


def test_source_and_thread_filters_are_index_requests_and_immediately_hide_stale_rows():
    state, (owner, _, _) = {}, app()
    value = snapshot(extra_clock=True)
    UI.publish(state, value)
    UI.command(owner, state, ["projection", "node"])
    UI.command(owner, state, ["source", "source-a"])
    UI.command(owner, state, ["thread", "worker"])
    for query in UI.query_requests(state):
        assert query["source_ids"] == ["source-a"] and query["thread"] == "worker"
    content = text(state)
    assert "Thread worker" in content and "Thread main" not in content
    assert "Node node-b" not in content
    UI.command(owner, state, ["source", "all"])
    UI.command(owner, state, ["thread", "all"])
    assert "source_ids" not in UI.query_options(
        state
    ) and "thread" not in UI.query_options(state)


def test_exact_event_link_outside_range_and_bookmark_restore_raw_detail_id():
    state, (owner, _, _) = {}, app()
    UI.publish(state, snapshot(complete=True))
    identifier = "source-a:python:outside"
    UI.command(owner, state, ["event", identifier])
    assert UI.detail_request(state) == identifier
    assert "outside this query" in text(state)
    detail = dict(
        snapshot()["intervals"][0],
        id=identifier,
        name="outside_range",
        start_ns=100,
        end_ns=200,
    )
    state["workspace_detail"] = detail
    assert "Selected: outside_range" in text(state)
    UI.command(owner, state, ["bookmark", "Before", "synchronization"])
    UI.command(owner, state, ["frame", "source-a:python:1"])
    UI.command(owner, state, ["bookmarks-next"])
    assert UI.detail_request(state) == identifier
    assert UI.initialize(state)["label"] == "Before synchronization"
    restored = {}
    UI.restore_preferences(restored, UI.save_preferences(state))
    assert UI.initialize(restored)["bookmarks"][0]["selected_detail_id"] == identifier
    assert UI.initialize(restored)["bookmarks"][0]["label"] == "Before synchronization"


def test_switching_runs_restores_exact_attempt_context_and_persisted_history():
    state, (owner, _, _) = {}, app()
    first = snapshot(complete=True)
    UI.publish(state, first)
    UI.command(owner, state, ["frame", "source-a:python:2"])
    UI.command(owner, state, ["pin"])
    UI.command(owner, state, ["bookmark", "Baseline phase"])
    UI.command(owner, state, ["analysis", "call-tree"])
    chosen = UI.query_options(state)
    saved_bookmarks = deepcopy(UI.initialize(state)["bookmarks"])
    second = deepcopy(first)
    second["identity"]["job_id"] = "99"
    UI.publish(state, second)
    assert UI.initialize(state)["selected"] is None
    UI.command(owner, state, ["delta", "30s"])
    UI.publish(state, first)
    assert UI.query_options(state) == chosen
    view = UI.initialize(state)
    assert view["selected"] == "source-a:python:2" and view["pinned"] == ["lane-worker"]
    assert view["analysis_mode"] == "call-tree" and view["bookmarks"] == saved_bookmarks
    other = {}
    UI.restore_preferences(other, UI.save_preferences(state))
    UI.publish(other, second)
    UI.publish(other, first)
    assert UI.query_options(other) == chosen
    assert UI.initialize(other)["bookmarks"] == saved_bookmarks


def test_run_history_is_bounded_and_malformed_or_mismatched_entries_are_ignored():
    import json

    state, (owner, _, _) = {}, app()
    for number in range(40):
        value = snapshot(complete=True)
        value["identity"]["job_id"] = str(number)
        UI.publish(state, value)
        UI.command(owner, state, ["bookmark", "Run", str(number)])
    saved = UI.save_preferences(state)
    assert len(saved["runs"]) <= 32
    assert len(json.dumps(saved["runs"]).encode()) <= 512 << 10
    key = next(iter(saved["runs"]))
    saved["runs"][key]["run_key"] = "different identity"
    saved["runs"]["not-an-identity-hash"] = saved["runs"][key]
    other = {}
    UI.restore_preferences(other, saved)
    assert key not in other["workspace_runs"]
    assert "not-an-identity-hash" not in other["workspace_runs"]


def test_analysis_and_symbol_reports_are_published_only_and_match_current_selection():
    state, (owner, _, _) = {}, app()
    UI.publish(state, snapshot(complete=True))
    UI.command(owner, state, ["frame", "source-a:python:1"])
    before_revision = state["workspace_revision"]
    UI.command(owner, state, ["analysis", "metrics"])
    assert state["workspace_revision"] == before_revision + 1
    assert "Preparing this analysis" in text(state)

    state["workspace_analysis"] = dict(
        mode="metrics",
        report=dict(
            scope="same covered range",
            metrics=[
                dict(
                    name="physical_core_busy",
                    value=None,
                    reason="No scheduler execution evidence",
                    unit="percent",
                )
            ],
        ),
    )
    assert "No scheduler execution evidence" in text(state)
    state["workspace_symbol"] = dict(
        event_id="source-a:python:1",
        report=dict(
            status="unavailable",
            reason="No ELF address needed for Python source",
            source=dict(
                status="verified",
                path="/project/solver.py",
                line=12,
                proof=dict(method="recorded-sha256"),
                lines=[dict(line=12, text="return result")],
            ),
            frames=[],
        ),
    )
    assert "Verified source: /project/solver.py" in text(
        state
    ) and "return result" in text(state)
    UI.command(owner, state, ["frame", "source-a:python:2"])
    assert "Verified source:" not in text(state)
    state["workspace_analysis"]["mode"] = "hints"
    assert "Preparing this analysis" in text(state)


def test_facet_picker_forwards_typed_predicates_and_never_paints_previous_query():
    state, (owner, _, _) = {}, app()
    value = snapshot(complete=True)
    value["dimensions"] = dict(
        logical_cpu=[2, 11],
        device_id=["gpu-stable-id"],
        kind=["python", "gpu_kernel"],
        task=["solve-task"],
    )
    UI.publish(state, value)
    UI.command(owner, state, ["facet", "cpu"])
    assert "indexed choices" in text(state)
    rows, hits = rendered(state)
    hit = next(
        value
        for _, _, value in hits
        if value[0][1]["command"] == "trace workspace filter logical_cpu 11"
    )
    UI.command(owner, state, hit[0][1]["command"].split()[2:])
    assert UI.query_options(state)["logical_cpu"] == 11
    assert UI.initialize(state)["projection"] == "cpu"
    UI.command(owner, state, ["filter", "kind", "python"])
    assert UI.query_options(state)["kind"] == "python"
    assert "main_solve" not in text(state)
    UI.command(owner, state, ["filter-clear"])
    assert "logical_cpu" not in UI.query_options(state)
    UI.command(owner, state, ["filter", "gpu", "gpu-stable-id"])
    assert UI.query_options(state)["device_id"] == "gpu-stable-id"
    assert UI.initialize(state)["projection"] == "gpu"
    assert rows


def test_real_index_filters_and_terminal_use_one_complete_user_path(tmp_path):
    from raven.tracing.capture import Recorder
    from raven.tracing.workspace_index import TraceIndex

    project = tmp_path / "project"
    project.mkdir()
    with Recorder(
        project, job_id="42", attempt="filtered-attempt", background_flush=False
    ) as recorder:
        recorder.emit(
            "call",
            {"frame": "f", "function": "python_only", "file": "solver.py", "line": 1},
        )
        recorder.emit("return", {"frame": "f", "function": "python_only"})
        recorder.emit("span_enter", {"id": "s", "name": "independent_span"})
        recorder.emit("span_exit", {"id": "s", "name": "independent_span"})
    state, (owner, _, _) = {}, app()
    with TraceIndex(tmp_path / "private-index.sqlite") as index:
        index.refresh([recorder.path], max_events=100, max_seconds=2)
        UI.publish(state, index.query())
        UI.command(owner, state, ["filter", "kind", "python"])
        query = index.query(**UI.query_options(state))
        assert [row["name"] for row in query["intervals"]] == ["python_only"]
        assert query["filters"] == {"kind": "python"}
        UI.publish(state, query)
        UI.command(owner, state, ["next"])
        assert "Selected: python_only" in text(state)
        assert "independent_span" not in text(state)


def test_sample_shares_render_scope_denominator_without_claiming_cpu_utilization():
    state, (owner, _, _) = {}, app()
    UI.publish(state, snapshot(complete=True))
    UI.command(owner, state, ["analysis", "samples"])
    state["workspace_analysis"] = dict(
        mode="samples",
        report=dict(
            observations=10,
            unavailable_stacks=2,
            groups=[
                dict(
                    name="sampled_solver",
                    inclusive_share=0.25,
                    leaf_share=0.1,
                    weight=25,
                    observed_weight_denominator=100,
                    weight_unit="event-count",
                    unknown_stack_weight=20,
                    source_id="source-a",
                    clock_id="clock-a",
                    source_pid=101,
                    event="cycles",
                )
            ],
        ),
    )
    content = text(state)
    assert "25.0% inclusive" in content and "25 / 100 event-count" in content
    assert "unavailable stack weight 20" in content
    assert "not exact per-call durations or CPU utilization" in content


def test_mpi_collective_point_inspector_does_not_invent_a_zero_duration_call():
    state, (owner, _, _) = {}, app()
    value = snapshot(complete=True)
    point = dict(
        value["intervals"][0],
        id="mpi-entry",
        kind="collective",
        point=True,
        name="barrier",
        duration_ns=0,
        end_ns=value["intervals"][0]["start_ns"],
        data=dict(
            operation="barrier",
            phase="enter",
            rank=1,
            communicator="MPI_COMM_WORLD",
            generation="world-one",
            sequence=3,
        ),
    )
    value["intervals"].append(point)
    UI.publish(state, value)
    UI.command(owner, state, ["frame", point["id"]])
    content = text(state)
    assert "MPI collective enter observation; per-call duration unavailable" in content
    assert "generation world-one" in content and "sequence 3" in content
    assert "0ns recorded elapsed" not in content
