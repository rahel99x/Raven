#!/usr/bin/env python3
"""Run incremental quality contracts without formatting unrelated legacy code."""
from pathlib import Path
import subprocess
import sys


def main():
    root = Path(__file__).resolve().parents[1]
    paths = ["raven/transport", "raven/command_handlers.py"]
    commands = [
        [str(Path(sys.executable).parent / "lint-imports"), "--config", ".importlinter"],
        [sys.executable, "-m", "ruff", "check", "--config", "ruff-boundary.toml", *paths],
        [sys.executable, "-m", "ruff", "format", "--check", "--config", "ruff-boundary.toml", *paths],
        [sys.executable, "-m", "mypy", "--config-file", "mypy-boundary.ini"],
    ]
    for command in commands:
        try:
            completed = subprocess.run(command, cwd=root, check=False)
        except FileNotFoundError:
            print("Install Raven's development dependencies: python -m pip install -e '.[dev]'", file=sys.stderr)
            return 2
        if completed.returncode:
            return completed.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
