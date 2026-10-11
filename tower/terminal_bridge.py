"""Private, optional shell observations. Messages never authorize operations."""
from __future__ import annotations

import argparse
import hmac
import json
import os
from pathlib import Path
import secrets
import shutil
import shlex
import socket
import stat
import struct
import sys
import tempfile
import time

SCHEMA = "tower.terminal-bridge/v1"
MAX_MESSAGE = 8192
ENV_KEYS = frozenset(("PATH", "SHELL", "LANG", "LC_ALL", "TERM", "COLORTERM", "LOADEDMODULES", "MODULEPATH", "CONDA_DEFAULT_ENV", "VIRTUAL_ENV", "OMP_NUM_THREADS", "CUDA_VISIBLE_DEVICES", "SLURM_JOB_ID"))
EVENTS = frozenset(("start", "end", "prompt", "submission", "project", "artifact", "acceptance", "environment"))


def _bounded(value):
    if not isinstance(value, dict) or value.get("schema") != SCHEMA or value.get("event") not in EVENTS:
        raise ValueError("Unsupported terminal bridge message.")
    for key in ("session", "token"):
        if not isinstance(value.get(key), str) or not 1 <= len(value[key]) <= 128:
            raise ValueError("Invalid terminal bridge identity.")
    payload = {}
    limits = {"cwd": 4096, "command": 2048, "job": 128, "project": 4096, "artifact": 4096,
              "manifest": 4096, "host": 256, "clock": 128, "command_id": 128, "label": 128}
    for key, limit in limits.items():
        if key in value:
            if not isinstance(value[key], str) or len(value[key]) > limit or "\0" in value[key]:
                raise ValueError("Invalid terminal bridge field: " + key)
            payload[key] = value[key]
    if "status" in value:
        if isinstance(value["status"], bool) or not isinstance(value["status"], int) or not -255 <= value["status"] <= 255:
            raise ValueError("Invalid command exit status.")
        payload["status"] = value["status"]
    if value["event"] == "environment":
        for key in ("environment", "packages", "modules"):
            source = value.get(key, {})
            if not isinstance(source, dict) or len(source) > 64:
                raise ValueError("Invalid environment summary.")
            safe = {}
            for name, item in source.items():
                if not isinstance(name, str) or not isinstance(item, str) or len(name) > 128 or len(item) > 4096 or "\0" in name + item:
                    raise ValueError("Invalid environment summary field.")
                if key == "environment" and name not in ENV_KEYS:
                    continue
                safe[name] = item
            payload[key] = safe
    # Arrival time is trusted local time. Remote clock claims are observations.
    payload.update(event=value["event"], session=value["session"], schema=SCHEMA,
                   received=time.time(), monotonic=time.monotonic(), provenance="shell observation")
    return payload


class Bridge:
    """Bounded nonblocking AF_UNIX datagrams; uid plus per-session capability."""
    def __init__(self, directory=None):
        self.directory = Path(tempfile.mkdtemp(prefix="tower-bridge-", dir=directory))
        self.path = str(self.directory / "events.sock")
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sessions = {}
        self.rejected = self.received = 0
        self.closed = False
        try:
            os.chmod(self.directory, 0o700)
            if hasattr(socket, "SO_PASSCRED"):
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
            self.socket.bind(self.path)
            os.chmod(self.path, 0o600)
            self.socket.setblocking(False)
        except BaseException:
            self.close()
            raise

    def register(self, session_id):
        session_id = str(session_id)
        if not session_id or len(session_id) > 128 or "\0" in session_id:
            raise ValueError("Invalid terminal session identity.")
        if session_id not in self.sessions and len(self.sessions) >= 32:
            raise ValueError("Terminal bridge session limit reached.")
        token = self.sessions.setdefault(session_id, secrets.token_urlsafe(32))
        return {"TOWER_BRIDGE_SOCKET": self.path, "TOWER_BRIDGE_TOKEN": token,
                "TOWER_BRIDGE_SESSION": session_id}

    def unregister(self, session_id):
        self.sessions.pop(str(session_id), None)

    def poll(self, limit=32):
        if self.closed:
            return []
        result = []
        for _ in range(max(0, min(int(limit), 128))):
            try:
                data, ancillary, flags, _address = self.socket.recvmsg(MAX_MESSAGE + 1, socket.CMSG_SPACE(12))
            except BlockingIOError:
                break
            if self.closed:
                break
            try:
                if len(data) > MAX_MESSAGE or flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC):
                    raise ValueError("Oversize bridge message.")
                if hasattr(socket, "SO_PASSCRED"):
                    credentials = [struct.unpack("3i", body[:12]) for level, kind, body in ancillary
                                   if level == socket.SOL_SOCKET and kind == socket.SCM_CREDENTIALS and len(body) >= 12]
                    if len(credentials) != 1 or credentials[0][1] != os.getuid():
                        raise ValueError("Bridge peer ownership mismatch.")
                value = json.loads(data)
                message = _bounded(value)
                expected = self.sessions.get(message["session"])
                if expected is None or not hmac.compare_digest(expected, value["token"]):
                    raise ValueError("Bridge session token mismatch.")
            except (ValueError, TypeError, UnicodeError, RecursionError):
                self.rejected += 1
                continue
            self.received += 1
            result.append(message)
        return result

    def close(self):
        if not self.closed:
            self.closed = True
            self.socket.close()
            shutil.rmtree(self.directory)
            self.sessions.clear()


def report(event, *, env=None, **fields):
    env = os.environ if env is None else env
    path, token, session = (env.get(key, "") for key in
                            ("TOWER_BRIDGE_SOCKET", "TOWER_BRIDGE_TOKEN", "TOWER_BRIDGE_SESSION"))
    if not path or not token or not session:
        raise ValueError("This shell has no Tower bridge; open a Tower terminal session first.")
    info = os.lstat(path)
    parent = os.stat(Path(path).parent)
    if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077
            or parent.st_uid != os.getuid() or parent.st_mode & 0o077):
        raise ValueError("The Tower bridge must be a private socket owned by this user.")
    value = {"schema": SCHEMA, "event": event, "session": session, "token": token, **fields}
    _bounded(value)
    data = json.dumps(value, ensure_ascii=True, allow_nan=False).encode()
    if len(data) > MAX_MESSAGE:
        raise ValueError("Terminal report exceeds the message budget.")
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as client:
        client.settimeout(0.05)
        client.sendto(data, path)
    return True


def _hook_template(shell):
    """Print explicit integration; do not edit startup files or capture input.

    Bash uses PS0 rather than replacing the user's DEBUG trap. Commands are
    never extracted from shell history. Zsh/Fish command labels require opt-in.
    """
    if shell == "bash":
        return r'''# Tower optional Bash 4.4+ hooks. No command text is captured.
if (( BASH_VERSINFO[0] < 4 || (BASH_VERSINFO[0] == 4 && BASH_VERSINFO[1] < 4) )); then
  printf '%s\n' 'Tower shell integration requires Bash 4.4 or later.' >&2
  return 2
fi
if [[ -z ${_TOWER_HOOK_INSTALLED:-} ]]; then
  _TOWER_HOOK_INSTALLED=1
  _tower_prompt() {
    local tower_exit=$?
    command tower terminal report end --status "$tower_exit" --cwd "$PWD" 2>/dev/null
    command tower terminal report prompt --cwd "$PWD" 2>/dev/null
    return "$tower_exit"
  }
  PS0='$(command tower terminal report start --cwd "$PWD" 2>/dev/null)'"${PS0-}"
  if [[ $(declare -p PROMPT_COMMAND 2>/dev/null) == "declare -a"* ]]; then
    PROMPT_COMMAND=(_tower_prompt "${PROMPT_COMMAND[@]}")
  else
    PROMPT_COMMAND="_tower_prompt${PROMPT_COMMAND:+; $PROMPT_COMMAND}"
  fi
fi
'''
    if shell == "zsh":
        return r'''# Tower optional Zsh hooks. Command text requires explicit opt-in.
if [[ -z ${_TOWER_HOOK_INSTALLED:-} ]]; then
  _TOWER_HOOK_INSTALLED=1
  autoload -Uz add-zsh-hook
  _tower_preexec() {
    if [[ ${TOWER_CAPTURE_COMMANDS:-0} == 1 ]]; then
      command tower terminal report start --cwd "$PWD" --command "$1" 2>/dev/null
    else
      command tower terminal report start --cwd "$PWD" 2>/dev/null
    fi
  }
  _tower_precmd() {
    local tower_exit=$?
    command tower terminal report end --status "$tower_exit" --cwd "$PWD" 2>/dev/null
    command tower terminal report prompt --cwd "$PWD" 2>/dev/null
    return "$tower_exit"
  }
  add-zsh-hook preexec _tower_preexec
  add-zsh-hook precmd _tower_precmd
fi
'''
    if shell == "fish":
        return r'''# Tower optional Fish hooks. Command text requires explicit opt-in.
if not set -q _TOWER_HOOK_INSTALLED
  set -g _TOWER_HOOK_INSTALLED 1
  function _tower_preexec --on-event fish_preexec
    if test "$TOWER_CAPTURE_COMMANDS" = 1
      command tower terminal report start --cwd "$PWD" --command "$argv[1]" 2>/dev/null
    else
      command tower terminal report start --cwd "$PWD" 2>/dev/null
    end
  end
  function _tower_postexec --on-event fish_postexec
    set -l tower_exit $status
    command tower terminal report end --status "$tower_exit" --cwd "$PWD" 2>/dev/null
  end
  function _tower_prompt --on-event fish_prompt
    command tower terminal report prompt --cwd "$PWD" 2>/dev/null
  end
end
'''
    raise ValueError("Supported hooks: bash, zsh, fish.")



def helper_command(shell, *arguments):
    """Use the actual installed/source helper, independent of aliases and PATH."""
    if shell not in ("bash", "zsh", "fish"):
        raise ValueError("Supported hooks: bash, zsh, fish.")
    argv = [os.path.abspath(sys.executable), str(Path(__file__).resolve()), *arguments]
    def quote(value):
        if shell == "fish":
            return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"
        return shlex.quote(value)
    return " ".join(quote(str(value)) for value in argv)


def shell_hook(shell):
    template = _hook_template(shell).replace("command tower terminal report", "_tower_report")
    command = helper_command(shell, "report")
    if shell == "fish":
        helper = "function _tower_report\n  command " + command + " $argv\nend\n"
    else:
        helper = "_tower_report() { command " + command + ' "$@"; }\n'
    return "# Helper paths are bound to the Tower installation that generated this hook.\n" + helper + template

def cli(argv=None):
    parser = argparse.ArgumentParser(prog="tower terminal")
    sub = parser.add_subparsers(dest="action", required=True)
    hook = sub.add_parser("hook")
    hook.add_argument("--shell", choices=("bash", "zsh", "fish"), default="bash")
    emit = sub.add_parser("report")
    emit.add_argument("event", choices=sorted(EVENTS))
    for key in ("cwd", "command", "job", "project", "artifact", "manifest", "host", "command-id", "label"):
        emit.add_argument("--" + key)
    emit.add_argument("--status", type=int)
    emit.add_argument("--summary", help="Explicit JSON package/module summary for an environment report")
    args = parser.parse_args(argv)
    try:
        if args.action == "hook":
            print(shell_hook(args.shell), end="")
        else:
            fields = {key: value for key, value in vars(args).items() if key not in ("action", "event", "summary") and value is not None}
            if args.event == "environment":
                fields["environment"] = {key: os.environ[key] for key in ENV_KEYS if key in os.environ}
                if args.summary:
                    fd = os.open(args.summary, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
                    with os.fdopen(fd, "rb") as stream:
                        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                            raise ValueError("Environment summary must be a regular file.")
                        raw = stream.read(MAX_MESSAGE + 1)
                    if len(raw) > MAX_MESSAGE:
                        raise ValueError("Environment summary exceeds the bridge limit.")
                    summary = json.loads(raw)
                    if not isinstance(summary, dict) or set(summary) - {"packages", "modules"}:
                        raise ValueError("Environment summary accepts packages and modules dictionaries only.")
                    fields.update(summary)
            elif args.summary:
                raise ValueError("--summary is only valid for an environment report.")
            report(args.event, **fields)
        return 0
    except (OSError, ValueError) as exc:
        print("tower terminal: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(cli())
