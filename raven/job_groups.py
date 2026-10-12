"""Conservative launch groups and shared, identity-safe table projections.

Groups are presentation objects, never scheduler allocations.  Every Child
retains its original record and ID; a Header cannot be passed to a job action.
Inference reads supplied snapshots only and never queries Slurm or the disk.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import blake2s
import re
import shlex
from types import MappingProxyType
from typing import Any

from .model import Finished, Job, stamp

BURST_SECONDS = 10
MAX_ID_GAP = 1
SWEEP_SECONDS = 30
SWEEP_ID_GAP = 8
SWEEP_DENSITY = 4
DEPENDENCY_SECONDS = 60
DEPENDENCY_ID_SPAN = 256
MAX_COLLAPSED = 256
_ARRAY = re.compile(r"^([0-9]{1,19})_([0-9]{1,19}|\[[0-9,:%-]+\])(?:\+[0-9]{1,19})?$")
_HET = re.compile(r"^([0-9]{1,19})\+[0-9]{1,19}$")
_MARKER = re.compile(r"^(?:launch:|group:)([A-Za-z0-9][A-Za-z0-9_.-]{0,127})$")
_EXPLICIT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_FAMILY = re.compile(r"^(.*?)[_.-]([0-9]+)$")
_EMPTY = frozenset(("", "Unknown", "N/A", "NONE", "None", "(null)", "-"))
_MISSING = object()
_EMPTY_VALUES = (_MISSING,) * 12
_EMPTY_MAP = {}
_RECORD_FIELDS = ("id", "name", "submit", "cluster", "user", "account", "workdir", "command",
                  "RavenLaunchId", "LaunchId", "LaunchGroup", "JobGroup", "comment", "dependency", "start")


def _value(record, key, default=""):
    if isinstance(record, dict):
        return record.get(key, default)
    value = getattr(record, key, _MISSING)
    if value is not _MISSING:
        return value
    get = getattr(record, "get", None)
    return get(key, default) if callable(get) else default


def _text(value):
    return value if isinstance(value, str) and value not in _EMPTY and len(value) <= 4096 else ""


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
    if first.rsplit("/", 1)[-1] in ("bash", "sh", "zsh", "python", "python3", "perl", "Rscript"):
        # Inline snippets and interpreter switches provide no script identity.
        if len(pieces) < 2 or pieces[1].startswith("-"):
            return command
        return first + " " + pieces[1]
    return first


_STATUS = (("running", "run", "running", "green"), ("pending", "pend", "pending", "yellow"),
           ("dependent", "dep", "dependency wait", "cyan"), ("blocked", "never", "dependency never satisfied", "red"),
           ("completed", "done", "completed", "green"), ("failed", "fail", "failed", "red"),
           ("cancelled", "cancel", "cancelled", "yellow"), ("other", "other", "other", "dim"))


def _waiting_dependency(value):
    from .deps import KINDS, parse_dependency
    value = _text(value)
    if not value:
        return False
    # squeue can retain fulfilled dependency clauses while Resources is the
    # current blocker. Strip fulfilled IDs before evaluating the remainder.
    value = re.sub(r"[0-9_]+(?:\+[0-9]+)?\(fulfilled\)", "", value, flags=re.IGNORECASE)
    return any(kind in KINDS and (kind == "singleton" or
               len(jid) <= 64 and (jid.isascii() and jid.isdigit() or _ARRAY.fullmatch(jid)))
               for kind, jid in parse_dependency(value))


@dataclass(frozen=True)
class StatusSummary:
    counts: Mapping[str, int]
    compressed: bool = False


def status_counts(records):
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
        state = str(_value(record, "state")).split(" ", 1)[0].rstrip("+").upper()
        reason = str(_value(record, "reason")).replace("_", "").replace(" ", "").lower()
        if state in ("PENDING", "PD"):
            if "dependencyneversatisfied" in reason:
                key = "blocked"
            elif "dependency" in reason or _waiting_dependency(_value(record, "dependency")):
                key = "dependent"
            else:
                key = "pending"
        elif state in ("RUNNING", "R", "COMPLETING", "CG", "CONFIGURING", "CF", "RESIZING", "SIGNALING", "STAGE_OUT"):
            key = "running"
        elif state in ("COMPLETED", "CD"):
            key = "completed"
        elif state.startswith("CANCEL") or state == "CA":
            key = "cancelled"
        elif state in ("FAILED", "F", "OUT_OF_MEMORY", "OOM", "TIMEOUT", "TO", "NODE_FAIL", "NF", "BOOT_FAIL", "BF", "DEADLINE", "DL", "PREEMPTED", "PR", "REVOKED", "RV"):
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
    for key, short, label, style in _STATUS:
        if counts[key]:
            if result:
                result.append((" | " if ascii_ else " · ", "dim"))
            result.append((f"{counts[key]} {short if compact else label}", style))
    if compressed:
        result.append((" (records)", "dim"))
    return result or [("0 jobs", "dim")]


def summary(records, compact=False):
    return "".join(text for text, _ in summary_segments(records, ascii_=True, compact=compact))


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


def _records(snap, duplicates=None):
    """Queue records win over departed/accounting records of the same ID."""
    records = {}
    from .manual_job_groups import selection_input
    for record in snap.get("group", ()):
        jid = _text(_value(record, "id"))
        if jid:
            previous = records.get(jid)
            if duplicates is not None and previous is not None and previous is not record:
                duplicates.append(selection_input(previous, snap))
            records[jid] = record
    for record in snap.get("finished", ()):
        jid = _text(_value(record, "id"))
        if jid:
            previous = records.get(jid)
            if duplicates is not None and previous is not None and previous is not record:
                duplicates.append(selection_input(previous, snap))
            records[jid] = record
    for jid, record in snap.get("departed_jobs", {}).items():
        if isinstance(jid, str):
            previous = records.get(jid)
            if duplicates is not None and previous is not None and previous is not record:
                duplicates.append(selection_input(previous, snap))
            records[jid] = record
    for record in snap.get("jobs", ()):
        jid = _text(_value(record, "id"))
        if jid:
            previous = records.get(jid)
            if duplicates is not None and previous is not None and previous is not record:
                duplicates.append(selection_input(previous, snap))
            records[jid] = record
    return records


def _evidence(record, snap, previous=None, inherit=()):
    jid = _text(_value(record, "id"))
    details = snap.get("details", _EMPTY_MAP).get(jid, _EMPTY_MAP)
    details = details if isinstance(details, dict) or isinstance(details, Mapping) else {}
    native = type(record) is Job or type(record) is Finished
    source = record.__dict__ if native else record if isinstance(record, Mapping) else None
    finished_omitted = (type(record) is Finished and previous is not None and previous.submit
                        and previous.submit == _text(record.submit))

    def field(name, scheduler):
        if scheduler in details:
            return _text(details[scheduler])
        raw = source.get(name, _MISSING) if source is not None else getattr(record, name, _MISSING)
        if raw is not _MISSING:
            value = _text(raw)
            if not value and previous and (name in inherit or finished_omitted and name == "command"):
                return getattr(previous, name, "")
            return value
        # Finished records omit owner, account, and command. Carry only fields
        # omitted by a record-type transition, not deleted metadata or blanks.
        return getattr(previous, name, "") if previous and (name in inherit or finished_omitted and name == "dependency") else ""

    structural = None
    if match := _ARRAY.fullmatch(jid):
        if match[2].startswith("["):
            from .arrays import parse_range
            try:
                parse_range(match[2])
            except ValueError:
                pass
            else:
                structural = ("array", match[1])
        else:
            structural = ("array", match[1])
    elif match := _HET.fullmatch(jid):
        structural = ("heterogeneous", match[1])
    if structural:
        return _Evidence(jid, "", "", field("cluster", "Cluster"), "", "", "", "", "", structural)

    user = field("user", "UserId")
    if re.fullmatch(r"[^()]+\([0-9]+\)", user):
        user = user.rsplit("(", 1)[0]
    explicit = ""
    for key in ("RavenLaunchId", "LaunchId", "LaunchGroup", "JobGroup"):
        candidate = _text(details.get(key)) or _text(source.get(key) if source is not None else _value(record, key))
        if candidate and _EXPLICIT.fullmatch(candidate):
            explicit = candidate
            break
    if not explicit:
        comment = field("comment", "Comment")
        match = _MARKER.fullmatch(comment)
        explicit = match[1] if match else ""
    if not explicit:
        tags = snap.get("tags", {}).get(jid, {})
        tags = tags.get("tags", ()) if isinstance(tags, Mapping) else ()
        tags = tags if isinstance(tags, (list, tuple, set, frozenset)) else ()
        markers = sorted({match[1] for tag in tags if isinstance(tag, str)
                          and (match := _MARKER.fullmatch(tag))})
        # Conflicting markers provide no reliable shared launch identity.
        explicit = markers[0] if len(markers) == 1 else ""
    return _Evidence(jid, _text(_value(record, "name")), field("submit", "SubmitTime"),
                     field("cluster", "Cluster"), user, field("account", "Account"),
                     field("workdir", "WorkDir"), field("command", "Command"),
                     explicit, structural, field("dependency", "Dependency"), field("comment", "Comment"))


def _input(record, jid, snap, *, details_map=None, tags_map=None):
    """Cheap exact field comparison before parsing or constructing evidence."""
    native = type(record) is Job or type(record) is Finished
    source = record.__dict__ if native else (record if isinstance(record, dict) or callable(getattr(record, "get", None)) else getattr(record, "__dict__", None))
    if source is None:
        fields = tuple(getattr(record, key, _MISSING) for key in _RECORD_FIELDS)
    else:
        get = source.get
        fields = (get("id", _MISSING), get("name", _MISSING), get("submit", _MISSING),
                  get("cluster", _MISSING), get("user", _MISSING), get("account", _MISSING),
                  get("workdir", _MISSING), get("command", _MISSING),
                  get("RavenLaunchId", _MISSING), get("LaunchId", _MISSING),
                  get("LaunchGroup", _MISSING), get("JobGroup", _MISSING),
                  get("comment", _MISSING), get("dependency", _MISSING), get("start", _MISSING))
    details_map = snap.get("details", _EMPTY_MAP) if details_map is None else details_map
    details = details_map.get(jid, _EMPTY_MAP)
    if not details or not (isinstance(details, dict) or isinstance(details, Mapping)):
        values = _EMPTY_VALUES
    else:
        get = details.get
        values = (get("WorkDir", _MISSING), get("UserId", _MISSING), get("Account", _MISSING),
                  get("Command", _MISSING), get("SubmitTime", _MISSING), get("Cluster", _MISSING),
                  get("RavenLaunchId", _MISSING), get("LaunchId", _MISSING),
                  get("LaunchGroup", _MISSING), get("JobGroup", _MISSING),
                  get("Comment", _MISSING), get("Dependency", _MISSING))
    tags_map = snap.get("tags", _EMPTY_MAP) if tags_map is None else tags_map
    tags = tags_map.get(jid, _EMPTY_MAP)
    tags = tags.get("tags", ()) if isinstance(tags, dict) or isinstance(tags, Mapping) else ()
    tags = tuple(tags) if isinstance(tags, (list, tuple, set, frozenset)) else ()
    return fields, values, tags, type(record)


class Registry:
    """One snapshot-sensitive inference cache and collapse registry per app."""

    def __init__(self, collapsed=()):
        self.collapsed = set(value for value in collapsed if valid_group_id(value))
        self.index = Index(MappingProxyType({}), MappingProxyType({}))
        self._automatic_index = self.index
        self._empty_index = self.index
        self.auto_enabled = True
        self._source_revision = 0
        self.selection_revision = 0
        self.selection_snapshot = None
        self._selection_inputs = None
        self._unknown_identity_ids = set()
        self._unknown_identity_refs = {}
        from .manual_job_groups import ManualState
        self.manual = ManualState()
        self._fingerprint = None
        self._evidence = {}
        self._inputs = {}
        self.inference_count = 0

    def ensure(self, snap):
        duplicates = []
        records = _records(snap, duplicates)
        automatic = self._ensure_automatic(snap, records)
        from .manual_job_groups import _RecordRef
        references = {}
        for jid in self._unknown_identity_ids:
            record = records.get(jid)
            old = self._unknown_identity_refs.get(jid)
            references[jid] = old if old is not None and old.record is record else _RecordRef(record)
        self._unknown_identity_refs = references
        identity_inputs = self._source_revision, tuple(duplicates), references
        if identity_inputs != self._selection_inputs:
            self._selection_inputs = identity_inputs
            self.selection_revision += 1
        self.selection_snapshot = snap
        self.index = self.manual.apply(automatic if self.auto_enabled else self._empty_index,
                                       records, snap, self._source_revision)
        return self.index

    def _ensure_automatic(self, snap, records):
        updates = {}
        details_map, tags_map = snap.get("details", _EMPTY_MAP), snap.get("tags", _EMPTY_MAP)
        for jid, record in records.items():
            value = _input(record, jid, snap, details_map=details_map, tags_map=tags_map)
            if self._inputs.get(jid, _MISSING) != value:
                updates[jid] = value
        # Every input is still compared, including in-place field corrections.
        # An unchanged maintenance frame does not allocate and populate two
        # full inference dictionaries only to throw them away afterwards.
        if not updates and len(records) == len(self._inputs) and self._fingerprint is not None:
            return self._automatic_index
        evidence, inputs = {}, {}
        for jid, record in records.items():
            if jid not in updates:
                inputs[jid], evidence[jid] = self._inputs[jid], self._evidence[jid]
            else:
                value = inputs[jid] = updates[jid]
                previous = self._evidence.get(jid)
                fields, values, tags, record_type = value
                old_input = self._inputs.get(jid)
                inherited = ()
                if old_input is not None and old_input[3] is not record_type:
                    same_launch = previous and previous.submit and previous.submit == _text(_value(record, "submit"))
                    inherited = tuple(name for name, old, new in zip(_RECORD_FIELDS, old_input[0], fields)
                                      if old is not _MISSING and (new is _MISSING or
                                          (record_type is Finished and old_input[3] is Job and same_launch
                                           and name in ("user", "account", "command", "cluster", "comment") and new == "")))
                # Ordinary accounting records have no owner/account/command
                # provenance. Reject that common case before parsing fields
                # or constructing evidence, while still noticing new details,
                # markers and retained live provenance on the next check.
                impossible = (previous is None and values is _EMPTY_VALUES and not tags
                              and jid.isascii() and jid.isdigit()
                              and not any(isinstance(item, str) and item for item in fields[8:14])
                              and not all(isinstance(fields[index], str) and fields[index]
                                          for index in (1, 2, 4, 5, 6)))
                evidence[jid] = None if impossible else _evidence(record, snap, previous, inherited)
        self._inputs = inputs
        from .manual_job_groups import _submit
        self._unknown_identity_ids.intersection_update(inputs)
        for jid, (fields, details, _, _) in updates.items():
            if _submit(_text(fields[2]) or _text(details[4])):
                self._unknown_identity_ids.discard(jid)
            else:
                self._unknown_identity_ids.add(jid)
        self._source_revision += 1
        if evidence == self._evidence and self._fingerprint is not None:
            return self._automatic_index
        previous = self._evidence
        self._evidence = evidence
        self._fingerprint = True
        self.inference_count += 1
        self._automatic_index = self._infer(evidence, previous)
        return self._automatic_index

    def _infer(self, evidence, previous):
        assigned, candidates, claimed = set(), [], set()
        structural, explicit = defaultdict(list), defaultdict(list)
        for item in evidence.values():
            if item is None:
                continue
            if item.structural:
                structural[(item.cluster, *item.structural)].append(item)
            elif item.explicit:
                explicit[(item.cluster, item.user, item.account, item.workdir, item.explicit)].append(item)

        for (cluster, kind, parent), items in structural.items():
            if len(items) < 2:
                continue
            suffix = ":" + _digest((cluster,)) if cluster else ""
            gid = f"{kind}:{parent}{suffix}"
            reason = "Slurm array task IDs share allocation " if kind == "array" else "Slurm heterogeneous component IDs share allocation "
            candidates.append(Group(gid, f"{'Array' if kind == 'array' else 'Heterogeneous'} {parent}",
                                    kind, reason + parent, "certain", tuple(sorted(item.id for item in items))))
            assigned.update(item.id for item in items)

        for cohort, items in explicit.items():
            if len(items) < 2:
                continue
            marker = cohort[-1]
            candidates.append(Group("launch:" + _digest(cohort), f"Launch {marker}", "explicit",
                                    f"Matching explicit launch marker {marker}", "explicit",
                                    tuple(sorted(item.id for item in items)), cohort))
            assigned.update(item.id for item in items)

        # Dependencies are explicit relations, but they are not launch IDs.
        # Require shared ownership/project and a bounded launch window so a
        # daily pipeline never swallows a previous day's prerequisite.
        ordinary = {item.id: item for item in evidence.values() if item is not None
                    and item.id not in assigned and not item.structural and not item.explicit
                    and len(item.id) <= 19 and item.id.isascii() and item.id.isdigit()
                    and all((item.user, item.account, item.workdir, item.name))}
        timed = {jid: stamp(item.submit) for jid, item in ordinary.items()}
        timed = {jid: when for jid, when in timed.items() if when is not None}
        claimed.update(group.id for group in candidates)
        self._dependency_groups(ordinary, timed, candidates, assigned, previous, claimed)

        bursts = defaultdict(list)
        for jid, when in timed.items():
            if jid in assigned:
                continue
            item = ordinary[jid]
            family = _FAMILY.fullmatch(item.name)
            name = family[1] if family and family[1] else item.name
            cohort = (item.cluster, item.user, item.account, item.workdir, name)
            bursts[cohort].append((when, int(jid), item))

        for cohort, items in bursts.items():
            items.sort(key=lambda value: (value[0], value[1]))
            run = []
            for entry in items:
                when, number, item = entry
                if run and (when - run[0][0] > SWEEP_SECONDS or not 0 < number - run[-1][1] <= SWEEP_ID_GAP):
                    self._sweep_group(cohort, run, candidates, previous, claimed)
                    run = []
                run.append(entry)
            self._sweep_group(cohort, run, candidates, previous, claimed)

        groups = {group.id: group for group in candidates}
        by_job = {jid: group.id for group in candidates for jid in group.members}
        return Index(MappingProxyType(groups), MappingProxyType(by_job))

    def _sweep_group(self, cohort, run, candidates, previous, claimed):
        scripts = {_script(entry[2].command) for entry in run if entry[2].command}
        dense = run and run[-1][1] - run[0][1] <= SWEEP_DENSITY * (len(run) - 1)
        if len(run) >= 3 and dense and len(scripts) <= 1 and "" not in scripts:
            reason = ("At least 3 jobs share owner, account, work directory and name family; "
                      "submit times span at most 30 seconds; IDs form a dense launch burst")
            if scripts:
                reason += "; observed script paths match"
            self._inferred_group(cohort, run, candidates, previous, "burst", SWEEP_SECONDS, reason, claimed)
            return
        # Two jobs need all the older, stronger evidence. A wider incomplete
        # run cannot hide its valid narrow subgroups.
        narrow = []
        for entry in run:
            when, number, item = entry
            if narrow and (when - narrow[0][0] > BURST_SECONDS or number - narrow[-1][1] != MAX_ID_GAP
                           or item.command != narrow[-1][2].command):
                self._burst_group(cohort, narrow, candidates, previous, claimed)
                narrow = []
            narrow.append(entry)
        self._burst_group(cohort, narrow, candidates, previous, claimed)

    def _burst_group(self, cohort, run, candidates, previous, claimed):
        if len(run) < 2 or not run[0][2].command or len({entry[2].name for entry in run}) > 1 and len(run) < 3:
            return
        self._inferred_group(cohort, run, candidates, previous, "burst", BURST_SECONDS,
                             "Submit times span at most 10 seconds; owner, account, work directory, command and name match; job IDs are adjacent", claimed)

    def _inferred_group(self, cohort, run, candidates, previous, kind, horizon, reason, claimed):
        members = tuple(entry[2].id for entry in run)
        start = run[0][0]
        old = {self._automatic_index.by_job[jid] for jid in members if jid in self._automatic_index.by_job}
        compatible = [self._automatic_index.groups[gid] for gid in old
                      if gid not in claimed and self._automatic_index.groups[gid].kind == kind
                      and self._automatic_index.groups[gid].cohort == cohort
                      and abs(start - self._automatic_index.groups[gid].first_submit) <= horizon
                      and any(previous.get(jid) is not None and previous[jid].submit == self._evidence[jid].submit
                              for jid in members if jid in self._automatic_index.groups[gid].members)]
        gid = min((group.id for group in compatible), default=kind + ":" + _digest((*cohort, members[0], start)))
        label = f"Launch {cohort[-1]}" if kind == "burst" else f"Pipeline {members[0]}"
        candidates.append(Group(gid, label, kind, reason, "likely", members, cohort, start))
        claimed.add(gid)

    def _dependency_groups(self, ordinary, timed, candidates, assigned, previous, claimed):
        from .deps import KINDS, parse_dependency
        dependencies = {jid: item.dependency for jid, item in ordinary.items() if jid in timed and item.dependency}
        if not dependencies:
            return
        parents = {jid: jid for jid in timed}
        bounds = {jid: [timed[jid], timed[jid], int(jid), int(jid)] for jid in timed}
        sizes = {jid: 1 for jid in timed}

        def root(jid):
            while parents[jid] != jid:
                parents[jid] = parents[parents[jid]]
                jid = parents[jid]
            return jid

        for jid in sorted(dependencies, key=lambda value: (timed[value], int(value))):
            item = ordinary[jid]
            scope = (item.cluster, item.user, item.account, item.workdir)
            for kind, prerequisite in parse_dependency(item.dependency):
                if kind not in KINDS or prerequisite not in timed or prerequisite == jid:
                    continue
                other = ordinary[prerequisite]
                if scope != (other.cluster, other.user, other.account, other.workdir):
                    continue
                left, right = root(jid), root(prerequisite)
                if left == right:
                    continue
                a, b = bounds[left], bounds[right]
                combined = [min(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), max(a[3], b[3])]
                if combined[1] - combined[0] > DEPENDENCY_SECONDS or combined[3] - combined[2] > DEPENDENCY_ID_SPAN:
                    continue
                if sizes[left] < sizes[right]:
                    left, right = right, left
                parents[right] = left
                sizes[left] += sizes[right]
                bounds[left] = combined
        components = defaultdict(list)
        for jid, when in timed.items():
            components[root(jid)].append((when, int(jid), ordinary[jid]))
        for run in components.values():
            if len(run) < 2:
                continue
            run.sort(key=lambda entry: (entry[0], entry[1]))
            item = run[0][2]
            cohort = (item.cluster, item.user, item.account, item.workdir)
            self._inferred_group(cohort, run, candidates, previous, "dependency", DEPENDENCY_SECONDS,
                                 "Direct dependency links share owner, account and work directory; submit span is at most 60 seconds and job ID span is at most 256", claimed)
            assigned.update(entry[2].id for entry in run)

    def is_collapsed(self, group):
        # Old releases persisted numeric array parents. Preserve that setting.
        parent = group.label.split()[-1]
        return group.id in self.collapsed or group.kind == "array" and group.id == "array:" + parent and parent in self.collapsed

    def fold(self, group_id, collapsed=None):
        if not valid_group_id(group_id):
            return False
        group = self.index.groups.get(group_id)
        if group is None:
            return False
        value = not self.is_collapsed(group) if collapsed is None else bool(collapsed)
        self.collapsed.discard(group.id)
        parent = group.label.split()[-1]
        if group.kind == "array":
            self.collapsed.discard(parent)
        stored = parent if group.kind == "array" and group.id == "array:" + parent else group.id
        if value:
            # Keep the legacy unscoped array parent setting compatible with
            # existing sort reanchoring and saved preferences.
            self.collapsed.add(stored)
        if len(self.collapsed) > MAX_COLLAPSED:
            self.collapsed = (set(sorted(self.collapsed - {stored})[-(MAX_COLLAPSED - 1):]) | {stored}
                              if value else set(sorted(self.collapsed)[-MAX_COLLAPSED:]))
        return True

    def project(self, records, *, enabled=True, index=None):
        records = list(records)
        index = self.index if index is None else index
        if not enabled:
            return [Child(record) for record in records]
        visible = defaultdict(list)
        counted = set()
        for record in records:
            jid = _value(record, "id")
            group_id = index.by_job.get(jid)
            if group_id and jid not in counted:
                visible[group_id].append(record)
                counted.add(jid)
        seen, result = set(), []
        for record in records:
            group_id = index.by_job.get(_value(record, "id"))
            if not group_id:
                result.append(Child(record))
                continue
            group = index.groups[group_id]
            if group_id not in seen:
                result.append(Header(group, tuple(visible[group_id]), self.is_collapsed(group)))
                seen.add(group_id)
            if not self.is_collapsed(group):
                result.append(Child(record, group_id))
        return result


def valid_group_id(value):
    return isinstance(value, str) and len(value) <= 128 and bool(re.fullmatch(r"[A-Za-z0-9:_.-]+", value))


def registry(app):
    value = getattr(app, "job_groups", None)
    state = getattr(app, "table_state", {})
    if not isinstance(value, Registry):
        value = app.job_groups = Registry(state.get("collapsed", ()))
    else:
        value.collapsed = {item for item in state.get("collapsed", ()) if valid_group_id(item)}
    value.manual.configure(state.get("manual_groups"))
    value.auto_enabled = state.get("auto_groups", True) is not False
    return value


def frame_index(app, snap):
    """Reuse an explicitly bounded render pass, never a snapshot-ID cache."""
    current = getattr(app, "job_groups_frame_index", None)
    if isinstance(current, tuple) and len(current) == 2 and current[0] is snap:
        return current[1]
    return registry(app).ensure(snap)


@contextmanager
def frame(app, snap):
    """Share one exact inference check during compose, then always clear it."""
    previous = getattr(app, "job_groups_frame_index", None)
    value = registry(app)
    index = value.ensure(snap) if getattr(app, "table_state", {}).get("groups", False) else value.index
    app.job_groups_frame_index = (snap, index)
    try:
        yield index
    finally:
        app.job_groups_frame_index = previous


def project(app, snap, records, *, enabled=None, index=None):
    value = registry(app)
    if enabled is None:
        enabled = bool(getattr(app, "table_state", {}).get("groups", False))
    index = frame_index(app, snap) if enabled and index is None else index
    return value.project(records, enabled=enabled, index=index)


def fold(app, group_id, collapsed=None):
    value = registry(app)
    changed = value.fold(group_id, collapsed)
    if changed:
        # An explicit disclosure click takes ownership of this fold. Undo
        # Auto must not later erase a newer user decision about that group.
        undo = getattr(app, "grouping_auto_undo", None)
        if isinstance(undo, dict):
            group = value.index.groups[group_id]
            undo.get("added", set()).discard(group_id)
            if group.kind == "array":
                undo.get("added", set()).discard(group.label.split()[-1])
        app.table_state["collapsed"] = sorted(value.collapsed)
        if value.is_collapsed(value.index.groups[group_id]):
            _reanchor(app, group_id)
    return changed


def project_records(app, snap, records, tab="history", *, enabled=None, index=None):
    """Keep actual representative rows, with separate presentation metadata.

    An open group leaves the caller's complete sort and pin order unchanged.
    A closed group retains its first visible record, so selection, log lookup,
    and cancellation always refer to one real allocation.  Filtered-out records
    are not reintroduced, and totals distinguish visible from observed jobs.
    """
    records = list(records)
    value = registry(app)
    if enabled is None:
        enabled = bool(getattr(app, "table_state", {}).get("groups", False))
    metadata = getattr(app, "job_group_metadata", None)
    if not isinstance(metadata, dict):
        metadata = app.job_group_metadata = {}
    current = metadata[tab] = {}
    if not enabled:
        return records
    index = frame_index(app, snap) if index is None else index
    visible = defaultdict(list)
    counted = set()
    for record in records:
        jid = _value(record, "id")
        gid = index.by_job.get(jid)
        if gid and jid not in counted:
            visible[gid].append(record)
            counted.add(jid)
    first = {gid: _value(members[0], "id") for gid, members in visible.items()}
    counts = {gid: tuple(members) for gid, members in visible.items()}
    # Reuse one immutable summary per closed group for INFO, badges and
    # tooltips in this projection. Rebuild next frame so in-place state/reason
    # updates cannot inherit stale launch-cache data.
    statistics = {gid: StatusSummary(MappingProxyType(status_counts(members)),
                                    any("_[" in str(_value(record, "id")) for record in members))
                  for gid, members in counts.items() if value.is_collapsed(index.groups[gid])}
    result = []
    for record in records:
        jid = _value(record, "id")
        gid = index.by_job.get(jid)
        if gid is None:
            result.append(record)
            continue
        group = index.groups[gid]
        header = jid == first[gid]
        collapsed = value.is_collapsed(group)
        current[jid] = GroupMeta(group, first[gid], header, collapsed, counts[gid], statistics.get(gid))
        if header or not collapsed:
            result.append(record)
    return result


def metadata_for_record(app, tab, job_id):
    return getattr(app, "job_group_metadata", {}).get(tab, {}).get(job_id)


def project_rows(app, rows, snap=None, tab="jobs", *, index=None):
    rows = list(rows)
    if snap is None:
        # The legacy entry point receives prepared queue rows. Supplying the
        # complete snapshot enables lifecycle and explicit evidence grouping.
        snap = {"jobs": [row["job"] for row in rows]}
    selected = project_records(app, snap, [row["job"] for row in rows], tab, index=index)
    visible = {_value(record, "id") for record in selected}
    result = []
    for row in rows:
        jid = _value(row["job"], "id")
        if jid not in visible:
            continue
        meta = metadata_for_record(app, tab, jid)
        if meta is None:
            result.append(row)
            continue
        item = dict(row, _group=meta)
        if meta.header:
            group_summary = f"{meta.group.label}: {meta.visible_count}/{meta.total_count} observed records"
            if meta.collapsed:
                item["name"] = f"{meta.group.label} / {meta.visible_count} records"
                item["st"] = "GRP"
                item["info"] = summary(meta.stats or meta.records, compact=True)
                for key in ("progress", "time", "left", "cpus", "gpu", "where", "cpu%", "eff", "mem%", "gpu%", "flags", "part"):
                    item[key] = ""
                item["_progress_animation"] = None
            else:
                item["info"] = str(item.get("info", "")) + " | " + group_summary + "; Left collapses"
        result.append(item)
    return result


def _reanchor(app, group_id):
    selected = getattr(app, "selected_id", None)
    tab = getattr(app, "tab", "")
    contexts = ("jobs", "recent") if tab == "jobs" else (tab, "history:" + tab, "history_dock:" + tab, "history_dock")
    meta = next((metadata_for_record(app, context, selected) for context in contexts
                 if metadata_for_record(app, context, selected) is not None), None)
    if meta is None or meta.group.id != group_id or meta.representative_id == selected:
        return
    app.selected_id = meta.representative_id
    cursor = getattr(app, "cursor", {})
    ids = {"jobs": getattr(app, "last_jobs_ids", ()), "history": getattr(app, "last_history_ids", ()),
           "group": getattr(app, "group_ids", ()), "deps": getattr(app, "dep_ids", ())}.get(tab, ()) or ()
    if tab in cursor and meta.representative_id in ids:
        cursor[tab] = ids.index(meta.representative_id)
