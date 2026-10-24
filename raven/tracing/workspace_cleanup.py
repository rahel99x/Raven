"""Small bounded cleanup lane; importing tracing creates no threads."""

from __future__ import annotations

import queue
import threading

_pending = queue.Queue(maxsize=8)
_lock = threading.Lock()
_thread = None


def _drain():
    global _thread
    while True:
        try:
            session = _pending.get(timeout=1)
        except queue.Empty:
            with _lock:
                if _pending.empty():
                    _thread = None
                    return
            continue
        try:
            session.close()
        except Exception as exc:
            # Keep cleanup alive for other owned sessions; callers can inspect
            # the bounded error without a trace containing source data.
            session.cleanup_error = type(exc).__name__ + ": " + str(exc)[:300]
        finally:
            _pending.task_done()


def retire(session):
    """Only owned index/server resources enter this queue, never job control."""
    global _thread
    with _lock:
        try:
            _pending.put_nowait(session)
        except queue.Full:
            immediate = True
        else:
            immediate = False
            if _thread is None:
                _thread = threading.Thread(
                    target=_drain, name="raven-trace-cleanup", daemon=True
                )
                _thread.start()
    if immediate:
        # Admission permits only a bounded number of readers. This fallback
        # bounds cleanup memory even during rapid scripted navigation.
        session.close()
