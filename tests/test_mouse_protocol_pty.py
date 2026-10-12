"""Real ncurses preserves fragmented mouse framing and the following key."""
import json
import os
from pathlib import Path
import select
import struct
import subprocess
import sys
import time

import pytest


CHILD = r'''
import curses, json, os, time
from pathlib import Path
from raven import screen
from raven.config import Config
from raven.controller import App
from raven.layout import Glyphs
from raven.model import Job, Store
from raven.views import Views

def publish_marker(path, text):
    # A visible marker must already contain its complete payload. The parent
    # can observe path creation between open("w") and the first write.
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(text)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)

result = {"events": []}
def main(window):
    curses.raw()
    curses.set_escdelay(25)
    curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)
    curses.mouseinterval(0)
    window.keypad(True)
    cfg = Config({"animations": False, "startup_animation": False, "gpu_sampling": False})
    store = Store(persist=False)
    store.jobs = [Job("7", "training", "cpu", "RUNNING", cpus=4)]
    app = App(store, None, None, cfg, "test", interactive=False)
    views = Views(Glyphs(False), cfg)
    app.views_ref = views
    cache = screen._FrameCache()
    cache.rebuild(app, views, store, None, 160, 40)
    reader = screen._InputReader(window)
    Path(os.environ["RAVEN_PTY_READY"]).touch()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not app.quit:
        window.timeout(5 if reader.escape else 100)
        event = reader.read(curses)
        if reader.escape:
            Path(os.environ["RAVEN_PTY_PREFIX"]).touch()
        if event is None or event[0] is None:
            continue
        result["events"].append(event[0])
        if event[0] == "mouse":
            Path(os.environ["RAVEN_PTY_WARMUP"]).touch()
        elif event[0] == "esc":
            Path(os.environ["RAVEN_PTY_ESCAPED"]).touch()
        screen._apply_input(app, event, cache.hits, curses)
        if app.tab != "jobs":
            raise AssertionError("Mouse bytes activated " + app.tab)
    result.update(tab=app.tab, selected_id=app.selected_id, quit=app.quit)
    if app.research:
        app.research.close()

curses.wrapper(main)
with open(os.environ["RAVEN_PTY_RESULT"], "w") as file:
    json.dump(result, file)
'''


@pytest.mark.skipif(os.name != "posix", reason="A POSIX pseudo-terminal is required")
@pytest.mark.parametrize("terminal", ["screen", "xterm-256color"])
@pytest.mark.parametrize("scenario", ["escape-120ms", "sgr-middle-120ms", "escape-300ms"])
def test_fragmented_report_through_real_curses_and_app_keeps_jobs_page(tmp_path, terminal, scenario):
    pytest.importorskip("curses")
    pty = pytest.importorskip("pty")
    fcntl = pytest.importorskip("fcntl")
    termios = pytest.importorskip("termios")
    script, ready, result, prefix, warmup_seen, escaped = (tmp_path / name for name in
        ("child.py", "ready", "result.json", "prefix", "warmup", "escaped"))
    script.write_text(CHILD)
    root = Path(__file__).resolve().parents[1]
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 160, 0, 0))
    environment = dict(os.environ, TERM=terminal, LC_ALL="C.UTF-8", PYTHONPATH=str(root),
                       RAVEN_PTY_READY=str(ready), RAVEN_PTY_RESULT=str(result),
                       RAVEN_PTY_PREFIX=str(prefix), RAVEN_PTY_WARMUP=str(warmup_seen),
                       RAVEN_PTY_ESCAPED=str(escaped))
    process = subprocess.Popen([sys.executable, str(script)], cwd=root, env=environment,
                               stdin=slave, stdout=slave, stderr=slave)
    os.close(slave)
    output = bytearray()
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            if select.select([master], [], [], .01)[0]:
                try:
                    output.extend(os.read(master, 65536))
                except OSError:
                    break
        if not ready.exists() and b"could not find terminal" in output:
            pytest.skip("The requested terminal description is unavailable")
        assert ready.exists(), output.decode("utf-8", "replace")
        warmup = b"\x1b[<35;82;25M"
        report = b"\x1b[MCr9" if scenario != "sgr-middle-120ms" else warmup
        split = 1 if scenario != "sgr-middle-120ms" else 9
        os.write(master, warmup + report[:split])
        # Ncurses may own a partial SGR prefix internally. Its warmup event
        # establishes that the child is reading; raw Escape also has a prefix
        # acknowledgement before the timed fragment gap begins.
        acknowledgement = warmup_seen if scenario == "sgr-middle-120ms" else prefix
        while not acknowledgement.exists() and process.poll() is None and time.monotonic() < deadline:
            if select.select([master], [], [], .005)[0]:
                try:
                    output.extend(os.read(master, 65536))
                except OSError:
                    break
        assert acknowledgement.exists(), output.decode("utf-8", "replace")
        time.sleep(.3 if scenario == "escape-300ms" else .12)
        if scenario == "escape-300ms":
            while not escaped.exists() and process.poll() is None and time.monotonic() < deadline:
                if select.select([master], [], [], .005)[0]:
                    try:
                        output.extend(os.read(master, 65536))
                    except OSError:
                        break
            assert escaped.exists(), "The late-fragment case did not exercise Escape recovery"
        os.write(master, report[split:] + b"q")
        while time.monotonic() < deadline:
            if select.select([master], [], [], .05)[0]:
                try:
                    output.extend(os.read(master, 65536))
                except OSError:
                    break
            elif process.poll() is not None:
                break
        process.wait(timeout=1)
        assert process.returncode == 0, output.decode("utf-8", "replace")
        captured = json.loads(result.read_text())
        expected = ["mouse", "esc", "mouse", "q"] if scenario == "escape-300ms" else ["mouse", "mouse", "q"]
        assert captured["events"] == expected
        assert captured["tab"] == "jobs" and captured["quit"]
        if scenario != "escape-300ms":
            assert captured["selected_id"] == "7"
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=2)
        os.close(master)


WIDE_CHILD = CHILD.replace('result = {"events": []}', 'result = {"events": [], "pointers": []}').replace(
    'cache.rebuild(app, views, store, None, 160, 40)',
    '''app.job_panel_state.update(mode="analytics", analytics_view="job")
    for index in range(30):
        store.record("7", {"k": "live", "t": 180 + index, "cpu": index / 40, "rss": 1024 ** 3})
    cache.rebuild(app, views, store, None, 300, 250)''').replace(
    'Path(os.environ["RAVEN_PTY_READY"]).touch()',
    '''plots = app.chart_interaction_state.get("plots", [])
    center = next(((p.visible.left + p.visible.right) // 2, (p.visible.top + p.visible.bottom) // 2)
                  for p in plots if p.visible.right - p.visible.left > 5 and p.visible.bottom - p.visible.top > 2)
    publish_marker(os.environ["RAVEN_PTY_READY"], json.dumps({"center": center, "raw": reader.raw}))''').replace(
    'Path(os.environ["RAVEN_PTY_WARMUP"]).touch()',
    '''result["pointers"].append(list(event[1]))
            Path(os.environ["RAVEN_PTY_WARMUP"]).touch()''').replace(
    'result.update(tab=app.tab, selected_id=app.selected_id, quit=app.quit)',
    'result.update(tab=app.tab, selected_id=app.selected_id, quit=app.quit, capture=bool(app.chart_interaction_state.get("capture")))')


@pytest.mark.skipif(os.name != "posix", reason="A POSIX pseudo-terminal is required")
@pytest.mark.parametrize("terminal", ["screen", "screen-256color", "tmux-256color", "xterm-256color"])
def test_wide_utf8_and_raw_mouse_reports_preserve_live_graph_gestures(tmp_path, terminal):
    """Coordinate 'l' stays inside a mouse report through the real App path."""
    pytest.importorskip("curses")
    pty = pytest.importorskip("pty")
    fcntl = pytest.importorskip("fcntl")
    termios = pytest.importorskip("termios")
    script, ready, result, prefix, warmup, escaped = (tmp_path / name for name in
        ("wide.py", "ready", "result.json", "prefix", "warmup", "escaped"))
    script.write_text(WIDE_CHILD)
    root = Path(__file__).resolve().parents[1]
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 250, 300, 0, 0))
    environment = dict(os.environ, TERM=terminal, LC_ALL="C.UTF-8", PYTHONPATH=str(root),
                       RAVEN_PTY_READY=str(ready), RAVEN_PTY_RESULT=str(result),
                       RAVEN_PTY_PREFIX=str(prefix), RAVEN_PTY_WARMUP=str(warmup),
                       RAVEN_PTY_ESCAPED=str(escaped))
    process = subprocess.Popen([sys.executable, str(script)], cwd=root, env=environment,
                               stdin=slave, stdout=slave, stderr=slave)
    os.close(slave)
    output = bytearray()
    deadline = time.monotonic() + 10

    def read_output(wait=.005):
        if select.select([master], [], [], wait)[0]:
            try:
                output.extend(os.read(master, 65536))
            except OSError:
                return False
        return True

    try:
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            if not read_output():
                break
        if not ready.exists() and b"could not find terminal" in output:
            pytest.skip("The requested terminal description is unavailable")
        assert ready.exists(), output.decode("utf-8", "replace")
        metadata = json.loads(ready.read_text())
        assert metadata["raw"]
        cx, cy = metadata["center"]
        press = f"\x1b[<0;{cx + 1};{cy + 1}M".encode()
        release = f"\x1b[<0;{cx + 1};{cy + 1}m".encode()
        os.write(master, press)
        while not warmup.exists() and process.poll() is None and time.monotonic() < deadline:
            read_output()
        assert warmup.exists(), output.decode("utf-8", "replace")
        expected = [(cx, cy)]
        # Every byte boundary is exercised. A UTF-8 X coordinate at 120 leaves
        # ASCII 'l' as the Y-coordinate byte at 75: the exact former Logs jump.
        for encoding in ("utf-8", "latin1"):
            for x, y in ((120,75), (81,120), (190,24), (200,200), (24,81)):
                report = ("\x1b[M" + "".join(chr(n) for n in (64, x + 33, y + 33))).encode(encoding)
                for split in range(1, len(report)):
                    os.write(master, report[:split])
                    time.sleep(.002)
                    os.write(master, report[split:])
                    expected.append((x, y))
                    read_output(0)
        # Retain an unfinished Unicode coordinate past the Escape grace too.
        # Expiring C2 here would turn its eventual ASCII 'l' suffix into Logs.
        for delay in (.12, .3):
            os.write(master, b"\x1b[MC\xc2")
            time.sleep(delay)
            os.write(master, b"\x99l")
            expected.append((120,75))
        os.write(master, release + b"q")
        expected.append((cx,cy))
        while time.monotonic() < deadline and read_output(.02):
            if process.poll() is not None:
                break
        process.wait(timeout=1)
        assert process.returncode == 0, output.decode("utf-8", "replace")
        captured = json.loads(result.read_text())
        assert captured["events"] == ["mouse"] * len(expected) + ["q"]
        assert [tuple(pointer[1:3]) for pointer in captured["pointers"]] == expected
        assert captured["tab"] == "jobs" and captured["selected_id"] == "7"
        assert captured["quit"] and not captured["capture"]
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=2)
        os.close(master)


KEY_CHILD = CHILD.replace('result = {"events": []}', 'result = {"events": [], "payloads": []}').replace(
    'Path(os.environ["RAVEN_PTY_READY"]).touch()',
    'publish_marker(os.environ["RAVEN_PTY_READY"], json.dumps(list(screen._terminal_keys(curses).items())))').replace(
    'result["events"].append(event[0])',
    '''result["events"].append(event[0])
        result["payloads"].append(event[1])
        publish_marker(os.environ["RAVEN_PTY_WARMUP"], str(len(result["events"])))
        if event[0] == "resize":
            Path(os.environ["RAVEN_PTY_PREFIX"]).touch()''').replace(
    'screen._apply_input(app, event, cache.hits, curses)',
    '''if event[0] in ("mouse", "q"):
            screen._apply_input(app, event, cache.hits, curses)''')


@pytest.mark.skipif(os.name != "posix", reason="A POSIX pseudo-terminal is required")
@pytest.mark.parametrize("terminal", ["screen", "screen-256color", "tmux-256color", "xterm-256color"])
def test_raw_input_preserves_actual_terminal_keys_unicode_paste_and_resize(tmp_path, terminal):
    import signal
    pytest.importorskip("curses")
    pty = pytest.importorskip("pty")
    fcntl = pytest.importorskip("fcntl")
    termios = pytest.importorskip("termios")
    script, ready, result, prefix, warmup, escaped = (tmp_path / name for name in
        ("keys.py", "ready", "result.json", "resize", "warmup", "escaped"))
    script.write_text(KEY_CHILD)
    root = Path(__file__).resolve().parents[1]
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 160, 0, 0))
    environment = dict(os.environ, TERM=terminal, LC_ALL="C.UTF-8", PYTHONPATH=str(root),
                       RAVEN_PTY_READY=str(ready), RAVEN_PTY_RESULT=str(result),
                       RAVEN_PTY_PREFIX=str(prefix), RAVEN_PTY_WARMUP=str(warmup),
                       RAVEN_PTY_ESCAPED=str(escaped))
    process = subprocess.Popen([sys.executable, str(script)], cwd=root, env=environment,
                               stdin=slave, stdout=slave, stderr=slave)
    os.close(slave)
    output = bytearray()
    deadline = time.monotonic() + 10

    def read_output(wait=.005):
        if select.select([master], [], [], wait)[0]:
            try:
                output.extend(os.read(master, 65536))
            except OSError:
                return False
        return True

    try:
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            if not read_output():
                break
        if not ready.exists() and b"could not find terminal" in output:
            pytest.skip("The requested terminal description is unavailable")
        assert ready.exists(), output.decode("utf-8", "replace")
        bindings = json.loads(ready.read_text())
        assert any(name == "f10" for sequence, name in bindings)
        expected = [name for sequence, name in bindings]
        os.write(master, "".join(sequence for sequence, name in bindings).encode())
        text = "界é🚀"
        for byte in text.encode():
            os.write(master, bytes((byte,)))
            time.sleep(.002)
            read_output(0)
        os.write(master, "\x1b[200~log\n界\x1b[201~".encode())
        expected.extend(list(text) + ["paste"])
        for delay, suffix, name in ((.12, b"A", "up"), (.3, b"P", "f1")):
            while time.monotonic() < deadline and process.poll() is None:
                if warmup.exists() and int(warmup.read_text()) >= len(expected):
                    break
                read_output()
            assert warmup.exists() and int(warmup.read_text()) >= len(expected)
            prefix.unlink(missing_ok=True)
            os.write(master, b"\x1bO")
            while not prefix.exists() and process.poll() is None and time.monotonic() < deadline:
                read_output()
            assert prefix.exists(), "The SS3 prefix was not consumed before the timed gap"
            time.sleep(delay)
            os.write(master, suffix)
            expected.append(name)
        # Wait for the final split key before using the acknowledgement path
        # for SIGWINCH, so an old prefix cannot stand in for resize evidence.
        while time.monotonic() < deadline and process.poll() is None:
            if int(warmup.read_text()) >= len(expected):
                break
            read_output()
        assert int(warmup.read_text()) >= len(expected)
        prefix.unlink(missing_ok=True)
        # Unknown parameterized SS3 remains one control. Its '5' parameter
        # must not become a page shortcut; recognized terminfo keys above win.
        os.write(master, b"\x1bO1;5A")
        # SIGWINCH stays available with keypad disabled, rather than requiring
        # a keyboard escape or interrupting the parser's pending byte state.
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 41, 161, 0, 0))
        os.kill(process.pid, signal.SIGWINCH)
        while not prefix.exists() and process.poll() is None and time.monotonic() < deadline:
            read_output()
        assert prefix.exists(), output.decode("utf-8", "replace")
        os.write(master, b"q")
        expected.extend(["resize", "q"])
        while time.monotonic() < deadline and read_output(.02):
            if process.poll() is not None:
                break
        process.wait(timeout=1)
        assert process.returncode == 0, output.decode("utf-8", "replace")
        captured = json.loads(result.read_text())
        assert captured["events"] == expected
        assert captured["payloads"][expected.index("paste")] == "log\n界"
        assert captured["tab"] == "jobs" and captured["quit"]
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=2)
        os.close(master)


SLOW_CHILD = CHILD.replace(
    'Path(os.environ["RAVEN_PTY_PREFIX"]).touch()',
    'publish_marker(os.environ["RAVEN_PTY_PREFIX"], str(len(result["events"])))').replace(
    'Path(os.environ["RAVEN_PTY_WARMUP"]).touch()',
    'publish_marker(os.environ["RAVEN_PTY_WARMUP"], str(sum(name == "mouse" for name in result["events"])))')


@pytest.mark.skipif(os.name != "posix", reason="A POSIX pseudo-terminal is required")
@pytest.mark.parametrize("terminal", ["screen", "screen-256color", "tmux-256color", "xterm-256color"])
def test_every_utf8_mouse_byte_boundary_remains_framed_after_300ms(tmp_path, terminal):
    pytest.importorskip("curses")
    pty = pytest.importorskip("pty")
    fcntl = pytest.importorskip("fcntl")
    termios = pytest.importorskip("termios")
    script, ready, result, prefix, warmup, escaped = (tmp_path / name for name in
        ("slow.py", "ready", "result.json", "prefix", "warmup", "escaped"))
    script.write_text(SLOW_CHILD)
    root = Path(__file__).resolve().parents[1]
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 160, 0, 0))
    environment = dict(os.environ, TERM=terminal, LC_ALL="C.UTF-8", PYTHONPATH=str(root),
                       RAVEN_PTY_READY=str(ready), RAVEN_PTY_RESULT=str(result),
                       RAVEN_PTY_PREFIX=str(prefix), RAVEN_PTY_WARMUP=str(warmup),
                       RAVEN_PTY_ESCAPED=str(escaped))
    process = subprocess.Popen([sys.executable, str(script)], cwd=root, env=environment,
                               stdin=slave, stdout=slave, stderr=slave)
    os.close(slave)
    output = bytearray()
    deadline = time.monotonic() + 10

    def await_file(path, minimum=None):
        while process.poll() is None and time.monotonic() < deadline:
            if path.exists():
                if minimum is None or int(path.read_text()) >= minimum:
                    return
            if select.select([master], [], [], .005)[0]:
                try:
                    output.extend(os.read(master, 65536))
                except OSError:
                    break
        if not ready.exists() and b"could not find terminal" in output:
            pytest.skip("The requested terminal description is unavailable")
        raise AssertionError(output.decode("utf-8", "replace"))

    try:
        await_file(ready)
        os.write(master, b"\x1b[<35;82;25M")
        await_file(warmup, 1)
        report = b"\x1b[MC\xc2\x99l"
        for split in range(1, len(report)):
            prefix.unlink(missing_ok=True)
            os.write(master, report[:split])
            await_file(prefix)
            time.sleep(.3)
            os.write(master, report[split:])
            await_file(warmup, split + 1)
        os.write(master, b"q")
        while time.monotonic() < deadline:
            if select.select([master], [], [], .02)[0]:
                try:
                    output.extend(os.read(master, 65536))
                except OSError:
                    break
            elif process.poll() is not None:
                break
        process.wait(timeout=1)
        assert process.returncode == 0, output.decode("utf-8", "replace")
        captured = json.loads(result.read_text())
        # An Escape-only prefix beyond its bounded grace is indistinguishable
        # from a genuine Escape. Its delayed report body must remain framed.
        assert captured["events"] == ["mouse", "esc"] + ["mouse"] * 6 + ["q"]
        assert captured["tab"] == "jobs" and captured["quit"]
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=2)
        os.close(master)


@pytest.mark.parametrize("previous", [None, '{"count": 1}'])
@pytest.mark.parametrize("fail_write", [False, True])
def test_child_marker_publication_never_exposes_partial_contents(tmp_path, monkeypatch, previous, fail_write):
    """Pause inside the actual child helper's write, not a timing-based retry."""
    import ast
    definition = next(node for node in ast.parse(CHILD).body
                      if isinstance(node, ast.FunctionDef) and node.name == "publish_marker")
    namespace = {"os": os, "Path": Path}
    exec(compile(ast.Module(body=[definition], type_ignores=[]), "<child marker>", "exec"), namespace)
    marker = tmp_path / "ready"
    if previous is not None:
        marker.write_text(previous)
    original = Path.write_text
    observed = []
    def interrupted_write(path, text, *args, **kwargs):
        original(path, text[:1], *args, **kwargs)
        observed.append(marker.read_text() if marker.exists() else None)
        assert observed[-1] == previous
        if fail_write:
            raise OSError("Interrupted marker write")
        return original(path, text, *args, **kwargs)
    monkeypatch.setattr(Path, "write_text", interrupted_write)
    if fail_write:
        with pytest.raises(OSError, match="Interrupted marker"):
            namespace["publish_marker"](marker, '{"count": 2}')
        assert (marker.read_text() if marker.exists() else None) == previous
    else:
        namespace["publish_marker"](marker, '{"count": 2}')
        assert json.loads(marker.read_text()) == {"count": 2}
    assert observed == [previous]
    assert not marker.with_name("ready.tmp").exists()
