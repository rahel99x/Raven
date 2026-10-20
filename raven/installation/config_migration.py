"""Explicit copy-only Raven migration with bounded inventory and rollback.

Legacy files, evidence schema identifiers, active journals and socket ownership
are never rewritten. A private journal records only newly created destinations.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import time
import uuid

from ..configuration.policy import (
    DEFAULTS,
    SCHEMA,
    canonical,
    legacy_settings,
    merge,
    read_file,
    validate,
    _roots,
)

PLAN_SCHEMA = "raven.migration-plan/v1"
JOURNAL_SCHEMA = "raven.migration-journal/v1"
ACTIVATION_SCHEMA = "raven.migration-activation/v1"
UI_IMPORT_BYTES = 256 << 10
MAX_FILES = 256
MAX_FILE = 16 << 20
MAX_TOTAL = 64 << 20
MAX_PLAN = 2 << 20
_EXCLUDED_NAMES = {
    "plugins",
    "services",
    "sockets",
    "runtime",
    "cache",
    "caches",
    "terminal",
    "ssh",
    "masters",
}
_EXCLUDED_SUFFIXES = (
    ".sock",
    ".socket",
    ".pid",
    ".lock",
    ".db",
    ".sqlite",
    ".sqlite3",
    "-wal",
    "-shm",
)
_HISTORY_NAMES = {"series", "history", "events", "journals", "traces", "recordings"}


def _json(value):
    return (
        json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False, indent=2)
        + "\n"
    ).encode()


def _digest(value):
    return hashlib.sha256(_json(value)).hexdigest()


def private_directory(path):
    """Create private owned directories; never traverse a destination symlink."""
    path = Path(os.path.abspath(os.path.expanduser(os.fspath(path))))
    from ..config_trust import directory_fd

    try:
        with directory_fd(path, private=True, create=True):
            pass
    except OSError as exc:
        if exc.errno not in (errno.ELOOP, errno.ENOTDIR):
            raise
        raise ValueError(
            "Migration destinations must be directories without symlinks."
        ) from exc
    return path


def write_new(path, data, *, before_publish=None):
    """Publish a complete new private file atomically, without overwrite."""
    path = Path(path)
    private_directory(path.parent)
    temporary = path.parent / ("." + path.name + ".raven-" + uuid.uuid4().hex)
    fd = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
    )
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            identity = os.fstat(stream.fileno())
        if before_publish is not None:
            before_publish(
                {
                    "path": str(path),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "device": identity.st_dev,
                    "inode": identity.st_ino,
                    "staging": str(temporary),
                }
            )
        os.link(temporary, path, follow_symlinks=False)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _read(path, limit=MAX_FILE):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_uid not in (os.getuid(), 0):
            raise ValueError(
                "Migration reads only regular files owned by this user or root."
            )
        if before.st_size > limit:
            raise ValueError("File exceeds the explicit migration byte limit.")
        data = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    identity = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns)
    if len(data) > limit or identity(before) != identity(after):
        raise ValueError(
            "File changed while reading; stop the old writer and prepare a new plan."
        )
    return data, {
        "device": before.st_dev,
        "inode": before.st_ino,
        "bytes": len(data),
        "mtime_ns": before.st_mtime_ns,
        "sha256": hashlib.sha256(data).hexdigest(),
        "mode": stat.S_IMODE(before.st_mode),
    }


def _unsafe_name(path, include_history):
    names = set(path.parts)
    if names & _EXCLUDED_NAMES or str(path).endswith(_EXCLUDED_SUFFIXES):
        return "runtime/cache/service ownership is never migrated"
    if not include_history and (
        names & _HISTORY_NAMES or path.suffix in (".jsonl", ".gz")
    ):
        return "history requires --include-history; originals remain readable"
    return ""


def inventory(root, *, include_history=False, max_files=MAX_FILES, max_total=MAX_TOTAL):
    root = Path(os.path.abspath(os.path.expanduser(os.fspath(root))))
    rows, skipped = [], []
    total = 0
    if not root.exists():
        return {
            "root": str(root),
            "files": [],
            "skipped": [],
            "bytes": 0,
            "limited": False,
        }
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Legacy inventory root must be a real directory.")
    pending, limited = [(root, 0)], False
    visits = 0
    while pending:
        directory, depth = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                visits += 1
                relative = Path(entry.path).relative_to(root)
                reason = _unsafe_name(relative, include_history)
                if visits > max_files * 4 or len(rows) >= max_files:
                    limited = True
                    break
                info = entry.stat(follow_symlinks=False)
                if (
                    reason
                    or stat.S_ISLNK(info.st_mode)
                    or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode))
                ):
                    skipped.append(
                        {
                            "path": str(relative),
                            "reason": reason or "symlink or non-regular active source",
                        }
                    )
                    continue
                if stat.S_ISDIR(info.st_mode):
                    if depth >= 3:
                        skipped.append(
                            {"path": str(relative), "reason": "bounded inventory depth"}
                        )
                        limited = True
                    else:
                        pending.append((Path(entry.path), depth + 1))
                    continue
                if info.st_size > MAX_FILE or total + info.st_size > max_total:
                    skipped.append(
                        {
                            "path": str(relative),
                            "reason": "byte budget; migrate this history separately",
                        }
                    )
                    limited = True
                    continue
                try:
                    _data, record = _read(entry.path)
                except (OSError, ValueError) as exc:
                    skipped.append({"path": str(relative), "reason": str(exc)[:512]})
                    continue
                record.update(path=str(relative), source=str(Path(entry.path)))
                rows.append(record)
                total += record["bytes"]
        if limited and (visits > max_files * 4 or len(rows) >= max_files):
            break
    return {
        "root": str(root),
        "files": sorted(rows, key=lambda value: value["path"]),
        "skipped": skipped[:MAX_FILES],
        "bytes": total,
        "limited": limited,
    }


def plan(
    *,
    source_config=None,
    source_state=None,
    destination_config=None,
    destination_state=None,
    include_history=False,
    env=None,
):
    if not source_config or not source_state:
        raise ValueError(
            "New migration plans require explicit --source-config and --source-state directories."
        )
    config, state, _home = _roots(dict(os.environ if env is None else env))
    source_config = Path(source_config)
    source_state = Path(source_state)
    destination_config = Path(destination_config or config / "raven/config.json")
    destination_state = Path(destination_state or state / "raven")
    sources = {
        "config": inventory(source_config, include_history=include_history),
        "state": inventory(source_state, include_history=include_history),
    }
    destination_config = Path(os.path.abspath(os.path.expanduser(destination_config)))
    destination_state = Path(os.path.abspath(os.path.expanduser(destination_state)))
    for source in sources.values():
        root = Path(source["root"])
        if (
            destination_state == root
            or root in destination_state.parents
            or destination_config == root
            or root in destination_config.parents
        ):
            raise ValueError(
                "Migration destinations must be separate from legacy sources."
            )
    value = {
        "schema": PLAN_SCHEMA,
        "created": time.time(),
        "sources": sources,
        "destination_config": str(destination_config),
        "destination_state": str(destination_state),
        "include_history": bool(include_history),
        "files": [],
        "conflicts": [],
        "converted_config": None,
        "ownership": "copy-only; no allocation, PID, socket or service adoption",
    }
    for category, source in sources.items():
        for record in source["files"]:
            value["files"].append({**record, "category": category})
    if (
        len(value["files"]) > MAX_FILES
        or sum(item["bytes"] for item in value["files"]) > MAX_TOTAL
    ):
        raise ValueError(
            "Combined config/state migration exceeds its budget; choose smaller explicit roots."
        )
    old_config = next(
        (
            Path(source_config) / name
            for name in ("config.json", "config.toml")
            if (Path(source_config) / name).is_file()
        ),
        None,
    )
    if old_config:
        source_doc = read_file(old_config)
        if source_doc.get("schema") == SCHEMA:
            validate(source_doc, partial=True)
            converted = merge(DEFAULTS, source_doc)
        elif "schema" in source_doc:
            raise ValueError("Unsupported configuration schema; expected " + SCHEMA)
        else:
            converted = merge(DEFAULTS, legacy_settings(source_doc))
        converted["storage"].update(
            state_dir=str(destination_state),
            durable_results=str(destination_state / "results"),
        )
        value["converted_config"] = converted
    if os.path.lexists(destination_config):
        value["conflicts"].append(
            "Raven config already exists; migration preserves it and archives legacy settings only."
        )
        value["converted_config"] = None
    value["ui_candidates"] = []
    for record in value["files"]:
        if (
            Path(record["path"]).name != "ui.json"
            or record["bytes"] > UI_IMPORT_BYTES
            or len(value["ui_candidates"]) >= 32
        ):
            continue
        try:
            raw, current = _read(record["source"], UI_IMPORT_BYTES)
            if current["sha256"] != record["sha256"]:
                continue
            from ..ui_state import decode

            ui = decode(raw)
            scope = (
                ui.get("forecast_state", {}).get("scope")
                if isinstance(ui.get("forecast_state"), dict)
                else None
            )
            scope = (
                scope if isinstance(scope, dict) and len(_json(scope)) <= 4096 else None
            )
            value["ui_candidates"].append(
                {
                    "category": record["category"],
                    "path": record["path"],
                    "sha256": record["sha256"],
                    "scope": scope,
                }
            )
        except (OSError, ValueError, TypeError, RecursionError):
            continue
    activation = destination_state / "migration-activation.json"
    value["activation"] = (
        str(activation)
        if value["ui_candidates"] and not os.path.lexists(activation)
        else None
    )
    if value["activation"]:
        if value["converted_config"] is not None:
            config_bytes = canonical(value["converted_config"]) + b"\n"
        elif os.path.lexists(destination_config):
            config_bytes, _identity = _read(destination_config, 256 << 10)
        else:
            config_bytes = None
        value["configuration_sha256"] = (
            hashlib.sha256(config_bytes).hexdigest()
            if config_bytes is not None
            else None
        )
    value["id"] = _digest(value)[:24]
    for record in value["files"]:
        record["destination"] = str(
            destination_state
            / "legacy-imports"
            / value["id"]
            / record["category"]
            / record["path"]
        )
    value["digest"] = _digest(value)
    return value


def _validate_plan(value):
    if not isinstance(value, dict) or value.get("schema") != PLAN_SCHEMA:
        raise ValueError("Expected " + PLAN_SCHEMA)
    unsigned = {key: item for key, item in value.items() if key != "digest"}
    if value.get("digest") != _digest(unsigned):
        raise ValueError("The migration plan changed; create a new plan.")
    if (
        not isinstance(value.get("id"), str)
        or len(value["id"]) != 24
        or not all(c in "0123456789abcdef" for c in value["id"])
    ):
        raise ValueError("Invalid migration identity.")
    if not isinstance(value.get("files"), list) or len(value["files"]) > MAX_FILES:
        raise ValueError("Migration file limit exceeded.")
    state = Path(value["destination_state"])
    if not state.is_absolute() or not Path(value["destination_config"]).is_absolute():
        raise ValueError("Migration destinations must be absolute.")
    if value.get("activation") not in (None, str(state / "migration-activation.json")):
        raise ValueError("Invalid migration activation destination.")
    for record in value["files"]:
        relative = Path(record["path"])
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or record["category"] not in ("config", "state")
        ):
            raise ValueError("Migration contains an invalid relative source.")
        expected = (
            state / "legacy-imports" / value["id"] / record["category"] / relative
        )
        source = Path(value["sources"][record["category"]]["root"]) / relative
        if record["destination"] != str(expected) or record["source"] != str(source):
            raise ValueError("Migration destination/source identity changed.")
    candidates = value.get("ui_candidates", [])
    if not isinstance(candidates, list) or len(candidates) > 32:
        raise ValueError("Migration UI candidate limit exceeded.")
    allowed_ui = {
        (item["category"], item["path"], item["sha256"])
        for item in value["files"]
        if Path(item["path"]).name == "ui.json" and item["bytes"] <= UI_IMPORT_BYTES
    }
    for candidate in candidates:
        if (
            candidate["category"],
            candidate["path"],
            candidate["sha256"],
        ) not in allowed_ui:
            raise ValueError(
                "Migration UI candidate is not a bounded inventoried source."
            )
    return value


def _activation_bytes(value):
    receipt = {
        "schema": ACTIVATION_SCHEMA,
        "id": value["id"],
        "plan_digest": value["digest"],
        "journal": str(
            Path(value["destination_state"]) / "migrations" / (value["id"] + ".json")
        ),
        "configuration": value["destination_config"],
        "configuration_sha256": value.get("configuration_sha256"),
        "candidates": [],
    }
    for candidate in value.get("ui_candidates", []):
        destination = str(
            Path(value["destination_state"])
            / "legacy-imports"
            / value["id"]
            / candidate["category"]
            / candidate["path"]
        )
        receipt["candidates"].append(
            {
                "path": destination,
                "sha256": candidate["sha256"],
                "scope": candidate["scope"],
            }
        )
    return _json(receipt)


@contextlib.contextmanager
def _lock(directory):
    private_directory(directory)
    path = Path(directory) / ".migration.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise ValueError("Migration lock must be an owned private regular file.")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError(
                "Another migration is active; wait before retrying."
            ) from exc
        yield
    finally:
        os.close(fd)


def _journal_write(path, value):
    data = _json(value)
    if not path.exists():
        write_new(path, data)
        return
    _previous, info = _read(path, MAX_PLAN)
    if info["mode"] & 0o077:
        raise ValueError("Migration journal permissions changed.")
    temporary = path.parent / ("." + path.name + "." + uuid.uuid4().hex)
    write_new(temporary, data)
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _journal_read(path):
    data, _info = _read(path, MAX_PLAN)
    value = json.loads(data)
    if not isinstance(value, dict) or value.get("schema") != JOURNAL_SCHEMA:
        raise ValueError("Invalid migration journal.")
    _validate_plan(value.get("plan"))
    expected = (
        Path(value["plan"]["destination_state"])
        / "migrations"
        / (value["plan"]["id"] + ".json")
    )
    if Path(path).absolute() != expected:
        raise ValueError(
            "Migration journal location does not match its ownership plan."
        )
    return value


def _cleanup_staging(pending):
    """Remove only the inode journaled before publishing this exact output."""
    if not pending or not pending.get("staging"):
        return
    stage, destination = Path(pending["staging"]), Path(pending["path"])
    if stage.parent != destination.parent or not stage.name.startswith(
        "." + destination.name + ".raven-"
    ):
        raise ValueError("Migration staging identity is invalid.")
    if os.path.lexists(stage):
        _raw, current = _read(stage)
        if any(current[key] != pending[key] for key in ("device", "inode", "sha256")):
            raise ValueError(
                "Migration staging file changed; reconcile before retrying."
            )
        stage.unlink()


def apply(value):
    value = _validate_plan(value)
    destination = Path(value["destination_state"])
    journal_path = destination / "migrations" / (value["id"] + ".json")
    with _lock(destination / "migrations"):
        if journal_path.exists():
            journal = _journal_read(journal_path)
            if (
                journal["plan"]["digest"] != value["digest"]
                or journal.get("state") == "rolled-back"
            ):
                raise ValueError(
                    "This migration was changed or rolled back; create a new plan."
                )
        else:
            # Check every source before creating any imported file.
            for record in value["files"]:
                _data, current = _read(record["source"])
                if any(
                    current[key] != record[key]
                    for key in ("device", "inode", "bytes", "mtime_ns", "sha256")
                ):
                    raise ValueError(
                        "Legacy source changed; stop active writers and create a new migration plan."
                    )
            journal = {
                "schema": JOURNAL_SCHEMA,
                "plan": value,
                "state": "applying",
                "created": time.time(),
                "outputs": [],
            }
            _journal_write(journal_path, journal)
        expected_outputs = [
            (record["destination"], record["sha256"], record)
            for record in value["files"]
        ]
        if value["converted_config"] is not None:
            raw = canonical(value["converted_config"]) + b"\n"
            expected_outputs.append(
                (value["destination_config"], hashlib.sha256(raw).hexdigest(), None)
            )
        if value.get("activation"):
            expected_outputs.append(
                (
                    value["activation"],
                    hashlib.sha256(_activation_bytes(value)).hexdigest(),
                    "activation",
                )
            )
        completed = {item["path"]: item for item in journal["outputs"]}
        pending = journal.get("pending")
        if pending:
            _cleanup_staging(pending)
        for path, expected_hash, record in expected_outputs:
            if record == "activation":
                raw = _activation_bytes(value)
            elif record is None:
                raw = canonical(value["converted_config"]) + b"\n"
            else:
                raw, current = _read(record["source"])
                if any(
                    current[key] != record[key]
                    for key in ("device", "inode", "bytes", "mtime_ns", "sha256")
                ):
                    raise ValueError(
                        "Legacy source changed during migration; original was preserved. Reconcile or rollback this journal."
                    )
            if os.path.lexists(path):
                _existing, info = _read(path)
                if info["sha256"] != expected_hash:
                    raise ValueError(
                        "A migration destination changed; nothing was overwritten."
                    )
                owned = completed.get(path) or (
                    pending if pending and pending["path"] == path else None
                )
                if (
                    not owned
                    or info["mode"] & 0o077
                    or any(
                        info[key] != owned[key] for key in ("device", "inode", "sha256")
                    )
                ):
                    raise ValueError(
                        "Existing destination is not proven to belong to this interrupted migration."
                    )
            else:
                if path in completed:
                    raise ValueError(
                        "A completed migration destination disappeared; reconcile or rollback this journal."
                    )

                def before_publish(identity):
                    journal["pending"] = identity
                    _journal_write(journal_path, journal)

                write_new(path, raw, before_publish=before_publish)
            if path not in completed:
                _data, info = _read(path)
                item = {
                    "path": path,
                    "sha256": expected_hash,
                    "device": info["device"],
                    "inode": info["inode"],
                }
                journal["outputs"].append(item)
                journal.pop("pending", None)
                completed[path] = item
                _journal_write(journal_path, journal)
        journal["state"] = "complete"
        journal["completed"] = time.time()
        _journal_write(journal_path, journal)
    return {
        "schema": "raven.migration-result/v1",
        "state": "complete",
        "journal": str(journal_path),
        "files": len(journal["outputs"]),
        "originals": "unchanged",
        "historical_schemas": "preserved",
    }


def rollback(path):
    path = Path(path).absolute()
    value = _journal_read(path)
    plan_value = value["plan"]
    allowed = {
        record["destination"]: record["sha256"] for record in plan_value["files"]
    }
    if plan_value["converted_config"] is not None:
        allowed[plan_value["destination_config"]] = hashlib.sha256(
            canonical(plan_value["converted_config"]) + b"\n"
        ).hexdigest()
    if plan_value.get("activation"):
        allowed[plan_value["activation"]] = hashlib.sha256(
            _activation_bytes(plan_value)
        ).hexdigest()
    with _lock(path.parent):
        value = _journal_read(path)
        if value["state"] == "rolled-back":
            return {
                "schema": "raven.migration-result/v1",
                "state": "rolled-back",
                "removed": 0,
                "originals": "unchanged",
            }
        outputs = list(value["outputs"])
        if value.get("pending") and value["pending"]["path"] not in {
            item["path"] for item in outputs
        }:
            outputs.append(value["pending"])
        for output in outputs:
            if allowed.get(output["path"]) != output["sha256"]:
                raise ValueError("Journal output is not authorized by its plan.")
            if os.path.lexists(output["path"]):
                _data, current = _read(output["path"])
                if any(
                    current[key] != output[key] for key in ("device", "inode", "sha256")
                ):
                    raise ValueError(
                        "Migrated output changed or is in use; rollback refuses to remove it."
                    )
        removed = 0
        _cleanup_staging(value.get("pending"))
        for output in reversed(outputs):
            if os.path.lexists(output["path"]):
                os.unlink(output["path"])
                removed += 1
        value.update(state="rolled-back", rolled_back=time.time())
        value.pop("pending", None)
        _journal_write(path, value)
    return {
        "schema": "raven.migration-result/v1",
        "state": "rolled-back",
        "removed": removed,
        "originals": "unchanged",
        "journal": str(path),
    }


def import_ui(state_dir, current_scope):
    """Read a confirmed migration's safe preferences; never adopt live state.

    The caller uses this only when new scoped UI state is absent. Job group
    identities require an exact type-sensitive old connection/owner scope.
    """
    status = {"state": "missing", "message": "", "groups_imported": False}
    receipt_path = Path(state_dir) / "migration-activation.json"
    try:
        raw, identity = _read(receipt_path, UI_IMPORT_BYTES)
        receipt = json.loads(raw)
        if receipt.get("schema") != ACTIVATION_SCHEMA:
            raise ValueError("Unsupported migration activation schema.")
        journal_path = Path(state_dir) / "migrations" / (receipt["id"] + ".json")
        if receipt.get("journal") != str(journal_path):
            raise ValueError("Migration activation journal identity differs.")
        journal = _journal_read(journal_path)
        if journal.get("state") != "complete" or journal["plan"][
            "digest"
        ] != receipt.get("plan_digest"):
            raise ValueError("Migration is incomplete or rolled back.")
        if _activation_bytes(journal["plan"]) != raw:
            raise ValueError("Migration activation differs from its reviewed plan.")
        owned = next(
            (
                entry
                for entry in journal["outputs"]
                if entry["path"] == str(receipt_path)
            ),
            None,
        )
        if not owned or any(
            owned[key] != identity[key] for key in ("device", "inode", "sha256")
        ):
            raise ValueError("Migration activation ownership changed.")
        config_matches = False
        if receipt.get("configuration_sha256"):
            _config_raw, config_info = _read(receipt["configuration"], 256 << 10)
            config_matches = config_info["sha256"] == receipt["configuration_sha256"]

        def matches(candidate):
            saved = candidate.get("scope")
            return (
                config_matches
                and isinstance(current_scope, dict)
                and isinstance(saved, dict)
                and saved == current_scope
                and all(
                    type(saved[key]) is type(item)
                    for key, item in current_scope.items()
                )
            )

        candidates = receipt.get("candidates", [])[:32]
        candidate = next(
            (entry for entry in candidates if matches(entry)),
            candidates[0] if candidates else None,
        )
        if candidate is None:
            return {}, status
        raw, info = _read(candidate["path"], UI_IMPORT_BYTES)
        if info["sha256"] != candidate["sha256"]:
            raise ValueError("Archived UI preferences changed after migration.")
        from ..ui_state import decode

        source = decode(raw)
        result = {
            key: source[key]
            for key in (
                "theme",
                "tab",
                "sort",
                "reverse",
                "analytics_view",
                "nodes_view",
                "research_view",
                "log_wrap",
            )
            if key in source
        }
        workbench = source.get("workbench", {})
        table = workbench.get("table_ui", {}) if isinstance(workbench, dict) else {}
        if isinstance(table, dict):
            safe_table = {
                key: table[key]
                for key in (
                    "hidden",
                    "order",
                    "widths",
                    "sorts",
                    "column_layout_version",
                    "auto_groups",
                )
                if key in table
            }
            if matches(candidate):
                safe_table.update(
                    {
                        key: table[key]
                        for key in ("groups", "collapsed", "manual_groups")
                        if key in table
                    }
                )
                status["groups_imported"] = "manual_groups" in safe_table
            result["workbench"] = {"table_ui": safe_table}
        status.update(
            state="migrated",
            message="Read-only migrated preferences; group identities restored for the exact connection."
            if status["groups_imported"]
            else "Read-only migrated appearance; groups remain archived because matching connection authority was not established.",
        )
        return result, status
    except FileNotFoundError:
        return {}, status
    except (OSError, ValueError, KeyError, TypeError, RecursionError) as exc:
        return {}, {**status, "state": "unavailable", "message": str(exc)[:300]}


def cli(argv=None):
    parser = argparse.ArgumentParser(prog="ravenl migrate")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--plan", action="store_true")
    action.add_argument("--apply", action="store_true")
    action.add_argument("--rollback", metavar="JOURNAL")
    parser.add_argument(
        "--plan-file", help="Read an existing reviewed plan for --apply"
    )
    parser.add_argument(
        "--save-plan", help="Write a new private plan without overwrite"
    )
    parser.add_argument(
        "--source-config",
        help="Explicit previous configuration directory for a new plan",
    )
    parser.add_argument(
        "--source-state", help="Explicit previous state directory for a new plan"
    )
    parser.add_argument("--destination-config")
    parser.add_argument("--destination-state")
    parser.add_argument("--include-history", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.rollback:
            result = rollback(args.rollback)
        else:
            if args.plan_file:
                raw, _ = _read(args.plan_file, MAX_PLAN)
                value = _validate_plan(json.loads(raw))
            else:
                value = plan(
                    source_config=args.source_config,
                    source_state=args.source_state,
                    destination_config=args.destination_config,
                    destination_state=args.destination_state,
                    include_history=args.include_history,
                )
            if args.save_plan:
                write_new(args.save_plan, _json(value))
            result = apply(value) if args.apply else value
        print(json.dumps(result, indent=2, ensure_ascii=True, allow_nan=False))
        return 0
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print("Raven migration: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(cli())
