"""Pure bounded Slurm host-list expansion."""

import re


def expand_hostlist(value, *, maximum=4096):
    """Bounded local Slurm host-list expansion, with no subprocess or RPC.

    Unsupported or oversized syntax is rejected instead of silently treating
    an unexpanded expression as verified membership in an allocation.
    """
    if not isinstance(value, str) or len(value) > 65536 or not value:
        raise ValueError("Invalid bounded host list")
    groups = re.split(r",(?![^\[]*\])", value)
    result = []
    for group in groups:
        parts = re.split(r"(\[[^\[\]]+\])", group)
        names = [""]
        for part in parts:
            if not part:
                continue
            if part.startswith("["):
                suffixes = []
                for item in part[1:-1].split(","):
                    match = re.fullmatch(r"([0-9]+)(?:-([0-9]+))?", item)
                    if match is None:
                        raise ValueError("Unsupported host-list range")
                    first, last = int(match[1]), int(match[2] or match[1])
                    if last < first or last - first + 1 > maximum:
                        raise ValueError("Host-list range exceeds its bound")
                    width = len(match[1]) if match[1].startswith("0") else 0
                    suffixes.extend(
                        str(number).zfill(width) for number in range(first, last + 1)
                    )
                    if len(suffixes) > maximum:
                        raise ValueError("Host-list expansion exceeds its bound")
            else:
                if not re.fullmatch(r"[A-Za-z0-9_.-]+", part):
                    raise ValueError("Unsupported host-list syntax")
                suffixes = [part]
            if len(names) * len(suffixes) + len(result) > maximum:
                raise ValueError("Host-list expansion exceeds its bound")
            names = [prefix + suffix for prefix in names for suffix in suffixes]
        result.extend(names)
    if len(result) > maximum or any(not name or len(name) > 253 for name in result):
        raise ValueError("Host-list expansion exceeds its bound")
    return result
