"""Real HTTP/TLS fixtures, exact attempt binding and bounded site telemetry."""
import copy
import hashlib
import http.server
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from tower import prometheus_source as T, ops_platform as OPS, operations as O

IDENTITY = {'cluster': 'alpha', 'job_id': '123_4', 'attempt': 'submitted/started'}


def manifest(endpoint):
    return {'schema': T.SCHEMA, 'id': 'site-metrics', 'endpoint': endpoint, 'query': 'job_gpu_percent{job="123_4"}',
            'identity': IDENTITY, 'labels': {'cluster': 'cluster', 'job_id': 'slurm_job_id', 'attempt': 'tower_attempt'},
            'metric': {'name': 'GPU utilization', 'unit': 'percent', 'semantics': 'gauge'},
            'start': 10, 'end': 11, 'step': .5, 'timeout': 3, 'max_bytes': 200000, 'auth': {'kind': 'none'}, 'allow_http': True}


def response():
    return {'status': 'success', 'data': {'resultType': 'matrix', 'result': [
        {'metric': {'cluster': 'alpha', 'slurm_job_id': '123_4', 'tower_attempt': 'submitted/started', 'gpu_uuid': 'GPU-example'},
         'values': [[10, '50'], [10.5, 'NaN'], [11, '70']]}]}}


@pytest.fixture
def server():
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.server.requests.append({'path': self.path, 'authorization': self.headers.get('Authorization')})
            if self.server.delay:
                time.sleep(self.server.delay)
            self.send_response(self.server.code)
            if self.server.location:
                self.send_header('Location', self.server.location)
            payload = self.server.body
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass
        def log_message(self, *args):
            pass
    server_obj = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server_obj.requests, server_obj.body, server_obj.code, server_obj.delay, server_obj.location = [], json.dumps(response()).encode(), 200, 0, None
    thread = threading.Thread(target=server_obj.serve_forever, daemon=True)
    thread.start()
    try:
        yield server_obj
    finally:
        server_obj.shutdown()
        server_obj.server_close()
        thread.join(2)


def endpoint(server, scheme='http'):
    return f'{scheme}://127.0.0.1:{server.server_address[1]}'


def test_actual_http_query_exact_identity_missing_values_and_no_secret_result(server):
    doc = manifest(endpoint(server))
    result = T.collect(doc)
    assert [row['data']['value'] for row in result['records']] == [50, None, 70]
    assert result['coverage']['mode'] == 'samples'
    assert result['coverage']['complete'] is None
    assert '/api/v1/query_range?' in server.requests[0]['path']
    assert server.requests[0]['authorization'] is None
    assert 'auth' not in json.dumps(result)


@pytest.mark.parametrize('field', ['cluster', 'slurm_job_id', 'tower_attempt'])
def test_every_series_requires_exact_attempt_labels(field):
    data = response()
    data['data']['result'][0]['metric'][field] = 'other'
    with pytest.raises(ValueError, match='identity label mismatch'):
        T.normalize(data, manifest('http://127.0.0.1:9999'))


@pytest.mark.parametrize('values', [[[10, 'x']], [[10, '1'], [10, '2']], [[12, '1']], [[10, 1]], [[10, '1e999']], [[11, '1'], [10, '2']]])
def test_bad_measurements_rejected(values):
    data = response()
    data['data']['result'][0]['values'] = values
    with pytest.raises(ValueError):
        T.normalize(data, manifest('http://127.0.0.1:9999'))


def test_duplicate_series_and_native_histogram_unsupported():
    data = response()
    data['data']['result'] *= 2
    with pytest.raises(ValueError, match='Duplicate'):
        T.normalize(data, manifest('http://127.0.0.1:9999'))
    data = response()
    data['data']['result'][0]['histograms'] = []
    with pytest.raises(ValueError, match='fields'):
        T.normalize(data, manifest('http://127.0.0.1:9999'))


@pytest.mark.parametrize('change', [
    {'endpoint': 'https://user:password@example.invalid'}, {'endpoint': 'file:///tmp/data'},
    {'endpoint': 'https://example.invalid?token=secret'}, {'endpoint': 'http://example.invalid', 'allow_http': False},
    {'end': 0}, {'step': 0}, {'timeout': 1000}, {'max_bytes': 1},
    {'labels': {'cluster': 'x', 'job_id': 'x', 'attempt': 'x'}},
    {'auth': {'kind': 'bearer_env', 'source': 'TOKEN'}}])
def test_manifest_limits_and_auth_transport(change):
    doc = manifest('http://127.0.0.1:9999')
    doc.update(change)
    with pytest.raises(ValueError):
        T.validate(doc)


def test_no_redirect_following_and_response_byte_budget(server):
    server.code, server.location = 302, 'http://127.0.0.1:1/should-not-contact'
    with pytest.raises(ValueError, match='redirects'):
        T.collect(manifest(endpoint(server)))
    assert len(server.requests) == 1
    server.code, server.location = 200, None
    doc = manifest(endpoint(server))
    doc['max_bytes'] = 128
    with pytest.raises(ValueError, match='Content-Length'):
        T.collect(doc)


def test_credential_file_permissions_and_unknown_environment(tmp_path, monkeypatch):
    token = tmp_path / 'token'
    token.write_text('example-token\n')
    token.chmod(0o644)
    with pytest.raises(ValueError, match='private'):
        T._credential({'kind': 'bearer_file', 'source': str(token)})
    token.chmod(0o600)
    assert T._credential({'kind': 'bearer_file', 'source': str(token)}) == 'example-token'
    monkeypatch.delenv('TOWER_MISSING_TOKEN', raising=False)
    with pytest.raises(ValueError, match='missing'):
        T._credential({'kind': 'bearer_env', 'source': 'TOWER_MISSING_TOKEN'})
    link = tmp_path / 'link'
    link.symlink_to(token)
    with pytest.raises(ValueError, match='symlink'):
        T._credential({'kind': 'bearer_file', 'source': str(link)})


def test_cancel_prevents_network_and_cancelled_read_is_bounded(server):
    stop = threading.Event()
    stop.set()
    with pytest.raises(ValueError, match='cancelled'):
        T.collect(manifest(endpoint(server)), cancel=stop)
    assert server.requests == []
    stop.clear()
    server.delay = .25
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(T.collect, manifest(endpoint(server)), cancel=stop)
        time.sleep(.05)
        stop.set()
        with pytest.raises(ValueError, match='cancelled'):
            future.result(2)


def test_opt_in_source_cache_policy_and_inspect_without_query(server, tmp_path):
    doc = manifest(endpoint(server))
    doc['policy'] = {'schema': 'tower.site-policy/v1', 'id': 'site', 'revision': '1', 'sources': ['site-metrics'], 'operations': ['query-range'],
                     'allocation_required': True, 'allow_intrusive': False,
                     'limits': {'min_interval': .5, 'max_duration': 5, 'max_bytes': 500000, 'max_records': 20000, 'max_inflight': 2}}
    doc['cache_directory'] = str(tmp_path / 'cache')
    path = tmp_path / 'query.json'
    path.write_text(json.dumps(doc))
    ctx = O.Context(scope={'cluster': 'alpha'})
    OPS.run('site-telemetry', {'manifest': str(path), 'action': 'inspect'}, ctx)
    assert server.requests == []
    a = OPS.run('site-telemetry', {'manifest': str(path), 'action': 'query'}, ctx)
    b = OPS.run('site-telemetry', {'manifest': str(path), 'action': 'query'}, ctx)
    assert a['data'] == b['data'] and len(server.requests) == 1
    doc['policy']['allow_intrusive'] = 'yes'
    with pytest.raises(ValueError):
        T.collect(doc)


def test_real_tls_bearer_and_redirect_credential_not_forwarded(server, tmp_path, monkeypatch):
    openssl = shutil.which('openssl')
    if not openssl:
        pytest.skip('OpenSSL CLI unavailable for ephemeral TLS fixture')
    key, cert = tmp_path / 'key.pem', tmp_path / 'cert.pem'
    subprocess.run([openssl, 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1', '-keyout', str(key), '-out', str(cert),
                    '-subj', '/CN=127.0.0.1', '-addext', 'subjectAltName=IP:127.0.0.1'], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert, key)
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    doc = manifest(endpoint(server, 'https'))
    doc['ca_file'] = str(cert)
    doc['auth'] = {'kind': 'bearer_env', 'source': 'TOWER_PROM_TOKEN'}
    monkeypatch.setenv('TOWER_PROM_TOKEN', 'test-private-bearer')
    result = T.collect(doc)
    assert server.requests[-1]['authorization'] == 'Bearer test-private-bearer'
    assert 'test-private-bearer' not in json.dumps(result)
    server.code, server.location = 302, 'http://127.0.0.1:1/steal'
    with pytest.raises(ValueError, match='redirects') as exc:
        T.collect(doc)
    assert 'test-private-bearer' not in str(exc.value)


def test_transport_hard_deadline_reaps_stalled_dns_or_tls_child(monkeypatch):
    original = T.subprocess.Popen
    children = []
    def stalled(argv, **kwargs):
        child = original([argv[0], '-I', '-S', '-c', 'import time; time.sleep(10)'], **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(T.subprocess, 'Popen', stalled)
    doc = manifest('http://127.0.0.1:1')
    doc['timeout'] = .1
    start = time.monotonic()
    with pytest.raises(ValueError, match='deadline'):
        T.collect(doc)
    assert time.monotonic() - start < 1.5
    assert children[0].poll() is not None
