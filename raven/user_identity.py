"""Local login identity comes from the uid, independently of shell variables."""

from __future__ import annotations

import os
import pwd


def current_user():
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        return str(os.getuid())
