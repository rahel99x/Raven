"""Bounded, local submission planning. Preparing a plan never runs its script or Slurm.

Only ``preview`` and ``submit`` contact the scheduler, and only when called explicitly.
The caller must obtain confirmation before calling ``submit``. Unknown site flags are
reported rather than guessed; plans can still be inspected when preflight fails.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shlex
import stat
from pathlib import Path
from typing import Any, Mapping, Sequence

SCHEMA = "raven.submission-plan/v1"
MAX_SCRIPT_BYTES = 8 << 20
MAX_PLAN_BYTES = 2 << 20
MAX_OPTIONS = 256
MAX_PATHS = 256
MAX_INPUTS = 128
MAX_VALUE = 65536
MAX_COMMAND_BYTES = 128 << 10
MAX_ISSUES = 128
MAX_METADATA_DEPTH = 20

# Explicitly supported options. Modes which change what sbatch executes or hide
# its receipt are deliberately excluded (wrap, wait, quiet, help, test-only).
VALUE_OPTIONS = {
    "account", "array", "begin", "chdir", "comment", "constraint", "container",
    "container-id", "cpus-per-task", "cpus-per-gpu", "deadline", "dependency",
    "distribution", "error", "exclude", "export", "gpu-bind", "gpu-freq", "gpus",
    "gpus-per-node", "gpus-per-socket", "gpus-per-task", "gres", "hint", "input",
    "job-name", "licenses", "mail-type", "mail-user", "mem", "mem-per-cpu",
    "mem-per-gpu", "nodes", "nodelist", "ntasks", "ntasks-per-core",
    "ntasks-per-gpu", "ntasks-per-node", "ntasks-per-socket", "open-mode",
    "partition", "qos", "reservation", "signal", "threads-per-core", "time",
    "time-min", "tmp", "wckey",
}
SWITCHES = {"exclusive", "hold", "no-requeue", "requeue", "overcommit", "oversubscribe", "parsable"}
SHORT = {"A": "account", "a": "array", "c": "cpus-per-task", "C": "constraint",
         "d": "dependency", "D": "chdir", "e": "error", "i": "input", "J": "job-name",
         "N": "nodes", "n": "ntasks", "o": "output", "p": "partition", "q": "qos",
         "t": "time", "w": "nodelist", "x": "exclude"}
VALUE_OPTIONS.add("output")
PLACEHOLDER = re.compile(r"\$\{[^}]*\}|\$[A-Za-z_][A-Za-z_0-9]*|\{\{[^}]*\}\}|<[A-Z][A-Z_0-9]*>")
CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _issue(issues: list, code: str, message: str, level: str = "error") -> None:
    if len(issues) < MAX_ISSUES:
        issues.append({"level": level, "code": code, "message": message})
    elif level == "error" and not any(item["level"] == "error" for item in issues):
        # Never let a large number of warnings hide a later blocking error.
        issues[-1] = {"level": "error", "code": "issues_truncated", "message": "additional preflight issue: " + message}


def _read_script(path: Path) -> bytes:
    # fstat and a nonblocking open keep device files and FIFOs from hanging.
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError("batch script must be a regular file")
        data = handle.read(MAX_SCRIPT_BYTES + 1)
    if len(data) > MAX_SCRIPT_BYTES:
        raise ValueError(f"batch script exceeds {MAX_SCRIPT_BYTES // (1 << 20)} MiB")
    return data


def _options(tokens: Sequence[str], source: str, issues: list, line: int = 0) -> list:
    if len(tokens) > MAX_OPTIONS:
        _issue(issues, "too_many_options", f"{source}: at most {MAX_OPTIONS} option tokens are supported")
        return []
    parsed, i = [], 0
    while i < len(tokens):
        token = tokens[i]
        if not isinstance(token, str) or len(token) > MAX_VALUE or CONTROL.search(token):
            _issue(issues, "invalid_option", f"{source}: option tokens must be bounded strings without control characters")
            i += 1
            continue
        value = None
        if token.startswith("--"):
            name, equals, value = token[2:].partition("=")
            if name not in VALUE_OPTIONS | SWITCHES:
                _issue(issues, "unsupported_option", f"{source}: unsupported option --{name}; use a supported batch-script option")
                i += 1
                continue
            if name in SWITCHES:
                if equals:
                    _issue(issues, "invalid_switch", f"{source}: --{name} takes no value in this workbench")
                value = None
            elif not equals:
                if i + 1 >= len(tokens) or not isinstance(tokens[i + 1], str) or tokens[i + 1].startswith("-"):
                    _issue(issues, "missing_value", f"{source}: --{name} needs a value (use --{name}=VALUE for a leading dash)")
                    i += 1
                    continue
                i += 1
                value = tokens[i]
        elif token.startswith("-") and len(token) > 1 and token[1] in SHORT:
            name = SHORT[token[1]]
            if len(token) > 2:
                value = token[2:]
            elif i + 1 < len(tokens) and isinstance(tokens[i + 1], str) and not tokens[i + 1].startswith("-"):
                i += 1
                value = tokens[i]
            else:
                _issue(issues, "missing_value", f"{source}: {token} needs a value")
                i += 1
                continue
        else:
            _issue(issues, "unexpected_argument", f"{source}: unexpected argument {token!r}; pass only supported sbatch options")
            i += 1
            continue
        if value is not None and (not value or len(value) > MAX_VALUE or CONTROL.search(value)):
            _issue(issues, "invalid_value", f"{source}: --{name} needs a nonempty value without control characters")
            i += 1
            continue
        if value is not None and PLACEHOLDER.search(value):
            _issue(issues, "unexpanded_placeholder", f"{source}: --{name} contains an unexpanded placeholder; Slurm does not expand shell variables in directives")
        parsed.append({"key": name.replace("-", "_"), "flag": "--" + name, "value": value, "source": source, "line": line})
        i += 1
    return parsed


def _directives(text: str, issues: list) -> list:
    active, result, directive_lines, ignored = True, [], 0, 0
    for number, line in enumerate(io.StringIO(text), 1):
        match = re.match(r"^\s*#SBATCH(?:\s+|$)(.*)", line)
        if match:
            if not active:
                ignored += 1
                continue
            directive_lines += 1
            if len(result) >= MAX_OPTIONS or directive_lines > MAX_OPTIONS:
                _issue(issues, "too_many_directives", f"at most {MAX_OPTIONS} directives are supported")
                break
            try:
                tokens = shlex.split(match.group(1), comments=True)
            except ValueError as exc:
                _issue(issues, "directive_syntax", f"line {number}: {exc}")
                continue
            result.extend(_options(tokens, f"line {number}", issues, number))
        elif line.strip() and not line.lstrip().startswith("#"):
            active = False
    if ignored:
        _issue(issues, "ignored_directive", f"{ignored} #SBATCH directive(s) after the first command are ignored by Slurm", "warning")
    return result


def _time(value: str) -> bool:
    if value.lower() in {"infinite", "unlimited"}:
        return True
    if not re.fullmatch(r"[0-9]{1,10}(?:-[0-9]{1,10})?(?::[0-9]{1,10}){0,2}", value):
        return False
    day, separator, clock = value.partition("-")
    bits = [int(x) for x in (clock if separator else day).split(":")]
    if separator and bits[0] >= 24:
        return False
    return all(x < 60 for x in bits[1:])


def _array(value: str) -> int:
    if len(value) > MAX_VALUE or value.count("%") > 1:
        raise ValueError("invalid array expression")
    spec, separator, limit = value.partition("%")
    if separator and (not re.fullmatch(r"[0-9]{1,10}", limit) or not 0 < int(limit) <= 2147483647):
        raise ValueError("array concurrency must be a positive integer")
    parts = spec.split(",")
    if len(parts) > 1024:
        raise ValueError("array expression has more than 1024 segments")
    count = 0
    for part in parts:
        match = re.fullmatch(r"([0-9]{1,10})(?:-([0-9]{1,10})(?::([0-9]{1,10}))?)?", part)
        if not match:
            raise ValueError("use array indices or inclusive START-END[:STEP] ranges")
        first, last, step = int(match[1]), int(match[2] or match[1]), int(match[3] or 1)
        if last < first or step <= 0 or last > 2147483647:
            raise ValueError("array indices must be ordered, nonnegative 32-bit integers with positive steps")
        count += (last - first) // step + 1
    return count


def _validate_resources(resources: dict, issues: list) -> None:
    numeric = {"cpus_per_task", "cpus_per_gpu", "ntasks", "ntasks_per_node", "ntasks_per_core",
               "ntasks_per_gpu", "ntasks_per_socket", "threads_per_core"}
    for key in numeric & resources.keys():
        value = resources[key]
        if not re.fullmatch(r"[0-9]{1,10}", value) or not 0 < int(value) <= 2147483647:
            _issue(issues, "invalid_resource", f"--{key.replace('_', '-')} must be a positive 32-bit integer")
    for key in {"mem", "mem_per_cpu", "mem_per_gpu", "tmp"} & resources.keys():
        if not re.fullmatch(r"\d+(?:\.\d+)?[KMGTkmgt]?", resources[key]):
            _issue(issues, "invalid_memory", f"--{key.replace('_', '-')} needs a nonnegative memory amount, e.g. 32G")
    memory = {"mem", "mem_per_cpu", "mem_per_gpu"} & resources.keys()
    if len(memory) > 1:
        _issue(issues, "memory_conflict", "--mem, --mem-per-cpu, and --mem-per-gpu are mutually exclusive; replace the conflicting script directive")
    if "cpus_per_task" in resources and "cpus_per_gpu" in resources:
        _issue(issues, "cpu_conflict", "--cpus-per-task and --cpus-per-gpu are mutually exclusive")
    if "ntasks_per_gpu" in resources and "gpus_per_task" in resources:
        _issue(issues, "task_gpu_conflict", "--ntasks-per-gpu and --gpus-per-task are mutually exclusive")
    if "nodes" in resources:
        match = re.fullmatch(r"([0-9]{1,10})(?:-([0-9]{1,10}))?", resources["nodes"])
        if not match or not 0 < int(match[1]) <= int(match[2] or match[1]) <= 2147483647:
            _issue(issues, "invalid_nodes", "--nodes needs a positive count or ascending MIN-MAX range")
    for key in {"time", "time_min"} & resources.keys():
        if not _time(resources[key]):
            _issue(issues, "invalid_time", f"--{key.replace('_', '-')} has an invalid Slurm time format")
    gpu_keys = {"gpus", "gpus_per_node", "gpus_per_socket", "gpus_per_task"} & resources.keys()
    for key in gpu_keys:
        if not re.fullmatch(r"(?:[A-Za-z0-9_.-]+:)?[1-9][0-9]{0,9}", resources[key]):
            _issue(issues, "invalid_gpu", f"--{key.replace('_', '-')} needs a GPU count or TYPE:COUNT")
    if resources.get("gres", "").startswith("gpu") and gpu_keys:
        _issue(issues, "gpu_conflict", "use GPU --gres or --gpus* requests, rather than both, to avoid inconsistent GPU allocations")
    if "array" in resources:
        try:
            resources["array_task_count_upper_bound"] = _array(resources["array"])
        except ValueError as exc:
            _issue(issues, "invalid_array", str(exc))
    if len(resources.get("job_name", "").encode("utf-8")) > 128:
        _issue(issues, "invalid_job_name", "job names must fit in 128 UTF-8 bytes")
    if resources.get("open_mode", "append") not in {"append", "truncate"}:
        _issue(issues, "invalid_open_mode", "--open-mode must be append or truncate")


def _path(value: str, directory: Path) -> Path:
    candidate = Path(value).expanduser()
    candidate = directory / candidate if not candidate.is_absolute() else candidate
    try:
        return candidate.resolve()
    except (OSError, RuntimeError, ValueError):
        # Broken/cyclic links and inaccessible components should become ordinary
        # preflight errors during stat/read, rather than crashing preparation.
        return Path(os.path.abspath(candidate))


def _paths(values: Sequence, issues: list, label: str) -> list:
    limit = MAX_INPUTS if label == "inputs" else MAX_PATHS
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence) or len(values) > limit:
        _issue(issues, "invalid_paths", f"{label} must be a sequence with at most {limit} declared paths")
        return []
    result = []
    for item in values:
        value = item.get("path") if isinstance(item, Mapping) else item
        if not isinstance(value, str) or not value or len(value) > MAX_VALUE or CONTROL.search(value) or PLACEHOLDER.search(value):
            _issue(issues, "invalid_path", f"{label} contain an invalid or unexpanded path")
        else:
            result.append(item if isinstance(item, str) else dict(item))
    return result


def _metadata_bound(value: Any, depth_limit: int = MAX_METADATA_DEPTH) -> None:
    stack, count = [(value, 0)], 0
    while stack:
        item, depth = stack.pop()
        count += 1
        if depth > depth_limit or count > 50000:
            raise ValueError("metadata is too deeply nested or has too many values")
        if isinstance(item, dict):
            if len(item) > 10000 or any(not isinstance(key, str) for key in item):
                raise ValueError("metadata objects require bounded string keys")
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, (list, tuple)):
            if len(item) > 10000:
                raise ValueError("metadata lists are too large")
            stack.extend((child, depth + 1) for child in item)


def _digest(plan: dict) -> str:
    # Protect review fields as well as execution arguments. Array retry context
    # may be attached separately, but cannot alter the exact submission plan.
    fields = {key: plan[key] for key in ("schema", "script", "workdir", "argv", "command", "overrides", "script_sha256",
                                       "parameters", "inputs", "outputs", "resources", "directives", "issues", "valid")}
    fields.update({key: plan[key] for key in ("workflow_node_id", "symbolic_dependencies", "workflow_orchestration", "requires_workflow_orchestration", "submittable") if key in plan})
    if isinstance(plan.get("array_retry"), dict) and "manifest" in plan["array_retry"]:
        fields["array_retry"] = plan["array_retry"]
    _metadata_bound(fields, depth_limit=24)
    try:
        data = json.dumps(fields, sort_keys=True, ensure_ascii=True, allow_nan=False).encode()
    except RecursionError as exc:
        raise ValueError("submission plan metadata is excessively nested") from exc
    return hashlib.sha256(data).hexdigest()


def prepare(script: str | os.PathLike, workdir: str | os.PathLike | None = None,
            overrides: Sequence[str] = (), parameters: Mapping | None = None,
            inputs: Sequence = (), outputs: Sequence = ()) -> dict:
    """Inspect local files and return an exact, JSON-serializable submission plan.

    Options are argv tokens, never a shell command. Script directives are honored
    only before the first command, and explicit command-line options win. The
    effective working directory is made explicit in ``--chdir``. No recursive
    path traversal or array expansion is performed.
    """
    issues: list = []
    original_cwd = Path.cwd()
    if CONTROL.search(os.fspath(script)) or (workdir is not None and CONTROL.search(os.fspath(workdir))):
        _issue(issues, "invalid_path", "script and working directory paths must not contain control characters")
    script_path = _path(os.fspath(script), original_cwd)
    data = b""
    try:
        data = _read_script(script_path)
        text = data.decode("utf-8")
    except (OSError, ValueError, UnicodeError) as exc:
        text = ""
        _issue(issues, "script_unreadable", f"cannot inspect batch script: {exc}")
    if not data.startswith(b"#!") or not data.split(b"\n", 1)[0][2:].strip():
        _issue(issues, "missing_shebang", "batch script must start with an interpreter shebang, e.g. #!/bin/bash")
    if b"\x00" in data or b"\r" in data:
        _issue(issues, "script_encoding", "batch script must use text without NUL bytes or Windows CRLF line endings")
    directives = _directives(text, issues)
    if isinstance(overrides, (str, bytes)) or not isinstance(overrides, Sequence):
        _issue(issues, "invalid_overrides", "overrides must be a sequence of argv tokens, not a shell command")
        override_tokens = []
    else:
        override_tokens = list(overrides[:MAX_OPTIONS + 1])
    flags = _options(override_tokens, "overrides", issues)
    resources = {}
    for option in directives + flags:
        key = option["key"]
        if key in resources and resources[key] != option["value"]:
            _issue(issues, "option_override", f"--{key.replace('_', '-')} changed from {resources[key]!r} to {option['value']!r}", "info")
        resources[key] = option["value"]
    directory = _path(os.fspath(workdir) if workdir is not None else resources.get("chdir") or str(original_cwd), original_cwd)
    if not directory.is_dir():
        _issue(issues, "invalid_workdir", f"working directory does not exist: {directory}")
    resources["chdir"] = str(directory)
    _validate_resources(resources, issues)
    declared_inputs = _paths(inputs, issues, "inputs")
    declared_outputs = _paths(outputs, issues, "outputs")
    input_paths, output_paths, hash_bytes = set(), set(), 0
    for value in declared_inputs:
        name = value["path"] if isinstance(value, dict) else value
        path = _path(name, directory)
        input_paths.add(path)
        if not path.exists():
            _issue(issues, "missing_input", f"declared input does not exist: {path}")
        else:
            original = Path(name).expanduser()
            original = original if original.is_absolute() else directory / original
            if not original.is_file():
                _issue(issues, "invalid_input", f"declare regular input files for reproducible fingerprints: {original}")
        if isinstance(value, dict):
            budget = value.get("max_bytes", 64 << 20)
            if (set(value) - {"path", "hash", "max_bytes"} or not isinstance(value.get("hash", False), bool)
                    or not isinstance(budget, int) or isinstance(budget, bool) or not 0 <= budget <= 64 << 20):
                _issue(issues, "invalid_input_declaration", "input declarations accept path, boolean hash, and a 0..64 MiB integer max_bytes budget")
            elif value.get("hash", False) and path.is_file():
                size = path.stat().st_size
                hash_bytes += size
                if size > budget or hash_bytes > 64 << 20:
                    _issue(issues, "input_hash_budget", "declared input exceeds its hash budget or the aggregate 64 MiB budget")
    for value in declared_outputs:
        path = _path(value["path"] if isinstance(value, dict) else value, directory)
        if path in output_paths or path in input_paths or path == script_path:
            _issue(issues, "output_collision", f"declared output would overwrite an input, script, or duplicate output: {path}")
        output_paths.add(path)
        if path.exists():
            _issue(issues, "output_exists", f"declared output already exists: {path}", "warning")
        if not path.parent.is_dir():
            _issue(issues, "missing_output_parent", f"declared output parent does not yet exist: {path.parent}", "warning")
    for key in ("output", "error", "input"):
        value = resources.get(key)
        if not value:
            continue
        if key == "input" and not _path(value, directory).is_file():
            _issue(issues, "missing_stdin", f"--input file does not exist: {value}")
        if key != "input":
            if "array" in resources and not re.search(r"(?<!%)%(?:\d+)?[aj]", value):
                _issue(issues, "array_log_collision", f"--{key} lacks %a or %j; array tasks may write to the same log", "warning")
            if "%" not in value:
                path = _path(value, directory)
                stdin_path = _path(resources["input"], directory) if resources.get("input") else None
                if path in input_paths or path == script_path or path == stdin_path:
                    _issue(issues, "output_collision", f"--{key} would overwrite a declared input or the script: {path}")
                if not path.parent.is_dir():
                    _issue(issues, "missing_log_parent", f"--{key} parent does not exist before the job starts: {path.parent}")
            elif "%" not in str(Path(value).parent) and not _path(value, directory).parent.is_dir():
                _issue(issues, "missing_log_parent", f"--{key} parent does not exist before the job starts: {Path(value).parent}")
    inherited = sorted(key for key in os.environ if key.startswith("SBATCH_"))
    if inherited:
        _issue(issues, "inherited_sbatch_options", "unset inherited sbatch options before planning: " + ", ".join(inherited))
    try:
        if parameters is not None and not isinstance(parameters, Mapping):
            raise ValueError("parameters must be a JSON object")
        params = dict(parameters or {})
        _metadata_bound({"parameters": params, "inputs": declared_inputs, "outputs": declared_outputs})
        metadata = json.dumps({"parameters": params, "inputs": declared_inputs, "outputs": declared_outputs}, allow_nan=False)
        if len(metadata.encode()) > MAX_PLAN_BYTES:
            raise ValueError("declared metadata exceeds size limit")
        detached = json.loads(metadata)
        params, declared_inputs, declared_outputs = detached["parameters"], detached["inputs"], detached["outputs"]
    except (TypeError, ValueError, RecursionError) as exc:
        _issue(issues, "invalid_metadata", f"parameters and path declarations must be finite JSON data: {exc}")
        params, declared_inputs, declared_outputs = {}, [], []
    from .provenance import validate_metadata
    try:
        validate_metadata(resources=resources, parameters=params, inputs=declared_inputs)
    except ValueError as exc:
        _issue(issues, "invalid_metadata", str(exc))
    safe_tokens = [token for token in override_tokens if isinstance(token, str)]
    argv = ["--parsable"] + safe_tokens + ["--chdir=" + str(directory), str(script_path)]
    if sum(len(token.encode()) + 1 for token in argv) > MAX_COMMAND_BYTES:
        _issue(issues, "command_too_large", "submission argv exceeds the bounded 128 KiB command limit")
    plan = {"schema": SCHEMA, "script": str(script_path), "workdir": str(directory), "argv": argv,
            "command": shlex.join(["sbatch"] + argv), "resources": resources, "directives": directives,
            "overrides": safe_tokens, "parameters": params, "inputs": declared_inputs, "outputs": declared_outputs,
            "script_sha256": hashlib.sha256(data).hexdigest(), "issues": issues,
            "valid": not any(item["level"] == "error" for item in issues)}
    plan["plan_id"] = _digest(plan)
    return plan


def _revalidate(plan: dict, slurm) -> dict:
    if isinstance(plan, dict) and (plan.get("submittable") is False or plan.get("workflow_orchestration") == "review_only" or plan.get("requires_workflow_orchestration") or plan.get("symbolic_dependencies")):
        raise ValueError("workflow plans have symbolic dependencies and require explicit orchestration; prepare a concrete scheduler dependency plan before submitting")
    if not isinstance(plan, dict) or plan.get("schema") != SCHEMA:
        raise ValueError("not a supported Raven submission plan")
    if plan.get("plan_id") != _digest(plan):
        raise ValueError("submission plan changed after preparation")
    from .remote import SshBackend
    from .record import ReplayBackend
    backend = slurm.b
    seen = set()
    while hasattr(backend, "inner") and id(backend) not in seen:
        seen.add(id(backend))
        backend = backend.inner
    if isinstance(backend, (SshBackend, ReplayBackend)):
        raise ValueError("local submission preflight is unavailable in SSH or replay mode; run Raven on the cluster login node")
    fresh = prepare(plan["script"], workdir=plan["workdir"], overrides=plan["overrides"],
                    parameters=plan["parameters"], inputs=plan["inputs"], outputs=plan["outputs"])
    if fresh["script_sha256"] != plan["script_sha256"]:
        raise ValueError("batch script changed after preparation; prepare and review a new plan")
    if not plan.get("valid") or not fresh["valid"]:
        messages = "; ".join(item["message"] for item in fresh["issues"] if item["level"] == "error")
        raise ValueError("submission preflight failed: " + (messages or "original plan was invalid"))
    if isinstance(plan.get("array_retry"), dict) and "manifest" in plan["array_retry"]:
        from .array_manifest import validate_retry
        from .local_files import LocalFiles
        import copy
        validate_retry(plan, LocalFiles())
        fresh["array_retry"] = copy.deepcopy(plan["array_retry"])
        fresh["plan_id"] = _digest(fresh)
    return fresh


def preview(plan: dict, slurm) -> dict:
    """Explicitly ask Slurm to test the exact plan without creating a job."""
    try:
        fresh = _revalidate(plan, slurm)
        ok, output = slurm.preview_submit(fresh["argv"], fresh["workdir"])
        return {"ok": bool(ok), "output": output, "command": shlex.join(["sbatch", "--test-only"] + fresh["argv"])}
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError) as exc:
        return {"ok": False, "output": str(exc), "error": str(exc)}


def _job_id(output: str) -> str | None:
    found = set()
    for line in output.splitlines():
        match = re.fullmatch(r"\s*(?:Submitted batch job )?([1-9]\d*)(?:;[A-Za-z0-9_.-]+)?\s*", line)
        if match:
            found.add(match[1])
    return next(iter(found)) if len(found) == 1 else None


def submit(plan: dict, slurm, passport_directory: str | os.PathLike | None = None) -> dict:
    """Submit a reviewed plan; the caller must obtain explicit user confirmation.

    Provenance is captured before contacting Slurm, so a passport write failure
    prevents submission. The immutable passport describes the plan; the returned
    job ID is its linkage receipt. An accepted but unparseable scheduler response
    is marked ``submitted=True`` and must never be retried automatically.
    """
    try:
        fresh = _revalidate(plan, slurm)
        from . import provenance
        resources = dict(fresh["resources"])
        mapping = fresh.get("array_retry", {}).get("manifest")
        if mapping:
            # The passport keeps a compact immutable linkage; the reviewed plan
            # and submission receipt retain the complete selected entry records.
            resources["raven_array_manifest"] = {key: mapping[key] for key in ("schema", "source", "revision", "cluster", "array_id")}
        passport = provenance.capture(fresh["workdir"], script=fresh["script"],
                                      resources=resources, parameters=fresh["parameters"], inputs=fresh["inputs"])
        passport_path = str(provenance.save(passport, passport_directory)) if passport_directory is not None else None
        # Capture performs bounded file reads too. Recheck the script, declared
        # inputs, cwd, and hidden sbatch environment options immediately before
        # handing the path to sbatch; arbitrary user-edited files are not locked.
        fresh = _revalidate(plan, slurm)
        try:
            ok, output, _ = slurm.submit(fresh["argv"], fresh["workdir"])
            if not isinstance(output, str):
                raise ValueError("scheduler returned a malformed receipt")
        except (OSError, ValueError, TypeError, AttributeError, RuntimeError) as exc:
            # A wrapper can fail after its inner scheduler call has succeeded
            # (for example while recording the receipt). Never claim rejection.
            message = "submission outcome is unknown: " + str(exc) + "; inspect the queue before retrying"
            return {"ok": False, "submitted": None, "state": "unknown", "output": message, "error": message,
                    "job_id": None, "command": fresh["command"], "passport": passport, "passport_path": passport_path}
        jid = _job_id(output) if ok else None
        uncertain = not ok and bool(re.search(r"timed?\s*out|timeout|connection reset", output, re.IGNORECASE))
        result = {"ok": bool(ok and jid), "submitted": None if uncertain else bool(ok),
                  "state": "unknown" if uncertain or (ok and not jid) else "accepted" if ok else "rejected",
                  "output": output, "job_id": jid,
                  "command": fresh["command"], "passport": passport, "passport_path": passport_path}
        if mapping:
            result["array_retry"] = fresh["array_retry"]
        if ok and not jid:
            result["error"] = "Slurm accepted the command but returned no unambiguous job ID; inspect the queue before retrying"
        elif uncertain:
            result["error"] = "submission outcome is unknown after a transport timeout; inspect the queue before retrying"
        return result
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError) as exc:
        return {"ok": False, "submitted": False, "state": "not_submitted", "output": str(exc), "error": str(exc), "job_id": None}


def save(plan: dict, path: str | os.PathLike) -> Path:
    """Save a bounded review plan without overwriting an existing file."""
    if plan.get("schema") != SCHEMA or plan.get("plan_id") != _digest(plan):
        raise ValueError("invalid or modified submission plan")
    data = (json.dumps(plan, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()
    if len(data) > MAX_PLAN_BYTES:
        raise ValueError("submission plan exceeds size limit")
    target = Path(path)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    return target


def load(path: str | os.PathLike) -> dict:
    """Load a review plan; action methods still revalidate current local files."""
    target = Path(path)
    fd = os.open(target, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError("plan must be a regular file")
        data = handle.read(MAX_PLAN_BYTES + 1)
    if len(data) > MAX_PLAN_BYTES:
        raise ValueError("submission plan exceeds size limit")
    try:
        plan = json.loads(data)
    except (RecursionError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("plan is not valid bounded JSON") from exc
    _metadata_bound(plan, depth_limit=24)
    try:
        if not isinstance(plan, dict) or plan.get("schema") != SCHEMA or plan.get("plan_id") != _digest(plan):
            raise ValueError("invalid or modified submission plan")
    except (KeyError, TypeError) as exc:
        raise ValueError("invalid or incomplete submission plan") from exc
    return plan
