"""Real local Git recovery; no network services or installed-release mutation."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from raven import update_revert as R, update_source as S


def git(source, *args):
    return subprocess.check_output(["git", "-C", str(source), *args], stderr=subprocess.DEVNULL).decode().strip()


@pytest.fixture
def repository(tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    source = tmp_path / "source"
    subprocess.run(["git", "clone", str(remote), str(source)], check=True, capture_output=True)
    git(source, "checkout", "-b", "main")
    git(source, "config", "user.email", "fixture@example.invalid")
    git(source, "config", "user.name", "Recovery fixture")
    for name, value in {"code.py": "old code\n", "staged.py": "old staged\n", "deleted.py": "old deleted\n", "config.json": '{"user":"old"}\n', ".gitignore": ".venv/\n.raven/\nignored/\n"}.items():
        (source / name).write_text(value)
    git(source, "add", ".")
    git(source, "commit", "-m", "original")
    git(source, "push", "-u", "origin", "main")
    old = git(source, "rev-parse", "HEAD")
    (source / "code.py").write_text("new code\n")
    (source / "added.py").write_text("new tracked\n")
    git(source, "add", ".")
    git(source, "commit", "-m", "update")
    git(source, "push", "origin", "main")
    new = git(source, "rev-parse", "HEAD")
    git(source, "reset", "--hard", old)
    (source / "code.py").write_text("original local code\n")
    (source / "staged.py").write_text("original staged change\n")
    git(source, "add", "staged.py")
    (source / "staged.py").write_text("original unstaged over staged\n")
    (source / "deleted.py").unlink()
    (source / "notes.txt").write_text("original untracked\n")
    for name in (".venv", ".raven", "ignored"):
        (source / name).mkdir()
        (source / name / "retained").write_text("runtime before\n")
    return source, old, new


def update(repository, tmp_path):
    source, old, new = repository
    outcome = S.update_source(source, overwrite=True, protected_paths=(source / "config.json",),
        backup_root=tmp_path / "backups", backup_metadata={"kind": "managed", "release": "old-release"})
    assert git(source, "rev-parse", "HEAD") == new
    assert outcome["backup"]
    (source / "config.json").write_text('{"user":"current"}\n')
    for name in (".venv", ".raven", "ignored"):
        (source / name / "retained").write_text("runtime current\n")
    return Path(outcome["backup"])


def mutate_manifest(backup, mutation):
    manifest = json.loads((backup / "manifest.json").read_text())
    mutation(manifest)
    raw = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()
    (backup / "manifest.json").write_bytes(raw)
    complete = {"schema": "raven.source-backup-complete/v1", "manifest_sha256": hashlib.sha256(raw).hexdigest()}
    (backup / "COMPLETE.json").write_text(json.dumps(complete))


def snapshot(source):
    return {"head": git(source, "rev-parse", "HEAD"), "index": (source / ".git/index").read_bytes(),
            "files": {str(path.relative_to(source)): path.read_bytes() for path in source.rglob("*") if path.is_file() and ".git" not in path.parts}}


def assert_preserved(source):
    assert (source / "config.json").read_text() == '{"user":"current"}\n'
    for name in (".venv", ".raven", "ignored"):
        assert (source / name / "retained").read_text() == "runtime current\n"


def test_update_then_revert_twice_restores_contents_index_and_keeps_current_settings(repository, tmp_path):
    source, old, new = repository
    original_index = (source / ".git/index").read_bytes()
    backup = update(repository, tmp_path)
    before = snapshot(source)
    count = len(list((tmp_path / "backups").iterdir()))
    plan = R.revert_source(backup, mode="plan")
    assert plan["status"] == "planned" and plan["recovery_backup"] is None
    assert snapshot(source) == before
    assert len(list((tmp_path / "backups").iterdir())) == count
    restored = R.revert_source(backup, backup_root=tmp_path / "backups", backup_metadata={"kind": "managed", "release": "new-release"})
    assert restored["status"] == "reverted" and restored["installation"]["release"] == "old-release"
    assert git(source, "rev-parse", "HEAD") == old
    assert (source / "code.py").read_text() == "original local code\n"
    assert (source / "staged.py").read_text() == "original unstaged over staged\n"
    assert (source / "notes.txt").read_text() == "original untracked\n"
    assert not (source / "deleted.py").exists() and not (source / "added.py").exists()
    assert (source / ".git/index").read_bytes() == original_index
    assert_preserved(source)
    second = R.revert_source(restored["recovery_backup"], backup_root=tmp_path / "backups")
    assert git(source, "rev-parse", "HEAD") == new
    assert (source / "code.py").read_text() == "new code\n"
    assert (source / "added.py").read_text() == "new tracked\n"
    assert not (source / "notes.txt").exists()
    assert second["installation"]["release"] == "new-release"
    assert_preserved(source)
    assert git(source, "show-ref", "--verify", json.loads((backup / "manifest.json").read_text())["retained_ref"])


@pytest.mark.parametrize("damage", ["missing-complete", "digest", "payload", "manifest-link", "payload-link", "hardlink", "escape", "storage", "duplicate", "ancestor", "head", "branch", "source", "index", "index-hash", "unknown-kind", "count", "deleted-overlap", "incomplete"])
def test_tampered_or_unsafe_backup_refused_without_source_mutation(repository, tmp_path, damage):
    source, _, _ = repository
    backup = update(repository, tmp_path)
    manifest = json.loads((backup / "manifest.json").read_text())
    item = next(entry for entry in manifest["files"] if entry["path"] == "code.py")
    payload = backup / item["storage"]
    if damage == "missing-complete":
        (backup / "COMPLETE.json").unlink()
    elif damage == "digest":
        (backup / "manifest.json").write_text("{}")
    elif damage == "payload":
        payload.write_bytes(b"tampered")
    elif damage in ("manifest-link", "payload-link"):
        target = backup / "manifest.json" if damage == "manifest-link" else payload
        copy = tmp_path / "redirected"
        copy.write_bytes(target.read_bytes())
        target.unlink(); target.symlink_to(copy)
    elif damage == "hardlink":
        os.link(payload, tmp_path / "linked")
    elif damage == "index-hash":
        (backup / "git-index").write_bytes(b"bad-index")
    else:
        def alter(value):
            entry = next(entry for entry in value["files"] if entry["path"] == "code.py")
            if damage == "escape": entry["path"] = "../outside"
            elif damage == "storage": entry["storage"] = "files/staged.py"
            elif damage == "duplicate": value["files"].append(dict(entry))
            elif damage == "ancestor":
                nested = dict(entry, path="code.py/nested", storage="files/code.py/nested")
                value["files"].append(nested)
            elif damage == "head": value["original_head"] = "0" * 40
            elif damage == "branch": value["branch"] = "other"
            elif damage == "source": value["source"] = str(tmp_path)
            elif damage == "index":
                data = b"bad index with matching hash"
                (backup / "git-index").write_bytes(data)
                value["index"].update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
            elif damage == "unknown-kind": entry["kind"] = "device"
            elif damage == "count": entry["bytes"] = R.MAX_FILE_BYTES + 1
            elif damage == "deleted-overlap": value["deleted_tracked_paths"].append("code.py")
            elif damage == "incomplete": value["files"].remove(entry)
        mutate_manifest(backup, alter)
    before = snapshot(source)
    with pytest.raises((ValueError, OSError)):
        R.revert_source(backup, backup_root=tmp_path / "backups")
    assert snapshot(source) == before
    assert_preserved(source)


def test_backup_directory_symlink_and_wrong_override_refused(repository, tmp_path):
    source, _, _ = repository
    backup = update(repository, tmp_path)
    link = tmp_path / "backup-link"; link.symlink_to(backup, target_is_directory=True)
    for chosen, override in ((link, None), (backup, tmp_path / "other")):
        with pytest.raises(ValueError):
            R.revert_source(chosen, mode="plan", source_override=override)
    assert_preserved(source)


def test_safety_backup_failure_does_not_change_source(repository, tmp_path, monkeypatch):
    source, _, _ = repository
    backup = update(repository, tmp_path)
    before = snapshot(source)
    def fail(*args, **kwargs):
        raise OSError("no space for recovery snapshot")
    monkeypatch.setattr(S, "create_backup", fail)
    with pytest.raises(OSError, match="no space"):
        R.revert_source(backup)
    assert snapshot(source) == before


def test_interrupt_after_ref_move_keeps_recoverable_safety_backup(repository, tmp_path, monkeypatch):
    source, _, new = repository
    backup = update(repository, tmp_path)
    real_git = S.git
    def interrupt(checkout, *args, **kwargs):
        if args[:2] == ("update-ref", "refs/heads/main"):
            real_git(checkout, *args, **kwargs)
            raise KeyboardInterrupt()
        return real_git(checkout, *args, **kwargs)
    monkeypatch.setattr(S, "git", interrupt)
    with pytest.raises(KeyboardInterrupt) as caught:
        R.revert_source(backup, backup_root=tmp_path / "backups")
    safety = caught.value.backup
    assert Path(safety, "COMPLETE.json").exists()
    assert_preserved(source)
    monkeypatch.setattr(S, "git", real_git)
    R.revert_source(safety, backup_root=tmp_path / "backups")
    assert git(source, "rev-parse", "HEAD") == new
    assert (source / "code.py").read_text() == "new code\n"
    assert_preserved(source)


def test_current_file_hardlink_refused_before_backup(repository, tmp_path):
    source, _, _ = repository
    backup = update(repository, tmp_path)
    os.link(source / "code.py", tmp_path / "outside-code")
    count = len(list((tmp_path / "backups").iterdir()))
    with pytest.raises(R.RevertError, match="hardlinks"):
        R.revert_source(backup, backup_root=tmp_path / "backups")
    assert len(list((tmp_path / "backups").iterdir())) == count


def test_explicit_current_configuration_added_after_update_is_preserved(repository, tmp_path):
    source, _, _ = repository
    backup = update(repository, tmp_path)
    new_config = source / "personal.json"; new_config.write_text("current configuration")
    R.revert_source(backup, protected_paths=(new_config,), backup_root=tmp_path / "backups")
    assert new_config.read_text() == "current configuration"
    assert_preserved(source)


@pytest.mark.parametrize("original_directory", [False, True])
def test_inventoried_file_directory_transitions_restore_without_recursive_delete(repository, tmp_path, original_directory):
    source, _, _ = repository
    update(repository, tmp_path)
    target = source / "transition"
    if original_directory:
        target.mkdir(); (target / "child").write_text("original child")
    else:
        target.write_text("original file")
    backup = S.create_backup(source, backup_root=tmp_path / "backups", protected_paths=(source / "config.json",))
    if original_directory:
        (target / "child").unlink(); target.rmdir(); target.write_text("current file")
    else:
        target.unlink(); target.mkdir(); (target / "child").write_text("current child")
    R.revert_source(backup["path"], backup_root=tmp_path / "backups")
    assert ((target / "child").read_text() if original_directory else target.read_text()) == ("original child" if original_directory else "original file")
    assert_preserved(source)


def test_source_symlink_leaf_restored_without_following_target(repository, tmp_path):
    source, _, _ = repository
    update(repository, tmp_path)
    outside = tmp_path / "external"; outside.write_text("never modified")
    link = source / "source-link"; link.symlink_to(outside)
    backup = S.create_backup(source, backup_root=tmp_path / "backups", protected_paths=(source / "config.json",))
    link.unlink(); link.write_text("current regular file")
    R.revert_source(backup["path"], backup_root=tmp_path / "backups")
    assert link.is_symlink() and os.readlink(link) == str(outside)
    assert outside.read_text() == "never modified"


def test_unsafe_backup_permissions_refused(repository, tmp_path):
    source, _, _ = repository
    backup = update(repository, tmp_path)
    before = snapshot(source)
    backup.chmod(0o755)
    with pytest.raises(ValueError):
        R.revert_source(backup, mode="plan")
    assert snapshot(source) == before


def test_duplicate_json_field_refused_even_with_matching_completion_digest(repository, tmp_path):
    source, _, _ = repository
    backup = update(repository, tmp_path)
    original = (backup / "manifest.json").read_bytes()
    raw = b'{"schema":"raven.source-backup/v1",' + original.lstrip()[1:]
    (backup / "manifest.json").write_bytes(raw)
    (backup / "COMPLETE.json").write_text(json.dumps({"schema":"raven.source-backup-complete/v1","manifest_sha256":hashlib.sha256(raw).hexdigest()}))
    before = snapshot(source)
    with pytest.raises(R.RevertError, match="JSON"):
        R.revert_source(backup)
    assert snapshot(source) == before


def test_post_backup_write_failure_reports_backup_and_can_be_recovered(repository, tmp_path, monkeypatch):
    source, _, new = repository
    backup = update(repository, tmp_path)
    write = R._write
    def fail(*args, **kwargs):
        raise OSError("injected write failure")
    monkeypatch.setattr(R, "_write", fail)
    with pytest.raises(R.RevertError) as caught:
        R.revert_source(backup, backup_root=tmp_path / "backups")
    assert caught.value.backup and Path(caught.value.backup, "COMPLETE.json").exists()
    assert_preserved(source)
    monkeypatch.setattr(R, "_write", write)
    R.revert_source(caught.value.backup, backup_root=tmp_path / "backups")
    assert git(source, "rev-parse", "HEAD") == new
    assert (source / "code.py").read_text() == "new code\n"
    assert_preserved(source)


def test_git_writer_is_excluded_during_source_restore(repository, tmp_path, monkeypatch):
    source, _, _ = repository
    backup = update(repository, tmp_path)
    write = R._write
    attempts = []
    def competing_git(*args, **kwargs):
        assert (source / ".git/index.lock").is_file()
        outcome = subprocess.run(["git", "-C", str(source), "add", "--", "code.py"], capture_output=True)
        attempts.append(outcome.returncode)
        return write(*args, **kwargs)
    monkeypatch.setattr(R, "_write", competing_git)
    R.revert_source(backup, backup_root=tmp_path / "backups")
    assert attempts and all(code != 0 for code in attempts)
    assert not (source / ".git/index.lock").exists()
    assert_preserved(source)


def test_git_index_change_after_snapshot_is_not_overwritten(repository, tmp_path, monkeypatch):
    source, _, new = repository
    backup = update(repository, tmp_path)
    (source / "concurrent.txt").write_text("other writer's file")
    unchanged = S.assert_unchanged
    calls = []
    concurrent_index = []
    def change_after_last_check(*args, **kwargs):
        unchanged(*args, **kwargs)
        calls.append(True)
        if len(calls) == 2:
            git(source, "add", "--", "concurrent.txt")
            concurrent_index.append((source / ".git/index").read_bytes())
    monkeypatch.setattr(S, "assert_unchanged", change_after_last_check)
    with pytest.raises(R.RevertError, match="index changed") as caught:
        R.revert_source(backup, backup_root=tmp_path / "backups")
    assert caught.value.backup
    assert concurrent_index == [(source / ".git/index").read_bytes()]
    assert git(source, "rev-parse", "HEAD") == new
    assert (source / "code.py").read_text() == "new code\n"
    assert not (source / ".git/index.lock").exists()


def test_actual_sigterm_during_restore_retains_backup_and_releases_index_lock(repository, tmp_path):
    source, _, new = repository
    backup = update(repository, tmp_path)
    code = '''
import json, os, signal, sys
from raven import update_revert as R
def terminate(*args, **kwargs):
    os.kill(os.getpid(), signal.SIGTERM)
R._write = terminate
try:
    R.revert_source(sys.argv[1], backup_root=sys.argv[2])
except R.RevertError as exc:
    print(json.dumps({"backup": exc.backup, "error": str(exc)}))
    raise SystemExit(37)
raise SystemExit(99)
'''
    result = subprocess.run([sys.executable, "-c", code, str(backup), str(tmp_path / "backups")], text=True, capture_output=True, timeout=15)
    assert result.returncode == 37, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["backup"] and "termination" in receipt["error"]
    assert not (source / ".git/index.lock").exists()
    assert_preserved(source)
    R.revert_source(receipt["backup"], backup_root=tmp_path / "backups")
    assert git(source, "rev-parse", "HEAD") == new
    assert (source / "code.py").read_text() == "new code\n"
