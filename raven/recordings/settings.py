"""Recording preferences and migration actions on Setup's existing worker lane."""

from __future__ import annotations

import copy
from pathlib import Path

from raven.recording_settings_port import SettingsCallbacks


def save_format(request):
    from ..configuration import policy as C
    from ..configuration.allocation import _read_snapshot, _decode, _atomic_write

    value = request["format"]
    if value not in ("jsonl", "blocks-v1"):
        raise ValueError("Choose jsonl or blocks-v1")
    path = Path(request["path"])
    raw, token = _read_snapshot(path)
    document = _decode(raw, path)
    profile = request["profile"]
    target = document.get("profiles", {}).get(profile)
    existing = C.merge(
        document.get("storage", {}), (target or {}).get("storage", {})
    ).get("series_format", "jsonl")
    if existing != request["expected"]:
        raise ValueError(
            "Recording format changed in the file; reopen settings before saving"
        )
    if target is None:
        target = (
            document
            if profile in ("default-slurm", "slurm")
            or document.get("profile") == profile
            else document.setdefault("profiles", {}).setdefault(profile, {})
        )
    target.setdefault("storage", {})["series_format"] = value
    C.validate(document, partial=True)
    _, warnings = _atomic_write(path, document, token)
    return {"format": value, "path": str(path), "warnings": warnings}


def command(app, parts, *, ui=None):
    U = ui if ui is not None else getattr(app, "recording_settings_port", None)
    if not isinstance(U, SettingsCallbacks):
        raise ValueError(
            "Recording settings require the application's Setup callback port"
        )
    state = U.state(app)
    if parts[0] == "storage-format" and len(parts) == 2:
        resolved = getattr(app, "raven_resolved", None) or getattr(
            app.cfg, "raven_resolved", None
        )
        if resolved is None or resolved.legacy_source:
            U.notify(
                app,
                "Migrate to a managed configuration before saving the recording format.",
                error=True,
            )
            return True
        expected = (
            app.cfg.get("raven", {}).get("storage", {}).get("series_format", "jsonl")
        )
        request = {
            "format": parts[1],
            "expected": expected,
            "profile": resolved.data["profile"],
            "path": resolved.source,
        }
        if not request["path"]:
            from raven.telemetry_settings import initialize

            request["path"] = initialize(app)["config_path"]
        scope = U.scope(app)

        def completed(result):
            if isinstance(result, Exception):
                U.notify(
                    app, "Recording settings were not saved: " + str(result), error=True
                )
                return
            if U.scope(app) != scope:
                U.notify(
                    app,
                    "Saved the original profile. Reopen its recording settings after switching connections.",
                )
                return
            raw = app.cfg.get("raven", {})
            raw.setdefault("storage", {})["series_format"] = result["format"]
            app.store.series_storage = result["format"]
            U.notify(
                app,
                "Saved "
                + result["format"]
                + ". Existing compact files remain canonical; JSONL originals are preserved. "
                + " ".join(result["warnings"]),
            )

        U.submit(
            app, "Saving recording format", lambda: save_format(request), completed
        )
        return True
    if (
        parts[0] in ("storage-retain-preview", "storage-retain", "storage-tier-query")
        and len(parts) == 1
    ):
        store = app.store
        jid = getattr(app, "selected_id", None)
        if not jid:
            U.notify(
                app, "Select an exact job before inspecting retention.", error=True
            )
            return True
        with store.lock:
            identity = store.capture_series_identity(jid)
            path = store._series_identity_path(identity)
        if path is None or not identity.verified:
            U.notify(
                app,
                "Retention requires verified attempt-scoped persisted recordings.",
                error=True,
            )
            return True
        from raven.recordings.storage import read_path
        from raven.recordings.retention import target_path
        from raven.recordings.maintenance import run as maintenance

        settings = copy.deepcopy(
            app.cfg.get("raven", {}).get("storage", {}).get("retention", {})
        )
        apply = parts[0] == "storage-retain"
        scope = U.scope(app)

        def work():
            if apply and not store.writable:
                raise ValueError("This session does not own mutable state")
            with store._persist_io_lock:
                source = read_path(path)
                if parts[0] == "storage-tier-query":
                    return maintenance(
                        store, {"action": "query", "source": target_path(source)}
                    )
                limits = store.raven_limits
                if limits is not None:
                    with limits.disk_lock:
                        limits._inventory()
                        target = target_path(source)
                        old_size = limits._known_size(target)
                        quota = max(
                            0,
                            limits.values["storage_bytes"]
                            - limits.disk_bytes
                            + (old_size or 0),
                        )
                        if (
                            old_size is None
                            and limits.disk_count >= limits.values["storage_files"]
                        ):
                            quota = 0
                        result = maintenance(
                            store,
                            {
                                "action": "retention",
                                "source": source,
                                "settings": settings,
                                "apply": apply,
                                "quota": quota,
                            },
                        )
                        if apply:
                            limits.disk = None
                            limits.disk_bytes = 0
                            limits._inventory()
                else:
                    result = maintenance(
                        store,
                        {
                            "action": "retention",
                            "source": source,
                            "settings": settings,
                            "apply": apply,
                        },
                    )
                return result

        def completed(result):
            if isinstance(result, Exception):
                U.notify(app, "Retention unavailable: " + str(result), error=True)
                return
            if (
                U.scope(app) != scope
                or getattr(app, "selected_id", None) != jid
                or store.capture_series_identity(jid) != identity
            ):
                U.notify(
                    app,
                    "Completed retention for the original job "
                    + jid
                    + "; reopen its storage page to inspect results.",
                )
                return
            if parts[0] == "storage-tier-query":
                lines = [
                    "Retained tier query: " + result["message"],
                    "Source: " + result["source"]["path"],
                    "Observed gaps: " + str(len(result["observed_gaps"])),
                ]
                for row in result["records"]:
                    if row["fidelity"] == "raw":
                        lines.append(
                            "RAW t="
                            + str(row["sample"]["t"])
                            + "; source row "
                            + str(row["source_row"])
                        )
                    else:
                        lines.append(
                            "ROLLUP "
                            + str(row["tier_seconds"])
                            + "s ["
                            + str(row["start"])
                            + ", "
                            + str(row["end"])
                            + ") count="
                            + str(row["count"])
                        )
                        for name, metric in list(row["metrics"].items())[:16]:
                            lines.append(
                                name[:80]
                                + ": min="
                                + str(metric["min"])[:80]
                                + ", max="
                                + str(metric["max"])[:80]
                                + ", observed="
                                + str(metric["count"])
                                + ", unit="
                                + str(metric["unit"] or "unknown")
                            )
                        if len(row["metrics"]) > 16:
                            lines.append(
                                "Additional metrics available through raven series query."
                            )
                if result["partial"]:
                    lines.append(
                        "UI result limit reached; use raven series query with an explicit larger limit."
                    )
                state["retention_lines"] = lines
                U.notify(
                    app,
                    "Tier results are below. Original raw data remains authoritative.",
                )
            else:
                state["retention_result"] = copy.deepcopy(result)
                U.notify(
                    app,
                    (
                        "Working archive published. "
                        if apply
                        else "Retention preview only. "
                    )
                    + str(result["raw_records"])
                    + " raw records; "
                    + str(result["rollup_records"])
                    + " rollups; "
                    + str(result["expired_from_working_copy"])
                    + " expired from working copy. Original raw source preserved.",
                )

        U.submit(
            app,
            "Inspecting retention"
            if not apply
            else "Publishing retention working copy",
            work,
            completed,
        )
        return True
    if parts[0] in ("storage-preview", "storage-convert") and len(parts) == 1:
        store = app.store
        jid = getattr(app, "selected_id", None)
        if not jid:
            U.notify(
                app, "Select an exact job before previewing its recordings.", error=True
            )
            return True
        with store.lock:
            identity = store.capture_series_identity(jid)
            path = store._series_identity_path(identity)
        if path is None:
            U.notify(app, "Persistence is disabled for this session.", error=True)
            return True
        from raven.recordings.storage import json_path
        from raven.recordings.maintenance import run as maintenance

        path = json_path(path)
        apply = parts[0] == "storage-convert"
        scope = U.scope(app)

        def work():
            if apply and not store.writable:
                raise ValueError("This session does not own mutable state")
            with store._persist_io_lock:
                limits = store.raven_limits
                if limits is not None:
                    with limits.disk_lock:
                        limits._inventory()
                        quota = max(
                            0, limits.values["storage_bytes"] - limits.disk_bytes
                        )
                        if limits.disk_count >= limits.values["storage_files"]:
                            quota = 0
                        result = maintenance(
                            store,
                            {
                                "action": "migrate",
                                "source": path,
                                "apply": apply,
                                "quota": quota,
                            },
                        )
                        if apply:
                            limits.disk = None
                            limits.disk_bytes = 0
                            limits._inventory()
                else:
                    result = maintenance(
                        store, {"action": "migrate", "source": path, "apply": apply}
                    )
                if apply:
                    store._series_format_cache.clear()
            return result

        def completed(result):
            if isinstance(result, Exception):
                U.notify(
                    app, "Recording conversion unavailable: " + str(result), error=True
                )
                return
            if (
                U.scope(app) != scope
                or getattr(app, "selected_id", None) != jid
                or store.capture_series_identity(jid) != identity
            ):
                U.notify(
                    app,
                    "Completed conversion for the original job "
                    + jid
                    + "; reopen its storage page to inspect results.",
                )
                return
            state["storage_result"] = copy.deepcopy(result)
            if apply:
                with store.lock:
                    if store.capture_series_identity(jid) == identity:
                        store._series_loaded.discard(jid)
            U.notify(
                app,
                (
                    "Verified compact copy published; source preserved. "
                    if apply
                    else "Preview only; no files changed. "
                )
                + str(result["raw_samples"])
                + " raw samples; "
                + str(result["source_bytes"])
                + " → "
                + str(result["compact_bytes"])
                + " bytes.",
            )

        U.submit(
            app,
            "Converting recordings" if apply else "Previewing recordings",
            work,
            completed,
        )
        return True
    return False
