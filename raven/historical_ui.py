"""Read-only as-of inspection in the existing workbench and run context.

Immutable snapshots are decoded and laid out by the session's bounded worker
lane. The paint path only slices already-prepared rows; it never opens evidence.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
import time

from . import historical_replay as H, layout as L
from .log_text import display_text


def initialize(app):
    if not hasattr(app, "historical_state"):
        app.historical_state = dict(timeline=H.Timeline(), recording=False, pending=None,
            lane=None, next=0., token=None, selected=None, sequence=None, result=None,
            rows=[], view="jobs", top=0, error="", saved=None, diff=False,
            generation=0, kind="capture", controls=[], painted=None, focus=0,
            view_request=None, view_generation=0, rendered=None, when=None, epoch=None,
            context_label="")
    return app.historical_state


def active(app):
    return getattr(app, "mode", "") == "historical"


def command_names():
    return ["asof"]


def _submit(app, function, *args, kind="capture"):
    state = initialize(app)
    if state["pending"] is not None:
        raise ValueError("A historical operation is already pending")
    scheduler = getattr(app, "worker_scheduler", None)
    if scheduler is None:
        raise ValueError("Historical recording requires the session's bounded worker scheduler")
    if state["lane"] is None:
        state["lane"] = scheduler.lane("research")
    state["pending"] = state["lane"].submit(function, *args)
    state["pending_generation"] = state["generation"]
    state["kind"] = kind


def _read(path):
    from .evidence_index import _read as read_verified
    return H.Timeline.load(read_verified(path, H.MAX_ARCHIVE + (64 << 10)))


def _save(destination, timeline):
    from . import science_files as SF
    return SF.publish(SF.destination(destination), files=[("timeline.json", H.encoded(timeline.document()), False)],
                      receipt={"schema": H.SCHEMA, "semantics": "Observer snapshots, not complete source transitions"})


def _invalidate(app):
    app.ui_publication_revision = getattr(app, "ui_publication_revision", 0) + 1


def _request_view(app):
    state = initialize(app)
    state["view_generation"] += 1
    state["view_request"] = state["view_generation"]
    state["painted"] = None
    state["result"], state["rows"] = None, []
    _invalidate(app)


def tick(app):
    state = initialize(app)
    pending = state["pending"]
    if pending is not None and pending.done():
        state["pending"] = None
        try:
            value = pending.result()
            if state["generation"] == state["pending_generation"]:
                if state["kind"] == "capture":
                    sequence = state["timeline"].append_prepared(value)
                    if active(app) and state["sequence"] is None:
                        state["sequence"] = sequence
                        _request_view(app)
                elif state["kind"] == "load":
                    state["timeline"] = value
                    state["sequence"] = value.frames[-1]["sequence"] if value.frames else None
                    _request_view(app)
                elif state["kind"] == "view":
                    generation, result, rows, width = value
                    if generation == state["view_generation"]:
                        state.update(result=result, rows=rows, rendered=width,
                                     sequence=result.get("sequence"), top=0)
                else:
                    app.say("Historical archive saved: " + str(value))
                state["error"] = ""
                _invalidate(app)
        except (OSError, ValueError, RuntimeError) as exc:
            state["error"] = str(exc)
            app.fail("Historical observation: " + str(exc))
    if state["pending"] is not None:
        return
    if active(app) and state["view_request"] is not None:
        generation = state["view_request"]
        try:
            _submit(app, _view, state["timeline"].clone(), state["sequence"], state["when"],
                    state["epoch"], state["view"], state["diff"],
                    max(20, min(512, getattr(app, "width", 80) - 8)),
                    bool(getattr(app, "_ascii_cfg", False)), generation, kind="view")
            state["view_request"] = None
        except (ValueError, RuntimeError) as exc:
            state["error"] = str(exc)
        return
    if not state["recording"] or time.monotonic() < state["next"]:
        return
    state["next"] = time.monotonic() + 2
    selected = state["selected"] if active(app) else getattr(app, "selected_id", None)
    token_fn = getattr(app.store, "ui_publication_token", None)
    token = (token_fn() if callable(token_fn) else None, selected)
    if token[0] is not None and token == state["token"]:
        return
    try:
        _submit(app, H.captured, app.store, selected)
        state["token"] = token
    except (ValueError, RuntimeError) as exc:
        state["error"] = str(exc)


def _identity(app, selected):
    return app.store.capture_series_identity(selected) if selected else None


def _open(app):
    from .setup_ui import _pending_review
    state = initialize(app)
    if not active(app):
        if _pending_review(app):
            raise ValueError("Close the pending action review before entering historical inspection")
        state["saved"] = {"mode": "main" if app.mode == "palette" else app.mode, "selected_id": app.selected_id,
                          "tab": app.tab, "cursor": copy.deepcopy(app.cursor), "top": copy.deepcopy(app.top)}
        state["selected"] = app.selected_id
        state["saved_identity"] = _identity(app, app.selected_id)
        state["context_label"] = ""
        if app.mode == "run_home":
            binding = getattr(app, "run_home_state", {}).get("binding") or {}
            state["context_label"] = "From run home: " + str(binding.get("run_id", "unknown")) + " / attempt " + str(binding.get("attempt", "unknown")) + "; snapshot attribution is shown separately."
        from .terminal_ui import _focus
        from .pointer_routing import begin_gesture
        _focus(app, False)
        begin_gesture(app)
        app.mode = "historical"
        app.confirm = {}
        getattr(app, "toolbar_state", {}).update(menu=None, panel=None, focus=None)
    if state["timeline"].frames and state["sequence"] is None:
        state["sequence"] = state["timeline"].frames[-1]["sequence"]
    _request_view(app)


def _close(app):
    state = initialize(app)
    for key, value in (state["saved"] or {"mode": "main"}).items():
        setattr(app, key, value)
    current = _identity(app, getattr(app, "selected_id", None))
    if state.get("saved_identity") != current:
        app.selected_id = None
        app.say("Live selection cleared: the original recording attempt is no longer the same identity")
    state["saved"] = None
    state["view_generation"] += 1
    state["view_request"] = None
    _invalidate(app)


def run_command(app, args):
    if not args or args[0] != "asof":
        return False
    state = initialize(app)
    values = args[1:]
    try:
        if active(app) and values[:1] in (["record"], ["save"]):
            raise ValueError("Return Live before changing recording or saving an archive")
        if values == ["live"]:
            if active(app): _close(app)
        elif len(values) == 2 and values[0] == "key" and active(app):
            handle_key(app, values[1])
        elif values in (["record"], ["record", "on"], ["record", "off"]):
            state["recording"] = values[-1] != "off"
            state["next"], state["token"] = 0., None
            app.say("Historical snapshot recording " + ("enabled; bounded memory, explicit save" if state["recording"] else "stopped"))
            tick(app)
        elif len(values) == 2 and values[0] in ("save", "load"):
            if values[0] == "save":
                if not state["timeline"].frames: raise ValueError("No historical snapshots are available")
                _submit(app, _save, values[1], state["timeline"].clone(), kind="save")
            else:
                if state["pending"] is not None: raise ValueError("Wait for the pending historical operation")
                _open(app)
                state["recording"] = False
                state["generation"] += 1
                state["when"] = state["epoch"] = None
                state["view_request"] = None
                _submit(app, _read, values[1], kind="load")
        elif not values or len(values) in (1, 2):
            moment = H.timestamp(values[0]) if values else None
            epoch = int(values[1]) if len(values) == 2 else None
            _open(app)
            state["when"], state["epoch"] = moment, epoch
        else:
            raise ValueError("asof [UTC_ISO_OR_UNIX [CLOCK_EPOCH] | live | record on/off | save NEW_DIRECTORY | load FILE]")
    except (OSError, ValueError, RuntimeError, TypeError, OverflowError) as exc:
        app.fail(str(exc))
    return True


def _move(app, direction):
    state = initialize(app)
    sequences = [frame["sequence"] for frame in state["timeline"].frames]
    if not sequences: return
    index = sequences.index(state["sequence"]) if state["sequence"] in sequences else len(sequences)-1
    state["sequence"] = sequences[max(0, min(len(sequences)-1, index+direction))]
    state["when"] = state["epoch"] = None
    state["top"] = 0
    _request_view(app)


def handle_key(app, key):
    if not active(app): return False
    state = initialize(app)
    if key in ("esc", "q", "ctrl-c"): _close(app)
    elif key in ("tab", "btab"):
        state["focus"] = (state["focus"] + (1 if key == "tab" else -1)) % 7
    elif key in ("enter", "space"):
        handle_key(app, ("esc", "left", "right", "1", "2", "3", "d")[state["focus"]])
    elif key == "left": _move(app, -1)
    elif key == "right": _move(app, 1)
    elif key in ("1", "2", "3"):
        state["view"] = {"1": "jobs", "2": "history", "3": "analytics"}[key]
        state["top"] = 0
        _request_view(app)
    elif key == "d":
        state["diff"] = not state["diff"]
        state["top"] = 0
        _request_view(app)
    elif key in ("up", "down", "pgup", "pgdn"):
        state["top"] = max(0, state["top"] + {"up": -1, "down": 1, "pgup": -10, "pgdn": 10}[key])
    return True  # Consume all global action/shell shortcuts in this context.


def handle_mouse(app, y, x, button="left", shift=False, **kwargs):
    if not active(app): return False
    if button in ("wheel-up", "wheel-down"):
        handle_key(app, "up" if button == "wheel-up" else "down")
    elif button in ("left", "press"):
        state = initialize(app)
        if state["painted"] == _frame(app):
            for row, _, item in state["controls"]:
                if row == y and item["left"] <= x < item["right"]:
                    handle_key(app, item["action"][1].split()[-1])
                    break
    return True


def _frame(app):
    state = initialize(app)
    return (getattr(app, "width", None), getattr(app, "height", None), state["view_generation"], state["top"], state["focus"])


def _utc(value):
    try:
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        return str(value) + " Unix seconds (outside calendar display range)"


def _view(timeline, sequence, when, epoch, view, diff, width, ascii_, generation):
    result = timeline.at(when, clock_epoch=epoch) if when is not None else timeline.read(sequence)
    lines = []
    charts = []
    if result.get("status") != "observed":
        lines += [result.get("reason", "No observation retained"), "Use :asof record on from live, or :asof load FILE."]
    else:
        value = result["snapshot"]
        lines += [f"Observed {_utc(value['observed_at'])} · sequence {result['sequence']} · clock epoch {result['clock_epoch']}",
                  f"Queue source time: {value['source_time']} · selected: {value['selected'] or 'none'}",
                  result["semantics"], "Coverage: " + json.dumps(value["coverage"], sort_keys=True)]
        for source, health in value["source_health"].items():
            if isinstance(health, dict) and health.get("error"):
                lines.append("Retained source error (" + str(source) + "): " + str(health["error"]))
        if result.get("unknown_since") is not None:
            lines.append("Unknown interval after observation; no changes are inferred.")
        if result["evicted"]:
            lines.append(f"{result['evicted']} earlier snapshots expired; those intervals are unknown.")
        if diff:
            previous = [frame for frame in timeline.frames if frame["sequence"] < result["sequence"]]
            if previous:
                before = timeline.read(previous[-1]["sequence"])["snapshot"]
                change = H.difference(before, value)
                for key in ("appeared", "disappeared", "changed", "ambiguous"):
                    lines += [key.title() + ": " + json.dumps(row, sort_keys=True) for row in change[key]]
                lines.append(change["limits"])
            else:
                lines.append("No previous retained snapshot for comparison.")
        elif view in ("jobs", "history"):
            lines += [f"{row.get('id', '?')}  {row.get('state', '?')}  {row.get('name', '')}  submit={row.get('submit', '')} start={row.get('start', '')}" for row in value[view]]
            if not value[view]: lines.append("No rows in this observation.")
        else:
            identity = value.get("identity")
            lines.append("Attempt: " + json.dumps(identity, sort_keys=True))
            if not identity or not identity.get("verified"):
                lines.append("Attempt unverified; samples are isolated observations, not durable attempt attribution.")
            samples = [row for row in value["samples"] if H._finite(row.get("t"))
                       and -62135596800 <= row["t"] <= min(value["observed_at"], 253402300799)]
            future = len(value["samples"])-len(samples)
            lines.append(f"{len(samples)} retained source samples; {future} unknown/future/out-of-range clocks excluded from placement.")
            lines.append("Chart bars are observed values; blank intervals remain unknown.")
            charts = _charts(samples, width, ascii_)
            # Exact source records remain available after the compact charts.
            lines += ["Exact recorded values (source timestamps; no interpolation)"]
            lines += [json.dumps(row, sort_keys=True) for row in samples]
    rows = [[(display_text(line), "text")] for line in lines]
    if charts:
        position = next(i for i, line in enumerate(lines) if line.startswith("Exact recorded values"))
        rows[position:position] = charts
    return generation, result, rows, width


def _charts(samples, width, ascii_):
    from .charts import vbar_chart
    g = L.Glyphs(ascii_)
    live = [row for row in samples if row.get("k") == "live"]
    rows = []
    # Independent sources: absent GPU never suppresses available CPU/memory.
    for field, label, unit, scale in (("cpu", "CPU per allocated core", "%", 100), ("rss", "Resident memory", "MiB", 1 / (1 << 20))):
        values = [row.get(field) * scale if H._finite(row.get(field)) and row[field] >= 0 else None for row in live]
        rows += vbar_chart(g, values, width, 3, title=label, unit=unit,
                           sample_times=[row["t"] for row in live], sample_interval=1e-300)
    devices = sorted({str(device) for row in samples if row.get("k") == "gpu" and isinstance(row.get("gpu"), dict) for device in row["gpu"]})
    if not devices:
        rows.append([("GPU recording unavailable in this observation; CPU/memory remain independent.", "dim")])
    for device in devices[:8]:
        chosen = [row for row in samples if row.get("k") == "gpu" and isinstance(row.get("gpu"), dict) and device in row["gpu"]]
        values = []
        for row in chosen:
            item = row["gpu"][device]
            value = item[0] if isinstance(item, list) and item else None
            values.append(value if H._finite(value) and 0 <= value <= 100 else None)
        rows += vbar_chart(g, values, width, 3, hi=100, title="GPU " + device, unit="%",
                           sample_times=[row["t"] for row in chosen], sample_interval=1e-300)
    if len(devices) > 8:
        rows.append([(f"{len(devices)-8} additional devices retained in exact records; chart budget is eight.", "dim")])
    return rows


def overlay(views, snap, app, width, height):
    if not active(app): return None
    from .control_rows import buttons, place_hits
    state = initialize(app)
    height = height or 30
    target = max(20, min(512, width-8))
    if state["rendered"] is not None and state["rendered"] != target and state["view_request"] is None:
        _request_view(app)
        state["rendered"] = None
    choices = [(key, title, ("command", "asof key " + key)) for key, title in
               (("esc", "Live"), ("left", "Previous"), ("right", "Next"), ("1", "Jobs"), ("2", "History"), ("3", "Analytics"), ("d", "Diff"))]
    controls, hits = buttons(views.g, max(10, width-8), choices, group="historical", selected=choices[state["focus"]][0])
    prefix = [[("Read-only · arrows: snapshots · 1/2/3: views · d: diff · Esc: live", "dim")]]
    if state["context_label"]: prefix.append([(display_text(state["context_label"]), "dim")])
    if state["error"]: prefix.append([("Error: " + display_text(state["error"]), "red")])
    if state["view_request"] is not None or state["pending"] is not None and state["kind"] in ("view", "load"):
        prefix.append([("Reading retained observation…", "dim")])
    visible = max(1, height-len(controls)-len(prefix)-4)
    state["top"] = min(state["top"], max(0, len(state["rows"])-visible))
    rows = controls + prefix + state["rows"][state["top"]:state["top"]+visible]
    rendered = L.box(views.g, rows, width, height, "Historical observations · " + state["view"].title(), min_width=max(20, width-4))
    state["controls"] = place_hits(hits, rendered[1:-1])
    state["painted"] = _frame(app)
    return rendered


def shutdown(app):
    state = initialize(app)
    state["generation"] += 1
    state["recording"] = False
    if state["pending"] is not None: state["pending"].cancel()
    if state["lane"] is not None: state["lane"].shutdown(wait=False, cancel_futures=True)
