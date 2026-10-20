"""Compatibility alias for job_logs.paths."""

import sys
from .job_logs import paths as _implementation

sys.modules[__name__] = _implementation
