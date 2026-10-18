"""Actual tmux transport check; a protocol peer, not a visual emulator study."""
from __future__ import annotations

import argparse
import codecs
import hashlib
import json
import os
from pathlib import Path
import pty
import select
import shlex
import signal
import struct
import subprocess
import sys
import tempfile
import time
import fcntl
import termios

import pyte


def run(root, tmux, mode):
    with tempfile.TemporaryDirectory(prefix="rv-tmux-") as directory:
        work = Path(directory)
        socket = work / "socket"
        config, receipt, child = (work / name for name in ("config.json", "receipt.json", "child.txt"))
        config.write_text(json.dumps({"animations": False, "startup_animation": False,
                                     "gpu_sampling": False, "smooth_scrolling": False,
                                     "synchronized_output": mode, "terminal": {"shell": "/bin/sh"}}))
        wrapper = work / "capture.py"
        # Capture only the existing diagnostic snapshot at normal teardown.
        wrapper.write_text(
            "import json,runpy\nfrom raven.terminal_sync import TerminalSync\n"
            "original=TerminalSync.close\n"
            "def close(self):\n"
            " try:\n"
            f"  open({str(receipt)!r},'w').write(json.dumps(self.snapshot()))\n"
            " finally: original(self)\n"
            "TerminalSync.close=close\nrunpy.run_module('raven',run_name='__main__')\n")
        tmux_config = work / "tmux.conf"
        tmux_config.write_text("set -g status off\nset -g default-terminal tmux-256color\n")
        command = [sys.executable, str(wrapper), "--fake", "--no-state", "--no-plugins", "--config", str(config)]
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 42, 160, 0, 0))
        env = dict(os.environ, TERM="xterm-256color", LC_ALL="C.UTF-8", PYTHONPATH=str(root),
                   PS1="MUX_READY> ", ENV="")
        env.pop("TMUX", None)
        process = subprocess.Popen([str(tmux), "-S", str(socket), "-f", str(tmux_config),
                                    "new-session", "-s", "qualification", shlex.join(command)],
                                   cwd=root, env=env, stdin=slave, stdout=slave, stderr=slave,
                                   start_new_session=True)
        os.close(slave)
        display = pyte.Screen(160, 42)
        stream = pyte.Stream(display)
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        output = bytearray()
        deadline = time.monotonic() + 25
        replied = 0

        def rendered():
            return "\n".join(display.display)

        def pump():
            nonlocal replied
            if not select.select([master], [], [], .02)[0]:
                return
            try:
                data = os.read(master, 65536)
            except OSError:
                return
            output.extend(data)
            if len(output) > 2 * 1024 * 1024:
                raise AssertionError("output budget exceeded")
            stream.feed(decoder.decode(data))
            # Outer peer explicitly reports unsupported, so tmux itself must
            # mediate any inner capability. This is not graphical qualification.
            count = output.count(b"\x1b[?2026$p")
            if count > replied:
                os.write(master, b"\x1b[?2026;0$y" * (count - replied))
                replied = count

        def until(predicate, label):
            while time.monotonic() < deadline:
                status = process.poll()
                if predicate():
                    return
                if status is not None:
                    raise AssertionError((label, status, rendered(), output[-2000:]))
                pump()
            raise AssertionError((label, rendered(), output[-2000:]))

        try:
            until(lambda: "File" in rendered() and "Jobs" in rendered(), "initial frame")
            os.write(master, b":terminal show\r")
            until(lambda: "Terminal focus" in rendered() and "running" in rendered(), "embedded shell")
            command_text = "printf '%s\\n' 'qyg:u' > " + shlex.quote(str(child))
            os.write(master, command_text.encode() + b"\r")
            until(lambda: child.exists() and child.read_bytes() == b"qyg:u\n", "exact child input")
            fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
            os.killpg(process.pid, signal.SIGWINCH)
            until(lambda: "Terminal focus" in rendered(), "frame survives resize")
            if mode == "auto":
                settled = time.monotonic() + 1.1
                until(lambda: time.monotonic() >= settled, "capability deadline")
            os.write(master, b"\x1dtq")
            until(lambda: process.poll() is not None, "normal quit")
            assert process.returncode == 0, process.returncode
            observed = json.loads(receipt.read_text())
            assert observed["mode"] == mode
            if mode == "off":
                assert observed["frames"] == 0
            if mode == "auto":
                assert not observed['pending']
            if mode == "on":
                assert observed["frames"] > 0
            return {"mode": mode, "result": "passed", "snapshot": observed,
                    "outer_query_replies": replied, "outer_bytes": len(output),
                    "outer_sha256": hashlib.sha256(output).hexdigest(),
                    "assertions": ["initial Jobs frame", "embedded shell exact input",
                                   "resize", "normal exit", "configured mode"]}
        finally:
            subprocess.run([str(tmux), "-S", str(socket), "kill-server"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=2)
            os.close(master)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--tmux", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = {"schema": "raven.tmux-qualification/v1", "python": sys.version,
              "tmux": subprocess.check_output([str(args.tmux), "-V"], text=True).strip(),
              "scope": "Actual local tmux, PTY and embedded shell; simulated scheduler; no visual tearing or remote-site claim.",
              "source_hashes": {p: hashlib.sha256((args.root / p).read_bytes()).hexdigest()
                                for p in ("raven/terminal_sync.py", "raven/screen.py", "raven/workbench.py")},
              "cases": []}
    try:
        for mode in ("auto", "off", "on"):
            report["cases"].append(run(args.root, args.tmux, mode))
    finally:
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"passed": len(report["cases"]), "tmux": report["tmux"]}))


if __name__ == "__main__":
    main()
