"""Bounded register delivery to a running, local Vim or Neovim instance.

Discovery never starts an editor. Payloads are data in private temporary files;
only a fixed ``setreg`` expression is evaluated, and no buffer is changed.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
import shutil
import stat
import subprocess
import tempfile

DISCOVERY_TIMEOUT = 0.4
DELIVERY_TIMEOUT = 0.8
MAX_BYTES = 8 << 20


@dataclass(frozen=True)
class Target:
    kind: str
    executable: str
    server: str


def _executable(name):
    path = shutil.which(name)
    if not path:
        return None
    path = os.path.realpath(path)
    try:
        return path if stat.S_ISREG(os.stat(path).st_mode) and os.access(path, os.X_OK) else None
    except OSError:
        return None


def _socket(path):
    if not isinstance(path, str) or not path or not os.path.isabs(path) or any(ord(c) < 32 for c in path):
        return False
    try:
        result = os.lstat(path)
        return stat.S_ISSOCK(result.st_mode) and result.st_uid == os.getuid()
    except (OSError, AttributeError):
        return False


def discover(environ=None):
    """Find an explicitly inherited Neovim socket or one unambiguous Vim server."""
    environ = os.environ if environ is None else environ
    nvim = _executable("nvim")
    if nvim:
        for variable in ("NVIM", "NVIM_LISTEN_ADDRESS"):
            address = environ.get(variable)
            if _socket(address):
                return Target("Neovim", nvim, address), ""
    vim = _executable("vim")
    if vim:
        try:
            result = subprocess.run([vim, "--serverlist"], capture_output=True, timeout=DISCOVERY_TIMEOUT,
                                    check=False, env=dict(environ))
            servers = result.stdout.decode("utf-8", errors="strict").splitlines() if result.returncode == 0 else []
            servers = [s.strip() for s in servers if s.strip() and len(s.strip()) <= 256
                       and not any(ord(c) < 32 for c in s.strip())]
            requested = environ.get("RAVEN_VIM_SERVER")
            if requested and requested in servers:
                return Target("Vim", vim, requested), ""
            if not requested and len(servers) == 1:
                return Target("Vim", vim, servers[0]), ""
            if len(servers) > 1 or requested:
                return None, "Choose a running Vim server with RAVEN_VIM_SERVER; clipboard copying remains available."
        except (OSError, UnicodeError, subprocess.TimeoutExpired):
            pass
    return None, "No running local Vim/Neovim server was found; clipboard copying remains available."


def _literal(value):
    return "'" + value.replace("'", "''") + "'"


def _expression(path):
    # This fixed expression writes only the unnamed and last-yank registers.
    # readfile(..., 'b') plus join preserves trailing newlines and empty text.
    value = 'join(readfile(' + _literal(path) + ', "b"), "\\n")'
    return 'setreg("0", ' + value + ', "v") + setreg(\'"\', getreg("0"), "v")'


def send_file(path, *, target=None, environ=None, cancel=None):
    """Deliver complete UTF-8 data; never follow a source symlink or send a prefix."""
    if cancel is not None and cancel():
        return False, "Editor yank cancelled."
    if target is None:
        target, reason = discover(environ)
        if target is None:
            return False, reason
    if (not isinstance(target, Target) or target.kind not in ("Neovim", "Vim")
            or not isinstance(target.executable, str) or not os.path.isabs(target.executable)
            or not isinstance(target.server, str) or not target.server
            or any(ord(char) < 32 for char in target.server)):
        return False, "Editor target is invalid; clipboard copying remains available."
    if target.kind == "Neovim" and not _socket(target.server):
        return False, "Neovim socket changed or closed; clipboard copying remains available."
    temporary = None
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_BYTES:
            return False, "Editor yank needs a regular UTF-8 export of at most 8 MiB; the complete export is preserved."
        with os.fdopen(fd, "rb") as source:
            fd = None
            data = source.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            return False, "Editor yank exceeds 8 MiB; the complete export is preserved."
        try:
            data.decode("utf-8", errors="strict")
        except UnicodeError:
            return False, "Editor yank cannot preserve invalid UTF-8; the complete export is preserved."
        if b"\x00" in data:
            return False, "Editor yank cannot preserve NUL bytes; the complete export is preserved."
        if cancel is not None and cancel():
            return False, "Editor yank cancelled."
        with tempfile.NamedTemporaryFile(prefix="raven-yank-", suffix=".txt", delete=False) as output:
            temporary = output.name
            os.chmod(temporary, 0o600)
            output.write(data)
        expression = _expression(temporary)
        arguments = [target.executable, "--server", target.server, "--remote-expr", expression] if target.kind == "Neovim" else [target.executable, "--servername", target.server, "--remote-expr", expression]
        result = subprocess.run(arguments, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=DELIVERY_TIMEOUT, check=False,
                                env=None if environ is None else dict(environ))
        # setreg returns zero on success. An editor's expression error can
        # otherwise be returned as successful process exit with error text.
        if result.returncode == 0 and result.stdout.strip() == b"0":
            return True, "Yanked complete selection into " + target.kind + " registers 0 and unnamed."
        return False, "Editor register delivery failed; clipboard copying remains available."
    except (OSError, ValueError, UnicodeError, subprocess.TimeoutExpired):
        return False, "Editor register delivery failed or timed out; clipboard copying remains available."
    finally:
        if fd is not None:
            os.close(fd)
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def send(text, *, target=None, environ=None):
    """Use the same exact-data path for a small rendered selection."""
    temporary = None
    try:
        data = text.encode("utf-8", errors="strict")
        if len(data) > MAX_BYTES:
            return False, "Editor yank exceeds 8 MiB; clipboard copying remains available."
        with tempfile.NamedTemporaryFile(prefix="raven-yank-source-", suffix=".txt", delete=False) as output:
            temporary = output.name
            output.write(data)
        return send_file(temporary, target=target, environ=environ)
    except (OSError, UnicodeError):
        return False, "Editor yank could not preserve the text; clipboard copying remains available."
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def mode(app):
    value = getattr(app, "cfg", {}).get("clipboard", {}).get("destination", "copy")
    return "yank" if value == "yank" else "copy"


def options(app):
    return {"destination": mode(app)}


def toggle(app):
    config = getattr(app, "cfg", {})
    destination = "copy" if mode(app) == "yank" else "yank"
    if callable(getattr(config, "set", None)):
        config.set("clipboard.destination", destination)
    else:
        config.setdefault("clipboard", {})["destination"] = destination
    if destination == "copy":
        app.say("Copy mode: selections go to the clipboard and a private export.")
    else:
        app.say("Yank mode: use a running local Vim/Neovim server; clipboard copy is the fallback.")
    return destination
