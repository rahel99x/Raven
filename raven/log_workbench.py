"""Compatibility alias for job_logs.workbench."""
import sys
from .job_logs import workbench as _implementation

sys.modules[__name__] = _implementation
