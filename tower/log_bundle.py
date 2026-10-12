"""Complete, cancellable History log bundles and a confined local destination picker.

Only ``capture_jobs`` is suitable for the input thread. All other public methods
perform I/O and must run on the existing ResearchHub worker. Sources stay on the
selected file adapter; destinations are always local, descriptor-anchored paths.
Raw bytes are preserved. Clipboard delivery uses the existing strict UTF-8,
complete-file transport and never substitutes a visible tail or a short prefix.
"""
from __future__ import annotations

from contextlib import contextmanager
import copy
import datetime
import hashlib
import json
import os
import re
import stat
import uuid

from . import clipboard as clipboard_io, projects
from .log_catalog import build_catalog
from .log_copy import CHUNK_BYTES, _snapshot, _local_fd_snapshot, _check_snapshot
from .remote import LocalFiles

MAX_JOBS = 1024
MAX_DIRECTORY_ENTRIES = 4096
MAX_VISIBLE_DIRECTORIES = 512
_JOB = re.compile(r"[0-9]+(?:_[0-9]+)?(?:\+[0-9]+)?\Z")
_LOG_SUFFIX = re.compile(r"\.(?:out|err|log|stdout|stderr)(?:\.[A-Za-z0-9_-]+)*$", re.I)
_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _clean(value, limit=4096):
    return "".join(char if char.isprintable() else " " for char in str(value))[:limit]


def _cancel(cancel):
    if cancel is not None and (cancel() if callable(cancel) else cancel.is_set()):
        raise InterruptedError("Log export cancelled")


def _path(value):
    if not isinstance(value, str) or not value or len(value) > 4096 or not value.isprintable():
        raise ValueError("Choose an exact printable directory path of at most 4096 characters")
    return os.path.abspath(os.path.expanduser(value))


@contextmanager
def _absolute_directory(path):
    """Open every absolute parent without following symlinks, including root."""
    path = _path(path)
    fd = os.open(os.path.sep, _DIRECTORY_FLAGS)
    try:
        for part in path.split(os.path.sep):
            if not part:
                continue
            next_fd = os.open(part, _DIRECTORY_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        yield path, fd
    finally:
        os.close(fd)


@contextmanager
def _private_directory(path, cancel=None):
    """Create missing staging parents only through pinned, no-symlink descriptors.

    ``makedirs`` would follow a state-root or ancestor symlink before the final
    no-follow open could reject it. Checking and creating each component through
    its already validated parent prevents even rejected exports from writing
    inside such a target. Newly created folders are private.
    """
    path = _path(path)
    fd = os.open(os.path.sep, _DIRECTORY_FLAGS)
    try:
        for part in path.split(os.path.sep):
            if not part:
                continue
            _cancel(cancel)
            try:
                next_fd = os.open(part, _DIRECTORY_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                _cancel(cancel)
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    # A concurrent creator still must pass the no-follow open.
                    pass
                next_fd = os.open(part, _DIRECTORY_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        _cancel(cancel)
        yield path, fd
    finally:
        os.close(fd)


@contextmanager
def _destination(root, current=None):
    root = _path(root)
    current = _path(current or root)
    if os.path.commonpath((root, current)) != root:
        raise ValueError("Destination must stay inside the configured projects folder")
    with _absolute_directory(root) as (_, root_fd):
        fd = os.dup(root_fd)
        try:
            relative = os.path.relpath(current, root)
            if relative != ".":
                for part in relative.split(os.path.sep):
                    next_fd = os.open(part, _DIRECTORY_FLAGS, dir_fd=fd)
                    os.close(fd)
                    fd = next_fd
            yield root, current, fd
        finally:
            os.close(fd)


def capture_jobs(job_ids, snap, *, registered_root="", binding=None,
                 project_logs=(), log_settings=None):
    """Capture exact selected records and cached details without filesystem work.

    The limit rejects the whole request; it never silently omits selected jobs.
    A selected project binding applies only to its exact declared job identity.
    """
    ids = list(dict.fromkeys(str(value) for value in job_ids))
    if not ids or len(ids) > MAX_JOBS or any(not _JOB.fullmatch(value) for value in ids):
        raise ValueError(f"Select 1 to {MAX_JOBS} exact scheduler job IDs before exporting logs")
    records = {record.id: record for record in list(snap.get("jobs", ())) + list(snap.get("finished", ()))
               + list(snap.get("departed_jobs", {}).values())}
    jobs = []
    for jid in ids:
        record = records.get(jid)
        if record is None:
            from .model import Finished
            record = Finished(jid, state="UNKNOWN")
        item = {"job_id": jid, "record": copy.copy(record),
                "details": copy.deepcopy(snap.get("details", {}).get(jid, {}))}
        if isinstance(binding, dict) and binding.get("job_id") == jid:
            item["binding"] = copy.deepcopy(binding)
            item["project_logs"] = copy.deepcopy(list(project_logs))
        jobs.append(item)
    return {"jobs": jobs, "registered_root": registered_root,
            "log_settings": copy.deepcopy(log_settings or {})}


def _missing(job_id, label, path, message):
    return {"job_id": job_id, "label": _clean(label, 160), "path": _clean(path),
            "message": _clean(message)}


def _known(details):
    return any(isinstance(details.get(key), str) and details[key]
               and details[key].lower() not in ("(null)", "n/a", "unknown", "none")
               for key in ("StdOut", "StdErr"))


def _both_known(details):
    return all(isinstance(details.get(key), str) and details[key]
               and details[key].lower() not in ("(null)", "n/a", "unknown", "none")
               for key in ("StdOut", "StdErr"))


def _job_details(item, slurm, warnings):
    jid, record = item["job_id"], item["record"]
    details = dict(item.get("details") or {})
    if details.get("JobId", jid) != jid:
        details = {}
        warnings.append(f"Job {jid}: cached metadata named a different job and was discarded")
    if not _both_known(details) and slurm is not None:
        # Finished jobs can be purged from scontrol. Preserve exact accounting
        # resolution, including raw array IDs, rather than borrowing a cursor.
        for method in ("details", "historical_details"):
            fn = getattr(slurm, method, None)
            if fn is None:
                continue
            try:
                value = fn(jid)
                if not isinstance(value, dict) or value.get("JobId", jid) != jid:
                    raise ValueError("scheduler response named a different job")
                details.update(value)
                if _both_known(details):
                    break
            except Exception as exc:
                warnings.append(f"Job {jid}: {method}: {_clean(exc)}")
    details.setdefault("JobId", jid)
    details.setdefault("JobName", getattr(record, "name", ""))
    if not details.get("WorkDir") and getattr(record, "workdir", ""):
        details["WorkDir"] = record.workdir
    return details


def _project_logs(item, details, request, files, inventories, warnings):
    """Inventory scans are reused across selected jobs; only exact matches bind."""
    jid = item["job_id"]
    binding = item.get("binding")
    if binding:
        if type(files) is not LocalFiles:
            warnings.append(f"Job {jid}: project inventory discovery is unavailable on this file backend")
            return None
        selected = projects.select_run(binding["project_root"], binding["run_id"], files=files)
        current = selected["binding"]
        if current.get("job_id") != jid or current.get("attempt") != binding.get("attempt"):
            raise ValueError("project run job/attempt changed; select its current record and retry")
        return selected
    if type(files) is not LocalFiles:
        return None
    roots = projects.job_project_roots(details.get("WorkDir", ""), request.get("registered_root", ""))
    matches, incomplete = [], False
    for root in roots:
        if root not in inventories:
            try:
                inventories[root] = projects.discover_project(root, files=files)
            except FileNotFoundError:
                inventories[root] = None
            except (OSError, ValueError) as exc:
                inventories[root] = {"runs": [], "limited": True, "warnings": [_clean(exc)]}
        found = inventories[root]
        if not found:
            continue
        if found.get("limited") or found.get("warnings"):
            incomplete = True
            warnings.append(f"Job {jid}: project inventory discovery was incomplete at {root}")
        matches.extend((root, run["run_id"]) for run in found.get("runs", ()) if run.get("job_id") == jid)
    if incomplete or len(matches) > 1:
        if len(matches) > 1:
            warnings.append(f"Job {jid}: multiple project attempts declare this ID; automatic binding refused")
        return None
    if len(matches) != 1:
        return None
    selected = projects.select_run(*matches[0], files=files)
    if selected["binding"].get("job_id") != jid:
        raise ValueError("project inventory changed scheduler identity during discovery")
    return selected


def discover_logs(request, *, slurm=None, files=None, cancel=None, progress=None):
    """Discover scheduler streams, exact job directory logs and project declarations.

    Metadata, missing files and all bounded-discovery warnings are explicit.
    Content is not read here. Missing declared streams survive in ``missing``.
    """
    from .views import stdout_path
    files = files or LocalFiles()
    report = {"status": "ready", "jobs": [], "entries": [], "missing": [],
              "warnings": [], "total_bytes": 0, "file_count": 0}
    inventories, identities = {}, {}
    try:
        jobs = request.get("jobs", ())
        if not 1 <= len(jobs) <= MAX_JOBS:
            raise ValueError(f"Select 1 to {MAX_JOBS} jobs")
        if getattr(files, "remote", False):
            report["warnings"].append("Remote scheduler and manifest logs are supported; automatic local project-inventory discovery is unavailable. Exports are saved on the machine running Tower.")
        for index, item in enumerate(jobs):
            _cancel(cancel)
            jid = item["job_id"]
            if not isinstance(jid, str) or not _JOB.fullmatch(jid):
                raise ValueError("Invalid selected scheduler job ID")
            job = {"job_id": jid, "entries": [], "missing": [], "messages": []}
            report["jobs"].append(job)
            try:
                details = _job_details(item, slurm, job["messages"])
                _cancel(cancel)
                selected = _project_logs(item, details, request, files, inventories, job["messages"])
                binding = selected["binding"] if selected else None
                stdout = stdout_path(item["record"], details, files, "StdOut", probe=True)
                stderr = stdout_path(item["record"], details, files, "StdErr", probe=True)
                manifest = request.get("log_settings", {}).get("manifest_file", "")
                if manifest:
                    manifest = str(manifest).replace("{job_id}", jid)
                    if not os.path.isabs(manifest):
                        base = details.get("WorkDir", "")
                        if not base or not os.path.isabs(base):
                            job["messages"].append("Registered log manifest requires the exact selected job's workdir")
                            manifest = ""
                        else:
                            manifest = os.path.join(base, manifest)
                # A project manifest was validated against this run; do not
                # re-read it through the broader unconfined generic catalog.
                catalog = build_catalog(jid, stdout, stderr, manifest_file="" if selected else manifest, files=files)
                job["messages"].extend(catalog.get("messages", ()))
                entries = list(catalog["entries"])
                if selected:
                    entries = list(selected["logs"]) + entries
                    job["messages"].extend(selected.get("warnings", ()))
                    for warning in selected.get("warnings", ()):
                        # Refused declared outputs remain expected, even when
                        # the safe project picker cannot admit their path.
                        if warning.startswith(("stdout: binding refused", "stderr: binding refused", "log_index:")) or (warning.startswith("Log ") and "unsafe declaration refused" in warning):
                            label = warning.split(":", 1)[0].removeprefix("Log ")
                            job["missing"].append(_missing(jid, label, "", warning))
                seen = set()
                for entry in entries:
                    _cancel(cancel)
                    # Generic directory discovery can include checkpoints and
                    # scripts bearing the job ID. Only identifiable log suffixes
                    # are automatic; explicit scheduler/manifest paths stay exact.
                    if entry.get("source") == "discovered" and not _LOG_SUFFIX.search(entry["path"]):
                        continue
                    path = entry["path"]
                    key = path if getattr(files, "remote", False) else os.path.abspath(path)
                    if key in seen:
                        continue
                    seen.add(key)
                    value = dict(entry, job_id=jid)
                    if entry.get("source") == "project":
                        value["binding"] = dict(binding)
                    try:
                        value["snapshot"] = _snapshot(files, path)
                        identity = ("remote" if getattr(files, "remote", False) else "local",
                                    repr(value["snapshot"]["ident"]))
                        existing = identities.get(identity)
                        if existing is None:
                            value["job_ids"] = [jid]
                            value["aliases"] = [{"job_id": jid, "path": path, "label": value.get("label", "log")}]
                            value["associations"] = [{"job_id": jid, "path": path,
                                                       "snapshot": value["snapshot"], "binding": value.get("binding")}]
                            identities[identity] = value
                            report["entries"].append(value)
                            report["total_bytes"] += value["snapshot"]["size"]
                        else:
                            if jid not in existing["job_ids"]:
                                existing["job_ids"].append(jid)
                            existing["aliases"].append({"job_id": jid, "path": path, "label": value.get("label", "log")})
                            existing["associations"].append({"job_id": jid, "path": path,
                                                             "snapshot": value["snapshot"], "binding": value.get("binding")})
                        job["entries"].append(value)
                    except (OSError, ValueError) as exc:
                        job["missing"].append(_missing(jid, value.get("label", "log"), path, exc))
                if not job["entries"] and not job["missing"]:
                    job["missing"].append(_missing(jid, "stdout/stderr", "", details.get("LogPathError") or "No scheduler or project log paths were reported for this exact job"))
            except InterruptedError:
                raise
            except Exception as exc:
                job["missing"].append(_missing(jid, "log discovery", "", exc))
            report["missing"].extend(job["missing"])
            report["warnings"].extend(f"Job {jid}: {_clean(value)}" for value in job["messages"])
            if progress:
                progress(index + 1, len(jobs))
        report["file_count"] = len(report["entries"])
        if report["missing"] or report["warnings"]:
            report["status"] = "partial" if report["entries"] else "error"
    except InterruptedError as exc:
        report.update(status="cancelled", message=str(exc), entries=[], total_bytes=0, file_count=0)
    except Exception as exc:
        report.update(status="error", message=_clean(exc))
    return report


def list_directories(root, current=None, *, cancel=None):
    """List a bounded directory page without admitting symlinks or special files."""
    result = {"status": "ready", "root": "", "current": "", "parent": None,
              "entries": [], "limited": False, "message": ""}
    try:
        _cancel(cancel)
        with _destination(root, current) as (base, path, fd):
            result.update(root=base, current=path, parent=os.path.dirname(path) if path != base else None)
            with os.scandir(fd) as iterator:
                for index, entry in enumerate(iterator):
                    _cancel(cancel)
                    if index >= MAX_DIRECTORY_ENTRIES:
                        result["limited"] = True
                        break
                    if not entry.name.isprintable() or not entry.is_dir(follow_symlinks=False):
                        continue
                    result["entries"].append({"name": entry.name, "path": os.path.join(path, entry.name)})
                    if len(result["entries"]) >= MAX_VISIBLE_DIRECTORIES:
                        result["limited"] = True
                        break
            result["entries"].sort(key=lambda entry: (entry["name"].casefold(), entry["name"]))
            if result["limited"]:
                result["message"] = f"Listing limited to {MAX_DIRECTORY_ENTRIES} inspected entries and {MAX_VISIBLE_DIRECTORIES} directories; use a known directory path to continue"
    except InterruptedError as exc:
        result.update(status="cancelled", message=str(exc))
    except (OSError, ValueError, TypeError) as exc:
        result.update(status="error", message=_clean(exc))
    return result


def mkdir_directory(root, current, name, *, cancel=None):
    """Create a single private child, never overwrite or follow a symlink."""
    try:
        if not isinstance(name, str) or not 1 <= len(name.encode("utf-8")) <= 255 or not name.isprintable() or name in (".", "..") or any(char in name for char in "/\\"):
            raise ValueError("Folder name must be a single printable name of 1 to 255 UTF-8 bytes")
        _cancel(cancel)
        with _destination(root, current) as (_, path, fd):
            os.mkdir(name, 0o700, dir_fd=fd)
            created = os.path.join(path, name)
        result = list_directories(root, current, cancel=cancel)
        result["created"] = created
        return result
    except InterruptedError as exc:
        return {"status": "cancelled", "message": str(exc), "entries": []}
    except (OSError, ValueError, TypeError, UnicodeError) as exc:
        return {"status": "error", "message": _clean(exc), "entries": []}


@contextmanager
def _source(entry, files):
    """Pin local sources; project declarations reject every symlink parent."""
    binding, path = entry.get("binding"), entry["path"]
    fd = parent = None
    if type(files) is not LocalFiles:
        yield None
        return
    try:
        if binding:
            selected = projects.select_run(binding["project_root"], binding["run_id"], files=files)
            current = selected["binding"]
            if current.get("job_id") != binding.get("job_id") or current.get("attempt") != binding.get("attempt") or path not in {log["path"] for log in selected["logs"]}:
                raise ValueError("Project log declaration or exact job attempt changed; rediscover logs")
            with _absolute_directory(os.path.sep) as (_, root_fd):
                fd, parent, _ = projects._open_local(root_fd, path.lstrip(os.path.sep))
        else:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("Log source must be a regular file")
        yield fd
    finally:
        if fd is not None:
            os.close(fd)
        if parent is not None:
            os.close(parent)


def _component(value, fallback="log"):
    value = str(value)
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip(".")[:100]
    return safe or fallback


def _remove_tree(parent_fd, name):
    """Descriptor-relative cancellation cleanup, including on Python 3.10."""
    fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    try:
        with os.scandir(fd) as iterator:
            for entry in iterator:
                if entry.is_dir(follow_symlinks=False):
                    _remove_tree(fd, entry.name)
                else:
                    os.unlink(entry.name, dir_fd=fd)
    finally:
        os.close(fd)
    os.rmdir(name, dir_fd=parent_fd)


def _verify_associations(entry, files, cancel):
    """Deduplication must preserve every selected job's exact source provenance."""
    for association in entry.get("associations", ()):
        _cancel(cancel)
        try:
            current = _snapshot(files, association["path"])
            before = association["snapshot"]
            _check_snapshot(before, current, before["size"])
            if association.get("binding"):
                with _source(association, files) as fd:
                    _check_snapshot(before, _local_fd_snapshot(fd), before["size"])
        except (OSError, ValueError) as exc:
            raise ValueError(f"Associated job {association['job_id']}: {_clean(exc)}") from exc


def _stream(entry, files, output, cancel, progress):
    _verify_associations(entry, files, cancel)
    with _source(entry, files) as source_fd:
        before = _local_fd_snapshot(source_fd) if source_fd is not None else _snapshot(files, entry["path"])
        initial = entry.get("snapshot")
        # A selection must never silently switch to a different file between
        # discovery and Save. Appends are valid initial-byte snapshots.
        if initial is not None:
            _check_snapshot(initial, before, initial["size"])
        last_size = before["size"]
        from .raven_execution import source_read
        source_read(before["size"])
        _check_snapshot(before, _snapshot(files, entry["path"]), last_size)
        offset, digest, utf8_valid, last = 0, hashlib.sha256(), True, b""
        import codecs
        decoder = codecs.getincrementaldecoder("utf-8")("strict")
        while offset < before["size"]:
            _cancel(cancel)
            last_size = _check_snapshot(before, _snapshot(files, entry["path"]), last_size)
            if source_fd is not None:
                last_size = _check_snapshot(before, _local_fd_snapshot(source_fd), last_size)
            count = min(CHUNK_BYTES, before["size"] - offset)
            data = os.read(source_fd, count) if source_fd is not None else files.read(entry["path"], offset, count)
            if not isinstance(data, bytes) or len(data) != count:
                raise OSError("Log returned a short or invalid read; no partial file was published")
            output.write(data)
            digest.update(data)
            if utf8_valid:
                try:
                    decoder.decode(data, final=False)
                except UnicodeError:
                    utf8_valid = False
            offset += count
            last = data[-1:]
            if progress:
                progress(offset, before["size"])
        if utf8_valid:
            try:
                decoder.decode(b"", final=True)
            except UnicodeError:
                utf8_valid = False
        after = _snapshot(files, entry["path"])
        _check_snapshot(before, after, last_size)
        if source_fd is not None:
            _check_snapshot(before, _local_fd_snapshot(source_fd), last_size)
        if entry.get("binding"):
            # Re-open after the full stream to catch parent substitutions and
            # changed run/attempt declarations, not only a replaced final file.
            with _source(entry, files) as verified:
                _check_snapshot(before, _local_fd_snapshot(verified), last_size)
        _verify_associations(entry, files, cancel)
        _cancel(cancel)
        return {"bytes": offset, "sha256": digest.hexdigest(), "utf8": utf8_valid,
                "grew": after["size"] > before["size"], "last_byte": last}


def export_logs(report, destination=None, *, root=None, state_dir=None, clipboard=False,
                files=None, cancel=None, progress=None, use_osc52=True, use_tools=True,
                tty_path="/dev/tty", copy_destination="copy", editor_context=None):
    """Publish a unique raw job/source hierarchy and manifest, then optionally copy.

    Directory copies never overwrite. Individual missing/changed logs are listed
    per job; valid files remain available and the result is explicitly partial.
    Cancellation before publication removes the whole unfinished bundle. Clipboard
    failures preserve all raw exports. Live append snapshots identify their scope.
    """
    files = files or LocalFiles()
    result = {"status": "error", "message": "", "export_path": "", "clipboard_path": "",
              "clipboard": {"methods": [], "warnings": []}, "missing": list(report.get("missing", ())),
              "warnings": list(report.get("warnings", ())), "jobs": [], "files": [], "bytes": 0}
    bundle_fd = parent_fd = None
    name = None
    export_path = ""
    published = False
    try:
        _cancel(cancel)
        entries = report.get("entries", ())
        from .raven_execution import router_for, large_log_export
        router = router_for()
        if router.managed and (len(entries) > 128 or sum(entry.get("snapshot", {}).get("size", 0) for entry in entries) > 2 << 20):
            if getattr(files, "remote", False):
                raise ValueError("Large remote log export needs an explicit worker-visible artifact mapping")
            exported = large_log_export(router, {"report": report, "destination": destination, "root": root,
                "state_dir": state_dir, "clipboard": clipboard}, cancel)
            # Workers publish bytes only. Clipboard/editor/terminal delivery
            # belongs to the original interface and its explicit destination.
            if clipboard and exported.get("clipboard_path"):
                try:
                    exported["clipboard"] = clipboard_io.copy_file(exported["clipboard_path"], tty_path=tty_path,
                        use_osc52=use_osc52, use_tools=use_tools, destination=copy_destination,
                        editor_context=editor_context,
                        cancel=(lambda: cancel() if callable(cancel) else cancel.is_set()) if cancel is not None else None)
                except Exception as exc:
                    exported["clipboard"] = {"methods": [], "warnings": ["Clipboard delivery failed: " + _clean(exc)]}
                exported.setdefault("warnings", []).extend(exported["clipboard"].get("warnings", ()))
                exported["status"] = "partial" if exported.get("missing") or exported["warnings"] else "ready"
                message = exported.get("message", "").replace("; clipboard delivery unavailable; complete bundle preserved", "")
                methods = exported["clipboard"].get("methods", ())
                exported["message"] = message + "; " + (", ".join(methods) if methods else "clipboard delivery unavailable; complete bundle preserved")
            return exported
        if report.get("status") in ("error", "cancelled") and not entries:
            raise ValueError(report.get("message") or "No available log outputs were found")
        if not entries:
            raise ValueError("No available log outputs were found")
        if clipboard:
            base = _path(os.path.join(state_dir, "exports") if state_dir else os.path.join(os.getcwd(), "tower-exports"))
            context = _private_directory(base, cancel)
        else:
            if not root or not destination:
                raise ValueError("Directory export requires a configured projects root and selected destination")
            context = _destination(root, destination)
        with context as opened:
            base, fd = (opened[1], opened[2]) if len(opened) == 3 else opened
            parent_fd = os.dup(fd)
            name = "tower-logs-" + datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:12]
            os.mkdir(name, 0o700, dir_fd=parent_fd)
            bundle_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
            export_path = os.path.join(base, name)
            total = sum(entry.get("snapshot", {}).get("size", 0) for entry in entries)
            completed = 0
            if progress:
                progress(0, total)
            for entry in entries:
                _cancel(cancel)
                job_ids = list(entry.get("job_ids") or [entry["job_id"]])
                job_dir = "job-" + _component(job_ids[0], "unknown")
                source_dir = _component(entry.get("id", "log")) + "-" + hashlib.sha256(entry["path"].encode("utf-8")).hexdigest()[:12]
                # Each source gets its original basename; parent folders prevent
                # same-name files from separate nodes/directories from colliding.
                basename = os.path.basename(entry["path"])
                if not basename or not basename.isprintable() or len(os.fsencode(basename)) > 255:
                    basename = _component(basename)
                relative = os.path.join(job_dir, source_dir, basename)
                job_fd = source_parent = output_fd = None
                try:
                    try:
                        os.mkdir(job_dir, 0o700, dir_fd=bundle_fd)
                    except FileExistsError:
                        pass
                    job_fd = os.open(job_dir, _DIRECTORY_FLAGS, dir_fd=bundle_fd)
                    os.mkdir(source_dir, 0o700, dir_fd=job_fd)
                    source_parent = os.open(source_dir, _DIRECTORY_FLAGS, dir_fd=job_fd)
                    output_fd = os.open(basename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=source_parent)
                    with os.fdopen(output_fd, "wb") as output:
                        output_fd = None
                        streamed = _stream(entry, files, output, cancel,
                                           (lambda done, size: progress(completed + done, max(total, completed + size))) if progress else None)
                        output.flush()
                    completed += streamed["bytes"]
                    result["bytes"] += streamed["bytes"]
                    result["files"].append({"job_ids": job_ids, "source_id": entry.get("id", "log"),
                                            "source_path": entry["path"], "label": entry.get("label", "log"),
                                            "relative_path": relative, "aliases": entry.get("aliases", ()),
                                            **{key: streamed[key] for key in ("bytes", "sha256", "utf8", "grew")}})
                    if streamed["grew"]:
                        result["warnings"].append(f"Job {', '.join(job_ids)}: {entry['path']} grew during export; copied its complete initial byte range")
                except InterruptedError:
                    raise
                except (OSError, ValueError) as exc:
                    if source_parent is not None:
                        try:
                            os.unlink(basename, dir_fd=source_parent)
                        except FileNotFoundError:
                            pass
                    result["missing"].extend(_missing(jid, entry.get("label", "log"), entry["path"], exc) for jid in job_ids)
                finally:
                    for descriptor in (output_fd, source_parent, job_fd):
                        if descriptor is not None:
                            os.close(descriptor)
            _cancel(cancel)
            result["jobs"] = [job.get("job_id") for job in report.get("jobs", ())]
            if not result["files"]:
                raise ValueError("No complete log files could be exported; inspect the missing-output list")
            manifest = {"schema": "tower.log-bundle/v1", "created_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                        "snapshot": "Complete initial byte range of each file; sources are not locked",
                        "jobs": result["jobs"], "files": result["files"], "missing": result["missing"], "warnings": result["warnings"]}
            with os.fdopen(os.open("manifest.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=bundle_fd), "w", encoding="utf-8") as output:
                json.dump(manifest, output, ensure_ascii=True, indent=2)
                output.write("\n")
            if clipboard:
                with os.fdopen(os.open("all-logs.txt", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=bundle_fd), "wb") as combined:
                    for exported in result["files"]:
                        _cancel(cancel)
                        heading = (f"===== JOB {', '.join(exported['job_ids'])} | {exported['label']} =====\n"
                                   f"Source: {exported['source_path']}\nBytes: {exported['bytes']} | SHA256: {exported['sha256']}\n\n")
                        combined.write(heading.encode("utf-8"))
                        source_fd, source_parent, _ = projects._open_local(bundle_fd, exported["relative_path"])
                        try:
                            with os.fdopen(source_fd, "rb") as source:
                                source_fd = None
                                while True:
                                    _cancel(cancel)
                                    chunk = source.read(CHUNK_BYTES)
                                    if not chunk:
                                        break
                                    combined.write(chunk)
                        finally:
                            if source_fd is not None:
                                os.close(source_fd)
                            os.close(source_parent)
                        combined.write(b"\n\n===== END LOG =====\n\n")
                result["clipboard_path"] = os.path.join(export_path, "all-logs.txt")
            _cancel(cancel)
            # Publishing a pathname is valid only while it still names the
            # descriptor selected by the user; a renamed/replaced destination
            # must not make the saved-path feedback point at another directory.
            with (_absolute_directory(base) if clipboard else _destination(root, destination)) as checked:
                check_fd = checked[-1]
                if (os.fstat(check_fd).st_dev, os.fstat(check_fd).st_ino) != (os.fstat(parent_fd).st_dev, os.fstat(parent_fd).st_ino):
                    raise OSError("Destination changed during copying; no bundle was published")
            result["export_path"] = export_path
            published = True
        if clipboard:
            try:
                result["clipboard"] = clipboard_io.copy_file(result["clipboard_path"], tty_path=tty_path,
                                                            use_osc52=use_osc52, use_tools=use_tools,
                                                            destination=copy_destination, editor_context=editor_context,
                                                            cancel=(lambda: cancel() if callable(cancel) else cancel.is_set()) if cancel is not None else None)
            except Exception as exc:
                result["clipboard"] = {"methods": [], "warnings": ["Clipboard delivery failed: " + _clean(exc)]}
            result["warnings"].extend(result["clipboard"].get("warnings", ()))
            if any(not entry["utf8"] for entry in result["files"]):
                result["warnings"].append("Clipboard skipped for invalid UTF-8: complete original bytes remain in the raw bundle; no replacement characters were inserted")
        result["status"] = "partial" if result["missing"] or result["warnings"] else "ready"
        result["message"] = f"Saved {len(result['files'])} complete logs ({result['bytes']:,} bytes) to {result['export_path']}"
        if clipboard:
            methods = result["clipboard"].get("methods", ())
            result["message"] += "; " + (", ".join(methods) if methods else "clipboard delivery unavailable; complete bundle preserved")
        if result["missing"]:
            result["message"] += f"; {len(result['missing'])} missing or unavailable outputs (see alert)"
    except InterruptedError as exc:
        result.update(status="cancelled", message=str(exc))
    except Exception as exc:
        result.update(status="error", message=_clean(exc))
    finally:
        if bundle_fd is not None:
            os.close(bundle_fd)
        if parent_fd is not None:
            if not published and name:
                # fd-relative removal keeps cleanup on the opened destination if
                # another process renames/replaces its path during the operation.
                try:
                    _remove_tree(parent_fd, name)
                    result.update(export_path="", clipboard_path="", files=[], bytes=0)
                except OSError as exc:
                    result["incomplete_path"] = export_path
                    result["warnings"].append("Incomplete bundle cleanup failed at " + export_path + ": " + _clean(exc))
                    result["message"] += "; incomplete bundle remains at " + export_path
            os.close(parent_fd)
    return result
