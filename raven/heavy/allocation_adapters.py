"""Structured Raven launch plans; no scheduler commands run while planning.

The generic Slurm adapter is deliberately marked unqualified until exercised
on a site's real disposable allocation. It never uses an arbitrary shell
wrapper, automatic overlap, or a missing-Slurm-to-desktop fallback.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import sys
import time


@dataclass(frozen=True)
class VerifiedReuse:
    """Receipt supplied by a fresh owned-allocation review, never a JobID alone."""

    job_id: str
    cluster: str
    owner_uid: int
    submit_time: str
    start_time: str
    observed_at: float
    remaining_seconds: int
    reserved_cpus: int
    reserved_memory_mib: int
    node_names: tuple[str, ...]
    exclusive_worker_resources: bool
    exact_step_supported: bool


def _integer(value, name, minimum, maximum):
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise ValueError(f"{name} must be an integer in {minimum}..{maximum}")
    return value


def _label(value, name, *, optional=False):
    if optional and value in ("", None):
        return ""
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"[A-Za-z0-9_.@:+/-]{1,128}", value)
        or value.startswith("-")
    ):
        raise ValueError(f"Invalid {name}; use a structured identifier")
    return value


def _duration(seconds):
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, seconds = divmod(rest, 60)
    return (f"{days}-" if days else "") + f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _ssh_options(value):
    """Preserve connection routing without enabling PTYs or command wrappers."""
    if not isinstance(value, list) or len(value) > 64:
        raise ValueError("SSH options must be a bounded argument list")
    allowed = {
        "addressfamily",
        "bindaddress",
        "compression",
        "connectionattempts",
        "connecttimeout",
        "identityfile",
        "identitiesonly",
        "proxyjump",
        "userknownhostsfile",
        "globalknownhostsfile",
        "hostkeyalias",
        "stricthostkeychecking",
        "port",
        "serveraliveinterval",
        "serveralivecountmax",
        "tcpkeepalive",
        "controlmaster",
        "controlpath",
        "controlpersist",
        "loglevel",
    }
    result = []
    for offset in range(0, len(value), 2):
        option = value[offset]
        argument = value[offset + 1] if offset + 1 < len(value) else None
        if (
            option not in ("-p", "-i", "-J", "-o")
            or not isinstance(argument, str)
            or not argument
            or len(argument) > 512
            or any(c in argument for c in "\0\r\n")
        ):
            raise ValueError(
                "Unsupported SSH launch option; use explicit -p, -i, -J, -o options or an approved SSH alias"
            )
        if option == "-p" and (
            not argument.isdigit() or not 1 <= int(argument) <= 65535
        ):
            raise ValueError("Invalid SSH port")
        if option == "-o":
            key, separator, setting = argument.partition("=")
            if not separator or key.lower() not in allowed or not setting:
                raise ValueError(
                    "SSH option cannot wrap commands, alter stream ownership, or request a terminal"
                )
            if key.lower() == "stricthostkeychecking" and setting.lower() not in (
                "yes",
                "ask",
                "accept-new",
            ):
                raise ValueError("SSH host-key verification cannot be disabled")
        result.extend((option, argument))
    return result


def scrub_launch_environment(environ=None, *, cpus=1):
    """Scrub launch hints only; Slurm subsequently supplies real step variables."""
    source = os.environ if environ is None else environ
    env = {
        str(k): str(v)
        for k, v in source.items()
        if not str(k).startswith(("SLURM_", "SBATCH_", "SRUN_", "SALLOC_"))
    }
    # The same controller selector must reach launch, placement and recovery.
    if source.get("SLURM_CONF"):
        env["SLURM_CONF"] = str(source["SLURM_CONF"])
    env["LC_ALL"] = "C"
    for key in (
        "OMP_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "MKL_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
        "VECLIB_MAXIMUM_THREADS",
    ):
        env[key] = str(max(1, int(cpus)))
    return env


def config_data(config):
    data = getattr(config, "data", config)
    if not isinstance(data, dict):
        raise ValueError("Raven allocation needs a resolved configuration object")
    # Detach nested user mappings before the request is reviewed or launched.
    detached = json.loads(json.dumps(data, allow_nan=False))
    restrictions = getattr(config, "restrictions", None)
    if restrictions is not None:
        detached["_raven_site_restrictions"] = json.loads(
            json.dumps(restrictions, allow_nan=False)
        )
    return detached


def prepare(config, reuse=None, *, observed_cluster=None):
    data = config_data(config)
    if data.get("schema") == "raven.config/v1":
        from ..configuration.policy import admission_config

        data = admission_config(data)
    site, execution, connection = (
        data.get("site", {}),
        data.get("execution", {}),
        data.get("connection", {}),
    )
    allocation = data.get("allocation", {})
    heavy = data.get("resources", {}).get("heavy", {})
    kind = site.get("kind", "slurm")
    if kind not in ("desktop", "slurm"):
        raise ValueError("Choose an explicit desktop or Slurm site profile")
    if execution.get("allow_login_heavy", False):
        raise ValueError("Raven does not support login-host Heavy execution")
    cpus = _integer(
        allocation.get("cpus_per_task", 2)
        if kind == "slurm"
        else heavy.get("max_workers", 2),
        "worker CPUs",
        1,
        64,
    )
    memory = _integer(
        allocation.get("memory_mib", 4096)
        if kind == "slurm"
        else heavy.get("memory_mib", 4096),
        "worker memory MiB",
        128,
        1048576,
    )
    wall = _integer(
        allocation.get("walltime_seconds", 3600)
        if kind == "slurm"
        else heavy.get("wall_seconds", 3600),
        "worker walltime seconds",
        1,
        604800,
    )
    resources = {
        **heavy,
        "max_workers": min(
            cpus, _integer(heavy.get("max_workers", cpus), "maximum workers", 1, 64)
        ),
        "memory_mib": min(
            memory,
            _integer(heavy.get("memory_mib", memory), "memory MiB", 128, 1048576),
        ),
        "wall_seconds": min(
            wall, _integer(heavy.get("wall_seconds", wall), "wall seconds", 1, 604800)
        ),
    }
    resources["max_tasks"] = min(
        8, max(1, int(heavy.get("max_tasks", 2))), (resources["memory_mib"] - 64) // 32
    )
    resources["cpus"] = resources["max_workers"]
    resources["cpu_seconds"] = min(86400, max(1, int(heavy.get("cpu_seconds", 3600))))
    resources["wall_seconds"] = min(86400, resources["wall_seconds"])
    resources["output_bytes"] = min(
        512 * 1024, max(1024, int(heavy.get("output_bytes", 512 * 1024)))
    )
    resources["input_bytes"] = min(
        8 * 1024 * 1024, max(1024, int(heavy.get("input_bytes", 8 * 1024 * 1024)))
    )
    python = execution.get("worker_python") or sys.executable
    if (
        not isinstance(python, str)
        or not python.startswith("/")
        or "\0" in python
        or "\n" in python
        or len(python) > 4096
    ):
        raise ValueError("Worker Python must be an explicit absolute executable path")
    transport = connection.get("transport", "none" if kind == "desktop" else "local")
    cluster = _label(
        site.get("cluster_id", "")
        or observed_cluster
        or (reuse.cluster if isinstance(reuse, VerifiedReuse) else ""),
        "cluster identifier",
        optional=True,
    )
    host = "local desktop" if kind == "desktop" else "local Slurm gateway"
    prefix = []
    ownership = "local-worker"
    reuse_data = None
    ssh_options = []
    ssh_target = ""
    if kind == "desktop":
        if execution.get("desktop_execution") != "explicit" or execution.get(
            "heavy_requires_allocation", False
        ):
            raise ValueError("Desktop Heavy needs an explicit desktop execution policy")
        if transport != "none" or execution.get("heavy_target", "local") != "local":
            raise ValueError("Desktop execution cannot impersonate a Slurm connection")
        if reuse is not None:
            raise ValueError("Desktop workers do not reuse Slurm allocations")
    else:
        if execution.get("heavy_target", "slurm") != "slurm" or not execution.get(
            "heavy_requires_allocation", True
        ):
            raise ValueError("Slurm Heavy must execute in a verified allocation")
        if allocation.get("adapter", "slurm-cli") != "slurm-cli":
            raise ValueError(
                "This release supports the structured slurm-cli adapter; arbitrary wrappers are unavailable"
            )
        if (
            _integer(allocation.get("nodes", 1), "nodes", 1, 1) != 1
            or _integer(allocation.get("tasks", 1), "worker tasks", 1, 1) != 1
        ):
            raise ValueError("One Raven worker uses one node and one task")
        if transport not in ("local", "ssh"):
            raise ValueError("Slurm launch requires local or approved SSH transport")
        if transport == "ssh":
            host = _label(connection.get("host"), "SSH host alias")
            user = _label(connection.get("user", ""), "SSH user", optional=True)
            if user and not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", user):
                raise ValueError("SSH user must be one account identifier")
            if user and "@" in host:
                raise ValueError(
                    "Specify the SSH user once, in connection.user or the host alias"
                )
            ssh_target = (user + "@" if user else "") + host
            ssh_options = _ssh_options(connection.get("ssh_options", []))
        account = _label(allocation.get("account", ""), "account", optional=True)
        partition = _label(allocation.get("partition", ""), "partition", optional=True)
        qos = _label(allocation.get("qos", ""), "QOS", optional=True)
        gpus = _integer(allocation.get("gpus", 0), "GPUs", 0, 64)
        if reuse is None:
            prefix = [
                "salloc",
                "--nodes=1",
                "--ntasks=1",
                f"--cpus-per-task={cpus}",
                f"--mem={memory}M",
                f"--time={_duration(wall)}",
            ]
            for name, value in (
                ("account", account),
                ("partition", partition),
                ("qos", qos),
            ):
                if value:
                    prefix.append(f"--{name}={value}")
            if gpus:
                prefix.append(f"--gpus={gpus}")
            prefix += [
                "srun",
                "--nodes=1",
                "--ntasks=1",
                f"--cpus-per-task={cpus}",
                "--unbuffered",
                "--kill-on-bad-exit=1",
            ]
            ownership = "dedicated-allocation"
        else:
            if not isinstance(reuse, VerifiedReuse):
                raise ValueError(
                    "Reuse requires a fresh exact allocation review receipt; a JobID alone is insufficient"
                )
            if (
                not re.fullmatch(r"[1-9][0-9]{0,19}", reuse.job_id)
                or reuse.owner_uid < 0
                or (transport == "local" and reuse.owner_uid != os.getuid())
                or reuse.cluster != cluster
            ):
                raise ValueError(
                    "Reuse allocation owner, cluster, or numeric identity does not match"
                )
            if not reuse.submit_time or not reuse.start_time or not reuse.node_names:
                raise ValueError("Reuse receipt lacks exact attempt or node evidence")
            if (
                not math.isfinite(reuse.observed_at)
                or not 0 <= time.time() - reuse.observed_at <= 120
            ):
                raise ValueError(
                    "Reuse allocation review expired; verify the current attempt again"
                )
            if not reuse.exclusive_worker_resources or not reuse.exact_step_supported:
                raise ValueError(
                    "Reuse needs reserved worker resources and a qualified exact-step adapter; automatic overlap is disabled"
                )
            if (
                reuse.reserved_cpus < cpus
                or reuse.reserved_memory_mib < memory
                or reuse.remaining_seconds - max(0, time.time() - reuse.observed_at)
                < wall
            ):
                raise ValueError(
                    "Reuse allocation has insufficient reserved resources or remaining time"
                )
            reuse_data = asdict(reuse)
            prefix = [
                "srun",
                f"--jobid={reuse.job_id}",
                "--nodes=1",
                "--ntasks=1",
                f"--cpus-per-task={cpus}",
                f"--mem={memory}M",
                "--exact",
                "--immediate=10",
                "--unbuffered",
                "--kill-on-bad-exit=1",
            ]
            # Pin the node whose per-node capacity the review established.
            if len(reuse.node_names) == 1:
                prefix.append("--nodelist=" + _label(reuse.node_names[0], "reuse node"))
            ownership = "owned-step-only"
    review = {
        "schema": "raven.allocation-plan/v1",
        "site_kind": kind,
        "backend": "desktop" if kind == "desktop" else "slurm-cli",
        "receipt_directory": data.get("storage", {}).get("allocation_receipts_dir", ""),
        "target": host,
        "transport": transport,
        "cluster_id": cluster,
        "owner_uid": os.getuid(),
        "ssh_target": ssh_target,
        "ssh_options": ssh_options,
        "resources": resources,
        "requested_resources": {
            "nodes": 1,
            "tasks": 1,
            "cpus_per_task": cpus,
            "memory_mib": memory,
            "walltime_seconds": wall,
            "account": allocation.get("account", ""),
            "partition": allocation.get("partition", ""),
            "qos": allocation.get("qos", ""),
            "gpus": allocation.get("gpus", 0),
        },
        "prefix": prefix,
        "worker_python": python,
        "ownership": ownership,
        "reuse": reuse_data,
        "pending_timeout_seconds": _integer(
            allocation.get("pending_timeout_seconds", 1200),
            "pending timeout seconds",
            1,
            86400,
        ),
        "idle_release_seconds": _integer(
            allocation.get("idle_release_seconds", 600),
            "idle release seconds",
            0,
            86400,
        ),
        "qualification": {
            "local_process_tests": "implemented",
            "real_slurm": "untested",
            "site_adapter": "unqualified" if kind == "slurm" else "not-applicable",
        },
        "review": "Desktop process resource limits"
        if kind == "desktop"
        else "Uses charged compute resources in the selected Slurm profile; no automatic renewal or login-host fallback",
    }
    review["digest"] = hashlib.sha256(
        json.dumps(
            review, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()
    return review


def build_launch(plan, worker_argv, *, request_id=None):
    """Quote remote argv once; no path or identifier becomes executable syntax."""
    if (
        not isinstance(worker_argv, list)
        or not worker_argv
        or not all(isinstance(arg, str) and "\0" not in arg for arg in worker_argv)
    ):
        raise ValueError("Invalid fixed Raven worker argument vector")
    worker = [plan["worker_python"], *worker_argv[1:]]
    if plan["site_kind"] == "desktop" and worker_argv[1:] == [
        "-m",
        "raven.raven_worker",
    ]:
        worker = [
            plan["worker_python"],
            "-c",
            "import runpy,sys;sys.path.insert(0,sys.argv.pop(1));runpy.run_module('raven.raven_worker',run_name='__main__')",
            str(Path(__file__).resolve().parent.parent.parent),
        ]
    prefix = list(plan["prefix"])
    if request_id is not None and plan["ownership"] == "dedicated-allocation":
        if not isinstance(request_id, str) or not re.fullmatch(
            r"[0-9a-f]{32}", request_id
        ):
            raise ValueError("Allocation recovery requires an exact request token")
        prefix.insert(1, "--job-name=raven-heavy-" + request_id)
    elif request_id is not None and plan["ownership"] == "owned-step-only":
        if not isinstance(request_id, str) or not re.fullmatch(
            r"[0-9a-f]{32}", request_id
        ):
            raise ValueError("Step recovery requires an exact request token")
        prefix.insert(1, "--job-name=raven-step-" + request_id)
    argv = [*prefix, *worker]
    if plan["transport"] == "ssh":
        remote = [
            plan["worker_python"],
            "-m",
            "raven.raven_allocation_helper",
            "--remote-launch",
            "--cleanup-timeout="
            + str(max(35, int(plan.get("message_timeout_seconds", 10)) + 5)),
            *argv,
        ]
        return [
            "ssh",
            "-T",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=4",
            *plan.get("ssh_options", []),
            "--",
            plan.get("ssh_target") or plan["target"],
            shlex.join(remote),
        ]
    return argv
