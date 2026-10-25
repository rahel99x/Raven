"""Literal shell declarations shared by reviewed batch-script composers."""

import re
import shlex


def with_environment(script, values, guard=""):
    """Keep leading scheduler directives intact; quote every declared value."""
    if (
        not isinstance(script, str)
        or not isinstance(guard, str)
        or not isinstance(values, dict)
    ):
        raise ValueError("Shell composition requires text and an environment mapping")
    for key, value in values.items():
        if (
            not isinstance(key, str)
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
            or not isinstance(value, str)
            or "\0" in value
        ):
            raise ValueError(
                "Shell environment requires valid names and NUL-free literal values"
            )
    lines = script.splitlines(keepends=True)
    end = next(
        (
            i
            for i, line in enumerate(lines)
            if line.strip() and not line.lstrip().startswith("#")
        ),
        len(lines),
    )
    prefix = "".join(lines[:end])
    if prefix and not prefix.endswith("\n"):
        prefix += "\n"
    exports = "".join(
        "export " + key + "=" + shlex.quote(value) + "\n"
        for key, value in values.items()
    )
    return prefix + exports + guard + "".join(lines[end:])
