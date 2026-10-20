"""First-run profile/setup services without CLI or dashboard delegation."""

from __future__ import annotations
import json
import os
from pathlib import Path
import pwd
import shutil
import sys
from ..configuration import policy as C

KINDS = (
    ("local-slurm", "Workstation or cluster login with local Slurm commands"),
    ("ssh-slurm", "HPC cluster reached through SSH"),
    ("standalone", "Standalone workstation without Slurm"),
)
FORM_FIELDS = (
    "kind",
    "name",
    "host",
    "user",
    "cluster",
    "worker_python",
    "account",
    "partition",
)
DEMO_ARGS = ["--fake", "--no-state", "--no-plugins"]


def should_offer_first_run(resolved, argv=(), *, stdin=None, stdout=None, env=None):
    """Offer setup only to fresh, ordinary interactive dashboard launches."""
    env = os.environ if env is None else env
    stdin, stdout = (
        sys.stdin if stdin is None else stdin,
        sys.stdout if stdout is None else stdout,
    )
    if not stdin.isatty() or not stdout.isatty() or env.get("RAVEN_NO_ONBOARDING"):
        return False
    if (
        resolved.source
        or resolved.legacy_source
        or env.get("RAVEN_CONFIG")
        or env.get("RAVEN_PROFILE")
    ):
        return False
    # Opt-in command paths and automation must retain their established behavior.
    # The bootstrap keeps a fresh Heavy launch in Light while setup is open;
    # saving preferences is never permission to request an allocation.
    return not argv


def installation_report(resolved):
    report = C.doctor(resolved)
    try:
        username = pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        username = "unavailable"
    report.update(
        schema="raven.getting-started/v1",
        login=username,
        python=sys.executable,
        python_version=".".join(map(str, sys.version_info[:3])),
        commands={name: shutil.which(name) for name in ("raven", "ravenl", "ravenh")},
    )
    for name, value in report["tools"].items():
        value["path"] = shutil.which(name)
    for name in ("sbatch", "scontrol", "sacctmgr"):
        path = shutil.which(name)
        report["tools"][name] = {
            "path": path,
            "status": "available" if path else "unsupported",
            "qualification": "local executable presence only",
        }
    report["worker_readiness"] = (
        "Unverified: a local Python check does not test a compute node or SSH host."
    )
    return report


def report_lines(report):
    lines = [
        "Login: " + report["login"],
        "Python: " + report["python_version"] + " at " + report["python"],
    ]
    for name, path in report["commands"].items():
        lines.append(
            name
            + ": "
            + (path or "not on PATH; use the installer-provided absolute command")
        )
    lines.extend(
        name + ": " + (item["path"] or "not found locally")
        for name, item in report["tools"].items()
    )
    lines += [
        "Embedded terminal: " + report["terminal_engine"],
        report["worker_readiness"],
        "These are local presence checks. Missing local Slurm commands are expected for SSH connections.",
    ]
    return lines


def default_form(resolved):
    data = resolved.data
    connection, allocation = data["connection"], data["allocation"]
    kind = (
        "standalone"
        if data["site"]["kind"] == "desktop"
        else "ssh-slurm"
        if connection["transport"] == "ssh"
        else "local-slurm"
    )
    label = data.get("ui", {}).get("profile_label") or (
        data["profile"] if resolved.source else "My workstation"
    )
    return {
        "kind": kind,
        "name": label,
        "host": connection["host"],
        "user": connection["user"],
        "cluster": data["site"]["cluster_id"],
        "worker_python": data["execution"]["worker_python"],
        "account": allocation["account"],
        "partition": allocation["partition"],
    }


def stage_form(service, form, *, make_default=False):
    return service.stage(
        form["name"],
        form["kind"],
        host=form["host"],
        user=form["user"],
        cluster=form["cluster"],
        worker_python=form["worker_python"],
        heavy={"account": form["account"], "partition": form["partition"]},
        make_default=make_default,
    )


def preview_lines(draft):
    data = draft["effective"]
    connection, allocation = data["connection"], data["allocation"]
    target = connection["host"] if connection["transport"] == "ssh" else "this machine"
    lines = [
        "Profile: "
        + draft.get("label", draft["profile"])
        + " ("
        + draft["profile"]
        + ")",
        "Environment: " + dict(KINDS).get(draft.get("kind", ""), data["site"]["kind"]),
        "Connection: " + target,
        "Default for future launches: " + ("yes" if draft["make_default"] else "no"),
        "Configuration: " + draft["path"],
        "Heavy worker Python: "
        + (
            data["execution"]["worker_python"]
            or "not configured; Light remains available"
        ),
    ]
    if data["site"]["kind"] == "slurm":
        lines += [
            "Raven Heavy billing: "
            + (allocation["account"] or "Slurm's configured default"),
            "Heavy partition: "
            + (allocation["partition"] or "Slurm's configured default"),
            "Scientific jobs keep their own script or launch-recipe account and resources.",
        ]
    else:
        lines.append(
            "Standalone execution: no Slurm account; local resource limits still apply."
        )
    lines += [
        "Saving does not submit a job, allocate compute, or switch a running dashboard's connection.",
        "Compute-node Python, account permissions, and scheduler availability remain unverified.",
    ]
    lines.extend("Notice: " + str(item) for item in draft.get("warnings", ()))
    return lines


def _draft_path(resolved):
    return Path(resolved.data["storage"]["state_dir"]) / "onboarding-draft.json"


def save_progress(resolved, form, make_default):
    from ..configuration.allocation import _atomic_write, _read_snapshot

    path = _draft_path(resolved)
    _, token = _read_snapshot(path)
    payload = {
        "schema": "raven.onboarding-draft/v1",
        "source": resolved.source,
        "form": {key: str(form[key]) for key in FORM_FIELDS},
        "make_default": bool(make_default),
    }
    _atomic_write(path, payload, token)
    return str(path)


def load_progress(resolved):
    from ..configuration.allocation import _read_snapshot

    raw, _ = _read_snapshot(_draft_path(resolved))
    if raw is None:
        return None
    value = json.loads(raw)
    if (
        not isinstance(value, dict)
        or value.get("schema") != "raven.onboarding-draft/v1"
        or value.get("source") != resolved.source
        or not isinstance(value.get("form"), dict)
        or set(value["form"]) != set(FORM_FIELDS)
        or type(value.get("make_default")) is not bool
        or any(
            not isinstance(item, str)
            or len(item) > 4096
            or any(ord(c) < 32 for c in item)
            for item in value["form"].values()
        )
        or value["form"]["kind"] not in dict(KINDS)
    ):
        raise ValueError(
            "Saved setup draft is invalid or belongs to another configuration. Start a new setup draft."
        )
    return value


def _interactive(resolved, form, make_default, ask):
    print(
        "Getting started with Raven\n\n"
        + "\n".join(report_lines(installation_report(resolved)))
    )
    print(
        "\nNo jobs or allocations are requested during setup.\nType :quit to stop or :draft to save progress and stop."
    )
    print(
        "\n"
        + "\n".join(f"{index}. {label}" for index, (_, label) in enumerate(KINDS, 1))
    )
    previous_kind = form["kind"]
    choice = ask("Environment [1 local Slurm / 2 SSH cluster / 3 standalone]: ").strip()
    if choice in (":quit", ":draft"):
        if choice == ":draft":
            print("Draft saved: " + save_progress(resolved, form, make_default))
        return None
    if choice:
        if choice not in ("1", "2", "3"):
            raise ValueError("Choose environment 1, 2, or 3.")
        form["kind"] = KINDS[int(choice) - 1][0]
    if form["kind"] != previous_kind:
        form.update(host="", user="", account="", partition="")
        form["worker_python"] = "" if form["kind"] == "ssh-slurm" else sys.executable
    fields = [("name", "Profile name")]
    if form["kind"] == "ssh-slurm":
        fields += [
            ("host", "SSH host alias"),
            ("user", "SSH login (empty uses SSH configuration)"),
        ]
    fields.append(
        (
            "worker_python",
            "Worker Python (compute-host path; empty permits Light setup)",
        )
    )
    if form["kind"] != "standalone":
        fields += [
            ("account", "Raven Heavy billing account (empty uses Slurm default)"),
            ("partition", "Heavy partition (empty uses Slurm default)"),
        ]
    print("Press Enter to keep the shown value; type - to clear an optional value.")
    for key, label in fields:
        value = ask(label + " [" + form[key] + "]: ").strip()
        if value in (":quit", ":draft"):
            if value == ":draft":
                print("Draft saved: " + save_progress(resolved, form, make_default))
            return None
        if value:
            form[key] = "" if value == "-" else value
    if resolved.source:
        make_default = ask(
            "Use this as the default profile for future launches? [y/N] "
        ).strip().lower() in ("y", "yes")
    return form, make_default
