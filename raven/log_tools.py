"""Compatibility alias for job_logs.tools."""
import sys
from .job_logs import tools as _implementation

sys.modules[__name__] = _implementation
