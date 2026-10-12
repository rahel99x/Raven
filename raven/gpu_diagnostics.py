"""Bounded GPU evidence from the actual scheduler, allocation and file paths.

No sampler, plugins, job submission, or configuration writes are required.
Live probes run vendor tools through Raven's allocation-scoped sampling path.
"""
from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import shlex
import subprocess
import time

from . import __version__
from .model import gres_gpus
from .gpu_collectors import parse_probe_output, probe_command
from .remote import LocalFiles, SshBackend
from .slurm import Backend, CommandError, JOB_FMT, NVSMI, SACCT_FIELDS, Slurm, parse_jobs, parse_nvsmi, parse_sacct

MAX_JOBS = 8
MAX_NODES = 4
MAX_COMMANDS = 128
MAX_STREAM = 16_384
MAX_EVIDENCE = 1 << 20
MAX_TRACE = 1 << 20
MAX_SERIES = 2 << 20
DEADLINE = 60.0
JOB_ID = re.compile(r"[0-9]+(?:_[0-9]+)?(?:\+[0-9]+)?\Z")
TOOLS = ("squeue", "scontrol", "sacct", "srun", "python3", "nvidia-smi", "amd-smi", "xpu-smi", "ssh", "lspci")
TOOL_PROBE = ('for tool in ' + ' '.join(TOOLS) + '; do '
              'if path=$(command -v "$tool" 2>/dev/null); then printf "%s|%s\\n" "$tool" "$path"; fi; done')


def valid_job_id(value):
    if value == "all" or JOB_ID.fullmatch(value):
        return value
    raise ValueError("use all or an individual numeric job ID, such as 123 or 123_4")


def _text(value):
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value or "")


class EvidenceBackend(Backend):
    """Record both streams and failures once, preserving production argv.

    Native and SSH backends expose complete error output here. Replay and test
    backends keep their own execution semantics. No shell interpolation occurs.
    """
    def __init__(self, inner, duration=DEADLINE):
        self.inner = inner
        # Preserve the existing collection owner for local host-list expansion.
        # Recording evidence must not reintroduce extra scheduler commands.
        self.collection = getattr(inner, "collection", None)
        self.deadline = time.monotonic() + duration
        self.records = []
        self.retained = 0
        self.omitted = 0

    def _keep(self, text):
        encoded = _text(text).encode("utf-8", "replace")
        room = max(0, min(MAX_STREAM, MAX_EVIDENCE - self.retained))
        excerpt = encoded[:room].decode("utf-8", "ignore")
        self.retained += len(excerpt.encode("utf-8"))
        return excerpt, len(encoded), len(encoded) > room

    def run(self, cmd, timeout=8.0):
        cmd = list(cmd)
        start = time.monotonic()
        remaining = self.deadline - start
        entry = dict(argv=cmd, timeout_seconds=min(timeout, max(0, remaining)),
                     returncode=None, status="error", stdout="", stderr="")
        out, err, rc = "", "", None
        failure = None
        try:
            if remaining <= 0 or len(self.records) >= MAX_COMMANDS:
                entry["status"] = "skipped"
                raise CommandError("GPU diagnostic command or 60-second time budget exhausted")
            actual_timeout = min(timeout, remaining)
            if isinstance(self.inner, SshBackend):
                rc, out, err = self.inner.runner(self.inner.argv(cmd), min(actual_timeout + 5, remaining))
            elif type(self.inner) is Backend:
                result = subprocess.run(cmd, capture_output=True, text=True,
                                        encoding="utf-8", errors="replace", timeout=actual_timeout)
                rc, out, err = result.returncode, result.stdout, result.stderr
            else:
                out, _ = self.inner.run(cmd, actual_timeout)
                rc = 0
            if rc != 0:
                raise CommandError(f"{cmd[0]} exit {rc}: {(err or out).strip()[:4096]}")
            entry["status"] = "ok"
        except subprocess.TimeoutExpired as exc:
            out, err = _text(exc.stdout), _text(exc.stderr)
            entry["status"] = "timeout"
            failure = CommandError(f"{cmd[0]}: timed out after {entry['timeout_seconds']:g}s")
        except (CommandError, OSError) as exc:
            failure = exc if isinstance(exc, CommandError) else CommandError(f"{cmd[0]}: {exc}")
        finally:
            entry["returncode"] = rc
            entry["elapsed_seconds"] = round(time.monotonic() - start, 4)
            for field, value in (("stdout", out), ("stderr", err)):
                excerpt, size, truncated = self._keep(value)
                entry[field], entry[field + "_bytes"], entry[field + "_truncated"] = excerpt, size, truncated
            if failure:
                entry["error"] = str(failure)[:4096]
            if len(self.records) < MAX_COMMANDS:
                self.records.append(entry)
            else:
                self.omitted += 1
        if failure:
            raise failure
        return out, time.monotonic() - start


def _counter(value, *, percent=False):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value) and value >= 0 and (not percent or value <= 100)
    except OverflowError:
        return False


def _failure_code(message):
    lower = message.lower()
    if "budget exhausted" in lower:
        return "diagnostic_budget"
    if "timed out" in lower or "timeout" in lower:
        return "probe_timeout"
    if "permission denied" in lower or "not permitted" in lower:
        return "permission_denied"
    if any(marker in lower for marker in (
            "mixed gpu vendor indices", "numeric slurm gpu ids",
            "gpu allocation identifiers unavailable", "gpu probe allocation identity",
            "no gpu device could be matched", "device count exceeds the requested job allocation",
            "runtime visibility", "cuda visibility", "cuda_visible_devices")):
        return "allocation_mapping_unavailable"
    if "driver" in lower or "nvml" in lower or "driver/library" in lower:
        return "driver_unavailable"
    if "not found" in lower or "no such file" in lower:
        return "command_missing"
    if "invalid job" in lower or "no allocation" in lower:
        return "allocation_ended"
    if "step creation" in lower or "resources" in lower or "overlap" in lower:
        return "allocation_probe_failed"
    return "sampling_failed"


def _details_match(detail, job_id):
    """Accept exact array/component identity even when JobId is a raw ID."""
    if detail.get("JobId") == job_id:
        return True
    if "_" in job_id:
        parent, task = job_id.split("_", 1)
        return detail.get("ArrayJobId") == parent and detail.get("ArrayTaskId") == task
    if "+" in job_id:
        parent, offset = job_id.split("+", 1)
        return detail.get("HetJobId") == parent and detail.get("HetJobOffset") == offset
    return False


def diagnose(cfg, backend, files, *, user, job_id="all", no_gpu=False,
             state_path="", no_state=False, max_jobs=MAX_JOBS):
    """Inspect current user jobs and optionally one exact accounting job."""
    valid_job_id(job_id)
    max_jobs = max(1, min(MAX_JOBS, int(max_jobs)))
    managed = bool(cfg.get("raven", {}).get("managed"))
    gpu_mode = cfg.get("raven", {}).get("collection", {}).get("gpu", "existing-telemetry")
    evidence = EvidenceBackend(backend)
    # Native remote reads use the recorder's deadline. Managed files already
    # own typed metadata/byte admission; replacing that adapter would lose it.
    if getattr(files, "remote", False) and not managed:
        from .remote import RemoteFiles
        files = RemoteFiles(evidence, timeout=cfg["timeouts"]["command"])
    slurm = Slurm(evidence, user, timeout=cfg["timeouts"]["command"], gpu_timeout=cfg["timeouts"]["gpu"], gpu_provider=cfg.get("gpu_provider", "auto"))
    checks, jobs_report = [], []

    def add(code, status, detail, *, job="", next_step="", evidence_data=None):
        item = dict(code=code, status=status, detail=_text(detail)[:4096])
        if job:
            item["job_id"] = job
        if next_step:
            item["next_step"] = next_step
        if evidence_data is not None:
            item["evidence"] = evidence_data
        checks.append(item)
        return item

    if not managed:
        try:
            output, _ = evidence.run(["sh", "-c", TOOL_PROBE], slurm.timeout)
            available = dict(line.split("|", 1) for line in output.splitlines() if "|" in line)
            add("command_paths", "ok", "Command paths on the scheduler connection host.", evidence_data=available)
        except CommandError as exc:
            add("command_paths", "warning", exc)

    saved_gpu = None
    if state_path and not no_state:
        try:
            with open(os.path.join(state_path, "ui.json"), "rb") as handle:
                raw = handle.read((1 << 20) + 1)
            if len(raw) > (1 << 20):
                raise ValueError("saved UI state exceeds the diagnostic read limit")
            ui = json.loads(raw)
            saved_gpu = ui.get("gpu") if isinstance(ui, dict) and type(ui.get("gpu")) is bool else None
            add("saved_sampling", "warning" if saved_gpu is False and not managed else "ok",
                f"Saved GPU toggle: {saved_gpu if saved_gpu is not None else 'not set'}.",
                next_step="Use :gpu on in Raven." if saved_gpu is False and not managed else "")
        except FileNotFoundError:
            pass
        except (OSError, ValueError, RecursionError) as exc:
            add("saved_sampling", "warning", f"Could not read saved GPU preference: {exc}")
    enabled = not managed and bool(saved_gpu if saved_gpu is not None else cfg["gpu_sampling"]) and not no_gpu
    if managed:
        add("sampling_settings", "ok" if gpu_mode == "existing-telemetry" else "warning",
            f"Managed GPU collection: {gpu_mode}. Raven reads existing job telemetry; automatic device probes are disabled.",
            next_step="Select the job's Series view and provide its existing logs/gpu-util-JOBID.csv trace. :gpu on does not enable allocation probes.",
            evidence_data=dict(collection_gpu=gpu_mode, automatic_device_probes=False,
                               config_gpu_sampling=bool(cfg["gpu_sampling"]), cli_no_gpu=no_gpu, saved_gpu=saved_gpu))
    else:
        add("sampling_settings", "ok" if enabled else "warning",
            f"GPU sampling {'enabled' if enabled else 'disabled'}; interval {cfg['intervals']['gpu']:g}s; command timeout {slurm.gpu_timeout:g}s.",
            next_step="Remove --no-gpu and use :gpu on; check gpu_sampling in the selected profile." if not enabled else "",
            evidence_data=dict(config_gpu_sampling=bool(cfg["gpu_sampling"]), cli_no_gpu=no_gpu, saved_gpu=saved_gpu))

    # Host inventory is useful diagnostically but never supplies job graphs.
    host_samples = []
    if not managed:
        try:
            output, _ = evidence.run(probe_command(slurm.gpu_provider, slurm.gpu_timeout - 1,
                                                  resolve_visibility=False), slurm.gpu_timeout)
            host_samples, _, warnings = parse_probe_output(output, require_scope=False)
            add("host_gpu_inventory", "ok" if host_samples else "warning",
                f"{len(host_samples)} devices visible on the scheduler connection host. This is not a job measurement.",
                evidence_data=[{**asdict(sample), "node": "scheduler-host"} for sample in host_samples])
            for warning in warnings:
                add("host_gpu_provider", "warning", warning)
        except (CommandError, ValueError) as exc:
            add("host_gpu_inventory", "warning", exc,
                next_step="Check python3 and the selected GPU utility/driver. A GPU-free login host is normal on a cluster.")
    if not host_samples and not managed:
        try:
            output, _ = evidence.run(["lspci", "-nn"], slurm.timeout)
            devices = [line for line in output.splitlines() if any(word in line.lower() for word in ("vga", "3d controller", "display controller"))]
            add("pci_gpu_inventory", "warning", "GPU display/controller devices on the connection host.", evidence_data=devices[:32])
            if devices:
                add("gpu_provider_setup", "warning", "Hardware inventory does not establish allocation ownership or available counters.",
                    next_step="Use :gpuprovider auto (or nvidia, amd, intel). Install the matching vendor utility and driver; check Slurm GPU GRES and compute-node python3.")
        except CommandError as exc:
            add("pci_gpu_inventory", "warning", exc)

    live_jobs = []
    try:
        output, _ = evidence.run(["squeue", "-u", user, "-h", "-o", JOB_FMT], slurm.timeout)
        live_jobs = parse_jobs(output)
        ignored = [job.id for job in live_jobs if not JOB_ID.fullmatch(job.id)]
        live_jobs = [job for job in live_jobs if JOB_ID.fullmatch(job.id)]
        if ignored:
            add("nonindividual_queue_ids", "warning", f"Skipped {len(ignored)} nonindividual or invalid queue IDs; compressed array groups cannot be probed as individual allocations.",
                evidence_data=ignored[:32], next_step="Use --gpu-check with an exact array task ID, such as 123_4.")
        for job in live_jobs:
            job.user = user
        add("queue", "ok", f"Read {len(live_jobs)} jobs for scheduler user {user}.")
        if any(job.state == "RUNNING" and not job.gpus for job in live_jobs):
            try:
                allocations = slurm.gpu_allocations()
                corrected = []
                for job in live_jobs:
                    allocation = allocations.get(job.id)
                    if allocation and allocation[1] > job.gpus:
                        corrected.append(dict(job_id=job.id, queue_gpus=job.gpus, allocated_gpus=allocation[1]))
                        job.gpu_type, job.gpus = allocation
                add("allocation_batch", "ok" if allocations else "warning",
                    f"The allocation TRES lookup detected GPUs missing from the per-node queue field for {len(corrected)} jobs.",
                    evidence_data=corrected,
                    next_step="If this Slurm version lacks tres-alloc, compare the exact job's TRES in allocation_fields." if not allocations else "")
            except CommandError as exc:
                add("allocation_batch", "warning", exc)
    except CommandError as exc:
        add("queue", "error", exc, next_step="Check squeue and the Slurm controller before GPU sampling.")

    selected = live_jobs if job_id == "all" else [job for job in live_jobs if job.id == job_id]
    finished = None
    if job_id != "all" and not selected:
        try:
            output, _ = evidence.run(["sacct", "-u", user, "-j", job_id, "-n", "-P", "-o", SACCT_FIELDS], slurm.timeout)
            finished = next((job for job in parse_sacct(output) if job.id == job_id), None)
        except CommandError as exc:
            add("accounting", "warning", exc, job=job_id)
        if finished is None:
            add("job_not_found", "error", "The exact job was not found in this user's queue or accounting results.", job=job_id,
                next_step="Check --user, --profile, and the job ID. Accounting can be disabled on a desktop.")
    if not selected and job_id == "all":
        add("no_current_jobs", "warning", "There are no current jobs to test. Host inventory alone cannot create a job graph.",
            next_step="Run this check while a GPU job is running, or supply an exact completed job ID.")
    # Prefer running GPU jobs but retain CPU and pending allocations as evidence.
    selected = sorted(selected, key=lambda job: (job.state != "RUNNING", not bool(job.gpus)))
    omitted_jobs = max(0, len(selected) - max_jobs)
    selected = selected[:max_jobs]
    if omitted_jobs:
        add("job_limit", "warning", f"Detailed probes cover {max_jobs} jobs; {omitted_jobs} further jobs were not probed.",
            next_step="Use --gpu-check JOBID to inspect another exact job.")
    node_cache = {}
    for job in selected + ([finished] if finished is not None else []):
        entry = dict(job_id=job.id, state=job.state, name=job.name, allocated_gpus=job.gpus,
                     workdir=job.workdir, sampling_eligible=False, graph_samples=0)
        jobs_report.append(entry)
        detail = {}
        if job in selected:
            job.hosts = slurm.hostnames(job.nodelist) if job.nodelist else []
            try:
                detail = slurm.details(job.id)
                if not _details_match(detail, job.id):
                    add("job_details_identity", "error", "Allocation details did not match the exact requested job ID; the response was discarded.", job=job.id)
                    detail = {}
                fields = ("JobId", "ArrayJobId", "ArrayTaskId", "HetJobId", "HetJobOffset", "JobState", "NodeList", "NumNodes", "WorkDir", "TRES", "AllocTRES", "ReqTRES", "TresPerJob", "TresPerNode", "TresPerTask", "Gres")
                entry["allocation_fields"] = {key: detail[key] for key in fields if key in detail}
                _, detail_count = gres_gpus(detail.get("AllocTRES") or detail.get("TRES") or "")
                if detail_count > job.gpus:
                    add("queue_gpu_underreported", "warning", f"Queue and batch allocation lookup report {job.gpus} GPUs but allocation details report {detail_count}.", job=job.id,
                        next_step="Inspect the allocation_batch check; the diagnostic can probe this exact allocation but the queue detection path has insufficient data.")
                entry["allocated_gpus"] = max(job.gpus, detail_count)
            except CommandError as exc:
                add("job_details", "warning", exc, job=job.id)
            for node in job.hosts[:MAX_NODES]:
                if node not in node_cache:
                    try:
                        output, _ = evidence.run(["scontrol", "show", "node", node, "-o"], slurm.timeout)
                        from .slurm import parse_kv
                        fields = parse_kv(output)
                        node_cache[node] = {key: fields[key] for key in ("NodeName", "State", "Gres", "GresUsed", "CfgTRES", "AllocTRES") if key in fields}
                    except CommandError as exc:
                        node_cache[node] = {"error": str(exc)}
            entry["nodes"] = {node: node_cache[node] for node in job.hosts[:MAX_NODES]}
            if len(job.hosts) > MAX_NODES:
                add("node_limit", "warning", f"Node evidence covers the first {MAX_NODES}/{len(job.hosts)} resolved hosts. The allocation srun probe requests at most {MAX_NODES} nodes; device coverage is partial.", job=job.id)
        if not entry["allocated_gpus"]:
            add("allocation_missing", "warning", "Slurm reports no GPU allocation for this job; Raven does not assign host-wide GPU readings to CPU jobs.", job=job.id,
                next_step="On Fedora, configure GPU GRES for the compute node and request --gpus=1 or --gres=gpu:1. A visible desktop GPU alone is not an allocation.")
        elif job.state != "RUNNING":
            add("not_running", "warning", f"Job state {job.state} cannot supply new live samples. Retained samples and a recorded trace can still be viewed.", job=job.id)
        elif managed:
            add("existing_telemetry", "ok" if gpu_mode == "existing-telemetry" else "warning",
                "Managed Raven does not launch GPU sampling steps. The diagnostic checks the job's existing trace and retained measurements.",
                job=job.id)
        elif not enabled:
            add("sampling_disabled", "warning", "The job has GPUs, but the effective sampling preference is off.", job=job.id, next_step="Use :gpu on.")
        else:
            entry["sampling_eligible"] = True
            # If the diagnostic discovered a missing count, probe that exact job.
            probe_job = replace(job, gpus=entry["allocated_gpus"], hosts=job.hosts[:MAX_NODES], nodes=min(job.nodes, MAX_NODES))
            try:
                start_command = len(evidence.records)
                samples = slurm.gpu(probe_job)
                entry["devices"] = [asdict(sample) for sample in samples]
                attempts = evidence.records[start_command:]
                entry["sampling_scope"] = "devices associated by exact job identity and stable GPU IDs or a complete visible device set"
                for warning in slurm.gpu_probe_warnings.get(job.id, ()):
                    add("gpu_partial_coverage", "warning", warning, job=job.id)
                valid = sum(_counter(sample.util, percent=True) for sample in samples)
                entry["graph_samples"] = valid
                if valid:
                    add("live_samples", "ok", f"Read utilization for {valid}/{len(samples)} observed devices. New rate and efficiency-proxy curves can be recorded.", job=job.id)
                else:
                    add("utilization_unsupported", "warning", "Devices were detected, but utilization counters are unavailable or invalid; these are gaps, not zero utilization.", job=job.id,
                        next_step="Inspect the selected vendor output in commands.jsonl. Some devices, MIG modes, and drivers do not expose this counter.")
            except CommandError as exc:
                code = _failure_code(str(exc))
                guidance = ("Read the allocation probe in commands.jsonl; fix the reported step, command, driver, or permission failure.")
                if code == "allocation_mapping_unavailable":
                    guidance = ("Read the srun reply in commands.jsonl: compare the exact job ID, Slurm GPU IDs, "
                                "runtime visibility, and resolved device UUIDs. On NVIDIA, Raven resolves numeric "
                                "CUDA_VISIBLE_DEVICES with the CUDA driver inside the allocation; if resolution "
                                "failed, check its reported reason and access to libcuda.so.1 there. An AMD "
                                "display device does not require an AMD utility to sample an allocated NVIDIA "
                                "GPU. Do not change device visibility to include unallocated GPUs.")
                add(code, "error", exc, job=job.id, next_step=guidance)

        # The delimiter-separated queue/accounting path preserves spaces.
        # scontrol's ordinary key/value parser can truncate that same path.
        workdir = job.workdir or detail.get("WorkDir")
        if not isinstance(workdir, str) or not os.path.isabs(workdir) or "\0" in workdir:
            workdir = ""
        path = os.path.join(workdir, "logs", f"gpu-util-{job.id}.csv") if workdir else ""
        entry["trace"] = dict(path=path, rows=0)
        if not path:
            add("trace_workdir_missing", "warning", "No work directory is available to locate the optional GPU trace.", job=job.id)
        else:
            try:
                metadata = files.snapshot_stat(path)
                entry["trace"]["bytes"] = metadata["size"]
                rows = slurm.gpu_trace(path, files, max_bytes=MAX_TRACE)
                entry["trace"]["rows"] = len(rows)
                valid = sum(_counter(row.get("util"), percent=True) for row in rows)
                entry["trace"]["valid_utilization_rows"] = valid
                add("trace_read", "ok" if valid else "warning", f"Read {len(rows)} trace rows; {valid} have valid utilization. Only the last 1 MiB is inspected.", job=job.id)
            except (OSError, ValueError) as exc:
                add("trace_unavailable", "warning", f"{path}: {exc}", job=job.id,
                    next_step=("GPU rate graphs need an existing trace producer inside the job; see the GPU guide."
                               if managed else "This trace is optional when live sampling works. Use the GPU guide to record it inside the job."))

        if state_path and not no_state:
            safe_id = "".join(char if char.isalnum() or char in "_-" else "_" for char in job.id)
            series_path = os.path.join(state_path, "series", safe_id + ".jsonl")
            entry["retained_gpu_rows"] = entry["retained_valid_gpu_rows"] = 0
            try:
                local = LocalFiles()
                metadata = local.snapshot_stat(series_path)
                offset = max(0, metadata["size"] - MAX_SERIES)
                data = local.read(series_path, offset, MAX_SERIES)
                if offset:
                    _, _, data = data.partition(b"\n")
                malformed = 0
                for raw in data.splitlines():
                    try:
                        item = json.loads(raw)
                    except (ValueError, RecursionError):
                        malformed += 1
                        continue
                    if not isinstance(item, dict) or item.get("k") != "gpu" or not _counter(item.get("t")):
                        continue
                    devices = item.get("gpu")
                    if not isinstance(devices, dict):
                        malformed += 1
                        continue
                    entry["retained_gpu_rows"] += 1
                    if any(isinstance(values, (list, tuple)) and values and _counter(values[0], percent=True) for values in devices.values()):
                        entry["retained_valid_gpu_rows"] += 1
                add("retained_samples", "ok" if entry["retained_valid_gpu_rows"] else "warning",
                    f"{entry['retained_gpu_rows']} retained GPU rows; {entry['retained_valid_gpu_rows']} have measured utilization; {malformed} malformed lines omitted.", job=job.id,
                    evidence_data=dict(path=series_path, bytes=metadata["size"], inspected_bytes=len(data), tail_only=bool(offset)))
            except OSError as exc:
                add("retained_samples", "warning", f"Retained GPU cache unavailable: {exc}", job=job.id)

    errors = [item for item in checks if item["status"] == "error"]
    available_jobs = sum(bool(item["graph_samples"] or item["trace"].get("valid_utilization_rows") or item.get("retained_valid_gpu_rows")) for item in jobs_report)
    return dict(schema="raven.gpu-check/v1", raven_version=__version__,
                generated_at=datetime.now(timezone.utc).isoformat(), mode="remote" if getattr(files, "remote", False) else "local",
                user=user, profile=cfg.profile_name, requested_job=job_id, sampling_enabled=enabled,
                collection_mode=gpu_mode if managed else "allocation-probes",
                scope="NVIDIA/AMD/Intel telemetry, verified job allocations, optional NVIDIA CSV traces, and retained scheduler samples. Host inventory is not job telemetry.",
                gpu_provider=slurm.gpu_provider,
                limits=dict(jobs=MAX_JOBS, nodes_per_job=MAX_NODES, commands=MAX_COMMANDS, seconds=DEADLINE,
                            stream_bytes=MAX_STREAM, retained_evidence_bytes=MAX_EVIDENCE, trace_bytes=MAX_TRACE, series_bytes=MAX_SERIES),
                summary=dict(errors=len(errors), warnings=sum(item["status"] == "warning" for item in checks),
                             jobs_checked=len(jobs_report), jobs_omitted=omitted_jobs, jobs_with_graph_data=available_jobs),
                checks=checks, jobs=jobs_report, commands=evidence.records, commands_omitted=evidence.omitted,
                ok=not errors)


def render(result):
    def plain(value):
        # Captured control sequences must never execute in a diagnostic terminal.
        return "".join(char if char == "\n" or char.isprintable() and char != "\x1b" else "?" for char in _text(value))
    lines = [f"Raven {plain(result['raven_version'])} / GPU detection check", plain(result["scope"]),
             f"User: {plain(result['user'])}  Profile: {plain(result['profile'] or 'default')}  Job: {plain(result['requested_job'])}", ""]
    for check in result["checks"]:
        lines.append(f"{plain(check['status']).upper():7} {plain(check['code'])}" + (f" [{plain(check['job_id'])}]" if check.get("job_id") else ""))
        lines.append("  " + plain(check["detail"]).replace("\n", "\n  "))
        if check.get("evidence") is not None:
            lines.append("  Evidence: " + plain(json.dumps(check["evidence"], ensure_ascii=True)))
        if check.get("next_step"):
            lines.append("  Next: " + plain(check["next_step"]))
    lines.extend(["", "Command evidence (bounded excerpts):"])
    for number, command in enumerate(result["commands"], 1):
        lines.append(f"{number:3}. {command['status'].upper()} {plain(shlex.join(command['argv']))}")
        for field in ("error", "stderr", "stdout"):
            if command.get(field):
                lines.append("     " + field + ": " + plain(command[field]).replace("\n", "\n     "))
        if command.get("stdout_truncated") or command.get("stderr_truncated"):
            lines.append("     Output excerpt was truncated; original byte counts are in report.json.")
    summary = result["summary"]
    lines.extend(["", f"Result: {summary['errors']} errors, {summary['warnings']} warnings; {summary['jobs_with_graph_data']}/{summary['jobs_checked']} checked jobs have graph data.",
                  ("Existing telemetry needs a producer writing new measurements; Raven reads it without launching sampling steps."
                   if result.get("collection_mode") in ("existing-telemetry", "off") else
                   "A successful probe is a point-in-time check. Restart Raven after updating, then allow GPU sampling to run.")])
    return "\n".join(lines)


def save(result, directory):
    """Create a private new directory; preserve any existing report or path."""
    path = Path(directory).expanduser().absolute()
    path.mkdir(mode=0o700)
    artifacts = {"report.json": json.dumps(result, indent=2, allow_nan=False) + "\n",
                 "report.txt": render(result) + "\n",
                 "commands.jsonl": "".join(json.dumps(command, allow_nan=False) + "\n" for command in result["commands"])}
    for name, text in artifacts.items():
        fd = os.open(path / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
    return str(path)
