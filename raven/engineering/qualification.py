"""Reviewed tiny compute probes, separate from executable-presence checks.

Probe results are local evidence, not remote attestation. Ownership and native
attempt are revalidated through the scheduler when results are collected. A
successful import is never reported as worker placement or GPU qualification.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import shlex
import shutil
import socket
import sys
import tempfile
import time
import uuid

from .. import evidence as E
from .. import science_files
from . import _storage as S

SCHEMA = "raven.site-probe/v1"
RESULT = "raven.site-probe-result/v1"


def _paths(project, identifier):
    from .. import projects

    root, fd = projects._root(str(project))
    os.close(fd)
    base = Path(root) / ".raven" / "engineering" / "qualification"
    return base / (identifier + ".sh"), base / (identifier + ".result.json")


def _config(value):
    E.keys(value, set(), {"python", "scratch", "imports", "worker", "trace", "gpu", "resources"}, "compute probe configuration")
    config = {"python": value.get("python", sys.executable),
              "scratch": value.get("scratch", tempfile.gettempdir()),
              "imports": value.get("imports", ["raven"]),
              "worker": value.get("worker", True), "trace": value.get("trace", True),
              "gpu": value.get("gpu", False), "resources": value.get("resources", {})}
    for key in ("python", "scratch"):
        E.text(config[key], key, 4096)
        if not os.path.isabs(config[key]) or os.path.normpath(config[key]) != config[key]:
            raise ValueError("Compute Python and scratch must be explicit absolute paths on the compute host")
    if (not isinstance(config["imports"], list) or len(config["imports"]) > 16
            or len(set(map(str, config["imports"]))) != len(config["imports"])):
        raise ValueError("Choose at most 16 explicit Python imports")
    for name in config["imports"]:
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", name, re.ASCII):
            raise ValueError("Probe imports must be dotted Python module names")
    for key in ("worker", "trace", "gpu"):
        if type(config[key]) is not bool:
            raise ValueError("Probe switches must be booleans")
    resources = config["resources"]
    E.keys(resources, set(), {"account", "partition", "cpus", "memory_mib", "wall_seconds"}, "probe resource budget")
    limits = {"cpus": (1, 4, 1), "memory_mib": (256, 4096, 512), "wall_seconds": (30, 300, 120)}
    for key, (minimum, maximum, default) in limits.items():
        item = resources.get(key, default)
        if type(item) is not int or not minimum <= item <= maximum:
            raise ValueError("Tiny compute probe " + key + " exceeds its bounded qualification budget")
        resources[key] = item
    for key in ("account", "partition"):
        if key in resources:
            E.text(resources[key], "probe " + key, 256)
    return copy.deepcopy(config)


def prepare(project, scheduler, config=None, *, manifest=None):
    """Persist a no-clobber script and return native review; never submit it.

    Kubernetes needs an explicit caller-provided Job manifest with an image and
    mounts for the project/report path. Only the first container's command is
    replaced in the returned, reviewed copy; the supplied manifest is retained.
    """
    config = _config(copy.deepcopy(config or {}))
    identifier = "probe-" + uuid.uuid4().hex
    script_path, result_path = _paths(project, identifier)
    if scheduler.scheduler == "htcondor":
        script_path = script_path.with_suffix(".submit")
    config.update(nonce=uuid.uuid4().hex, scheduler=scheduler.scheduler, scope=scheduler.scope,
                  project=str(script_path.parents[3]),
                  result_path=str(result_path), identifier=identifier)
    program = "from raven.engineering.qualification import execute; execute(" + repr(config) + ")"
    command = [config["python"], "-c", program]
    if scheduler.scheduler == "slurm":
        # A verified compute step is needed by Heavy's placement probe. An
        # allocation environment alone must never be promoted to that evidence.
        command = ["srun", "--nodes=1", "--ntasks=1", "--cpus-per-task=1", "--"] + command
    header = "#!/bin/sh\n"
    if scheduler.scheduler == "slurm":
        header += ("#SBATCH --chdir=" + shlex.quote(config["project"]) + "\n"
                   "#SBATCH --output=" + shlex.quote(str(script_path.with_suffix("")) + "-%j.out") + "\n"
                   "#SBATCH --error=" + shlex.quote(str(script_path.with_suffix("")) + "-%j.err") + "\n")
    script = (header + "set -eu\nexec " + shlex.join(command) + "\n").encode()
    if scheduler.scheduler == "htcondor":
        if config["resources"].get("partition"):
            raise ValueError("HTCondor qualification uses native requirements, not a Slurm/PBS partition")
        # HTCondor consumes a submit description, not a shell program. Native
        # new-style arguments quote each argv item and escape quote characters;
        # the reviewed file contains the complete probe program, no mutable
        # secondary executable script.
        arguments = " ".join("'" + item.replace("'", "''") + "'" for item in ["-c", program])
        arguments = '"' + arguments.replace('"', '""') + '"'
        script = ("universe = vanilla\nexecutable = " + json.dumps(config["python"]) + "\n"
                  "arguments = " + arguments + "\n"
                  "getenv = False\nshould_transfer_files = NO\n"
                  'environment = "RAVEN_QUALIFICATION_JOB_ID=$(Cluster).$(Process)"\n'
                  "periodic_remove = (JobStatus == 2 && (time() - JobCurrentStartDate) > "
                  + str(config["resources"]["wall_seconds"]) + ")\nqueue 1\n").encode()
    document = {"schema": SCHEMA, "id": identifier, "config": config,
                "scope": scheduler.scope_key, "script": str(script_path),
                "script_sha256": hashlib.sha256(script).hexdigest(),
                "created_at": time.time(), "status": "prepared", "review": None}
    with S.locked(project, "qualification") as folder:
        output = os.open(script_path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=folder)
        with os.fdopen(output, "wb") as stream:
            stream.write(script)
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(folder)
        if scheduler.scheduler == "kubernetes":
            if manifest is None:
                document.update(status="needs-manifest", reason="Provide a Kubernetes Job manifest with the chosen image and explicit shared project/report mounts")
            else:
                submission = copy.deepcopy(manifest)
                spec = submission.get("spec", {})
                pod = spec.get("template", {}).get("spec", {})
                container = pod.get("containers", [])
                if len(container) != 1 or not isinstance(container[0], dict) or pod.get("initContainers"):
                    raise ValueError("Kubernetes qualification needs an explicit Job container and mounted report path")
                if spec.get("parallelism", 1) != 1 or spec.get("completions", 1) != 1:
                    raise ValueError("Tiny Kubernetes qualification permits exactly one job and one container")
                if spec.get("backoffLimit", 0) != 0 or pod.get("restartPolicy", "Never") != "Never":
                    raise ValueError("Tiny Kubernetes qualification permits no automatic pod retry or restart")
                spec["backoffLimit"] = 0
                pod["restartPolicy"] = "Never"
                mounts = container[0].get("volumeMounts", [])
                mounted = any(isinstance(item, dict) and isinstance(item.get("mountPath"), str)
                              and str(result_path).startswith(item["mountPath"].rstrip("/") + "/") for item in mounts)
                if not mounted:
                    raise ValueError("Mount the explicit shared result directory in the Kubernetes probe container")
                spec["activeDeadlineSeconds"] = config["resources"]["wall_seconds"]
                resources = container[0].setdefault("resources", {})
                for kind in ("requests", "limits"):
                    quantities = resources.setdefault(kind, {})
                    for key, value in quantities.items():
                        if key not in {"cpu", "memory"} and value not in (0, "0"):
                            raise ValueError("A portable tiny qualification probe must not request extra accelerators or extended resources")
                    quantities.update(cpu=str(config["resources"]["cpus"]),
                                      memory=str(config["resources"]["memory_mib"]) + "Mi")
                container[0]["command"] = [config["python"], "-c", program]
                container[0].pop("args", None)
                # Pod UID is not Job UID. The Job controller's native labels
                # expose both job name and controller UID through DownwardAPI.
                bindings = {
                    "RAVEN_QUALIFICATION_JOB_NAME": "metadata.labels['batch.kubernetes.io/job-name']",
                    "RAVEN_QUALIFICATION_JOB_UID": "metadata.labels['batch.kubernetes.io/controller-uid']",
                    "RAVEN_QUALIFICATION_NAMESPACE": "metadata.namespace",
                    "RAVEN_QUALIFICATION_POD_UID": "metadata.uid",
                }
                environment = container[0].get("env", [])
                if not isinstance(environment, list) or any(not isinstance(item, dict) for item in environment):
                    raise ValueError("Kubernetes probe environment must be an explicit list")
                container[0]["env"] = [item for item in environment if item.get("name") not in bindings]
                container[0]["env"].extend({"name": key, "valueFrom": {"fieldRef": {"fieldPath": field}}}
                                            for key, field in bindings.items())
                document["review"] = scheduler.review("submit", submission={"manifest": submission})
        else:
            submission = dict(config["resources"], script=str(script_path), name="raven-qualification")
            if scheduler.scheduler == "htcondor":
                submission.pop("wall_seconds", None)  # enforced in reviewed periodic_remove above
            elif scheduler.scheduler in {"lsf", "flux"}:
                submission.pop("memory_mib", None)
                document["unverified_limits"] = ["The native adapter cannot express portable memory limits; review the site's effective default memory before submitting this tiny probe"]
            document["review"] = scheduler.review("submit", submission=submission)
        return S.write(folder, identifier + ".json", document, exclusive=True)


def load(project, identifier):
    S.name(identifier)
    with S.project_store(project, "qualification") as folder:
        document = S.read(folder, identifier + ".json")
    if document.get("schema") != SCHEMA or document.get("id") != identifier:
        raise ValueError("Qualification state no longer identifies the selected probe")
    with S.project_store(project, "qualification") as folder:
        try:
            document["latest_receipt"] = S.read(folder, identifier + ".latest.json")
        except FileNotFoundError:
            pass
    return document


def _observed_job(scheduler):
    if scheduler == "slurm":
        identifier = os.environ.get("SLURM_JOB_ID", "")
        if os.environ.get("SLURM_ARRAY_JOB_ID") and os.environ.get("SLURM_ARRAY_TASK_ID"):
            identifier = os.environ["SLURM_ARRAY_JOB_ID"] + "_" + os.environ["SLURM_ARRAY_TASK_ID"]
        return identifier
    if scheduler in {"openpbs", "pbspro"}:
        return os.environ.get("PBS_JOBID", "")
    if scheduler == "lsf":
        value = os.environ.get("LSB_JOBID", "")
        index = os.environ.get("LSB_JOBINDEX", "0")
        return value + ("[" + index + "]" if index != "0" and value else "")
    if scheduler == "flux":
        value = os.environ.get("FLUX_JOB_ID", "")
        if value:
            from ..schedulers.flux import canonical_job_id

            return canonical_job_id(value)
        return ""
    if scheduler == "htcondor":
        value = os.environ.get("RAVEN_QUALIFICATION_JOB_ID", "")
        return value if re.fullmatch(r"[0-9]+\.[0-9]+", value) else ""
    if scheduler == "kubernetes":
        return os.environ.get("RAVEN_QUALIFICATION_JOB_NAME", "")
    # Kubernetes Job UID and HTCondor attempt identity cannot be inferred from
    # a PID or host. Their native collector must link the returned receipt.
    return ""


def _observe_native(config, observed_id):
    """One explicitly reviewed, bounded native self-job lookup on compute.

    Missing compute-side scheduler tools/permissions stay unverified. Runtime
    environment job numbers alone never establish the submission/restart epoch.
    """
    if config["scheduler"] == "kubernetes":
        return None  # The Job/controller UID comes from native DownwardAPI.
    if not observed_id:
        return {"status": "unavailable", "reason": "The native runtime did not expose its job ID"}
    try:
        from ..schedulers import create
        from ..slurm import Backend

        client = create(config["scheduler"], Backend(), config["scope"], timeout=3,
                        max_bytes=1 << 20, max_records=64)
        record = client.observe(observed_id)
        if record.get("user") != client.scope["user"]:
            raise ValueError("Compute native observation belongs to another login")
        return {"status": "observed", "identity": record["identity"], "user": record["user"],
                "source_sha256": E.digest(record), "source": "bounded exact native compute-side observation",
                "observed_at": time.time()}
    except (OSError, RuntimeError, ValueError, AttributeError) as exc:
        reason = "".join(char if char.isprintable() else " " for char in str(exc)[:2048])
        return {"status": "unavailable", "reason": reason}


def execute(config):
    """Run inside an explicitly submitted compute job; write one private result.

    This callable executes only modules explicitly listed in the reviewed probe.
    It is deliberately separate from Raven's default startup and doctor path.
    """
    started = time.monotonic()
    checks = []

    def check(name, operation):
        before = time.monotonic()
        try:
            detail = operation()
            row = {"name": name, "status": "passed", "detail": detail}
        except Exception as exc:
            error = type(exc).__name__ + ": " + str(exc)[:2048]
            row = {"name": name, "status": "failed", "error": "".join(char if char.isprintable() else " " for char in error)}
        row["seconds"] = time.monotonic() - before
        checks.append(row)
        return row

    for name in config["imports"]:
        def probe_import(name=name):
            from ..bounded_command import execute as command
            from ..slurm import Backend

            # The reviewed module executes in a bounded allocation-local child;
            # it cannot extend Raven's in-process registry or scheduler trust.
            code = ("import importlib,sys\ntry:\n print(importlib.import_module(sys.argv[1]).__name__)\n"
                    "except Exception as exc:\n print(type(exc).__name__ + ': ' + str(exc)[:1000], file=sys.stderr)\n raise SystemExit(1)\n")
            value = command(Backend(), [sys.executable, "-c", code, name], 3, 4096)
            return {"module": value.strip(), "scope": "allocation-local bounded child process"}

        check("import:" + name, probe_import)
    scratch = None
    try:
        def filesystem():
            nonlocal scratch
            scratch = tempfile.TemporaryDirectory(prefix="raven-qualification-", dir=config["scratch"])
            path = Path(scratch.name) / "read-after-write"
            with path.open("xb") as stream:
                stream.write(b"raven-read-after-write\n")
                stream.flush()
                os.fsync(stream.fileno())
            if path.read_bytes() != b"raven-read-after-write\n":
                raise ValueError("Compute filesystem read-after-write differed")
            return {"bytes": 23, "semantics": "private compute-host scratch; cross-node visibility unverified"}

        check("filesystem", filesystem)
        if scratch is not None:
            def metrics():
                from ..metrics import _record, write_metric

                path = Path(scratch.name) / "metrics.jsonl"
                write_metric(path, {"qualification": 1.0}, t=0)
                record = _record(json.loads(path.read_text()))
                return {"retained_samples": 1, "value": record["metrics"]["qualification"]}

            check("metrics-recording", metrics)
            if config["trace"]:
                observed = _observed_job(config["scheduler"])
                if config["scheduler"] == "slurm" and observed:
                    def trace():
                        from ..tracing.capture import Recorder
                        from ..tracing.protocol import TraceReader

                        with Recorder(scratch.name, job_id=observed, attempt=config["nonce"],
                                      max_bytes=65536, register=False) as recorder:
                            with recorder.span("qualification"):
                                sum(range(100))
                        reader = TraceReader(recorder.path)
                        try:
                            snapshot = reader.refresh()
                        finally:
                            reader.close()
                        if not snapshot["events"] or snapshot["status"] == "INVALID":
                            raise ValueError("The retained recording did not contain verified events")
                        return {"retained_events": len(snapshot["events"]), "status": snapshot["status"]}

                    check("trace-recording", trace)
                else:
                    checks.append({"name": "trace-recording", "status": "unavailable",
                                   "reason": "The current retained recorder requires an exact Slurm recording identity; generic native tracing remains unqualified"})
        else:
            checks.append({"name": "metrics-recording", "status": "unavailable",
                           "reason": "Filesystem readiness failed; a recording could not be created"})
            if config["trace"]:
                checks.append({"name": "trace-recording", "status": "unavailable",
                               "reason": "Filesystem readiness failed; a retained trace could not be created"})
        if config["worker"]:
            if config["scheduler"] == "slurm":
                def worker():
                    from ..heavy.worker_client import WorkerClient

                    client = WorkerClient({"site_kind": "slurm", "resources": {
                        "cpus": 1, "memory_mib": config["resources"]["memory_mib"],
                        "max_tasks": 1, "wall_seconds": 15}},
                        launch_argv=[sys.executable, "-m", "raven.raven_worker"])
                    try:
                        client.start()
                        deadline = time.monotonic() + 10
                        while time.monotonic() < deadline and not client.placement and client.status != "failed":
                            client.poll(.05)
                        if not client.placement:
                            raise ValueError(client.error or "Worker did not reach placement within 10 seconds")
                        return {"stage": "placement", "tasks_executed": 0,
                                "ownership": "Startup probe only; scheduler placement verification remains required before analysis"}
                    finally:
                        client.close()

                check("worker-startup", worker)
            else:
                checks.append({"name": "worker-startup", "status": "unavailable",
                               "reason": "Heavy allocation ownership is currently verified for Slurm; this probe does not bypass that boundary"})
        usage = resource.getrusage(resource.RUSAGE_SELF)
        checks.append({"name": "process-counters", "status": "passed", "detail": {
            "user_cpu_seconds": usage.ru_utime, "system_cpu_seconds": usage.ru_stime,
            "maxrss_native": usage.ru_maxrss,
            "semantics": "Process counters only; scheduler accounting, per-GPU and multi-node counters remain unverified"}})
        if config["gpu"] and shutil.which("nvidia-smi"):
            def gpu():
                from ..bounded_command import execute as command
                from ..slurm import Backend

                text = command(Backend(), ["nvidia-smi", "--query-gpu=uuid,utilization.gpu,memory.used", "--format=csv,noheader"], 3, 65536)
                return {"source": "nvidia-smi", "native_units": "percent,MiB", "rows": text.splitlines()[:32],
                        "semantics": "Accessible NVIDIA device counters only; not per-job attribution or GPU kernel tracing"}

            check("gpu-counters", gpu)
        elif config["gpu"]:
            checks.append({"name": "gpu-counters", "status": "unavailable",
                           "reason": "No qualified local GPU counter command is available. Configure the site's native telemetry provider; this does not imply that a GPU is broken or absent"})
    finally:
        if scratch is not None:
            scratch.cleanup()
    result = {"schema": RESULT, "id": config["identifier"], "nonce": config["nonce"],
              "scheduler": config["scheduler"], "observed_job_id": _observed_job(config["scheduler"]),
              "host": socket.gethostname(), "python": sys.executable,
              "python_version": sys.version.split()[0], "checks": checks,
              "seconds": time.monotonic() - started,
              "status": "failed" if any(row["status"] == "failed" for row in checks) else "partial" if any(row["status"] != "passed" for row in checks) else "passed",
              "qualification": "Actual compute-process checks only; not production-scale, all-node or hardware-family certification"}
    if config["scheduler"] == "kubernetes":
        result["observed_native_binding"] = {
            "namespace": os.environ.get("RAVEN_QUALIFICATION_NAMESPACE", ""),
            "job_uid": os.environ.get("RAVEN_QUALIFICATION_JOB_UID", ""),
            "pod_uid": os.environ.get("RAVEN_QUALIFICATION_POD_UID", ""),
        }
    else:
        result["native_observation"] = _observe_native(config, result["observed_job_id"])
    data = S.encoded(result, limit=65536)
    S.name(config["identifier"])
    _, target = _paths(config["project"], config["identifier"])
    if str(target) != config["result_path"]:
        raise ValueError("Compute result path differs from its reviewed project namespace")
    with S.project_store(config["project"], "qualification") as folder:
        output = os.open(target.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=folder)
        with os.fdopen(output, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(folder)
    return result


def collect(project, identifier, scheduler, identity):
    """Verify result binding against the exact current native attempt.

    The caller must supply the identity from the submission receipt or native
    lookup. Missing environment linkage remains unverified, never silently good.
    """
    from ..schedulers.core import validate_identity

    identity = validate_identity(identity)
    document = load(project, identifier)
    if (document["scope"] != scheduler.scope_key or identity["scope"] != scheduler.scope_key
            or identity["scheduler"] != scheduler.scheduler):
        raise ValueError("Qualification result belongs to another scheduler authority")
    current = scheduler.fresh_detail(identity)
    if current.get("identity") != identity:
        raise ValueError("Scheduler attempt changed; this result cannot qualify the replacement job")
    if current.get("user") != scheduler.scope.get("user"):
        raise ValueError("Qualification job owner differs from the selected login")
    from .. import projects

    with S.project_store(project, "qualification") as folder:
        filename = identifier + ".result.json"
        before = os.stat(filename, dir_fd=folder, follow_symlinks=False)
        S._private(before)
        raw, size = projects._read(folder, filename, 65536)
        if science_files.signature(before) != science_files.signature(os.stat(filename, dir_fd=folder, follow_symlinks=False)):
            raise ValueError("Qualification result changed during inspection")
        reference = {"path": document["config"]["result_path"], "sha256": hashlib.sha256(raw).hexdigest(),
                     "size": size, "signature": list(science_files.signature(before))}
    result = E.loads(raw)
    E.keys(result, {"schema", "id", "nonce", "scheduler", "observed_job_id", "host", "python", "python_version",
                    "checks", "seconds", "status", "qualification"}, {"observed_native_binding", "native_observation"}, "compute probe result")
    for key in ("id", "nonce", "scheduler", "observed_job_id", "host", "python", "python_version", "qualification"):
        E.text(result[key], "compute result " + key, 4096, empty=key == "observed_job_id")
    E.finite(result["seconds"], "compute probe elapsed seconds", 0)
    if (not isinstance(result["checks"], list) or not 1 <= len(result["checks"]) <= 32
            or result["status"] not in {"passed", "partial", "failed"}):
        raise ValueError("Compute result has invalid readiness checks")
    expected_status, seen = "passed", set()
    for check in result["checks"]:
        E.keys(check, {"name", "status"}, {"detail", "error", "reason", "seconds"}, "compute check")
        E.text(check["name"], "check name", 256)
        if check["name"] in seen or check["status"] not in {"passed", "failed", "unavailable"}:
            raise ValueError("Compute result check identities or statuses are invalid")
        seen.add(check["name"])
        if check["status"] == "failed":
            expected_status = "failed"
        elif check["status"] == "unavailable" and expected_status != "failed":
            expected_status = "partial"
    required = {"filesystem", "metrics-recording", "process-counters"}
    required.update("import:" + name for name in document["config"]["imports"])
    required.update(key + "-" + suffix for key, suffix in (("worker", "startup"), ("trace", "recording"), ("gpu", "counters"))
                    if document["config"][key])
    if seen != required:
        raise ValueError("Compute result is missing a requested check or contains an unexpected check")
    if result["status"] != expected_status:
        raise ValueError("Compute result status differs from its actual checks")
    if (result.get("schema") != RESULT or result.get("id") != identifier
            or result.get("nonce") != document["config"]["nonce"]
            or result.get("scheduler") != scheduler.scheduler):
        raise ValueError("Compute result nonce or scheduler differs from the reviewed probe")
    observed = result.get("observed_job_id")
    verified = observed == identity["native_id"]
    if observed and not verified:
        raise ValueError("Compute result identifies another native job")
    if scheduler.scheduler == "kubernetes":
        binding = result.get("observed_native_binding", {})
        E.keys(binding, set(), {"namespace", "job_uid", "pod_uid"}, "Kubernetes compute binding")
        for key, value in binding.items():
            E.text(value, "Kubernetes compute " + key, 1024, empty=True)
        if (binding.get("namespace") and binding["namespace"] != identity.get("namespace")
                or binding.get("job_uid") and binding["job_uid"] != identity.get("uid")):
            raise ValueError("Kubernetes compute namespace or Job UID differs from the exact native attempt")
        verified = verified and bool(binding.get("namespace") and binding.get("job_uid") and binding.get("pod_uid"))
    else:
        observation = result.get("native_observation")
        if not isinstance(observation, dict) or observation.get("status") != "observed":
            verified = False
        else:
            E.keys(observation, {"status", "identity", "user", "source_sha256", "source", "observed_at"}, label="compute native observation")
            observed_identity = validate_identity(observation["identity"])
            if observed_identity != identity or observation["user"] != scheduler.scope.get("user"):
                raise ValueError("Compute-reported native attempt or owner differs from the selected scheduler attempt")
            if not E.HASH.fullmatch(observation["source_sha256"]):
                raise ValueError("Compute native source digest is invalid")
            E.finite(observation["observed_at"], "native observation time", 0)
    limits = document.get("unverified_limits", [])
    status = result["status"] if verified else "unverified"
    if status == "passed" and limits:
        status = "partial"
    scheduler_state = current.get("state", "UNKNOWN")
    if status in {"passed", "partial"}:
        if scheduler_state in {"FAILED", "CANCELLED"}:
            status = "failed"
        elif scheduler_state != "COMPLETED":
            status = "partial"
    receipt = {"schema": "raven.site-qualification/v1", "probe": identifier, "identity": identity,
               "result": result, "source": reference, "identity_verified": verified,
               "status": status, "unverified_limits": limits, "scheduler_state": scheduler_state,
               "reason": "Fresh owned native attempt matches the compute-side identity observation" if verified else "Only compute readiness or a job number was observed; an exact native execution attempt remains unverified",
               "integrity": "Hash and nonce detect accidental mismatches; they are not remote attestation"}
    with S.locked(project, "qualification") as folder:
        filename = identifier + ".receipt-" + E.digest(receipt) + ".json"
        try:
            S.write(folder, filename, receipt, exclusive=True)
        except ValueError:
            # The same immutable observation is an idempotent collection. A
            # later terminal scheduler state publishes a different receipt.
            if S.read(folder, filename) != receipt:
                raise
        S.write(folder, identifier + ".latest.json", {"path": filename, "status": status,
                                                     "sha256": E.digest(receipt)})
        return receipt
