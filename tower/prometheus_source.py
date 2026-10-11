"""Explicit, bounded Prometheus HTTP JSON query_range connector.

No discovery, redirects, URL credentials, shell execution or implicit queries.
Authentication is loaded only when an explicitly requested collection executes.
Exact cluster/job/attempt labels are mandatory for every returned series.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import sys
import stat
import threading
import time
import urllib.parse

from . import evidence as E, site_policy as P
from .shared_collection import SharedQueryCache

SCHEMA = "tower.prometheus-query/v1"


def validate(value):
    E.canonical(value)
    E.keys(value, {"schema", "id", "endpoint", "query", "identity", "labels", "metric", "start", "end", "step", "timeout", "max_bytes", "auth"},
           {"allow_http", "ca_file", "policy", "cache_directory", "interval"}, "Prometheus query")
    if value["schema"] != SCHEMA:
        raise ValueError("Expected " + SCHEMA)
    E.text(value["id"], "Prometheus source id", 256)
    endpoint = E.text(value["endpoint"], "Prometheus endpoint", 4096)
    parsed = urllib.parse.urlsplit(endpoint)
    if parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise ValueError("Prometheus endpoint must be an explicit HTTP(S) base URL without credentials/query/fragment")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("Prometheus endpoint port is invalid") from exc
    if "allow_http" in value and type(value["allow_http"]) is not bool:
        raise ValueError("allow_http must be boolean")
    if parsed.scheme == "http" and not value.get("allow_http", False):
        raise ValueError("Plain HTTP requires explicit allow_http; HTTPS is the default")
    E.text(value["query"], "PromQL query", 8192)
    E.identity(value["identity"])
    labels = E.keys(value["labels"], {"cluster", "job_id", "attempt"}, label="Prometheus identity labels")
    for item in labels.values():
        if not isinstance(item, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", item):
            raise ValueError("Prometheus identity labels must be explicit label names")
    if len(set(labels.values())) != 3:
        raise ValueError("Cluster/job/attempt must use distinct identity labels")
    metric = E.keys(value["metric"], {"name", "unit", "semantics"}, label="Prometheus metric")
    E.text(metric["name"], "metric name", 512)
    if not isinstance(metric["unit"], str) or metric["unit"] not in E.UNITS or metric["semantics"] not in ("gauge", "counter", "rate"):
        raise ValueError("Metric requires a recognized unit and gauge/counter/rate semantics")
    for key in ("start", "end"):
        E.finite(value[key], key, 0)
    E.finite(value["step"], "query step", .001)
    if value["end"] < value["start"] or (value["end"] - value["start"]) / value["step"] + 1 > E.MAX_RECORDS:
        raise ValueError("Prometheus range is reversed or exceeds 20000 points per series")
    E.finite(value["timeout"], "query timeout", .05)
    if value["timeout"] > 60:
        raise ValueError("Prometheus timeout exceeds 60 seconds")
    E.integer(value["max_bytes"], "HTTP response bytes", 4 << 20)
    if value["max_bytes"] < 128:
        raise ValueError("Prometheus response budget must be at least 128 bytes")
    auth = value["auth"]
    if not isinstance(auth, dict) or auth.get("kind") not in ("none", "bearer_env", "bearer_file"):
        raise ValueError("Authentication must be none, bearer_env or bearer_file")
    E.keys(auth, {"kind"} if auth["kind"] == "none" else {"kind", "source"}, label="Prometheus authentication")
    if auth["kind"] != "none":
        E.text(auth["source"], "credential source", 4096)
        if auth["kind"] == "bearer_env" and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", auth["source"]):
            raise ValueError("Credential environment name is invalid")
        if parsed.scheme != "https":
            raise ValueError("Bearer authentication requires HTTPS")
    if "ca_file" in value:
        E.text(value["ca_file"], "CA file", 4096)
    if "policy" in value:
        P.validate(value["policy"])
    if value.get("cache_directory"):
        E.text(value["cache_directory"], "cache directory", 4096)
        if "policy" not in value:
            raise ValueError("Shared telemetry caching requires an explicit collection policy")
    E.finite(value.get("interval", .5), "collection interval", .001)
    return E.loads(E.canonical(value))


def _credential(auth):
    if auth["kind"] == "none":
        return None
    if auth["kind"] == "bearer_env":
        value = os.environ.get(auth["source"], "")
    else:
        path = Path(auth["source"]).expanduser().absolute()
        if any(parent.is_symlink() for parent in (path, *path.parents)):
            raise ValueError("Credential file path must not traverse symlinks")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_mode & 0o077 or before.st_size > 16384:
                raise ValueError("Credential file must be private, owned, regular and at most 16 KiB")
            raw = stream.read(16385)
            after = os.fstat(stream.fileno())
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns) or len(raw) > 16384:
                raise ValueError("Credential file changed during read")
            try:
                value = raw.decode("utf-8").strip()
            except UnicodeError as exc:
                raise ValueError("Credential must be UTF-8 text") from exc
    if not value or len(value) > 16384 or any(not char.isprintable() or char.isspace() for char in value):
        raise ValueError("Bearer credential is missing or malformed")
    return value


def _fetch(manifest, credential, cancel):
    # A socket timeout does not bound DNS resolution. Own a trusted HTTP worker
    # process so the wall-clock limit covers resolver/TLS/read stalls as well.
    payload = {key: manifest[key] for key in ("endpoint", "query", "start", "end", "step", "timeout", "max_bytes")}
    if "ca_file" in manifest:
        payload["ca_file"] = manifest["ca_file"]
    payload["credential"] = credential
    import json
    pending = bytearray(json.dumps(payload, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode() + b"\n")
    if len(pending) > 65536:
        raise ValueError("HTTP request metadata exceeds its 64 KiB bound")
    if cancel.is_set():
        raise ValueError("Telemetry query cancelled before network access")
    deadline = time.monotonic() + manifest["timeout"]
    process = subprocess.Popen([str(Path(sys.executable).resolve()), "-I", "-S", str(Path(__file__).with_name("prometheus_transport.py"))],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
                               env={"PATH": os.defpath, "LANG": "C.UTF-8"})
    output, errors = bytearray(), bytearray()
    try:
        with selectors.DefaultSelector() as selector:
            for stream, event, name in ((process.stdin, selectors.EVENT_WRITE, "in"), (process.stdout, selectors.EVENT_READ, "out"), (process.stderr, selectors.EVENT_READ, "err")):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, event, name)
            while selector.get_map():
                if cancel.is_set():
                    raise ValueError("Telemetry query cancelled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError("Telemetry query exceeded its wall-clock deadline")
                for key, _ in selector.select(min(.02, remaining)):
                    if key.data == "in":
                        try:
                            sent = os.write(key.fileobj.fileno(), pending[:65536])
                            del pending[:sent]
                        except BrokenPipeError:
                            pending.clear()
                        if not pending:
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                        continue
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    target, limit = (output, manifest["max_bytes"]) if key.data == "out" else (errors, 4096)
                    target.extend(chunk)
                    if len(target) > limit:
                        raise ValueError("Telemetry transport exceeded its output byte budget")
            process.wait(timeout=max(.01, deadline - time.monotonic()))
            if cancel.is_set():
                raise ValueError("Telemetry query cancelled")
            if process.returncode:
                # The worker emits only a fixed error vocabulary. Do not
                # surface arbitrary worker stderr, queries or credentials.
                code = errors.decode("ascii", "ignore").strip()
                if re.fullmatch(r"HTTP-[0-9]{3}", code):
                    raise ValueError("Prometheus HTTP status " + code[5:] + "; redirects are not followed")
                descriptions = {"LENGTH": "Telemetry Content-Length exceeds the response budget",
                                "LIMIT": "Telemetry response exceeds its byte budget",
                                "ENCODING": "Compressed HTTP telemetry responses are not supported"}
                raise ValueError(descriptions.get(code, "Prometheus connection/TLS/read failed within its bounded request"))
            return E.loads(bytes(output))
    except subprocess.TimeoutExpired as exc:
        raise ValueError("Telemetry worker exceeded its deadline") from exc
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=2)
        for stream in (process.stdin, process.stdout, process.stderr):
            if not stream.closed:
                stream.close()


def normalize(value, manifest):
    manifest = validate(manifest)
    E.canonical(value)
    if not isinstance(value, dict) or value.get("status") != "success" or not isinstance(value.get("data"), dict) or value["data"].get("resultType") != "matrix":
        raise ValueError("Prometheus response must be a successful query_range matrix")
    result = value["data"].get("result")
    if not isinstance(result, list) or len(result) > 1024:
        raise ValueError("Prometheus series count exceeds its bound")
    records, seen = [], set()
    for series in result:
        E.keys(series, {"metric", "values"}, label="Prometheus matrix series")
        labels = series["metric"]
        if not isinstance(labels, dict) or len(labels) > 128:
            raise ValueError("Prometheus series labels exceed their bound")
        for key, label in labels.items():
            E.text(key, "series label key", 128)
            E.text(label, "series label value", 1024, empty=True)
        for field, label in manifest["labels"].items():
            if labels.get(label) != manifest["identity"][field]:
                raise ValueError("Telemetry execution identity label mismatch; tighten the query to the exact job attempt")
        token = E.digest(labels)
        if token in seen:
            raise ValueError("Duplicate Prometheus series identity")
        seen.add(token)
        values = series["values"]
        if not isinstance(values, list) or len(values) + len(records) > E.MAX_RECORDS:
            raise ValueError("Telemetry points exceed the 20000-record budget")
        previous = None
        for index, point in enumerate(values):
            if not isinstance(point, list) or len(point) != 2 or not isinstance(point[1], str):
                raise ValueError("Prometheus points must be [numeric timestamp, string sample]")
            ts = E.finite(point[0], "Prometheus timestamp", 0)
            if not manifest["start"] <= ts <= manifest["end"] or previous is not None and ts <= previous:
                raise ValueError("Telemetry timestamps are outside the query range, duplicated or unordered")
            previous = ts
            missing = point[1] in ("NaN", "+Inf", "-Inf", "Inf")
            try:
                number = None if missing else float(point[1])
            except ValueError as exc:
                raise ValueError("Prometheus sample is not numeric") from exc
            if number is not None:
                E.finite(number, "Prometheus sample")
            records.append({"id": f"{token}:{index}", "kind": "metric", "data": {"name": manifest["metric"]["name"], "unit": manifest["metric"]["unit"], "value": number,
                            "time": ts, "attributes": {"labels": labels, "semantics": manifest["metric"]["semantics"], "quality": "nonfinite-source-sample" if missing else "observed"}}})
    warnings = value.get("warnings", [])
    if not isinstance(warnings, list) or len(warnings) > 64 or any(not isinstance(item, str) for item in warnings):
        raise ValueError("Prometheus warnings must be a bounded string list")
    return E.receipt(manifest["identity"], {"id": manifest["id"], "format": "prometheus-query-range-json", "version": "v1", "sha256": E.digest(value)}, records,
                     clock={"domain": "Unix epoch seconds reported by Prometheus", "unit": "s", "uncertainty": None},
                     coverage={"mode": "samples", "complete": None, "scope": ["explicit PromQL query_range", "exact cluster/job/attempt labels"], "gaps": [], "omitted": 0},
                     transforms=[{"name": "prometheus-query-range", "version": "1", "parameters": {"query": manifest["query"], "start": manifest["start"], "end": manifest["end"], "step": manifest["step"]}}],
                     metadata={"warnings": [*warnings, "Prometheus evaluation step does not establish exporter sampling cadence or lossless recording; nonfinite samples remain missing."], "source_warnings": bool(warnings)})


def collect(value, *, cancel=None):
    manifest = validate(value)
    stop = cancel if cancel is not None else threading.Event()
    if stop.is_set():
        raise ValueError("Telemetry query cancelled")
    credential = _credential(manifest["auth"])
    source_caps = {"schema": P.CAPABILITIES, "source": manifest["id"], "version": "prometheus-http-api/v1", "operations": ["query-range"], "min_interval": .5, "status": "available"}
    request = {"source": manifest["id"], "operation": "query-range", "interval": manifest.get("interval", .5), "duration": manifest["timeout"],
               "max_bytes": manifest["max_bytes"], "max_records": E.MAX_RECORDS, "allocation": manifest["identity"], "intrusive": False}
    if "policy" in manifest:
        decision = P.admit(manifest["policy"], request, source_caps)
        if not decision["allowed"]:
            raise ValueError("Telemetry policy refused: " + "; ".join(decision["reasons"]))
    def loader(event):
        return _fetch(manifest, credential, event)
    if manifest.get("cache_directory"):
        # Principal fingerprint is used only in the private cache key; it is
        # never copied into a receipt, support report or exception.
        scope = {"connection": E.digest({"endpoint": manifest["endpoint"], "principal": hashlib.sha256((credential or "anonymous").encode()).hexdigest()}), "generation": E.digest({k: v for k, v in manifest.items() if k not in {"cache_directory", "auth"}})}
        cache = SharedQueryCache(manifest["cache_directory"])
        result = cache.query(scope, {"request": request, "parameters": {"query": manifest["query"], "start": manifest["start"], "end": manifest["end"], "step": manifest["step"]}},
                             loader, manifest["policy"], source_caps, read_only=True, cancel=stop)
        return normalize(result["value"], manifest)
    return normalize(loader(stop), manifest)
