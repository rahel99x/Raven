"""Compatibility alias for entry.launcher."""
import sys
from .entry import launcher as _implementation

if __name__ == "__main__":
    raise SystemExit(_implementation.light_main())

sys.modules[__name__] = _implementation
