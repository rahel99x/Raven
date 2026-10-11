"""Live exact-attempt trace subscription, using the existing background reader.

Paints consume published snapshots. Neither a hover nor a draw opens a trace,
attaches a debugger, or changes recording. Display pause only pauses this reader.
"""
from __future__ import annotations

import os
import threading
import time

from . import layout as L
from .control_rows import buttons


def initialize(app):
    if not isinstance(getattr(app, "trace_panel_state", None), dict):
        app.trace_panel_state = dict(token=0, context=None, status="idle", error="", result=None,
            reader=None, callback=None, cancel=None, next_read=0.0, path="", explicit="", sources=[],
            index=0, paused=False, verified=False, paint=None, discover_at=0.0)
    return app.trace_panel_state


def command_names():
    return ["trace"]


def _context(app):
    jid = getattr(app, "selected_id", None)
    if not jid:
        return None
    record, details = app.store.record_context(jid)
    if record is None:
        return None
    project = getattr(app, "project_state", {})
    binding = project.get("binding") or {}
    if binding.get("job_id") != jid:
        binding = {}
    root = (binding.get("project_root") or project.get("root") if binding else details.get("WorkDir")) or project.get("registered_root") or ""
    cfg = getattr(app.cfg, "data", app.cfg)
    cluster = str(cfg.get("cluster_name", "") or details.get("Cluster", ""))
    from .execution_ui import connection_scope
    try:
        scope = connection_scope(app)
    except ValueError:
        return None
    # This uses only published metadata; no filesystem or scheduler calls.
    key = (str(jid), str(getattr(record, "submit", "") or ""), str(getattr(record, "start", "") or ""),
           app.store.job_attempt(jid), root, cluster, binding.get("run_id", ""), repr(scope))
    return dict(key=key, job_id=str(jid), submit=key[1], start=key[2], root=root, cluster=cluster,
                run_id=binding.get("run_id", ""), remote=bool(getattr(getattr(app, "files", None), "remote", False)))


def _visible(app):
    return (getattr(app, "tab", "") in ("jobs", "history") and getattr(app, "mode", "") == "main"
            and getattr(app, "job_panel_state", {}).get("mode") == "trace")


def cancel(app, *, clear=False):
    state = initialize(app)
    state["token"] += 1
    if state["cancel"] is not None:
        state["cancel"].set()
    hub = getattr(app, "research", None)
    if hub is not None and state["callback"] is not None:
        hub.cancel_task(state["callback"])
    state.update(callback=None, cancel=None)
    if clear:
        state.update(context=None, result=None, reader=None, sources=[], path="", explicit="", status="idle", index=0, error="", verified=False, paint=None, discover_at=0.0)


def run_command(app, args):
    if not args or args[0] != "trace":
        return False
    state = initialize(app)
    from .job_panels import _activate
    if len(args) == 1:
        if getattr(app, "tab", "") not in ("jobs", "history"):
            app.enter_tab("jobs")
        _activate(app, "trace")
    elif len(args) == 3 and args[1] == "attach":
        if not args[2] or len(args[2]) > 4096 or any(not c.isprintable() for c in args[2]):
            app.fail("Choose a bounded printable trace manifest path.")
        elif _context(app) is None:
            app.fail("Select the exact job before attaching its trace.")
        else:
            cancel(app, clear=True)
            state.update(explicit=args[2], context=_context(app), next_read=0.0)
            _activate(app, "trace")
    elif args[1:] in (["refresh"], ["pause"], ["follow"], ["clear"]):
        operation = args[1]
        if operation == "clear":
            cancel(app, clear=True)
        else:
            state["paused"] = operation == "pause"
            if operation != "pause":
                state["next_read"] = 0.0
            if operation == "refresh":
                state["discover_at"] = 0.0
    elif len(args) == 3 and args[1] == "select":
        try:
            index = int(args[2])
            if not 0 <= index < len(state["sources"]):
                raise ValueError()
            cancel(app)
            state.update(index=index, path="", reader=None, result=None, next_read=0.0)
        except (TypeError, ValueError):
            app.fail("Choose a visible trace source index.")
    else:
        app.fail("trace [attach MANIFEST|refresh|pause|follow|clear|select INDEX]")
    return True


def _matches(identity, context):
    if identity.get("job_id") != context["job_id"]:
        raise ValueError("Trace belongs to a different job.")
    if context["cluster"] and identity.get("cluster") != context["cluster"]:
        raise ValueError("Trace belongs to a different cluster.")
    if context["run_id"] and (identity.get("run_id") == context["run_id"] or identity.get("attempt") == context["run_id"]):
        return True
    if context["submit"] and identity.get("slurm_submit") == context["submit"]:
        if context["start"]:
            return identity.get("slurm_start") == context["start"]
        return True
    expected = context["submit"] + ("/" + context["start"] if context["start"] else "")
    return bool(context["submit"] and identity.get("attempt") == expected)


def _read(context, explicit, index, previous_path, reader, event, source_cache=None):
    from .trace_protocol import TraceReader, discover_trace, read_json
    if context["remote"]:
        raise ValueError("Open Tower on the trace host to inspect its journal; remote views never substitute local files.")
    if explicit:
        sources = [explicit]
    elif source_cache is not None:
        sources = source_cache
    elif context["root"]:
        candidates = discover_trace(context["root"], context["job_id"])
        sources = []
        for path in candidates:
            if event.is_set():
                return None
            identity = read_json(path).get("identity", {})
            # Registered projects may retain the same numeric job ID from
            # different clusters. Ignore other identities during discovery;
            # explicitly attached mismatches still fail visibly below.
            if context["cluster"] and identity.get("cluster") != context["cluster"]:
                continue
            if _matches(identity, context):
                sources.append(path)
    else:
        sources = []
    if not sources:
        return dict(sources=[], snapshot=None, reader=None, path="", verified=False, discovered=source_cache is None)
    path = sources[max(0, min(index, len(sources) - 1))]
    reader = reader if reader is not None and path == previous_path else TraceReader(path)
    snapshot = reader.refresh(max_events=4096, max_bytes=2 << 20, max_seconds=.015, cancel=event)
    verified = _matches(snapshot["identity"], context)
    if not verified and not explicit:
        raise ValueError("Trace attempt cannot be verified against the current job.")
    return dict(sources=sources, snapshot=snapshot, reader=reader, path=path, verified=verified, discovered=source_cache is None)


def tick(app):
    state = initialize(app)
    context = _context(app) if _visible(app) else None
    if context is None:
        if state["callback"] is not None:
            cancel(app)
        return
    if state["context"] is None or state["context"]["key"] != context["key"]:
        cancel(app, clear=True)
        state["context"] = context
    if state["paused"] or state["callback"] is not None or time.monotonic() < state["next_read"]:
        return
    from .job_panels import _worker
    hub = _worker(app, getattr(app, "files", None))
    with hub.lock:
        if hub.closed or hub.pending or hub.future is not None and not hub.future.done():
            return
    token, event = state["token"], threading.Event()
    explicit, index, path, reader = state["explicit"], state["index"], state["path"], state["reader"]
    source_cache = list(state["sources"]) if time.monotonic() < state["discover_at"] else None

    def work():
        return _read(context, explicit, index, path, reader, event, source_cache)

    def done(result):
        current = _context(app)
        if event.is_set() or state["token"] != token or not _visible(app) or current is None or current["key"] != context["key"]:
            return
        state.update(callback=None, cancel=None, next_read=time.monotonic() + 0.5)
        if isinstance(result, Exception):
            from .operations import clean
            state.update(status="error", error=clean(result, 400), result=None, discover_at=0.0)
            return
        if result is not None:
            state.update(sources=result["sources"], result=result["snapshot"], reader=result["reader"], path=result["path"],
                         verified=result["verified"], error="", status="ready" if result["snapshot"] else "missing")
            if result.get("discovered"):
                state["discover_at"] = time.monotonic() + 5.0
            if result["snapshot"] is not None and not result["snapshot"].get("caught_up", True):
                state["next_read"] = time.monotonic() + .02

    if hub.start_task(work, done):
        state.update(callback=done, cancel=event, status="loading" if state["result"] is None else state["status"])


def panel(views, app, job, width):
    """Render published data only, with ordinary Details scrolling/selection."""
    state = initialize(app)
    owner_state = state
    context = _context(app)
    if context is None or state["context"] is None or context["key"] != state["context"]["key"]:
        # A redraw can precede tick after a row change. Never paint the prior
        # attempt's frames under the new job's heading.
        state = dict(state, result=None, error="", status="waiting for job evidence", sources=[])
    signature = (context["key"] if context else None, job.id, width, id(state["result"]), state["status"],
                 state["error"], state["paused"], state["verified"], state["index"], tuple(state["sources"][:128]), views.g.ascii)
    cached = owner_state.get("paint")
    if cached is not None and cached[0] == signature:
        return cached[1]
    controls = [("refresh", "Refresh", ("command", "trace refresh")),
                ("follow", "Follow" if state["paused"] else "Pause view", ("command", "trace follow" if state["paused"] else "trace pause")),
                ("plan", "Capture plan", ("command", "ops trace-capture")),
                ("snapshot", "Python snapshot", ("command", "ops trace-snapshot"))]
    rows, raw_hits = buttons(views.g, width, controls, group="trace", prefix="trace:")
    hits = [(y, "job_panel_action", (("trace_command", {"job": job.id, "command": value["action"][1]}), value["left"], value["right"]))
            for y, _, value in raw_hits]
    rows.append([(f" Trace for job {job.id}  / {state['status']}", "heading")])
    if state["error"]:
        rows.append([(state["error"], "warning")])
    if state["result"] is None:
        rows += [[("No verified recording is attached yet.", "dim")],
                 [("Start the application with tower trace run, or attach its manifest with :trace attach PATH.", "dim")],
                 [("Recording must begin before the events you need; opening this view does not attach a debugger.", "dim")]]
    else:
        if not state["verified"]:
            rows.append([("Explicit source: current Slurm attempt is not verified.", "warning")])
        snapshot = state["result"]
        identity = snapshot.get("identity", {})
        rows.append([(f" {identity.get('node', '?')} / PID {identity.get('pid', '?')} / attempt {identity.get('attempt', '?')}", "cyan")])
        for i, path in enumerate(state["sources"][:128]):
            source_rows, source_hits = buttons(views.g, width, [(str(i), f"Source {i + 1}: {os.path.basename(os.path.dirname(path))}",
                ("command", "trace select " + str(i)))], selected=str(state["index"]), group="trace-sources", prefix="trace-source:")
            hits.extend((len(rows) + y, "job_panel_action", (("trace_command", {"job": job.id, "command": value["action"][1]}), value["left"], value["right"])) for y, _, value in source_hits)
            rows.extend(source_rows)
        for message in snapshot.get("rows", ())[:1600]:
            rows.append([(str(message), "text")])
        for message in snapshot.get("warnings", ())[:32]:
            rows.append([(str(message), "warning")])
        if len(snapshot.get("rows", ())) > 1600:
            rows.append([("Display bounded to 1600 trace lines; inspect the recording for additional data.", "warning")])
    rendered = _wrap(rows, hits, width)
    # Keep the source object alive with the cache: id reuse cannot alias a
    # later snapshot even if a worker replaces the published result.
    owner_state["paint"] = (signature, rendered, state["result"])
    return rendered


def _wrap(rows, hits, width):
    """Cell-aware bounded wrapping; keep control coordinates on their own rows."""
    from .operations import clean
    width = max(1, width)
    control_rows = {y for y, _, _ in hits}
    output, positions = [], {}
    for y, row in enumerate(rows):
        positions[y] = len(output)
        if y in control_rows:
            output.append(L.clip_row(row, width))
        else:
            line, cells = [], 0
            for text, style in row:
                chunk = ""
                for char in clean(text, 8192):
                    size = L.vlen(char)
                    if cells + size > width:
                        if chunk:
                            line.append((chunk, style))
                        output.append(line)
                        line, chunk, cells = [], "", 0
                    # A one-cell terminal cannot hold a wide glyph.
                    if size > width:
                        char, size = "?", 1
                    chunk += char
                    cells += size
                if chunk:
                    line.append((chunk, style))
            output.append(line)
        if len(output) >= 8000:
            output = output[:8000]
            output.append([(L.cut("Display limit reached; inspect the recording for more.", width), "warning")])
            break
    return output, [(positions[y], kind, value) for y, kind, value in hits if y in positions and positions[y] < len(output)]


def handle_key(app, key):
    return False


def handle_mouse(app, y, x, button="left", shift=False):
    return False


def overlay(views, snap, app, width, height):
    return None
