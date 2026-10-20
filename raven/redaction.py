"""Dependency-free redaction for diagnostics and explicitly shared evidence."""

from __future__ import annotations

import re

_ASSIGNMENT = re.compile(
    r"""(?im)(["']?[\w.-]*(?:password|passwd|token|secret|api[_-]?key|access[_-]?key)[\w.-]*["']?\s*[=:]\s*["']?)([^\s"',;]+)"""
)
_AUTHORIZATION = re.compile(r"(?im)(authorization\s*:\s*)([^\r\n,;]+)")
_PASSWORD_FLAG = re.compile(
    r"""(?i)(--(?:password|passwd|token|secret|api-key)(?:=|\s+)["']?)([^\s"']+)"""
)
_TOKENS = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9_]{6,}|github_pat_[A-Za-z0-9_]{6,}|hf_[A-Za-z0-9]{6,}|sk-[A-Za-z0-9_-]{6,}|AKIA[A-Z0-9]{12,}|xox[baprs]-[A-Za-z0-9-]{6,}|eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)\b"
)
_URL_USERINFO = re.compile(r"(?i)(https?://)[^/@\s]+@")


def redact(text):
    """Mask common credential forms; this is not proof all secrets are absent."""
    value = str(text)
    for expression in (_AUTHORIZATION, _ASSIGNMENT, _PASSWORD_FLAG):
        value = expression.sub(lambda match: match.group(1) + "[REDACTED]", value)
    value = _TOKENS.sub("[REDACTED]", value)
    return _URL_USERINFO.sub(r"\1[REDACTED]@", value)
