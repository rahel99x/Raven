"""Compact storage preserves raw JSON and attempt identity under faults."""
import json
import math
import os
from pathlib import Path
import struct
import threading

import pytest
from hypothesis import given, settings, strategies as st

from raven import series_blocks as B, series_storage as S
from raven.model import Store, Job
from raven.raven_limits import LightLimits

NAME = "v2-37-" + "a" * 64


def job(store):
    record = Job("7", "train", "cpu", "RUNNING", submit="2026-10-17T10:00:00", start="2026-10-17T10:00:01", cluster="lab")
    store.apply_jobs([record])
    details = dict(JobId="7", SubmitTime=record.submit, StartTime=record.start, Restarts="0", Cluster="lab")
    store.details['7'] = details; store.bind_series_scheduler('7', details)


def test_exact_json_values_order_missing_null_and_signed_zero(tmp_path):
    path = tmp_path / (NAME + '.rvb')
    values = [{'t': 1792279500.000001, 'cpu': -0., 'rss': 2**63 + 1, 'gpu': {'0': [None, 0., True]}},
              {'cpu': None, 't': 2**70}, {'t': 3., 'metadata': 'é Ω \\ "'}]
    B.append(path, values)
    result, status = B.read(path)
    assert [S._canonical(row) for row in result] == [S._canonical(row) for row in values]
    assert struct.pack('<d', result[0]['cpu']) == struct.pack('<d', -0.)
    assert status['verified_samples'] == 3 and status['fidelity'] == 'raw'


@pytest.mark.parametrize('cut', [1, 4, 17, 60])
def test_truncated_append_returns_verified_prefix_and_refuses_further_append(tmp_path, cut):
    path = tmp_path / (NAME + '.rvb')
    B.append(path, [{'t': 1., 'cpu': 2.}])
    complete = path.read_bytes()
    frame = next(B.frames([{'t': 2., 'cpu': 3.}], path))
    path.write_bytes(complete + frame[:-cut])
    rows, status = B.read(path)
    assert rows == [{'t': 1., 'cpu': 2.}] and status['status'] == 'error'
    before = path.read_bytes()
    with pytest.raises(ValueError): B.append(path, [{'t': 3.}])
    assert path.read_bytes() == before


def test_corrupt_interior_stops_at_prefix_and_never_skips_to_later_blocks(tmp_path):
    path = tmp_path / (NAME + '.rvb')
    frames = [next(B.frames([{'t': index, 'v': index}], path)) for index in range(3)]
    corrupted = bytearray(frames[1]); corrupted[B.HEADER.size + 2] ^= 1
    path.write_bytes(frames[0] + corrupted + frames[2])
    rows, status = B.read(path)
    assert rows == [{'t': 0, 'v': 0}] and status['blocks'] == 1 and status['status'] == 'error'


def test_filename_rebinding_and_oversized_header_are_refused(tmp_path):
    path = tmp_path / (NAME + '.rvb'); B.append(path, [{'t': 1}])
    other = tmp_path / ('v2-38-' + 'b' * 64 + '.rvb'); other.write_bytes(path.read_bytes())
    assert B.read(other)[1]['status'] == 'error'
    header = B.HEADER.pack(B.MAGIC, B.MAX_RAW + 1, 1, 1, B._identity(path), b'0' * 32)
    path.write_bytes(header)
    assert B.read(path)[1]['status'] == 'error'


def test_copy_verify_preview_publication_and_cancel_keep_source(tmp_path):
    source = tmp_path / (NAME + '.jsonl')
    values = [{'t': index, 'cpu': index % 3} for index in range(1100)]
    original = b''.join(S._canonical(row) for row in values); source.write_bytes(original)
    preview = S.migrate(source)
    assert preview['status'] == 'preview' and not source.with_suffix('.rvb').exists()
    cancel = threading.Event(); cancel.set()
    with pytest.raises(ValueError, match='cancel'): S.migrate(source, apply=True, cancel=cancel)
    assert source.read_bytes() == original and not list(tmp_path.glob('*.stage-*'))
    done = S.migrate(source, apply=True)
    assert done['sha256_values'] == preview['sha256_values'] and done['raw_samples'] == len(values)
    assert B.read(source.with_suffix('.rvb'))[0] == values and source.read_bytes() == original
    with pytest.raises(ValueError, match='exists'): S.migrate(source, apply=True)


def test_incomplete_json_source_and_quota_never_publish_or_truncate(tmp_path):
    source = tmp_path / (NAME + '.jsonl'); source.write_bytes(b'{"t":1}\n{"t":')
    original = source.read_bytes()
    with pytest.raises(ValueError, match='incomplete'): S.migrate(source, apply=True)
    assert source.read_bytes() == original and not source.with_suffix('.rvb').exists()
    source.write_bytes(b'{"t":1}\n')
    with pytest.raises(ValueError, match='quota'): S.migrate(source, apply=True, quota=1)
    assert not source.with_suffix('.rvb').exists()


@pytest.mark.parametrize('managed', [False, True])
def test_store_opt_in_migrates_reopens_and_keeps_compact_authoritative(tmp_path, managed):
    store = Store(str(tmp_path)); job(store); store.record('7', {'t': 1., 'cpu': .1}); original = Path(store._series_path('7')); store.close()
    # Conversion is explicit; changing the preference never starts a hidden
    # bulk migration from the ordinary sampler publication path.
    S.migrate(original, apply=True)
    limits = LightLimits({}, str(tmp_path)) if managed else None
    store = Store(str(tmp_path), series_storage='blocks-v1', raven_limits=limits); job(store)
    store.record('7', {'t': 2., 'cpu': .2}); packed = Path(store._series_path('7')); store.close()
    assert packed.suffix == '.rvb' and json.loads(original.read_text()) == {'t': 1., 'cpu': .1}
    assert B.read(packed)[0] == [{'t': 1., 'cpu': .1}, {'t': 2., 'cpu': .2}]
    store = Store(str(tmp_path)); job(store)
    assert store.series_of('7') == [{'t': 1., 'cpu': .1}, {'t': 2., 'cpu': .2}]
    store.record('7', {'t': 3., 'cpu': .3}); store.close()
    assert B.read(packed)[0][-1] == {'t': 3., 'cpu': .3}
    assert original.read_text().count('\n') == 1


def test_cli_migration_requires_scope_ownership_and_export_is_no_clobber(tmp_path, capsys):
    from raven.series_cli import main
    directory = tmp_path / 'series'; directory.mkdir()
    source = directory / (NAME + '.jsonl'); source.write_bytes(b'{"t":1}\n')
    owner = Store(str(tmp_path))
    assert main(['migrate', str(source), '--apply']) == 1
    assert 'Another Raven writer' in capsys.readouterr().err
    owner.close()
    assert main(['migrate', str(source), '--apply']) == 0
    output = tmp_path / 'export.jsonl'
    assert main(['export', str(source.with_suffix('.rvb')), '--output', str(output)]) == 0
    assert output.read_bytes() == source.read_bytes()
    assert main(['export', str(source.with_suffix('.rvb')), '--output', str(output)]) == 1


@given(st.lists(st.tuples(st.floats(allow_nan=False, allow_infinity=False), st.integers(-(2**100), 2**100)), max_size=40))
@settings(max_examples=30, deadline=None)
def test_arbitrary_finite_float_and_big_integer_round_trips(values):
    rows = [{'t': timestamp, 'value': value} for timestamp, value in values]
    if not rows: return
    encoded = B.encode(rows, b'i' * 32)
    decoded = B.decode(encoded, b'i' * 32)
    assert [S._canonical(row) for row in rows] == [S._canonical(row) for row in decoded]


def test_overflow_json_exponent_is_never_admitted_as_finite_raw_value(tmp_path):
    source = tmp_path / (NAME + '.jsonl'); source.write_bytes(b'{"t":1,"cpu":1e999}\n')
    with pytest.raises(ValueError, match='finite'): S.migrate(source, apply=True)
    assert not source.with_suffix('.rvb').exists()


def test_total_decompressed_budget_stops_verified_prefix(tmp_path, monkeypatch):
    path = tmp_path / (NAME + '.rvb')
    rows = [{'t': 1, 'data': 'a'*1000}]
    B.append(path, rows); B.append(path, rows)
    monkeypatch.setattr(B, 'MAX_FILE', 1500)
    restored, status = B.read(path)
    assert restored == rows and status['status'] == 'limited' and status['decoded_bytes'] < 1500


def test_format_preference_keeps_existing_jsonl_until_explicit_conversion(tmp_path, monkeypatch):
    store = Store(str(tmp_path)); job(store); store.record('7', {'t': 1.}); original = Path(store._series_path('7'))
    store.series_storage = 'blocks-v1'
    def forbidden(*args, **kw): pytest.fail('ordinary publication must not run bulk conversion')
    monkeypatch.setattr(S, 'migrate', forbidden)
    store.record('7', {'t': 2.})
    assert list(S.json_rows(original)) == [{'t': 1.}, {'t': 2.}]
    assert not original.with_suffix('.rvb').exists()
    store.close()


def test_explicit_prefix_recovery_preserves_corrupt_source_and_labels_coverage(tmp_path, capsys):
    from raven.series_cli import main
    source = tmp_path / (NAME + '.rvb'); B.append(source, [{'t': 1.}]); original = source.read_bytes()+b'partial'
    source.write_bytes(original); output = tmp_path / 'recovered.jsonl'
    assert main(['recover', str(source), '--output', str(output)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['status'] == 'recovered-prefix' and not result['complete'] and 'Truncated' in result['read_error']
    assert source.read_bytes() == original and list(S.json_rows(output)) == [{'t': 1.}]
