"""Exercise the downloaded Bash entry point without network or installation."""
from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import pty
import selectors
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "install.sh").read_text()
PYTHON = str(Path(sys.executable).absolute())
BASH = os.environ.get('RAVEN_TEST_BASH') or shutil.which("bash")
INSTALLER = """import json, os, sys
from pathlib import Path
Path(os.environ['BOOTSTRAP_CAPTURE']).write_text(json.dumps({
    'args': sys.argv[1:], 'source': str(Path(__file__).resolve().parents[1]),
    'stdin_tty': sys.stdin.isatty(), 'stdout_tty': sys.stdout.isatty(),
}))
raise SystemExit(int(os.environ.get('INSTALL_EXIT', '0')))
"""
GIT = """import json, os, shlex, shutil, signal, subprocess, sys, time
from pathlib import Path
args = sys.argv[1:]
if os.environ.get('VERIFY_AUTH_ENV'):
    settings = {os.environ['GIT_CONFIG_KEY_' + str(i)]: os.environ['GIT_CONFIG_VALUE_' + str(i)]
                for i in range(int(os.environ['GIT_CONFIG_COUNT']))}
    assert settings == {'http.extraHeader': 'Authorization: fixture-secret', 'http.proxy': 'https://fixture-proxy.invalid'}
    assert shlex.split(os.environ['GIT_CONFIG_PARAMETERS']) == ["credential.helper=fixture with spaces", "http.extraHeader=fixture's second secret"]
    for key in ('GIT_CONFIG_GLOBAL', 'GIT_CONFIG_SYSTEM', 'GIT_SSL_CAINFO', 'GIT_SSH_COMMAND'):
        assert os.environ[key] == os.environ['EXPECTED_' + key]
    assert 'GIT_WORK_TREE' not in os.environ and 'GIT_INDEX_FILE' not in os.environ
assert args[:2] == ['-c', 'core.fsmonitor=false']
configuration = {}
while args[:1] == ['-c']:
    key, value = args[1].split('=', 1)
    configuration[key] = value
    args = args[2:]
assert all(configuration.get(key) == value for key, value in {
    'pack.threads':'1', 'index.threads':'1', 'fetch.parallel':'1', 'checkout.workers':'1'}.items())
with open(os.environ['GIT_CALLS'], 'a') as stream:
    stream.write(json.dumps({'args': ['-c', 'core.fsmonitor=false'] + args, 'git_dir': os.environ.get('GIT_DIR')}) + '\\n')
if args[0] == 'clone':
    assert args[1:8] == ['--depth', '1', '--branch', 'main', '--single-branch', '--', 'https://github.com/rahel99x/Raven.git']
    if os.environ.get('CLONE_WAIT'):
        if os.environ.get('GIT_IGNORE_TERM'):
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        helper_ready = os.environ['CLONE_WAIT'] + '.helper'
        helper = subprocess.Popen([sys.executable, '-c',
            'import signal, sys, time; from pathlib import Path; '
            'signal.signal(signal.SIGTERM, signal.SIG_IGN); '
            'Path(sys.argv[1]).write_text("ready"); time.sleep(600)', helper_ready])
        while not Path(helper_ready).exists():
            time.sleep(0.01)
        Path(os.environ['CLONE_WAIT']).write_text(json.dumps({'git': os.getpid(), 'helper': helper.pid, 'bootstrap': os.getppid()}))
        time.sleep(600)
    if os.environ.get('CLONE_FAIL'):
        Path(args[-1]).mkdir()
        (Path(args[-1]) / 'partial').write_text('partial clone')
        raise SystemExit(17)
    shutil.copytree(os.environ['SOURCE_SEED'], args[-1])
    if os.environ.get('RACE_TARGET'):
        target = Path(os.environ['RACE_TARGET'])
        if os.environ.get('RACE_FILE'):
            target.write_text('concurrent file')
        else:
            target.mkdir()
            (target / 'sentinel').write_text('concurrent directory')
elif args[0] == '-C':
    if args[2:] == ['rev-parse', '--show-toplevel']:
        print(os.environ.get('GIT_TOPLEVEL', args[1]))
    elif args[2:] == ['config', '--get-all', 'remote.origin.url']:
        print(os.environ.get('GIT_REMOTE', 'https://github.com/rahel99x/Raven.git'))
    else:
        raise AssertionError(args)
else:
    raise AssertionError(args)
"""


def source(path, *, git=True):
    path.mkdir(mode=0o700)
    (path / "scripts").mkdir()
    (path / "scripts/install.py").write_text(INSTALLER)
    (path / "pyproject.toml").write_text('[project]\nname = "raven"\n')
    if git:
        (path / ".git").mkdir()
    return path


@pytest.fixture
def bootstrap(tmp_path):
    bin_dir = tmp_path / "tools"
    bin_dir.mkdir()
    git = bin_dir / "git"
    git.write_text("#!" + PYTHON + "\n" + GIT)
    git.chmod(0o700)
    seed = source(tmp_path / "seed")
    env = os.environ.copy()
    env.update(PATH=str(bin_dir), XDG_DATA_HOME=str(tmp_path / "user data"),
               BOOTSTRAP_CAPTURE=str(tmp_path / "installed.json"),
               GIT_CALLS=str(tmp_path / "git.jsonl"), SOURCE_SEED=str(seed))

    def run(*args, local=None, python=True, **changes):
        selected_env = env | changes
        arguments = ["--python", PYTHON] if python else []
        if local is not None:
            command = [BASH, str(local / "install.sh"), *arguments, *args]
            data = None
        else:
            command = [BASH, "-s", "--", *arguments, *args]
            data = SCRIPT
        result = subprocess.run(command, input=data, text=True, capture_output=True,
                                env=selected_env, cwd=tmp_path, timeout=15)
        return result

    def calls():
        path = Path(env["GIT_CALLS"])
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def installed():
        return json.loads(Path(env["BOOTSTRAP_CAPTURE"]).read_text())

    run.env, run.calls, run.installed = env, calls, installed
    return run


def test_local_checkout_needs_no_git_and_preserves_literal_arguments(bootstrap, tmp_path):
    local = source(tmp_path / "local source", git=False)
    (local / "install.sh").write_text(SCRIPT)
    args = ["--prefix", "release $(touch injected) `touch other`", "--bin-dir", "--source-dir",
            "--without-terminal", "--no-setup"]
    result = bootstrap(*args, local=local, PATH="")
    assert result.returncode == 0, result.stderr
    assert bootstrap.installed()["args"] == args
    assert bootstrap.installed()["source"] == str(local)
    assert not bootstrap.calls()
    assert not (tmp_path / "injected").exists()
    assert not (tmp_path / "other").exists()


def test_downloaded_script_clones_private_default_and_delegates(bootstrap, tmp_path):
    result = bootstrap("--without-terminal", "--no-setup", GIT_DIR="/unrelated/repository")
    assert result.returncode == 0, result.stderr
    target = Path(bootstrap.env["XDG_DATA_HOME"]) / "raven/source"
    assert bootstrap.installed()["source"] == str(target)
    assert bootstrap.installed()["args"] == ["--without-terminal", "--no-setup"]
    assert not bootstrap.installed()["stdin_tty"]
    assert target.stat().st_mode & 0o077 == 0
    assert not list(target.parent.glob(".raven-source-*"))
    assert len([call for call in bootstrap.calls() if "clone" in call["args"]]) == 1
    assert all(call["git_dir"] is None for call in bootstrap.calls())


def test_explicit_source_override_and_literal_shell_metacharacters(bootstrap, tmp_path):
    target = tmp_path / "raven $(touch injected) `touch other`"
    result = bootstrap("--clone", "--source-dir=" + str(target), "--prefix=release directory")
    assert result.returncode == 0, result.stderr
    assert bootstrap.installed()["source"] == str(target)
    assert bootstrap.installed()["args"] == ["--prefix=release directory"]
    assert not (tmp_path / "injected").exists()
    assert not (tmp_path / "other").exists()


def test_new_private_checkout_can_use_system_sticky_temporary_parent(bootstrap):
    with tempfile.TemporaryDirectory(prefix="raven-bootstrap-parent-") as name:
        target = Path(name)
        target.rmdir()
        result = bootstrap("--clone", "--source-dir", str(target))
        assert result.returncode == 0, result.stderr
        assert target.stat().st_uid == os.getuid()
        assert target.stat().st_mode & 0o077 == 0
        assert bootstrap.installed()["source"] == str(target)


@pytest.mark.parametrize("remote", ["https://github.com/rahel99x/Raven.git",
                                     "git@github.com:rahel99x/Raven.git",
                                     "ssh://git@github.com/rahel99x/Raven.git"])
def test_existing_official_checkout_reused_without_fetch_or_edit(bootstrap, tmp_path, remote):
    target = source(tmp_path / "existing checkout")
    dirty = target / "local edits.txt"
    dirty.write_text("keep my changes")
    result = bootstrap("--source-dir", str(target), GIT_REMOTE=remote)
    assert result.returncode == 0, result.stderr
    assert dirty.read_text() == "keep my changes"
    assert bootstrap.installed()["source"] == str(target)
    assert [call["args"][4:] for call in bootstrap.calls()] == [
        ["rev-parse", "--show-toplevel"], ["config", "--get-all", "remote.origin.url"]]


def test_git_authentication_environment_survives_without_logging_values(bootstrap, tmp_path):
    target = source(tmp_path / "authenticated source")
    settings = [("core.worktree", "/unrelated/tree"),
                ("http.extraHeader", "Authorization: fixture-secret"),
                ("Core.Bare", "true"), ("http.proxy", "https://fixture-proxy.invalid")]
    env = {"VERIFY_AUTH_ENV": "1", "GIT_CONFIG_COUNT": str(len(settings)),
           "GIT_WORK_TREE": "/unrelated/tree", "GIT_INDEX_FILE": "/unrelated/index",
           "GIT_CONFIG_PARAMETERS": " ".join(shlex.quote(value) for value in (
               "core.worktree=/wrong", "credential.helper=fixture with spaces",
               "http.extraHeader=fixture's second secret", "core.bare=true"))}
    for index, (key, value) in enumerate(settings):
        env["GIT_CONFIG_KEY_" + str(index)] = key
        env["GIT_CONFIG_VALUE_" + str(index)] = value
    for key, value in {"GIT_CONFIG_GLOBAL": str(tmp_path / "global config"),
                       "GIT_CONFIG_SYSTEM": str(tmp_path / "system config"),
                       "GIT_SSL_CAINFO": str(tmp_path / "trusted CA.pem"),
                       "GIT_SSH_COMMAND": "ssh -oProxyCommand=fixture"}.items():
        env[key] = env["EXPECTED_" + key] = value
    result = bootstrap("--source-dir", str(target), **env)
    assert result.returncode == 0, result.stderr
    assert len(bootstrap.calls()) == 2
    observed = result.stdout + result.stderr + Path(bootstrap.env["GIT_CALLS"]).read_text()
    assert "fixture-secret" not in observed
    assert "second secret" not in observed
    assert "fixture-proxy.invalid" not in observed


def test_real_git_clones_through_injected_official_url_rewrite(bootstrap, tmp_path):
    real_git = shutil.which("git")
    assert real_git is not None
    origin = source(tmp_path / "local origin with spaces", git=False)
    git_env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    git_env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)
    for args in (["init", "--initial-branch=main"], ["add", "."],
                 ["-c", "user.name=Raven fixture", "-c", "user.email=fixture@example.invalid",
                  "commit", "-m", "Local bootstrap fixture"]):
        subprocess.run([real_git, "-C", str(origin), *args], env=git_env, check=True,
                       capture_output=True, timeout=10)
    target = tmp_path / "real bootstrapped source"
    result = bootstrap("--clone", "--source-dir", str(target), PATH=os.environ["PATH"],
                       GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                       GIT_CONFIG_SYSTEM=os.devnull, GIT_CONFIG_COUNT="2",
                       GIT_CONFIG_KEY_0="url." + origin.as_uri() + ".insteadOf",
                       GIT_CONFIG_VALUE_0="https://github.com/rahel99x/Raven.git",
                       GIT_CONFIG_KEY_1="core.worktree", GIT_CONFIG_VALUE_1=str(tmp_path / "wrong tree"))
    assert result.returncode == 0, result.stderr
    assert bootstrap.installed()["source"] == str(target)
    stored = subprocess.run([real_git, "-C", str(target), "config", "--get", "remote.origin.url"],
                            env=git_env, check=True, text=True, capture_output=True, timeout=10)
    assert stored.stdout.strip() == "https://github.com/rahel99x/Raven.git"
    assert not (tmp_path / "wrong tree").exists()
    assert not list(tmp_path.glob(".raven-source-*"))


@pytest.mark.parametrize("config", [{"GIT_CONFIG_COUNT": "129"},
                                    {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.extraHeader"},
                                    {"GIT_CONFIG_PARAMETERS": "'private-invalid-secret"}])
def test_invalid_injected_git_configuration_fails_without_disclosing_values(bootstrap, tmp_path, config):
    target = source(tmp_path / "existing")
    result = bootstrap("--source-dir", str(target), **config)
    assert result.returncode == 2
    assert "Injected Git configuration" in result.stderr
    assert "private-invalid-secret" not in result.stderr
    assert not bootstrap.calls()


@pytest.mark.parametrize("kind", ["non-git", "file", "symlink", "wrong-origin", "multiple-origins", "nested", "writable"])
def test_unrelated_or_unsafe_existing_destination_is_preserved(bootstrap, tmp_path, kind):
    target = tmp_path / "preserve"
    if kind == "file":
        target.write_text("untouched")
        sentinel = target
    elif kind == "symlink":
        other = source(tmp_path / "other")
        sentinel = other / "untouched"
        sentinel.write_text("untouched")
        target.symlink_to(other, target_is_directory=True)
    else:
        source(target, git=kind != "non-git")
        sentinel = target / "untouched"
        sentinel.write_text("untouched")
        if kind == "writable":
            target.chmod(0o777)
    env = {}
    if kind == "wrong-origin":
        env["GIT_REMOTE"] = "https://secret-token@example.invalid/not-raven.git"
    elif kind == "multiple-origins":
        env["GIT_REMOTE"] = "https://example.invalid/unrelated.git\nhttps://github.com/rahel99x/Raven.git"
    elif kind == "nested":
        env["GIT_TOPLEVEL"] = str(target.parent)
    result = bootstrap("--source-dir", str(target), **env)
    if kind == 'symlink':
        assert result.returncode == 0, result.stderr
        assert bootstrap.installed()['source'] == str(other)
        assert sentinel.read_text() == 'untouched'
        return
    assert result.returncode == 2
    assert sentinel.read_text() == "untouched"
    assert "secret-token" not in result.stderr
    assert not Path(bootstrap.env["BOOTSTRAP_CAPTURE"]).exists()
    assert not any("clone" in call["args"] for call in bootstrap.calls())


def test_symlink_ancestor_is_rejected_without_following_it(bootstrap, tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(actual, target_is_directory=True)
    result = bootstrap("--source-dir", str(alias / "raven"))
    assert result.returncode == 2
    assert not list(actual.iterdir())
    assert not bootstrap.calls()


def test_clone_failure_removes_only_private_stage(bootstrap, tmp_path):
    target = tmp_path / "missing"
    neighbor = tmp_path / ".raven-source-someone-else"
    neighbor.mkdir()
    hooks = tmp_path / "signal-observer"
    hooks.mkdir()
    signals = tmp_path / "signals.txt"
    (hooks / "sitecustomize.py").write_text("""import os
original = os.killpg
def observe(pid, signum):
    with open(os.environ['SIGNAL_CAPTURE'], 'a') as stream:
        stream.write(str(pid) + ':' + str(signum) + '\\n')
    return original(pid, signum)
os.killpg = observe
""")
    result = bootstrap("--clone", "--source-dir", str(target), CLONE_FAIL="1",
                       PYTHONPATH=str(hooks), SIGNAL_CAPTURE=str(signals))
    assert result.returncode == 2
    assert not target.exists()
    assert list(tmp_path.glob(".raven-source-*")) == [neighbor]
    assert not Path(bootstrap.env["BOOTSTRAP_CAPTURE"]).exists()
    assert not signals.exists(), "A completed, reaped Git command must never be signaled"


@pytest.mark.parametrize("ignore_term,direct_term", [(True, False), (False, False), (False, True)])
def test_interrupt_stops_owned_git_and_removes_private_stage(bootstrap, tmp_path, ignore_term, direct_term):
    target = tmp_path / "interrupted source"
    ready = tmp_path / "git-ready"
    process = subprocess.Popen([BASH, "-s", "--", "--python", PYTHON,
                                "--clone", "--source-dir", str(target)], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               env=bootstrap.env | {"CLONE_WAIT": str(ready),
                                                    "GIT_IGNORE_TERM": "1" if ignore_term else ""},
                               cwd=tmp_path, start_new_session=True)
    unrelated = subprocess.Popen([PYTHON, "-c", "import time; time.sleep(600)"], start_new_session=True)
    git_pid = None

    def still_running(pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        # Linux containers may defer init's reaping of an orphan zombie. A
        # terminated helper is distinct from a helper still doing transport I/O.
        status = Path("/proc") / str(pid) / "stat"
        try:
            return status.read_text().rsplit(")", 1)[1].split()[0] != "Z"
        except FileNotFoundError:
            return Path("/proc").exists() is False

    try:
        process.stdin.write(SCRIPT.encode())
        process.stdin.close()
        process.stdin = None
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            assert process.poll() is None
            time.sleep(0.01)
        assert ready.exists(), "Git never reached the controlled waiting state"
        identities = json.loads(ready.read_text())
        git_pid = identities["git"]
        signum = signal.SIGTERM if direct_term else signal.SIGINT
        if direct_term:
            os.kill(identities["bootstrap"], signum)
        else:
            os.killpg(process.pid, signum)
        _, errors = process.communicate(timeout=15)
        assert process.returncode in (128 + signum, -signum), errors.decode()
        with pytest.raises(ProcessLookupError):
            os.kill(git_pid, 0)
        deadline = time.monotonic() + 2
        while still_running(identities["helper"]) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not still_running(identities["helper"])
        assert unrelated.poll() is None
        assert not target.exists()
        assert not list(tmp_path.glob(".raven-source-*"))
    finally:
        if git_pid is not None and still_running(git_pid):
            os.killpg(git_pid, signal.SIGKILL)
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=3)
        unrelated.terminate()
        unrelated.wait(timeout=3)


@pytest.mark.parametrize("kind", ["file", "directory"])
def test_destination_created_during_clone_is_preserved(bootstrap, tmp_path, kind):
    target = tmp_path / "raced target"
    result = bootstrap("--clone", "--source-dir", str(target), RACE_TARGET=str(target),
                       RACE_FILE="1" if kind == "file" else "")
    assert result.returncode == 2
    assert "appeared during cloning" in result.stderr
    preserved = target if kind == "file" else target / "sentinel"
    assert preserved.read_text() == "concurrent " + kind
    assert not list(tmp_path.glob(".raven-source-*"))


def test_plan_missing_source_does_not_need_git_create_or_contact_network(bootstrap):
    result = bootstrap("--plan", PATH="")
    assert result.returncode == 0, result.stderr
    assert "No files or network requests made" in result.stderr
    assert not Path(bootstrap.env["XDG_DATA_HOME"]).exists()
    assert not bootstrap.calls()
    assert not Path(bootstrap.env["BOOTSTRAP_CAPTURE"]).exists()


def test_existing_source_plan_delegates_without_clone(bootstrap, tmp_path):
    target = source(tmp_path / "existing")
    result = bootstrap("--source-dir", str(target), "--plan", "--bin-dir", "another bin")
    assert result.returncode == 0, result.stderr
    assert bootstrap.installed()["args"] == ["--plan", "--bin-dir", "another bin"]
    assert not any("clone" in call["args"] for call in bootstrap.calls())


def test_actual_installer_plan_works_without_git_or_writes(bootstrap, tmp_path):
    prefix, bin_dir = tmp_path / "real releases", tmp_path / "real bin"
    result = bootstrap("--plan", "--prefix", str(prefix), "--bin-dir", str(bin_dir),
                       local=ROOT, PATH="")
    assert result.returncode == 0, result.stderr
    assert "Ready to install" in result.stdout
    assert str(prefix) in result.stdout
    assert str(bin_dir) in result.stdout
    assert not prefix.exists()
    assert not bin_dir.exists()
    assert not bootstrap.calls()


@pytest.mark.parametrize("arguments", [["--python", "/missing/python"], ["--python="],
                                       ["--source-dir"], ["--source-dir="],
                                       ["--prefix"], ["--bin-dir"]])
def test_invalid_bootstrap_arguments_fail_before_writes(bootstrap, arguments):
    result = bootstrap(*arguments, python=False)
    assert result.returncode == 2
    assert "Raven installation:" in result.stderr
    assert not bootstrap.calls()
    assert not Path(bootstrap.env["XDG_DATA_HOME"]).exists()


def test_missing_python_and_git_have_actionable_errors(bootstrap):
    result = bootstrap(python=False, PATH="")
    assert result.returncode == 2
    assert "Python 3.10+" in result.stderr
    result = bootstrap(PATH="")
    assert result.returncode == 2
    assert "Install Git" in result.stderr
    assert not Path(bootstrap.env["XDG_DATA_HOME"]).exists()


def test_unsupported_python_is_skipped_for_supported_fallback(bootstrap, tmp_path):
    bin_dir = Path(bootstrap.env["PATH"])
    old = bin_dir / "python3"
    old.write_text("#!" + BASH + "\nexit 1\n")
    old.chmod(0o700)
    supported = bin_dir / "python"
    supported.write_text("#!" + BASH + "\nexec " + shlex.quote(PYTHON) + ' "$@"\n')
    supported.chmod(0o700)
    result = bootstrap("--plan", python=False)
    assert result.returncode == 0, result.stderr
    result = bootstrap("--python", str(old), python=False)
    assert result.returncode == 2
    assert "Python 3.10+" in result.stderr


def test_separator_stops_wrapper_parsing_and_preserves_installer_exit(bootstrap, tmp_path):
    local = source(tmp_path / "local", git=False)
    (local / "install.sh").write_text(SCRIPT)
    args = ["--source-dir", "literal target", "--python=literal", "--no-setup", "--", "--help"]
    result = bootstrap("--", *args, local=local, INSTALL_EXIT="23")
    assert result.returncode == 23
    assert bootstrap.installed()["args"] == args
    assert not (tmp_path / "literal target").exists()


def test_explicit_source_takes_precedence_over_script_checkout(bootstrap, tmp_path):
    local = source(tmp_path / "local source")
    (local / "install.sh").write_text(SCRIPT)
    target = tmp_path / "chosen source"
    result = bootstrap("--clone", "--source-dir", str(target), local=local)
    assert result.returncode == 0, result.stderr
    assert bootstrap.installed()["source"] == str(target)
    assert any("clone" in call["args"] for call in bootstrap.calls())


def test_control_characters_in_source_are_rejected_before_creation(bootstrap, tmp_path):
    target = tmp_path / "source\nunsafe"
    result = bootstrap("--source-dir", str(target))
    assert result.returncode == 2
    assert "control characters" in result.stderr
    assert not target.exists()
    assert not bootstrap.calls()


def test_downloaded_script_restores_controlling_tty_for_guided_setup(bootstrap, tmp_path):
    """Supply source through a pipe while preserving the user's terminal."""
    local = source(tmp_path / "tty checkout")
    master, slave = pty.openpty()
    code = """import fcntl, os, sys, termios
os.setsid()
fcntl.ioctl(1, termios.TIOCSCTTY, 0)
os.execv(sys.argv[1], sys.argv[1:])
"""
    process = subprocess.Popen([PYTHON, "-c", code, BASH, "-s", "--", "--python", PYTHON,
                                "--source-dir", str(local)], stdin=subprocess.PIPE,
                               stdout=slave, stderr=slave, env=bootstrap.env, cwd=tmp_path)
    os.close(slave)
    output = bytearray()
    selector = selectors.DefaultSelector()
    selector.register(master, selectors.EVENT_READ)
    try:
        process.stdin.write(SCRIPT.encode())
        process.stdin.close()
        deadline = time.monotonic() + 10
        while process.poll() is None and time.monotonic() < deadline:
            if selector.select(0.05):
                try:
                    output.extend(os.read(master, 65536))
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
        assert process.wait(timeout=1) == 0, output.decode(errors="replace")
        assert bootstrap.installed()["stdin_tty"] is True
        assert bootstrap.installed()["stdout_tty"] is True
    finally:
        selector.close()
        os.close(master)
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                process.kill()
        process.wait(timeout=3)
