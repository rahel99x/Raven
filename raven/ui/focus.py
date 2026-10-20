"""Bounded modal ownership; an unhandled event never reaches a covered owner.

Composition supplies callbacks, so this contract imports no feature modules.
A toolbar can cover a dialog without replacing its identity. Captures and shell
focus can add another frame; only the highest frame receives each input event.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable

MAX_DEPTH = 8


@dataclass(frozen=True)
class Owner:
    name: str
    key: Callable[[object, str], object] | None = None
    mouse: Callable[..., object] | None = None
    paste: Callable[[object, str], object] | None = None


@dataclass(frozen=True)
class Frame:
    owner: Owner
    identity: tuple[Any, ...]


class FocusStack:
    """One session's small ordered stack, independent of event-handler results."""

    def __init__(self) -> None:
        self.frames: tuple[Frame, ...] = ()
        self.revision = 0

    def publish(self, frames: Iterable[Frame]) -> None:
        values = tuple(frames)
        if len(values) > MAX_DEPTH:
            raise ValueError("Focus stack exceeds its bounded modal depth")
        if len({frame.owner.name for frame in values}) != len(values):
            raise ValueError("A focus owner cannot occupy two stack frames")
        if values != self.frames:
            self.frames = values
            self.revision += 1

    @property
    def top(self) -> Frame | None:
        return self.frames[-1] if self.frames else None

    def key(self, app: object, key: str) -> bool:
        frame = self.top
        if frame is None:
            return False
        if frame.owner.key is not None:
            frame.owner.key(app, key)
        return True

    def mouse(
        self, app: object, y: int, x: int, *, button: str = "left", shift: bool = False
    ) -> bool:
        frame = self.top
        if frame is None:
            return False
        if frame.owner.mouse is not None:
            frame.owner.mouse(app, y, x, button=button, shift=shift)
        return True
