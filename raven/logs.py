"""Compatibility alias for job_logs.buffer."""
import sys
from .job_logs import buffer as _implementation

sys.modules[__name__] = _implementation
