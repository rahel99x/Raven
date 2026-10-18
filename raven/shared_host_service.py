"""Experimental administrator-configured, read-only scheduler fan-out.

Unix credentials authenticate a peer; an explicit policy separately authorizes
its user/project/account/site projection. This is not a privileged file or command API.
"""
from __future__ import annotations

import argparse
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import signal
import stat
import threading
import time

from . import session_contract as C
from .session_service import Server, Client, SOCKET
from .config_trust import directory_fd, read as trusted_read

POLICY = 'raven.shared-host-policy/v1'
MAX_CACHE_BYTES = 8 << 20
MAX_POLICY_BYTES = 256 << 10
PROJECT_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}')
EXACT_JOB_ID = re.compile(r'[1-9][0-9]*(?:_[0-9]+|\+[0-9]+)?')
ATTEMPT_FIELDS = ('cluster', 'job_id', 'submit_time', 'start_time', 'restart_count')
FIELDS = ('id', 'name', 'state', 'partition', 'cluster', 'user', 'account', 'submit', 'start', 'end',
          'elapsed', 'limit', 'nodes', 'cpus', 'gpus', 'mem_req', 'req_mem', 'rss', 'cpu_time', 'exit')


def attempt_key(value):
    """Only complete native parent attempts may authorize project visibility."""
    if not isinstance(value, dict) or set(value) != set(ATTEMPT_FIELDS):
        raise ValueError('Project membership needs a complete native attempt identity')
    if not isinstance(value['cluster'], str) or not value['cluster'] or len(value['cluster']) > 128 or not value['cluster'].isprintable():
        raise ValueError('Project membership needs an exact bounded cluster')
    if not isinstance(value['job_id'], str) or len(value['job_id']) > 128 or not EXACT_JOB_ID.fullmatch(value['job_id']):
        raise ValueError('Project membership needs an exact job/task/component; ranges are not attempts')
    for field in ('submit_time', 'start_time'):
        if type(value[field]) is not int or not 0 < value[field] <= 253402300799:
            raise ValueError('Project membership needs known positive native submit/start seconds')
    if value['start_time'] < value['submit_time']:
        raise ValueError('Project attempt start precedes submission')
    if type(value['restart_count']) is not int or not 0 <= value['restart_count'] < 2**32:
        raise ValueError('Project membership needs a known native restart count')
    return tuple(value[field] for field in ATTEMPT_FIELDS)


def membership_index(value):
    index = {}
    for project_id, attempts in value.get('project_memberships', {}).items():
        for attempt in attempts:
            index.setdefault(attempt_key(attempt), set()).add(project_id)
    return {key: frozenset(projects) for key, projects in index.items()}


def policy(value):
    required = {'schema', 'id', 'socket_gid', 'principals', 'interval_seconds', 'max_records'}
    if not isinstance(value, dict) or not required <= set(value) <= required | {'project_memberships'} or value['schema'] != POLICY:
        raise ValueError('Expected a complete raven.shared-host-policy/v1 object')
    if not isinstance(value['id'], str) or not value['id'] or len(value['id']) > 128:
        raise ValueError('Shared collector policy needs a bounded identity')
    if type(value['socket_gid']) is not int or not 0 <= value['socket_gid'] < 2**31:
        raise ValueError('Shared socket group must be an explicit numeric GID')
    if type(value['interval_seconds']) is not int or not 5 <= value['interval_seconds'] <= 3600:
        raise ValueError('Shared collection interval must be 5..3600 seconds')
    if type(value['max_records']) is not int or not 1 <= value['max_records'] <= 10000:
        raise ValueError('Shared scheduler record bound must be 1..10000')
    projects = value.get('project_memberships', {})
    if not isinstance(projects, dict) or len(projects) > 256:
        raise ValueError('Declare at most 256 protected project memberships')
    total = 0
    for project_id, attempts in projects.items():
        if not isinstance(project_id, str) or not PROJECT_ID.fullmatch(project_id):
            raise ValueError('Project membership needs an exact Raven project_id')
        if not isinstance(attempts, list) or len(attempts) > 1024:
            raise ValueError('Project attempt membership must be a bounded list')
        keys = [attempt_key(attempt) for attempt in attempts]
        if len(set(keys)) != len(keys):
            raise ValueError('Duplicate project attempt membership')
        total += len(keys)
    if total > 1024:
        raise ValueError('Shared policy exceeds 1024 total project attempt memberships')
    principals = value['principals']
    if not isinstance(principals, dict) or not 1 <= len(principals) <= 256:
        raise ValueError('Declare 1..256 explicit shared collector principals')
    for uid, grant in principals.items():
        if not isinstance(uid, str) or not uid.isascii() or not uid.isdecimal() or str(int(uid)) != uid or int(uid) >= 2**31:
            raise ValueError('Principal keys must be exact numeric Unix UIDs')
        if not isinstance(grant, dict) or not {'user', 'accounts', 'site'} <= set(grant) <= {'user', 'accounts', 'site', 'projects'}:
            raise ValueError('Each principal needs explicit user, accounts and site grants')
        try:
            login = pwd.getpwuid(int(uid)).pw_name
        except KeyError as exc:
            raise ValueError('Shared principal is not known to this host: ' + uid) from exc
        if grant['user'] != login:
            raise ValueError('Principal scheduler login must match the authenticated host account')
        if type(grant['site']) is not bool or not isinstance(grant['accounts'], list) or len(grant['accounts']) > 64:
            raise ValueError('Account/site visibility must be explicit and bounded')
        if any(not isinstance(account, str) or not account or len(account) > 128 or not account.isprintable() for account in grant['accounts']) or len(set(grant['accounts'])) != len(grant['accounts']):
            raise ValueError('Account grants must be unique bounded names')
        selected = grant.get('projects', [])
        if not isinstance(selected, list) or len(selected) > 64 or any(not isinstance(item, str) or item not in projects for item in selected) or len(set(selected)) != len(selected):
            raise ValueError('Project grants must name unique declared projects, at most 64 per principal')
    raw = C.encoded(value)
    if len(raw) > MAX_POLICY_BYTES:
        raise ValueError('Shared policy exceeds 256 KiB')
    C.loads(raw)
    return value


def load_policy(path):
    raw, info = trusted_read(path, limit=MAX_POLICY_BYTES)
    value = policy(C.loads(raw))
    generation = hashlib.sha256(C.encoded(value)).hexdigest()
    return value, generation, (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _record_owner(record):
    if not isinstance(record, dict) or not isinstance(record.get('user'), str) or not record['user']:
        raise ValueError('Scheduler ownership is missing or ambiguous; no shared response published')


def _record_attempt(record):
    if record.get('_attempt') is None:
        return None
    key = attempt_key(record['_attempt'])
    if key[0] != record.get('cluster') or key[1] != record.get('id'):
        raise ValueError('Native attempt identity differs from the scheduler row')
    return key


def validate_records(records):
    # Publication owns a deep copy. Validate private authority metadata once
    # before any grant can see it, including own-user/account/site grants.
    for record in records:
        _record_owner(record)
        _record_attempt(record)


def project(records, grant, memberships=None):
    """Filter first, then project an allowlist; never return raw scheduler rows."""
    result = []
    permitted_projects = frozenset(grant.get('projects', []))
    for record in records:
        _record_owner(record)
        matched = frozenset()
        if memberships and permitted_projects:
            matched = memberships.get(_record_attempt(record), frozenset()) & permitted_projects
        if grant['site'] or record['user'] == grant['user'] or record.get('account') in grant['accounts'] or matched:
            row = {key: record[key] for key in FIELDS if key in record}
            if matched:
                row['projects'] = sorted(matched)
            result.append(row)
    return result


class SharedServer(Server):
    socket_mode = 0o660

    def __init__(self, directory, policy_file):
        self.policy_file = Path(policy_file).expanduser().absolute()
        self.policy, self.generation, self._policy_identity = load_policy(self.policy_file)
        self._memberships = membership_index(self.policy)
        self._gid = self.policy['socket_gid']
        self._views = OrderedDict()
        self._view_metadata = {}
        self._uid_rates = {}
        self._cache_bytes = 0
        self._source = {'jobs': [], 'finished': [], 'queue_observed_at': 0., 'accounting_observed_at': 0.,
                        'source_errors': {'jobs': 'No scheduler observation yet', 'finished': 'No scheduler observation yet'}}
        self.policy_error = ''
        super().__init__(directory, {'kind': POLICY, 'policy_id': self.policy['id'], 'uid': os.getuid()}, max_clients=256)
        try:
            os.chown(self.root/SOCKET, -1, self._gid)
        except BaseException:
            self.close()
            raise

    def directory_policy(self, directory, *, create=False):
        root = Path(directory).expanduser().absolute()
        with directory_fd(root.parent, create=create) as parent:
            try:
                os.mkdir(root.name, mode=0o750, dir_fd=parent)
            except FileExistsError:
                pass
            else:
                created = os.open(root.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                try:
                    os.fchown(created, -1, self._gid)
                    os.fchmod(created, 0o750)
                finally:
                    os.close(created)
        with directory_fd(root):
            info = root.stat()
            if info.st_uid != os.getuid() or info.st_gid != self._gid or stat.S_IMODE(info.st_mode) != 0o750:
                raise ValueError('Shared service directory must be owned by the service account, mode0750 and the approved socket group')
        if len(os.fsencode(root/SOCKET)) >= 104:
            raise ValueError('Shared service socket path is too long')
        return root

    def authorized(self, uid):
        return not self.policy_error and str(uid) in self.policy['principals']

    def admit_connection(self, uid):
        # A principal's idle connections must not consume the whole host pool.
        # This bounded scan avoids a second mutable accounting structure.
        return sum(client['uid'] == uid for client in self.clients.values()) < 4

    def reload_policy(self):
        try:
            info = self.policy_file.lstat()
            identity = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            if identity == self._policy_identity and not self.policy_error:
                return False
            value, generation, identity = load_policy(self.policy_file)
            if value['socket_gid'] != self._gid or value['id'] != self.policy['id']:
                raise ValueError('Socket group/service identity changed; stop and restart the service after administrator review')
        except (OSError, ValueError) as exc:
            if str(exc) != self.policy_error:
                print('Shared collector policy rejected: '+str(exc), file=__import__('sys').stderr)
            self.policy_error = str(exc)
            for connection in tuple(self.clients):
                self._drop(connection)
            return True
        self._policy_identity = identity
        self.policy_error = ''
        if generation == self.generation:
            return False
        self.policy, self.generation = value, generation
        self._memberships = membership_index(value)
        print('Shared collector policy generation accepted: '+generation, file=__import__('sys').stderr)
        self._views.clear()
        self._view_metadata.clear()
        self._uid_rates.clear()
        self._cache_bytes = 0
        # Do not drain any previously authorized queued data after observing
        # revocation. Already delivered bytes cannot be retroactively revoked.
        for connection in tuple(self.clients):
            self._drop(connection)
        return True

    def publish_source(self, value):
        if not isinstance(value, dict) or set(value) != {'jobs', 'finished', 'queue_observed_at', 'accounting_observed_at', 'source_errors'}:
            raise ValueError('Invalid shared scheduler observation')
        raw = C.encoded(value)
        if len(raw) > 4 << 20:
            raise ValueError('Shared scheduler response exceeds 4 MiB')
        # All checks apply to the private copy that will actually be published,
        # never to an externally mutable input followed by a later copy.
        value = C.loads(raw)
        for source in ('jobs', 'finished'):
            if not isinstance(value[source], list) or len(value[source]) > self.policy['max_records']:
                raise ValueError('Shared scheduler response exceeds the admitted record bound')
            # Ownership ambiguity cannot be hidden by another user's filter.
            validate_records(value[source])
        if not isinstance(value['source_errors'], dict) or set(value['source_errors']) != {'jobs', 'finished'} or any(not isinstance(error, str) or len(error) > 4096 for error in value['source_errors'].values()):
            raise ValueError('Invalid shared source health')
        C._time(value['queue_observed_at'])
        C._time(value['accounting_observed_at'])
        self._source = value
        for entry in self._views.values():
            entry['fresh'] = False

    def query(self, client, value):
        C.validate_request(value)
        uid = client['uid']
        grant = self.policy['principals'].get(str(uid))
        if grant is None or self.policy_error:
            raise ValueError('Shared scheduler visibility was revoked')
        if any(len(self._source[source]) > self.policy['max_records'] for source in ('jobs', 'finished')):
            raise ValueError('Retained source exceeds the new policy bound; await an admitted replacement')
        key = (uid, self.generation, hashlib.sha256(C.encoded(grant)).hexdigest())
        entry = self._views.get(key)
        if entry is None or not entry['fresh']:
            snapshot = {source: project(self._source[source], grant, self._memberships) for source in ('jobs', 'finished')}
            snapshot.update({key: self._source[key] for key in ('queue_observed_at', 'accounting_observed_at', 'source_errors')})
            snapshot['policy_generation'] = self.generation
            snapshot['job_fields'] = list(FIELDS) + ['projects']
            snapshot['authorization'] = {'user': grant['user'], 'accounts': list(grant['accounts']),
                                         'projects': list(grant.get('projects', [])), 'site': grant['site']}
            # No global row counts, event sequences or source digests are
            # exposed: those would disclose other users' activity indirectly.
            snapshot['coverage'] = {'mode': 'authorized projection within service-account scheduler visibility',
                                    'complete': not any(self._source['source_errors'].values()),
                                    'project_binding': 'Only exact administrator-listed native attempts with known cluster, submission, start and restart; unknown identities abstain.'}
            if max(len(snapshot['jobs']), len(snapshot['finished'])) > 2000:
                raise ValueError('Authorized view exceeds the session row budget; use a narrower administrator grant')
            size = len(C.encoded(snapshot))
            if size > MAX_CACHE_BYTES:
                raise ValueError('Authorized projection exceeds the shared response cache bound')
            if entry:
                observations = entry['observations']
            else:
                retained = self._view_metadata.get(key)
                observations = C.Observations({'uid': uid, 'policy_generation': self.generation, 'grant': grant}, retained)
                # Service-scoped deterministic session ID permits cache eviction
                # without turning a reconnect into a different user's context.
                observations.session = hashlib.sha256((self.observations.session+str(uid)+self.generation).encode()).hexdigest()[:32]
                observations.epoch = self.observations.epoch
            observations.publish(snapshot)
            observations.stale = any(self._source['source_errors'].values())
            if entry:
                self._cache_bytes -= entry['size']
            entry = {'observations': observations, 'size': size, 'fresh': True}
            self._views[key] = entry
            self._cache_bytes += size
            while self._cache_bytes > MAX_CACHE_BYTES:
                old_key, old = self._views.popitem(last=False)
                self._cache_bytes -= old['size']
                retained = old['observations'].retained()
                retained['snapshot'] = {}
                self._view_metadata[old_key] = retained
        self._views.move_to_end(key)
        return entry['observations'].query(value)

    def admit_read(self, client, now):
        uid = client['uid']
        tokens, updated = self._uid_rates.get(uid, (4., now))
        tokens = min(4., tokens + max(0., now-updated)*4)
        if tokens < 1:
            self._uid_rates[uid] = (tokens, now)
            raise ValueError('Shared per-user read quota reached; retry later')
        super().admit_read(client, now)
        self._uid_rates[uid] = (tokens-1, now)

    def poll(self, timeout=.5):
        self.reload_policy()
        return super().poll(timeout)


def collect(backend, previous, *, max_records, queue=True, accounting=True, cancel=None):
    """Fixed native read commands only; structured identity is mandatory."""
    from . import slurm_json as J
    from .slurm import ParseDiagnostics
    from .model import to_plain
    from .operations import _backend_command
    from .platform_session import GovernedBackend
    value = {**previous, 'source_errors': dict(previous['source_errors'])}
    commands = []
    if queue:
        commands.append(('jobs', ['squeue', '--json'], J.queue, 'queue_observed_at'))
    if accounting:
        commands.append(('finished', ['sacct', '--json', '--allusers', '-X', '-S', 'now-24hours'], J.accounting, 'accounting_observed_at'))
    for source, argv, parse, timestamp in commands:
        try:
            raw = backend.observe(argv, 8, 4 << 20, cancel) if type(backend) is GovernedBackend else _backend_command(backend, argv, 8, 4 << 20, cancel)
            document = J.document(raw)
            report = ParseDiagnostics('shared-'+source)
            records = parse(document, report)
            if report.rejected or len(records) > max_records:
                raise ValueError('Rejected records or exceeded the admitted record bound')
            # Accounting sorts completed rows and excludes active rows. Match by
            # exact native ID rather than zipping it with the original array.
            attempts = {}
            for native in document['jobs']:
                if source == 'jobs':
                    jid = J.identifier(native)
                    submit, start = native.get('submit_time'), native.get('start_time')
                else:
                    array, het = native.get('array', {}), native.get('het', {})
                    jid = J.identifier({'job_id': native.get('job_id'), 'array_job_id': array.get('job_id'),
                                        'array_task_id': array.get('task_id'), 'array_task_string': array.get('task'),
                                        'het_job_id': het.get('job_id'), 'het_job_offset': het.get('job_offset')})
                    submit, start = native['time'].get('submission'), native['time'].get('start')
                try:
                    attempt = {'cluster': J.text(native.get('cluster')), 'job_id': jid,
                               'submit_time': J.integer(submit), 'start_time': J.integer(start),
                               'restart_count': J.integer(native.get('restart_cnt'))}
                    attempt_key(attempt)
                    if source == 'jobs' and J.state(native.get('job_state')) not in ('RUNNING', 'SUSPENDED', 'COMPLETING'):
                        # Pending start_time can be a scheduler prediction.
                        # It cannot establish a verified execution attempt.
                        attempt = None
                except ValueError:
                    # Missing restart/start is not evidence for restart zero.
                    # Other explicit grants remain usable for this row.
                    attempt = None
                attempts[jid] = attempt
            shaped = [{**{key: value for key, value in to_plain(record).items() if key in FIELDS},
                       '_attempt': attempts[record.id]} for record in records]
            validate_records(shaped)
            value[source], value[timestamp] = shaped, time.time()
            value['source_errors'][source] = ''
        except (OSError, ValueError, RuntimeError) as exc:
            # Detailed source diagnostics stay in administrator-owned logs;
            # scheduler error text can contain another user's job attributes.
            print('Shared collector '+source+': '+str(exc), file=__import__('sys').stderr)
            value['source_errors'][source] = 'Scheduler source unavailable; retained authorized observations may be stale. Administrator diagnostics contain details.'
    return value


class SharedClient(Client):
    def __init__(self, directory, *, server_uid, timeout=5):
        if type(server_uid) is not int or not 0 <= server_uid < 2**31:
            raise ValueError('Choose the independently trusted shared-service Unix UID')
        self.root = Path(directory).expanduser().absolute()
        # Service identity is independently selected; it need not be this
        # client's UID. Do not weaken config/plugin trust to accommodate it.
        system_uid = os.stat('/').st_uid
        fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for component in self.root.parts[1:]:
                following = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = following
                info = os.fstat(fd)
                sticky = info.st_uid in {0, system_uid} and info.st_mode & stat.S_ISVTX
                if info.st_uid not in {0, system_uid, server_uid} or (info.st_mode & 0o022 and not sticky):
                    raise ValueError('Shared service directory has an untrusted ancestor')
            info = os.fstat(fd)
            if info.st_uid != server_uid or stat.S_IMODE(info.st_mode) != 0o750:
                raise ValueError('Shared service directory ownership/mode differs from the trusted service identity')
        finally:
            os.close(fd)
        self.expected_server_uid = server_uid
        self.timeout = timeout
        self.session = self.epoch = ''
        self.cursor = 0


def serve(directory, policy_file, *, config=None, site=None, cancel=None):
    from .raven_config import resolve, to_raven_config
    from .platform_session import wrap_backend
    from .slurm import Backend
    resolved = resolve(config, site=site, launcher='light')
    if resolved.data['connection']['transport'] != 'local':
        raise ValueError('Shared host collection requires an explicit local Slurm profile')
    stop = cancel or threading.Event()
    server = SharedServer(directory, policy_file)
    backend = pool = None
    pending = None
    next_queue = next_accounting = next_health = 0.
    try:
        backend = wrap_backend(Backend(), to_raven_config(resolved), {'backend': 'local', 'host': __import__('socket').gethostname(),
                               'profile': resolved.data['profile'], 'uid': os.getuid(), 'user': pwd.getpwuid(os.getuid()).pw_name}, str(server.root/'collection'))
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='raven-shared-read')
        while not stop.is_set():
            now = time.monotonic()
            server.reload_policy()
            if now >= next_health:
                print(json.dumps({'schema': 'raven.shared-host-health/v1', 'policy_id': server.policy['id'],
                                  'generation': server.generation, 'policy_error': server.policy_error,
                                  'clients': len(server.clients), 'cache_bytes': server._cache_bytes,
                                  'source_errors': server._source['source_errors'],
                                  'queue_observed_at': server._source['queue_observed_at'],
                                  'accounting_observed_at': server._source['accounting_observed_at']}, sort_keys=True), file=__import__('sys').stderr)
                next_health = now + 60
            if pending is not None and pending.done():
                try:
                    server.publish_source(pending.result())
                except (OSError, ValueError, RuntimeError) as exc:
                    print('Shared collector publication: '+str(exc), file=__import__('sys').stderr)
                    server._source = {**server._source, 'source_errors': {source: 'New observation rejected; retained data may be stale.' for source in ('jobs', 'finished')}}
                    for entry in server._views.values():
                        entry['fresh'] = False
                pending = None
            if pending is None and not server.policy_error and now >= min(next_queue, next_accounting):
                queue, accounting = now >= next_queue, now >= next_accounting
                collection = resolved.data['collection']
                if queue:
                    next_queue = now + max(server.policy['interval_seconds'], collection['queue_seconds'])
                if accounting:
                    next_accounting = now + max(server.policy['interval_seconds'], collection['history_seconds'])
                pending = pool.submit(collect, backend, server._source, max_records=server.policy['max_records'], queue=queue, accounting=accounting, cancel=stop)
            server.poll(.25)
    finally:
        stop.set()
        if pool is not None:
            pool.shutdown(wait=True, cancel_futures=True)
        server.close()
        close = getattr(backend, 'close', None)
        if close:
            close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('serve', 'query', 'watch', 'tui'))
    parser.add_argument('directory')
    parser.add_argument('--policy')
    parser.add_argument('--server-uid', type=int)
    parser.add_argument('--config')
    parser.add_argument('--site')
    args = parser.parse_args(argv)
    stop = threading.Event()
    previous = {number: signal.signal(number, lambda *_: stop.set()) for number in (signal.SIGINT, signal.SIGTERM)}
    try:
        if args.action != 'serve':
            if args.server_uid is None:
                raise ValueError('Shared clients require the independently trusted --server-uid')
            client = SharedClient(args.directory, server_uid=args.server_uid)
            if args.action == 'tui':
                from .session_service import _terminal
                _terminal(client)
                return 0
            while not stop.is_set():
                print(C.encoded(client.query()).decode(), flush=True)
                if args.action == 'query':
                    break
                stop.wait(1)
            return 0
        if not args.policy:
            raise ValueError('Shared service requires an administrator-reviewed --policy file')
        serve(args.directory, args.policy, config=args.config, site=args.site, cancel=stop)
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print('Raven shared collector: '+str(exc), file=__import__('sys').stderr)
        return 2
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


if __name__ == '__main__':
    raise SystemExit(main())
