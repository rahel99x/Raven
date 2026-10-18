"""Tier fidelity, bounded compaction and raw source protection under faults."""
from decimal import Decimal
import json
from pathlib import Path
import threading

import pytest
from raven import series_retention as R, series_storage as S


def source(tmp_path, rows):
    p = tmp_path / ('v2-37-' + 'a'*64 + '.jsonl')
    p.write_bytes(b''.join(S._canonical(row) for row in rows))
    return p


def settings(**kw):
    return {'raw_seconds': 100, 'ten_second_seconds': 300, 'minute_seconds': 1000,
            'gap_seconds': 30, 'protect_raw': False, **kw}


def test_tiers_have_disjoint_counts_bounds_units_and_provenance(tmp_path):
    rows = [{'t': t, 'k': 'live', 'cpu': t % 13, 'rss': 10**20, 'custom': t} for t in range(1,1001)]
    p = source(tmp_path, rows)
    before = p.read_bytes()
    preview = R.build(p, settings=settings(), as_of=1000)
    assert preview['status'] == 'preview' and not Path(R.target_path(p)).exists()
    result = R.build(p, settings=settings(), as_of=1000, apply=True)
    assert p.read_bytes() == before and result['raw_records'] == 101
    q = R.query(R.target_path(p))
    counts = sum(r.get('count', 1) for r in q['records'])
    assert counts == len(rows)
    for r in q['records']:
        if r['fidelity'] == 'rollup':
            assert r['start'] < r['end'] <= 900
            assert r['metrics']['cpu']['unit'] == 'fraction' and r['metrics']['custom']['unit'] is None
            assert r['metrics']['rss']['sum_decimal'] == str(Decimal(10**20) * r['count'])
        else:
            assert r['sample']['t'] >= 900
    assert not q['partial'] and q['fidelity'] == 'mixed-explicit'


def test_pinned_raw_cannot_expire_without_explicit_unpin_and_original_stays(tmp_path):
    p = source(tmp_path, [{'t': 1., 'x': 1}, {'t': 999., 'x': 2}])
    first = R.build(p, settings=settings(protect_raw=True), as_of=1000, apply=True)
    assert first['raw_records'] == 2 and first['rollup_records'] == 0
    with pytest.raises(ValueError, match='pinned'):
        R.build(p, settings=settings(), as_of=1000, apply=True)
    R.pin(R.target_path(p), False)
    result = R.build(p, settings=settings(), as_of=1000, apply=True)
    assert result['raw_records'] == 1 and result['rollup_records'] == 1
    assert len(list(S.json_rows(p))) == 2


def test_extremes_missing_irregular_and_late_correction_are_explicit(tmp_path):
    p = source(tmp_path, [{'t': 1., 'x': 1e308}, {'t': 2., 'x': 1e308}, {'t': 3., 'x': None},
                          {'t': 121., 'x': -(10**400)}, {'t': 999., 'x': 0}])
    R.build(p, settings=settings(), as_of=1000, apply=True)
    q = R.query(R.target_path(p)); first = q['records'][0]
    assert Decimal(first['metrics']['x']['sum_decimal']) == Decimal(int(1e308) * 2)
    assert first['metrics']['x']['missing_count'] == 1
    assert q['observed_gaps'] == [[3., 121.], [121., 999.]]
    assert q['records'][1]['metrics']['x']['min'] == -(10**400)
    previous = R.inspect(R.target_path(p))['source']['sha256_values']
    rows = list(S.json_rows(p)); rows[1]['x'] = 1.; p.write_bytes(b''.join(S._canonical(r) for r in rows))
    R.build(p, settings=settings(), as_of=1000, apply=True)
    assert R.inspect(R.target_path(p))['source']['sha256_values'] != previous
    assert R.query(R.target_path(p))['records'][0]['metrics']['x']['min'] == 1.


def test_boundary_query_does_not_claim_clipped_aggregate_exactness(tmp_path):
    p = source(tmp_path, [{'t': t, 'x': t} for t in range(1,1001)])
    R.build(p, settings=settings(), as_of=1000, apply=True)
    q = R.query(R.target_path(p), start=703., end=915.)
    assert q['boundary_rollups'] and q['records'][0]['boundary_partial']
    assert q['records'][0]['start'] == 700
    assert all(r['sample']['t'] < 915 for r in q['records'] if r['fidelity'] == 'raw')
    assert R.query(R.target_path(p), limit=1)['partial']


def test_cancel_quota_and_atomic_replace_failure_keep_prior_generation(tmp_path, monkeypatch):
    p = source(tmp_path, [{'t': 1., 'x': 1}])
    R.build(p, settings=settings(), as_of=1000, apply=True)
    target = Path(R.target_path(p)); old = target.read_bytes(); raw = p.read_bytes()
    stop = threading.Event(); stop.set()
    with pytest.raises(ValueError, match='cancel'): R.build(p, apply=True, cancel=stop)
    with pytest.raises(ValueError, match='quota'): R.build(p, apply=True, quota=1)
    def fail(*args): raise OSError('disk full')
    monkeypatch.setattr(R.os, 'replace', fail)
    with pytest.raises(OSError, match='disk full'): R.build(p, apply=True)
    assert target.read_bytes() == old and p.read_bytes() == raw and not list(tmp_path.glob('*.stage-*'))


def test_corruption_and_bad_timestamp_never_return_silent_partial_tiers(tmp_path):
    p = source(tmp_path, [{'t': 1., 'x': 1}]); R.build(p, apply=True)
    target = Path(R.target_path(p)); target.write_bytes(target.read_bytes()[:-3])
    with pytest.raises(ValueError): R.query(target)
    p = source(tmp_path, [{'t': '2026-01-01', 'x': 1}])
    target.unlink()
    with pytest.raises(ValueError, match='epoch'): R.build(p, apply=True)
    assert not target.exists()


def test_metric_alias_collision_is_not_summed_twice(tmp_path):
    p = source(tmp_path, [{'t': 1, 'k': 'gpu', 'gpu/0/util': 4, 'gpu': {'0': [3, 2, 1]}}])
    with pytest.raises(ValueError, match='duplicate metric'): R.build(p, settings=settings(), as_of=1000)


def test_checksum_valid_malformed_tiers_fail_before_query(tmp_path):
    from raven import series_blocks as B
    p = source(tmp_path, [{'t': 1, 'x': 1}]); R.build(p, settings=settings(), as_of=1000, apply=True)
    target = Path(R.target_path(p)); rows = list(S.block_rows(target))
    del rows[1]['start']
    target.write_bytes(b''.join(B.frames(rows, target)))
    with pytest.raises(ValueError, match='Invalid rollup'): R.query(target)


def test_cli_retention_query_pin_and_owner_lock(tmp_path, capsys):
    from raven.series_cli import main
    directory = tmp_path / 'series'; directory.mkdir()
    p = source(directory, [{'t': 1, 'x': 1}, {'t': 999, 'x': 2}])
    assert main(['retention', str(p), '--as-of', '1000', '--apply']) == 0
    assert main(['pin', R.target_path(p), '--unpin']) == 0
    assert main(['retention', str(p), '--as-of', '1000', '--raw-seconds', '100', '--unprotect-working-copy', '--apply']) == 0
    capsys.readouterr()
    assert main(['query', R.target_path(p), '--start', '0', '--end', '1000']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['fidelity'] == 'mixed-explicit' and any(row['fidelity'] == 'rollup' for row in result['records'])
    assert main(['inspect', R.target_path(p)]) == 0
    from raven.state_ownership import StateOwner
    owner = StateOwner(tmp_path, strict=True)
    try: assert main(['pin', R.target_path(p)]) == 1
    finally: owner.close()
