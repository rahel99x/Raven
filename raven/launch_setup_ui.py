"""Small one-time reporting declarations editor, hosted by Export data's modal.

These settings describe producer inputs. Saving never launches a workload or
claims that a declared scientific measurement has been observed.
"""
from __future__ import annotations

import copy
import math
from pathlib import Path

FIELDS = (("application_metrics", "Application metrics JSONL file (project-relative)"),
          ("artifact", "Add declared artifact path (project-relative)"),
          ("array_manifest", "Array-to-sample manifest (project-relative)"),
          ("workers", "Scientific worker count"), ("problem_size", "Problem size"),
          ("repeat", "Repeat identifier (number)"),
          ("report_python", "Compute-node Python executable"),
          ("recipe_name", "Save launch recipe name"))


def open_setup(app):
    from . import data_export_ui as UI
    from .launch_settings import load_settings
    project = getattr(app, "project_state", {})
    root = (project.get("binding") or {}).get("project_root") or project.get("registered_root") or project.get("root")
    if not root:
        UI._feedback(app, "Open a project with File > Open project before saving its launch settings.", True)
        return False
    if getattr(app, "mode", "main") not in ("main", "setup", "data_export"):
        UI._feedback(app, "Close the current dialog before configuring project launch settings.", True)
        return False
    state = UI.initialize(app)
    if state["running"] or state["pending"]:
        UI._feedback(app, "Wait for or cancel the current export operation before configuring launch settings.", True)
        return False
    state.update(generation=state["generation"]+1, return_mode=getattr(app, "mode", "main"),
                 setup_root=root, setup_form={}, setup_fields={}, editing=None, error="", message="", cursor=0, top=0)
    app.mode = "data_export"
    files = getattr(app, "files", None) or getattr(getattr(app, "research", None), "files", None)
    if getattr(files, "remote", False):
        UI._failure(app, "Project launch preferences must be saved on the project host. Run Raven there; remote settings writes are unsupported.")
        return False
    def completed(value):
        launch = copy.deepcopy(value.get("launch", {}))
        state.update(stage="setup", settings=value, setup_form=launch)
        _fields(state)
        UI._invalidate(state)
    UI._queue(app, "Read project launch preferences", lambda event, progress: load_settings(root), completed)
    return True


def _fields(state):
    launch = state["setup_form"]
    fields = {key: str(launch.get(key, "")) for key in ("application_metrics", "array_manifest", "report_python")}
    fields.update({key: str(launch.get("scientific_context", {}).get(key, "")) for key in ("workers", "problem_size", "repeat")})
    from .launch_features import FEATURES
    fields.update({"report:" + item["name"]: str(launch.get("research_sources", {}).get(item["name"], ""))
                   for item in FEATURES if item["name"] not in ("experiment", "arrays", "artifacts")})
    fields.update(artifact="", recipe_name="paper-run")
    state["setup_fields"] = fields


def document(app):
    from .data_export_ui import initialize
    from .launch_features import FEATURES
    state = initialize(app)
    form, fields = state["setup_form"], state["setup_fields"]
    rows = [("Close project setup", "close"), ("Save project launch settings", "setup:save"),
            ("Job's project folder: " + state["setup_root"], None),
            ("Declarations are inputs, not observed measurements. Saving does not submit a job, allocate Heavy, or execute Operations. Future raven launch commands use these settings; explicit CLI choices take precedence.", None)]
    if state["error"]:
        rows.append(("Issue: " + state["error"], None))
    if state["message"]:
        rows.append((state["message"], None))
    for key, label in FIELDS:
        rows.append((label + ": " + (fields.get(key) or "[unset]"), "field:setup:" + key))
    for key, label in (("gpu", "GPU recording"), ("cpu_memory", "CPU/memory process recording"), ("trace", "Stack tracing (requires application hook)")):
        rows.append((("[x] " if form.get(key, key == "cpu_memory") else "[ ] ") + label, "setup:toggle:" + key))
    rows.append(("Trace adapter: " + (form.get("trace_adapter") or "Not configured"), "setup:adapter"))
    rows.append(("Source \"$RAVEN_SHELL_HELPER\" then use raven_exec python3 program.py in the script for the selected Python adapter. The Python monitoring adapter requires Python 3.12+; Python 3.10/3.11 needs explicit legacy-profile consent. Selecting trace alone cannot instrument every runtime.", None))
    for item in FEATURES:
        name, label = item["name"], item["label"]
        rows.append((("[x] " if name in form.get("features", []) else "[ ] ") + label, "setup:feature:" + name))
        if name in form.get("features", []):
            rows.append(("  Needs: " + item["prerequisites"], None))
            if "report:" + name in fields:
                rows.append(("  Native " + label + " report (project-relative): " + (fields["report:" + name] or "[unset]"),
                             "field:setup:report:" + name))
    for index, artifact in enumerate(form.get("artifacts", ())):
        rows.append(("Remove artifact: " + str(artifact), "setup:remove:" + str(index)))
    rows.append(("Save named launch recipe", "setup:recipe-save"))
    for name in sorted(state["settings"].get("recipes", {})):
        rows.append(("Load launch recipe: " + name, "setup:recipe:" + name))
    rows.append(("Disable future automatic exports", "setup:auto-off"))
    for name in sorted(state["settings"].get("export_presets", {})):
        rows.append(("Completion export preset: " + name, "setup:preset:" + name))
    rows.append(("Save settings, then preview raven launch SCRIPT --check. A static configuration cannot certify scheduler admission, compute-node packages, GPU assignment, scientific comparability, or native-report prerequisites.", None))
    return rows


def apply_field(app, key, value):
    from .data_export_ui import initialize
    state = initialize(app)
    state["setup_fields"][key] = value
    if key == "artifact" and value:
        artifacts = state["setup_form"].setdefault("artifacts", [])
        if value not in artifacts:
            if len(artifacts) >= 256:
                raise ValueError("At most 256 explicit artifact paths may be declared")
            artifacts.append(value)
        state["setup_fields"]["artifact"] = ""


def _resolved(state):
    launch = copy.deepcopy(state["setup_form"])
    fields = state["setup_fields"]
    for key in ("application_metrics", "array_manifest", "report_python"):
        if fields.get(key):
            launch[key] = fields[key]
        else:
            launch.pop(key, None)
    scientific = dict(launch.get("scientific_context", {}))
    for key in ("workers", "problem_size", "repeat"):
        value = fields.get(key, "")
        if not value:
            scientific.pop(key, None)
            continue
        number = float(value)
        if not math.isfinite(number) or number <= 0:
            raise ValueError(key + " must be a finite positive declaration")
        if key == "workers" and not number.is_integer():
            raise ValueError("Scientific worker count must be an integer")
        scientific[key] = int(number) if number.is_integer() else number
    launch["scientific_context"] = scientific
    launch["research_sources"] = {key[len("report:"):]: value for key, value in fields.items()
                                  if key.startswith("report:") and value}
    if launch.get("trace") and not launch.get("trace_adapter"):
        raise ValueError("Choose a tracing adapter and add raven_exec to the script before enabling tracing")
    if not launch.get("trace"):
        launch.pop("trace_adapter", None)
    return launch


def save(app, *, recipe=False):
    from . import data_export_ui as UI
    state = UI.initialize(app)
    try:
        launch = _resolved(state)
    except ValueError as exc:
        state["error"] = str(exc)
        UI._feedback(app, exc, True)
        UI._invalidate(state)
        return
    project, name = state["setup_root"], state["setup_fields"]["recipe_name"].strip()
    def work(event, progress):
        from .launch import _declared_paths
        from .launch_settings import update_settings
        if event.is_set():
            raise InterruptedError("Launch preference save cancelled")
        _declared_paths(launch, Path(project))
        return update_settings(project, {"recipes": {name: launch}} if recipe else {"launch": launch},
                               replace_sections=() if recipe else ("launch",))
    def completed(value):
        state.update(stage="setup", settings=value, setup_form=launch, error="",
                     message="Saved " + ("launch recipe " + name if recipe else "project launch settings") + ". Preview the actual script with raven launch SCRIPT --check.")
        UI._invalidate(state)
    UI._queue(app, "Save project launch declarations", work, completed)


def activate(app, action):
    from . import data_export_ui as UI
    state = UI.initialize(app)
    form = state["setup_form"]
    parts = action.split(":", 2)
    kind, value = parts[1], parts[2] if len(parts) == 3 else ""
    if kind in ("save", "recipe-save"):
        save(app, recipe=kind == "recipe-save")
    elif kind == "toggle":
        form[value] = not form.get(value, value == "cpu_memory")
    elif kind == "feature":
        form["features"] = UI._toggle(form.get("features", []), value)
    elif kind == "adapter":
        values = ["", "python-monitoring", "python-legacy-profile"]
        current = form.get("trace_adapter", "")
        form["trace_adapter"] = values[(values.index(current)+1) % len(values)] if current in values else ""
    elif kind == "remove":
        index = int(value)
        if 0 <= index < len(form.get("artifacts", [])):
            form["artifacts"].pop(index)
    elif kind == "recipe":
        state["setup_form"] = copy.deepcopy(state["settings"]["recipes"][value])
        _fields(state)
        state["message"] = "Recipe loaded for editing. Save project settings to use these defaults, or launch with --recipe " + value + "."
    elif kind == "auto-off":
        form["auto_export"] = False
        form["auto_export_scope"] = {}
    elif kind == "preset":
        preference = copy.deepcopy(state["settings"]["export_presets"][value])
        destination = preference.get("destination", {})
        if destination.get("mode") == "absolute":
            import socket, getpass
            if destination.get("host") != socket.gethostname() or destination.get("user") != getpass.getuser():
                state["error"] = "This preset's absolute destination belongs to another host or user. Edit it in Export data here before using it."
                return
        if preference.get("range_policy", "full") not in ("full", "full-recorded-run") or preference.get("start") is not None or preference.get("end") is not None:
            state["error"] = "Completion presets for future launches must use the full recorded run, without timestamps from an earlier attempt."
            UI._invalidate(state)
            return
        form["auto_export_scope"] = {"host": destination["host"], "user": destination["user"]} if destination.get("mode") == "absolute" else {}
        destination.pop("user", None)
        form["auto_export"] = preference
        state["message"] = "Completion export preset selected for future launches; Save project settings to persist."
    UI._invalidate(state)
