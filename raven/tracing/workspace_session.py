"""Owned, bounded run-index sessions shared by terminal and CLI viewers.

Only background work calls these functions. Painting consumes their published
JSON-compatible results. Raw recordings remain the authoritative evidence.
"""

from __future__ import annotations

from pathlib import Path
import threading
import time

_FILTERS = {
    "source_ids",
    "thread",
    "search",
    "node",
    "process",
    "logical_cpu",
    "device_id",
    "kind",
    "kinds",
    "task",
    "rank",
}
_FUNCTION_FILTERS = _FILTERS | {"start_ns", "end_ns", "clock_id"}


class WorkspaceSession:
    """One run, one owned index; caller serializes publication by UI generation."""

    def __init__(self, paths, *, expected=None, index_path=None, light=False):
        from .workspace_index import TraceIndex
        from .workspace_follow import source_paths

        self.paths = source_paths(paths)
        self.expected = dict(expected or {})
        self.light = light
        budget = (128 if light else 512) << 20
        reader_budget = (16 if light else 64) << 20
        if index_path:
            self.index = TraceIndex(
                Path(index_path), max_bytes=budget, reader_memory_bytes=reader_budget
            )
        else:
            from .workspace_cache import open_index

            self.index = open_index(
                self.expected,
                self.paths,
                max_bytes=budget,
                reader_memory_bytes=reader_budget,
            )
        self.path = self.index.path
        self.lock = threading.RLock()
        self.closed = False
        self.browser = None
        self.baseline = None
        self.comparison_context = None
        self.capacity = None
        self._retiring = threading.Event()
        self._browser_stop = threading.Event()
        self._browser_reader = None
        self._discovery = None
        self._next_discovery = 0.0
        self.discovery_errors = []
        self._metadata_errors = []
        self._follow_errors = []

    def retire(self):
        """Selection changes retire owned I/O off the terminal's input thread."""
        if not self._retiring.is_set():
            self._retiring.set()
            from .workspace_cleanup import retire

            retire(self)

    def close(self):
        self._browser_stop.set()
        reader = self._browser_reader
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=2)
        with self.lock:
            if self.closed:
                return
            self.closed = True
            try:
                if self.browser is not None:
                    self.browser.close()
                    self.browser = None
            finally:
                try:
                    if self.baseline is not None and self.baseline is not self:
                        self.baseline.close()
                finally:
                    self.index.close()

    def set_baseline(self, paths, *, context=None, light=False):
        """Only the owner registers sources; HTTP clients cannot supply paths."""
        with self.lock:
            if self.browser is not None:
                raise ValueError(
                    "Choose the baseline before opening the browser viewer"
                )
            from .workspace_follow import source_paths

            paths = source_paths(paths)
            previous = self.baseline
            if paths == self.paths:
                self.baseline = self
            elif (
                previous is not None
                and previous is not self
                and previous.paths == paths
            ):
                self.baseline = previous
            else:
                self.baseline = WorkspaceSession(paths, light=light)
            self.comparison_context = dict(context) if context else None
            if (
                previous is not None
                and previous is not self
                and previous is not self.baseline
            ):
                previous.close()

    def set_capacity(self, value):
        from .workspace_metrics_context import validate_context

        with self.lock:
            if self.browser is not None:
                raise ValueError(
                    "Choose the resource context before opening the browser"
                )
            self.capacity = validate_context(value) if value is not None else None

    def set_discovery(self, callback, *, primed=False):
        """Register an owner-only metadata callback; never supplied by HTTP.

        Primed means the owner has just completed the initial metadata scan.
        Replacing an equivalent UI closure does not postpone every later scan.
        """
        if callback is not None and not callable(callback):
            raise ValueError("Trace discovery must be a trusted owner callback")
        with self.lock:
            if self.closed:
                raise ValueError("Trace workspace was closed")
            first = self._discovery is None
            self._discovery = callback
            self.index.collection_following = callback is not None
            if callback is None:
                self._collection_errors([])
            elif first:
                self._next_discovery = time.monotonic() + 2 if primed else 0.0

    def _collection_errors(self, values, *, channel="follow"):
        from ..job_logs.text import display_text

        if not isinstance(values, (list, tuple)):
            raise ValueError("Trace discovery error metadata must be a bounded list")
        errors = []
        for value in values[:64]:
            if not isinstance(value, dict):
                value = {"read_error": str(value)}
            errors.append(
                {
                    "path": display_text(str(value.get("path", ""))[:4096]),
                    "read_error": display_text(
                        str(
                            value.get("read_error", "Unknown source discovery failure")
                        )[:2048]
                    ),
                }
            )
        if channel == "metadata":
            self._metadata_errors = errors
        else:
            self._follow_errors = errors
        seen, errors = set(), []
        for row in (*self._metadata_errors, *self._follow_errors):
            signature = row["path"], row["read_error"]
            if signature not in seen and len(errors) < 64:
                errors.append(row)
                seen.add(signature)
        self.discovery_errors = errors
        self.index.collection_error = "; ".join(
            (row["path"] + ": " if row["path"] else "") + row["read_error"]
            for row in errors[:4]
        )[:4000]
        self.index.collection_errors = errors

    def _discover(self, cancel=None):
        """Keep verified known paths while bounded source registration catches up."""
        if self._discovery is None or time.monotonic() < self._next_discovery:
            return
        from .workspace_follow import Cancellation

        cancellation = Cancellation(cancel, self._browser_stop, self._retiring)
        if cancellation.is_set():
            raise InterruptedError("Trace workspace discovery cancelled")
        self._next_discovery = time.monotonic() + 2
        try:
            discovered = self._discovery(cancellation)
            if cancellation.is_set():
                raise InterruptedError("Trace workspace discovery cancelled")
            if not isinstance(discovered, dict):
                raise ValueError("Trace source discovery returned no bounded catalog")
            paths = discovered.get("sources", [])
            if not isinstance(paths, (list, tuple)) or len(paths) > 512:
                raise ValueError("Trace source discovery exceeds the 512-source budget")
            if any(
                not isinstance(path, str)
                or not path
                or len(path) > 4096
                or not path.isprintable()
                for path in paths
            ):
                raise ValueError("Trace source discovery returned an invalid path")
            if paths:
                from .workspace_follow import source_paths

                paths = source_paths(paths)
            joined = tuple(dict.fromkeys((*self.paths, *paths)))
            if len(joined) > 512:
                raise ValueError(
                    "Run exceeds the 512-source workspace budget; known verified sources remain available"
                )
            errors = discovered.get("discovery_errors", [])
            if not isinstance(errors, (list, tuple)):
                raise ValueError(
                    "Trace source discovery returned invalid error metadata"
                )
            if not paths and not errors:
                errors = [
                    {
                        "read_error": "No current source registrations were discovered; retaining known verified sources"
                    }
                ]
            self.paths = joined
            self._collection_errors(errors)
            self._collection_errors([], channel="metadata")
        except InterruptedError:
            if cancellation.is_set():
                raise
            self._collection_errors(
                [
                    {
                        "read_error": "Source discovery was interrupted; retaining known verified sources"
                    }
                ]
            )
        except Exception as exc:
            self._collection_errors([{"read_error": str(exc)}])
        finally:
            self._next_discovery = max(self._next_discovery, time.monotonic() + 2)

    def refresh(self, options=None, *, light=False, cancel=None):
        from .workspace_model import project

        options = dict(options or {})
        with self.lock:
            if self.closed:
                raise ValueError("Trace workspace was closed")
            if type(options.get("job_finished")) is bool:
                self.index.owner_job_finished = options["job_finished"]
            if options.get("discovery_errors") or options.get("discovery_complete"):
                self._collection_errors(
                    options.get("discovery_errors", []), channel="metadata"
                )
            self._discover(cancel)
            catalog = self.index.refresh(
                self.paths,
                expected=self.expected,
                max_events=512 if light else 4096,
                max_seconds=0.015 if light else 0.05,
                cancel=cancel,
            )
            if cancel is not None and cancel.is_set():
                raise InterruptedError("Trace workspace query cancelled")
            keys = _FUNCTION_FILTERS | {"limit"}
            query = {
                key: value
                for key, value in options.items()
                if key in keys and value is not None
            }
            live = options.get("mode") == "live" or (
                options.get("mode", "auto") == "auto"
                and any(
                    source.get("status") == "LIVE"
                    for source in catalog.get("sources", [])
                )
            )
            if live:
                bounds = catalog.get("bounds", []) + catalog.get("aligned_bounds", [])
                clock = query.get("clock_id") or (
                    bounds[0]["clock_id"] if bounds else None
                )
                matching = [row for row in bounds if row["clock_id"] == clock]
                bound = (
                    dict(
                        clock_id=clock,
                        start_ns=min(row["start_ns"] for row in matching),
                        end_ns=max(row["end_ns"] for row in matching),
                    )
                    if matching
                    else None
                )
                if bound:
                    query.update(
                        clock_id=bound["clock_id"],
                        start_ns=max(
                            bound["start_ns"],
                            bound["end_ns"] - options.get("delta_ns", 10_000_000_000),
                        ),
                        end_ns=bound["end_ns"],
                    )
            requests = [query]
            if options.get("all_clocks") and catalog.get("bounds"):
                bounds = catalog["bounds"]
                previous_requests = {
                    row.get("clock_id"): row for row in options.get("requests", [])
                }
                requests = []
                per_clock = max(1, 2000 // len(bounds))
                for bound in bounds:
                    request = {
                        key: value for key, value in query.items() if key in _FILTERS
                    }
                    request.update(previous_requests.get(bound["clock_id"], {}))
                    request.update(
                        clock_id=bound["clock_id"],
                        search=query.get("search", ""),
                        limit=per_clock,
                    )
                    if live:
                        request.update(
                            start_ns=max(
                                bound["start_ns"],
                                bound["end_ns"]
                                - options.get("delta_ns", 10_000_000_000),
                            ),
                            end_ns=bound["end_ns"],
                        )
                    requests.append(request)
                result = self.index.query_many(requests)
            else:
                result = self.index.query(**query)
            result.setdefault("catalog", catalog)
            result.setdefault("sources", catalog.get("sources", []))
            result.setdefault("bounds", catalog.get("bounds", []))
            result["caught_up"] = catalog.get("caught_up", False)
            if self.discovery_errors:
                result["discovery_errors"] = list(self.discovery_errors)
                result["caught_up"] = False
                result["warnings"] = list(result.get("warnings", [])) + [
                    "Source discovery is incomplete; other verified sources remain available."
                ]
            result["workspace_projection"] = project(
                result, options.get("perspective", "execution")
            )
            result["workspace_projection_name"] = options.get(
                "perspective", "execution"
            )
            selected = options.get("selected")
            result["selected_detail"] = (
                self.index.detail(selected) if selected else None
            )
            if (
                selected
                and (options.get("binary") or options.get("source_root"))
                and result["selected_detail"]
            ):
                from .workspace_symbols import resolve_symbol

                symbol = result["selected_detail"]
                frame_index = options.get("sample_frame_index")
                if frame_index is not None:
                    frames = symbol.get("data", {}).get("frames", [])
                    if (
                        type(frame_index) is not int
                        or not 0 <= frame_index < len(frames)
                        or not isinstance(frames[frame_index], dict)
                    ):
                        raise ValueError(
                            "Choose an original recorded sample frame index"
                        )
                    symbol = frames[frame_index]
                result["symbol"] = resolve_symbol(
                    symbol,
                    binary=options.get("binary"),
                    source_root=options.get("source_root"),
                )
            self._analysis(result, options)
            if options.get("functions"):
                if options.get("all_clocks"):
                    result["functions"] = self._functions_many(requests, cancel)
                    if result["functions"]["unqueried_ranges"]:
                        result["warnings"] = list(result.get("warnings", [])) + [
                            "Function statistics reached their shared work budget; "
                            + str(len(result["functions"]["unqueried_ranges"]))
                            + " clock ranges remain unqueried. Choose a clock or narrow the range."
                        ]
                    elif result["functions"]["truncated"]:
                        result["warnings"] = list(result.get("warnings", [])) + [
                            "Function statistics reached a row or byte limit; queried clock ranges remain listed. Narrow the selection to inspect omitted rows."
                        ]
                else:
                    result["functions"] = self.index.functions(
                        **{
                            key: value
                            for key, value in query.items()
                            if key in _FUNCTION_FILTERS
                        }
                    )
            if options.get("overview"):
                result["overview"] = self.index.overview(
                    **{
                        key: value
                        for key, value in query.items()
                        if key in {"source_ids", "clock_id"}
                    }
                )
            if self.baseline is not None:
                from .workspace_analysis import compare

                baseline_result = (
                    self.index.query(limit=2000)
                    if self.baseline is self
                    else self.baseline.refresh(
                        {"mode": "explore"}, light=light, cancel=cancel
                    )
                )
                result["baseline_caught_up"] = baseline_result.get("caught_up", False)
                result["comparison"] = compare(
                    result, baseline_result, context=self.comparison_context
                )
            result["index_path"] = str(self.path)
            result["cache_rebuilt"] = getattr(self.index, "cache_rebuilt", False)
            return result

    def _functions_many(self, requests, cancel=None):
        """Share one SQL deadline across domains instead of one second each.

        The index's nested budget owner preserves its progress handler and
        serialization contract. Cancellation is checked between bounded SQL
        calls; unfinished domains are disclosed without discarding the timeline.
        """
        from .workspace_index_storage import _json

        tables, pending = [], []
        deadline = time.monotonic() + 1.0
        with self.index.lock, self.index._query_budget(seconds=1.0):
            for position, request in enumerate(requests):
                if cancel is not None and cancel.is_set():
                    raise InterruptedError("Trace function statistics cancelled")
                if time.monotonic() >= deadline:
                    pending = requests[position:]
                    break
                try:
                    tables.append(
                        self.index.functions(
                            **{
                                key: value
                                for key, value in request.items()
                                if key in _FUNCTION_FILTERS
                            },
                            limit=max(1, 100 // max(1, len(requests))),
                        )
                    )
                except ValueError as exc:
                    if "work budget exceeded" not in str(exc):
                        raise
                    pending = requests[position:]
                    break
            if cancel is not None and cancel.is_set():
                raise InterruptedError("Trace function statistics cancelled")
        combined = (
            dict(tables[0])
            if tables
            else {
                "schema": "raven.trace-workspace-functions/v1",
                "functions": [],
                "units": "nanoseconds",
                "clock_id": None,
                "filters": {},
                "alignment": "Independent clock ranges; no common axis is inferred",
                "limitations": [],
            }
        )
        rows, payload_bytes, byte_limit = [], 0, 8 << 20
        total_rows = sum(len(table["functions"]) for table in tables)
        for table in tables:
            for row in table["functions"]:
                amount = len(_json(row).encode("utf-8"))
                if len(rows) >= 100 or payload_bytes + amount > byte_limit:
                    break
                rows.append(row)
                payload_bytes += amount
            else:
                continue
            break
        combined.update(
            functions=rows,
            payload_bytes=payload_bytes,
            byte_limit=byte_limit,
            omitted_function_rows=total_rows - len(rows),
            truncated=bool(pending)
            or any(table["truncated"] for table in tables)
            or total_rows > len(rows),
            queried_ranges=[dict(request) for request in requests[: len(tables)]],
            unqueried_ranges=[dict(request) for request in pending],
            scope="Per-clock selected windows; unrelated node timestamps are not aligned",
        )
        return combined

    def _analysis(self, result, options):
        """Explicit bounded analyses share the exact visible query/evidence scope."""
        if options.get("call_tree") or options.get("bottom_up"):
            from .workspace_model import call_tree

            for name in ("call_tree", "bottom_up"):
                if options.get(name):
                    result[name] = call_tree(result, bottom_up=name == "bottom_up")
        if options.get("metrics"):
            from .workspace_metrics_context import metrics

            result["metrics"] = metrics(result, context=self.capacity)
        if options.get("samples"):
            from .workspace_model import sample_statistics

            result["samples"] = sample_statistics(result)
        if options.get("causal") or options.get("counters") or options.get("hints"):
            from .workspace_analysis import causal_slice, counter_lens, bottleneck_hints

            if options.get("causal"):
                result["causal"] = causal_slice(result, options["causal"])
            if options.get("counters"):
                result["counters"] = counter_lens(result)
            if options.get("hints"):
                result["hints"] = bottleneck_hints(result)

    def open_browser(self, *, open_browser=False):
        from .workspace_browser import start

        with self.lock:
            if self.closed:
                raise ValueError("Trace workspace was closed")
            if self.browser is None:
                self.browser = start(
                    self.index,
                    open_browser=open_browser,
                    baseline=self.baseline.index if self.baseline is not None else None,
                    comparison_context=self.comparison_context,
                    capacity=self.capacity,
                )
                self._browser_stop.clear()
                self._browser_reader = threading.Thread(
                    target=self._ingest_browser,
                    name="raven-trace-viewer-reader",
                    daemon=True,
                )
                self._browser_reader.start()
            return self.browser

    def _ingest_browser(self):
        """Explicit viewer lifetime, independent of the terminal's active tab.

        The optional trusted callback scans bounded registrations independently
        of terminal ticks. No scheduling RPC or recorder control is performed.
        """
        while not self._browser_stop.wait(0.5):
            with self.lock:
                if self.closed:
                    return
                owners = [self]
                if self.baseline is not None and self.baseline is not self:
                    owners.append(self.baseline)
            for owner in owners:
                if self._browser_stop.is_set():
                    return
                try:
                    with owner.lock:
                        if owner.closed:
                            continue
                        owner._discover(self._browser_stop)
                        owner.index.refresh(
                            owner.paths,
                            expected=owner.expected,
                            max_events=512 if owner.light else 4096,
                            max_seconds=0.015 if owner.light else 0.05,
                            cancel=self._browser_stop,
                        )
                        owner.index.viewer_read_error = ""
                except Exception as exc:
                    from ..notifications import message_text

                    owner.index.viewer_read_error = message_text(exc)[0]


def expected_identity(snapshot):
    identity = snapshot.get("identity", {})
    result = {
        key: identity[key]
        for key in ("job_id", "attempt", "cluster")
        if key in identity
    }
    if identity.get("run_id") and identity["run_id"] != identity.get("attempt"):
        result["run_id"] = identity["run_id"]
    return result
