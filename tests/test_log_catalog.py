"""Real portable manifests and bounded, backend-correct log catalogue reads."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import pytest

from raven import log_catalog
from raven.log_catalog import LogCatalog, build_catalog
from raven.remote import LocalFiles, RemoteFiles


def manifest(path, entries, **values):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema": "raven.logs/v1", "logs": entries, **values}), encoding="utf-8")
    return str(path)


def paths(result):
    return [entry["path"] for entry in result["entries"]]


def test_manifest_multiple_locations_and_groups(tmp_path):
    source = tmp_path / "metadata" / "logs.json"
    external = tmp_path / "outside" / "worker 2.log"
    file = manifest(source, [
        {"id": "train", "path": "../components/train.log", "label": "Training", "group": "Workers"},
        {"id": "worker2", "path": str(external), "group": "Workers", "description": "Rank 2"},
        {"id": "build", "path": "./build//compiler.log", "label": "Compilation", "group": "Setup"},
    ], job_id="77", run_id="portable-run-1")
    result = build_catalog("77", "", "", manifest_file=file)
    assert result["status"] == "ready"
    assert paths(result) == [str(tmp_path / "components" / "train.log"), str(external), str(source.parent / "build/compiler.log")]
    assert result["entries"][0]["id"] == "manifest.train"
    assert result["entries"][1]["description"] == "Rank 2"
    assert [entry["group"] for entry in result["entries"]] == ["Workers", "Workers", "Setup"]


def test_local_relative_manifest_resolves_its_own_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    manifest(tmp_path / "metadata/logs.json", [{"id": "task", "path": "../task.log"}])
    result = build_catalog("77", "", "", manifest_file="metadata/logs.json")
    assert result["manifest_file"] == str(tmp_path / "metadata/logs.json")
    assert paths(result) == [str(tmp_path / "task.log")]


@pytest.mark.parametrize("field,value", [
    ("schema", "raven.logs/v2"), ("job_id", "78"), ("job_id", 77),
    ("run_id", "../other"), ("run_id", "bad/name"), ("run_id", "é"),
    ("run_id", ""), ("logs", {}), ("logs", [None]), ("unexpected", True),
])
def test_invalid_manifest_atomic_scheduler_still_available(tmp_path, field, value):
    payload = {"schema": "raven.logs/v1", "logs": [{"id": "would-leak", "path": "one.log"}], field: value}
    source = tmp_path / "logs.json"
    source.write_text(json.dumps(payload), encoding="utf-8")
    result = build_catalog("77", str(tmp_path / "77.out"), "", manifest_file=str(source))
    assert result["status"] == "partial"
    assert paths(result) == [str(tmp_path / "77.out")]
    assert "Log manifest unavailable" in " ".join(result["messages"])


@pytest.mark.parametrize("entry", [
    {"id": "../bad", "path": "one.log"}, {"id": "ok"},
    {"id": "ok", "path": ""}, {"id": "ok", "path": "logs/*.log"},
    {"id": "ok", "path": "logs/[1].log"}, {"id": "ok", "path": "logs\\one.log"},
    {"id": "ok", "path": "~/.secret"}, {"id": "ok", "path": "one\n.log"},
    {"id": "ok", "path": "one.log", "label": "\x1b[31m"},
    {"id": "ok", "path": "one.log", "group": ""},
    {"id": "ok", "path": "one.log", "description": "x" * 513},
    {"id": "ok", "path": "one.log", "label": "x" * 161},
    {"id": "ok", "path": "one.log", "extra": "unknown"},
    {"id": "ok", "path": "x" * 4097},
])
def test_invalid_entry_rejects_entire_manifest(tmp_path, entry):
    source = manifest(tmp_path / "logs.json", [{"id": "valid", "path": "valid.log"}, entry])
    result = build_catalog("77", "", "", manifest_file=source)
    assert result["status"] == "error"
    assert result["entries"] == []


def test_duplicate_ids_rejected_and_duplicate_paths_deduplicated(tmp_path):
    source = manifest(tmp_path / "logs.json", [{"id": "same", "path": "first.log"}, {"id": "same", "path": "second.log"}])
    result = build_catalog("77", "", "", manifest_file=source)
    assert result["status"] == "error"
    assert "duplicate log IDs" in result["messages"][0]
    source = manifest(tmp_path / "logs.json", [{"id": "one", "path": "./first.log"}, {"id": "two", "path": "sub/../first.log"}])
    result = build_catalog("77", "", "", manifest_file=source)
    assert result["status"] == "ready"
    assert len(result["entries"]) == 1


def test_scheduler_duplicate_path_and_manifest_collision_are_one_file(tmp_path):
    output = str(tmp_path / "77.out")
    source = manifest(tmp_path / "logs.json", [{"id": "stdout", "path": "77.out"}, {"id": "extra", "path": "component.log"}])
    result = build_catalog("77", output, output, manifest_file=source)
    assert len(result["entries"]) == 2
    assert result["entries"][0]["label"] == "stdout / stderr"
    assert result["entries"][0]["group"] == "Scheduler"
    assert result["entries"][1]["source"] == "manifest"


def test_relative_native_path_deduplicates_absolute_manifest_alias(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source = manifest(tmp_path / "logs.json", [{"id": "alias", "path": str(tmp_path / "77.out")}])
    result = build_catalog("77", "77.out", "", manifest_file=source)
    assert paths(result) == ["77.out"]


def test_discovers_both_stdout_and_stderr_directories_with_exact_id_boundaries(tmp_path):
    first, second = tmp_path / "out", tmp_path / "err"
    first.mkdir()
    second.mkdir()
    for directory, name in [
        (first, "77.out"), (second, "77.err"), (first, "rank_77_worker.log"),
        (second, "worker-77.debug.log"), (first, "177.out"), (first, "777.out"),
        (second, "job77.out"), (second, "77a.out"), (second, "77_2.out"),
    ]:
        (directory / name).write_text("log", encoding="utf-8")
    result = build_catalog("77", str(first / "77.out"), str(second / "77.err"))
    assert result["status"] == "ready"
    assert set(paths(result)) == {str(first / "77.out"), str(second / "77.err"), str(first / "rank_77_worker.log"), str(second / "worker-77.debug.log"), str(second / "77_2.out")}


def test_parent_job_includes_array_tasks_and_heterogeneous_components(tmp_path):
    allowed = ["77.out", "worker_77_0.log", "77_12.out", "77+1.err"]
    excluded = ["777_0.out", "177+1.err", "other77_0.log"]
    for name in allowed + excluded:
        (tmp_path / name).touch()
    result = build_catalog("77", str(tmp_path / "77.out"), "")
    assert set(paths(result)) == {str(tmp_path / name) for name in allowed}


@pytest.mark.parametrize("jid,allowed,excluded", [
    ("77_2", ["77_2.out", "shared-77.out", "worker_77_2_rank.log"], ["77_20.out", "77_3.out", "177_2.out"]),
    ("77+1", ["77+1.out", "shared-77.out"], ["77+10.out", "77+2.out", "177+1.out"]),
])
def test_array_and_heterogeneous_parent_boundary(tmp_path, jid, allowed, excluded):
    for name in allowed + excluded:
        (tmp_path / name).touch()
    result = build_catalog(jid, str(tmp_path / allowed[0]), "")
    assert set(paths(result)) == {str(tmp_path / name) for name in allowed}


def test_catalog_does_not_read_or_stat_log_contents(tmp_path):
    class Files(LocalFiles):
        def listdir(self, path):
            assert path == str(tmp_path)
            return ["77.out", "worker-77.log"]

        def stat(self, path):
            raise AssertionError("catalog must not stat each log")

        def read(self, path, offset, length):
            raise AssertionError("catalog must not read log contents")

    result = build_catalog("77", str(tmp_path / "77.out"), "", files=Files())
    assert len(result["entries"]) == 2
    assert not result["messages"]


def test_directory_limits_and_invalid_names_are_visible(tmp_path):
    class Files(LocalFiles):
        def listdir(self, path):
            return ["77\x1b.log", "77/evil.log", "77\\evil.log"] + [f"unrelated_x{index}.log" for index in range(4094)] + ["77-after-cutoff.log"]

    result = build_catalog("77", str(tmp_path / "77.out"), "", files=Files())
    assert paths(result) == [str(tmp_path / "77.out")]
    assert result["status"] == "partial"
    assert "4096 names" in result["messages"][0]


def test_catalog_total_limit_includes_native_scheduler_logs(tmp_path):
    source = manifest(tmp_path / "logs.json", [{"id": f"log-{index}", "path": f"component-{index}.log"} for index in range(256)])
    result = build_catalog("77", str(tmp_path / "77.out"), str(tmp_path / "77.err"), manifest_file=source)
    assert len(result["entries"]) == 256
    assert result["status"] == "partial"
    assert "256-log limit" in " ".join(result["messages"])


def test_manifest_entry_count_limit_rejects_before_partial_admission(tmp_path):
    source = manifest(tmp_path / "logs.json", [{"id": f"log-{index}", "path": f"component-{index}.log"} for index in range(257)])
    result = build_catalog("77", "", "", manifest_file=source)
    assert result["entries"] == []
    assert "256 entries" in result["messages"][0]


@pytest.mark.parametrize("payload", [
    b'{"schema":"raven.logs/v1","schema":"raven.logs/v1","logs":[]}',
    b'{"schema":"raven.logs/v1","logs":[],"run_id":NaN}',
    b'{"schema":"raven.logs/v1","logs":[],"run_id":1e400}',
    b'\xff', b'[]', b'null', b'{',
])
def test_strict_manifest_json(tmp_path, payload):
    source = tmp_path / "logs.json"
    source.write_bytes(payload)
    result = build_catalog("77", "", "", manifest_file=str(source))
    assert result["status"] == "error"
    assert result["entries"] == []


def test_local_manifest_size_limit(tmp_path):
    source = tmp_path / "logs.json"
    source.write_bytes(b" " * (log_catalog.MAX_MANIFEST_BYTES + 1))
    result = build_catalog("77", "", "", manifest_file=str(source))
    assert "262144" in result["messages"][0]


@pytest.mark.parametrize("kind", ["symlink", "fifo", "directory"])
def test_nonregular_local_manifest_rejected_without_blocking(tmp_path, kind):
    source = tmp_path / "logs.json"
    if kind == "symlink":
        target = tmp_path / "target.json"
        manifest(target, [])
        source.symlink_to(target)
    elif kind == "fifo":
        os.mkfifo(source)
    else:
        source.mkdir()
    result = build_catalog("77", "", "", manifest_file=str(source))
    assert result["status"] == "error"
    assert result["entries"] == []


def test_resolved_path_length_limit(tmp_path):
    source = manifest(tmp_path / "logs.json", [{"id": "long", "path": "x" * 4090}])
    result = build_catalog("77", "", "", manifest_file=source)
    assert "resolves beyond" in result["messages"][0]


class RemoteFixture(LocalFiles):
    remote = True

    def __init__(self, payload, *, changed=False, truncated=False, fail=False):
        self.payload = payload
        self.changed = changed
        self.truncated = truncated
        self.fail = fail
        self.calls = []
        self.stats = 0

    def stat(self, path):
        self.calls.append(("stat", path))
        self.stats += 1
        if self.fail:
            raise OSError("SSH unavailable")
        return len(self.payload), (1, self.stats if self.changed else 2)

    def read(self, path, offset, length):
        self.calls.append(("read", path, offset, length))
        return self.payload[:-1] if self.truncated else self.payload

    def listdir(self, path):
        self.calls.append(("listdir", path))
        return ["77.out", "77.err", "worker-77.log"]


def test_remote_manifest_has_no_local_fallback_or_path_rebasing(tmp_path):
    source = tmp_path / "logs.json"
    manifest(source, [{"id": "local-wrong", "path": "local.log"}])
    remote = RemoteFixture(json.dumps({"schema": "raven.logs/v1", "job_id": "77", "logs": [{"id": "remote", "path": "../remote.log"}]}).encode())
    result = build_catalog("77", "", "", manifest_file=str(source), files=remote)
    assert paths(result) == [str(tmp_path.parent / "remote.log")]
    assert remote.calls[1] == ("read", str(source), 0, len(remote.payload) + 1)
    assert result["entries"][0]["id"] == "manifest.remote"


@pytest.mark.parametrize("option", ["changed", "truncated", "fail"])
def test_remote_manifest_unstable_or_failed_reads_do_not_fall_back(tmp_path, option):
    source = manifest(tmp_path / "logs.json", [{"id": "local", "path": "local.log"}])
    remote = RemoteFixture(b'{"schema":"raven.logs/v1","logs":[]}', **{option: True})
    result = build_catalog("77", "", "", manifest_file=source, files=remote)
    assert result["status"] == "error"
    assert result["entries"] == []


def test_remote_bounded_directory_listing_keeps_same_backend():
    payload = b'{"schema":"raven.logs/v1","logs":[]}'
    calls = []

    class SSH:
        def run(self, command, timeout):
            calls.append(command)
            if command[:3] == ["stat", "-c", "%f %d %i %s %Y %Z"]:
                return f"81a4 1 2 {len(payload)} 1 1", 0
            if command[0] == "stat":
                return f"1 2 {len(payload)}", 0
            script = command[2]
            if "find --" in script:
                assert "head -z -n 4097" in script
                return base64.b64encode(b"worker-77.log\0worker-777.log\0").decode(), 0
            assert "dd if=" in script
            return base64.b64encode(payload).decode(), 0

    files = RemoteFiles(SSH())
    result = build_catalog("77", "/remote/out/77.out", "/remote/err/77.err", manifest_file="/remote/logs.json", files=files)
    assert result["status"] == "ready"
    assert set(paths(result)) == {"/remote/out/77.out", "/remote/err/77.err", "/remote/out/worker-77.log", "/remote/err/worker-77.log"}
    assert len([command for command in calls if command[0] == "stat"]) == 4
    assert len([command for command in calls if command[0] == "sh" and "find --" in command[2]]) == 2


@pytest.mark.parametrize("mode", ["a1ff", "11a4", "41ed"])
def test_real_remote_adapter_manifest_regular_file_check(mode):
    class SSH:
        def run(self, command, timeout):
            assert command[0] == "stat"
            return f"{mode} 1 2 0 1 1", 0

    result = build_catalog("77", "", "", manifest_file="/remote/logs.json", files=RemoteFiles(SSH()))
    assert result["status"] == "error"
    assert "regular file" in result["messages"][0]


def test_remote_oversized_directory_response_refused():
    class SSH:
        def run(self, command, timeout):
            return "A" * (log_catalog.MAX_DIRECTORY_REPLY + 1), 0

    result = build_catalog("77", "/remote/77.out", "", files=RemoteFiles(SSH()))
    assert paths(result) == ["/remote/77.out"]
    assert "bounded reply limit" in result["messages"][0]


class DeferredWorker:
    def __init__(self):
        self.tasks = []
        self.busy = False

    def start_task(self, read, completion):
        if self.busy:
            return False
        self.busy = True
        self.tasks.append((read, completion))
        return True

    def finish(self, value=None):
        read, completion = self.tasks.pop(0)
        self.busy = False
        completion(read() if value is None else value)


def test_cached_coordinator_synchronous_contexts_ttl_force_and_eight_entry_bound(monkeypatch):
    now = [10.0]
    reads = []
    monkeypatch.setattr(log_catalog.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(log_catalog, "build_catalog", lambda jid, out, err, **kwargs: reads.append((jid, out, err, kwargs)) or {"status": "ready", "job_id": jid, "entries": [], "messages": [], "manifest_file": kwargs["manifest_file"]})
    catalog = LogCatalog()
    catalog.request("77", "one", "two")
    catalog.request("77", "one", "two")
    assert len(reads) == 1
    now[0] += 29
    catalog.request("77", "one", "two")
    assert len(reads) == 1
    now[0] += 1
    catalog.request("77", "one", "two")
    assert len(reads) == 2
    catalog.request("77", "one", "two", force=True)
    assert len(reads) == 3
    catalog.request("77", "changed", "two", "manifest.json")
    assert len(reads) == 4
    for jid in range(20):
        catalog.request(str(jid), "", "")
    assert len(catalog.cache) == 8


def test_generation_drops_old_job_result_and_never_returns_it_for_new_selection():
    catalog, worker = LogCatalog(), DeferredWorker()
    first = catalog.request("77", "", "", worker=worker)
    assert first["status"] == "loading"
    second = catalog.request("88", "", "", worker=worker)
    assert second["job_id"] == "88"
    assert second["entries"] == []
    assert len(worker.tasks) == 1
    worker.finish()
    assert catalog.current("77", "", "")["status"] == "loading"
    assert catalog.current("88", "", "")["status"] == "loading"
    catalog.request("88", "", "", worker=worker)
    worker.finish()
    assert catalog.current("88", "", "")["status"] == "ready"
    assert catalog.current("77", "", "")["status"] == "loading"


def test_worker_busy_retries_without_unbounded_queue():
    catalog, worker = LogCatalog(), DeferredWorker()
    worker.busy = True
    for _ in range(20):
        assert catalog.request("77", "", "", worker=worker)["status"] == "loading"
    assert worker.tasks == []
    assert catalog.pending is None
    worker.busy = False
    catalog.request("77", "", "", worker=worker)
    for _ in range(20):
        catalog.request("77", "", "", worker=worker)
    assert len(worker.tasks) == 1
    worker.finish()
    assert catalog.current("77", "", "")["status"] == "ready"


def test_configure_and_close_discard_pending_results():
    catalog, worker = LogCatalog(), DeferredWorker()
    catalog.request("77", "", "", worker=worker)
    replacement = RemoteFixture(b"{}")
    catalog.configure(files=replacement)
    worker.finish()
    assert catalog.current("77", "", "")["status"] == "loading"
    assert catalog.files is replacement
    catalog.request("88", "", "", worker=worker)
    catalog.close()
    worker.finish()
    assert catalog.cache == {}
    assert catalog.current("88", "", "")["status"] == "error"


def test_background_errors_are_visible_and_completion_clears_pending():
    catalog, worker = LogCatalog(), DeferredWorker()
    catalog.request("77", "", "", worker=worker)
    worker.finish(ValueError("bad\x1binput"))
    result = catalog.current("77", "", "")
    assert result["status"] == "error"
    assert result["messages"] == ["bad input"]
    assert catalog.pending is None


@pytest.mark.parametrize("fail", [False, True])
def test_existing_single_worker_wait_publishes_success_or_failure(monkeypatch, fail):
    from raven.research import ResearchHub

    if fail:
        def read(*args, **kwargs):
            raise ValueError("reader failure")
        monkeypatch.setattr(log_catalog, "build_catalog", read)
    hub = ResearchHub({"research": {}})
    catalog = LogCatalog()
    try:
        result = catalog.request("77", "", "", worker=hub, wait=True)
        assert result["status"] == ("error" if fail else "ready")
        assert catalog.pending is None
        assert hub.pending is None
        if fail:
            assert result["messages"] == ["reader failure"]
    finally:
        catalog.close()
        hub.close()


def test_selected_job_invalid_controls_never_reach_output_or_scan():
    result = build_catalog("77\x1b[31m", "", "")
    assert result["status"] == "error"
    assert "\x1b" not in str(result)
