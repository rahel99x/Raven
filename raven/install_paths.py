"""Compatibility alias for :mod:`raven.installation.install_paths`."""

import sys as _sys
from importlib import import_module as _import_module

_implementation = _import_module(".installation.install_paths", __package__)
_sys.modules[__name__] = _implementation
