"""Real file failures preserve a readable previous UI preference publication."""
import json
import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from tower import ui_state
from tower.model import Store


def test_legacy_roundtrip_metadata_and_last_good_recovery(tmp_path):
    store = Store(str(tmp_path))
    path = tmp_path / "ui.json"
    path.write_text('{"theme":"darcula","gpu":true}')
    assert store.load_ui() == {"theme": "darcula", "gpu": True}
    assert store.save_ui({"theme": "gruvbox-dark", "name": "界"})
    raw = json.loads(path.read_text())
    assert raw["theme"] == "gruvbox-dark"  # Older readers still work.
    assert raw[ui_state.META]["version"] == 1
    assert store.load_ui() == {"theme": "gruvbox-dark", "name": "界"}
    path.write_text('{"theme":')
    assert store.load_ui() == {"theme": "darcula", "gpu": True}
    assert store.ui_state_status["state"] == "recovered"
    assert store.save_ui({"theme": "default"})
    assert store.load_ui() == {"theme": "default"}
    assert json.loads((tmp_path / "ui.json.bak").read_text())["theme"] == "darcula"


def test_detects_checksum_corruption_and_recovers_without_writing(tmp_path):
    store = Store(str(tmp_path))
    store.save_ui({"gpu": False})
    store.save_ui({"gpu": True})
    path = tmp_path / "ui.json"
    value = json.loads(path.read_text())
    value["gpu"] = False
    path.write_text(json.dumps(value))
    before = path.read_bytes()
    assert store.load_ui() == {"gpu": False}
    assert store.ui_state_status["state"] == "recovered"
    assert "checksum" in store.ui_state_status["message"]
    assert path.read_bytes() == before


@pytest.mark.parametrize("metadata", [None, False, [], {}, {"version": True}, {"version": 1}])
def test_invalid_explicit_metadata_does_not_downgrade_to_legacy(tmp_path, metadata):
    store = Store(str(tmp_path))
    assert store.save_ui({"theme": "default"})
    assert store.save_ui({"theme": "darcula"})
    path = tmp_path / "ui.json"
    path.write_text(json.dumps({"theme": "unverified", ui_state.META: metadata}))
    assert store.load_ui() == {"theme": "default"}
    assert store.ui_state_status["state"] == "recovered"


def test_newer_version_is_preserved_and_not_masked_by_backup(tmp_path):
    store = Store(str(tmp_path))
    store.save_ui({"theme": "default"})
    store.save_ui({"theme": "dark"})
    path = tmp_path / "ui.json"
    raw = json.dumps({"theme": "future", ui_state.META: {"version": 200}}).encode()
    path.write_bytes(raw)
    assert store.load_ui() == {} and store.ui_state_status["state"] == "newer"
    assert not store.save_ui({"theme": "old"})
    assert path.read_bytes() == raw


@pytest.mark.parametrize("bad", [None, [], True, {"x": float("nan")}, {ui_state.META: {}}])
def test_invalid_updates_preserve_last_good_document(tmp_path, bad):
    store = Store(str(tmp_path))
    store.save_ui({"tab": "history"})
    before = (tmp_path / "ui.json").read_bytes()
    assert not store.save_ui(bad)
    assert store.ui_state_status["state"] == "error"
    assert (tmp_path / "ui.json").read_bytes() == before
    assert store.load_ui() == {"tab": "history"}


def test_failed_primary_replace_keeps_previous_publication_and_cleans_temporary(tmp_path, monkeypatch):
    store = Store(str(tmp_path))
    assert store.save_ui({"tab": "history"})
    replace = os.replace
    def fail_new(source, target):
        if str(target).endswith("ui.json"):
            raise OSError("disk failure")
        return replace(source, target)
    monkeypatch.setattr(ui_state.os, "replace", fail_new)
    assert not store.save_ui({"tab": "jobs"})
    assert store.ui_state_status["state"] == "error"
    assert store.load_ui() == {"tab": "history"}
    assert not list(tmp_path.glob(".tower-ui-*"))


def test_failed_fsync_does_not_publish_partial_file(tmp_path, monkeypatch):
    store = Store(str(tmp_path))
    assert store.save_ui({"tab": "history"})
    def failed(_):
        raise OSError("storage failure")
    monkeypatch.setattr(ui_state.os, "fsync", failed)
    assert not store.save_ui({"tab": "jobs"})
    assert store.load_ui() == {"tab": "history"}
    assert not list(tmp_path.glob(".tower-ui-*"))


def test_bounded_state_and_nonregular_files_do_not_block(tmp_path, monkeypatch):
    monkeypatch.setattr(ui_state, "MAX_BYTES", 128)
    path = tmp_path / "ui.json"
    path.write_bytes(b" " * 129)
    value, status = ui_state.read(str(path))
    assert value == {} and status["state"] == "unavailable"
    path.unlink()
    os.mkfifo(path)
    assert ui_state.read(str(path))[1]["state"] == "unavailable"
    path.unlink()
    target = tmp_path / "target"
    target.write_text('{"private":"data"}')
    path.symlink_to(target)
    assert ui_state.read(str(path))[0] == {}
    assert ui_state.write(str(path), {"tab": "jobs"})["state"] == "ready"
    assert target.read_text() == '{"private":"data"}'


def test_concurrent_readers_never_observe_partial_json(tmp_path):
    store = Store(str(tmp_path))
    assert store.save_ui({"counter": 0})
    def write():
        for index in range(30):
            assert store.save_ui({"counter": index, "text": "界" * 1000})
    def read():
        for _ in range(100):
            value, status = ui_state.read(str(tmp_path / "ui.json"))
            assert isinstance(value["counter"], int)
            assert status["state"] == "ready"
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(write), pool.submit(read), pool.submit(read)]
        for future in futures:
            future.result()


def test_no_state_is_read_and_write_free(tmp_path):
    path = tmp_path / "absent"
    store = Store(str(path), persist=False)
    assert store.load_ui() == {}
    store.save_ui({"tab": "history"})
    assert not path.exists()
