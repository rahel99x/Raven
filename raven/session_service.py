"""Optional read-only session daemon and reconnecting clients.

Run explicitly with ``python -m raven.session_service --help``. Installation and
ordinary Raven launch never start a daemon. No client request triggers collection
or scheduler mutation; one managed Light sampler owns observations.
"""
from __future__ import annotations

import argparse
from collections import deque
import json
import os
from pathlib import Path
import selectors
import signal
import socket
import stat
import struct
import sys
import threading
import time

from . import session_contract as C
from .config_trust import directory_fd, read as trusted_read
from .services_io import write_json_file
from .state_ownership import StateOwner
from .transport.framing import write_pending

SOCKET = 'session.sock'
MAX_CLIENTS = 64
MAX_QUEUED = 2 * C.MAX_RESPONSE
MAX_TOTAL_QUEUED = 8 << 20


def peer_uid(connection):
    if not hasattr(socket, 'SO_PEERCRED'):
        raise ValueError('Session service requires Linux SO_PEERCRED authentication')
    return struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]


def private_root(directory, *, create=False):
    root = Path(directory).expanduser().absolute()
    with directory_fd(root, private=True, create=create):
        pass
    if len(os.fsencode(root / SOCKET)) >= 104:
        raise ValueError('Session directory is too long for a Unix socket; choose a shorter host-local path')
    return root


def observation(store, *, max_records=2000):
    """Detach a bounded public observation; exclude logs, commands and config."""
    from .model import to_plain
    fields = ('id', 'name', 'state', 'partition', 'cluster', 'user', 'account', 'submit', 'start', 'end',
              'elapsed', 'limit', 'nodes', 'cpus', 'gpus', 'mem_req', 'req_mem', 'rss', 'cpu_time', 'exit', 'reason')
    def rows(records):
        return [{name: getattr(record, name) for name in fields if hasattr(record, name)} for record in records[:max_records]]
    with store.lock:
        counts = {'jobs': len(store.jobs), 'finished': len(store.finished), 'nodes': len(store.nodes)}
        value = {'jobs': rows(store.jobs), 'finished': rows(store.finished),
                 'nodes': to_plain(dict(list(store.nodes.items())[:max_records])),
                 'health': to_plain(store.health), 'events': to_plain(list(store.events)[-200:]),
                 'queue_observed_at': store.t_jobs, 'error': store.error, 'job_fields': list(fields),
                 'coverage': {name: {'observed': count, 'returned': min(count, max_records), 'omitted': max(0, count-max_records)} for name, count in counts.items()}}
    return value


class Server:
    """Selector owner with peer authentication and bounded slow-client queues."""
    directory_policy = staticmethod(private_root)
    socket_mode = 0o600

    def __init__(self, directory, scope, *, max_clients=MAX_CLIENTS):
        if type(max_clients) is not int or not 1 <= max_clients <= 256:
            raise ValueError('Session clients must be bounded to 1..256')
        self.root = self.directory_policy(directory, create=True)
        self.owner = StateOwner(str(self.root), strict=True)
        self.selector = selectors.DefaultSelector()
        self.listener = None
        self.clients = {}
        self.max_clients = max_clients
        self.closed = False
        self._snapshot_wire = {}
        self._snapshot_wire_bytes = 0
        self.metrics = {'accepted': 0, 'rejected_peers': 0, 'queries': 0, 'slow_clients': 0}
        self._rate_tokens, self._rate_updated = 64., time.monotonic()
        self._read_cursor = 0
        try:
            retained = {}
            try:
                raw, _ = trusted_read(self.root / 'session.json', private=True, limit=C.MAX_RESPONSE)
                retained = C.loads(raw)
            except FileNotFoundError:
                pass
            self.observations = C.Observations(scope, retained)
            path = self.root / SOCKET
            if os.path.lexists(path):
                info = path.lstat()
                if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != self.socket_mode:
                    raise ValueError('Refusing to replace an unsafe or unrelated session socket')
                # The kernel writer lock, not PID guesses, establishes that no
                # cooperating owner can be using this stale path.
                path.unlink()
            self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.listener.bind(str(path))
            path.chmod(self.socket_mode)
            self.socket_identity = (path.stat().st_dev, path.stat().st_ino)
            self.listener.listen(32)
            self.listener.setblocking(False)
            self.selector.register(self.listener, selectors.EVENT_READ, None)
            write_json_file(self.root / 'session.json', self.observations.retained())
        except BaseException:
            self.close()
            raise

    def publish(self, value):
        changed = self.observations.publish(value)
        if changed:
            write_json_file(self.root / 'session.json', self.observations.retained())
        return changed

    def _drop(self, connection):
        self.clients.pop(connection, None)
        try:
            self.selector.unregister(connection)
        except (KeyError, ValueError):
            pass
        connection.close()

    def _accept(self):
        connection, _ = self.listener.accept()
        try:
            uid = peer_uid(connection)
            if not self.authorized(uid) or len(self.clients) >= self.max_clients or not self.admit_connection(uid):
                self.metrics['rejected_peers'] += 1
                connection.close()
                return
            connection.setblocking(False)
            client = {'decoder': C.decoder(request=True), 'queue': deque(), 'queued': 0, 'last': time.monotonic(), 'last_write': time.monotonic(), 'tokens': 4., 'rate_updated': time.monotonic(), 'uid': uid}
            self.clients[connection] = client
            self.selector.register(connection, selectors.EVENT_READ, client)
            self.metrics['accepted'] += 1
        except BaseException:
            self._drop(connection)
            raise

    def authorized(self, uid):
        return uid == os.getuid()

    def admit_connection(self, uid):
        """Additional service-specific admission; selector thread owns clients."""
        return True

    def query(self, client, value):
        return self.observations.query(value)

    def encode_response(self, client, result):
        """Serialize each immutable publication once, separately from its age/cursor."""
        snapshot = result.get("snapshot")
        if snapshot is None:
            return C.message(result)
        key = (client["uid"], result.get("session"), result.get("epoch"), result.get("sequence"), id(snapshot))
        raw_snapshot = self._snapshot_wire.get(key)
        if raw_snapshot is None:
            raw_snapshot = C.encoded(snapshot)
            # Keep one publication per principal; never retain unbounded old
            # sequences or a second private projection for the same principal.
            for old in tuple(self._snapshot_wire):
                if old[0] == client["uid"]:
                    self._snapshot_wire_bytes -= len(self._snapshot_wire.pop(old))
            while self._snapshot_wire and self._snapshot_wire_bytes + len(raw_snapshot) > MAX_TOTAL_QUEUED // 2:
                old = next(iter(self._snapshot_wire))
                self._snapshot_wire_bytes -= len(self._snapshot_wire.pop(old))
            if len(raw_snapshot) <= MAX_TOTAL_QUEUED // 2:
                self._snapshot_wire[key] = raw_snapshot
                self._snapshot_wire_bytes += len(raw_snapshot)
        metadata = C.encoded({key: value for key, value in result.items() if key != "snapshot"})
        body = metadata[:-1] + b',"snapshot":' + raw_snapshot + b'}'
        return C.frame(body, C.POLICY)

    def admit_read(self, client, now):
        client['read_requested_at'] = now
        # Divide sustained capacity across requesting principals, then their
        # requesting connections. Idle sockets receive no share and cannot
        # throttle a healthy reader merely by remaining connected.
        active = [peer for peer in self.clients.values() if peer.get('read_requested_at', -float('inf')) >= now - 1. and self.authorized(peer['uid'])]
        by_uid = {}
        for peer in active:
            by_uid[peer['uid']] = by_uid.get(peer['uid'], 0) + 1
        rate = min(4., 32. / max(1, len(by_uid)) / max(1, by_uid.get(client['uid'], 1)))
        client['tokens'] = min(4., client['tokens'] + (now-client['rate_updated'])*rate)
        client['rate_updated'] = now
        self._rate_tokens = min(64., self._rate_tokens + (now-self._rate_updated)*32)
        self._rate_updated = now
        if client['tokens'] < 1 or self._rate_tokens < 1:
            raise ValueError('Session read quota reached; retry later without changing collection cadence')
        client['tokens'] -= 1
        self._rate_tokens -= 1

    def _read(self, connection, client):
        data = connection.recv(C.MAX_REQUEST)
        if not data:
            self._drop(connection)
            return
        client['last'] = time.monotonic()
        client['decoder'].feed(data)
        self._drain(connection, client)

    def _drain(self, connection, client):
        """One bounded batch; complete retained frames need no new socket bytes."""
        if client['queue']:
            return
        for value in client['decoder'].take(1):
            now = time.monotonic()
            try:
                if not self.authorized(client['uid']):
                    raise ValueError('Session visibility was revoked')
                self.admit_read(client, now)
                result = self.query(client, value)
            except ValueError as exc:
                result = {'schema': C.SCHEMA, 'version': C.VERSION, 'error': str(exc)}
            raw = self.encode_response(client, result)
            if len(raw) > MAX_QUEUED:
                self.metrics['slow_clients'] += 1
                self._drop(connection)
                return
            while (sum(item['queued'] for item in self.clients.values()) + len(raw) > MAX_TOTAL_QUEUED
                   or sum(item['queued'] for item in self.clients.values() if item['uid'] == client['uid']) + len(raw) > MAX_TOTAL_QUEUED // 2):
                uid_full = sum(item['queued'] for item in self.clients.values() if item['uid'] == client['uid']) + len(raw) > MAX_TOTAL_QUEUED // 2
                candidates = [(peer, item) for peer, item in self.clients.items()
                              if item['queued'] and (not uid_full or item['uid'] == client['uid'])]
                if not candidates:
                    self.metrics['slow_clients'] += 1
                    self._drop(connection)
                    return
                peer, _ = max(candidates, key=lambda value: value[1]['queued'])
                self.metrics['slow_clients'] += 1
                self._drop(peer)
                if peer is connection:
                    return
            client['queue'].append(memoryview(raw))
            client['queued'] += len(raw)
            client['last_write'] = time.monotonic()
            self.metrics['queries'] += 1
        self.selector.modify(connection, selectors.EVENT_WRITE if client['queue'] else selectors.EVENT_READ, client)

    def poll(self, timeout=.5):
        # Each peer gets one finite drain before waiting. Partial frames do not
        # wake the selector; a complete retained frame must make progress even
        # when the peer has sent all its bytes already.
        buffered = False
        peers = list(self.clients.items())
        offset = self._read_cursor % max(1, len(peers))
        self._read_cursor += 1
        for connection, client in peers[offset:] + peers[:offset]:
            if client['queue'] or not client['decoder'].has_complete_frame:
                continue
            try:
                self._drain(connection, client)
                buffered |= connection in self.clients and not client['queue'] and client['decoder'].has_complete_frame
            except (OSError, ValueError):
                self._drop(connection)
        ready = self.selector.select(0 if buffered else timeout)
        offset = self._read_cursor % max(1, len(ready))
        for key, mask in ready[offset:] + ready[:offset]:
            connection, client = key.fileobj, key.data
            try:
                if client is None:
                    self._accept()
                    continue
                if mask & selectors.EVENT_READ:
                    self._read(connection, client)
                if connection in self.clients and mask & selectors.EVENT_WRITE:
                    for written in write_pending(client['queue'], connection.send, zero_is_error=True):
                        client['queued'] -= written
                        client['last_write'] = time.monotonic()
                    self.selector.modify(connection, selectors.EVENT_WRITE if client['queue'] else selectors.EVENT_READ, client)
            except (OSError, ValueError):
                if client is not None:
                    self._drop(connection)
        now = time.monotonic()
        for connection, client in tuple(self.clients.items()):
            if now - client['last'] > 30 or client['queue'] and now - client['last_write'] > 10:
                self._drop(connection)

    def close(self):
        if self.closed:
            return
        self.closed = True
        for connection in tuple(self.clients):
            self._drop(connection)
        if self.listener is not None:
            self.listener.close()
        self.selector.close()
        path = self.root / SOCKET
        try:
            info = path.lstat()
            if (info.st_dev, info.st_ino) == getattr(self, 'socket_identity', None):
                path.unlink()
        except FileNotFoundError:
            pass
        self.owner.close()


class Client:
    def __init__(self, directory, *, timeout=5):
        self.root = private_root(directory)
        self.timeout = timeout
        self.expected_server_uid = os.getuid()
        self.session = self.epoch = ''
        self.cursor = 0

    def query(self, operation='events'):
        # Each exchange reconnects independently. There is no mutation replay;
        # request frequency does not trigger collection or reset its deadlines.
        deadline = time.monotonic() + self.timeout
        def remaining():
            budget = deadline - time.monotonic()
            if budget <= 0:
                raise TimeoutError('Session exchange exceeded its absolute timeout; retained client state is unchanged')
            return budget
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(remaining())
            connection.connect(str(self.root / SOCKET))
            if peer_uid(connection) != self.expected_server_uid:
                raise ValueError('Session server has a different local owner')
            connection.settimeout(remaining())
            connection.sendall(C.message(C.request(operation, session=self.session, epoch=self.epoch, cursor=self.cursor), request=True))
            decoder = C.decoder()
            while True:
                connection.settimeout(remaining())
                block = connection.recv(65536)
                remaining()
                if not block:
                    raise ConnectionError('Session disconnected before its response; retained client state is unchanged')
                decoder.feed(block)
                values = decoder.take(1)
                if values:
                    remaining()
                    return self.accept(values[0])

    def accept(self, value):
        if value.get('schema') != C.SCHEMA or value.get('version') != C.VERSION:
            raise ValueError('Session protocol version mismatch')
        if 'error' in value:
            raise ValueError(value['error'])
        C.validate_response(value)
        if self.session and value.get('session') != self.session:
            raise ValueError('Session owner identity changed; explicit reconnect required')
        self.session, self.epoch, self.cursor = value['session'], value['epoch'], value['sequence']
        return value


class RemoteClient(Client):
    """Installed-only SSH bootstrap; no source shipping or automatic install."""
    def __init__(self, directory, backend, *, python='python3'):
        self.remote_directory, self.backend, self.python = str(directory), backend, python
        self.session = self.epoch = ''
        self.cursor = 0

    def query(self, operation='events'):
        from .operation_support import _backend_command
        request = C.request(operation, session=self.session, epoch=self.epoch, cursor=self.cursor)
        text = _backend_command(self.backend, [self.python, '-m', 'raven.session_service', 'bridge', self.remote_directory,
                                             '--request', C.encoded(request).decode()], 10, C.MAX_RESPONSE, None)
        return self.accept(C.loads(text))


def _managed_session(directory, config=None, site=None, *, demo=False):
    from . import cli
    from .configuration.dashboard import Config
    from .configuration.policy import resolve, to_raven_config
    resolved = resolve(config, site=site, launcher='light', cli_overrides={'storage': {'state_dir': str(directory / 'state')}})
    cfg = Config(to_raven_config(resolved))
    args = cli.parse(['--no-plugins', '--no-gpu'] + (['--fake', '--no-state'] if demo else []))
    session = cli.build(args, cfg)
    return session, {'user': session.user, 'profile': resolved.data['profile'], 'connection': resolved.data['connection'], 'demo': demo}


def serve(directory, *, config=None, site=None, demo=False, cancel=None):
    root = private_root(directory, create=True)
    session = server = None
    cancel = cancel or threading.Event()
    try:
        # Acquire the service owner before constructing any collection owner.
        # Scope is resolved first without starting threads or scheduler work.
        from .configuration.policy import resolve
        resolved = resolve(config, site=site, launcher='light')
        scope = {'uid': os.getuid(), 'profile': resolved.data['profile'], 'connection': resolved.data['connection'], 'demo': demo}
        server = Server(root, scope)
        session, _ = _managed_session(root, config, site, demo=demo)
        session.sampler.start()
        deadline = time.monotonic()
        while not cancel.is_set():
            now = time.monotonic()
            if now >= deadline:
                value = observation(session.store)
                # A restart's empty Store is not a successful fresh observation.
                # Retain the previous context until at least one source finishes.
                if any(health.get('last_ok') for health in value['health'].values()):
                    try:
                        server.publish(value)
                    except ValueError as exc:
                        server.observations.stale = True
                        server.observations.error = str(exc)
                deadline = now + 1
            server.poll(min(.5, max(0., deadline-time.monotonic())))
    finally:
        if session is not None:
            session.close()
        if server is not None:
            server.close()


class LatestReader:
    """One in-flight read and one latest result; the UI never waits on IPC."""
    def __init__(self, client):
        from .ui_wakeup import UIWakeup
        self.client = client
        self.wakeup = UIWakeup()
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.result, self.error = None, ''
        self.thread = threading.Thread(target=self._run, daemon=True, name='raven-session-reader')
        self.thread.start()

    def _run(self):
        while not self.stop.is_set():
            try:
                result, error = self.client.query(), ''
            except (OSError, ValueError, RuntimeError) as exc:
                result, error = None, str(exc)
            with self.lock:
                if result is not None:
                    self.result = result
                self.error = error
            self.wakeup.notify()
            self.stop.wait(1)

    def latest(self):
        with self.lock:
            return self.result, self.error

    def close(self):
        self.stop.set()
        # The sole daemon reader owns its bounded transport timeout. Closing a
        # UI never blocks on a remote process or creates a replacement reader.
        self.wakeup.close()


def _terminal(client):
    import curses
    from .ui_wakeup import UIWaiter
    def draw(window):
        reader = LatestReader(client)
        waiter = UIWaiter(sys.stdin, reader.wakeup)
        try:
            _terminal_loop(window, reader, waiter)
        finally:
            waiter.close()
            reader.close()
    curses.wrapper(draw)


def _terminal_loop(window, reader, waiter):
    import curses
    offset = 0
    while True:
        retained, error = reader.latest()
        window.erase()
        height, width = window.getmaxyx()
        lines = ['Raven session — read-only | q quit', error or ('Connecting…' if retained is None else
                 f"Session {retained['session'][:8]} · update {retained['sequence']} · published {max(0., time.time()-retained['observed_at']):.1f}s ago")]
        if retained:
            snapshot = retained['snapshot']
            authorization = snapshot.get('authorization')
            if authorization:
                lines.append('Visibility: own login '+authorization['user']+
                             ('; site' if authorization['site'] else '')+
                             ('; accounts '+', '.join(authorization['accounts']) if authorization['accounts'] else '')+
                             ('; projects '+', '.join(authorization['projects']) if authorization['projects'] else ''))
            observed = snapshot.get('queue_observed_at', 0)
            lines.append('Queue observation: ' + (f'{max(0., time.time()-observed):.1f}s ago' if observed else 'unavailable'))
            queue_error = snapshot.get('health', {}).get('jobs', {}).get('error', '')
            if queue_error:
                lines.append('Queue read error: ' + str(queue_error))
            for source, source_error in snapshot.get('source_errors', {}).items():
                if source_error:
                    lines.append(source.title()+' source: '+source_error)
            if retained['service_error']:
                lines.append('Service error: ' + retained['service_error'])
            if retained['gap']:
                lines.append('Replay gap: ' + retained['gap']['reason'])
            if retained['stale']:
                lines.append('Retained observations are stale; awaiting collection.')
            for kind in ('jobs', 'finished'):
                lines.append(kind.title())
                for job in retained['snapshot'].get(kind, []):
                    projects = ' ['+', '.join(job['projects'])+']' if job.get('projects') else ''
                    lines.append(f"{str(job.get('id', '')):>10}  {str(job.get('state', '')):<14}  {job.get('name', '')}"+projects)
        header, content = lines[:2], lines[2:]
        page = max(1, height-3)
        offset = max(0, min(offset, max(0, len(content)-page)))
        for row, line in enumerate((header + content[offset:offset+page])[:max(0, height-1)]):
            try:
                window.addnstr(row, 0, line.replace('\n', ' ').replace('\r', ' ').replace('\t', ' '), max(0, width-1))
            except curses.error:
                pass
        window.refresh()
        window.timeout(0 if waiter.available else 100)
        key = window.getch()
        if key < 0 and waiter.available:
            waiter.wait(1)
            key = window.getch()
        if key in (ord('q'), 27):
            return
        if key in (curses.KEY_DOWN, ord('j')):
            offset += 1
        elif key in (curses.KEY_UP, ord('k')):
            offset -= 1
        elif key == curses.KEY_NPAGE:
            offset += page
        elif key == curses.KEY_PPAGE:
            offset -= page


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('serve', 'query', 'watch', 'tui', 'bridge'))
    parser.add_argument('directory', help='Private host-local session directory; choose a short path')
    parser.add_argument('--config')
    parser.add_argument('--site')
    parser.add_argument('--demo', action='store_true', help='Explicit simulated sampler, never live qualification')
    parser.add_argument('--request', help=argparse.SUPPRESS)
    parser.add_argument('--ssh-host', help='Connect to an already installed remote Raven service')
    parser.add_argument('--ssh-user', default='')
    args = parser.parse_args(argv)
    try:
        if args.action == 'serve':
            if args.ssh_host:
                raise ValueError('Start the service explicitly on its host; remote clients never start collectors')
            stop = threading.Event()
            previous = {signum: signal.signal(signum, lambda *_: stop.set()) for signum in (signal.SIGINT, signal.SIGTERM)}
            try:
                serve(args.directory, config=args.config, site=args.site, demo=args.demo, cancel=stop)
            finally:
                for signum, handler in previous.items():
                    signal.signal(signum, handler)
            return 0
        if args.ssh_host:
            from .remote import SshBackend
            client = RemoteClient(args.directory, SshBackend(args.ssh_host, args.ssh_user))
        else:
            client = Client(args.directory)
        if args.action == 'bridge':
            if not args.request or args.ssh_host:
                raise ValueError('The installed bridge requires one bounded local request')
            if len(args.request.encode()) > C.MAX_REQUEST:
                raise ValueError('Bridge request exceeds its bound')
            value = C.loads(args.request)
            C.validate_request(value)
            client.session, client.epoch, client.cursor = value['session'], value['epoch'], value['cursor']
            print(C.encoded(client.query(value['op'])).decode())
        elif args.action == 'tui':
            _terminal(client)
        elif args.action == 'watch':
            while True:
                try:
                    print(C.encoded(client.query()).decode(), flush=True)
                except (OSError, ValueError) as exc:
                    print(json.dumps({'schema': C.SCHEMA, 'error': str(exc), 'retained_cursor': client.cursor}), flush=True)
                threading.Event().wait(1)
        else:
            print(json.dumps(client.query('snapshot'), indent=2))
        return 0
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, RuntimeError) as exc:
        print('Raven session: ' + str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
