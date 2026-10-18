"""Readiness hints for the UI owner; producers never perform UI or broker I/O."""
from __future__ import annotations

import selectors
import socket
import threading


class UIWakeup:
    """One coalesced byte per publication burst, with race-safe drain/close.

    A notifier may hold a Store lock: this method only takes a short private
    lock and sends at most one byte on a nonblocking socket. It runs no user
    callbacks. A closed UI is an ordinary no-op for still-draining producers.
    """

    def __init__(self):
        self.reader, self._writer = socket.socketpair()
        self.reader.setblocking(False)
        self._writer.setblocking(False)
        self._lock = threading.Lock()
        self._pending = self._closed = False

    def notify(self, _future=None):
        with self._lock:
            if self._closed or self._pending:
                return
            self._pending = True
            try:
                self._writer.send(b"\0")
            except (BlockingIOError, InterruptedError):
                # Readiness already exists for a full socket; an interrupted
                # first send is also retained as a pending in-process hint.
                pass
            except OSError:
                self._pending = False

    @property
    def pending(self):
        with self._lock:
            return self._pending and not self._closed

    def drain(self):
        with self._lock:
            if self._closed:
                return False
            pending = self._pending
            try:
                self.reader.recv(4096)
            except (BlockingIOError, InterruptedError, OSError):
                pass
            self._pending = False
            return pending

    def close(self):
        with self._lock:
            if not self._closed:
                self._closed = True
                self.reader.close()
                self._writer.close()


def notify(owner):
    """Only our nonblocking primitive is admitted under a publication lock."""
    wakeup = getattr(owner, "ui_wakeup", None)
    if type(wakeup) is UIWakeup:
        wakeup.notify()


class UIWaiter:
    """Select readiness only; ncurses and each existing broker keep byte ownership.

    Registering stdin can fail on embedding/fallback terminals. The caller then
    uses the existing curses timeout. External descriptors are refreshed each
    wait because services may reconnect or exit between UI turns.
    """

    def __init__(self, stdin, wakeup):
        self.wakeup = wakeup
        self.selector = None
        self.available = False
        self._external = set()
        self._stdin_fd = None
        try:
            self.selector = selectors.DefaultSelector()
            self._stdin_fd = self.selector.register(stdin, selectors.EVENT_READ, "stdin").fd
            self.selector.register(wakeup.reader, selectors.EVENT_READ, "publication")
            self.available = True
        except (OSError, ValueError, TypeError, AttributeError):
            if self.selector is not None:
                self.selector.close()

    def input_closed(self):
        """Observe hangup without consuming ncurses-owned input bytes."""
        try:
            import select
            poller = select.poll()
            poller.register(self._stdin_fd, select.POLLIN)
            return any(mask & (select.POLLHUP | select.POLLERR | select.POLLNVAL)
                       for _, mask in poller.poll(0))
        except (OSError, ValueError, TypeError, AttributeError):
            return False

    def wait(self, seconds, providers=()):
        if not self.available:
            return frozenset()
        for fd in self._external:
            try:
                self.selector.unregister(fd)
            except (OSError, ValueError, KeyError):
                pass
        self._external.clear()
        buffered = False
        for provider in providers:
            readiness = getattr(provider, "readiness", None)
            if not callable(readiness):
                continue
            descriptors, ready = readiness()
            buffered = buffered or ready
            for fileobj, events in descriptors:
                try:
                    key = self.selector.register(fileobj, events, "client")
                    self._external.add(key.fd)
                except (OSError, ValueError, KeyError):
                    # A concurrently closed descriptor is observed by its owner
                    # on the next finite maintenance deadline.
                    pass
        pending = self.wakeup.pending
        try:
            events = self.selector.select(0 if buffered or pending else max(0, seconds))
        except InterruptedError:
            return frozenset(("signal",))
        except (OSError, ValueError):
            self.available = False
            return frozenset(("fallback",))
        result = {key.data for key, _ in events}
        if pending:
            result.add("publication")
        if buffered:
            result.add("client")
        if "publication" in result:
            self.wakeup.drain()
        return frozenset(result)

    def close(self):
        self.available = False
        if self.selector is not None:
            self.selector.close()
