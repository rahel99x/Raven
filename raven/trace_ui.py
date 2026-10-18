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
            index=0, paused=False, verified=False, paint=None, discover_at=0.0, summary=False,
            processes=[], show_helpers=None, process_page=0)
    return app.trace_panel_state


def restore(app, data):
    if isinstance(data, dict) and type(data.get("show_helpers")) is bool:
        initialize(app)["show_helpers"] = data["show_helpers"]


def save(app):
    choice = initialize(app)["show_helpers"]
    return {"show_helpers": choice} if type(choice) is bool else {}


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
    from .raven_execution import router_for
    return dict(key=key, job_id=str(jid), submit=key[1], start=key[2], root=root, cluster=cluster,
                light=router_for(app).managed,
                run_root=binding.get("run_root", ""), binding=dict(binding),
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
        state.update(context=None, result=None, reader=None, sources=[], processes=[], process_page=0, path="", explicit="", status="idle", index=0, error="", verified=False, paint=None, discover_at=0.0)


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
    elif args[1:] in (["summary"], ["events"]):
        state.update(summary=args[1] == "summary", next_read=0.0, paint=None)
        app.say("Trace summaries use matched retained intervals; order and causality remain unanswerable" if state["summary"] else "Original retained trace events")
    elif args[1:] == ["helpers"]:
        state["show_helpers"] = not bool(state["show_helpers"])
        state.update(paint=None, next_read=0.0, process_page=0)
        app.save()
    elif args[1:] in (["processes-next"], ["processes-previous"]):
        state["process_page"] = max(0, state["process_page"] + (1 if args[1] == "processes-next" else -1))
        state["paint"] = None
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
        app.fail("trace [attach MANIFEST|refresh|pause|follow|clear|helpers|select INDEX]")
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


def _helper(process):
    return process.get("role") in {"helper", "dataloader-worker", "pool-worker", "multiprocessing-helper", "resource-tracker"}


def _process(manifest, path):
    from .trace_protocol import validate_identity
    if manifest.get("schema") != "raven.trace/v1":
        raise ValueError("Expected a trace manifest")
    identity = validate_identity(manifest.get("identity"))
    process = {key: identity[key] for key in ("pid", "parent_pid", "role", "launcher", "rank",
               "local_rank", "world_size", "interpreter") if key in identity}
    if "process" in manifest and manifest["process"] != process:
        raise ValueError("Trace process metadata disagrees with its identity")
    for key in ("role", "rank", "pid", "parent_pid", "local_rank", "world_size", "launcher"):
        process.setdefault(key, identity.get(key))
    coverage = manifest.get("coverage", {})
    count = manifest.get("committed_seq")
    count = count if type(count) is int and count >= 0 else None
    reasons = [str(value) for value in manifest.get("warnings", [])[:16]]
    if coverage.get("complete") is False:
        reasons.append("Producer declares incomplete coverage.")
    process.update(path=path, events=count, status="declared", coverage=coverage.get("scope", "undeclared"),
                   reason="; ".join(reasons), helpers_default=coverage.get("helpers_default", False))
    return process


def _launch_sources(context):
    """Read only registered paths for the selected attempt, in worker context."""
    from .trace_protocol import read_json
    from .projects import read_bound_json, relative_path
    root = context.get("run_root")
    if not root:
        return [], []
    path = os.path.join(root, "trace/index.json")
    binding = context.get("binding")
    try:
        value = read_bound_json(binding, path, max_bytes=1 << 20) if binding else read_json(path)
    except FileNotFoundError:
        return [], []
    sources = value.get("sources") if isinstance(value, dict) else None
    if not isinstance(value, dict) or value.get("schema") != "raven.trace-index/v1" or not isinstance(sources, list) or len(sources) > 8192:
        raise ValueError("Invalid bounded run trace index")
    paths, gaps, seen = [], [], set()
    for entry in sources:
        if not isinstance(entry, dict):
            continue
        if entry.get("schema") == "raven.trace/v1":
            path = os.path.join(root, relative_path(entry.get("path")))
            if path not in seen:
                paths.append(path)
                seen.add(path)
        elif entry.get("schema") == "raven.trace-gap/v1" and entry.get("run_id") == context.get("run_id"):
            process = dict(entry.get("process") or {})
            process.update(path="", events=0, status=entry.get("status", "unavailable"),
                           coverage="observed attachment gap", reason=str(entry.get("reason", "Unknown attachment failure")))
            gaps.append(process)
    return paths, gaps


def _read(context, explicit, index, previous_path, reader, event, source_cache=None, process_cache=None):
    from .trace_protocol import TraceReader, discover_trace, read_json
    if context["remote"]:
        raise ValueError("Open Raven on the trace host to inspect its journal; remote views never substitute local files.")
    processes = [] if process_cache is None else [dict(row) for row in process_cache]
    if explicit:
        sources = [explicit]
    elif source_cache is not None:
        sources = source_cache
    elif context["root"] or context.get("run_root"):
        candidates, processes = _launch_sources(context)
        seen = set(candidates)
        if context["root"]:
            candidates += [path for path in discover_trace(context["root"], context["job_id"]) if path not in seen]
        sources = []
        for path in candidates:
            if event.is_set():
                return None
            manifest = read_json(path, limit=65536)
            identity = manifest.get("identity", {})
            # Registered projects may retain the same numeric job ID from
            # different clusters. Ignore other identities during discovery;
            # explicitly attached mismatches still fail visibly below.
            if context["cluster"] and identity.get("cluster") != context["cluster"]:
                continue
            if _matches(identity, context):
                sources.append(path)
                processes.append(_process(manifest, path))
    else:
        sources = []
    if not sources:
        return dict(sources=[], processes=processes, snapshot=None, reader=None, path="", verified=False, discovered=source_cache is None, index=0)
    path = sources[max(0, min(index, len(sources) - 1))]
    manifest = read_json(path, limit=65536)
    show_helpers = context.get("show_helpers")
    if show_helpers is None:
        show_helpers = any(row.get("helpers_default") is True for row in processes)
    if not explicit and not show_helpers and _helper(_process(manifest, path)):
        visible = next((row for row in processes if row.get("path") in sources and not _helper(row)), None)
        if visible is None:
            return dict(sources=sources, processes=processes, snapshot=None, reader=None, path="", verified=False,
                        discovered=source_cache is None, index=index)
        path = visible["path"]
        manifest = read_json(path, limit=65536)
    index = sources.index(path)
    reader = reader if reader is not None and path == previous_path else TraceReader(path)
    snapshot = reader.refresh(max_events=512 if context.get("light") else 4096,
                              max_bytes=256 << 10 if context.get("light") else 2 << 20,
                              max_seconds=.015, cancel=event)
    verified = _matches(snapshot["identity"], context)
    if not verified and not explicit:
        raise ValueError("Trace attempt cannot be verified against the current job.")
    if context.get("binding") and context.get("run_root"):
        from .projects import read_bound_json
        read_bound_json(context["binding"], os.path.join(context["run_root"], "run.json"), max_bytes=65536)
    current = _process(snapshot["manifest"], path)
    current.update(status=snapshot["status"].lower(), checked=snapshot["read_seq"],
                   reason="; ".join(snapshot.get("warnings", [])[:16]))
    processes = [current if row.get("path") == path else row for row in processes]
    if not any(row.get("path") == path for row in processes):
        processes.append(current)
    return dict(sources=sources, processes=processes, snapshot=snapshot, reader=reader, path=path,
                verified=verified, discovered=source_cache is None, index=index)


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
    process_cache = state["processes"] if source_cache is not None else None
    context = dict(context, show_helpers=state["show_helpers"])

    summary_requested = state.get("summary", False)
    def work():
        result = _read(context, explicit, index, path, reader, event, source_cache, process_cache)
        if summary_requested and result is not None and result.get("snapshot") is not None:
            from .trace_summary import summarize, answer
            try:
                summary = summarize(result["snapshot"])
                result["snapshot"]["query_summary"] = [
                    {"phase": group["phase"], "rank": group["rank"], "count": group["count"],
                     "p95": answer(summary, "quantile", phase=group["phase"], rank=group["rank"])}
                    for group in summary["sources"][0]["groups"]]
            except ValueError as exc:
                result["snapshot"]["query_summary_error"] = str(exc)
        return result

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
            state.update(sources=result["sources"], processes=result["processes"], index=result["index"], result=result["snapshot"], reader=result["reader"], path=result["path"],
                         verified=result["verified"], error="", status="ready" if result["snapshot"] else "missing")
            if state["show_helpers"] is None:
                state["show_helpers"] = any(row.get("helpers_default") is True for row in result["processes"])
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
        state = dict(state, result=None, error="", status="waiting for job evidence", sources=[], processes=[])
    signature = (context["key"] if context else None, job.id, width, id(state["result"]), state["status"],
                 state["error"], state["paused"], state.get("summary", False), state["show_helpers"], state["process_page"], id(state["processes"]), state["verified"], state["index"], tuple(state["sources"][:128]), views.g.ascii)
    cached = owner_state.get("paint")
    if cached is not None and cached[0] == signature:
        return cached[1]
    controls = [("refresh", "Refresh", ("command", "trace refresh")),
                ("follow", "Follow" if state["paused"] else "Pause view", ("command", "trace follow" if state["paused"] else "trace pause")),
                ("plan", "Capture plan", ("command", "ops trace-capture")),
                ("snapshot", "Python snapshot", ("command", "ops trace-snapshot"))]
    controls.append(("summary", "Events" if state.get("summary") else "Summaries",
                     ("command", "trace events" if state.get("summary") else "trace summary")))
    controls.append(("helpers", "Hide helpers" if state["show_helpers"] else "Show helpers", ("command", "trace helpers")))
    rows, raw_hits = buttons(views.g, width, controls, group="trace", prefix="trace:")
    hits = [(y, "job_panel_action", (("trace_command", {"job": job.id, "command": value["action"][1]}), value["left"], value["right"]))
            for y, _, value in raw_hits]
    rows.append([(f" Trace for job {job.id}  / {state['status']}", "heading")])
    if state["error"]:
        rows.append([(state["error"], "warning")])
    processes = state.get("processes", [])
    if processes:
        source_indices = {path: index for index, path in enumerate(state["sources"])}
        hidden = sum(_helper(row) for row in processes) if not state["show_helpers"] else 0
        rows.append([(f"Processes: {len(processes)} registered / {hidden} helpers hidden", "heading")])
        visible_processes = [row for row in processes if state["show_helpers"] or not _helper(row)]
        page = min(state["process_page"], max(0, (len(visible_processes)-1) // 32))
        if len(visible_processes) > 32:
            rows.append([(f"Process page {page + 1} / {(len(visible_processes)+31)//32}", "dim")])
            page_controls = []
            if page:
                page_controls.append(("previous", "Previous processes", ("command", "trace processes-previous")))
            if (page+1)*32 < len(visible_processes):
                page_controls.append(("next", "Next processes", ("command", "trace processes-next")))
            page_rows, page_hits = buttons(views.g, width, page_controls, group="trace-pages", prefix="trace-page:")
            hits.extend((len(rows) + y, "job_panel_action", (("trace_command", {"job": job.id, "command": value["action"][1]}), value["left"], value["right"])) for y, _, value in page_hits)
            rows.extend(page_rows)
        rows.append([("Role / rank / PID / events / coverage", "muted")])
        for process in visible_processes[page*32:(page+1)*32]:
            path = process.get("path")
            label = (f"{process.get('role') or 'unknown'} / {process.get('rank') if process.get('rank') is not None else '-'} / "
                     f"{process.get('pid') if process.get('pid') is not None else '?'} / "
                     f"{process.get('events') if process.get('events') is not None else '?'} / {process.get('status', 'unknown')}")
            if path in source_indices:
                index = source_indices[path]
                source_rows, source_hits = buttons(views.g, width, [(str(index), label, ("command", "trace select " + str(index)))],
                    selected=str(state["index"]), group="trace-sources", prefix="trace-source:")
                hits.extend((len(rows) + y, "job_panel_action", (("trace_command", {"job": job.id, "command": value["action"][1]}), value["left"], value["right"])) for y, _, value in source_hits)
                rows.extend(source_rows)
            else:
                rows.append([(label, "warning")])
            if process.get("reason"):
                rows.append([("Why / coverage: " + process["reason"], "warning")])
        rows.append([("Counts come from producer receipts; journal verification advances for the selected process. Undiscovered processes remain unknown.", "dim")])
    if state["result"] is None:
        rows += [[("No verified recording is attached yet.", "dim")],
                 [("Launch with raven launch SCRIPT --trace, or attach its manifest with :trace attach PATH.", "dim")],
                 [("Recording must begin before the events you need; opening this view does not attach a debugger.", "dim")]]
    else:
        if not state["verified"]:
            rows.append([("Explicit source: current Slurm attempt is not verified.", "warning")])
        snapshot = state["result"]
        identity = snapshot.get("identity", {})
        rows.append([(f" {identity.get('node', '?')} / PID {identity.get('pid', '?')} / attempt {identity.get('attempt', '?')}", "cyan")])
        rows.append([(f"Verified events: {snapshot.get('read_seq', 0)} / {snapshot.get('committed_seq', '?')} committed; "
                      f"retained window: {len(snapshot.get('events', []))} events", "dim")])
        if state.get("summary"):
            rows.append([("Experimental: matched retained intervals; emitter-declared rank, unknown when absent.", "warning")])
            rows.append([("P95 bounds measure duration values; no event-order or causal guarantee.", "dim")])
            if snapshot.get("query_summary_error"):
                rows.append([(snapshot["query_summary_error"], "warning")])
            for group in snapshot.get("query_summary", []):
                answer = group['p95']
                interval = str(answer.get('lower', '?')) + '–' + str(answer.get('upper', '?')) + ' ns'
                rows.append([(f"{group['phase']} / rank {group['rank']}: count {group['count']} exact; p95 {interval} ({answer['answerability']})", "text")])
        for message in (() if state.get("summary") else snapshot.get("rows", ())[:1600]):
            rows.append([(str(message), "text")])
        for message in snapshot.get("warnings", ())[:32]:
            rows.append([(str(message), "warning")])
        if len(snapshot.get("rows", ())) > 1600:
            rows.append([("Display bounded to 1600 trace lines; inspect the recording for additional data.", "warning")])
    rendered = _wrap(rows, hits, width)
    # Keep the source object alive with the cache: id reuse cannot alias a
    # later snapshot even if a worker replaces the published result.
    owner_state["paint"] = (signature, rendered, state["result"], state["processes"])
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
