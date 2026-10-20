"""Compatibility alias for job_logs.match_index."""
import sys
from .job_logs import match_index as _implementation

sys.modules[__name__] = _implementation
