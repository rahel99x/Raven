"""Complete ASCII report coverage and isolation from the interactive dashboard."""
import copy

import pytest

from raven import layout, report
from raven.config import Config
from raven.controller import App
from raven.model import Health, Job, Store
from raven.views import Views


def make_report_app():
    store = Store(persist=False)
    config = Config()
    app = App(store, None, None, config, "scientist", ascii_=False)
    views = Views(layout.Glyphs(False), config)
    return store, app, views


def test_complete_report_has_all_pages_subviews_and_truthful_empty_states():
    store, app, views = make_report_app()
    page = report.build(store.snapshot(), app, views)
    assert page.isascii() and "\x1b" not in page
    for i, name in enumerate(['JOBS', 'CLUSTER', 'HISTORY', 'ANALYTICS', 'NODES', 'GROUP', 'DEPENDENCIES', 'LOG', 'SOURCES', 'RESEARCH', 'EVENT JOURNAL'], 1):
        assert f'{i:02d} / {name}' in page
    for subview in ['history', 'timeline', 'advisor', 'compare']:
        assert f'analytics / {subview}' in page
    assert 'cluster map' in page
    assert 'no running job and no recorded series yet' in page
    assert 'No completed runs in this snapshot.' in page
    assert 'No activity recorded yet.' in page
    assert 'unknown' in page
    assert '<html' not in page and '<svg' not in page and '<script' not in page
    assert max(map(len, page.splitlines())) <= 132


def test_report_clears_only_private_filter_and_renders_every_job_series():
    store, app, views = make_report_app()
    store.jobs = [Job(str(i), f'simulation-{i}', 'cpu', 'RUNNING', cpus=1) for i in range(15)]
    for job in store.jobs:
        store.record(job.id, dict(t=1000, k='live', cpu=0.5, eff=0.5, rss=None))
    app.filter = 'simulation-0'
    app.selected_id = '7'
    app.log_job = '7'
    app.tab = 'log'
    app.analytics_view = 'advisor'
    app.analytics_job = '5'
    app.nodes_view = 'map'
    app.cursor['jobs'] = 7
    app.top['jobs'] = 4
    app.marks = {'3', '7'}
    app.compare_ids = ['3', '7']
    app.logs.path = '/nonexistent/log.out'
    app.logs.top = 16
    app.logs.page = 23
    app.logs.bookmarks = {'/nonexistent/log.out': [2, 9]}
    app.logs.candidates = {'7': (1000, [])}
    app_before = vars(app).copy()
    # Worker primitives and service bindings retain identity; presentation data
    # and the read-only inspector's cache must remain unchanged by rendering.
    primitives = (app.execution_state["cancel"], app.execution_state["progress"],
                  app.history_log_export_state["progress_lock"], app.shell_checks_state["cancel"],
                  app.telemetry_state["inspector"])
    telemetry_cache_before = copy.deepcopy(app.telemetry_state["inspector"].cache)
    memo = {id(value): value for value in primitives}
    mutable_before = {key: copy.deepcopy(value, memo.copy()) for key, value in vars(app).items()
                      if isinstance(value, (dict, list, set))}
    log_before = copy.deepcopy(vars(app.logs), memo={id(app.logs.files): app.logs.files})
    views_before = vars(views).copy()
    page = report.build(store.snapshot(), app, views)
    assert 'job series 15/15' in page  # No silent 12-job cap or current-filter truncation.
    assert 'simulation-14' in page and '1 cpu samples' in page
    assert vars(app) == app_before
    for key, before in mutable_before.items():
        assert getattr(app, key) == before
    assert app.telemetry_state["inspector"].cache == telemetry_cache_before
    assert vars(app.logs) == log_before
    assert vars(views) == views_before
    assert not views.g.ascii


def test_report_cannot_emit_terminal_control_sequences_or_non_ascii_names():
    store, app, views = make_report_app()
    store.jobs = [Job('17', 'caf\u00e9\x1b[31m', 'cpu', 'RUNNING', cpus=1)]
    store.events.append(dict(t=1000, kind='alert', text='unsafe\x1b]52;c;payload\x07\nspoofed', job='17'))
    store.health['source'] = Health(name='source', error='bad\x1b[2J\x00\u2603')
    page = report.build(store.snapshot(), app, views, title='Raven\r\x1b[2J')
    assert page.isascii()
    assert all(ch == '\n' or ' ' <= ch <= '~' for ch in page)
    assert '\\x1b' in page and '\\x07' in page and '\\nspoofed' in page
    assert '\\xe9' in page and '\\u2603' in page
    assert not any(line.startswith('spoofed') for line in page.splitlines())


def test_demo_banner_and_bounded_report_width():
    store, app, views = make_report_app()
    assert '[DEMO]' not in report.build(store.snapshot(), app, views)
    app.demo = True
    for width in (60, 80, 160):
        page = report.build(store.snapshot(), app, views, width=width)
        assert '[DEMO] Simulated cluster data.' in page
        assert max(map(len, page.splitlines())) <= width
        assert '09 / SOURCES' in page


def test_export_failure_does_not_change_interactive_glyphs_or_state(monkeypatch):
    store, app, views = make_report_app()
    app.tab = 'nodes'
    app.filter = 'important'
    state = vars(app).copy()
    glyphs = views.g

    def broken(*args, **kwargs):
        raise RuntimeError('render failed')

    monkeypatch.setattr(views, 'compose', broken)
    with pytest.raises(RuntimeError, match='render failed'):
        report.build(store.snapshot(), app, views)
    assert vars(app) == state and views.g is glyphs


def test_private_report_copies_shell_evidence_without_copying_or_driving_worker_primitives(monkeypatch):
    store, app, views = make_report_app()
    state = app.shell_checks_state
    event = state["cancel"]

    def pending_callback(value):
        pytest.fail("report invoked the live shell-check completion")

    state.update(running=True, callback=pending_callback,
                 result={"status": "findings", "sha256": "a" * 64,
                         "findings": [{"message": "keep original evidence"}]},
                 control_hits=[(5, "control", {"id": "shell-checks:back", "left": 3, "right": 9})])
    local = report._ui_copy(app)
    private = local.shell_checks_state
    assert private is not state
    assert private["cancel"] is event and private["callback"] is pending_callback
    private["result"]["findings"][0]["message"] = "private edit"
    private["control_hits"][0][2]["left"] = 99
    assert state["result"]["findings"][0]["message"] == "keep original evidence"
    assert state["control_hits"][0][2]["left"] == 3
    inspector = app.telemetry_state["inspector"]
    assert local.telemetry_state["inspector"] is inspector
    monkeypatch.setattr(inspector, "inspect", lambda *a, **k: pytest.fail("report started telemetry inspection"))
    page = report.build(store.snapshot(), app, views)
    assert "End of report" in page
    assert not event.is_set() and state["running"]
    assert state["callback"] is pending_callback
    assert state["result"]["findings"][0]["message"] == "keep original evidence"


def test_event_journal_wraps_long_diagnostics_without_losing_content():
    store, app, views = make_report_app()
    message = ' '.join(f'event-word-{i}' for i in range(80))
    store.events.append(dict(t=1000, kind='alert', text=message))
    store.health['source'] = Health(name='source', error=' '.join(f'error-word-{i}' for i in range(80)))
    page = report.build(store.snapshot(), app, views, width=80)
    assert all(f'event-word-{i}' in page for i in range(80))
    assert all(f'error-word-{i}' in page for i in range(80))
    assert max(map(len, page.splitlines())) <= 80
