from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from tower import ops_services, services_io, services_staging as staging, services_workflows as workflows, supervisor
from tower.operations import Context
from tower.remote import LocalFiles


@pytest.fixture
def ctx(tmp_path):
    return Context(slurm=SimpleNamespace(user="researcher", b=SimpleNamespace()), files=LocalFiles(),
                   state_dir=str(tmp_path / "state"), scope={"user": "researcher", "cluster": "test"},
                   cancel=threading.Event())


def manifest(tmp_path, contents=(b"input-data",)):
    entries = []
    for i, data in enumerate(contents):
        source = tmp_path / f"source-{i}"
        source.write_bytes(data)
        entries.append({"id": str(i), "source": str(source), "destination": str(tmp_path / f"target-{i}"),
                        "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                        "direction": "input" if i % 2 == 0 else "output"})
    return {"schema": staging.SCHEMA, "id": "campaign-A", "entries": entries}


def runner(argv, **kwargs):
    if argv[0] == "squeue":
        return "42|RUNNING|science|2026-01-01T00:00:00|2026-01-01T00:01:00|node1\n"
    return "41|COMPLETED|prior|2026-01-01T00:00:00|2026-01-01T00:01:00|2026-01-01T00:02:00|0:0\n"


def wait_until(predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.02)
    assert predicate(), "Timed out waiting for service state"


@pytest.mark.parametrize("value", [0, .5, 301, float("nan"), float("inf"), "no"])
def test_supervisor_interval_bounds(value):
    with pytest.raises(ValueError):
        supervisor.configuration(interval=value)


@pytest.mark.parametrize("value", [0, 10001, True, "1.5", -1])
def test_supervisor_record_bounds(value):
    with pytest.raises(ValueError):
        supervisor.configuration(max_records=value)


def test_supervisor_restart_reconciles_without_duplicate_events(tmp_path):
    path = tmp_path / "service"
    config = supervisor.configuration(user="alice", cluster="alpha", interval=1)
    first = supervisor.serve(path, config, runner=runner, max_polls=1)
    assert len(first["events"]) == 2
    second = supervisor.serve(path, config, runner=runner, max_polls=1)
    assert second["generation"] == 2
    assert len(second["events"]) == 2
    assert supervisor.status(path)["running"] is False
    assert supervisor.status(path)["stale"] is True
    other = supervisor.serve(path, {**config, "cluster": "beta"}, runner=runner, max_polls=1)
    assert other["generation"] == 1
    assert all(row["cluster"] == "beta" for row in other["active"])


def test_supervisor_lock_single_writer_and_stale_stop_token(tmp_path):
    directory = services_io.private_directory(tmp_path / "service")
    lock = supervisor._lock(directory)
    try:
        with pytest.raises(RuntimeError, match="already owns"):
            supervisor.serve(directory, supervisor.configuration(), max_polls=1, runner=runner)
    finally:
        os.close(lock)
    services_io.write_json_file(directory / "stop.json", {"token": "old-session"})
    result = supervisor.serve(directory, supervisor.configuration(), token="new-session", max_polls=1, runner=runner)
    assert result["generation"] == 1


def test_supervisor_attempt_requeue_has_no_false_completion():
    config = supervisor.configuration()
    row = {"id": "4", "submit": "S", "start": "first", "state": "FAILED", "cluster": ""}
    previous = {"active": [], "finished": [row]}
    current = {"active": [{**row, "start": "second", "state": "RUNNING"}], "finished": [row], "errors": [], "time": 1}
    result = supervisor.reconcile(previous, current, config)
    assert [(event["start"], event["after"]) for event in result["events"]] == [("second", "RUNNING")]
    current["finished"] = [{**row, "start": "second", "state": "FAILED"}]
    again = supervisor.reconcile(result, current, config)
    assert again["events"] == result["events"]


def test_supervisor_failed_source_keeps_stale_observation():
    config = supervisor.configuration()
    good = supervisor.reconcile({}, supervisor.collect(config, runner=runner), config)
    def failing(argv, **kwargs):
        raise RuntimeError("scheduler unavailable")
    bad = supervisor.reconcile(good, supervisor.collect(config, runner=failing), config)
    assert bad["active"] == good["active"]
    assert bad["events"] == good["events"]
    assert len(bad["errors"]) == 2


def test_supervisor_collect_caps_records_and_scopes_commands():
    seen = []
    def many(argv, **kwargs):
        seen.append(argv)
        return runner(argv) * 4
    result = supervisor.collect(supervisor.configuration(user="alice", cluster="alpha", max_records=2), runner=many)
    assert len(result["active"]) == len(result["finished"]) == 2
    assert result["truncated"] == ["active", "finished"]
    assert all("alpha" in argv and "alice" in argv for argv in seen)
    assert all(argv[0] in ("squeue", "sacct") for argv in seen)


def test_supervisor_pid_reuse_and_private_directory(tmp_path):
    directory = services_io.private_directory(tmp_path / "service")
    services_io.write_json_file(directory / "owner.json", {"pid": os.getpid(), "start_ticks": "wrong", "token": "x"})
    lock = supervisor._lock(directory)
    try:
        assert supervisor.status(directory)["running"] is False
    finally:
        os.close(lock)
    directory.chmod(0o755)
    with pytest.raises(ValueError, match="0700"):
        supervisor.status(directory)


@pytest.mark.skipif(not Path("/proc").is_dir(), reason="Linux process identity integration")
def test_supervisor_real_process_start_reconnect_stop_restart(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("squeue", "sacct"):
        script = bin_dir / name
        script.write_text("#!/bin/sh\nprintf '%s\\n' '" + runner([name]).strip() + "'\n")
        script.chmod(0o700)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    directory = tmp_path / "service"
    config = supervisor.configuration(user="alice", interval=1)
    first = supervisor.start(directory, config)
    token = first["owner"]["token"]
    try:
        wait_until(lambda: supervisor.status(directory).get("generation", 0) >= 1)
        assert supervisor.status(directory)["running"]
        with pytest.raises(RuntimeError, match="already running"):
            supervisor.start(directory, config)
        with pytest.raises(ValueError, match="changed after review"):
            supervisor.stop(directory, expected_token="old")
        supervisor.stop(directory, expected_token=token)
        wait_until(lambda: not supervisor.status(directory)["running"])
        second = supervisor.start(directory, config)
        assert second["owner"]["token"] != token
        wait_until(lambda: supervisor.status(directory).get("generation", 0) >= 2)
    finally:
        supervisor.stop(directory)
        wait_until(lambda: not supervisor.status(directory)["running"])
        supervisor.stop(directory)


def test_supervisor_reaps_owned_child_after_lock_release_before_process_exit(tmp_path, monkeypatch):
    directory = tmp_path / "monitor"
    released, exit_gate = tmp_path / "released", tmp_path / "exit"
    code = """
import sys,time
from pathlib import Path
from tower import supervisor
supervisor.serve(sys.argv[1], supervisor.configuration(), runner=lambda *a,**kw:'', max_polls=1)
Path(sys.argv[2]).touch()
deadline=time.monotonic()+5
while not Path(sys.argv[3]).exists() and time.monotonic()<deadline:
    time.sleep(.005)
"""
    child = subprocess.Popen([sys.executable, "-c", code, str(directory), str(released), str(exit_gate)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    supervisor._CHILDREN[child.pid] = child
    original_wait = child.wait
    waits = []
    def wait(*args, **kwargs):
        waits.append(kwargs.get("timeout"))
        exit_gate.touch()  # Exit only when reconciliation actually waits.
        return original_wait(*args, **kwargs)
    monkeypatch.setattr(child, "wait", wait)
    try:
        wait_until(released.exists)
        assert child.poll() is None
        assert not supervisor.status(directory)["running"]
        assert child.pid in supervisor._CHILDREN and not waits
        assert supervisor.stop(directory)["state"] == "stopped"
        assert waits == [supervisor._EXIT_REAP_SECONDS]
        assert child.returncode == 0 and child.pid not in supervisor._CHILDREN
    finally:
        exit_gate.touch()
        original_wait(timeout=6)
        supervisor._CHILDREN.pop(child.pid, None)


def test_supervisor_slow_owned_exit_is_bounded_and_later_collected(monkeypatch, tmp_path):
    waits, signals = [], []
    child = SimpleNamespace(returncode=None, pid=87654)
    child.poll = lambda: child.returncode
    def wait(timeout):
        waits.append(timeout)
        raise subprocess.TimeoutExpired("supervisor", timeout)
    child.wait = wait
    monkeypatch.setattr(supervisor, "_CHILDREN", {child.pid: child})
    monkeypatch.setattr(os, "kill", lambda *args: signals.append(args))
    directory = services_io.private_directory(tmp_path / "monitor")
    services_io.write_json_file(directory / "owner.json", {"pid": child.pid, "token": "old", "start_ticks": "0"})
    assert not supervisor.stop(directory)["running"]
    assert waits == [supervisor._EXIT_REAP_SECONDS] and not signals
    assert child.pid in supervisor._CHILDREN
    # Even an overwritten owner record cannot hide a completed direct child.
    services_io.write_json_file(directory / "owner.json", {"pid": 87655, "token": "replacement", "start_ticks": "1"})
    child.returncode = 0
    supervisor.status(directory)
    assert not supervisor._CHILDREN and not signals


def test_supervisor_stop_does_not_wait_on_live_or_unowned_service(monkeypatch, tmp_path):
    directory = services_io.private_directory(tmp_path / "monitor")
    child = SimpleNamespace(pid=87654, poll=lambda: None,
                            wait=lambda **kwargs: pytest.fail("waited on a live persistent service"))
    monkeypatch.setattr(supervisor, "_CHILDREN", {child.pid: child})
    monkeypatch.setattr(supervisor, "status", lambda *args: {
        "running": True, "owner": {"pid": child.pid, "token": "live"}})
    assert supervisor.stop(directory)["state"] == "stop requested"
    assert supervisor._CHILDREN[child.pid] is child
    assert services_io.read_json_file(directory / "stop.json")["token"] == "live"
    monkeypatch.setattr(supervisor, "status", lambda *args: {
        "running": False, "state": "stopped", "owner": {"pid": child.pid + 1, "token": "unowned"}})
    assert not supervisor.stop(directory)["running"]
    assert supervisor._CHILDREN[child.pid] is child


@pytest.mark.parametrize("reaper", ["poll", "wait"])
def test_supervisor_reaper_cannot_remove_concurrent_pid_replacement(monkeypatch, reaper):
    from concurrent.futures import ThreadPoolExecutor
    checking, release_check, registering = threading.Event(), threading.Event(), threading.Event()
    old = SimpleNamespace(pid=87654)
    replacement = SimpleNamespace(pid=old.pid)
    def released_lock(*args, **kwargs):
        assert supervisor._CHILDREN_LOCK.acquire(blocking=False), "poll/wait held the registry lock"
        supervisor._CHILDREN_LOCK.release()
        return 0
    old.poll = old.wait = released_lock
    class Registry(dict):
        calls = 0
        def get(self, key, default=None):
            self.calls += 1
            # Wait mode first gets the handle, then checks it after waiting.
            if self.calls == (2 if reaper == "wait" else 1):
                checking.set()
                assert release_check.wait(2)
            return super().get(key, default)
    registry = Registry({old.pid: old})
    monkeypatch.setattr(supervisor, "_CHILDREN", registry)
    def install():
        registering.set()
        supervisor._remember_child(replacement)
    with ThreadPoolExecutor(max_workers=2) as pool:
        collecting = pool.submit(supervisor._reap_children if reaper == "poll" else
                                 lambda: supervisor._reap_stopped({"pid": old.pid}))
        try:
            assert checking.wait(1)
            installing = pool.submit(install)
            assert registering.wait(1)
            # The compare and remove must form one critical section. A launch
            # cannot install its recycled PID between these two operations.
            acquired = supervisor._CHILDREN_LOCK.acquire(blocking=False)
            if acquired:
                supervisor._CHILDREN_LOCK.release()
            assert not acquired
        finally:
            release_check.set()
        collecting.result(timeout=2)
        installing.result(timeout=2)
    assert registry[old.pid] is replacement


def test_bounded_command_output_timeout_and_cancel():
    assert services_io.bounded_command([sys.executable, "-c", "print('good')"]) == "good\n"
    with pytest.raises(ValueError, match="exceeds"):
        services_io.bounded_command([sys.executable, "-c", "print('x'*100000)"], limit=100)
    with pytest.raises(TimeoutError):
        services_io.bounded_command([sys.executable, "-c", "import time; time.sleep(10)"], timeout=.05)
    event = threading.Event()
    event.set()
    with pytest.raises(InterruptedError):
        services_io.bounded_command([sys.executable, "-c", "print('bad')"], cancel=event)


def test_bounded_command_reaps_group_after_leader_exits(tmp_path):
    marker = tmp_path / "child"
    code = ("import os,time; p=os.fork(); "
            f"open({str(marker)!r},'w').write(str(p)) if p else None; "
            "os._exit(0) if p else time.sleep(20)")
    with pytest.raises(TimeoutError):
        services_io.bounded_command([sys.executable, "-c", code], timeout=.2)
    pid = int(marker.read_text())
    def dead():
        try:
            status = Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1]
            return status.startswith("Z")
        except FileNotFoundError:
            return True
    wait_until(dead)


@pytest.mark.parametrize("document", ['{"a":1,"a":2}', '{"a":NaN}', '{"a":1e9999}'])
def test_service_json_strict(tmp_path, document):
    path = tmp_path / "input.json"
    path.write_text(document)
    with pytest.raises(ValueError):
        services_io.read_json_file(path)


def test_staging_roundtrip_and_resume(tmp_path):
    document = manifest(tmp_path, (b"input", b"output", b""))
    prepared = staging.inspect(document)
    receipts = tmp_path / "receipts"
    result = staging.execute(prepared, receipts)
    assert [entry["status"] for entry in result] == ["copied"] * 3
    for entry in document["entries"]:
        assert Path(entry["source"]).read_bytes() == Path(entry["destination"]).read_bytes()
    assert [entry["status"] for entry in staging.execute(prepared, receipts)] == ["verified-existing"] * 3
    receipt = services_io.read_json_file(receipts / (prepared["revision"] + ".json"))
    assert len(receipt["entries"]) == 3
    assert not list(tmp_path.glob(".tower-transfer-*"))


@pytest.mark.parametrize("kind", ["checksum", "size", "direction", "relative", "same", "duplicate", "cycle", "retention", "unknown"])
def test_staging_invalid_manifests(tmp_path, kind):
    document = manifest(tmp_path, (b"a", b"b"))
    entry = document["entries"][0]
    if kind == "checksum": entry["sha256"] = "x"
    if kind == "size": entry["size"] = True
    if kind == "direction": entry["direction"] = "delete"
    if kind == "relative": entry["source"] = "a.txt"
    if kind == "same": entry["destination"] = entry["source"]
    if kind == "duplicate": document["entries"][1]["destination"] = entry["destination"]
    if kind == "cycle": entry["destination"] = document["entries"][1]["source"]
    if kind == "retention": document["retention"] = "delete-source"
    if kind == "unknown": document["recursive"] = True
    with pytest.raises(ValueError):
        staging.validate(document)


def test_staging_source_mutated_after_review(tmp_path):
    document = manifest(tmp_path)
    prepared = staging.inspect(document)
    Path(document["entries"][0]["source"]).write_bytes(b"changed")
    result = staging.execute(prepared, tmp_path / "receipts")
    assert result[0]["status"] == "failed"
    assert not Path(document["entries"][0]["destination"]).exists()


def test_staging_checksum_failure_partial_success_and_no_clobber(tmp_path):
    document = manifest(tmp_path, (b"good", b"bad", b"last"))
    document["entries"][1]["sha256"] = "0" * 64
    Path(document["entries"][2]["destination"]).write_bytes(b"user data")
    prepared = staging.inspect(document)
    result = staging.execute(prepared, tmp_path / "receipts")
    assert [entry["status"] for entry in result] == ["copied", "failed", "failed"]
    assert Path(document["entries"][2]["destination"]).read_bytes() == b"user data"
    assert not Path(document["entries"][1]["destination"]).exists()
    assert not list(tmp_path.glob(".tower-transfer-*"))


def test_staging_cancellation_cleans_temp_and_persists_partial_receipt(tmp_path, monkeypatch):
    document = manifest(tmp_path, (b"first", b"large" * 400000))
    prepared = staging.inspect(document)
    cancel = threading.Event()
    original = staging.transfer
    def cancelling(entry, **kwargs):
        if entry["id"] == "1":
            cancel.set()
        return original(entry, **kwargs)
    monkeypatch.setattr(staging, "transfer", cancelling)
    result = staging.execute(prepared, tmp_path / "receipts", cancel=cancel)
    assert [row["status"] for row in result] == ["copied", "cancelled"]
    receipt = services_io.read_json_file(tmp_path / "receipts" / (prepared["revision"] + ".json"))
    assert receipt["entries"][-1]["status"] == "cancelled"
    cancel.clear()
    monkeypatch.setattr(staging, "transfer", original)
    assert [row["status"] for row in staging.execute(prepared, tmp_path / "receipts")] == ["verified-existing", "copied"]


def test_staging_refuses_symlinks_hardlink_aliases_and_fifo(tmp_path):
    document = manifest(tmp_path)
    source = Path(document["entries"][0]["source"])
    destination = Path(document["entries"][0]["destination"])
    alias = tmp_path / "alias"
    alias.symlink_to(source)
    linked = copy.deepcopy(document)
    linked["entries"][0]["source"] = str(alias)
    with pytest.raises(OSError):
        staging.inspect(linked)
    prepared = staging.inspect(document)
    destination.symlink_to(source)
    assert staging.execute(prepared, tmp_path / "receipts")[0]["status"] == "failed"
    destination.unlink()
    os.link(source, destination)
    assert staging.execute(prepared, tmp_path / "receipts")[0]["status"] == "failed"
    source.unlink()
    os.mkfifo(source)
    with pytest.raises(ValueError, match="regular"):
        staging.inspect(document)


def test_staging_concurrent_revision_lock(tmp_path):
    prepared = staging.inspect(manifest(tmp_path))
    receipts = services_io.private_directory(tmp_path / "receipts")
    fd = os.open(receipts / (prepared["revision"] + ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(ValueError, match="already running"):
            staging.execute(prepared, receipts)
    finally:
        os.close(fd)


def test_staging_destination_directory_swap(tmp_path):
    document = manifest(tmp_path)
    target_dir = tmp_path / "out"
    target_dir.mkdir()
    document["entries"][0]["destination"] = str(target_dir / "result")
    prepared = staging.inspect(document)
    target_dir.rename(tmp_path / "old-out")
    target_dir.mkdir()
    result = staging.execute(prepared, tmp_path / "receipts")
    assert result[0]["status"] == "failed"
    assert not (target_dir / "result").exists()


def test_ops_staging_scope_and_source_bound_review(tmp_path, ctx):
    source = tmp_path / "manifest.json"
    source.write_text(json.dumps(manifest(tmp_path)))
    review = ops_services.run("staging", {"manifest": str(source)}, ctx)
    assert review["plan"]
    source.write_text(json.dumps({**json.loads(source.read_text()), "id": "changed"}))
    with pytest.raises(ValueError, match="manifest changed"):
        ops_services.apply("staging", review["plan"], ctx)


@pytest.mark.parametrize("feature,params", [("supervisor", {}), ("staging", {"manifest": "missing"})])
def test_ops_services_reject_remote_local_substitutes(ctx, feature, params):
    remote = Context(**{**ctx.__dict__, "files": SimpleNamespace(remote=True)})
    with pytest.raises(ValueError, match="target host"):
        ops_services.run(feature, params, remote)
    replay = Context(**{**ctx.__dict__, "replay": True})
    with pytest.raises(ValueError, match="recorded"):
        ops_services.run(feature, params, replay)


def test_ops_supervisor_connection_scoped_default(ctx):
    first = ops_services._directory(ctx, {})
    other = Context(**{**ctx.__dict__, "scope": {**ctx.scope, "cluster": "other"}})
    assert first != ops_services._directory(other, {})


def test_ops_supervisor_mismatched_explicit_directory(tmp_path, ctx):
    directory = tmp_path / "service"
    supervisor.serve(directory, supervisor.configuration(user="someone", cluster="elsewhere"), runner=runner, max_polls=1)
    value = ops_services.run("supervisor", {"directory": str(directory)}, ctx)
    assert any("another cluster" in warning for warning in value["warnings"])
    assert any("someone" in row for row in value["rows"])
    with pytest.raises(ValueError, match="another cluster"):
        ops_services.run("supervisor", {"directory": str(directory), "action": "start"}, ctx)


def test_nextflow_native_tsv_deduplicates_attempts_and_ignores_partial(tmp_path, ctx):
    source = tmp_path / "trace.txt"
    source.write_text("task_id\thash\tnative_id\tname\tstatus\tattempt\n"
                      "1\tab/cd\t42\talign\tRUNNING\t1\n1\tab/cd\t42\talign\tCOMPLETED\t1\n"
                      "1\txx/yy\t43\talign\tRUNNING\t2\n2\tpartial")
    value = workflows.nextflow(ctx, str(source), "runA")
    assert len(value["nodes"]) == 2
    assert value["nodes"][0]["state"] == "COMPLETED"
    assert value["nodes"][1]["native_id"] == "43"
    assert value["incomplete_tail"]
    assert value["ownership"] == "engine"


def test_nextflow_tail_is_bounded_and_does_not_scan_full_source(tmp_path, ctx, monkeypatch):
    source = tmp_path / "trace.txt"
    source.write_text("task_id\tstatus\n" + "".join(f"{i}\tCOMPLETED\n" for i in range(300)))
    monkeypatch.setattr(workflows, "LIMIT", 200)
    value = workflows.nextflow(ctx, str(source))
    assert value["truncated"]
    assert 1 < len(value["nodes"]) < 20
    assert value["nodes"][-1]["id"] == "299"


@pytest.mark.parametrize("text", ["missing\tstatus\n1\tCOMPLETED\n", "task_id\tstatus\tstatus\n", "task_id\tstatus\n1\n", "task_id\tstatus\n1\tRUNNING\textra\n"])
def test_nextflow_malformed_trace(tmp_path, ctx, text):
    source = tmp_path / "trace.txt"
    source.write_text(text)
    with pytest.raises(ValueError):
        workflows.nextflow(ctx, str(source))


def test_workflow_remote_reader_never_substitutes_local(ctx):
    content = b"task_id\tstatus\nnative-task\tRUNNING\n"
    seen = []
    class Files:
        remote = True
        def snapshot_stat(self, path):
            seen.append(("stat", path))
            return {"size": len(content), "ident": (1, 2), "updated": (3, 4)}
        def read(self, path, offset, length):
            seen.append(("read", path, offset, length))
            return content[offset:offset + length]
    remote = Context(**{**ctx.__dict__, "files": Files()})
    value = ops_services.run("workflow-engine", {"source": "/remote/trace.tsv"}, remote)
    assert value["data"]["nodes"][0]["id"] == "native-task"
    assert all(row[1] == "/remote/trace.tsv" for row in seen)


def test_nextflow_rejects_concurrent_write(ctx):
    class Files:
        calls = 0
        def snapshot_stat(self, path):
            self.calls += 1
            return {"size": len(b"task_id\tstatus\n1\tRUNNING\n"), "updated": self.calls}
        def read(self, path, offset, length):
            return b"task_id\tstatus\n1\tRUNNING\n"[offset:offset + length]
    changed = Context(**{**ctx.__dict__, "files": Files()})
    with pytest.raises(ValueError, match="changed"):
        workflows.nextflow(changed, "trace")


def test_snakemake_native_dag_does_not_invent_runtime(tmp_path, ctx):
    source = tmp_path / "dag.json"
    source.write_text(json.dumps({"nodes": [{"id": 0, "value": {"label": "align"}}, {"id": 1, "value": {"label": "report"}}],
                                 "links": [{"source": 0, "target": 1}]}))
    value = workflows.snakemake(ctx, str(source))
    assert value["nodes"][1]["dependencies"] == ["0"]
    assert all(node["state"] == "unknown" and node["native_id"] == "" for node in value["nodes"])
    assert not value["runtime_available"]


@pytest.mark.parametrize("kind", ["cycle", "missing", "duplicate", "self", "wrong-engine"])
def test_snakemake_invalid_runtime_dag(tmp_path, ctx, kind):
    value = {"schema": "tower.workflow-engine/v1", "engine": "snakemake", "workflow_id": "runA",
             "nodes": [{"id": "a", "state": "complete", "dependencies": []},
                       {"id": "b", "state": "running", "dependencies": ["a"]}]}
    if kind == "cycle": value["nodes"][0]["dependencies"] = ["b"]
    if kind == "missing": value["nodes"][1]["dependencies"] = ["missing"]
    if kind == "duplicate": value["nodes"][1]["id"] = "a"
    if kind == "self": value["nodes"][1]["dependencies"] = ["b"]
    if kind == "wrong-engine": value["engine"] = "nextflow"
    source = tmp_path / "dag.json"
    source.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        workflows.snakemake(ctx, str(source))


def test_staging_existing_destination_replaced_during_verification(tmp_path, monkeypatch):
    document = manifest(tmp_path, (b"same",))
    prepared = staging.inspect(document)
    destination = Path(document["entries"][0]["destination"])
    destination.write_bytes(b"same")
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"evil")
    original = os.read
    swapped = False
    def read(fd, count):
        nonlocal swapped
        result = original(fd, count)
        if result == b"same" and not swapped:
            swapped = True
            os.replace(replacement, destination)
        return result
    monkeypatch.setattr(staging.os, "read", read)
    result = staging.execute(prepared, tmp_path / "receipts")
    assert result[0]["status"] == "failed"
    assert destination.read_bytes() == b"evil"


def test_staging_cancel_mid_chunk_cleans_temp_and_lists_remainder(tmp_path, monkeypatch):
    document = manifest(tmp_path, (b"a" * (staging.CHUNK * 2), b"next", b"last"))
    prepared = staging.inspect(document)
    cancel = threading.Event()
    original = os.read
    def read(fd, count):
        data = original(fd, count)
        if len(data) == staging.CHUNK:
            cancel.set()
        return data
    monkeypatch.setattr(staging.os, "read", read)
    results = staging.execute(prepared, tmp_path / "receipts", cancel=cancel)
    assert [row["status"] for row in results] == ["cancelled", "not-attempted", "not-attempted"]
    assert not list(tmp_path.glob(".tower-transfer-*"))
    assert not any(Path(item["destination"]).exists() for item in document["entries"])


def test_staging_source_mutates_during_copy(tmp_path, monkeypatch):
    document = manifest(tmp_path, (b"abcd",))
    prepared = staging.inspect(document)
    source = Path(document["entries"][0]["source"])
    original = os.read
    changed = False
    def read(fd, count):
        nonlocal changed
        data = original(fd, count)
        if data == b"abcd" and not changed:
            changed = True
            source.write_bytes(b"abcd")
        return data
    monkeypatch.setattr(staging.os, "read", read)
    assert staging.execute(prepared, tmp_path / "receipts")[0]["status"] == "failed"
    assert not Path(document["entries"][0]["destination"]).exists()


def test_staging_destination_directory_moves_during_copy(tmp_path, monkeypatch):
    document = manifest(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    document["entries"][0]["destination"] = str(out / "result")
    prepared = staging.inspect(document)
    original = os.read
    changed = False
    def read(fd, count):
        nonlocal changed
        data = original(fd, count)
        if data and not changed:
            changed = True
            out.rename(tmp_path / "moved-out")
            out.mkdir()
        return data
    monkeypatch.setattr(staging.os, "read", read)
    assert staging.execute(prepared, tmp_path / "receipts")[0]["status"] == "failed"
    assert not (out / "result").exists()
    assert not list((tmp_path / "moved-out").iterdir())


def test_services_sequential_transfer_observe_reconcile(ctx, tmp_path):
    content = b"task_id\tstatus\tnative_id\nlogical-1\tCOMPLETED\t41\n"
    document = manifest(tmp_path, (content,))
    source = tmp_path / "manifest.json"
    source.write_text(json.dumps(document))
    params = {"manifest": str(source)}
    plan = ops_services.run("staging", params, ctx)["plan"]
    result = ops_services.apply("staging", plan, ctx)
    assert result["status"] == "ok"
    trace = document["entries"][0]["destination"]
    workflow = ops_services.run("workflow-engine", {"source": trace, "workflow_id": "experiment-A"}, ctx)
    monitor = supervisor.serve(tmp_path / "monitor", supervisor.configuration(user="researcher", cluster="test"), runner=runner, max_polls=1)
    assert workflow["data"]["nodes"][0]["native_id"] == monitor["finished"][0]["id"]
    assert workflow["data"]["ownership"] == "engine"
    assert ops_services.apply("staging", plan, ctx)["data"]["entries"][0]["status"] == "verified-existing"


def test_snakemake_large_linear_dag_is_iterative(tmp_path, ctx):
    source = tmp_path / "large.json"
    source.write_text(json.dumps({"schema": "tower.workflow-engine/v1", "engine": "snakemake", "workflow_id": "large",
                                 "nodes": [{"id": str(i), "dependencies": [str(i - 1)] if i else []} for i in range(10000)]}))
    result = workflows.snakemake(ctx, str(source))
    assert len(result["nodes"]) == 10000


def test_documented_schema_examples_are_accepted(ctx):
    root = Path(__file__).resolve().parents[1]
    staging.validate(json.loads((root / "docs/examples/staging-manifest.json").read_text()))
    value = workflows.snakemake(ctx, str(root / "docs/examples/workflow-engine.json"))
    assert value["nodes"][1]["native_id"] == "1235"
    for path in (root / "docs/schemas/staging.schema.json", root / "docs/schemas/workflow-engine.schema.json"):
        assert json.loads(path.read_text())["$schema"].endswith("/schema")


def test_supervisor_sigterm_cancels_slow_collector(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    child_pid = tmp_path / "collector.pid"
    script = bin_dir / "squeue"
    script.write_text(f"#!{sys.executable}\nimport os,time\nopen({str(child_pid)!r},'w').write(str(os.getpid()))\ntime.sleep(30)\n")
    script.chmod(0o700)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    directory = tmp_path / "monitor"
    view = supervisor.start(directory, supervisor.configuration(interval=1))
    pid = view["owner"]["pid"]
    try:
        wait_until(child_pid.exists)
        os.kill(pid, signal.SIGTERM)
        wait_until(lambda: not supervisor.status(directory)["running"])
        collector = int(child_pid.read_text())
        assert not Path(f"/proc/{collector}").exists()
        assert services_io.read_json_file(directory / "snapshot.json")["state"] == "stopped"
    finally:
        supervisor.stop(directory)
    assert pid not in supervisor._CHILDREN
