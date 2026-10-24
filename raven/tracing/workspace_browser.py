"""Explicit, private, read-only loopback access to one trace workspace.

The caller owns ingestion and the index. This service never accepts a file path,
starts a recorder, changes a job, or closes the caller's index. URLs carry the
session credential in a fragment so normal HTTP access logs do not contain it.
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager, nullcontext
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import threading
from urllib.parse import parse_qs, quote, urlsplit

MAX_RESPONSE_BYTES = 4 << 20
MAX_TARGET_BYTES = 48 << 10
MAX_FILTER_SOURCES = 512
MAX_QUERY_INTERVALS = 2000
_EXECUTION_FILTERS = (
    "node",
    "process",
    "thread",
    "logical_cpu",
    "device_id",
    "kind",
    "task",
    "rank",
)


class RequestError(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def _wire(value, key=""):
    """Keep timestamps exact in JavaScript; display coordinates are derived later."""
    if isinstance(value, dict):
        return {name: _wire(child, name) for name, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_wire(child, key) for child in value]
    if type(value) is int and (key.endswith("_ns") or abs(value) > (1 << 53) - 1):
        return str(value)
    return value


def _json(value, maximum=MAX_RESPONSE_BYTES):
    data = bytearray()
    for chunk in json.JSONEncoder(
        ensure_ascii=True, allow_nan=False, separators=(",", ":")
    ).iterencode(_wire(value)):
        data.extend(chunk.encode("utf-8"))
        if len(data) > maximum:
            raise RequestError(
                413,
                "Trace response exceeds its byte budget; narrow the range or reduce the interval limit.",
            )
    return bytes(data)


def _identity(catalog):
    identity = catalog.get("identity", {})
    if not isinstance(identity, dict):
        raise ValueError("Invalid trace workspace identity")
    return (
        hashlib.sha256(
            json.dumps(identity, sort_keys=True, allow_nan=False).encode()
        ).hexdigest()
        if identity
        else None
    )


def _text(value, label, maximum):
    if not value or len(value) > maximum or not value.isprintable():
        raise RequestError(400, label + " must be a bounded printable value")
    return value


def _integer(value, label, *, minimum=0, maximum=(1 << 64) - 1):
    if not value.isascii() or not value.isdecimal() or len(value) > 20:
        raise RequestError(400, "Invalid " + label)
    number = int(value)
    if not minimum <= number <= maximum:
        raise RequestError(400, "Invalid " + label)
    return number


def _parameters(target):
    if len(target) > MAX_TARGET_BYTES:
        raise RequestError(414, "Trace request target is too long")
    split = urlsplit(target)
    if split.scheme or split.netloc or split.fragment:
        raise RequestError(400, "Use a relative trace request path")
    try:
        # CPython 3.10's strict parser rejects an empty query as an empty
        # field; later versions return {}. An absent query has no fields to
        # validate. Keep strict parsing for every nonempty query.
        values = (
            parse_qs(
                split.query,
                keep_blank_values=True,
                strict_parsing=True,
                max_num_fields=16,
                encoding="utf-8",
                errors="strict",
            )
            if split.query
            else {}
        )
    except (ValueError, UnicodeError) as exc:
        raise RequestError(400, "Malformed trace query") from exc
    if any(len(items) != 1 for items in values.values()):
        raise RequestError(400, "Duplicate trace query field")
    return split.path, {key: items[0] for key, items in values.items()}


def _filters(values, catalog, allowed):
    if set(values) - set(allowed):
        raise RequestError(400, "Unsupported trace query field")
    result = {}
    for key in ("start_ns", "end_ns"):
        if key in values:
            result[key] = _integer(values[key], key)
    if (
        "start_ns" in result
        and "end_ns" in result
        and result["end_ns"] <= result["start_ns"]
    ):
        raise RequestError(400, "Trace range end must follow its start")
    if "source_ids" in values:
        ids = values["source_ids"].split(",")
        known = {str(row.get("id", "")) for row in catalog.get("sources", [])}
        if (
            len(ids) > MAX_FILTER_SOURCES
            or len(set(ids)) != len(ids)
            or any(not item or item not in known for item in ids)
        ):
            raise RequestError(400, "Unknown or excessive trace source filter")
        result["source_ids"] = ids
    if "clock_id" in values:
        clock = _text(values["clock_id"], "Clock domain", 1024)
        known = {
            str(row.get("clock_id", ""))
            for row in (*catalog.get("bounds", []), *catalog.get("aligned_bounds", []))
        }
        if clock not in known:
            raise RequestError(400, "Unknown trace clock domain")
        result["clock_id"] = clock
    if "thread" in values:
        result["thread"] = _text(values["thread"], "Thread", 256)
    for name in ("node", "process", "device_id", "kind", "task", "rank"):
        if name in values:
            result[name] = _text(values[name], "Recorded " + name, 1024)
    if "logical_cpu" in values:
        result["logical_cpu"] = _integer(
            values["logical_cpu"], "Logical CPU", maximum=(1 << 31) - 1
        )
    if "search" in values:
        if (
            len(values["search"]) > 256
            or not values["search"].isprintable()
            and values["search"]
        ):
            raise RequestError(400, "Search is too long or contains control characters")
        result["search"] = values["search"]
    return result


def _open(url, environ=None):
    environ = os.environ if environ is None else environ
    if environ.get("SSH_CONNECTION") or environ.get("SSH_TTY"):
        return "Viewer is on this SSH host's loopback interface. Forward its port explicitly, or use the offline export on your workstation."
    if sys.platform != "darwin" and not (
        environ.get("DISPLAY") or environ.get("WAYLAND_DISPLAY")
    ):
        return "No graphical desktop detected. Open the private viewer URL locally or forward its port from your workstation."
    opener = shutil.which("open" if sys.platform == "darwin" else "xdg-open")
    if opener is None:
        return "No desktop browser opener is installed; open the private viewer URL manually."
    try:
        process = subprocess.Popen(
            [opener, url],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
        if process.poll() not in (None, 0):
            return "The browser opener failed; open the private viewer URL manually."
    except OSError:
        return "The browser opener failed; open the private viewer URL manually."
    return "Browser open requested; the request does not prove that a tab is visible."


@contextmanager
def _locked(index, baseline=None):
    # Two viewers may compare A/B in opposite directions. A stable lock order
    # avoids cross-index deadlock while keeping each response one snapshot.
    indexes = sorted(
        {id(item): item for item in (index, baseline) if item is not None}.values(),
        key=id,
    )
    with ExitStack() as stack:
        for item in indexes:
            lock = getattr(item, "lock", None)
            stack.enter_context(
                lock
                if lock is not None
                else item.read_lock()
                if hasattr(item, "read_lock")
                else nullcontext()
            )
        yield


class _Server(HTTPServer):
    allow_reuse_address = False
    request_queue_size = 8

    def handle_error(self, request, client_address):
        # A browser closing a tab or a slow local client is not a trace failure.
        pass


class Viewer:
    """An owned server handle; close stops viewing without closing the index."""

    def __init__(self, index, *, baseline=None, comparison_context=None, capacity=None):
        self.index = index
        self.baseline = baseline
        if capacity is not None:
            from .workspace_metrics_context import validate_context

            capacity = validate_context(capacity)
        self.capacity = capacity
        if comparison_context is not None and not isinstance(comparison_context, dict):
            raise ValueError("Comparison context must be an object")
        if comparison_context is not None:
            from .protocol import _bounded_data
            from .workspace_analysis_compare import CONTEXT_SCHEMA

            _bounded_data(comparison_context)
            if comparison_context.get("schema") != CONTEXT_SCHEMA:
                raise ValueError("Unsupported trace comparison context schema")
        context_raw = json.dumps(comparison_context, allow_nan=False)
        if len(context_raw.encode()) > 64 << 10:
            raise ValueError("Comparison context exceeds its 64 KiB bound")
        self.comparison_context = json.loads(context_raw)
        self.baseline_identity = (
            _identity(baseline.catalog()) if baseline is not None else None
        )
        self.token = secrets.token_urlsafe(32)
        self.session_id = secrets.token_hex(16)
        self.run_identity = _identity(index.catalog())
        self._closed = False
        self._close_lock = threading.Lock()
        self._server = _Server(("127.0.0.1", 0), _Handler)
        self._server.viewer = self
        self.origin = "http://127.0.0.1:" + str(self._server.server_address[1])
        self.url = self.origin + "/#token=" + quote(self.token, safe="")
        self.message = "Private loopback viewer ready; keep its session URL private."
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.1},
            name="raven-trace-browser",
            daemon=True,
        )
        self._thread.start()

    def close(self):
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            self._server.shutdown()
            self._server.server_close()
            self._thread.join(timeout=3)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _catalog(self):
        catalog = self.index.catalog()
        identity = _identity(catalog)
        if self.run_identity is None:
            self.run_identity = identity
        elif identity != self.run_identity:
            raise RequestError(
                409,
                "The trace workspace identity changed; close this viewer and open the selected run again.",
            )
        read_error = getattr(self.index, "viewer_read_error", "")
        if read_error:
            catalog = dict(catalog, viewer_read_error=str(read_error)[:2000])
        return catalog

    def dispatch(self, path, values):
        with _locked(self.index, self.baseline):
            catalog = self._catalog()
            if (
                self.baseline is not None
                and _identity(self.baseline.catalog()) != self.baseline_identity
            ):
                raise RequestError(
                    409,
                    "The registered baseline identity changed; reopen the comparison.",
                )
            if path == "/api/catalog":
                if values:
                    raise RequestError(400, "Catalog does not accept query fields")
                result = dict(
                    catalog,
                    comparison={
                        "configured": self.baseline is not None,
                        "baseline_identity": self.baseline.catalog().get("identity", {})
                        if self.baseline is not None
                        else None,
                    },
                )
            elif path == "/api/query":
                filters = _filters(
                    values,
                    catalog,
                    (
                        "start_ns",
                        "end_ns",
                        "source_ids",
                        "clock_id",
                        "thread",
                        "search",
                        "limit",
                        "projection",
                    )
                    + _EXECUTION_FILTERS,
                )
                filters["limit"] = _integer(
                    values.get("limit", "1000"),
                    "Interval limit",
                    minimum=1,
                    maximum=MAX_QUERY_INTERVALS,
                )
                projection = values.get("projection", "execution")
                if projection not in {"execution", "cpu", "gpu"}:
                    raise RequestError(400, "Unknown trace perspective")
                result = self.index.query(**filters)
                from .workspace_metrics_context import metrics
                from .workspace_model import project, explicit_flows

                measured = metrics(
                    result, context=self.capacity, source_ids=filters.get("source_ids")
                )
                result = project(result, perspective=projection)
                result["metrics"] = measured
                result["flows"] = explicit_flows(result)
            elif path == "/api/overview":
                filters = _filters(values, catalog, ("source_ids", "clock_id", "bins"))
                filters["bins"] = _integer(
                    values.get("bins", "256"),
                    "Overview bin count",
                    minimum=8,
                    maximum=1024,
                )
                result = self.index.overview(**filters)
            elif path == "/api/functions":
                filters = _filters(
                    values,
                    catalog,
                    (
                        "source_ids",
                        "start_ns",
                        "end_ns",
                        "limit",
                        "clock_id",
                        "thread",
                        "search",
                    )
                    + _EXECUTION_FILTERS,
                )
                filters["limit"] = _integer(
                    values.get("limit", "100"), "Function limit", minimum=1, maximum=200
                )
                result = self.index.functions(**filters)
            elif path == "/api/detail":
                if set(values) != {"id"}:
                    raise RequestError(400, "Choose one recorded interval ID")
                result = self.index.detail(_text(values["id"], "Interval ID", 512))
                if result is None:
                    raise RequestError(
                        404, "That recorded interval is unavailable in this workspace"
                    )
            elif path == "/api/analysis":
                filters = _filters(
                    values,
                    catalog,
                    (
                        "source_ids",
                        "clock_id",
                        "start_ns",
                        "end_ns",
                        "selected_id",
                        "thread",
                        "search",
                    )
                    + _EXECUTION_FILTERS,
                )
                query = self.index.query(**filters, limit=MAX_QUERY_INTERVALS)
                from .workspace_analysis import (
                    causal_slice,
                    compare,
                    counter_lens,
                    bottleneck_hints,
                )

                selected_id = values.get("selected_id")
                if selected_id is not None:
                    selected_id = _text(selected_id, "Selected event ID", 512)
                result = {
                    "counter_lens": counter_lens(query),
                    "hints": bottleneck_hints(query),
                    "causal_slice": causal_slice(query, selected_id)
                    if selected_id
                    else {
                        "status": "unavailable",
                        "reason": "Select a recorded event to inspect its explicit causal context.",
                    },
                    "comparison": compare(
                        query,
                        self.baseline.query(limit=MAX_QUERY_INTERVALS),
                        context=self.comparison_context,
                    )
                    if self.baseline is not None
                    else {
                        "status": "unavailable",
                        "reason": "No baseline was registered when this viewer opened; use Raven's explicit baseline option.",
                    },
                    "query_truncated": query.get("truncated", False),
                    "scope": "Current bounded query; heuristics do not prove an application bottleneck or a complete global critical path.",
                }
                read_errors = {
                    name: str(getattr(item, "viewer_read_error", ""))[:2000]
                    for name, item in (
                        ("current", self.index),
                        ("baseline", self.baseline),
                    )
                    if item is not None and getattr(item, "viewer_read_error", "")
                }
                if read_errors:
                    result["read_errors"] = read_errors
                    result["comparison"]["stale"] = True
                    if result["comparison"]["status"] == "comparable":
                        result["comparison"]["status"] = "partial"
            elif path == "/api/flows":
                filters = _filters(
                    values,
                    catalog,
                    ("source_ids", "clock_id", "start_ns", "end_ns")
                    + _EXECUTION_FILTERS,
                )
                query = self.index.query(**filters, limit=MAX_QUERY_INTERVALS)
                from .workspace_model import explicit_flows

                result = explicit_flows(query)
                result["query_truncated"] = query.get("truncated", False)
            elif path == "/api/tree":
                filters = _filters(
                    values,
                    catalog,
                    (
                        "source_ids",
                        "clock_id",
                        "start_ns",
                        "end_ns",
                        "bottom_up",
                        "thread",
                        "search",
                    )
                    + _EXECUTION_FILTERS,
                )
                bottom_up = values.get("bottom_up", "0")
                if bottom_up not in {"0", "1"}:
                    raise RequestError(400, "Invalid call-tree direction")
                query = self.index.query(**filters, limit=MAX_QUERY_INTERVALS)
                from .workspace_model import call_tree

                result = call_tree(query, bottom_up=bottom_up == "1")
            else:
                raise RequestError(404, "Unknown trace viewer route")
            self._catalog()
            return {"workspace_id": self.session_id, "result": result}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "RavenTrace"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(2)

    def log_message(self, format, *args):
        pass

    def _headers(self):
        viewer = self.server.viewer
        if len(self.path) > MAX_TARGET_BYTES:
            raise RequestError(414, "Trace request target exceeds its budget")
        if sum(len(key) + len(value) for key, value in self.headers.items()) > 16 << 10:
            raise RequestError(431, "Trace request headers exceed their budget")
        if self.headers.get_all("Host", []) != [urlsplit(viewer.origin).netloc]:
            raise RequestError(403, "Viewer accepts its exact loopback host only")
        origins = self.headers.get_all("Origin", [])
        if origins and origins != [viewer.origin]:
            raise RequestError(403, "Foreign browser origins cannot access this trace")
        if self.headers.get("Sec-Fetch-Site", "") not in {"", "same-origin", "none"}:
            raise RequestError(403, "Cross-site trace requests are refused")

    def _send(
        self, code, body, content_type="application/json; charset=utf-8", *, csp=None
    ):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Connection", "close")
        self.send_header(
            "Content-Security-Policy",
            csp or "default-src 'none'; frame-ancestors 'none'",
        )
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            pass

    def do_GET(self):
        try:
            self._headers()
            path, values = _parameters(self.path)
            if path == "/" and not values:
                from .workspace_document import render, content_security_policy

                self._send(
                    200,
                    render(),
                    "text/html; charset=utf-8",
                    csp=content_security_policy(),
                )
                return
            viewer = self.server.viewer
            auth = self.headers.get_all("Authorization", [])
            expected = "Bearer " + viewer.token
            if (
                len(auth) != 1
                or not auth[0].isascii()
                or not secrets.compare_digest(auth[0], expected)
            ):
                raise RequestError(
                    401, "This private trace viewer requires its session credential"
                )
            body = _json(viewer.dispatch(path, values))
            self._send(200, body)
        except RequestError as exc:
            self._send(exc.status, _json({"error": str(exc)}))
        except Exception as exc:
            self._send(
                503, _json({"error": "Trace query unavailable: " + str(exc)[:500]})
            )

    def do_POST(self):
        self._send(405, _json({"error": "Trace viewer is read-only"}))

    do_PUT = do_POST
    do_PATCH = do_POST
    do_DELETE = do_POST
    do_OPTIONS = do_POST
    do_HEAD = do_POST


def start(
    index, *, open_browser=False, baseline=None, comparison_context=None, capacity=None
):
    """Start one explicit viewer for an already-owned index, never a recorder."""
    viewer = Viewer(
        index,
        baseline=baseline,
        comparison_context=comparison_context,
        capacity=capacity,
    )
    if open_browser:
        viewer.message = _open(viewer.url)
    return viewer
