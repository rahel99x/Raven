"""Fresh child ownership shim; no Python fork callback runs in the UI."""
from __future__ import annotations

import os
import signal
import sys


def main(argv=None):
    values = list(sys.argv[1:] if argv is None else argv)
    if len(values) < 2:
        return 64
    try:
        expected_parent = int(values.pop(0))
    except ValueError:
        return 64
    if os.getppid() != expected_parent:
        return 125
    if sys.platform.startswith("linux"):
        import ctypes
        if ctypes.CDLL(None).prctl(1, signal.SIGTERM, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
            return 125
        if os.getppid() != expected_parent:
            return 125
    try:
        os.execvpe(values[0], values, os.environ)
    except OSError as exc:
        sys.stderr.write("Raven owned launcher could not execute: " + str(exc)[:512] + "\n")
        return 127


if __name__ == "__main__":
    raise SystemExit(main())
