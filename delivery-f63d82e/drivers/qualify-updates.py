#!/usr/bin/env python3
"""Exercise actual installed update/revert lifecycles against an owned local Git origin.

Requires Git, venv/pip and access to the configured Python package index.
--pipx additionally requires pipx on PATH. No public repository is changed,
no scheduler is contacted, and no shell profile or existing install is modified.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAX_LOG = 16 << 20


def source_snapshot(root):
    """Include added/deleted paths and HEAD, not only initially present bytes."""
    names = sorted(
        set(
            filter(
                None,
                subprocess.check_output(
                    [
                        "git",
                        "ls-files",
                        "--cached",
                        "--others",
                        "--exclude-standard",
                        "-z",
                    ],
                    cwd=root,
                )
                .decode()
                .split("\0"),
            )
        )
    )
    return {
        "commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip(),
        "inventory": names,
        "hashes": {
            name: hashlib.sha256((root / name).read_bytes()).hexdigest()
            for name in names
            if (root / name).is_file()
        },
    }


def supervise(
    command,
    path,
    *,
    cwd,
    env=None,
    stdin_path=None,
    timeout=900,
    grace=10,
    max_log=MAX_LOG,
):
    """Bound output and stop only our pinned process group before reaping.

    The updater/installer owns further sessions. TERM gives its cleanup handlers
    time to stop those children; WNOWAIT retains our leader PID until the final
    group signal, preventing accidental signalling after PID reuse.
    """
    if not hasattr(os, "waitid") or not hasattr(os, "WNOWAIT"):
        raise RuntimeError(
            "Update qualification needs POSIX waitid/WNOWAIT for owned process cleanup"
        )
    process, failure, stop_at, exited_at = None, "", None, None
    input_stream = None
    ownership_lost = False
    interrupted = []
    handlers = {}
    result = {
        "exit": None,
        "failure": "",
        "timed_out": False,
        "interrupted_signal": None,
        "log_limit": max_log,
        "log_bytes": 0,
        "discarded_log_bytes": 0,
        "reaped": False,
    }
    started = time.monotonic()

    def interrupt(signum, frame):
        if not interrupted:
            interrupted.append(signum)

    def exited():
        nonlocal ownership_lost
        try:
            return (
                os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                is not None
            )
        except ChildProcessError:
            ownership_lost = True
            raise RuntimeError("An external reaper removed the owned command leader")

    def send(signum):
        if ownership_lost:
            return
        try:
            os.killpg(process.pid, signum)
        except ProcessLookupError:
            pass

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            handlers[signum] = signal.signal(signum, interrupt)
        with path.open("wb") as stream, selectors.DefaultSelector() as selector:
            if stdin_path is not None:
                input_stream = Path(stdin_path).open("rb")  # noqa: SIM115 — closed with child pipes in finally
            process = subprocess.Popen(
                [str(item) for item in command],
                cwd=cwd,
                env=env,
                stdin=input_stream if input_stream is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            os.set_blocking(process.stdout.fileno(), False)
            selector.register(process.stdout, selectors.EVENT_READ)
            eof = False
            while True:
                now = time.monotonic()
                if interrupted and not failure:
                    result["interrupted_signal"] = interrupted[0]
                    failure = "Interrupted by signal " + str(interrupted[0])
                if now - started >= timeout and not failure:
                    result["timed_out"] = True
                    failure = "Command exceeded " + str(timeout) + " seconds"
                if failure and stop_at is None:
                    send(signal.SIGTERM)
                    stop_at = now + grace
                for key, _ in selector.select(0.05):
                    try:
                        block = os.read(key.fd, 65536)
                    except BlockingIOError:
                        continue
                    if not block:
                        selector.unregister(key.fileobj)
                        eof = True
                        continue
                    keep = min(len(block), max(0, max_log - result["log_bytes"]))
                    stream.write(block[:keep])
                    result["log_bytes"] += keep
                    result["discarded_log_bytes"] += len(block) - keep
                    if keep < len(block) and not failure:
                        failure = (
                            "Command exceeded the " + str(max_log) + " byte log limit"
                        )
                done = exited()
                if done and exited_at is None:
                    exited_at = time.monotonic()
                if (
                    done
                    and eof
                    or stop_at is not None
                    and (done or time.monotonic() >= stop_at)
                ):
                    break
                if (
                    exited_at is not None
                    and time.monotonic() - exited_at >= 2
                    and not failure
                ):
                    failure = (
                        "Command exited while a descendant retained its output pipe"
                    )
    except BaseException as exc:  # noqa: BLE001 — retain cancellation/failure evidence after owned cleanup
        failure = failure or type(exc).__name__ + ": " + str(exc)
    finally:
        if process is not None and not ownership_lost:
            try:
                if failure and stop_at is None:
                    send(signal.SIGTERM)
                    deadline = time.monotonic() + grace
                    while not exited() and time.monotonic() < deadline:
                        time.sleep(0.02)
                # Even an exited leader stays unreaped until this final signal.
                # Normal completion also removes any same-group orphan.
                send(signal.SIGKILL)
                result["exit"] = process.wait(timeout=5)
                result["reaped"] = True
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                failure = failure or "Owned process cleanup failed: " + str(exc)
        if process is not None and process.stdout is not None:
            process.stdout.close()
        if input_stream is not None:
            input_stream.close()
        for signum, handler in handlers.items():
            signal.signal(signum, handler)
    result["failure"] = failure
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--pipx", action="store_true", help="Also exercise the real pipx owning manager"
    )
    parser.add_argument('--pipx-backend', choices=('pip', 'uv'), help='Select a current pipx backend explicitly; default uses pipx discovery')
    args = parser.parse_args()
    output = args.output.absolute()
    output.mkdir(parents=True, exist_ok=False)
    work = Path(tempfile.mkdtemp(prefix="raven-update-qualification-"))
    report = {
        "schema": "raven.update-qualification/v1",
        "passed": False,
        "work": str(work),
        "checks": [],
        "limits": "Actual piped managed bootstrap/clone and optional pipx local clone, virtual environments and installed update/revert commands against an owned local Git origin; no HTTP bootstrap download, public source download, live Slurm, remote cluster, GPU or other operating system qualification.",
    }
    report_file = output / "result.json"
    version_tree = ast.parse((ROOT / "raven/__init__.py").read_text())
    baseline_version = next(
        ast.literal_eval(node.value)
        for node in version_tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "__version__"
            for target in node.targets
        )
    )
    updated_version = f"{int(baseline_version.split('.')[0]) + 1}.0.0"
    report["fixture_versions"] = [baseline_version, updated_version]

    def save():
        report_file.write_text(json.dumps(report, indent=2) + "\n")

    def run(label, command, *, cwd=work, env=None, expected=0, stdin_path=None):
        start = time.monotonic()
        path = output / (label + ".log")
        print(label, flush=True)
        outcome = supervise(command, path, cwd=cwd, env=env, stdin_path=stdin_path)
        raw = path.read_bytes() if path.exists() else b""
        record = {
            "label": label,
            "command": [str(x) for x in command],
            "cwd": str(cwd),
            "stdin": str(stdin_path) if stdin_path is not None else None,
            **outcome,
            "expected_exit": expected,
            "seconds": time.monotonic() - start,
            "log": path.name,
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
        report["checks"].append(record)
        save()
        if outcome["failure"] or outcome["exit"] != expected:
            raise RuntimeError("Qualification failed: " + label + "; see " + str(path))
        return raw.decode("utf-8", "replace")

    initial_source = source_snapshot(ROOT)
    names = initial_source["inventory"]
    report["source_commit"] = initial_source["commit"]
    report["source_inventory"] = names
    report["source_hashes"] = initial_source["hashes"]
    save()
    seed = work / "origin"
    seed.mkdir()
    for name in names:
        source = ROOT / name
        if not source.is_file():
            continue
        target = seed / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target, follow_symlinks=False)
    git_env = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    git_env.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_AUTHOR_NAME="Raven qualification",
        GIT_AUTHOR_EMAIL="qualification@example.invalid",
        GIT_COMMITTER_NAME="Raven qualification",
        GIT_COMMITTER_EMAIL="qualification@example.invalid",
    )
    run("origin-init", ["git", "init", "--initial-branch=main"], cwd=seed, env=git_env)
    (seed / "update-fixture-config.json").write_text('{"account":"fixture-default"}\n')
    (seed / 'raven/_qualification_deleted_probe.py').write_text('VALUE = "old module"\n')
    (seed / 'raven/_qualification_swapped_probe').mkdir()
    (seed / 'raven/_qualification_swapped_probe/__init__.py').write_text('VALUE = "old package"\n')
    run("origin-add", ["git", "add", "--force", "--all"], cwd=seed, env=git_env)
    run(
        "origin-commit",
        ["git", "commit", "-m", "Qualification baseline"],
        cwd=seed,
        env=git_env,
    )
    first = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=seed, text=True, env=git_env
    ).strip()
    modes = ["managed"] + (["pipx"] if args.pipx else [])
    environments = {}
    pipx = shutil.which("pipx")
    if args.pipx and not pipx:
        raise RuntimeError("--pipx requires pipx on PATH")
    for mode in modes:
        base = work / mode
        base.mkdir()
        source = base / "source"
        home = base / "home"
        home.mkdir()
        bin_dir = base / "bin"
        bin_dir.mkdir()
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("RAVEN_", "PIPX_", "GIT_"))
        }
        env.update(
            HOME=str(home),
            XDG_CONFIG_HOME=str(home / "config"),
            XDG_STATE_HOME=str(home / "state"),
            XDG_CACHE_HOME=str(home / "cache"),
            PATH=str(bin_dir)
            + os.pathsep
            + str(Path(sys.executable).parent)
            + os.pathsep
            + os.environ["PATH"],
            PIPX_HOME=str(base / "pipx"),
            PIPX_BIN_DIR=str(bin_dir),
            PIPX_MAN_DIR=str(base / "man"),
            PIPX_DEFAULT_PYTHON=sys.executable,
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
        )
        if args.pipx_backend:
            env['PIPX_DEFAULT_BACKEND'] = args.pipx_backend
        if mode == "managed":
            # Exercise stdin bootstrap discovery and its real Git clone, while
            # resolving the documented official URL to this owned fixture.
            env.update(
                GIT_CONFIG_COUNT="1",
                GIT_CONFIG_KEY_0="url." + str(seed) + ".insteadOf",
                GIT_CONFIG_VALUE_0="https://github.com/rahel99x/Raven.git",
            )
        else:
            run(mode + "-clone", ["git", "clone", str(seed), str(source)], env=env)
        config = source / "update-fixture-config.json"
        env["RAVEN_CONFIG"] = str(config)
        persistent = home / "config/raven/config.json"
        persistent.parent.mkdir(parents=True)
        persistent.write_text("external configuration is preserved\n")
        if mode == "managed":
            run(
                mode + "-install",
                [
                    "bash",
                    "-s",
                    "--",
                    "--clone",
                    "--source-dir",
                    source,
                    "--python",
                    sys.executable,
                    "--prefix",
                    base / "releases",
                    "--bin-dir",
                    bin_dir,
                    "--without-terminal",
                    "--no-setup",
                ],
                env=env,
                stdin_path=ROOT / "install.sh",
            )
        else:
            run(
                mode + "-install",
                [
                    pipx,
                    "install",
                    "--python",
                    sys.executable,
                    str(source) + "[terminal]",
                ],
                env=env,
            )
        version = run(
            mode + "-baseline-version", [bin_dir / "raven", "--version"], env=env
        )
        assert baseline_version in version
        if mode == 'pipx':
            metadata = json.loads((base / 'pipx/venvs/raven/pipx_metadata.json').read_text())
            report['pipx_backend'] = metadata.get('backend', 'pip')
        environments[mode] = (base, source, bin_dir, env, persistent)
    (seed / "raven/__init__.py").write_text(
        (seed / "raven/__init__.py")
        .read_text()
        .replace(
            f'__version__ = "{baseline_version}"', f'__version__ = "{updated_version}"'
        )
    )
    (seed / "pyproject.toml").write_text(
        (seed / "pyproject.toml")
        .read_text()
        .replace(f'version = "{baseline_version}"', f'version = "{updated_version}"')
    )
    (seed / "update-fixture-config.json").write_text(
        '{"account":"remote-new-default"}\n'
    )
    (seed / 'raven/_qualification_deleted_probe.py').unlink()
    shutil.rmtree(seed / 'raven/_qualification_swapped_probe')
    (seed / 'raven/_qualification_swapped_probe.py').write_text('VALUE = "new module"\n')
    run("origin-add-next", ["git", "add", "--all"], cwd=seed, env=git_env)
    run(
        "origin-commit-next",
        ["git", "commit", "-m", "Qualification upstream update"],
        cwd=seed,
        env=git_env,
    )
    second = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=seed, text=True, env=git_env
    ).strip()
    for mode, (base, source, bin_dir, env, persistent) in environments.items():
        stale = source / 'build/lib/raven'
        (stale / '_qualification_swapped_probe').mkdir(parents=True, exist_ok=True)
        (stale / '_qualification_deleted_probe.py').write_text('VALUE = "stale build module"\n')
        (stale / '_qualification_swapped_probe/__init__.py').write_text('VALUE = "stale build package"\n')
        code = source / "raven/__init__.py"
        code.write_text(
            code.read_text() + "\n# local source edit preserved by recovery\n"
        )
        edited = code.read_bytes()
        note = source / "local-note.txt"
        note.write_text("untracked local source\n")
        config = source / "update-fixture-config.json"
        config.write_text('{"account":"my-lab"}\n')
        plan = run(mode + "-plan", [bin_dir / "raven", "update", "--allow-fork", "--plan"], env=env)
        assert "no network access" in plan
        checked = run(
            mode + "-check", [bin_dir / "ravenh", "update", "--allow-fork", "--check"], env=env
        )
        assert second[:12] in checked
        assert (
            subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=source, text=True, env=git_env
            ).strip()
            == first
        )
        run(mode + "-refuse-edits", [bin_dir / "raven", "update", "--allow-fork"], env=env, expected=2)
        assert code.read_bytes() == edited and note.exists()
        updated = run(
            mode + "-overwrite", [bin_dir / "raven", "update", "--allow-fork", "--overwrite"], env=env
        )
        backup = Path(
            next(
                line.split(": ", 1)[1]
                for line in updated.splitlines()
                if line.startswith("Recovery backup: ")
            )
        )
        assert backup.is_dir() and "raven revert " in updated
        assert updated_version in run(
            mode + "-updated-version", [bin_dir / "ravenh", "--version"], env=env
        )
        python = (base / 'releases/current/bin/python' if mode == 'managed' else base / 'pipx/venvs/raven/bin/python')
        run(mode + '-pristine-upstream-deletion', [python, '-I', '-c',
            'import importlib.util; assert importlib.util.find_spec("raven._qualification_deleted_probe") is None; '
            'from raven import _qualification_swapped_probe as probe; assert probe.VALUE == "new module"'], env=env)
        assert not note.exists() and b"local source edit" not in code.read_bytes()
        assert config.read_text() == '{"account":"my-lab"}\n'
        config.write_text('{"account":"my-current-lab"}\n')
        run(
            mode + "-revert-plan",
            [bin_dir / "raven", "revert", backup, "--plan"],
            env=env,
        )
        reverted = run(mode + "-revert", [bin_dir / "raven", "revert", backup], env=env)
        recovery = Path(
            next(
                line.split(": ", 1)[1]
                for line in reverted.splitlines()
                if line.startswith("Recovery backup: ")
            )
        )
        assert (
            code.read_bytes() == edited
            and note.read_text() == "untracked local source\n"
        )
        assert config.read_text() == '{"account":"my-current-lab"}\n'
        assert baseline_version in run(
            mode + "-reverted-version", [bin_dir / "ravenl", "--version"], env=env
        )
        run(mode + "-revert-recovery", [bin_dir / "raven", "revert", recovery], env=env)
        assert updated_version in run(
            mode + "-recovered-version", [bin_dir / "raven", "--version"], env=env
        )
        assert (
            not note.exists() and config.read_text() == '{"account":"my-current-lab"}\n'
        )
        assert persistent.read_text() == "external configuration is preserved\n"
        if mode == "pipx":
            run(
                mode + "-terminal-extras",
                [bin_dir / "raven", "terminal", "selftest", "--json"],
                env=env,
            )
        report.setdefault("lifecycles", {})[mode] = {
            "backup": str(backup),
            "revert_safety_backup": str(recovery),
            "passed": True,
        }
        save()
    report["final_source"] = source_snapshot(ROOT)
    report["source_stable"] = report["final_source"] == initial_source
    save()
    if not report["source_stable"]:
        raise RuntimeError(
            "Source changed during installed lifecycle qualification; rerun on the delivered revision"
        )
    report["passed"] = True
    save()
    print(
        json.dumps(
            {
                "passed": True,
                "modes": modes,
                "checks": len(report["checks"]),
                "result": str(report_file),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
