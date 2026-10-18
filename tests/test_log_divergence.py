from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from raven import layout as L, log_divergence as divergence, log_workbench as workbench
from raven.log_match_index import template_key
from test_log_workbench import app_for, complete, rows_text


def source(rank, lines, cohort='workers', **kw):
    return dict(rank_identity=dict(rank=rank, cohort=cohort), path=f'{rank}.log', label=f'rank {rank}', lines=lines, **kw)


def header(rank, cohort='workers', attempt='run1', job_id='77'):
    return json.dumps(dict(raven_rank=dict(rank=rank, cohort=cohort, attempt=attempt, job_id=job_id))).encode() + b'\n'


def test_conservative_templates_preserve_semantic_values_and_order():
    assert template_key('2026-10-01T01:00:00Z [rank=1] exit 137', 1) == '<time> <rank> exit 137'
    assert template_key('rank=1 exit 137', 1) != template_key('rank=0 exit 0', 0)
    result = divergence.compare([source(0, ['begin', 'step 1', 'done']), source(1, ['begin', 'step 2', 'done'])])
    cohort = result['cohorts'][0]
    assert cohort['first_divergence'] == [1, 1] and cohort['last_common'] == [0, 0]
    assert cohort['agreement'] == 'tie / no majority'
    assert [ref['original'] for ref in cohort['references']] == [1, 1]


def test_roles_do_not_require_global_majority_and_clock_reordering_does_not_reorder_events():
    result = divergence.compare([source(0, ['2026-10-01T00:00:02Z A', '2026-10-01T00:00:01Z B']),
                                 source(1, ['2026-10-01T00:00:00Z A', '2026-10-01T00:00:03Z B']),
                                 source(2, ['coordinator ready'], 'coordinator')])
    assert {c['cohort']: c['status'] for c in result['cohorts']} == {'coordinator': 'insufficient', 'workers': 'matched_retained_window'}


def test_majority_describes_observations_not_correctness():
    result = divergence.compare([source(0, ['x']), source(1, ['x']), source(2, ['y'])])
    assert result['cohorts'][0]['agreement'] == 'strict majority'
    assert 'faulty rank' in result['limitations'][0]


def test_truncation_requires_unique_anchor_and_repeated_templates_remain_ambiguous():
    result = divergence.compare([source(0, ['a', 'a'], truncated=True), source(1, ['a', 'a'])])
    assert result['cohorts'][0]['status'] == 'unanswerable'
    result = divergence.compare([source(0, ['old', 'anchor', 'a', 'a'], truncated=True), source(1, ['anchor', 'b'])])
    cohort = result['cohorts'][0]
    assert cohort['last_common'] == [1, 0] and cohort['first_divergence'] == [2, 1]
    assert any('Repeated' in n for n in cohort['notices'])


def test_missing_and_ended_logs_never_claim_whole_run_agreement():
    result = divergence.compare([source(0, ['a']), source(1, ['a', 'b']), dict(label='lost', error='rotated')])
    assert result['notices'] == ['lost: rotated']
    cohort = result['cohorts'][0]
    assert cohort['references'][0]['original'] is None
    assert any('stream ends' in n for n in cohort['notices'])


@pytest.mark.parametrize('raw', [b'not json\n', b'{}\n', b'{"raven_rank":[]}\n', header(True),
                                header(0, attempt='old'), header(0, job_id='other'), b'x' * 4096])
def test_identity_requires_complete_exact_attempt_header(raw):
    with pytest.raises(ValueError):
        divergence.identity(raw, attempt='run1', job_id='77')


def test_identity_header_and_resource_limits():
    assert divergence.identity(header(0), attempt='run1', job_id='77')['rank'] == 0
    with pytest.raises(ValueError, match='Duplicate rank'):
        divergence.compare([source(0, []), source(0, [])])
    with pytest.raises(ValueError, match='at most'):
        divergence.compare([source(i, []) for i in range(17)])
    with pytest.raises(ValueError, match='limit'):
        divergence.compare([source(0, ['a'] * 241)])


def test_background_complete_ui_and_raw_evidence(tmp_path):
    app = app_for()
    app.project_state = dict(binding=dict(job_id='77', run_id='run1'))
    app.log_job = '77'
    paths = [tmp_path / f'{rank}.log' for rank in (0, 1)]
    for rank, path in enumerate(paths):
        path.write_bytes(header(rank) + b'begin\n' + (b'failed 137\n' if rank else b'completed 0\n'))
    app.logs.entries = [dict(id=str(i), path=str(p), label=f'rank {i}') for i, p in enumerate(paths)]
    app.logs.path = str(paths[0])
    views = SimpleNamespace(g=L.Glyphs(False))
    try:
        assert workbench.run_command(app, ['logdivergence', '0', '1'])
        workbench.overlay(views, {}, app, 80, 24)
        complete(app)
        rendered = workbench.overlay(views, {}, app, 80, 24)
        text = rows_text(rendered)
        assert 'divergence' in text and 'failed 137' in text and 'Last common' in text
        assert all(L.vlen(L.row_text(row)) <= 80 for _, _, row in rendered)
        app.log_workbench_state['cursor'] = next(i for i, row in enumerate(app.log_workbench_state['rows']) if 'failed 137' in row['text'])
        assert workbench.handle_key(app, 'o')
        citation = app.log_workbench_state['citation']
        assert citation['path'] == str(paths[1]) and citation['excerpt_line'] == 'failed 137'
        assert paths[1].read_bytes() == header(1) + b'begin\nfailed 137\n'
        workbench.run_command(app, ['logdivergence', '0', '1'])
        app.project_state['binding']['run_id'] = 'new-attempt'
        assert 'changed' in workbench._alternate(app)['error']
    finally:
        app.research.close()


def test_unbound_selection_and_unknown_catalog_source_refused():
    app = app_for()
    try:
        workbench.run_command(app, ['logdivergence', '0', '1'])
        assert not app.command_ok and 'exact run' in app.message
        app.project_state = dict(binding=dict(job_id='77', run_id='run1'))
        workbench.run_command(app, ['logdivergence', 'unknown', 'missing'])
        assert 'registered' in app.message
    finally:
        app.research.close()


def test_empty_windows_are_unknown_not_whole_run_agreement():
    result = divergence.compare([source(0, []), source(1, [])])
    assert result['cohorts'][0]['status'] == 'insufficient'


def test_rotated_header_cannot_bind_new_tail_to_old_rank(tmp_path):
    from raven.remote import LocalFiles
    paths = [tmp_path / '0.log', tmp_path / '1.log']
    for rank, path in enumerate(paths): path.write_bytes(header(rank) + b'start\n')
    class Rotating(LocalFiles):
        changed = False
        def read(self, path, offset, length):
            raw = super().read(path, offset, length)
            if not self.changed:
                self.changed = True
                old = tmp_path / 'old.log'
                paths[0].rename(old)
                paths[0].write_bytes(header(9) + b'replacement\n')
            return raw
    app = app_for(Rotating())
    app.project_state = dict(binding=dict(job_id='77', run_id='run1'))
    app.logs.entries = [dict(id=str(i), path=str(path), label=str(i)) for i, path in enumerate(paths)]
    try:
        workbench.run_command(app, ['logdivergence', '0', '1'])
        assert workbench._alternate(app) is None
        complete(app)
        report = workbench._alternate(app)['sources'][0]['divergence_report']
        assert any('rotated' in notice for notice in report['notices'])
        assert all(c['status'] == 'insufficient' for c in report['cohorts'])
    finally: app.research.close()
