"""Small core handlers dispatched by the existing canonical command registry.

The controller retains input parsing, extension precedence and error feedback.
These handlers preserve existing context/review behavior; they do not register
new commands or execute scheduler actions independently.
"""

from __future__ import annotations

from typing import Any


def export(app: Any, args: list[str], line: str) -> None:
    if args and (len(args) != 1 or args[0] not in ("text", "csv", "json", "report")):
        app.fail("export <text|csv|json|report>")
        return
    app.export(args[0] if args else "text")


def replay(app: Any, args: list[str], line: str) -> None:
    app.replay_control(args[0] if args else "", " ".join(args[1:]))


def wrap(app: Any, args: list[str], line: str) -> None:
    app.handle_action("wrap")


def bookmark(app: Any, args: list[str], line: str) -> None:
    app.handle_action("bookmark")


def evaluate(app: Any, args: list[str], line: str) -> None:
    payload = line.strip().split(None, 1)
    app.say(app.evaluate(payload[1] if len(payload) > 1 else ""))


def advise(app: Any, args: list[str], line: str) -> None:
    app.say(app.advise(args[0] if args else None))
