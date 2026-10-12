"""Synthetic diagnostic oracle. Only the explicit 'needle' line triggers it."""
from pathlib import Path
import sys

lines = Path(sys.argv[1]).read_text(encoding="utf-8").splitlines()
if "needle" in lines:
    print("RAVEN_DEMO_TARGET: deliberate reproducible assertion")
    raise SystemExit(7)
print("RAVEN_DEMO_CLEAR")
