from types import SimpleNamespace
from raven import scrollbars as S, scrolling
from raven.interaction import Rect, publish as publish_graph


def app():
    return SimpleNamespace(mode='main', tab='analytics', width=80, height=30, cfg={},
                           cursor={}, theme='default', animations_enabled=True,
                           interaction_state={'pointer': None})


def pane(a, count=100, page=10, target=0, context='job1', rect=(5, 3, 15, 30)):
    a.offset = target
    return S.register(a, 'test', rect, count, page, target, target,
                      lambda value: setattr(a, 'offset', value), context=context, header=(4, 3, 30))


def test_drag_capture_clamps_and_never_changes_cursor():
    a = app()
    pane(a)
    S.publish(a, 80, 30)
    assert S.handle_mouse(a, 5, 29, 'press')
    assert S.handle_mouse(a, 1000, -200, 'drag')
    assert a.offset == 90 and a.cursor == {}
    assert S.handle_mouse(a, -100, 200, 'release')
    assert a.offset == 0 and not S.initialize(a)['capture']


def test_endpoint_buttons_and_keyboard_semantics():
    a = app()
    pane(a)
    S.publish(a, 80, 30)
    assert S.handle_mouse(a, 4, 5, 'left')
    assert a.offset == 90
    assert S.activate(a, 'test', 'top') and a.offset == 0
    descriptors = S.descriptors(a)
    assert {item['action'] for item in descriptors} == {('scrollbar', 'test', 'top'), ('scrollbar', 'test', 'bottom')}


def test_source_or_geometry_change_consumes_stale_release():
    for change in ('source', 'geometry', 'tab'):
        a = app()
        pane(a)
        S.publish(a, 80, 30)
        S.handle_mouse(a, 5, 29, 'press')
        S.begin_frame(a)
        pane(a, context='job2' if change == 'source' else 'job1',
             rect=(6, 3, 16, 30) if change == 'geometry' else (5, 3, 15, 30))
        if change == 'tab':
            a.tab = 'research'
            assert S.handle_mouse(a, 8, 29, 'release')
        else:
            S.publish(a, 80, 30)
            assert S.handle_mouse(a, 8, 29, 'release')
        assert a.offset == 0
        assert not S.initialize(a)['capture']


def test_count_growth_does_not_drop_drag():
    a = app()
    pane(a)
    S.publish(a, 80, 30)
    S.handle_mouse(a, 5, 29, 'press')
    S.begin_frame(a)
    pane(a, count=200)
    S.publish(a, 80, 30)
    assert S.initialize(a)['capture']
    S.handle_mouse(a, 14, 29, 'release')
    assert a.offset == 190


def test_modal_blocks_underlying_bar_and_header():
    a = app()
    pane(a)
    S.publish(a, 80, 30, overlays=[(y, 0, [(' ' * 50, '')]) for y in range(4, 15)])
    assert not S.handle_mouse(a, 4, 5, 'left')
    assert not S.handle_mouse(a, 5, 29, 'press')
    assert not S.descriptors(a)
    assert not S.feedback(a)


def test_staging_transforms_and_absolute_modal_geometry():
    a = app()
    pane(a)
    S.register(a, 'modal', (10, 10, 20, 40), 30, 10, 0, 0, lambda value: None, absolute=True, layer=1)
    records = S.take_since(a, 0)
    mapped = S.map_records(records, {y: y + 2 for y in range(4, 15)}, dx=3, dy=4)
    assert mapped[0].rect == Rect(11, 6, 21, 33)
    assert mapped[0].header == (10, 6, 33)
    assert mapped[1].rect == Rect(10, 10, 20, 40)


def test_manual_offsets_bounded_context_guard():
    a = app()
    for index in range(200):
        S.set_manual(a, str(index), index, context='source')
    assert len(S.initialize(a)['manual']) == S.MAX_MANUAL
    assert S.manual(a, '199', context='source') == 199
    assert S.manual(a, '199', context='changed') is None


def test_nonoverflow_pane_remains_available_to_text_selection():
    a = app()
    pane(a, count=4, page=10)
    S.publish(a, 80, 30)
    assert len(S.initialize(a)['panes']) == 1
    assert not S.feedback(a) and not S.descriptors(a)
    assert not S.handle_mouse(a, 5, 29, 'press')


def test_reduced_motion_scrollbar_target_immediate():
    a = app()
    a.theme = 'reader'
    pane(a)
    S.publish(a, 80, 30)
    S.handle_mouse(a, 4, 5, 'left')
    assert scrolling.viewport(a, 'test', a.offset, 100, 10) == 90


def test_count_shrink_clamps_manual_seek_before_first_reverse_wheel():
    a = app()
    pane(a)
    S.publish(a, 80, 30)
    S.activate(a, 'test', 'bottom')
    S.begin_frame(a)
    pane(a, count=50, target=40)
    S.publish(a, 80, 30)
    assert S.manual(a, 'test') == 40
    assert S.handle_mouse(a, 14, 29, 'wheel-up')
    assert a.offset == 37


def test_batched_wheel_uses_latest_target_without_repaint():
    a = app()
    pane(a, count=1000)
    S.publish(a, 80, 30)
    for _ in range(100):
        assert S.handle_mouse(a, 6, 7, 'wheel-down')
    assert a.offset == 300
    for _ in range(100):
        S.handle_mouse(a, 6, 7, 'wheel-up')
    assert a.offset == 0


def test_fresh_scrollbar_press_commits_old_text_capture():
    a = app()
    a.text_selection_state = {'capture': {'key': 'old'}, 'selection': {'anchor': 2, 'end': 4}}
    pane(a)
    S.publish(a, 80, 30)
    assert S.handle_mouse(a, 5, 29, 'press')
    assert a.text_selection_state['capture'] is None
    assert a.text_selection_state['selection'] == {'anchor': 2, 'end': 4}
