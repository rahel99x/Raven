"""Resource harness bounds and fixture integrity; sustained runs stay explicit."""
from __future__ import annotations

import importlib.util
import math
from pathlib import Path

import pytest


SPEC = importlib.util.spec_from_file_location('benchmark_raven_resources',
    Path(__file__).resolve().parents[1] / 'scripts/benchmark_raven_resources.py')
B = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(B)


def test_standard_fixture_has_exactly_one_hundred_stable_jobs():
    first, second = B.fixture_specs(100), B.fixture_specs(100)
    assert first == second
    assert len({job['id'] for job in first}) == 100
    assert sum(job['start'] is not None for job in first) == 80
    assert sum(job['start'] is None for job in first) == 20
    assert all(job['gn'] == 0 and job['dur'] > 3600 for job in first)


def test_disk_sampler_never_follows_symlink_outside_fixture(tmp_path):
    external = tmp_path / 'outside'
    external.mkdir()
    (external / 'large').write_bytes(b'x' * 10000)
    scratch = tmp_path / 'fixture'
    scratch.mkdir()
    (scratch / 'small').write_bytes(b'abc')
    (scratch / 'link').symlink_to(external, target_is_directory=True)
    sample = B.bounded_disk(scratch)
    assert sample['complete']
    assert sample['bytes'] == 3
    assert sample['files_and_directories'] == 2


def test_source_digest_tracks_content_and_filename(tmp_path):
    package = tmp_path / 'raven'
    package.mkdir()
    source = package / 'one.py'
    source.write_text('value = 1\n')
    before = B.source_digest(tmp_path)
    source.write_text('value = 2\n')
    changed = B.source_digest(tmp_path)
    assert changed != before
    source.rename(package / 'two.py')
    assert B.source_digest(tmp_path) != changed


def test_source_digest_uses_selected_checkout_package_metadata(tmp_path):
    (tmp_path / 'pyproject.toml').write_text('[tool.setuptools]\npackages = ["retained_fixture"]\n')
    package = tmp_path / 'retained_fixture'
    package.mkdir()
    source = package / 'one.py'
    source.write_text('value = 1\n')
    before = B.source_digest(tmp_path)
    unrelated = tmp_path / 'raven'
    unrelated.mkdir()
    (unrelated / 'one.py').write_text('wrong = True\n')
    assert B.source_digest(tmp_path) == before
    source.write_text('value = 2\n')
    assert B.source_digest(tmp_path) != before


def test_quantiles_preserve_units_and_report_empty_series():
    assert B.summary([]) == {'count': 0}
    result = B.summary([10, 20, 30])
    assert result == {'count': 3, 'mean': 20, 'min': 10, 'p50': 20, 'p95': 30, 'p99': 30, 'max': 30}


@pytest.mark.parametrize('args', [
    ['--duration', 'nan'], ['--duration', 'inf'], ['--duration', '0'],
    ['--jobs', '10001'], ['--sample-seconds', '61'], ['--width', '401'],
    ['--height', '14'], ['--warmup', '-1'], ['--duration', '1', '--sample-seconds', '2'],
])
def test_invalid_measurements_refuse_before_process_launch(monkeypatch, args):
    monkeypatch.setattr(B, 'run', lambda _: pytest.fail('Invalid benchmark launched an application'))
    with pytest.raises(SystemExit) as exc:
        B.main(args)
    assert exc.value.code == 2
