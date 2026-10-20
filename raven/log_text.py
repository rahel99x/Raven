"""Compatibility alias for job_logs.text."""
import sys
from .job_logs import text as _implementation

sys.modules[__name__] = _implementation
