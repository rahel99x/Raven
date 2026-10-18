"""Six-cell progress from published application reports, with labeled time use.

Rendering helpers perform no discovery, reads, series copies or requests.
Maintenance requests use the single
existing research worker. Elapsed/requested time is never completion progress.
"""
from __future__ import annotations

from dataclasses import dataclass
from collections import OrderedDict
from itertools import islice
import copy
import math
import os
import threading
import time

from . import layout as L
from .model import Finished, secs, stamp

METRIC_KEYS = ("progress_fraction", "progress_pct", "completed_steps", "total_steps")
POLL_INTERVAL = 8.0
MAX_BATCH = 4
MAX_REPORTS = 128
MAX_INVENTORIES = 256
CLOCK_FRAMES = "◴◷◶◵"
HOURGLASS_FRAMES = "⧗⧖"
CLOCK_INTERVAL = 0.4
HOURGLASS_INTERVAL = 0.8
MAX_ANIMATIONS = 256
MAX_ANIMATION_HITS = 4096


def initialize(app):
    state = getattr(app, "job_progress_state", None)
    if not isinstance(state, dict):
        state = app.job_progress_state = {"identity": None, "last": None}
    defaults = {"reports": OrderedDict(), "inventory_key": None, "inventory_index": {},
                "inventory_root": "", "batch_token": 0, "batch_callback": None,
                "batch_cancel": None, "batch_last": None, "batch_cursor": 0,
                "animation_slots": (), "animation_context": None}
    for key, value in defaults.items():
        state.setdefault(key, value)
    return state


def command_names():
    return []


def handle_key(app, key):
    return False


def run_command(app, args):
    return False


def overlay(views, snap, app, width, height):
    return None


def _selected_tick(app):
    """Refresh selected, already-bound progress on the existing idle worker.

    The foreground uses an exact record lookup and copies only binding/settings.
    Actual report reads retain ResearchHub's bounded reader and publication
    rules. Explicit commands, project rebinding and Quick Advisor take priority.
    """
    state = initialize(app)
    if (not getattr(app, "interactive", False) or getattr(app, "tab", "") != "jobs"
            or getattr(app, "mode", "main") != "main"):
        return False
    project = getattr(app, "project_state", {})
    binding = project.get("binding") if isinstance(project, dict) else None
    jid = getattr(app, "selected_id", None)
    if (not isinstance(binding, dict) or not jid or binding.get("job_id") != jid
            or not binding.get("metrics_file") or project.get("busy") or project.get("auto_pending")):
        return False
    if project.get("auto_suppressed") == jid or project.get("status") in ("off", "disabled", "closed"):
        return False
    if getattr(app, "job_panel_state", {}).get("quick", {}).get("status") in ("queued", "loading"):
        return False
    hub = getattr(app, "research", None)
    if hub is None:
        return False
    with hub.lock:
        if hub.closed or hub.pending or hub.future is not None and not hub.future.done():
            return False
        if hub.settings.get("metrics_file") != binding["metrics_file"]:
            return False
        generation = hub.generation
        identity = (generation, jid, binding.get("run_id"), binding.get("attempt"),
                    binding.get("run_root"), binding["metrics_file"])
        from .refresh_rate import file_interval, multiplier
        interval = file_interval(POLL_INTERVAL, multiplier(app), remote=bool(getattr(hub.files, "remote", False)))
        now = time.monotonic()
        if identity == state["identity"] and state["last"] is not None and now - state["last"] < interval:
            return False
        record, details = app.store.record_context(jid)
        if record is None:
            return False
        settings = copy.deepcopy(hub.settings)
        context = {"view": "experiment", "jid": jid, "explicit_jid": jid, "job": record,
                   "snap": {"jobs": [] if isinstance(record, Finished) else [record],
                            "finished": [record] if isinstance(record, Finished) else [],
                            "details": {jid: details}},
                   "settings": settings, "log_settings": {}, "project_logs": None,
                   "project_warnings": [], "run_id": binding.get("run_id"),
                   "run_root": binding.get("run_root"), "binding": copy.deepcopy(binding),
                   "generation": generation}
        hub.request(context)
        state.update(identity=identity, last=now)
        return True


def _inventory_index(app, state):
    """Memoize only bounded run identity/path fields, including in-place edits."""
    project = getattr(app, "project_state", {})
    if not isinstance(project, dict):
        return "", {}
    if project.get("status") in ("off", "disabled", "closed"):
        return "", {}
    binding = project.get("binding") or {}
    root = binding.get("project_root") or project.get("root", "")
    runs = project.get("runs", [])
    if (not isinstance(root, str) or not root.startswith("/") or len(root) > 4096
            or not isinstance(runs, (list, tuple)) or len(runs) > MAX_INVENTORIES):
        return "", {}
    records = []
    for run in runs:
        inventory = run.get("inventory") if isinstance(run, dict) else None
        if not isinstance(inventory, dict) or inventory.get("schema") != "raven.run/v1":
            continue
        jid, run_id, paths = inventory.get("job_id"), inventory.get("run_id"), inventory.get("paths", {})
        if (not isinstance(jid, str) or not jid or not isinstance(run_id, str) or not run_id
                or run.get("job_id", jid) != jid or run.get("run_id", run_id) != run_id
                or not isinstance(paths, dict)):
            continue
        path = paths.get("metrics")
        if not isinstance(path, str) or not path or len(path) > 4096:
            path = None
        records.append((jid, run_id, inventory.get("attempt"), path,
                        inventory.get("start"), inventory.get("submit"), inventory.get("end")))
    key = (root, tuple(records))
    if key != state["inventory_key"]:
        index, ambiguous = {}, set()
        for record in records:
            if record[0] in index:
                ambiguous.add(record[0])
            index[record[0]] = record
        for jid in list(index):
            if jid in ambiguous or index[jid][3] is None:
                del index[jid]
        state.update(inventory_key=key, inventory_root=root, inventory_index=index)
    return root, state["inventory_index"]


def _signature(store, job):
    return (store.job_attempt(job.id), job.id, getattr(job, "start", ""), getattr(job, "submit", ""))


def _matches_inventory(job, metadata):
    for field, position in (("start", 4), ("submit", 5)):
        actual = stamp(getattr(job, field, ""))
        reported = _number(metadata[position])
        # The application can start after Slurm's allocation starts. Earlier
        # attempt timestamps are stale; later application launch is legitimate.
        if actual is not None and reported is not None and reported < actual - 1:
            return False
    ended = stamp(job.end) if isinstance(job, Finished) else None
    if ended is not None and (started := _number(metadata[4])) is not None and started > ended + 1:
        return False
    return True


def cancel_automatic(app):
    """Let explicit commands detach one progress batch without blocking."""
    state = initialize(app)
    state["batch_token"] += 1
    if state["batch_cancel"] is not None:
        state["batch_cancel"].set()
    hub = getattr(app, "research", None)
    if hub is not None and state["batch_callback"] is not None:
        hub.cancel_task(state["batch_callback"])
    state.update(batch_callback=None, batch_cancel=None)


def _batch_tick(app):
    state = initialize(app)
    project = getattr(app, "project_state", {})
    if project.get("busy") or project.get("auto_pending"):
        return False
    if getattr(app, "job_panel_state", {}).get("quick", {}).get("status") in ("queued", "loading"):
        return False
    hub = getattr(app, "research", None)
    if hub is None:
        return False
    from .refresh_rate import file_interval, multiplier
    interval = file_interval(POLL_INTERVAL, multiplier(app), remote=bool(getattr(hub.files, "remote", False)))
    now = time.monotonic()
    if state["batch_last"] is not None and now - state["batch_last"] < interval:
        return False
    with hub.lock:
        if hub.closed or hub.pending or hub.future is not None and not hub.future.done():
            return False
        from .remote import LocalFiles
        if type(hub.files) is not LocalFiles:
            return False
        root, index = _inventory_index(app, state)
        if not root or not index:
            return False
        visible = getattr(app, "job_progress_visible", ())
        if not isinstance(visible, (list, tuple)):
            return False
        # A real screen has at most a few dozen rows. Bound malformed embedding
        # adapters too, without traversing an entire queued accounting history.
        suppressed = project.get("auto_suppressed")
        candidates = list(dict.fromkeys(jid for jid in visible[:256]
                                       if isinstance(jid, str) and jid in index and jid != suppressed))
        if not candidates:
            return False
        cursor = state["batch_cursor"] % len(candidates)
        ordered = candidates[cursor:] + candidates[:cursor]
        captured = []
        for jid in ordered:
            job, _ = app.store.record_context(jid)
            if job is not None and _matches_inventory(job, index[jid]):
                captured.append((jid, _signature(app.store, job), index[jid]))
            if len(captured) == MAX_BATCH:
                break
        if not captured:
            return False
        state["batch_cursor"] = (cursor + len(captured)) % len(candidates)
        generation, token = hub.generation, state["batch_token"] + 1
        event = threading.Event()
        state["batch_token"] = token

        def work():
            from . import projects
            from .metrics import MetricReader
            reader_identity = (id(hub.files), generation, root)
            retained_reader = getattr(hub, "_job_progress_reader", None)
            if retained_reader is None or retained_reader[0] != reader_identity:
                # Scalar progress does not need 64 full chart series. This
                # reader uses the same worker and bounded confined file API.
                retained_reader = (reader_identity, MetricReader(hub.files, max_points=1, max_bytes=65536, max_streams=64))
                hub._job_progress_reader = retained_reader
            reader, result = retained_reader[1], []
            for jid, signature, metadata in captured:
                if event.is_set():
                    break
                try:
                    selected = projects.select_run(root, metadata[1], files=hub.files)
                    binding = selected["binding"]
                    inventory = selected["run"]["inventory"]
                    current = (inventory.get("job_id"), inventory.get("run_id"), inventory.get("attempt"),
                               inventory.get("paths", {}).get("metrics"), inventory.get("start"),
                               inventory.get("submit"), inventory.get("end"))
                    if current != metadata or binding.get("job_id") != jid or not binding.get("metrics_file"):
                        result.append((jid, signature, metadata, None, ""))
                        continue
                    data = _compact(reader.read_confined(binding["metrics_file"], binding))
                    result.append((jid, signature, metadata, data, binding["metrics_file"]))
                except (OSError, ValueError, TypeError, KeyError):
                    result.append((jid, signature, metadata, None, ""))
            return result

        def completed(result):
            if token != state["batch_token"] or event.is_set():
                return
            state.update(batch_callback=None, batch_cancel=None)
            current_root, current_index = _inventory_index(app, state)
            if (isinstance(result, Exception) or current_root != root or hub.generation != generation
                    or getattr(app, "tab", "") != "jobs" or getattr(app, "mode", "main") != "main"):
                return
            for jid, signature, metadata, data, path in result:
                job, _ = app.store.record_context(jid)
                if (job is None or _signature(app.store, job) != signature or current_index.get(jid) != metadata
                        or not _matches_inventory(job, metadata)):
                    state["reports"].pop(jid, None)
                    continue
                source = _source(data, jid, job)
                if source is None:
                    state["reports"].pop(jid, None)
                    continue
                state["reports"][jid] = dict(source=source, signature=signature, metadata=metadata,
                                             root=root, path=path)
                state["reports"].move_to_end(jid)
                while len(state["reports"]) > MAX_REPORTS:
                    state["reports"].popitem(last=False)

        if not hub.start_task(work, completed):
            return False
        state.update(batch_callback=completed, batch_cancel=event, batch_last=now)
        return True


def tick(app):
    if (not getattr(app, "interactive", False) or getattr(app, "tab", "") != "jobs"
            or getattr(app, "mode", "main") != "main"):
        return False
    return _selected_tick(app) or _batch_tick(app)


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        value = float(value)
    except (OverflowError, ValueError):
        return None
    return value if math.isfinite(value) else None


@dataclass(frozen=True)
class Source:
    fraction: float
    source: str = "reported progress"
    unit: str = ""
    timestamp: float | None = None


@dataclass(frozen=True)
class Observation:
    fraction: float | None
    basis: str
    source: str = ""
    unit: str = ""

    @property
    def style(self):
        if self.basis == "reported":
            return "cyan"
        if self.basis == "time":
            return L.level(self.fraction or 0)
        return "dim"

    @property
    def text(self):
        return self.format()

    def format(self, ascii_=False):
        """Return six cells: a narrow source icon, a gap and a four-cell bar.

        A fixed spacer keeps the clock clear of the plot at every fraction,
        including 100%. Numeric fractions remain available for sorting and
        inspection; squeezing their digits into this chart would remove the
        spacer or clip the bar. Emoji clocks and hourglasses occupy two cells
        on common terminals, so these markers use narrow text characters.
        """
        if self.fraction is None:
            return ("w wait" if ascii_ else "⧗ wait") if self.basis == "pending" else "   -- "
        fraction = _number(self.fraction)
        if fraction is None:
            return "   -- "
        eighths = int(max(0, min(1, fraction)) * 32 + .5)
        if ascii_:
            prefix = "t" if self.basis == "time" else "p"
            cells = ["=" if eighths >= (i + 1) * 8 else ">" if eighths > i * 8 else "-" for i in range(4)]
        else:
            prefix = "◷" if self.basis == "time" else "▸"
            cells = ["░" if eighths <= i * 8 else "▏▎▍▌▋▊▉█"[min(8, eighths - i * 8) - 1]
                     for i in range(4)]
        return prefix + " " + "".join(cells)


def _animation_context(app):
    return (getattr(app, "tab", ""), getattr(app, "mode", "main"),
            getattr(app, "theme", ""), bool(getattr(app, "animations_enabled", False)))


def publish_animation(app, rows, hits, *, ascii_=False):
    """Retain only icon cells in the final, already painted Jobs geometry.

    Row IDs come from the existing hit publication; eligibility comes from
    the visible table slice. No scheduler queries, samples or file reads are
    needed, and offscreen or clipped progress columns cannot animate.
    """
    state = initialize(app)
    state["animation_context"] = _animation_context(app)
    state["animation_slots"] = ()
    if ascii_ or state["animation_context"][:2] != ("jobs", "main") or not state["animation_context"][3]:
        return
    eligible = getattr(app, "job_progress_animation", {})
    if not isinstance(eligible, dict) or not isinstance(rows, (list, tuple)) or not isinstance(hits, (list, tuple)):
        return
    left = None
    for hit in islice(hits, MAX_ANIMATION_HITS):
        if not isinstance(hit, (tuple, list)) or len(hit) != 3:
            continue
        y, kind, value = hit
        if (kind == "sort_header" and isinstance(value, (tuple, list)) and len(value) == 4
                and tuple(value[:2]) == ("jobs", "progress") and all(isinstance(x, int) and not isinstance(x, bool) for x in value[2:])
                and value[3] - value[2] == 6):
            left = value[2]
            break
    if left is None or left < 0:
        return
    slots = []
    seen = set()
    for hit in islice(hits, MAX_ANIMATION_HITS):
        if not isinstance(hit, (tuple, list)) or len(hit) != 3:
            continue
        y, kind, jid = hit
        if kind != "job" or not isinstance(jid, str) or eligible.get(jid) not in ("clock", "hourglass"):
            continue
        if not isinstance(y, int) or isinstance(y, bool) or not 0 <= y < len(rows) or (y, left) in seen:
            continue
        cell = _glyph_at(rows[y], left)
        expected = CLOCK_FRAMES if eligible[jid] == "clock" else HOURGLASS_FRAMES
        if cell not in expected:
            continue
        seen.add((y, left))
        slots.append((y, left, eligible[jid]))
        if len(slots) == MAX_ANIMATIONS:
            break
    state["animation_slots"] = tuple(slots)


def _glyph_at(row, x):
    used = 0
    for text, _ in row:
        width = L.vlen(text)
        if used <= x < used + width:
            offset = x - used
            prefix = L.truncate(text, offset)
            if L.vlen(prefix) != offset:
                return None
            return text[len(prefix):len(prefix) + 1]
        used += width
    return None


def _replace_glyph(row, x, glyph):
    """Replace a known one-cell marker without changing its painted style."""
    used = 0
    for index, (text, style) in enumerate(row):
        width = L.vlen(text)
        if used <= x < used + width:
            offset = x - used
            prefix = L.truncate(text, offset)
            if L.vlen(prefix) != offset or len(prefix) >= len(text) or L.vlen(text[len(prefix)]) != 1:
                return row
            if text[len(prefix)] == glyph:
                return row
            changed = list(row)
            changed[index] = (prefix + glyph + text[len(prefix) + 1:], style)
            return changed
        used += width
    return row


def next_animation_deadline(app, *, now=None):
    """Next visible icon boundary; absent/offscreen animations need no wake."""
    state = getattr(app, "job_progress_state", {})
    if (not isinstance(state, dict) or state.get("animation_context") != _animation_context(app)
            or not getattr(app, "animations_enabled", False)):
        return float("inf")
    now = time.monotonic() if now is None else now
    intervals = {CLOCK_INTERVAL if kind == "clock" else HOURGLASS_INTERVAL
                 for _, _, kind in state.get("animation_slots", ())}
    return min(((math.floor(now / interval) + 1) * interval for interval in intervals), default=float("inf"))


def animate_rows(app, rows, *, now=None):
    """Patch visible progress icons without rebuilding the cached document.

    The owner wakes at the next visible 400/800ms icon boundary. Animation does
    not change scheduler/file polling or trigger a chart/research recomputation.
    Hover, marks and cursor colours are retained.
    """
    state = getattr(app, "job_progress_state", {})
    if (not isinstance(state, dict) or state.get("animation_context") != _animation_context(app)
            or not getattr(app, "animations_enabled", False)):
        return rows
    now = _number(time.monotonic() if now is None else now)
    if now is None or now < 0:
        return rows
    out = rows
    for y, x, kind in state.get("animation_slots", ()):
        if not 0 <= y < len(rows):
            continue
        frames, interval = (CLOCK_FRAMES, CLOCK_INTERVAL) if kind == "clock" else (HOURGLASS_FRAMES, HOURGLASS_INTERVAL)
        if _glyph_at(rows[y], x) not in frames:
            continue
        glyph = frames[int(now / interval) % len(frames)]
        row = _replace_glyph(rows[y], x, glyph)
        if row is not rows[y]:
            if out is rows:
                out = list(rows)
            out[y] = row
    return out


def _fraction(progress, latest):
    if isinstance(progress, dict):
        completed, total = _number(progress.get("completed")), _number(progress.get("total"))
        unit = progress.get("unit", "")
        if (completed is not None and total is not None and 0 <= completed <= total and total > 0
                and isinstance(unit, str) and len(unit) <= 64 and (not unit or unit.isprintable())):
            return completed / total, "reported progress", unit
    if not isinstance(latest, dict):
        return None
    alternatives = []
    fraction = _number(latest.get("progress_fraction"))
    percent = _number(latest.get("progress_pct"))
    completed, total = _number(latest.get("completed_steps")), _number(latest.get("total_steps"))
    if fraction is not None and 0 <= fraction <= 1:
        alternatives.append((fraction, "progress_fraction", ""))
    if percent is not None and 0 <= percent <= 100:
        alternatives.append((percent / 100, "progress_pct", ""))
    if completed is not None and total is not None and 0 <= completed <= total and total > 0:
        alternatives.append((completed / total, "completed_steps/total_steps", "steps"))
    if not alternatives or any(not math.isclose(value[0], alternatives[0][0], rel_tol=1e-9, abs_tol=1e-12)
                               for value in alternatives[1:]):
        return None
    return alternatives[0]


def _compact(data):
    """Copy only scalar progress fields from the published metric snapshot."""
    if not isinstance(data, dict):
        return None
    progress = data.get("progress", data)
    latest = data.get("latest", {})
    return dict(status=data.get("status", "ok"), job_id=data.get("job_id"),
                generation=data.get("generation"), last_t=data.get("last_t"),
                job_start=data.get("job_start"), job_submit=data.get("job_submit"),
                progress={key: progress.get(key, "" if key == "unit" else None) for key in ("completed", "total", "unit")}
                         if isinstance(progress, dict) else None,
                latest={key: latest.get(key) for key in METRIC_KEYS} if isinstance(latest, dict) else None)


def _source(data, jid, job, generation=None):
    if not isinstance(data, dict) or data.get("status", "ok") not in ("ok", "partial"):
        return None
    if data.get("job_id") not in (None, jid):
        return None
    if data.get("generation") is not None and generation is not None and data["generation"] != generation:
        return None
    for field in ("start", "submit"):
        recorded = data.get("job_" + field)
        actual = getattr(job, field, "")
        if recorded is not None and actual and recorded != actual:
            return None
    reported = _fraction(data.get("progress"), data.get("latest"))
    if reported is None:
        return None
    timestamp = _number(data.get("last_t"))
    if data.get("last_t") is not None and (timestamp is None or timestamp < 0 or timestamp > 253402300799):
        return None
    if timestamp is not None:
        lower = stamp(getattr(job, "start", "")) or stamp(getattr(job, "submit", ""))
        upper = stamp(getattr(job, "end", "")) if isinstance(job, Finished) else None
        # Slurm accounting dates have one-second precision; an application
        # report in the recorded end second still belongs to this attempt.
        if lower is not None and timestamp < lower or upper is not None and timestamp >= upper + 1:
            return None
    return Source(*reported, timestamp=timestamp)


def _retain_selected(app, state, root, index, jid, job, source, store, generation):
    """Keep a validated run report when selection reconfigures the shared Hub.

    The inventory and scheduler attempt, rather than the currently selected
    job, own this bounded scalar cache. Manual file attachments still use the
    Hub's configuration generation and cannot populate this cache.
    """
    project = getattr(app, "project_state", {})
    binding = project.get("binding") if isinstance(project, dict) else None
    metadata = index.get(jid)
    if (not root or metadata is None or not isinstance(binding, dict)
            or project.get("auto_suppressed") == jid
            or binding.get("project_root") != root or binding.get("job_id") != jid
            or binding.get("run_id") != metadata[1] or binding.get("attempt") != metadata[2]
            or not _matches_inventory(job, metadata)):
        return
    run_root = os.path.join(root, "runs", metadata[1])
    from .projects import relative_path
    try:
        relative_path(metadata[3])
    except ValueError:
        return
    path = os.path.join(run_root, metadata[3])
    if binding.get("run_root") != run_root or binding.get("metrics_file") != path:
        return
    with app.research.lock:
        if app.research.generation != generation or app.research.settings.get("metrics_file") != path:
            return
    state["reports"][jid] = dict(source=source, signature=_signature(store, job),
                                  metadata=metadata, root=root, path=path)
    state["reports"].move_to_end(jid)
    while len(state["reports"]) > MAX_REPORTS:
        state["reports"].popitem(last=False)


def published(app, snap):
    """Collect compact, exact-job sources once for a table rendering pass.

    Cache keys must match the Hub's current configuration generation and the
    exact experiment/job identity. Old configuration snapshots, wrong IDs and
    known prior-attempt timestamps cannot enter the returned mapping.
    An embedding adapter may supply the same typed report at snap['progress'].
    """
    jobs = {job.id: job for records in (snap.get("finished", ()), snap.get("departed_jobs", {}).values(),
                                      snap.get("jobs", ())) for job in records}
    compact = {}
    hub = getattr(app, "research", None)
    generation = getattr(hub, "generation", None)
    if hub is not None and hasattr(hub, "lock") and hasattr(hub, "cache"):
        with hub.lock:
            generation = hub.generation
            for key, entry in hub.cache.items():
                if (isinstance(key, tuple) and len(key) == 3 and key[:2] == (generation, "experiment")
                        and isinstance(key[2], str) and key[2] in jobs
                        and isinstance(entry, (tuple, list)) and len(entry) == 2):
                    compact[key[2]] = _compact(entry[1])
    result = {}
    store = getattr(app, "store", None)
    state = initialize(app) if store is not None else getattr(app, "job_progress_state", None)
    root, index = "", {}
    if isinstance(state, dict) and store is not None:
        root, index = _inventory_index(app, state)
        for jid, entry in state["reports"].items():
            job = jobs.get(jid)
            if (job is None or jid == getattr(app, "project_state", {}).get("auto_suppressed")
                    or entry["root"] != root or index.get(jid) != entry["metadata"]
                    or entry["signature"] != _signature(store, job) or not _matches_inventory(job, entry["metadata"])):
                continue
            cached_source = entry["source"]
            verified = _source({"progress": {"completed": cached_source.fraction, "total": 1,
                                             "unit": cached_source.unit}, "last_t": cached_source.timestamp}, jid, job)
            if verified is not None:
                result[jid] = cached_source
    for jid, data in compact.items():
        source = _source(data, jid, jobs[jid], generation)
        if source is not None:
            result[jid] = source
            if isinstance(state, dict) and store is not None:
                _retain_selected(app, state, root, index, jid, jobs[jid], source, store, generation)
        else:
            # A new selected-source read is authoritative, including removal
            # or invalidation of its previously reported progress fields.
            result.pop(jid, None)
            if isinstance(state, dict):
                state["reports"].pop(jid, None)
    supplied = snap.get("progress", {})
    if isinstance(supplied, dict):
        for jid, data in supplied.items():
            if isinstance(jid, str) and jid in jobs:
                source = _source(_compact(data), jid, jobs[jid], generation)
                if source is not None:
                    result[jid] = source
    return result


def observation(job, sources):
    if getattr(job, "pending", False) or getattr(job, "state", "") == "PENDING":
        return Observation(None, "pending", "waiting for execution")
    source = sources.get(job.id)
    if isinstance(source, Source) and (value := _number(source.fraction)) is not None and 0 <= value <= 1:
        return Observation(source.fraction, "reported", source.source, source.unit)
    elapsed = _number(secs(getattr(job, "elapsed", "")))
    limit = _number(secs(getattr(job, "limit", "")))
    if elapsed is not None and limit is not None and limit > 0 and elapsed >= 0:
        fraction = elapsed / limit
        if math.isfinite(fraction):
            return Observation(fraction, "time", "elapsed/requested wall-time")
    return Observation(None, "unknown", "no published progress or finite time limit")
