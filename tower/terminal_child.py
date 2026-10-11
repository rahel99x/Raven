"""Clean executable PTY child bootstrap; never run this in Tower's UI process.

The broker passes an already-open slave as fd 0/1/2.  A fresh Python process
establishes the session and controlling terminal before exec, so no Python
``preexec_fn`` or fork callback runs after Tower starts its worker threads.
"""
from __future__ import annotations

import fcntl
import os
import signal
import sys
import termios


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        return 64
    try:
        os.setsid()
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
        os.tcsetpgrp(0, os.getpgrp())
        for name in ("SIGINT", "SIGQUIT", "SIGTSTP", "SIGTTIN", "SIGTTOU", "SIGPIPE", "SIGHUP"):
            signal.signal(getattr(signal, name), signal.SIG_DFL)
        os.execvpe(argv[0], argv, os.environ)
    except (OSError, ValueError) as exc:
        # The diagnostic remains inside the child PTY, never the outer terminal.
        os.write(2, ("Tower terminal: " + str(exc) + "\r\n").encode("utf-8", "replace"))
        return 127


if __name__ == "__main__":
    raise SystemExit(main())
