"""Explicit group-edit actions over canonical bounded membership state."""

from __future__ import annotations
from raven.jobs.group_membership import (
    MAX_GROUPS as MAX_GROUPS,
    MAX_IDENTITIES as MAX_IDENTITIES,
    MAX_LABEL as MAX_LABEL,
    _GROUP_ID as _GROUP_ID,
    _JOB_ID as _JOB_ID,
    _SUBMIT as _SUBMIT,
    _UNSET as _UNSET,
    _NATIVE as _NATIVE,
    _RAW_FIELDS as _RAW_FIELDS,
    _ordered_ids as _ordered_ids,
    _ordered_members as _ordered_members,
    _label as _label,
    _RecordRef as _RecordRef,
    _submit as _submit,
    _valid_submit as _valid_submit,
    _identity as _identity,
    _selection_token as _selection_token,
    selection_input as selection_input,
    SelectionTokenCache as SelectionTokenCache,
    selection_tokens as selection_tokens,
    validate_state as validate_state,
    Result as Result,
    TargetEvidence as TargetEvidence,
    ManualState as ManualState,
)


def _prepare(app, snap):
    from raven.jobs.group_domain import _records, registry

    value = registry(app)
    value.ensure(snap)
    # Failed capacity checks must not accumulate references to every record
    # ever selected. Retain provenance only for actual stored overrides.
    retained = {
        item[0] for members in value.manual._groups.values() for item in members
    }
    retained.update(item[0] for item in value.manual._detached)
    value.manual._identities = {
        jid: entry for jid, entry in value.manual._identities.items() if jid in retained
    }
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
            persistent_groups.append(
                {"id": gid, "label": label, "members": [list(item) for item in members]}
            )
        else:
            session_groups[gid] = tuple(members)
            session_labels[gid] = label
    persistent_detached = [
        list(item) for item in sorted(detached) if _identity(item) is not None
    ]
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
    from raven.jobs.group_records import _digest, _value, shared_name_prefix

    if (
        not isinstance(job_ids, (tuple, list, set, frozenset))
        or len(job_ids) > MAX_IDENTITIES
    ):
        return Result(False, f"Select between 2 and {MAX_IDENTITIES} jobs to group")
    ids = _ordered_ids({item for item in job_ids if isinstance(item, str)})
    if len(ids) < 2:
        return Result(False, "Select at least two jobs to group")
    registry, state, records = _prepare(app, snap)
    if any(token is None for token in selection_tokens(snap, ids).values()):
        return Result(
            False,
            "Some selected job IDs refer to different attempts or unavailable records; refresh the list and select again",
        )
    keys = tuple(state.identity(jid, records, snap) for jid in ids)
    if any(key is None for key in keys):
        return Result(
            False,
            "Some selected jobs are no longer available; select the current rows again",
        )
    groups, detached = state.groups(), state.detached()
    chosen = set(keys)
    gid = "manual:" + _digest(part for key in keys for part in key)
    for existing, members in groups.items():
        if set(members) == chosen and not chosen & detached:
            return Result(
                False, "These jobs already share a manual group", existing, ids
            )
    updated = {
        old: tuple(item for item in members if item not in chosen)
        for old, members in groups.items()
    }
    updated = {old: members for old, members in updated.items() if members}
    updated[gid] = keys
    detached -= chosen
    if len(updated) > MAX_GROUPS:
        return Result(
            False,
            "Saved grouping limit reached; use :jobgroup reset to restore automatic grouping",
        )
    if sum(map(len, updated.values())) + len(detached) > MAX_IDENTITIES:
        return Result(
            False,
            "Saved grouping identity limit reached; use :jobgroup reset to restore automatic grouping",
        )
    label = state._labels.get(gid)
    if label is None:
        label = (
            shared_name_prefix(_value(records[jid], "name") for jid in ids)
            or "Manual group " + ids[0]
        )
    _save(app, registry, updated, detached, labels={gid: label})
    registry.ensure(snap)
    session = any(_identity(key) is None for key in keys)
    return Result(
        True,
        f"Grouped {len(ids)} jobs"
        + (" for this session (submission time unavailable)" if session else ""),
        gid,
        ids,
    )


def _target_evidence(registry, state, group_id, tokens):
    group = registry.index.groups.get(group_id)
    if group is None or not 2 <= len(group.members) <= MAX_IDENTITIES:
        return None
    if any(tokens.get(jid) is None for jid in group.members):
        return None
    identities = state._groups.get(group_id, ())
    if len(identities) > MAX_IDENTITIES or not _label(group.label):
        return None
    return TargetEvidence(
        group_id,
        group.kind,
        group.label,
        group.members,
        identities,
        tuple((jid, tokens[jid]) for jid in group.members),
    )


def target_evidence(app, snap, group_id):
    """Capture a bounded group target once; do not call this on pointer motion."""
    if not isinstance(group_id, str):
        return None
    return target_evidences(app, snap, (group_id,)).get(group_id)


def target_evidences(
    app, snap, group_ids, *, index=None, tokens=None, all_groups=False
):
    """Publish exact destination evidence in one bounded, cached render step.

    A checked render-frame index avoids inference; unchanged source revisions
    reuse the evidence itself, including hidden collapsed members. Supplied
    tokens must cover all members, not just the rendered representatives.
    Explicit menu opening may set all_groups=True to enumerate all known
    destinations once; per-destination persistence limits still apply.
    """
    from raven.jobs.group_domain import registry as get_registry

    if not isinstance(group_ids, (list, tuple, set, frozenset)):
        return {}
    registry = get_registry(app)
    if index is None:
        index = registry.ensure(snap)
    elif index is not registry.index or registry.selection_snapshot is not snap:
        return {}
    state = registry.manual
    ids = tuple(
        sorted(
            {gid for gid in group_ids if isinstance(gid, str) and gid in index.groups}
        )
    )
    if not all_groups:
        ids = ids[:MAX_GROUPS]
    cache_key = registry.selection_revision, state.revision, id(index), ids, all_groups
    if (
        tokens is None
        and state._target_cache is not None
        and state._target_cache[0] == cache_key
    ):
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
    from raven.jobs.group_records import _digest

    if (
        not isinstance(group_id, str)
        or not isinstance(job_ids, (tuple, list, set, frozenset))
        or not 1 <= len(job_ids) <= MAX_IDENTITIES
        or any(
            not isinstance(jid, str) or not _JOB_ID.fullmatch(jid) for jid in job_ids
        )
    ):
        return Result(False, "Select current jobs and an existing group to add them to")
    ids = _ordered_ids(set(job_ids))
    registry, state, records = _prepare(app, snap)
    group = registry.index.groups.get(group_id)
    if group is None or len(group.members) > MAX_IDENTITIES:
        return Result(
            False,
            "The destination group changed or exceeds the grouping limit; select its current row again",
        )
    tokens = selection_tokens(snap, set(ids) | set(group.members))
    if any(token is None for token in tokens.values()):
        return Result(
            False,
            "Some selected or destination job IDs refer to different attempts or unavailable records; refresh the list and select again",
        )
    current = _target_evidence(registry, state, group_id, tokens)
    if current is None or expected is not None and current != expected:
        return Result(
            False, "The destination group changed; select its current row again"
        )
    keys = tuple(state.identity(jid, records, snap) for jid in ids)
    if any(key is None for key in keys):
        return Result(
            False,
            "Some selected jobs are no longer available; select the current rows again",
        )
    destination_keys = (
        current.identities
        if group_id in state._groups
        else tuple(state.identity(jid, records, snap) for jid in group.members)
    )
    if any(key is None for key in destination_keys):
        return Result(
            False, "The destination group changed; select its current row again"
        )
    chosen, destination = set(keys), set(destination_keys)
    if chosen <= destination:
        return Result(
            False, "These jobs already belong to " + group.label, group_id, ids
        )
    combined = _ordered_members(destination | chosen)
    groups, detached = state.groups(), state.detached()
    gid = (
        group_id
        if group_id in groups
        else "manual:"
        + _digest(part for key in _ordered_members(destination) for part in key)
    )
    updated = {
        old: tuple(item for item in members if item not in chosen)
        for old, members in groups.items()
        if old != gid
    }
    updated = {old: members for old, members in updated.items() if members}
    updated[gid] = combined
    detached -= destination | chosen
    if len(updated) > MAX_GROUPS:
        return Result(
            False,
            "Saved grouping limit reached; use :jobgroup reset to restore automatic grouping",
        )
    if sum(map(len, updated.values())) + len(detached) > MAX_IDENTITIES:
        return Result(
            False,
            "Saved grouping identity limit reached; use :jobgroup reset to restore automatic grouping",
        )
    was_collapsed = registry.is_collapsed(group)
    _save(app, registry, updated, detached, labels={gid: group.label})
    registry.ensure(snap)
    if gid != group_id and was_collapsed:
        registry.fold(gid, True)
        app.table_state["collapsed"] = sorted(registry.collapsed)
    count = len(chosen - destination)
    session = any(_identity(key) is None for key in combined)
    return Result(
        True,
        f"Added {count} job{'s' if count != 1 else ''} to {group.label}"
        + (" for this session (submission time unavailable)" if session else ""),
        gid,
        ids,
    )


def detach(app, snap, job_ids=(), group_ids=()):
    """Detach selected members or whole explicitly targeted collapsed groups."""
    if (
        not isinstance(job_ids, (tuple, list, set, frozenset))
        or len(job_ids) > MAX_IDENTITIES
        or not isinstance(group_ids, (tuple, list, set, frozenset))
        or len(group_ids) > MAX_GROUPS
    ):
        return Result(False, "Invalid job grouping selection")
    registry, state, records = _prepare(app, snap)
    groups, detached = state.groups(), state.detached()
    target_ids = {jid for jid in job_ids if isinstance(jid, str)}
    for gid in group_ids:
        group = registry.index.groups.get(gid) if isinstance(gid, str) else None
        if group is not None:
            target_ids.update(group.members)
    if any(token is None for token in selection_tokens(snap, target_ids).values()):
        return Result(
            False,
            "Some selected job IDs refer to different attempts or unavailable records; refresh the list and select again",
        )
    chosen, ids = set(), set()
    for jid in job_ids:
        if not isinstance(jid, str):
            continue
        key = state.identity(jid, records, snap)
        if key is None:
            return Result(
                False,
                "Some selected jobs are no longer available; select the current rows again",
            )
        if registry.index.for_job(jid) is not None or any(
            key in members for members in groups.values()
        ):
            chosen.add(key)
            ids.add(jid)
    for gid in group_ids:
        if not isinstance(gid, str):
            continue
        group = registry.index.groups.get(gid)
        if group is None:
            return Result(
                False, "The selected group changed; select its current row again"
            )
        if gid in groups:
            keys = groups[gid]
        else:
            keys = tuple(state.identity(jid, records, snap) for jid in group.members)
        if any(key is None for key in keys):
            return Result(
                False, "The selected group changed; select its current row again"
            )
        chosen.update(keys)
        ids.update(key[0] for key in keys)
    if not chosen:
        return Result(False, "Select grouped jobs or a collapsed group to ungroup")
    updated = {
        gid: tuple(item for item in members if item not in chosen)
        for gid, members in groups.items()
    }
    updated = {gid: members for gid, members in updated.items() if members}
    detached |= chosen
    if sum(map(len, updated.values())) + len(detached) > MAX_IDENTITIES:
        return Result(
            False,
            "Saved grouping identity limit reached; use :jobgroup reset to restore automatic grouping",
        )
    _save(app, registry, updated, detached)
    registry.ensure(snap)
    return Result(
        True,
        f"Ungrouped {len(ids)} jobs; automatic grouping will leave them separate",
        job_ids=tuple(sorted(ids)),
    )


def reset(app):
    """Explicitly discard only presentation overrides and resume inference."""
    from raven.jobs.group_domain import registry

    value = registry(app)
    state = value.manual
    if not state.groups() and not state.detached():
        return Result(False, "No manual grouping overrides to reset")
    _save(app, value, {}, set())
    app.table_state["collapsed"] = [
        gid
        for gid in app.table_state.get("collapsed", ())
        if not isinstance(gid, str) or not gid.startswith("manual:")
    ]
    value.collapsed = {gid for gid in value.collapsed if not gid.startswith("manual:")}
    state._identities.clear()
    state._index = None
    state._key = None
    return Result(
        True,
        "Manual groups and exclusions cleared"
        + ("; automatic grouping restored" if app.table_state.get("groups") else ""),
    )
