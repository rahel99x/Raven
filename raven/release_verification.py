"""Compatibility alias for :mod:`raven.installation.release_verification`."""

import sys as _sys
from importlib import import_module as _import_module

_implementation = _import_module(".installation.release_verification", __package__)
_sys.modules[__name__] = _implementation
