"""New registered streams reach an owned browser without terminal ticks."""

import json
import os
import select
import signal
import subprocess
from threading import Event
import time
from urllib.request import Request, urlopen

import pytest

from raven.tracing.capture import Recorder
from raven.tracing.workspace_follow import project_discovery, verified_identity
from raven.tracing.workspace_follow import deferred_project_discovery
from raven.tracing.workspace_session import WorkspaceSession


IDENTITY = {"job_id": "42", "attempt": "one", "cluster": "local"}


def recorder(root, *, attempt="one"):
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return Recorder(
        root, job_id="42", attempt=attempt, cluster="local", background_flush=False
    )


def call(rec, name):
    rec.emit("call", {"frame": name, "function": name}, thread="main")
    rec.emit("return", {"frame": name}, thread="main")
    rec.flush()


def ready(session):
    for _ in range(100):
        value = session.refresh({"mode": "explore"})
        if value["caught_up"]:
            return value
    pytest.fail("Verified source fixture did not catch up")


def request(viewer, path):
    request = Request(
        viewer.origin + path, headers={"Authorization": "Bearer " + viewer.token}
    )
    with urlopen(request, timeout=3) as response:
        return json.loads(response.read())["result"]


def wait_for(predicate, *, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.03)
    pytest.fail("Owned live source discovery did not publish its bounded result")


def test_background_viewer_discovers_new_stream_without_any_terminal_refresh(tmp_path):
    project = tmp_path / "project"
    with recorder(project) as first:
        call(first, "first_window")
    session = WorkspaceSession(
        [first.path], expected=IDENTITY, index_path=tmp_path / "cache.sqlite"
    )
    try:
        identity = verified_identity(ready(session))
        session.set_discovery(project_discovery(project, identity), primed=True)
        viewer = session.open_browser()
        with recorder(project) as second:
            call(second, "later_window")
        with recorder(project, attempt="another-attempt") as foreign:
            call(foreign, "foreign_attempt")

        def arrived():
            result = request(viewer, "/api/query")
            names = {row["name"] for row in result["intervals"]}
            return result if {"first_window", "later_window"} <= names else None

        result = wait_for(arrived)
        assert "foreign_attempt" not in {row["name"] for row in result["intervals"]}
        assert request(viewer, "/api/catalog")["collection_following"] is True
        assert (
            str(second.path) in session.paths and str(foreign.path) not in session.paths
        )
    finally:
        session.close()


def test_corrupt_registry_keeps_known_source_ingestion_and_health_until_recovery(
    tmp_path,
):
    project = tmp_path / "project"
    with recorder(project) as rec:
        call(rec, "before")
        session = WorkspaceSession(
            [rec.path], expected=IDENTITY, index_path=tmp_path / "cache.sqlite"
        )
        try:
            identity = verified_identity(ready(session))
            registry = project / ".raven/traces/index.json"
            original = registry.read_bytes()
            session.set_discovery(project_discovery(project, identity))
            registry.write_text("{incomplete")
            value = session.refresh({"mode": "explore"})
            assert value["collection_error"] and not value["caught_up"]
            # An empty cached foreground metadata result is not a successful
            # rescan of the failed registry and cannot erase the actual error.
            cached = session.refresh({"discovery_errors": [], "mode": "explore"})
            assert cached["collection_error"]
            viewer = session.open_browser()
            call(rec, "during_registry_error")
            wait_for(
                lambda: any(
                    row["name"] == "during_registry_error"
                    for row in request(viewer, "/api/query")["intervals"]
                )
            )
            catalog = request(viewer, "/api/catalog")
            assert catalog["collection_error"] and not catalog["caught_up"]
            assert request(viewer, "/api/query")["collection_error"]
            registry.write_bytes(original)
            with session.lock:
                session._next_discovery = 0
            wait_for(lambda: not request(viewer, "/api/catalog")["collection_error"])
            assert session.paths == (str(rec.path),)
        finally:
            session.close()


def test_discovery_cadence_and_source_quota_preserve_known_verified_paths(tmp_path):
    with recorder(tmp_path / "project") as rec:
        call(rec, "known")
    session = WorkspaceSession(
        [rec.path], expected=IDENTITY, index_path=tmp_path / "cache.sqlite"
    )
    calls = []
    try:
        ready(session)

        def discover(cancel):
            calls.append(time.monotonic())
            return {"sources": [str(rec.path)], "discovery_errors": []}

        session.set_discovery(discover)
        session.refresh()
        session.refresh()
        assert len(calls) == 1
        session._next_discovery = 0
        session.set_discovery(discover, primed=True)
        session.refresh()
        assert len(calls) == 2
        session.set_discovery(
            lambda cancel: {"sources": ["/unopened/" + str(i) for i in range(513)]}
        )
        session._next_discovery = 0
        value = session.refresh({"mode": "explore"})
        assert "512-source" in value["collection_error"]
        assert session.paths == (str(rec.path),)
        assert value["intervals"][0]["name"] == "known"
    finally:
        session.close()


def test_close_cancels_owned_discovery_and_leaves_recorder_running(tmp_path):
    project = tmp_path / "project"
    with recorder(project) as rec:
        call(rec, "before_close")
        session = WorkspaceSession(
            [rec.path], expected=IDENTITY, index_path=tmp_path / "cache.sqlite"
        )
        entered = Event()

        def delayed(cancel):
            entered.set()
            deadline = time.monotonic() + 3
            while not cancel.is_set() and time.monotonic() < deadline:
                time.sleep(0.01)
            return {"sources": [str(rec.path)]}

        ready(session)
        session.set_discovery(delayed)
        session.open_browser()
        assert entered.wait(2)
        session.close()
        assert not session._browser_reader.is_alive()
        assert rec.emit("marker", {"name": "still_running_after_viewer_closed"})
        rec.flush()


def test_cli_explicit_follow_joins_only_initial_verified_attempt(tmp_path, capsys):
    from raven.tracing import workspace_cli

    project = tmp_path / "project"
    paths = []
    for name, attempt in (("first", "one"), ("second", "one"), ("foreign", "another")):
        with recorder(project, attempt=attempt) as rec:
            call(rec, name)
        paths.append(rec.path)
    assert (
        workspace_cli.workspace(
            [
                str(paths[0]),
                "--index",
                str(tmp_path / "cli.sqlite"),
                "--follow-project",
                str(project),
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert {row["name"] for row in result["intervals"]} == {"first", "second"}
    assert result["collection_following"] is True


def test_follow_requires_actual_initial_verification_and_missing_registry_stays_unknown(
    tmp_path,
):
    with pytest.raises(ValueError, match="verified committed"):
        verified_identity({"identity": IDENTITY, "sources": []})
    with recorder(tmp_path / "project") as rec:
        call(rec, "one")
    session = WorkspaceSession(
        [rec.path], expected=IDENTITY, index_path=tmp_path / "cache.sqlite"
    )
    try:
        identity = verified_identity(ready(session))
        session.set_discovery(project_discovery(tmp_path / "project", identity))
        (tmp_path / "project/.raven/traces/index.json").unlink()
        value = session.refresh()
        assert "not yet available" in value["collection_error"]
        assert value["intervals"] and not value["caught_up"]
    finally:
        session.close()


def test_deferred_follow_waits_for_real_committed_prefix_without_blocking_known_reads(
    tmp_path,
):
    project = tmp_path / "project"
    project.mkdir(mode=0o700)
    with Recorder(
        project,
        job_id="42",
        attempt="one",
        cluster="local",
        background_flush=False,
        flush_interval=60,
        flush_events=1000,
    ) as rec:
        session = WorkspaceSession([rec.path], index_path=tmp_path / "cache.sqlite")
        try:
            session.refresh({"mode": "explore"})
            session.set_discovery(
                deferred_project_discovery(
                    project,
                    session.index.catalog,
                    on_identity=lambda identity: setattr(session, "expected", identity),
                )
            )
            waiting = session.refresh({"mode": "explore"})
            assert "verified committed" in waiting["collection_error"]
            assert session.expected == {}
            call(rec, "first_committed")
            session.refresh({"mode": "explore"})
            session._next_discovery = 0
            verified = session.refresh({"mode": "explore"})
            assert not verified["collection_error"]
            assert session.expected == IDENTITY
            assert verified["intervals"][0]["name"] == "first_committed"
        finally:
            session.close()


def test_custom_run_identity_is_retained_and_wrong_custom_run_is_not_joined(tmp_path):
    project = tmp_path / "project"
    project.mkdir(mode=0o700)
    with Recorder(
        project,
        job_id="42",
        attempt="one",
        cluster="local",
        background_flush=False,
        identity_extra={"run_id": "custom-run"},
    ) as first:
        call(first, "first")
    with Recorder(
        project,
        job_id="42",
        attempt="one",
        cluster="local",
        background_flush=False,
        identity_extra={"run_id": "another-run"},
    ) as foreign:
        call(foreign, "wrong_run")
    session = WorkspaceSession([first.path], index_path=tmp_path / "cache.sqlite")
    try:
        identity = verified_identity(ready(session))
        assert identity["run_id"] == "custom-run"
        session.set_discovery(project_discovery(project, identity))
        session.refresh()
        assert str(foreign.path) not in session.paths
        assert session.paths == (str(first.path),)
    finally:
        session.close()


def test_cli_serve_opens_while_first_prefix_is_not_yet_committed(
    tmp_path, capsys, monkeypatch
):
    from raven.tracing import workspace_cli

    project = tmp_path / "project"
    project.mkdir(mode=0o700)
    with Recorder(
        project,
        job_id="42",
        attempt="one",
        cluster="local",
        background_flush=False,
        flush_interval=60,
        flush_events=1000,
    ) as rec:

        def finish_owned_viewer(seconds):
            raise KeyboardInterrupt

        monkeypatch.setattr(workspace_cli.time, "sleep", finish_owned_viewer)
        assert (
            workspace_cli.workspace(
                [
                    str(rec.path),
                    "--index",
                    str(tmp_path / "cli.sqlite"),
                    "--follow-project",
                    str(project),
                    "--serve",
                ]
            )
            == 0
        )
        result = json.loads(capsys.readouterr().out)
        assert result["schema"] == "raven.trace-workspace-viewer/v1"
        assert "127.0.0.1" in result["url"]
        assert rec.emit("marker", {"name": "scientific_program_continues"})


def test_actual_browser_live_follows_later_closed_window_without_terminal_ticks(
    tmp_path,
):
    from test_trace_workspace_browser import browser_prerequisites

    node, chromium = browser_prerequisites()
    project = tmp_path / "project"
    with recorder(project) as first:
        call(first, "first_closed_window")
    session = WorkspaceSession(
        [first.path], expected=IDENTITY, index_path=tmp_path / "cache.sqlite"
    )
    process = None
    try:
        identity = verified_identity(ready(session))
        session.set_discovery(project_discovery(project, identity), primed=True)
        viewer = session.open_browser()
        script = tmp_path / "follow-browser.cjs"
        script.write_text(r"""
const {chromium}=require('playwright');
(async()=>{const browser=await chromium.launch({executablePath:process.argv[2],headless:true,args:['--no-sandbox','--disable-dev-shm-usage']});
try{const page=await browser.newPage();const errors=[];page.on('pageerror',error=>errors.push(error.message));
await page.goto(process.argv[3]);await page.locator('#event_body button').filter({hasText:'first_closed_window'}).waitFor();
await page.locator('#live').click();await page.waitForFunction(()=>document.getElementById('live').getAttribute('aria-pressed')==='true');
console.log(JSON.stringify({ready:true}));
await page.locator('#event_body button').filter({hasText:'later_closed_window'}).waitFor({timeout:8000});
await page.waitForTimeout(1200);
if(await page.locator('#live').getAttribute('aria-pressed')!=='true')throw Error('A completed chunk incorrectly stopped live registration following');
if(errors.length)throw Error(errors.join('\n'));console.log(JSON.stringify({passed:true,newClosedWindow:true,liveFollowPreserved:true}));
}finally{await browser.close()}})().catch(error=>{console.error(error);process.exit(1)});
""")
        process = subprocess.Popen(
            [node, str(script), chromium, viewer.url],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        available, _, _ = select.select([process.stdout], [], [], 8)
        assert available, "Browser did not finish its initial user action"
        line = process.stdout.readline()
        assert line and json.loads(line)["ready"]
        with recorder(project) as later:
            call(later, "later_closed_window")
        output, error = process.communicate(timeout=12)
        assert process.returncode == 0, error
        assert json.loads(output)["liveFollowPreserved"]
    finally:
        if process is not None and process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=3)
        session.close()
