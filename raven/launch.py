"""Compatibility alias for entry.workload."""
import sys
from .entry import workload as _implementation

if __name__ == "__main__":
    raise SystemExit(_implementation.main())

sys.modules[__name__] = _implementation
