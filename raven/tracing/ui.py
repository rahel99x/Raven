"""Live exact-attempt trace subscription, using the existing background reader.

Paints consume published snapshots. Neither a hover nor a draw opens a trace,
attaches a debugger, or changes recording. Display pause only pauses this reader.
"""

from __future__ import annotations

import os
import threading
import time

from .. import layout as L
from ..control_rows import buttons


def initialize(app):
    if not isinstance(getattr(app, "trace_panel_state", None), dict):
        app.trace_panel_state = dict(
            token=0,
            context=None,
            status="idle",
            error="",
            result=None,
            reader=None,
            callback=None,
            cancel=None,
            next_read=0.0,
            path="",
            explicit="",
            sources=[],
            index=0,
            paused=False,
            verified=False,
            paint=None,
            discover_at=0.0,
            summary=False,
            processes=[],
            show_helpers=None,
            process_page=0,
            view="timeline",
            view_choice=None,
        )
    app.trace_panel_state.setdefault(
        "timeline_view", {"window": None, "thread": None, "selected": None, "query": ""}
    )
    return app.trace_panel_state


def restore(app, data):
    if isinstance(data, dict) and type(data.get("show_helpers")) is bool:
        initialize(app)["show_helpers"] = data["show_helpers"]
    if isinstance(data, dict) and data.get("view") in {
        "timeline",
        "events",
        "summary",
        "workspace",
    }:
        initialize(app).update(
            view=data["view"],
            view_choice=data["view"],
            summary=data["view"] == "summary",
        )
    if isinstance(data, dict) and data.get("track") in {"calls", "cores"}:
        initialize(app)["timeline_view"]["track"] = data["track"]
    if isinstance(data, dict) and isinstance(data.get("workspace"), dict):
        from .workspace_ui import restore_preferences

        restore_preferences(initialize(app), data["workspace"])


def save(app):
    state = initialize(app)
    choice = state["show_helpers"]
    result = {"show_helpers": choice} if type(choice) is bool else {}
    if state.get("view_choice") is not None:
        result["view"] = state["view_choice"]
    if state.get("timeline_view", {}).get("track") in {"calls", "cores"}:
        result["track"] = state["timeline_view"]["track"]
    if "workspace_view" in state:
        from .workspace_ui import save_preferences

        result["workspace"] = save_preferences(state)
    return result


def command_names():
    return ["trace", "traces"]


def _context(app):
    state = initialize(app)
    standalone = state.get("workspace_explicit")
    if standalone and getattr(app, "tab", "") == "traces":
        from ..connection_scope import execution_scope as connection_scope
        from ..execution.policy import router_for

        try:
            scope = connection_scope(app)
        except ValueError:
            return None
        return dict(
            key=("retained-manifest", standalone, repr(scope)),
            job_id="recording",
            standalone=True,
            path=standalone,
            light=router_for(app).managed,
            remote=bool(getattr(getattr(app, "files", None), "remote", False)),
        )
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
    root = (
        (
            binding.get("project_root") or project.get("root")
            if binding
            else details.get("WorkDir")
        )
        or project.get("registered_root")
        or ""
    )
    cfg = getattr(app.cfg, "data", app.cfg)
    cluster = str(cfg.get("cluster_name", "") or details.get("Cluster", ""))
    from ..connection_scope import execution_scope as connection_scope

    try:
        scope = connection_scope(app)
    except ValueError:
        return None
    # This uses only published metadata; no filesystem or scheduler calls.
    key = (
        str(jid),
        str(getattr(record, "submit", "") or ""),
        str(getattr(record, "start", "") or ""),
        app.store.job_attempt(jid),
        root,
        cluster,
        binding.get("run_id", ""),
        repr(scope),
    )
    from ..execution.policy import router_for
    from ..model import Finished

    return dict(
        key=key,
        job_id=str(jid),
        job_finished=isinstance(record, Finished),
        submit=key[1],
        start=key[2],
        root=root,
        cluster=cluster,
        light=router_for(app).managed,
        run_root=binding.get("run_root", ""),
        binding=dict(binding),
        run_id=binding.get("run_id", ""),
        remote=bool(getattr(getattr(app, "files", None), "remote", False)),
    )


def _visible(app):
    return (
        getattr(app, "tab", "") == "traces" and getattr(app, "mode", "") == "main"
    ) or (
        getattr(app, "tab", "") in ("jobs", "history")
        and getattr(app, "mode", "") == "main"
        and getattr(app, "job_panel_state", {}).get("mode") == "trace"
    )


def cancel(app, *, clear=False):
    state = initialize(app)
    from . import browser as trace_browser

    trace_browser.cancel(app)
    state["token"] += 1
    browser_cancel = state.pop("workspace_browser_cancel", None)
    if browser_cancel is not None:
        browser_cancel.set()
    if state["cancel"] is not None:
        state["cancel"].set()
    hub = getattr(app, "research", None)
    if hub is not None and state["callback"] is not None:
        hub.cancel_task(state["callback"])
    state.update(callback=None, cancel=None)
    if clear:
        workspace = state.pop("workspace_session", None)
        if workspace is not None:
            workspace.retire()
        state.pop("workspace_result", None)
        state.pop("workspace_detail", None)
        state.pop("workspace_explicit", None)
        for key in (
            "workspace_functions",
            "workspace_overview",
            "workspace_analysis",
            "workspace_symbol",
            "workspace_symbol_paths",
            "workspace_capacity_request",
        ):
            state.pop(key, None)
        state.update(
            context=None,
            result=None,
            reader=None,
            sources=[],
            processes=[],
            process_page=0,
            path="",
            explicit="",
            status="idle",
            index=0,
            error="",
            verified=False,
            paint=None,
            discover_at=0.0,
            paused=False,
            next_read=0.0,
        )
        from .timeline_ui import reset

        reset(state)


def run_command(app, args):
    if not args or args[0] not in {"trace", "traces"}:
        return False
    state = initialize(app)
    from ..ui.owner_ports import panels

    from .timeline_ui import command as timeline_command

    current = _context(app)
    if args[0] == "traces":
        app.enter_tab("traces")
        state.update(
            view="workspace",
            view_choice="workspace",
            summary=False,
            next_read=0.0,
            paint=None,
        )
        return True
    if args[1:2] == ["workspace"]:
        from .workspace_ui import command as workspace_command

        if args[2:3] == ["open"] and len(args) == 4:
            path = args[3]
            if not path or len(path) > 4096 or not path.isprintable():
                app.fail("Choose a bounded printable retained manifest path.")
                return True
            cancel(app, clear=True)
            app.enter_tab("traces")
            app.selected_id = None
            state.update(
                workspace_explicit=path,
                view="workspace",
                view_choice="workspace",
                next_read=0.0,
            )
            state["context"] = _context(app)
            app.say(
                "Opening retained evidence; scheduler ownership is not inferred from a manifest."
            )
            return True
        if args[2:] == ["runs"]:
            state["workspace_runs_open"] = not state.get("workspace_runs_open", False)
            return True
        if args[2:] in (["run-next"], ["run-previous"]):
            ids = state.get("workspace_run_ids", [])
            if ids:
                selected = getattr(app, "selected_id", None)
                current_index = ids.index(selected) if selected in ids else -1
                target = ids[
                    (current_index + (1 if args[2] == "run-next" else -1)) % len(ids)
                ]
                if app.store.record_context(target)[0] is not None:
                    state.pop("workspace_explicit", None)
                    app.selected_id = target
            return True
        if args[2:3] == ["job"] and len(args) == 4:
            if app.store.record_context(args[3])[0] is None:
                app.fail("Choose a retained job or History record.")
            else:
                state.pop("workspace_explicit", None)
                app.selected_id = args[3]
                state.update(next_read=0.0, paint=None, workspace_runs_open=False)
            return True
        if args[2:3] == ["browser"]:
            _workspace_browser(app, state, current)
            return True
        if args[2:3] == ["symbol"]:
            if len(args) not in (4, 5) or any(
                not value or len(value) > 4096 or not value.isprintable()
                for value in args[3:]
            ):
                app.fail(
                    "Use trace workspace symbol BINARY [SOURCE_ROOT]; '-' selects source proof only."
                )
                return True
            state["workspace_symbol_paths"] = {
                "binary": None if args[3] == "-" else args[3],
                "source_root": args[4] if len(args) == 5 else None,
            }
            state["workspace_revision"] = state.get("workspace_revision", 0) + 1
            state.update(next_read=0.0, paint=None)
            return True
        if args[2:3] == ["capacity"]:
            if (
                len(args) != 4
                or not args[3]
                or len(args[3]) > 4096
                or not args[3].isprintable()
            ):
                app.fail(
                    "Use trace workspace capacity CONTEXT.json, or capacity clear; no allocation is changed."
                )
                return True
            state["workspace_capacity_request"] = (
                None if args[3] == "clear" else args[3],
                state.get("workspace_revision", 0) + 1,
            )
            state["workspace_revision"] = state.get("workspace_revision", 0) + 1
            state.update(next_read=0.0, paint=None)
            return True
        state.update(
            view="workspace", view_choice="workspace", next_read=0.0, paint=None
        )
        if len(args) == 2:
            if getattr(app, "tab", "") not in ("jobs", "history", "traces"):
                app.enter_tab("traces")
            elif getattr(app, "tab", "") != "traces":
                panels(app).activate(app, "trace")
            app.save()
            return True
        workspace_command(app, state, args[2:])
        return True
    if (
        state.get("context") is not None
        and current is not None
        and state["context"]["key"] == current["key"]
        and timeline_command(app, state, args[1:])
    ):
        return True
    if len(args) == 1:
        if getattr(app, "tab", "") not in ("jobs", "history"):
            app.enter_tab("jobs")
        panels(app).activate(app, "trace")
    elif len(args) == 3 and args[1] == "attach":
        if (
            not args[2]
            or len(args[2]) > 4096
            or any(not c.isprintable() for c in args[2])
        ):
            app.fail("Choose a bounded printable trace manifest path.")
        elif _context(app) is None:
            app.fail("Select the exact job before attaching its trace.")
        else:
            cancel(app, clear=True)
            state.update(explicit=args[2], context=_context(app), next_read=0.0)
            panels(app).activate(app, "trace")
    elif args[1:] in (["summary"], ["events"], ["timeline"]):
        if (state.get("result") or {}).get("workspace_catalog_only"):
            state.update(result=None, reader=None)
        state.update(
            summary=args[1] == "summary",
            view=args[1],
            view_choice=args[1],
            next_read=0.0,
            paint=None,
        )
        app.save()
        app.say(
            "Trace summaries use matched retained intervals; order and causality remain unanswerable"
            if state["summary"]
            else "Recorded visual intervals"
            if args[1] == "timeline"
            else "Original retained trace events"
        )
    elif args[1:] == ["browser"]:
        if state.get("view") == "workspace":
            _workspace_browser(app, state, current)
            return True
        if (
            current is None
            or state.get("context") is None
            or current["key"] != state["context"]["key"]
            or state["result"] is None
        ):
            app.say(
                "Select a job and wait for its verified trace before opening a browser timeline."
            )
        else:
            from .browser import request

            request(app, state["result"], current)
    elif args[1:] == ["helpers"]:
        state["show_helpers"] = not bool(state["show_helpers"])
        state.update(paint=None, next_read=0.0, process_page=0)
        app.save()
    elif args[1:] in (["processes-next"], ["processes-previous"]):
        state["process_page"] = max(
            0, state["process_page"] + (1 if args[1] == "processes-next" else -1)
        )
        state["paint"] = None
    elif args[1:] in (["refresh"], ["pause"], ["follow"], ["clear"]):
        operation = args[1]
        if state.get("view") == "workspace" and operation in {"pause", "follow"}:
            from .workspace_ui import command as workspace_command

            workspace_command(
                app, state, ["explore" if operation == "pause" else "live"]
            )
            return True
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
            from .timeline_ui import reset

            reset(state)
        except (TypeError, ValueError):
            app.fail("Choose a visible trace source index.")
    else:
        app.fail(
            "trace [timeline|events|summary|browser|find NAME|attach MANIFEST|refresh|pause|follow|clear|helpers|select INDEX]"
        )
    return True


def _matches(identity, context):
    if identity.get("job_id") != context["job_id"]:
        raise ValueError("Trace belongs to a different job.")
    if context["cluster"] and identity.get("cluster") != context["cluster"]:
        raise ValueError("Trace belongs to a different cluster.")
    # A launch/run identifier cannot override contradictory scheduler evidence.
    # Sources without these optional anchors retain the existing unique-run
    # binding and explicit, unverified retained-recording paths.
    for source_key, selected_key in (
        ("slurm_submit", "submit"),
        ("slurm_start", "start"),
    ):
        recorded, selected = identity.get(source_key), context.get(selected_key)
        if recorded and selected and recorded != selected:
            return False
    if context["run_id"] and (
        identity.get("run_id") == context["run_id"]
        or identity.get("attempt") == context["run_id"]
    ):
        return True
    if context["submit"] and identity.get("slurm_submit") == context["submit"]:
        if context["start"]:
            return identity.get("slurm_start") == context["start"]
        return True
    expected = context["submit"] + ("/" + context["start"] if context["start"] else "")
    return bool(context["submit"] and identity.get("attempt") == expected)


def _helper(process):
    return process.get("role") in {
        "helper",
        "dataloader-worker",
        "pool-worker",
        "multiprocessing-helper",
        "resource-tracker",
    }


def _process(manifest, path):
    from .protocol import validate_identity

    if manifest.get("schema") != "raven.trace/v1":
        raise ValueError("Expected a trace manifest")
    identity = validate_identity(manifest.get("identity"))
    process = {
        key: identity[key]
        for key in (
            "pid",
            "parent_pid",
            "role",
            "launcher",
            "rank",
            "local_rank",
            "world_size",
            "interpreter",
        )
        if key in identity
    }
    if "process" in manifest and manifest["process"] != process:
        raise ValueError("Trace process metadata disagrees with its identity")
    for key in (
        "role",
        "rank",
        "pid",
        "parent_pid",
        "local_rank",
        "world_size",
        "launcher",
    ):
        process.setdefault(key, identity.get(key))
    coverage = manifest.get("coverage", {})
    count = manifest.get("committed_seq")
    count = count if type(count) is int and count >= 0 else None
    reasons = [str(value) for value in manifest.get("warnings", [])[:16]]
    if coverage.get("complete") is False:
        reasons.append("Producer declares incomplete coverage.")
    process.update(
        path=path,
        events=count,
        status="declared",
        coverage=coverage.get("scope", "undeclared"),
        reason="; ".join(reasons),
        helpers_default=coverage.get("helpers_default", False),
    )
    return process


def _launch_sources(context):
    """Read only registered paths for the selected attempt, in worker context."""
    from .protocol import read_json
    from ..projects import read_bound_json, relative_path

    root = context.get("run_root")
    if not root:
        return [], []
    path = os.path.join(root, "trace/index.json")
    binding = context.get("binding")
    try:
        value = (
            read_bound_json(binding, path, max_bytes=1 << 20)
            if binding
            else read_json(path)
        )
    except FileNotFoundError:
        return [], []
    sources = value.get("sources") if isinstance(value, dict) else None
    if (
        not isinstance(value, dict)
        or value.get("schema") != "raven.trace-index/v1"
        or not isinstance(sources, list)
        or len(sources) > 8192
    ):
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
        elif entry.get("schema") == "raven.trace-gap/v1" and entry.get(
            "run_id"
        ) == context.get("run_id"):
            process = dict(entry.get("process") or {})
            process.update(
                path="",
                events=0,
                status=entry.get("status", "unavailable"),
                coverage="observed attachment gap",
                reason=str(entry.get("reason", "Unknown attachment failure")),
            )
            gaps.append(process)
    return paths, gaps


def _read(
    context,
    explicit,
    index,
    previous_path,
    reader,
    event,
    source_cache=None,
    process_cache=None,
):
    from .protocol import TraceReader, discover_trace, read_json

    if context["remote"]:
        raise ValueError(
            "Open Raven on the trace host to inspect its journal; remote views never substitute local files."
        )
    processes = [] if process_cache is None else [dict(row) for row in process_cache]
    if explicit:
        sources = [explicit]
    elif source_cache is not None:
        sources = source_cache
    elif context["root"] or context.get("run_root"):
        candidates, processes = _launch_sources(context)
        seen = set(candidates)
        if context["root"]:
            candidates += [
                path
                for path in discover_trace(context["root"], context["job_id"])
                if path not in seen
            ]
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
        return dict(
            sources=[],
            processes=processes,
            snapshot=None,
            reader=None,
            path="",
            verified=False,
            discovered=source_cache is None,
            index=0,
        )
    path = sources[max(0, min(index, len(sources) - 1))]
    manifest = read_json(path, limit=65536)
    show_helpers = context.get("show_helpers")
    if show_helpers is None:
        show_helpers = any(row.get("helpers_default") is True for row in processes)
    if not explicit and not show_helpers and _helper(_process(manifest, path)):
        visible = next(
            (
                row
                for row in processes
                if row.get("path") in sources and not _helper(row)
            ),
            None,
        )
        if visible is None:
            return dict(
                sources=sources,
                processes=processes,
                snapshot=None,
                reader=None,
                path="",
                verified=False,
                discovered=source_cache is None,
                index=index,
            )
        path = visible["path"]
        manifest = read_json(path, limit=65536)
    index = sources.index(path)
    reader = (
        reader if reader is not None and path == previous_path else TraceReader(path)
    )
    snapshot = reader.refresh(
        max_events=512 if context.get("light") else 4096,
        max_bytes=256 << 10 if context.get("light") else 2 << 20,
        max_seconds=0.015,
        cancel=event,
    )
    if not event.is_set():
        from .timeline import build, index as timeline_index

        try:
            signature = (
                snapshot["read_seq"],
                snapshot["evicted"],
                snapshot["status"],
                snapshot["committed_seq"],
                snapshot["sha256"],
                tuple(snapshot["warnings"]),
                snapshot["covered"],
            )
            prepared = getattr(reader, "_timeline_prepared", None)
            if prepared is None or prepared[0] != signature:
                model = build(snapshot)
                prepared = reader._timeline_prepared = (
                    signature,
                    model,
                    timeline_index(model),
                )
            snapshot["timeline"], snapshot["timeline_index"] = prepared[1:]
        except ValueError as exc:
            snapshot["timeline_error"] = str(exc)
    verified = _matches(snapshot["identity"], context)
    if not verified and not explicit:
        raise ValueError("Trace attempt cannot be verified against the current job.")
    if context.get("binding") and context.get("run_root"):
        from ..projects import read_bound_json

        read_bound_json(
            context["binding"],
            os.path.join(context["run_root"], "run.json"),
            max_bytes=65536,
        )
    current = _process(snapshot["manifest"], path)
    current.update(
        status=snapshot["status"].lower(),
        checked=snapshot["read_seq"],
        reason="; ".join(snapshot.get("warnings", [])[:16]),
    )
    processes = [current if row.get("path") == path else row for row in processes]
    if not any(row.get("path") == path for row in processes):
        processes.append(current)
    return dict(
        sources=sources,
        processes=processes,
        snapshot=snapshot,
        reader=reader,
        path=path,
        verified=verified,
        discovered=source_cache is None,
        index=index,
    )


def _read_workspace(result, context, session, options, event):
    from .workspace_session import WorkspaceSession, expected_identity

    if not (
        result.get("verified") or result.get("association_verified")
    ) and not context.get("standalone"):
        raise ValueError(
            "Run-wide tracing needs an exact verified job-attempt association."
        )
    expected = expected_identity(result["snapshot"])
    paths = tuple(result["sources"])
    created = session is None or session.closed or session.expected != expected
    if created:
        if session is not None:
            session.close()
        session = WorkspaceSession(
            paths, expected=expected, light=context.get("light", False)
        )
    else:
        with session.lock:
            joined = tuple(dict.fromkeys(session.paths + paths))
            if len(joined) > 512:
                raise ValueError(
                    "Run exceeds the 512-source workspace budget; select a smaller source set."
                )
            session.paths = joined
    try:
        capacity_request = options.get("capacity_request")
        if capacity_request is not None and capacity_request != getattr(
            session, "capacity_request", None
        ):
            from .protocol import read_json

            session.set_capacity(
                read_json(capacity_request[0], limit=64 << 10)
                if capacity_request[0]
                else None
            )
            session.capacity_request = capacity_request
        prepared = session.refresh(
            dict(
                options,
                discovery_errors=result.get("discovery_errors", []),
                discovery_complete=bool(result.get("discovered")),
            ),
            light=context.get("light", False),
            cancel=event,
        )
        if event.is_set():
            if created:
                session.close()
            return
    except BaseException:
        if created:
            session.close()
        raise
    by_path = {
        os.path.abspath(source["path"]): source
        for source in prepared.get("sources", [])
        if source.get("path")
    }
    selected = by_path.get(os.path.abspath(result["path"]), {})
    for process in result.get("processes", []):
        source = by_path.get(os.path.abspath(process.get("path") or "."))
        if source:
            process.update(
                status=source.get("status", "INDEXING").lower(),
                checked=source.get("read_seq", 0),
                reason=source.get("error")
                or "; ".join(source.get("warnings", [])[:16]),
            )
    snapshot = result["snapshot"]
    snapshot.update(
        {
            key: selected[key]
            for key in (
                "read_seq",
                "committed_seq",
                "sha256",
                "caught_up",
                "status",
                "warnings",
            )
            if key in selected
        }
    )
    snapshot["covered"] = selected.get("coverage", snapshot.get("covered", {}))
    result["verified"] = bool(
        not context.get("standalone")
        and result.get("association_verified", result.get("verified"))
        and selected.get("validated")
    )
    result["association"] = (
        "declared recording; scheduler association not checked"
        if context.get("standalone")
        else "ready"
        if result["verified"]
        else "waiting for verified journal evidence"
    )
    if result.get("discovery_errors"):
        result["association"] += "; discovery incomplete"
    if selected.get("error"):
        result["association"] += "; selected source " + selected.get(
            "read_status", "unavailable"
        )
    result.update(workspace_session=session, workspace_result=prepared)


def _workspace_browser(app, state, current):
    session = state.get("workspace_session")
    if (
        session is None
        or current is None
        or state.get("context") is None
        or current["key"] != state["context"]["key"]
    ):
        app.say(
            "Open a run workspace and wait for verified sources before opening its live browser."
        )
        return
    from .workspace_browser_effect import request

    request(app, session, current)


def render_global(views, snap, app, width, height):
    """A global destination over published Jobs/History and exact-run state."""
    from ..job_logs.text import display_text

    state = initialize(app)
    separator = " - " if views.g.ascii else " — "
    rows = [
        [(" Trace workspace" + separator + "live and retained run evidence", "heading")]
    ]
    from itertools import chain

    records, seen = [], set()
    for record in chain(snap.get("jobs", ()), snap.get("finished", ())):
        key = str(record.id)
        if key not in seen:
            records.append(record)
            seen.add(key)
            if len(records) == 64:
                break
    state["workspace_run_ids"] = [str(record.id) for record in records]
    if len(snap.get("jobs", ())) + len(snap.get("finished", ())) > 64:
        rows.append(
            [
                (
                    "Showing up to 64 cached runs; use History or open a retained manifest for older runs.",
                    "dim",
                )
            ]
        )
    if not getattr(app, "selected_id", None) and records:
        rows.append([("Choose a running or completed job below.", "dim")])
    run_controls, hits = buttons(
        views.g,
        width,
        [
            ("runs", "Choose run", ("command", "trace workspace runs")),
            ("previous", "Previous", ("command", "trace workspace run-previous")),
            ("next", "Next", ("command", "trace workspace run-next")),
        ],
        group="trace-runs",
        prefix="trace-runs:",
    )
    hits = [(y + len(rows), kind, value) for y, kind, value in hits]
    rows.extend(run_controls)
    choices = [
        (
            str(record.id),
            display_text(str(record.id) + " " + str(record.name))[:96],
            ("command", "trace workspace job " + str(record.id)),
        )
        for record in records
        if state.get("workspace_runs_open") or not getattr(app, "selected_id", None)
    ]
    chooser, chooser_hits = buttons(
        views.g,
        width,
        choices,
        selected=getattr(app, "selected_id", None),
        group="trace-runs",
        prefix="trace-run:",
    )
    hits.extend((y + len(rows), kind, value) for y, kind, value in chooser_hits)
    rows.extend(chooser)
    context = _context(app)
    if context is None:
        rows.extend(
            [
                [("No selected run. Choose a job or open a retained manifest:", "dim")],
                [(":trace workspace open MANIFEST", "text")],
            ]
        )
    else:
        if state.get("context") is None or state["context"]["key"] != context["key"]:
            shown = dict(
                state,
                workspace_result=None,
                workspace_detail=None,
                workspace_functions=None,
            )
        else:
            shown = state
        from .workspace_ui import render

        body, body_hits = render(views, shown, context["job_id"], width)
        rows.append(
            [
                (
                    state.get("error") or state.get("status", "idle"),
                    "warning" if state.get("error") else "dim",
                )
            ]
        )
        for y, kind, value in body_hits:
            if kind == "job_panel_action":
                action, left, right = value
                payload = action[1]
                value = dict(
                    id="trace-global:" + str(y) + ":" + str(left),
                    group="trace-workspace",
                    left=left,
                    right=right,
                    action=("command", payload["command"]),
                )
                kind = "control"
            hits.append((y + len(rows), kind, value))
        rows.extend(body)
        global_controls, global_hits = buttons(
            views.g,
            width,
            [("browser", "Live browser", ("command", "trace workspace browser"))],
            group="trace-global",
            prefix="trace-global:",
        )
        hits.extend((y + len(rows), kind, value) for y, kind, value in global_hits)
        rows.extend(global_controls)
    top = max(
        0, min(app.top.get("traces", 0), max(0, len(rows) - (height or len(rows))))
    )
    visible = rows[top:] if height is None else rows[top : top + max(0, height)]
    return [L.clip_row(row, width) for row in visible], [
        (y - top, kind, value)
        for y, kind, value in hits
        if y >= top and (height is None or y < top + height)
    ]


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
    if (
        state["paused"]
        or state["callback"] is not None
        or time.monotonic() < state["next_read"]
    ):
        return
    from ..background_tasks import research_hub as _worker

    hub = _worker(app, getattr(app, "files", None))
    with hub.lock:
        if (
            hub.closed
            or hub.pending
            or hub.future is not None
            and not hub.future.done()
        ):
            return
    token, event = state["token"], threading.Event()
    explicit, index, path, reader = (
        state["explicit"],
        state["index"],
        state["path"],
        state["reader"],
    )
    source_cache = (
        list(state["sources"]) if time.monotonic() < state["discover_at"] else None
    )
    process_cache = state["processes"] if source_cache is not None else None
    context = dict(context, show_helpers=state["show_helpers"])

    workspace_requested = (
        state.get("view") == "workspace" or getattr(app, "tab", "") == "traces"
    )
    summary_requested = state.get("summary", False) and not workspace_requested
    fallback_sources = list(state["sources"])
    fallback_processes = [dict(row) for row in state["processes"]]
    fallback_snapshot = state.get("result")
    workspace = state.get("workspace_session")
    workspace_revision = state.get("workspace_revision", 0)
    workspace_options = {}
    if workspace_requested:
        from .workspace_ui import (
            initialize as workspace_initialize,
            query_options,
            query_requests,
            detail_request,
            symbol_request,
        )

        view = workspace_initialize(state)
        workspace_options = dict(
            query_options(state),
            perspective=view["projection"],
            mode=view["mode"],
            delta_ns=view["delta_ns"],
            selected=detail_request(state),
            requests=query_requests(state),
            all_clocks=view.get("all_clocks", False),
            functions=view.get("show_functions", False),
            overview=bool(state.get("workspace_dirty")),
            **state.get("workspace_symbol_paths", {}),
            capacity_request=state.get("workspace_capacity_request"),
            job_finished=context.get("job_finished"),
        )
        symbol = symbol_request(state)
        if "frame_index" in symbol:
            workspace_options["sample_frame_index"] = symbol["frame_index"]
        analysis = view.get("analysis_mode", "none")
        if analysis != "none":
            workspace_options[analysis.replace("-", "_")] = True
            if analysis == "causal":
                workspace_options["causal"] = detail_request(state)

    def work():
        if workspace_requested:
            from .workspace_discovery import read as discover

            try:
                result = discover(
                    context,
                    explicit,
                    index,
                    event,
                    source_cache,
                    process_cache,
                    launch_sources=_launch_sources,
                    matches=_matches,
                    process=_process,
                )
            except (OSError, ValueError) as exc:
                if (
                    workspace is None
                    or workspace.closed
                    or not fallback_sources
                    or context.get("standalone")
                    or explicit
                ):
                    raise
                # Continue already registered sources while registry metadata
                # is unavailable. Their journals retain exact-attempt checks.
                result = discover(
                    context,
                    explicit,
                    index,
                    event,
                    fallback_sources,
                    fallback_processes,
                    launch_sources=_launch_sources,
                    matches=_matches,
                    process=_process,
                )
                if result is not None:
                    result.setdefault("discovery_errors", []).append(
                        {
                            "path": context.get("root", "registry"),
                            "read_error": str(exc)[:8192],
                        }
                    )
            if (
                result is not None
                and result.get("snapshot") is None
                and workspace is not None
                and not workspace.closed
                and fallback_sources
                and fallback_snapshot is not None
                and not context.get("standalone")
                and not explicit
                and _matches(fallback_snapshot["identity"], context)
            ):
                # Absence from a temporarily unreadable registry does not
                # withdraw a previously verified exact-attempt subscription.
                result.update(
                    sources=fallback_sources,
                    processes=fallback_processes,
                    snapshot=dict(fallback_snapshot),
                    reader=None,
                    path=path,
                    verified=False,
                    association_verified=True,
                )
                result.setdefault("discovery_errors", []).append(
                    {
                        "path": context.get("root", "registry"),
                        "read_error": "No matching source metadata is currently available; continuing the previously verified source set.",
                    }
                )
        else:
            result = _read(
                context,
                explicit,
                index,
                path,
                reader,
                event,
                source_cache,
                process_cache,
            )
        if (
            workspace_requested
            and result is not None
            and result.get("snapshot") is not None
        ):
            _read_workspace(result, context, workspace, workspace_options, event)
            session = result.get("workspace_session")
            if session is not None and not explicit and not context.get("standalone"):
                from .workspace_discovery import read as discover

                def registered_sources(cancel):
                    return discover(
                        context,
                        "",
                        0,
                        cancel,
                        launch_sources=_launch_sources,
                        matches=_matches,
                        process=_process,
                    )

                session.set_discovery(
                    registered_sources, primed=bool(result.get("discovered"))
                )
        if (
            summary_requested
            and result is not None
            and result.get("snapshot") is not None
        ):
            from .summary import summarize, answer

            try:
                summary = summarize(result["snapshot"])
                result["snapshot"]["query_summary"] = [
                    {
                        "phase": group["phase"],
                        "rank": group["rank"],
                        "count": group["count"],
                        "p95": answer(
                            summary,
                            "quantile",
                            phase=group["phase"],
                            rank=group["rank"],
                        ),
                    }
                    for group in summary["sources"][0]["groups"]
                ]
            except ValueError as exc:
                result["snapshot"]["query_summary_error"] = str(exc)
        return result

    def done(result):
        current = _context(app)
        if (
            event.is_set()
            or state["token"] != token
            or not _visible(app)
            or current is None
            or current["key"] != context["key"]
        ):
            if isinstance(result, dict) and result.get("workspace_session") is not None:
                abandoned = result["workspace_session"]
                if abandoned is not state.get("workspace_session"):
                    abandoned.retire()
            return
        state.update(callback=None, cancel=None, next_read=time.monotonic() + 0.5)
        if workspace_requested and workspace_revision != state.get(
            "workspace_revision", 0
        ):
            if isinstance(result, dict):
                pending_session = result.get("workspace_session")
                if pending_session is not None and pending_session is not state.get(
                    "workspace_session"
                ):
                    # Keep newly ingested evidence, but never publish an old
                    # viewport/selection after a newer user command.
                    state["workspace_session"] = pending_session
            state["next_read"] = 0.0
            return
        if isinstance(result, Exception):
            from ..notifications import message_text

            state.update(
                status="stale" if state.get("workspace_result") else "error",
                error=message_text(result)[0],
                discover_at=0.0,
            )
            _workspace_notice(app, context, state["error"])
            if not workspace_requested:
                state["result"] = None
            return
        if result is not None:
            if result.get("workspace_session") is not None:
                state["workspace_session"] = result["workspace_session"]
                from .workspace_ui import publish

                prepared = result["workspace_result"]
                publish(state, prepared)
                _workspace_source_notices(app, context, prepared)
                state["workspace_detail"] = prepared.get("selected_detail")
                state["workspace_functions"] = prepared.get("functions")
                state["workspace_overview"] = prepared.get("overview")
                if prepared.get("symbol") is not None:
                    state["workspace_symbol"] = {
                        "event_id": workspace_options.get("selected"),
                        "report": prepared["symbol"],
                    }
                analysis = state.get("workspace_view", {}).get("analysis_mode", "none")
                state["workspace_analysis"] = {
                    "mode": analysis,
                    "report": prepared.get(analysis.replace("-", "_")),
                }
                state["workspace_dirty"] = False
            previous_stream = (
                (state.get("result") or {}).get("identity", {}).get("stream")
            )
            current_stream = (
                (result.get("snapshot") or {}).get("identity", {}).get("stream")
            )
            if not workspace_requested and (
                previous_stream != current_stream
                or state.get("path") != result.get("path")
            ):
                from .timeline_ui import reset

                reset(state)
            state.update(
                sources=result["sources"],
                processes=result["processes"],
                index=result["index"],
                result=result["snapshot"],
                reader=result["reader"],
                path=result["path"],
                verified=result["verified"],
                error="",
                status=(
                    result.get("association", "ready")
                    if result["snapshot"]
                    else "missing"
                ),
            )
            if result["snapshot"] is not None:
                _workspace_notice(app, context, "")
            if state["show_helpers"] is None:
                state["show_helpers"] = any(
                    row.get("helpers_default") is True for row in result["processes"]
                )
            if result.get("discovered"):
                state["discover_at"] = time.monotonic() + 5.0
            if (
                not workspace_requested
                and result["snapshot"] is not None
                and not result["snapshot"].get("caught_up", True)
            ):
                state["next_read"] = time.monotonic() + 0.02
            if workspace_requested and _workspace_pending(
                result.get("workspace_result") or {}
            ):
                state["next_read"] = time.monotonic() + 0.05

    if hub.start_task(work, done):
        state.update(
            callback=done,
            cancel=event,
            status="loading" if state["result"] is None else state["status"],
        )


def _workspace_notice(app, context, error):
    """Retain the actual read error once, with normal-cadence recovery evidence."""
    from ..notifications import initialize as center_for

    center = center_for(app)
    key = ("trace-workspace", context["key"])
    binding = getattr(app, "project_state", {}).get("binding") or {}
    run = ()
    if not context.get("standalone") and binding.get("job_id") == context["job_id"]:
        from ..run_home import identity

        run = identity(binding)
    if error:
        temporary = any(
            word in error.casefold()
            for word in ("retry later", "uncommitted", "temporarily")
        )
        center.condition(
            key,
            error,
            level="mild" if temporary else "warning",
            source="Trace workspace",
            job="" if context.get("standalone") else context["job_id"],
            setting="recordings",
            run=run,
            observed_at=time.time(),
        )
    else:
        center.resolve_condition(
            key,
            "A newer trace read verified the available committed evidence.",
            observed_at=time.time(),
        )


def _workspace_pending(result):
    """Catch up actual queued pages, rather than spin on a quarantined source."""
    if result.get("index", {}).get("state") == "storage-blocked":
        return False
    if result.get("discovery", {}).get("pending_count", 0):
        return True
    return any(
        source.get("status") != "INVALID"
        and not source.get("error")
        and type(source.get("read_seq")) is int
        and type(source.get("committed_seq")) is int
        and source["read_seq"] < source["committed_seq"]
        for source in result.get("sources", [])
    )


def _workspace_source_notices(app, context, result):
    """Full read errors reach the inbox even when other sources remain usable."""
    from ..notifications import initialize as center_for

    center = center_for(app)
    rows = [
        (source.get("id", "unknown"), source.get("error", ""))
        for source in result.get("sources", [])
    ]
    rows.append(("index", result.get("index", {}).get("error", "")))
    collection = result.get("catalog", result)
    errors = result.get("discovery_errors", [])
    collection_error = "\n".join(
        (row.get("path", "") + ": " if row.get("path") else "")
        + row.get("read_error", "")
        for row in errors
    ) or collection.get("collection_error", "")
    rows.append(("collection", collection_error))
    for source_id, error in rows:
        key = ("trace-workspace-source", context["key"], source_id)
        if error:
            center.condition(
                key,
                error,
                level="warning",
                source="Trace source " + str(source_id),
                job="" if context.get("standalone") else context["job_id"],
                setting="recordings",
            )
        else:
            center.resolve_condition(
                key,
                "A newer verified source/index observation cleared the read error.",
                observed_at=time.time(),
            )


def panel(views, app, job, width):
    """Render published data only, with ordinary Details scrolling/selection."""
    state = initialize(app)
    owner_state = state
    context = _context(app)
    if (
        context is None
        or state["context"] is None
        or context["key"] != state["context"]["key"]
    ):
        # A redraw can precede tick after a row change. Never paint the prior
        # attempt's frames under the new job's heading.
        state = dict(
            state,
            result=None,
            error="",
            status="waiting for job evidence",
            sources=[],
            processes=[],
        )
    browser = getattr(app, "trace_browser_state", {})
    browser = (
        browser
        if context is not None and browser.get("context") == context["key"]
        else {}
    )
    signature = (
        context["key"] if context else None,
        job.id,
        width,
        id(state["result"]),
        state["status"],
        state["error"],
        state["paused"],
        state.get("summary", False),
        state.get("view", "timeline"),
        tuple(sorted(state.get("timeline_view", {}).items())),
        id(state.get("workspace_result")),
        repr(state.get("workspace_view", {})),
        browser.get("pending", False),
        browser.get("message", ""),
        browser.get("path", ""),
        state["show_helpers"],
        state["process_page"],
        id(state["processes"]),
        state["verified"],
        state["index"],
        tuple(state["sources"][:128]),
        views.g.ascii,
    )
    cached = owner_state.get("paint")
    if cached is not None and cached[0] == signature:
        return cached[1]
    controls = [
        ("workspace", "Run workspace", ("command", "trace workspace")),
        ("timeline", "Timeline", ("command", "trace timeline")),
        ("events", "Stacks", ("command", "trace events")),
        ("summary", "Summaries", ("command", "trace summary")),
        ("browser", "View in browser", ("command", "trace browser")),
        (
            "follow",
            "Follow" if state["paused"] else "Pause view",
            ("command", "trace follow" if state["paused"] else "trace pause"),
        ),
    ]
    if state.get("view") == "workspace":
        controls = [
            ("timeline", "Single-source view", ("command", "trace timeline")),
            ("browser", "Live browser", ("command", "trace workspace browser")),
            ("plan", "Capture plan", ("command", "ops trace-capture")),
        ]
    elif state.get("view", "timeline") != "timeline":
        controls += [
            ("refresh", "Refresh", ("command", "trace refresh")),
            ("plan", "Capture plan", ("command", "ops trace-capture")),
            ("snapshot", "Python snapshot", ("command", "ops trace-snapshot")),
        ]
    if state.get("view") != "workspace":
        controls.append(
            (
                "helpers",
                "Hide helpers" if state["show_helpers"] else "Show helpers",
                ("command", "trace helpers"),
            )
        )
    rows, raw_hits = buttons(
        views.g,
        width,
        controls,
        selected=state.get("view", "timeline"),
        group="trace",
        prefix="trace:",
    )
    hits = [
        (
            y,
            "job_panel_action",
            (
                ("trace_command", {"job": job.id, "command": value["action"][1]}),
                value["left"],
                value["right"],
            ),
        )
        for y, _, value in raw_hits
    ]
    rows.append([(f" Trace for job {job.id}  / {state['status']}", "heading")])
    if state["error"]:
        rows.append([(state["error"], "warning")])
    if browser.get("pending"):
        rows.append([("Preparing the private browser timeline…", "dim")])
    elif browser.get("message"):
        rows.append([(browser["message"], "cyan")])
        if browser.get("path"):
            rows.append([("Browser snapshot: " + browser["path"], "text")])
    processes = state.get("processes", [])
    process_start = len(rows)
    if processes and state.get("view") != "workspace":
        source_indices = {path: index for index, path in enumerate(state["sources"])}
        hidden = (
            sum(_helper(row) for row in processes) if not state["show_helpers"] else 0
        )
        rows.append(
            [
                (
                    f"Processes: {len(processes)} registered / {hidden} helpers hidden",
                    "heading",
                )
            ]
        )
        visible_processes = [
            row for row in processes if state["show_helpers"] or not _helper(row)
        ]
        page = min(state["process_page"], max(0, (len(visible_processes) - 1) // 32))
        if len(visible_processes) > 32:
            rows.append(
                [
                    (
                        f"Process page {page + 1} / {(len(visible_processes) + 31) // 32}",
                        "dim",
                    )
                ]
            )
            page_controls = []
            if page:
                page_controls.append(
                    (
                        "previous",
                        "Previous processes",
                        ("command", "trace processes-previous"),
                    )
                )
            if (page + 1) * 32 < len(visible_processes):
                page_controls.append(
                    ("next", "Next processes", ("command", "trace processes-next"))
                )
            page_rows, page_hits = buttons(
                views.g, width, page_controls, group="trace-pages", prefix="trace-page:"
            )
            hits.extend(
                (
                    len(rows) + y,
                    "job_panel_action",
                    (
                        (
                            "trace_command",
                            {"job": job.id, "command": value["action"][1]},
                        ),
                        value["left"],
                        value["right"],
                    ),
                )
                for y, _, value in page_hits
            )
            rows.extend(page_rows)
        rows.append([("Role / rank / PID / events / coverage", "muted")])
        for process in visible_processes[page * 32 : (page + 1) * 32]:
            path = process.get("path")
            label = (
                f"{process.get('role') or 'unknown'} / {process.get('rank') if process.get('rank') is not None else '-'} / "
                f"{process.get('pid') if process.get('pid') is not None else '?'} / "
                f"{process.get('events') if process.get('events') is not None else '?'} / {process.get('status', 'unknown')}"
            )
            if path in source_indices:
                index = source_indices[path]
                source_rows, source_hits = buttons(
                    views.g,
                    width,
                    [(str(index), label, ("command", "trace select " + str(index)))],
                    selected=str(state["index"]),
                    group="trace-sources",
                    prefix="trace-source:",
                )
                hits.extend(
                    (
                        len(rows) + y,
                        "job_panel_action",
                        (
                            (
                                "trace_command",
                                {"job": job.id, "command": value["action"][1]},
                            ),
                            value["left"],
                            value["right"],
                        ),
                    )
                    for y, _, value in source_hits
                )
                rows.extend(source_rows)
            else:
                rows.append([(label, "warning")])
            if process.get("reason"):
                rows.append([("Why / coverage: " + process["reason"], "warning")])
        rows.append(
            [
                (
                    "Counts come from producer receipts; journal verification advances for the selected process. Undiscovered processes remain unknown.",
                    "dim",
                )
            ]
        )
    process_end = len(rows)
    if state["result"] is None:
        rows += [
            [("No verified recording is attached yet.", "dim")],
            [
                (
                    "Launch with raven launch SCRIPT --trace, or attach its manifest with :trace attach PATH.",
                    "dim",
                )
            ],
            [
                (
                    "Recording must begin before the events you need; opening this view does not attach a debugger.",
                    "dim",
                )
            ],
        ]
    elif state.get("view") == "workspace":
        from .workspace_ui import render

        workspace_rows, workspace_hits = render(views, state, job.id, width)
        hits.extend((len(rows) + y, kind, value) for y, kind, value in workspace_hits)
        rows.extend(workspace_rows)
    else:
        if not state["verified"]:
            rows.append(
                [("Explicit source: current Slurm attempt is not verified.", "warning")]
            )
        snapshot = state["result"]
        identity = snapshot.get("identity", {})
        rows.append(
            [
                (
                    f" {identity.get('node', '?')} / PID {identity.get('pid', '?')} / attempt {identity.get('attempt', '?')}",
                    "cyan",
                )
            ]
        )
        rows.append(
            [
                (
                    f"Verified events: {snapshot.get('read_seq', 0)} / {snapshot.get('committed_seq', '?')} committed; "
                    f"retained window: {len(snapshot.get('events', []))} events",
                    "dim",
                )
            ]
        )
        if state.get("summary"):
            rows.append(
                [
                    (
                        "Experimental: matched retained intervals; emitter-declared rank, unknown when absent.",
                        "warning",
                    )
                ]
            )
            rows.append(
                [
                    (
                        "P95 bounds measure duration values; no event-order or causal guarantee.",
                        "dim",
                    )
                ]
            )
            if snapshot.get("query_summary_error"):
                rows.append([(snapshot["query_summary_error"], "warning")])
            for group in snapshot.get("query_summary", []):
                answer = group["p95"]
                interval = (
                    str(answer.get("lower", "?"))
                    + "–"
                    + str(answer.get("upper", "?"))
                    + " ns"
                )
                rows.append(
                    [
                        (
                            f"{group['phase']} / rank {group['rank']}: count {group['count']} exact; p95 {interval} ({answer['answerability']})",
                            "text",
                        )
                    ]
                )
        elif state.get("view", "timeline") == "timeline" and "timeline" in snapshot:
            from .timeline_ui import render

            timeline_rows, timeline_hits = render(views, state, job.id, width)
            hits.extend(
                (len(rows) + y, kind, value) for y, kind, value in timeline_hits
            )
            rows.extend(timeline_rows)
        if snapshot.get("timeline_error"):
            rows.append([(snapshot["timeline_error"], "warning")])
        for message in (
            ()
            if state.get("summary")
            or state.get("view", "timeline") == "timeline"
            and snapshot.get("timeline", {}).get("intervals")
            else snapshot.get("rows", ())[:1600]
        ):
            rows.append([(str(message), "text")])
        for message in snapshot.get("warnings", ())[:32]:
            rows.append([(str(message), "warning")])
        if len(snapshot.get("rows", ())) > 1600:
            rows.append(
                [
                    (
                        "Display bounded to 1600 trace lines; inspect the recording for additional data.",
                        "warning",
                    )
                ]
            )
    if (
        state.get("view", "timeline") == "timeline"
        and (state.get("result") or {}).get("timeline")
        and process_end > process_start
    ):
        # Keep the chart above the potentially long process catalog. Process
        # switching remains an ordinary exact-source control below the chart.
        count = process_end - process_start
        rows = (
            rows[:process_start] + rows[process_end:] + rows[process_start:process_end]
        )
        hits = [
            (
                y
                if y < process_start
                else len(rows) - count + y - process_start
                if y < process_end
                else y - count,
                kind,
                value,
            )
            for y, kind, value in hits
        ]
    rendered = _wrap(rows, hits, width)
    # Keep the source object alive with the cache: id reuse cannot alias a
    # later snapshot even if a worker replaces the published result.
    owner_state["paint"] = (signature, rendered, state["result"], state["processes"])
    return rendered


def _wrap(rows, hits, width):
    """Cell-aware bounded wrapping; keep control coordinates on their own rows."""
    from ..operations import clean

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
            output.append(
                [
                    (
                        L.cut(
                            "Display limit reached; inspect the recording for more.",
                            width,
                        ),
                        "warning",
                    )
                ]
            )
            break
    return output, [
        (positions[y], kind, value)
        for y, kind, value in hits
        if y in positions and positions[y] < len(output)
    ]


def handle_key(app, key):
    panels = getattr(app, "job_panel_state", {})
    state = initialize(app)
    if _visible(app) and state.get("view") == "workspace":
        if getattr(app, "tab", "") != "traces" and panels.get("focus") != "content":
            return False
        action = {
            "+": "zoom-in",
            "=": "zoom-in",
            "-": "zoom-out",
            "left": "pan-left",
            "right": "pan-right",
            "f": "fit",
            "n": "next",
            "N": "previous",
            "[": "parent",
            "]": "child",
            "p": "pin",
        }.get(key)
        if action:
            run_command(app, ["trace", "workspace", action])
            return True
    if (
        not _visible(app)
        or panels.get("focus") != "content"
        or initialize(app).get("view", "timeline") != "timeline"
    ):
        return False
    from ..ui.workspace_state import initialize as layout_state

    if layout_state(app).focus != "details":
        return False
    action = {
        "+": "zoom-in",
        "=": "zoom-in",
        "-": "zoom-out",
        "left": "pan-left",
        "right": "pan-right",
        "f": "fit",
        "t": "thread-next",
        "n": "next",
        "N": "previous",
    }.get(key)
    if action is None:
        return False
    run_command(app, ["trace", action])
    return True


def handle_mouse(app, y, x, button="left", shift=False):
    return False


def overlay(views, snap, app, width, height):
    return None


def shutdown(app):
    state = initialize(app)
    session = state.pop("workspace_session", None)
    cancel(app, clear=True)
    if session is not None:
        session.close()
