"""Compatibility alias for :mod:`raven.installation.update_revert`."""

import sys as _sys
from importlib import import_module as _import_module

_implementation = _import_module(".installation.update_revert", __package__)
_sys.modules[__name__] = _implementation
