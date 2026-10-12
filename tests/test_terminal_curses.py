"""Exercise terminal ownership through the actual outer curses application."""
from __future__ import annotations

import codecs
import json
import os
from pathlib import Path
import select
import shlex
import signal
import struct
import subprocess
import sys
import time

import pytest


@pytest.mark.skipif(os.name != "posix", reason="A POSIX pseudo-terminal is required")
def test_outer_curses_terminal_keeps_shortcuts_and_fragmented_mouse_in_their_owner(tmp_path):
    pytest.importorskip("curses")
    pty = pytest.importorskip("pty")
    fcntl = pytest.importorskip("fcntl")
    termios = pytest.importorskip("termios")
    pyte = pytest.importorskip("pyte")
    root = Path(__file__).resolve().parents[1]
    config, trace, result = (tmp_path / name for name in ("config.json", "trace.json", "child.txt"))
    config.write_text(json.dumps({
        "animations": False, "startup_animation": False, "gpu_sampling": False,
        "smooth_scrolling": False, "terminal": {"shell": "/bin/sh"},
    }))
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 42, 160, 0, 0))
    environment = dict(os.environ, TERM="xterm-256color", LC_ALL="C.UTF-8",
                       PYTHONPATH=str(root), PS1="OUTER_READY> ", ENV="")
    process = subprocess.Popen(
        [sys.executable, "-m", "raven", "--fake", "--no-state", "--no-plugins",
         "--config", str(config), "--ui-trace", str(trace)],
        cwd=root, env=environment, stdin=slave, stdout=slave, stderr=slave,
        start_new_session=True,
    )
    os.close(slave)
    display = pyte.Screen(160, 42)
    stream = pyte.Stream(display)
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    output = bytearray()
    deadline = time.monotonic() + 15

    def rendered():
        return "\n".join(display.display)

    def pump(seconds=.01):
        if select.select([master], [], [], max(0, min(seconds, deadline - time.monotonic())))[0]:
            try:
                data = os.read(master, 65536)
            except OSError:
                return
            output.extend(data)
            del output[:-512 * 1024]
            stream.feed(decoder.decode(data))

    def until(predicate, label):
        while time.monotonic() < deadline:
            # Observe liveness before the predicate: an expected clean exit
            # between two polls must not be reported as an unexpected exit.
            returncode = process.poll()
            if predicate():
                return
            assert returncode is None, (label, returncode, rendered(), output[-3000:])
            pump()
        pytest.fail(label + " timed out\n" + rendered() + "\n" + repr(output[-3000:]))

    try:
        until(lambda: "File" in rendered() and "Jobs" in rendered(), "Initial Raven frame")
        os.write(master, b":terminal show\r")
        until(lambda: "OUTER_READY>" in rendered(), "Embedded shell prompt")

        # Every character passes through ncurses, the production decoder,
        # controller, broker, child PTY, emulator and outer renderer. These
        # bytes would quit, yank, group, ungroup or open the palette in Raven.
        command = ("printf '%s\\n' 'qyg:u' > " + shlex.quote(str(result)) +
                   "; printf '%s:%s\\n' 'OUTER_RENDERED' 'qyg:u'").encode()
        split = command.index(b"qyg:u") + 2
        os.write(master, command[:split])
        reports = [b"\x1b[<35;125;14M", b"\x1b[<35;22;13M", b"\x1b[<35;145;21M"]
        for report in reports:
            # Hold a complete mouse prefix across outer-loop turns, including
            # moves between Jobs and Terminal. Hover must not change focus.
            os.write(master, report[:4])
            fragment_deadline = time.monotonic() + .025
            while time.monotonic() < fragment_deadline:
                pump(.005)
            os.write(master, report[4:])
        os.write(master, command[split:] + b"\r")
        until(lambda: result.exists() and result.read_bytes() == b"qyg:u\n", "Exact child input")
        until(lambda: "OUTER_RENDERED:qyg:u" in rendered(), "Rendered child output")
        assert process.poll() is None
        os.write(master, b"\x1dtq")
        until(lambda: process.poll() is not None, "Explicit return to Raven and quit")
        assert process.returncode == 0, output.decode("utf-8", "replace")
        captured = json.loads(trace.read_text())
        assert captured["inputs"]["mouse"] >= len(reports)
        assert all(item["context"]["tab"] == item["after"]["tab"] == "jobs"
                   for item in captured["transitions"]), captured["transitions"]
    finally:
        # Give the normal application shutdown the first chance to collect its
        # broker and child. Signals target only the process group created here.
        if process.poll() is None:
            try:
                os.write(master, b"\x1dtq")
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=2)
        os.close(master)
