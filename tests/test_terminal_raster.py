import base64
import io
import json
from pathlib import Path
import re
from types import SimpleNamespace

import pytest

from raven import layout as L
from raven.chart_interaction import Plot
from raven.interaction import Rect
from raven.terminal_raster import TerminalRaster, capability, encode, MAX_ENCODED


class Terminal(io.StringIO):
    def isatty(self): return True


def fixture(clock=lambda: 1., stream=None):
    stream = stream or Terminal()
    raster = TerminalRaster(stream, 'kitty', environ={'TERM': 'xterm-kitty', 'KITTY_WINDOW_ID': '7'}, clock=clock)
    rect = Rect(1, 2, 3, 6)
    plot = Plot(('cpu',), rect, rect, (0, 10), (0, 1))
    app = SimpleNamespace(mode='main', tab='jobs', selected_id='7', ascii=False,
                          chart_interaction_state={'plots': (plot,)}, text_selection_state={})
    rows = [[('axis label', 'cyan')], [('  ⠁⠂⠄⡀tail', 'green')], [('  ⠈⠐⠠⢀tail', 'green')], [('time axis', 'dim')]]
    return app, rows, raster, stream


def draw(raster, app, rows, overlays=(), width=20, height=4):
    prepared = raster.prepare(app, rows, overlays, width, height)
    raster.before_flush(); assert raster.after_flush()
    return prepared


@pytest.mark.parametrize('option,env,enabled', [
    ('off', {'TERM':'xterm-kitty','KITTY_WINDOW_ID':'7'}, False),
    ('kitty', {'TERM':'xterm-kitty','KITTY_WINDOW_ID':'7'}, True),
    ('kitty', {'TERM':'xterm-256color'}, False),
    ('kitty', {'TERM':'xterm-kitty','KITTY_WINDOW_ID':'7','TMUX':'socket'}, False),
    ('kitty', {'TERM':'xterm-kitty','KITTY_WINDOW_ID':'7','SSH_CONNECTION':'remote'}, False),
    ('sixel', {'TERM':'xterm-kitty','KITTY_WINDOW_ID':'7'}, False)])
def test_explicit_capability_and_protocol_fallback(option, env, enabled):
    assert capability(option, Terminal(), env)[0] is enabled
    assert not capability(option, io.StringIO(), env)[0]


def test_exact_braille_occupancy_chunks_and_exclusive_image_identity():
    app, rows, raster, terminal = fixture()
    prepared = draw(raster, app, rows)
    assert L.row_text(prepared[1]) == '      tail'
    assert L.row_text(rows[1]) == '  ⠁⠂⠄⡀tail'  # original remains export/copy source
    raw = terminal.getvalue()
    assert f'i={raster.image_id}' in raw and 'q=2' in raw and 'f=32,s=8,v=8,c=4,r=2' in raw
    chunks = re.findall('\x1b_G[^;]+;([^\x1b]*)\x1b\\\\', raw)
    rgba = base64.b64decode(''.join(chunks))
    assert len(rgba) == 8 * 8 * 4
    assert sum(rgba[i] == 255 for i in range(3, len(rgba), 4)) == 8
    before = terminal.getvalue()
    draw(raster, app, rows)
    assert terminal.getvalue() == before
    assert app.chart_interaction_state['plots'][0].rect == Rect(1, 2, 3, 6)


@pytest.mark.parametrize('change', ['overlay', 'selection', 'capture', 'mode', 'view', 'resize', 'clip'])
def test_overlay_selection_resize_and_view_lifecycle_restore_text(change):
    now = [1.]
    app, rows, raster, terminal = fixture(clock=lambda: now[0])
    draw(raster, app, rows)
    overlays, width = (), 20
    if change == 'overlay': overlays = [(1, 2, [('menu', 'rev')])]
    if change == 'selection': app.text_selection_state['selection'] = {'anchor': 0}
    if change == 'capture': app.chart_interaction_state['capture'] = {'active': True}
    if change == 'mode': app.mode = 'settings'
    if change == 'view': app.tab = 'history'; app.chart_interaction_state['plots'] = ()
    if change == 'resize': width = 4
    if change == 'clip':
        plot = app.chart_interaction_state['plots'][0]
        app.chart_interaction_state['plots'] = (Plot(plot.key, plot.rect, Rect(1,2,2,6), plot.x_bounds, plot.y_bounds),)
    prepared = draw(raster, app, rows, overlays, width)
    assert prepared is rows
    assert f'a=d,d=I,i={raster.image_id}' in terminal.getvalue()
    assert 'd=A' not in terminal.getvalue() and raster.active is None


def test_rate_limit_never_leaves_stale_image_over_new_source_and_close_is_idempotent():
    now = [1.]
    app, rows, raster, terminal = fixture(clock=lambda: now[0])
    draw(raster, app, rows)
    changed = list(rows); changed[1] = [('  ⣿⣿⣿⣿tail', 'green')]
    now[0] = 1.1
    assert draw(raster, app, changed) is changed and raster.active is None
    now[0] = 2.
    assert draw(raster, app, changed) is not changed
    raster.close(); before = terminal.getvalue(); raster.close()
    assert terminal.getvalue() == before


def test_semantic_style_changes_restore_complete_colored_text():
    now = [1.]
    app, rows, raster, terminal = fixture(clock=lambda: now[0])
    draw(raster, app, rows)
    colored = list(rows)
    colored[1] = [('  ', 'axis'), ('⠁⠂', 'green'), ('⠄⡀tail', 'red')]
    assert draw(raster, app, colored) is colored
    assert raster.active is None and f'a=d,d=I,i={raster.image_id}' in terminal.getvalue()
    # Axis/blank styles outside actual occupied dots do not force a fallback.
    uniform = list(rows)
    uniform[1] = [('  ', 'axis'), ('⠁⠂⠄⡀', 'green'), ('tail', 'red')]
    now[0] = 2.
    assert raster.prepare(app, uniform, (), 20, 4) is not uniform


def test_bounds_unknown_glyphs_and_write_failure_keep_fallback():
    raw = encode([['⣿'] * 80 for _ in range(24)], 1, 0, 0)
    assert len(raw) <= MAX_ENCODED
    with pytest.raises(ValueError): encode([['⣿'] * 81], 1, 0, 0)
    with pytest.raises(ValueError): encode([['\x1b']], 1, 0, 0)
    app, rows, raster, terminal = fixture()
    rows[1] = [('  界test', '')]
    assert draw(raster, app, rows) is rows
    class Failing(Terminal):
        failed = False
        def write(self, raw):
            if not self.failed:
                self.failed = True
                raise OSError('output failure')
            return super().write(raw)
    app, rows, raster, terminal = fixture(stream=Failing())
    raster.prepare(app, rows, (), 20, 4)
    assert not raster.after_flush() and not raster.enabled
    assert 'a=d,d=I' in terminal.getvalue()
    assert raster.prepare(app, rows, (), 20, 4) is rows


def test_default_config_matches_published_ui_value_contract():
    from raven.config import DEFAULTS
    import jsonschema
    schema = json.loads((Path(__file__).parents[1] / 'docs/schemas/terminal-raster.v1.schema.json').read_text())
    jsonschema.Draft202012Validator(schema).validate(DEFAULTS['terminal_raster'])
    assert DEFAULTS['terminal_raster'] == 'off'


from test_quiet_jobs_frames import scene


@pytest.mark.parametrize('write_succeeds', [True, False])
def test_actual_curses_hook_orders_cleanup_flush_graphics_and_restores_fallback(scene, monkeypatch, write_succeeds):
    import curses
    from raven import screen, terminal_raster
    calls = []
    class Renderer:
        enabled = True
        status = 'fixture'
        changed = True
        def __init__(self, *_): pass
        def prepare(self, app, rows, overlays, width, height):
            calls.append('prepare'); return rows
        def before_flush(self): calls.append('delete')
        def after_flush(self): calls.append('image'); return write_succeeds
        def close(self): calls.append('close')
    monkeypatch.setattr(terminal_raster, 'TerminalRaster', Renderer)
    scene.app.cfg.data['terminal_raster'] = 'kitty'
    scene.app.cfg.data['animations'] = False
    monkeypatch.setattr(curses, 'has_colors', lambda: False)
    for name in ('curs_set', 'mousemask', 'mouseinterval', 'set_escdelay'):
        monkeypatch.setattr(curses, name, lambda *args: None)
    monkeypatch.setattr(curses, 'doupdate', lambda: calls.append('flush'))
    class Window:
        def getmaxyx(self): return 40, 160
        def get_wch(self): raise curses.error('no input')
        def keypad(self, enabled): pass
        def timeout(self, milliseconds): pass
        def erase(self): pass
        def addstr(self, *args): pass
        def noutrefresh(self): pass
    monkeypatch.setattr(curses, 'wrapper', lambda callback: callback(Window()))
    monkeypatch.setattr(screen, '_wait_for_input', lambda *args: ('q', None))
    screen.run_curses(scene.app, scene.views, None, scene.store, None, scene.app.cfg)
    assert calls == ['prepare', 'delete', 'flush', 'image'] + ([] if write_succeeds else ['flush']) + ['close']
