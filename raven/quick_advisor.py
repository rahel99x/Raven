"""Explicit, bounded job diagnostics on the existing research worker.

The panel never starts analysis from a render. Results describe an observation
captured on request, rather than a prediction of a job's physical requirements.
In particular Slurm MaxRSS is a task peak, not aggregate job memory.
"""
from __future__ import annotations

from collections import deque
import copy
import json
import math
import os
import statistics
import stat
import threading
import time
from itertools import islice

from . import advisor, clock
from .model import Finished, hms, human, nbytes, secs, stamp, terminal_state
from .research import clean

MAX_SERIES = 10_000
MAX_SERIES_BYTES = 8 * 1024 * 1024
MAX_HISTORY = 256
MAX_TRACE = 4096
MAX_STEPS = 128
MAX_GPU_DEVICES = 128
MAX_DURATION = 3162240000  # Match the reporting standard's 100-year bound.
MAX_TIMESTAMP = 253402300799
MAX_MEMORY = 9223372036854775807


def initialize(app):
    panel = getattr(app, "job_panel_state", None)
    if not isinstance(panel, dict):
        panel = app.job_panel_state = {}
    state = panel.get("quick")
    if not isinstance(state, dict):
        state = panel["quick"] = {}
    for key, value in dict(status="idle", token=0, job=None, result=None,
                          callback=None, cancel=None).items():
        state.setdefault(key, value)
    return state


def _number(value, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        value = float(value)
    except (OverflowError, ValueError):
        return None
    return value if math.isfinite(value) and value >= 0 and (not positive or value > 0) else None


def _duration(value):
    if not isinstance(value, str) or len(value) > 256:
        return None
    duration = _number(secs(value))
    return duration if duration is not None and duration <= MAX_DURATION else None


def _timestamp(value):
    timestamp = _number(value)
    return timestamp if timestamp is not None and timestamp <= MAX_TIMESTAMP else None


def _memory(value, *, positive=False):
    memory = _number(value, positive=positive)
    return memory if memory is not None and memory <= MAX_MEMORY else None


def _attempt(store, jid):
    record, _ = store.record_context(jid)
    if record is None:
        return None
    key = (store.job_attempt(jid), store.capture_series_identity(jid), type(record).__name__, record.id, record.name, record.state,
           getattr(record, "submit", ""), getattr(record, "start", ""), record.cpus,
           record.nodes, record.gpus, record.limit)
    return key + ((record.end, record.cpu_time, record.rss, record.req_mem, record.elapsed)
                  if isinstance(record, Finished) else (record.mem_req, record.command))


def cancel(app):
    state = initialize(app)
    state["token"] += 1
    event = state.get("cancel")
    if event is not None:
        event.set()
    hub, callback = getattr(app, "research", None), state.get("callback")
    if hub is not None and callback is not None:
        hub.cancel_task(callback)
    state.update(status="idle", job=None, result=None, callback=None, cancel=None)


def request(app):
    """Record one explicit request; ticks retry a busy worker without queuing."""
    cancel(app)
    state = initialize(app)
    jid = getattr(app, "selected_id", None)
    key = _attempt(app.store, jid) if jid else None
    if key is None:
        state.update(status="idle", job=jid)
        return False
    # An explicit analysis has precedence over automatic project discovery.
    from .project_ui import cancel_automatic
    cancel_automatic(app)
    state.update(status="queued", job=jid, attempt=key, cancel=threading.Event(),
                 requested_at=clock.now(), started=time.monotonic())
    _start(app)
    return True


def _visible(app, state):
    return (getattr(app, "tab", "") in ("jobs", "history") and getattr(app, "mode", "main") == "main"
            and getattr(app, "job_panel_state", {}).get("mode") == "quick"
            and getattr(app, "selected_id", None) == state.get("job"))


def _start(app):
    state = initialize(app)
    if state["status"] != "queued" or not _visible(app, state):
        return
    from .job_panels import _worker
    hub = _worker(app, getattr(getattr(app, "views_ref", None), "files", None))
    # Do not add analysis behind a running file read. One pending request in UI
    # state is sufficient, and cancellation can never accumulate worker tasks.
    with hub.lock:
        if hub.closed:
            state.update(status="error", error="The background reader is closed.")
            return
        if hub.pending or hub.future is not None and not hub.future.done():
            return
        token, jid, key, event = state["token"], state["job"], state["attempt"], state["cancel"]

        def work():
            payload = (capture(app.store, jid, event, light=True) if hub.execution.managed
                       else capture(app.store, jid, event))
            if event.is_set():
                return None
            return analyze(payload, event=event)

        def completed(result):
            if state["token"] != token or event.is_set() or not _visible(app, state):
                return
            state["callback"] = None
            if _attempt(app.store, jid) != key:
                cancel(app)
                return
            if isinstance(result, Exception):
                state.update(status="error", error=clean(result, limit=512))
            elif result is None:
                state.update(status="idle", result=None)
            else:
                state.update(status="ok", result=result)

        if hub.start_task(work, completed):
            state.update(status="loading", callback=completed)


def tick(app):
    state = initialize(app)
    if state["status"] in ("queued", "loading", "ok", "error") and (
            not _visible(app, state) or _attempt(app.store, state.get("job")) != state.get("attempt")):
        cancel(app)
        return
    if state["status"] == "queued":
        _start(app)


def _sample(item):
    """Copy only fields used by this report; external series cannot grow it."""
    if not isinstance(item, dict):
        return None
    sample = {key: item.get(key) for key in ("t", "k", "cpu", "eff", "rss", "cpu_time")}
    gpu = item.get("gpu")
    if isinstance(gpu, dict):
        sample["gpu"] = {str(key)[:128]: list(value[:3]) for key, value in islice(gpu.items(), MAX_GPU_DEVICES)
                         if isinstance(value, (list, tuple))}
    return sample


def _recorded_series(store, jid, event, *, max_series=MAX_SERIES, max_bytes=MAX_SERIES_BYTES, identity=None):
    """Read a bounded persistent tail outside the Store lock.

    Headless Store.series_of restores retained history synchronously. This
    cancellable diagnosis keeps its own read budget and leaves the ordinary
    interactive series cache untouched.
    """
    with store.lock:
        current = store.capture_series_identity(jid)
        if identity is not None and current != identity:
            raise ValueError("Recording attempt changed before Advisor acquisition; retry for the current attempt.")
        identity = current
        source = store.series.get(jid, ())
        source_count = len(source)
        memory = [_sample(item) for item in reversed(list(islice(reversed(source), max_series)))]
        already_loaded = jid in store._series_loaded
        path = store._series_identity_path(identity) if not already_loaded else None
    points, warnings = deque(maxlen=max_series), []
    retained = min(max_series, store.series_keep)
    if source_count > retained:
        warnings.append(f"Analysis uses the latest {retained:,} of {source_count:,} in-memory observations.")
    if path:
        from .series_storage import uses_blocks, compact_path
        if uses_blocks(path):
            from .series_blocks import read
            rows, status = read(compact_path(path), max_series, max_bytes=max_bytes,
                                max_decoded_bytes=max_bytes, cancel=event)
            points.extend(sample for row in rows if (sample := _sample(row)) is not None)
            if status["status"] not in {"ready", "missing"}:
                warnings.append(status["message"])
            if status["verified_samples"] > max_series:
                warnings.append(f"Analysis uses the latest {max_series:,} observations from the verified compact prefix.")
            if event.is_set():
                return [], warnings
        else:
            try:
                opener = lambda target, flags: os.open(target, flags | os.O_NONBLOCK | os.O_NOFOLLOW)
                with open(path, "rb", opener=opener) as stream:
                    metadata = os.fstat(stream.fileno())
                    if not stat.S_ISREG(metadata.st_mode):
                        raise OSError("Recorded series must be a regular file.")
                    size = metadata.st_size
                    offset = max(0, size - max_bytes)
                    stream.seek(offset)
                    data = stream.read(max_bytes)
                if offset:
                    data = data.partition(b"\n")[2]
                    warnings.append(f"Persistent series exceeds the {max_bytes:,}-byte budget; only its latest bounded tail was read.")
                for index, line in enumerate(data.splitlines()):
                    if index % 256 == 0 and event.is_set():
                        return [], warnings
                    try:
                        sample = _sample(json.loads(line))
                    except (ValueError, TypeError, RecursionError):
                        continue
                    if sample is not None:
                        points.append(sample)
            except FileNotFoundError:
                pass
            except OSError as exc:
                warnings.append("Persistent series unavailable: " + clean(exc, limit=160))
    seen = {(item.get("t"), item.get("k")) for item in memory if item is not None
            and isinstance(item.get("t"), (str, int, float, type(None)))
            and isinstance(item.get("k"), (str, type(None)))}
    merged = [item for item in points if isinstance(item.get("t"), (str, int, float, type(None)))
              and isinstance(item.get("k"), (str, type(None)))
              and (item.get("t"), item.get("k")) not in seen]
    merged.extend(item for item in memory if item is not None)
    merged.sort(key=lambda item: _timestamp(item.get("t")) or 0)
    if len(merged) > retained:
        warnings.append(f"Analysis uses the latest {retained:,} retained observations.")
    with store.lock:
        if store.capture_series_identity(jid) != identity:
            raise ValueError("Recording attempt changed during Advisor acquisition; retry for the current attempt.")
    return merged[-retained:], warnings


def capture(store, jid, event=None, *, light=False):
    """Capture exact published job evidence on the worker, with fixed bounds."""
    event = event or threading.Event()
    with store.lock:
        identity = store.capture_series_identity(jid)
        job, details = store.record_context(jid)
        if job is None:
            raise ValueError("This job is no longer in the scheduler or accounting history.")
        job = copy.deepcopy(job)
        live = copy.deepcopy(store.live.get(jid))
        history = list(islice(store.finished, 512)) if light else list(store.finished)
        steps = copy.deepcopy((store.steps.get(jid) or store.fin_steps.get(jid) or [])[:MAX_STEPS])
        trace_source = store.trace.get(jid, [])
        trace = [dict(item) for item in trace_source[-MAX_TRACE:] if isinstance(item, dict)]
        trace_count = len(trace_source)
        gpu = copy.deepcopy((store.gpu.get(jid) or [])[:MAX_GPU_DEVICES])
        metadata = copy.deepcopy(store.tags.get(jid, {}))
    # Filtering accounting outside the lock keeps unrelated history off the
    # foreground path. Same-name runs remain a hint, not proof of equal work.
    same = []
    matched = 0
    for record in history:
        if record.name == job.name and record.id != jid:
            matched += 1
            if len(same) < MAX_HISTORY:
                same.append(copy.deepcopy(record))
    series, warnings = _recorded_series(store, jid, event,
        max_series=512 if light else MAX_SERIES, max_bytes=256 << 10 if light else MAX_SERIES_BYTES, identity=identity)
    if light:
        warnings.append("Light Advisor uses at most 512 retained samples and 512 history records; this is a bounded summary.")
    if matched > MAX_HISTORY:
        warnings.append(f"Same-name history is limited to {MAX_HISTORY} of {matched:,} runs.")
    if trace_count > MAX_TRACE:
        warnings.append(f"GPU trace is limited to its latest {MAX_TRACE:,} observations.")
    return dict(job=job, details=details, live=live, history=same, series=series,
                steps=steps, trace=trace, gpu=gpu, metadata=metadata, warnings=warnings,
                captured_at=clock.now())


def _percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1))]


def _timed(values):
    """A time-weighted mean cannot assign an unobserved long gap full weight."""
    if not values:
        return None, 0.0
    ordered = sorted(values)
    intervals = [right[0] - left[0] for left, right in zip(ordered, ordered[1:]) if right[0] > left[0]]
    if not intervals:
        return statistics.fmean(value for _, value in ordered), 0.0
    cap = statistics.median(intervals) * 3
    weights = [min(cap, max(0, right[0] - left[0])) for left, right in zip(ordered, ordered[1:])]
    weight = sum(weights)
    # A stored CPU rate describes the interval preceding that observation.
    return (sum(right[1] * part for right, part in zip(ordered[1:], weights)) / weight if weight
            else statistics.fmean(value for _, value in ordered)), ordered[-1][0] - ordered[0][0]


def analyze(payload, *, event=None):
    """Build a concise evidence report. No scheduler calls or allocation edits."""
    event = event or threading.Event()
    job, series = payload["job"], payload.get("series", [])[-MAX_SERIES:]
    details, steps = payload.get("details", {}), payload.get("steps", [])
    live, history = payload.get("live"), payload.get("history", [])[:MAX_HISTORY]
    state = terminal_state(job.state)
    elapsed, limit = _duration(job.elapsed) or 0, _duration(job.limit)
    active = not isinstance(job, Finished)
    result = dict(job=job.id, name=clean(job.name, limit=256), state=state,
                  captured_at=payload.get("captured_at", clock.now()), sections=[],
                  limitations=list(payload.get("warnings", [])), sample_count=len(series),
                  compatible_runs=0, source="retained observations + published scheduler/accounting")
    sections, limitations = result["sections"], result["limitations"]
    if event.is_set():
        return None
    # Per-ID series may survive a Slurm requeue. Reject observations outside
    # this attempt when Slurm supplies its actual start/end timestamps.
    lower = stamp(getattr(job, "start", ""))
    upper = stamp(job.end) if isinstance(job, Finished) else None
    if state == "PENDING":
        series, live, steps = [], None, []
    elif lower is not None or upper is not None:
        retained = [sample for sample in series if (when := _timestamp(sample.get("t"))) is not None
                    and (lower is None or when >= lower) and (upper is None or when < upper + 1)]
        if len(retained) != len(series):
            limitations.append("Observations outside this attempt's published start/end interval were excluded.")
        series = retained
    result["sample_count"] = len(series)

    def section(name, status, title, facts, action, values=()):
        sections.append(dict(name=name, status=status, title=title, facts=facts,
                             action=action, values=list(values)[-24:]))

    rates, timed_rates, rss, timestamps = [], [], [], []
    for sample in series:
        if sample.get("k") != "live":
            continue
        t = _timestamp(sample.get("t"))
        if t is not None:
            timestamps.append(t)
        rate = _number(sample.get("cpu"))
        if rate is not None:
            rates.append(rate)
            if t is not None:
                timed_rates.append((t, rate))
        value = _memory(sample.get("rss"), positive=True)
        if value is not None:
            rss.append(value)
    measured = len(rates)
    mean, span = _timed(timed_rates)
    if mean is None and rates:
        mean = statistics.fmean(rates)
    if isinstance(job, Finished):
        final = _number(job.cpu_eff)
    else:
        final = _number(getattr(live, "avg", None))
    efficiency = final if final is not None else mean
    cpu_facts = [f"Allocated {job.cpus} CPUs; observed rates {measured:,} samples."]
    try:
        task_count = int(details.get("NumTasks", 0))
    except (TypeError, ValueError, OverflowError):
        task_count = 0
    complex_cpu_scope = active and (job.nodes > 1 or task_count > 1 or any(step.ntasks > 1 for step in steps)
                                   or any(not step.id.endswith((".batch", ".extern")) for step in steps))
    if active:
        cpu_facts.append("Live rates use reported batch/step CPU time; verify that launched ranks and other steps are included.")
    if complex_cpu_scope:
        limitations.append("Live sstat AveCPU is a task/step average; multi-task or multi-node job CPU demand is not established by that sample.")
    if efficiency is None:
        section("CPU", "unknown", "CPU efficiency is not observed", cpu_facts,
                "Keep the CPU request until task CPU time or utilization samples are available.")
        limitations.append("CPU task accounting is unavailable; a missing sample does not mean idle CPU.")
    else:
        cpu_facts.append(f"{'Accounting efficiency' if final is not None else 'Observed mean rate'} {100 * efficiency:.0f}% per allocated CPU.")
        if rates:
            p90, peak = _percentile(rates, .9), max(rates)
            cpu_facts.append(f"Rate mean {100 * (mean or 0):.0f}%; P90 {100 * p90:.0f}%; peak {100 * peak:.0f}%.")
            if len(rates) >= 8:
                chunk = max(1, len(rates) // 4)
                first, last = statistics.fmean(rates[:chunk]), statistics.fmean(rates[-chunk:])
                cpu_facts.append(f"Early to recent rates {100 * first:.0f}% to {100 * last:.0f}% (different phases can explain this change).")
        else:
            p90 = peak = efficiency
        if complex_cpu_scope:
            title, status = "Live CPU accounting does not establish aggregate job use", "caution"
            action = "Inspect all launched steps and ranks. Obtain aggregate CPU accounting before testing fewer CPUs."
        elif efficiency > 1.05 or peak > 1.1:
            title, status = "CPU accounting scope needs review", "caution"
            action = "Check task count and CPU-time scope; values over 100% can make core recommendations invalid."
        elif measured >= 8 and span >= 120 and efficiency < .5 and peak < .7 and job.cpus > 1:
            want = min(job.cpus, max(1, math.ceil(max(efficiency, p90, peak) * job.cpus / advisor.CPU_TARGET)))
            title, status = "CPU request may be larger than this workload uses", "caution"
            action = f"Benchmark {want} CPUs on an equivalent run. P90 bursts are retained; validate throughput before reducing the request."
        elif peak >= .7 and efficiency < .5:
            title, status = "CPU work is bursty; preserve the busy phases", "caution"
            action = "Check I/O, synchronization and phase timing. A low average alone does not justify fewer CPUs."
        elif efficiency >= .85:
            title, status = "Allocated CPUs are busy", "good"
            action = "Keep the current CPU request; test more cores only if the workload can scale in parallel."
        else:
            title, status = "CPU request needs more comparable evidence", "unknown"
            action = "Inspect workload phases and measure an equivalent run before changing CPU count."
        section("CPU", status, title, cpu_facts, action, rates)

    if event.is_set():
        return None
    peak_values = rss + [value for value in [_memory(getattr(job, "rss", None), positive=True),
                                           _memory(getattr(live, "rss", None), positive=True)] if value is not None]
    peak_values.extend(value for step in steps if (value := _memory(step.rss, positive=True)) is not None)
    peak = max(peak_values) if peak_values else None
    request = _memory(getattr(job, "req_mem", 0) if isinstance(job, Finished) else job.mem_bytes) or 0
    per_node = None
    if not isinstance(job, Finished) and job.mem_req.endswith("n"):
        per_node = nbytes(job.mem_req)
    if details.get("MinMemoryNode"):
        per_node = nbytes(str(details["MinMemoryNode"])) or per_node
    if job.nodes == 1 and request:
        per_node = request
    memory_facts = [f"Allocated memory {human(request) if request else 'unknown'} over {job.nodes} node(s).",
                    "MaxRSS is the largest observed task RSS, not total job memory."]
    if peak is None:
        title, status = "Memory usage is not observed", "unknown"
        action = "Keep the request until task RSS and, preferably, aggregate node memory are measured."
    else:
        memory_facts.append(f"Task RSS peak {human(peak)}; {len(rss):,} retained memory observations.")
        if len(rss) >= 8:
            chunk = max(1, len(rss) // 4)
            first, last = statistics.median(rss[:chunk]), statistics.median(rss[-chunk:])
            memory_facts.append(f"Early to recent task RSS {human(first)} to {human(last)}; no extrapolation of future growth.")
        if per_node:
            fraction = peak / per_node
            memory_facts.append(f"Task peak is {100 * fraction:.0f}% of the {human(per_node)} node allocation (a lower bound on pressure).")
        else:
            fraction = None
            memory_facts.append("Per-node allocation is unknown; a job-wide request cannot establish task or node headroom.")
        if state == "OUT_OF_MEMORY":
            title, status = "The scheduler reports an out-of-memory failure", "risk"
            action = "Inspect the failed step and logs; increase the matching per-node request or reduce memory use. Recorded RSS may miss the failing peak."
        elif fraction is not None and fraction >= .85:
            title, status = "An observed task approaches the node memory limit", "risk"
            action = "Collect aggregate node peaks and keep additional memory margin. Multiple tasks can make node pressure higher."
        else:
            title, status = "Observed task memory does not establish free headroom", "unknown"
            action = "Keep the memory request until aggregate node usage is known; do not size whole-job memory from MaxRSS alone."
    if state == "OUT_OF_MEMORY" and peak is None:
        title, status = "Out-of-memory failure; peak RSS is unavailable", "risk"
        action = "Inspect failed steps and logs; absence of a captured peak does not show that memory was sufficient."
    section("Memory", status, title, memory_facts, action,
            [value / (per_node or peak or 1) for value in rss])
    limitations.append("Task RSS does not measure aggregate job memory or physical memory need; node headroom is unverified.")

    compatible = [run for run in history if run.name == job.name and run.cpus == job.cpus
                  and run.nodes == job.nodes and run.gpus == job.gpus and terminal_state(run.state) == "COMPLETED"]
    result["compatible_runs"] = len(compatible)
    completed_times = [value for run in compatible if (value := _duration(run.elapsed)) is not None and value > 0]
    facts = [f"Elapsed {hms(elapsed)}; limit {hms(limit) if limit is not None else 'unknown/unlimited'}.",
             f"Comparable allocation: {len(compatible)} completed same-name run(s). Equal names do not prove equal input or workload."]
    if completed_times:
        facts.append(f"Previous elapsed median {hms(statistics.median(completed_times))}; longest {hms(max(completed_times))}.")
    if state == "TIMEOUT":
        title, status = "The job reached its wall-time limit", "risk"
        action = "Check progress and checkpoint support; use equivalent completed runs to review a longer limit."
    elif active and limit and elapsed >= limit * .9:
        title, status = "The job is near its wall-time limit", "risk"
        action = "Check progress and checkpoints. Resource samples do not establish remaining work or a completion ETA."
    elif not active and state == "COMPLETED" and elapsed:
        want = max([elapsed] + completed_times) * advisor.TIME_HEADROOM
        title, status = "The observed run completed within its allocation", "good"
        action = f"A test limit with 30% margin is {advisor.round_time(want)}; validate on equivalent inputs and preserve the longest observed run."
    elif completed_times and limit and max(completed_times) > limit:
        title, status = "A same-name run exceeded the present time limit", "caution"
        action = "Compare inputs and workload; the historical duration is a warning, not this job's predicted completion time."
    else:
        title, status = "Remaining runtime is not established", "unknown"
        action = "Keep the time request until equivalent completed runs or application progress supply a runtime basis."
    section("Wall time", status, title, facts, action)

    utils, gpu_memory = [], []
    for sample in series:
        if sample.get("k") != "gpu" or not isinstance(sample.get("gpu"), dict):
            continue
        for values in list(sample["gpu"].values())[:MAX_GPU_DEVICES]:
            if not isinstance(values, (list, tuple)):
                continue
            util = _number(values[0]) if values else None
            if util is not None and util <= 100:
                utils.append(util)
            if len(values) >= 3:
                used, total = _memory(values[1]), _memory(values[2], positive=True)
                if used is not None and total is not None:
                    gpu_memory.append(min(1, used / total))
    trace = payload.get("trace", [])[-MAX_TRACE:]
    if state == "PENDING":
        trace = []
    elif lower is not None or upper is not None:
        trace = [point for point in trace if (when := _timestamp(point.get("t"))) is not None
                 and (lower is None or when >= lower) and (upper is None or when < upper + 1)]
    trace_utils = [_number(point.get("util")) for point in trace]
    trace_utils = [value for value in trace_utils if value is not None and value <= 100]
    gpu_facts = [f"Allocated {job.gpus} GPUs; {len(utils):,} sampled device observations; {len(trace_utils):,} job-trace observations."]
    observed = utils or trace_utils
    if not observed and state != "PENDING":
        for device in payload.get("gpu", [])[:MAX_GPU_DEVICES]:
            util = _number(device.util)
            if util is not None and util <= 100:
                observed.append(util)
            used, total = _memory(device.used), _memory(device.total, positive=True)
            if used is not None and total is not None:
                gpu_memory.append(min(1, used / total))
        if observed:
            gpu_facts.append("Only the latest published GPU snapshot is available; device phases are unobserved.")
    if observed:
        gpu_facts.append(f"Observed utilization mean {statistics.fmean(observed):.0f}%; P90 {_percentile(observed, .9):.0f}%; peak {max(observed):.0f}%.")
        if utils and trace_utils:
            gpu_facts.append(f"Job trace mean {statistics.fmean(trace_utils):.0f}%; trace and scheduler samples are kept separate.")
        if gpu_memory:
            gpu_facts.append(f"Observed device memory peak {100 * max(gpu_memory):.0f}% of reported device capacity.")
        title, status = ("Observed GPU activity is low", "caution") if statistics.fmean(observed) < 30 else ("GPU activity is observed", "good")
        action = "Inspect data loading, CPU staging and synchronization; utilization alone does not prove that fewer GPUs preserve throughput."
    else:
        title, status = "GPU utilization is not observed", "unknown"
        action = "Enable supported GPU sampling or the job trace before changing a GPU request." if job.gpus else "This job requests no GPUs."
    if job.gpus or observed:
        section("GPU", status, title, gpu_facts, action, [value / 100 for value in observed])

    # Keep regular Advisor CPU/time observations, while rejecting its legacy
    # whole-job memory flags when only task RSS is available.
    # Invalid external samples remain missing evidence, never infinities fed
    # into rounding or averages in the older Advisor implementation.
    regular_series = [dict(sample, rss=_memory(sample.get("rss")) or 0)
                      for sample in series if sample.get("k") == "live"]
    regular_job = copy.copy(job)
    regular_job.elapsed = hms(_duration(job.elapsed)) if _duration(job.elapsed) is not None else ""
    regular_job.limit = hms(limit) if limit is not None else ""
    if isinstance(regular_job, Finished):
        regular_job.rss = _memory(regular_job.rss) or 0
        regular_job.cpu_time = _number(regular_job.cpu_time)
        regular_job.req_mem = _memory(regular_job.req_mem) or 0
    regular_live = copy.copy(live) if live is not None else None
    if regular_live is not None:
        regular_live.rss = _memory(regular_live.rss) or 0
        regular_live.avg = _number(regular_live.avg)
    regular_history = [copy.copy(item) for item in compatible]
    for item in regular_history:
        item.rss, item.cpu_time, item.req_mem = _memory(item.rss) or 0, _number(item.cpu_time), _memory(item.req_mem) or 0
        duration = _duration(item.elapsed)
        item.elapsed = hms(duration) if duration is not None else ""
        duration = _duration(item.limit)
        item.limit = hms(duration) if duration is not None else ""
    regular = (advisor.advise_finished(regular_job, regular_history) if isinstance(regular_job, Finished)
               else advisor.advise_running(regular_job, regular_live, regular_series, regular_history))
    regular_facts = [f"Regular Advisor basis: {regular.runs} run(s); suggestions require an equivalent workload."]
    regular_facts.append(f"Inspector allocation: {job.cpus} CPUs / {job.nodes} node(s) / {job.gpus} GPUs / {clean(job.partition, limit=64)} partition.")
    if details.get("NumTasks") or details.get("CPUs/Task") or details.get("NumCPUs"):
        regular_facts.append("Scheduler task scope: " + " / ".join(f"{key} {clean(details[key], limit=48)}"
                            for key in ("NumTasks", "CPUs/Task", "NumCPUs") if key in details))
    if regular.cpus_suggest:
        regular_facts.append(f"Its average-efficiency CPU estimate is {regular.cpus_suggest}; the CPU section above also checks sampled bursts.")
    if regular.time_suggest:
        regular_facts.append(f"Its elapsed-history time estimate is {regular.time_suggest}; use the wall-time qualifications above.")
    if regular.mem_suggest:
        regular_facts.append("Its task-peak memory flag is withheld because aggregate node memory is unverified.")
    notes = [clean(value, limit=256) for value in regular.notes[:6]]
    regular_facts.extend(notes)
    if steps:
        regular_facts.append(f"Inspector steps: {len(steps)}; task counts " + ", ".join(str(step.ntasks) for step in steps[:8]) + ".")
        for step in steps[:8]:
            if step.cpu_time and step.ntasks > 1 and step.min_cpu is not None:
                # Live sstat reports AveCPU, while finished sacct steps report
                # TotalCPU. Do not divide an already averaged live value again.
                mean_cpu = step.cpu_time if active else step.cpu_time / step.ntasks
                ratio = step.min_cpu / mean_cpu
                if 0 <= ratio < .6:
                    regular_facts.append(f"Step {clean(step.id, limit=64)}: slowest rank CPU time is {100 * ratio:.0f}% of the rank mean; inspect imbalance and I/O.")
    if getattr(job, "dependency", ""):
        regular_facts.append("Dependency: " + clean(job.dependency, limit=160))
    if getattr(job, "reason", ""):
        regular_facts.append("Scheduler reason: " + clean(job.reason, limit=160))
    if getattr(job, "exit", ""):
        regular_facts.append("Accounting exit: " + clean(job.exit, limit=64))
    if payload.get("metadata", {}).get("note"):
        regular_facts.append("Job note: " + clean(payload["metadata"]["note"], limit=256))
    section("Context", "caution" if state in ("FAILED", "NODE_FAIL", "BOOT_FAIL") else "unknown",
            "Inspector and regular Advisor context", regular_facts,
            "Read exact-job logs for failures; allocation changes cannot repair every application or node failure.")

    valid_times = [_timestamp(sample.get("t")) for sample in series]
    valid_times = [value for value in valid_times if value is not None]
    observation_span = max(valid_times) - min(valid_times) if len(valid_times) > 1 else 0
    result.update(observation_span=observation_span, cpu_span=span,
                  observation_fraction=min(1.0, observation_span / elapsed) if elapsed else None,
                  missing=[section["name"] for section in sections if section["status"] == "unknown"])
    limitations.append(f"Observation span {hms(observation_span)} across {len(series):,} retained samples. Gaps are unobserved; the span is not continuous coverage.")
    if active:
        limitations.append("A running job is a partial observation; later phases can change CPU and memory demand.")
    if state == "PENDING":
        limitations.append("This job is pending; previous same-name runs do not establish this attempt's resource use.")
    if not details:
        limitations.append("Scheduler inspection fields are unavailable; allocation scope may remain incomplete.")
    result["summary"] = ("Review allocation risks" if any(item["status"] == "risk" for item in sections)
                         else "Review measured opportunities" if any(item["status"] == "caution" for item in sections)
                         else "Evidence summary; retain unverified allocations")
    return result
