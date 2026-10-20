"""Explicit UI callbacks for recording preference application services."""

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class SettingsCallbacks:
    state: Callable[[object], dict]
    scope: Callable[[object], tuple]
    submit: Callable[..., bool]
    notify: Callable[..., None]
