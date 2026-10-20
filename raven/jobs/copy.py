"""Copy exact selected job records without borrowing another pane's text.

The clipboard uses the same compact tab-separated columns in every job list:
JOBID, NAME, STATE, PARTITION, ELAPSED, CPUS, GPUS, INFO.  Only record lines
are included. A collapsed group's selected row still identifies its real
representative allocation, just like log lookup and other exact-job actions.
"""

from __future__ import annotations

from contextlib import nullcontext

from raven import clipboard
from raven.jobs import selection as job_selection
from raven.jobs.manual_groups import selection_tokens

MAX_BYTES = 8 << 20
FIELDS = ("id", "name", "state", "partition", "elapsed", "cpus", "gpus")
_ESCAPES = {value: f"\\x{value:02x}" for value in (*range(32), 127)}
_ESCAPES.update({10: "\\n", 13: "\\r", 9: "\\t"})


def _value(record, name, default=""):
    return (
        record.get(name, default)
        if isinstance(record, dict)
        else getattr(record, name, default)
    )


def _snapshot(app, targets):
    """Read just the selected records; never copy metric histories on input."""
    frozen = getattr(app, "table_tools_state", {}).get("freeze")
    if isinstance(frozen, dict):
        source, lock = frozen, nullcontext()
    else:
        source = getattr(app, "store", None)
        lock = getattr(source, "lock", None) or nullcontext()
    wanted = set(targets)
    with lock:
        read = (
            source.get
            if isinstance(source, dict)
            else lambda name, default: getattr(source, name, default)
        )
        snap = {
            key: tuple(
                record for record in read(key, ()) if _value(record, "id") in wanted
            )
            for key in ("jobs", "finished", "group")
        }
        snap["departed_jobs"] = {
            jid: record
            for jid, record in read("departed_jobs", {}).items()
            if jid in wanted
        }
        details = read("details", {})
        snap["details"] = {
            jid: dict(details[jid])
            for jid in wanted
            if isinstance(details.get(jid), dict)
        }
        observed = selection_tokens(snap, targets)
        records = {}
        # Match the provenance reader's precedence. Validate all sources before
        # selecting a preferred row, so a reused ID cannot hide an old attempt.
        for source_records in (
            snap["group"],
            snap["finished"],
            snap["departed_jobs"].values(),
            snap["jobs"],
        ):
            for record in source_records:
                values = [_value(record, field) for field in FIELDS]
                info = (
                    _value(record, "reason")
                    or _value(record, "exit")
                    or _value(record, "nodelist")
                )
                records[_value(record, "id")] = (*values, info)
    return observed, records


def _cell(value):
    """Keep one record per line, including unusual scheduler-supplied names."""
    text = "" if value is None else str(value)
    return (
        text.translate(_ESCAPES)
        .encode("utf-8", errors="backslashreplace")
        .decode("utf-8")
    )


def copy_selection(app):
    """Return True if the focused native job list owns this copy, even on error."""
    source = job_selection.context(app)
    if source is None:
        return False
    ids = source["ids"]
    marks = getattr(app, "marks", ())
    targets = tuple(dict.fromkeys(jid for jid in ids if jid in marks))
    if not targets:
        chosen = source.get("selected")
        targets = (chosen,) if chosen in ids else ()
    if not targets:
        app.fail("Select a job in this pane before copying.")
        return True
    # Marks outlive repaints. A marked allocation that vanished from the new
    # projected order must not silently turn a multi-row copy into a subset.
    # Healthy marks in other lists are still outside this pane's copy scope.
    state = job_selection.initialize(app)
    bound, scopes = state["mark_tokens"], state["mark_scopes"]
    same_scopes = (
        {"jobs", "recent"}
        if source["scope"] in ("jobs", "recent")
        else {source["scope"]}
    )
    if any(
        jid not in source["tokens"] and scopes.get(jid) in same_scopes for jid in marks
    ):
        app.fail(
            "Some selected jobs are no longer in this list. Show or reselect those rows before copying; no partial copy was sent."
        )
        return True
    extras = tuple(
        jid
        for jid in marks
        if jid not in source["tokens"] and bound.get(jid) is not None
    )
    observed, records = _snapshot(app, (*targets, *extras))
    expected = source.get("tokens", {})
    if any(
        expected.get(jid) is None
        or observed.get(jid) != expected[jid]
        or jid not in records
        for jid in targets
    ) or any(observed.get(jid) != bound[jid] for jid in extras):
        app.fail(
            "Selected jobs changed; select the current rows again. No partial copy was sent."
        )
        return True
    lines, size = [], 0
    for jid in targets:
        line = "\t".join(_cell(value) for value in records[jid]) + "\n"
        size += len(line.encode("utf-8"))
        if size > MAX_BYTES:
            app.fail(
                "The selected jobs exceed the copy limit; select fewer jobs or export the source. No partial copy was sent."
            )
            return True
        lines.append(line)
    message = clipboard.copy_for_app(
        app, "".join(lines), getattr(app, "state_dir", None), **clipboard.options(app)
    )
    app.say(message)
    store = getattr(app, "store", None)
    if store is not None:
        store.event("copy", message)
    return True
