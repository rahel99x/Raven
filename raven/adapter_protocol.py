"""Explicit external adapter processes with bounded NDJSON framing and deadlines.

Adapters are installed executable code, not a security sandbox. Inspection of a
manifest never starts it. Each request owns a process group; cancellation first
sends a protocol message, then reaps the group after a short grace period.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import selectors
import signal
import stat
import subprocess
import time
import uuid

from . import evidence as E

SCHEMA = "raven.adapter/v1"
PROTOCOL = "raven.adapter-protocol/v1"
MAX_FRAME = 1 << 20
MAX_TOTAL = 8 << 20
METHODS = {"describe", "validate", "run"}


def _asset(path, expected=None):
    E.text(path, "adapter asset path", 4096)
    if not os.path.isabs(path):
        raise ValueError("Adapter executable and asset paths must be absolute")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > 128 << 20:
            raise ValueError("Adapter assets must be bounded regular files (no symlinks)")
        digest = hashlib.sha256()
        while True:
            chunk = stream.read(1 << 20)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(stream.fileno())
        fingerprint = lambda st: (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
        if fingerprint(before) != fingerprint(after) or fingerprint(after) != fingerprint(os.stat(path, follow_symlinks=False)):
            raise ValueError("Adapter asset changed during inspection")
    value = digest.hexdigest()
    if expected is not None and value != expected:
        raise ValueError("Adapter asset hash changed: " + path)
    return {"path": path, "sha256": value, "size": before.st_size}


def validate_manifest(value, *, verify_assets=False):
    E.canonical(value)
    E.keys(value, {"schema", "id", "version", "protocol", "argv", "environment", "methods", "assets", "limits"}, {"description", "permissions"}, "adapter manifest")
    if value["schema"] != SCHEMA or value["protocol"] != PROTOCOL:
        raise ValueError("Unsupported adapter manifest/protocol version")
    E.text(value["id"], "adapter id", 256)
    E.text(value["version"], "adapter version", 256)
    argv = value["argv"]
    if not isinstance(argv, list) or not 1 <= len(argv) <= 64:
        raise ValueError("Adapter argv must have 1..64 arguments")
    for arg in argv:
        E.text(arg, "adapter argument", 4096, empty=True)
    if not os.path.isabs(argv[0]):
        raise ValueError("Adapter executable must be an absolute path")
    methods = value["methods"]
    if not isinstance(methods, list) or not methods or any(type(m) is not str or m not in METHODS for m in methods) or len(set(methods)) != len(methods):
        raise ValueError("Adapter methods must explicitly name describe, validate or run")
    env = value["environment"]
    if not isinstance(env, dict) or len(env) > 32:
        raise ValueError("Adapter environment must be a bounded explicit mapping")
    for key, item in env.items():
        if not isinstance(key, str) or not key.replace("_", "A").isalnum() or not key or key[0].isdigit():
            raise ValueError("Invalid adapter environment variable name")
        E.text(item, "environment value", 4096, empty=True)
    assets = value["assets"]
    if not isinstance(assets, list) or not 1 <= len(assets) <= 128:
        raise ValueError("Adapter needs pinned executable/assets")
    paths, total_asset_bytes = set(), 0
    for asset in assets:
        E.keys(asset, {"path", "sha256"}, label="adapter asset")
        E.text(asset["path"], "asset path", 4096)
        if not os.path.isabs(asset["path"]) or not isinstance(asset["sha256"], str) or not E.HASH.fullmatch(asset["sha256"]) or asset["path"] in paths:
            raise ValueError("Invalid or duplicate pinned adapter asset")
        paths.add(asset["path"])
        if verify_assets:
            total_asset_bytes += _asset(asset["path"], asset["sha256"])["size"]
            if total_asset_bytes > 256 << 20:
                raise ValueError("Pinned adapter assets exceed the 256 MiB total bound")
    if argv[0] not in paths:
        raise ValueError("The executable must be included in pinned assets")
    if verify_assets:
        for argument in argv[1:]:
            if os.path.isabs(argument) and os.path.isfile(argument) and argument not in paths:
                raise ValueError("Existing absolute file arguments must be included in pinned adapter assets")
    limits = E.keys(value["limits"], {"timeout", "frame_bytes", "total_bytes", "stderr_bytes"}, label="adapter limits")
    E.finite(limits["timeout"], "adapter timeout", .01)
    if limits["timeout"] > 120:
        raise ValueError("Adapter timeout exceeds 120 seconds")
    for key, maximum in (("frame_bytes", MAX_FRAME), ("total_bytes", MAX_TOTAL), ("stderr_bytes", 65536)):
        E.integer(limits[key], key, maximum)
        if limits[key] < 1:
            raise ValueError("Adapter byte limits must be positive")
    if limits["frame_bytes"] > limits["total_bytes"]:
        raise ValueError("Frame limit cannot exceed total limit")
    if "permissions" in value:
        if not isinstance(value["permissions"], list) or len(value["permissions"]) > 32:
            raise ValueError("Adapter permissions must be a bounded list of declarations")
        for item in value["permissions"]:
            E.text(item, "permission declaration")
    # Copy through strict JSON to detach the caller's mutable manifest.
    return E.loads(E.canonical(value))


class AdapterClient:
    def __init__(self, manifest):
        self.manifest = validate_manifest(manifest)

    def request(self, method, params=None, *, cancel=None):
        if type(method) is not str or method not in self.manifest["methods"]:
            raise ValueError("Adapter method is not declared")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise ValueError("Adapter request parameters must be an object")
        if cancel is not None and cancel.is_set():
            raise ValueError("Adapter request cancelled before execution")
        manifest = validate_manifest(self.manifest, verify_assets=True)
        request_id = uuid.uuid4().hex
        request = E.canonical({"schema": PROTOCOL, "id": request_id, "method": method, "params": params}) + b"\n"
        limits = manifest["limits"]
        if len(request) > limits["frame_bytes"]:
            raise ValueError("Adapter request exceeds its frame budget")
        env = {"PATH": os.defpath, "LANG": "C.UTF-8", **manifest["environment"]}
        process = subprocess.Popen(manifest["argv"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=True, env=env, cwd=str(Path(manifest["argv"][0]).parent))
        response, total, error_size = None, 0, 0
        buffer, pending = bytearray(), bytearray(request)
        deadline = time.monotonic() + limits["timeout"]
        cancelled_at = None
        try:
            with selectors.DefaultSelector() as selector:
                for stream, event, name in ((process.stdout, selectors.EVENT_READ, "out"), (process.stderr, selectors.EVENT_READ, "err"), (process.stdin, selectors.EVENT_WRITE, "in")):
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, event, name)
                while selector.get_map():
                    now = time.monotonic()
                    if cancel is not None and cancel.is_set() and cancelled_at is None:
                        cancelled_at = now
                        # Never interleave a cancellation object into a partial
                        # request frame. Finish it, then append the cancel frame.
                        pending.extend(E.canonical({"schema": PROTOCOL, "id": request_id, "method": "cancel", "params": {}}) + b"\n")
                        if process.stdin and not process.stdin.closed:
                            try:
                                selector.register(process.stdin, selectors.EVENT_WRITE, "in")
                            except KeyError:
                                pass
                    if cancelled_at is not None and now - cancelled_at >= .2:
                        raise ValueError("Adapter request cancelled; process group reaped")
                    if now >= deadline:
                        raise ValueError("Adapter request timed out")
                    if response is not None:
                        if cancelled_at is not None:
                            raise ValueError("Adapter request cancelled")
                        if not process.stdin.closed:
                            try:
                                selector.unregister(process.stdin)
                            except KeyError:
                                pass
                            process.stdin.close()
                    for key, _ in selector.select(min(.02, deadline - now)):
                        if key.data == "in":
                            try:
                                written = os.write(key.fileobj.fileno(), pending[:65536])
                                del pending[:written]
                            except BrokenPipeError:
                                pending.clear()
                            if not pending:
                                selector.unregister(key.fileobj)
                            continue
                        chunk = os.read(key.fileobj.fileno(), 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        total += len(chunk)
                        if total > limits["total_bytes"]:
                            raise ValueError("Adapter output exceeds total budget")
                        if key.data == "err":
                            error_size += len(chunk)
                            if error_size > limits["stderr_bytes"]:
                                raise ValueError("Adapter stderr exceeds its bound")
                            continue  # Do not include arbitrary secrets in errors.
                        buffer.extend(chunk)
                        while b"\n" in buffer:
                            line, _, remainder = buffer.partition(b"\n")
                            buffer = bytearray(remainder)
                            if len(line) + 1 > limits["frame_bytes"]:
                                raise ValueError("Adapter frame exceeds its bound")
                            frame = E.loads(bytes(line))
                            E.keys(frame, {"schema", "id", "type"}, {"result", "error", "progress"}, "adapter response")
                            if frame["schema"] != PROTOCOL or frame["id"] != request_id:
                                raise ValueError("Adapter response version/request identity mismatch")
                            kind = frame["type"]
                            if response is not None:
                                raise ValueError("Duplicate or trailing adapter response after terminal result")
                            if kind == "progress" and set(frame) == {"schema", "id", "type", "progress"}:
                                E.finite(frame["progress"], "adapter progress", 0)
                                if frame["progress"] > 1:
                                    raise ValueError("Adapter progress exceeds one")
                            elif kind == "result" and set(frame) == {"schema", "id", "type", "result"}:
                                if response is not None or not isinstance(frame["result"], dict):
                                    raise ValueError("Duplicate or invalid terminal adapter response")
                                response = frame["result"]
                            elif kind == "error" and set(frame) == {"schema", "id", "type", "error"}:
                                E.text(frame["error"], "adapter error", 4096)
                                raise ValueError("Adapter reported an error: " + frame["error"])
                            else:
                                raise ValueError("Malformed adapter response")
                        if len(buffer) > limits["frame_bytes"]:
                            raise ValueError("Unterminated adapter frame exceeds its bound")
                if cancelled_at is not None:
                    raise ValueError("Adapter request cancelled")
                if response is None or buffer:
                    raise ValueError("Adapter exited without one complete result")
                try:
                    process.wait(timeout=max(.01, deadline - time.monotonic()))
                except subprocess.TimeoutExpired as exc:
                    raise ValueError("Adapter failed to exit after its result") from exc
                if process.returncode != 0:
                    raise ValueError("Adapter exited unsuccessfully")
                return response
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=2)
            for stream in (process.stdin, process.stdout, process.stderr):
                if not stream.closed:
                    stream.close()
