"""Bulk historical exports preserve exact ownership, content and destination scope."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import threading

import pytest

from raven import log_bundle, projects
from raven.model import Finished
from raven.remote import LocalFiles

@pytest.fixture(autouse=True)
def durable_exports(tmp_path, monkeypatch):
    monkeypatch.setenv("RAVEN_EXPORT_DIR", str(tmp_path / "durable-exports"))



def request_for(paths, *, settings=None, root=""):
    records, details = [], {}
    for jid, stdout, stderr in paths:
        records.append(Finished(jid, name="historical " + jid, workdir=str(Path(stdout).parent) if stdout else ""))
        details[jid] = {"JobId": jid, "StdOut": str(stdout), "StdErr": str(stderr),
                        "WorkDir": str(Path(stdout).parent) if stdout else ""}
    return log_bundle.capture_jobs([record.id for record in records],
                                  {"finished": records, "details": details},
                                  registered_root=root, log_settings=settings)


def discover(paths, **kwargs):
    return log_bundle.discover_logs(request_for(paths), **kwargs)


def exported_bytes(result):
    root = Path(result["export_path"])
    return {entry["source_path"]: (root / entry["relative_path"]).read_bytes() for entry in result["files"]}


def save(report, tmp_path, **kwargs):
    return log_bundle.export_logs(report, str(tmp_path), root=str(tmp_path), **kwargs)


def test_capture_is_cheap_exact_deduplicated_and_independent(tmp_path, monkeypatch):
    def no_io(*args, **kwargs):
        pytest.fail("capture must not perform filesystem I/O")
    monkeypatch.setattr(os, "stat", no_io)
    monkeypatch.setattr(os, "open", no_io)
    finished = Finished("7", name="target")
    details = {"JobId": "7", "StdOut": "/exact/7.out"}
    binding = {"job_id": "8", "run_id": "another"}
    request = log_bundle.capture_jobs(["7", "7"], {"finished": [finished], "details": {"7": details}}, binding=binding)
    finished.name = "changed"
    details["StdOut"] = "/wrong/8.out"
    assert len(request["jobs"]) == 1
    assert request["jobs"][0]["record"].name == "target"
    assert request["jobs"][0]["details"]["StdOut"] == "/exact/7.out"
    assert "binding" not in request["jobs"][0]


@pytest.mark.parametrize("ids", [[], [""], ["7.batch"], ["../7"], ["7\n"], ["abc"], ["7;echo"], [str(i) for i in range(1025)]])
def test_capture_refuses_all_invalid_or_excess_selection(ids):
    with pytest.raises(ValueError, match="exact scheduler job IDs"):
        log_bundle.capture_jobs(ids, {})


def test_all_stdout_stderr_directory_and_manifest_locations_are_owned(tmp_path):
    out, err, extra = [tmp_path / name for name in ("out", "err", "third location")]
    for directory in (out, err, extra):
        directory.mkdir()
    allowed = [out / "77.out", err / "77.err", out / "rank-77.log", err / "worker_77.log", extra / "worker.log"]
    excluded = [out / "777.out", out / "177.log", out / "77.py", out / "77.checkpoint", err / "other77.log"]
    for source in allowed + excluded:
        source.write_bytes((source.name + "\n").encode())
    manifest = extra / "logs.json"
    manifest.write_text(json.dumps({"schema": "raven.logs/v1", "job_id": "77", "logs": [{"id": "worker", "path": "worker.log"}]}))
    request = request_for([("77", allowed[0], allowed[1])], settings={"manifest_file": str(manifest)})
    report = log_bundle.discover_logs(request)
    assert report["status"] == "ready", report
    assert {entry["path"] for entry in report["entries"]} == set(map(str, allowed))
    result = save(report, tmp_path)
    assert result["status"] == "ready", result
    assert exported_bytes(result) == {str(source): source.read_bytes() for source in allowed}
    document = json.loads((Path(result["export_path"]) / "manifest.json").read_text())
    assert document["schema"] == "raven.log-bundle/v1"
    assert document["jobs"] == ["77"] and len(document["files"]) == 5
    for directory in Path(result["export_path"]).rglob("*"):
        assert stat.S_IMODE(directory.stat().st_mode) == (0o700 if directory.is_dir() else 0o600)


def test_scheduler_array_placeholders_and_accounting_fallback_are_exact(tmp_path):
    source = tmp_path / "100_2_102.out"
    source.write_bytes(b"task two only\n")
    calls = []
    class Scheduler:
        def details(self, jid):
            calls.append(("control", jid))
            raise OSError("purged")
        def historical_details(self, jid):
            calls.append(("accounting", jid))
            return {"JobId": jid, "JobIDRaw": "102", "ArrayJobId": "100", "ArrayTaskId": "2",
                    "StdOut": "%A_%a_%j.out", "StdErr": "%A_%a_%j.out", "WorkDir": str(tmp_path), "LogPathSource": "sacct"}
    request = log_bundle.capture_jobs(["100_2"], {"finished": [Finished("100_2")]})
    report = log_bundle.discover_logs(request, slurm=Scheduler())
    assert calls == [("control", "100_2"), ("accounting", "100_2")]
    assert [entry["path"] for entry in report["entries"]] == [str(source)]
    assert "purged" in " ".join(report["warnings"])


def test_missing_second_declared_stream_is_reported_with_job_and_path(tmp_path):
    source, absent = tmp_path / "7.out", tmp_path / "7.err"
    source.write_bytes(b"full output\n")
    report = discover([("7", source, absent)])
    assert report["status"] == "partial"
    assert report["missing"][0]["job_id"] == "7"
    assert report["missing"][0]["label"] == "stderr"
    assert report["missing"][0]["path"] == str(absent)
    result = save(report, tmp_path)
    assert result["status"] == "partial" and len(result["files"]) == 1
    assert result["missing"] == report["missing"]


def test_unknown_job_does_not_guess_current_jobs_logs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "-99.out").write_bytes(b"unrelated guessed file")
    report = log_bundle.discover_logs(log_bundle.capture_jobs(["99"], {}))
    assert report["status"] == "error" and not report["entries"]
    assert report["missing"][0]["job_id"] == "99"


def test_shared_inode_outputs_are_deduplicated_with_job_associations(tmp_path):
    source, alias = tmp_path / "shared.log", tmp_path / "alias.log"
    source.write_bytes(b"shared source")
    os.link(source, alias)
    report = discover([("7", source, source), ("8", alias, alias)])
    assert report["file_count"] == 1 and report["total_bytes"] == source.stat().st_size
    assert report["entries"][0]["job_ids"] == ["7", "8"]
    assert {entry["path"] for entry in report["entries"][0]["aliases"]} == {str(source), str(alias)}
    result = save(report, tmp_path)
    assert result["files"][0]["job_ids"] == ["7", "8"]


@pytest.mark.parametrize("data", [b"", b"single", b"first\r\n\tsecond\n", b"\xff\x00\x1braw", "é→最後\n".encode(), b"FIRST\n" + b"x" * (3 * log_bundle.CHUNK_BYTES) + b"\nLAST"])
def test_complete_initial_bytes_preserved_and_unique_without_overwrite(tmp_path, data):
    source = tmp_path / "77.out"
    source.write_bytes(data)
    report = discover([("77", source, source)])
    first, second = save(report, tmp_path), save(report, tmp_path)
    assert first["status"] == second["status"] == "ready"
    assert first["export_path"] != second["export_path"]
    assert exported_bytes(first)[str(source)] == exported_bytes(second)[str(source)] == data
    assert first["bytes"] == len(data)


def test_same_basenames_from_multiple_locations_survive(tmp_path):
    paths = []
    for index in range(3):
        directory = tmp_path / str(index)
        directory.mkdir()
        source = directory / "77.log"
        source.write_bytes(f"source {index}".encode())
        paths.append(source)
    manifest = tmp_path / "logs.json"
    manifest.write_text(json.dumps({"schema": "raven.logs/v1", "job_id": "77", "logs": [{"id": str(i), "path": str(source)} for i, source in enumerate(paths)]}))
    report = log_bundle.discover_logs(request_for([("77", paths[0], paths[1])], settings={"manifest_file": str(manifest)}))
    result = save(report, tmp_path)
    assert len(result["files"]) == 3
    assert all(Path(entry["relative_path"]).name == "77.log" for entry in result["files"])
    assert exported_bytes(result) == {str(source): source.read_bytes() for source in paths}


def test_clipboard_combines_entire_logs_with_clear_headers(tmp_path, monkeypatch):
    paths = [tmp_path / "7.out", tmp_path / "7.err"]
    for index, source in enumerate(paths):
        source.write_bytes(f"FULL-{index}\tline\r\nEND-{index}".encode())
    sent = []
    def copy_file(path, **kwargs):
        sent.append(Path(path).read_bytes())
        return {"methods": ["test clipboard"], "warnings": []}
    monkeypatch.setattr(log_bundle.clipboard_io, "copy_file", copy_file)
    result = log_bundle.export_logs(discover([("7", *paths)]), state_dir=str(tmp_path / "state"), clipboard=True)
    assert result["status"] == "ready"
    assert len(sent) == 1
    for source in paths:
        assert source.read_bytes() in sent[0]
        assert str(source).encode() in sent[0]
    assert sent[0].count(b"===== JOB 7") == 2
    assert exported_bytes(result) == {str(source): source.read_bytes() for source in paths}


def test_clipboard_invalid_utf8_preserves_raw_and_sends_no_prefix(tmp_path, monkeypatch):
    source = tmp_path / "7.out"
    source.write_bytes(b"valid-prefix\n\xffinvalid\n")
    monkeypatch.setattr(log_bundle.clipboard_io, "osc52", lambda *args: pytest.fail("must not send invalid text"))
    monkeypatch.setattr(log_bundle.clipboard_io, "_tool_from_file", lambda *args: pytest.fail("must not send invalid text"))
    result = log_bundle.export_logs(discover([("7", source, source)]), state_dir=str(tmp_path / "state"), clipboard=True)
    assert result["status"] == "partial"
    assert not result["clipboard"]["methods"]
    assert "invalid UTF-8" in " ".join(result["warnings"])
    assert exported_bytes(result)[str(source)] == source.read_bytes()


def test_clipboard_limit_and_transport_failure_preserve_full_export(tmp_path, monkeypatch):
    source = tmp_path / "7.out"
    source.write_bytes(b"FIRST\n" + b"x" * 80000 + b"\nLAST")
    monkeypatch.setattr(log_bundle.clipboard_io, "osc52", lambda *args: pytest.fail("must not send truncated OSC52"))
    result = log_bundle.export_logs(discover([("7", source, source)]), state_dir=str(tmp_path / "state"), clipboard=True, use_tools=False)
    assert result["status"] == "partial"
    assert "OSC 52 skipped" in " ".join(result["warnings"])
    assert exported_bytes(result)[str(source)] == source.read_bytes()


def test_clipboard_creates_missing_nested_private_staging_parents(tmp_path, monkeypatch):
    source = tmp_path / "7.out"
    source.write_bytes(b"complete\n")
    state = tmp_path / "new-parent/nested/state"
    monkeypatch.setenv("RAVEN_EXPORT_DIR", str(state / "exports"))
    result = log_bundle.export_logs(discover([("7", source, source)]), state_dir=str(state),
                                   clipboard=True, use_osc52=False, use_tools=False)
    assert result["status"] == "ready"
    assert exported_bytes(result)[str(source)] == b"complete\n"
    for path in (tmp_path / "new-parent", tmp_path / "new-parent/nested", state, state / "exports"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700


def test_clipboard_raced_staging_creator_cannot_substitute_symlink(tmp_path, monkeypatch):
    source = tmp_path / "7.out"
    source.write_bytes(b"complete\n")
    report = discover([("7", source, source)])
    outside = tmp_path / "outside"
    outside.mkdir()
    mkdir = os.mkdir
    def race(name, mode=0o777, *, dir_fd=None):
        if name == "new-state":
            os.symlink(str(outside), name, dir_fd=dir_fd)
            raise FileExistsError("concurrent symlink")
        return mkdir(name, mode, dir_fd=dir_fd)
    monkeypatch.setenv("RAVEN_EXPORT_DIR", str(tmp_path / "new-state"))
    monkeypatch.setattr(log_bundle.os, "mkdir", race)
    result = log_bundle.export_logs(report, state_dir=str(tmp_path / "new-state"),
                                   clipboard=True, use_osc52=False, use_tools=False)
    assert result["status"] == "error" and not result["export_path"]
    assert not list(outside.iterdir())


@pytest.mark.parametrize("change", ["rotate", "rewrite", "shrink", "append"])
def test_mutating_source_during_stream_never_publishes_incomplete_file(tmp_path, monkeypatch, change):
    source = tmp_path / "7.out"
    source.write_bytes(b"original complete\n")
    report = discover([("7", source, source)])
    read = os.read
    changed = False
    def mutate(fd, size):
        nonlocal changed
        data = read(fd, size)
        if not changed:
            changed = True
            if change == "rotate":
                source.rename(tmp_path / "rotated.out")
                source.write_bytes(b"replacement")
            elif change == "append":
                with source.open("ab") as output:
                    output.write(b"new appendix\n")
            elif change == "rewrite":
                source.write_bytes(b"x" * len(data))
            else:
                source.write_bytes(b"short")
        return data
    monkeypatch.setattr(log_bundle.os, "read", mutate)
    result = save(report, tmp_path)
    if change == "append":
        assert result["status"] == "partial" and result["files"][0]["grew"]
        assert exported_bytes(result)[str(source)] == b"original complete\n"
    else:
        assert result["status"] == "error" and not result["export_path"]
        assert result["missing"][0]["job_id"] == "7"
        assert not list(tmp_path.glob("raven-logs-*"))


def test_rotation_between_discovery_and_save_is_refused(tmp_path):
    source = tmp_path / "7.out"
    source.write_bytes(b"chosen")
    report = discover([("7", source, source)])
    source.rename(tmp_path / "old")
    source.write_bytes(b"wrong replacement")
    result = save(report, tmp_path)
    assert result["status"] == "error" and "rotated" in result["missing"][0]["message"]
    assert not list(tmp_path.glob("raven-logs-*"))


def test_stream_cancel_event_removes_whole_unfinished_bundle(tmp_path):
    source = tmp_path / "7.out"
    source.write_bytes(b"x" * (2 * log_bundle.CHUNK_BYTES))
    cancelled = threading.Event()
    def progress(done, total):
        if done:
            cancelled.set()
    result = save(discover([("7", source, source)]), tmp_path, cancel=cancelled, progress=progress)
    assert result["status"] == "cancelled"
    assert not result["export_path"] and not result["files"]
    assert not list(tmp_path.glob("raven-logs-*"))


def test_cancel_before_discovery_and_export_is_explicit(tmp_path):
    report = log_bundle.discover_logs(log_bundle.capture_jobs(["7"], {}), cancel=lambda: True)
    assert report["status"] == "cancelled" and not report["entries"]
    result = save(report, tmp_path, cancel=lambda: True)
    assert result["status"] == "cancelled"
    assert not list(tmp_path.glob("raven-logs-*"))


def test_directory_list_mkdir_parent_and_symlinks_are_confined(tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    (root / "sub").mkdir()
    (root / "file").write_bytes(b"no")
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)
    listing = log_bundle.list_directories(str(root))
    assert listing["status"] == "ready" and listing["parent"] is None
    assert [entry["name"] for entry in listing["entries"]] == ["sub"]
    created = log_bundle.mkdir_directory(str(root), str(root / "sub"), "New folder")
    assert created["status"] == "ready" and created["created"] == str(root / "sub/New folder")
    assert stat.S_IMODE((root / "sub/New folder").stat().st_mode) == 0o700
    assert log_bundle.list_directories(str(root), str(root / "escape"))["status"] == "error"
    assert log_bundle.list_directories(str(root), str(outside))["status"] == "error"
    assert not list(outside.iterdir())


@pytest.mark.parametrize("name", ["", ".", "..", "../escape", "a/b", "a\\b", "bad\nname", "\x1b", "x" * 256, "é" * 128])
def test_mkdir_refuses_unsafe_names_without_writing(tmp_path, name):
    result = log_bundle.mkdir_directory(str(tmp_path), str(tmp_path), name)
    assert result["status"] == "error" and not list(tmp_path.iterdir())


def test_mkdir_does_not_replace_existing_folder_or_file(tmp_path):
    existing = tmp_path / "existing"
    existing.mkdir()
    (existing / "keep").write_bytes(b"existing")
    result = log_bundle.mkdir_directory(str(tmp_path), str(tmp_path), "existing")
    assert result["status"] == "error"
    assert (existing / "keep").read_bytes() == b"existing"


def test_destination_symlink_or_escape_is_refused_for_export(tmp_path):
    root, outside = tmp_path / "projects", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "escape").symlink_to(outside)
    source = tmp_path / "7.out"
    source.write_bytes(b"actual")
    report = discover([("7", source, source)])
    for destination in (outside, root / "escape"):
        result = log_bundle.export_logs(report, str(destination), root=str(root))
        assert result["status"] == "error" and not result["export_path"]
    assert not list(outside.iterdir())


def test_replaced_destination_before_publish_is_detected_and_pinned_cleanup(tmp_path):
    source = tmp_path / "7.out"
    source.write_bytes(b"actual")
    destination = tmp_path / "destination"
    destination.mkdir()
    report = discover([("7", source, source)])
    changed = False
    def progress(done, total):
        nonlocal changed
        if done and not changed:
            changed = True
            destination.rename(tmp_path / "renamed")
            destination.mkdir()
    result = log_bundle.export_logs(report, str(destination), root=str(tmp_path), progress=progress)
    assert result["status"] == "error" and "Destination changed" in result["message"]
    assert not list(destination.iterdir()) and not list((tmp_path / "renamed").iterdir())


def test_directory_bounds_are_explicit_and_cancellable(tmp_path, monkeypatch):
    for index in range(5):
        (tmp_path / str(index)).mkdir()
    monkeypatch.setattr(log_bundle, "MAX_VISIBLE_DIRECTORIES", 2)
    result = log_bundle.list_directories(str(tmp_path))
    assert result["status"] == "ready" and result["limited"]
    assert len(result["entries"]) == 2 and "limited" in result["message"]
    cancelled = log_bundle.list_directories(str(tmp_path), cancel=lambda: True)
    assert cancelled["status"] == "cancelled"


class Adapter(LocalFiles):
    remote = True
    def __init__(self, data, *, short=False):
        self.data = data
        self.short = short
        self.reads = []
    def snapshot_stat(self, path):
        return {"size": len(self.data[path]), "ident": path, "updated": 1}
    def listdir(self, path):
        return []
    def read(self, path, offset, size):
        self.reads.append((path, offset, size))
        return self.data[path][offset:offset + size - (1 if self.short else 0)]


def test_remote_adapter_never_reads_local_lookalike_and_exports_exact_bytes(tmp_path):
    adapter = Adapter({"/remote/7.out": b"remote exact\xff\n"})
    report = discover([("7", "/remote/7.out", "/remote/7.out")], files=adapter)
    result = save(report, tmp_path, files=adapter)
    assert result["status"] == "partial"  # explicit inventory limitation
    assert exported_bytes(result)["/remote/7.out"] == b"remote exact\xff\n"
    assert adapter.reads == [("/remote/7.out", 0, 14)]
    assert "Remote scheduler" in " ".join(result["warnings"])


def test_remote_short_read_is_not_a_complete_output(tmp_path):
    adapter = Adapter({"/remote/7.out": b"complete"}, short=True)
    report = discover([("7", "/remote/7.out", "/remote/7.out")], files=adapter)
    result = save(report, tmp_path, files=adapter)
    assert result["status"] == "error" and "short" in result["missing"][0]["message"]
    assert not list(tmp_path.glob("raven-logs-*"))


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    template = Path(__file__).resolve().parents[1] / "examples/project-template"
    shutil.copytree(template, root)
    spec = importlib.util.spec_from_file_location("bundle_reporting", root / "reporting.py")
    producer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(producer)
    run = producer.begin_run(root, "attempt-1", job_id="77", name="owned", script="experiment.py")
    for role in ("stdout", "stderr"):
        (run / "logs" / (role + ".log")).write_bytes((role + "\n").encode())
    return root, producer, run


def test_exact_project_job_links_all_declared_multiple_locations(project, tmp_path):
    root, producer, run = project
    outside = tmp_path / "external.log"
    outside.write_bytes(b"declared external\n")
    producer.register_log(run, "external", outside)
    request = log_bundle.capture_jobs(["77"], {"finished": [Finished("77", workdir=str(run))]}, registered_root=str(root))
    report = log_bundle.discover_logs(request)
    assert {entry["path"] for entry in report["entries"]} == {str(run / "logs/stdout.log"), str(run / "logs/stderr.log"), str(outside)}
    assert all(entry.get("binding", {}).get("job_id") == "77" for entry in report["entries"])
    result = save(report, tmp_path)
    assert len(result["files"]) == 3 and not result["missing"]
    assert exported_bytes(result)[str(outside)] == outside.read_bytes()


def test_project_changed_job_identity_between_discovery_and_save_is_refused(project, tmp_path):
    root, _, run = project
    request = log_bundle.capture_jobs(["77"], {"finished": [Finished("77", workdir=str(run))]}, registered_root=str(root))
    report = log_bundle.discover_logs(request)
    inventory = json.loads((run / "run.json").read_text())
    inventory["job_id"] = "78"
    (run / "run.json").write_text(json.dumps(inventory))
    result = save(report, tmp_path)
    assert result["status"] == "error" and not result["files"]
    assert all(entry["job_id"] == "77" and "changed" in entry["message"] for entry in result["missing"])


def test_project_changed_log_declaration_between_discovery_and_save_is_refused(project, tmp_path):
    root, _, run = project
    request = log_bundle.capture_jobs(["77"], {"finished": [Finished("77", workdir=str(run))]}, registered_root=str(root))
    report = log_bundle.discover_logs(request)
    inventory = json.loads((run / "run.json").read_text())
    inventory["paths"].pop("stdout")
    (run / "run.json").write_text(json.dumps(inventory))
    index = json.loads((run / "logs.json").read_text())
    index["logs"] = [entry for entry in index["logs"] if entry["path"] != "logs/stdout.log"]
    (run / "logs.json").write_text(json.dumps(index))
    result = save(report, tmp_path)
    assert result["status"] == "partial" and len(result["files"]) == 1
    assert "declaration" in result["missing"][0]["message"]


def test_project_selected_binding_never_exports_another_attempt(project, tmp_path):
    root, _, run = project
    selected = projects.select_run(str(root), "attempt-1")
    bad_binding = dict(selected["binding"], attempt=999)
    request = log_bundle.capture_jobs(["77"], {"finished": [Finished("77", workdir=str(run))]}, binding=bad_binding, project_logs=selected["logs"])
    report = log_bundle.discover_logs(request)
    assert report["status"] == "error" and not report["entries"]
    assert "attempt changed" in report["missing"][0]["message"]
