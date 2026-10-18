"""Bounded, explicit interoperable views of a verified journal prefix.

The journal remains authoritative. Timeline and evented-stack views are not
sampled CPU profiles and do not infer activity outside recorded intervals.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

from . import trace_protocol as protocol


def _pairs(events):
    """Pair observed frame entries/exits without inventing unknown stack ends."""
    stacks, pairs = {}, {}
    frame_events = 0
    for index, event in enumerate(events):
        kind, data = event['kind'], event['data']
        if kind not in {'call', 'resume', 'return', 'yield', 'unwind'}:
            continue
        frame_events += 1
        stack = stacks.setdefault(event['thread'], [])
        if not isinstance(data.get('frame'), str) or not data['frame']:
            stack.clear()
            continue
        if kind in {'call', 'resume'}:
            if not isinstance(data.get('function'), str):
                stack.clear()
                continue
            stack.append(index)
        elif stack:
            opening = stack[-1]
            if data.get('frame') != events[opening]['data'].get('frame'):
                stack.clear()
            else:
                stack.pop()
                pairs[opening] = index
    return pairs, frame_events - 2 * len(pairs)


def _provenance(snapshot, omitted):
    return {'schema': 'raven.trace-export/v1', 'identity': snapshot['identity'],
            'coverage': snapshot['covered'], 'source_status': snapshot['status'],
            'committed_seq': snapshot['committed_seq'], 'read_seq': snapshot['read_seq'],
            'verified_prefix_sha256': snapshot['sha256'], 'evicted_events': snapshot['evicted'],
            'unpaired_frame_events': omitted, 'warnings': snapshot['warnings'],
            'limitations': ['Only the verified retained prefix is exported.',
                'Recorded wall-time intervals are not sampled CPU time or complete logical async stacks.',
                'Unpaired frames have unknown boundaries; no synthetic closing events are added.']}


def convert(snapshot, format='perfetto'):
    if format not in {'perfetto', 'speedscope'}:
        raise ValueError('Choose perfetto or speedscope')
    if snapshot['status'] == 'INVALID':
        raise ValueError('Cannot export an invalid trace; inspect and recover its verified journal first')
    events = snapshot['events']
    if not events:
        raise ValueError('No verified retained events are available to export')
    origin = events[0]['mono_ns']
    pairs, omitted = _pairs(events)
    provenance = _provenance(snapshot, omitted)
    threads = {name: index + 1 for index, name in enumerate(dict.fromkeys(e['thread'] for e in events))}
    if format == 'perfetto':
        result, consumed, spans = [], set(), {}
        for opening, closing in pairs.items():
            first, last = events[opening], events[closing]
            consumed.update((opening, closing))
            result.append({'ph': 'X', 'cat': 'python', 'name': first['data'].get('function', 'unknown'),
                'ts': (first['mono_ns'] - origin) / 1000, 'dur': (last['mono_ns'] - first['mono_ns']) / 1000,
                'pid': first['pid'], 'tid': threads[first['thread']],
                'args': {'first_seq': first['seq'], 'last_seq': last['seq'], 'exit': last['kind'],
                         'file': first['data'].get('file'), 'line': first['data'].get('line')}})
        for index, event in enumerate(events):
            if index in consumed:
                continue
            data, kind = event['data'], event['kind']
            key = (event['thread'], data.get('span_id') if isinstance(data.get('span_id'), str) else None)
            if kind == 'span_start' and isinstance(data.get('span_id'), str):
                spans[key] = event
            elif kind == 'span_end' and key in spans:
                first = spans.pop(key)
                result.append({'ph': 'X', 'cat': 'cooperative', 'name': first['data'].get('name', 'span'),
                    'ts': (first['mono_ns'] - origin) / 1000, 'dur': (event['mono_ns'] - first['mono_ns']) / 1000,
                    'pid': event['pid'], 'tid': threads[event['thread']],
                    'args': {**first['data'], 'first_seq': first['seq'], 'last_seq': event['seq']}})
            # Keep all non-frame events, including span boundaries and unknown
            # frame boundaries, as observed instants. This retains their data.
            result.append({'ph': 'i', 's': 't', 'cat': 'recorded', 'name': kind,
                'ts': (event['mono_ns'] - origin) / 1000, 'pid': event['pid'], 'tid': threads[event['thread']],
                'args': {**data, 'seq': event['seq']}})
        result.sort(key=lambda e: (e['ts'], -e.get('dur', 0)))
        return {'traceEvents': result, 'displayTimeUnit': 'ms', 'raven': provenance}

    if snapshot['covered'].get('adapter') not in {'python-profile', 'python-sys-monitoring'}:
        raise ValueError('Speedscope requires recorded Python frame events; use Perfetto for cooperative/native timelines')
    if not pairs:
        raise ValueError('No matched Python frame intervals are available for an evented speedscope view')
    frames, frame_ids, profiles = [], {}, {}
    closes = {closing: opening for opening, closing in pairs.items()}
    for index, event in enumerate(events):
        opening = index if index in pairs else closes.get(index)
        if opening is None:
            continue
        first = events[opening]
        data = first['data']
        key = (data['function'], data.get('file') if isinstance(data.get('file'), str) else None,
               data.get('line') if type(data.get('line')) is int else None)
        if key not in frame_ids:
            frame_ids[key] = len(frames)
            frame = {'name': str(key[0])}
            if isinstance(key[1], str):
                frame['file'] = key[1]
            if type(key[2]) is int and key[2] > 0:
                frame['line'] = key[2]
            frames.append(frame)
        profiles.setdefault(event['thread'], []).append({'type': 'O' if index in pairs else 'C',
            'at': event['mono_ns'] - origin, 'frame': frame_ids[key]})
    return {'$schema': 'https://www.speedscope.app/file-format-schema.json',
        'name': 'Raven recorded Python intervals', 'exporter': 'Raven', 'activeProfileIndex': 0,
        'shared': {'frames': frames}, 'profiles': [{'type': 'evented', 'name': 'Thread ' + str(threads[thread]),
            'unit': 'nanoseconds', 'startValue': rows[0]['at'], 'endValue': rows[-1]['at'], 'events': rows}
            for thread, rows in profiles.items()], 'raven': provenance}


def export(path, output, *, format='perfetto', managed=False):
    snapshot = protocol.read_trace(path, max_events=512 if managed else protocol.MAX_EVENTS,
                                   max_bytes=256 << 10 if managed else protocol.MAX_BYTES)
    document = convert(snapshot, format)
    raw = json.dumps(document, ensure_ascii=True, allow_nan=False, separators=(',', ':')).encode() + b'\n'
    if len(raw) > (2 << 20 if managed else 32 << 20):
        raise ValueError('Export exceeds its output byte budget; choose a smaller recording prefix')
    output = Path(output).expanduser()
    fd, temporary = tempfile.mkstemp(prefix='.raven-trace-', dir=output.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        # Publish only a complete file, exclusively. Failed writes cannot leave
        # an apparent successful view or remove a concurrently replaced target.
        os.link(temporary, output)
    finally:
        os.unlink(temporary)
    return {'path': str(output), 'format': format, 'read_seq': snapshot['read_seq'],
            'source_status': snapshot['status'], 'bytes': len(raw), 'warnings': snapshot['warnings']}
