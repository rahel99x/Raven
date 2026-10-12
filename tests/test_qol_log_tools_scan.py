from __future__ import annotations

import os
import re

import pytest

from raven import log_scan as scan
from raven.remote import LocalFiles


def test_pages_keep_exact_bytes_absolute_lines_crlf_and_invalid_utf8(tmp_path):
    p = tmp_path / 'job.out'
    raw = b'one\r\n\xe7\x95\x8c two\n\xfftail'
    p.write_bytes(raw)
    page = scan.read_page(LocalFiles(), str(p))
    assert [r['line'] for r in page['rows']] == [1, 2, 3]
    assert [r['text'] for r in page['rows']] == ['one', '界 two', '\ufffdtail']
    assert b''.join(r['raw'] for r in page['rows']) == raw
    assert page['complete'] and page['next'] == len(raw)


def test_page_continuity_and_exact_byte_alignment(tmp_path):
    p = tmp_path / 'job.out'
    raw = b''.join(f'line-{n}\n'.encode() for n in range(1000))
    p.write_bytes(raw)
    offset, collected = 0, []
    while offset < len(raw):
        page = scan.read_page(LocalFiles(), str(p), offset, max_bytes=97, max_rows=7)
        assert page['end'] > offset
        collected.extend(r['raw'] for r in page['rows'])
        offset = page['next']
    assert b''.join(collected) == raw
    page = scan.read_page(LocalFiles(), str(p), 2)
    assert page['rows'][0]['text'] == 'line-1'
    assert page['rows'][0]['line'] is None


def test_giant_line_pages_report_fragments_and_do_not_exceed_budget(tmp_path):
    p = tmp_path / 'job.out'
    p.write_bytes(b'x' * (scan.PAGE_BYTES * 2) + b'\nnext\n')
    page = scan.read_page(LocalFiles(), str(p), max_bytes=100)
    assert page['partial']
    assert len(page['rows'][0]['raw']) == 100
    assert page['rows'][0]['partial_end']
    assert page['next'] == 100


@pytest.mark.parametrize('options, expected', [({}, [1, 2, 3, 4]), ({'case': True}, [2, 3, 4]), ({'word': True}, [1, 2]), ({'case': True, 'word': True}, [2])])
def test_search_modes_are_explicit(tmp_path, options, expected):
    p = tmp_path / 'job.out'
    p.write_text('ERROR\nerror\nerrors\npreerrorpost\n')
    result = scan.search_source(LocalFiles(), {'id': 'stdout', 'path': str(p)}, 'error', **options)
    assert [m['line'] for m in result['matches']] == expected
    assert result['complete']


def test_literal_regex_and_context_preserve_exact_sources(tmp_path):
    p = tmp_path / 'job.out'
    p.write_text('before\nerror.*timeout\nafter\nERROR long timeout\nend\n')
    source = {'id': 'scheduler.err', 'job_id': '123_10', 'path': str(p), 'label': 'stderr'}
    literal = scan.search_source(LocalFiles(), source, 'error.*timeout')
    assert [m['line'] for m in literal['matches']] == [2]
    regex = scan.search_source(LocalFiles(), source, 'ERROR.*timeout', regex=True)
    assert [m['line'] for m in regex['matches']] == [2, 4]
    assert regex['matches'][0]['before'][0]['text'] == 'before'
    assert regex['matches'][0]['after'][0]['text'] == 'after'
    assert regex['matches'][0]['source'] == source
    assert regex['matches'][1]['offset'] == len('before\nerror.*timeout\nafter\n')


def test_full_source_search_finds_prefix_outside_small_tail(tmp_path):
    p = tmp_path / 'job.out'
    p.write_bytes(b'FAILED at first line\n' + b'.' * 200000 + b'\nlast line\n')
    from raven.logs import LogBuffer
    buf = LogBuffer(str(p), max_bytes=1024)
    buf.refresh()
    assert not any('FAILED' in s for s in buf.all_lines())
    report = scan.search_source(LocalFiles(), {'path': str(p)}, 'FAILED')
    assert report['matches'][0]['line'] == 1
    assert report['complete']


def test_literal_matches_long_line_boundary_and_regex_reports_omission(tmp_path):
    p = tmp_path / 'job.out'
    p.write_bytes(b'x' * (scan.MAX_LINE_BYTES - 2) + b'failure' + b'x' * 20 + b'\nshort failure\n')
    result = scan.search_source(LocalFiles(), {'path': str(p)}, 'failure')
    assert [m['line'] for m in result['matches']] == [1, 2]
    assert result['complete']
    result = scan.search_source(LocalFiles(), {'path': str(p)}, 'failure', regex=True)
    assert [m['line'] for m in result['matches']] == [2]
    assert result['long_regex_lines'] == 1 and not result['complete']


def test_limits_are_honest_and_progress_measures_actual_bytes(tmp_path):
    p = tmp_path / 'job.out'
    p.write_bytes(b'error\n' * 20000)
    progress = []
    report = scan.search_source(LocalFiles(), {'path': str(p)}, 'error', max_results=3, progress=lambda *v: progress.append(v))
    assert len(report['matches']) == 3 and report['limited'] and not report['complete']
    assert progress and progress[0][0] <= p.stat().st_size
    report = scan.search_source(LocalFiles(), {'path': str(p)}, 'absent', max_bytes=70)
    assert report['scanned'] == 70 and not report['complete']


def test_cancel_before_io_and_between_chunks(tmp_path):
    p = tmp_path / 'job.out'
    p.write_bytes(b'a\n' * 100000)
    with pytest.raises(scan.ScanCancelled): scan.read_page(LocalFiles(), str(p), cancel=lambda: True)
    checks = []
    def cancel():
        checks.append(True)
        return len(checks) > 5
    with pytest.raises(scan.ScanCancelled): scan.search_source(LocalFiles(), {'path': str(p)}, 'z', cancel=cancel)


@pytest.mark.parametrize('method', ['page', 'search'])
def test_rotation_and_same_size_mutation_reject_publication(tmp_path, method):
    p = tmp_path / 'job.out'
    p.write_bytes(b'error\nother\n')
    class Rotating(LocalFiles):
        def read(self, path, offset, length):
            raw = super().read(path, offset, length)
            replacement = p.with_suffix('.new')
            replacement.write_bytes(b'ERROR\nOTHER\n')
            replacement.replace(p)
            return raw
    files = Rotating()
    with pytest.raises(scan.SourceChanged):
        if method == 'page': scan.read_page(files, str(p))
        else: scan.search_source(files, {'path': str(p)}, 'error')


def test_append_reports_initial_snapshot_and_does_not_include_later_bytes(tmp_path):
    p = tmp_path / 'job.out'
    p.write_bytes(b'error\n')
    class Appending(LocalFiles):
        def read(self, path, offset, length):
            raw = super().read(path, offset, length)
            with p.open('ab') as f: f.write(b'later error\n')
            return raw
    result = scan.search_source(Appending(), {'path': str(p)}, 'error')
    assert result['appended'] and result['size'] == 6
    assert [m['line'] for m in result['matches']] == [1]


def test_multi_source_errors_keep_exact_catalog_identity(tmp_path):
    first, second = tmp_path / 'a', tmp_path / 'b'
    first.write_text('first error\n')
    second.write_text('second error\n')
    sources = [{'id': 'out', 'path': str(first)}, {'id': 'err', 'path': str(second)}, {'id': 'missing', 'path': str(tmp_path / 'none')}]
    result = scan.search_sources(LocalFiles(), sources, 'error')
    assert [m['source']['id'] for m in result['matches']] == ['out', 'err']
    assert result['reports'][2]['source']['id'] == 'missing'
    assert 'error' in result['reports'][2] and not result['complete']


def test_locate_absolute_line_byte_percent_and_iso_time(tmp_path):
    p = tmp_path / 'job.out'
    p.write_text('2026-10-05T10:00:00Z first\n2026-10-05T10:00:01Z second\n2026-10-05T10:00:02Z third\n')
    source = {'path': str(p)}
    assert 'second' in scan.locate(LocalFiles(), source, 'line', '2')['rows'][0]['text']
    assert 'second' in scan.locate(LocalFiles(), source, 'time', '2026-10-05T10:00:01Z')['rows'][0]['text']
    assert scan.locate(LocalFiles(), source, 'byte', '0')['start'] == 0
    assert scan.locate(LocalFiles(), source, 'percent', '100')['rows'] == []
    with pytest.raises(ValueError): scan.locate(LocalFiles(), source, 'line', '100')


@pytest.mark.parametrize('expression', ['(a+)+$', '(a|aa)+$', '(a)\\1', '(?=a)a', 'a.*b.*c', '['])
def test_regex_budget_rejects_unbounded_backtracking(expression):
    with pytest.raises(ValueError): scan.compile_search(expression, regex=True)


def test_regular_source_requirement_rejects_directory_and_fifo(tmp_path):
    with pytest.raises(OSError): scan.read_page(LocalFiles(), str(tmp_path))
    fifo = tmp_path / 'fifo'
    os.mkfifo(fifo)
    with pytest.raises(OSError): scan.read_page(LocalFiles(), str(fifo))


def test_remote_legacy_adapter_never_uses_local_paths():
    class RemoteAdapter(LocalFiles):
        remote = True
        def stat(self, path):
            assert path == '/remote/job.out'
            return 12, (9, 99)
        def read(self, path, offset, length):
            return b'error\nother\n'[offset:offset + length]
    result = scan.search_source(RemoteAdapter(), {'path': '/remote/job.out'}, 'error')
    assert result['matches'][0]['line'] == 1 and result['complete']
    assert scan.read_page(RemoteAdapter(), '/remote/job.out')['complete']


def test_regex_oversized_lines_are_explicit_coverage_gaps(tmp_path):
    p = tmp_path / 'job.out'
    p.write_bytes(b'error ' + b'x' * scan.REGEX_LINE_BYTES + b'\nerror\n')
    result = scan.search_source(LocalFiles(), {'path': str(p)}, 'error', regex=True)
    assert [row['line'] for row in result['matches']] == [2]
    assert result['long_regex_lines'] == 1 and not result['complete']


def test_same_size_edit_and_short_reads_are_rejected(tmp_path):
    p = tmp_path / 'job.out'
    p.write_bytes(b'error\n')
    class Mutating(LocalFiles):
        def read(self, path, offset, count):
            data = super().read(path, offset, count)
            p.write_bytes(b'other\n')
            return data
    with pytest.raises(scan.SourceChanged): scan.read_page(Mutating(), str(p))
    class Short(LocalFiles):
        def read(self, path, offset, count): return super().read(path, offset, max(0, count - 1))
    with pytest.raises(scan.SourceChanged): scan.search_source(Short(), {'path': str(p)}, 'other')


def test_unicode_whole_word_and_query_dashes(tmp_path):
    p = tmp_path / 'job.out'
    p.write_bytes('界 error\nerrors\nERROR!\n--failed\n'.encode() + b'\xff error\r\n')
    report = scan.search_source(LocalFiles(), {'path': str(p)}, 'error', word=True)
    assert [m['line'] for m in report['matches']] == [1, 3, 5]
    assert scan.search_source(LocalFiles(), {'path': str(p)}, '--failed')['matches'][0]['line'] == 4


def test_unicode_literal_match_survives_large_line_fragment_utf8_boundary(tmp_path):
    p = tmp_path / 'job.out'
    p.write_bytes(b'x' * (scan.MAX_LINE_BYTES - 1) + '界failed'.encode() + b'\n')
    result = scan.search_source(LocalFiles(), {'path': str(p)}, '界failed')
    assert len(result['matches']) == 1 and result['matches'][0]['line'] == 1
    assert result['complete']


def test_newline_burst_scans_with_bounded_fragments_and_complete_progress(tmp_path):
    p = tmp_path / 'job.out'
    p.write_bytes(b'\n' * 200000 + b'failed\n')
    result = scan.search_source(LocalFiles(), {'path': str(p)}, 'failed')
    assert result['matches'][0]['line'] == 200001
    assert result['scanned'] == p.stat().st_size and result['complete']


def test_word_ending_at_fragment_boundary_waits_for_next_character(tmp_path):
    p = tmp_path / 'job.out'
    p.write_bytes(b'x' * (scan.MAX_LINE_BYTES - 6) + b' error' + b's\nerror\n')
    result = scan.search_source(LocalFiles(), {'path': str(p)}, 'error', word=True)
    assert [m['line'] for m in result['matches']] == [2]


def test_source_generator_has_correct_complete_coverage(tmp_path):
    p = tmp_path / 'job.out'
    p.write_text('error\n')
    result = scan.search_sources(LocalFiles(), ({'path': str(p)} for _ in range(1)), 'error')
    assert result['complete']


def test_regex_parser_supports_python310_without_private_re_or_possessive_constant(monkeypatch):
    import builtins
    import sys
    from types import SimpleNamespace
    from re import _parser, _constants
    original = builtins.__import__
    constants = SimpleNamespace(**{key: value for key, value in vars(_constants).items() if key != 'POSSESSIVE_REPEAT'})
    monkeypatch.setitem(sys.modules, 'sre_parse', SimpleNamespace(parse=_parser.parse))
    monkeypatch.setitem(sys.modules, 'sre_constants', constants)
    def old_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == 're' and ('_parser' in fromlist or '_constants' in fromlist):
            raise ImportError('Python 3.10 private parser is unavailable')
        return original(name, globals, locals, fromlist, level)
    monkeypatch.setattr(builtins, '__import__', old_import)
    assert scan.compile_search('ERROR.*timeout', regex=True).search('ERROR long timeout')
    with pytest.raises(ValueError): scan.compile_search('(a+)+', regex=True)
