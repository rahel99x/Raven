"""Bounded, attempt-safe presentation overrides for automatic job groups.

Only explicit commands change this state. The render path overlays immutable
indices and caches the result until inference, identity, or preferences change.
An unknown submission time is deliberately session-only and tied to the exact
record object; a replacement ambiguous record cannot inherit an old decision.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from functools import lru_cache
from itertools import chain
from operator import attrgetter
import re
from types import MappingProxyType

from .model import Job, Finished

MAX_GROUPS = 256
MAX_IDENTITIES = 8192
MAX_LABEL = 4128  # Inferred names can contain a 4096-character source plus prefix.
_GROUP_ID = re.compile(r"manual:[0-9a-f]{20}\Z")
_JOB_ID = re.compile(r"[A-Za-z0-9_+\[\]:%,.-]{1,128}\Z")
_SUBMIT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\Z")
_UNSET = object()
_NATIVE = (Job, Finished)
_RAW_FIELDS = attrgetter("id", "submit", "cluster", "name", "start", "user", "account")


def _ordered_ids(values):
    from .table_sort import _natural
    return tuple(sorted(values, key=lambda value: (_natural(value), value)))


def _ordered_members(values):
    from .table_sort import _natural
    return tuple(sorted(values, key=lambda value: (_natural(value[0]), value)))


def _label(value):
    return isinstance(value, str) and 0 < len(value) <= MAX_LABEL and value.isprintable() and bool(value.strip())


class _RecordRef:
    """Retain ambiguous records so Python cannot recycle their identity."""
    __slots__ = ("record",)

    def __init__(self, record):
        self.record = record

    def __eq__(self, other):
        return isinstance(other, _RecordRef) and self.record is other.record

    def __hash__(self):
        return id(self.record)


def _submit(value):
    return isinstance(value, str) and len(value) == 19 and _valid_submit(value)


@lru_cache(maxsize=8192)
def _valid_submit(value):
    if not _SUBMIT.fullmatch(value):
        return False
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def _identity(value):
    if not isinstance(value, (tuple, list)) or len(value) != 3:
        return None
    jid, submit, cluster = value
    if (not isinstance(jid, str) or not _JOB_ID.fullmatch(jid) or not _submit(submit)
            or not isinstance(cluster, str) or len(cluster) > 128
            or cluster and not cluster.isprintable()):
        return None
    return jid, submit, cluster


def _selection_token(record, snap):
    from .job_groups import _value, _text
    jid = _value(record, "id")
    if not isinstance(jid, str) or not _JOB_ID.fullmatch(jid):
        return None
    details = snap.get("details", {}).get(jid, {})
    details = details if isinstance(details, Mapping) else {}
    submit = _text(_value(record, "submit")) or _text(details.get("SubmitTime"))
    cluster = _text(_value(record, "cluster")) or _text(details.get("Cluster"))
    if len(cluster) > 128 or cluster and not cluster.isprintable():
        return None
    if _submit(submit):
        return jid, submit, cluster, None
    signature = tuple(_text(_value(record, name)) for name in
                      ("name", "cluster", "submit", "start", "user", "account"))
    return jid, "", cluster, id(record), signature, _RecordRef(record)


def selection_input(record, snap):
    """Exact cheap cache evidence, including ambiguous record replacement."""
    if type(record) in _NATIVE and _submit(record.submit):
        details_map = snap.get("details", {})
        details = details_map.get(record.id) if details_map else None
        if not isinstance(details, Mapping):
            return record.id, record.submit, record.cluster
        return record.id, record.submit, record.cluster, details.get("Cluster", "")
    from .job_groups import _value, _text
    source = record if isinstance(record, dict) else getattr(record, "__dict__", None)
    get = source.get if isinstance(source, dict) else lambda name, default="": _value(record, name, default)
    jid = get("id", "")
    details = snap.get("details", {}).get(jid, {}) if isinstance(jid, str) else {}
    details = details if isinstance(details, Mapping) else {}
    # Keep raw fields. Canonicalization only runs when these exact values
    # change, so malformed/missing provenance cannot masquerade as unchanged.
    submit, detailed_submit = get("submit", ""), details.get("SubmitTime", "")
    attempt = None if _submit(_text(submit) or _text(detailed_submit)) else _RecordRef(record)
    return (jid, submit, get("cluster", ""), get("name", ""),
            get("start", ""), get("user", ""), get("account", ""),
            detailed_submit, details.get("Cluster", ""), attempt)


class SelectionTokenCache:
    """Reuse frame provenance through the already checked inference registry."""
    def __init__(self):
        self._key = None
        self._result = {}
        self._source_refs = ()
        self._fallback = False
        self._had_details = False
        self.build_count = 0

    def update(self, snap, ids=None, *, registry=None):
        chosen = None if ids is None else frozenset(ids)
        if registry is not None and registry.selection_snapshot is snap:
            provenance = (registry, registry.selection_revision)
            refs = ()
            self._fallback = False
        else:
            sources = (snap.get("group", ()), snap.get("finished", ()),
                       snap.get("departed_jobs", {}).values(), snap.get("jobs", ()))
            refs = tuple(chain.from_iterable(sources))
            details = snap.get("details", {})
            # Reuse stored scalar evidence without allocating a second full
            # 50k-record tuple tree (and triggering periodic GC sweeps).
            if (self._fallback and self._key is not None and chosen == self._key[1]
                    and bool(details) == self._had_details and len(refs) == len(self._source_refs)):
                unchanged = True
                for record, previous, evidence in zip(refs, self._source_refs, self._key[0]):
                    if record is not previous:
                        unchanged = False
                        break
                    if type(record) in _NATIVE:
                        if _RAW_FIELDS(record) != evidence[1]:
                            unchanged = False
                            break
                        if details:
                            detail = details.get(record.id)
                            extra = (detail.get("SubmitTime", ""), detail.get("Cluster", "")) if isinstance(detail, Mapping) else ()
                            if extra != evidence[2]:
                                unchanged = False
                                break
                    elif selection_input(record, snap) != evidence:
                        unchanged = False
                        break
                if unchanged:
                    return self._result
            self._fallback = True
            self._had_details = bool(details)
            # The common disabled-grouping path needs exact correction checks,
            # not normalized identities twice per frame. Native scalar fields
            # are read in C; retained records make raw identity values safe.
            if not details:
                provenance = tuple((id(record), _RAW_FIELDS(record)) if type(record) in _NATIVE
                                   else selection_input(record, snap) for record in refs)
            else:
                raw = []
                for record in refs:
                    if type(record) in _NATIVE:
                        detail = details.get(record.id)
                        evidence = (detail.get("SubmitTime", ""), detail.get("Cluster", "")) if isinstance(detail, Mapping) else ()
                        raw.append((id(record), _RAW_FIELDS(record), evidence))
                    else:
                        raw.append(selection_input(record, snap))
                provenance = tuple(raw)
        key = provenance, chosen
        if key != self._key:
            self._result = selection_tokens(snap, chosen)
            self._key = key
            self.build_count += 1
        self._source_refs = refs
        return self._result


def selection_tokens(snap, ids=None):
    """Freeze selection provenance once per frame, without querying Slurm.

    Ambiguous reused IDs return None rather than selecting whichever source
    happened to win a dictionary merge. Missing cluster provenance alone is
    tolerated; contradictory explicit clusters or submissions are not.
    """
    from .job_groups import _value
    wanted = None if ids is None else {jid for jid in ids if isinstance(jid, str)}
    result = {} if wanted is None else dict.fromkeys(wanted)
    seen, conflicted = {}, set()
    sources = (snap.get("group", ()), snap.get("finished", ()),
               snap.get("departed_jobs", {}).values(), snap.get("jobs", ()))
    for source in sources:
        for record in source:
            jid = _value(record, "id")
            if not isinstance(jid, str) or wanted is not None and jid not in wanted or jid in conflicted:
                continue
            token = _selection_token(record, snap)
            old = seen.get(jid)
            if token is None or old is not None and (
                    old[1] != token[1] or old[2] and token[2] and old[2] != token[2]
                    or not token[1] and old != token):
                conflicted.add(jid)
                result[jid] = None
            else:
                # An accounting row with omitted cluster provenance must not
                # erase a previously observed cluster before another source.
                seen[jid] = old if old is not None and old[2] and not token[2] else token
                result[jid] = token
    return result


def validate_state(value):
    """Return a bounded JSON-ready copy, rejecting corrupt/ambiguous entries.

    First valid membership wins when an edited preference file duplicates an
    identity. Detached identities win over groups. No unbounded input scan is
    needed, even for nested attacker-sized preference lists.
    """
    result = {"version": 1, "groups": [], "detached": []}
    if not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] != 1:
        return result
    detached = value.get("detached", ())
    used = set()
    if isinstance(detached, (list, tuple)):
        for candidate in detached[:MAX_IDENTITIES]:
            key = _identity(candidate)
            if key is not None and key not in used:
                used.add(key)
                result["detached"].append(list(key))
    groups = value.get("groups", ())
    seen = set()
    budget = MAX_IDENTITIES - len(used)
    if isinstance(groups, (list, tuple)):
        for group in groups[:MAX_GROUPS]:
            if not isinstance(group, dict):
                continue
            gid, members = group.get("id"), group.get("members")
            if (not isinstance(gid, str) or not _GROUP_ID.fullmatch(gid) or gid in seen
                    or not isinstance(members, (list, tuple))):
                continue
            cleaned = []
            for candidate in members[:budget]:
                key = _identity(candidate)
                if key is not None and key not in used:
                    used.add(key)
                    cleaned.append(list(key))
            budget -= len(cleaned)
            if cleaned:
                seen.add(gid)
                entry = {"id": gid, "members": cleaned}
                if _label(group.get("label")):
                    entry["label"] = group["label"]
                result["groups"].append(entry)
            if budget <= 0:
                break
    return result


@dataclass(frozen=True)
class Result:
    changed: bool
    message: str
    group_id: str | None = None
    job_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class TargetEvidence:
    """Exact destination provenance captured when a menu or drag starts.

    Status-only updates do not invalidate a destination. A changed member,
    attempt, label, or override does, preventing late input from using a group
    which merely inherited the same screen position or inferred ID.
    """
    group_id: str
    kind: str
    label: str
    members: tuple[str, ...]
    identities: tuple[tuple[str, str, str], ...]
    tokens: tuple[tuple[str, tuple], ...]


class ManualState:
    def __init__(self):
        self._reference = _UNSET
        self.persistent = validate_state(None)
        self.session_groups = {}
        self.session_labels = {}
        self.session_detached = set()
        self.revision = 0
        self.overlay_count = 0
        self._key = None
        self._index = None
        self._identities = {}
        self._next_session = 0
        self._groups = {}
        self._labels = {}
        self._detached = set()
        self._session_ids = ()
        self._target_cache = None

    def configure(self, value):
        # Command writes replace the complete validated object. No preference
        # serialization or scan takes place on ordinary mouse reports.
        if value is self._reference:
            return
        self._reference = value
        self.persistent = validate_state(value)
        self._groups = {**{group["id"]: tuple(tuple(item) for item in group["members"])
                           for group in self.persistent["groups"]}, **self.session_groups}
        self._labels = {**{group["id"]: group.get("label", "Manual group " + group["members"][0][0])
                           for group in self.persistent["groups"]}, **self.session_labels}
        self._detached = {tuple(item) for item in self.persistent["detached"]} | self.session_detached
        self._session_ids = tuple(sorted({item[0] for members in self.session_groups.values() for item in members
                                         if item[1].startswith("session:")} |
                                        {item[0] for item in self.session_detached if item[1].startswith("session:")}))
        self.revision += 1

    def groups(self):
        return dict(self._groups)

    def detached(self):
        return set(self._detached)

    def identity(self, jid, records, snap):
        from .job_groups import _value, _text
        record = records.get(jid)
        if record is None or not isinstance(jid, str) or not _JOB_ID.fullmatch(jid):
            return None
        details = snap.get("details", {}).get(jid, {})
        details = details if isinstance(details, Mapping) else {}
        submit = _text(_value(record, "submit")) or _text(details.get("SubmitTime"))
        cluster = _text(_value(record, "cluster")) or _text(details.get("Cluster"))
        if len(cluster) > 128 or cluster and not cluster.isprintable():
            return None
        previous = self._identities.get(jid)
        if _submit(submit):
            # Accounting can omit cluster provenance already observed for the
            # exact submission. A conflicting nonempty cluster never matches.
            if not cluster and previous and previous[1][1] == submit:
                cluster = previous[1][2]
            key = jid, submit, cluster
            self._identities[jid] = (record, key, None)
            return key
        signature = tuple(_text(_value(record, name)) for name in
                          ("name", "cluster", "submit", "start", "user", "account"))
        # The same ambiguous record can temporarily omit proven cluster
        # details, but contradictory supplemental provenance is a new identity.
        if (previous and previous[0] is record and previous[2] == signature
                and (not cluster or cluster == previous[1][2])):
            return previous[1]
        self._next_session += 1
        key = jid, "session:" + str(self._next_session), cluster
        self._identities[jid] = (record, key, signature)
        return key

    def apply(self, automatic, records, snap, source_revision):
        groups, detached = self._groups, self._detached
        if not groups and not detached:
            return automatic
        key = (id(automatic), source_revision, self.revision,
               tuple((jid, id(records.get(jid))) for jid in self._session_ids))
        if key == self._key:
            return self._index
        from .job_groups import Group, Index
        needed = {item[0] for members in groups.values() for item in members} | {item[0] for item in detached}
        current = {jid: self.identity(jid, records, snap) for jid in needed}
        self._identities = {jid: entry for jid, entry in self._identities.items() if jid in records and jid in needed}
        excluded = {jid for jid, identity in current.items() if identity in detached}
        manual = {}
        for gid, members in groups.items():
            present = tuple(item[0] for item in members if current.get(item[0]) == item)
            excluded.update(present)
            if len(present) >= 2:
                manual[gid] = Group(gid, self._labels.get(gid, "Manual group " + members[0][0]), "manual",
                                    "Jobs explicitly grouped in Raven; scheduler allocations are unchanged",
                                    "manual", present)
        merged = {}
        for gid, group in automatic.groups.items():
            members = tuple(jid for jid in group.members if jid not in excluded)
            if members == group.members or len(members) >= 2:
                merged[gid] = group if members == group.members else replace(group, members=members)
        merged.update(manual)
        self._index = Index(MappingProxyType(merged), MappingProxyType(
            {jid: gid for gid, group in merged.items() for jid in group.members}))
        self._key = key
        self.overlay_count += 1
        return self._index


def _prepare(app, snap):
    from .job_groups import _records, registry
    value = registry(app)
    value.ensure(snap)
    # Failed capacity checks must not accumulate references to every record
    # ever selected. Retain provenance only for actual stored overrides.
    retained = {item[0] for members in value.manual._groups.values() for item in members}
    retained.update(item[0] for item in value.manual._detached)
    value.manual._identities = {jid: entry for jid, entry in value.manual._identities.items() if jid in retained}
    return value, value.manual, _records(snap)


def _save(app, registry, groups, detached, *, labels=None):
    state = registry.manual
    persistent_groups, session_groups = [], {}
    labels = {**state._labels, **(labels or {})}
    session_labels = {}
    for gid, members in groups.items():
        members = _ordered_members(members)
        label = labels.get(gid, "Manual group " + members[0][0])
        if all(_identity(item) is not None for item in members):
            persistent_groups.append({"id": gid, "label": label, "members": [list(item) for item in members]})
        else:
            session_groups[gid] = tuple(members)
            session_labels[gid] = label
    persistent_detached = [list(item) for item in sorted(detached) if _identity(item) is not None]
    data = {"version": 1, "groups": persistent_groups, "detached": persistent_detached}
    app.table_state["manual_groups"] = data
    state.session_groups = session_groups
    state.session_labels = session_labels
    state.session_detached = {item for item in detached if _identity(item) is None}
    state.configure(data)
    app.manual_job_groups_revision = getattr(app, "manual_job_groups_revision", 0) + 1
    # A compose-scoped inference cache must never expose pre-command rows.
    app.job_groups_frame_index = None


def create(app, snap, job_ids):
    """Group exact selected records, leaving unrelated members untouched."""
    from .job_groups import _digest, _value, shared_name_prefix
    if not isinstance(job_ids, (tuple, list, set, frozenset)) or len(job_ids) > MAX_IDENTITIES:
        return Result(False, f"Select between 2 and {MAX_IDENTITIES} jobs to group")
    ids = _ordered_ids({item for item in job_ids if isinstance(item, str)})
    if len(ids) < 2:
        return Result(False, "Select at least two jobs to group")
    registry, state, records = _prepare(app, snap)
    if any(token is None for token in selection_tokens(snap, ids).values()):
        return Result(False, "Some selected job IDs refer to different attempts or unavailable records; refresh the list and select again")
    keys = tuple(state.identity(jid, records, snap) for jid in ids)
    if any(key is None for key in keys):
        return Result(False, "Some selected jobs are no longer available; select the current rows again")
    groups, detached = state.groups(), state.detached()
    chosen = set(keys)
    gid = "manual:" + _digest(part for key in keys for part in key)
    for existing, members in groups.items():
        if set(members) == chosen and not chosen & detached:
            return Result(False, "These jobs already share a manual group", existing, ids)
    updated = {old: tuple(item for item in members if item not in chosen)
               for old, members in groups.items()}
    updated = {old: members for old, members in updated.items() if members}
    updated[gid] = keys
    detached -= chosen
    if len(updated) > MAX_GROUPS:
        return Result(False, "Saved grouping limit reached; use :jobgroup reset to restore automatic grouping")
    if sum(map(len, updated.values())) + len(detached) > MAX_IDENTITIES:
        return Result(False, "Saved grouping identity limit reached; use :jobgroup reset to restore automatic grouping")
    label = state._labels.get(gid)
    if label is None:
        label = shared_name_prefix(_value(records[jid], "name") for jid in ids) or "Manual group " + ids[0]
    _save(app, registry, updated, detached, labels={gid: label})
    registry.ensure(snap)
    session = any(_identity(key) is None for key in keys)
    return Result(True, f"Grouped {len(ids)} jobs" + (" for this session (submission time unavailable)" if session else ""), gid, ids)


def _target_evidence(registry, state, group_id, tokens):
    group = registry.index.groups.get(group_id)
    if group is None or not 2 <= len(group.members) <= MAX_IDENTITIES:
        return None
    if any(tokens.get(jid) is None for jid in group.members):
        return None
    identities = state._groups.get(group_id, ())
    if len(identities) > MAX_IDENTITIES or not _label(group.label):
        return None
    return TargetEvidence(group_id, group.kind, group.label, group.members,
                          identities, tuple((jid, tokens[jid]) for jid in group.members))


def target_evidence(app, snap, group_id):
    """Capture a bounded group target once; do not call this on pointer motion."""
    if not isinstance(group_id, str):
        return None
    return target_evidences(app, snap, (group_id,)).get(group_id)


def target_evidences(app, snap, group_ids, *, index=None, tokens=None, all_groups=False):
    """Publish exact destination evidence in one bounded, cached render step.

    A checked render-frame index avoids inference; unchanged source revisions
    reuse the evidence itself, including hidden collapsed members. Supplied
    tokens must cover all members, not just the rendered representatives.
    Explicit menu opening may set all_groups=True to enumerate all known
    destinations once; per-destination persistence limits still apply.
    """
    from .job_groups import registry as get_registry
    if not isinstance(group_ids, (list, tuple, set, frozenset)):
        return {}
    registry = get_registry(app)
    if index is None:
        index = registry.ensure(snap)
    elif index is not registry.index or registry.selection_snapshot is not snap:
        return {}
    state = registry.manual
    ids = tuple(sorted({gid for gid in group_ids if isinstance(gid, str) and gid in index.groups}))
    if not all_groups:
        ids = ids[:MAX_GROUPS]
    cache_key = registry.selection_revision, state.revision, id(index), ids, all_groups
    if tokens is None and state._target_cache is not None and state._target_cache[0] == cache_key:
        return dict(state._target_cache[1])
    wanted, eligible = set(), []
    for gid in ids:
        group = index.groups.get(gid)
        if group is None or len(group.members) > MAX_IDENTITIES:
            continue
        if not all_groups and len(wanted) + len(group.members) > MAX_IDENTITIES:
            continue
        wanted.update(group.members)
        eligible.append(gid)
    if tokens is None:
        tokens = selection_tokens(snap, wanted) if wanted else {}
        cached = True
    else:
        cached = False
    result = {}
    for gid in eligible:
        evidence = _target_evidence(registry, state, gid, tokens)
        if evidence is not None:
            result[gid] = evidence
    if cached:
        state._target_cache = cache_key, result
    return dict(result)


def add(app, snap, group_id, job_ids, *, expected=None):
    """Atomically move exact jobs into an existing manual or automatic group.

    Automatic destinations become explicit overrides of their current exact
    membership. Saved absent members remain in manual destinations. Canonical
    membership is naturally ordered; callers retain their own table sort.
    """
    from .job_groups import _digest
    if (not isinstance(group_id, str) or not isinstance(job_ids, (tuple, list, set, frozenset))
            or not 1 <= len(job_ids) <= MAX_IDENTITIES
            or any(not isinstance(jid, str) or not _JOB_ID.fullmatch(jid) for jid in job_ids)):
        return Result(False, "Select current jobs and an existing group to add them to")
    ids = _ordered_ids(set(job_ids))
    registry, state, records = _prepare(app, snap)
    group = registry.index.groups.get(group_id)
    if group is None or len(group.members) > MAX_IDENTITIES:
        return Result(False, "The destination group changed or exceeds the grouping limit; select its current row again")
    tokens = selection_tokens(snap, set(ids) | set(group.members))
    if any(token is None for token in tokens.values()):
        return Result(False, "Some selected or destination job IDs refer to different attempts or unavailable records; refresh the list and select again")
    current = _target_evidence(registry, state, group_id, tokens)
    if current is None or expected is not None and current != expected:
        return Result(False, "The destination group changed; select its current row again")
    keys = tuple(state.identity(jid, records, snap) for jid in ids)
    if any(key is None for key in keys):
        return Result(False, "Some selected jobs are no longer available; select the current rows again")
    destination_keys = (current.identities if group_id in state._groups else
                        tuple(state.identity(jid, records, snap) for jid in group.members))
    if any(key is None for key in destination_keys):
        return Result(False, "The destination group changed; select its current row again")
    chosen, destination = set(keys), set(destination_keys)
    if chosen <= destination:
        return Result(False, "These jobs already belong to " + group.label, group_id, ids)
    combined = _ordered_members(destination | chosen)
    groups, detached = state.groups(), state.detached()
    gid = group_id if group_id in groups else "manual:" + _digest(part for key in _ordered_members(destination) for part in key)
    updated = {old: tuple(item for item in members if item not in chosen)
               for old, members in groups.items() if old != gid}
    updated = {old: members for old, members in updated.items() if members}
    updated[gid] = combined
    detached -= destination | chosen
    if len(updated) > MAX_GROUPS:
        return Result(False, "Saved grouping limit reached; use :jobgroup reset to restore automatic grouping")
    if sum(map(len, updated.values())) + len(detached) > MAX_IDENTITIES:
        return Result(False, "Saved grouping identity limit reached; use :jobgroup reset to restore automatic grouping")
    was_collapsed = registry.is_collapsed(group)
    _save(app, registry, updated, detached, labels={gid: group.label})
    registry.ensure(snap)
    if gid != group_id and was_collapsed:
        registry.fold(gid, True)
        app.table_state["collapsed"] = sorted(registry.collapsed)
    count = len(chosen - destination)
    session = any(_identity(key) is None for key in combined)
    return Result(True, f"Added {count} job{'s' if count != 1 else ''} to {group.label}"
                  + (" for this session (submission time unavailable)" if session else ""), gid, ids)


def detach(app, snap, job_ids=(), group_ids=()):
    """Detach selected members or whole explicitly targeted collapsed groups."""
    if (not isinstance(job_ids, (tuple, list, set, frozenset)) or len(job_ids) > MAX_IDENTITIES
            or not isinstance(group_ids, (tuple, list, set, frozenset)) or len(group_ids) > MAX_GROUPS):
        return Result(False, "Invalid job grouping selection")
    registry, state, records = _prepare(app, snap)
    groups, detached = state.groups(), state.detached()
    target_ids = {jid for jid in job_ids if isinstance(jid, str)}
    for gid in group_ids:
        group = registry.index.groups.get(gid) if isinstance(gid, str) else None
        if group is not None:
            target_ids.update(group.members)
    if any(token is None for token in selection_tokens(snap, target_ids).values()):
        return Result(False, "Some selected job IDs refer to different attempts or unavailable records; refresh the list and select again")
    chosen, ids = set(), set()
    for jid in job_ids:
        if not isinstance(jid, str):
            continue
        key = state.identity(jid, records, snap)
        if key is None:
            return Result(False, "Some selected jobs are no longer available; select the current rows again")
        if registry.index.for_job(jid) is not None or any(key in members for members in groups.values()):
            chosen.add(key)
            ids.add(jid)
    for gid in group_ids:
        if not isinstance(gid, str):
            continue
        group = registry.index.groups.get(gid)
        if group is None:
            return Result(False, "The selected group changed; select its current row again")
        if gid in groups:
            keys = groups[gid]
        else:
            keys = tuple(state.identity(jid, records, snap) for jid in group.members)
        if any(key is None for key in keys):
            return Result(False, "The selected group changed; select its current row again")
        chosen.update(keys)
        ids.update(key[0] for key in keys)
    if not chosen:
        return Result(False, "Select grouped jobs or a collapsed group to ungroup")
    updated = {gid: tuple(item for item in members if item not in chosen) for gid, members in groups.items()}
    updated = {gid: members for gid, members in updated.items() if members}
    detached |= chosen
    if sum(map(len, updated.values())) + len(detached) > MAX_IDENTITIES:
        return Result(False, "Saved grouping identity limit reached; use :jobgroup reset to restore automatic grouping")
    _save(app, registry, updated, detached)
    registry.ensure(snap)
    return Result(True, f"Ungrouped {len(ids)} jobs; automatic grouping will leave them separate",
                  job_ids=tuple(sorted(ids)))


def reset(app):
    """Explicitly discard only presentation overrides and resume inference."""
    from .job_groups import registry
    value = registry(app)
    state = value.manual
    if not state.groups() and not state.detached():
        return Result(False, "No manual grouping overrides to reset")
    _save(app, value, {}, set())
    app.table_state["collapsed"] = [gid for gid in app.table_state.get("collapsed", ())
                                    if not isinstance(gid, str) or not gid.startswith("manual:")]
    value.collapsed = {gid for gid in value.collapsed if not gid.startswith("manual:")}
    state._identities.clear()
    state._index = None
    state._key = None
    return Result(True, "Manual groups and exclusions cleared" +
                  ("; automatic grouping restored" if app.table_state.get("groups") else ""))
