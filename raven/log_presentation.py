"""Compatibility alias for job_logs.presentation."""
import sys
from .job_logs import presentation as _implementation

sys.modules[__name__] = _implementation
