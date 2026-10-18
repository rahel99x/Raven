"""Optional, reviewed pure-analysis WASI subset; trusted Python stays separate."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import selectors
import struct
import subprocess
import sys
import tempfile
import time

from . import evidence as E

RUNTIME = '49.0.0'
ABI = 'raven.f64-summary/wasi-preview1-v1'
MAX_MODULE = 1 << 20
MAX_INPUT = 1 << 20
MAX_OUTPUT = 65536


def capability():
    try:
        version = importlib.metadata.version('wasmtime')
    except importlib.metadata.PackageNotFoundError:
        raise ValueError('Optional WASI runtime missing; install this Raven release with the wasi extra') from None
    if version != RUNTIME:
        raise ValueError('WASI qualification requires wasmtime==' + RUNTIME + '; found ' + version)
    return {'runtime': 'wasmtime', 'version': version, 'abi': ABI}


def manifest(value):
    E.keys(value, {'schema', 'module', 'sha256', 'abi', 'limits'}, label='WASI analysis manifest')
    if value['schema'] != 'raven.wasi-analysis/v1' or value['abi'] != ABI:
        raise ValueError('Unsupported WASI analysis schema or ABI')
    E.text(value['module'], 'module path', 4096)
    if not isinstance(value['sha256'], str) or not E.HASH.fullmatch(value['sha256']):
        raise ValueError('WASI module requires an exact SHA256')
    limits = E.keys(value['limits'], {'memory_bytes', 'fuel', 'wall_seconds', 'output_bytes'}, label='WASI limits')
    ranges = {'memory_bytes': (65536, 32 << 20), 'fuel': (1000, 100000000), 'wall_seconds': (1, 30), 'output_bytes': (32, MAX_OUTPUT)}
    for name, (minimum, maximum) in ranges.items():
        if type(limits[name]) is not int or not minimum <= limits[name] <= maximum:
            raise ValueError(f'WASI {name} must be an integer in {minimum}..{maximum}')
    return value


def input_bytes(value):
    E.keys(value, {'schema', 'values'}, label='WASI analysis input')
    values = value['values']
    if value['schema'] != 'raven.f64-series/v1' or not isinstance(values, list) or not 1 <= len(values) <= 65535:
        raise ValueError('Analysis requires 1..65535 finite numeric values')
    try:
        if any(type(number) not in (int, float) or not math.isfinite(number) for number in values):
            raise ValueError('Analysis values must be finite numbers, excluding booleans')
        if any(type(number) is int and float(number) != number for number in values):
            raise ValueError('Integer analysis values must be exactly representable as f64; explicit rounding belongs in the source program')
        raw = struct.pack('<I', len(values)) + struct.pack('<'+'d'*len(values), *values)
    except (OverflowError, struct.error) as exc:
        raise ValueError('Analysis values exceed the f64 ABI') from exc
    return raw


def inspect(value, module, data):
    value = manifest(value)
    if not isinstance(module, bytes) or not 8 <= len(module) <= MAX_MODULE or module[:8] != b'\0asm\x01\0\0\0':
        raise ValueError('Supply a compiled WebAssembly v1 module of at most 1 MiB')
    if hashlib.sha256(module).hexdigest() != value['sha256']:
        raise ValueError('WASI module differs from its reviewed SHA256')
    raw = input_bytes(data)
    return {**capability(), 'module_sha256': value['sha256'], 'input_sha256': hashlib.sha256(raw).hexdigest(),
            'samples': len(data['values']), 'limits': dict(value['limits']),
            'allowed_imports': ['wasi_snapshot_preview1.fd_read', 'wasi_snapshot_preview1.fd_write', 'wasi_snapshot_preview1.proc_exit'],
            'preopens': [], 'environment': [], 'network': False, 'clocks': False, 'randomness': False}


def execute(value, module, data, *, cancel=None):
    """Pass immutable reviewed bytes; parent independently bounds output/time."""
    reviewed = inspect(value, module, data)
    raw = input_bytes(data)
    if cancel is not None and cancel.is_set():
        raise RuntimeError('WASI analysis cancelled')
    limits = value['limits']
    with tempfile.TemporaryDirectory(prefix='raven-wasi-') as temporary:
        root = Path(temporary)
        module_file, input_file = root/'module.wasm', root/'input.bin'
        module_file.write_bytes(module)
        input_file.write_bytes(raw)
        with module_file.open('rb') as wasm, input_file.open('rb') as source:
            command = [sys.executable, '-I', str(Path(__file__).with_name('_wasi_runner.py')),
                       str(wasm.fileno()), str(source.fileno()), json.dumps(limits, separators=(',', ':'))]
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       pass_fds=(wasm.fileno(), source.fileno()),
                                       env={'PATH': os.defpath, 'LANG': 'C.UTF-8'})
            selector = selectors.DefaultSelector()
            buffers = {'stdout': bytearray(), 'stderr': bytearray()}
            deadline = time.monotonic() + limits['wall_seconds']
            try:
                for stream, name in ((process.stdout, 'stdout'), (process.stderr, 'stderr')):
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, selectors.EVENT_READ, name)
                while selector.get_map():
                    if cancel is not None and cancel.is_set():
                        raise RuntimeError('WASI analysis cancelled')
                    remaining = deadline-time.monotonic()
                    if remaining <= 0:
                        raise RuntimeError('WASI analysis exceeded its wall-time budget')
                    for key, _ in selector.select(min(.05, remaining)):
                        chunk = os.read(key.fd, 8192)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        target = buffers[key.data]
                        target.extend(chunk)
                        if sum(map(len, buffers.values())) > limits['output_bytes'] + 16384:
                            raise RuntimeError('WASI runner exceeded its parent output budget')
                remaining = max(.001, deadline-time.monotonic())
                process.wait(timeout=remaining)
                if process.returncode:
                    error = buffers['stderr'].decode('utf-8', 'replace')[:4096]
                    raise ValueError('WASI analysis refused or trapped: '+error.strip())
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError('WASI analysis exceeded its wall-time budget') from exc
            finally:
                if process.poll() is None:
                    # The guest cannot spawn processes. Keep the runner in
                    # the owned worker task group so outer cancellation also
                    # kills it if this parent is terminated abruptly.
                    process.kill()
                process.wait()
                selector.close()
                process.stdout.close()
                process.stderr.close()
    output = bytes(buffers['stdout'])
    if len(output) != 32:
        raise ValueError('WASI adapter must emit exactly 32 bytes for the declared summary ABI')
    count, total, minimum, maximum = struct.unpack('<Qddd', output)
    if count != len(data['values']) or any(not math.isfinite(number) for number in (total, minimum, maximum)) or minimum > maximum:
        raise ValueError('WASI adapter returned an invalid summary')
    # These are adapter claims, not independently certified science. The
    # reference fixture checks fidelity; arbitrary reviewed modules may lie.
    return {'schema': 'raven.wasi-analysis-result/v1', **reviewed,
            'output_sha256': hashlib.sha256(output).hexdigest(),
            'summary': {'count': count, 'sum': total, 'minimum': minimum, 'maximum': maximum},
            'scientific_validity_verified': False}
