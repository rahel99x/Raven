"""Compatibility module for :mod:`raven.heavy.worker_client`."""

import sys
from .heavy import worker_client as _implementation

sys.modules[__name__] = _implementation
