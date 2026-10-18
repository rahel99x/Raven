"""Generated fragmentation supplements native/PTY pointer ownership regressions."""
from collections import deque
import curses

from hypothesis import given, settings, strategies as st

from raven.screen import _InputReader


class Window:
    def __init__(self):
        self.values = deque()

    def getch(self):
        return self.values.popleft() if self.values else -1

    def keypad(self, enabled):
        pass

    def timeout(self, milliseconds):
        pass


@given(st.integers(0, 199), st.integers(0, 99), st.lists(st.integers(1, 8), min_size=1, max_size=10))
@settings(max_examples=60, deadline=None)
def test_mouse_byte_partitions_do_not_become_keys_or_duplicate_presses(x, y, chunks):
    window = Window()
    reader = _InputReader(window)
    wire = f"\x1b[<0;{x + 1};{y + 1}M".encode()
    offset, index, events = 0, 0, []
    while offset < len(wire):
        size = chunks[index % len(chunks)]
        window.values.extend(wire[offset:offset + size])
        while window.values:
            event = reader.read(curses)
            if event is not None:
                events.append(event)
        offset += size
        index += 1
    assert len(events) == 1 and events[0][0] == "mouse"
    assert events[0][1][1:3] == (x, y)
