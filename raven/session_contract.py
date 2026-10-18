"""Bounded read-only session observations, independent of a UI or scheduler.

The session owns collection. Clients read retained state; they cannot submit
commands, change polling, request file reads, or recover an allocation.
"""
from __future__ import annotations

from collections import deque
import hashlib
import json
import math
import re
import time
import uuid

from .transport.framing import Decoder, FramePolicy, finite_constant, frame, unique_object

SCHEMA = 'raven.session/v1'
VERSION = 1
MAX_REQUEST = 16384
MAX_RESPONSE = 2 << 20
MAX_EVENTS = 64
POLICY = FramePolicy('session', MAX_RESPONSE, 2 * MAX_RESPONSE, eager_header=True, max_messages=16)
REQUEST_POLICY = FramePolicy('session request', MAX_REQUEST, 2 * MAX_REQUEST, eager_header=True, max_messages=8)


def loads(raw):
    try:
        value = json.loads(raw, object_pairs_hook=unique_object, parse_constant=finite_constant)
    except (UnicodeError, RecursionError) as exc:
        raise ValueError('Invalid session JSON') from exc
    from .evidence import bounded
    bounded(value, max_nodes=200000)
    if not isinstance(value, dict):
        raise ValueError('Session envelopes must be objects')
    return value


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode()


def message(value, *, request=False):
    return frame(encoded(value), REQUEST_POLICY if request else POLICY)


def decoder(*, request=False):
    return Decoder(REQUEST_POLICY if request else POLICY, loads)


def request(operation, *, session='', epoch='', cursor=0):
    value = {'schema': SCHEMA, 'version': VERSION, 'op': operation, 'session': session, 'epoch': epoch, 'cursor': cursor}
    validate_request(value)
    return value


def validate_request(value):
    if set(value) != {'schema', 'version', 'op', 'session', 'epoch', 'cursor'}:
        raise ValueError('Unknown or missing session request fields')
    if value['schema'] != SCHEMA or type(value['version']) is not int or value['version'] != VERSION:
        raise ValueError('Session protocol version mismatch; install compatible client and service')
    if value['op'] not in ('hello', 'snapshot', 'events'):
        raise ValueError('The session accepts read-only hello, snapshot and events queries')
    for name in ('session', 'epoch'):
        if not isinstance(value[name], str) or (value[name] and (len(value[name]) != 32 or any(c not in '0123456789abcdef' for c in value[name]))):
            raise ValueError('Invalid ' + name + ' identity')
    if type(value['cursor']) is not int or not 0 <= value['cursor'] < 2**63:
        raise ValueError('Invalid session replay cursor')


def _time(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1e12:
        raise ValueError('Invalid session observation time')


def validate_snapshot(value):
    if not isinstance(value, dict):
        raise ValueError('Session snapshot must be an object')
    for source in ('jobs', 'finished'):
        if source in value:
            if not isinstance(value[source], list) or len(value[source]) > 2000:
                raise ValueError('Session job list exceeds its contract')
            for row in value[source]:
                if not isinstance(row, dict) or not isinstance(row.get('id'), str) or not row['id'] or len(row['id']) > 128:
                    raise ValueError('Session row needs an exact bounded job identity')
                for name in ('name', 'state'):
                    if name in row and not isinstance(row[name], str):
                        raise ValueError('Session job labels must be text')
                if 'projects' in row and (not isinstance(row['projects'], list) or len(row['projects']) > 64 or
                                           any(not isinstance(item, str) or not item or len(item) > 128 for item in row['projects'])):
                    raise ValueError('Invalid shared session project labels')
    if 'queue_observed_at' in value:
        _time(value['queue_observed_at'])
    if 'authorization' in value:
        grant = value['authorization']
        if not isinstance(grant, dict) or set(grant) != {'user', 'accounts', 'projects', 'site'}:
            raise ValueError('Invalid shared session authorization description')
        if not isinstance(grant['user'], str) or not grant['user'] or len(grant['user']) > 256 or type(grant['site']) is not bool:
            raise ValueError('Invalid shared session principal description')
        for name in ('accounts', 'projects'):
            if not isinstance(grant[name], list) or len(grant[name]) > 64 or any(not isinstance(item, str) or not item or len(item) > 128 for item in grant[name]):
                raise ValueError('Invalid shared session grant description')
    if 'source_errors' in value:
        errors = value['source_errors']
        if not isinstance(errors, dict) or set(errors) != {'jobs', 'finished'} or any(not isinstance(error, str) or len(error) > 4096 for error in errors.values()):
            raise ValueError('Invalid shared session source errors')
    if 'health' in value:
        if not isinstance(value['health'], dict) or len(value['health']) > 128:
            raise ValueError('Session source health exceeds its contract')
        for health in value['health'].values():
            if not isinstance(health, dict):
                raise ValueError('Session source health must be an object')
            for name in ('last_ok', 'last_try'):
                if name in health:
                    _time(health[name])


def validate_response(value):
    required = {'schema', 'version', 'session', 'epoch', 'sequence', 'scope', 'stale', 'observed_at', 'service_error',
                'age_seconds', 'capabilities', 'mutations', 'gap', 'events', 'snapshot'}
    if not isinstance(value, dict) or not required <= value.keys():
        raise ValueError('Incomplete session response')
    validate_request(request('events', session=value['session'], epoch=value['epoch'], cursor=value['sequence']))
    if not value['session'] or not value['epoch']:
        raise ValueError('Session response needs exact nonempty ownership identities')
    if value['schema'] != SCHEMA or type(value['version']) is not int or value['version'] != VERSION:
        raise ValueError('Session protocol version mismatch')
    if not re.fullmatch('[0-9a-f]{64}', str(value['scope'])) or type(value['stale']) is not bool:
        raise ValueError('Invalid session scope or freshness state')
    if value['capabilities'] != ['snapshot', 'events'] or value['mutations'] is not False:
        raise ValueError('Unexpected session action/security semantics')
    _time(value['observed_at'])
    if value['age_seconds'] is not None:
        _time(value['age_seconds'])
    if not isinstance(value['service_error'], str) or len(value['service_error']) > 4096:
        raise ValueError('Invalid session error diagnostic')
    if not isinstance(value['events'], list) or len(value['events']) > MAX_EVENTS:
        raise ValueError('Session replay exceeds its event bound')
    previous = -1
    for event in value['events']:
        if not isinstance(event, dict) or set(event) != {'sequence', 'observed_at', 'kind', 'sha256'}:
            raise ValueError('Invalid session replay event')
        if type(event['sequence']) is not int or not previous < event['sequence'] <= value['sequence'] or event['kind'] != 'snapshot' or not re.fullmatch('[0-9a-f]{64}', str(event['sha256'])):
            raise ValueError('Invalid session event identity or order')
        previous = event['sequence']
        _time(event['observed_at'])
    if value['gap'] is not None:
        gap = value['gap']
        if not isinstance(gap, dict) or set(gap) != {'reason', 'after', 'through'} or not isinstance(gap['reason'], str) or len(gap['reason']) > 1024:
            raise ValueError('Invalid replay gap')
        if any(type(gap[k]) is not int or not 0 <= gap[k] < 2**63 for k in ('after', 'through')):
            raise ValueError('Invalid replay gap sequence')
    validate_snapshot(value['snapshot'])
    loads(encoded(value))
    return value


class Observations:
    """One service-owned sequence; no per-client collectors or event queues."""
    def __init__(self, scope, retained=None):
        self.scope = hashlib.sha256(encoded(scope)).hexdigest()
        self.epoch = uuid.uuid4().hex
        retained = retained or {}
        if not isinstance(retained, dict):
            raise ValueError('Retained session must be an object')
        if retained and (retained.get('schema') != SCHEMA or retained.get('scope') != self.scope):
            raise ValueError('Retained session belongs to a different connection; choose a new directory')
        if retained:
            if set(retained) != {'schema', 'scope', 'session', 'sequence', 'snapshot', 'observed_at'}:
                raise ValueError('Invalid retained session contract')
            validate_request(request('events', session=retained['session'], cursor=retained['sequence']))
            if not retained['session']:
                raise ValueError('Retained session identity is missing')
            _time(retained['observed_at'])
            validate_snapshot(retained['snapshot'])
            loads(encoded(retained))
        self.session = retained.get('session') or uuid.uuid4().hex
        self.sequence = retained.get('sequence', 0)
        if type(self.sequence) is not int or not 0 <= self.sequence < 2**63 - 1:
            raise ValueError('Invalid retained session sequence')
        self.snapshot = retained.get('snapshot', {})
        self.observed_at = retained.get('observed_at', 0.)
        self.stale = True
        self.error = ''
        self.events = deque(maxlen=MAX_EVENTS)
        self._digest = None

    def publish(self, snapshot):
        raw = encoded(snapshot)
        if len(raw) > MAX_RESPONSE - 65536:
            raise ValueError('Observation exceeds session response budget; no partial snapshot was published')
        digest = hashlib.sha256(raw).digest()
        if digest == self._digest:
            return False
        detached = loads(raw)
        validate_snapshot(detached)
        self._digest = digest
        self.sequence += 1
        self.snapshot = detached
        self.observed_at = time.time()
        self.stale = False
        self.error = ''
        self.events.append({'sequence': self.sequence, 'observed_at': self.observed_at, 'kind': 'snapshot', 'sha256': digest.hex()})
        return True

    def retained(self):
        return {'schema': SCHEMA, 'scope': self.scope, 'session': self.session, 'sequence': self.sequence,
                'snapshot': self.snapshot, 'observed_at': self.observed_at}

    def query(self, value):
        validate_request(value)
        if value['session'] and value['session'] != self.session:
            raise ValueError('Session identity changed; reconnect explicitly to inspect the new session')
        cursor = value['cursor']
        first = self.events[0]['sequence'] if self.events else self.sequence + 1
        gap = None
        if value['epoch'] and value['epoch'] != self.epoch:
            gap = {'reason': 'service restarted; replay before this epoch is unavailable', 'after': cursor, 'through': self.sequence}
        elif cursor > self.sequence:
            raise ValueError('Replay cursor is ahead of this session')
        elif cursor < first - 1:
            gap = {'reason': 'bounded replay history expired', 'after': cursor, 'through': first - 1}
        return {'schema': SCHEMA, 'version': VERSION, 'session': self.session, 'epoch': self.epoch, 'sequence': self.sequence,
                'scope': self.scope, 'stale': self.stale, 'observed_at': self.observed_at,
                'service_error': self.error,
                'age_seconds': max(0., time.time() - self.observed_at) if self.observed_at else None,
                'capabilities': ['snapshot', 'events'], 'mutations': False, 'gap': gap,
                'events': [event for event in self.events if event['sequence'] > cursor], 'snapshot': self.snapshot}
