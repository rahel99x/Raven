#!/usr/bin/env python3
"""Source-checkout entry points; no editable installation is required."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tower.raven_launch import light_main, heavy_main

if __name__ == "__main__":
    mode = sys.argv.pop(1)
    if mode not in {"light", "heavy"}:
        raise SystemExit("Expected light or heavy launcher")
    raise SystemExit((light_main if mode == "light" else heavy_main)(sys.argv[1:]))
