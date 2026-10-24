"""Pure launch form conversion and background-only reviewed submission.

The UI captures a plan without mutation. Submission resolves it again so an
edited script, project preference, environment, or recipe requires new review.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil

from ..entry import workload as launch
from . import settings as launch_settings


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False).encode()
    ).hexdigest()


def arguments(draft):
    from ..configuration.profiles import workload_defaults

    project = os.path.abspath(os.path.expanduser(draft.get("project", "").strip()))
    if not draft.get("project", "").strip():
        raise ValueError("Choose the folder that contains your job script and outputs")
    script = draft.get("script", "").strip()
    if not script:
        raise ValueError("Choose an existing Bash or POSIX-shell script")
    argv = [script, "--project", project]
    if draft.get("recipe"):
        argv.extend(("--recipe", draft["recipe"]))
    execution = draft.get("execution", "slurm")
    if execution not in ("slurm", "local"):
        raise ValueError("Choose Slurm or explicit standalone execution")
    if execution == "local":
        argv.append("--local")
    else:
        captured = draft.get("captured_plan")
        if captured:
            argv.extend(
                "--sbatch=--" + key + ("=" + str(value) if value is not True else "")
                for key, value in captured.get("effective_options", {}).items()
                if value is not False
            )
        defaults = workload_defaults(
            account=draft.get("account", ""),
            partition=draft.get("partition", ""),
            qos=draft.get("qos", ""),
            cpus=draft.get("cpus") or None,
            memory=draft.get("memory") or None,
            time_limit=draft.get("time") or None,
            gpus=draft.get("gpus") or None,
        )
        argv.extend(
            "--sbatch=--" + key + "=" + str(value) for key, value in defaults.items()
        )
    for field, flag in (
        ("gpu", "gpu"),
        ("cpu_memory", "cpu-memory"),
        ("trace", "trace"),
    ):
        if draft.get(field) is not None:
            argv.append("--" + ("" if draft[field] else "no-") + flag)
    if draft.get("trace_profile"):
        argv.extend(("--trace-profile", draft["trace_profile"]))
    if draft.get("basic_research"):
        argv.extend(("--research", "experiment", "evidence"))
    if draft.get("arguments", ""):
        # This parses quoting only. No shell, expansion, globbing, substitution,
        # or interpolation is used for application arguments.
        argv.extend(("--", *shlex.split(draft["arguments"], posix=True)))
    return project, argv


def review(draft, *, environ=None, connection=None):
    env = dict(os.environ if environ is None else environ)
    project, argv = arguments(draft)
    captured = draft.get("captured_plan")
    launch_env = (
        {key: value for key, value in env.items() if not key.startswith("SBATCH_")}
        if captured
        else env
    )
    plan = launch.resolve_plan(
        argv,
        cwd=draft.get("submit_dir") or project,
        environ=launch_env,
        connection=connection,
        captured_settings=captured.get("settings", {}) if captured else None,
    )
    if captured and plan["script_sha256"] != captured.get("script_sha256"):
        raise ValueError(
            "The original script changed since this run. Start a fresh Launch job to review the changed source; Run again preserves the recorded script identity."
        )
    if captured and any(
        value is False and plan["effective_options"].get(key) not in (None, False)
        for key, value in captured.get("effective_options", {}).items()
    ):
        raise ValueError(
            "The recorded run disabled a script scheduler flag through its environment. Start a fresh Launch job to review that override explicitly."
        )
    if plan["execution"] == "local":
        for item in plan["readiness"]:
            if item["feature"] == "logs":
                item["reason"] = (
                    "Launch job captures stdout and stderr in private files and registers them with this run. The CLI command instead inherits terminal streams."
                )
    return {
        "plan": plan,
        "argv": argv,
        "project": project,
        "command": shlex.join(["raven", "launch", *argv]),
        "environment": _digest(env),
        "connection": _digest(connection or {}),
        "fingerprint": _digest(plan),
    }


def submit(
    reviewed,
    draft,
    *,
    environ=None,
    connection=None,
    runner=None,
    cancelled=lambda: False,
):
    env = dict(os.environ if environ is None else environ)
    current = review(draft, environ=env, connection=connection)
    if any(
        current[key] != reviewed[key]
        for key in ("fingerprint", "environment", "connection", "argv", "project")
    ):
        raise ValueError(
            "The script, settings, environment, or connection changed after review. Review the launch again; nothing was submitted."
        )
    if cancelled():
        raise InterruptedError("Launch closed before submission; nothing was submitted")
    from ..launching import runtime as launch_runtime

    if current["plan"]["execution"] == "local":
        return launch_runtime.start_local_plan(current["plan"], environ=env)
    if runner is None and shutil.which("sbatch", path=env.get("PATH")) is None:
        raise ValueError(
            "Slurm sbatch is unavailable here. Run Raven on the submission host, or explicitly select Standalone workstation. Nothing was submitted."
        )
    options = {"environ": env}
    if draft.get("captured_plan"):
        options["environ"] = {
            key: value for key, value in env.items() if not key.startswith("SBATCH_")
        }
    if runner is not None:
        options["runner"] = runner
    return launch_runtime.execute_plan(current["plan"], **options)


def project_options(root, *, environ=None):
    """Bounded settings and top-level script suggestions, never recursive scans."""
    from ..projects import _root

    root, fd = _root(os.path.abspath(os.path.expanduser(root)))
    os.close(fd)
    settings = launch_settings.load_settings(root)
    user = launch_settings.load_user_settings(
        os.environ if environ is None else environ
    )
    scripts = []
    with os.scandir(root) as entries:
        for count, entry in enumerate(entries):
            if count >= 512:
                break
            if entry.name.endswith((".sh", ".bash")) and entry.is_file(
                follow_symlinks=False
            ):
                scripts.append(entry.name)
    return {
        "root": root,
        "scripts": sorted(scripts)[:32],
        "settings": settings,
        "recipes": sorted(set(settings["recipes"]) | set(user["recipes"])),
    }
