"""Standalone bounded diagnostic driver, also staged into reviewed Slurm jobs.

Only explicit recipes execute. A private run claim is committed before any
child starts. Interrupted work is never silently retried. This process boundary
is not a security sandbox: the recipe is trusted user code.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import random
import selectors
import signal
import stat
import subprocess
import sys
import tarfile
import time
import io
import statistics
import threading


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def _json(data):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError("Duplicate diagnostic JSON key")
            result[key] = value
        return result
    try:
        return json.loads(data, object_pairs_hook=pairs,
                          parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Nonfinite diagnostic JSON")))
    except (RecursionError, UnicodeError) as exc:
        raise ValueError("Diagnostic JSON must be bounded UTF-8") from exc


def _atomic(path, value):
    data = (json.dumps(value, sort_keys=True, allow_nan=False) + "\n").encode()
    if len(data) > 4 << 20:
        raise ValueError("Diagnostic receipt exceeds 4 MiB")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    directory = os.open(Path(path).parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _read(path, maximum=4 << 20):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
            raise ValueError("Diagnostic input must be a bounded regular file")
        data = stream.read(maximum+1)
        after = os.fstat(stream.fileno())
        signature = lambda v: (v.st_dev, v.st_ino, v.st_mtime_ns, v.st_ctime_ns, v.st_size)
        if len(data) > maximum or signature(before) != signature(after) or signature(after) != signature(os.stat(path, follow_symlinks=False)):
            raise ValueError("Diagnostic input changed during reading")
    return data


def _object(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= value.keys() or set(value) - set(required) - set(optional):
        raise ValueError("Diagnostic document has missing or unknown fields")
    return value


def _integer(value, low, high):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"Expected an integer within {low}..{high}")
    return value


def _number(value, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Expected a finite numeric limit")
    try:
        valid = math.isfinite(value) and low <= value <= high
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError(f"Expected a finite number within {low}..{high}")
    return value


def _name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", value):
        raise ValueError("Diagnostic IDs use 1..80 letters, digits, dots, dashes or underscores")
    return value


def _arguments(value):
    if not isinstance(value, list) or len(value) > 128 or any(not isinstance(v, str) or len(v) > 4096 or "\x00" in v for v in value):
        raise ValueError("Arguments must be a bounded string array")
    return value


def validate(recipe):
    _object(recipe, {"schema", "id", "mode", "kind", "command", "files", "limits", "oracle"}, {"cases", "input", "partition", "account", "repository"})
    if recipe["schema"] != "raven.diagnostic-campaign/v1" or recipe["mode"] not in {"local-diagnostic", "slurm"} or recipe["kind"] not in {"campaign", "minimize", "regression"}:
        raise ValueError("Expected a diagnostic-campaign/v1 with explicit local-diagnostic/slurm mode and campaign/minimize/regression kind")
    _name(recipe["id"])
    argv = _arguments(recipe["command"])
    if not argv or not Path(argv[0]).is_absolute() or any(c in argv[0] for c in "{}\n\r"):
        raise ValueError("Command must begin with one explicit absolute executable path")
    limits = _object(recipe["limits"], {"max_trials", "trial_seconds", "total_seconds", "cpus", "memory_mb", "output_bytes", "repeats"})
    _integer(limits["max_trials"], 1, 256)
    _integer(limits["cpus"], 1, 8)
    _integer(limits["memory_mb"], 32, 65536)
    _integer(limits["output_bytes"], 1024, 1 << 20)
    _integer(limits["repeats"], 1, 10)
    _number(limits["trial_seconds"], .05, 120)
    _number(limits["total_seconds"], .05, 3600)
    if limits["trial_seconds"] > limits["total_seconds"] or limits["repeats"] > limits["max_trials"]:
        raise ValueError("Per-trial/repetition limits exceed the campaign budget")
    oracle = _object(recipe["oracle"], {"exit_code", "contains"})
    _integer(oracle["exit_code"], 0, 255)
    if not isinstance(oracle["contains"], str) or not oracle["contains"] or len(oracle["contains"]) > 1024 or "\x00" in oracle["contains"]:
        raise ValueError("An explicit nonempty output signature is required; arbitrary nonzero exits are not an oracle")
    files = recipe["files"]
    if not isinstance(files, list) or len(files) > 32:
        raise ValueError("Declare at most 32 source files")
    names = set()
    for entry in files:
        _object(entry, {"name", "path", "sha256"})
        if _name(entry["name"]) in names:
            raise ValueError("Duplicate source name")
        names.add(entry["name"])
        if not isinstance(entry["path"], str) or not entry["path"] or len(entry["path"]) > 4096 or any(c in entry["path"] for c in "\x00\r\n"):
            raise ValueError("Source path must be explicit")
        if not isinstance(entry["sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", entry["sha256"]):
            raise ValueError("Source files require exact SHA256 identities")
    if recipe["kind"] == "campaign":
        if "input" in recipe or "repository" in recipe or not isinstance(recipe.get("cases"), list) or not 1 <= len(recipe["cases"]) <= 256:
            raise ValueError("Campaign requires 1..256 explicit cases")
        seen = set()
        for case in recipe["cases"]:
            _object(case, {"id", "arguments"})
            if _name(case["id"]) in seen:
                raise ValueError("Duplicate case ID")
            seen.add(case["id"])
            _arguments(case["arguments"])
    elif recipe["kind"] == "minimize":
        if "cases" in recipe or "repository" in recipe or recipe.get("input") not in names or "{input}" not in argv:
            raise ValueError("Minimization requires a named declared input and a literal {input} argument")
    else:
        if "cases" in recipe or "input" in recipe or "{result}" not in argv or limits["repeats"] < 3:
            raise ValueError("Regression sweeps require {result}, at least three repeats, and no cases/input")
        repository = _object(recipe.get("repository"), {"path", "commits", "baseline", "minimum_change", "metric", "confidence"})
        if not isinstance(repository["path"], str) or not repository["path"] or len(repository["path"]) > 4096 or any(c in repository["path"] for c in "\x00\r\n"):
            raise ValueError("Repository path must be explicit")
        if not isinstance(repository["commits"], list) or not 2 <= len(repository["commits"]) <= 8:
            raise ValueError("A regression sweep supports 2..8 explicit commits")
        revision_ids, commit_ids = set(), set()
        for revision in repository["commits"]:
            _object(revision, {"id", "commit"})
            key = _name(revision["id"])
            commit = revision["commit"]
            if key in revision_ids or not isinstance(commit, str) or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", commit) or commit in commit_ids:
                raise ValueError("Revisions require unique IDs and full immutable commit hashes")
            revision_ids.add(key)
            commit_ids.add(commit)
        if repository["baseline"] != repository["commits"][0]["id"]:
            raise ValueError("The first explicit commit must be the baseline")
        _number(repository["minimum_change"], 0, 1e9)
        _number(repository["confidence"], .8, .95)
        if repository["metric"] not in {"wall_seconds", "result_seconds"}:
            raise ValueError("Regression metric is wall_seconds or result_seconds")
    all_arguments = list(argv)
    for case in recipe.get("cases", []):
        all_arguments.extend(case["arguments"])
    for arg in all_arguments:
        if arg.startswith("{file:") and arg.endswith("}"):
            if arg[6:-1] not in names:
                raise ValueError("Command references an undeclared source")
        elif arg.startswith("{repo:") and arg.endswith("}"):
            relative = Path(arg[6:-1])
            if recipe["kind"] != "regression" or not relative.parts or relative.is_absolute() or any(p in {".", ".."} for p in relative.parts):
                raise ValueError("Repository placeholder must name a relative path in a regression snapshot")
        elif "{" in arg or "}" in arg:
            if arg not in {"{input}", "{result}"}:
                raise ValueError("Placeholders must occupy an entire argument")
            if arg == "{input}" and recipe["kind"] != "minimize":
                raise ValueError("Only minimization has an input placeholder")
    for field in ("partition", "account"):
        if field in recipe:
            _name(recipe[field])
    return recipe


def archive_bytes(repository, commit, *, cancel=None, timeout=15, limit=4 << 20):
    """Export one immutable commit without changing refs/index/worktree or running hooks."""
    if not isinstance(commit, str) or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", commit):
        raise ValueError("Git archive requires a full commit hash")
    process = None
    chunks = bytearray()
    error = bytearray()
    started = time.monotonic()
    try:
        process = subprocess.Popen(["git", "-c", "core.hooksPath=/dev/null", "-c", "core.attributesFile=/dev/null",
                                    "-C", str(repository), "archive", "--format=tar", commit],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, start_new_session=True)
        with selectors.DefaultSelector() as selector:
            for stream, target in ((process.stdout, chunks), (process.stderr, error)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, target)
            while selector.get_map():
                if cancel is not None and cancel.is_set():
                    raise ValueError("Git snapshot inspection cancelled")
                if time.monotonic()-started >= timeout:
                    raise ValueError("Git snapshot exceeded its time budget")
                for key, _ in selector.select(.025):
                    data = os.read(key.fd, 65536)
                    if not data:
                        selector.unregister(key.fileobj)
                    else:
                        key.data.extend(data)
                        if len(chunks)+len(error) > limit:
                            raise ValueError("Git snapshot exceeds the 4 MiB per-revision budget")
        reason = wait_for_exit(process, started + timeout, cancel)
        if reason == "cancelled":
            raise ValueError("Git snapshot inspection cancelled")
        if reason == "timeout":
            raise ValueError("Git snapshot exceeded its time budget")
        if process.returncode:
            raise ValueError("Git snapshot failed: " + error.decode("utf-8", "replace")[:1024])
        # Git archive's pax global comment carries the exact commit identity.
        data = bytes(chunks)
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
            if archive.pax_headers.get("comment") != commit:
                raise ValueError("Git archive did not identify the requested commit (tree IDs are not accepted)")
            _members(archive)
        return data
    except (OSError, subprocess.TimeoutExpired, tarfile.TarError) as exc:
        raise ValueError("Git snapshot could not complete within its declared scope: " + str(exc)[:512]) from exc
    finally:
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
            process.stdout.close()
            process.stderr.close()


def _members(archive):
    members = archive.getmembers()
    if len(members) > 10000:
        raise ValueError("Git snapshot has more than 10000 entries")
    total = 0
    seen = set()
    for member in members:
        name = Path(member.name)
        if not name.parts or name.is_absolute() or any(p in {".", ".."} for p in name.parts) or "\x00" in member.name:
            raise ValueError("Git snapshot contains a path outside its private directory")
        if member.name in seen or not (member.isfile() or member.isdir()):
            raise ValueError("Git snapshot links, special entries and duplicate paths are unsupported")
        seen.add(member.name)
        total += member.size
        if member.size < 0 or total > 4 << 20:
            raise ValueError("Git snapshot content exceeds its budget")
    return members


def extract_snapshot(data, destination):
    destination = Path(destination)
    destination.mkdir(mode=0o700)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
        members = _members(archive)
        for member in members:
            target = destination/member.name
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True, mode=0o700)
            else:
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with archive.extractfile(member) as source, target.open("xb") as output:
                    output.write(source.read(member.size))
                target.chmod(0o700 if member.mode & 0o111 else 0o600)


def regression_summary(repository, measurements):
    """Seeded bootstrap medians, explicit family adjustment, conservative abstention."""
    baseline = repository["baseline"]
    reference = measurements[baseline]
    comparisons = []
    alpha = (1-repository["confidence"])/(len(repository["commits"])-1)
    threshold = repository["minimum_change"]
    for revision in repository["commits"][1:]:
        values = measurements[revision["id"]]
        a = [r["value"] for r in reference if r["value"] is not None and r["scientific_valid"]]
        b = [r["value"] for r in values if r["value"] is not None and r["scientific_valid"]]
        interval = None
        difference = statistics.median(b)-statistics.median(a) if a and b else None
        if len(a) != len(reference) or len(b) != len(values) or min(len(a), len(b)) < 3:
            state = "inconclusive"
        elif len(set(a)) < 2 or len(set(b)) < 2:
            state = "inconclusive"
        else:
            rng = random.Random(_hash({"baseline": a, "candidate": b, "commit": revision["commit"]}))
            draws = sorted(statistics.median(rng.choices(b, k=len(b)))-statistics.median(rng.choices(a, k=len(a))) for _ in range(2000))
            interval = [draws[max(0, math.floor(alpha/2*len(draws)))], draws[min(len(draws)-1, math.ceil((1-alpha/2)*len(draws))-1)]]
            state = "regression" if interval[0] > threshold else "improvement" if interval[1] < -threshold else "within-declared-margin" if interval[0] >= -threshold and interval[1] <= threshold else "inconclusive"
        comparisons.append({"id": revision["id"], "commit": revision["commit"], "state": state,
                            "difference": difference, "interval": interval, "reference_n": len(a), "candidate_n": len(b)})
    regressed, nonmonotonic = False, False
    for row in comparisons:
        if row["state"] == "regression":
            regressed = True
        elif regressed and row["state"] in {"improvement", "within-declared-margin"}:
            nonmonotonic = True
    return {"comparisons": comparisons, "nonmonotonic": nonmonotonic,
            "first_observed_regression": next((row["id"] for row in comparisons if row["state"] == "regression"), None),
            "method": "2000 seeded independent bootstrap median differences; Bonferroni-adjusted percentile intervals",
            "limits": "Small samples and host noise limit bootstrap coverage. Declared commit order need not be monotonic. No causal commit attribution or automatic bisect is claimed."}


def _child(limits, argv):
    """Single-threaded resource supervisor; reap a trial if its driver dies."""
    import ctypes
    import resource
    if sys.platform != "linux":
        raise ValueError("The diagnostic resource supervisor requires Linux")
    def parent_died(*_):
        os.killpg(os.getpgrp(), signal.SIGKILL)
    signal.signal(signal.SIGTERM, parent_died)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "Cannot establish diagnostic parent-death cleanup")
    if os.getppid() != limits["driver_pid"]:
        parent_died()
    def restrict(kind, soft, hard):
        # A diagnostic nested inside an already-governed worker cannot raise
        # either inherited ceiling. In particular CPU soft30/hard30 cannot be
        # changed to the otherwise useful soft30/hard31 grace pair.
        inherited_soft, inherited_hard = resource.getrlimit(kind)
        if inherited_hard != resource.RLIM_INFINITY:
            hard = min(hard, inherited_hard)
        if inherited_soft != resource.RLIM_INFINITY:
            soft = min(soft, inherited_soft)
        resource.setrlimit(kind, (min(soft, hard), hard))
    restrict(resource.RLIMIT_CORE, 0, 0)
    cpu = max(1, math.ceil(limits["trial_seconds"]))
    restrict(resource.RLIMIT_CPU, cpu, cpu + 1)
    memory = limits["memory_mb"] * 1024 * 1024
    restrict(resource.RLIMIT_AS, memory, memory)
    restrict(resource.RLIMIT_FSIZE, limits["output_bytes"], limits["output_bytes"])
    if hasattr(os, "sched_getaffinity"):
        available = sorted(os.sched_getaffinity(0))
        os.sched_setaffinity(0, available[:limits["cpus"]])
    for key in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ[key] = str(limits["cpus"])
    child = subprocess.Popen(argv, stdin=subprocess.DEVNULL)
    returncode = child.wait()
    if returncode < 0:
        os.kill(os.getpid(), -returncode)
    raise SystemExit(returncode)


def _execute(argv, directory, limits, timeout, cancel=None):
    started = time.monotonic()
    data = bytearray()
    process = None
    reason = None
    try:
        if cancel is not None and cancel.is_set():
            return {"returncode": None, "state": "cancelled", "seconds": 0.0, "output": ""}
        if timeout <= 0:
            return {"returncode": None, "state": "timeout", "seconds": 0.0, "output": ""}
        process = subprocess.Popen([sys.executable, "-I", "-S", str(Path(__file__).resolve()), "--child", json.dumps({**limits, "driver_pid": os.getpid()}), *argv],
                                   cwd=directory, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        os.set_blocking(process.stdout.fileno(), False)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                if cancel is not None and cancel.is_set():
                    reason = "cancelled"
                    break
                remaining = timeout-(time.monotonic()-started)
                if remaining <= 0:
                    reason = "timeout"
                    break
                for key, _ in selector.select(min(.025, remaining)):
                    chunk = os.read(key.fd, min(16384, limits["output_bytes"]+1-len(data)))
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        data.extend(chunk)
                        if len(data) > limits["output_bytes"]:
                            reason = "output-limit"
                            break
                if reason:
                    break
        if reason is None:
            reason = wait_for_exit(process, started + timeout, cancel)
        return {"returncode": process.returncode if reason is None else None,
                "state": reason or "finished", "seconds": time.monotonic()-started,
                "output": data[:limits["output_bytes"]].decode("utf-8", "replace")}
    except OSError as exc:
        return {"returncode": None, "state": "launch-error", "seconds": time.monotonic()-started, "output": str(exc)[:1024]}
    finally:
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
            process.stdout.close()


def wait_for_exit(process, deadline, cancel=None, interval=.025):
    """Keep cancellation live after a child closes all captured output pipes.

    Pipe EOF is not process completion. Waiting for the entire remaining
    deadline in that state previously left cancellation unobserved for up to
    a full trial or command budget. The helper is shared by reviewed commands,
    Git snapshots and diagnostic children, without creating polling threads.
    """
    while True:
        if cancel is not None and cancel.is_set():
            return "cancelled"
        if process.poll() is not None:
            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "timeout"
        try:
            process.wait(timeout=min(interval, remaining))
        except subprocess.TimeoutExpired:
            continue


class TrialBudget:
    """Monotonic admission shared by every diagnostic trial algorithm."""

    def __init__(self, limits, cancel=None):
        self.limits = dict(limits)
        self.cancel = cancel
        self.started = time.monotonic()
        self.attempts = 0

    def remaining(self):
        return max(0.0, self.limits["total_seconds"] - (time.monotonic() - self.started))

    def stopped(self):
        if self.cancel is not None and self.cancel.is_set():
            return "cancelled"
        if self.remaining() <= 0:
            return "timeout"
        return None

    def available(self):
        return self.attempts < self.limits["max_trials"] and self.stopped() is None


class TrialLifecycle:
    """Serial preparation, bounded execution, measurement and durable receipt.

    Algorithms supply preparation and measurement callbacks. They do not own
    admission, sequence assignment, deadline checks or publication ordering.
    The code stays in this standalone module so reviewed Slurm workspaces
    continue to execute with Python ``-I -S`` and no Raven installation.
    """

    def __init__(self, root, limits, verify, cancel=None):
        self.root = Path(root)
        self.budget = TrialBudget(limits, cancel)
        self.verify = verify
        self.trials = []
        self._failed = False
        self._lock = threading.Lock()
        (self.root/"trials").mkdir(mode=0o700)

    def perform(self, prepare, execute, measure):
        """Return a committed result, or None if no trial can be admitted.

        ``prepare(directory, sequence)`` returns an intent and execution data.
        ``execute(data, timeout)`` returns an observed process outcome.
        ``measure(directory, sequence, outcome)`` returns the receipt payload.
        A failed publication blocks this lifecycle; a retry needs a new review.
        """
        with self._lock:
            if self._failed:
                raise ValueError("Trial lifecycle was interrupted; inspect receipts before a new review")
            if not self.budget.available():
                return None
            try:
                self.verify()
                if not self.budget.available():
                    return None
                sequence = self.budget.attempts
                self.budget.attempts += 1
                directory = self.root/"trials"/f"{sequence:04d}"
                directory.mkdir(mode=0o700)
                intent, prepared = prepare(directory, sequence)
                _atomic(directory/"intent.json", intent)
                # Staging or fsync can exhaust a budget or receive a cancel.
                # Record that outcome without starting an unbudgeted child.
                reason = self.budget.stopped()
                if reason is None:
                    outcome = execute(prepared, min(self.budget.limits["trial_seconds"], self.budget.remaining()))
                else:
                    outcome = {"returncode": None, "state": reason, "seconds": 0.0, "output": ""}
                result = measure(directory, sequence, outcome)
                _atomic(directory/"result.json.receipt", result)
                self.trials.append(result)
                self.verify()
                return result
            except BaseException:
                self._failed = True
                raise


def repeated_trials(callback, repeats):
    """Do not run more repetitions after an unavailable/infrastructure result."""
    answers = []
    for _ in range(repeats):
        answer = callback()
        answers.append(answer)
        if answer is None:
            break
    return answers


def minimize(items, test):
    """Deterministic complement delta debugging; test returns True/False/None.

    None means budget, timeout or inconsistent oracle, and stops reduction.
    Each accepted candidate preserves the explicitly tested property.
    """
    current = list(items)
    if test(current) is not True:
        return {"items": current, "state": "original-not-confirmed", "one_minimal": False}
    empty = test([])
    if empty is None:
        return {"items": current, "state": "inconclusive", "one_minimal": False}
    if empty:
        return {"items": [], "state": "reduced", "one_minimal": True}
    granularity = 2
    while len(current) >= 2:
        chunk = math.ceil(len(current)/granularity)
        reduced = False
        for start in range(0, len(current), chunk):
            candidate = current[:start] + current[start+chunk:]
            answer = test(candidate)
            if answer is None:
                return {"items": current, "state": "inconclusive", "one_minimal": False}
            if answer:
                current = candidate
                granularity = max(2, granularity-1)
                reduced = True
                break
        if reduced:
            continue
        if granularity >= len(current):
            return {"items": current, "state": "reduced", "one_minimal": True}
        granularity = min(len(current), granularity*2)
    return {"items": current, "state": "reduced", "one_minimal": True}


def run(recipe, workspace, *, cancel=None):
    validate(recipe)
    root = Path(workspace).absolute()
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Driver requires its staged private workspace")
    if recipe["mode"] == "slurm" and not os.environ.get("SLURM_JOB_ID"):
        raise ValueError("Slurm diagnostic recipe cannot execute outside its allocation")
    sources = root / "sources"
    execution = _json(_read(root/"execution.json"))
    executable = execution["executable"]
    def verify_sources():
        for entry in recipe["files"]:
            data = _read(sources/entry["name"], 8 << 20)
            if hashlib.sha256(data).hexdigest() != entry["sha256"]:
                raise ValueError("Staged source changed during diagnostic execution")
        for record in (execution["executable"], execution["python"]):
            data = _read(record["path"], 64 << 20)
            if hashlib.sha256(data).hexdigest() != record["sha256"]:
                raise ValueError("Diagnostic executable changed after review")
    verify_sources()
    # Refuse a second invocation, even after a crash with no final receipt.
    _atomic(root/"run.claim.json", {"schema": "raven.diagnostic-claim/v1", "recipe_sha256": _hash(recipe),
                                    "created": time.time(), "pid": os.getpid(), "job_id": os.environ.get("SLURM_JOB_ID")})
    limits, cached = recipe["limits"], {}
    lifecycle = TrialLifecycle(root, limits, verify_sources, cancel)
    started, trials = lifecycle.budget.started, lifecycle.trials

    def trial(arguments, candidate=None, revision=None, case_id=None):
        def prepare(directory, sequence):
            checkout = None
            if revision is not None:
                archive_data = _read(root/"revisions"/(revision["id"]+".tar"), 4 << 20)
                expected = execution["revisions"][revision["id"]]
                if hashlib.sha256(archive_data).hexdigest() != expected["sha256"] or expected["commit"] != revision["commit"]:
                    raise ValueError("Staged Git snapshot changed")
                checkout = directory/"checkout"
                extract_snapshot(archive_data, checkout)
            if candidate is not None:
                (directory/"input.txt").write_bytes(candidate)
            argv = []
            for item in recipe["command"] + arguments:
                if item == "{input}":
                    argv.append(str(directory/"input.txt"))
                elif item == "{result}":
                    argv.append(str(directory/"result.json"))
                elif item.startswith("{file:") and item.endswith("}"):
                    argv.append(str(sources/item[6:-1]))
                elif item.startswith("{repo:") and item.endswith("}") and checkout is not None:
                    argv.append(str(checkout/item[6:-1]))
                else:
                    argv.append(item)
            argv[0] = executable["path"]
            intent = {"sequence": sequence, "arguments": argv, "input_sha256": hashlib.sha256(candidate).hexdigest() if candidate is not None else None}
            if case_id is not None:
                intent["case_id"] = case_id
            return intent, (argv, checkout or directory)

        def execute(prepared, timeout):
            argv, directory = prepared
            return _execute(argv, directory, limits, timeout, cancel)

        def measure(directory, sequence, result):
            output = result.pop("output")
            (directory/"output.log").write_text(output, encoding="utf-8")
            matched = result["state"] == "finished" and result["returncode"] == recipe["oracle"]["exit_code"] and recipe["oracle"]["contains"] in output
            result.update(sequence=sequence, oracle_matched=matched,
                          output_sha256=hashlib.sha256(output.encode()).hexdigest())
            if case_id is not None:
                result["case_id"] = case_id
                if "{result}" in recipe["command"]:
                    try:
                        raw_result = _read(directory/"result.json", min(limits["output_bytes"], 65536))
                        application = _json(raw_result)
                        if not isinstance(application, dict):
                            raise ValueError("Campaign result must be a JSON object")
                        result["application"] = application
                        result["application_sha256"] = hashlib.sha256(raw_result).hexdigest()
                    except (OSError, ValueError, TypeError) as exc:
                        result["application_error"] = str(exc)[:512]
            if revision is not None:
                measurement, science, error = None, False, None
                try:
                    observed = _json(_read(directory/"result.json", limits["output_bytes"]))
                    if not isinstance(observed, dict) or observed.get("scientific_valid") is not True:
                        raise ValueError("Missing or failed application scientific check")
                    value = result["seconds"] if recipe["repository"]["metric"] == "wall_seconds" else observed.get("seconds")
                    measurement = _number(value, 0, 1e9)
                    science = matched
                except (ValueError, OSError, TypeError) as exc:
                    error = str(exc)[:512]
                result.update(revision=revision["id"], commit=revision["commit"],
                              measurement={"value": measurement, "scientific_valid": science, "error": error})
            return result

        result = lifecycle.perform(prepare, execute, measure)
        # Signals/launch failures are infrastructure/unsupported outcomes,
        # not evidence against a failure or a new scientific observation.
        if result is None or result["state"] != "finished" or result["returncode"] is None or result["returncode"] < 0:
            return None
        return result["oracle_matched"]

    try:
        if recipe["kind"] == "campaign":
            cases = []
            state = "completed"
            for case in recipe["cases"]:
                answers = repeated_trials(lambda: trial(case["arguments"], case_id=case["id"]), limits["repeats"])
                case_state = "inconclusive" if None in answers or len(set(answers)) > 1 else "matched" if all(answers) else "not-matched"
                cases.append({"id": case["id"], "state": case_state})
                if None in answers:
                    state = "partial"
                    break
            result = {"state": state, "cases": cases}
        elif recipe["kind"] == "minimize":
            original = _read(sources/recipe["input"], 1 << 20)
            lines = original.splitlines(keepends=True)
            if len(lines) > 10000:
                raise ValueError("Minimizer supports at most 10000 input lines")
            def test(candidate):
                data = b"".join(candidate)
                key = hashlib.sha256(data).hexdigest()
                if key in cached:
                    return cached[key]
                answers = repeated_trials(lambda: trial([], data), limits["repeats"])
                answer = None if None in answers or len(set(answers)) > 1 else all(answers)
                cached[key] = answer
                return answer
            reduced = minimize(lines, test)
            result_bytes = b"".join(reduced.pop("items"))
            (root/"minimal-input.txt").write_bytes(result_bytes)
            result = {**reduced, "original_bytes": len(original), "reduced_bytes": len(result_bytes),
                      "reduced_sha256": hashlib.sha256(result_bytes).hexdigest(),
                      "scope": "line deletion under the declared repeated exit-code/output-signature oracle; not a globally smallest or universally reproducible case"}
        else:
            repository = recipe["repository"]
            measurements = {revision["id"]: [] for revision in repository["commits"]}
            state = "completed"
            # Alternate direction to reduce fixed chronological ordering bias.
            for repeat in range(limits["repeats"]):
                revisions = repository["commits"] if repeat % 2 == 0 else list(reversed(repository["commits"]))
                for revision in revisions:
                    before = len(trials)
                    answer = trial([], revision=revision)
                    if len(trials) > before:
                        measurements[revision["id"]].append(trials[-1]["measurement"])
                    if answer is None:
                        state = "partial"
                        break
                if state == "partial":
                    break
            result = {"state": state, "measurements": measurements, **regression_summary(repository, measurements)}
        result.update(schema="raven.diagnostic-result/v1", recipe_sha256=_hash(recipe), trials=trials,
                      seconds=time.monotonic()-started, job_id=os.environ.get("SLURM_JOB_ID"))
        _atomic(root/"result.json", result)
        return result
    except BaseException as exc:
        _atomic(root/"error.json", {"state": "interrupted", "error": str(exc)[:2048], "trials_committed": len(trials),
                                    "reconciliation": "Read trial receipts. Do not rerun this workspace; create an explicitly reviewed new campaign."})
        raise


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "--child":
        _child(json.loads(argv[1]), argv[2:])
        return 1
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recipe")
    parser.add_argument("workspace")
    args = parser.parse_args(argv)
    recipe = _json(_read(args.recipe))
    cancel = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: cancel.set())
    signal.signal(signal.SIGINT, lambda *_: cancel.set())
    # Publish after installing handlers: cancellation cannot race an unhandled TERM.
    pid = os.getpid()
    status = Path(f"/proc/{pid}/stat").read_text()
    start = status[status.rfind(")")+2:].split()[19]
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    _atomic(Path(args.workspace)/"driver.process.json", {"pid": pid, "start": start, "boot": boot,
                                                        "recipe_sha256": _hash(recipe)})
    print(json.dumps(run(recipe, args.workspace, cancel=cancel), sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
