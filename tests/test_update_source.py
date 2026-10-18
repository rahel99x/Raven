"""Real temporary Git repositories exercise update ownership and recovery."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

import pytest

from raven import update_source as U


def run(path, *arguments):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    result = subprocess.run(["git", "-c", "user.name=Raven test", "-c", "user.email=raven@example.invalid",
                             "-C", str(path), *arguments], capture_output=True, check=True, env=env)
    return result.stdout.decode().strip()


@pytest.fixture
def checkout(tmp_path):
    remote, seed, source = (tmp_path / name for name in ("remote.git", "seed", "source"))
    remote.mkdir(); seed.mkdir()
    run(remote, "init", "--bare", "--initial-branch=workstation")
    run(seed, "init", "--initial-branch=workstation")
    for name, content in {"app.py": "version = 1\n", "config.py": "APPLICATION = 1\n",
                          "settings.json": '{"user":"original"}\n',
                          ".gitignore": ".venv/\n.raven/\n*.cache\n"}.items():
        (seed / name).write_text(content)
    (seed / "old").mkdir(); (seed / "old/file.txt").write_text("old\n")
    run(seed, "add", "."); run(seed, "commit", "-m", "initial")
    run(seed, "remote", "add", "origin", str(remote)); run(seed, "push", "-u", "origin", "workstation")
    run(tmp_path, "clone", str(remote), str(source))
    return seed, source


def advance(seed, **files):
    for name, content in (files or {"app.py": "version = 2\n"}).items():
        path = seed / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    run(seed, "add", "."); run(seed, "commit", "-m", "upstream update")
    run(seed, "push", "origin", "workstation")
    return run(seed, "rev-parse", "HEAD")


def manifest(result):
    backup = Path(result["backup"])
    raw = (backup / "manifest.json").read_bytes()
    assert json.loads((backup / "COMPLETE.json").read_text())["manifest_sha256"] == hashlib.sha256(raw).hexdigest()
    return backup, json.loads(raw)


def test_plan_is_network_free_and_check_only_fetches_current_tracking_branch(checkout, monkeypatch):
    seed, source = checkout
    original = run(source, "rev-parse", "HEAD")
    target = advance(seed)
    calls = []
    real = U.git
    def tracked(*args, **kwargs):
        calls.append(args[1:]); return real(*args, **kwargs)
    monkeypatch.setattr(U, "git", tracked)
    plan = U.update_source(source, mode="plan")
    assert plan["branch"] == "workstation" and plan["status"] == "planned"
    assert not plan["network_checked"] and not any(c[0] == "fetch" for c in calls)
    (source / "app.py").write_text("local\n")
    check = U.update_source(source, mode="check")
    assert check["target_head"] == target and check["dirty"] and check["behind"] == 1
    assert check["status"] == "update-available" and not check["changed"]
    assert check["backup"] is None and run(source, "rev-parse", "HEAD") == original
    assert (source / "app.py").read_text() == "local\n"
    assert any("+refs/heads/workstation:" in arg for call in calls for arg in call)


def test_clean_fast_forward_and_up_to_date_preserve_backup_and_branch(checkout):
    seed, source = checkout
    original = run(source, "rev-parse", "HEAD")
    target = advance(seed)
    result = U.update_source(source, backup_metadata={"method": "managed", "release": "previous"})
    assert result["status"] == "updated" and result["changed"]
    assert run(source, "rev-parse", "HEAD") == target
    assert run(source, "branch", "--show-current") == "workstation"
    backup, data = manifest(result)
    assert data["original_head"] == original and data["target_head"] == target
    assert data["installation"] == {"method": "managed", "release": "previous"}
    assert (backup / "files/app.py").read_text() == "version = 1\n"
    assert run(source, "rev-parse", data["retained_ref"]) == original
    assert U.update_source(source)["status"] == "up-to-date"


@pytest.mark.parametrize("kind", ["tracked", "untracked", "staged"])
def test_default_refuses_local_source_changes_without_discard(checkout, kind):
    seed, source = checkout
    advance(seed)
    original = run(source, "rev-parse", "HEAD")
    path = source / ("scratch.txt" if kind == "untracked" else "app.py")
    path.write_text("local work\n")
    if kind == "staged":
        run(source, "add", "app.py")
        path.write_text("version = 1\n")
    with pytest.raises(U.SourceUpdateError, match="Local source changes"):
        U.update_source(source)
    assert run(source, "rev-parse", "HEAD") == original
    assert path.exists()


def test_overwrite_backs_up_code_untracked_and_index_preserves_configuration_runtime(checkout, tmp_path):
    seed, source = checkout
    target = advance(seed, **{"app.py": "version = 2\n", "config.py": "APPLICATION = 2\n", "settings.json": '{"upstream":true}\n'})
    (source / "config.py").write_text("LOCAL_APPLICATION_CHANGE = True\n")
    (source / "scratch.txt").write_text("untracked source\n")
    (source / "app.py").write_text("staged local work\n"); run(source, "add", "app.py")
    original_index = (source / ".git/index").read_bytes()
    settings = source / "settings.json"; settings.write_text('{"user":"keep me"}\n')
    external = tmp_path / "personal.json"; external.write_text('{"secret":"unchanged"}\n')
    for folder in (".raven", ".venv"):
        (source / folder).mkdir(); (source / folder / "runtime").write_text("preserved\n")
    (source / "recording.cache").write_text("ignored recording\n")
    shutil.rmtree(source / "old")
    result = U.update_source(source, overwrite=True, protected_paths=[settings, external])
    assert run(source, "rev-parse", "HEAD") == target
    assert (source / "config.py").read_text() == "APPLICATION = 2\n"
    assert settings.read_text() == '{"user":"keep me"}\n'
    assert external.read_text() == '{"secret":"unchanged"}\n'
    assert not (source / "scratch.txt").exists()
    assert (source / "recording.cache").read_text() == "ignored recording\n"
    assert all((source / folder / "runtime").read_text() == "preserved\n" for folder in (".raven", ".venv"))
    backup, data = manifest(result)
    assert (backup / "files/config.py").read_text() == "LOCAL_APPLICATION_CHANGE = True\n"
    assert (backup / "files/scratch.txt").read_text() == "untracked source\n"
    assert (backup / "git-index").read_bytes() == original_index
    assert data["deleted_tracked_paths"] == ["old/file.txt"]
    assert not any(entry["path"].startswith((".venv/", ".raven/")) for entry in data["files"])


def test_default_update_preserves_modified_tracked_config_without_overwrite(checkout):
    seed, source = checkout
    target = advance(seed, **{"settings.json": '{"upstream":true}\n'})
    settings = source / "settings.json"; settings.write_text('{"local":true}\n')
    result = U.update_source(source, protected_paths=[settings])
    assert result["dirty"] and not result["source_dirty"] and run(source, "rev-parse", "HEAD") == target
    assert settings.read_text() == '{"local":true}\n'


def test_diverged_branch_requires_overwrite_and_retains_local_commit(checkout):
    seed, source = checkout
    (source / "local.txt").write_text("local commit\n"); run(source, "add", "."); run(source, "commit", "-m", "local")
    original = run(source, "rev-parse", "HEAD")
    target = advance(seed)
    check = U.update_source(source, mode="check")
    assert check["divergent"] and check["ahead"] == check["behind"] == 1
    with pytest.raises(U.SourceUpdateError, match="Local source"):
        U.update_source(source)
    result = U.update_source(source, overwrite=True)
    assert run(source, "rev-parse", "HEAD") == target
    assert run(source, "rev-parse", result["preserved_commit_ref"]) == original


def test_detached_or_ambiguous_tracking_refused(checkout):
    _, source = checkout
    run(source, "checkout", "--detach")
    with pytest.raises(U.SourceUpdateError):
        U.update_source(source, mode="plan")
    run(source, "checkout", "workstation")
    run(source, "config", "--add", "branch.workstation.merge", "refs/heads/another")
    with pytest.raises(U.SourceUpdateError, match="unambiguous"):
        U.update_source(source)


def test_network_failure_never_leaks_remote_credentials_or_changes_source(checkout):
    _, source = checkout
    original = run(source, "rev-parse", "HEAD")
    run(source, "remote", "set-url", "origin", "https://secret-token:password@127.0.0.1:1/no-repository")
    with pytest.raises(U.SourceUpdateError) as caught:
        U.update_source(source)
    assert "secret-token" not in str(caught.value) and "password" not in str(caught.value)
    assert run(source, "rev-parse", "HEAD") == original


@pytest.mark.parametrize("kind", ["source-link", "git-link", "writable-parent", "config-link", "config-fifo"])
def test_unsafe_source_or_configuration_refused(checkout, tmp_path, kind):
    _, source = checkout
    protected = []
    if kind == "source-link":
        link = tmp_path / "source-link"; link.symlink_to(source, target_is_directory=True); source = link
    elif kind == "git-link":
        (source / ".git").rename(source / "real-git"); (source / ".git").symlink_to("real-git", target_is_directory=True)
    elif kind == "writable-parent":
        tmp_path.chmod(0o777)
    elif kind == "config-link":
        path = tmp_path / "config"; path.symlink_to(source / "settings.json"); protected = [path]
    else:
        path = tmp_path / "fifo"; os.mkfifo(path); protected = [path]
    with pytest.raises((OSError, ValueError)):
        U.update_source(source, protected_paths=protected)


def test_environment_git_overrides_cannot_redirect_source(checkout, tmp_path, monkeypatch):
    _, source = checkout
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "wrong"))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path / "wrong-tree"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.worktree")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(tmp_path / "wrong-tree"))
    assert U.update_source(source, mode="plan")["source"] == str(source)


def test_backup_failure_and_bound_precede_destructive_git(checkout, monkeypatch):
    seed, source = checkout
    target = advance(seed)
    original = run(source, "rev-parse", "HEAD")
    with pytest.raises(U.SourceUpdateError, match="outside"):
        U.update_source(source, overwrite=True, backup_root=source / "backup")
    monkeypatch.setattr(U, "MAX_BACKUP_BYTES", 1)
    with pytest.raises(U.SourceUpdateError, match="budget"):
        U.update_source(source, overwrite=True)
    assert run(source, "rev-parse", "HEAD") == original != target


def test_concurrent_file_change_after_backup_prevents_mutation(checkout, monkeypatch):
    seed, source = checkout
    advance(seed)
    original = run(source, "rev-parse", "HEAD")
    create = U.create_backup
    def racing(*args, **kwargs):
        result = create(*args, **kwargs)
        (source / "app.py").write_text("concurrent editor\n")
        return result
    monkeypatch.setattr(U, "create_backup", racing)
    with pytest.raises(U.SourceUpdateError, match="concurrently"):
        U.update_source(source, overwrite=True)
    assert run(source, "rev-parse", "HEAD") == original
    assert (source / "app.py").read_text() == "concurrent editor\n"


@pytest.mark.parametrize("failure", [U.SourceUpdateError("injected failure"), KeyboardInterrupt()])
def test_configuration_restored_even_when_mutation_is_interrupted(checkout, monkeypatch, failure):
    seed, source = checkout
    advance(seed, **{"settings.json": '{"upstream":true}\n'})
    settings = source / "settings.json"; settings.write_text('{"keep":true}\n')
    real = U.git
    def broken(path, *args, **kwargs):
        if args[0] == "clean":
            raise failure
        return real(path, *args, **kwargs)
    monkeypatch.setattr(U, "git", broken)
    with pytest.raises(U.SourceUpdateError) as caught:
        U.update_source(source, overwrite=True, protected_paths=[settings])
    assert settings.read_text() == '{"keep":true}\n'
    assert caught.value.backup and Path(caught.value.backup, "COMPLETE.json").exists()


def test_target_cannot_overwrite_ignored_recordings_or_redirect_config(checkout):
    seed, source = checkout
    (source / "capture.cache").write_text("existing recording\n")
    (seed / "capture.cache").write_text("new tracked content\n")
    run(seed, "add", "-f", "capture.cache"); run(seed, "commit", "-m", "track ignored path"); run(seed, "push")
    original = run(source, "rev-parse", "HEAD")
    with pytest.raises(U.SourceUpdateError, match="ignored local data"):
        U.update_source(source, overwrite=True)
    assert (source / "capture.cache").read_text() == "existing recording\n"
    assert run(source, "rev-parse", "HEAD") == original


def test_source_symlink_is_backed_up_without_reading_its_target(checkout, tmp_path):
    seed, source = checkout
    advance(seed)
    secret = tmp_path / "outside"; secret.write_text("never read into source backup\n")
    (source / "local-link").symlink_to(secret)
    result = U.update_source(source, overwrite=True)
    backup, data = manifest(result)
    entry = next(item for item in data["files"] if item["path"] == "local-link")
    assert entry["kind"] == "symlink" and (backup / entry["storage"]).read_text() == str(secret)
    assert secret.read_text() == "never read into source backup\n"
    assert not (source / "local-link").exists()


def test_target_symlink_cannot_redirect_protected_configuration(checkout):
    seed, source = checkout
    (seed / "settings.json").unlink(); (seed / "settings.json").symlink_to("app.py")
    run(seed, "add", "."); run(seed, "commit", "-m", "symlink configuration"); run(seed, "push")
    original = run(source, "rev-parse", "HEAD")
    with pytest.raises(U.SourceUpdateError, match="redirect"):
        U.update_source(source, overwrite=True, protected_paths=[source / "settings.json"])
    assert run(source, "rev-parse", "HEAD") == original
    assert not (source / "settings.json").is_symlink()


def test_absent_protected_configuration_stays_absent_after_update(checkout):
    seed, source = checkout
    advance(seed, **{"settings.json": '{"upstream":true}\n'})
    settings = source / "settings.json"; settings.unlink()
    result = U.update_source(source, protected_paths=[settings])
    assert result["status"] == "updated" and not settings.exists()
    _, data = manifest(result)
    assert "settings.json" in data["missing_paths"]


def test_real_sigterm_restores_configuration_and_reaps_git_children(checkout, tmp_path):
    seed, source = checkout
    advance(seed, **{"settings.json": '{"upstream":true}\n'})
    settings = source / "settings.json"; settings.write_text('{"keep":true}\n')
    marker = tmp_path / "reset-finished"
    program = r'''
import json,signal,sys
from pathlib import Path
from raven import update_source as U
real=U.git
def pause(source,*args,**kwargs):
    if args[0]=='clean':
        Path(sys.argv[2]).write_text('ready')
        signal.pause()
    return real(source,*args,**kwargs)
U.git=pause
try:
    U.update_source(sys.argv[1],overwrite=True,protected_paths=[str(Path(sys.argv[1])/'settings.json')])
except U.SourceUpdateError as exc:
    print(json.dumps({'backup':exc.backup}),flush=True)
    sys.exit(7)
'''
    process = subprocess.Popen([sys.executable, "-c", program, str(source), str(marker)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 10
        while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(.01)
        assert marker.exists()
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 7, stderr
        assert Path(json.loads(stdout)["backup"], "COMPLETE.json").exists()
    finally:
        if process.poll() is None:
            process.kill(); process.wait(timeout=5)
    assert settings.read_text() == '{"keep":true}\n'


def test_git_output_and_time_bounds_are_enforced(checkout, tmp_path, monkeypatch):
    _, source = checkout
    monkeypatch.setattr(U, "MAX_OUTPUT", 4)
    with pytest.raises(U.SourceUpdateError, match="output budget"):
        U.git(source, "ls-files")
    binary = tmp_path / "bin"; binary.mkdir()
    fake = binary / "git"; fake.write_text("#!/bin/sh\nexec /bin/sleep 30\n"); fake.chmod(0o700)
    monkeypatch.setenv("PATH", str(binary))
    monkeypatch.setattr(U, "COMMAND_TIMEOUT", .03)
    children = []; original = U.subprocess.Popen
    def tracked(*args, **kwargs):
        child = original(*args, **kwargs); children.append(child); return child
    monkeypatch.setattr(U.subprocess, "Popen", tracked)
    with pytest.raises(U.SourceUpdateError, match="timed out"):
        U.git(source, "status")
    assert len(children) == 1 and children[0].poll() is not None


def test_normal_global_git_network_configuration_and_includes_remain_supported(checkout, tmp_path, monkeypatch):
    seed, source = checkout
    target = advance(seed)
    remote = run(source, "remote", "get-url", "origin")
    run(source, "remote", "set-url", "origin", "raven-network-fixture:origin")
    included = tmp_path / "network.gitconfig"
    included.write_text('[url "' + Path(remote).as_uri() + '"]\n\tinsteadOf = raven-network-fixture:origin\n')
    global_config = tmp_path / "global.gitconfig"
    global_config.write_text('[include]\n\tpath = ' + str(included) + '\n')
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    result = U.update_source(source)
    assert result["target_head"] == target and result["status"] == "updated"


def test_injected_git_network_configuration_survives_redirect_filter(checkout, monkeypatch):
    seed, source = checkout
    target = advance(seed)
    remote = run(source, "remote", "get-url", "origin")
    run(source, "remote", "set-url", "origin", "raven-injected-fixture:origin")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "3")
    for index, (key, value) in enumerate([
        ("core.worktree", "/does-not-belong-to-this-checkout"),
        ("url." + Path(remote).as_uri() + ".insteadOf", "raven-injected-fixture:origin"),
        ("http.extraHeader", "Authorization: fixture-only-secret")]):
        monkeypatch.setenv("GIT_CONFIG_KEY_" + str(index), key)
        monkeypatch.setenv("GIT_CONFIG_VALUE_" + str(index), value)
    env = U._git_environment()
    assert env["GIT_CONFIG_COUNT"] == "2"
    assert env["GIT_CONFIG_VALUE_1"] == "Authorization: fixture-only-secret"
    assert U.update_source(source)["target_head"] == target


def test_legacy_injected_parameters_and_authentication_environment_are_retained(checkout, monkeypatch):
    _, source = checkout
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'core.worktree=/wrong' 'raven.example=value with spaces'")
    monkeypatch.setenv("GIT_ASKPASS", "/user/configured/askpass")
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -F /user/configured/ssh-config")
    monkeypatch.setenv("GIT_SSL_CAINFO", "/user/configured/ca.pem")
    env = U._git_environment()
    assert env["GIT_ASKPASS"] == "/user/configured/askpass"
    assert env["GIT_SSH_COMMAND"] == "ssh -F /user/configured/ssh-config"
    assert env["GIT_SSL_CAINFO"] == "/user/configured/ca.pem"
    assert U.git(source, "config", "--get", "raven.example").strip() == b"value with spaces"
    assert U.update_source(source, mode="plan")["source"] == str(source)


def test_target_regular_file_cannot_replace_configuration_directory(checkout):
    seed, source = checkout
    directory = source / "personal"; directory.mkdir()
    (directory / "settings.json").write_text('{"keep":true}\n')
    advance(seed, personal="upstream source file\n")
    original = run(source, "rev-parse", "HEAD")
    with pytest.raises(U.SourceUpdateError, match="configuration directory"):
        U.update_source(source, overwrite=True, protected_paths=[directory])
    assert run(source, "rev-parse", "HEAD") == original
    assert (directory / "settings.json").read_text() == '{"keep":true}\n'


def test_content_fingerprint_detects_source_edits_without_network_or_writes(checkout, monkeypatch):
    _, source = checkout
    calls = []; original = U.git
    def tracked(path, *args, **kwargs):
        calls.append(args[0]); return original(path, *args, **kwargs)
    monkeypatch.setattr(U, "git", tracked)
    before = U.content_fingerprint(source)
    os.utime(source / ".git/index", None)
    assert U.content_fingerprint(source) == before
    (source / "app.py").write_text("a different build input\n")
    assert U.content_fingerprint(source) != before
    assert not set(calls) & {"fetch", "reset", "clean", "update-ref"}
    assert not (source.parent / ".raven-update-backups").exists()


def test_exited_git_leader_cannot_strand_helper_holding_stdout(checkout, tmp_path, monkeypatch):
    _, source = checkout
    binary = tmp_path / "bin"; binary.mkdir()
    helper_pid = tmp_path / "helper.pid"
    fake = binary / "git"
    fake.write_text("#!/bin/sh\n/bin/sh -c 'echo $$ > \"$1\"; exec /bin/sleep 30' helper \"$RAVEN_TEST_HELPER_PID\" &\nexit 0\n")
    fake.chmod(0o700)
    monkeypatch.setenv("PATH", str(binary))
    monkeypatch.setenv("RAVEN_TEST_HELPER_PID", str(helper_pid))
    monkeypatch.setattr(U, "COMMAND_TIMEOUT", .1)
    sentinel = subprocess.Popen(["/bin/sleep", "30"], start_new_session=True)
    def alive(pid):
        try:
            return Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1].split()[0] != "Z"
        except (FileNotFoundError, ProcessLookupError):
            # Exit can remove the entry before open or invalidate it during read.
            return False
    child = None
    try:
        with pytest.raises(U.SourceUpdateError, match="timed out"):
            U.git(source, "status")
        assert helper_pid.exists()
        child = int(helper_pid.read_text())
        deadline = time.monotonic() + 2
        while alive(child) and time.monotonic() < deadline:
            time.sleep(.01)
        assert not alive(child), "Git helper survived its bounded command"
        assert sentinel.poll() is None, "Cleanup signalled an unrelated process"
    finally:
        if child is not None and alive(child):
            os.kill(child, signal.SIGKILL)
        sentinel.kill(); sentinel.wait(timeout=2)
