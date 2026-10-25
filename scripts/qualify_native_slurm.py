#!/usr/bin/env python3
"""Qualify native scheduler services only in Raven's disposable Slurm fixture.

This wrapper reuses the pinned image, internal network, unprivileged fixture
login, source copy, evidence capture, and owned cleanup of qualify_slurm.py.
It never submits work to the invoking host's scheduler.
"""

from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from qualify_slurm import main as qualify


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if any(
        item == "--scenario" or item.startswith("--scenario=") for item in arguments
    ):
        raise ValueError("Native qualification selects its own disposable scenario")
    return qualify(["--scenario", "native-scheduler", *arguments])


if __name__ == "__main__":
    raise SystemExit(main())
