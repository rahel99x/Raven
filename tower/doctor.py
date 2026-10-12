"""Read-only prerequisite checks; never sample, submit, or change a cluster."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import stat
import sys

from .remote import SshBackend
from .slurm import CommandError

REQUIRED = ("squeue", "scontrol", "sinfo", "sacct", "sstat")
OPTIONAL = {
    "sshare": "fair-share information",
    "sreport": "allocation usage",
    "sacctmgr": "allocation limits",
    "sbatch": "queue forecasts and resubmission",
    "scancel": "job cancellation",
}


def terminal_evidence(cfg, *, state_dir="", environ=None, encoding=None):
    """Inspect local terminal hints and paths without writing or connecting."""
    env = os.environ if environ is None else environ
    encoding = encoding or getattr(sys.stdout, "encoding", None) or "ascii"
    checks = []
    def add(name, ok, detail):
        checks.append({"name": name, "status": "ok" if ok else "warning", "required": False, "detail": detail})
    try:
        "█▁⣿┌".encode(encoding)
        unicode_ok = True
    except (UnicodeError, LookupError):
        unicode_ok = False
    add("Output encoding", unicode_ok, f"{encoding}; use --ascii if block characters are unavailable.")
    term = env.get("TERM", "dumb")
    add("Terminal type", term not in ("", "dumb"), f"TERM={term}; interactive tests verify actual keys and glyphs.")
    add("Color hints", term not in ("", "dumb") and not env.get("NO_COLOR"),
        "Colors are disabled by NO_COLOR or a dumb terminal." if env.get("NO_COLOR") or term in ("", "dumb")
        else "Color support is an environment hint. Use :terminaltest to inspect the displayed palette.")
    add("Mouse and keys", False, "Use :terminaltest. Press keys and click the test grid; events never activate job actions.")
    clipboard = cfg.get("clipboard", {})
    from .clipboard import TOOLS
    tools = [cmd[0] for cmd in TOOLS if shutil.which(cmd[0])]
    display = bool(env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")) or sys.platform in ("darwin", "win32", "cygwin")
    add("Clipboard transport", bool(clipboard.get("osc52", True)) or bool(clipboard.get("tools", True) and tools and display),
        f"OSC52 {'enabled' if clipboard.get('osc52', True) else 'disabled'}; local tools {'enabled' if clipboard.get('tools', True) else 'disabled'}; installed tools: {', '.join(tools) or 'none'}; "
        "SSH clipboard delivery depends on the local terminal. Exact file exports remain available.")
    if env.get("TMUX"):
        add("tmux clipboard", False, "Check set-clipboard in tmux and OSC52 support in the local terminal. :terminaltest clipboard requests a test copy.")
    if env.get("SSH_CONNECTION") or env.get("SSH_TTY"):
        add("SSH terminal", True, "This is an SSH session. Test the local terminal and any tmux layer; no extra SSH connection is opened.")
    for label, path, writable in (("Configuration", getattr(cfg, "path", ""), False), ("State and exports", state_dir, True)):
        if not path:
            add(label, True, "No custom path is set." if not writable else "Persistence is disabled or its path is not supplied.")
            continue
        absolute = os.path.abspath(os.path.expanduser(path))
        try:
            info = os.stat(absolute, follow_symlinks=False)
            regular = stat.S_ISDIR(info.st_mode) if writable else stat.S_ISREG(info.st_mode)
            access = os.access(absolute, os.W_OK if writable else os.R_OK)
            add(label, regular and access, absolute + (" is accessible." if regular and access else " is unavailable or has unsuitable permissions."))
        except OSError:
            parent = os.path.dirname(absolute)
            add(label, os.path.isdir(parent) and os.access(parent, os.W_OK if writable else os.R_OK),
                absolute + " does not exist. Check its parent path and permissions.")
    return checks


def diagnose(cfg, *, fake=False, host="", ssh_user=""):
    """Return capability evidence, including missing optional tools separately."""
    host = "" if fake else (host or cfg["host"])
    mode = "demo" if fake else "remote" if host else "local"
    checks = []

    def add(name, ok, detail, required=True):
        checks.append(dict(name=name, status="ok" if ok else "error" if required else "warning",
                           required=required, detail=detail))

    add("Python", sys.version_info >= (3, 10), sys.version.split()[0])
    add("Interactive terminal", importlib.util.find_spec("curses") is not None,
        "curses supports the interactive screen; text, JSON and ASCII reports also work without it", False)
    if fake:
        add("Simulated cluster", True, "No Slurm installation or cluster credentials required")
    else:
        found = set()
        connected = True
        if host:
            connected = bool(shutil.which("ssh"))
            add("SSH client", connected, "OpenSSH must be available locally")
            if connected and cfg.get("raven", {}).get("managed"):
                add("Remote tools", True, "Not probed during managed startup; admitted operations verify actual remote capabilities", False)
                checks.extend(terminal_evidence(cfg))
                return dict(mode=mode, ready=True, checks=checks, next_step="ravenl --host " + host,
                            scope="Local prerequisites only; remote scheduler tools and permissions remain unverified.")
            if connected:
                # Tool names are constants; no host, username, or config enters the shell program.
                probe = ('for tool in ' + " ".join((*REQUIRED, *OPTIONAL)) + '; do '
                         'if command -v "$tool" >/dev/null 2>&1; then printf "%s\\n" "$tool"; fi; done')
                try:
                    backend = SshBackend(host, user=ssh_user or cfg["ssh_user"], opts=cfg["ssh_opts"], control=False)
                    output, _ = backend.run(["sh", "-c", probe], timeout=8)
                    found = set(output.splitlines())
                    add("SSH connection", True, "Read-only command probe completed with host-key verification")
                except (CommandError, ValueError) as exc:
                    connected = False
                    add("SSH connection", False, str(exc))
        else:
            found = {tool for tool in (*REQUIRED, *OPTIONAL) if shutil.which(tool)}
        if connected:
            for tool in REQUIRED:
                add(tool, tool in found, "available" if tool in found else "Load your site's Slurm module or use --host LOGIN")
            for tool, purpose in OPTIONAL.items():
                add(tool, tool in found, purpose, False)

    checks.extend(terminal_evidence(cfg))
    ready = all(c["status"] != "error" for c in checks)
    return dict(mode=mode, ready=ready, checks=checks,
                next_step=("tower --fake" if fake else "tower --host LOGIN" if host else "tower") if ready
                else "Use tower --fake to explore; load Slurm locally or configure SSH access to a login node.",
                scope="Prerequisites only; cluster permissions, accounting data and live job behavior require a live smoke check.")


def render(result, *, as_json=False):
    if as_json:
        return json.dumps(result, indent=2)
    lines = [f"Raven / {result['mode']} environment", ""]
    for check in result["checks"]:
        label = {"ok": "OK", "warning": "OPTIONAL", "error": "MISSING"}[check["status"]]
        lines.append(f"  {label:8} {check['name']}: {check['detail']}")
    lines.extend(["", "Prerequisites ready." if result["ready"] else "Some required capabilities are missing.",
                  result["scope"], f"Next: {result['next_step']}"])
    return "\n".join(lines)
