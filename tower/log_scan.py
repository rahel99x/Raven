"""Bounded, cancellable scans of exact regular log sources.

All functions are worker APIs. They never launch a shell or enumerate paths.
Results describe a snapshot of the first ``size`` bytes. Appends are allowed;
replacement, truncation, and same-size edits invalidate the result.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime
import re
import time
import os
import stat
import codecs

from .remote import LocalFiles, RemoteFiles

from .log_text import display_text

CHUNK_BYTES = 64 << 10
PAGE_BYTES = 256 << 10
MAX_LINE_BYTES = 256 << 10
REGEX_LINE_BYTES = 8192
MAX_SCAN_BYTES = 512 << 20
MAX_RESULTS = 2000
MAX_SOURCES = 32
MAX_SECONDS = 30.0


class ScanCancelled(ValueError):
    pass


class SourceChanged(ValueError):
    pass


def snapshot(files, path):
    method = getattr(files, "snapshot_stat", None)
    inherited_local = getattr(type(files), "snapshot_stat", None) is LocalFiles.snapshot_stat
    if method is not None and (type(files) is LocalFiles or isinstance(files, RemoteFiles) or not inherited_local or (not getattr(files, "remote", False) and getattr(type(files), "stat", None) is LocalFiles.stat)):
        value = method(path)
    else:
        # Legacy stat/read adapters must never fall through to local OS paths.
        size, identity = files.stat(path)
        value = {"size": size, "ident": identity, "updated": None}
    if not isinstance(value, dict):
        raise ValueError("Invalid regular-file metadata")
    size, ident = value.get("size"), value.get("ident")
    if type(size) is not int or size < 0 or not isinstance(ident, (tuple, list)) or len(ident) != 2 or any(type(v) is not int or v < 0 for v in ident):
        raise ValueError("Invalid regular-file metadata")
    return dict(value, ident=tuple(ident))


def check_snapshot(files, path, before):
    after = snapshot(files, path)
    if after["ident"] != before["ident"] or after["size"] < before["size"] or (after["size"] == before["size"] and after.get("updated") != before.get("updated")):
        raise SourceChanged("Log source changed during inspection; retry the command")
    return after


def _check(cancel, deadline=None):
    if cancel and cancel():
        raise ScanCancelled("Log operation cancelled")
    if deadline is not None and time.monotonic() > deadline:
        raise ScanCancelled("Log scan reached its time limit; narrow the source or retry")


def _read(files, path, offset, count, expected=None):
    if not getattr(files, "remote", False) and getattr(type(files), "read", None) is LocalFiles.read:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise OSError("Log source must be a regular file")
            if expected is not None and (info.st_dev, info.st_ino) != expected["ident"]:
                raise SourceChanged("Log source was replaced during inspection")
            data = os.pread(fd, count, offset)
        finally:
            os.close(fd)
    else:
        data = files.read(path, offset, count)
    if not isinstance(data, bytes) or len(data) != count:
        raise SourceChanged("Log read was incomplete; refresh and retry")
    return data


def _text(data):
    return display_text(data.decode("utf-8", "replace").rstrip("\r\n"))


def read_page(files, path, offset=0, *, max_bytes=PAGE_BYTES, max_rows=500, line=None, expected=None, cancel=None, align=True):
    """Read a bounded page at a byte position, retaining original line bytes.

    ``line`` is the one-based line at ``offset`` when known. A page from zero
    has absolute line numbers. Other arbitrary-byte pages show exact offsets.
    A giant source line is represented as a labelled continuation fragment.
    """
    _check(cancel)
    before = snapshot(files, path)
    if expected is not None and tuple(expected) != before["ident"]:
        raise SourceChanged("The selected log file was replaced; search it again")
    if type(offset) is not int or offset < 0:
        raise ValueError("Byte position must be a nonnegative integer")
    budget = max(1, min(PAGE_BYTES, int(max_bytes)))
    count_rows = max(1, min(500, int(max_rows)))
    offset = min(offset, before["size"])
    start = max(0, offset - 1)
    count = min(before["size"] - start, budget + (1 if offset else 0))
    raw = _read(files, path, start, count, before) if count else b""
    _check(cancel)
    partial_start = False
    if offset:
        preceding, raw = raw[:1], raw[1:]
        start = offset
        if preceding != b"\n" and not align:
            partial_start = True
        elif preceding != b"\n":
            boundary = raw.find(b"\n")
            if boundary >= 0:
                start += boundary + 1
                raw = raw[boundary + 1:]
                if line is not None:
                    line += 1
            else:
                partial_start = True
    else:
        line = 1
    rows, pos = [], 0
    while pos < len(raw) and len(rows) < count_rows:
        nl = raw.find(b"\n", pos)
        end = len(raw) if nl < 0 else nl + 1
        # Keep a cut final line for the next page when preceding rows exist.
        if nl < 0 and start + end < before["size"] and rows:
            break
        chunk = raw[pos:end]
        rows.append({"raw": chunk, "text": _text(chunk), "offset": start + pos,
                     "end": start + end, "line": line,
                     "partial_start": partial_start and not rows,
                     "partial_end": nl < 0 and start + end < before["size"]})
        if line is not None and nl >= 0:
            line += 1
        pos = end
    after = check_snapshot(files, path, before)
    return {"path": path, "snapshot": before, "rows": rows, "start": start,
            "end": start + pos, "next": start + pos, "previous": max(0, start - budget),
            "size": before["size"], "appended": after["size"] > before["size"],
            "complete": start == 0 and start + pos == before["size"],
            "partial": any(row["partial_start"] or row["partial_end"] for row in rows)}


def _iter_lines(files, path, before, *, cancel=None, progress=None, max_bytes=MAX_SCAN_BYTES, deadline=None):
    """Yield bounded fragments; continuations retain the same absolute line."""
    limit = min(before["size"], max(1, int(max_bytes)))
    offset, line, line_start = 0, 1, 0
    pending = b""
    while offset < limit:
        _check(cancel, deadline)
        count = min(CHUNK_BYTES, limit - offset)
        pending += _read(files, path, offset, count, before)
        offset += count
        if progress:
            progress(offset, before["size"])
        position = 0
        while position < len(pending):
            nl = pending.find(b"\n", position)
            remaining = len(pending) - position
            if nl >= 0 and nl + 1 - position <= MAX_LINE_BYTES:
                end = nl + 1
                yield {"raw": pending[position:end], "line": line, "offset": line_start, "complete": True}
                line_start += end - position
                line += 1
                position = end
            elif remaining >= MAX_LINE_BYTES:
                end = position + MAX_LINE_BYTES
                yield {"raw": pending[position:end], "line": line, "offset": line_start, "complete": False}
                line_start += MAX_LINE_BYTES
                position = end
            else:
                break
        pending = pending[position:]
    if pending:
        yield {"raw": pending, "line": line, "offset": line_start,
               "complete": limit == before["size"]}


def compile_search(query, *, regex=False, case=False, word=False):
    if not isinstance(query, str) or not query or len(query) > 1024:
        raise ValueError("Search text must contain 1 to 1024 characters")
    pattern = query if regex else re.escape(query)
    if regex:
        # Avoid expressions whose backtracking can monopolize the single worker.
        # One variable repetition supports useful forms such as ERROR.*timeout.
        try:
            from re import _parser, _constants
        except ImportError:  # Python 3.10 exposes the parser under its older name.
            import sre_parse as _parser
            import sre_constants as _constants
        repeat_ops = tuple(getattr(_constants, name) for name in ("MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT") if hasattr(_constants, name))
        try:
            parsed = _parser.parse(pattern, 0)
        except re.error as exc:
            raise ValueError("Invalid search expression: " + str(exc)) from exc
        repeats = [0]
        def inspect(nodes, repeated=False):
            for op, arg in nodes:
                if op in (_constants.GROUPREF, _constants.GROUPREF_EXISTS, _constants.ASSERT, _constants.ASSERT_NOT):
                    raise ValueError("Use a regex without backreferences or look-around")
                if op in repeat_ops:
                    lo, hi, children = arg
                    if repeated or hi != lo and repeats[0] >= 1 or hi != _constants.MAXREPEAT and hi > 1000:
                        raise ValueError("Use a regex without nested or multiple unbounded repetitions")
                    repeats[0] += hi != lo
                    inspect(children, True)
                elif op == _constants.SUBPATTERN:
                    inspect(arg[-1], repeated)
                elif op == _constants.BRANCH:
                    if repeated:
                        raise ValueError("Use a regex without repeated alternatives")
                    for branch in arg[1]:
                        inspect(branch, repeated)
        inspect(parsed)
    if word:
        pattern = r"\b(?:" + pattern + r")\b"
    try:
        return re.compile(pattern, 0 if case else re.IGNORECASE)
    except re.error as exc:
        raise ValueError("Invalid search expression: " + str(exc)) from exc


def search_source(files, source, query, *, regex=False, case=False, word=False,
                  context=2, max_results=MAX_RESULTS, max_bytes=MAX_SCAN_BYTES,
                  cancel=None, progress=None, deadline=None):
    """Search one exact source; result limits never imply complete coverage."""
    matcher = compile_search(query, regex=regex, case=case, word=word)
    path = source["path"]
    before = snapshot(files, path)
    from .raven_execution import router_for, AdmissionError
    router = router_for()
    if router.managed and before["size"] > 2 << 20:
        blocked = router.require("large-log-search", worker_operation="log-search-files")
        if blocked is not None:
            raise AdmissionError(blocked["summary"])
        if getattr(files, "remote", False):
            raise AdmissionError("Large remote search needs a worker-visible source mapping")
        receipt = router.dispatch("log-search-files", {"sources": [source], "query": query,
            "options": {"regex": regex, "case": case, "word": word, "context": context, "max_results": max_results}})
        if receipt.get("status") == "deferred":
            raise AdmissionError(receipt["summary"])
        result = receipt["result"]
        if not result.get("reports") or result["reports"][0].get("error"):
            raise ValueError((result.get("reports") or [{}])[0].get("error", "Worker did not return source coverage"))
        return dict(result["reports"][0], matches=result["matches"])
    if router.managed:
        max_bytes = min(max_bytes, 2 << 20)
    results, previous, waiting = [], deque(maxlen=max(0, min(4, int(context)))), []
    scanned, continued, carry, current_line, current_match = 0, False, "", None, None
    long_regex_lines = 0
    decoder = None
    previous_raw_tail = b""
    limit = max(1, min(MAX_RESULTS, int(max_results)))
    deadline = time.monotonic() + MAX_SECONDS if deadline is None else deadline
    for item in _iter_lines(files, path, before, cancel=cancel, progress=progress, max_bytes=max_bytes, deadline=deadline):
        _check(cancel, deadline)
        scanned = item["offset"] + len(item["raw"])
        if current_line != item["line"]:
            current_line, current_match, continued, carry = item["line"], None, False, ""
            decoder = codecs.getincrementaldecoder("utf-8")("replace")
            previous_raw_tail = b""
        text = display_text(decoder.decode(item["raw"], final=item["complete"]).rstrip("\r\n"))
        snippet = {"line": item["line"], "offset": item["offset"], "text": text[:2048]}
        if not continued:
            for row in waiting[:]:
                row["after"].append(snippet)
                if len(row["after"]) >= context:
                    waiting.remove(row)
        # Literal searches cover long-line fragments and their boundaries. Regex
        # search deliberately excludes fragmented lines and reports that gap.
        candidate = carry + text
        found = matcher.search(candidate) if not regex or not continued and item["complete"] and len(item["raw"]) <= REGEX_LINE_BYTES else None
        if regex and (not item["complete"] or len(item["raw"]) > REGEX_LINE_BYTES) and not continued:
            long_regex_lines += 1
        if found and word and not item["complete"] and found.end() == len(candidate):
            # The next fragment must establish a real word ending.
            found = None
        if found and current_match is None:
            current_match = {"source": dict(source), "snapshot": before, "line": item["line"],
                             "offset": max(0, item["offset"] - len(previous_raw_tail)) if continued and found.start() < len(carry) else item["offset"],
                             "text": candidate[max(0, found.start() - 80):found.end() + 256][:2048],
                             "before": list(previous), "after": [], "fragment": continued or not item["complete"]}
            results.append(current_match)
            if context:
                waiting.append(current_match)
            if len(results) >= limit:
                break
        carry = candidate[-max(1024, len(query) * 2):]
        previous_raw_tail = item["raw"][-8192:]
        continued = not item["complete"]
        if item["complete"]:
            previous.append(snippet)
    after = check_snapshot(files, path, before)
    complete = scanned >= before["size"] and long_regex_lines == 0
    return {"source": dict(source), "snapshot": before, "matches": results,
            "scanned": scanned, "size": before["size"], "complete": complete,
            "limited": scanned < before["size"], "long_regex_lines": long_regex_lines,
            "appended": after["size"] > before["size"]}


def search_sources(files, sources, query, **options):
    """Search bounded catalog declarations. Errors remain tied to their source."""
    compile_search(query, **{key: options.get(key, False) for key in ("regex", "case", "word")})
    sources = list(sources)
    from .raven_execution import router_for, AdmissionError
    router = router_for()
    if router.managed:
        if len(sources) > 32:
            raise AdmissionError("Choose at most 32 log sources per request")
        total = sum(snapshot(files, source["path"])["size"] for source in sources)
        if total > 2 << 20:
            blocked = router.require("large-log-search", worker_operation="log-search-files")
            if blocked is not None:
                raise AdmissionError(blocked["summary"])
            if getattr(files, "remote", False):
                raise AdmissionError("Large remote search needs a worker-visible source mapping")
            supplied = {key: options[key] for key in ("regex", "case", "word", "context", "max_results") if key in options}
            receipt = router.dispatch("log-search-files", {"sources": sources, "query": query, "options": supplied})
            if receipt.get("status") == "deferred":
                raise AdmissionError(receipt["summary"])
            return receipt["result"]
    results, reports, seen = [], [], set()
    deadline = time.monotonic() + MAX_SECONDS
    max_results = max(1, min(MAX_RESULTS, int(options.pop("max_results", MAX_RESULTS))))
    for source in list(sources)[:MAX_SOURCES]:
        path = source.get("path") if isinstance(source, dict) else None
        if not isinstance(path, str) or not path or path in seen:
            continue
        seen.add(path)
        _check(options.get("cancel"), deadline)
        try:
            report = search_source(files, source, query, deadline=deadline,
                                   max_results=max_results - len(results), **options)
            results.extend(report["matches"])
            reports.append({key: value for key, value in report.items() if key != "matches"})
        except ScanCancelled:
            raise
        except (OSError, ValueError) as exc:
            reports.append({"source": dict(source), "error": str(exc)[:512], "complete": False})
        if len(results) >= max_results:
            break
    return {"matches": results, "reports": reports, "query": query,
            "complete": len(seen) == len({s.get('path') for s in sources if isinstance(s, dict)}) and all(r.get("complete") for r in reports)}


def locate(files, source, kind, value, *, cancel=None, progress=None):
    """Resolve an absolute source location without using tail-relative indices."""
    path = source["path"]
    before = snapshot(files, path)
    from .raven_execution import router_for, AdmissionError
    if router_for().managed and kind in {"line", "time"} and before["size"] > 2 << 20:
        raise AdmissionError("Absolute line/time lookup exceeds Light's 2 MiB scan; use a byte/percentage window or Heavy search")
    if kind == "byte":
        offset = int(value)
        if offset < 0:
            raise ValueError("Byte position must be nonnegative")
        return read_page(files, path, min(offset, before["size"]), expected=before["ident"], cancel=cancel)
    if kind == "percent":
        number = float(value)
        if not 0 <= number <= 100:
            raise ValueError("Percentage must be between 0 and 100")
        return read_page(files, path, int(before["size"] * number / 100), expected=before["ident"], cancel=cancel)
    if kind == "line":
        wanted = int(value)
        if wanted < 1:
            raise ValueError("Absolute line numbers start at 1")
    elif kind == "time":
        wanted = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    else:
        raise ValueError("Location kind must be line, byte, percent, or time")
    found = None
    deadline = time.monotonic() + MAX_SECONDS
    for item in _iter_lines(files, path, before, cancel=cancel, progress=progress, deadline=deadline):
        if kind == "line" and item["line"] == wanted:
            found = item
            break
        if kind == "time":
            match = re.search(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?", _text(item["raw"])[:256])
            if match:
                try:
                    timestamp = datetime.fromisoformat(match.group().replace("Z", "+00:00"))
                    if (timestamp.tzinfo is None) != (wanted.tzinfo is None):
                        continue
                    if timestamp >= wanted:
                        found = item
                        break
                except ValueError:
                    continue
    check_snapshot(files, path, before)
    if found is None:
        raise ValueError("No matching location within the scan coverage")
    return read_page(files, path, found["offset"], line=found["line"], expected=before["ident"], cancel=cancel)
