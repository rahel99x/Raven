"""Allocation lifecycle controls over immutable manager publications.

Painting, hover, and keyboard navigation never contact the scheduler. Only a
reviewed action calls the manager; its helper owns allocation I/O and lifetime.
"""
from __future__ import annotations

import copy
import re
import time

from . import layout as L
from .control_rows import buttons, place_hits
from .log_text import display_text

ACTIVE = frozenset(('preparing', 'pending', 'starting', 'heavy', 'draining', 'reconciling'))
STATES = frozenset(('light', *ACTIVE))


def initialize(app):
    state = getattr(app, 'raven_ui_state', None)
    if not isinstance(state, dict):
        state = dict(snapshot={'state': 'light', 'mode': 'light'}, revision=0,
                     review=None, cursor=0, top=0, page=1, return_mode='main',
                     control_hits=[], painted=None, message='', manager=None,
                     reuse_edit=None,reuse_pending=None)
        app.raven_ui_state = state
    return state


def bind(app, manager):
    app.raven_allocation_manager = manager
    state = initialize(app)
    state.update(manager=manager, review=None, control_hits=[], painted=None)
    _collect(app)
    return status(app)


def _collect(app):
    state = initialize(app)
    manager = getattr(app, 'raven_allocation_manager', None)
    if manager is None:
        return False
    try:
        value = manager.snapshot()
        if not isinstance(value, dict) or value.get('state') not in STATES:
            raise ValueError('invalid allocation publication')
        startup = getattr(manager, 'background_startup', None)
        if startup is not None:
            value = startup.publication(value)
        value = copy.deepcopy(value)
        # A pending/reconciling allocation is never advertised as ready.
        value['mode'] = 'heavy' if value['state'] == 'heavy' and not value.get('cleanup_uncertain') else 'light'
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        value = {**state['snapshot'], 'state': 'reconciling', 'mode': 'light',
                 'cleanup_uncertain': True, 'error': str(exc)[:512]}
    changed = value != state['snapshot']
    if changed:
        old_frame = _frame(app)
        state['snapshot'] = value
        state['revision'] += 1
        if old_frame != _frame(app):
            state['control_hits'] = []
            state['painted'] = None
        from .telemetry_settings import sync as sync_telemetry
        sync_telemetry(app)
    pending=state.get('reuse_pending')
    if pending:
        result=value.get('reuse_review',{})
        if getattr(app,'mode','')!='raven' or pending['tab']!=getattr(app,'tab',''):
            state['reuse_pending']=None
        elif (result.get('job_id')==pending['job_id'] and result.get('generation')==pending['generation']
              and result.get('token')==pending.get('token')):
            if result.get('state')=='ready':
                state['review']={'action':'reuse','identity':_identity(app),'plan':copy.deepcopy(result['plan']),
                    'snapshot':copy.deepcopy(value),'tab':pending['tab'],'reuse_token':result['token'],
                    'reuse_job':pending['job_id'],'expires_at':result.get('expires_at')}
                state.update(reuse_pending=None,cursor=0,top=0,painted=None,control_hits=[])
                changed=True
            elif result.get('state')=='error':
                state.update(reuse_pending=None,message=_text(result.get('error') or 'Allocation reuse verification failed.',512))
                changed=True
    return changed


def tick(app):
    manager = getattr(app, 'raven_allocation_manager', None)
    if manager is None:
        return False
    try:
        manager.tick()
        startup = getattr(manager, 'background_startup', None)
        if startup is not None:
            startup.tick()
    except (OSError, RuntimeError, ValueError) as exc:
        state = initialize(app)
        state['message'] = 'Allocation connection: ' + str(exc)[:512]
    return _collect(app)


def shutdown(app):
    manager = getattr(app, 'raven_allocation_manager', None)
    if manager is not None:
        from .raven_launch import shutdown_heavy
        shutdown_heavy(manager)


def status(app):
    """Read cached UI data only; no manager or scheduler call on repaint."""
    return initialize(app)['snapshot']


def token(app):
    state = initialize(app)
    return state['revision'], state['review'] is not None


def _text(value, limit=240):
    return display_text(str(value if value is not None else 'unknown'))[:limit]


def _target(app):
    snap = status(app)
    target = snap.get('target')
    if isinstance(target, dict):
        target = target.get('kind') or target.get('host')
    config = getattr(app, 'cfg', {}).get('raven', {})
    if not target:
        target = config.get('execution', {}).get('heavy_target', 'unconfigured')
    return _text(target, 64)


def summary(app):
    snap = status(app)
    return f"{_label(snap)}; target {_target(app)}; job {_text(snap.get('job_id') or '-')}; host {_text(snap.get('host') or 'unverified')}."


def _label(snap):
    if snap.get('cleanup_uncertain'):
        return 'Heavy cleanup uncertain'
    if snap.get('startup', {}).get('phase') == 'reviewing':
        return 'Heavy verifying allocation'
    if snap.get('state') == 'light':
        if snap.get('error'):
            return 'Heavy unavailable'
        if snap.get('startup', {}).get('phase') == 'cancelled':
            return 'Heavy cancelled'
        return 'Light'
    return {'preparing': 'Heavy preparing', 'pending': 'Heavy allocation queued',
            'starting': 'Heavy verifying worker', 'heavy': 'Heavy ready',
            'draining': 'Heavy releasing', 'reconciling': 'Heavy cleanup uncertain'}.get(snap.get('state'), 'Light')


def headline(app):
    snap = status(app)
    return _label(snap) + '/' + _target(app)


def observation_notice(app, snapshot):
    """Explain missing governed observations without polling or changing state."""
    config=getattr(app,'cfg',{}).get('raven')
    if not isinstance(config,dict):
        return None
    if not config.get('collection',{}).get('slurm_enabled',True):
        profile='desktop profile' if config.get('site',{}).get('kind')=='desktop' else 'profile'
        return 'Slurm unavailable: collection is disabled by this '+profile+'.'
    from . import clock
    missing,disabled,observed=[],[],[]
    for source,label in (('jobs','queue'),('finished','history')):
        health=snapshot.get('health',{}).get(source)
        value=(lambda key,default:getattr(health,key,default)) if not isinstance(health,dict) else health.get
        if not value('enabled',True):
            disabled.append(label)
        elif not value('last_ok',0):
            missing.append(label)
        else:
            age=max(0,int(clock.now()-value('last_ok',0)))
            observed.append(label+' age '+str(age)+'s')
    if not missing and not disabled:
        return None
    parts=[]
    if missing:
        parts.append('Waiting for governed '+'/'.join(missing)+' observation')
    if disabled:
        parts.append('/'.join(disabled).capitalize()+' unavailable: source disabled')
    parts.append('; '.join(observed) if observed else 'source age unavailable')
    return '; '.join(parts)+'.'


def button(app, available):
    """Fixed-width lifecycle badge, separate from the worker concurrency switch."""
    if available < 10:
        return None
    snap = status(app)
    label = ('Uncertain' if snap.get('cleanup_uncertain') else
             'Retry Heavy' if snap.get('startup', {}).get('retryable') else
             {'pending': 'Heavy queued', 'starting': 'Heavy starting', 'heavy': 'Heavy ready',
              'preparing': 'Heavy starting', 'draining': 'Heavy releasing'}.get(snap.get('state'), 'Light'))
    size = 23 if available >= 23 else 15 if available >= 15 else 10
    compact = ('Check' if snap.get('cleanup_uncertain') else
               'Retry' if snap.get('startup', {}).get('retryable') else
               {'pending': 'Queued', 'starting': 'Verify', 'heavy': 'Heavy',
                'preparing': 'Start', 'draining': 'Drain'}.get(snap.get('state'), 'Light'))
    content = label + '/' + _target(app) if size == 23 else compact if size == 10 else label
    text = ' [' + L.pad(L.cut(content, size-4, True), size-4) + '] '
    style = ('warning' if snap.get('cleanup_uncertain') or snap.get('state') in ACTIVE-{'heavy'}
             else 'accent') + '+bold+bg:surface-sunken'
    return text, style


def _identity(app):
    snap = status(app)
    return (id(getattr(app, 'raven_allocation_manager', None)), snap.get('generation'),
            snap.get('request_id'), _target(app))


def _frame(app):
    state = initialize(app)
    return (getattr(app, 'mode', 'main'), getattr(app, 'tab', ''),
            getattr(app, 'width', None), getattr(app, 'height', None),
            _identity(app), tuple(choice[0] for choice in _choices(app)), id(state['review']),
            state.get('reuse_edit') is not None)


def _say(app, text, error=False):
    initialize(app)['message'] = text
    callback = getattr(app, 'fail' if error else 'say', None)
    if callable(callback):
        callback(text)


def _open(app):
    state = initialize(app)
    if getattr(app, 'mode', 'main') != 'raven':
        state['return_mode'] = 'main' if getattr(app, 'mode', 'main') in ('palette', 'confirm') else getattr(app, 'mode', 'main')
        # This is an explicit navigation action. Background allocation events
        # never blur a shell or invalidate its captured input generation.
        from . import terminal_ui
        if terminal_ui.focused(app):
            terminal_ui.blur(app)
        from .pointer_routing import begin_gesture
        begin_gesture(app)
    app.mode = 'raven'
    state.update(control_hits=[], painted=None)


def _choices(app):
    state = initialize(app)
    if state.get('reuse_edit') is not None:
        return [('dismiss','Back'),('verify','Verify allocation')]
    if state.get('reuse_pending'):
        return [('dismiss','Back')]
    if state['review']:
        action = state['review']['action']
        return [('dismiss', 'Back'), ('accept', 'Confirm ' + action)]
    snap = status(app)
    choices = [('inspect', 'Inspect allocation')]
    if snap.get('cleanup_uncertain'):
        pass  # Inspection only until ownership/cleanup has been reconciled.
    elif snap.get('state') == 'light':
        choices.append(('retry', 'Retry Heavy') if snap.get('startup', {}).get('retryable')
                       else ('request', 'Request compute'))
        if getattr(app,'raven_allocation_manager',None) is not None:
            choices.append(('reuse','Reuse allocation'))
    elif snap.get('state') in ('preparing', 'pending', 'starting'):
        choices.append(('cancel', 'Cancel request'))
    elif snap.get('state') == 'heavy':
        choices.append(('release', 'Release compute'))
    choices.append(('close', 'Close'))
    return choices


def command_names():
    return ['raven']


def run_command(app, args):
    if not args or args[0] != 'raven':
        return False
    state = initialize(app)
    action = args[1] if len(args) > 1 else 'status'
    if len(args) > 2 and action != 'reuse':
        _say(app, 'Use raven status|request|retry|cancel|release|inspect|close.', True)
        return True
    if action == 'close':
        if getattr(app, 'mode', '') == 'raven':
            app.mode = state['return_mode']
        state.update(review=None, reuse_edit=None,reuse_pending=None,control_hits=[], painted=None)
        return True
    if action == 'dismiss':
        state.update(review=None,reuse_edit=None,reuse_pending=None,cursor=0,top=0,control_hits=[],painted=None)
        return True
    if action == 'reuse':
        _open(app)
        state.update(review=None,reuse_pending=None,cursor=0,top=0)
        if len(args)==2:
            state['reuse_edit']=''
        elif len(args)==3:
            state['reuse_edit']=args[2]
            run_command(app,['raven','verify'])
        else:
            _say(app,'Use raven reuse [JOBID].',True)
        return True
    if action=='verify':
        manager=getattr(app,'raven_allocation_manager',None)
        jid=state.get('reuse_edit')
        if not isinstance(jid,str) or not re.fullmatch(r'[0-9]+',jid) or len(jid)>64:
            _say(app,'Enter one numeric Slurm allocation JobID.',True)
            return True
        method=getattr(manager,'request_reuse_review',None)
        if not callable(method):
            _say(app,'Allocation reuse requires a verified ownership and resource receipt; this manager has no supported verifier.',True)
            return True
        try:
            generation=status(app).get('generation')
            result=method(jid)
            if not isinstance(result,str) or not result: raise ValueError('reuse verification was refused')
            state.update(reuse_edit=None,review=None,reuse_pending={'job_id':jid,'generation':generation,'tab':getattr(app,'tab',''),'token':result},
                         control_hits=[],painted=None,message='Verifying ownership, remaining time and reserved resources; no worker starts yet.')
            _collect(app)
        except (OSError,ValueError,RuntimeError) as exc:
            _say(app,'Reuse unavailable: '+str(exc)[:512],True)
        return True
    if action in ('status', 'inspect'):
        _open(app)
        state.update(review=None,reuse_edit=None,reuse_pending=None,cursor=0,top=0)
        return True
    manager = getattr(app, 'raven_allocation_manager', None)
    if manager is None:
        _open(app)
        _say(app, 'Compute is unavailable: start Raven with an explicit site profile.', True)
        return True
    if action in ('request', 'retry', 'cancel', 'release'):
        _collect(app)
        snap = status(app)
        if action not in {item[0] for item in _choices(app)}:
            _open(app)
            _say(app, 'Allocation state changed; inspect the current request before acting.', True)
            return True
        try:
            plan = manager.plan() if action in ('request', 'retry') else None
            state['review'] = {'action': action, 'identity': _identity(app), 'plan': copy.deepcopy(plan),
                               'snapshot': copy.deepcopy(snap), 'tab': getattr(app, 'tab', '')}
            state.update(cursor=0, top=0, message='')
            _open(app)
        except (OSError, RuntimeError, ValueError, TypeError) as exc:
            _open(app)
            _say(app, 'Compute request unavailable: ' + str(exc)[:512], True)
        return True
    if action == 'accept':
        review = state['review']
        if not review or getattr(app, 'mode', '') != 'raven' or review.get('tab') != getattr(app, 'tab', ''):
            state['review'] = None
            _say(app, 'Open and review a compute action first.', True)
            return True
        # Read current local metadata again; never use a selected scientific
        # job or a different manager generation as the action destination.
        _collect(app)
        state.update(review=None, control_hits=[], painted=None, cursor=0, top=0)
        if review['identity'] != _identity(app):
            _say(app, 'Allocation identity changed. Review the current request again.', True)
            return True
        try:
            action = review['action']
            if action == 'reuse':
                if review.get('expires_at') and time.monotonic()>=review['expires_at']:
                    state['reuse_edit']=review['reuse_job']
                    return run_command(app,['raven','verify'])
                startup = getattr(manager, 'background_startup', None)
                if startup is not None:
                    startup.close()
                result=manager.request(reuse=review['reuse_token'],plan=review['plan'])
            elif action in ('request', 'retry'):
                if status(app).get('state') != 'light' or status(app).get('cleanup_uncertain'):
                    raise ValueError('allocation is no longer idle')
                if action == 'retry':
                    startup = getattr(manager, 'background_startup', None)
                    if startup is None:
                        raise ValueError('No Heavy startup is available to retry.')
                    result = startup.retry(plan=review['plan'])
                else:
                    result = manager.request(plan=review['plan'])
            elif action in ('cancel', 'release'):
                startup = getattr(manager, 'background_startup', None)
                result = (startup.cancel(generation=review['snapshot'].get('generation')) if startup is not None
                          else getattr(manager, action)(generation=review['snapshot'].get('generation')))
            if result is False:
                raise ValueError('manager refused this request; inspect current allocation state')
            _collect(app)
            _say(app, 'Compute ' + action + ' requested. ' + summary(app))
        except (OSError, RuntimeError, ValueError, TypeError) as exc:
            _say(app, 'Compute action refused: ' + str(exc)[:512], True)
        return True
    _say(app, 'Use raven status|request|retry|cancel|release|inspect|close.', True)
    return True


def _lines(app):
    state = initialize(app)
    review = state['review']
    snap = review['snapshot'] if review else status(app)
    lines = [summary(app), 'Inspect jobs and logs while Heavy starts; analysis becomes available after verification.']
    if snap.get('state') in ('preparing', 'pending', 'starting'):
        lines.append('Use Cancel request to stop startup. Closing this panel keeps startup running.')
    if snap.get('startup', {}).get('retryable'):
        lines.append('Use Retry Heavy after correcting the issue; every retry repeats admission and verification.')
    if state.get('reuse_edit') is not None:
        lines.insert(0,'Exact Slurm allocation JobID: '+state['reuse_edit']+'_')
        lines.insert(1,'Reuse verifies ownership and reserved resources. It never cancels the parent allocation.')
    if state.get('reuse_pending'):
        lines.insert(0,'Verifying allocation '+state['reuse_pending']['job_id']+' in the background.')
    if review:
        lines.insert(0, 'Review ' + review['action'] + ': no action occurs until Confirm.')
        if review['action'] in ('request','retry','reuse'):
            plan = review['plan'] or {}
            for key in ('target', 'site', 'site_kind', 'cluster_id', 'transport', 'ownership', 'backend', 'qualification', 'requested_resources', 'resources'):
                if key in plan:
                    value = plan[key]
                    if isinstance(value, dict):
                        lines.extend(_text(key) + ' ' + _text(k) + ': ' + _text(v) for k,v in list(value.items())[:24])
                    else:
                        lines.append(key + ': ' + _text(value,1024))
            lines.append('Only the owned worker step is released; the parent allocation remains intact.' if review['action']=='reuse'
                         else 'Local Heavy consumes the declared desktop CPU and memory budget.' if plan.get('site_kind')=='desktop'
                         else 'Dedicated compute can charge the configured account while reserved.')
            if review['action'] == 'retry' and snap.get('startup', {}).get('reuse_job'):
                lines.append('Reverify and reuse allocation '+_text(snap['startup']['reuse_job'])+'; never request a replacement allocation.')
        else:
            lines.append('Only this Raven request and its owned worker are targeted.')
    for key in ('generation', 'request_id', 'job_id', 'host', 'remaining_seconds', 'ownership', 'resources', 'qualification', 'error'):
        if snap.get(key) is not None:
            value = snap[key]
            if isinstance(value, dict):
                lines.extend(key + ' ' + _text(k) + ': ' + _text(v) for k,v in list(value.items())[:24])
            else:
                lines.append(key + ': ' + _text(value))
    if snap.get('cleanup_uncertain'):
        lines.append('Cleanup uncertain. Heavy work is unavailable. Reconcile the exact request before retrying.')
    if getattr(app, 'raven_allocation_manager', None) is None:
        lines.append('No allocation manager is attached. Use an explicit Raven site profile to request compute.')
    if state['message']:
        lines.append(state['message'])
    from .raven_diagnostics import snapshot as diagnostic_snapshot
    diagnostics=diagnostic_snapshot(app)
    for key,value in diagnostics.items():
        if isinstance(value,dict):
            lines.extend('Raven '+_text(key)+' '+_text(name)+': '+_text(item)
                         for name,item in list(value.items())[:24])
        elif key=='sources' and isinstance(value,(list,tuple)):
            for source in value[:64]:
                if isinstance(source,dict):
                    lines.append('Source '+_text(source.get('source'))+': age '+_text(source.get('age_seconds'))+
                                 's; interval '+_text(source.get('interval_seconds'))+'s; inflight '+_text(source.get('inflight')))
        elif not isinstance(value,(list,tuple)):
            lines.append('Raven '+_text(key)+': '+_text(value))
    limits=getattr(app,'raven_limits',None)
    if limits:
        lines.extend('Coverage '+_text(key)+': '+_text(value) for key,value in list(limits.coverage.copy().items())[:16])
    lines.append('Left/right: choose action; Enter: activate; Up/down or wheel: scroll; Esc: return.')
    return lines


def handle_key(app, key):
    if getattr(app, 'mode', '') != 'raven':
        return False
    state = initialize(app)
    if state.get('reuse_edit') is not None:
        if key in ('esc','q'):
            run_command(app,['raven','dismiss'])
        elif key=='enter':
            run_command(app,['raven','verify'])
        elif key in ('backspace','delete'):
            state['reuse_edit']=state['reuse_edit'][:-1]
        elif len(key)==1 and key in '0123456789' and len(state['reuse_edit'])<64:
            state['reuse_edit']+=key
        return True
    if key in ('esc', 'q'):
        run_command(app, ['raven', 'dismiss' if state['review'] else 'close'])
    elif key in ('left', 'right', 'tab', 'btab'):
        state['cursor'] = (state['cursor'] + (-1 if key in ('left','btab') else 1)) % len(_choices(app))
        state.update(control_hits=[], painted=None)
    elif key in ('up', 'down', 'pgup', 'pgdn', 'home', 'end'):
        top = state['top']
        state['top'] = max(0, {'up':top-1,'down':top+1,'pgup':top-state['page'],
                              'pgdn':top+state['page'],'home':0,'end':10**6}[key])
    elif key in ('enter', 'space'):
        choices = _choices(app)
        run_command(app, ['raven', choices[min(state['cursor'],len(choices)-1)][0]])
    elif key == ':':
        return False
    return True


def handle_mouse(app, y, x, button='left', shift=False):
    if getattr(app, 'mode', '') != 'raven':
        return False
    state = initialize(app)
    if button in ('wheel-up', 'wheel-down'):
        handle_key(app, 'up' if button == 'wheel-up' else 'down')
    elif button in ('left','press') and state['painted'] == _frame(app):
        for row, kind, item in tuple(state['control_hits']):
            if row == y and item['left'] <= x < item['right']:
                run_command(app, list(item['action'][1].split()))
                break
    return True


def overlay(views, snap, app, width, height):
    if getattr(app, 'mode', '') != 'raven':
        return None
    state = initialize(app)
    state.update(control_hits=[], painted=None)
    choices = _choices(app)
    state['cursor'] = min(state['cursor'], len(choices)-1)
    inner = max(1, width-6)
    # Keep controls above the scrolling report; no long report hides release.
    visible_choices = choices if height >= 10 else [choices[state['cursor']]]
    controls, hits = buttons(views.g, inner, [(key,label,('command','raven '+key)) for key,label in visible_choices],
                             selected=choices[state['cursor']][0], group='raven', prefix='raven:')
    report=[]
    for line in _lines(app):
        text = _text(line,1024)
        if views.g.ascii:
            text = text.encode('ascii', 'replace').decode('ascii')
        current, used = '', 0
        for char in text:
            cells = L.vlen(char)
            if used and used+cells > inner:
                report.append([(current,'text')]); current,used='',0
            current += char
            used += cells
        report.append([(current,'text')])
    capacity = max(0, height-4-len(controls))
    state['page'] = max(1, capacity)
    state['top'] = min(state['top'], max(0,len(report)-capacity))
    rows = controls + report[state['top']:state['top']+capacity]
    rendered = L.box(views.g, rows, width, height, 'Raven compute', min_width=20)
    state['control_hits'] = place_hits(hits, rendered[1:-1])
    state['painted'] = _frame(app)
    return rendered
