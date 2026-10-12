"""Coalesce preference publications on the shared background worker budget.

The UI copies ordinary preference values but does not encode JSON or touch a
file. One accepted writer drains the newest pending value; skipped intermediate
preferences are never externally visible actions. Shutdown collects the writer
before the session closes its worker scheduler.
"""
from __future__ import annotations

import copy
import threading


class UIWriter:
    def __init__(self, store, scheduler):
        self.store = store
        self.lane = scheduler.lane("source")
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.latest = None
        self.future = None
        self.scheduled = False
        self.closed = False
        self.sequence = 0
        self.completed = 0

    def publish(self, data):
        # Workbench save methods return ordinary dict/list/scalar preferences.
        # Detach them while their UI owner is stable, before worker execution.
        value = copy.deepcopy(data)
        with self.lock:
            if self.closed:
                return False
            self.sequence += 1
            self.latest = (self.sequence, value)
            self._schedule()
        return True

    def _schedule(self):
        if self.scheduled or self.latest is None or self.closed:
            return
        self.scheduled = True
        try:
            self.future = self.lane.submit(self._drain)
        except RuntimeError as exc:
            self.scheduled = False
            self.store.ui_state_status = {"state": "pending", "message": "Preferences await a worker: " + str(exc)[:200]}

    def retry(self):
        with self.lock:
            self._schedule()

    def _drain(self):
        while True:
            with self.lock:
                pending, self.latest = self.latest, None
                if pending is None:
                    self.scheduled = False
                    self.changed.notify_all()
                    return
            sequence, value = pending
            try:
                self.store.save_ui(value)
            except Exception as exc:
                self.store.ui_state_status = {"state": "error", "message": "Could not save preferences: " + str(exc)[:240]}
            finally:
                with self.lock:
                    self.completed = sequence
                    self.changed.notify_all()

    def flush(self):
        """Wait for preferences published before this explicit barrier.

        This blocking lifecycle/testing API must not be used from a UI frame.
        Queue rejection or cancellation is reconciled on the calling thread;
        a running writer remains the sole owner of disk publication.
        """
        with self.lock:
            target = self.sequence
        while True:
            with self.lock:
                if self.completed >= target:
                    return
                abandoned = self.future is not None and self.future.done()
                if not self.scheduled or abandoned:
                    self.scheduled = True
                    self.future = None
                    own_drain = True
                else:
                    own_drain = False
                    self.changed.wait(.1)
            if own_drain:
                self._drain()

    def close(self):
        with self.lock:
            self.closed = True
        self.flush()
        self.lane.shutdown(wait=True)


def bind(app, scheduler):
    if not getattr(app, "interactive", False) or not getattr(app.store, "persist", False):
        return None
    if getattr(app, "ui_writer", None) is None:
        app.ui_writer = UIWriter(app.store, scheduler)
    return app.ui_writer


def save(app, data):
    writer = getattr(app, "ui_writer", None)
    return writer.publish(data) if writer is not None else app.store.save_ui(data)


def tick(app):
    writer = getattr(app, "ui_writer", None)
    if writer is not None:
        writer.retry()


def flush(app):
    """Explicit publication barrier; ordinary interactive saves stay deferred."""
    writer = getattr(app, "ui_writer", None)
    if writer is not None:
        writer.flush()


def close(app):
    writer = getattr(app, "ui_writer", None)
    if writer is not None:
        writer.close()
