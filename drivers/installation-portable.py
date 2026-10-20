#!/usr/bin/env python3
"""Reproduce the installer review's warm, local 2,672-file update fixture.

Run ``prepare`` once in a fresh private work directory, then invoke each mode in
a separate process. Run serially without builds or tests competing for resources.
The baseline revision must remain available in the selected source repository.
Measurements qualify this Linux/local-Git fixture, not NFS, Slurm or GPU behavior.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import resource
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SCHEMA = "raven.install-review-fixture/v1"
FILES = 2672
MODES = ("baseline", "current", "baseline-raw", "current-raw")


def git(root, *args):
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    return (
        subprocess.check_output(
            [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "init.templateDir=",
                "-c",
                "user.name=Review",
                "-c",
                "user.email=review@example.invalid",
                "-C",
                str(root),
                *args,
            ],
            env=env,
            stderr=subprocess.DEVNULL,
        )
        .decode()
        .strip()
    )


def write_new(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)


def encode(value):
    return (
        json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n"
    ).encode()


def payload_digest(root):
    digest = hashlib.sha256()
    paths = sorted(root.glob("layer*/branch*/input*.py"))
    if len(paths) != FILES:
        raise ValueError("The complete 2,672-file payload is required")
    for path in paths:
        digest.update(path.relative_to(root).as_posix().encode() + b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def prepare(source_root, workdir, baseline):
    """Create only new fixtures; never reuse or clean a pre-existing directory."""
    baseline_commit = git(
        source_root, "rev-parse", "--verify", "--end-of-options", baseline + "^{commit}"
    )
    if len(baseline_commit) not in (40, 64) or any(
        char not in "0123456789abcdef" for char in baseline_commit
    ):
        raise ValueError("The baseline must resolve to one immutable Git commit")
    os.mkdir(workdir, mode=0o700)
    info = workdir.lstat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError(
            "The fresh benchmark work directory must be owned and mode 0700"
        )
    seed = workdir / "seed"
    seed.mkdir(mode=0o700)
    git(seed, "init", "-b", "main")
    for index in range(FILES):
        path = (
            seed
            / f"layer{index % 32:02d}"
            / f"branch{index % 8:02d}"
            / f"input{index:04d}.py"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("VALUE = " + str(index) + "\n", encoding="utf-8")
    git(seed, "add", ".")
    git(seed, "commit", "-m", "baseline")
    initial_commit = git(seed, "rev-parse", "HEAD")
    for mode in ("baseline", "current"):
        git(workdir, "clone", str(seed), str(workdir / mode))
    (seed / "layer00/branch00/input0000.py").write_text(
        "VALUE = 999\n", encoding="utf-8"
    )
    git(seed, "add", ".")
    git(seed, "commit", "-m", "upstream")
    modules = {}
    for name in ("update_source", "config_trust"):
        data = subprocess.check_output(
            ["git", "show", baseline_commit + ":raven/" + name + ".py"],
            cwd=source_root,
            stderr=subprocess.DEVNULL,
        )
        filename = "baseline_" + name + ".py"
        write_new(workdir / filename, data)
        modules[filename] = hashlib.sha256(data).hexdigest()
    manifest = {
        "schema": SCHEMA,
        "uid": os.getuid(),
        "workdir": str(workdir),
        "device": info.st_dev,
        "inode": info.st_ino,
        "source_root": str(source_root),
        "baseline_revision": baseline_commit,
        "baseline_modules": modules,
        "fixture_initial_commit": initial_commit,
        "fixture_target_commit": git(seed, "rev-parse", "HEAD"),
        "payload_sha256": payload_digest(seed),
        "files": FILES,
    }
    write_new(workdir / "fixture.json", encode(manifest))
    return manifest


def read_owned(path, *, limit=65536):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_size > limit
        ):
            raise ValueError("Benchmark metadata must be a bounded private owned file")
        data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError("Benchmark metadata exceeds its bound")
        return data


def validate(workdir, source_root, baseline):
    """Refuse other users' paths and any symlink inside the private fixture."""
    info = workdir.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise ValueError("Run prepare in a fresh owned 0700 directory first")
    manifest = json.loads(read_owned(workdir / "fixture.json"))
    if not isinstance(manifest, dict):
        raise TypeError("The benchmark owner receipt must be a JSON object")
    expected = (SCHEMA, os.getuid(), str(workdir), info.st_dev, info.st_ino)
    if (
        tuple(
            manifest.get(key) for key in ("schema", "uid", "workdir", "device", "inode")
        )
        != expected
    ):
        raise ValueError(
            "The benchmark work directory does not match its owner receipt"
        )
    if (
        manifest.get("source_root") != str(source_root)
        or manifest.get("files") != FILES
    ):
        raise ValueError("Use the source repository recorded by prepare")
    pinned = git(
        source_root, "rev-parse", "--verify", "--end-of-options", baseline + "^{commit}"
    )
    if manifest.get("baseline_revision") != pinned:
        raise ValueError("Use the baseline revision recorded by prepare")
    for name in ("update_source", "config_trust"):
        filename = "baseline_" + name + ".py"
        actual = hashlib.sha256(read_owned(workdir / filename)).hexdigest()
        if manifest.get("baseline_modules", {}).get(filename) != actual:
            raise ValueError("A retained baseline module changed")
    pending = [workdir]
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                child = entry.stat(follow_symlinks=False)
                if child.st_uid != os.getuid() or not (
                    stat.S_ISREG(child.st_mode) or stat.S_ISDIR(child.st_mode)
                ):
                    raise ValueError(
                        "Only owned directories and regular fixture files are allowed"
                    )
                if stat.S_ISDIR(child.st_mode):
                    pending.append(Path(entry.path))
    return manifest


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def measure(mode, source_root, workdir, manifest):
    # An interrupted or completed mode is never retried in place: prepare a new
    # work directory to retain the exact one-update baseline for every sample.
    write_new(workdir / ("." + mode + ".started"), b"one process, one update\n")
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(source_root))
    import raven

    if not Path(raven.__file__).resolve().is_relative_to(source_root):
        raise ValueError("Imported Raven did not come from --source-root")
    if mode.startswith("current"):
        from raven import update_source

        module = update_source
    else:
        # Loading only the historical trust/updater modules preserves the
        # original separate-process import and peak-RSS comparison.
        trust = load(
            "raven._baseline_config_trust", workdir / "baseline_config_trust.py"
        )
        raven.config_trust = trust
        sys.modules["raven.config_trust"] = trust
        module = load(
            "raven._baseline_update_source", workdir / "baseline_update_source.py"
        )
    source = workdir / mode
    if mode.endswith("-raw"):
        git(workdir, "clone", str(workdir / "seed"), str(source))
        git(source, "reset", "--hard", "HEAD~1")
    if git(source, "rev-parse", "HEAD") != manifest["fixture_initial_commit"]:
        raise ValueError("Fixture no longer has the original pre-update commit")
    if git(source, "remote", "get-url", "origin") != str(workdir / "seed"):
        raise ValueError("Fixture remote is not the owned local seed")
    if git(source, "status", "--porcelain"):
        raise ValueError("Fixture contains local changes")
    # Equal warm-byte-cache setup. No cold-cache claim is made.
    for path in source.rglob("*.py"):
        path.read_bytes()
    start = time.perf_counter()
    self_before = resource.getrusage(resource.RUSAGE_SELF)
    child_before = resource.getrusage(resource.RUSAGE_CHILDREN)
    result = module.update_source(source, backup_root=workdir / (mode + "-backups"))
    elapsed = time.perf_counter() - start
    self_after = resource.getrusage(resource.RUSAGE_SELF)
    child_after = resource.getrusage(resource.RUSAGE_CHILDREN)

    def delta(field):
        return (
            getattr(self_after, field)
            - getattr(self_before, field)
            + getattr(child_after, field)
            - getattr(child_before, field)
        )

    head = git(source, "rev-parse", "HEAD")
    if not result["changed"] or head != manifest["fixture_target_commit"]:
        raise ValueError("The measured update did not reach the exact expected commit")
    payload = payload_digest(source)
    if payload != manifest["payload_sha256"] or git(source, "status", "--porcelain"):
        raise ValueError("The updated payload differs from the exact upstream fixture")
    measurement = {
        "mode": mode,
        "seconds": elapsed,
        "files": FILES,
        "process_max_rss_kib": self_after.ru_maxrss,
        "child_max_rss_kib": child_after.ru_maxrss,
        "cpu_user_seconds": delta("ru_utime"),
        "cpu_system_seconds": delta("ru_stime"),
        "input_blocks_512_bytes": delta("ru_inblock"),
        "output_blocks_512_bytes": delta("ru_oublock"),
        "changed": result["changed"],
        "head": head,
        "payload_sha256": payload,
    }
    write_new(workdir / (mode + ".json"), encode(measurement))
    return measurement


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", *MODES))
    parser.add_argument(
        "--source-root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument(
        "--workdir",
        type=Path,
        default=Path(tempfile.gettempdir())
        / ("raven-install-review-" + str(os.getuid())),
        help="fresh private fixture directory; prepare refuses an existing path",
    )
    parser.add_argument("--baseline", default="b98742f", help="historical Git revision")
    args = parser.parse_args(argv)
    if sys.platform != "linux":
        parser.error("These recorded RSS/I/O units require Linux")
    source_root = args.source_root.expanduser().resolve(strict=True)
    # Resolve the chosen parent once, but never follow an existing final root.
    selected = args.workdir.expanduser().absolute()
    workdir = selected.parent.resolve(strict=True) / selected.name
    try:
        if args.mode == "prepare":
            value = prepare(source_root, workdir, args.baseline)
        else:
            manifest = validate(workdir, source_root, args.baseline)
            value = measure(args.mode, source_root, workdir, manifest)
    except (OSError, TypeError, ValueError, subprocess.CalledProcessError) as exc:
        parser.exit(1, "Benchmark refused: " + str(exc) + "\n")
    print(json.dumps(value, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
