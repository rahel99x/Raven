#!/usr/bin/env python3
"""Run Raven's GPU check from a checkout, without installing a new command."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from raven.cli import main

if __name__ == "__main__":
    # A leading individual job ID is optional. All ordinary Raven connection,
    # profile and config flags remain available.
    args = sys.argv[1:]
    job = args.pop(0) if args and not args[0].startswith("-") else "all"
    raise SystemExit(main(["--gpu-check", job, *args]))
