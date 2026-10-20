"""Compatibility alias for launching.runtime."""
import sys
from .launching import runtime as _implementation

if __name__ == "__main__":
    raise SystemExit(_implementation.main())

sys.modules[__name__] = _implementation
