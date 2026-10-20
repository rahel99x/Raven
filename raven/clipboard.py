"""Copying text out of a terminal session: the terminal's clipboard through OSC 52 (works over ssh in terminals that
allow it: iTerm2, Windows Terminal, kitty, alacritty, foot, wezterm, xterm with allowWindowOps, tmux with
set-clipboard on), a local clipboard tool when one is reachable, and always a file, so nothing is ever lost."""

from __future__ import annotations

import base64
import codecs
import os
import shutil
import stat
import subprocess
import sys
import time
import uuid
from typing import List, Optional

TOOLS = [
    ["pbcopy"],
    ["wl-copy"],
    ["xclip", "-selection", "clipboard"],
    ["xsel", "--clipboard", "--input"],
    ["clip.exe"],
]
OSC52_LIMIT = 100_000  # bytes of base64 most terminals accept in one sequence


def options(app):
    """Capture destination and transport preferences before queuing copy work."""
    preferences = getattr(app, "cfg", {}).get("clipboard", {})
    return {
        "use_osc52": bool(preferences.get("osc52", True)),
        "use_tools": bool(preferences.get("tools", True)),
        "destination": "yank" if preferences.get("destination") == "yank" else "copy",
    }


def osc52(text: str, tty_path: str = "/dev/tty") -> bool:
    """Request the complete text through OSC 52; never send a truncated prefix.

    A successful write confirms only that a request was sent. Terminals can
    disable clipboard access or impose a smaller limit without acknowledging it.
    """
    try:
        encoded = text.encode("utf-8")
    except UnicodeError:
        return False
    if len(encoded) > OSC52_LIMIT // 4 * 3:
        return False
    data = base64.b64encode(encoded).decode("ascii")
    seq = f"\033]52;c;{data}\a"
    if os.environ.get("TMUX"):
        seq = "\033Ptmux;" + seq.replace("\033", "\033\033") + "\033\\"
    elif os.environ.get("STY"):
        seq = "\033P" + seq + "\033\\"
    try:
        with open(tty_path, "wb") as f:
            f.write(seq.encode("ascii"))
            f.flush()
        return True
    except OSError:
        try:
            if sys.__stdout__ is None or not sys.__stdout__.isatty():
                return False
            sys.__stdout__.write(seq)
            sys.__stdout__.flush()
            return True
        except Exception:
            return False


def local_tool(text: str) -> Optional[str]:
    """Pipe to the first clipboard tool that exists and can reach a display; the tool's name on success."""
    has_display = bool(
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    ) or sys.platform in ("darwin", "win32", "cygwin")
    deadline = time.monotonic() + 2.0
    for cmd in TOOLS:
        if shutil.which(cmd[0]) is None:
            continue
        if cmd[0] in ("xclip", "xsel", "wl-copy") and not has_display:
            continue
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            r = subprocess.run(
                cmd, input=text.encode("utf-8"), capture_output=True, timeout=remaining
            )
            if r.returncode == 0:
                return cmd[0]
        except (OSError, UnicodeError, subprocess.TimeoutExpired):
            continue
    return None


def copy_for_app(app, text, state_dir=None, **options):
    """Keep accepted immutable selections off the terminal event thread."""
    if not getattr(app, "interactive", False):
        return copy(text, state_dir, **options)
    scheduler = getattr(getattr(app, "sampler", None), "worker_scheduler", None)
    if scheduler is None:
        scheduler = getattr(getattr(app, "research", None), "worker_scheduler", None)
    if scheduler is None:
        # Application service ownership is injected; clipboard transports must
        # not import pane renderers to obtain a background executor.
        provider = getattr(app, "background_worker_scheduler", None)
        scheduler = provider() if callable(provider) else None
    if scheduler is None:
        return "Copy could not be queued: the application has no background service"
    poll(app)
    pending = getattr(app, "clipboard_pending", None)
    if pending is None:
        pending = app.clipboard_pending = []
    if len(pending) >= 8:
        return "Clipboard queue is full; wait for the accepted copies to finish"
    lane = getattr(app, "clipboard_lane", None)
    if lane is None or lane.closed:
        try:
            lane = scheduler.lane("source")
        except ValueError:
            lane = scheduler.lane("research")
        app.clipboard_lane = lane
    try:
        pending.append(lane.submit(copy, str(text), state_dir, **options))
    except RuntimeError as exc:
        return "Copy could not be queued: " + str(exc)
    return "Copy queued; the immutable selection will be exported in the background"


def poll(app):
    pending = getattr(app, "clipboard_pending", None)
    if pending is None:
        return
    remaining = []
    for future in pending:
        if not future.done():
            remaining.append(future)
            continue
        try:
            message = future.result()
        except Exception as exc:
            message = "Copy failed: " + str(exc)
        app.say(message)
    app.clipboard_pending = remaining


def to_file(
    text: str, state_dir: Optional[str], name: str = "clipboard.txt"
) -> Optional[str]:
    directory_fd = None
    temporary = None
    try:
        if (
            not isinstance(name, str)
            or not name
            or os.path.basename(name) != name
            or name in (".", "..")
        ):
            return None
        encoded = text.encode("utf-8")
        from .export import export_dir

        base = export_dir(state_dir)
        directory_fd = os.open(
            base,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            existing = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if not stat.S_ISREG(existing.st_mode) or existing.st_uid != os.getuid():
                return None
        except FileNotFoundError:
            pass
        temporary = ".clipboard-" + uuid.uuid4().hex + ".tmp"
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
        with os.fdopen(fd, "wb") as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
        temporary = None
        return os.path.join(base, name)
    except (OSError, UnicodeError, ValueError):
        return None
    finally:
        if temporary and directory_fd is not None:
            try:
                os.unlink(temporary, dir_fd=directory_fd)
            except OSError:
                pass
        if directory_fd is not None:
            os.close(directory_fd)


def _regular_file(path: str):
    """Open only the published regular export, without following its symlink."""
    fd = os.open(
        path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("clipboard source must be a regular file")
        return os.fdopen(fd, "rb")
    except Exception:
        os.close(fd)
        raise


def _tool_from_file(path: str) -> Optional[str]:
    """Stream an already exported file to a local clipboard process."""
    has_display = bool(
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    ) or sys.platform in ("darwin", "win32", "cygwin")
    deadline = time.monotonic() + 2.0
    for cmd in TOOLS:
        if shutil.which(cmd[0]) is None or (
            cmd[0] in ("xclip", "xsel", "wl-copy") and not has_display
        ):
            continue
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            with _regular_file(path) as source:
                result = subprocess.run(
                    cmd,
                    stdin=source,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=remaining,
                )
            if result.returncode == 0:
                return cmd[0]
        except (OSError, subprocess.TimeoutExpired):
            continue
    return None


def copy_file(
    path: str,
    *,
    tty_path: str = "/dev/tty",
    use_osc52: bool = True,
    use_tools: bool = True,
    cancel=None,
    destination="copy",
    editor_context=None,
) -> dict:
    """Deliver a complete private export without loading a large file in memory.

    Text transports receive only strict UTF-8. The caller keeps the exact-byte
    export when encoding or transport limits prevent copying to a clipboard.
    """
    result = {"methods": [], "warnings": [], "bytes": 0, "text": False}
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    small = bytearray()
    fits = True
    try:
        with _regular_file(path) as source:
            file_size = os.fstat(source.fileno()).st_size
            while True:
                if cancel is not None and cancel():
                    raise InterruptedError("clipboard delivery cancelled")
                data = source.read(65536)
                if not data:
                    break
                result["bytes"] += len(data)
                decoder.decode(data, final=False)
                if fits and len(small) + len(data) <= OSC52_LIMIT // 4 * 3:
                    small.extend(data)
                else:
                    fits = False
                    small.clear()
            decoder.decode(b"", final=True)
    except UnicodeError:
        result["bytes"] = file_size
        result["warnings"].append(
            "Clipboard skipped: the log is not valid UTF-8; the full raw export is preserved."
        )
        return result
    except OSError as exc:
        result["warnings"].append("Clipboard source could not be read: " + str(exc))
        return result
    result["text"] = True
    if cancel is not None and cancel():
        result["warnings"].append("Clipboard delivery cancelled.")
        return result
    if destination == "yank":
        from . import editor_yank

        success, message = editor_yank.send_file(
            path, target=editor_context, cancel=cancel
        )
        if success:
            result["methods"].append(message)
            return result
        result["warnings"].append(message)
    if use_osc52:
        if not fits:
            result["warnings"].append(
                f"OSC 52 skipped: complete log exceeds the {OSC52_LIMIT}-byte base64 limit."
            )
        elif osc52(small.decode("utf-8"), tty_path):
            result["methods"].append("OSC 52 request sent")
        else:
            result["warnings"].append("OSC 52 request could not be sent to a terminal.")
    if use_tools:
        if cancel is not None and cancel():
            result["warnings"].append("Local clipboard delivery cancelled.")
            return result
        tool = _tool_from_file(path)
        if tool:
            result["methods"].append(tool)
        else:
            result["warnings"].append("No local clipboard tool accepted the full log.")
    return result


def copy(
    text: str,
    state_dir: Optional[str] = None,
    tty_path: str = "/dev/tty",
    use_osc52: bool = True,
    use_tools: bool = True,
    destination="copy",
    editor_context=None,
) -> str:
    """Send ``text`` everywhere it can go; returns a one-line account of where it went."""
    where: List[str] = []
    notices: List[str] = []
    try:
        encoded_size = len(text.encode("utf-8"))
    except UnicodeError:
        return "copy failed: text is not valid UTF-8"
    path = to_file(text, state_dir)
    if destination == "yank":
        from . import editor_yank

        success, message = (
            editor_yank.send_file(path, target=editor_context)
            if path
            else editor_yank.send(text, target=editor_context)
        )
        if success:
            return message + (" Private export: " + path if path else "")
        notices.append(message)
    if use_osc52:
        if osc52(text, tty_path):
            notices.append(
                "OSC 52 request sent (terminal acceptance cannot be confirmed)"
            )
        elif encoded_size > OSC52_LIMIT // 4 * 3:
            notices.append("OSC 52 skipped: complete selection exceeds its size limit")
    if use_tools:
        tool = local_tool(text)
        if tool:
            where.append(tool)
    if path:
        where.append(path)
    n = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
    message = f"copied {n} line{'s' if n != 1 else ''} to " + (
        ", ".join(where) if where else "nowhere (no tool or writable directory)"
    )
    return message + ("; " + "; ".join(notices) if notices else "")
