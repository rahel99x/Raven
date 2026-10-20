"""Compatibility alias for job_logs.bundle."""
import sys
from .job_logs import bundle as _implementation

sys.modules[__name__] = _implementation
