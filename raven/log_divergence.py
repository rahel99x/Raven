"""Compatibility alias for job_logs.divergence."""
import sys
from .job_logs import divergence as _implementation

sys.modules[__name__] = _implementation
