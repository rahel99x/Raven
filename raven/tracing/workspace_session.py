"""Owned, bounded run-index sessions shared by terminal and CLI viewers.

Only background work calls these functions. Painting consumes their published
JSON-compatible results. Raw recordings remain the authoritative evidence.
"""

from __future__ import annotations

from pathlib import Path
import threading

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

        self.paths = tuple(dict.fromkeys(map(str, paths)))
        if not self.paths or len(self.paths) > 512:
            raise ValueError("Choose 1–512 exact-attempt trace sources")
        self.expected = dict(expected or {})
        self.light = light
        budget = (128 if light else 512) << 20
        if index_path:
            self.index = TraceIndex(Path(index_path), max_bytes=budget)
        else:
            from .workspace_cache import open_index

            self.index = open_index(self.expected, self.paths, max_bytes=budget)
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
            paths = tuple(dict.fromkeys(map(str, paths)))
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

    def refresh(self, options=None, *, light=False, cancel=None):
        from .workspace_model import project

        options = dict(options or {})
        with self.lock:
            if self.closed:
                raise ValueError("Trace workspace was closed")
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
                    tables = [
                        self.index.functions(
                            **{
                                key: value
                                for key, value in request.items()
                                if key in _FUNCTION_FILTERS
                            },
                            limit=max(1, 100 // len(requests)),
                        )
                        for request in requests
                    ]
                    combined = dict(tables[0])
                    combined["functions"] = [
                        row for table in tables for row in table["functions"]
                    ][:100]
                    combined["truncated"] = (
                        any(table["truncated"] for table in tables)
                        or sum(len(table["functions"]) for table in tables) > 100
                    )
                    combined["scope"] = (
                        "Per-clock selected windows; unrelated node timestamps are not aligned"
                    )
                    result["functions"] = combined
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

        This performs no discovery, scheduling RPC or recorder control. The
        same index writer serializes verified bounded source pages.
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
    return {
        key: identity[key]
        for key in ("job_id", "attempt", "cluster")
        if key in identity
    }
