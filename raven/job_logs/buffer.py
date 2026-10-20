"""Bounded incremental log snapshots and logical source-line selection.

Explicit noninteractive reads are synchronous. Interactive reads publish
immutable snapshots through the existing research worker, so shared filesystem
and network latency never block terminal rendering or keyboard navigation.
"""

from __future__ import annotations

import codecs
import copy
import re
import time
from typing import List, Optional, Tuple

from .text import display_text
from ..local_files import LocalFiles


class LogBuffer:
    def __init__(
        self, path: str, max_bytes: int = 32 << 20, files: Optional[LocalFiles] = None
    ):
        self.path = path
        self.max_bytes = max(1, int(max_bytes))
        self.files = files or LocalFiles()
        self.last_refresh = 0.0
        self.lines: List[str] = []
        self.raw_lines: List[
            bytes
        ] = []  # unmodified completed lines, without their trailing LF
        self.size = 0  # bytes of the file consumed so far
        self.ident: Optional[Tuple[int, int]] = None  # (device, inode) of the file read
        self.partial = ""  # an unterminated last line kept until it is completed
        self.truncated = (
            False  # the beginning of the file was skipped (larger than max_bytes)
        )
        self.skipped_bytes = 0
        self.error = ""
        self.reloads = 0
        self._partial_raw = b""
        self._line_bytes: List[int] = []
        self._retained_bytes = 0
        self._count_key = None
        self._count_value = 0
        self.loading = False
        self.source_updated = None  # mutation metadata of the published worker snapshot
        # A worker snapshot is a new Python object, while its logical retained
        # line identities survive append-only refreshes.
        self._session_token = object()

    def _worker_copy(self):
        """Copy mutable containers on the worker, never on a terminal frame."""
        clone = copy.copy(self)
        clone.lines = self.lines[:]
        clone.raw_lines = self.raw_lines[:]
        clone._line_bytes = self._line_bytes[:]
        return clone

    # ---- reading ----------------------------------------------------------------------------------
    def _reset(self):
        self.source_updated = None
        self.lines, self.size, self.partial, self.truncated, self.skipped_bytes = (
            [],
            0,
            "",
            False,
            0,
        )
        self._partial_raw, self._line_bytes, self.raw_lines, self._retained_bytes = (
            b"",
            [],
            [],
            0,
        )

    def refresh(self) -> bool:
        """Bring the buffer up to date; True when anything changed.  A remote file is not re-checked more often
        than ``files.min_refresh`` seconds."""
        if (
            self.files.min_refresh
            and time.time() - self.last_refresh < self.files.min_refresh
        ):
            return False
        self.last_refresh = time.time()
        try:
            size, ident = self.files.stat(self.path)
        except OSError as e:
            self.error = str(e)
            return False
        self.error = ""
        if (
            self.ident != ident or size < self.size
        ):  # a new file under the same name, or truncation
            self.reloads += 1 if self.ident is not None else 0
            self._reset()
            self.ident = ident
        if size == self.size:
            return False
        offset = max(self.size, size - self.max_bytes)
        jumped = offset > self.size
        # Read one preceding byte to tell a line boundary from a partial first line.
        read_offset = offset - 1 if jumped else offset
        try:
            data = self.files.read(self.path, read_offset, size - read_offset)
        except OSError as e:
            self.error = str(e)
            return False
        consumed = len(data)
        if jumped:
            self.lines, self.raw_lines, self._line_bytes, self._partial_raw = (
                [],
                [],
                [],
                b"",
            )
            self._retained_bytes = 0
            self.skipped_bytes = offset
            self.truncated = True
            if data.startswith(b"\n"):
                data = data[1:]
            else:
                data = data[1:]
                nl = data.find(b"\n")
                if nl >= 0:
                    self.skipped_bytes += nl + 1
                    data = data[nl + 1 :]
        # A file may shrink after stat; only advance by the bytes actually read.
        self.size = read_offset + consumed
        self._retained_bytes += len(data)
        raw = self._partial_raw + data
        # A newline-only burst must not allocate millions of temporary line objects.
        parts = raw.rsplit(b"\n", 200_001)
        if len(parts) > 200_001:
            skipped = len(parts.pop(0)) + 1
            self._retained_bytes -= skipped
            self.skipped_bytes += skipped
            self.truncated = True
        self._partial_raw = parts.pop()
        self.raw_lines.extend(parts)
        self.lines.extend(
            display_text(line.decode("utf-8", "replace").rstrip("\r")) for line in parts
        )
        self._line_bytes.extend(len(line) + 1 for line in parts)
        drop = 0
        while drop < len(self.lines) and (
            self._retained_bytes > self.max_bytes or len(self.lines) - drop > 200_000
        ):
            self._retained_bytes -= self._line_bytes[drop]
            self.skipped_bytes += self._line_bytes[drop]
            drop += 1
        if drop:
            del self.lines[:drop]
            del self.raw_lines[:drop]
            del self._line_bytes[:drop]
            self.truncated = True
        if len(self._partial_raw) > self.max_bytes:
            skipped = len(self._partial_raw) - self.max_bytes
            self._partial_raw = self._partial_raw[-self.max_bytes :]
            self._retained_bytes -= skipped
            self.skipped_bytes += skipped
            self.truncated = True
        # Keep an incomplete UTF-8 character for the next append instead of corrupting it.
        self.partial = display_text(
            codecs.getincrementaldecoder("utf-8")("replace").decode(
                self._partial_raw, final=False
            )
        )
        return True

    # ---- windows ----------------------------------------------------------------------------------
    @property
    def total(self) -> int:
        return len(self.lines) + (1 if self._partial_raw else 0)

    def all_lines(self) -> List[str]:
        return self.lines + ([self.partial] if self._partial_raw else [])

    def raw_range(self, first: int, last: int) -> bytes:
        """Unmodified bytes for retained logical lines, including their actual endings.

        Rendering uses normalized strings. Copying instead keeps tabs, CRLF,
        and the complete width of each source line; an unterminated last line
        receives no invented newline. The selected range alone is assembled.
        """
        if self.error or self.total == 0:
            return b""
        first, last = sorted((first, last))
        first, last = max(0, first), min(self.total - 1, last)
        if first > last:
            return b""
        completed = len(self.raw_lines)
        if first >= completed:
            return self._partial_raw
        data = b"\n".join(self.raw_lines[first : min(last + 1, completed)]) + b"\n"
        return data + self._partial_raw if last >= completed else data

    def clamp_top(self, top: Optional[int], n: int) -> Optional[int]:
        """``top`` as an absolute line index for a page of ``n`` lines; None means following (the last page)."""
        total = self.total
        if top is None or total <= n:
            return None
        return max(0, min(top, total - n))

    def window(self, top: Optional[int], n: int) -> Tuple[List[str], int]:
        """(lines, first index) of the page: the last ``n`` lines when ``top`` is None."""
        total = self.total
        n = max(1, n)
        if top is None or total <= n:
            start = max(0, total - n)
        else:
            start = max(0, min(top, total - n))
        page = self.lines[start : start + n]
        if self._partial_raw and start + n > len(self.lines):
            page.append(self.partial)
        return page, start

    def find(
        self, pattern: str, from_index: Optional[int] = None, backwards: bool = False
    ) -> Optional[int]:
        """Index of the next line matching ``pattern`` (a regular expression, case-insensitive; a plain string
        when it is not a valid one) before or after ``from_index``; None when nothing matches."""
        try:
            rx = re.compile(pattern, re.IGNORECASE)
        except re.error:
            rx = re.compile(re.escape(pattern), re.IGNORECASE)
        lines = self.all_lines()
        if backwards:
            rng = range(
                (len(lines) - 1) if from_index is None else from_index - 1, -1, -1
            )
        else:
            rng = range(0 if from_index is None else from_index + 1, len(lines))
        for i in rng:
            if rx.search(lines[i]):
                return i
        return None

    def count(self, pattern: str) -> int:
        key = pattern, self.ident, self.size, self.reloads, self.skipped_bytes
        if key == self._count_key:
            return self._count_value
        try:
            rx = re.compile(pattern, re.IGNORECASE)
        except re.error:
            rx = re.compile(re.escape(pattern), re.IGNORECASE)
        count = sum(1 for line in self.lines if rx.search(line))
        count += bool(self._partial_raw and rx.search(self.partial))
        self._count_key, self._count_value = key, count
        return count


class LogSession:
    """The log tab's state across frames: one buffer per path, the page position, the search."""

    def __init__(self, max_bytes: int = 32 << 20, files: Optional[LocalFiles] = None):
        self.max_bytes = max_bytes
        self.files = files or LocalFiles()
        self.buffers: dict = {}
        self.path: str = ""
        self.top: Optional[int] = None  # None: following the end
        self.page = 20  # lines on the last rendered page
        self.search = ""
        self.match: Optional[int] = None
        self.wrap = False  # long lines wrapped to the width instead of cut
        self.which = "out"  # out | err: the job's stdout or stderr
        self.file_index = (
            0  # 0: stdout / stderr; 1..: the other files of the job's log directory
        )
        self.candidates: dict = {}  # job id -> (time, [paths]) of the other files
        self.bookmarks: dict = {}  # path -> sorted line indices
        self.last_bookmark: Optional[int] = (
            None  # the bookmark the last jump landed on (the next jump continues from it)
        )
        self.cursor: Optional[int] = (
            None  # retained logical line, independent of wrapped screen rows
        )
        self.selection_anchor: Optional[int] = None
        self.selection_end: Optional[int] = None
        self.selection_path: Optional[str] = None
        self.selection_all = False
        self.selection_generation = 0
        self._buffer_token = None
        self.browser = False
        self.browse_return = False
        self.browser_cursor = 0
        self.browser_top = 0
        self.browser_page = 10
        self.file_filter = ""
        self.entry = None
        self.entries = []
        self.catalog = None
        self._async_pending = None
        self._async_active = None
        self._async_generation = 0
        self._async_attempts = {}
        self.polling_multiplier = 1

    # ---- bookmarks ------------------------------------------------------------------------------
    def toggle_bookmark(self, path: str, index: int) -> bool:
        marks = self.bookmarks.setdefault(path, [])
        if index in marks:
            marks.remove(index)
            if not marks:
                del self.bookmarks[path]
            return False
        marks.append(index)
        marks.sort()
        return True

    def next_bookmark(self, path: str, after: Optional[int]) -> Optional[int]:
        marks = self.bookmarks.get(path, [])
        if not marks:
            return None
        for i in marks:
            if after is None or i > after:
                return i
        return marks[0]  # wrap around

    def current_line(self, buf: Optional[LogBuffer]) -> Optional[int]:
        """The logical cursor, visible search/bookmark, or natural viewport position."""
        self.sync_buffer(buf)
        if buf is None or buf.error or buf.total == 0:
            return None
        if self.cursor is not None:
            return self.cursor
        lines, start = buf.window(self.top, self.page)
        if self.match is not None and start <= self.match < start + len(lines):
            return self.match
        if self.last_bookmark is not None and start <= self.last_bookmark < start + len(
            lines
        ):
            return self.last_bookmark
        return start if self.top is not None else start + len(lines) - 1

    def buffer(
        self, path: str, *, worker=None, background=False
    ) -> Optional[LogBuffer]:
        """Get a synchronous buffer or a published interactive file snapshot.

        ``worker`` follows ResearchHub's start_task/poll_task contract: completion
        executes on the UI thread. With a worker, background reads never perform
        file I/O here, including cold loads and while the shared worker is busy.
        Remote reads without a worker remain visibly pending; local callers
        without one retain the synchronous API.
        """
        if not path:
            if self._async_active is not None:
                self._async_generation += 1
                self._async_active = None
            self.sync_buffer(None)
            return None
        if path != self.path:
            self.path, self.top, self.match, self.last_bookmark = path, None, None, None
            self.clear_selection(reset_cursor=True)
            self._buffer_token = None
        buf = self.buffers.get(path)
        if (
            buf is None
            or buf.files is not self.files
            or buf.max_bytes != max(1, int(self.max_bytes))
        ):
            buf = self.buffers[path] = LogBuffer(path, self.max_bytes, self.files)
            if len(self.buffers) > getattr(self, "max_buffers", 8):
                oldest = next(iter(self.buffers))
                if oldest != path:
                    del self.buffers[oldest]
        if background and (worker is not None or getattr(self.files, "remote", False)):
            self._request_background(path, buf, worker)
            # Publication may happen only in worker.poll_task, outside this
            # function. A cold snapshot is visibly loading, never an empty file.
            buf = self.buffers[path]
        else:
            buf.loading = False
            buf.refresh()
        self.sync_buffer(buf)
        return buf

    def invalidate_remote(self):
        """Explicit refresh discards pending publications and file polling deadlines."""
        self._async_generation += 1
        self._async_attempts.clear()

    def _request_background(self, path, base, worker):
        files = self.files
        key = (id(files), path, base.max_bytes)
        if key != self._async_active:
            self._async_active = key
            self._async_generation += 1
        if base.ident is None and not base.error:
            base.loading = True
        interval = max(
            1.5 if getattr(files, "remote", False) else 0.5,
            float(getattr(files, "min_refresh", 0)),
        )
        from ..cadence import file_interval

        interval = file_interval(
            interval,
            self.polling_multiplier,
            remote=bool(getattr(files, "remote", False)),
        )
        now = time.monotonic()
        if (
            worker is None
            or self._async_pending is not None
            or now - self._async_attempts.get(key, -interval) < interval
        ):
            return
        token = (self._async_generation, key)
        self._async_pending = token

        def metadata():
            if hasattr(files, "snapshot_stat"):
                result = files.snapshot_stat(path)
                size, ident = result.get("size"), result.get("ident")
            else:
                size, ident = files.stat(path)
                result = {"size": size, "ident": ident}
            if (
                type(size) is not int
                or size < 0
                or not isinstance(ident, (tuple, list))
                or len(ident) != 2
                or any(type(v) is not int or v < 0 for v in ident)
            ):
                raise ValueError("Invalid log metadata")
            return dict(result, ident=tuple(ident))

        def read():
            try:
                before = metadata()
                # A producer can rewrite a file without changing its inode or
                # byte size. Published mutation metadata detects that case;
                # only the private worker copy is reset and reloaded.
                rewritten = (
                    before["ident"] == base.ident
                    and before["size"] == base.size
                    and base.source_updated is not None
                    and before.get("updated") is not None
                    and before["updated"] != base.source_updated
                )
                # Unchanged polls retain the shallow-copy optimization.
                clone = (
                    base._worker_copy()
                    if rewritten
                    or before["ident"] != base.ident
                    or before["size"] != base.size
                    else copy.copy(base)
                )
                if rewritten:
                    clone._reset()
                    clone.reloads += 1
                clone.loading = False

                class SnapshotFiles:
                    # LogBuffer uses this exact target snapshot rather than a
                    # second SSH stat (which could describe a symlink instead).
                    min_refresh = 0.0
                    remote = getattr(files, "remote", False)

                    def stat(_self, _path):
                        return before["size"], before["ident"]

                    def read(_self, selected, offset, length):
                        return files.read(selected, offset, length)

                clone.files = SnapshotFiles()
                changed = clone.refresh()
                if clone.error:
                    raise OSError(clone.error)
                if changed or rewritten:
                    after = metadata()
                    if (
                        after["ident"] != before["ident"]
                        or after["size"] < before["size"]
                        or (
                            after["size"] == before["size"]
                            and after.get("updated") != before.get("updated")
                        )
                    ):
                        raise ValueError(
                            "Log changed during inspection; refresh to retry"
                        )
                    if clone.size != before["size"]:
                        raise ValueError("Log read was incomplete; refresh to retry")
                clone.source_updated = before.get("updated")
            except Exception as exc:
                # Keep the previous complete bytes, and mark them unavailable
                # for selection rather than publish a half-refreshed buffer.
                clone = base._worker_copy()
                clone.loading, clone.error = False, str(exc)[:512]
            finally:
                clone.files = files
            return clone

        def complete(result):
            if self._async_pending == token:
                self._async_pending = None
            if (
                token != (self._async_generation, self._async_active)
                or self.path != path
                or self.files is not files
                or self.buffers.get(path) is not base
            ):
                return
            if isinstance(result, Exception):
                # Ordinary backend errors are handled on the worker. Keep this
                # exceptional completion path constant-time on the UI thread.
                error = str(result)[:512]
                result = LogBuffer(path, base.max_bytes, files)
                result._session_token, result.ident = base._session_token, base.ident
                result.loading, result.error = False, error
            self.buffers[path] = result
            self.sync_buffer(result)

        try:
            admitted = worker.start_task(read, complete)
        except Exception:
            self._async_pending = None
            raise
        if not admitted:
            self._async_pending = None
            return
        self._async_attempts[key] = now
        if len(self._async_attempts) > 8:
            del self._async_attempts[next(iter(self._async_attempts))]

    # ---- logical cursor and line selection -------------------------------------------------------
    def clear_selection(self, reset_cursor: bool = False):
        if self.selection_path is not None:
            self.selection_generation += 1
        self.selection_anchor = self.selection_end = self.selection_path = None
        self.selection_all = False
        if reset_cursor:
            self.cursor = None

    @property
    def selection_active(self) -> bool:
        return self.selection_path == self.path and (
            self.selection_all
            or (self.selection_anchor is not None and self.selection_end is not None)
        )

    def sync_buffer(self, buf: Optional[LogBuffer]):
        """Invalidate positions when file identity or retained-line numbering changes.

        Appending without dropping a prefix preserves a paused cursor and range.
        Rotation, truncation, buffer replacement, tail eviction, and path changes
        clear stale positions before any copy or rendering can use them.
        """
        if buf is None:
            self.clear_selection(reset_cursor=True)
            self._buffer_token = None
            return
        if buf.path != self.path:
            self.path, self.top, self.match, self.last_bookmark = (
                buf.path,
                None,
                None,
                None,
            )
            self.clear_selection(reset_cursor=True)
            self._buffer_token = None
        token = (buf._session_token, buf.ident, buf.reloads, buf.skipped_bytes)
        if self._buffer_token is not None and token != self._buffer_token:
            self.clear_selection(reset_cursor=True)
            self.top, self.match, self.last_bookmark = None, None, None
        self._buffer_token = token
        if buf.error or buf.total == 0:
            self.clear_selection(reset_cursor=True)
            return
        if self.cursor is not None:
            self.cursor = max(0, min(buf.total - 1, self.cursor))
            self._show_cursor(buf)

    def _show_cursor(self, buf: LogBuffer):
        if self.cursor is None:
            return
        page = max(1, self.page)
        start = self.top if self.top is not None else max(0, buf.total - page)
        start = max(0, min(start, max(0, buf.total - page)))
        if self.cursor < start:
            start = self.cursor
        elif self.cursor >= start + page:
            start = self.cursor - page + 1
        self.top = max(0, min(start, max(0, buf.total - page)))

    def begin_selection(
        self, buf: Optional[LogBuffer], index: Optional[int] = None
    ) -> bool:
        self.sync_buffer(buf)
        if buf is None or buf.error or buf.total == 0:
            return False
        index = self.current_line(buf) if index is None else index
        if index is None:
            return False
        self.cursor = max(0, min(buf.total - 1, index))
        self.selection_anchor = self.selection_end = self.cursor
        self.selection_path, self.selection_all = self.path, False
        self.selection_generation += 1
        self._show_cursor(buf)
        return True

    def select_all(self, buf: Optional[LogBuffer]) -> bool:
        self.sync_buffer(buf)
        if buf is None or buf.error or buf.total == 0:
            return False
        self.selection_anchor = self.selection_end = None
        self.selection_path, self.selection_all = self.path, True
        self.selection_generation += 1
        return True

    def move_cursor(self, action: str, buf: Optional[LogBuffer]) -> Optional[int]:
        self.sync_buffer(buf)
        if buf is None or buf.error or buf.total == 0:
            return None
        current = self.current_line(buf)
        if current is None:
            return None
        page = max(1, self.page - 1)
        targets = {
            "up": current - 1,
            "down": current + 1,
            "page_up": current - page,
            "page_down": current + page,
            "home": 0,
            "end": buf.total - 1,
            "follow": buf.total - 1,
        }
        if action not in targets:
            return current
        target = max(0, min(buf.total - 1, targets[action]))
        self.match = self.last_bookmark = None
        if action in ("end", "follow") and not self.selection_active:
            self.cursor, self.top = None, None
            return target
        self.cursor = target
        if self.selection_active and not self.selection_all:
            self.selection_end = target
        self._show_cursor(buf)
        return target

    def is_selected(self, index: int) -> bool:
        if not self.selection_active:
            return False
        if self.selection_all:
            return True
        return (
            min(self.selection_anchor, self.selection_end)
            <= index
            <= max(self.selection_anchor, self.selection_end)
        )

    def selection_bytes(self, buf: Optional[LogBuffer]) -> bytes:
        self.sync_buffer(buf)
        if buf is None or not self.selection_active or self.selection_all:
            return b""
        return buf.raw_range(self.selection_anchor, self.selection_end)

    def selection_text(self, buf: Optional[LogBuffer]) -> str:
        return self.selection_bytes(buf).decode("utf-8", "replace")

    @property
    def following(self) -> bool:
        return self.top is None

    def scroll(self, delta: int, buf: Optional[LogBuffer]):
        """Move the page by ``delta`` lines (negative: up); reaching the end resumes following."""
        if buf is None:
            return
        total, n = buf.total, self.page
        cur = self.top if self.top is not None else max(0, total - n)
        new = cur + delta
        if new >= total - n:
            self.top = None
        else:
            self.top = max(0, new)

    def goto(self, index: Optional[int], buf: Optional[LogBuffer]):
        self.sync_buffer(buf)
        if buf is None or buf.error or buf.total == 0:
            return
        if index is None:
            self.cursor, self.top = None, None
            return
        self.cursor = max(0, min(buf.total - 1, index))
        if self.selection_active and not self.selection_all:
            self.selection_end = self.cursor
        self.top = max(
            0, min(self.cursor - self.page // 2, max(0, buf.total - self.page))
        )

    def find_next(
        self, buf: Optional[LogBuffer], backwards: bool = False
    ) -> Optional[int]:
        if buf is None or not self.search:
            return None
        start = self.match if self.match is not None else self.cursor
        if start is None and self.top is not None:
            start = self.top + self.page
        i = buf.find(self.search, start, backwards=backwards)
        if i is None and start is not None:  # wrap around
            i = buf.find(self.search, None, backwards=backwards)
        if i is not None:
            self.match = i
            self.goto(i, buf)
        return i
