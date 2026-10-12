"""Dock geometry, bounded rendering, exact sources and pointer capture."""
from types import SimpleNamespace

import pytest

from raven import history_browser as H, job_groups, layout as L, pane_drag
from raven.config import Config
from raven.controller import App
from raven.model import Finished, Job, Store


@pytest.fixture
def browser():
    store = Store(persist=False)
    store.jobs = [Job("900", "active", "cpu", "RUNNING", submit="2026-01-01T12:00:00")]
    store.finished = [Finished(str(i), "past-" + str(i), "FAILED" if i % 2 else "COMPLETED",
                               end="2025-12-01T12:00:00") for i in range(300, 350)]
    store.departed_jobs["800"] = Job("800", "departed", "cpu", "COMPLETING")
    app = App(store, None, None, Config(), "test", interactive=False)
    app.tab, app.body_origin, app.width, app.height = "analytics", 6, 160, 50
    app.selected_id, app.analytics_job = "900", "900"
    views = SimpleNamespace(g=L.Glyphs(False))
    yield SimpleNamespace(app=app, store=store, views=views)
    if app.research:
        app.research.close()


def render(browser, width=160, height=40, renderer=None):
    app = browser.app
    app.width, app.height = width, height + app.body_origin + 1
    pane_drag.begin_frame(app, width, app.height)
    return H.wrap_render(browser.views, browser.store.snapshot(), app, width, height,
                        renderer or (lambda w, h: ([[('NATIVE', 'cyan')]], [])))


def drag_point(app):
    rect = app.history_browser_rect
    y, _, control = next(hit for hit in H.initialize(app)['frame']['hits']
                         if hit[1] == 'control' and hit[2]['id'].endswith(':drag'))
    return rect.y + y, rect.x + control['left']


@pytest.mark.parametrize("dock", H.DOCKS)
@pytest.mark.parametrize("width,height", [(160, 40), (80, 18), (40, 14), (10, 8), (2, 2), (1, 1)])
def test_every_layout_fits_and_builds_one_source_at_actual_dimensions(browser, dock, width, height):
    H._view(browser.app)["dock"] = dock
    calls = []
    def source(w, h):
        calls.append((w, h))
        return ([[('原文 content', 'cyan')]] * h,
                [(0, 'control', {'id': 'native', 'label': 'Native', 'left': 0, 'right': min(5, w),
                                 'action': ('command', 'refresh'), 'group': 'native'})] if h and w else [])
    rows, hits = render(browser, width, height, source)
    rect = browser.app.history_browser_content_rect
    assert calls == [(rect.width, rect.height)]
    assert len(rows) == height
    assert all(L.vlen(L.row_text(row)) <= width for row in rows)
    for y, kind, value in hits:
        assert 0 <= y < height
        if kind == 'control':
            assert 0 <= value['left'] < value['right'] <= width
    native = next((hit for hit in hits if hit[1] == 'control' and hit[2]['id'] == 'native'), None)
    if rect.width and rect.height:
        assert native and native[0] == rect.y - browser.app.body_origin
        assert native[2]['left'] == rect.x


@pytest.mark.parametrize("tab", H.TABS)
def test_completed_failed_departed_and_running_ids_remain_exact(browser, tab, monkeypatch):
    app = browser.app
    app.tab = tab
    opened = []
    monkeypatch.setattr(app, 'open_log', lambda jid: opened.append(jid))
    render(browser)
    monkeypatch.setattr(browser.store, 'snapshot', lambda: pytest.fail('Job activation copied a full store snapshot'))
    for jid in ('900', '800', '301', '302'):
        assert H.activate(app, jid)
        assert H._view(app)['selected'] == jid
        expected = {'analytics': 'analytics_job', 'deps': 'selected_id', 'research': 'research_job_id'}.get(tab)
        if expected:
            assert getattr(app, expected) == jid
        else:
            assert opened[-1] == jid


def test_unavailable_job_does_not_fall_back_to_running_id(browser):
    render(browser)
    before = browser.app.analytics_job
    assert not H.activate(browser.app, 'not-in-snapshot')
    assert browser.app.analytics_job == before
    assert 'no longer available' in browser.app.message


def test_in_place_state_and_newly_completed_records_update_without_restarting(browser):
    render(browser)
    state = H.initialize(browser.app)
    browser.store.finished[0].state = 'OUT_OF_MEMORY'
    browser.store.jobs = []
    browser.store.finished.insert(0, Finished('900', 'active', 'COMPLETED'))
    rows, _ = render(browser)
    by_id = {record.id: record for record in state['records']}
    assert by_id['300'].state == 'OUT_OF_MEMORY'
    assert by_id['900'].state == 'COMPLETED'
    assert len(by_id) == len(state['records'])
    assert 'COMPLETED' in L.to_text(rows, 160)


def test_source_priority_is_active_then_accounting_then_departed(browser):
    browser.store.finished += [Finished('900', 'stale-accounting', 'COMPLETED'), Finished('800', 'terminal', 'FAILED')]
    render(browser)
    by_id = {record.id: record for record in H.initialize(browser.app)['records']}
    assert by_id['900'].name == 'active'
    assert by_id['800'].name == 'terminal'


def test_wheel_scrolls_history_without_loading_content_sources(browser, monkeypatch):
    render(browser, 160, 18)
    rect = browser.app.history_browser_rect
    monkeypatch.setattr(browser.app, 'open_log', lambda *_: pytest.fail('Wheel opened a log'))
    monkeypatch.setattr(browser.store, 'snapshot', lambda: pytest.fail('Wheel requested a snapshot'))
    selected = browser.app.analytics_job
    assert H.handle_mouse(browser.app, rect.y + 2, rect.x + 4, 'wheeldown')
    assert H._view(browser.app)['top'] == 3
    assert browser.app.analytics_job == selected


def test_keyboard_selection_remains_visible_and_escape_returns_to_content(browser):
    render(browser, 160, 18)
    assert H.run_command(browser.app, ['history-focus'])
    assert H.handle_key(browser.app, 'end')
    assert browser.app.analytics_job == H.initialize(browser.app)['items'][-1].record.id
    rows, _ = render(browser, 160, 18)
    assert browser.app.analytics_job in L.to_text(rows, 160)
    assert H.handle_key(browser.app, 'esc')
    assert not H.handle_key(browser.app, 'up')


def test_selection_reanchors_when_new_records_prepend(browser):
    render(browser, 160, 18)
    H.activate(browser.app, '320')
    render(browser, 160, 18)
    browser.store.jobs.insert(0, Job('999', 'new-launch', 'cpu', 'RUNNING', submit='2026-02-01T00:00:00'))
    render(browser, 160, 18)
    state = H.initialize(browser.app)
    assert state['items'][state['index']].record.id == '320'
    H.handle_key(browser.app, 'down')
    assert browser.app.analytics_job == state['items'][state['index']].record.id


@pytest.mark.parametrize("dock,target", [('left', 'right'), ('right', 'top'), ('top', 'bottom'), ('bottom', 'left')])
def test_handle_drag_previews_and_commits_only_valid_edge(browser, dock, target, monkeypatch):
    H._view(browser.app)['dock'] = dock
    saves = []
    monkeypatch.setattr(browser.app, 'save', lambda: saves.append(True))
    render(browser)
    rect = browser.app.history_browser_rect
    start = drag_point(browser.app)
    points = {'left': (26, 0), 'right': (26, 159), 'top': (6, 80), 'bottom': (45, 80)}
    assert H.handle_mouse(browser.app, *start, 'press')
    assert H.handle_mouse(browser.app, *points[target], 'drag')
    assert H.initialize(browser.app)['drag']['preview'] == target
    rows, _ = render(browser)
    assert 'Drop job history: ' + target in L.to_text(rows, 160)
    assert H.handle_mouse(browser.app, *points[target], 'release')
    assert H._view(browser.app)['dock'] == target
    assert saves == [True]
    assert H.initialize(browser.app)['drag'] is None


def test_handle_click_without_motion_does_not_redock(browser, monkeypatch):
    H._view(browser.app)['dock'] = 'right'
    render(browser)
    rect = browser.app.history_browser_rect
    monkeypatch.setattr(browser.app, 'save', lambda: pytest.fail('Click saved a new dock'))
    H.handle_mouse(browser.app, *drag_point(browser.app), 'press')
    H.handle_mouse(browser.app, *drag_point(browser.app), 'release')
    assert H._view(browser.app)['dock'] == 'right'


@pytest.mark.parametrize('reason', ['central', 'outside', 'resize', 'tab', 'modal', 'menu', 'escape'])
def test_invalid_or_interrupted_dock_drag_reverts(browser, reason, monkeypatch):
    H._view(browser.app)['dock'] = 'right'
    render(browser)
    rect = browser.app.history_browser_rect
    H.handle_mouse(browser.app, *drag_point(browser.app), 'press')
    monkeypatch.setattr(browser.app, 'save', lambda: pytest.fail('Cancelled drag saved'))
    if reason == 'escape':
        assert H.handle_key(browser.app, 'esc')
    else:
        if reason == 'resize':
            browser.app.width += 1
        elif reason == 'tab':
            browser.app.tab = 'research'
        elif reason == 'modal':
            browser.app.mode = 'help'
        elif reason == 'menu':
            browser.app.toolbar_state['menu'] = 'view'
        point = (25, 80) if reason == 'central' else (6, -1) if reason == 'outside' else (6, 80)
        assert H.handle_mouse(browser.app, *point, 'release')
    assert H.initialize(browser.app)['views']['analytics']['dock'] == 'right'
    assert H.initialize(browser.app)['drag'] is None


@pytest.mark.parametrize('reason', ['resize', 'tab', 'modal', 'menu'])
def test_maintenance_cancels_stale_capture_without_waiting_for_a_mouse_report(browser, reason):
    render(browser)
    rect = browser.app.history_browser_rect
    H.handle_mouse(browser.app, *drag_point(browser.app), 'press')
    assert H.initialize(browser.app)['drag']
    if reason == 'resize':
        browser.app.width += 1
    elif reason == 'tab':
        browser.app.tab = 'jobs'
    elif reason == 'modal':
        browser.app.mode = 'help'
    else:
        browser.app.toolbar_state['menu'] = 'view'
    H.tick(browser.app)
    assert H.initialize(browser.app)['drag'] is None


def test_browser_focus_does_not_leak_to_another_workspace(browser):
    render(browser)
    H.run_command(browser.app, ['history-focus'])
    assert H.initialize(browser.app)['focused']
    browser.app.tab = 'log'
    render(browser)
    assert not H.initialize(browser.app)['focused']


def test_off_button_and_restore_preserve_orientation_and_recover_space(browser):
    H.run_command(browser.app, ['history-dock', 'left'])
    H.run_command(browser.app, ['history-browser', 'off'])
    rows, _ = render(browser)
    rect = browser.app.history_browser_content_rect
    assert rect.width == 160 and rect.height == 39
    assert 'Job history +' in L.row_text(rows[0])
    data = H.save(browser.app)
    browser.app.history_browser_state = None
    H.restore(browser.app, data)
    assert H._view(browser.app)['dock'] == 'left'
    assert H._view(browser.app)['enabled'] is False
    render(browser)
    H.handle_mouse(browser.app, 6, 4, 'left')
    assert H._view(browser.app)['enabled'] is True


def test_dock_button_changes_the_effective_auto_layout_on_its_first_click(browser):
    render(browser)
    assert H.initialize(browser.app)['frame']['dock'] == 'right'
    H.run_command(browser.app, ['history-dock', 'next'])
    assert H._view(browser.app)['dock'] == 'bottom'


def test_focus_command_recovers_a_hidden_browser(browser):
    H.run_command(browser.app, ['history-browser', 'off'])
    render(browser)
    assert not H.handle_key(browser.app, 'up')
    H.run_command(browser.app, ['history-focus'])
    assert H._view(browser.app)['enabled'] is True


def test_separator_drag_has_buffer_and_does_not_poll_or_save_until_release(browser, monkeypatch):
    render(browser)
    divider = pane_drag.initialize(browser.app)['dividers']['history:analytics']
    saves = []
    monkeypatch.setattr(browser.app, 'save', lambda: saves.append(True))
    monkeypatch.setattr(browser.store, 'snapshot', lambda: pytest.fail('Divider polled Slurm'))
    assert pane_drag.handle_mouse(browser.app, divider.y + 3, divider.x - 1, 'press')
    assert pane_drag.handle_mouse(browser.app, divider.y + 3, divider.x - 20, 'drag')
    assert H._view(browser.app)['ratio'] > 25
    assert saves == []
    assert pane_drag.handle_mouse(browser.app, divider.y + 3, divider.x - 20, 'release')
    assert saves == [True]


def test_group_representatives_keep_actual_job_ids_and_fold_globally(browser):
    browser.store.jobs = [Job('1200_1', 'cohort', 'cpu', 'RUNNING'), Job('1200_2', 'cohort', 'cpu', 'RUNNING')]
    browser.app.table_state['groups'] = True
    browser.app.analytics_job = '1200_2'
    _, hits = render(browser)
    groups = [hit for hit in hits if hit[1] == 'control' and ':group:' in hit[2]['id']]
    assert groups
    state = H.initialize(browser.app)
    group_id = groups[0][2]['action'][1].split()[-1]
    assert job_groups.fold(browser.app, group_id, True)
    _, hits = render(browser)
    group_items = [item for item in state['items'] if item.meta and item.meta.group.id == group_id]
    assert len(group_items) == 1
    assert group_items[0].record.id in ('1200_1', '1200_2')
    assert all(item.record.id != group_id for item in state['items'])
    assert H.activate(browser.app, group_items[0].record.id)


def test_left_right_fold_a_selected_child_without_changing_its_data_source(browser):
    browser.store.jobs = [Job('1200_1', 'cohort', 'cpu', 'RUNNING'), Job('1200_2', 'cohort', 'cpu', 'RUNNING')]
    render(browser)
    assert H.activate(browser.app, '1200_1')
    render(browser)
    assert H.handle_key(browser.app, 'left')
    rows, hits = render(browser)
    assert browser.app.analytics_job == '1200_1'
    assert len([item for item in H.initialize(browser.app)['items'] if item.meta]) == 1
    representative = next(hit for hit in hits if hit[1] == 'control' and hit[2]['id'] == 'history:analytics:job:1200_2')
    assert '>1200_1' in L.row_text(rows[representative[0]])
    assert '1200_2 [>1200_1]' in L.row_text(rows[representative[0]])
    assert any('sel' in style.split('+') for _, style in rows[representative[0]])
    assert representative[2]['action'] == ('command', 'history-job 1200_2')
    assert H.handle_key(browser.app, 'right')
    render(browser)
    assert len([item for item in H.initialize(browser.app)['items'] if item.meta]) == 2
    assert browser.app.analytics_job == '1200_1'


def test_ten_thousand_records_publish_only_visible_controls(browser, monkeypatch):
    browser.store.finished = [Finished(str(i), 'history', 'COMPLETED') for i in range(10000)]
    calls = []
    rows, hits = render(browser, 160, 20, lambda w, h: (calls.append((w, h)) or ([[]] * h), []))
    assert len(H.initialize(browser.app)['records']) == 10000
    assert len(hits) < 40
    assert len(rows) == 20 and len(calls) == 1
    # Stable data avoids sorting/parsing all records on the next frame.
    monkeypatch.setattr(H, 'stamp', lambda *_: pytest.fail('Stable browser re-sorted records'))
    monkeypatch.setattr(job_groups, 'project_records', lambda *_args, **_kwargs: pytest.fail('Stable browser rebuilt 10,000 projected items'))
    render(browser, 160, 20)


def test_geometry_offset_is_idempotent(browser):
    render(browser)
    H.offset_hits(browser.app, 8)
    first = browser.app.history_browser_rect
    H.offset_hits(browser.app, 8)
    assert browser.app.history_browser_rect == first
    assert first.y == 8


def test_sanitized_record_labels_and_ascii_glyphs(browser):
    browser.store.jobs[0].name = 'bad\x1b[31m\tname界'
    browser.views.g = L.Glyphs(True)
    rows, _ = render(browser)
    text = L.to_text(rows, 160)
    assert '\x1b' not in text and '\t' not in text
    assert '::' in text and '⠿' not in text and '◆' not in text
    assert text.isascii()


def test_ascii_group_labels_escape_external_unicode(browser):
    browser.store.jobs = [Job('1200_1', 'cohort界', 'cpu', 'RUNNING'), Job('1200_2', 'cohort界', 'cpu', 'RUNNING')]
    browser.views.g = L.Glyphs(True)
    rows, _ = render(browser)
    assert L.to_text(rows, 160).isascii()


@pytest.mark.parametrize('tab', H.TABS)
@pytest.mark.parametrize('dock', ['left', 'right', 'top', 'bottom'])
def test_native_compose_preserves_explicit_history_job_and_hit_geometry(browser, tab, dock):
    from raven.views import Views
    app = browser.app
    app.tab = tab
    app.analytics_job, app.research_job_id, app.log_job = '301', '301', '301'
    app.log_record = browser.store.finished[1]
    app.selected_id = '301'
    H._view(app).update(dock=dock, selected='301', explicit=True)
    views = Views(browser.views.g, app.cfg)
    app.views_ref = views
    rows, hits = views.compose(browser.store.snapshot(), app, 160, 50)
    assert len(rows) == 50
    assert all(L.vlen(L.row_text(row)) <= 160 for row in rows)
    assert all(0 <= y < 50 for y, _, _ in hits)
    expected = {'analytics': 'analytics_job', 'research': 'research_job_id', 'log': 'log_job', 'deps': 'selected_id'}[tab]
    assert getattr(app, expected) == '301'
    item = next(hit for hit in hits if hit[1] == 'control' and hit[2]['id'] == 'history:' + tab + ':job:301')
    assert item[2]['action'] == ('command', 'history-job 301')
    rect = app.history_browser_rect
    assert rect.contains(item[0], item[2]['left'])
    control = app.interaction_state['graph'].get(item[2]['id'])
    assert control.rect.left == item[2]['left']
    assert control.rect.top == item[0]


def test_native_browser_click_consumes_history_column_before_log_line_selection(browser):
    from raven.views import Views
    app = browser.app
    app.tab, app.log_job, app.log_record = 'log', '900', browser.store.jobs[0]
    views = Views(browser.views.g, app.cfg)
    app.views_ref = views
    rows, hits = views.compose(browser.store.snapshot(), app, 160, 50)
    H.run_command(app, ['history-scroll', 'end'])
    rows, hits = views.compose(browser.store.snapshot(), app, 160, 50)
    target = next(hit for hit in hits if hit[1] == 'control' and hit[2]['id'] == 'history:log:job:301')
    # A source line at the same y must not intercept a history-pane click.
    hits.append((target[0], 'log_line', 4))
    from raven.interaction import publish
    from raven.metric_live import descriptors as live_descriptors
    from raven.scrollbars import descriptors as scroll_descriptors
    publish(app, app.frame_rows, hits, 160, 50,
            overlays=(getattr(app, 'content_overlay_rows', [])
                      + getattr(app, 'toolbar_overlay_rows', [])),
            extra_controls=live_descriptors(app) + scroll_descriptors(app))
    app.click(target[0], target[2]['left'] + 3, hits)
    assert app.log_job == '301'
    assert app.log_record.id == '301'
    assert not app.logs.selection_active


def test_native_browser_shares_one_group_input_check_per_composed_frame(browser, monkeypatch):
    from raven.views import Views
    app = browser.app
    app.tab = 'analytics'
    views = Views(browser.views.g, app.cfg)
    app.views_ref = views
    registry = job_groups.registry(app)
    original, calls = registry.ensure, []
    def ensure(snap):
        calls.append(snap)
        return original(snap)
    monkeypatch.setattr(registry, 'ensure', ensure)
    views.compose(browser.store.snapshot(), app, 160, 50)
    assert len(calls) == 1
    assert app.job_groups_frame_index is None


def test_native_dependency_graph_does_not_promote_an_old_jobs_selection_to_scope(browser):
    from raven.views import Views
    app = browser.app
    app.tab, app.selected_id = 'deps', '900'
    views = Views(browser.views.g, app.cfg)
    app.views_ref = views
    rows, _ = views.compose(browser.store.snapshot(), app, 160, 50)
    assert H._view(app)['explicit'] is False
    assert H._view(app)['selected'] is None
    assert 'dependency chains:' in L.to_text(rows, 160)
    assert 'dependencies / 900' not in L.to_text(rows, 160)


def test_native_dependency_graph_keeps_native_selection_without_becoming_scoped(browser):
    from raven.views import Views
    browser.store.jobs.append(Job('901', 'dependent', 'cpu', 'PENDING', dependency='afterok:900'))
    app = browser.app
    app.tab = 'deps'
    views = Views(browser.views.g, app.cfg)
    app.views_ref = views
    views.compose(browser.store.snapshot(), app, 160, 50)
    rows, _ = views.compose(browser.store.snapshot(), app, 160, 50)
    assert H._view(app)['explicit'] is False
    assert 'dependency chains:' in L.to_text(rows, 160)


@pytest.mark.parametrize('tab', ['analytics', 'deps'])
def test_native_removed_explicit_job_does_not_select_an_unrelated_running_job(browser, tab):
    from raven.views import Views
    app = browser.app
    app.tab = tab
    views = Views(browser.views.g, app.cfg)
    app.views_ref = views
    views.compose(browser.store.snapshot(), app, 160, 50)
    assert H.activate(app, '301')
    browser.store.finished = [record for record in browser.store.finished if record.id != '301']
    rows, _ = views.compose(browser.store.snapshot(), app, 160, 50)
    field = 'analytics_job' if tab == 'analytics' else 'selected_id'
    assert getattr(app, field) in ('301', None)
    text = L.to_text(rows, 160)
    assert '301' in text and ('unavailable' in text.lower() or 'no longer' in text.lower())


def test_session_restore_only_retains_explicit_scope_when_marked(browser):
    H.restore(browser.app, {'views': {'deps': {'selected': '301', 'explicit': True},
                                    'analytics': {'selected': '302'}}})
    assert H.initialize(browser.app)['views']['deps']['explicit'] is True
    assert H.initialize(browser.app)['views']['analytics']['explicit'] is False


MOUSE = SimpleNamespace(BUTTON1_CLICKED=1, BUTTON1_PRESSED=2, BUTTON1_RELEASED=4,
    BUTTON1_DOUBLE_CLICKED=8, BUTTON3_CLICKED=16, BUTTON3_PRESSED=32,
    BUTTON4_PRESSED=64, BUTTON5_PRESSED=128, REPORT_MOUSE_POSITION=256, BUTTON_SHIFT=512)


@pytest.mark.parametrize('dock', ['left', 'right', 'top', 'bottom'])
@pytest.mark.parametrize('motion', [MOUSE.REPORT_MOUSE_POSITION, MOUSE.REPORT_MOUSE_POSITION | MOUSE.BUTTON1_PRESSED])
def test_real_input_routes_dock_handle_capture_motion_and_release(browser, dock, motion):
    from raven import screen
    from raven.views import Views
    app = browser.app
    H._view(app)['dock'] = dock
    views = Views(browser.views.g, app.cfg)
    app.views_ref = views
    views.compose(browser.store.snapshot(), app, 160, 50)
    rect = app.history_browser_rect
    origin = app.body_origin
    def mouse(y, x, bits):
        screen._apply_input(app, ('mouse', (0, x, y, 0, bits)), app.last_hits, MOUSE)
    mouse(*drag_point(app), MOUSE.BUTTON1_PRESSED)
    assert H.initialize(app)['drag'] is not None
    assert pane_drag.initialize(app)['capture'] is None
    # Move to the right edge from any starting orientation.
    mouse(origin + 10, 159, motion)
    assert H.initialize(app)['drag']['preview'] == 'right'
    views.compose(browser.store.snapshot(), app, 160, 50)
    mouse(origin + 10, 159, MOUSE.BUTTON1_RELEASED)
    assert H._view(app)['dock'] == 'right'
    assert H.initialize(app)['drag'] is None


@pytest.mark.parametrize('tab', ['research', 'log'])
def test_real_input_routes_browser_wheel_before_data_scrolling(browser, tab):
    from raven import screen
    from raven.views import Views
    app = browser.app
    app.tab = tab
    views = Views(browser.views.g, app.cfg)
    app.views_ref = views
    views.compose(browser.store.snapshot(), app, 160, 30)
    rect = app.history_browser_rect
    before = app.research_scroll, app.logs.top, app.logs.cursor
    screen._apply_input(app, ('mouse', (0, rect.x + 5, rect.y + 2, 0, MOUSE.BUTTON5_PRESSED)), app.last_hits, MOUSE)
    assert H._view(app)['top'] == 3
    assert (app.research_scroll, app.logs.top, app.logs.cursor) == before
