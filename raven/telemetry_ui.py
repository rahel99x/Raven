"""Pinned, read-only sampling inspector using the shared background lane."""
from __future__ import annotations

from dataclasses import asdict, is_dataclass

from . import layout as L
from . import telemetry


def initialize(app):
    if not isinstance(getattr(app, "telemetry_state", None), dict):
        app.telemetry_state = {"inspector": telemetry.Inspector(), "generation": 0,
            "job_id": "", "scope": None, "attempt": {}, "report": None, "lines": [],
            "running": False, "error": "", "cursor": 0, "anchor": None, "row_selected": False,
            "top": 0, "page": 1, "focus": "lines", "button": 0,
            "hits": [], "control_hits": [], "line_hits": [], "render_generation": None,
            "return_mode": "main", "wrapped": None}
    return app.telemetry_state


def command_names():
    return ["telemetry"]


def _scope(app):
    slurm = getattr(getattr(app, "actions", None), "slurm", None)
    backend = getattr(slurm, "b", None)
    cfg = getattr(app, "cfg", {})
    current, chain, seen = backend, [], set()
    for _ in range(8):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        chain.append((id(current), str(getattr(current, "host", ""))))
        current = getattr(current, "inner", None)
    return (tuple(chain), str(getattr(slurm, "user", "")),
            str(cfg.get("host", "")), str(getattr(app, "profile_name", ""))), backend


def _job(app, job_id):
    resolve = getattr(app, "job_record", None)
    return resolve(job_id) if callable(resolve) and job_id else None


def _attempt(record):
    return {key: telemetry.clean(getattr(record, key, ""), 256)
            for key in ("submit", "start", "state", "cluster")} if record else {}


def _same_attempt(before, after):
    for key in ("submit", "cluster"):
        if before.get(key) and before[key] != after.get(key):
            return False
    if before.get("state") not in ("PENDING", "CONFIGURING", "") and before.get("start"):
        return before["start"] == after.get("start")
    # Transition from pending to running changes the sample's target context.
    if before.get("state") in ("PENDING", "CONFIGURING"):
        return before.get("state") == after.get("state")
    return True


def _known_evidence(app, job_id, attempt):
    """Read small published metadata; never copy graphs or trigger sources."""
    sampler = getattr(app, "sampler", None)
    intervals = {}
    for source in ("live", "gpu", "trace"):
        try:
            if sampler is not None:
                identity = "|".join(attempt.get(k, "") for k in ("submit", "start"))
                intervals[source] = (sampler.sampling_interval(source, job_id, identity)
                                     if job_id else sampler.effective_interval(source))
        except (AttributeError, TypeError, ValueError, OverflowError):
            pass
    hub = getattr(app, "research", None)
    if hub is not None:
        try:
            # No selected-source assumption: this is the baseline reader rate.
            intervals["research"] = hub.refresh_interval()
        except (AttributeError, TypeError, ValueError, OverflowError):
            pass
    health, live, gpu = {}, None, None
    store = getattr(app, "store", None)
    if store is not None:
        with store.lock:
            for name in ("live", "gpu", "trace"):
                value = store.health.get(name)
                if is_dataclass(value):
                    health[name] = asdict(value)
                elif isinstance(value, dict):
                    health[name] = dict(value)
            observation = store.live.get(job_id)
            if observation is not None:
                live = {"timestamp": getattr(observation, "t", None)}
            devices = store.gpu.get(job_id)
            if devices:
                gpu = {"devices": len(devices)}
    return {"intervals": intervals, "health": health,
            "gpu_enabled": getattr(sampler, "gpu_sampling", None),
            "gpu_observation": gpu, "live_observation": live}


def open_inspector(app, job_id=None, refresh=False):
    """Open exact selected job evidence, or cluster capabilities if unselected."""
    state = initialize(app)
    if job_id is None:
        # Refresh stays pinned even if the underlying workspace selection moves.
        job_id = state["job_id"] if refresh and app.mode == "telemetry" else getattr(app, "selected_id", "") or ""
    try:
        job_id = telemetry.validate_job_id(job_id)
    except ValueError as exc:
        app.fail(str(exc))
        return False
    scope, backend = _scope(app)
    if backend is None:
        app.fail("Telemetry inspection needs a Slurm connection.")
        return False
    if getattr(app, "replay", None):
        app.fail("Telemetry inspection is unavailable during replay; use recorded Sources evidence.")
        return False
    hub = getattr(app, "research", None)
    if hub is None:
        app.fail("The background reader is unavailable in this session.")
        return False
    # Duplicate button/key activation never queues another scheduler request.
    if state["running"] and state["job_id"] == job_id and state["scope"] == scope:
        app.say("Telemetry inspection is already running.")
        return True
    if app.mode != "telemetry":
        state["return_mode"] = "details" if app.mode == "details" else "main"
    state["generation"] += 1
    generation = state["generation"]
    expected = _attempt(_job(app, job_id))
    state.update(job_id=job_id, attempt=expected, scope=scope, running=False, report=None,
                 lines=[], error="", cursor=0, anchor=None, row_selected=False, top=0, focus="lines", button=0,
                 hits=[], control_hits=[], line_hits=[], wrapped=None, render_generation=None)
    app.mode = "telemetry"
    evidence = _known_evidence(app, job_id, expected)

    def read():
        return state["inspector"].inspect(backend, scope=scope, job_id=job_id,
            expected=expected, refresh=refresh, **evidence)

    def completed(value):
        # The shared hub invokes this on the UI thread. Never navigate or
        # overwrite another modal, another connection, or a reused job ID.
        if generation != state["generation"] or state["scope"] != scope:
            return
        state["running"] = False
        if _scope(app)[0] != scope:
            state["error"] = "Connection changed during inspection. Reopen Telemetry in the current connection."
            return
        current = _attempt(_job(app, job_id))
        if expected and current and not _same_attempt(expected, current):
            state["error"] = "Job attempt changed during inspection. Refresh to inspect the new attempt."
            return
        if isinstance(value, Exception):
            state["error"] = "Telemetry inspection failed: " + telemetry.clean(value)
            return
        if not isinstance(value, dict) or value.get("schema") != telemetry.SCHEMA:
            state["error"] = "Telemetry inspection returned invalid evidence."
            return
        # Configuration can establish a cluster absent from ordinary squeue
        # records. Require the already-known record fields to match the read;
        # newly discovered evidence must not turn an unknown into a conflict.
        # expected -> current above still rejects loss/change of a previously
        # captured identity, and the connection scope remains authoritative.
        if current and not _same_attempt(current, value.get("attempt", {})):
            state["error"] = "The scheduler evidence does not match the current job attempt. Refresh to inspect again."
            return
        state["report"], state["lines"] = value, telemetry.report_lines(value)
        state["wrapped"] = None

    state["running"] = True
    if not getattr(app, "interactive", True):
        # --run has no UI loop to publish futures. Execute the same bounded
        # read directly and expose the complete structured diagnostic report.
        try:
            value = read()
        except Exception as exc:
            value = exc
        completed(value)
        app.research_result = state["report"] or {"schema": telemetry.SCHEMA,
            "status": "unavailable", "job_id": job_id, "errors": [state["error"]]}
        app.command_ok = app.research_result["status"] != "unavailable"
    elif not hub.start_task(read, completed):
        state["running"] = False
        state["error"] = "The shared background reader is busy. Select Refresh after it finishes."
    return True


def run_command(app, args):
    if not args or args[0] != "telemetry":
        return False
    if len(args) > 2:
        app.fail("Use telemetry [JOBID|refresh|copy|close].")
    elif len(args) == 2 and args[1] in ("copy", "close"):
        if app.mode == "telemetry":
            _copy(app, all_report=True) if args[1] == "copy" else _close(app)
    elif len(args) == 2 and args[1] == "refresh":
        open_inspector(app, refresh=True)
    else:
        open_inspector(app, args[1] if len(args) == 2 else None)
    return True


def _close(app):
    state = initialize(app)
    state["generation"] += 1
    state.update(running=False, hits=[], control_hits=[], line_hits=[], render_generation=None)
    app.mode = state["return_mode"]


def _copy(app, *, all_report=False):
    from . import clipboard
    state = initialize(app)
    if not state["lines"]:
        app.fail("No telemetry evidence is available to copy yet.")
        return
    if all_report or state["anchor"] is None and not state.get("row_selected"):
        lines = state["lines"]
    else:
        anchor = state["cursor"] if state["anchor"] is None else state["anchor"]
        first, last = sorted((anchor, state["cursor"]))
        lines = state["lines"][first:last + 1]
    app.say(clipboard.copy_for_app(app, "\n".join(lines) + "\n", app.state_dir, **clipboard.options(app)))


def _activate(app, index):
    if index == 0:
        open_inspector(app, initialize(app)["job_id"], refresh=True)
    elif index == 1:
        _copy(app, all_report=True)
    else:
        _close(app)


def handle_key(app, key):
    if app.mode != "telemetry":
        return False
    state = initialize(app)
    if key in ("esc", "q"):
        _close(app)
    elif key == ":":
        return False
    elif key == "r":
        _activate(app, 0)
    elif key == "y":
        _copy(app)
    elif key == "Y":
        _copy(app, all_report=True)
    elif key in ("tab", "btab"):
        state["focus"] = "buttons" if state["focus"] == "lines" else "lines"
    elif key in ("left", "right"):
        state["focus"] = "buttons"
        state["button"] = (state["button"] + (1 if key == "right" else -1)) % 3
    elif key in ("enter", "space") and state["focus"] == "buttons":
        _activate(app, state["button"])
    elif key == "v" and state["lines"]:
        state["anchor"] = state["cursor"] if state["anchor"] is None else None
        state["focus"] = "lines"
    elif key in ("up", "down", "pgup", "pgdn", "home", "end"):
        from . import scrollbars, scrolling
        scrollbars.resume(app, "modal:telemetry")
        scrolling.note_input(app, "key")
        state["focus"] = "lines"
        last = max(0, len(state["lines"]) - 1)
        delta = {"up": -1, "down": 1, "pgup": -state["page"], "pgdn": state["page"]}
        state["cursor"] = (0 if key == "home" else last if key == "end" else
                           max(0, min(last, state["cursor"] + delta[key])))
        state["row_selected"] = bool(state["lines"])
    return True


def handle_mouse(app, y, x, button="left", shift=False):
    if app.mode != "telemetry":
        return False
    state = initialize(app)
    if button == "right":
        state["anchor"] = None
        state["row_selected"] = False
        return True
    if state["render_generation"] != state["generation"]:
        return True
    if button in ("wheel-up", "wheel-down"):
        from . import scrolling
        scrolling.note_input(app, "wheel")
        pointer = getattr(app, "interaction_state", None)
        if isinstance(pointer, dict):
            pointer.update(active=False, focused=None)
        state["top"] = max(0, state["top"] + (3 if button == "wheel-down" else -3))
        return True
    if button not in ("left", "press"):
        return True
    for hy, left, right, index in state["hits"]:
        if y == hy and left <= x < right:
            state["focus"], state["button"] = "buttons", index
            _activate(app, index)
            return True
    for hy, left, right, index in state["line_hits"]:
        if y == hy and left <= x < right:
            from .text_selection import clear
            clear(app)
            pointer = getattr(app, "interaction_state", None)
            if isinstance(pointer, dict):
                pointer.update(active=False, focused=None)
            if shift and state["anchor"] is None:
                state["anchor"] = state["cursor"]
            elif not shift:
                state["anchor"] = None
            state["cursor"], state["focus"] = index, "lines"
            state["row_selected"] = True
            return True
    # Every press/motion/release is consumed while the modal is visible.
    return True


def _wrapped_lines(state, width, ascii_):
    """Wrap once per result/width, retaining original evidence-line identity."""
    from .research_text import clean
    identity = (state["generation"], id(state["lines"]), width, ascii_)
    if state["wrapped"] and state["wrapped"][0] == identity:
        return state["wrapped"][1]
    result = []
    for index, line in enumerate(state["lines"]):
        text = clean(line, ascii_, limit=2048)
        if not text:
            result.append((index, ""))
        while text:
            part = L.truncate(text, width)
            if not part:
                part = "?"
                text = text[1:]
            else:
                text = text[len(part):]
            result.append((index, part))
    state["wrapped"] = (identity, result)
    return result


def overlay(views, snap, app, width, height):
    state = initialize(app)
    state["hits"], state["control_hits"], state["line_hits"] = [], [], []
    state["render_generation"] = None
    if app.mode != "telemetry":
        return None
    from . import modal_scrollbars as B
    from .control_rows import buttons, place_hits
    # Content and all hitboxes come exclusively from the current published
    # report. No scheduler, file, or worker operation occurs while painting.
    content_width = max(1, width - 8)
    labels = ("R Refresh", "Y Copy", "Esc Close") if width >= 42 else ("R", "Y", "Esc")
    identifiers = ("refresh", "copy", "close")
    controls, control_hits = buttons(views.g, content_width,
        [(ident, label, ("command", "telemetry " + ident)) for ident, label in zip(identifiers, labels)],
        selected=identifiers[state["button"]] if state["focus"] == "buttons" else None,
        group="telemetry", prefix="telemetry:")
    wrapped = _wrapped_lines(state, content_width, views.g.ascii)
    page = state["page"] = max(1, height - len(controls) - 7)
    context = ("telemetry", state["generation"], width)
    focus = next((i for i, (index, _) in enumerate(wrapped) if index == state["cursor"]), 0)
    target = state["top"]
    previous = state.get("focus_seen")
    if previous != (state["generation"], state["cursor"]):
        target = min(target, focus) if focus < target else max(target, focus - page + 1)
        state["focus_seen"] = (state["generation"], state["cursor"])
    logical, painted = B.window(app, "modal:telemetry", target, len(wrapped), page,
                               context=context, focus=state["cursor"])
    state["top"] = logical
    title = "Telemetry: " + (state["job_id"] or "cluster")
    rows = controls + [[("Read cadence differs from producer cadence.", "dim")]]
    data_start = len(rows)
    logical_hits = []
    selected = sorted((state["anchor"], state["cursor"])) if state["anchor"] is not None else None
    if state["running"]:
        rows.append([("Reading scheduler evidence in the background...", "accent")])
    elif state["error"]:
        from .research_text import clean
        error = clean(state["error"], views.g.ascii)
        while error and len(rows) < page + data_start:
            part = L.truncate(error, content_width)
            rows.append([(part or "?", "warning")])
            error = error[len(part) if part else 1:]
    else:
        for index, text in wrapped[painted:painted + page]:
            style = "sel" if index == state["cursor"] or selected and selected[0] <= index <= selected[1] else "text"
            logical_hits.append((len(rows), index))
            rows.append([(text, style)])
    rows.append([("Arrows/pages scroll | v select | y copy | Y full report | Tab buttons", "dim")])
    rendered = L.box(views.g, rows, width, height, title)
    state["control_hits"] = place_hits(control_hits, rendered[1:-1])
    for y, _, value in state["control_hits"]:
        index = identifiers.index(value["action"][1].split()[-1])
        state["hits"].append((y, value["left"], value["right"], index))
    if len(rendered) >= 3:
        for logical_row, index in logical_hits:
            if logical_row + 1 < len(rendered) - 1:
                hy, hx, row = rendered[logical_row + 1]
                state["line_hits"].append((hy, hx + 1, hx + L.vlen(L.row_text(row)) - 2, index))
    state["render_generation"] = state["generation"]
    return B.boxed(app, "modal:telemetry", rendered, start=data_start, count=len(wrapped), page=page,
                   target=logical, painted=painted, setter=lambda value: state.update(top=value),
                   context=context, header=-1)
