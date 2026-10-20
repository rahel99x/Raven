"""Reviewed site profiles and portable project launch preferences.

Review never creates files or contacts a scheduler. Saving a site affects future
launches only; it cannot rebind a running worker or allocate scientific compute.
The existing allocation discovery and atomic config writer remain authoritative.
"""

from __future__ import annotations

import copy
from decimal import Decimal, ROUND_CEILING
import hashlib
import os
from pathlib import Path
import re
import shlex
import threading

from . import policy as C
from . import allocation as S

KINDS = {
    "local-slurm": "Workstation with Slurm",
    "ssh-slurm": "HPC cluster over SSH",
    "standalone": "Standalone workstation",
}
_MEMORY = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*([kmgt](?:i?b)?|b)?", re.I)
_DURATION = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*([dhms])", re.I)


def _text(value, field, *, empty=True, limit=128):
    if (
        not isinstance(value, str)
        or len(value) > limit
        or not value.isprintable()
        and value
    ):
        raise ValueError(field + " must be printable single-line text.")
    value = value.strip()
    if not empty and not value:
        raise ValueError(field + " is required.")
    return value


def _positive(value, field, maximum=1 << 40, minimum=1):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(field + " must be a whole number.")
    text = str(value).strip()
    if not re.fullmatch(r"[0-9]{1,16}", text):
        raise ValueError(field + " must be a whole number.")
    number = int(text)
    if not minimum <= number <= maximum:
        raise ValueError(field + f" must be between {minimum} and {maximum}.")
    return number


def parse_memory_mib(value):
    """Return whole MiB, rounding up; bare values mean MiB, GB is decimal."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("Memory must be a size such as 8 GiB or 8192 MiB.")
    text = str(value).strip()
    match = _MEMORY.fullmatch(text) if len(text) <= 64 else None
    if not match:
        raise ValueError("Memory must be a size such as 8 GiB or 8192 MiB.")
    units = {
        "": 1 << 20,
        "b": 1,
        "k": 1 << 10,
        "kb": 1000,
        "kib": 1 << 10,
        "m": 1 << 20,
        "mb": 1000**2,
        "mib": 1 << 20,
        "g": 1 << 30,
        "gb": 1000**3,
        "gib": 1 << 30,
        "t": 1 << 40,
        "tb": 1000**4,
        "tib": 1 << 40,
    }
    number = Decimal(match[1]) * units[(match[2] or "").lower()] / (1 << 20)
    if not 0 < number <= (1 << 40):
        raise ValueError("Memory must be positive and at most 1099511627776 MiB.")
    return int(number.to_integral_value(rounding=ROUND_CEILING))


def parse_duration_seconds(value):
    """Accept seconds, 1h 30m, HH:MM:SS, MM:SS, or D-HH:MM:SS."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("Time must be a duration such as 1h or 01:00:00.")
    text = str(value).strip().lower()
    if not text or len(text) > 64:
        raise ValueError("Time must be a duration such as 1h or 01:00:00.")
    if text.isascii() and text.isdigit():
        return _positive(text, "Time in seconds", maximum=31536000)
    clock = re.fullmatch(r"(?:(\d+)-)?(\d+):(\d{2})(?::(\d{2}))?", text)
    if clock:
        days, first, second, third = clock.groups()
        if (
            days is not None
            and third is None
            or int(second) > 59
            or third is not None
            and int(third) > 59
            or days is not None
            and int(first) > 23
        ):
            raise ValueError(
                "Clock time requires valid D-HH:MM:SS, HH:MM:SS or MM:SS fields."
            )
        seconds = (
            (int(days or 0) * 86400 + int(first) * 3600 + int(second) * 60 + int(third))
            if third is not None
            else int(first) * 60 + int(second)
        )
    else:
        seconds, offset, seen = Decimal(0), 0, set()
        for match in _DURATION.finditer(text):
            if text[offset : match.start()].strip() or match[2] in seen:
                raise ValueError(
                    "Time requires distinct duration units, for example 1h 30m."
                )
            seen.add(match[2])
            seconds += (
                Decimal(match[1]) * {"d": 86400, "h": 3600, "m": 60, "s": 1}[match[2]]
            )
            offset = match.end()
        if not seen or text[offset:].strip():
            raise ValueError("Time must be a duration such as 1h or 01:00:00.")
        seconds = int(seconds.to_integral_value(rounding=ROUND_CEILING))
    return _positive(seconds, "Time in seconds", maximum=31536000)


def workload_defaults(
    *,
    account="",
    partition="",
    qos="",
    cpus=None,
    memory=None,
    time_limit=None,
    gpus=None,
    nodes=None,
    tasks=None,
):
    """Build scientific-workload defaults, never Heavy-worker preferences.

    Empty values are omitted so script directives and scheduler defaults remain
    authoritative. Project defaults already have lower precedence than scripts.
    """
    result = {}
    for key, value in (("account", account), ("partition", partition), ("qos", qos)):
        if value not in (None, ""):
            result[key] = S._label(_text(value, key), key, False)
    for key, value in (
        ("cpus-per-task", cpus),
        ("gpus", gpus),
        ("nodes", nodes),
        ("ntasks", tasks),
    ):
        if value not in (None, ""):
            result[key] = str(_positive(value, key, minimum=0 if key == "gpus" else 1))
    if memory not in (None, ""):
        result["mem"] = str(parse_memory_mib(memory)) + "M"
    if time_limit not in (None, ""):
        seconds = parse_duration_seconds(time_limit)
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        result["time"] = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return result


def save_project_recipe(project, name, defaults, *, expected=None):
    """Save a launch recipe under .raven, preserving unrelated project settings."""
    from ..launching import settings as L

    checked = L.validate_settings(
        {"recipes": {_text(name, "Recipe name", empty=False, limit=64): defaults}}
    )
    return L.update_settings(
        project, {"recipes": checked["recipes"]}, expected=expected
    )


def _profile_id(label):
    slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:100]
    return slug or "profile-" + hashlib.sha256(label.encode()).hexdigest()[:12]


def _kind(data):
    return (
        "standalone"
        if data["site"]["kind"] == "desktop"
        else "ssh-slurm"
        if data["connection"]["transport"] == "ssh"
        else "local-slurm"
    )


class ProfileSetup:
    """A bounded snapshot of named sites; saves only on an explicit action."""

    def __init__(self, resolved, backend=None, *, env=None):
        if not isinstance(resolved, C.Resolved):
            raise ValueError("Site setup requires a managed Raven configuration.")
        self.resolved, self.backend = resolved, backend
        self.env = dict(os.environ if env is None else env)
        self.path = (
            Path(resolved.source or str(C._roots(self.env)[0] / "raven/config.json"))
            .expanduser()
            .absolute()
        )
        self._loaded, self._token, self._document = False, None, None
        self._lock = threading.RLock()
        self._save_unavailable = False
        self._discovery = S.AllocationSetup(resolved, backend, env=self.env)

    def _load(self):
        if self._save_unavailable:
            raise ValueError(
                "The saved configuration identity is unavailable; reopen Raven before saving again."
            )
        if not self._loaded:
            if self.resolved.legacy_source:
                raise ValueError(
                    "Migrate the legacy configuration before saving named sites."
                )
            raw, self._token = S._read_snapshot(self.path)
            self._document = S._decode(raw, self.path)
            self._loaded = True

    def _revision(self):
        return hashlib.sha256(
            C.canonical({"path": str(self.path), "token": self._token})
        ).hexdigest()

    def _base(self, profile=None):
        # Match resolve(): the built-in desktop baseline precedes file
        # preferences, while a named desktop overlay applies it afterward.
        base = C.merge(C.DEFAULTS, C.DESKTOP if profile == "desktop" else {})
        base = C.merge(
            base,
            {key: value for key, value in self._document.items() if key != "profiles"},
        )
        _config, state, home = C._roots(self.env)
        for key, default in (
            ("state_dir", state / "raven"),
            ("durable_results", home / "raven-results"),
        ):
            base["storage"][key] = base["storage"][key] or str(default)
        base["profiles"] = copy.deepcopy(self._document.get("profiles", {}))
        return base

    def list_profiles(self):
        with self._lock:
            self._load()
            entries = copy.deepcopy(self._document.get("profiles", {}))
            root = self._document.get("profile", self.resolved.data["profile"])
            entries.setdefault(root, {})
            result = []
            for key, overlay in entries.items():
                base = self._base(key)
                if overlay.get("site", {}).get("kind") == "desktop":
                    base = C.merge(base, C.DESKTOP)
                data = C.merge(base, overlay)
                if (
                    not self.resolved.source
                    and key == self.resolved.data["profile"]
                    and not overlay
                ):
                    data = self.resolved.as_dict()
                result.append(
                    {
                        "profile": key,
                        "label": data["ui"]["profile_label"] or key,
                        "kind": _kind(data),
                        "host": data["connection"]["host"],
                        "user": data["connection"]["user"],
                        "cluster": data["site"]["cluster_id"],
                        "heavy_account": data["allocation"]["account"],
                        "worker_python": data["execution"]["worker_python"],
                        "connection": copy.deepcopy(data["connection"]),
                        "heavy": copy.deepcopy(data["allocation"]),
                        "default": key == root,
                        "active": key == self.resolved.data["profile"],
                    }
                )
            return sorted(
                result, key=lambda item: (not item["default"], item["label"].casefold())
            )

    def stage(
        self,
        name,
        kind,
        *,
        host="",
        user="",
        cluster=None,
        worker_python=None,
        heavy=None,
        make_default=True,
    ):
        with self._lock:
            self._load()
            label = _text(name, "Site name", empty=False)
            if kind not in KINDS or type(make_default) is not bool:
                raise ValueError(
                    "Choose local-slurm, ssh-slurm, or standalone and an explicit default preference."
                )
            known = {row["profile"]: row for row in self.list_profiles()}
            matches = [
                key
                for key, row in known.items()
                if row["label"] == label or key == label
            ]
            if len(matches) > 1:
                raise ValueError(
                    "Site name is ambiguous; select its exact profile identifier."
                )
            key = matches[0] if matches else _profile_id(label)
            if key in known and not matches:
                raise ValueError(
                    "This site name maps to an existing identifier; choose a distinct name."
                )
            if matches and label == key:
                label = known[key]["label"]
            if (
                key not in self._document.get("profiles", {})
                and len(self._document.get("profiles", {})) >= 32
            ):
                raise ValueError("At most 32 named sites can be saved.")
            host, user = _text(host, "SSH host", limit=256), _text(user, "SSH user")
            if kind == "ssh-slurm":
                if (
                    not host
                    or host.startswith("-")
                    or not re.fullmatch(r"[A-Za-z0-9_.:\[\]-]+", host)
                ):
                    raise ValueError(
                        "SSH needs a host name or SSH-config alias; put the login in the separate user field."
                    )
                if user and S._USER.fullmatch(user) is None:
                    raise ValueError("SSH user must be a login name.")
            elif host or user:
                raise ValueError(
                    "SSH host and user apply only to an HPC cluster over SSH."
                )
            same_connection = (
                key in known
                and known[key]["kind"] == kind
                and known[key]["host"] == host
                and known[key]["user"] == user
            )
            if cluster is None:
                cluster = known[key]["cluster"] if same_connection else ""
            if worker_python is None:
                worker_python = known[key]["worker_python"] if same_connection else ""
            cluster = S._label(_text(cluster, "Cluster name"), "Cluster name")
            worker_python = _text(worker_python, "Worker Python", limit=4096)
            if worker_python and not os.path.isabs(worker_python):
                raise ValueError(
                    "Worker Python must be an absolute path on the execution host."
                )
            if not worker_python and kind != "ssh-slurm":
                from ..install_paths import worker_python as stable_python

                worker_python = stable_python()
            if heavy is None:
                heavy = {}
            if not isinstance(heavy, dict) or set(heavy) - set(S.FIELDS):
                raise ValueError(
                    "Heavy preferences accept only allocation account and resource fields."
                )
            allocation = copy.deepcopy(C.DEFAULTS["allocation"])
            if key in known and known[key]["kind"] == kind:
                allocation.update(known[key]["heavy"])
            normalized = {}
            for field, value in heavy.items():
                if field in S.LIMITS:
                    value = (
                        parse_memory_mib(value)
                        if field == "memory_mib"
                        else parse_duration_seconds(value)
                        if field == "walltime_seconds"
                        else _positive(
                            value, field, minimum=0 if field == "gpus" else 1
                        )
                    )
                    low, high = S.LIMITS[field]
                    if not low <= value <= high:
                        raise ValueError(
                            field
                            + f" must be in {low}..{high} for Raven's Heavy worker."
                        )
                else:
                    value = S._label(_text(value, field), field)
                normalized[field] = value
            if kind == "standalone" and any(
                normalized.get(field) for field in ("account", "partition", "qos")
            ):
                raise ValueError(
                    "A standalone workstation has no Slurm billing account, partition or QOS."
                )
            allocation.update(normalized)
            allocation["adapter"] = "desktop" if kind == "standalone" else "slurm-cli"
            connection = copy.deepcopy(C.DEFAULTS["connection"])
            # Editing resources on an existing connection must preserve SSH
            # options and credential references; a new target starts clean.
            if same_connection:
                connection = copy.deepcopy(known[key]["connection"])
            connection.update(
                transport="none"
                if kind == "standalone"
                else "ssh"
                if kind == "ssh-slurm"
                else "local",
                host=host,
                user=user,
            )
            overlay = copy.deepcopy(self._document.get("profiles", {}).get(key, {}))
            overlay = C.merge(
                overlay,
                {
                    "site": {
                        "kind": "desktop" if kind == "standalone" else "slurm",
                        "cluster_id": cluster,
                    },
                    "connection": connection,
                    "execution": {
                        "startup": "light",
                        "heavy_target": "local" if kind == "standalone" else "slurm",
                        "heavy_requires_allocation": kind != "standalone",
                        "desktop_execution": "explicit"
                        if kind == "standalone"
                        else "disabled",
                        "worker_python": worker_python,
                    },
                    "allocation": allocation,
                    "collection": {"slurm_enabled": kind != "standalone"},
                    "ui": {"profile_label": label},
                },
            )
            if kind == "standalone":
                # Standalone admission takes its actual worker limits from
                # resources.heavy rather than Slurm allocation preferences.
                for field, target in (
                    ("cpus_per_task", "max_workers"),
                    ("memory_mib", "memory_mib"),
                    ("walltime_seconds", "wall_seconds"),
                ):
                    if field in normalized:
                        overlay.setdefault("resources", {}).setdefault("heavy", {})[
                            target
                        ] = normalized[field]
            candidate = C.merge(self._base(), overlay)
            candidate["profile"] = key
            C.validate(candidate)
            policies = [
                (
                    "captured:" + str(index),
                    {"schema": C.POLICY_SCHEMA, "restrictions": item["rules"]},
                )
                for index, item in enumerate(self.resolved.restrictions)
                if "rules" in item
            ]
            for path in C.SYSTEM_POLICIES:
                if os.path.lexists(path):
                    policies.append(
                        (
                            "mandatory:" + path,
                            C._policy(C.read_file(path, trusted=True)),
                        )
                    )
            if candidate["site"]["policy_file"]:
                policy = os.path.expanduser(candidate["site"]["policy_file"])
                policies.append(("selected:" + policy, C._policy(C.read_file(policy))))
            effective, restrictions = C.enforce_site_restrictions(candidate, policies)
            # Persist the values actually reviewed, including resource caps
            # from a session-specific policy. Mandatory policy is checked
            # again when saving and by the normal launcher on later runs.
            requested_fields = C.flatten(candidate)
            for path, value in C.flatten(effective).items():
                if value != requested_fields.get(path):
                    target = overlay
                    parts = path.split(".")
                    for part in parts[:-1]:
                        target = target.setdefault(part, {})
                    target[parts[-1]] = copy.deepcopy(value)
            warnings = [
                "Saving this site changes future launches. The current connection and Heavy worker stay unchanged.",
                "Heavy-worker billing and resources do not configure the scientific workload. Save workload defaults as a project recipe.",
            ]
            warnings += [
                item["path"]
                + " is limited by site policy to "
                + str(item["effective"])
                + "."
                for item in restrictions
                if "path" in item and item["effective"] != item["requested"]
            ]
            if kind != "standalone" and not allocation["account"]:
                warnings.append(
                    "No Heavy account flag will be sent. Slurm selects its configured default; site admission rules still apply."
                )
            if kind == "ssh-slurm" and not worker_python:
                warnings.append(
                    "Set the remote Worker Python path before requesting Heavy; Light can connect without it."
                )
            if self.path.suffix.lower() == ".toml":
                warnings.append(
                    "Saving preserves TOML values but rewrites comments and formatting."
                )
            sources = {field: "Raven default" for field in C.flatten(C.DEFAULTS)}
            sources.update(
                {field: "configuration file" for field in C.flatten(self._document)}
            )
            sources.update({field: "site " + label for field in C.flatten(overlay)})
            for item in restrictions:
                if "path" in item:
                    sources[item["path"]] = "site policy"
            return {
                "profile": key,
                "label": label,
                "kind": kind,
                "effective": effective,
                "sources": sources,
                "warnings": warnings,
                "revision": self._revision(),
                "path": str(self.path),
                "make_default": make_default,
                "overlay": overlay,
                "changes": {
                    "name": label,
                    "kind": kind,
                    "host": host,
                    "user": user,
                    "cluster": cluster,
                    "worker_python": worker_python,
                    "heavy": normalized,
                    "make_default": make_default,
                },
            }

    def save(self, draft):
        with self._lock:
            self._load()
            if not isinstance(draft, dict) or draft.get("revision") != self._revision():
                raise ValueError(
                    "Site preferences changed; reopen Setup and review before saving."
                )
            checked = self.stage(**draft.get("changes", {}))
            document = copy.deepcopy(self._document)
            document.setdefault("profiles", {})[checked["profile"]] = checked["overlay"]
            if checked["make_default"]:
                document["profile"] = checked["profile"]
            C.validate(document, partial=True)
            token, warnings = S._atomic_write(self.path, document, self._token)
            self._token, self._document = token, document
            self._save_unavailable = token is None
            command = shlex.join(
                ["ravenl", "--config", str(self.path), "--site", checked["profile"]]
            )
            return {
                "path": str(self.path),
                "profile": checked["profile"],
                "label": checked["label"],
                "command": command,
                "warnings": checked["warnings"] + warnings,
            }

    def discover(self, draft=None, *, refresh=False):
        data = draft.get("effective", {}) if draft else self.resolved.data
        connection = data.get("connection")
        if (
            connection != self.resolved.data["connection"]
            or data.get("site", {}).get("kind") != self.resolved.data["site"]["kind"]
        ):
            return {
                "username": "",
                "cluster": "",
                "accounts": [],
                "partitions": [],
                "errors": [
                    "Save this site and connect to it before discovering its login and Slurm account associations."
                ],
                "qualification": "Accounts are discovered only through the active governed connection.",
            }
        return self._discovery.discover(refresh=refresh)
