"""Versioned Raven bootstrap policy. Standard library only; no collectors start here.

Permission is intersected with mandatory site policy after preferences resolve.
Tool availability and Slurm environment variables never grant Heavy execution.
"""

from __future__ import annotations

from dataclasses import dataclass
import copy
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import shutil

from ..telemetry_profiles import (
    DEFAULTS as TELEMETRY_DEFAULTS,
    PRESETS,
    validate_custom,
)

SCHEMA = "raven.config/v1"
POLICY_SCHEMA = "raven.site-policy/v1"
MAX_CONFIG = 262144
SYSTEM_POLICIES = ("/etc/raven/policy.json",)
CAPABILITIES = (
    "view",
    "scheduler-read",
    "scheduler-action",
    "local-files",
    "terminal",
    "heavy-analysis",
    "instrumentation",
)

DEFAULTS = {
    "schema": SCHEMA,
    "profile": "default-slurm",
    "site": {"kind": "slurm", "cluster_id": "", "policy_file": ""},
    "connection": {
        "transport": "local",
        "host": "",
        "user": "",
        "credential_reference": "",
        "ssh_options": [],
    },
    "execution": {
        "startup": "light",
        "heavy_target": "slurm",
        "heavy_requires_allocation": True,
        "desktop_execution": "disabled",
        "allow_login_heavy": False,
        "on_allocation_end": "light",
        "worker_python": "",
        "capabilities": list(CAPABILITIES),
    },
    "allocation": {
        "adapter": "slurm-cli",
        "account": "",
        "partition": "",
        "qos": "",
        "nodes": 1,
        "tasks": 1,
        "cpus_per_task": 2,
        "memory_mib": 4096,
        "gpus": 0,
        "walltime_seconds": 3600,
        "pending_timeout_seconds": 1200,
        "idle_release_seconds": 600,
        "reuse": "explicit",
        "auto_request_on_light_start": False,
        "auto_renew": False,
        "disconnect_policy": "stop-owned-worker",
    },
    "collection": {
        "owner": "user-host",
        "slurm_enabled": True,
        "remote_connections": "explicit",
        "queue_seconds": 60,
        "starts_seconds": 120,
        "history_seconds": 300,
        "inventory_seconds": 300,
        "details_seconds": 60,
        "live_statistics": "selected-on-demand",
        "live_seconds": 60,
        "gpu": "existing-telemetry",
        "weather_probes": False,
        "account_seconds": 900,
        "log_follow_seconds": 5,
        "research_seconds": 5,
        "trace_seconds": 1,
        "idle_after_seconds": 300,
        "idle_interval_factor": 5,
        "jitter_fraction": 0.20,
        "max_inflight": 2,
        "controller_requests_per_minute": 12,
        "controller_burst": 2,
        "accounting_requests_per_minute": 2,
        "step_launches_per_minute": 6,
        "step_launch_interval_seconds": 10,
        "statistics_requests_per_minute": 4,
        "action_validation_requests_per_minute": 6,
        "max_response_bytes": 4194304,
        "max_response_records": 10000,
        "live_batch_jobs": 16,
        "circuit_failures": 3,
        "circuit_seconds": 120,
        "filesystem_metadata_per_minute": 120,
        "file_bytes_per_minute": 16777216,
        "log_file_bytes_per_minute": 16777216,
        "local_processes_per_minute": 60,
        "service_failure": "cached-only",
    },
    "resources": {
        "light": {
            "log_window_bytes": 2097152,
            "prepared_cache_bytes": 33554432,
            "pending_result_bytes": 8388608,
            "max_terminal_sessions": 2,
            "terminal_scrollback_bytes": 2097152,
            "memory_mib": 256,
            "max_workers": 2,
            "max_jobs": 10000,
            "max_history_jobs": 10000,
            "max_job_rows": 5000,
            "max_history_rows": 5000,
            "max_series_jobs": 128,
            "series_bytes": 16777216,
            "max_log_buffers": 4,
            "persistence_queue_bytes": 8388608,
            "storage_bytes": 268435456,
            "storage_files": 512,
            "max_worker_queue": 64,
            "max_source_commands": 2,
        },
        "heavy": {
            "max_workers": 2,
            "memory_mib": 4096,
            "scratch_mib": 8192,
            "max_tasks": 2,
            "enforcement": "best-available",
            "cpu_seconds": 3600,
            "wall_seconds": 3600,
            "input_bytes": 8388608,
            "output_bytes": 8388608,
        },
    },
    "storage": {
        "state_dir": "",
        "allocation_receipts_dir": "",
        "coordination_dir": "auto-host-local",
        "durable_results": "",
        "series_format": "jsonl",
        "retention": {
            "raw_seconds": 86400,
            "ten_second_seconds": 604800,
            "minute_seconds": 2592000,
            "gap_seconds": 2.0,
            "protect_raw": True,
        },
    },
    "telemetry": copy.deepcopy(TELEMETRY_DEFAULTS),
    "ui": {
        "theme": "terminal",
        "show_execution_target": True,
        "show_observation_age": True,
        "ascii": False,
        "animations": True,
        "density": "comfortable",
        "worker_mode": "multi",
        "profile_label": "",
    },
    "profiles": {},
}
DESKTOP = {
    "profile": "desktop",
    "site": {"kind": "desktop"},
    "connection": {
        "transport": "none",
        "host": "",
        "user": "",
        "credential_reference": "",
        "ssh_options": [],
    },
    "execution": {
        "heavy_target": "local",
        "heavy_requires_allocation": False,
        "desktop_execution": "explicit",
    },
    "allocation": {"adapter": "desktop"},
    "collection": {"slurm_enabled": False},
}

_ENUMS = {
    "telemetry.preset": set(PRESETS),
    "storage.series_format": {"jsonl", "blocks-v1"},
    "site.kind": {"slurm", "desktop"},
    "connection.transport": {"none", "local", "ssh"},
    "execution.startup": {"light", "heavy"},
    "execution.heavy_target": {"slurm", "local"},
    "execution.desktop_execution": {"disabled", "explicit"},
    "execution.on_allocation_end": {"light"},
    "allocation.adapter": {"slurm-cli", "desktop"},
    "allocation.reuse": {"explicit"},
    "allocation.disconnect_policy": {"stop-owned-worker"},
    "collection.owner": {"user-host"},
    "collection.remote_connections": {"explicit"},
    "collection.live_statistics": {"off", "selected-on-demand"},
    "collection.gpu": {"off", "existing-telemetry"},
    "collection.service_failure": {"cached-only"},
    "resources.heavy.enforcement": {"best-available", "hard-required"},
    "ui.density": {"comfortable", "compact", "focused"},
    "ui.worker_mode": {"single", "multi"},
}
_FALSE_ONLY = {
    "execution.allow_login_heavy",
    "allocation.auto_request_on_light_start",
    "allocation.auto_renew",
    "collection.weather_probes",
}
_STRINGS = {
    "profile": 128,
    "site.cluster_id": 128,
    "site.policy_file": 4096,
    "connection.host": 256,
    "connection.user": 128,
    "connection.credential_reference": 512,
    "execution.worker_python": 4096,
    "allocation.account": 128,
    "allocation.partition": 128,
    "allocation.qos": 128,
    "storage.state_dir": 4096,
    "storage.allocation_receipts_dir": 4096,
    "storage.coordination_dir": 4096,
    "storage.durable_results": 4096,
    "ui.theme": 64,
    "ui.profile_label": 128,
}


def canonical(value):
    try:
        data = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode()
    except (TypeError, ValueError, RecursionError, OverflowError) as exc:
        raise ValueError(
            "Raven configuration must contain bounded finite JSON values."
        ) from exc
    if len(data) > MAX_CONFIG:
        raise ValueError("Raven configuration exceeds 256 KiB.")
    return data


def merge(base, overlay):
    value = copy.deepcopy(base)
    for key, item in overlay.items():
        value[key] = (
            merge(value[key], item)
            if isinstance(item, dict) and isinstance(value.get(key), dict)
            else copy.deepcopy(item)
        )
    return value


def flatten(value, prefix=""):
    result = {}
    for key, item in value.items():
        path = prefix + "." + key if prefix else key
        if isinstance(item, dict) and path != "profiles":
            result.update(flatten(item, path))
        else:
            result[path] = copy.deepcopy(item)
    return result


def _get(value, path):
    for part in path.split("."):
        value = value[part]
    return value


def _set(value, path, item):
    parts = path.split(".")
    for part in parts[:-1]:
        value = value[part]
    value[parts[-1]] = item


def _numeric(path, value, expected):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or abs(value) > 1 << 50
        or not math.isfinite(value)
    ):
        raise ValueError(
            path + " must be a finite numeric value with the documented unit."
        )
    if isinstance(expected, int) and not isinstance(value, int):
        # Intervals accept fractional seconds; byte/count limits must be integral.
        if not path.endswith("_seconds"):
            raise ValueError(path + " requires an integer.")
    low, high = (0, 1 << 40) if path == "allocation.gpus" else (0.001, 1 << 40)
    if (
        path.endswith("_seconds")
        or path.endswith("wall_seconds")
        or path.endswith("cpu_seconds")
    ):
        low, high = (0.1, 31536000)
    if path.endswith("_per_minute"):
        low, high = (0, (1 << 40) if path.endswith("file_bytes_per_minute") else 100000)
    if path.endswith("jitter_fraction"):
        low, high = (0, 0.5)
    if path.endswith("idle_interval_factor"):
        low, high = (1, 100)
    if path == "allocation.idle_release_seconds":
        low = 0  # Zero is the documented explicit idle-release disable option.
    if path.endswith(("max_workers", "max_tasks", "max_inflight")):
        low, high = (1, 256)
    if path.endswith("max_terminal_sessions"):
        low, high = (1, 8)
    if not low <= value <= high:
        raise ValueError(f"{path} is outside [{low}, {high}].")


def validate(document, *, partial=False):
    if not isinstance(document, dict):
        raise ValueError("Raven configuration must be an object.")
    canonical(document)

    def walk(value, model, prefix=""):
        for key, item in value.items():
            path = prefix + "." + key if prefix else key
            if key not in model:
                raise ValueError("Unknown Raven configuration key: " + path)
            expected = model[key]
            if path == "profiles":
                if not isinstance(item, dict) or len(item) > 32:
                    raise ValueError(
                        "Profiles must be a dictionary of at most 32 named profiles."
                    )
                for name, profile in item.items():
                    if (
                        not isinstance(name, str)
                        or not name
                        or len(name) > 128
                        or not all(c.isalnum() or c in "_.-" for c in name)
                    ):
                        raise ValueError("Invalid Raven profile name.")
                    if not isinstance(profile, dict) or "profiles" in profile:
                        raise ValueError("Profiles cannot nest other profiles.")
                    walk(profile, DEFAULTS)
            elif isinstance(expected, dict):
                if not isinstance(item, dict):
                    raise ValueError(path + " must be an object.")
                walk(item, expected, path)
            elif path in _ENUMS:
                if path == "collection.owner" and item in ("process", "site-service"):
                    raise ValueError(
                        "collection.owner="
                        + item
                        + " has no qualified adapter; use user-host shared collection."
                    )
                if not isinstance(item, str) or item not in _ENUMS[path]:
                    raise ValueError(
                        path + " must be one of " + ", ".join(sorted(_ENUMS[path]))
                    )
            elif isinstance(expected, bool):
                if not isinstance(item, bool):
                    raise ValueError(path + " must be boolean.")
                if path in _FALSE_ONLY and item:
                    raise ValueError(path + " is unavailable in the managed profile.")
            elif isinstance(expected, (int, float)):
                _numeric(path, item, expected)
            elif isinstance(expected, list):
                if (
                    not isinstance(item, list)
                    or len(item) > 64
                    or not all(
                        isinstance(x, str)
                        and len(x) <= 512
                        and not any(c in x for c in "\0\n\r")
                        for x in item
                    )
                ):
                    raise ValueError(path + " must be a bounded string list.")
                if path == "execution.capabilities" and set(item) - set(CAPABILITIES):
                    raise ValueError(
                        "Unknown Raven capability in execution.capabilities."
                    )
            else:
                if (
                    not isinstance(item, str)
                    or len(item) > _STRINGS.get(path, 128)
                    or any(c in item for c in "\0\n\r")
                ):
                    raise ValueError(path + " must be a bounded single-line string.")
                if path == "schema" and item != SCHEMA:
                    raise ValueError("Expected " + SCHEMA)

    walk(document, DEFAULTS)

    def telemetry_checks(value):
        if "retention" in value.get("storage", {}):
            from ..recordings.retention import policy

            retention = value["storage"]["retention"]
            if all(
                key in retention
                for key in ("raw_seconds", "ten_second_seconds", "minute_seconds")
            ):
                policy(retention)
        if "custom" in value.get("telemetry", {}):
            validate_custom(value["telemetry"]["custom"], partial=True)
        for profile in value.get("profiles", {}).values():
            telemetry_checks(profile)

    telemetry_checks(document)
    if not partial:
        if document.get("schema") != SCHEMA:
            raise ValueError("Expected " + SCHEMA)
        # Full validation also verifies callers did not omit required defaults.
        if set(flatten(DEFAULTS)) - set(flatten(document)):
            raise ValueError("Resolved Raven configuration is incomplete.")
        site, connection, execution = (
            document["site"],
            document["connection"],
            document["execution"],
        )
        if site["kind"] == "desktop":
            if (
                connection["transport"] != "none"
                or document["collection"]["slurm_enabled"]
            ):
                raise ValueError(
                    "Desktop local execution uses transport=none and slurm_enabled=false; configure a separate Slurm profile for a cluster."
                )
            if (
                execution["heavy_target"] != "local"
                or execution["desktop_execution"] != "explicit"
                or execution["heavy_requires_allocation"]
            ):
                raise ValueError(
                    "Desktop execution requires an explicit desktop policy."
                )
            if document["allocation"]["adapter"] != "desktop":
                raise ValueError("The desktop profile needs the desktop adapter.")
        else:
            if (
                connection["transport"] == "none"
                or execution["heavy_target"] != "slurm"
                or not execution["heavy_requires_allocation"]
            ):
                raise ValueError(
                    "A cluster profile requires allocated Slurm Heavy execution."
                )
            if document["allocation"]["adapter"] != "slurm-cli":
                raise ValueError("No qualified cluster launch adapter was selected.")
            if execution["desktop_execution"] != "disabled":
                raise ValueError(
                    "Cluster profiles cannot permit local desktop Heavy execution."
                )
        if connection["transport"] == "ssh" and not connection["host"]:
            raise ValueError("SSH transport requires an explicit host alias.")
        if connection["transport"] != "ssh" and connection["host"]:
            raise ValueError("A configured host requires SSH transport.")
        if execution["worker_python"] and not os.path.isabs(execution["worker_python"]):
            raise ValueError(
                "execution.worker_python must be an absolute path on its execution host."
            )
    return copy.deepcopy(document)


def read_file(path, *, trusted=False):
    from ..config_trust import read

    raw, _info = read(path, limit=MAX_CONFIG, site=trusted)
    if str(path).lower().endswith(".toml"):
        try:
            import tomllib
        except ImportError as exc:
            raise ValueError(
                "TOML requires Python 3.11 or later; use the equivalent JSON configuration."
            ) from exc
        value = tomllib.loads(raw.decode("utf-8"))
    else:

        def pairs(items):
            result = {}
            for key, item in items:
                if key in result:
                    raise ValueError("Duplicate configuration key: " + key)
                result[key] = item
            return result

        value = json.loads(raw, object_pairs_hook=pairs)
    canonical(value)
    if not isinstance(value, dict):
        raise ValueError("Configuration must be an object.")
    return value


def _policy(document):
    allowed = {"schema", "name", "recommendations", "restrictions"}
    if (
        not isinstance(document, dict)
        or set(document) - allowed
        or document.get("schema") != POLICY_SCHEMA
    ):
        raise ValueError("Expected strict " + POLICY_SCHEMA)
    recommendations = document.get("recommendations", {})
    validate(recommendations, partial=True)
    restrictions = document.get("restrictions", {})
    keys = {
        "allowed_site_kinds",
        "allowed_targets",
        "allowed_transports",
        "allowed_capabilities",
        "allowed_accounts",
        "allowed_partitions",
        "allow_desktop",
        "deny_heavy",
        "require_allocation",
        "require_account",
        "floors",
        "ceilings",
        "hard_enforcement",
    }
    if not isinstance(restrictions, dict) or set(restrictions) - keys:
        raise ValueError("Unknown site restriction.")
    for name, item in restrictions.items():
        if name in ("floors", "ceilings"):
            if not isinstance(item, dict):
                raise ValueError(
                    name + " must map numeric configuration paths to limits."
                )
            for path, limit in item.items():
                if (
                    path not in flatten(DEFAULTS)
                    or not isinstance(_get(DEFAULTS, path), (int, float))
                    or isinstance(_get(DEFAULTS, path), bool)
                ):
                    raise ValueError("Restriction is not a numeric budget: " + path)
                _numeric(path, limit, _get(DEFAULTS, path))
                if name == "floors" and not (
                    path.startswith("collection.") and path.endswith("_seconds")
                ):
                    raise ValueError("Floors apply only to collection intervals.")
                if (
                    name == "ceilings"
                    and path.startswith("collection.")
                    and path.endswith("_seconds")
                ):
                    raise ValueError("Collection intervals use floors, not ceilings.")
        elif name.startswith("allowed_"):
            if (
                not isinstance(item, list)
                or len(item) > 128
                or not all(isinstance(x, str) and 0 < len(x) <= 256 for x in item)
            ):
                raise ValueError(name + " must be a bounded string list.")
        elif not isinstance(item, bool):
            raise ValueError(name + " must be boolean.")
    return copy.deepcopy(document)


def enforce_site_restrictions(data, policies):
    result = copy.deepcopy(data)
    applied = []
    allowed_capabilities = set(result["execution"]["capabilities"])
    for label, document in policies:
        restrictions = _policy(document).get("restrictions", {})
        for key, path in (
            ("allowed_site_kinds", "site.kind"),
            ("allowed_targets", "execution.heavy_target"),
            ("allowed_transports", "connection.transport"),
            ("allowed_accounts", "allocation.account"),
            ("allowed_partitions", "allocation.partition"),
        ):
            if (
                key in ("allowed_accounts", "allowed_partitions")
                and not _get(result, path)
                and result["execution"]["startup"] != "heavy"
            ):
                continue
            if key in restrictions and _get(result, path) not in restrictions[key]:
                raise ValueError(
                    f"Site policy {label} does not permit {path}={_get(result, path)!r}."
                )
        if (
            not restrictions.get("allow_desktop", True)
            and result["site"]["kind"] == "desktop"
        ):
            raise ValueError(
                "Mandatory site restrictions forbid desktop execution in this installation."
            )
        if (
            restrictions.get("require_allocation")
            and not result["execution"]["heavy_requires_allocation"]
        ):
            raise ValueError(
                "Site policy requires an allocation; desktop Heavy is unavailable."
            )
        if (
            restrictions.get("require_account")
            and result["execution"]["startup"] == "heavy"
            and not result["allocation"]["account"]
        ):
            raise ValueError("Site policy requires an account before requesting Heavy.")
        if restrictions.get("deny_heavy"):
            allowed_capabilities.discard("heavy-analysis")
            allowed_capabilities.discard("instrumentation")
        if "allowed_capabilities" in restrictions:
            allowed_capabilities.intersection_update(
                restrictions["allowed_capabilities"]
            )
        for kind, reducer in (("floors", max), ("ceilings", min)):
            for path, limit in restrictions.get(kind, {}).items():
                before = _get(result, path)
                after = reducer(before, limit)
                _set(result, path, after)
                applied.append(
                    {
                        "policy": label,
                        "path": path,
                        "requested": before,
                        "effective": after,
                        "rule": kind,
                    }
                )
        if restrictions.get("hard_enforcement"):
            result["resources"]["heavy"]["enforcement"] = "hard-required"
        applied.append({"policy": label, "rules": copy.deepcopy(restrictions)})
    result["execution"]["capabilities"] = sorted(allowed_capabilities)
    if not allowed_capabilities:
        raise ValueError(
            "Site and user capability permissions have an empty intersection."
        )
    if (
        result["execution"]["startup"] == "heavy"
        and "heavy-analysis" not in allowed_capabilities
    ):
        raise ValueError(
            "Site policy disables new Heavy admission; Light remains available."
        )
    validate(result)
    return result, applied


def _roots(env):
    home = Path(env.get("HOME") or os.path.expanduser("~"))
    return (
        Path(env.get("XDG_CONFIG_HOME") or home / ".config"),
        Path(env.get("XDG_STATE_HOME") or home / ".local/state"),
        home,
    )


def legacy_settings(document):
    """Semantic read-only conversion. Fast preferences never lower Raven floors."""
    if not isinstance(document, dict):
        raise ValueError("Legacy configuration must be an object.")
    output = {}
    presentation = {
        key: document[key]
        for key in ("theme", "ascii", "animations", "worker_mode")
        if key in document
    }
    if presentation:
        output["ui"] = presentation
    if "host" in document:
        host = document["host"]
        output["connection"] = {
            "transport": "ssh" if host else "local",
            "host": host,
            "user": document.get("ssh_user", ""),
            "ssh_options": document.get("ssh_opts", []),
        }
    if "account" in document:
        output["allocation"] = {"account": document["account"]}
    intervals = document.get("intervals", {})
    names = {
        "jobs": "queue_seconds",
        "finished": "history_seconds",
        "nodes": "inventory_seconds",
        "details": "details_seconds",
        "live": "live_seconds",
        "starts": "starts_seconds",
        "account": "account_seconds",
    }
    if isinstance(intervals, dict):
        mapped = {}
        for old, new in names.items():
            value = intervals.get(old)
            if (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and 0 < value < 31536000
            ):
                mapped[new] = max(DEFAULTS["collection"][new], value)
        if mapped:
            output["collection"] = mapped
    profiles = document.get("profiles", {})
    if isinstance(profiles, dict):
        output["profiles"] = {
            name: legacy_settings(
                {key: value for key, value in profile.items() if key != "profiles"}
            )
            for name, profile in profiles.items()
            if isinstance(profile, dict)
        }
        for profile in output["profiles"].values():
            profile.pop("profiles", None)
    validate(output, partial=True)
    return output


@dataclass(frozen=True)
class Resolved:
    data: dict
    origins: dict
    restrictions: tuple
    warnings: tuple
    source: str = ""
    legacy_source: str = ""
    legacy_profile: str = ""
    source_telemetry: dict | None = None
    extra_policy_path: str = ""

    @property
    def digest(self):
        return hashlib.sha256(canonical(self.data)).hexdigest()

    def as_dict(self):
        return copy.deepcopy(self.data)

    def explain(self, *, show_paths=False):
        data = self.as_dict()
        origins = copy.deepcopy(self.origins)
        if not show_paths:
            for path in (
                "site.policy_file",
                "execution.worker_python",
                "storage.state_dir",
                "storage.allocation_receipts_dir",
                "storage.durable_results",
            ):
                if _get(data, path):
                    _set(data, path, "<configured path>")
            coordination = data["storage"]["coordination_dir"]
            if coordination != "auto-host-local":
                data["storage"]["coordination_dir"] = "<configured host-local path>"
            if data["connection"]["credential_reference"]:
                data["connection"]["credential_reference"] = "<credential reference>"
            origins = {
                key: value.split(":", 1)[0]
                if value.startswith(("file:", "legacy:"))
                else value
                for key, value in origins.items()
            }
        return {
            "schema": "raven.config-explanation/v1",
            "effective": data,
            "origins": origins,
            "restrictions": copy.deepcopy(self.restrictions),
            "warnings": list(self.warnings),
            "digest": self.digest,
            "legacy_view": bool(self.legacy_source),
            "credentials_exported": False,
        }


def resolve(
    config_path=None,
    *,
    site=None,
    legacy_profile=None,
    launcher="light",
    cli_overrides=None,
    site_policy_path=None,
    env=None,
    system_policy_paths=None,
):
    env = dict(os.environ if env is None else env)
    if launcher not in ("light", "heavy"):
        raise ValueError("Launcher intent must be light or heavy.")
    config_root, state_root, home = _roots(env)
    data = copy.deepcopy(DEFAULTS)
    data["storage"].update(
        state_dir=str(state_root / "raven"),
        allocation_receipts_dir=str(state_root / "raven" / "allocations"),
        durable_results=str(home / "raven-results"),
    )
    from ..install_paths import worker_python as stable_python

    data["execution"]["worker_python"] = stable_python()
    origins = {key: "default" for key in flatten(data)}
    warnings, policies = [], []

    def apply(document, origin):
        nonlocal data
        validate(document, partial=True)
        data = merge(data, document)
        for key in flatten(document):
            origins[key] = origin

    for path in SYSTEM_POLICIES if system_policy_paths is None else system_policy_paths:
        if os.path.lexists(path):
            document = _policy(read_file(path, trusted=system_policy_paths is None))
            policies.append(("mandatory:" + str(path), document))
            apply(document.get("recommendations", {}), "site recommendation")
    explicit = config_path or env.get("RAVEN_CONFIG")
    candidates = (
        [Path(explicit).expanduser()]
        if explicit
        else [config_root / "raven/config.json", config_root / "raven/config.toml"]
    )
    source = next((str(path) for path in candidates if os.path.lexists(path)), "")
    if explicit and not source:
        raise ValueError(
            "The explicitly requested Raven/legacy configuration does not exist."
        )
    legacy_source = ""
    supplied = {}
    if source:
        supplied = read_file(source)
        if supplied.get("schema") == SCHEMA:
            validate(supplied, partial=True)
        elif "schema" in supplied:
            raise ValueError("Unsupported configuration schema; expected " + SCHEMA)
        else:
            legacy_source = source
            supplied = legacy_settings(supplied)
    if legacy_profile:
        warnings.append(
            "--profile is a legacy alias for selecting the old named profile; prefer --site for Raven profiles."
        )
    profile_name = (
        site
        or legacy_profile
        or env.get("RAVEN_PROFILE")
        or supplied.get("profile")
        or data["profile"]
    )
    # An unversioned 'desktop' profile can mean a workstation running Slurm. Only
    # Raven's explicit desktop intent may authorize non-Slurm local execution.
    legacy_selected = bool(legacy_source and not site and not env.get("RAVEN_PROFILE"))
    if profile_name == "desktop" and not legacy_selected:
        apply(DESKTOP, "explicit desktop profile")
    elif profile_name not in (
        "default-slurm",
        "slurm",
    ) and profile_name not in supplied.get("profiles", {}):
        # A standalone named profile document identifies itself by profile.
        if supplied.get("profile") != profile_name:
            raise ValueError("Unknown Raven site/profile: " + str(profile_name))
    apply(
        supplied,
        "legacy:" + legacy_source
        if legacy_source
        else "file:" + source
        if source
        else "default",
    )
    if site == "desktop" and supplied.get("profile") != "desktop":
        apply(DESKTOP, "CLI explicit desktop profile")
        data["execution"]["worker_python"] = stable_python()
    if profile_name in supplied.get("profiles", {}):
        selected = supplied["profiles"][profile_name]
        if selected.get("site", {}).get("kind") == "desktop":
            apply(DESKTOP, "explicit desktop profile")
        apply(selected, "profile:" + profile_name)
    data["profile"] = profile_name
    origins["profile"] = (
        "CLI --site"
        if site
        else "legacy --profile"
        if legacy_profile
        else origins.get("profile", "default")
    )
    extra_paths = [
        data["site"]["policy_file"],
        site_policy_path or env.get("RAVEN_SITE_POLICY"),
    ]
    loaded = (
        {str(path) for path in SYSTEM_POLICIES if system_policy_paths is None}
        if system_policy_paths is None
        else set(map(str, system_policy_paths))
    )
    for path in extra_paths:
        if path and str(path) not in loaded:
            document = _policy(read_file(os.path.expanduser(path)))
            policies.append(("selected:" + str(path), document))
            # Recommendations precede user preferences; restrictions remain last.
            user_values = data
            for key, value in flatten(document.get("recommendations", {})).items():
                if origins.get(key) == "default":
                    _set(user_values, key, value)
                    origins[key] = "site recommendation"
            loaded.add(str(path))
    if env.get("RAVEN_STATE_DIR"):
        apply(
            {"storage": {"state_dir": env["RAVEN_STATE_DIR"]}}, "environment state path"
        )
    if env.get("RAVEN_WORKER_PYTHON"):
        apply(
            {"execution": {"worker_python": env["RAVEN_WORKER_PYTHON"]}},
            "environment worker path",
        )
    if cli_overrides:
        apply(cli_overrides, "CLI")
    data["execution"]["startup"] = launcher
    origins["execution.startup"] = (
        "explicit " + ("ravenh" if launcher == "heavy" else "ravenl") + " intent"
    )
    for key in ("state_dir", "allocation_receipts_dir", "durable_results"):
        data["storage"][key] = os.path.abspath(os.path.expanduser(data["storage"][key]))
    if (
        not data["execution"]["worker_python"]
        and data["connection"]["transport"] != "ssh"
    ):
        data["execution"]["worker_python"] = stable_python()
    if (
        data["connection"]["transport"] == "ssh"
        and origins.get("execution.worker_python") == "default"
    ):
        data["execution"]["worker_python"] = ""
        warnings.append(
            "SSH Heavy requires execution.worker_python to name the installed interpreter on the remote host."
        )
    if legacy_source:
        warnings.append(
            "Read-only legacy configuration view; originals are unchanged. Use ravenl migrate for an explicit migration."
        )
    validate(data)
    data, restrictions = enforce_site_restrictions(data, policies)
    for rule in restrictions:
        if "path" in rule:
            origins[rule["path"]] = "site restriction"
    source_telemetry = merge(
        TELEMETRY_DEFAULTS,
        merge(
            supplied.get("telemetry", {}),
            supplied.get("profiles", {}).get(profile_name, {}).get("telemetry", {}),
        ),
    )
    return Resolved(
        data,
        origins,
        tuple(restrictions),
        tuple(warnings),
        source,
        legacy_source,
        profile_name
        if legacy_source and profile_name in supplied.get("profiles", {})
        else "",
        source_telemetry,
        site_policy_path or env.get("RAVEN_SITE_POLICY", ""),
    )


def admission_config(config, *, system_policy_paths=None):
    """Recheck captured and current policy before any new Heavy admission.

    The internal restriction list survives the manager's detached copy but is
    never accepted as a user-config key. Captured rules can only tighten the
    independently reloaded mandatory policy, including after config reload.
    """
    data = copy.deepcopy(getattr(config, "data", config))
    data.pop("managed", None)
    captured = data.pop("_raven_site_restrictions", getattr(config, "restrictions", ()))
    policies = []
    for index, item in enumerate(captured):
        if isinstance(item, dict) and "rules" in item:
            policies.append(
                (
                    "captured:" + str(index),
                    {"schema": POLICY_SCHEMA, "restrictions": item["rules"]},
                )
            )
    paths = SYSTEM_POLICIES if system_policy_paths is None else system_policy_paths
    for path in paths:
        if os.path.lexists(path):
            policies.append(
                (
                    "mandatory:" + str(path),
                    _policy(read_file(path, trusted=system_policy_paths is None)),
                )
            )
    path = data.get("site", {}).get("policy_file")
    if path:
        policies.append(
            ("selected:" + str(path), _policy(read_file(os.path.expanduser(path))))
        )
    data["execution"]["startup"] = "heavy"
    validate(data)
    effective, _rules = enforce_site_restrictions(data, policies)
    return effective


def to_raven_config(resolved):
    value = resolved.data if isinstance(resolved, Resolved) else resolved
    validate(value)
    collection, ui, light = (
        value["collection"],
        value["ui"],
        value["resources"]["light"],
    )
    intervals = {
        "jobs": collection["queue_seconds"],
        "starts": collection["starts_seconds"],
        "finished": collection["history_seconds"],
        "nodes": collection["inventory_seconds"],
        "partitions": collection["inventory_seconds"],
        "details": collection["details_seconds"],
        "live": collection["live_seconds"],
        "gpu": collection["live_seconds"],
        "trace": collection["live_seconds"],
        "account": collection["account_seconds"],
        "budget": collection["account_seconds"],
        "share": collection["account_seconds"],
        "weather": collection["inventory_seconds"],
        "fin_details": collection["details_seconds"],
    }
    return {
        "raven": {
            **copy.deepcopy(value),
            "managed": True,
            "_raven_site_restrictions": copy.deepcopy(
                getattr(resolved, "restrictions", ())
            ),
        },
        "state_namespace": "raven",
        "host": value["connection"]["host"],
        "ssh_user": value["connection"]["user"],
        "ssh_opts": value["connection"]["ssh_options"],
        "account": value["allocation"]["account"],
        "theme": ui["theme"],
        "ascii": ui["ascii"],
        "animations": ui["animations"],
        "startup_animation": False,
        "worker_mode": ui["worker_mode"],
        "workspace": {"density": ui["density"]},
        "intervals": intervals,
        "gpu_sampling": False,
        "weather": False,
        "weather_probes": [],
        "budget": False,
        "plugins": [],
        "alerts": [],
        "notify": {"command": "", "events": []},
        "log_max_mb": light["log_window_bytes"] / (1 << 20),
        "terminal": {"max_sessions": light["max_terminal_sessions"]},
        "collection_cache_dir": ""
        if value["storage"]["coordination_dir"] == "auto-host-local"
        else value["storage"]["coordination_dir"],
    }


def doctor(resolved):
    """Presence checks only: no scheduler calls, installations, or allocations."""
    value = resolved.data if isinstance(resolved, Resolved) else resolved
    from ..state_ownership import StateOwner
    from ..config_trust import directory_fd

    state = (
        StateOwner.preflight(value["storage"]["state_dir"])
        if value["storage"]["state_dir"]
        else {
            "state": "disabled",
            "path": "",
            "message": "No persistent state path is configured.",
        }
    )
    receipts = value["storage"].get("allocation_receipts_dir")
    receipt_status = {"status": "unknown", "path": receipts}
    if receipts:
        candidate = Path(receipts)
        while not candidate.exists() and candidate != candidate.parent:
            candidate = candidate.parent
        try:
            with directory_fd(candidate):
                pass
            receipt_status["status"] = "available"
        except (OSError, ValueError) as exc:
            receipt_status.update(status="inaccessible", reason=str(exc))
    tools = {}
    names = ["squeue", "sacct", "salloc", "srun", "ssh"]
    for name in names:
        path = shutil.which(name)
        tools[name] = {
            "status": "available" if path else "unsupported",
            "path": os.path.basename(path) if path else None,
            "qualification": "executable presence only; permissions and remote placement untested",
        }
    try:
        terminal = importlib.util.find_spec("pyte") is not None
    except (ImportError, ValueError):
        terminal = False
    return {
        "schema": "raven.doctor/v1",
        "profile": value["profile"],
        "site_kind": value["site"]["kind"],
        "mode": value["execution"]["startup"],
        "tools": tools,
        "terminal_engine": "available" if terminal else "unsupported",
        "state": state,
        "allocation_receipts": receipt_status,
        "cgroup": {
            "status": "unknown" if os.path.isdir("/sys/fs/cgroup") else "unsupported",
            "qualification": "delegation and enforcement are not inferred from directory presence",
        },
        "heavy": "requires explicit verified admission",
        "gpu": "not probed; request explicitly",
        "qualification": "bounded local presence checks; no live Slurm or site qualification",
        "mutations": 0,
    }


def worker_policy_check(request, *, system_policy_paths=None):
    """Worker-side mandatory policy check; client preferences cannot bypass it."""
    if not isinstance(request, dict):
        raise ValueError("Worker admission request must be an object.")
    kind = request.get("site_kind")
    if kind not in ("slurm", "desktop"):
        raise ValueError("Worker execution requires an explicit site kind.")
    if kind == "desktop" and not request.get("desktop_explicit"):
        raise ValueError("Desktop Heavy requires an explicit desktop profile.")
    data = merge(DEFAULTS, DESKTOP) if kind == "desktop" else copy.deepcopy(DEFAULTS)
    data["execution"]["startup"] = "heavy"
    resources = request.get("resources", {})
    for key in (
        "memory_mib",
        "scratch_mib",
        "max_tasks",
        "enforcement",
        "cpu_seconds",
        "wall_seconds",
        "input_bytes",
        "output_bytes",
    ):
        if key in resources:
            data["resources"]["heavy"][key] = resources[key]
    if "cpus" in resources:
        data["resources"]["heavy"]["max_workers"] = resources["cpus"]
    context = request.get("context", {})
    if isinstance(context, dict):
        for key in ("account", "partition", "qos"):
            if isinstance(context.get(key), str):
                data["allocation"][key] = context[key]
        if isinstance(context.get("transport"), str):
            data["connection"]["transport"] = context["transport"]
        if (
            isinstance(context.get("host"), str)
            and data["connection"]["transport"] == "ssh"
        ):
            data["connection"]["host"] = context["host"]
    policies = []
    for path in SYSTEM_POLICIES if system_policy_paths is None else system_policy_paths:
        if os.path.lexists(path):
            policies.append(
                (
                    "mandatory:" + str(path),
                    _policy(read_file(path, trusted=system_policy_paths is None)),
                )
            )
    validate(data)
    effective, _ = enforce_site_restrictions(data, policies)
    if effective["resources"]["heavy"] != data["resources"]["heavy"]:
        raise ValueError(
            "Requested worker resources exceed mandatory site restrictions; prepare a new admission."
        )
    return copy.deepcopy(request)


def reload_config(current, **options):
    """Build an independent candidate; callers swap it only after success.

    Reload never touches a worker, terminal, allocation, or stored evidence.
    Existing task identities keep their captured digest and destination.
    """
    if not isinstance(current, Resolved):
        raise ValueError("Reload requires the current resolved revision.")
    options.setdefault("config_path", current.source or current.legacy_source or None)
    if current.legacy_source and current.data["site"]["kind"] != "desktop":
        options.setdefault("legacy_profile", current.legacy_profile or None)
    else:
        options.setdefault("site", current.data["profile"])
    options.setdefault("launcher", current.data["execution"]["startup"])
    options.setdefault("site_policy_path", current.extra_policy_path or None)
    return resolve(**options)


def schema_document():
    """Publish the structural schema; cross-field/site checks remain runtime checks."""

    def properties(model, prefix=""):
        output = {}
        for name, value in model.items():
            path = prefix + "." + name if prefix else name
            if path == "profiles":
                output[name] = {
                    "type": "object",
                    "maxProperties": 32,
                    "additionalProperties": {"$ref": "#/$defs/profile"},
                }
            elif isinstance(value, dict):
                output[name] = {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": properties(value, path),
                }
            elif path == "schema":
                output[name] = {"const": SCHEMA}
            elif path in _ENUMS:
                output[name] = {"enum": sorted(_ENUMS[path])}
            elif isinstance(value, bool):
                output[name] = (
                    {"const": False} if path in _FALSE_ONLY else {"type": "boolean"}
                )
            elif path == "allocation.idle_release_seconds":
                output[name] = {"type": "integer", "minimum": 0, "maximum": 31536000}
            elif path == "collection.step_launch_interval_seconds":
                output[name] = {"type": "number", "minimum": 0.1, "maximum": 31536000}
            elif path.startswith("telemetry.custom."):
                from ..telemetry_profiles import BY_KEY

                field = BY_KEY[name]
                output[name] = {
                    "type": "integer" if field["type"] == "int" else "number",
                    "minimum": field["min"],
                    "maximum": field["max"],
                }
            elif isinstance(value, (int, float)):
                kind = (
                    "number"
                    if isinstance(value, float) or path.endswith("_seconds")
                    else "integer"
                )
                lower = (
                    0
                    if path == "allocation.gpus"
                    or path.endswith(("_per_minute", "jitter_fraction"))
                    else 0.001
                    if kind == "number"
                    else 1
                )
                output[name] = {"type": kind, "minimum": lower, "maximum": (1 << 40)}
            elif isinstance(value, list):
                item = (
                    {"enum": list(CAPABILITIES)}
                    if path == "execution.capabilities"
                    else {"type": "string", "maxLength": 512}
                )
                output[name] = {"type": "array", "items": item, "maxItems": 64}
            else:
                output[name] = {
                    "type": "string",
                    "maxLength": _STRINGS.get(path, 128),
                    "pattern": r"^[^\u0000\r\n]*$",
                }
        return output

    root = properties(DEFAULTS)
    partial = {
        "type": "object",
        "additionalProperties": False,
        "properties": {key: value for key, value in root.items() if key != "profiles"},
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "raven.config/v1",
        "title": "Raven configuration",
        "type": "object",
        "required": ["schema"],
        "additionalProperties": False,
        "properties": root,
        "$defs": {"profile": partial},
        "$comment": "Runtime validation also enforces placement, units, policy intersection and available adapters before admission.",
    }
