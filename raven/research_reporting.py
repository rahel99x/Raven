"""Bash-friendly, explicit Research reporting; never called by a renderer.

The packaged reporting primitives also ship as the standalone
examples/project-template/reporting.py. Jobs write separate attempt directories;
a short advisory lock serializes metadata publications, not job execution.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from importlib import resources
from pathlib import Path

from . import reporting as R
from .projects import validate_inventory

_JOB = re.compile(r"[1-9][0-9]{0,18}(?:_[0-9]{1,19})?\Z")
_CLUSTER = re.compile(r"[A-Za-z0-9_.-]{0,128}\Z")


from .launching.io import _lock as _lock


def initialize(root, name=None):
    root = R._directory(root)
    path = R._under(root, ".raven/project.json", create_parent=True)
    value = {"schema": "raven.project/v1", "name": R._text(name or root.name, "project name"),
             "runs": "runs", "producer": "raven report", "paths": {"planning": "reports/planning.json"}}
    with _lock(root):
        if path.exists():
            current = R._read_json(path)
            if not isinstance(current, dict) or current.get("schema") != value["schema"]:
                raise ValueError("existing project descriptor is not a Raven descriptor")
            return current
        R._atomic_json(path, value)
    return value


def _vendor_bytes(path):
    """Read one stable regular source file, refusing links and unbounded input."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > R.MAX_JSON:
            raise ValueError("vendored client must be a regular file of at most 1 MiB")
        chunks, remaining = [], R.MAX_JSON + 1
        while remaining:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        signature = lambda value: (value.st_dev, value.st_ino, value.st_size,
                                   value.st_mtime_ns, value.st_ctime_ns)
        if (len(raw) != before.st_size or len(raw) > R.MAX_JSON
                or signature(before) != signature(os.fstat(fd))
                or signature(before) != signature(path.lstat())):
            raise ValueError("vendored client changed while being read; retry")
        return raw
    finally:
        os.close(fd)


def _vendor_write(path, raw, *, replace):
    """Publish a complete private source file, preserving the old file on error."""
    R._directory(path.parent)
    if os.path.lexists(path) and not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError("vendored client must be a regular file; symlinks refused")
    fd, temporary = tempfile.mkstemp(prefix=".raven-client-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def vendor_client(root, path="raven.py", *, force=False):
    """Copy the standalone metrics API and retain its verified source identity."""
    _managed_driver_admission("vendor-client")
    if type(force) is not bool:
        raise ValueError("force must be boolean")
    from .client import API_VERSION
    source = resources.files("raven").joinpath("client.py").read_bytes()
    if len(source) > R.MAX_JSON:
        raise ValueError("packaged metrics client exceeds the 1 MiB source budget")
    with _lock(root):
        root = R._directory(root)
        destination = R._under(root, path, create_parent=True)
        if destination.suffix != ".py":
            raise ValueError("vendored client path must name a .py file inside the project")
        relative = str(destination.relative_to(root))
        config_path = R._under(root, ".raven/config.json")
        config = R._read_json(config_path) if os.path.lexists(config_path) else {}
        if not isinstance(config, dict):
            raise TypeError("project .raven/config.json must contain a JSON object")
        previous = _vendor_bytes(destination)
        recorded = config.get("metrics_client", {})
        digest = hashlib.sha256(source).hexdigest()
        if (previous is not None and previous != source and not force
                and (not isinstance(recorded, dict) or recorded.get("path") != relative
                     or recorded.get("sha256") != hashlib.sha256(previous).hexdigest())):
            raise ValueError("existing client has local edits or no recorded checksum; use --force to replace it")
        metadata = {"api_version": API_VERSION, "path": relative, "sha256": digest}
        updated = dict(config, metrics_client=metadata)
        R._encoded(updated)  # Validate before either publication.
        changed = previous != source
        if changed:
            if _vendor_bytes(destination) != previous:
                raise ValueError("vendored client changed before publication; retry")
            _vendor_write(destination, source, replace=previous is not None)
        try:
            R._atomic_json(config_path, updated, replace=True)
        except (OSError, ValueError):
            # Metadata and source are separate atomic files. Restore the source
            # after a failed metadata publication, but never replace a new edit.
            if changed and _vendor_bytes(destination) == source:
                if previous is None:
                    destination.unlink()
                else:
                    _vendor_write(destination, previous, replace=True)
            raise
        return metadata


def _identity(job_id=None, environ=None):
    env = os.environ if environ is None else environ
    parent = env.get("SLURM_ARRAY_JOB_ID", "")
    index = env.get("SLURM_ARRAY_TASK_ID", "")
    selected = job_id or (f"{parent}_{index}" if parent and index else env.get("SLURM_JOB_ID", ""))
    if not isinstance(selected, str) or not _JOB.fullmatch(selected):
        raise ValueError("start requires a real Slurm job ID (--job-id or SLURM_JOB_ID)")
    restart = env.get("SLURM_RESTART_COUNT", "0")
    if not re.fullmatch(r"[0-9]{1,9}", restart):
        raise ValueError("SLURM_RESTART_COUNT must be a nonnegative integer")
    cluster = env.get("SLURM_CLUSTER_NAME", "")
    if not _CLUSTER.fullmatch(cluster):
        raise ValueError("invalid Slurm cluster name")
    # Cluster prefix prevents identically numbered jobs on shared project roots
    # from overwriting each other. Keep the full identity separately in metadata.
    import hashlib
    prefix = hashlib.sha256(cluster.encode()).hexdigest()[:12] + "-" if cluster else ""
    attempt = int(restart) + 1
    return selected, attempt, f"{prefix}job-{selected}-a{attempt}", cluster


def _submission_path(root, job_id, cluster):
    import hashlib
    prefix = hashlib.sha256(cluster.encode()).hexdigest()[:12] if cluster else "local"
    return R._under(root, f".raven/submissions/{prefix}-{job_id.split('_')[0]}.json", create_parent=True)


def _job_index(root, selected, cluster, run_id):
    import hashlib
    prefix = hashlib.sha256(cluster.encode()).hexdigest()[:12] + "-" if cluster else ""
    path = R._under(root, f".raven/jobs/{prefix}{selected}.json", create_parent=True)
    value = R._read_json(path) if path.exists() else {"schema": "raven.job-index/v1", "job_id": selected, "cluster": cluster, "run_ids": []}
    if (not isinstance(value, dict) or value.get("schema") != "raven.job-index/v1" or value.get("job_id") != selected
            or value.get("cluster") != cluster or not isinstance(value.get("run_ids"), list)
            or len(value["run_ids"]) > 128 or any(not isinstance(item, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", item) for item in value["run_ids"])):
        raise ValueError("invalid exact job index")
    if run_id not in value["run_ids"]:
        if len(value["run_ids"]) >= 128:
            raise ValueError("job attempt index exceeds 128 attempts; archive earlier attempts")
        value["run_ids"].append(run_id)
    R._atomic_json(path, value, replace=True)
    if cluster:
        alias = R._under(root, f".raven/jobs/{selected}.json", create_parent=True)
        previous = R._read_json(alias) if alias.exists() else None
        if previous and previous.get("schema") == "raven.job-index-ambiguous/v1":
            return  # once ambiguous, never silently restore an unscoped alias
        if previous and previous.get("cluster") not in ("", cluster):
            value = {"schema": "raven.job-index-ambiguous/v1", "job_id": selected,
                     "clusters": sorted({cluster, str(previous.get("cluster", ""))})}
        elif previous and not previous.get("cluster") and set(previous.get("run_ids", [])) - set(value["run_ids"]):
            value = {"schema": "raven.job-index-ambiguous/v1", "job_id": selected, "clusters": ["", cluster]}
        R._atomic_json(alias, value, replace=True)


def _json_object(value, name):
    result = json.loads(value)
    if not isinstance(result, dict):
        raise ValueError(name + " must be a JSON object")
    R._encoded(result)
    return result


def start(root, *, experiment=None, script=None, name=None, job_id=None, parameters=None,
          resources=None, metrics=None, logs=(), environ=None):
    env = os.environ if environ is None else environ
    root = R._directory(root)
    initialize(root)
    selected, attempt, run_id, cluster = _identity(job_id, env)
    with _lock(root):
        token = env.get("RAVEN_SUBMISSION_TOKEN", "")
        intent_path = None
        if token:
            if not re.fullmatch(r"[0-9a-f]{32}", token):
                raise ValueError("invalid submission token")
            intent_path = R._under(root, f".raven/launches/{token}.json")
        record_path = _submission_path(root, selected, cluster)
        if not record_path.exists() and cluster:
            record_path = _submission_path(root, selected, "")
        record = R._read_json(intent_path) if intent_path else R._read_json(record_path) if record_path.exists() else {}
        if record and record.get("schema") != "raven.launch/v1":
            raise ValueError("invalid submission receipt")
        if record.get("job_id", record.get("observed_job_id")) not in (None, selected.split("_")[0]):
            raise ValueError("submission receipt identity mismatch")
        if record.get("cluster") and record["cluster"] != cluster:
            raise ValueError("submission receipt belongs to a different cluster")
        script = script or record.get("script")
        experiment = experiment or record.get("experiment")
        if not script or not experiment:
            raise ValueError("provide --script and --experiment when no submission receipt is available")
        actual_parameters = parameters if parameters is not None else record.get("parameters", {})
        array_document = None
        if record.get("array_manifest"):
            from .array_manifest import parse
            array_document = dict(record["array_manifest"], array_id=selected.split("_")[0], cluster=cluster)
            array = parse(R._encoded(array_document, 4 << 20))
            if "_" in selected:
                entry = array.lookup(selected, cluster)
                if entry is None:
                    raise ValueError("array task index is absent from its scientific manifest")
                actual_parameters = dict(actual_parameters, **dict(entry.parameters))
        if metrics:
            from .projects import relative_path
            relative_path(metrics)
        if record.get("run_id_base"):
            # A launch token distinguishes recycled scheduler IDs; array tasks
            # and restarts still use the existing exact job indexes.
            run_id = record["run_id_base"] + ("-t" + selected.split("_", 1)[1] if "_" in selected else "") + "-a" + str(attempt)
        elif record.get("run_id") and "_" not in selected and attempt == 1:
            run_id = record["run_id"]
        existing = root / "runs" / run_id / "run.json"
        if existing.exists():
            manifest = validate_inventory(R._read_json(existing))
            if manifest.get("job_id") != selected or manifest.get("state") not in {"RUNNING", "SUBMITTED"}:
                raise ValueError("this attempt already exists; do not reuse a finished attempt")
            if manifest.get("experiment_id") != experiment:
                raise ValueError("this job attempt belongs to another experiment")
            expected_script = script if record.get("plan") else str(R._under(root, script).relative_to(root))
            if manifest.get("provenance", {}).get("script") != expected_script:
                raise ValueError("this job attempt belongs to another script")
            if manifest["state"] == "RUNNING":
                _job_index(root, selected, cluster, run_id)
                return existing.parent
            run = existing.parent
            manifest.update(state="RUNNING", start=time.time())
        else:
            run = R.begin_run(root, run_id, name=name or record.get("name") or experiment,
                              script=record.get("source_snapshot", script), experiment_id=experiment, attempt=attempt, job_id=selected,
                              parameters=actual_parameters,
                              resources=resources if resources is not None else record.get("resources", {}))
            manifest = R._read_json(run / "run.json")
            manifest["provenance"]["script"] = script
        launch = {"cluster": cluster, "dependency": record.get("dependency", ""),
                  "submission_token": record.get("token", ""), "array_id": selected.split("_")[0] if "_" in selected else "",
                  "array_index": int(selected.split("_")[1]) if "_" in selected else None, "restart_count": attempt - 1}
        manifest["metadata"] = {"raven_launch": launch}
        if record.get("submitted"):
            manifest["submit"] = record["submitted"]
        if metrics:
            from .projects import relative_path
            manifest["paths"]["metrics"] = relative_path(metrics)
        if array_document is not None:
            R._atomic_json(run / "reports" / "array-manifest.json", array_document, replace=True)
            manifest["paths"]["array_manifest"] = "reports/array-manifest.json"
        # Native reports remain read-only data, never shell commands or plans to execute.
        validate_inventory(manifest)
        R._atomic_json(run / "run.json", manifest, replace=True)
        _job_index(root, selected, cluster, run.name)
        if intent_path:
            record.update(observed_job_id=selected.split("_")[0], observed_cluster=cluster)
            if "_" not in selected and attempt == 1:
                record["observed_run_id"] = run.name
            R._atomic_json(intent_path, record, replace=True)
        for label, path in logs:
            R.register_log(run, "declared." + label, path, label=label, group="Declared logs")
        for label, pattern in record.get("logs", {}).items():
            try:
                resolved = _log_pattern(pattern, selected, env, Path(record.get("workdir", root)))
                if resolved:
                    index = R._read_json(run / "logs.json")
                    if not any(row["id"] == "slurm." + label for row in index["logs"]):
                        R.register_log(run, "slurm." + label, resolved, label="Slurm " + label, group="Slurm")
                elif record.get("plan"):
                    raise ValueError("node/rank-dependent log pattern requires explicit per-rank registration")
            except (ValueError, OSError) as exc:
                if not record.get("plan"):
                    raise
                launch.setdefault("log_warnings", []).append("Slurm " + label + " is preserved but unavailable as a regular retained log: " + str(exc))
        if launch.get("log_warnings"):
            R._atomic_json(run / "run.json", manifest, replace=True)
    return run


def _log_pattern(pattern, selected, env, root):
    parent, _, index = selected.partition("_")
    values = {"%A": parent, "%a": index or "4294967294", "%j": env.get("SLURM_JOB_ID", parent),
              "%x": env.get("SLURM_JOB_NAME", ""), "%u": env.get("USER", "")}
    placeholder = "\x01"
    value = str(pattern).replace("%%", placeholder)
    for token, replacement in values.items():
        if token in value and not replacement:
            return None
        value = value.replace(token, replacement)
    if "%" in value:
        return None  # node/rank-dependent patterns need explicitly reported per-rank paths
    value = value.replace(placeholder, "%")
    return str(Path(value) if os.path.isabs(value) else root / value)


def _metadata_options(script, options):
    """Read static #SBATCH data, never source or evaluate a shell script."""
    fd = os.open(script, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > R.MAX_SCRIPT:
            raise ValueError("batch script must be regular and at most 8 MiB")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            source = stream.read(R.MAX_SCRIPT + 1)
        after, named = os.fstat(fd), os.stat(script, follow_symlinks=False)
        signature = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
        if len(source) > R.MAX_SCRIPT or signature(before) != signature(after) or signature(after) != signature(named):
            raise ValueError("batch script changed while being read")
    finally:
        os.close(fd)
    tokens = []
    for line in source.decode("utf-8").splitlines():
        line = line.strip()
        if line.startswith("#SBATCH"):
            tokens.extend(shlex.split(line[7:], comments=True))
        elif line and not line.startswith("#"):
            break
    # Slurm precedence is CLI > SBATCH_* environment > script directives.
    for variable, option in (("SBATCH_DEPENDENCY", "--dependency"), ("SBATCH_OUTPUT", "--output"),
                             ("SBATCH_ERROR", "--error"), ("SBATCH_ARRAY_INX", "--array"), ("SBATCH_EXPORT", "--export")):
        if variable in os.environ:
            tokens.append(option + "=" + os.environ[variable])
    tokens.extend(options)
    mapping = {"--dependency": "dependency", "-d": "dependency", "--output": "stdout", "-o": "stdout", "--error": "stderr", "-e": "stderr", "--array": "array", "-a": "array", "--export": "export"}
    out = {}
    i = 0
    while i < len(tokens):
        token = tokens[i]
        key, equal, value = token.partition("=")
        if key in {"hetjob", "packjob"}:
            raise ValueError("heterogeneous packs require their own reviewed Raven operation")
        if (key in {"-W", "-M", "-h", "-V"} or key.startswith("-M")
                or key.startswith("--") and any(item.startswith(key) for item in ("--wrap", "--wait", "--test-only", "--clusters", "--help", "--version"))
                or key != "--export" and "--export-file".startswith(key)):
            raise ValueError("batch directives cannot change the tracked submission mode: " + key)
        if key in mapping:
            if not equal:
                i += 1
                if i == len(tokens):
                    raise ValueError(f"missing value for {key}")
                value = tokens[i]
            out[mapping[key]] = value
        elif len(token) > 2 and token[:2] in mapping:
            out[mapping[token[:2]]] = token[2:]
        i += 1
    return out


def submit(root, *, experiment, script, options=(), name=None, parameters=None,
           resources=None, array_manifest=None, runner=subprocess.run):
    """Submit once; a durable intent prevents an ambiguous failure looking safe to retry."""
    _managed_driver_admission("submit")
    root = R._directory(root)
    initialize(root)
    R._text(experiment, "experiment")
    script_path = R._under(root, script)
    if not stat.S_ISREG(script_path.lstat().st_mode) or script_path.stat().st_size > R.MAX_SCRIPT:
        raise ValueError("batch script must be a regular file of at most 8 MiB")
    options = list(options)
    if len(options) > 256 or any(not isinstance(item, str) or len(item) > 4096 or "\x00" in item for item in options):
        raise ValueError("sbatch options exceed their bounded argument budget")
    # These flags defeat the one-script/one-receipt contract. Arrays are supported;
    # heterogeneous packs have their own reviewed Raven operation.
    for option in options:
        if not option.startswith("--"):
            raise ValueError("tracked submissions use --option=value syntax; positional scripts and short options are refused")
        if option == ":" or option == "--" or option.startswith(("--wrap", "--chdir", "--parsable=", "--wait", "--test-only", "--help", "--version", "--clusters", "--export-file")) or option.startswith(("-D", "-M")) or option in {"-W", "-h", "-V"}:
            raise ValueError("unsupported sbatch option for a tracked submission: " + option)
        if "=" not in option and option not in {"--exclusive", "--oversubscribe", "--requeue", "--no-requeue", "--hold", "--parsable", "--contiguous", "--spread-job", "--use-min-nodes", "--verbose", "--quiet"}:
            raise ValueError("tracked submission values require --option=value syntax")
        key = option.partition("=")[0]
        if any(forbidden.startswith(key) for forbidden in ("--wrap", "--wait", "--test-only", "--help", "--version", "--chdir", "--clusters")):
            raise ValueError("abbreviated submission-mode options are refused")
    metadata = _metadata_options(script_path, options)
    export = metadata.get("export", "ALL").split(",")
    required_exports = {"RAVEN_SUBMISSION_TOKEN", "RAVEN_PROJECT_ROOT", "RAVEN_EXPERIMENT", "RAVEN_SCRIPT", "RAVEN_PARAMETERS", "RAVEN_RESOURCES"}
    if ("ALL" not in export and not required_exports <= set(export)
            or any(item.partition("=")[0] in required_exports and "=" in item for item in export)):
        raise ValueError("tracked submission requires --export=ALL or all six RAVEN_* handshake variable names; export policies must not replace their values")
    for variable in ("SBATCH_WAIT", "SBATCH_TEST_ONLY", "SBATCH_CLUSTERS", "SBATCH_WRAP", "SBATCH_EXPORT_FILE"):
        if os.environ.get(variable):
            raise ValueError(f"unset {variable} before using tracked submission")
    R._text(name or experiment, "name")
    params = R._parameters(parameters or {})
    request = R._resources(resources or {})
    declaration = None
    if array_manifest:
        from .array_manifest import parse
        declaration = R._read_json(R._under(root, array_manifest))
        # The supplied parent ID is only a placeholder until sbatch returns.
        parse(R._encoded(dict(declaration, array_id="1", cluster=""), 4 << 20))
        if not metadata.get("array"):
            raise ValueError("an array manifest requires an explicit --array or #SBATCH --array")
    token = uuid.uuid4().hex
    intent = {"schema": "raven.launch/v1", "token": token, "status": "submitting", "script": str(script_path.relative_to(root)),
              "experiment": experiment, "name": name or experiment, "parameters": params, "resources": request,
              "dependency": metadata.get("dependency", ""), "submitted": time.time(), "argv": ["sbatch", "--parsable", "--chdir", str(root), *options, str(script_path)]}
    pattern = "slurm-%A_%a.out" if metadata.get("array") else "slurm-%j.out"
    intent["logs"] = {"stdout": metadata.get("stdout", pattern)}
    if metadata.get("stderr") and metadata["stderr"] != intent["logs"]["stdout"]:
        intent["logs"]["stderr"] = metadata["stderr"]
    if declaration is not None:
        intent["array_manifest"] = declaration
    intent["array"] = bool(metadata.get("array"))
    return _submit_intent(root, intent, runner=runner)



def _submission_unknown(root, path, intent, error):
    """Retain runtime evidence even when the scheduler receipt is ambiguous."""
    with _lock(root):
        observed = R._read_json(path)
        merged = dict(intent)
        for key in ("observed_job_id", "observed_cluster", "observed_run_id"):
            if key in observed:
                merged[key] = observed[key]
        merged.update(status="unknown", error=error[:2048])
        R._atomic_json(path, merged, replace=True)
        intent.update(merged)


def _submit_intent(root, intent, *, runner=subprocess.run, cwd=None, environment=None):
    """Shared durable one-submit protocol for explicit and prepared launchers.

    Callers finish all validation/materialization first. An uncertain result
    never triggers another scheduler invocation. The prepared caller embeds
    its handshake in the wrapper, preserving isolated Slurm export policies.
    """
    root = R._directory(root)
    token, experiment, script = intent["token"], intent["experiment"], intent["script"]
    name, params, request = intent["name"], intent["parameters"], intent["resources"]
    declaration = intent.get("array_manifest")
    from .execution.policy import router_for
    from .launching.scheduler_context import capture as scheduler_context, validated as valid_scheduler_context
    router = router_for()
    context = scheduler_context(getattr(router, "source_backend", None), router.policy)
    if context is not None:
        intent["scheduler_context"] = context
    if intent.get("scheduler_context") is not None:
        from .connection_scope import selection_digest
        if not valid_scheduler_context(intent["scheduler_context"]):
            raise ValueError("Launch scheduler context is invalid; preview this launch again")
        if intent["scheduler_context"].get("selection_sha256") != selection_digest(os.environ if environment is None else environment):
            raise ValueError("Slurm configuration selection changed after preview; preview this launch again")
    path = R._under(root, f".raven/launches/{token}.json", create_parent=True)
    R._atomic_json(path, intent)
    # File-backed capture bounds memory even if a wrapper prints excessive data.
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            env = dict(os.environ, RAVEN_PROJECT_ROOT=str(root), RAVEN_EXPERIMENT=experiment,
                       RAVEN_SCRIPT=script,
                       RAVEN_PARAMETERS=json.dumps(params, separators=(",", ":")),
                       RAVEN_RESOURCES=json.dumps(request, separators=(",", ":")), RAVEN_SUBMISSION_TOKEN=token)
            if environment is not None:
                env = dict(environment)
            from .execution.admission import reporting_submission
            with reporting_submission(intent["argv"]):
                result = runner(intent["argv"], cwd=cwd or root, env=env, stdout=stdout, stderr=stderr, timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            _submission_unknown(root, path, intent, str(exc))
            raise ValueError(f"submission outcome unknown; inspect {path}; do not submit again blindly") from exc
        stdout.seek(0)
        stderr.seek(0)
        output = stdout.read(65537).decode("utf-8", "replace").strip()
        errors = stderr.read(8192).decode("utf-8", "replace").strip()
    if result.returncode or not re.fullmatch(r"[1-9][0-9]{0,18}(?:;[A-Za-z0-9_.-]{1,128})?", output):
        _submission_unknown(root, path, intent, errors or output[:2048])
        raise ValueError(f"sbatch did not return an unambiguous job ID; inspect {path}: {intent['error']}")
    job_id, _, cluster = output.partition(";")
    cluster = cluster or (os.environ if environment is None else environment).get("SLURM_CLUSTER_NAME", "")
    if not _CLUSTER.fullmatch(cluster):
        raise ValueError(f"job {output} submitted, but cluster identity is invalid; inspect {path}")
    intent.update(status="submitted", job_id=job_id, cluster=cluster)
    if declaration is not None:
        intent["array_manifest"] = dict(declaration, array_id=job_id, cluster=cluster)
    try:
        with _lock(root):
            observed = R._read_json(path)
            if observed.get("observed_job_id") not in (None, job_id):
                raise ValueError("runtime job ID disagrees with sbatch response")
            if observed.get("observed_cluster"):
                if cluster and cluster != observed["observed_cluster"]:
                    raise ValueError("runtime cluster disagrees with sbatch response")
                cluster = observed["observed_cluster"]
                intent["cluster"] = cluster
                if "array_manifest" in intent:
                    intent["array_manifest"]["cluster"] = cluster
            _, _, run_id, _ = _identity(job_id, {"SLURM_CLUSTER_NAME": cluster})
            if intent.get("run_id_base"):
                run_id = intent["run_id_base"] + "-a1"
            run_id = observed.get("observed_run_id", run_id)
            intent["run_id"] = run_id
            # The token receipt retains a known accepted ID even if a later
            # alias/index write fails. It is also the immutable identity anchor
            # when a scheduler eventually recycles the same numeric job ID.
            for key in ("observed_job_id", "observed_cluster", "observed_run_id"):
                if key in observed:
                    intent[key] = observed[key]
            R._atomic_json(path, intent, replace=True)
            alias = _submission_path(root, job_id, cluster)
            if intent.get("run_id_base") and alias.exists():
                previous = R._read_json(alias)
                tokens = previous.get("tokens", [previous.get("token")])
                tokens = [value for value in tokens if isinstance(value, str)]
                R._atomic_json(alias, {"schema": "raven.submission-ambiguous/v1", "job_id": job_id,
                    "cluster": cluster, "tokens": (tokens + [token])[-128:]}, replace=True)
            else:
                R._atomic_json(alias, intent)
            # Publish a pending experiment immediately; the runtime helper will
            # transition this exact attempt instead of creating a second run.
            run = root / "runs" / run_id
            if not (run / "run.json").exists():
                run = R.begin_run(root, run_id, name=name or experiment, script=intent.get("source_snapshot", script),
                                  experiment_id=experiment, job_id=job_id, parameters=params, resources=request)
                manifest = R._read_json(run / "run.json")
                manifest["provenance"]["script"] = script
                manifest.update(state="SUBMITTED", submit=intent["submitted"], metadata={"raven_launch": {
                    "cluster": cluster, "dependency": intent["dependency"], "submission_token": token,
                    "array_id": job_id if intent.get("array") else "", "array_index": None, "restart_count": 0}})
                manifest.pop("start", None)
                if intent.get("plan"):
                    plan = intent["plan"]
                    from .launching.contract import plan_inventory
                    manifest["metadata"]["automation"] = {"schema": "raven.automation/v1", "plan": plan_inventory(plan),
                        "project_root": str(root), "filesystem_host": plan.get("filesystem_host", ""), "filesystem_id": plan.get("filesystem_id", ""),
                        "application_outcome": "not started", "scheduler_outcome": "pending or unknown", "warnings": []}
                    R._atomic_json(run / "reports" / "submit.json", {"schema": "raven.launch-report/v1", "plan": plan,
                        "receipt": {"run_id": run_id, "job_id": job_id, "attempt": 1, "cluster": cluster,
                                    "token": token, "execution": "slurm", "application_exit_code": None,
                                    "scheduler_outcome": "pending or unknown", "argv": intent["argv"]}})
                if declaration is not None:
                    R._atomic_json(run / "reports" / "array-manifest.json", intent["array_manifest"])
                    manifest["paths"]["array_manifest"] = "reports/array-manifest.json"
                validate_inventory(manifest)
                R._atomic_json(run / "run.json", manifest, replace=True)
            _job_index(root, job_id, cluster, run_id)
    except (OSError, ValueError) as exc:
        raise ValueError(f"job {output} WAS submitted; receipt publication failed: {exc}; do not resubmit") from exc
    return output


def finish(run, *, exit_code=0, state=None, runtime_seconds=None, results=None, scaling=None):
    run = R._directory(run)
    with _lock(run):
        manifest = validate_inventory(R._read_json(run / "run.json"))
        # Recover an interrupted final two-file publication without rewriting
        # the factual terminal summary or requiring the application to rerun.
        if manifest["state"] == "RUNNING" and (run / "summary.json").exists():
            saved = R._summary(R._read_json(run / "summary.json"))
            if saved.get("id") != manifest["run_id"] or saved.get("job_id") != manifest.get("job_id"):
                raise ValueError("saved summary identity mismatch")
            manifest.update(state=saved["state"], end=saved["end"])
            R._atomic_json(run / "run.json", manifest, replace=True)
        if manifest["state"] in R.TERMINAL:
            summary = R._summary(R._read_json(run / "summary.json"))
            if summary.get("exit_code") != exit_code or (state and summary["state"] != state):
                raise ValueError("attempt was already finished with a different result")
            return summary
        return R.finish_run(run, state=state or ("COMPLETED" if exit_code == 0 else "FAILED"),
                            runtime_seconds=runtime_seconds, results=results, exit_code=exit_code, scaling=scaling)


def aggregate(root):
    """Publish one shared factual bundle; avoid O(runs) writes after each finish."""
    _managed_driver_admission("aggregate")
    from .projects import discover_project
    with _lock(root, "aggregate.lock"):
        project = discover_project(str(root))
        if project["limited"]:
            raise ValueError("comparison inventory exceeds 256 attempts; split or archive the campaign before aggregating")
        selected = ["runs/" + row["run_id"] for row in project["runs"] if row["state"] in R.TERMINAL and (Path(root) / "runs" / row["run_id"] / "summary.json").is_file()]
        if not selected:
            return {"history": []}
        document = R.export_planning(root, selected)
        return document


def _pairs(values):
    result = []
    for item in values:
        key, equal, value = item.partition("=")
        if not equal:
            raise ValueError("declarations use NAME=VALUE")
        result.append((key, value))
    return result


def _managed_driver_admission(command):
    from .execution.admission import cli_admission
    from .execution.policy import AdmissionError
    blocked = cli_admission(["report", command])
    if blocked is not None:
        raise AdmissionError(blocked["summary"])


def main(argv=None):
    parser = argparse.ArgumentParser(prog="raven report", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="initialize an optional project descriptor")
    init.add_argument("--root", default=os.environ.get("RAVEN_PROJECT_ROOT", "."))
    init.add_argument("--name")
    vendor = sub.add_parser("vendor-client", help="copy the standalone Python metrics API into a project")
    vendor.add_argument("--root", default=os.environ.get("RAVEN_PROJECT_ROOT", "."))
    vendor.add_argument("--path", default="raven.py", help="project-relative Python destination (default: raven.py)")
    vendor.add_argument("--force", action="store_true", help="replace a locally edited or untracked destination")
    for command in ("submit", "start", "exec"):
        item = sub.add_parser(command)
        item.add_argument("--root", default=os.environ.get("RAVEN_PROJECT_ROOT", "."))
        item.add_argument("--experiment", default=os.environ.get("RAVEN_EXPERIMENT"), required=command == "submit" and not os.environ.get("RAVEN_EXPERIMENT"))
        item.add_argument("--script", default=os.environ.get("RAVEN_SCRIPT"), required=command == "submit" and not os.environ.get("RAVEN_SCRIPT"))
        item.add_argument("--name")
        item.add_argument("--parameters", default=os.environ.get("RAVEN_PARAMETERS"), help="JSON object of actual experiment parameters")
        item.add_argument("--resources", default=os.environ.get("RAVEN_RESOURCES"), help="JSON object of known requested resources")
        if command == "submit":
            item.add_argument("--array-manifest")
            item.add_argument("args", nargs=argparse.REMAINDER)
        else:
            item.add_argument("--job-id")
            item.add_argument("--metrics", help="exact path relative to the new attempt directory")
            item.add_argument("--log", action="append", default=[], help="NAME=PATH; relative to the attempt directory")
            if command == "exec":
                item.add_argument("args", nargs=argparse.REMAINDER)
    end = sub.add_parser("finish")
    end.add_argument("--run", default=os.environ.get("RAVEN_RUN_DIR"), required=not os.environ.get("RAVEN_RUN_DIR"))
    end.add_argument("--exit-code", type=int, default=0)
    end.add_argument("--state", choices=sorted(R.TERMINAL))
    end.add_argument("--runtime-seconds", type=float)
    end.add_argument("--results", help="JSON object of actual scientific results")
    metric = sub.add_parser("metric")
    metric.add_argument("--run", default=os.environ.get("RAVEN_RUN_DIR"), required=not os.environ.get("RAVEN_RUN_DIR"))
    metric.add_argument("--value", action="append", default=[], help="NAME=NUMBER")
    metric.add_argument("--step", type=int)
    metric.add_argument("--phase")
    metric.add_argument("--completed", type=float)
    metric.add_argument("--total", type=float)
    metric.add_argument("--unit", default="")
    report = sub.add_parser("research")
    report.add_argument("view", choices=sorted(R.REPORT_VIEWS))
    report.add_argument("source")
    report.add_argument("--run", default=os.environ.get("RAVEN_RUN_DIR"), required=not os.environ.get("RAVEN_RUN_DIR"))
    logs = sub.add_parser("log")
    logs.add_argument("id")
    logs.add_argument("path")
    logs.add_argument("--run", default=os.environ.get("RAVEN_RUN_DIR"), required=not os.environ.get("RAVEN_RUN_DIR"))
    logs.add_argument("--label", default="")
    passport = sub.add_parser("passport", help="capture declared source, Git, input and environment evidence")
    passport.add_argument("--run", default=os.environ.get("RAVEN_RUN_DIR"), required=not os.environ.get("RAVEN_RUN_DIR"))
    passport.add_argument("--input", action="append", default=[], help="exact input path, size/mtime only")
    passport.add_argument("--environment", action="append", default=[], help="explicit nonsecret environment name")
    artifact = sub.add_parser("artifact")
    artifact.add_argument("path", help="exact output path relative to this run")
    artifact.add_argument("--run", default=os.environ.get("RAVEN_RUN_DIR"), required=not os.environ.get("RAVEN_RUN_DIR"))
    artifact.add_argument("--format", choices=("json", "csv", "text"))
    artifact.add_argument("--optional", action="store_true")
    agg = sub.add_parser("aggregate")
    agg.add_argument("--root", default=os.environ.get("RAVEN_PROJECT_ROOT", "."))
    args = parser.parse_args(argv)
    try:
        _managed_driver_admission(args.command)
        if args.command == "init":
            initialize(args.root, args.name)
            return 0
        if args.command == "vendor-client":
            print(json.dumps(vendor_client(args.root, args.path, force=args.force), sort_keys=True))
            return 0
        if args.command in {"submit", "start", "exec"}:
            kwargs = {key: getattr(args, key) for key in ("experiment", "script", "name")}
            for key in ("parameters", "resources"):
                value = getattr(args, key)
                kwargs[key] = _json_object(value, key) if value is not None else None
            if args.command == "submit":
                print(submit(args.root, **kwargs, array_manifest=args.array_manifest,
                             options=args.args[1:] if args.args[:1] == ["--"] else args.args))
                return 0
            if args.command == "exec" and (not args.args or args.args == ["--"]):
                raise ValueError("exec requires a command after --")
            run = start(args.root, **kwargs, job_id=args.job_id, metrics=args.metrics, logs=_pairs(args.log))
            if args.command == "start":
                print(run)
                return 0
            command = args.args[1:] if args.args[:1] == ["--"] else args.args
            if not command:
                raise ValueError("exec requires a command after --")
            with _lock(run):
                # Persistent exclusive claim: a duplicate launcher must never
                # rerun payload after an uncertain exit or interrupted wait.
                R._atomic_json(run / ".execution.json", {"schema": "raven.execution/v1", "pid": os.getpid(), "started": time.time()})
            env = dict(os.environ, RAVEN_RUN_DIR=str(run), RAVEN_PROJECT_ROOT=str(Path(args.root).absolute()))
            before = time.monotonic()
            process = None
            interrupted = []
            def forward(signum, frame):
                interrupted.append(signum)
                if process is not None and process.poll() is None:
                    process.send_signal(signum)
            handlers = {sig: signal.signal(sig, forward) for sig in (signal.SIGINT, signal.SIGTERM)}
            try:
                def log_stream(path):
                    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_NONBLOCK | os.O_NOFOLLOW)
                    if not stat.S_ISREG(os.fstat(fd).st_mode):
                        os.close(fd)
                        raise ValueError("command logs must be regular files")
                    return os.fdopen(fd, "ab")
                with log_stream(run / "logs" / "stdout.log") as out, log_stream(run / "logs" / "stderr.log") as err:
                    process = subprocess.Popen(command, env=env, stdout=out, stderr=err)
                    for signum in interrupted:
                        if process.poll() is None:
                            process.send_signal(signum)
                    code = process.wait()
                code = 128 - code if code < 0 else code
                if interrupted:
                    code = 128 + interrupted[-1]
            except OSError as exc:
                code = 127
                print(f"raven report: cannot execute command: {exc}", file=sys.stderr)
            finally:
                for sig, handler in handlers.items():
                    signal.signal(sig, handler)
            finish(run, exit_code=code, state="INTERRUPTED" if interrupted else None, runtime_seconds=time.monotonic() - before)
            try:
                aggregate(args.root)
            except (OSError, ValueError) as exc:
                print(f"raven report: job result saved; comparison refresh deferred: {exc}", file=sys.stderr)
            return code
        if args.command == "finish":
            finish(args.run, exit_code=args.exit_code, state=args.state, runtime_seconds=args.runtime_seconds,
                   results=_json_object(args.results, "results") if args.results else None)
        elif args.command == "metric":
            values = _pairs(args.value)
            if len({key for key, value in values}) != len(values):
                raise ValueError("duplicate metric names")
            progress = None
            if args.completed is not None or args.total is not None:
                progress = {"completed": args.completed, "total": args.total, "unit": args.unit}
            with _lock(Path(args.run).absolute()):
                manifest = validate_inventory(R._read_json(Path(args.run) / "run.json"))
                if manifest["paths"].get("metrics", "metrics.jsonl") != "metrics.jsonl":
                    raise ValueError("this run binds an external metric producer; write to its declared path instead")
                R.write_metric(args.run, {key: float(value) for key, value in values}, step=args.step, phase=args.phase, progress=progress)
        elif args.command == "research":
            with _lock(Path(args.run).absolute()):
                R.publish_research(args.run, args.view, R._read_json(args.source))
        elif args.command == "log":
            with _lock(Path(args.run).absolute()):
                R.register_log(args.run, args.id, args.path, label=args.label)
        elif args.command == "passport":
            from .provenance import capture, save
            run = R._directory(args.run)
            manifest = validate_inventory(R._read_json(run / "run.json"))
            value = capture(run.parent.parent, script=manifest["provenance"]["script"],
                            resources=manifest.get("resources", {}), parameters=manifest.get("parameters", {}),
                            inputs=args.input, environment_names=args.environment, job_id=manifest.get("job_id"))
            with _lock(run):
                R._directory(run / "passports")
                print(save(value, run / "passports"))
        elif args.command == "artifact":
            from .artifacts import _contract
            run = R._directory(args.run)
            with _lock(run):
                path = R._under(run, ".raven/contracts/outputs.v1.json", create_parent=True)
                contract = R._read_json(path) if path.exists() else {"version": 1, "outputs": []}
                entry = {"path": args.path, "required": not args.optional}
                if args.format:
                    entry["format"] = args.format
                if any(item.get("path") == args.path for item in contract["outputs"]):
                    raise ValueError("artifact path already declared")
                contract["outputs"].append(entry)
                _contract(contract)
                R._atomic_json(path, contract, replace=True)
                manifest = validate_inventory(R._read_json(run / "run.json"))
                manifest["paths"]["contract"] = ".raven/contracts/outputs.v1.json"
                validate_inventory(manifest)
                R._atomic_json(run / "run.json", manifest, replace=True)
        elif args.command == "aggregate":
            print(json.dumps({"attempts": len(aggregate(args.root)["history"])}))
        return 0
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(f"raven report: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
