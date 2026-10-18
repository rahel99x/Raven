"""Explicit-fidelity working archives; original scientific evidence is preserved.

A single staged, checksummed file contains its policy, source digest and tier
records. Atomic replacement publishes one complete generation. This module
never deletes, truncates or rewrites source recordings or external receipts.
"""
from __future__ import annotations

from decimal import Decimal, localcontext
import hashlib
import math
import os
from pathlib import Path
import secrets
import time

from . import series_blocks as B, series_storage as S

SCHEMA = 'raven.retention/v1'
DEFAULTS = {'raw_seconds': 86400, 'ten_second_seconds': 604800, 'minute_seconds': 2592000,
            'gap_seconds': 2., 'protect_raw': True}
MAX_RECORDS = 50000
MAX_BYTES = 32 << 20
UNANSWERABLE = ['original sample order within a rollup', 'within-bin quantiles',
                'cross-metric covariance', 'event order or causality', 'time-weighted integrals',
                'rates from aggregated counters', 'values inside unobserved intervals']


def policy(value=None):
    result = {**DEFAULTS, **(value or {})}
    if set(result) != set(DEFAULTS) or type(result['protect_raw']) is not bool:
        raise ValueError('Invalid retention policy fields')
    for name in ('raw_seconds', 'ten_second_seconds', 'minute_seconds', 'gap_seconds'):
        v = result[name]
        if type(v) not in (int, float) or not math.isfinite(v) or not 0 < v <= 31536000:
            raise ValueError('Retention durations must be finite, positive and at most one year')
    if not result['raw_seconds'] <= result['ten_second_seconds'] <= result['minute_seconds']:
        raise ValueError('Retention ages must satisfy raw <= ten-second <= minute')
    return result


def target_path(source):
    return str(source).rsplit('.', 1)[0] + '.rvt'


def _number(value):
    if type(value) not in (int, float): return False
    if type(value) is int: return True
    return math.isfinite(value)


def _metrics(row):
    kind = row.get('k', '')
    known = {'cpu': 'fraction', 'eff': 'fraction', 'rss': 'bytes', 'cpu_time': 'seconds'} if kind == 'live' else {}
    for key, value in row.items():
        if key not in ('t', 'k') and _number(value):
            yield key, value, known.get(key)
    if kind == 'gpu' and isinstance(row.get('gpu'), dict):
        for device, values in row['gpu'].items():
            if isinstance(values, list):
                for name, unit, value in zip(('util', 'used', 'total'), ('%', 'MiB', 'MiB'), values):
                    if _number(value): yield 'gpu/' + device + '/' + name, value, unit


def _aggregate(rows, lower, upper, seconds, gap):
    buckets = {}
    for ordinal, row in rows:
        t = row['t']
        if not isinstance(row.get('k', ''), str) or len(row.get('k', '')) > 256: raise ValueError('Invalid native recording kind')
        start = max(lower, math.floor(t / seconds) * seconds)
        end = min(upper, (math.floor(t / seconds) + 1) * seconds)
        key = start, end, str(row.get('k', ''))
        bucket = buckets.setdefault(key, {'fidelity': 'rollup', 'tier_seconds': seconds,
            'start': start, 'end': end, 'kind': key[2], 'count': 0, 'first_time': t, 'last_time': t,
            'source_rows': [ordinal, ordinal], 'metrics': {}, 'observed_gaps': [], 'coverage': 'observations-only'})
        if t - bucket['last_time'] > gap:
            bucket['observed_gaps'].append([bucket['last_time'], t])
        bucket['last_time'] = t; bucket['count'] += 1; bucket['source_rows'][1] = ordinal
        seen_metrics = set()
        for metric, value, unit in _metrics(row):
            if len(metric) > 512: raise ValueError('Metric name exceeds its bound')
            if metric in seen_metrics: raise ValueError('Ambiguous duplicate metric field: ' + metric)
            seen_metrics.add(metric)
            stat = bucket['metrics'].setdefault(metric, {'unit': unit, 'count': 0,
                'min': value, 'max': value, 'min_source_row': ordinal, 'max_source_row': ordinal, '_sum': Decimal(0)})
            if value < stat['min']: stat['min'], stat['min_source_row'] = value, ordinal
            if value > stat['max']: stat['max'], stat['max_source_row'] = value, ordinal
            stat['count'] += 1
            # Exact finite binary64/int addition in a context larger than the
            # bounded representation (up to 1074 binary exponent bits).
            with localcontext() as context:
                context.prec = 5000
                stat['_sum'] += Decimal(value)
    result = []
    for key in sorted(buckets):
        bucket = buckets[key]
        for stat in bucket['metrics'].values():
            stat['sum_decimal'] = str(stat.pop('_sum'))
            stat['missing_count'] = bucket['count'] - stat['count']
        result.append(bucket)
    return result


def build(source, *, settings=None, as_of=None, apply=False, cancel=None, quota=None, max_bytes=MAX_BYTES, staging_token=None):
    max_bytes = min(max_bytes, MAX_BYTES)
    if type(max_bytes) is not int or max_bytes <= 0: raise ValueError('Invalid retention byte budget')
    settings = policy(settings)
    as_of = time.time() if as_of is None else as_of
    if not _number(as_of) or abs(as_of) > 1e15: raise ValueError('Retention observation time must be bounded epoch seconds')
    source = Path(source).absolute()
    if source.suffix not in ('.jsonl', '.rvb'):
        raise ValueError('Retention requires an original JSONL or compact raw recording')
    target = Path(target_path(source))
    fingerprint = S._fingerprint(source)
    existing = None
    if target.exists():
        existing = inspect(target, max_bytes=max_bytes)
        if existing['pinned'] and not settings['protect_raw']:
            raise ValueError('This working archive is pinned; explicitly unpin it before reducing raw retention')
    digest = hashlib.sha256(); rows = []; future = 0
    source_rows = S.block_rows(source, cancel=cancel, max_bytes=max_bytes) if source.suffix == '.rvb' else S.json_rows(source, cancel=cancel, max_bytes=max_bytes)
    for ordinal, row in enumerate(source_rows):
        if ordinal >= MAX_RECORDS: raise ValueError('Retention source exceeds 50000-record working budget')
        digest.update(S._canonical(row))
        if not _number(row.get('t')) or abs(row['t']) > 1e15: raise ValueError('Retention requires bounded epoch timestamps; source preserved')
        if row['t'] > as_of: future += 1
        rows.append((ordinal, row))
    # Sorting is stable and retains physical source-row provenance; late
    # corrections rebuild the entire bounded generation instead of merging
    # aggregates that can no longer be subtracted exactly.
    rows.sort(key=lambda item: item[1]['t'])
    raw_start = -math.inf if settings['protect_raw'] else as_of - settings['raw_seconds']
    ten_start, minute_start = as_of - settings['ten_second_seconds'], as_of - settings['minute_seconds']
    raw, ten, minute = [], [], []
    expired = 0; gaps = []
    previous = None
    for ordinal, row in rows:
        t = row['t']
        if previous is not None and t - previous > settings['gap_seconds']:
            gaps.append([previous, t])
            if len(gaps) > 2048: raise ValueError('Gap provenance budget exceeded; raw source preserved')
        previous = t
        if t >= raw_start: raw.append({'fidelity': 'raw', 'source_row': ordinal, 'sample': row})
        elif t >= ten_start: ten.append((ordinal, row))
        elif t >= minute_start: minute.append((ordinal, row))
        else: expired += 1
    rollups = (_aggregate(minute, minute_start, ten_start, 60, settings['gap_seconds']) +
               _aggregate(ten, ten_start, raw_start, 10, settings['gap_seconds']))
    metadata = {'schema': SCHEMA, 'fidelity': 'manifest', 'as_of': as_of, 'policy': settings, 'pinned': settings['protect_raw'],
        'source': {'path': str(source), 'sha256_values': digest.hexdigest(), 'bytes': fingerprint[2],
                   'records': len(rows), 'preserved': True, 'identity': B._identity(source).hex()},
        'aggregation': 'sample-weighted counts, exact decimal sums of original finite values; no interpolation',
        'unanswerable_from_rollups': UNANSWERABLE, 'observed_gaps': gaps,
        'gap_threshold_seconds': settings['gap_seconds'], 'future_observations': future,
        'raw_records': len(raw), 'rollup_records': len(rollups), 'expired_from_working_copy': expired,
        'original_raw_records_preserved': len(rows), 'units': 'null means unknown; no physical comparison is inferred'}
    records = [metadata, *rollups, *raw]
    staging_token = staging_token or secrets.token_hex(8)
    if len(staging_token) != 16 or any(c not in '0123456789abcdef' for c in staging_token): raise ValueError('Invalid staging token')
    temporary = target.with_name(target.name + '.stage-' + staging_token)
    total = 0; output = None
    try:
        if apply: output = os.fdopen(B._open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL), 'wb')
        for frame in B.frames(records, target):
            if cancel is not None and cancel.is_set(): raise ValueError('Retention compaction cancelled; prior generation preserved')
            total += len(frame)
            if total > max_bytes or quota is not None and total > quota:
                raise ValueError('Retention publication exceeds its disk/byte quota')
            if output: output.write(frame)
        if S._fingerprint(source) != fingerprint: raise ValueError('Source changed; rebuild retention from a stable snapshot')
        result = {**metadata, 'target': str(target), 'archive_bytes': total, 'status': 'preview'}
        if output:
            output.flush(); os.fsync(output.fileno()); output.close(); output = None
            actual = list(S.block_rows(temporary, identity=B._identity(target), cancel=cancel, max_bytes=max_bytes))
            _validate(actual)
            if actual != records: raise ValueError('Retention verification failed; prior generation preserved')
            if S._fingerprint(source) != fingerprint: raise ValueError('Source changed during verification')
            os.replace(temporary, target)
            fd = os.open(target.parent, os.O_DIRECTORY | os.O_RDONLY)
            try: os.fsync(fd)
            finally: os.close(fd)
            result['status'] = 'published'
        return result
    finally:
        if output: output.close()
        try: temporary.unlink()
        except FileNotFoundError: pass


def _load(path, max_bytes=MAX_BYTES):
    rows = []
    for row in S.block_rows(path, max_bytes=min(MAX_BYTES, max_bytes)):
        if len(rows) >= MAX_RECORDS + 1: raise ValueError('Retention archive exceeds its record budget')
        rows.append(row)
    return _validate(rows)


def _validate(rows):
    if not rows or rows[0].get('schema') != SCHEMA or rows[0].get('fidelity') != 'manifest':
        raise ValueError('Unknown retention archive schema')
    metadata = rows[0]
    settings = policy(metadata.get('policy'))
    if type(metadata.get('pinned')) is not bool: raise ValueError('Invalid retention pin state')
    source = metadata.get('source')
    if (not isinstance(source, dict) or not isinstance(source.get('path'), str) or len(source['path']) > 4096
            or type(source.get('records')) is not int or not 0 <= source['records'] <= MAX_RECORDS
            or any(not isinstance(source.get(key), str) or len(source[key]) != 64
                   or any(c not in '0123456789abcdef' for c in source[key]) for key in ('sha256_values', 'identity'))
            or not _number(metadata.get('as_of'))):
        raise ValueError('Invalid retention source identity or time bounds')
    def row_id(value):
        return type(value) is int and 0 <= value < source['records']
    def gaps(value):
        return (isinstance(value, list) and len(value) <= 2048 and
            all(isinstance(pair, list) and len(pair) == 2 and all(_number(t) for t in pair)
                and pair[0] < pair[1] for pair in value))
    if not gaps(metadata.get('observed_gaps')): raise ValueError('Invalid retained gap provenance')
    raw_count = rollup_count = total = 0
    raw_ids = set(); intervals = {}
    cutoff = metadata['as_of'] - settings['raw_seconds']
    for row in rows[1:]:
        if row.get('fidelity') == 'raw':
            sample = row.get('sample'); ident = row.get('source_row')
            if (not isinstance(sample, dict) or not _number(sample.get('t')) or not row_id(ident)
                    or ident in raw_ids or not settings['protect_raw'] and sample['t'] < cutoff):
                raise ValueError('Invalid raw retention record or repeated source row')
            raw_ids.add(ident); raw_count += 1; total += 1
        elif row.get('fidelity') == 'rollup':
            count = row.get('count'); start = row.get('start'); end = row.get('end')
            kind = row.get('kind'); metric_values = row.get('metrics'); provenance = row.get('source_rows')
            if (settings['protect_raw'] or row.get('tier_seconds') not in (10, 60)
                    or not all(_number(t) for t in (start, end, row.get('first_time'), row.get('last_time')))
                    or not start <= row['first_time'] <= row['last_time'] < end <= cutoff
                    or end - start > row['tier_seconds']
                    or type(count) is not int or not 1 <= count <= source['records']
                    or not isinstance(kind, str) or len(kind) > 256
                    or not isinstance(metric_values, dict) or len(metric_values) > 256
                    or not isinstance(provenance, list) or len(provenance) != 2 or not all(row_id(v) for v in provenance)
                    or not gaps(row.get('observed_gaps'))):
                raise ValueError('Invalid rollup count, time interval or provenance')
            intervals.setdefault(kind, []).append((start, end))
            for name, stat in metric_values.items():
                if (not isinstance(name, str) or len(name) > 512 or not isinstance(stat, dict)
                        or type(stat.get('count')) is not int or not 1 <= stat['count'] <= count
                        or stat.get('missing_count') != count - stat['count']
                        or not _number(stat.get('min')) or not _number(stat.get('max')) or stat['min'] > stat['max']
                        or not row_id(stat.get('min_source_row')) or not row_id(stat.get('max_source_row'))
                        or stat.get('unit') is not None and (not isinstance(stat['unit'], str) or len(stat['unit']) > 128)
                        or not isinstance(stat.get('sum_decimal'), str) or len(stat['sum_decimal']) > 6000):
                    raise ValueError('Invalid rollup statistic or units')
                try: valid_sum = Decimal(stat['sum_decimal']).is_finite()
                except Exception: valid_sum = False
                if not valid_sum: raise ValueError('Invalid exact decimal aggregate')
            total += count; rollup_count += 1
        else:
            raise ValueError('Unknown retention record fidelity')
    for values in intervals.values():
        values.sort()
        if any(right[0] < left[1] for left, right in zip(values, values[1:])):
            raise ValueError('Overlapping retention tier intervals')
    expired = metadata.get('expired_from_working_copy')
    if (metadata.get('raw_records') != raw_count or metadata.get('rollup_records') != rollup_count
            or type(expired) is not int or expired < 0 or total + expired != source['records']):
        raise ValueError('Retention coverage counts do not match the manifest')
    return rows


def inspect(path, *, max_bytes=MAX_BYTES):
    return _load(path, max_bytes)[0]


def pin(path, value, *, max_bytes=MAX_BYTES):
    if type(value) is not bool: raise ValueError('Pin must be boolean')
    rows = _load(path, max_bytes); rows[0]['pinned'] = value
    target = Path(path); temporary = target.with_name(target.name + '.stage-' + secrets.token_hex(8))
    try:
        with os.fdopen(B._open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL), 'wb') as output:
            for frame in B.frames(rows, target): output.write(frame)
            output.flush(); os.fsync(output.fileno())
        os.replace(temporary, target)
        fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)
        return {'status': 'published', 'protected_raw': value, 'source_preserved': True}
    finally:
        try: temporary.unlink()
        except FileNotFoundError: pass


def query(path, *, start=None, end=None, limit=10000, max_bytes=MAX_BYTES):
    """Return disjoint raw/rollup records; never clip aggregates as exact data."""
    if type(limit) is not int or not 1 <= limit <= MAX_RECORDS: raise ValueError('Invalid retention result limit')
    if any(value is not None and not _number(value) for value in (start, end)): raise ValueError('Invalid query time')
    if start is not None and end is not None and start >= end: raise ValueError('Query start must precede end')
    rows = _load(path, max_bytes); metadata = rows[0]
    output = []; partial = False; boundary = False
    for row in rows[1:]:
        raw = row['fidelity'] == 'raw'
        lower = row['sample']['t'] if raw else row['start']
        upper = lower if raw else row['end']
        if start is not None and (lower < start if raw else upper <= start): continue
        if end is not None and lower >= end: continue
        if len(output) >= limit: partial = True; break
        item = dict(row)
        if not raw and (start is not None and lower < start or end is not None and upper > end):
            item['boundary_partial'] = True; boundary = True
        output.append(item)
    return {'schema': SCHEMA, 'source': metadata['source'], 'records': output,
            'partial': partial, 'boundary_rollups': boundary, 'observed_gaps': metadata['observed_gaps'],
            'unanswerable_from_rollups': UNANSWERABLE, 'fidelity': 'mixed-explicit',
            'message': 'Boundary rollups describe their full intervals; they cannot answer a smaller interval exactly.' if boundary else 'Disjoint tier intervals; no interpolation.'}
