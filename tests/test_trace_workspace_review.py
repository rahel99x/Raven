"""Independent orchestration regression cases found during maintainer review."""

from __future__ import annotations

import json
from types import SimpleNamespace

from raven.tracing.capture import Recorder
from raven.tracing.workspace_session import WorkspaceSession


def recording(root, *, clock="a"):
    root.mkdir()
    return Recorder(
        root,
        job_id="42",
        attempt="a",
        cluster="local",
        initial_mono_ns=100,
        background_flush=False,
        coverage={
            "scope": "Explicit review fixture boundaries",
            "clock": {
                "schema": "raven.trace-clock/v1",
                "kind": "producer-report",
                "id": "review",
                "report_sha256": clock * 64,
            },
        },
    )


def emit(recorder, name, thread="t1", start=100):
    recorder.emit(
        "call", {"function": name, "frame": name}, thread=thread, mono_ns=start
    )
    recorder.emit("return", {"frame": name}, thread=thread, mono_ns=start + 5)
    recorder.flush()


def drain(session, options=None):
    for _ in range(100):
        result = session.refresh(options or {"mode": "explore"})
        if result["caught_up"]:
            return result
    raise AssertionError("fixture did not index")


def test_all_clocks_preserves_source_filter_for_new_domains(tmp_path):
    with (
        recording(tmp_path / "one", clock="a") as one,
        recording(tmp_path / "two", clock="b") as two,
    ):
        emit(one, "alpha")
        emit(two, "beta")
        with_session = WorkspaceSession(
            [one.path, two.path], index_path=tmp_path / "index.sqlite"
        )
        try:
            result = drain(with_session)
            selected = next(
                row["source_id"]
                for row in result["intervals"]
                if row["name"] == "alpha"
            )
            result = with_session.refresh(
                {"all_clocks": True, "source_ids": [selected], "mode": "explore"}
            )
            assert [row["name"] for row in result["intervals"]] == ["alpha"]
        finally:
            with_session.close()


def test_function_table_has_same_thread_and_search_scope_as_query(tmp_path):
    with recording(tmp_path / "one") as rec:
        emit(rec, "alpha", "t1", 100)
        emit(rec, "beta", "t2", 110)
        session = WorkspaceSession([rec.path], index_path=tmp_path / "index.sqlite")
        try:
            drain(session)
            result = session.refresh(
                {
                    "mode": "explore",
                    "thread": "t1",
                    "search": "alpha",
                    "functions": True,
                }
            )
            assert [row["name"] for row in result["intervals"]] == ["alpha"]
            assert [row["name"] for row in result["functions"]["functions"]] == [
                "alpha"
            ]
        finally:
            session.close()


def test_cli_explicit_retained_range_does_not_follow_live_tail(
    tmp_path, capsys, monkeypatch
):
    from raven.execution import policy
    from raven.tracing.workspace_cli import workspace

    monkeypatch.setattr(policy, "router_for", lambda: SimpleNamespace(managed=False))
    with recording(tmp_path / "one") as rec:
        emit(rec, "early", start=100)
        emit(rec, "late", start=40_000_000_000)
        assert (
            workspace(
                [
                    str(rec.path),
                    "--index",
                    str(tmp_path / "index.sqlite"),
                    "--start-ns",
                    "100",
                    "--end-ns",
                    "106",
                    "--max-seconds",
                    "1",
                ]
            )
            == 0
        )
        result = json.loads(capsys.readouterr().out)
        assert [row["name"] for row in result["intervals"]] == ["early"]
        assert result["range"]["start_ns"] == 100 and result["range"]["end_ns"] == 106


def test_baseline_browser_without_optional_comparability_context_is_usable(tmp_path):
    with recording(tmp_path / "one") as rec:
        emit(rec, "alpha")
        session = WorkspaceSession([rec.path], index_path=tmp_path / "index.sqlite")
        try:
            drain(session)
            session.set_baseline([rec.path])
            viewer = session.open_browser()
            assert viewer.url.startswith("http://127.0.0.1:")
        finally:
            session.close()


def test_all_clock_single_domain_still_uses_its_explore_window(tmp_path):
    with recording(tmp_path / "one") as rec:
        emit(rec, "early", start=100)
        emit(rec, "late", start=200)
        session = WorkspaceSession([rec.path], index_path=tmp_path / "index.sqlite")
        try:
            result = drain(session)
            clock = result["bounds"][0]["clock_id"]
            result = session.refresh(
                {
                    "all_clocks": True,
                    "mode": "explore",
                    "requests": [
                        {
                            "clock_id": clock,
                            "start_ns": 200,
                            "end_ns": 206,
                            "limit": 2000,
                        }
                    ],
                }
            )
            assert [row["name"] for row in result["intervals"]] == ["late"]
        finally:
            session.close()


def test_cancelled_browser_effect_cannot_open_retired_session_for_same_run(monkeypatch):
    from raven import background_tasks
    from raven.tracing import ui
    from raven.tracing import workspace_browser_effect as effect
    from raven.ui.trace_ports import TraceBrowserEffects

    queued, opened = [], []
    hub = SimpleNamespace(
        start_task=lambda work, done: queued.append((work, done)) or True
    )
    monkeypatch.setattr(background_tasks, "research_hub", lambda *args: hub)
    monkeypatch.setattr(effect, "_open", lambda url: opened.append(url) or "opened")
    context = {"key": ("same-run",)}
    viewer = SimpleNamespace(url="http://127.0.0.1:12345/#token=retired")
    session = SimpleNamespace(open_browser=lambda **kwargs: viewer, closed=False)
    session.retire = lambda: setattr(session, "closed", True)
    app = SimpleNamespace(say=lambda text: None, fail=lambda text: None)
    app.trace_browser_effects = TraceBrowserEffects(
        current_context=lambda app: context, cancel_reader=lambda app: None
    )
    state = ui.initialize(app)
    state["workspace_session"] = session
    assert effect.request(app, session, context)
    work, done = queued.pop()
    result = work()
    ui.cancel(app, clear=True)
    done(result)
    assert session.closed
    assert not opened
