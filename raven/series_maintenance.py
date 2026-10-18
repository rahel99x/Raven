"""Finite local process ownership for explicit storage maintenance actions."""
from __future__ import annotations
import json
import os
from pathlib import Path
import selectors
import secrets
import subprocess
import sys
import time

MAX_RESULT = 256 << 10
WALL_SECONDS = 20.


def run(store, request):
    """Caller holds Store's persistence I/O lock until this child is reaped."""
    if store._storage_cancel.is_set(): raise ValueError('Storage maintenance cancelled')
    request = dict(request, staging_token=secrets.token_hex(8))
    source = Path(request['source']).absolute()
    if source.parent != Path(store.state_dir).absolute() / 'series':
        raise ValueError('Local maintenance must remain inside its owned state/series directory')
    data = json.dumps(request, allow_nan=False).encode()
    if len(data) > 16384: raise ValueError('Storage maintenance request exceeds its bound')
    owner = getattr(store, '_state_owner', None)
    descriptor = owner.fd if owner is not None else None
    if request.get('apply') and (descriptor is None or not store.writable):
        raise ValueError('Storage mutation requires the original state writer ownership')
    # Inheriting the exact flock descriptor keeps the scope locked even if the
    # parent dies. The child has its own CPU/address-space/file/wall bounds.
    child = subprocess.Popen([sys.executable, '-m', 'raven.series_worker'],
        cwd=str(Path(__file__).resolve().parents[1]), stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
        pass_fds=(descriptor,) if descriptor is not None else ())
    selector = selectors.DefaultSelector(); output = bytearray(); errors = bytearray()
    deadline = time.monotonic() + WALL_SECONDS
    try:
        request_offset = 0
        os.set_blocking(child.stdin.fileno(), False)
        selector.register(child.stdin, selectors.EVENT_WRITE, None)
        for stream, target in ((child.stdout, output), (child.stderr, errors)):
            os.set_blocking(stream.fileno(), False); selector.register(stream, selectors.EVENT_READ, target)
        while selector.get_map():
            if store._storage_cancel.is_set(): raise ValueError('Storage maintenance cancelled; source and prior generation preserved')
            if time.monotonic() >= deadline: raise ValueError('Storage maintenance exceeded its 20-second wall budget')
            for key, _ in selector.select(.1):
                if key.data is None:
                    try:
                        request_offset += os.write(key.fd, memoryview(data)[request_offset:])
                    except BlockingIOError:
                        continue
                    except BrokenPipeError as exc:
                        raise ValueError('Storage child closed before receiving its request') from exc
                    if request_offset == len(data):
                        selector.unregister(key.fileobj); key.fileobj.close()
                    continue
                block = os.read(key.fileobj.fileno(), 65536)
                if not block: selector.unregister(key.fileobj); continue
                key.data.extend(block)
                if len(key.data) > MAX_RESULT: raise ValueError('Storage maintenance output exceeded its byte budget')
        status = child.wait(timeout=max(.01, deadline-time.monotonic()))
        if status: raise ValueError('Storage child failed or exceeded its resource budget: ' + errors.decode(errors='replace')[:2048])
        from .series_blocks import _json
        result = _json(bytes(output))
        if not isinstance(result, dict) or result.get('ok') is not True:
            raise ValueError(result.get('error', 'Malformed storage response') if isinstance(result, dict) else 'Malformed storage response')
        return result['value']
    finally:
        selector.close()
        if child.poll() is None: child.kill()
        child.wait()
        for stream in (child.stdin, child.stdout, child.stderr):
            if stream: stream.close()
        if request.get('apply'):
            suffix = '.rvb' if request['action'] == 'migrate' else '.rvt'
            staging = source.with_suffix(suffix).with_name(source.with_suffix(suffix).name + '.stage-' + request['staging_token'])
            try: staging.unlink()
            except FileNotFoundError: pass
