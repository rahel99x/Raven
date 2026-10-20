"""Compatibility alias for tracing.capabilities."""
import sys
from .tracing import capabilities as _implementation

if __name__ == "__main__":
    raise SystemExit(_implementation.main())

sys.modules[__name__] = _implementation
