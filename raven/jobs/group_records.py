"""Immutable group records and bounded snapshot-value helpers.

Groups are presentation objects, never scheduler allocations.  Every Child
retains its original record and ID; a Header cannot be passed to a job action.
Inference reads supplied snapshots only and never queries Slurm or the disk.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import blake2s
import re
import shlex
from typing import Any


BURST_SECONDS = 10
MAX_ID_GAP = 1
SWEEP_SECONDS = 30
SWEEP_ID_GAP = 8
SWEEP_DENSITY = 4
DEPENDENCY_SECONDS = 60
DEPENDENCY_ID_SPAN = 256
MAX_COLLAPSED = 256
MAX_GROUP_PREFIX = 160
_ARRAY = re.compile(r"^([0-9]{1,19})_([0-9]{1,19}|\[[0-9,:%-]+\])(?:\+[0-9]{1,19})?$")
_HET = re.compile(r"^([0-9]{1,19})\+[0-9]{1,19}$")
_MARKER = re.compile(r"^(?:launch:|group:)([A-Za-z0-9][A-Za-z0-9_.-]{0,127})$")
_EXPLICIT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_FAMILY = re.compile(r"^(.*?)[_.-]([0-9]+)$")
_NON_PRINTABLE_ASCII = re.compile(r"[^\x20-\x7e]")
_EMPTY = frozenset(("", "Unknown", "N/A", "NONE", "None", "(null)", "-"))
_MISSING = object()
_EMPTY_VALUES = (_MISSING,) * 12
_EMPTY_MAP = {}
_RECORD_FIELDS = (
    "id",
    "name",
    "submit",
    "cluster",
    "user",
    "account",
    "workdir",
    "command",
    "RavenLaunchId",
    "LaunchId",
    "LaunchGroup",
    "JobGroup",
    "comment",
    "dependency",
    "start",
)


def _value(record, key, default=""):
    if isinstance(record, dict):
        return record.get(key, default)
    value = getattr(record, key, _MISSING)
    if value is not _MISSING:
        return value
    get = getattr(record, "get", None)
    return get(key, default) if callable(get) else default


def _text(value):
    return (
        value
        if isinstance(value, str) and value not in _EMPTY and len(value) <= 4096
        else ""
    )


def shared_name_prefix(names):
    """Find a useful display prefix in one pass over a name iterator.

    Keep one reference and a shrinking character limit, never a name list,
    sorted copy, or trie. Plain names reuse their existing strings. Only a
    control-bearing name needs a temporary normalization of its bounded input.
    Work stops as soon as fewer than two characters can remain useful.
    """
    from ..job_logs.text import display_text

    def normalized(name, limit):
        if not isinstance(name, str) or len(name) > 4096:
            return ""
        # Unknown tokens are short. Avoid hashing a long new name merely to
        # test that small set before the bounded prefix scan.
        if len(name) <= MAX_GROUP_PREFIX and name in _EMPTY:
            return ""
        # ASCII names require no copies, and only the remaining candidate
        # prefix is examined. Printable Unicode takes one bounded slice;
        # normalization of the complete bounded input is for controls only.
        if (
            _NON_PRINTABLE_ASCII.search(name, 0, limit) is None
            or name[:limit].isprintable()
        ):
            return name
        name = display_text(name)
        if not name.isprintable():
            # Includes Unicode format controls not handled as ANSI escapes.
            name = "".join(char if char.isprintable() else " " for char in name)
        return name

    names = iter(names)
    first = normalized(next(names, ""), MAX_GROUP_PREFIX)
    limit = min(len(first), MAX_GROUP_PREFIX)
    if limit < 2:
        return ""
    for name in names:
        name = normalized(name, limit)
        limit = min(limit, len(name))
        offset = 0
        while offset < limit and first[offset] == name[offset]:
            offset += 1
        limit = offset
        if limit < 2:
            return ""
    prefix = first[:limit].rstrip(" _-./:|([{–—").strip()
    return prefix if len(prefix) >= 2 and any(char.isalnum() for char in prefix) else ""


def array_parent(group):
    """Read the scheduler identity independently of the presentation label."""
    if group.kind == "array" and group.id.startswith("array:"):
        return group.id.split(":", 2)[1]
    return ""


def _digest(parts):
    # Length prefixes avoid collisions between field separators in paths/names.
    digest = blake2s(digest_size=10)
    for part in parts:
        raw = str(part).encode("utf-8", "surrogatepass")
        digest.update(str(len(raw)).encode("ascii") + b":" + raw)
    return digest.hexdigest()


def _script(command):
    """Extract only a shell-tokenized executable/script, never execute it."""
    try:
        pieces = shlex.split(command)
    except ValueError:
        return ""
    if not pieces:
        return ""
    first = pieces[0]
    if first.rsplit("/", 1)[-1] in (
        "env",
        "srun",
        "mpirun",
        "mpiexec",
        "uv",
        "torchrun",
    ):
        # A shared launcher is not evidence of a shared scientific script.
        return command
    if first.rsplit("/", 1)[-1] in (
        "bash",
        "sh",
        "zsh",
        "python",
        "python3",
        "perl",
        "Rscript",
    ):
        # Inline snippets and interpreter switches provide no script identity.
        if len(pieces) < 2 or pieces[1].startswith("-"):
            return command
        return first + " " + pieces[1]
    return first


_STATUS = (
    ("running", "run", "running", "green"),
    ("pending", "pend", "pending", "yellow"),
    ("dependent", "dep", "dependency wait", "cyan"),
    ("blocked", "never", "dependency never satisfied", "red"),
    ("completed", "done", "completed", "green"),
    ("failed", "fail", "failed", "red"),
    ("cancelled", "cancel", "cancelled", "yellow"),
    ("other", "other", "other", "dim"),
)
_ACCOUNTING_STATUS = (("accounting", "accounting", "awaiting accounting", "dim"),)


def _waiting_dependency(value):
    from raven.deps import KINDS, parse_dependency

    value = _text(value)
    if not value:
        return False
    # squeue can retain fulfilled dependency clauses while Resources is the
    # current blocker. Strip fulfilled IDs before evaluating the remainder.
    value = re.sub(r"[0-9_]+(?:\+[0-9]+)?\(fulfilled\)", "", value, flags=re.IGNORECASE)
    return any(
        kind in KINDS
        and (
            kind == "singleton"
            or len(jid) <= 64
            and (jid.isascii() and jid.isdigit() or _ARRAY.fullmatch(jid))
        )
        for kind, jid in parse_dependency(value)
    )


@dataclass(frozen=True)
class StatusSummary:
    counts: Mapping[str, int]
    compressed: bool = False


def status_counts(records, *, awaiting_ids=()):
    """Disjoint state counts of distinct observed records, not expanded tasks.

    A fresh summary intentionally reads state/reason even when launch evidence
    has not changed. Compressed array rows count as one scheduler record.
    """
    if isinstance(records, StatusSummary):
        return records.counts
    counts = {key: 0 for key, _, _, _ in _STATUS}
    seen = set()
    for record in records:
        jid = _value(record, "id")
        if jid in seen:
            continue
        seen.add(jid)
        if jid in awaiting_ids:
            counts["accounting"] = counts.get("accounting", 0) + 1
            continue
        state = str(_value(record, "state")).split(" ", 1)[0].rstrip("+").upper()
        reason = str(_value(record, "reason")).replace("_", "").replace(" ", "").lower()
        if state in ("PENDING", "PD"):
            if "dependencyneversatisfied" in reason:
                key = "blocked"
            elif "dependency" in reason or _waiting_dependency(
                _value(record, "dependency")
            ):
                key = "dependent"
            else:
                key = "pending"
        elif state in (
            "RUNNING",
            "R",
            "COMPLETING",
            "CG",
            "CONFIGURING",
            "CF",
            "RESIZING",
            "SIGNALING",
            "STAGE_OUT",
        ):
            key = "running"
        elif state in ("COMPLETED", "CD"):
            key = "completed"
        elif state.startswith("CANCEL") or state == "CA":
            key = "cancelled"
        elif state in (
            "FAILED",
            "F",
            "OUT_OF_MEMORY",
            "OOM",
            "TIMEOUT",
            "TO",
            "NODE_FAIL",
            "NF",
            "BOOT_FAIL",
            "BF",
            "DEADLINE",
            "DL",
            "PREEMPTED",
            "PR",
            "REVOKED",
            "RV",
        ):
            key = "failed"
        else:
            key = "other"
        counts[key] += 1
    return counts


def summary_segments(records, *, ascii_=False, compact=True):
    if isinstance(records, StatusSummary):
        counts, compressed = records.counts, records.compressed
    else:
        records = tuple(records)
        counts = status_counts(records)
        compressed = any("_[" in str(_value(record, "id")) for record in records)
    result = []
    for key, short, label, style in _STATUS + _ACCOUNTING_STATUS:
        if counts.get(key, 0):
            if result:
                result.append((" | " if ascii_ else " · ", "dim"))
            result.append((f"{counts[key]} {short if compact else label}", style))
    if compressed:
        result.append((" (records)", "dim"))
    return result or [("0 jobs", "dim")]


def summary(records, compact=False):
    return "".join(
        text for text, _ in summary_segments(records, ascii_=True, compact=compact)
    )


@dataclass(frozen=True)
class Group:
    id: str
    label: str
    kind: str
    reason: str
    confidence: str
    members: tuple[str, ...]
    cohort: tuple = ()
    first_submit: float | None = None


@dataclass(frozen=True)
class Index:
    groups: Mapping[str, Group]
    by_job: Mapping[str, str]

    def for_job(self, job_id):
        return self.groups.get(self.by_job.get(job_id))


@dataclass(frozen=True)
class Header:
    group: Group
    records: tuple[Any, ...]
    collapsed: bool

    @property
    def visible_count(self):
        return len(self.records)

    @property
    def total_count(self):
        return len(self.group.members)


@dataclass(frozen=True)
class Child:
    record: Any
    group_id: str | None = None


@dataclass(frozen=True)
class GroupMeta:
    group: Group
    representative_id: str
    header: bool
    collapsed: bool
    records: tuple[Any, ...]
    stats: StatusSummary | None = None

    @property
    def visible_count(self):
        return len(self.records)

    @property
    def total_count(self):
        return len(self.group.members)


@dataclass(frozen=True)
class _Evidence:
    id: str
    name: str
    submit: str
    cluster: str
    user: str
    account: str
    workdir: str
    command: str
    explicit: str
    structural: tuple[str, str] | None
    dependency: str = ""
    comment: str = ""
    conflicting_marker: bool = False


def valid_group_id(value):
    return (
        isinstance(value, str)
        and len(value) <= 128
        and bool(re.fullmatch(r"[A-Za-z0-9:_.-]+", value))
    )


def contiguous_order(records, index):
    """Pack admitted batch members at their first source position, linearly."""
    if index is None:
        return list(records)
    blocks = {}
    for record in records:
        key = (
            ("group", index.by_job[record.id])
            if record.id in index.by_job
            else ("job", record.id)
        )
        blocks.setdefault(key, []).append(record)
    return [record for block in blocks.values() for record in block]
