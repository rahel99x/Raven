"""Small project-local launch/export preferences, never read during painting.

Loading and resolution are bounded, read-only operations. Only explicit saves
create state. False values are significant; merging never treats them as absent.
"""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import re
import stat
import uuid

SCHEMA = "raven.automation-settings/v1"
MAX_SETTINGS = 256 << 10
MAX_PRESETS = 64
_ROOT_KEYS = {"schema", "launch", "recipes", "export_presets", "default_export_preset", "export_preferences", "associations"}
_LAUNCH_KEYS = {"features", "gpu", "trace", "trace_adapter", "trace_scope", "trace_helpers", "trace_cpu_placement", "trace_process_bytes", "trace_run_bytes", "trace_processes", "cpu_memory", "sampling_seconds", "storage_bytes", "telemetry", "sbatch_defaults", "parameters", "resources", "application_metrics", "artifacts", "research_sources", "passport", "scientific_context", "array_manifest", "auto_export", "auto_export_scope", "report_python", "environment", "checkpoint_advice"}
TRACE_DEFAULTS = {"trace_scope": "project", "trace_helpers": False, "trace_cpu_placement": False,
                  "trace_process_bytes": 64 << 20, "trace_run_bytes": 256 << 20, "trace_processes": 128}
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")


def defaults():
    return {"schema": SCHEMA, "launch": {}, "recipes": {}, "export_presets": {}, "default_export_preset": "", "export_preferences": {}}


def _json(value):
    from .reporting import _encoded
    return _encoded(value, MAX_SETTINGS)


def _launch(value):
    if not isinstance(value, dict) or set(value) - _LAUNCH_KEYS:
        raise ValueError("Unknown or invalid launch setting: " + ", ".join(sorted(set(value) - _LAUNCH_KEYS)) if isinstance(value, dict) else "launch settings must be an object")
    result = deepcopy(value)
    for key in ("gpu", "trace", "cpu_memory", "trace_helpers", "trace_cpu_placement"):
        if key in result and not isinstance(result[key], bool):
            raise ValueError(key + " must be true or false")
    if "features" in result:
        from .launch_features import normalize_features
        if not isinstance(result["features"], list) or len(result["features"]) > 64:
            raise ValueError("features must be a bounded list")
        result["features"] = normalize_features(result["features"])
    for key in ("parameters", "resources", "research_sources", "passport", "scientific_context", "sbatch_defaults", "environment", "telemetry"):
        if key in result and not isinstance(result[key], dict):
            raise ValueError(key + " must be an object")
    for key in ("application_metrics", "array_manifest", "report_python", "checkpoint_advice"):
        if key in result and (not isinstance(result[key], str) or not result[key] or len(result[key]) > 4096):
            raise ValueError(key + " must be a bounded path")
    if "artifacts" in result and (not isinstance(result["artifacts"], list) or len(result["artifacts"]) > 256):
        raise ValueError("artifacts must be a list of at most 256 explicit declarations")
    if "auto_export" in result and result["auto_export"] is not False and not isinstance(result["auto_export"], dict):
        raise ValueError("auto_export must be false or export request options")
    scope = result.get("auto_export_scope", {})
    if (not isinstance(scope, dict) or set(scope) - {"host", "user"}
            or any(not isinstance(value, str) or not value or len(value) > 255 or not value.isprintable() for value in scope.values())):
        raise ValueError("auto_export_scope requires bounded host and user names")
    if "trace_adapter" in result and result["trace_adapter"] not in ("", "python-monitoring", "python-legacy-profile", "native-cooperative", "mpi"):
        raise ValueError("Unsupported trace_adapter; use python-monitoring or explicit python-legacy-profile")
    if "trace_scope" in result and result["trace_scope"] not in ("project", "all"):
        raise ValueError("trace_scope must be project or all")
    for key, lower, upper in (("trace_process_bytes", 4096, 1 << 30), ("trace_run_bytes", 4096, 4 << 30), ("trace_processes", 1, 512)):
        if key in result and (type(result[key]) is not int or not lower <= result[key] <= upper):
            raise ValueError(key + " must be an integer within the supported trace budget")
    for key, lower, upper in (("sampling_seconds", 1, 300), ("storage_bytes", 4096, 268435456)):
        if key in result and (isinstance(result[key], bool) or not isinstance(result[key], (int, float)) or not lower <= result[key] <= upper):
            raise ValueError(key + " is outside the supported recording budget")
    telemetry = result.get("telemetry", {})
    if set(telemetry) - {"interval_seconds", "timeout_seconds", "max_bytes_per_stream", "gpu_provider"}:
        raise ValueError("Unknown retained telemetry setting")
    from .reporting import _number
    for key, lower, upper, integer in (("interval_seconds", 1, 300, False), ("timeout_seconds", .2, 10, False), ("max_bytes_per_stream", 4096, 268435456, True)):
        if key in telemetry:
            _number(telemetry[key], key, minimum=lower, maximum=upper, integer=integer)
    if telemetry.get("gpu_provider", "auto") not in ("auto", "nvidia", "amd", "intel"):
        raise ValueError("gpu_provider must be auto, nvidia, amd, or intel")
    scientific = result.get("scientific_context", {})
    if set(scientific) - {"workers", "problem_size", "repeat"}:
        raise ValueError("Scientific context accepts workers, problem_size, and repeat; put other declarations in parameters")
    for key, minimum, maximum, integer in (("workers", 1, 1000000, True), ("repeat", 1, 10000, True), ("problem_size", 1e-9, 1e300, False)):
        if key in scientific:
            _number(scientific[key], key, minimum=minimum, maximum=maximum, integer=integer)
    return result


def validate_settings(value):
    _json(value)
    if not isinstance(value, dict) or value.get("schema", SCHEMA) != SCHEMA or set(value) - _ROOT_KEYS:
        raise ValueError("Invalid Raven automation settings schema or unknown field")
    result = defaults()
    result.update(deepcopy(value))
    result["launch"] = _launch(result["launch"])
    for key in ("recipes", "export_presets"):
        items = result[key]
        valid_name = (lambda name: bool(_NAME.fullmatch(name))) if key == "recipes" else (lambda name: isinstance(name, str) and 0 < len(name) <= 128 and name.isprintable())
        if not isinstance(items, dict) or len(items) > MAX_PRESETS or any(not valid_name(name) for name in items):
            raise ValueError(key + " requires at most 64 valid named entries")
        for name, setting in items.items():
            if not isinstance(setting, dict):
                raise ValueError(key + " entries must be objects")
            if key == "recipes":
                items[name] = _launch(setting)
    if not isinstance(result["export_preferences"], dict):
        raise ValueError("export_preferences must be an object")
    selected = result["default_export_preset"]
    if not isinstance(selected, str) or selected and selected not in result["export_presets"]:
        raise ValueError("default_export_preset must name an existing preset")
    associations = result.get("associations", [])
    if not isinstance(associations, list) or len(associations) > 256:
        raise ValueError("associations must contain at most 256 explicit script/root bindings")
    return result


def _read_at(fd, name):
    try:
        source = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    except FileNotFoundError:
        return defaults()
    try:
        info = os.fstat(source)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SETTINGS:
            raise ValueError("Automation settings must be a regular file of at most 256 KiB")
        with os.fdopen(source, "rb", closefd=False) as stream:
            data = stream.read(MAX_SETTINGS + 1)
        if len(data) > MAX_SETTINGS:
            raise ValueError("Automation settings exceed 256 KiB")
        return validate_settings(json.loads(data))
    finally:
        os.close(source)


def load_settings(project):
    """Read project settings without creating the .raven directory."""
    from .projects import _root
    _, root_fd = _root(str(project))
    try:
        try:
            directory = os.open(".raven", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
        except FileNotFoundError:
            return defaults()
        try:
            return _read_at(directory, "automation.json")
        finally:
            os.close(directory)
    finally:
        os.close(root_fd)


def load_user_settings(environ=None):
    env = os.environ if environ is None else environ
    base = Path(env.get("XDG_CONFIG_HOME") or str(Path(env.get("HOME", str(Path.home()))) / ".config")) / "raven"
    if not base.exists():
        return defaults()
    from .projects import _root
    _, fd = _root(str(base))
    try:
        return _read_at(fd, "automation.json")
    finally:
        os.close(fd)


def _save_locked(fd, value):
    data = _json(validate_settings(value)) + b"\n"
    temporary = ".automation-" + uuid.uuid4().hex + ".tmp"
    created = False
    try:
        out = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        created = True
        with os.fdopen(out, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            info = os.stat("automation.json", dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("Existing automation settings must be a regular file; symlinks refused")
        except FileNotFoundError:
            pass
        os.replace(temporary, "automation.json", src_dir_fd=fd, dst_dir_fd=fd)
        created = False
        os.fsync(fd)
    finally:
        if created:
            os.unlink(temporary, dir_fd=fd)
    return json.loads(data)


def update_settings(project, changes, *, replace=False, replace_sections=(), expected=None):
    """Serialize explicit preference edits with a bounded local metadata lock."""
    from .research_reporting import _lock
    from .projects import _root
    # Validate before creating any state, including invalid-preset edits.
    if not isinstance(changes, dict) or set(changes) - _ROOT_KEYS:
        raise ValueError("Invalid automation preference update")
    _json(changes)
    with _lock(project, "automation.lock"):
        _, root_fd = _root(str(project))
        try:
            fd = os.open(".raven", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
            try:
                observed = _read_at(fd, "automation.json")
                if expected is not None and observed != validate_settings(expected):
                    raise ValueError("Project launch settings changed since review; reopen the launch review before saving.")
                current = defaults() if replace else observed
                for key, value in changes.items():
                    if not replace and key not in replace_sections and key in ("launch", "recipes", "export_presets", "export_preferences") and isinstance(value, dict):
                        current[key] = dict(current.get(key, {}), **deepcopy(value))
                    else:
                        current[key] = deepcopy(value)
                return _save_locked(fd, current)
            finally:
                os.close(fd)
        finally:
            os.close(root_fd)


def save_settings(project, settings):
    return update_settings(project, validate_settings(settings), replace=True)


def resolve_settings(project, *, recipe=None, user_defaults=None, explicit=None):
    """CLI > selected recipe > project > user; return values and their sources."""
    saved = load_settings(project)
    user = validate_settings(user_defaults) if user_defaults is not None else defaults()
    result, sources = {}, {}
    layers = [(user["launch"], "user default"), (saved["launch"], "project setting")]
    if recipe:
        if recipe not in saved["recipes"] and recipe not in user["recipes"]:
            raise ValueError("Unknown launch recipe '" + recipe + "'; available: " + ", ".join(sorted(set(saved["recipes"]) | set(user["recipes"]))))
        layers.append((saved["recipes"].get(recipe, user["recipes"].get(recipe)), "recipe " + recipe))
    layers.append((_launch(explicit or {}), "command line"))
    for values, source in layers:
        for key, value in values.items():
            result[key], sources[key] = deepcopy(value), source
    return result, sources
