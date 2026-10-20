"""Compatibility alias for job_logs.catalog."""
import sys
from .job_logs import catalog as _implementation

sys.modules[__name__] = _implementation
