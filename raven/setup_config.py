"""Account discovery and reviewed preferences for the Setup allocation page.

Constructing this service does no I/O. Call discovery, staging and saving on
the existing background task lane. Discovery uses the governed, cached source
backend; saving preferences neither allocates resources nor starts a worker.
"""
from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import stat
import tempfile
import threading
import time

from . import raven_config as C
from .raven_allocation import ACTIVE
from .raven_allocation_adapters import config_data

FIELDS = ("account", "partition", "qos", "cpus_per_task", "memory_mib",
          "walltime_seconds", "gpus", "nodes", "tasks")
LIMITS = {"cpus_per_task": (1, 64), "memory_mib": (128, 1048576),
          "walltime_seconds": (1, 604800), "gpus": (0, 64),
          "nodes": (1, 1), "tasks": (1, 1)}
_LABEL = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.@:+/-]{0,127}\Z")
_USER = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}\$?\Z")
MAX_DISCOVERY = 256 * 1024
MAX_ASSOCIATIONS = 1024


def _label(value, name, optional=True):
    if optional and value == "":
        return value
    if not isinstance(value, str) or _LABEL.fullmatch(value) is None:
        raise ValueError(name + " must be one scheduler identifier, without spaces.")
    return value


def parse_associations(text, username):
    """Parse sacctmgr's explicit pipe format; never infer personal/lab status."""
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_DISCOVERY:
        raise ValueError("Account discovery exceeded its response limit.")
    result, seen = [], set()
    lines = text.splitlines()
    if len(lines) > MAX_ASSOCIATIONS:
        raise ValueError("Account discovery exceeded 1024 associations; enter an account manually.")
    for line in lines:
        if not line.strip():
            continue
        fields = line.strip().split("|")
        if len(fields) == 8 and fields[-1] == "":
            fields.pop()
        if len(fields) != 7:
            raise ValueError("Slurm returned an unsupported account association format.")
        user, default, cluster, account, partition, qos, default_qos = (x.strip() for x in fields)
        if user != username:
            continue
        if not account:
            continue
        for name, value in (("account", account), ("cluster", cluster), ("partition", partition),
                            ("default account", default), ("default QOS", default_qos)):
            _label(value, name)
        qualities = [item.strip() for item in qos.split(",") if item.strip()]
        if len(qualities) > 128:
            raise ValueError("Slurm returned too many QOS values for an association.")
        for quality in qualities:
            _label(quality, "QOS")
        key = (cluster, account, partition, tuple(qualities), default_qos)
        if key not in seen:
            seen.add(key)
            result.append({"account": account, "cluster": cluster, "partition": partition,
                           "qos": qualities, "default_qos": default_qos, "default": account == default})
    return sorted(result, key=lambda row: (not row["default"], row["cluster"], row["account"], row["partition"]))


def _read_snapshot(path):
    from .config_trust import read
    try:
        raw, info = read(path, limit=C.MAX_CONFIG, user_only=True)
    except FileNotFoundError:
        return None, None
    return raw, _snapshot_token(info, raw)


def _snapshot_token(info, raw):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, hashlib.sha256(raw).hexdigest())


def _decode(raw, path):
    if raw is None:
        return {"schema": C.SCHEMA}
    if path.suffix.lower() == ".toml":
        try:
            import tomllib
        except ImportError as exc:
            raise ValueError("Saving TOML requires Python 3.11 or later; use a JSON profile.") from exc
        value = tomllib.loads(raw.decode("utf-8"))
    else:
        def unique(items):
            output = {}
            for key, value in items:
                if key in output:
                    raise ValueError("Duplicate configuration key: " + key)
                output[key] = value
            return output
        value = json.loads(raw, object_pairs_hook=unique)
    if not isinstance(value, dict) or value.get("schema") != C.SCHEMA:
        raise ValueError("Migrate this configuration to raven.config/v1 before saving Setup preferences.")
    C.validate(value, partial=True)
    return value


def _encode(document, path):
    if path.suffix.lower() != ".toml":
        return (json.dumps(document, indent=2, ensure_ascii=True, allow_nan=False) + "\n").encode()
    # The config contract contains only finite JSON values, with no arrays of
    # tables or TOML dates. Quoted keys preserve named profiles exactly.
    lines = []
    def emit(table, prefix=()):
        if prefix:
            lines.append("[" + ".".join(json.dumps(key) for key in prefix) + "]")
        for key, value in table.items():
            if not isinstance(value, dict):
                lines.append(json.dumps(key) + " = " + json.dumps(value, ensure_ascii=False, allow_nan=False))
        lines.append("")
        for key, value in table.items():
            if isinstance(value, dict):
                emit(value, prefix + (key,))
    emit(document)
    raw = ("\n".join(lines).rstrip() + "\n").encode("utf-8")
    if _decode(raw, path) != document:
        raise ValueError("Configuration cannot be represented safely as TOML.")
    return raw


def _private_parent(path):
    """Create only beneath pinned, trusted ancestors; never follow a link."""
    from .config_trust import directory_fd
    with directory_fd(path.parent, create=True) as parent:
        if os.fstat(parent).st_uid != os.getuid():
            raise ValueError("The configuration directory must belong to the current user.")


def _atomic_write(path, document, token):
    raw = _encode(document, path)
    if len(raw) > C.MAX_CONFIG:
        raise ValueError("Updated configuration exceeds 256 KiB.")
    _private_parent(path)
    lock_fd = os.open(path.with_name("." + path.name + ".setup.lock"),
                      os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    temporary, written_fd = None, None
    warnings = []
    try:
        info = os.fstat(lock_fd)
        if info.st_uid != os.getuid() or not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise ValueError("Setup's configuration lock must be a private owned file.")
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if _read_snapshot(path)[1] != token:
            raise ValueError("Configuration changed since Setup was opened; relaunch Raven to load the current file before editing allocation settings.")
        fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
            # Pin our written inode through publication. A separate read of
            # the path afterward could accidentally adopt another writer's
            # replacement as the baseline for this service's old document.
            written_fd = os.dup(stream.fileno())
        if _read_snapshot(path)[1] != token:
            raise ValueError("Configuration changed while saving; relaunch Raven to load the current file before editing allocation settings.")
        if token is None:
            # link is an atomic no-clobber publication for a new destination.
            os.link(temporary, path, follow_symlinks=False)
            try:
                os.unlink(temporary)
                temporary = None
            except OSError:
                warnings.append("Settings were saved, but a private temporary configuration file could not be removed.")
        else:
            os.replace(temporary, path)
            temporary = None
        try:
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError:
            # Some HPC filesystems reject directory fsync (for example with
            # EINVAL). The new file is already visible. Report this durability
            # limit while keeping the manager and UI consistent with the save.
            warnings.append("Settings were saved, but the filesystem could not confirm directory durability; persistence after a system failure is not guaranteed.")
        try:
            saved_token = _snapshot_token(os.fstat(written_fd), raw)
        except OSError:
            saved_token = None
            warnings.append("Settings were saved, but their file identity could not be verified; relaunch Raven before editing allocation settings again.")
        return saved_token, warnings
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                # Never replace the original result with a cleanup error.
                # Any remaining temporary file is private to the current user.
                pass
        for fd in (written_fd, lock_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass


class AllocationSetup:
    """A connection-scoped draft; no changes take effect until explicit Save."""

    def __init__(self, resolved, backend=None, *, manager=None, env=None):
        if not isinstance(resolved, C.Resolved):
            raise ValueError("Allocation Setup needs a managed Raven profile; launch with ravenl or ravenh.")
        self.resolved, self.backend, self.manager = resolved, backend, manager
        self.env = dict(os.environ if env is None else env)
        self._lock = threading.RLock()
        self._snapshot = None
        self._save_unavailable = False
        self._loaded = False
        self._cached = None
        self._cache_until = 0.
        source = resolved.source or str(Path(self.env.get("XDG_CONFIG_HOME") or
                                            Path(self.env.get("HOME") or os.path.expanduser("~")) / ".config") / "raven/config.json")
        self.path = Path(source).expanduser().absolute()
        self.scope = hashlib.sha256(C.canonical({"profile": resolved.data["profile"],
                                               "connection": resolved.data["connection"],
                                               "site": resolved.data["site"], "uid": os.getuid()})).hexdigest()

    def _load(self):
        if self._save_unavailable:
            raise ValueError("The saved file identity is unavailable; relaunch Raven before editing allocation settings again.")
        if not self._loaded:
            if self.resolved.legacy_source:
                raise ValueError("Migrate the legacy configuration before saving Allocation settings.")
            raw, self._snapshot = _read_snapshot(self.path)
            self._document = _decode(raw, self.path)
            self._loaded = True

    def _query(self, argv, limit=MAX_DISCOVERY):
        if self.backend is None or getattr(self.backend, "ledger", None) is None:
            raise ValueError("Account discovery requires Raven's governed connection; manual entry remains available.")
        # run() uses the shared observational cache and quotas. command() is a
        # fresh action-review lane, which this discovery must not consume.
        text, _ = self.backend.run(argv, timeout=8.)
        if not isinstance(text, str) or len(text.encode("utf-8")) > limit:
            raise ValueError("Discovery response exceeded its size limit.")
        return text

    def discover(self, *, refresh=False):
        """Read associations, optionally retrying the governed source cache.

        Explicit refresh bypasses only this service's convenience cache. The
        source backend still owns cache freshness, quotas, and circuit recovery.
        """
        with self._lock:
            if not refresh and self._cached is not None and time.monotonic() < self._cache_until:
                return copy.deepcopy(self._cached)
            data = self.resolved.data
            result = {"username": "", "cluster": "", "accounts": [], "partitions": [],
                      "errors": [], "observed_at": time.time(), "scope": self.scope,
                      "qualification": "Associations are visible memberships, not a guarantee of available allocation or permission."}
            try:
                self._load()
            except (OSError, ValueError) as exc:
                result["errors"].append(str(exc)[:500])
            try:
                user = (self._query(["id", "-un"], 256).strip()
                        if data["connection"]["transport"] == "ssh" else pwd.getpwuid(os.getuid()).pw_name)
                if _USER.fullmatch(user) is None:
                    raise ValueError("The authenticated connection returned an unsupported user name.")
                result["username"] = user
            except (OSError, KeyError, ValueError, RuntimeError) as exc:
                result["errors"].append("Identity: " + str(exc)[:500])
            if result["username"] and data["site"]["kind"] == "slurm":
                try:
                    raw = self._query(["sacctmgr", "-n", "-P", "show", "user", "withassoc", "where",
                                       "name=" + result["username"],
                                       "format=User,DefaultAccount,Cluster,Account,Partition,QOS,DefaultQOS"])
                    result["accounts"] = parse_associations(raw, result["username"])
                    if not result["accounts"]:
                        result["errors"].append("No visible Slurm account associations; use scheduler defaults or enter your assigned account.")
                except (OSError, ValueError, RuntimeError) as exc:
                    result["errors"].append("Accounts: " + str(exc)[:500])
                try:
                    raw = self._query(["sinfo", "--noheader", "--format=%P"])
                    values = {line.strip().removesuffix("*") for line in raw.splitlines() if line.strip()}
                    if len(values) > 1024:
                        raise ValueError("Partition discovery exceeded 1024 entries.")
                    result["partitions"] = sorted(_label(value, "partition", False) for value in values)
                except (OSError, ValueError, RuntimeError) as exc:
                    result["errors"].append("Partitions: " + str(exc)[:500])
                try:
                    raw = self._query(["scontrol", "show", "config"])
                    match = re.search(r"^\s*ClusterName\s*=\s*(\S+)\s*$", raw, re.M)
                    if match is not None:
                        result["cluster"] = _label(match[1], "cluster", False)
                except (OSError, ValueError, RuntimeError) as exc:
                    result["errors"].append("Cluster: " + str(exc)[:500])
            self._cached = copy.deepcopy(result)
            self._cache_until = time.monotonic() + (30 if result["errors"] else 900)
            return result

    def stage(self, changes):
        with self._lock:
            self._load()
            if not isinstance(changes, dict) or set(changes) - set(FIELDS):
                raise ValueError("Setup accepts only the documented allocation fields.")
            candidate = self.resolved.as_dict()
            changes = copy.deepcopy(changes)
            for key, value in changes.items():
                if self.resolved.origins.get("allocation." + key) == "CLI" and value != candidate["allocation"][key]:
                    raise ValueError(key + " is set by a command-line override; relaunch without that override before editing it.")
                if key in LIMITS:
                    low, high = LIMITS[key]
                    if type(value) is not int or not low <= value <= high:
                        raise ValueError(key + f" must be an integer in {low}..{high}.")
                else:
                    _label(value, key)
                candidate["allocation"][key] = value
            # Unchanged command-line values remain overrides, never become
            # file preferences or lose their origin after saving another field.
            changes = {key: value for key, value in changes.items()
                       if value != self.resolved.data["allocation"][key]}
            if candidate["site"]["kind"] != "slurm":
                raise ValueError("This standalone desktop profile has no Slurm allocation; use a Slurm profile for account settings.")
            C.validate(candidate)
            captured = {**candidate, "_raven_site_restrictions": copy.deepcopy(self.resolved.restrictions)}
            effective = C.admission_config(captured)
            effective["execution"]["startup"] = candidate["execution"]["startup"]
            warnings = []
            for key in FIELDS:
                if effective["allocation"][key] != candidate["allocation"][key]:
                    warnings.append(key + " was limited by site policy to " + str(effective["allocation"][key]) + ".")
            if not effective["allocation"]["account"]:
                warnings.append("No account flag will be sent. Slurm chooses its configured default; site limits still apply.")
            if self.path.suffix.lower() == ".toml":
                warnings.append("Saving TOML preserves all configuration values; comments and formatting are rewritten.")
            # Stage is an immutable value to the caller: save independently
            # validates its changes again, rather than trusting edited data.
            return {"changes": changes, "effective": copy.deepcopy(effective), "warnings": warnings,
                    "revision": self.resolved.digest, "scope": self.scope}

    def save(self, draft):
        with self._lock:
            if not isinstance(draft, dict) or draft.get("revision") != self.resolved.digest or draft.get("scope") != self.scope:
                raise ValueError("Allocation settings changed; reopen the Setup page before saving.")
            checked = self.stage(draft.get("changes"))
            changes = checked["changes"]
            if not changes:
                raise ValueError("No allocation settings have changed.")
            candidate = checked["effective"]
            document = copy.deepcopy(self._document)
            profile = self.resolved.data["profile"]
            target = document.get("profiles", {}).get(profile)
            if target is None:
                # A default file with --site desktop cannot accidentally become
                # a Slurm file. Standalone desktop is rejected in stage above.
                if profile in ("default-slurm", "slurm") or document.get("profile") == profile:
                    target = document
                else:
                    target = document.setdefault("profiles", {}).setdefault(profile, {})
            allocation = target.setdefault("allocation", {})
            for key in changes:
                allocation[key] = candidate["allocation"][key]
            C.validate(document, partial=True)
            origins = copy.deepcopy(self.resolved.origins)
            for key in changes:
                if candidate["allocation"][key] != changes[key]:
                    origins["allocation." + key] = "site restriction"
                else:
                    origins["allocation." + key] = ("profile:" + profile if target is not document else "file:" + str(self.path))
            updated = C.Resolved(candidate, origins, self.resolved.restrictions, self.resolved.warnings, str(self.path),
                                 source_telemetry=copy.deepcopy(self.resolved.source_telemetry),
                                 extra_policy_path=self.resolved.extra_policy_path)
            manager = self.manager
            # Hold the same lock as request() while checking state, writing,
            # and swapping the Light manager's detached launch preferences.
            guard = manager._lock if manager is not None else threading.RLock()
            with guard:
                if manager is not None:
                    if manager._closed or manager.snapshot().get("state") in ACTIVE or manager._reuse_future is not None:
                        raise ValueError("Return to Light and finish allocation review before saving allocation settings.")
                    if manager.config.get("allocation") != self.resolved.data["allocation"]:
                        raise ValueError("Allocation configuration changed; relaunch Raven before editing allocation settings.")
                saved_token, save_warnings = _atomic_write(self.path, document, self._snapshot)
                if manager is not None:
                    manager.config = config_data(updated)
                    manager.policy_digest = updated.digest
                    manager._reuse_receipt = None
                    manager._state = {key: value for key, value in manager._state.items() if key != "reuse_review"}
            self.resolved, self._document = updated, document
            self._snapshot = saved_token
            self._save_unavailable = saved_token is None
            return {"path": str(self.path), "allocation": copy.deepcopy(candidate["allocation"]),
                    "data": copy.deepcopy(candidate), "warnings": checked["warnings"] + save_warnings, "resolved": updated}
