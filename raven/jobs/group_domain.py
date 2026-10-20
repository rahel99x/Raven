"""Snapshot-only job-group inference and contiguous record projections."""

from __future__ import annotations
import re
from collections import defaultdict
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import replace
from types import MappingProxyType
from raven.model import Finished, Job, stamp
from raven.jobs.group_records import (
    BURST_SECONDS as BURST_SECONDS,
    MAX_ID_GAP as MAX_ID_GAP,
    SWEEP_SECONDS as SWEEP_SECONDS,
    SWEEP_ID_GAP as SWEEP_ID_GAP,
    SWEEP_DENSITY as SWEEP_DENSITY,
    DEPENDENCY_SECONDS as DEPENDENCY_SECONDS,
    DEPENDENCY_ID_SPAN as DEPENDENCY_ID_SPAN,
    MAX_COLLAPSED as MAX_COLLAPSED,
    MAX_GROUP_PREFIX as MAX_GROUP_PREFIX,
    _ARRAY as _ARRAY,
    _HET as _HET,
    _MARKER as _MARKER,
    _EXPLICIT as _EXPLICIT,
    _FAMILY as _FAMILY,
    _NON_PRINTABLE_ASCII as _NON_PRINTABLE_ASCII,
    _EMPTY as _EMPTY,
    _MISSING as _MISSING,
    _EMPTY_VALUES as _EMPTY_VALUES,
    _EMPTY_MAP as _EMPTY_MAP,
    _RECORD_FIELDS as _RECORD_FIELDS,
    _value as _value,
    _text as _text,
    shared_name_prefix as shared_name_prefix,
    array_parent as array_parent,
    _digest as _digest,
    _script as _script,
    _STATUS as _STATUS,
    _ACCOUNTING_STATUS as _ACCOUNTING_STATUS,
    _waiting_dependency as _waiting_dependency,
    StatusSummary as StatusSummary,
    status_counts as status_counts,
    summary_segments as summary_segments,
    summary as summary,
    Group as Group,
    Index as Index,
    Header as Header,
    Child as Child,
    GroupMeta as GroupMeta,
    _Evidence as _Evidence,
    valid_group_id as valid_group_id,
    contiguous_order as contiguous_order,
)

from raven.ui.list_preferences import (
    after_fold,
    metadata_for_record as metadata_for_record,
    mode,
    batch_mode,
    CONTEXTS,
    fold_registry,
    is_collapsed,
)


def _records(snap, duplicates=None):
    """Queue records win over departed/accounting records of the same ID."""
    records = {}
    from raven.jobs.group_membership import selection_input

    for record in snap.get("group", ()):
        jid = _text(_value(record, "id"))
        if jid:
            previous = records.get(jid)
            if (
                duplicates is not None
                and previous is not None
                and previous is not record
            ):
                duplicates.append(selection_input(previous, snap))
            records[jid] = record
    for record in snap.get("finished", ()):
        jid = _text(_value(record, "id"))
        if jid:
            previous = records.get(jid)
            if (
                duplicates is not None
                and previous is not None
                and previous is not record
            ):
                duplicates.append(selection_input(previous, snap))
            records[jid] = record
    for jid, record in snap.get("departed_jobs", {}).items():
        if isinstance(jid, str):
            previous = records.get(jid)
            if (
                duplicates is not None
                and previous is not None
                and previous is not record
            ):
                duplicates.append(selection_input(previous, snap))
            records[jid] = record
    for record in snap.get("jobs", ()):
        jid = _text(_value(record, "id"))
        if jid:
            previous = records.get(jid)
            if (
                duplicates is not None
                and previous is not None
                and previous is not record
            ):
                duplicates.append(selection_input(previous, snap))
            records[jid] = record
    return records


def _evidence(record, snap, previous=None, inherit=()):
    jid = _text(_value(record, "id"))
    details = snap.get("details", _EMPTY_MAP).get(jid, _EMPTY_MAP)
    details = (
        details if isinstance(details, dict) or isinstance(details, Mapping) else {}
    )
    native = type(record) is Job or type(record) is Finished
    source = (
        record.__dict__ if native else record if isinstance(record, Mapping) else None
    )
    finished_omitted = (
        type(record) is Finished
        and previous is not None
        and previous.submit
        and previous.submit == _text(record.submit)
    )

    def field(name, scheduler):
        if scheduler in details:
            return _text(details[scheduler])
        raw = (
            source.get(name, _MISSING)
            if source is not None
            else getattr(record, name, _MISSING)
        )
        if raw is not _MISSING:
            value = _text(raw)
            if (
                not value
                and previous
                and (name in inherit or finished_omitted and name == "command")
            ):
                return getattr(previous, name, "")
            return value
        # Carry only proven omissions from the same submission; deleting
        # authoritative metadata must not recover stale field values.
        return getattr(previous, name, "") if previous and name in inherit else ""

    structural = None
    if match := _ARRAY.fullmatch(jid):
        if match[2].startswith("["):
            from raven.arrays import parse_range

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
        return _Evidence(
            jid,
            _text(_value(record, "name")),
            "",
            field("cluster", "Cluster"),
            "",
            "",
            "",
            "",
            "",
            structural,
        )

    user = field("user", "UserId")
    if re.fullmatch(r"[^()]+\([0-9]+\)", user):
        user = user.rsplit("(", 1)[0]
    explicit = ""
    conflicting_marker = False
    for key in ("RavenLaunchId", "LaunchId", "LaunchGroup", "JobGroup"):
        candidate = _text(details.get(key)) or _text(
            source.get(key) if source is not None else _value(record, key)
        )
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
        markers = sorted(
            {
                match[1]
                for tag in tags
                if isinstance(tag, str) and (match := _MARKER.fullmatch(tag))
            }
        )
        # Conflicting markers provide no reliable shared launch identity.
        explicit = markers[0] if len(markers) == 1 else ""
        conflicting_marker = len(markers) > 1
    result = _Evidence(
        jid,
        _text(_value(record, "name")),
        field("submit", "SubmitTime"),
        field("cluster", "Cluster"),
        user,
        field("account", "Account"),
        field("workdir", "WorkDir"),
        field("command", "Command"),
        explicit,
        structural,
        field("dependency", "Dependency"),
        field("comment", "Comment"),
        conflicting_marker,
    )
    if (
        type(record) in (Job, Finished)
        and previous is not None
        and previous.dependency
        and not result.dependency
        and not _text(_value(record, "dependency"))
        and previous.submit
        and result.submit == previous.submit == _text(record.submit)
        and (result.cluster, result.user, result.account, result.workdir)
        == (previous.cluster, previous.user, previous.account, previous.workdir)
    ):
        # Slurm removes satisfied dependencies while the same submission is
        # still queued or running; accounting also omits those relations. The
        # observed relation remains launch evidence, not a claim that the job
        # is currently waiting. Corrections and changed ownership/project
        # identity must not inherit that edge.
        result = replace(result, dependency=previous.dependency)
    return result


def _input(record, jid, snap, *, details_map=None, tags_map=None):
    """Cheap exact field comparison before parsing or constructing evidence."""
    native = type(record) is Job or type(record) is Finished
    source = (
        record.__dict__
        if native
        else (
            record
            if isinstance(record, dict) or callable(getattr(record, "get", None))
            else getattr(record, "__dict__", None)
        )
    )
    if source is None:
        fields = tuple(getattr(record, key, _MISSING) for key in _RECORD_FIELDS)
    else:
        get = source.get
        fields = (
            get("id", _MISSING),
            get("name", _MISSING),
            get("submit", _MISSING),
            get("cluster", _MISSING),
            get("user", _MISSING),
            get("account", _MISSING),
            get("workdir", _MISSING),
            get("command", _MISSING),
            get("RavenLaunchId", _MISSING),
            get("LaunchId", _MISSING),
            get("LaunchGroup", _MISSING),
            get("JobGroup", _MISSING),
            get("comment", _MISSING),
            get("dependency", _MISSING),
            get("start", _MISSING),
        )
    details_map = (
        snap.get("details", _EMPTY_MAP) if details_map is None else details_map
    )
    details = details_map.get(jid, _EMPTY_MAP)
    if not details or not (isinstance(details, dict) or isinstance(details, Mapping)):
        values = _EMPTY_VALUES
    else:
        get = details.get
        values = (
            get("WorkDir", _MISSING),
            get("UserId", _MISSING),
            get("Account", _MISSING),
            get("Command", _MISSING),
            get("SubmitTime", _MISSING),
            get("Cluster", _MISSING),
            get("RavenLaunchId", _MISSING),
            get("LaunchId", _MISSING),
            get("LaunchGroup", _MISSING),
            get("JobGroup", _MISSING),
            get("Comment", _MISSING),
            get("Dependency", _MISSING),
        )
    tags_map = snap.get("tags", _EMPTY_MAP) if tags_map is None else tags_map
    tags = tags_map.get(jid, _EMPTY_MAP)
    tags = (
        tags.get("tags", ())
        if isinstance(tags, dict) or isinstance(tags, Mapping)
        else ()
    )
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
        from raven.jobs.group_membership import ManualState

        self.manual = ManualState()
        self._fingerprint = None
        self._evidence = {}
        self._inputs = {}
        self.inference_count = 0
        self.heavy_publication = {}
        self._heavy_exclusion = None

    def ensure(self, snap):
        duplicates = []
        records = _records(snap, duplicates)
        automatic = self._ensure_automatic(snap, records)
        from raven.jobs.group_membership import _RecordRef

        references = {}
        for jid in self._unknown_identity_ids:
            record = records.get(jid)
            old = self._unknown_identity_refs.get(jid)
            references[jid] = (
                old if old is not None and old.record is record else _RecordRef(record)
            )
        self._unknown_identity_refs = references
        from raven.jobs.batch_sort import heavy_job_id

        heavy = heavy_job_id(self.heavy_publication, snap)
        identity_inputs = self._source_revision, tuple(duplicates), references, heavy
        if identity_inputs != self._selection_inputs:
            self._selection_inputs = identity_inputs
            self.selection_revision += 1
        self.selection_snapshot = snap
        self.index = self.manual.apply(
            automatic if self.auto_enabled else self._empty_index,
            records,
            snap,
            self._source_revision,
        )
        if heavy in self.index.by_job:
            # The current dashboard allocation is always a separate row. Apply
            # this to the shared registry so menus and row actions see exactly
            # the same membership as the queue, including explicit overrides.
            previous = self._heavy_exclusion
            if (
                previous is not None
                and previous[0] is self.index
                and previous[1] == heavy
            ):
                self.index = previous[2]
            else:
                original = self.index
                group = original.for_job(heavy)
                members = tuple(jid for jid in group.members if jid != heavy)
                groups, by_job = dict(original.groups), dict(original.by_job)
                by_job.pop(heavy, None)
                if len(members) > 1:
                    groups[group.id] = replace(group, members=members)
                else:
                    groups.pop(group.id)
                    for jid in members:
                        by_job.pop(jid, None)
                self.index = Index(MappingProxyType(groups), MappingProxyType(by_job))
                self._heavy_exclusion = original, heavy, self.index
        return self.index

    def _ensure_automatic(self, snap, records):
        updates = {}
        details_map, tags_map = (
            snap.get("details", _EMPTY_MAP),
            snap.get("tags", _EMPTY_MAP),
        )
        for jid, record in records.items():
            value = _input(
                record, jid, snap, details_map=details_map, tags_map=tags_map
            )
            if self._inputs.get(jid, _MISSING) != value:
                updates[jid] = value
        # Every input is still compared, including in-place field corrections.
        # An unchanged maintenance frame does not allocate and populate two
        # full inference dictionaries only to throw them away afterwards.
        if (
            not updates
            and len(records) == len(self._inputs)
            and self._fingerprint is not None
        ):
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
                    same_launch = (
                        previous
                        and previous.submit
                        and previous.submit == _text(_value(record, "submit"))
                    )
                    inherited = tuple(
                        name
                        for name, old, new in zip(_RECORD_FIELDS, old_input[0], fields)
                        if same_launch
                        and name != "dependency"
                        and old is not _MISSING
                        and (
                            new is _MISSING
                            or (
                                record_type is Finished
                                and old_input[3] is Job
                                and name
                                in ("user", "account", "command", "cluster", "comment")
                                and new == ""
                            )
                        )
                    )
                elif (
                    old_input is not None
                    and old_input[3] is Finished
                    and record_type is Finished
                    and previous is not None
                    and previous.submit
                    and previous.submit == _text(_value(record, "submit"))
                ):
                    # Legacy accounting omits these fields on every poll, not
                    # just the first Job -> Finished transition. Keep already
                    # inherited omissions when another field is amended, but
                    # never hide a nonempty field being cleared or the removal
                    # of authoritative scheduler details. WorkDir is required
                    # evidence and deliberately is not inherited here.
                    detail_fields = {
                        "user": 1,
                        "account": 2,
                        "command": 3,
                        "cluster": 5,
                        "comment": 10,
                    }
                    inherited = tuple(
                        name
                        for name, old, new in zip(_RECORD_FIELDS, old_input[0], fields)
                        if name in detail_fields
                        and old == new == ""
                        and old_input[1][detail_fields[name]] is _MISSING
                    )
                # Ordinary accounting records have no owner/account/command
                # provenance. Reject that common case before parsing fields
                # or constructing evidence, while still noticing new details,
                # markers and retained live provenance on the next check.
                impossible = (
                    previous is None
                    and values is _EMPTY_VALUES
                    and not tags
                    and jid.isascii()
                    and jid.isdigit()
                    and not any(isinstance(item, str) and item for item in fields[8:14])
                    and not all(
                        isinstance(fields[index], str) and fields[index]
                        for index in (1, 2, 4, 5, 6)
                    )
                )
                evidence[jid] = (
                    None if impossible else _evidence(record, snap, previous, inherited)
                )
        self._inputs = inputs
        from raven.jobs.group_membership import _submit

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
                explicit[
                    (item.cluster, item.user, item.account, item.workdir, item.explicit)
                ].append(item)

        for (cluster, kind, parent), items in structural.items():
            if len(items) < 2:
                continue
            suffix = ":" + _digest((cluster,)) if cluster else ""
            gid = f"{kind}:{parent}{suffix}"
            reason = (
                "Slurm array task IDs share allocation "
                if kind == "array"
                else "Slurm heterogeneous component IDs share allocation "
            )
            identity_label = (
                f"{'Array' if kind == 'array' else 'Heterogeneous'} {parent}"
            )
            prefix = shared_name_prefix(item.name for item in items)
            label = prefix + " / " + identity_label if prefix else identity_label
            candidates.append(
                Group(
                    gid,
                    label,
                    kind,
                    reason + parent,
                    "certain",
                    tuple(sorted(item.id for item in items)),
                )
            )
            assigned.update(item.id for item in items)

        for cohort, items in explicit.items():
            if len(items) < 2:
                continue
            marker = cohort[-1]
            label = (
                shared_name_prefix(item.name for item in items) or f"Launch {marker}"
            )
            candidates.append(
                Group(
                    "launch:" + _digest(cohort),
                    label,
                    "explicit",
                    f"Matching explicit launch marker {marker}",
                    "explicit",
                    tuple(sorted(item.id for item in items)),
                    cohort,
                )
            )
            assigned.update(item.id for item in items)

        # Dependencies are explicit relations, but they are not launch IDs.
        # Require shared ownership/project and a bounded launch window so a
        # daily pipeline never swallows a previous day's prerequisite.
        ordinary = {
            item.id: item
            for item in evidence.values()
            if item is not None
            and item.id not in assigned
            and not item.structural
            and not item.explicit
            and not item.conflicting_marker
            and len(item.id) <= 19
            and item.id.isascii()
            and item.id.isdigit()
            and all((item.user, item.account, item.workdir, item.name))
        }
        timed = {jid: stamp(item.submit) for jid, item in ordinary.items()}
        timed = {jid: when for jid, when in timed.items() if when is not None}
        claimed.update(group.id for group in candidates)
        self._dependency_groups(
            ordinary, timed, candidates, assigned, previous, claimed
        )

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
                if run and (
                    when - run[0][0] > SWEEP_SECONDS
                    or not 0 < number - run[-1][1] <= SWEEP_ID_GAP
                ):
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
            reason = (
                "At least 3 jobs share owner, account, work directory and name family; "
                "submit times span at most 30 seconds; IDs form a dense launch burst"
            )
            if scripts:
                reason += "; observed script paths match"
            self._inferred_group(
                cohort,
                run,
                candidates,
                previous,
                "burst",
                SWEEP_SECONDS,
                reason,
                claimed,
            )
            return
        # Two jobs need all the older, stronger evidence. A wider incomplete
        # run cannot hide its valid narrow subgroups.
        narrow = []
        for entry in run:
            when, number, item = entry
            if narrow and (
                when - narrow[0][0] > BURST_SECONDS
                or number - narrow[-1][1] != MAX_ID_GAP
                or item.command != narrow[-1][2].command
            ):
                self._burst_group(cohort, narrow, candidates, previous, claimed)
                narrow = []
            narrow.append(entry)
        self._burst_group(cohort, narrow, candidates, previous, claimed)

    def _burst_group(self, cohort, run, candidates, previous, claimed):
        if (
            len(run) < 2
            or not run[0][2].command
            or len({entry[2].name for entry in run}) > 1
            and len(run) < 3
        ):
            return
        self._inferred_group(
            cohort,
            run,
            candidates,
            previous,
            "burst",
            BURST_SECONDS,
            "Submit times span at most 10 seconds; owner, account, work directory, command and name match; job IDs are adjacent",
            claimed,
        )

    def _inferred_group(
        self, cohort, run, candidates, previous, kind, horizon, reason, claimed
    ):
        members = tuple(entry[2].id for entry in run)
        start = run[0][0]
        old = {
            self._automatic_index.by_job[jid]
            for jid in members
            if jid in self._automatic_index.by_job
        }
        compatible = [
            self._automatic_index.groups[gid]
            for gid in old
            if gid not in claimed
            and self._automatic_index.groups[gid].kind == kind
            and self._automatic_index.groups[gid].cohort == cohort
            and abs(start - self._automatic_index.groups[gid].first_submit) <= horizon
            and any(
                previous.get(jid) is not None
                and previous[jid].submit == self._evidence[jid].submit
                for jid in members
                if jid in self._automatic_index.groups[gid].members
            )
        ]
        gid = min(
            (group.id for group in compatible),
            default=kind + ":" + _digest((*cohort, members[0], start)),
        )
        label = shared_name_prefix(entry[2].name for entry in run)
        if not label:
            label = (
                f"Launch {cohort[-1]}" if kind == "burst" else f"Pipeline {members[0]}"
            )
        candidates.append(
            Group(gid, label, kind, reason, "likely", members, cohort, start)
        )
        claimed.add(gid)

    def _dependency_groups(
        self, ordinary, timed, candidates, assigned, previous, claimed
    ):
        from raven.deps import KINDS, parse_dependency

        dependencies = {
            jid: item.dependency
            for jid, item in ordinary.items()
            if jid in timed and item.dependency
        }
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
                if (
                    kind not in KINDS
                    or prerequisite not in timed
                    or prerequisite == jid
                ):
                    continue
                other = ordinary[prerequisite]
                if scope != (other.cluster, other.user, other.account, other.workdir):
                    continue
                left, right = root(jid), root(prerequisite)
                if left == right:
                    continue
                a, b = bounds[left], bounds[right]
                combined = [
                    min(a[0], b[0]),
                    max(a[1], b[1]),
                    min(a[2], b[2]),
                    max(a[3], b[3]),
                ]
                if (
                    combined[1] - combined[0] > DEPENDENCY_SECONDS
                    or combined[3] - combined[2] > DEPENDENCY_ID_SPAN
                ):
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
            self._inferred_group(
                cohort,
                run,
                candidates,
                previous,
                "dependency",
                DEPENDENCY_SECONDS,
                "Direct dependency links share owner, account and work directory; submit span is at most 60 seconds and job ID span is at most 256",
                claimed,
            )
            assigned.update(entry[2].id for entry in run)

    def is_collapsed(self, group):
        return is_collapsed(self, group)

    def fold(self, group_id, collapsed=None):
        return fold_registry(self, group_id, collapsed, maximum=MAX_COLLAPSED)

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
                result.append(
                    Header(group, tuple(visible[group_id]), self.is_collapsed(group))
                )
                seen.add(group_id)
            if not self.is_collapsed(group):
                result.append(Child(record, group_id))
        return result


def registry(app):
    value = getattr(app, "job_groups", None)
    state = getattr(app, "table_state", {})
    if not isinstance(value, Registry):
        value = app.job_groups = Registry(state.get("collapsed", ()))
    else:
        value.collapsed = {
            item for item in state.get("collapsed", ()) if valid_group_id(item)
        }
    value.manual.configure(state.get("manual_groups"))
    value.auto_enabled = state.get("auto_groups", True) is not False
    from raven.jobs.batch_sort import heavy_publication

    value.heavy_publication = heavy_publication(app)
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
    tab = getattr(app, "tab", "jobs")
    contexts = (
        ("jobs", "recent")
        if tab == "jobs"
        else (tab, "history:" + tab, "analytics:advisor" if tab == "analytics" else tab)
    )
    index = (
        value.ensure(snap)
        if any(mode(app, context) == "grouped" for context in contexts)
        else value.index
    )
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
        after_fold(app, value, group_id)
    return changed


def project_records(app, snap, records, tab="history", *, enabled=None, index=None):
    """Keep actual representative rows, with separate presentation metadata.

    List contexts pack complete groups in their completion/column sort order,
    or apply the explicitly chosen batch rank. Dependency trees retain their
    edge ordering. No record outside the caller's admitted list is fetched.
    A closed group retains its first visible record, so selection, log lookup,
    and cancellation always refer to one real allocation.  Filtered-out records
    are not reintroduced, and totals distinguish visible from observed jobs.
    """
    records = list(records)
    value = registry(app)
    if enabled is None:
        enabled = mode(app, tab) == "grouped"
    if tab == "recent":
        enabled = enabled and mode(app, tab) == "grouped"
    metadata = getattr(app, "job_group_metadata", None)
    if not isinstance(metadata, dict):
        metadata = app.job_group_metadata = {}
    current = metadata[tab] = {}
    index = frame_index(app, snap) if enabled and index is None else index
    if tab == "jobs" and (
        enabled
        or getattr(app, "table_state", {}).get("job_list_modes", {}).get(tab)
        != "sequential"
    ):
        from raven.jobs.batch_sort import order

        records = order(app, snap, records, index if enabled else None)
    elif tab in CONTEXTS and enabled:
        if batch_mode(app, tab):
            from raven.jobs.batch_sort import order

            observed = snap.get("group", records) if tab == "group" else records
            records = order(
                app,
                snap,
                records,
                index,
                observed=observed,
                sort_mode=batch_mode(app, tab),
            )
        else:
            from raven.jobs.group_records import contiguous_order as order

            records = order(records, index)
    if tab == "jobs" and not enabled:
        from raven.jobs.batch_sort import heavy_job_id, heavy_publication

        heavy = heavy_job_id(heavy_publication(app), snap)
        if heavy is not None:
            records = [
                record for record in records if _value(record, "id") == heavy
            ] + [record for record in records if _value(record, "id") != heavy]
    if not enabled:
        return records
    visible = defaultdict(list)
    counted = set()
    for record in records:
        jid = _value(record, "id")
        gid = index.by_job.get(jid)
        if gid and jid not in counted:
            visible[gid].append(record)
            counted.add(jid)
    first = {gid: _value(members[0], "id") for gid, members in visible.items()}
    retain_selected = tab in CONTEXTS and tab != "jobs"
    if retain_selected:
        # Preserve the actual selected child when switching back from an
        # individual-row list or receiving a newer completion. Keep the fold
        # and original block rank; a representative is always a real record.
        field = {
            "history:analytics": "analytics_job",
            "history:log": "log_job",
            "history:research": "research_job_id",
            "analytics:advisor": "analytics_job",
        }.get(tab, "selected_id")
        selected = getattr(app, field, None) or getattr(app, "selected_id", None)
        gid = index.by_job.get(selected)
        if gid in visible and value.is_collapsed(index.groups[gid]):
            chosen = next(
                (record for record in visible[gid] if _value(record, "id") == selected),
                None,
            )
            if chosen is not None:
                first[gid] = selected
    counts = {gid: tuple(members) for gid, members in visible.items()}
    # Reuse one immutable summary per closed group for INFO, badges and
    # tooltips in this projection. Rebuild next frame so in-place state/reason
    # updates cannot inherit stale launch-cache data.
    awaiting = (
        set(snap.get("departed_jobs", {}))
        - {_value(record, "id") for record in snap.get("jobs", ())}
        if tab == "recent"
        else set()
    )
    statistics = {
        gid: StatusSummary(
            MappingProxyType(status_counts(members, awaiting_ids=awaiting)),
            any("_[" in str(_value(record, "id")) for record in members),
        )
        for gid, members in counts.items()
        if tab == "recent" or value.is_collapsed(index.groups[gid])
    }
    result = []
    emitted = set()
    for record in records:
        jid = _value(record, "id")
        gid = index.by_job.get(jid)
        if gid is None:
            result.append(record)
            continue
        group = index.groups[gid]
        header = jid == first[gid]
        collapsed = value.is_collapsed(group)
        current[jid] = GroupMeta(
            group, first[gid], header, collapsed, counts[gid], statistics.get(gid)
        )
        if collapsed and retain_selected:
            if gid not in emitted:
                emitted.add(gid)
                representative = next(
                    member
                    for member in visible[gid]
                    if _value(member, "id") == first[gid]
                )
                result.append(representative)
        elif header or not collapsed:
            result.append(record)
    return result


def project_rows(app, rows, snap=None, tab="jobs", *, index=None):
    rows = list(rows)
    if snap is None:
        # The legacy entry point receives prepared queue rows. Supplying the
        # complete snapshot enables lifecycle and explicit evidence grouping.
        snap = {"jobs": [row["job"] for row in rows]}
    selected = project_records(
        app, snap, [row["job"] for row in rows], tab, index=index
    )
    # Render in projected order. Iterating the original rows would reintroduce
    # the interleaving which the complete-batch projection just removed.
    rows_by_id = {_value(row["job"], "id"): row for row in rows}
    result = []
    for record in selected:
        row = rows_by_id[_value(record, "id")]
        jid = _value(row["job"], "id")
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
                for key in (
                    "progress",
                    "time",
                    "left",
                    "cpus",
                    "gpu",
                    "where",
                    "cpu%",
                    "eff",
                    "mem%",
                    "gpu%",
                    "flags",
                    "part",
                ):
                    item[key] = ""
                item["_progress_animation"] = None
            else:
                item["info"] = (
                    str(item.get("info", ""))
                    + " | "
                    + group_summary
                    + "; Left collapses"
                )
        result.append(item)
    return result


from raven.ui.list_preferences import reanchor as _reanchor

__all__ = [
    "annotations",
    "BURST_SECONDS",
    "MAX_ID_GAP",
    "SWEEP_SECONDS",
    "SWEEP_ID_GAP",
    "SWEEP_DENSITY",
    "DEPENDENCY_SECONDS",
    "DEPENDENCY_ID_SPAN",
    "MAX_COLLAPSED",
    "MAX_GROUP_PREFIX",
    "shared_name_prefix",
    "array_parent",
    "StatusSummary",
    "status_counts",
    "summary_segments",
    "summary",
    "Group",
    "Index",
    "Header",
    "Child",
    "GroupMeta",
    "valid_group_id",
    "contiguous_order",
    "Registry",
    "registry",
    "frame_index",
    "frame",
    "project",
    "fold",
    "project_records",
    "project_rows",
    "metadata_for_record",
    "_reanchor",
]
