"""Read-only timed terminal recording player, isolated from Raven's UI loop.

Only this fixed player is executed. Recorded output is parsed as terminal data;
it is never a shell script or a command queue. File reads and expensive seeks
run in the player's owned process, not the dashboard or its sampling workers.
"""
from __future__ import annotations

import argparse
import codecs
import math
import os
from pathlib import Path
import select
import shutil
import sys
import termios
import time
import tty

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from raven import layout as L
from raven.palette import ansi_codes
from raven.terminal_recording import MAX_EVENT, MAX_EVENTS, MAX_FILE, MAX_SECONDS, read_recording


class Playback:
    """Incremental timeline replay with bounded per-tick parsing work."""
    def __init__(self, header, events):
        from raven.terminal_emulator import MAX_COLS, MAX_ROWS, TerminalEngine
        self.engine_type = TerminalEngine
        self.rows, self.cols = header["rows"], header["cols"]
        if (not isinstance(events, (tuple, list)) or len(events) > MAX_EVENTS):
            raise ValueError("Replay events exceed the supported recording budget")
        previous, total = 0.0, 0
        for event in events:
            if not isinstance(event, dict):
                raise ValueError("Invalid replay event")
            stamp = event.get("t")
            if (isinstance(stamp, bool) or not isinstance(stamp, (float, int)) or
                    not previous <= stamp <= MAX_SECONDS or not math.isfinite(stamp)):
                raise ValueError("Replay times must be finite and ordered")
            previous = stamp
            if event.get("event") == "output":
                payload = event.get("data")
                if not isinstance(payload, bytes) or len(payload) > MAX_EVENT:
                    raise ValueError("Replay output exceeds its chunk budget")
                total += len(payload)
                if total > MAX_FILE:
                    raise ValueError("Replay output exceeds its byte budget")
            elif event.get("event") == "resize":
                if (type(event.get("rows")) is not int or type(event.get("cols")) is not int or
                        not 1 <= event["rows"] <= MAX_ROWS or not 1 <= event["cols"] <= MAX_COLS):
                    raise ValueError("Replay viewport exceeds the supported 160 rows by 400 columns")
            else:
                raise ValueError("Replay accepts only recorded output and resize events")
        self.events = tuple(dict(event) for event in events)
        self.duration = float(events[-1]["t"]) if events else 0.0
        self.speed = 1.0
        self.playing = True
        self.scroll = 0
        self.seek(0.0)

    def seek(self, position):
        if not math.isfinite(float(position)):
            raise ValueError("Replay position must be finite")
        position = max(0.0, min(self.duration, float(position)))
        if (not hasattr(self, "engine") or position <= self.position
                or position < self.target):
            self.engine = self.engine_type(self.rows, self.cols, history_limit=1)
            self.index = self.offset = 0
            self.position = 0.0
        self.target = position
        self.anchor_time = time.monotonic()
        self.anchor_position = position
        self.scroll = min(self.scroll, self.engine.rows - 1)

    def toggle(self):
        self.anchor_position = self.target
        self.anchor_time = time.monotonic()
        self.playing = not self.playing

    def change_speed(self, multiplier):
        if not math.isfinite(float(multiplier)) or multiplier <= 0:
            raise ValueError("Replay speed multiplier must be positive and finite")
        self.anchor_position = self.target
        self.anchor_time = time.monotonic()
        self.speed = max(.125, min(16.0, self.speed * multiplier))

    def tick(self, now=None, *, budget=.012):
        now = time.monotonic() if now is None else now
        if not math.isfinite(float(now)) or not math.isfinite(float(budget)):
            raise ValueError("Replay clock and processing budget must be finite")
        if self.playing:
            self.target = max(0.0, min(self.duration, self.anchor_position + (now-self.anchor_time)*self.speed))
        deadline = time.monotonic() + max(.001, min(.05, budget))
        changed = False
        count = 0
        while self.index < len(self.events) and count < 256 and time.monotonic() < deadline:
            event = self.events[self.index]
            if event["t"] > self.target:
                break
            if event["event"] == "output":
                data = event["data"]
                part = data[self.offset:self.offset+1024]
                self.engine.feed(part)
                self.offset += len(part)
                changed = True
                if self.offset < len(data):
                    count += 1
                    continue
            elif event["event"] == "resize":
                self.engine.resize(event["rows"], event["cols"])
                self.scroll = min(self.scroll, self.engine.rows - 1)
                changed = True
            self.position = float(event["t"])
            self.index += 1
            self.offset = 0
            count += 1
        if self.index == len(self.events):
            self.playing = False
        # Replies belong to a historical application that is no longer here.
        # They are discarded as data, never sent to a shell or the outer TTY.
        self.engine.take_replies()
        return changed

    def handle(self, key):
        if key in ("q", "\x03", "\x04"):
            return False
        if key == " ":
            self.toggle()
        elif key in ("h", "l"):
            self.seek(self.target + (-5 if key == "h" else 5))
        elif key in ("g", "G"):
            self.seek(0 if key == "g" else self.duration)
        elif key in ("+", "=", "-"):
            self.change_speed(.5 if key == "-" else 2)
        elif key in ("j", "k"):
            self.scroll = max(0, min(self.engine.rows-1, self.scroll + (1 if key == "j" else -1)))
        return True


class PlaybackInput:
    """Frame outer controls before recognizing ASCII transport hotkeys.

    Mouse reports, replies, Alt chords and pasted text are never interpreted as
    a sequence of replay commands. Only complete plain input or supported arrow
    keys can control playback. All accumulated protocol state is bounded.
    """
    def __init__(self):
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.state = "plain"
        self.value = ""
        self.osc = False

    def feed(self, data):
        keys = []
        for char in self.decoder.decode(data):
            if self.state == "paste":
                self.value = (self.value + char)[-6:]
                if self.value == "\x1b[201~":
                    self.state, self.value = "plain", ""
                continue
            if self.state in ("string", "string_escape"):
                if (char in "\x18\x1a\x9c" or (self.osc and char == "\x07") or
                        (self.state == "string_escape" and char == "\\")):
                    self.state = "plain"
                else:
                    self.state = "string_escape" if char == "\x1b" else "string"
                continue
            if self.state == "csi":
                if char in "\x18\x1a":
                    self.state, self.value = "plain", ""
                elif char == "\x1b":
                    self.state, self.value = "escape", ""
                elif "@" <= char <= "~":
                    code = self.value + char
                    self.state, self.value = "plain", ""
                    if code == "200~":
                        self.state = "paste"
                    elif code in ("A", "B", "C", "D", "H", "F"):
                        keys.append({"A": "k", "B": "j", "C": "l", "D": "h", "H": "g", "F": "G"}[code])
                elif len(self.value) < 64:
                    self.value += char
                else:
                    self.value = "!" * 64
                continue
            if self.state == "escape":
                if char == "[":
                    self.state, self.value = "csi", ""
                elif char in "]PX^_":
                    self.state, self.osc = "string", char == "]"
                elif char in "O()#%":
                    self.state = "one"
                elif char != "\x1b":
                    self.state = "plain"
                continue
            if self.state == "one":
                self.state = "plain"
                continue
            if char == "\x1b":
                self.state = "escape"
            elif char == "\x9b":
                self.state, self.value = "csi", ""
            elif char in "\x90\x98\x9d\x9e\x9f":
                self.state, self.osc = "string", char == "\x9d"
            elif char in "qhlgG+=-jk \x03\x04":
                keys.append(char)
        return keys


def render(player, rows, cols, *, cache=None):
    """Generate only known screen/style controls from parsed, clipped cells."""
    snapshot = player.engine.snapshot(include_history=False)
    rows, cols = max(1, min(160, rows)), max(2, min(400, cols))
    state = "playing" if player.playing else "paused"
    catching_up = player.index < len(player.events) and player.events[player.index]["t"] <= player.target
    label = "seeking" if catching_up else state
    header = f"Replay {label} {player.target:.1f}/{player.duration:.1f}s {player.speed:g}x"
    help_line = "Space play/pause | h/l seek 5s | g/G ends | +/- speed | j/k scroll | q close"
    body = snapshot["lines"][player.scroll:player.scroll+max(0, rows-2)]
    lines = [[(header, "bold")], [(help_line, "dim")]] + body
    lines.extend([] for _ in range(max(0, rows-len(lines))))
    repaint = cache is None or cache.get("geometry") != (rows, cols)
    previous = () if repaint else cache.get("rows", ())
    current = []
    out = ["\x1b[?25l\x1b[H"] if repaint else []
    for index, row in enumerate(lines[:rows]):
        key = tuple(tuple(part) for part in row)
        current.append(key)
        if not repaint and index < len(previous) and key == previous[index]:
            continue
        out.append(f"\x1b[{index+1};1H\x1b[0m\x1b[2K")
        for text, style in L.clip_row(row, cols):
            # Belt and braces: no child control byte crosses the emulator.
            text = "".join(char for char in text if char.isprintable() or char == " ")
            codes = ansi_codes(style, color_depth=256, theme="terminal")
            out.extend(("\x1b[0m", "\x1b[" + codes + "m" if codes else "", text))
    if out:
        out.append("\x1b[0m")
    if cache is not None:
        cache.update(geometry=(rows, cols), rows=current)
    return "".join(out)


def start(app, path):
    from raven.terminal_ui import open_session
    return open_session(app, argv=[sys.executable, str(Path(__file__).resolve()), os.fspath(path)],
                        label="Replay: " + Path(path).name, kind="replay",
                        metadata={"read_only": True, "host": "local recording", "recording": os.fspath(path)})


def cli(argv=None):
    parser = argparse.ArgumentParser(prog="raven terminal replay", description=__doc__)
    parser.add_argument("path")
    args = parser.parse_args(argv)
    try:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            from raven.terminal_fusion import recording_text
            _, events = read_recording(args.path)
            print(recording_text(events))
            return 0
        print("Loading read-only recording; no commands will be executed...", flush=True)
        header, events = read_recording(args.path)
        player = Playback(header, events)
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        controls = PlaybackInput()
        try:
            tty.setcbreak(fd)
            sys.stdout.write("\x1b[?1049h\x1b[?2004h\x1b[2J")
            running = True
            next_paint = 0.0
            dirty, geometry, last_label = True, None, None
            render_cache = {}
            while running:
                dirty = player.tick() or dirty
                now = time.monotonic()
                if now >= next_paint:
                    try:
                        size = os.get_terminal_size(sys.stdout.fileno())
                    except OSError:
                        size = shutil.get_terminal_size((80, 24))
                    label = (player.playing, round(player.target, 1), player.speed, player.scroll)
                    if dirty or size != geometry or label != last_label:
                        sys.stdout.write(render(player, size.lines, size.columns, cache=render_cache))
                        sys.stdout.flush()
                        geometry, last_label, dirty = size, label, False
                    next_paint = now + .05
                readable, _, _ = select.select([fd], [], [], .01)
                if readable:
                    data = os.read(fd, 4096)
                    if not data:
                        break
                    for key in controls.feed(data):
                        if not player.handle(key):
                            running = False
                            break
                        dirty = True
            return 0
        finally:
            termios.tcsetattr(fd, termios.TCSANOW, old)
            sys.stdout.write("\x1b[0m\x1b[?2004l\x1b[?25h\x1b[?1049l")
            sys.stdout.flush()
    except KeyboardInterrupt:
        return 130
    except (ImportError, OSError, ValueError) as exc:
        message = "".join(char for char in str(exc)[:2000] if char.isprintable() or char == "\n")
        print("Raven replay: " + message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(cli())
