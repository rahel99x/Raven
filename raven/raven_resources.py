"""Compatibility module for :mod:`raven.heavy.resources`."""
import sys
from .heavy import resources as _implementation
sys.modules[__name__] = _implementation
