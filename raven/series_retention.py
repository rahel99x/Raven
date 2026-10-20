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
import sqlite3
import json
import tempfile
import itertools

from . import series_blocks as B, series_storage as S

SCHEMA = 'raven.retention/v1'
DEFAULTS = {'raw_seconds': 86400, 'ten_second_seconds': 604800, 'minute_seconds': 2592000,
            'gap_seconds': 2., 'protect_raw': True}
MAX_RECORDS = 1000000
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
        if key not in buckets and len(buckets) >= 256: raise ValueError('One time bin exceeds its 256-kind working budget')
        bucket = buckets.setdefault(key, {'fidelity': 'rollup', 'tier_seconds': seconds,
            'start': start, 'end': end, 'kind': key[2], 'count': 0, 'first_time': t, 'last_time': t,
            'source_rows': [ordinal, ordinal], 'metrics': {}, 'observed_gaps': [], 'coverage': 'observations-only'})
        if t - bucket['last_time'] > gap:
            if len(bucket['observed_gaps']) < 2048: bucket['observed_gaps'].append([bucket['last_time'], t])
            else: bucket['additional_gap_count'] = bucket.get('additional_gap_count', 0) + 1
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
    # SQLite supplies a bounded external ordering spool. A 48-hour recording
    # does not require one Python dictionary per observation or per raw output.
    with tempfile.TemporaryDirectory(prefix='raven-retention-') as directory:
        database = sqlite3.connect(str(Path(directory) / 'ordered.sqlite3'))
        try:
            database.execute('PRAGMA journal_mode=OFF')
            database.execute('PRAGMA temp_store=FILE')
            database.execute('PRAGMA cache_size=-4096')
            database.execute('CREATE TABLE observations(t REAL NOT NULL, ordinal INTEGER NOT NULL, row TEXT NOT NULL)')
            database.execute('CREATE INDEX chronological ON observations(t,ordinal)')
            digest = hashlib.sha256(); count = future = 0; diagnostics = {}
            source_rows = S.block_rows(source, cancel=cancel, max_bytes=max_bytes) if source.suffix == '.rvb' else S.json_rows(source, cancel=cancel, max_bytes=max_bytes, skip_invalid=True, diagnostics=diagnostics)
            for ordinal, row in enumerate(source_rows):
                if ordinal >= MAX_RECORDS: raise ValueError('Retention source exceeds its one-million-record working budget')
                if not _number(row.get('t')) or abs(row['t']) > 1e15: raise ValueError('Retention requires bounded epoch timestamps; source preserved')
                encoded = S._canonical(row); digest.update(encoded)
                database.execute('INSERT INTO observations VALUES(?,?,?)', (row['t'], ordinal, encoded.decode('ascii')))
                future += row['t'] > as_of; count += 1
                if count % 4096 == 0:
                    database.commit()
                    if (Path(directory) / 'ordered.sqlite3').stat().st_size > 128 << 20:
                        raise ValueError('Retention ordering spool exceeds its 128 MiB disk budget')
            database.commit()
            adaptive = settings['gap_seconds'] == DEFAULTS['gap_seconds']
            if adaptive:
                database.execute('CREATE TABLE deltas(value REAL NOT NULL)')
                database.execute('CREATE INDEX delta_order ON deltas(value)')
                previous = None; deltas = 0
                for (timestamp,) in database.execute('SELECT t FROM observations ORDER BY t,ordinal'):
                    if previous is not None and timestamp > previous:
                        database.execute('INSERT INTO deltas VALUES(?)', (timestamp - previous,)); deltas += 1
                    previous = timestamp
                if deltas:
                    middle = [row[0] for row in database.execute('SELECT value FROM deltas ORDER BY value LIMIT ? OFFSET ?', (1 if deltas % 2 else 2, (deltas-1)//2))]
                    settings['gap_seconds'] = max(15., 2.5 * math.fsum(middle) / len(middle))
                else: settings['gap_seconds'] = 15.
                database.commit()
            raw_start = -math.inf if settings['protect_raw'] else as_of - settings['raw_seconds']
            ten_start, minute_start = as_of - settings['ten_second_seconds'], as_of - settings['minute_seconds']
            stats = {'raw': 0, 'rollup': 0, 'expired': 0, 'previous': None, 'gaps': [], 'omitted': 0}
            def ordered():
                for ordinal, encoded in database.execute('SELECT ordinal,row FROM observations ORDER BY t,ordinal'):
                    if cancel is not None and cancel.is_set(): raise ValueError('Retention compaction cancelled; prior generation preserved')
                    # This private spool contains only our canonical encoding
                    # of already validated source rows.
                    row = json.loads(encoded); timestamp = row['t']; previous = stats['previous']
                    if previous is not None and timestamp - previous > settings['gap_seconds']:
                        if len(stats['gaps']) < 2048: stats['gaps'].append([previous, timestamp])
                        else: stats['omitted'] += 1
                    stats['previous'] = timestamp
                    yield ordinal, row
            def category(item):
                timestamp = item[1]['t']
                if timestamp >= raw_start: return 'raw', 0
                if timestamp >= ten_start: return 'ten', math.floor(timestamp / 10)
                if timestamp >= minute_start: return 'minute', math.floor(timestamp / 60)
                return 'expired', 0
            values = Path(directory) / 'records.jsonl'
            values_hash = hashlib.sha256(); output_count = 0
            with values.open('wb') as output:
                def emit(row):
                    nonlocal output_count
                    encoded = S._canonical(row); output.write(encoded); values_hash.update(encoded); output_count += 1
                    if output.tell() > 64 << 20: raise ValueError('Retention output spool exceeds its 64 MiB disk budget')
                for (tier, _), group in itertools.groupby(ordered(), category):
                    if tier == 'expired':
                        stats['expired'] += sum(1 for _ in group)
                    elif tier == 'raw':
                        for ordinal, row in group:
                            emit({'fidelity': 'raw', 'source_row': ordinal, 'sample': row}); stats['raw'] += 1
                    else:
                        lower, upper, seconds = (ten_start, raw_start, 10) if tier == 'ten' else (minute_start, ten_start, 60)
                        for row in _aggregate(group, lower, upper, seconds, settings['gap_seconds']):
                            emit(row); stats['rollup'] += 1
            metadata = {'schema': SCHEMA, 'fidelity': 'manifest', 'as_of': as_of, 'policy': settings, 'pinned': settings['protect_raw'],
                'source': {'path': str(source), 'sha256_values': digest.hexdigest(), 'bytes': fingerprint[2], 'records': count, 'preserved': True, 'identity': B._identity(source).hex()},
                'aggregation': 'sample-weighted counts, exact decimal sums of original finite values; no interpolation',
                'unanswerable_from_rollups': UNANSWERABLE, 'observed_gaps': stats['gaps'],
                'gap_threshold_seconds': settings['gap_seconds'], 'future_observations': future,
                'gap_threshold_adaptive': adaptive, 'additional_gap_count': stats['omitted'], 'gap_provenance_complete': not bool(stats['omitted']),
                'ignored_source_records': diagnostics.get('ignored_records', 0), 'raw_records': stats['raw'], 'rollup_records': stats['rollup'],
                'expired_from_working_copy': stats['expired'], 'original_raw_records_preserved': count,
                'units': 'null means unknown; no physical comparison is inferred',
                'working_memory': '4 MiB SQLite page cache plus one 1024-row encoder batch and one time-bin aggregate'}
            def records():
                yield metadata
                with values.open('rb') as stream:
                    for raw in stream: yield json.loads(raw)
            staging_token = staging_token or secrets.token_hex(8)
            if len(staging_token) != 16 or any(c not in '0123456789abcdef' for c in staging_token): raise ValueError('Invalid staging token')
            temporary = target.with_name(target.name + '.stage-' + staging_token)
            total = 0; output = None
            try:
                if apply: output = os.fdopen(B._open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL), 'wb')
                for frame in B.iter_frames(records(), target):
                    if cancel is not None and cancel.is_set(): raise ValueError('Retention compaction cancelled; prior generation preserved')
                    total += len(frame)
                    if total > max_bytes or quota is not None and total > quota: raise ValueError('Retention publication exceeds its disk/byte quota')
                    if output: output.write(frame)
                if S._fingerprint(source) != fingerprint: raise ValueError('Source changed; rebuild retention from a stable snapshot')
                result = {**metadata, 'target': str(target), 'archive_bytes': total, 'status': 'preview'}
                if output:
                    output.flush(); os.fsync(output.fileno()); output.close(); output = None
                    verified = hashlib.sha256(); actual_count = 0
                    def actual():
                        nonlocal actual_count
                        rows=iter(S.block_rows(temporary, identity=B._identity(target), cancel=cancel, max_bytes=max_bytes, max_records=MAX_RECORDS+1))
                        first=next(rows)
                        if first != metadata: raise ValueError('Retention manifest verification failed')
                        yield first
                        for row in rows:
                            verified.update(S._canonical(row)); actual_count += 1
                            yield row
                    _validate(actual())
                    if actual_count != output_count or verified.digest() != values_hash.digest(): raise ValueError('Retention verification failed; prior generation preserved')
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
        finally:
            database.close()


def _load(path, max_bytes=MAX_BYTES):
    rows = []
    for row in S.block_rows(path, max_bytes=min(MAX_BYTES, max_bytes), max_records=MAX_RECORDS+1):
        if len(rows) >= MAX_RECORDS + 1: raise ValueError('Retention archive exceeds its record budget')
        rows.append(row)
    _validate(rows)
    return rows


def _validate(rows):
    iterator = iter(rows)
    metadata = next(iterator, {})
    if metadata.get('schema') != SCHEMA or metadata.get('fidelity') != 'manifest':
        raise ValueError('Unknown retention archive schema')
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
    raw_ids = bytearray((source['records'] + 7) // 8); intervals = {}
    cutoff = metadata['as_of'] - settings['raw_seconds']
    for row in iterator:
        if row.get('fidelity') == 'raw':
            sample = row.get('sample'); ident = row.get('source_row')
            if (not isinstance(sample, dict) or not _number(sample.get('t')) or not row_id(ident)
                    or (raw_ids[ident // 8] & (1 << (ident % 8))) or not settings['protect_raw'] and sample['t'] < cutoff):
                raise ValueError('Invalid raw retention record or repeated source row')
            raw_ids[ident // 8] |= 1 << (ident % 8); raw_count += 1; total += 1
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
            if kind in intervals and start < intervals[kind]: raise ValueError('Overlapping or unordered retention tier intervals')
            intervals[kind] = end
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
    expired = metadata.get('expired_from_working_copy')
    if (metadata.get('raw_records') != raw_count or metadata.get('rollup_records') != rollup_count
            or type(expired) is not int or expired < 0 or total + expired != source['records']):
        raise ValueError('Retention coverage counts do not match the manifest')
    return metadata


def inspect(path, *, max_bytes=MAX_BYTES):
    return _validate(S.block_rows(path, max_bytes=min(MAX_BYTES, max_bytes), max_records=MAX_RECORDS+1))


def pin(path, value, *, max_bytes=MAX_BYTES):
    if type(value) is not bool: raise ValueError('Pin must be boolean')
    metadata = inspect(path, max_bytes=max_bytes); metadata['pinned'] = value
    target = Path(path); temporary = target.with_name(target.name + '.stage-' + secrets.token_hex(8))
    fingerprint = S._fingerprint(target)
    def rows():
        yield metadata
        source = iter(S.block_rows(target, max_bytes=min(MAX_BYTES,max_bytes),max_records=MAX_RECORDS+1))
        next(source)
        yield from source
    try:
        with os.fdopen(B._open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL), 'wb') as output:
            for frame in B.iter_frames(rows(), target): output.write(frame)
            output.flush(); os.fsync(output.fileno())
        if S._fingerprint(target)!=fingerprint: raise ValueError('Retention archive changed while pinning; retry')
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
    fingerprint = S._fingerprint(path)
    metadata = inspect(path, max_bytes=max_bytes)
    rows = iter(S.block_rows(path,max_bytes=min(MAX_BYTES,max_bytes),max_records=MAX_RECORDS+1))
    next(rows)
    output = []; partial = False; boundary = False
    for row in rows:
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
    rows.close()
    if S._fingerprint(path)!=fingerprint: raise ValueError('Retention archive changed while querying; retry')
    return {'schema': SCHEMA, 'source': metadata['source'], 'records': output,
            'partial': partial, 'boundary_rollups': boundary, 'observed_gaps': metadata['observed_gaps'],
            'unanswerable_from_rollups': UNANSWERABLE, 'fidelity': 'mixed-explicit',
            'message': 'Boundary rollups describe their full intervals; they cannot answer a smaller interval exactly.' if boundary else 'Disjoint tier intervals; no interpolation.'}
