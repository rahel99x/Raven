"""Bounded, evidence-linked comparison of explicitly identified rank logs.

Emitter headers are association evidence, not authentication. Comparison is
within declared role cohorts and retained source order; clocks never sort rows.
"""
from __future__ import annotations

from collections import Counter
import json

from .log_match_index import template_key

MAX_RANKS = 16
MAX_ROWS = 240
MAX_LINE = 8192
MAX_TEMPLATES = MAX_RANKS * MAX_ROWS
HEADER_BYTES = 4096


def identity(raw, *, attempt, job_id):
    """Require a bounded producer declaration matching the selected run."""
    first, separator, _ = raw.partition(b'\n')
    if not separator or len(first) >= HEADER_BYTES:
        raise ValueError('Missing complete Raven rank header in first 4096 bytes')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate rank-header field')
            result[key] = value
        return result
    try:
        value = json.loads(first, object_pairs_hook=unique)['raven_rank']
    except (ValueError, KeyError, TypeError, RecursionError) as exc:
        raise ValueError('Expected first-line JSON raven_rank declaration') from exc
    if not isinstance(value, dict) or set(value) != {'rank', 'attempt', 'job_id', 'cohort'}:
        raise ValueError('Rank header needs rank, attempt, job_id and cohort')
    if type(value['rank']) is not int or not 0 <= value['rank'] <= 1_000_000:
        raise ValueError('Invalid declared rank')
    if any(not isinstance(value[k], str) or not value[k] or len(value[k]) > 160
           or not value[k].isprintable() for k in ('attempt', 'job_id', 'cohort')):
        raise ValueError('Invalid rank-header identity')
    if value['attempt'] != attempt or value['job_id'] != job_id:
        raise ValueError('Rank header belongs to a different job or run attempt')
    return value


def compare(sources):
    """O(ranks * rows * bounded-line-length) work and retained-window memory.

    Each result identifies the first positional disagreement after a common
    anchor. A strict majority describes observations; it never labels a rank
    incorrect. Truncated inputs require a unique common anchor before comparing.
    """
    if len(sources) > MAX_RANKS:
        raise ValueError(f'Choose at most {MAX_RANKS} rank logs')
    cohorts, seen, notices = {}, set(), []
    for source in sources:
        if source.get('error'):
            notices.append(source['label'] + ': ' + source['error'])
            continue
        header = source['rank_identity']
        rank = header['rank']
        if rank in seen:
            raise ValueError(f'Duplicate rank {rank}; choose one stream per rank')
        seen.add(rank)
        lines = source['lines']
        if len(lines) > MAX_ROWS or any(len(line) > MAX_LINE for line in lines):
            raise ValueError('Rank comparison line/window limit exceeded')
        keys = [template_key(line, rank) for line in lines]
        cohorts.setdefault(header['cohort'], []).append((source, keys))
    results = []
    for cohort, members in sorted(cohorts.items()):
        base = dict(cohort=cohort, ranks=[s['rank_identity']['rank'] for s, _ in members],
                    last_common=None, first_divergence=None, references=[], notices=[])
        if not any(keys for _, keys in members):
            base.update(status='insufficient', notices=['No retained workload events to compare'])
            results.append(base)
            continue
        if len(members) < 2:
            base.update(status='insufficient', notices=['Only one accessible rank in this role cohort'])
            results.append(base)
            continue
        positions = [0] * len(members)
        limited = any(s.get('truncated') or s.get('omitted_lines') for s, _ in members)
        if limited:
            counts = [Counter(keys) for _, keys in members]
            candidates = set(k for k, count in counts[0].items() if count == 1)
            for count in counts[1:]:
                candidates.intersection_update(k for k, n in count.items() if n == 1)
            if not candidates:
                base.update(status='unanswerable', notices=['Truncated windows have no unique common anchor; repeated templates cannot establish alignment'])
                results.append(base)
                continue
            # Earliest worst-case position, with deterministic text tie-break.
            indexes = [{key: i for i, key in enumerate(keys)} for _, keys in members]
            anchor = min(candidates, key=lambda k: (max(index[k] for index in indexes), k))
            positions = [index[anchor] for index in indexes]
            base['notices'].append('Retained-window comparison only; earlier divergence and omitted events are unknown')
        while True:
            current = [keys[pos] if pos < len(keys) else None for (_, keys), pos in zip(members, positions)]
            if all(key is None for key in current):
                base['status'] = 'matched_retained_window'
                break
            if len(set(current)) != 1:
                count = Counter(current)
                key, frequency = count.most_common(1)[0]
                base.update(status='divergence', first_divergence=list(positions),
                            agreement='strict majority' if frequency > len(members) / 2 else 'tie / no majority',
                            majority_template=key if frequency > len(members) / 2 else None)
                if None in current:
                    base['notices'].append('A stream ends here; completion, missing writes and delayed output are not distinguished')
                break
            base['last_common'] = list(positions)
            positions = [p + 1 for p in positions]
        point = base['first_divergence'] or base['last_common']
        if point is not None:
            for (source, keys), pos in zip(members, point):
                base['references'].append(dict(rank=source['rank_identity']['rank'], path=source['path'],
                    original=pos if pos < len(keys) else None, template=keys[pos] if pos < len(keys) else '<end of retained stream>'))
        if any(len(keys) != len(set(keys)) for _, keys in members):
            base['notices'].append('Repeated templates retained in order; equal text does not identify a unique event')
        results.append(base)
    return dict(schema='raven.rank-divergence/v1', cohorts=results, notices=notices,
                association='producer-declared header matched to selected job and run attempt',
                bounds=dict(ranks=MAX_RANKS, rows_per_rank=MAX_ROWS, templates=MAX_TEMPLATES),
                limitations=['Ordered text comparison does not prove causality or identify a faulty rank.',
                            'Timestamps and declared rank prefixes are normalized; semantic numeric values are preserved.',
                            'Unavailable streams and omitted prefixes cannot establish whole-run agreement.'])


def rows(report):
    result = []
    for notice in report['notices']:
        result.append(dict(text=notice, style='yellow'))
    for cohort in report['cohorts']:
        result.append(dict(text=f"{cohort['cohort']}: {cohort['status']} ({len(cohort['ranks'])} ranks)", style='cyan+bold'))
        result.append(dict(text='Last common anchor: ' + str(cohort['last_common']) + ' (retained row indexes)', style='dim'))
        if cohort.get('agreement'):
            result.append(dict(text=cohort['agreement'] + '; this is not a diagnosis', style='yellow'))
        result.extend(dict(text=notice, style='yellow') for notice in cohort['notices'])
        for reference in cohort['references']:
            result.append(dict(text=f"rank {reference['rank']}: {reference['template']}",
                               source_path=reference['path'], original=reference['original'], style=''))
    return result
