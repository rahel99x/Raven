"""Shared embedded terminal pane; rendering never starts a process.

One session service belongs to the application, not a selected job or a page.
All clipboard selections pin terminal text, and input owns an explicit focus
identity so delayed bytes cannot be redirected to another shell.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import islice
from bisect import bisect_right
import os
import shlex
import socket
import time
from typing import Any

from . import layout as L

PAGES = frozenset(('jobs', 'history', 'analytics', 'deps', 'log', 'research'))
MAX_SELECTION_BYTES = 2 << 20
MAX_REPORT_LINES = 10000
MAX_SESSIONS = 8


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    width: int
    height: int

    def contains(self, y, x):
        return self.x <= x < self.x + self.width and self.y <= y < self.y + self.height


@dataclass
class TerminalState:
    service: Any = None
    launches: dict = field(default_factory=dict)
    active: str | None = None
    modes: dict = field(default_factory=dict)
    keyboard_focus: bool = False
    focus_generation: int = 0
    page: str = ''
    rect: Rect | None = None
    body_rect: Rect | None = None
    rail_rect: Rect | None = None
    controls: list = field(default_factory=list)
    copy_mode: bool = False
    follow: bool = True
    top: int = 0
    selected: tuple | None = None
    selection_anchor: int | None = None
    selection_end: int | None = None
    selection_source: tuple = ()
    selection_wraps: tuple = ()
    selected_wraps: tuple = ()
    history_wraps: dict = field(default_factory=dict)
    history_wrap_pages: dict = field(default_factory=dict)
    clipboard_preview: dict | None = None
    capture: tuple | None = None
    discard_release: bool = False
    prefix: bool = False
    error: str = ''
    dialog: dict | None = None
    preview: dict | None = None
    report: dict | None = None
    menu: bool = False
    menu_top: int = 0
    menu_index: int = 0
    menu_items: list | None = None
    visibility: dict = field(default_factory=dict)
    unread: dict = field(default_factory=dict)
    sequences: dict = field(default_factory=dict)
    sizes: dict = field(default_factory=dict)
    snapshots: dict = field(default_factory=dict)
    generation: int = 0
    painted: list = field(default_factory=list)
    painted_key: tuple = ()
    history_pages: dict = field(default_factory=dict)
    history_seen: dict = field(default_factory=dict)
    history_requested: dict = field(default_factory=dict)
    pending_search: bool = False
    history_loading: dict = field(default_factory=dict)
    history_snapshot: dict = field(default_factory=dict)
    geometry: tuple = ()
    hover: tuple | None = None
    search: str = ''
    search_matches: list = field(default_factory=list)
    search_index: int = -1
    history: dict = field(default_factory=dict)
    history_results: dict = field(default_factory=dict)

    @property
    def sessions(self):
        return self.service.sessions if self.service is not None else {}


def initialize(app):
    state = getattr(app, 'terminal_state', None)
    if not isinstance(state, TerminalState):
        state = TerminalState(page=str(getattr(app, 'tab', 'jobs')))
        app.terminal_state = state
    return state


def _say(app, text, failure=False):
    fn = getattr(app, 'fail' if failure else 'say', None)
    if callable(fn):
        fn(str(text))


def enabled(app):
    return isinstance(getattr(app, 'terminal_state', None), TerminalState) and getattr(app, 'tab', '') in PAGES


def showing(app):
    return enabled(app) and initialize(app).modes.get(getattr(app, 'tab', ''), 'details') == 'terminal'


def _bump(state):
    state.generation += 1


def _focus(app, value):
    state = initialize(app)
    value = bool(value)
    if state.keyboard_focus != value:
        state.keyboard_focus = value
        state.focus_generation += 1
        state.prefix = False
        if state.service is not None and state.active:
            try:
                state.service.send(state.active, {'type': 'focus', 'focused': value})
            except (OSError, ValueError, RuntimeError):
                pass
        _bump(state)
    if value:
        from .startup import dismiss
        dismiss(app)
        from .workspace_layout import initialize as layout_state
        layout_state(app).focus = 'details'
        panel = getattr(app, 'job_panel_state', None)
        if isinstance(panel, dict):
            panel['focus'] = ''


def blur(app):
    state = initialize(app)
    _focus(app, False)
    if state.capture:
        state.capture = None
        state.discard_release = True
    if state.preview:
        state.preview = None
        state.focus_generation += 1


def begin_frame(app):
    if not enabled(app):
        state = getattr(app, 'terminal_state', None)
        if isinstance(state, TerminalState):
            blur(app)
            state.rect = state.body_rect = state.rail_rect = None
            state.controls = []
        return
    state = initialize(app)
    page = getattr(app, 'tab', '')
    if page != state.page:
        blur(app)
        state.page = page
        state.focus_generation += 1
    state.rect = None
    state.controls = []


def focused(app):
    state = getattr(app, 'terminal_state', None)
    return (isinstance(state, TerminalState) and state.keyboard_focus and showing(app)
            and state.page == getattr(app, 'tab', '') and getattr(app, 'mode', 'main') == 'main')


def input_identity(app):
    state = getattr(app, 'terminal_state', None)
    return (state.active or 'terminal-ui', state.focus_generation) if focused(app) else None


def owns_mouse(app, y, x):
    state = getattr(app, 'terminal_state', None)
    return bool(isinstance(state, TerminalState) and showing(app) and
                (state.capture or state.body_rect and state.body_rect.contains(y, x)
                 or state.rail_rect and state.rail_rect.contains(y,x)))


def _service(app):
    state = initialize(app)
    if state.service is None:
        from .terminal_service import TerminalService
        state.service = TerminalService()
    return state.service


def _snapshot(app):
    state = initialize(app)
    if not state.active or state.service is None:
        return {}
    try:
        snapshot = state.service.snapshot(state.active)
    except (OSError, ValueError, RuntimeError, KeyError):
        return state.snapshots.get(state.active, {})
    if isinstance(snapshot, dict):
        state.snapshots[state.active] = snapshot
        return snapshot
    return {}


def _activate_session(app, sid):
    state = initialize(app)
    if state.active != sid:
        _focus(app, False)
        state.active = sid
        state.focus_generation += 1
        state.copy_mode, state.follow, state.top = False, True, 0
        state.selected = state.selection_anchor = state.selection_end = None
        state.selection_source = ()
        if state.capture:
            state.discard_release = True
        state.capture = None
        state.dialog = state.preview = state.report = state.clipboard_preview = None
        state.search_matches = []
    state.unread.pop(sid, None)
    state.modes[getattr(app, 'tab', 'jobs')] = 'terminal'
    _focus(app, True)
    _bump(state)


def open_session(app, *, argv=None, cwd=None, label=None, kind='local', metadata=None, **spec):
    """Explicit action only: create a shell/session with immutable launch context."""
    state = initialize(app)
    registered=[]
    try:
        service = _service(app)
        settings = getattr(app, 'cfg', {}).get('terminal', {})
        if len(service.sessions) >= max(1, min(MAX_SESSIONS, int(settings.get('max_sessions', MAX_SESSIONS)))):
            raise ValueError('Close a terminal session before opening another (limit 8)')
        rows = max(1, state.body_rect.height if state.body_rect else 20)
        cols = max(1, state.body_rect.width if state.body_rect else 80)
        if argv is None:
            shell = str(settings.get('shell') or os.environ.get('SHELL') or '/bin/bash')
            argv = [shell] + (['-l'] if settings.get('login') else []) + ['-i']
            metadata = {**(metadata or {}), 'shell': shell, 'host': socket.gethostname()}
        options = dict(rows=rows, cols=cols, history_lines=max(1,min(10000,int(settings.get('history_lines',2000)))), label=label or ('local:' + socket.gethostname()),
                       kind=kind, metadata=metadata or {})
        if argv is not None:
            options['argv'] = argv
        if cwd is not None:
            options['cwd'] = cwd
        options.update(spec)
        raven = getattr(app, 'cfg', {}).get('raven', {})
        if raven.get('managed'):
            budget = raven.get('resources', {}).get('light', {}).get('terminal_scrollback_bytes', 2 * 1024 * 1024)
            # Enforce after launch options: reconnect/review specs cannot raise
            # the resolved Light policy's per-session retention ceiling.
            options['history_bytes'] = min(max(0, int(budget)), max(0, int(options.get('history_bytes', budget))))
        try:
            from .terminal_fusion import register_session
            def env_factory(sid):
                registered.append(sid)
                return register_session(app, sid, host=(metadata or {}).get('host', socket.gethostname()),
                                        cwd=cwd or '', metadata=metadata)
            options['env_factory'] = env_factory
        except ImportError:
            pass
        sid = service.open(**options)
        if not isinstance(sid, str) or not sid:
            raise RuntimeError('Terminal service did not return a session identity')
        state.launches[sid] = dict(argv=argv, cwd=cwd, label=label, kind=kind, metadata=dict(metadata or {}))
        _activate_session(app, sid)
        state.error = ''
        return sid
    except (ImportError, OSError, ValueError, RuntimeError, TypeError) as exc:
        for registered_sid in registered:
            _unregister_bridge(app, registered_sid)
        state.error = str(exc)[:1000]
        _say(app, 'Terminal: ' + state.error, True)
        state.modes[getattr(app, 'tab', 'jobs')] = 'terminal'
        _bump(state)
        return None


def prepare_input(app, text, label='Prepared command', *, helper_kind='generated'):
    state = initialize(app)
    if not isinstance(text, str) or len(text.encode('utf-8')) > MAX_SELECTION_BYTES or '\x00' in text:
        _say(app, 'Prepared command is invalid or too large', True)
        return False
    if not state.active:
        _say(app, 'Open a terminal session before preparing input', True)
        return False
    state.preview = {'text': text, 'label': str(label)[:100], 'session': state.active, 'top':0,
                     'helper_kind': helper_kind}
    state.dialog = state.report = None
    state.modes[getattr(app, 'tab', 'jobs')] = 'terminal'
    state.focus_generation += 1
    _focus(app, True)
    _bump(state)
    return True


def show_report(app, lines, title='Terminal report'):
    state = initialize(app)
    bounded=[]
    size=0
    for line in islice(lines,MAX_REPORT_LINES+1):
        text=str(line)[:4096]
        size+=len(text.encode('utf-8'))
        if len(bounded)>=MAX_REPORT_LINES or size>MAX_SELECTION_BYTES:
            bounded.append('[Report truncated at its line/byte budget; narrow the requested evidence.]')
            break
        bounded.append(text)
    state.report = {'lines': tuple(bounded), 'title': str(title)[:100], 'top': 0}
    state.dialog = state.preview = state.clipboard_preview = None
    state.menu = False
    state.modes[getattr(app, 'tab', 'jobs')] = 'terminal'
    state.focus_generation += 1
    _focus(app, True)
    _bump(state)


def _send(app, event):
    state = initialize(app)
    if not state.active or state.service is None:
        return False
    try:
        event = dict(event)
        if event.get('type') == 'key':
            event['key'] = {'space':' ', 'pgup':'pageup', 'pgdn':'pagedown', 'ctrl-space':'ctrl- '}.get(event.get('key'), event.get('key'))
        if event.get('type') == 'mouse' and isinstance(event.get('button'), str):
            button = event['button']
            number = 1 if 'middle' in button else 2 if 'right' in button else 0
            event['action'] = 'release' if 'release' in button else 'move' if button in ('move','motion','hover') or 'drag' in button else 'press'
            event['button'] = number
            if button in ('wheel-up','wheel-down'):
                event['action'] = 'wheel'
                event['delta'] = 1 if button == 'wheel-up' else -1
            if button in ('move','motion','hover') and not state.capture:
                event['button'] = 3
        if event.get('type') == 'paste':
            limit = max(1, min(1<<20, int(getattr(app, 'cfg', {}).get('terminal', {}).get('paste_limit_bytes', 1<<20))))
            if len(str(event.get('text','')).encode('utf-8')) > limit:
                _say(app, 'Paste exceeds the configured terminal input limit', True)
                return False
        result = state.service.send(state.active, event)
        if result is False:
            _say(app, getattr(state.service,'error','') or 'Terminal input queue is full or the session has exited', True)
        return result is not False
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        state.error = str(exc)[:1000]
        _say(app, 'Terminal input: ' + state.error, True)
        return False


def handle_event(app, event):
    if not focused(app):
        return False
    state = initialize(app)
    if not isinstance(event, dict):
        return True
    if event.get('type') == 'key':
        return handle_key(app, event.get('key', ''))
    if state.dialog or state.preview or state.report or state.menu or state.copy_mode:
        if state.dialog and event.get('type') == 'paste':
            text = event.get('text', '')
            state.dialog['value'] += str(text).replace('\n', ' ').replace('\r', ' ')[:4096-len(state.dialog['value'])]
            _bump(state)
        return True
    if state.prefix:
        state.prefix=False
        _send(app, {'type':'key','key':'ctrl-]'})
    if (event.get('type')=='paste' and isinstance(event.get('text'),str)
            and ('\n' in event['text'] or '\r' in event['text'])
            and getattr(app,'cfg',{}).get('terminal',{}).get('paste_preview',True)):
        prepare_input(app,event['text'],'Multiline paste — review before Insert',helper_kind='user-paste')
        return True
    _send(app, event)
    return True


def _line_text(row):
    if isinstance(row, str):
        return row
    return ''.join(str(cell[0]) for cell in row if isinstance(cell, (tuple, list)) and len(cell) >= 2)


def _lines(app):
    snap = _snapshot(app)
    # The service owns the bounded retained history; selection pins only its
    # chosen text and cannot accidentally include neighboring Tower panes.
    return tuple(str(line) for line in initialize(app).history.get(initialize(app).active, snap.get('history', ()))) + tuple(_line_text(row) for row in snap.get('lines', ()))


def _copy(app):
    state = initialize(app)
    if state.selected is None:
        _say(app, 'Select terminal lines in Copy mode before copying', True)
        return False
    text = ''.join(line + ('' if index==len(state.selected)-1 or
        index<len(state.selected_wraps) and state.selected_wraps[index] else '\n')
        for index,line in enumerate(state.selected))
    if len(text.encode('utf-8')) > MAX_SELECTION_BYTES:
        _say(app, 'Terminal selection exceeds the 2 MiB copy limit', True)
        return False
    from . import clipboard
    _say(app, clipboard.copy(text, getattr(app, 'state_dir', None), **clipboard.options(app)))
    return True


def _pin_selection(state):
    if state.selection_anchor is None or state.selection_end is None:
        return
    a, b = sorted((state.selection_anchor, state.selection_end))
    source = state.selection_source
    selected = source[max(0, a):min(len(source), b + 1)]
    if sum(len(line.encode('utf-8')) + 1 for line in selected) <= MAX_SELECTION_BYTES:
        state.selected = selected
        state.selected_wraps = state.selection_wraps[max(0,a):min(len(source),b+1)]
    else:
        state.selected = None


def _scroll(app, action):
    state = initialize(app)
    if not state.report:
        request_history(app)
    page = max(1, state.body_rect.height if state.body_rect else 10)
    if state.report:
        page = max(1, page - 1)
        data = state.report
        limit = max(0, _report_document(app)['total'] - page)
        current = data['top']
    else:
        data = None
        limit = max(0, len(_lines(app)) - page)
        current = limit if state.follow else state.top
    target = (0 if action == 'home' else limit if action == 'end' else
              current + {'up': -1, 'down': 1, 'pgup': -page, 'pgdn': page}.get(action, 0))
    target = max(0, min(limit, target))
    if data:
        data['top'] = target
    else:
        state.top, state.follow = target, action == 'end'
    _bump(state)


def _prompt(app, action, label, value=''):
    state = initialize(app)
    state.dialog = {'action': action, 'label': label, 'value': value[:4096]}
    state.report = state.preview = None
    state.menu = False
    state.focus_generation += 1
    _focus(app, True)
    _bump(state)


def _search(app, query):
    state = initialize(app)
    request_history(app)
    if not query:
        return
    state.search = query
    state.pending_search = True
    lines = _lines(app)
    state.search_matches = [i for i, line in enumerate(lines) if query.casefold() in line.casefold()]
    state.search_index = 0 if state.search_matches else -1
    if state.search_matches:
        state.top, state.follow = state.search_matches[0], False
    _say(app, f'Terminal search: {len(state.search_matches)} matches')
    _bump(state)


def command_names():
    return ['terminal']


def run_command(app, args):
    if not args or args[0] != 'terminal':
        return False
    state = initialize(app)
    action = args[1] if len(args) > 1 else 'show'
    rest = args[2:]
    if action in ('show', 'toggle', 'details'):
        desired = 'details' if action == 'details' or action == 'toggle' and showing(app) else 'terminal'
        state.modes[getattr(app, 'tab', 'jobs')] = desired
        state.page = getattr(app, 'tab', 'jobs')
        if desired == 'terminal':
            if not state.active:
                open_session(app)
            else:
                _focus(app, True)
        else:
            blur(app)
            from .workspace_layout import initialize as layout_state
            layout_state(app).focus = 'details'
        _bump(state)
    elif action == 'maximize':
        from .workspace_layout import run_command as workspace_command
        workspace_command(app,['maximize'])
    elif action == 'focus':
        state.modes[getattr(app, 'tab', 'jobs')] = 'terminal'
        _focus(app, True)
    elif action == 'blur':
        blur(app)
    elif action == 'new':
        open_session(app, label=' '.join(rest) or None)
    elif action in ('next', 'previous', 'session'):
        sessions = list(state.service.sessions) if state.service is not None else []
        if action == 'session':
            sid = rest[0] if rest else None
        else:
            index = sessions.index(state.active) if state.active in sessions else 0
            sid = sessions[(index + (1 if action == 'next' else -1)) % len(sessions)] if sessions else None
        if sid in sessions:
            _activate_session(app, sid)
            state.menu = False
    elif action == 'close':
        # Explicit session close is separate from switching back to Details.
        if state.active and state.service:
            sid = state.active
            state.service.close_session(sid)
            state.launches.pop(sid,None)
            state.dialog=state.preview=state.report=state.clipboard_preview=None
            state.menu=False
            _unregister_bridge(app,sid)
            state.active = None
            state.focus_generation += 1
            state.selected = None
            others = [item for item in state.service.sessions if item != sid]
            if others:
                _activate_session(app, others[-1])
            _bump(state)
    elif action == 'restart':
        spec=state.launches.get(state.active)
        if spec and (spec.get('kind') not in ('local','ssh','tmux','container')
                or spec.get('metadata',{}).get('destination')=='allocation'
                or os.path.basename((spec.get('argv') or [''])[0]) in ('srun','sattach','salloc','sbatch')):
            _say(app,'Use the Allocation action for a fresh review; this session remains open.',True)
        elif spec:
            run_command(app,['terminal','close'])
            open_session(app,**spec)
        else:
            _say(app,'Original session launch is unavailable',True)
    elif action == 'cancel-input':
        if state.service is not None and state.active:
            state.service.request(state.active,'cancel_input')
            _say(app,'Requested cancellation of queued terminal input; already delivered bytes cannot be retracted.')
    elif action == 'copy-mode':
        request_history(app)
        state.copy_mode = not state.copy_mode
        state.selection_source = _lines(app) if state.copy_mode else ()
        state.selection_wraps = _wraps(app) if state.copy_mode else ()
        state.selected = state.selection_anchor = state.selection_end = None
        state.focus_generation += 1
        _focus(app, True)
        _bump(state)
    elif action == 'copy':
        _copy(app)
    elif action == 'follow':
        state.follow = not state.follow
        _bump(state)
    elif action == 'scroll' and rest:
        _scroll(app, rest[0])
    elif action == 'search':
        if rest:
            _search(app, ' '.join(rest))
        else:
            _prompt(app, 'search', 'Search retained terminal text')
    elif action == 'search-next':
        if state.search_matches:
            state.search_index = (state.search_index + 1) % len(state.search_matches)
            state.top = state.search_matches[state.search_index]
            state.follow = False
            _bump(state)
    elif action == 'rename':
        if rest and state.active and state.service:
            method = getattr(state.service, 'rename', None)
            if callable(method):
                method(state.active, ' '.join(rest)[:64])
            else:
                state.service.request(state.active, 'rename', label=' '.join(rest)[:64])
        else:
            _prompt(app, 'rename', 'Session name')
    elif action in ('menu','sessions'):
        state.menu = not state.menu if action == 'menu' else True
        state.menu_top = state.menu_index = 0
        state.menu_items = [(str(value.get('label',sid)) + (' *' if sid in state.unread else ''), ['terminal','session',sid])
                            for sid,value in state.sessions.items()] if action == 'sessions' else None
        state.dialog = state.preview = state.report = None
        state.focus_generation += 1
        _focus(app, True)
        _bump(state)
    elif action == 'prompt':
        subcommand = ' '.join(rest) if rest else 'ssh'
        _prompt(app, subcommand, 'terminal ' + subcommand + ' …')
    elif action in ('child-clipboard','links'):
        snap=_snapshot(app)
        entries=snap.get('clipboard_requests' if action=='child-clipboard' else 'links',())
        if not rest:
            items=[(str(index+1)+': '+str(entry.get('text','') if action=='child-clipboard' else entry.get('url','')).replace('\n',' ')[:72],
                    ['terminal',action,'id:'+str(entry.get('id',index+1))]) for index,entry in enumerate(entries)]
            state.menu_items=items or [('No pending clipboard requests' if action=='child-clipboard' else 'No visible links',['terminal','cancel'])]
            state.menu=True; state.menu_top=state.menu_index=0
            state.focus_generation+=1; _focus(app,True); _bump(state)
        else:
            try:
                if rest[0].startswith('id:'):
                    entry=next(item for item in entries if str(item.get('id'))==rest[0][3:])
                else:
                    entry=entries[int(rest[0])-1]
                    if int(rest[0])<1: raise ValueError()
            except (ValueError,IndexError,StopIteration):
                _say(app,'Choose a valid terminal item number',True)
                return True
            text=str(entry.get('text','') if action=='child-clipboard' else entry.get('url',''))
            show_report(app,[text,'Use Copy below to copy this exact text. No request is accepted automatically.'],
                        'Child clipboard request' if action=='child-clipboard' else 'Link destination')
            state.clipboard_preview={'text':text,'session':state.active,'label':action}
    elif action == 'accept-clipboard':
        preview=state.clipboard_preview
        if preview and preview['session']==state.active:
            from . import clipboard
            _say(app,clipboard.copy(preview['text'],getattr(app,'state_dir',None),**clipboard.options(app)))
        state.clipboard_preview=None
        state.report=None
        state.focus_generation+=1; _bump(state)
    elif action == 'health':
        show_report(app,_health_lines(app), 'Terminal health')
        state.report['health_session']=state.active
        if state.service is not None and state.active:
            state.service.request(state.active,'diagnostics')
    elif action == 'insert':
        preview = state.preview
        allowed = _prepared_allowed(app, preview)
        if allowed and preview and preview.get('targets'):
            from .terminal_workflows import deliver_broadcast
            try:
                _say(app, deliver_broadcast(app, preview))
            except (ValueError, OSError, RuntimeError) as exc:
                _say(app, str(exc), True)
        elif allowed and preview and preview['session'] == state.active:
            # Confirmed insertion is a paste, deliberately without Enter.
            _send(app, {'type': 'paste', 'text': preview['text']})
        state.preview = None
        state.focus_generation += 1
        _bump(state)
    elif action == 'cancel':
        state.dialog = state.preview = state.report = state.clipboard_preview = None
        state.menu = False
        state.focus_generation += 1
        _bump(state)
    elif action == 'accept':
        dialog = state.dialog
        if dialog:
            state.dialog = None
            state.focus_generation += 1
            try:
                tail = shlex.split(dialog['value']) if dialog['action'] not in ('search', 'rename') else [dialog['value']]
                run_command(app, ['terminal', *shlex.split(dialog['action']), *tail])
            except ValueError as exc:
                _say(app, str(exc), True)
    else:
        for name in ('terminal_workflows', 'terminal_fusion'):
            try:
                from importlib import import_module
                module = import_module('.' + name, __package__)
            except ImportError:
                continue
            if module.run_command(app, args):
                return True
        _say(app, 'terminal show|details|new|next|close|copy-mode|copy|search|menu|health', True)
    return True


def _prepared_allowed(app, preview):
    """Generated helpers retain execution admission; typed paste is shell input."""
    if not preview or preview.get('helper_kind') == 'user-paste' or not getattr(app,'cfg',{}).get('raven'):
        return True
    try:
        from .raven_execution import helper_admission
        snapshot = _snapshot(app)
        metadata = dict(snapshot.get('metadata') or {})
        metadata.update(session_kind=snapshot.get('kind'), helper_kind=preview.get('helper_kind','generated'),
                        targets=preview.get('targets',()))
        decision = helper_admission(app,preview['text'],metadata)
        if decision:
            _say(app,str(decision.get('reason') or decision.get('message') or 'Generated helper deferred by the active execution policy.'),True)
            return False
    except (ImportError, ValueError, RuntimeError, OSError, TypeError) as exc:
        _say(app,'Generated helper unavailable: '+str(exc)[:256],True)
        return False
    return True


def handle_key(app, key):
    if not focused(app):
        return False
    state = initialize(app)
    if state.prefix:
        state.prefix = False
        if key == 't':
            blur(app)
        elif key in ('ctrl-]', '\x1d'):
            _send(app, {'type': 'key', 'key': 'ctrl-]'})
        else:
            _send(app, {'type': 'key', 'key': 'ctrl-]'})
            _send(app, {'type': 'key', 'key': key})
        return True
    if key in ('ctrl-]', '\x1d'):
        state.prefix = True
        return True
    if state.dialog:
        if key in ('esc', 'ctrl-c'):
            run_command(app, ['terminal', 'cancel'])
        elif key == 'enter':
            run_command(app, ['terminal', 'accept'])
        elif key in ('backspace', 'ctrl-h'):
            state.dialog['value'] = state.dialog['value'][:-1]
        elif isinstance(key, str) and (len(key) == 1 or key == 'space'):
            state.dialog['value'] += (' ' if key == 'space' else key)[:max(0, 4096-len(state.dialog['value']))]
        _bump(state)
        return True
    if state.preview or state.menu or state.report:
        if state.menu and key in ('up','down','home','end','pgup','pgdn','enter'):
            items=state.menu_items if state.menu_items is not None else _actions()
            page=max(1,state.body_rect.height if state.body_rect else 5)
            if key=='enter' and items:
                command=items[min(state.menu_index,len(items)-1)][1]
                state.menu=False
                run_command(app,list(command))
            else:
                state.menu_index=max(0,min(max(0,len(items)-1),
                    0 if key=='home' else len(items)-1 if key=='end' else
                    state.menu_index+{'up':-1,'down':1,'pgup':-page,'pgdn':page}.get(key,0)))
                if state.menu_index<state.menu_top: state.menu_top=state.menu_index
                if state.menu_index>=state.menu_top+page: state.menu_top=state.menu_index-page+1
            _bump(state)
            return True
        if key in ('esc', 'q', 'ctrl-c'):
            run_command(app, ['terminal', 'cancel'])
        elif state.preview and key == 'enter':
            run_command(app, ['terminal', 'insert'])
        elif state.preview and key in ('up','down','home','end','pgup','pgdn'):
            _preview_scroll(app,key)
        elif state.report and key in ('up', 'down', 'home', 'end', 'pgup', 'pgdn'):
            _scroll(app, key)
        return True
    if state.copy_mode:
        if key in ('esc', 'q'):
            run_command(app, ['terminal', 'copy-mode'])
        elif key == 'y':
            _copy(app)
        elif key == '/':
            _prompt(app, 'search', 'Search retained terminal text')
        elif key == 'n':
            run_command(app, ['terminal', 'search-next'])
        elif key in ('up', 'down', 'home', 'end', 'pgup', 'pgdn'):
            _scroll(app, key)
            if state.selection_anchor is not None:
                limit = max(0, len(state.selection_source)-1)
                current = state.selection_end if state.selection_end is not None else state.selection_anchor
                state.selection_end = (0 if key == 'home' else limit if key == 'end' else
                    max(0, min(limit, current + (-1 if key in ('up','pgup') else 1))))
                _pin_selection(state)
        elif key in ('v', 'space', 'enter'):
            if not state.selection_source:
                state.selection_source = _lines(app)
                state.selection_wraps = _wraps(app)
            state.selection_anchor = state.selection_end = min(state.top, max(0, len(state.selection_source)-1))
            _pin_selection(state)
        return True
    _send(app, {'type': 'key', 'key': key})
    return True


def handle_mouse(app, y, x, button='left', shift=False, alt=False, ctrl=False):
    if not enabled(app) or getattr(app, 'mode', 'main') != 'main':
        return False
    if type(y) is not int or type(x) is not int or not isinstance(button,str):
        return False
    state = initialize(app)
    if state.page != getattr(app, 'tab', '') or state.geometry != (getattr(app, 'width', None), getattr(app, 'height', None)):
        return False
    from .pointer_routing import active as pointer_active
    if not state.capture and pointer_active(app) and button not in ('left','press'):
        return False
    button = {'wheel_up':'wheel-up', 'wheel_down':'wheel-down', 'press':'left'}.get(button, button)
    if state.discard_release and button in ('release', 'left-release','right-release','middle-release'):
        state.discard_release = False
        return True
    for cy, left, right, args in state.controls:
        if y == cy and left <= x < right:
            if state.capture:
                break
            if button == 'left':
                run_command(app, list(args))
                state.discard_release = True
            return button not in ('move', 'hover', 'motion')
    rect = state.body_rect
    inside = bool(showing(app) and rect and rect.contains(y, x))
    if state.capture:
        sid, generation, kind = state.capture
        if sid != state.active or generation != state.focus_generation:
            state.capture = None
            return True
        if kind == 'rail':
            if button in ('drag','motion','move','left-drag','release','left-release'):
                _rail_seek(app,y)
                if button in ('release','left-release'): state.capture=None
            return True
        if kind == 'select':
            if button in ('drag', 'move', 'motion', 'left-drag','drag-right','drag-middle', 'release', 'left-release','right-release','middle-release'):
                row = max(0, min(rect.height-1, y-rect.y))
                state.selection_end = max(0, min(len(state.selection_source)-1, state.top+row))
                _pin_selection(state)
                _bump(state)
            if button in ('release', 'left-release','right-release','middle-release'):
                state.capture = None
            return True
        if button in ('drag','move','motion','left-drag','drag-right','drag-middle','release','left-release','right-release','middle-release'):
            _send(app, {'type':'mouse', 'x':max(0,min(rect.width-1,x-rect.x)),
                'y':max(0,min(rect.height-1,y-rect.y)), 'button':button,'shift':shift,'alt':alt,'ctrl':ctrl})
            if button in ('release','left-release','right-release','middle-release'):
                state.capture = None
            return True
    if state.rail_rect and state.rail_rect.contains(y,x) and showing(app):
        if button=='left':
            from .pointer_routing import begin_gesture
            begin_gesture(app)
            _focus(app,True)
            state.capture=(state.active,state.focus_generation,'rail')
            _rail_seek(app,y)
        elif button in ('wheel-up','wheel-down'):
            _scroll(app,'up' if button=='wheel-up' else 'down')
        return True
    if not inside:
        if button == 'left':
            blur(app)
        return False
    if button in ('move', 'hover', 'motion'):
        from .interaction import handle_mouse as interaction_mouse
        interaction_mouse(app,y,x,button='motion')
        # Hover never changes keyboard owner.
        if focused(app) and not state.copy_mode and not (state.dialog or state.preview or state.report or state.menu):
            _send(app, {'type':'mouse','x':x-rect.x,'y':y-rect.y,'button':'move','shift':shift,'alt':alt,'ctrl':ctrl})
        return True
    if button in ('wheel-up','wheel-down') and state.preview:
        _preview_scroll(app,'up' if button=='wheel-up' else 'down')
        return True
    if button in ('wheel-up','wheel-down') and state.menu:
        handle_key(app,'up' if button=='wheel-up' else 'down')
        return True
    if button in ('wheel-up', 'wheel-down') and (state.copy_mode or state.report or not _snapshot(app).get('modes', {}).get('mouse')):
        _scroll(app, 'up' if button == 'wheel-up' else 'down')
        return True
    if button == 'right' and state.copy_mode:
        state.selected = state.selection_anchor = state.selection_end = None
        state.capture = None
        _bump(state)
        return True
    if button in ('left','middle','right'):
        from .pointer_routing import begin_gesture
        begin_gesture(app)
        _focus(app, True)
        if state.dialog or state.preview or state.menu or state.report:
            return True
        if state.copy_mode or shift:
            state.copy_mode = True
            state.selection_source = _lines(app)
            state.selection_wraps = _wraps(app)
            page = max(1, rect.height)
            if state.follow:
                state.top = max(0, len(state.selection_source)-page)
            state.follow = False
            row = min(max(0,len(state.selection_source)-1),state.top+y-rect.y)
            state.selection_anchor = state.selection_end = row
            _pin_selection(state)
            state.capture = (state.active, state.focus_generation, 'select')
            _bump(state)
            return True
        state.capture = (state.active, state.focus_generation, 'child')
    if not (state.dialog or state.preview or state.menu or state.report or state.copy_mode):
        _send(app, {'type':'mouse','x':x-rect.x,'y':y-rect.y,'button':button,'shift':shift,'alt':alt,'ctrl':ctrl})
    return True


def tick(app):
    state = getattr(app, 'terminal_state', None)
    if not isinstance(state, TerminalState) or state.service is None:
        return False
    try:
        changed = bool(state.service.poll())
        for sid in tuple(state.service.sessions):
            visible_now = sid == state.active and visible(app)
            if state.visibility.get(sid) != visible_now:
                state.service.request(sid, 'visible', visible=visible_now)
                state.visibility[sid] = visible_now
        try:
            from .terminal_fusion import tick as fusion_tick
            changed = bool(fusion_tick(app)) or changed
        except ImportError:
            pass
        for sid in tuple(state.service.sessions):
            snapshot = state.service.snapshot(sid)
            results = snapshot.get('results') or (snapshot.get('result', {}),)
            seen = state.history_seen.setdefault(sid, set())
            for result in results:
                request_id = result.get('request_id')
                if result.get('action') != 'history' or request_id in seen:
                    continue
                seen.add(request_id)
                if result.get('error'):
                    state.history_loading[sid] = False
                    state.history_requested.pop(sid,None)
                    state.error = 'Scrollback: ' + str(result['error'])[:500]
                    continue
                if len(seen)>64:
                    seen.intersection_update(sorted(seen)[-32:])
                payload = result.get('result', {})
                start = int(payload.get('history_start', snapshot.get('history_start', 0)))
                offset = max(0,int(payload.get('offset',0)))
                token=payload.get('snapshot_id')
                if offset == 0:
                    state.history_pages[sid]={}
                    state.history_wrap_pages[sid]={}
                    state.history_snapshot[sid]=token
                    total=max(0,min(10000,int(payload.get('total',0))))
                    for next_offset in range(1000,total,1000):
                        state.service.request(sid,'history',offset=next_offset,limit=min(1000,total-next_offset),snapshot_id=token)
                elif token != state.history_snapshot.get(sid):
                    continue
                pages = state.history_pages.setdefault(sid,{})
                wrap_pages=state.history_wrap_pages.setdefault(sid,{})
                wrapped=payload.get('wrapped',())
                for index,line in enumerate(payload.get('lines', ())):
                    pages[start+offset+index] = str(line)
                    wrap_pages[start+offset+index] = bool(wrapped[index]) if index<len(wrapped) else False
                lower=start
                for index in tuple(pages):
                    if index<lower or index>=lower+10000:
                        pages.pop(index,None)
                        wrap_pages.pop(index,None)
                state.history[sid] = tuple(pages[index] for index in sorted(pages))
                state.history_wraps[sid] = tuple(wrap_pages.get(index,False) for index in sorted(pages))
                state.history_results[sid] = request_id
                if len(pages)>=int(payload.get('total',0)):
                    state.history_loading[sid] = False
                if sid == state.active and state.copy_mode and state.selected is None:
                    state.selection_source = _lines(app)
                    state.selection_wraps = _wraps(app)
                if sid == state.active and state.pending_search and state.search:
                    values=_lines(app)
                    state.search_matches=[i for i,line in enumerate(values) if state.search.casefold() in line.casefold()]
                    state.search_index=0 if state.search_matches else -1
                    if state.search_matches:
                        state.top,state.follow=state.search_matches[0],False
                    if len(pages)>=int(payload.get('total',0)):
                        state.pending_search=False
                changed = True
            seq = snapshot.get('seq', 0)
            if seq != state.sequences.get(sid):
                state.sequences[sid] = seq
                state.snapshots[sid] = snapshot
                if sid != state.active or not showing(app):
                    state.unread[sid] = state.unread.get(sid, 0) + 1
                changed = True
        if state.report and state.report.get('health_session')==state.active:
            lines=tuple(_health_lines(app))
            if state.report['lines']!=lines:
                state.report['lines']=lines
                changed=True
        if changed:
            _bump(state)
        return changed
    except (OSError, ValueError, RuntimeError) as exc:
        message = str(exc)[:1000]
        changed = message != state.error
        state.error = message
        return changed


def shutdown(app):
    try:
        from .terminal_fusion import close
        close(app)
    except ImportError:
        pass
    state = getattr(app, 'terminal_state', None)
    if isinstance(state, TerminalState) and state.service is not None:
        state.service.shutdown()
        state.service = None
        state.keyboard_focus = False
        state.focus_generation += 1


def _plain_chrome(app):
    """Follow the actual renderer/Reader mode, including real App settings."""
    return bool(getattr(app,'ascii',False) or getattr(app,'_ascii_cfg',False)
                or getattr(app,'theme',None)=='reader'
                or getattr(getattr(getattr(app,'views_ref',None),'g',None),'ascii',False))


def _control(app, row, x, label, args, width, *, active=False):
    state = initialize(app)
    if _plain_chrome(app):
        label = label.translate(str.maketrans({'‹':'<','›':'>','↑':'^','↓':'v','·':'.','▏':'|','…':'...'}))
    text = '[' + label + ']'
    text = L.clip_row([(text, 'cyan+bold' if active else 'heading')], max(0, width))
    cells = L.vlen(L.row_text(text))
    if cells:
        state.controls.append((row, x, x+cells, tuple(args)))
    return text, cells


def header(app, width, *, x=0, y=0):
    """Paint the whole host title, including its navigation-focus gutter.

    Four blank cells belong to optional scroll-to-end controls.
    Never recover that space by slicing a previous title: a short Details
    document has no scroll gutter and its fourth cell is the letter ``D``.
    """
    state = initialize(app)
    width = max(0, width)
    focused_pane = getattr(getattr(app, 'layout_state', None), 'focus', 'main') == 'details'
    gutter = 4 if width >= 6 else 0
    marker = ('>' if _plain_chrome(app) else '▸') if focused_pane else ' '
    prefix = ' ' * gutter + (' ' + marker + ' ' if width >= 27 else '')
    style = 'accent+bold+bg:surface-raised' if focused_pane else 'secondary+bg:surface'
    row, hits, column = ([(prefix, style)] if prefix else []), [], len(prefix)
    for label, action in (('Details','details'),('Terminal','show')):
        segment, count = _control(app, y, x+column, label, ['terminal', action], max(0,width-column),
                                  active=(action == 'show') == showing(app))
        row.extend(segment)
        if count:
            hits.append((0,'control',{'id':'terminal:'+action,'label':label,'left':column,'right':column+count,
                'action':('command','terminal '+action)}))
        column += count
        if column < width:
            row.append((' ', '')); column += 1
    return L.fill_row(row, width), hits


def _actions():
    result = [('Sessions', ['terminal','sessions']), ('New shell', ['terminal','new']), ('Rename', ['terminal','rename']),
              ('Search', ['terminal','search']), ('Health', ['terminal','health']),
              ('SSH host', ['terminal','prompt','ssh']), ('Allocation', ['terminal','prompt','job']),
              ('Persistent tmux', ['terminal','prompt','tmux']), ('Container', ['terminal','prompt','container']),
              ('Armed broadcast', ['terminal','prompt','broadcast']),
              ('Restart original session', ['terminal','restart']),
              ('Cancel queued paste', ['terminal','cancel-input']),
              ('Many SSH hosts', ['terminal','prompt','ssh-many']),
              ('Directory', ['terminal','prompt','cwd']), ('Job directory', ['terminal','prompt','job-cwd']),
              ('Reviewed sequence', ['terminal','prompt','sequence']),
              ('Prepare diagnostic', ['terminal','prompt','prepare']), ('Recipes', ['terminal','prompt','recipe']),
              ('Child clipboard', ['terminal','child-clipboard']), ('Links', ['terminal','links']),
              ('Maximize / restore', ['terminal','maximize']),
              ('Copy selection', ['terminal','copy']), ('Follow output', ['terminal','follow']),
              ('Top', ['terminal','scroll','home']), ('Bottom', ['terminal','scroll','end']),
              ('Close session', ['terminal','close'])]
    try:
        from .terminal_fusion import actions
        result.extend(actions())
    except ImportError:
        pass
    return result


def _render(app, width, height, x, y):
    state = initialize(app)
    rows, hits = [], []
    head, controls = header(app, width, x=x, y=y)
    rows.append(head); hits.extend(controls)
    if height <= 1:
        state.body_rect = None
        return rows[:height], hits
    snap = _snapshot(app)
    label = str(snap.get('label', 'No session'))[:128]
    destination = str((snap.get('metadata') or {}).get('host') or snap.get('host') or socket.gethostname())
    status = str(snap.get('status', snap.get('state', 'not started')))
    session_count = len(state.service.sessions) if state.service is not None else 0
    rows.append(L.fill_row([(f' {destination} · {label} · {status} · {session_count} session(s)', 'dim')],width))
    toolbar = [('‹','previous'),('›','next'),('+','new'),('Tools','menu'),('Copy' if not state.copy_mode else 'Copy*','copy-mode'),
               ('Yank','copy'),('Follow*' if state.follow else 'Follow','follow'),('↑','scroll home'),('↓','scroll end')]
    if height > 2:
        line, column = [], 0
        for label, action in toolbar:
            text, count = _control(app,y+2,x+column,label,['terminal',*action.split()],max(0,width-column))
            if not count: break
            line.extend(text)
            hits.append((2,'control',{'id':'terminal:'+action,'label':label,'left':column,'right':column+count,
                                     'action':('command','terminal '+action)}))
            column += count
            if column < width: line.append((' ','')); column += 1
        rows.append(L.fill_row(line,width))
    body_y = len(rows)
    body_height = max(0,height-body_y-1)
    content_width=max(1,width-1) if width>1 else width
    next_body = Rect(x,y+body_y,content_width,body_height)
    state.rail_rect=Rect(x+width-1,y+body_y,1,body_height) if width>1 and body_height else None
    if state.body_rect is not None and state.body_rect != next_body and state.capture:
        state.capture = None
        state.discard_release = True
    state.body_rect = next_body
    if state.service is not None and state.active and body_height and width:
        size = (body_height,content_width)
        if state.sizes.get(state.active) != size:
            try:
                state.service.resize(state.active,*size)
                state.sizes[state.active] = size
            except (OSError,ValueError,RuntimeError):
                pass
    content = []
    if state.dialog:
        content = [[(state.dialog['label'],'heading+bold')],[(state.dialog['value']+'▏','text')],
                   [(' Enter accepts · Esc cancels','dim')]]
    elif state.preview:
        pasted=state.preview['text']
        warning=(' Included newlines can execute commands.' if '\n' in pasted or '\r' in pasted else ' Insert adds no Enter key.')
        content = [[(state.preview['label'],'heading+bold')],
                   [(' Confirm the intended shell or application is ready.','yellow')],
                   [(warning,'yellow')]]
        if state.preview.get('targets'):
            content += [[(' Armed: '+', '.join(str(item.get('label',item.get('id',''))) if isinstance(item,dict) else str(item)
                for item in state.preview['targets']), 'yellow+bold')]]
        preview_lines=_preview_lines(app,content_width)
        available=max(0,body_height-len(content))
        top=max(0,min(max(0,len(preview_lines)-available),state.preview['top']))
        state.preview['top']=top
        content += [[(line,'text')] for line in preview_lines[top:top+available]]
    elif state.report:
        content=[[(state.report['title'],'heading+bold')]]
        content += _report_window(app,state.report['top'],max(0,body_height-1),content_width)
    elif state.menu:
        menu_items=state.menu_items if state.menu_items is not None else _actions()
        for index,(label,args) in enumerate(menu_items[state.menu_top:state.menu_top+body_height]):
            text,count=_control(app,y+body_y+index,x,label,args,width,active=state.menu_top+index==state.menu_index)
            content.append(text)
            hits.append((body_y+index,'control',{'id':'terminal:tool:'+str(index),'label':label,'left':0,'right':count,
                'action':('command',shlex.join(args))}))
    elif state.error and not snap:
        content = [[(' Terminal unavailable: '+state.error,'red')], [(' Install Raven with its terminal extra, then retry [+].','dim')]]
    elif not snap:
        content = [[(' Select [+] to start an interactive local shell.','dim')]]
    elif state.copy_mode or not state.follow:
        values = state.selection_source if state.copy_mode and state.selection_source else _lines(app)
        top = max(0,min(max(0,len(values)-body_height),state.top))
        state.top=top
        a,b=sorted((state.selection_anchor,state.selection_end)) if state.selection_anchor is not None and state.selection_end is not None else (-1,-1)
        content = [[(line,'sel' if a<=top+index<=b else 'text')] for index,line in enumerate(values[top:top+body_height])]
    else:
        content = [[(str(text),str(style)) for text,style in row] for row in snap.get('lines',())[:body_height]]
        cursor=snap.get('cursor',{})
        shape=int(cursor.get('shape',0))
        blink=shape in (0,1,3,5) and getattr(app,'animations_enabled',True)
        if focused(app) and cursor.get('visible',True) and (not blink or int(time.monotonic()*2)%2==0):
            cx,cy=cursor.get('x',-1),cursor.get('y',-1)
            if type(cx) is int and type(cy) is int and 0<=cx<content_width and 0<=cy<len(content):
                # Apply cursor styling in cell coordinates without replacing
                # the character (wide/combining characters stay intact).
                content[cy] = _cursor_row(content[cy],cx,shape)
    rail=_rail_rows(app,body_height) if state.rail_rect else []
    for index in range(body_height):
        row=content[index] if index<len(content) else []
        rows.append(L.fill_row(row,content_width)+(rail[index] if rail else []))
    rows.extend(L.fill_row([],width) for _ in range(max(0,height-1-len(rows))))
    if len(rows)<height:
        hint = (' Copy mode · v select · arrows extend · y copy · Esc shell' if state.copy_mode else
                ' Esc closes panel · ↑↓ scroll' if state.report or state.menu else
                ' Terminal focus · Ctrl-] t: Raven' if focused(app) else ' Click terminal to type · Ctrl-] t: Raven')
        if state.dialog or state.preview or state.menu or state.report:
            action='accept' if state.dialog else 'insert' if state.preview else 'accept-clipboard' if state.clipboard_preview else 'cancel'
            label='Accept' if state.dialog else 'Insert' if state.preview else 'Copy' if state.clipboard_preview else 'Close'
            part,count=_control(app,y+height-1,x,label,['terminal',action],width)
            hits.append((height-1,'control',{'id':'terminal:footer:'+action,'label':label,'left':0,'right':count,'action':('command','terminal '+action)}))
            if action!='cancel' and width>count+1:
                part += [(' ','')]
                other,more=_control(app,y+height-1,x+count+1,'Cancel',['terminal','cancel'],width-count-1)
                part += other
                hits.append((height-1,'control',{'id':'terminal:footer:cancel','label':'Cancel','left':count+1,'right':count+1+more,'action':('command','terminal cancel')}))
            rows.append(L.fill_row(part,width))
        else:
            rows.append(L.fill_row([(hint,'cyan' if focused(app) else 'dim')],width))
    if _plain_chrome(app):
        translation=str.maketrans({'‹':'<','›':'>','↑':'^','↓':'v','·':'.','▏':'|','…':'...','×':'x'})
        chrome=set(range(body_y))|{height-1}
        if state.dialog or state.preview or state.report or state.menu: chrome.update(range(height))
        rows=[L.clip_row([(text.translate(translation),style) for text,style in row],width) if index in chrome else row for index,row in enumerate(rows)]
    return rows[:height],[(yy,kind,val) for yy,kind,val in hits if yy<height and val.get('left',0)<val.get('right',0)]


def _cursor_row(row,column,shape=0):
    result=[]; pos=0
    for text,style in row:
        for char in text:
            size=L.vlen(char)
            result.append((char,style+('+under' if shape in (3,4,5,6) else '+rev') if pos<=column<pos+max(1,size) else style))
            pos+=size
    if pos<=column:
        result.extend([(' '*(column-pos),''),(' ','rev')])
    return result


def render(app,width,height,*,x=0,y=0):
    state=initialize(app)
    state.rect=Rect(x,y,max(0,width),max(0,height))
    state.geometry=(getattr(app,'width',None),getattr(app,'height',None))
    state.controls=[]
    rows,hits=_render(app,max(0,width),max(0,height),x,y)
    if state.body_rect is None or state.body_rect.width<=0 or state.body_rect.height<=0:
        blur(app)
    state.painted=rows
    state.painted_key=_paint_key(app)
    return rows,hits


def feedback(app,rows=None):
    state=getattr(app,'terminal_state',None)
    if not isinstance(state,TerminalState):
        return rows if rows is not None else None
    if rows is None:
        return (state.generation,state.active,state.focus_generation,state.rect,state.keyboard_focus)
    if not showing(app) or state.rect is None or getattr(app,'mode','main')!='main':
        return rows
    rect=state.rect
    if state.geometry!=(getattr(app,'width',None),getattr(app,'height',None)):
        return rows
    key=_paint_key(app)
    if state.painted_key != key:
        state.controls=[]
        terminal,_=_render(app,rect.width,rect.height,rect.x,rect.y)
        state.painted=terminal
        state.painted_key=_paint_key(app)
    else:
        terminal=state.painted
    from .interaction import decorate
    terminal=decorate(app,terminal,origin=(rect.y,rect.x))
    rows = list(rows)
    for index,line in enumerate(terminal):
        target=rect.y+index
        if not 0<=target<len(rows): continue
        # Preserve outside panes exactly, including their style segments.
        rows[target]=_slice_row(rows[target],0,rect.x)+line+_slice_row(rows[target],rect.x+rect.width,10**8)
    return rows


def _slice_row(row,left,right):
    result=[]; col=0
    for text,style in row:
        out=''
        for char in text:
            size=L.vlen(char)
            if left<=col and col+size<=right:
                out+=char
            col+=size
        if out: result.append((out,style))
    return result


def visible(app):
    return showing(app) and initialize(app).rect is not None and getattr(app,'mode','main')=='main'


def overlay(views, snap, app, width, height):
    return None


def cancel_prepared(app):
    state=initialize(app)
    state.preview=None
    state.focus_generation+=1
    _bump(state)


def save(app):
    return {'version':1, 'modes':dict(initialize(app).modes)}


def restore(app,data):
    if isinstance(data,dict) and data.get('version')==1 and isinstance(data.get('modes'),dict):
        initialize(app).modes={key:value for key,value in data['modes'].items() if key in PAGES and value in ('details','terminal')}


def wrap_workspace(app, width, height, renderer, *, ascii_=False):
    """Host native documents and a terminal without replacing their state."""
    from . import chart_interaction, scrollbars
    from .workspace_layout import geometry, _content_origin, _main_geometry, _divider
    if height is None or height<=0 or width<=0:
        return renderer(width,height)
    origin_x,origin_y=_content_origin(app,width,height)
    state=initialize(app)
    state.geometry=(getattr(app,'width',None),getattr(app,'height',None))
    if not showing(app):
        # The host bar is composed with the global header, before body_origin
        # is established. Native documents publish absolute origins while they
        # render; adding a row afterwards would desynchronize those registries.
        # Native pages must not inherit Jobs' previous split rectangles. Their
        # row-selection guard reads these bounds before its Shift-range route.
        app.job_panel_rect = None
        app.workspace_main_rect = Rect(origin_x, origin_y, width, height)
        app.workspace_main_usable_height = height
        return renderer(width,height)
    rects=geometry(app,width,height,has_details=True)
    _main_geometry(app,rects)
    state_layout=getattr(app,'layout_state',None)
    if state_layout is not None: state_layout.available=('main','details')
    canvas=[L.fill_row([],width) for _ in range(height)]
    hits=[]
    main=rects.get('main')
    if main:
        mark=chart_interaction.mark(app); scroll_mark=scrollbars.mark(app)
        rows,source_hits=renderer(main.width,main.height)
        chart_interaction.place_since(app,mark,dy=main.y,dx=main.x,
            clip=(main.y,main.x,main.y+main.height,main.x+main.width))
        scrollbars.place_since(app,scroll_mark,dy=main.y,dx=main.x,
            clip=(main.y,main.x,main.y+main.height,main.x+main.width))
        for yy,row in enumerate(rows[:main.height]):
            canvas[main.y+yy]=_replace(canvas[main.y+yy],main.x,main.width,L.fill_row(row,main.width))
        for yy,kind,value in source_hits:
            if not 0<=yy<main.height: continue
            if isinstance(value,dict) and 'left' in value and 'right' in value:
                value={**value,'left':value['left']+main.x,'right':min(main.width,value['right'])+main.x}
            elif kind=='sort_header' and isinstance(value,tuple) and len(value)==4:
                value=(*value[:2],value[2]+main.x,min(main.width,value[3])+main.x)
            elif kind in ('node_cell','job_panel_tab','job_panel_view','job_panel_file','job_panel_action') and isinstance(value,tuple) and len(value)==3:
                value=(value[0],value[1]+main.x,min(main.width,value[2])+main.x)
            hits.append((yy+main.y,kind,value))
    detail=rects.get('details')
    if detail:
        terminal,terminal_hits=render(app,detail.width,detail.height,x=origin_x+detail.x,y=origin_y+detail.y)
        for yy,row in enumerate(terminal):
            canvas[detail.y+yy]=_replace(canvas[detail.y+yy],detail.x,detail.width,row)
        hits.extend((yy+detail.y,kind,{**value,'left':value['left']+detail.x,'right':value['right']+detail.x}) for yy,kind,value in terminal_hits)
    divider=_divider(app,rects,width,height)
    if divider:
        from . import pane_drag
        pane_drag.paint(canvas,app,divider.key,origin_x=origin_x,origin_y=origin_y,ascii_=ascii_)
        hit=pane_drag.control_hit(app,divider.key,origin_y=origin_y)
        if hit:
            yy,kind,value=hit
            hits.append((yy,kind,{**value,'left':value['left']-origin_x,'right':value['right']-origin_x}))
    return canvas,hits


def _replace(row,x,width,content):
    return _slice_row(row,0,x)+content+_slice_row(row,x+width,10**8)


def request_history(app):
    state=initialize(app)
    if state.service is not None and state.active:
        snap=_snapshot(app)
        count=max(0,min(10000,int(snap.get('history_count',0))))
        start=int(snap.get('history_start',0))
        previous=state.history_requested.get(state.active)
        if state.history_loading.get(state.active):
            return
        now=time.monotonic()
        if previous and (previous[:2]==(start,count) or now-previous[2]<0.2):
            return
        request=getattr(state.service,'request',None)
        if callable(request):
            if count:
                state.history_loading[state.active]=request(state.active,'history',offset=0,limit=min(1000,count)) is not False
            state.history_requested[state.active]=(start,count,now)


def _paint_key(app):
    state=initialize(app)
    snap=_snapshot(app)
    cursor=snap.get('cursor',{})
    blinking=(focused(app) and cursor.get('visible',True) and cursor.get('shape',0) in (0,1,3,5)
              and getattr(app,'animations_enabled',True) and not state.copy_mode and state.follow
              and not (state.report or state.dialog or state.preview or state.menu))
    return (state.generation,state.focus_generation,state.rect,state.active,
            getattr(getattr(app,'layout_state',None),'focus',None),
            int(time.monotonic()*2)%2 if blinking else 0, _plain_chrome(app))


def finalize_frame(app):
    state=getattr(app,'terminal_state',None)
    if not isinstance(state,TerminalState):
        return
    if (not showing(app) or getattr(app,'mode','main') != 'main' or state.rect is None or state.body_rect is None
            or state.body_rect.width<=0 or state.body_rect.height<=0):
        state.body_rect=state.rail_rect=None
        if state.keyboard_focus:
            blur(app)


def _wraps(app):
    state=initialize(app)
    snap=_snapshot(app)
    return tuple(state.history_wraps.get(state.active, snap.get('history_wrapped',()))) + tuple(snap.get('line_wrapped',()))


def _unregister_bridge(app,sid):
    try:
        from .terminal_fusion import unregister_session
        unregister_session(app,sid)
        return
    except ImportError:
        pass
    state=getattr(app,'terminal_fusion_state',{})
    bridge=state.get('bridge') if isinstance(state,dict) else None
    if bridge is not None:
        bridge.unregister(sid)


def _rail_rows(app,page):
    state=initialize(app)
    values=(state.menu_items if state.menu_items is not None else _actions()) if state.menu else state.report['lines'] if state.report else state.selection_source if state.copy_mode and state.selection_source else _lines(app)
    total=_report_document(app)['total'] if state.report else len(values)
    top=state.menu_top if state.menu else state.report['top'] if state.report else max(0,total-page) if state.follow else state.top
    size=max(1,min(page,round(page*page/max(1,total))))
    start=round((page-size)*min(max(0,total-page),max(0,top))/max(1,total-page))
    return [[(('|' if _plain_chrome(app) else '▐') if start<=index<start+size else ('|' if _plain_chrome(app) else '│'),
              'cyan' if start<=index<start+size else 'dim')] for index in range(page)]


def _rail_seek(app,y):
    state=initialize(app)
    rail=state.rail_rect
    if rail is None:
        return
    if state.menu:
        items=state.menu_items if state.menu_items is not None else _actions()
        state.menu_top=round(max(0,len(items)-rail.height)*max(0,min(rail.height-1,y-rail.y))/max(1,rail.height-1))
        state.menu_index=state.menu_top
    else:
        request_history(app)
        values=state.report['lines'] if state.report else state.selection_source if state.copy_mode and state.selection_source else _lines(app)
        page=rail.height-1 if state.report else rail.height
        total=_report_document(app)['total'] if state.report else len(values)
        target=round(max(0,total-page)*max(0,min(rail.height-1,y-rail.y))/max(1,rail.height-1))
        if state.report: state.report['top']=target
        else: state.top,state.follow=target,False
    _bump(state)


def _health_lines(app):
    state=initialize(app)
    snap=_snapshot(app)
    diagnostic=next((item.get('result',{}) for item in reversed(snap.get('results') or (snap.get('result',{}),))
                     if item.get('action')=='diagnostics' and not item.get('error')), {})
    result=['Session: '+str(state.active or 'none'),
            'State: '+str(snap.get('status',snap.get('state','not started'))),
            'Destination: '+str((snap.get('metadata') or {}).get('host') or snap.get('host') or socket.gethostname()),
            'Label: '+str(snap.get('label','none')),
            'Screen sequence / epoch: '+str(snap.get('seq',0))+' / '+str(snap.get('epoch',0)),
            'Viewport: '+str(snap.get('rows','?'))+' x '+str(snap.get('cols','?')),
            'Retained history: '+str(snap.get('history_count',len(snap.get('history',()))))+' lines',
            'History eviction boundary: '+str(snap.get('history_start',0)),
            'PTY input pending: '+str(snap.get('input_pending',0))+' bytes',
            'PTY input delivered: '+str(snap.get('input_bytes',diagnostic.get('delivered_bytes',0)))+' bytes',
            'PTY output read: '+str(snap.get('output_bytes',0))+' bytes',
            'Parser cumulative time: '+str(round(float(snap.get('parse_seconds',0))*1000,3))+' ms',
            'Unsupported controls: '+str(snap.get('unsupported',0)),
            'Engine: '+str(snap.get('engine','not started')),
            'Child PID: '+str(snap.get('pid','none')),
            'Modes: '+str(snap.get('modes',{})),
            'Recording: '+str(snap.get('record_state','off')),
            'Terminal I/O is independent of Slurm polling and worker mode.',
            'Closing terminates the owned shell; hiding preserves it.',
            'Error: '+(state.error or str(snap.get('error') or 'none'))]
    result.extend(str(key)+': '+str(value) for key,value in diagnostic.items())
    try:
        from .terminal_workflows import attachment_status
        status=attachment_status(app,snap.get('metadata') or {})
        if status: result.append('Allocation: '+str(status))
    except (ImportError,AttributeError,TypeError):
        pass
    return result


def _preview_lines(app,width=None):
    state=initialize(app)
    if not state.preview:
        return ()
    width=max(1,width or (state.body_rect.width if state.body_rect else 40))
    cached=state.preview.get('lines_cache')
    if cached and cached[0]==width:
        return cached[1]
    from .log_text import display_text
    values=[]
    # The preview is a rendered observation; the original payload is retained
    # unchanged for insertion. Its character and line limits are independent.
    for raw in state.preview['text'].splitlines() or ['']:
        clean=display_text(raw)
        for start in range(0,max(1,len(clean)),width):
            values.append(clean[start:start+width])
            if len(values)>=10000:
                values.append('[Preview limit: 10,000 rows. Cancel or disable paste_preview for a trusted larger payload.]')
                break
        if len(values)>10000:
            break
    state.preview['lines_cache']=(width,tuple(values))
    return tuple(values)


def _preview_scroll(app,action):
    state=initialize(app)
    if not state.preview:
        return
    page=max(1,(state.body_rect.height if state.body_rect else 8)-3-bool(state.preview.get('targets')))
    limit=max(0,len(_preview_lines(app))-page)
    top=state.preview.get('top',0)
    state.preview['top']=max(0,min(limit,0 if action=='home' else limit if action=='end' else
        top+{'up':-1,'down':1,'pgup':-page,'pgdn':page}.get(action,0)))
    _bump(state)


def _report_document(app,width=None):
    state=initialize(app)
    if not state.report:
        return {'total':0,'starts':(), 'counts':(), 'width':1}
    width=max(1,width or (state.body_rect.width if state.body_rect else 40))
    source=state.report['lines']
    cached=state.report.get('document')
    if cached and cached['width']==width and cached['source'] is source:
        return cached
    from .workspace_layout import _wrap_row
    from .log_text import display_text
    clean = cached['clean'] if cached and cached['source'] is source else tuple(display_text(text) for text in source)
    starts=[]; counts=[]; total=0
    for text in clean:
        starts.append(total)
        count=max(1,(len(text)+width-1)//width) if text.isascii() else max(1,len(_wrap_row([(text,'text')],width)))
        counts.append(count); total+=count
    cached={'total':total,'starts':tuple(starts),'counts':tuple(counts),'width':width,'source':source,'clean':clean}
    state.report['document']=cached
    return cached


def _report_window(app,top,page,width):
    state=initialize(app)
    document=_report_document(app,width)
    if not document['starts'] or page<=0:
        return []
    from .workspace_layout import _wrap_row
    from .log_text import display_text
    result=[]
    index=max(0,bisect_right(document['starts'],top)-1)
    while index<len(document['source']) and len(result)<page:
        text=document['clean'][index]
        offset=max(0,top-document['starts'][index])
        if text.isascii():
            count=document['counts'][index]
            result.extend([[(text[position*width:(position+1)*width],'text')]
                for position in range(offset,min(count,offset+page-len(result)))])
        else:
            rows=_wrap_row([(text,'text')],width) or [[]]
            result.extend(rows[offset:offset+page-len(result)])
        index+=1
    return result
