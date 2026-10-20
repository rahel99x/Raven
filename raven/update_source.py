"""Compatibility alias for :mod:`raven.installation.update_source`."""

import sys as _sys
from importlib import import_module as _import_module

_implementation = _import_module(".installation.update_source", __package__)
_sys.modules[__name__] = _implementation
