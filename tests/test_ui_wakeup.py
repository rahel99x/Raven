"""Publication hints and transport readiness retain their original byte owners."""
import curses
import json
import selectors
import socket
import struct
import threading
import time
from types import SimpleNamespace

import pytest

from raven import screen
from raven.model import Job, Store
from raven.transport.framing import Decoder, FramePolicy
from raven.ui_wakeup import UIWaiter, UIWakeup
from raven.worker_scheduler import WorkerScheduler


def test_many_notifications_coalesce_and_closed_producers_are_safe():
    wake = UIWakeup()
    for _ in range(100000):
        wake.notify()
    assert wake.reader.recv(100) == b"\0"
    with pytest.raises(BlockingIOError):
        wake.reader.recv(1)
    assert wake.drain()
    assert not wake.pending
    wake.notify()
    assert wake.drain()
    wake.close()
    wake.notify()
    assert not wake.drain()


def test_concurrent_publication_drain_cannot_lose_the_final_generation():
    wake = UIWakeup()
    stdin, peer = socket.socketpair()
    waiter = UIWaiter(stdin, wake)
    count = [0]
    finished = threading.Event()

    def publish():
        for generation in range(1, 5001):
            count[0] = generation
            wake.notify()
        finished.set()
        wake.notify()

    thread = threading.Thread(target=publish)
    thread.start()
    seen = 0
    deadline = time.monotonic() + 3
    try:
        while seen != 5000 and time.monotonic() < deadline:
            ready = waiter.wait(.1)
            if "publication" in ready:
                seen = count[0]
        assert seen == 5000 and finished.wait(1)
    finally:
        thread.join(1)
        waiter.close()
        wake.close()
        stdin.close()
        peer.close()


def test_selector_leaves_stdin_and_client_bytes_for_their_owners():
    wake = UIWakeup()
    stdin, keyboard = socket.socketpair()
    client, broker = socket.socketpair()
    waiter = UIWaiter(stdin, wake)
    provider = SimpleNamespace(readiness=lambda: (((client, selectors.EVENT_READ),), False))
    try:
        keyboard.send(b"key")
        broker.send(b"frame")
        assert waiter.wait(.1, (provider,)) == {"stdin", "client"}
        assert stdin.recv(10) == b"key" and client.recv(10) == b"frame"
        broker.close()
        assert "client" in waiter.wait(.1, (provider,))
        assert client.recv(10) == b""
        keyboard.close()
        assert waiter.input_closed()
    finally:
        waiter.close()
        wake.close()
        stdin.close()
        keyboard.close()
        client.close()
        broker.close()


def test_invalid_nonpollable_stdin_uses_fallback():
    wake = UIWakeup()
    waiter = UIWaiter(object(), wake)
    assert not waiter.available
    waiter.close()
    wake.close()


def test_selector_descriptor_exhaustion_uses_fallback(monkeypatch):
    def unavailable():
        raise OSError('No descriptors remain')
    monkeypatch.setattr(selectors, 'DefaultSelector', unavailable)
    waiter = UIWaiter(None, None)
    assert not waiter.available
    waiter.close()


def test_complete_buffered_client_frame_never_sleeps(monkeypatch):
    wake = UIWakeup()
    stdin, peer = socket.socketpair()
    waiter = UIWaiter(stdin, wake)
    provider = SimpleNamespace(readiness=lambda: ((), True))
    delays = []
    monkeypatch.setattr(waiter.selector, 'select', lambda timeout: delays.append(timeout) or [])
    try:
        assert waiter.wait(1, (provider,)) == {'client'}
        assert delays == [0]
    finally:
        waiter.close()
        wake.close()
        stdin.close()
        peer.close()


@pytest.mark.parametrize("data,ready", [
    (b"", False), (b"\0\0", False), (struct.pack("!I", 2) + b"{", False),
    (struct.pack("!I", 2) + b"{}", True), (struct.pack("!I", 0), True),
    (struct.pack("!I", 1001), True),
])
def test_decoder_readiness_does_not_consume_or_parse(data, ready):
    decoder = Decoder(FramePolicy("test", 1000, 2000), json.loads)
    decoder.feed(data)
    assert decoder.has_complete_frame is ready
    assert bytes(decoder.buffer) == data


def test_store_publications_and_worker_failure_wake_the_ui():
    store = Store(persist=False)
    wake = store.ui_wakeup = UIWakeup()
    scheduler = WorkerScheduler(workers=1, lanes=("source",))
    try:
        store.apply_jobs([Job("1", "job", "cpu", "RUNNING")])
        assert wake.drain()
        scheduler.ui_wakeup = wake
        lane = scheduler.lane("source")
        future = lane.submit(lambda: 1 / 0)
        with pytest.raises(ZeroDivisionError):
            future.result(timeout=1)
        # result() may wake before done callbacks finish; shutdown joins them.
        scheduler.shutdown(wait=True)
        assert wake.drain()
    finally:
        scheduler.shutdown(wait=True)
        wake.close()


def test_arbitrary_callback_is_not_called_under_store_lock():
    store = Store(persist=False)
    store.ui_wakeup = SimpleNamespace(notify=lambda: pytest.fail("untrusted callback"))
    store.apply_jobs([Job("1", "job", "cpu", "RUNNING")])


def test_escape_deadline_does_not_spin_for_partial_control_or_utf8():
    window = SimpleNamespace(get_wch=lambda: None)
    reader = screen._InputReader(window)
    reader.escape_time, reader.escape_grace = 10, .2
    reader.escape = "\x1b"
    assert reader.deadline() == 10.2
    for fragment in ("\x1b[", "\x1b[<0;", "\x1bO", ""):
        reader.escape = fragment
        assert reader.deadline() == float("inf")
    reader.utf8.decode(b"\xe2")
    assert reader.pending and reader.deadline() == float("inf")


def test_buffered_curses_input_precedes_selector_wait(monkeypatch):
    window = SimpleNamespace(timeout=lambda value: None)
    waiter = SimpleNamespace(available=True, wait=lambda *args: pytest.fail("buffered input delayed"))
    monkeypatch.setattr(screen, "_read_input", lambda *_: ("q", None))
    assert screen._wait_for_input(None, window, curses, None, waiter) == ("q", None)


def test_publication_reschedules_maintenance_without_touching_input(monkeypatch):
    window = SimpleNamespace(timeout=lambda value: None)
    waiter = SimpleNamespace(available=True, wait=lambda *args: {"publication"})
    cache = SimpleNamespace(wait_ms=lambda: 200, next_maintenance=100)
    monkeypatch.setattr(screen, "_read_input", lambda *_: None)
    assert screen._wait_for_input(SimpleNamespace(), window, curses, cache, waiter) is None
    assert cache.next_maintenance == 0


def test_visible_progress_uses_actual_boundaries_and_no_offscreen_wakes():
    from raven.job_progress import next_animation_deadline
    app = SimpleNamespace(tab='jobs', mode='main', theme='dark', animations_enabled=True,
                          job_progress_state={'animation_context': ('jobs', 'main', 'dark', True),
                                              'animation_slots': ((1, 2, 'clock'), (2, 2, 'hourglass'))})
    assert next_animation_deadline(app, now=10.1) == pytest.approx(10.4)
    app.job_progress_state['animation_slots'] = ((2, 2, 'hourglass'),)
    assert next_animation_deadline(app, now=10.5) == pytest.approx(11.2)
    app.job_progress_state['animation_slots'] = ()
    assert next_animation_deadline(app, now=10.1) == float('inf')
