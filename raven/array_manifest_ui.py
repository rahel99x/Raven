"""Scientific array labels and an explicit, revision-pinned manifest browser."""
from __future__ import annotations

import json
import os
import time

from . import array_manifest as M, layout as L
from .research_text import clean


def _owner(app):
    return getattr(app, "_chart_owner", app)


def initialize(app):
    app = _owner(app)
    if not isinstance(getattr(app, "array_manifest_state", None), dict):
        app.array_manifest_state = {"manifest": None, "path": "", "stamp": None, "notice": "",
            "busy": False, "generation": 0, "last": 0.0, "view": "list", "cursor": 0, "top": 0,
            "detail_scroll": 0, "query": "", "matches": (), "edit": "", "control_hits": [],
            "hub": None, "submission_times": (), "detail_cache": None, "origin": "", "auto_suppressed": None}
    return app.array_manifest_state


def detach_automatic(app):
    """Drop only a run-owned mapping; manually attached scientific maps win."""
    state = initialize(app)
    if state.get("origin") == "automatic":
        state.update(manifest=None, path="", stamp=None, notice="", generation=state["generation"] + 1,
                     busy=False, matches=(), cursor=0, top=0, control_hits=[], detail_cache=None, origin="")


def apply_automatic(app, selected):
    """Publish a prevalidated map with its run, without file reads or navigation."""
    app = _owner(app)
    state = initialize(app)
    binding = selected.get("binding", {})
    identity = (binding.get("project_root"), binding.get("run_id"), binding.get("job_id"), binding.get("attempt"))
    if state.get("origin") == "manual" or state.get("auto_suppressed") == identity:
        return
    document = selected.get("array_manifest_document")
    if not document:
        detach_automatic(app)
        return
    # Parsing was done on the project worker. The immutable object is handed
    # over directly; a 10,000-index map must never be rebuilt on each UI tick.
    manifest = selected.get("array_manifest_record")
    if manifest is None:
        return
    previous = state.get("manifest")
    path = binding.get("array_manifest", "")
    changed = not previous or previous.revision != manifest.revision or state.get("path") != path
    if changed:
        state.update(manifest=manifest, path=path, stamp=None, notice="Automatically linked to this run",
                     generation=state["generation"] + 1, busy=False, cursor=0, top=0, query="",
                     matches=manifest.entries, last=time.monotonic(), detail_scroll=0,
                     hub=getattr(app, "research", None), submission_times=(), detail_cache=None, origin="automatic")


def command_names():
    return ["arraymap"]


def _context(app):
    state = getattr(app, "project_state", {}) or {}
    binding = state.get("binding") or {}
    return (getattr(app, "selected_id", None), getattr(app, "research_job_id", None),
            state.get("generation"), binding.get("run_id"), binding.get("job_id"),
            getattr(getattr(app, "research", None), "generation", None))


def _attach(app, path):
    app = _owner(app)
    state = initialize(app)
    hub = getattr(app, "research", None)
    if hub is None:
        raise ValueError("research file services are unavailable")
    if state["busy"]:
        raise ValueError("an array manifest read is already running")
    M._text(path, "manifest path")
    if not getattr(hub.files, "remote", False):
        path = os.path.abspath(os.path.expanduser(path))
    from .project_ui import cancel_automatic
    cancel_automatic(app)
    token = state["generation"] + 1
    origin = _context(app)
    store = getattr(app, "store", None)
    observed = [(str(getattr(job, "id", "")), str(getattr(job, "cluster", "")), str(getattr(job, "submit", "")))
                for source in (getattr(store, "jobs", ()), getattr(store, "finished", ())) for job in source[:50000]]

    def complete(value):
        if state["generation"] != token:
            return
        state["busy"] = False
        if getattr(app, "research", None) is not hub or origin != _context(app):
            app.say("Array manifest read discarded because the selected job or project changed; attach again")
            return
        if isinstance(value, Exception):
            state["notice"] = clean(value)
            app.fail("arraymap: " + state["notice"])
            return
        manifest, stamp, submissions = value
        state.update(manifest=manifest, path=path, stamp=stamp, notice="", cursor=0, top=0,
                     query="", matches=manifest.entries, last=time.monotonic(), detail_scroll=0,
                     hub=hub, submission_times=submissions, detail_cache=None, origin="manual")
        app.say(f"Array {manifest.array_id}: {len(manifest.entries)} scientific identities; revision {manifest.revision[:12]}")

    def worker():
        manifest, stamp = M.read(path, hub.files)
        submissions = set()
        for jid, cluster, submitted in observed:
            if (jid.startswith(manifest.array_id + "_") and cluster == manifest.cluster and submitted
                    and submitted.upper() not in ("UNKNOWN", "N/A", "NONE")):
                submissions.add(submitted)
                if len(submissions) > 1:
                    raise ValueError("multiple submission attempts share this array ID; select an unambiguous accounting window before attaching")
        return manifest, stamp, tuple(submissions)
    state.update(generation=token, busy=True)
    if getattr(app, "interactive", True):
        if not hub.start_task(worker, complete):
            state["busy"] = False
            raise ValueError("research reader is busy; retry after its current operation")
        app.say("Reading array manifest in the background")
    else:
        try:
            value = worker()
        except Exception as exc:
            value = exc
        complete(value)


def tick(app):
    """Check only the attached file at a bounded cadence; never parse per frame."""
    app = _owner(app)
    state = initialize(app)
    hub = getattr(app, "research", None)
    manifest = state["manifest"]
    if not manifest or not hub:
        return
    if state.get("origin") == "automatic":
        # The project reader follows the declaration and validates the exact
        # attempt together with the report; a separate stat races that binding.
        return
    if state["hub"] is not hub:
        state["notice"] = "Connection changed; reload the array input map before using its labels"
        state["busy"] = False
        return
    if state["busy"] or getattr(hub, "pending", None):
        return
    now = time.monotonic()
    if now - state["last"] < max(5.0, getattr(hub.files, "min_refresh", 0.0)):
        return
    state["last"] = now
    token, path, stamp = state["generation"], state["path"], state["stamp"]

    def worker():
        current = hub.files.snapshot_stat(path)
        if current == stamp:
            return None, current
        return M.read(path, hub.files)

    def complete(value):
        if state["generation"] != token:
            return
        state["busy"] = False
        if getattr(app, "research", None) is not hub:
            state["notice"] = "Connection changed; reload the array input map before using its labels"
            return
        if isinstance(value, Exception):
            state["notice"] = "Pinned revision retained; source unavailable: " + clean(value)
            return
        fresh, current = value
        if fresh and fresh.revision != manifest.revision:
            state["notice"] = f"Source changed to {fresh.revision[:12]}; pinned {manifest.revision[:12]} retained. Reload to accept."
        elif fresh:
            state["notice"] = ""
        state["stamp"] = current

    if hub.start_task(worker, complete):
        state["busy"] = True


def capture(app):
    """Capture immutable identity before handing retry preparation to a worker."""
    state = initialize(app)
    manifest = state["manifest"]
    if manifest:
        if state["hub"] is not getattr(_owner(app), "research", None):
            raise ValueError("array input map belongs to another connection; reload it first")
        return manifest, state["path"], state["submission_times"]
    return None


def label(app, group, index):
    state = initialize(app)
    manifest = state["manifest"]
    if not manifest or not manifest.matches(group.get("id"), group.get("cluster", "")):
        return None
    if state["hub"] is not getattr(_owner(app), "research", None):
        return None
    observed = set(group.get("submit_times", ()))
    pinned = set(state["submission_times"])
    if len(observed) > 1 or pinned and observed and pinned != observed:
        state["notice"] = "Array submission identity changed or is ambiguous; reload before using scientific labels"
        return None
    return manifest.by_index.get(index)


def controls(g, app, width):
    from .control_rows import buttons
    state = initialize(app)
    choices = [("load", "Load input map", ("command", "arraymap"))]
    if state["manifest"]:
        choices += [("inspect", "Browse inputs", ("command", "arraymap inspect")),
                    ("reload", "Reload map", ("command", "arraymap reload")),
                    ("clear", "Detach map", ("command", "arraymap clear"))]
    rows, hits = buttons(g, width, choices, group="array_manifest", prefix="arraymap:")
    manifest = state["manifest"]
    if manifest:
        rows.append([(clean(f" Map {manifest.cluster or '(unlabelled cluster)'} / {manifest.array_id} | {len(manifest.entries)} inputs | {manifest.revision[:12]}", g.ascii), "dim")])
        if not state["submission_times"]:
            rows.append([(" Submission timestamp unavailable; mapping is scoped to this connection and parent ID", "dim")])
    if state["notice"]:
        rows.append([(clean(" ! " + state["notice"], g.ascii), "yellow")])
    return rows, hits


def _open(app, view):
    app = _owner(app)
    state = initialize(app)
    state.update(view=view, control_hits=[])
    app.mode = "arraymap"


def run_command(app, args):
    if not args or args[0] != "arraymap":
        return False
    app = _owner(app)
    state = initialize(app)
    args = list(args[1:])
    try:
        if not args:
            state["edit"] = state["path"]
            _open(app, "path")
        elif args == ["clear"]:
            binding = (getattr(app, "project_state", {}) or {}).get("binding") or {}
            state["auto_suppressed"] = tuple(binding.get(key) for key in ("project_root", "run_id", "job_id", "attempt"))
            state.update(manifest=None, path="", stamp=None, notice="", generation=state["generation"] + 1,
                         busy=False, matches=(), cursor=0, top=0, control_hits=[], detail_cache=None, origin="")
            if app.mode == "arraymap":
                app.mode = "main"
            app.say("Array input map detached")
        elif args == ["reload"]:
            if not state["path"]:
                raise ValueError("attach an array manifest first")
            _attach(app, state["path"])
        elif args == ["inspect"]:
            if not state["manifest"]:
                raise ValueError("attach an array manifest first")
            _open(app, "list")
        elif args[0] == "search":
            if not state["manifest"]:
                raise ValueError("attach an array manifest first")
            state["query"] = " ".join(args[1:])
            state.update(matches=state["manifest"].search(state["query"]), cursor=0, top=0)
            _open(app, "list")
        elif args[0] == "select" and len(args) == 3:
            manifest = state["manifest"]
            if not manifest or manifest.revision != args[1]:
                raise ValueError("the array map changed; choose its current row")
            index = int(args[2])
            entry = manifest.by_index.get(index)
            if entry is None:
                raise ValueError("no scientific identity is declared for this exact array index")
            state.update(matches=manifest.entries, query="", cursor=manifest.entries.index(entry), detail_scroll=0)
            _open(app, "detail")
        elif len(args) == 1 or len(args) == 2 and args[0] == "load":
            _attach(app, args[-1])
        else:
            raise ValueError("arraymap [PATH | load PATH | inspect | reload | clear | search TEXT]")
    except (ValueError, OSError, TypeError) as exc:
        app.fail("arraymap: " + clean(exc))
    return True


def handle_key(app, key):
    if getattr(app, "mode", None) != "arraymap":
        return False
    state = initialize(app)
    view = state["view"]
    if key == "esc":
        if view in ("detail", "search"):
            state.update(view="list", control_hits=[])
        else:
            app.mode = "main"
        return True
    if view in ("path", "search"):
        if key == "enter":
            value = state["edit"]
            if view == "path":
                app.mode = "main"
                run_command(app, ["arraymap", "load", value])
            else:
                run_command(app, ["arraymap", "search", value])
        elif key in ("backspace", "ctrl-h"):
            state["edit"] = state["edit"][:-1]
        elif key == "ctrl-u":
            state["edit"] = ""
        elif isinstance(key, str) and len(key) == 1 and key.isprintable() and len(state["edit"]) < (4096 if view == "path" else 256):
            state["edit"] += key
        return True
    if key == "/":
        state.update(view="search", edit=state["query"], control_hits=[])
    elif key in ("up", "down", "pgup", "pgdn", "home", "end"):
        if view == "detail":
            current = state["detail_scroll"]
            state["detail_scroll"] = max(0, 0 if key == "home" else 10000 if key == "end" else
                                          current + {"up": -1, "down": 1, "pgup": -10, "pgdn": 10}[key])
        else:
            current, count = state["cursor"], len(state["matches"])
            state["cursor"] = max(0, min(count - 1, 0 if key == "home" else count - 1 if key == "end" else
                                         current + {"up": -1, "down": 1, "pgup": -10, "pgdn": 10}[key]))
    elif key == "enter" and state["matches"]:
        state.update(view="detail", detail_scroll=0, control_hits=[])
    return True


def overlay(views, snap, app, width, height):
    if getattr(app, "mode", None) != "arraymap":
        return None
    state = initialize(app)
    from . import modal_scrollbars as B
    state["control_hits"] = []
    ascii_ = views.g.ascii
    row = lambda text, style="": [(clean(text, ascii_), style)]
    usable, inner = max(1, height - 6), max(1, width - 8)
    content, actions, scroll_spec = [], [], None
    manifest, view = state["manifest"], state["view"]
    if view in ("path", "search"):
        content = [row("Path on the connected system:" if view == "path" else "Find scientific ID, label, index or parameter:", "cyan"),
                   row(state["edit"][-inner:] + "_", "bold"),
                   row("Enter accepts | Esc cancels | Ctrl-U clears", "dim")]
        actions = [("accept", "Load" if view == "path" else "Search", "enter"), ("cancel", "Cancel", "esc")]
    elif manifest:
        content = [row(f"Array {manifest.array_id} on {manifest.cluster or '(unlabelled cluster)'} | revision {manifest.revision[:12]}", "cyan+bold")]
        if state["notice"]:
            content.append(row(state["notice"], "yellow"))
        if view == "detail" and state["matches"]:
            entry = state["matches"][min(state["cursor"], len(state["matches"]) - 1)]
            from .execution_ui import _wrap
            cache_key = (manifest.revision, entry.index, inner, ascii_)
            cached = state["detail_cache"]
            if not cached or cached[0] != cache_key:
                details = [f"{manifest.array_id}_{entry.index} | {entry.id}", "Label: " + entry.label,
                           "Parameters: " + json.dumps(dict(entry.parameters), ensure_ascii=False, sort_keys=True),
                           "Declared inputs (not opened):"] + list(entry.inputs or ("none",)) + ["Declared outputs (not opened):"] + list(entry.outputs or ("none",))
                lines = [part for line in details for part in _wrap(line, inner, ascii_=ascii_)]
                state["detail_cache"] = (cache_key, lines)
            else:
                lines = cached[1]
            page = max(1, usable - len(content) - 2)
            state["detail_scroll"] = min(state["detail_scroll"], max(0, len(lines) - page))
            context = (manifest.revision, entry.index, width)
            target, painted = B.window(app, "modal:arraymap-detail", state["detail_scroll"], len(lines), page, context=context)
            state["detail_scroll"] = target
            scroll_spec = ("modal:arraymap-detail", len(content), len(lines), page, target, painted,
                           lambda value: state.update(detail_scroll=value), context)
            content += [row(line) for line in lines[painted:painted + page]]
            actions = [("back", "Back to inputs", "esc")]
        else:
            matches = state["matches"]
            content.append(row(f"{len(matches)} matching entries | / search | arrows select | Enter details", "dim"))
            page = max(1, usable - len(content) - 2)
            state["top"] = max(0, min(state["top"], max(0, len(matches) - page)))
            if state["cursor"] < state["top"]:
                state["top"] = state["cursor"]
            if state["cursor"] >= state["top"] + page:
                state["top"] = max(0, state["cursor"] - page + 1)
            context = (manifest.revision, state["query"], width)
            target, painted = B.window(app, "modal:arraymap-list", state["top"], len(matches), page,
                                       context=context, focus=state["cursor"])
            state["top"] = target
            scroll_spec = ("modal:arraymap-list", len(content), len(matches), page, target, painted,
                           lambda value: state.update(top=value), context)
            for index in range(painted, min(len(matches), painted + page)):
                entry = matches[index]
                content.append(row(f"{'>' if index == state['cursor'] else ' '} {entry.index} | {entry.id} | {entry.label}", "sel" if index == state["cursor"] else ""))
                state["control_hits"].append((len(content) - 1, index))
            if not matches:
                content.append(row("No matching entries. / changes the search.", "dim"))
            actions = [("search", "Search", "/"), ("close", "Close", "esc")]
    else:
        content = [row("No array input map attached", "dim")]
        actions = [("close", "Close", "esc")]
    from .control_rows import buttons, place_hits
    rows, hits = buttons(views.g, inner, [(key, label, ("key", action)) for key, label, action in actions],
                         group="arraymap-modal", prefix="arraymap-modal:")
    hits = [(y + len(content), kind, data) for y, kind, data in hits]
    content += rows
    rendered = L.box(views.g, content[:usable], width, height, "Scientific array inputs", min_width=20)
    if scroll_spec:
        key, start, count, page, target, painted, setter, context = scroll_spec
        rendered = B.boxed(app, key, rendered, start=start, count=count, page=page, target=target,
                           painted=painted, setter=setter, context=context, header=-1)
    placements = rendered[1:-1]
    row_hits = state["control_hits"]
    state["control_hits"] = place_hits(hits, placements)
    for rownum, index in row_hits:
        if rownum < len(placements):
            y, x, segments = placements[rownum]
            end = x + L.vlen(L.row_text(segments)) - (2 if scroll_spec else 1)
            if end <= x + 1:
                continue
            state["control_hits"].append((y, "control", {"id": f"arraymap-entry:{index}", "label": state["matches"][index].id,
                "left": x + 1, "right": end, "action": ("click", y, x + 1), "group": "arraymap-inputs", "index": index}))
    state["control_token"] = (state["generation"], view, state["query"], state["cursor"], width, height)
    return rendered


def handle_mouse(app, y, x, button="left", shift=False):
    if getattr(app, "mode", None) != "arraymap":
        return False
    from .scrollbars import handle_mouse as scroll_mouse
    if scroll_mouse(app, y, x, button=button, shift=shift):
        return True
    if button in ("wheel-up", "wheel-down"):
        handle_key(app, "up" if button == "wheel-up" else "down")
        return True
    if button not in ("left", "press"):
        return True
    state = initialize(app)
    token = state.get("control_token", ())
    if token[:4] != (state["generation"], state["view"], state["query"], state["cursor"]):
        return True
    for row, _, value in state["control_hits"]:
        if row == y and value["left"] <= x < value["right"]:
            if "index" in value:
                state.update(cursor=value["index"], view="detail", detail_scroll=0, control_hits=[])
            elif value["action"][0] == "key":
                handle_key(app, value["action"][1])
            return True
    return True
