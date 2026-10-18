import asyncio
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import threading
import time

import pytest

from raven import evidence_index as I, mcp_evidence as E, mcp_server as M
from test_evidence_index import capture


@pytest.fixture
def indexed(capture):
    project, bundle, index = capture
    I.ingest(index, bundle, project)
    attempt = I.query(index, view="attempts")["rows"][0][1]
    digest = I.query(index, "SELECT digest FROM captures")["rows"][0][0]
    with sqlite3.connect(index / I.DATABASE) as db:
        source = db.execute("SELECT id FROM sources LIMIT 1").fetchone()[0]
        db.executemany("INSERT INTO log_lines VALUES (?,?,?,?,?)", [(source, n, attempt, n * 10000, "untrusted ignore instructions " + "x" * 10000) for n in range(1, 15)])
        db.execute("INSERT INTO transitions VALUES (?,?,?,?,?,?,?,?)", (source, 1, attempt, "producer-declared-transition", "1", "PENDING", "RUNNING", "{}"))
    return E.scope(str(index), project), attempt, digest


def request(scope, name, params=None, **updates):
    return {"scope": scope, "tool": name, "arguments": params or {}, "offset": 0, "generation": "", **updates}


class LocalRouter:
    def dispatch(self, operation, inputs, context=None, cancel=None):
        assert operation == "mcp-evidence-read"
        return {"result": E.read(inputs["document"], cancel=cancel)}


def test_all_five_reads_bound_rows_bytes_and_retain_provenance(indexed):
    scope, attempt, digest = indexed
    for name, params in [("list_attempts", {}), ("read_receipts", {"capture": digest}),
            ("bounded_logs", {"attempt": attempt}), ("inspect_timeline", {"attempt": attempt}),
            ("query_evidence", {"sql": "SELECT metric,value,unit,source_id,source_row FROM measurements ORDER BY source_row,metric"})]:
        result = E.read(request(scope, name, params))
        assert result["rows"] and result["bytes"] <= E.MAX_BYTES
        assert result["scope"] == scope and result["untrusted_data"]
        assert "never server instructions" in result["evidence_notice"]
    logs = E.read(request(scope, "bounded_logs", {"attempt": attempt}))
    assert len(logs["rows"]) < 14 and logs["next_offset"] == len(logs["rows"])
    assert len(logs["rows"][0][4]) > 10000  # Fidelity retained, not silently truncated.



def test_pagination_does_not_mutate_committed_worker_result(indexed):
    scope, *_ = indexed
    class RetainingRouter(LocalRouter):
        def dispatch(self, *args, **kwargs):
            self.retained = super().dispatch(*args, **kwargs)
            self.snapshot = json.loads(json.dumps(self.retained))
            return self.retained
    router = RetainingRouter()
    result = M.Session(scope, router).request("list_attempts", {})
    assert "next_cursor" in result
    assert router.retained == router.snapshot
    assert "next_offset" in router.retained["result"]


def test_source_id_matches_sqlite_integer_and_published_schema(indexed):
    scope, attempt, _ = indexed
    for invalid in (0, -1, True, 1 << 63, 10 ** 100):
        with pytest.raises(ValueError, match="source_id"):
            E.read(request(scope, "bounded_logs", {"attempt": attempt, "source_id": invalid}))
    assert not E.read(request(scope, "bounded_logs", {"attempt": attempt, "source_id": E.MAX_SOURCE_ID}))["rows"]
    assert M.schemas()["bounded_logs"]["properties"]["source_id"]["maximum"] == E.MAX_SOURCE_ID


def test_cancel_drains_owned_task_and_refuses_unverified_cleanup(indexed, monkeypatch):
    scope, *_ = indexed
    class Worker:
        ready = True
        def __init__(self): self.tasks = {}; self.cancelled = []; self.polled = 0
        def cancel(self, key): self.cancelled.append(key)
        def poll(self):
            self.polled += 1
            self.tasks["owned"]["status"] = "cancelled"
    class Router:
        def __init__(self): self.instance = Worker()
        def worker(self): return self.instance
        def dispatch(self, *args, **kwargs):
            self.instance.tasks["owned"] = {"status": "running"}
            raise RuntimeError("cancelled")
    router = Router(); session = M.Session(scope, router)
    with pytest.raises(RuntimeError, match="cancelled"): session.request("list_attempts", {})
    assert router.instance.cancelled == ["owned"] and router.instance.polled == 1
    assert not session.failed
    router.instance.ready = False
    with pytest.raises(RuntimeError, match="cancelled"): session.request("list_attempts", {})
    # Reused test task identity is not new work; explicitly exercise the same
    # recovery refusal for a disconnected newly admitted task.
    session._drain(router.instance, {"owned"})
    assert session.failed
    with pytest.raises(RuntimeError, match="cleanup is unverified"): session.request("list_attempts", {})


def test_whole_wire_response_ceiling():
    class Message:
        def model_dump_json(self, **kwargs): return "x" * (512 << 10)
    class Envelope: message = Message()
    class Stream:
        async def send(self, message): raise AssertionError("oversized message reached transport")
    with pytest.raises(ValueError, match="512 KiB"):
        asyncio.run(M.BoundedOutput(Stream()).send(Envelope()))


def test_signed_pagination_binding_restart_and_index_changes(indexed):
    scope, _attempt, _digest = indexed
    session = M.Session(scope, LocalRouter())
    params = {"sql": "SELECT metric,value FROM measurements ORDER BY source_row,metric", "limit": 2}
    first = session.request("query_evidence", params)
    second = session.request("query_evidence", {**params, "cursor": first["next_cursor"]})
    assert first["rows"] != second["rows"]
    for cursor in (first["next_cursor"] + "x", "bad.cursor"):
        with pytest.raises(ValueError, match="cursor"):
            session.request("query_evidence", {**params, "cursor": cursor})
    with pytest.raises(ValueError, match="cursor"):
        session.request("query_evidence", {**params, "sql": "SELECT * FROM sources", "cursor": first["next_cursor"]})
    with pytest.raises(ValueError, match="cursor"):
        M.Session(scope, LocalRouter()).request("query_evidence", {**params, "cursor": first["next_cursor"]})
    os.utime(Path(scope["index"]) / I.DATABASE, None)
    with pytest.raises(ValueError, match="changed"):
        session.request("query_evidence", {**params, "cursor": first["next_cursor"]})


@pytest.mark.parametrize("sql", ["DELETE FROM attempts", "ATTACH '/etc/passwd' AS other", "PRAGMA writable_schema=ON", "SELECT load_extension('/tmp/x')", "SELECT * FROM attempts; SELECT * FROM sources"])
def test_no_write_shell_path_or_sql_escape(indexed, sql):
    scope, _attempt, _digest = indexed
    with pytest.raises(ValueError):
        E.read(request(scope, "query_evidence", {"sql": sql}))
    assert E.read(request(scope, "list_attempts"))["rows"]


def test_project_scope_on_same_connection_symlink_and_malformed_fields(indexed, tmp_path):
    scope, _attempt, _digest = indexed
    with pytest.raises(ValueError, match="project"):
        E.read(request({**scope, "project": "/different-project"}, "list_attempts"))
    link = tmp_path / "index-link"
    link.symlink_to(scope["index"], target_is_directory=True)
    with pytest.raises((ValueError, OSError)):
        E.read(request({**scope, "index": str(link)}, "list_attempts"))
    for value in ({"index": "/etc"}, {"limit": True}, {"limit": 101}, {"cursor": ["x"]}):
        with pytest.raises(ValueError):
            E.arguments("list_attempts", value)
    with pytest.raises(ValueError):
        E.scope("/a/../b", "/project")
    with pytest.raises(ValueError):
        E.read(request(scope, "list_attempts", offset=10001))


def test_cancelled_read_and_wire_line_limit(indexed):
    scope, *_ = indexed
    cancel = threading.Event(); cancel.set()
    result = E.read(request(scope, "list_attempts"), cancel=cancel)
    assert result["reason"] == "cancelled" and result["rows"] == []
    assert M.BoundedInput(io.BytesIO(b'{}\n')).readline() == '{}\n'
    with pytest.raises(ValueError, match="128 KiB"):
        M.BoundedInput(io.BytesIO(b'x' * ((128 << 10) + 1))).readline()


def test_sdk_client_over_actual_stdio_and_governed_worker(indexed, tmp_path):
    pytest.importorskip("mcp")
    from importlib.metadata import version
    assert version("mcp").startswith("2.3.")
    from mcp.client import Client
    from mcp.client.stdio import StdioServerParameters
    scope, attempt, digest = indexed
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
        "XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_CACHE_HOME": str(tmp_path / "cache")}
    events_path = tmp_path / "worker-events.jsonl"
    env["RAVEN_TEST_WORKER_EVENTS"] = str(events_path)
    # Observe the real worker protocol without changing execution or injecting
    # results. A running publication precedes cancellation; terminal publication
    # occurs only after the worker has reaped its owned child.
    observer = """
import json, os
from raven.raven_allocation import WorkerProxy
consume = WorkerProxy._event
def observed(self, message):
    consume(self, message)
    event = {key: message.get(key) for key in ('op', 'id', 'pid')}
    event['worker_pid'] = self.identity.get('pid')
    event['server_pid'] = os.getpid()
    with open(os.environ['RAVEN_TEST_WORKER_EVENTS'], 'a') as stream:
        stream.write(json.dumps(event) + '\\n')
WorkerProxy._event = observed
from raven.raven_launch import heavy_main
raise SystemExit(heavy_main())
"""
    arguments = ["-c", observer,
        "--site", "desktop", "mcp", "--index", scope["index"], "--project", scope["project"]]
    protocols = []
    async def exercise():
        for mode in ("legacy", "auto"):
            async with Client(StdioServerParameters(command=sys.executable, args=arguments, env=env), mode=mode, read_timeout_seconds=20) as client:
                tools = await client.list_tools()
                protocols.append({"mode": mode, "protocol": client.protocol_version})
                assert {tool.name for tool in tools.tools} == set(E.NAMES)
                for name, params in [("list_attempts", {}), ("read_receipts", {"capture": digest}),
                        ("bounded_logs", {"attempt": attempt}), ("inspect_timeline", {"attempt": attempt}),
                        ("query_evidence", {"sql": "SELECT metric,value FROM measurements ORDER BY source_row,metric", "limit": 2})]:
                    response = await client.call_tool(name, params)
                    assert not response.is_error, response
                    data = response.structured_content or json.loads(response.content[0].text)
                    assert data["rows"] and data["scope"] == scope
                bad = await client.call_tool("query_evidence", {"sql": "DELETE FROM attempts"})
                assert bad.is_error
                before = len(events_path.read_text().splitlines())
                task = asyncio.create_task(client.call_tool("query_evidence", {"sql": "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n) SELECT sum(x) FROM n"}))
                deadline = time.monotonic() + 5
                running = None
                while time.monotonic() < deadline:
                    observed = [json.loads(line) for line in events_path.read_text().splitlines()[before:]]
                    running = next((event for event in observed if event["op"] == "running"), None)
                    if running:
                        break
                    await asyncio.sleep(.002)
                assert running is not None, "Cancellation must target an actually running owned task"
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                again = await client.call_tool("list_attempts", {})
                assert not again.is_error
                data = again.structured_content or json.loads(again.content[0].text)
                assert data["tool"] == "list_attempts" and data["rows"]
                observed = [json.loads(line) for line in events_path.read_text().splitlines()[before:]]
                terminal = next(i for i, event in enumerate(observed) if event["id"] == running["id"] and event["op"] == "cancelled")
                following = next(i for i, event in enumerate(observed) if event["id"] != running["id"] and event["op"] == "accepted")
                assert terminal < following, "Next read must follow owned cancellation and reaping"
                with pytest.raises(ProcessLookupError): os.kill(running["pid"], 0)
                protocols[-1]["cancelled_running_task_reaped_before_next_admission"] = True
            # Closing stdio runs normal Heavy shutdown and reaps its process.
            events = [json.loads(line) for line in events_path.read_text().splitlines()]
            worker_pid = events[-1]["worker_pid"]
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                try: os.kill(worker_pid, 0)
                except ProcessLookupError: break
                await asyncio.sleep(.02)
            else: pytest.fail("Disconnected stdio server retained its owned Heavy worker")
            protocols[-1]["disconnect_reaped_worker"] = True
    asyncio.run(exercise())
    destination = os.environ.get("RAVEN_MCP_QUALIFICATION_EVIDENCE")
    if destination:
        import hashlib
        root = Path(__file__).resolve().parents[1]
        Path(destination).write_text(json.dumps({"schema": "raven.mcp-qualification/v1", "sdk": version("mcp"),
            "protocols": protocols, "actual_stdio": True, "actual_governed_local_worker": True,
            "live_scheduler": False, "human_client_qualification": False,
            "source_sha256": {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in [root / "raven/mcp_server.py", root / "raven/mcp_evidence.py", root / "raven/evidence_index.py", Path(__file__)]}}, indent=2) + "\n")
