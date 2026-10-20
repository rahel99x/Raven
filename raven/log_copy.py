"""Compatibility alias for job_logs.copy."""
import sys
from .job_logs import copy as _implementation

sys.modules[__name__] = _implementation
