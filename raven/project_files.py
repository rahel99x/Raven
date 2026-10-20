"""Confined project root and exact local-adapter ports; no project/UI owner."""

from __future__ import annotations
import os
from .local_files import local_supported


def _local(files):
    if not local_supported(files):
        raise ValueError(
            "Project discovery requires Raven running locally on CARC; this file backend cannot prove confined directory reads"
        )


def _root(path):
    if not isinstance(path, str) or not path or "\x00" in path:
        raise ValueError("project root must be an explicit filesystem path")
    root = os.path.realpath(os.path.abspath(os.path.expanduser(path)))
    # Pin every parent, including the supplied root's ancestors. A valid
    # inventory cannot turn a replaced project directory into an external read.
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in root.split("/"):
            if not component:
                continue
            next_fd = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
            )
            os.close(fd)
            fd = next_fd
        return root, fd
    except BaseException:
        os.close(fd)
        raise
