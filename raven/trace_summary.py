"""Experimental query contracts over verified, matched trace intervals.

Integer dyadic bins give deterministic *value* bounds for nearest-rank
quantiles. They provide no rank-error, event-order or causal certificate.
"""
from __future__ import annotations

import argparse
import heapq
import json
import math
import os
from pathlib import Path
import tempfile

from . import trace_protocol as protocol
from .trace_export import _pairs

SCHEMA = 'raven.trace-summary/v1'
MAX_SOURCES = 16
MAX_GROUPS = 64
MAX_BINS = 8192
MAX_BYTES = 4 << 20
TOP_K = 32
SCOPE = 'Matched intervals in each verified retained prefix; exact does not mean whole-run complete'
PARAMETERS = dict(algorithm='dyadic-integer-duration', significant_bits=7,
                  top_k=TOP_K, quantile='nearest-rank', units='nanoseconds')


def bin_bounds(value):
    shift = max(0, value.bit_length() - PARAMETERS['significant_bits'])
    bucket = value >> shift
    return bucket << shift, ((bucket + 1) << shift) - 1


def _aggregate():
    return dict(count=0, sum=0, min=None, max=None, bins={}, top=[])


def _add(target, value):
    lower, upper = bin_bounds(value)
    key = (lower, upper)
    target['bins'][key] = target['bins'].get(key, 0) + 1
    target['count'] += 1
    target['sum'] += value
    target['min'] = value if target['min'] is None else min(target['min'], value)
    target['max'] = value if target['max'] is None else max(target['max'], value)
    if len(target['top']) < TOP_K:
        heapq.heappush(target['top'], value)
    elif value > target['top'][0]:
        heapq.heapreplace(target['top'], value)


def summarize(snapshot):
    if snapshot['status'] == 'INVALID':
        raise ValueError('Invalid journal cannot provide a summary')
    events = snapshot['events']
    if not events:
        raise ValueError('No retained verified events')
    pairs, omitted = _pairs(events)
    spans = {}
    for index, event in enumerate(events):
        data = event['data']
        span = data.get('span_id')
        if not isinstance(span, str):
            continue
        key = event['thread'], span
        if event['kind'] == 'span_start':
            if key in spans:
                omitted += 1
            spans[key] = index
        elif event['kind'] == 'span_end':
            opening = spans.pop(key, None)
            if opening is not None:
                pairs[opening] = index
            else:
                omitted += 1
    omitted += len(spans)
    groups = {}
    bins = 0
    for opening, closing in sorted(pairs.items()):
        first, last = events[opening], events[closing]
        duration = last['mono_ns'] - first['mono_ns']
        if duration < 0:
            raise ValueError('Negative interval in trace')
        data = first['data']
        phase = data.get('phase', data.get('name', data.get('function', 'unknown')))
        phase = phase if isinstance(phase, str) and 0 < len(phase) <= 256 else 'unknown'
        rank = data.get('rank')
        rank = rank if type(rank) is int and 0 <= rank <= 1_000_000 else 'unknown'
        key = phase, rank
        if key not in groups:
            if len(groups) == MAX_GROUPS:
                raise ValueError('Summary exceeds 64 phase/rank groups; narrow the source')
            groups[key] = _aggregate()
        aggregate = groups[key]
        before = len(aggregate['bins'])
        _add(aggregate, duration)
        bins += len(aggregate['bins']) - before
        if bins > MAX_BINS:
            raise ValueError('Summary histogram budget exceeded; raw evidence remains available')
    encoded = []
    for (phase, rank), aggregate in groups.items():
        encoded.append(dict(phase=phase, rank=rank, **{k: v for k, v in aggregate.items() if k not in {'bins', 'top'}},
                            bins=[[lo, hi, n] for (lo, hi), n in sorted(aggregate['bins'].items())],
                            top=sorted(aggregate['top'], reverse=True)))
    source = dict(identity=snapshot['identity'], first_seq=events[0]['seq'], last_seq=events[-1]['seq'],
                  digest=snapshot['sha256'], time_bounds=[events[0]['mono_ns'], events[-1]['mono_ns']],
                  coverage=snapshot['covered'], source_status=snapshot['status'], evicted=snapshot['evicted'],
                  unmatched_events=omitted, groups=encoded, warnings=snapshot['warnings'])
    return document([source])


def document(sources):
    return dict(schema=SCHEMA, parameters=dict(PARAMETERS), sources=sources,
                scope=SCOPE,
                rank_basis='Emitter-declared rank on opening events; unknown when absent',
                retention=dict(raw='unchanged by summarization; independent explicit journal retention policy',
                               summary='private user-selected file; delete by source digest or remove file'),
                limitations=['No event order, causality, inclusive/exclusive CPU accounting or unknown boundary inference.',
                             'Function/name labels stand in for phase only when explicitly present; not inferred segmentation.',
                             'Quantile bounds are duration-value intervals, not universal rank-error bounds.'])


def validate(value):
    if not isinstance(value, dict) or value.get('schema') != SCHEMA or value.get('parameters') != PARAMETERS:
        raise ValueError('Unsupported summary schema or sketch parameters')
    sources = value.get('sources')
    if not isinstance(sources, list) or len(sources) > MAX_SOURCES:
        raise ValueError('Summary source budget exceeded')
    bins, groups = 0, 0
    intervals = {}
    for source in sources:
        if not isinstance(source, dict):
            raise ValueError('Invalid summary source')
        protocol.validate_identity(source.get('identity'))
        coverage = source.get('coverage')
        if not isinstance(coverage, dict):
            raise ValueError('Missing source coverage')
        protocol._bounded_data(coverage)
        protocol.text(coverage.get('scope'), 'coverage scope', 8192)
        if 'complete' in coverage and coverage['complete'] is not None and type(coverage['complete']) is not bool:
            raise ValueError('Invalid source coverage completeness')
        status = source.get('source_status')
        if not isinstance(status, str) or status not in {'COMPLETE', 'PARTIAL', 'LIVE'}:
            raise ValueError('Invalid source status')
        warnings = source.get('warnings')
        if not isinstance(warnings, list) or len(warnings) > 64 or any(not isinstance(w, str) for w in warnings):
            raise ValueError('Invalid source warnings')
        protocol._bounded_data(warnings)
        if any(type(source.get(key)) is not int or not 0 <= source[key] < 1 << 63
               for key in ('evicted', 'unmatched_events')):
            raise ValueError('Invalid source loss counters')
        digest = source.get('digest')
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            raise ValueError('Invalid source digest')
        first, last = source.get('first_seq'), source.get('last_seq')
        bounds = source.get('time_bounds')
        if type(first) is not int or type(last) is not int or not 1 <= first <= last < 1 << 63:
            raise ValueError('Invalid source sequence range')
        if not isinstance(bounds, list) or len(bounds) != 2 or any(type(v) is not int or not 0 <= v < 1 << 63 for v in bounds) or bounds[0] > bounds[1]:
            raise ValueError('Invalid source time bounds')
        stream = tuple(source['identity'][key] for key in protocol.IDENTITY_KEYS)
        previous = intervals.setdefault(stream, [])
        if any(first <= end and start <= last for start, end in previous):
            raise ValueError('Overlapping source intervals would double count evidence')
        previous.append((first, last))
        seen = set()
        source_groups = source.get('groups')
        if not isinstance(source_groups, list) or len(source_groups) > MAX_GROUPS:
            raise ValueError('Invalid summary groups')
        for group in source_groups:
            if not isinstance(group, dict):
                raise ValueError('Invalid summary group')
            phase, rank = group.get('phase'), group.get('rank')
            if not isinstance(phase, str) or not 0 < len(phase) <= 256 or not (rank == 'unknown' or type(rank) is int and 0 <= rank <= 1_000_000):
                raise ValueError('Invalid phase/rank identity')
            if (phase, rank) in seen:
                raise ValueError('Duplicate phase/rank group')
            seen.add((phase, rank)); groups += 1
            count, total = group.get('count'), group.get('sum')
            if type(count) is not int or not 1 <= count <= 1_000_000 or type(total) is not int or not 0 <= total <= count * ((1 << 63) - 1):
                raise ValueError('Invalid aggregate counters')
            minimum, maximum = group.get('min'), group.get('max')
            if type(minimum) is not int or type(maximum) is not int or not 0 <= minimum <= maximum < 1 << 63 or not minimum * count <= total <= maximum * count:
                raise ValueError('Invalid aggregate extrema')
            histogram = group.get('bins')
            if not isinstance(histogram, list):
                raise ValueError('Missing histogram')
            actual, prev, lower_sum, upper_sum = 0, -1, 0, 0
            for row in histogram:
                if not isinstance(row, list) or len(row) != 3 or any(type(v) is not int for v in row):
                    raise ValueError('Invalid histogram bin')
                lower, upper, number = row
                if not 0 <= lower <= upper < 1 << 63 or number < 1 or lower <= prev or bin_bounds(lower) != (lower, upper):
                    raise ValueError('Invalid or overlapping dyadic bin')
                actual += number; prev = upper; bins += 1
                lower_sum += lower * number; upper_sum += upper * number
            top = group.get('top')
            if actual != count or not lower_sum <= total <= upper_sum or not isinstance(top, list) or len(top) != min(count, TOP_K) or any(type(v) is not int or not minimum <= v <= maximum for v in top) or top != sorted(top, reverse=True) or top[0] != maximum:
                raise ValueError('Inconsistent histogram/top-k aggregate')
            frequencies = {(lo, hi): number for lo, hi, number in histogram}
            top_frequencies = {}
            for duration in top:
                key = bin_bounds(duration)
                top_frequencies[key] = top_frequencies.get(key, 0) + 1
            if (bin_bounds(minimum) not in frequencies or bin_bounds(maximum) not in frequencies
                    or any(number > frequencies.get(key, 0) for key, number in top_frequencies.items())
                    or any(lo > top[-1] and top_frequencies.get((lo, hi), 0) != number
                           for lo, hi, number in histogram)
                    or count <= TOP_K and (sum(top) != total or top[-1] != minimum
                                         or top_frequencies != frequencies)):
                raise ValueError('Extrema/top-k contradict the retained histogram')
    if bins > MAX_BINS or groups > MAX_GROUPS:
        raise ValueError('Merged summary exceeds bounded histogram/group budget')
    return value


def merge(documents):
    sources = []
    for value in documents:
        validate(value)
        sources.extend(value['sources'])
    return validate(document(sources))


def delete(value, digest):
    validate(value)
    if not any(source['digest'] == digest for source in value['sources']):
        raise ValueError('Source digest is absent; nothing deleted')
    return document([source for source in value['sources'] if source['digest'] != digest])


def answer(value, query, *, phase=None, rank=None, quantile=.95, k=10, window=None):
    """Return answerability before the value; partial-window queries abstain."""
    validate(value)
    unknown = lambda reason: dict(answerability='unanswerable', reason=reason)
    if query not in {'count', 'sum', 'min', 'max', 'quantile', 'top-k'}:
        return unknown('This summary does not preserve that query family, event order or causality')
    if not math.isfinite(quantile) or not 0 <= quantile <= 1 or type(k) is not int or not 1 <= k <= TOP_K:
        raise ValueError('Quantile must be 0–1 and top-k must be 1–32')
    selected = []
    domains = {(s['identity']['node'], s['identity']['boot_id']) for s in value['sources']}
    if window is not None:
        if len(window) != 2 or any(type(v) is not int for v in window) or window[0] > window[1]:
            raise ValueError('Window needs increasing monotonic nanoseconds')
        if len(domains) > 1:
            return unknown('A monotonic time window cannot cross node/boot clock domains')
    for source in value['sources']:
        start, end = source['time_bounds']
        if window is not None:
            if end < window[0] or start > window[1]:
                continue
            if not window[0] <= start <= end <= window[1]:
                return unknown('Only complete retained source windows are deletable/queryable; boundary-cut events were not retained')
        selected.extend(g for g in source['groups'] if (phase is None or g['phase'] == phase) and (rank is None or g['rank'] == rank))
    if not selected:
        return unknown('No matching retained intervals; absence is not a zero-duration measurement')
    result = dict(answerability='exact', units='count' if query == 'count' else 'nanoseconds',
                  scope=SCOPE, observations=sum(g['count'] for g in selected))
    if query in {'count', 'sum'}:
        result['value'] = sum(g[query] for g in selected)
    elif query in {'min', 'max'}:
        result['value'] = (min if query == 'min' else max)(g[query] for g in selected)
    elif query == 'top-k':
        result['value'] = sorted((v for g in selected for v in g['top']), reverse=True)[:k]
    else:
        target = max(1, math.ceil(quantile * result['observations']))
        histogram = {}
        for group in selected:
            for lower, upper, count in group['bins']:
                histogram[(lower, upper)] = histogram.get((lower, upper), 0) + count
        cumulative = 0
        for (lower, upper), count in sorted(histogram.items()):
            cumulative += count
            if cumulative >= target:
                result.update(answerability='exact' if lower == upper else 'approximate',
                              value=(lower + upper) // 2, lower=lower, upper=upper,
                              error_contract='Exact nearest-rank duration lies inside [lower, upper]; no rank-error claim')
                break
    return result


def read(path):
    with open(path, 'rb') as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError('Summary exceeds 4 MiB')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate summary key')
            result[key] = value
        return result
    try:
        return validate(json.loads(raw, object_pairs_hook=unique))
    except (RecursionError, TypeError, KeyError, OverflowError) as exc:
        raise ValueError('Malformed summary') from exc


def write(path, value):
    validate(value)
    raw = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(',', ':')).encode() + b'\n'
    if len(raw) > MAX_BYTES:
        raise ValueError('Summary exceeds 4 MiB')
    output = Path(path).expanduser()
    fd, temporary = tempfile.mkstemp(prefix='.raven-summary-', dir=output.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        os.link(temporary, output)
    finally:
        os.unlink(temporary)
    return dict(path=str(output), bytes=len(raw), sources=len(value['sources']))


def main(argv=None):
    parser = argparse.ArgumentParser(prog='raven trace summary', description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    create = sub.add_parser('create'); create.add_argument('manifest'); create.add_argument('--output', required=True)
    combine = sub.add_parser('merge'); combine.add_argument('summaries', nargs='+'); combine.add_argument('--output', required=True)
    remove = sub.add_parser('delete'); remove.add_argument('summary'); remove.add_argument('--digest', required=True); remove.add_argument('--output', required=True)
    query = sub.add_parser('query'); query.add_argument('summary'); query.add_argument('query'); query.add_argument('--phase'); query.add_argument('--rank'); query.add_argument('--quantile', type=float, default=.95); query.add_argument('--k', type=int, default=10); query.add_argument('--window', nargs=2, type=int)
    args = parser.parse_args(argv)
    try:
        if args.action == 'create':
            from .raven_execution import router_for
            managed = router_for().managed
            value = summarize(protocol.read_trace(args.manifest, max_events=512 if managed else protocol.MAX_EVENTS,
                              max_bytes=256 << 10 if managed else protocol.MAX_BYTES))
            result = write(args.output, value)
        elif args.action == 'merge':
            if len(args.summaries) > MAX_SOURCES:
                raise ValueError('Choose at most 16 summaries')
            result = write(args.output, merge([read(path) for path in args.summaries]))
        elif args.action == 'delete':
            result = write(args.output, delete(read(args.summary), args.digest))
        else:
            rank = int(args.rank) if args.rank and args.rank != 'unknown' else args.rank
            result = answer(read(args.summary), args.query, phase=args.phase, rank=rank,
                            quantile=args.quantile, k=args.k, window=args.window)
    except (OSError, ValueError) as exc:
        import sys
        print(f'Raven trace summary: {exc}', file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0
