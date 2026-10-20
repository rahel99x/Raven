"""Compatibility alias for job_logs.scan."""
import sys
from .job_logs import scan as _implementation

sys.modules[__name__] = _implementation
