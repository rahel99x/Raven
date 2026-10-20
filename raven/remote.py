"""Remote mode: every Slurm command runs on a login node over ssh (a ControlMaster connection is reused, so a
command costs a round trip, not a handshake), and the files the dashboard reads (stdout, GPU traces) are read
there too, by offset, so following a log still costs one stat per frame."""
from __future__ import annotations

import re
import shlex
import subprocess
import time
from typing import Callable, List, Optional, Sequence, Tuple

from .slurm import Backend, CommandError


class SshBackend(Backend):
    def __init__(self, host: str, user: str = "", opts: Sequence[str] = (), control: bool = True, runner: Optional[Callable] = None):
        if (not isinstance(host, str) or len(host) > 253
                or not re.fullmatch(r"(?:[A-Za-z0-9][A-Za-z0-9_.:-]*|\[[A-Fa-f0-9:.]+\])", host)):
            raise ValueError("SSH host must be a hostname, address or configured host alias")
        if not isinstance(user, str) or user and (user.startswith("-") or not re.fullmatch(r"[A-Za-z0-9_.@-]+", user)):
            raise ValueError("SSH username contains unsupported characters")
        self.host, self.user, self.opts, self.control = host, user, list(opts), control
        self.runner = runner or self._run
        self.target = f"{user}@{host}" if user else host
        self.control_path = ""
        self.control_diagnostic = ""

    def argv(self, remote: Sequence[str]) -> List[str]:
        target, options = self.target, list(self.opts)
        if self.control:
            from .ssh_control import MultiplexUnavailable, prepare
            try:
                prefix, control_path = prepare(target, options)
                self.control_path, self.control_diagnostic = control_path, ""
            except (MultiplexUnavailable, OSError, ValueError) as exc:
                self.control_diagnostic = str(exc)
                self.control_path = ""
                prefix = ["ssh", "-o", "ControlMaster=no", "-o", "ControlPath=none", *options, "-S", "none"]
        else:
            # Explicit opt-out also overrides multiplexing in ~/.ssh/config.
            prefix = ["ssh", "-o", "ControlMaster=no", "-o", "ControlPath=none", *options, "-S", "none"]
        defaults = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=6", "-o", "ServerAliveInterval=15", "-o", "LogLevel=ERROR"]
        return [*prefix, *defaults, "--", target, shlex.join(remote)]

    @staticmethod
    def _run(argv: List[str], timeout: float) -> Tuple[int, str, str]:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr

    def run(self, cmd: Sequence[str], timeout: float = 8.0) -> Tuple[str, float]:
        t0 = time.perf_counter()
        try:
            rc, out, err = self.runner(self.argv(cmd), timeout + 5)
        except subprocess.TimeoutExpired:
            raise CommandError(f"{cmd[0]}@{self.host}: timed out after {timeout:g}s")
        except (OSError, ValueError) as e:
            raise CommandError(f"ssh: {e}")
        dt = time.perf_counter() - t0
        if rc == 255:
            raise CommandError(f"ssh {self.target}: {err.strip()[:200] or 'connection failed'}")
        if rc != 0:
            raise CommandError(f"{cmd[0]}@{self.host} exit {rc}: {(err or out).strip()[:200]}")
        return out, dt

    def inspect_command(self, argv, timeout, limit, cancel=None, *, depth=0):
        from .bounded_command import _process
        if self.runner is SshBackend._run:
            return _process(self.argv(argv), timeout + 5, limit, cancel)
        out = self.run(argv, timeout)[0]
        if not isinstance(out, str) or len(out.encode("utf-8")) > limit:
            raise CommandError("Command output exceeded the inspection limit.")
        return out

    def call(self, cmd: Sequence[str], timeout: float = 15.0) -> Tuple[bool, str]:
        try:
            rc, out, err = self.runner(self.argv(cmd), timeout + 5)
        except (OSError, ValueError, subprocess.TimeoutExpired) as e:
            return False, str(e)
        return rc == 0, (out + err).strip()


from .local_files import LocalFiles as LocalFiles


from .remote_files import RemoteFiles as RemoteFiles
