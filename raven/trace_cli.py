"""Compatibility alias for tracing.cli."""
import sys
from .tracing import cli as _implementation

if __name__ == "__main__":
    raise SystemExit(_implementation.main())

sys.modules[__name__] = _implementation
