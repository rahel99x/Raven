"""Atomic user state, exclusive local writers, and recovery without data loss."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from raven.model import Store
from raven import state_ownership, ui_state


def test_failed_tag_replace_preserves_old_file_and_retry_saves_latest(tmp_path, monkeypatch):
    store = Store(str(tmp_path))
    store.tag("7", "old")
    original = (tmp_path / "tags.json").read_bytes()
    replace = ui_state.os.replace
    monkeypatch.setattr(ui_state.os, "replace", lambda *args: (_ for _ in ()).throw(OSError("disk full")))
    store.tag("7", "new")
    store.pin("7")
    store.note("7", "latest note")
    assert (tmp_path / "tags.json").read_bytes() == original
    assert "disk full" in store.health["state tags"].error
    assert store.tags_of("7") == ["old", "new"]
    monkeypatch.setattr(ui_state.os, "replace", replace)
    store._save_tags()
    assert not store.health["state tags"].error
    assert json.loads((tmp_path / "tags.json").read_text())["7"] == {"tags": ["old", "new"], "pinned": True, "note": "latest note"}
    store.close()
    reopened = Store(str(tmp_path))
    assert reopened.pinned("7") and reopened.tags_of("7") == ["old", "new"]
    reopened.close()


def test_concurrent_save_never_holds_store_lock_and_newest_tags_win(tmp_path, monkeypatch):
    store = Store(str(tmp_path))
    entered, release = threading.Event(), threading.Event()
    original = ui_state._atomic
    def slow(path, payload):
        if b'"old"' in payload and b'"new"' not in payload:
            entered.set()
            assert release.wait(3)
        original(path, payload)
    monkeypatch.setattr(ui_state, "_atomic", slow)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(store.tag, "7", "old")
        assert entered.wait(2)
        assert store.lock.acquire(timeout=.1)
        store.lock.release()
        second = pool.submit(store.tag, "7", "new")
        release.set()
        first.result(2); second.result(2)
    assert json.loads((tmp_path / "tags.json").read_text())["7"]["tags"] == ["old", "new"]
    store.close()


def test_atomic_reader_only_observes_complete_old_or_new_snapshot(tmp_path):
    store = Store(str(tmp_path)); store.note("7", "A" * 10000)
    with ThreadPoolExecutor(1) as pool:
        writing = pool.submit(lambda: [store.note("7", char * 10000) for char in "AB" * 20])
        while not writing.done():
            assert json.loads((tmp_path / "tags.json").read_text())["7"]["note"] in ("A" * 10000, "B" * 10000)
        writing.result()
    store.close()


def test_old_plain_json_stays_compatible_and_bound_is_visible(tmp_path):
    (tmp_path / "tags.json").write_text('{"7":{"tags":["old"],"pinned":true,"note":"legacy"}}')
    store = Store(str(tmp_path)); assert store.pinned("7")
    store.note("7", "X" * (2 << 20))
    assert store.tags_status["state"] == "error" and "2 MiB" in store.tags_status["message"]
    assert json.loads((tmp_path / "tags.json").read_text())["7"]["note"] == "legacy"
    store.close()


def test_second_process_reader_cannot_write_and_crash_releases_owner(tmp_path):
    script = "from raven.model import Store; import sys,time; s=Store(sys.argv[1],strict_owner=True); print('ready',flush=True); time.sleep(60)"
    child = subprocess.Popen([sys.executable, "-c", script, str(tmp_path)], stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "ready"
        reader = Store(str(tmp_path))
        assert not reader.writable and "Another Raven writer" in reader.state_status["message"]
        reader.note("7", "must not overwrite")
        reader.record("7", {"t": 1})
        reader.save_ui({"tab": "history"})
        assert not (tmp_path / "tags.json").exists() and not (tmp_path / "ui.json").exists()
        assert not (tmp_path / "series").exists()
        reader.close()
        with pytest.raises(state_ownership.StateOwnershipError, match="Another Raven writer"):
            Store(str(tmp_path), strict_owner=True)
    finally:
        child.kill(); child.wait(5); child.stdout.close()
    recovered = Store(str(tmp_path), strict_owner=True)
    assert recovered.writable
    recovered.close()


def test_stale_foreign_host_metadata_does_not_trigger_pid_signal(tmp_path, monkeypatch):
    (tmp_path / ".writer.lock").write_text('{"host":"another-host","pid":1}')
    monkeypatch.setattr(os, "kill", lambda *args: pytest.fail("must not signal metadata PID"))
    owner = Store(str(tmp_path), strict_owner=True)
    other = Store(str(tmp_path / "profiles" / "lab"), strict_owner=True)
    assert owner.writable and other.writable
    owner.close(); other.close()


@pytest.mark.parametrize("kind", ["nfs", "lustre", "unknown"])
def test_network_state_is_readable_but_mutable_durability_is_refused(tmp_path, monkeypatch, kind):
    (tmp_path / "tags.json").write_text('{"7":{"tags":["keep"],"pinned":false,"note":""}}')
    monkeypatch.setattr(state_ownership, "filesystem_kind", lambda path: kind)
    reader = Store(str(tmp_path))
    assert reader.tags_of("7") == ["keep"] and not reader.writable
    assert kind in reader.state_status["message"]
    assert not (tmp_path / ".writer.lock").exists()
    with pytest.raises(state_ownership.StateOwnershipError, match="local filesystem"):
        Store(str(tmp_path), strict_owner=True)


def test_read_only_store_does_not_create_any_files(tmp_path):
    directory = tmp_path / "absent"
    store = Store(str(directory), read_only=True)
    store.event("inspect", "read-only")
    store.close()
    assert not directory.exists()


def test_unsafe_writer_lock_and_writable_ancestor_are_refused(tmp_path):
    target = tmp_path / "target"; target.write_text("untouched")
    (tmp_path / ".writer.lock").symlink_to(target)
    store = Store(str(tmp_path))
    assert not store.writable and target.read_text() == "untouched"
    (tmp_path / ".writer.lock").unlink()
    folder = tmp_path / "unsafe"; folder.mkdir(mode=0o777); folder.chmod(0o777)
    store = Store(str(folder / "state"))
    assert not store.writable and "writable" in store.state_status["message"]


def test_killed_atomic_publisher_leaves_previous_complete_tags(tmp_path):
    store = Store(str(tmp_path)); store.note("7", "previous"); store.close()
    script = '''from raven.model import Store
from raven import ui_state
import sys,time
s=Store(sys.argv[1],strict_owner=True)
def blocked(*args):
 print('prepared',flush=True)
 time.sleep(60)
ui_state.os.replace=blocked
s.note('7','replacement')
'''
    child = subprocess.Popen([sys.executable, "-c", script, str(tmp_path)], stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "prepared"
    finally:
        child.kill(); child.wait(5); child.stdout.close()
    assert json.loads((tmp_path / "tags.json").read_text())["7"]["note"] == "previous"
    recovered = Store(str(tmp_path), strict_owner=True); recovered.note("7", "recovered"); recovered.close()
    assert json.loads((tmp_path / "tags.json").read_text())["7"]["note"] == "recovered"


def test_locking_failure_is_not_reported_as_another_owner(tmp_path, monkeypatch):
    import errno, fcntl
    def unsupported(*args):
        raise OSError(errno.EOPNOTSUPP, "unsupported locking")
    monkeypatch.setattr(fcntl, "flock", unsupported)
    store = Store(str(tmp_path))
    assert "Cannot acquire" in store.state_status["message"]
    assert "Another Raven writer" not in store.state_status["message"]
    assert not store.writable


def test_session_cleanup_exception_still_releases_mutable_state(tmp_path, monkeypatch):
    from raven.cli import Session
    store = Store(str(tmp_path))
    session = Session(None, store, None, None, None, None, None, None, None)
    monkeypatch.setattr(session, "_close_services", lambda: (_ for _ in ()).throw(RuntimeError("other cleanup failed")))
    with pytest.raises(RuntimeError, match="other cleanup"):
        session.close()
    assert not store.writable
    recovered = Store(str(tmp_path), strict_owner=True); recovered.close()
