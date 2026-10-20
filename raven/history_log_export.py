"""Compatibility alias for job_logs.history_export."""
import sys
from .job_logs import history_export as _implementation

sys.modules[__name__] = _implementation
