"""Opt-in bounded observations of explicitly owned local Linux process trees.

Read-only /proc handles pin process identities. R is runnable-or-running,
D is uninterruptible sleep; neither is an exact scheduling/causal diagnosis.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import math
import os
import socket
import time

from .allocation_health import identity as worker_identity

SCHEMA = 'raven.process-profile/v1'
MAX_TASKS = 128
MAX_PROCESSES = 32
MAX_SAMPLES = 600
READ_BYTES = 32768


def _read(fd, name, limit=READ_BYTES):
    handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
    try:
        value = os.read(handle, limit + 1)
        if len(value) > limit:
            raise ValueError('Process metadata exceeds read budget')
        return value.decode('ascii', 'replace')
    finally:
        os.close(handle)


def _stat(fd):
    raw = _read(fd, 'stat', 8192)
    end = raw.rfind(')')
    fields = raw[end + 2:].split()
    if end < 0 or len(fields) < 37:
        raise ValueError('Incomplete process identity')
    return dict(state=fields[0], ppid=int(fields[1]), start_ticks=int(fields[19]),
                cpu_ticks=int(fields[11]) + int(fields[12]), processor=int(fields[36]))


def _open(pid):
    if type(pid) is not int or not 1 <= pid <= 1 << 30:
        raise ValueError('Choose a positive local PID')
    fd = os.open('/proc/' + str(pid), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if os.fstat(fd).st_uid != os.getuid():
            raise ValueError('Process is not owned by the authenticated worker user')
        _stat(fd)
    except BaseException:
        os.close(fd)
        raise
    return fd


def execution_identity(value):
    owner = worker_identity(value)
    if owner['host'] != socket.gethostname():
        raise ValueError('Collector is not on the verified Heavy worker host')
    if not os.path.isdir('/proc'):
        raise ValueError('Local Linux /proc is unavailable')
    return owner


def inspect(pid, execution, expected_start=None):
    owner = execution_identity(execution)
    fd = _open(pid)
    try:
        before = _stat(fd)
        if before['state'] in {'Z', 'X', 'x'}:
            raise ValueError('Reviewed process has exited')
        if expected_start is not None and before['start_ticks'] != expected_start:
            raise ValueError('Target PID was reused or the reviewed process exited')
        cgroup = _read(fd, 'cgroup', 8192)
        if _stat(fd)['start_ticks'] != before['start_ticks'] or os.fstat(fd).st_uid != owner['uid']:
            raise ValueError('Target identity changed during inspection')
        return dict(pid=pid, start_ticks=before['start_ticks'], uid=owner['uid'], node=owner['host'],
                    cgroup_sha256=hashlib.sha256(cgroup.encode()).hexdigest(), rank=None,
                    job_attribution='unknown; Heavy allocation identity belongs to the collector, not this target')
    finally:
        os.close(fd)


def settings(params):
    values = {}
    for name, default, low, high in [('seconds', 5, .1, 60), ('interval_ms', 100, 100, 2000),
                                    ('cpu_budget_percent', 2, .1, 10)]:
        raw = params.get(name, default)
        if isinstance(raw, bool):
            raise ValueError('Invalid profiler ' + name)
        try:
            value = float(raw)
        except (ValueError, TypeError) as exc:
            raise ValueError('Invalid profiler ' + name) from exc
        if not math.isfinite(value) or not low <= value <= high:
            raise ValueError('Profiler ' + name + ' exceeds its bounded range')
        values[name] = value
    return values


def _counters(fd, schedstats):
    before = _stat(fd)
    available, unavailable = {}, []
    available['cpu_ticks'] = before['cpu_ticks']
    for filename, mapping in [('status', {'voluntary_ctxt_switches': 'voluntary_switches', 'nonvoluntary_ctxt_switches': 'involuntary_switches'}),
                              ('io', {'read_bytes': 'read_bytes', 'write_bytes': 'write_bytes', 'rchar': 'read_characters', 'wchar': 'written_characters'}),
                              ('sched', {'se.nr_migrations': 'migrations'})]:
        try:
            fields = dict((key.strip(), value.strip()) for key, value in (line.split(':', 1) for line in _read(fd, filename).splitlines() if ':' in line))
            for source, target in mapping.items():
                try:
                    available[target] = int(fields[source])
                except (KeyError, ValueError):
                    unavailable.append(target)
        except (OSError, ValueError):
            unavailable.extend(mapping.values())
    if schedstats:
        try:
            runtime, delay, slices = _read(fd, 'schedstat', 256).split()[:3]
            available.update(runtime_ns=int(runtime), run_delay_ns=int(delay), scheduling_slices=int(slices))
        except (OSError, ValueError):
            unavailable += ['runtime_ns', 'run_delay_ns', 'scheduling_slices']
    else:
        unavailable += ['runtime_ns', 'run_delay_ns', 'scheduling_slices']
    after = _stat(fd)
    if after['start_ticks'] != before['start_ticks'] or os.fstat(fd).st_uid != os.getuid():
        raise ValueError('Process/thread identity changed while reading counters')
    return before, available, unavailable


def _task_ids(fd, remaining):
    handle = os.open('task', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
    try:
        rows, truncated = [], False
        with os.scandir(handle) as entries:
            for entry in entries:
                if not entry.name.isdigit():
                    continue
                if len(rows) == remaining:
                    truncated = True
                    break
                rows.append(int(entry.name))
        return sorted(rows), truncated
    finally:
        os.close(handle)


def _verify_chain(ancestors, uid):
    previous = None
    for pid, fd, start in ancestors:
        current = _stat(fd)
        if (current['start_ticks'] != start or current['state'] in {'Z', 'X', 'x'}
                or os.fstat(fd).st_uid != uid
                or previous is not None and current['ppid'] != previous):
            raise ValueError('Pinned ancestor exited or changed identity/ancestry')
        previous = pid


def _ancestry_fallback():
    """Kernel children files are optional; enumerate bounded same-UID metadata."""
    children, inspected, truncated = {}, 0, False
    with os.scandir('/proc') as entries:
        for entry in entries:
            if not entry.name.isdigit():
                continue
            if inspected >= 1024:
                truncated = True
                break
            inspected += 1
            fd = None
            try:
                fd = _open(int(entry.name))
                current = _stat(fd)
                if current['state'] not in {'Z', 'X', 'x'}:
                    children.setdefault(current['ppid'], []).append(entry.name)
            except (OSError, ValueError):
                continue
            finally:
                if fd is not None: os.close(fd)
    return children, truncated


def _check(cancel):
    if cancel is not None and cancel.is_set():
        raise InterruptedError('Process capture cancelled')


def collect(target, options, execution, *, cancel=None):
    owner = execution_identity(execution)
    if inspect(target['pid'], owner, target['start_ticks']) != target:
        raise ValueError('Reviewed target metadata changed; inspect again')
    options = settings(options)
    root = _open(target['pid'])
    start = time.monotonic(); deadline = start + options['seconds']
    interval = options['interval_ms'] / 1000
    observed, issues, sample_durations, intervals = {}, Counter(), [], []
    samples, status, collector_cpu = 0, 'completed', 0.
    child_handles = set()
    ticks = os.sysconf('SC_CLK_TCK')
    try:
        try:
            with open('/proc/sys/kernel/sched_schedstats', encoding='ascii') as stream:
                schedstats = stream.read(8).strip() == '1'
        except OSError:
            schedstats = False
        while time.monotonic() < deadline and samples < MAX_SAMPLES:
            _check(cancel)
            root_state = _stat(root)
            if root_state['state'] in {'Z', 'X', 'x'}:
                raise ValueError('Reviewed root exited; descendants are no longer sampled')
            if root_state['start_ticks'] != target['start_ticks'] or os.fstat(root).st_uid != owner['uid']:
                raise ValueError('Pinned root identity changed')
            begin, cpu_begin = time.monotonic(), time.process_time()
            fallback_children = None
            pending, seen, task_count = [(target['pid'], root, target['start_ticks'], ())], set(), 0
            while pending and len(seen) < MAX_PROCESSES and task_count < MAX_TASKS:
                pid, fd, expected, ancestors = pending.pop()
                parent = ancestors[-1][0] if ancestors else None
                try:
                    before = _stat(fd)
                    if (before['start_ticks'] != expected or os.fstat(fd).st_uid != owner['uid']
                            or parent is not None and before['ppid'] != parent):
                        issues['ancestry changed'] += 1
                        continue
                    seen.add(pid)
                    tids, truncated = _task_ids(fd, MAX_TASKS - task_count)
                    if truncated:
                        issues['task cap'] += 1
                    for tid in tids:
                        task_count += 1
                        _check(cancel)
                        if time.monotonic() >= deadline:
                            issues['sampling duration exhausted'] += 1
                            break
                        thread_fd = None
                        try:
                            thread_fd = os.open('task/' + str(tid), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                            if os.fstat(thread_fd).st_uid != owner['uid']:
                                raise ValueError('Thread ownership changed')
                            stat, counters, missing = _counters(thread_fd, schedstats)
                            _verify_chain(ancestors, owner['uid'])
                            after = _stat(fd)
                            if (after['start_ticks'] != expected or os.fstat(fd).st_uid != owner['uid']
                                    or parent is not None and after['ppid'] != parent):
                                raise ValueError('Ancestry changed during sampling')
                            key = pid, tid, stat['start_ticks']
                            if key not in observed:
                                if len(observed) >= MAX_TASKS:
                                    issues['retained identity cap'] += 1
                                    continue
                                observed[key] = dict(pid=pid, tid=tid, start_ticks=stat['start_ticks'], states=Counter(),
                                    first=dict(counters), last=dict(counters), unavailable=set(), observations=0)
                            row = observed[key]
                            row['states'][stat['state']] += 1; row['observations'] += 1
                            row['last'] = counters; row['unavailable'].update(missing)
                            try:
                                children = _read(thread_fd, 'children', 8192).split()
                            except (OSError, ValueError):
                                if fallback_children is None:
                                    fallback_children, truncated = _ancestry_fallback()
                                    issues['kernel children unavailable; bounded ancestry scan'] += 1
                                    if truncated: issues['ancestry scan cap'] += 1
                                children = fallback_children.get(pid, [])
                            for value in children:
                                if not value.isdigit() or int(value) in seen or any(item[0] == int(value) for item in pending):
                                    continue
                                if len(pending) + len(seen) >= MAX_PROCESSES:
                                    issues['process cap'] += 1
                                    break
                                child_fd = None
                                try:
                                    child_fd = _open(int(value)); child = _stat(child_fd)
                                    if child['ppid'] != pid:
                                        raise ValueError('Child ancestry changed')
                                    child_handles.add(child_fd)
                                    pending.append((int(value), child_fd, child['start_ticks'], ancestors + ((pid, fd, expected),))); child_fd = None
                                except (OSError, ValueError):
                                    issues['child exited or inaccessible'] += 1
                                finally:
                                    if child_fd is not None: os.close(child_fd)
                        except (OSError, ValueError):
                            issues['thread exited, changed or inaccessible'] += 1
                        finally:
                            if thread_fd is not None: os.close(thread_fd)
                except (OSError, ValueError):
                    issues['process exited, changed or inaccessible'] += 1
            for handle in child_handles:
                os.close(handle)
            child_handles.clear()
            samples += 1
            elapsed = time.monotonic() - begin; cpu = time.process_time() - cpu_begin
            collector_cpu += cpu; sample_durations.append(elapsed)
            required = max(elapsed, cpu) / (options['cpu_budget_percent'] / 100)
            interval = max(options['interval_ms'] / 1000, min(2., required))
            if required > 2.:
                issues['sampling overhead budget saturated'] += 1
                status = 'budget-exhausted'
                break
            intervals.append(interval)
            remaining = min(deadline - time.monotonic(), max(0., interval - elapsed))
            # Cancellation latency remains <=50ms even for an adapted 2s period.
            wake = time.monotonic() + remaining
            while time.monotonic() < wake:
                _check(cancel)
                time.sleep(min(.05, max(0., wake - time.monotonic())))
    except (OSError, ValueError) as exc:
        status = 'cancelled' if isinstance(exc, InterruptedError) else 'partial'; issues[str(exc)] += 1
    finally:
        for handle in child_handles:
            os.close(handle)
        os.close(root)
    rows = []
    for row in observed.values():
        deltas = {key: row['last'][key] - first for key, first in row['first'].items()
                  if key in row['last'] and row['last'][key] >= first}
        missing = set(row['unavailable']) | (set(row['first']) - set(deltas))
        rows.append({key: row[key] for key in ('pid', 'tid', 'start_ticks', 'observations')} |
                    dict(state_proportions={state: count / row['observations'] for state, count in row['states'].items()},
                         counter_deltas=deltas, unavailable=sorted(missing), rank=None))
    return dict(schema=SCHEMA, status=status, target=target, collector_execution=owner,
                settings=options, rows=rows, samples=samples, elapsed_seconds=time.monotonic() - start,
                collector_cpu_seconds=collector_cpu, sample_cost_seconds=sample_durations,
                actual_intervals_seconds=intervals, issues=dict(issues), cpu_ticks_per_second=ticks,
                coverage=dict(node=owner['host'], rank='unknown', processes=len({r['pid'] for r in rows}),
                              tasks=len(rows), complete=False, maximum_tasks=MAX_TASKS, maximum_processes=MAX_PROCESSES),
                limitations=['Point observations are not exact state dwell time or scheduling history.',
                    'R combines running and runnable. D does not identify a filesystem or cause.',
                    'Counters cover first-to-last successful observations; missing/reset counters are unknown.',
                    'Rank and scientific-job attribution are unknown; another node needs its own verified collector.',
                    'Requested 100 ms frequency adapts to collector cost; gaps and exited tasks remain incomplete.'])
