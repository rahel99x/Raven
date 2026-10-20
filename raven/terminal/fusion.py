"""Bounded, opt-in command observations and reviewable terminal evidence.

Shell reports are observations, never scheduler authority. Output attached to a
command is a rendered screen observation: the independent hook and PTY channels
cannot promise byte-exact command boundaries.
"""

from __future__ import annotations

import argparse
from collections import OrderedDict
import hashlib
import json
import math
import os
import re
import shlex
import socket
import sys
import time
import textwrap

from .recording import read_recording, recording_text as recording_text

from .evidence import (
    clean as clean,
    redact as redact,
    _record as _record,
    job_identity as job_identity,
    FusionStore as FusionStore,
    export_bundle as export_bundle,
    publish_share as publish_share,
    read_share as read_share,
    initialize as initialize,
    register_session as register_session,
    _terminal as _terminal,
    _service_snapshot as _service_snapshot,
    _snapshot_text as _snapshot_text,
    tick as tick,
    close as close,
    unregister_session as unregister_session,
    latest_completed as latest_completed,
)

MAX_BLOCKS = 512
MAX_OUTPUT = 32768
MAX_TOTAL = 2 << 20
MAX_PINS = 64
_SECRET = re.compile(
    r"(?im)(\b(?:password|passwd|token|secret|api[_-]?key|authorization)\b\s*[=:]\s*)([^\s,;]+)"
)
_ENV_ALLOWED = frozenset(
    (
        "PATH",
        "SHELL",
        "LANG",
        "LC_ALL",
        "TERM",
        "COLORTERM",
        "LOADEDMODULES",
        "MODULEPATH",
        "CONDA_DEFAULT_ENV",
        "VIRTUAL_ENV",
        "OMP_NUM_THREADS",
        "CUDA_VISIBLE_DEVICES",
        "SLURM_JOB_ID",
    )
)


def _background(app, title, worker):
    """Use the bounded research lane; workers return data, never touch the UI."""
    state = initialize(app)
    if state["busy"]:
        raise ValueError(
            "A terminal evidence operation is already running; wait for its result."
        )
    state["task_generation"] += 1
    generation = state["task_generation"]
    terminal = _terminal(app)
    owner = (terminal.active, terminal.focus_generation, getattr(app, "tab", None))

    def complete(value):
        if generation != state["task_generation"]:
            return
        state.update(busy=False, callback=None)
        if isinstance(value, Exception):
            if hasattr(app, "fail"):
                app.fail("Terminal evidence: " + clean(value))
            return
        state["last_result"] = {"title": title, "lines": list(value)[:4096]}
        current = _terminal(app)
        if owner == (
            current.active,
            current.focus_generation,
            getattr(app, "tab", None),
        ):
            _show(app, value, title)
        elif hasattr(app, "say"):
            app.say(title + " finished; :terminal evidence-result opens the result.")

    if getattr(app, "interactive", False):
        hub = getattr(app, "research", None)
        if hub is None or not hub.start_task(worker, complete):
            raise ValueError(
                "The background reader is busy; retry the terminal evidence operation shortly."
            )
        state.update(busy=True, callback=complete)
        if hasattr(app, "say"):
            app.say(title + " in the background.")
    else:
        state["busy"] = True
        try:
            value = worker()
        except Exception as exc:
            value = exc
        complete(value)
    return True


def command_names():
    return ["terminal"]


def actions():
    """Every evidence feature has a selectable Tools control and explicit input."""
    direct = [
        ("Command blocks", "blocks"),
        ("Command timeline", "timeline"),
        ("Command failures", "failures"),
        ("Evidence bookmarks", "pins"),
        ("Project/artifact handoffs", "handoffs"),
        ("Stop recording", "record off"),
        ("Scientific acceptance", "acceptance"),
        ("Last evidence result", "evidence-result"),
        ("Capability self-test", "selftest"),
        ("Terminal evidence help", "evidence-help"),
        ("Install optional Bash hooks", "hook bash"),
        ("Install optional Zsh hooks", "hook zsh"),
        ("Install optional Fish hooks", "hook fish"),
    ]
    prompts = [
        ("Open block: BLOCK_ID", "block"),
        ("Copy block: BLOCK_ID", "block-copy"),
        ("Search evidence: TEXT [job=ID]", "evidence-search"),
        ("Bookmark: BLOCK_ID LABEL", "bookmark"),
        ("Dismiss failure: BLOCK_ID", "dismiss"),
        ("Compare output: LEFT_ID RIGHT_ID", "compare"),
        ("Review export: BLOCK_ID... [exclude=TEXT]", "evidence-preview"),
        ("Export reviewed evidence: NEW_FILE", "evidence-export"),
        ("Record output: NEW_FILE", "record on"),
        ("Replay recording: FILE", "replay"),
        ("Capture Raven environment: NAME", "environment capture"),
        ("Compare environments: BEFORE AFTER", "environment diff"),
        ("Share read-only snapshot: NEW_FILE", "share"),
        ("View private snapshot: FILE", "share-read"),
        ("Open observed project: SESSION_ID", "handoff project"),
        ("Open declared artifact: SESSION_ID INDEX", "handoff artifact"),
        ("Open linked job logs: JOB_ID", "job-link"),
    ]
    return [
        (label, ["terminal", *shlex.split(command)]) for label, command in direct
    ] + [
        (label, ["terminal", "prompt", *shlex.split(command)])
        for label, command in prompts
    ]


def _show(app, lines, title="Terminal evidence"):
    from . import sessions as terminal_sessions

    terminal_sessions.show_report(
        app, [clean(line) for line in lines][:4096], title=title
    )


def _rows(values):
    return json.dumps(
        values, indent=2, ensure_ascii=False, allow_nan=False
    ).splitlines()


def run_command(app, args):
    if not args or args[0] != "terminal" or len(args) < 2:
        return False
    known = {
        "blocks",
        "block",
        "block-copy",
        "evidence-search",
        "bookmark",
        "pins",
        "timeline",
        "failures",
        "dismiss",
        "compare",
        "evidence-preview",
        "evidence-export",
        "environment",
        "share",
        "share-read",
        "replay",
        "record",
        "hook",
        "handoffs",
        "handoff",
        "job-link",
        "acceptance",
        "selftest",
        "evidence-help",
        "evidence-result",
    }
    action = args[1]
    if action not in known:
        return False
    state = initialize(app)
    store = state["store"]
    rest = args[2:]
    try:
        if action == "blocks":
            lines = [
                "Optional shell hooks establish command boundaries. No shell input is recorded.",
                "Output is an observed screen; independent channels cannot guarantee exact command output.",
            ]
            lines += [
                f"{b['id']}  session={b['session']}  exit={b['status']}  {b['duration']}s  {b['command'] or '(command text not captured)'}"
                for b in store.search(limit=100)
            ]
            lines.append(f"Evicted blocks: {store.evicted}. Open: :terminal block ID")
            _show(app, lines, "Command blocks")
        elif action == "block":
            if len(rest) != 1:
                raise ValueError("Usage: terminal block BLOCK_ID")
            _show(app, _rows(store.get(rest[0])))
        elif action == "block-copy":
            if len(rest) != 1:
                raise ValueError("Usage: terminal block-copy BLOCK_ID")
            block = store.get(rest[0])
            from .. import clipboard

            text = (block["command"] + "\n" if block["command"] else "") + block[
                "output"
            ]
            if not text:
                raise ValueError(
                    "That command block has no captured command text or observed output."
                )
            message = clipboard.copy_for_app(
                app, text, getattr(app, "state_dir", None), **clipboard.options(app)
            )
            if hasattr(app, "say"):
                app.say(message)
        elif action == "evidence-search":
            if not rest:
                raise ValueError(
                    "Usage: terminal evidence-search TEXT [host=HOST] [job=ID] [project=PATH] [status=N]"
                )
            filters, query = {}, []
            for value in rest:
                key, sep, item = value.partition("=")
                if sep and key in ("host", "job", "project", "status"):
                    filters[key] = int(item) if key == "status" else item
                else:
                    query.append(value)
            _show(
                app,
                _rows(store.search(" ".join(query), **filters)),
                "Cross-session evidence search",
            )
        elif action == "bookmark":
            if not rest:
                raise ValueError("Usage: terminal bookmark BLOCK_ID [LABEL]")
            _show(
                app,
                _rows(
                    store.bookmark(
                        rest[0],
                        " ".join(rest[1:]),
                        metrics=_metric_snapshot(app, store.get(rest[0])),
                    )
                ),
                "Pinned evidence",
            )
        elif action == "pins":
            _show(app, _rows(list(store.pins.values())), "Pinned evidence snapshots")
        elif action == "timeline":
            _show(
                app,
                [
                    "Clock: Raven local arrival. Remote skew is unknown; do not infer microsecond causality."
                ]
                + _rows(store.timeline(job=rest[0] if rest else None)),
                "Command timeline",
            )
        elif action == "failures":
            _show(app, _rows(store.failures()), "Failure investigation cards")
        elif action == "dismiss":
            if len(rest) != 1:
                raise ValueError("Usage: terminal dismiss BLOCK_ID")
            store.dismiss(rest[0])
            _show(app, ["Failure card dismissed."])
        elif action == "compare":
            if len(rest) not in (2, 3):
                raise ValueError(
                    "Usage: terminal compare LEFT_ID RIGHT_ID [none|trailing-space]"
                )
            frozen = FusionStore()
            frozen.blocks = OrderedDict((key, store.get(key)) for key in rest[:2])
            _background(
                app,
                "Command output comparison",
                lambda: (
                    frozen.compare(
                        *rest[:2], normalize=rest[2] if len(rest) == 3 else "none"
                    )
                    or ["No output differences."]
                ),
            )
        elif action == "evidence-preview":
            if not rest:
                raise ValueError(
                    "Usage: terminal evidence-preview BLOCK_ID... [exclude=TEXT]"
                )
            ids = [value for value in rest if not value.startswith("exclude=")]
            exclusions = [value[8:] for value in rest if value.startswith("exclude=")]
            bundle = store.bundle(ids, exclusions=exclusions)
            lines = preview_lines(bundle)
            state["preview"] = {
                "ids": ids,
                "exclusions": exclusions,
                "digest": hashlib.sha256(
                    json.dumps(
                        {key: bundle[key] for key in ("blocks", "environment")},
                        sort_keys=True,
                    ).encode()
                ).hexdigest(),
            }
            _show(
                app,
                lines
                + [
                    "Review this content. Export: :terminal evidence-export NEW_PRIVATE_FILE"
                ],
                "Review redacted reproduction evidence",
            )
        elif action == "evidence-export":
            if len(rest) != 1 or not state["preview"]:
                raise ValueError(
                    "First use terminal evidence-preview BLOCK_ID...; then terminal evidence-export NEW_FILE"
                )
            preview = state["preview"]
            bundle = store.bundle(preview["ids"], exclusions=preview["exclusions"])
            if (
                hashlib.sha256(
                    json.dumps(
                        {key: bundle[key] for key in ("blocks", "environment")},
                        sort_keys=True,
                    ).encode()
                ).hexdigest()
                != preview["digest"]
            ):
                state["preview"] = None
                raise ValueError(
                    "Evidence changed after preview. Review it again before exporting."
                )
            _background(
                app, "Evidence exported", lambda: _rows(export_bundle(rest[0], bundle))
            )
            state["preview"] = None
        elif action == "environment":
            if len(rest) == 2 and rest[0] == "capture":
                _show(
                    app,
                    _rows(store.capture_environment(rest[1], dict(os.environ))),
                    "Raven process environment summary",
                )
            elif len(rest) == 3 and rest[0] == "diff":
                _show(
                    app,
                    _rows(store.environment_diff(rest[1], rest[2])),
                    "Environment differences",
                )
            else:
                raise ValueError(
                    "Usage: terminal environment capture NAME | terminal environment diff BEFORE AFTER. Captures Raven's environment, not unknown child changes."
                )
        elif action == "share":
            if len(rest) != 1:
                raise ValueError(
                    "Usage: terminal share NEW_PRIVATE_FILE. Output may contain secrets; inspect before sharing."
                )
            sid, snapshot = _service_snapshot(app)
            if not sid or snapshot is None:
                raise ValueError(
                    "Open a terminal before publishing its output snapshot."
                )
            text = _snapshot_text(snapshot)
            _background(
                app,
                "Read-only local snapshot",
                lambda: _rows(publish_share(rest[0], text, session=sid)),
            )
        elif action == "share-read":
            if len(rest) != 1:
                raise ValueError("Usage: terminal share-read PRIVATE_FILE")
            _background(
                app, "Read-only shared snapshot", lambda: _rows(read_share(rest[0]))
            )
        elif action == "record":
            if (
                not rest
                or rest[0] not in ("on", "off")
                or (rest[0] == "on" and len(rest) != 2)
            ):
                raise ValueError(
                    "Usage: terminal record on NEW_PRIVATE_FILE | terminal record off. Output can contain echoed secrets."
                )
            terminal = _terminal(app)
            if not terminal.active or terminal.service is None:
                raise ValueError("Open a terminal before recording.")
            terminal.service.request(
                terminal.active,
                "record_start" if rest[0] == "on" else "record_stop",
                **({"path": rest[1]} if rest[0] == "on" else {}),
            )
            _show(
                app,
                [
                    "Recording request sent. Output only, 16 MiB maximum; input events are never recorded.",
                    "Child output may still contain secrets. Session health reports recording status.",
                ],
                "Terminal recording",
            )
        elif action == "replay":
            if len(rest) != 1:
                raise ValueError("Usage: terminal replay PRIVATE_RECORDING")
            from . import playback as terminal_playback

            terminal_playback.start(app, rest[0])
        elif action == "hook":
            if len(rest) != 1 or rest[0] not in ("bash", "zsh", "fish"):
                raise ValueError("Usage: terminal hook bash|zsh|fish")
            from . import sessions as terminal_sessions
            from .bridge import helper_command

            _sid, snapshot = _service_snapshot(app)
            if (
                not snapshot
                or snapshot.get("kind") != "local"
                or snapshot.get("metadata", {}).get("host") != socket.gethostname()
            ):
                raise ValueError(
                    "Install hooks in an explicit local Raven shell. Remote/allocation/container sessions need a helper installed on their own host."
                )
            from .workflows import shell_kind

            actual_shell = shell_kind(snapshot.get("metadata", {}).get("shell", ""))
            if actual_shell != rest[0]:
                raise ValueError(
                    "The requested hook shell differs from the selected terminal's known shell ("
                    + actual_shell
                    + ")."
                )
            command = helper_command(rest[0], "hook", "--shell", rest[0])
            text = (
                "source (" + command + " | psub)"
                if rest[0] == "fish"
                else "source <(" + command + ")"
            )
            terminal_sessions.prepare_input(
                app,
                text,
                label="Optional local shell hooks; confirm this session is at its shell prompt",
                helper_kind="shell-hook",
            )
        elif action == "handoffs":
            _show(
                app,
                _rows(list(store.sessions.values())),
                "Declared project, job and artifact context",
            )
        elif action == "job-link":
            if len(rest) != 1:
                raise ValueError("Usage: terminal job-link JOB_ID")
            linked = [
                identity
                for session in store.sessions.values()
                for identity in session["jobs"]
                if identity["id"] == rest[0]
            ]
            with app.store.lock:
                current = [
                    job_identity(job)
                    for job in list(app.store.jobs) + list(app.store.finished)
                    if str(_record(job).get("id")) == rest[0]
                ]
            if len(current) != 1 or current[0] not in linked:
                raise ValueError(
                    "The observed job attempt is no longer current; refresh its submission link."
                )
            app.open_log(rest[0])
        elif action == "handoff":
            if len(rest) < 2 or rest[0] not in ("project", "artifact"):
                raise ValueError(
                    "Usage: terminal handoff project SESSION_ID | terminal handoff artifact SESSION_ID INDEX"
                )
            source = store.sessions.get(rest[1])
            if not source:
                raise ValueError("Unknown terminal session.")
            if (
                source["host"] != socket.gethostname()
                or getattr(getattr(app, "research", None), "files", None)
                and getattr(app.research.files, "remote", False)
            ):
                raise ValueError(
                    "This handoff requires the terminal and Research to use the same local host; remote paths are not local substitutes."
                )
            from .ports import navigation

            if rest[0] == "project":
                if len(rest) != 2 or not source["project"]:
                    raise ValueError("Report a project path from that session first.")
                navigation(app).open_project(app, source["project"])
            else:
                if len(rest) != 3:
                    raise ValueError(
                        "Usage: terminal handoff artifact SESSION_ID INDEX (zero-based)"
                    )
                index = int(rest[2])
                if not 0 <= index < len(source["artifacts"]):
                    raise ValueError("Artifact index is outside the reported list.")
                selected = source["artifacts"][index]
                project = getattr(app, "project_state", {})
                tree = project.get("tree") or {}
                root = tree.get("root", "")
                node = next(
                    (
                        node
                        for node in tree.get("nodes", ())
                        if root
                        and os.path.normpath(os.path.join(root, node["path"]))
                        == selected["path"]
                    ),
                    None,
                )
                if node is None:
                    raise ValueError(
                        "Open this project's declared outputs first (:outputs). Only a verified declared artifact can be handed to Research."
                    )
                navigation(app).preview_artifact(app, node)
        elif action == "acceptance":
            from .ports import navigation

            if len(rest) not in (0, 2):
                raise ValueError(
                    "Usage: terminal acceptance [POLICY_PATH RESULTS_PATH]"
                )
            values = {"manifest": rest[0], "results": rest[1]} if rest else {}
            navigation(app).open_acceptance(app, values)
        elif action == "selftest":
            from . import diagnostics as terminal_diagnostics

            terminal_diagnostics.start(app)
        elif action == "evidence-result":
            result = state.get("last_result")
            if result is None:
                raise ValueError(
                    "No completed terminal evidence operation is available."
                )
            _show(app, result["lines"], result["title"])
        elif action == "evidence-help":
            _show(
                app,
                [
                    ":terminal hook bash|zsh|fish — insert optional hook setup; press Enter deliberately.",
                    "In shell: raven terminal report submission --job EXACT_SBATCH_PARSABLE_ID",
                    "In shell: raven terminal report project --project PATH",
                    "In shell: raven terminal report artifact --artifact PATH",
                    "In shell: raven terminal report acceptance --manifest PATH",
                    "In shell: raven terminal report environment --label NAME [--summary PACKAGE_MODULE_JSON]",
                    ":terminal blocks | block ID | block-copy ID | evidence-search TEXT [job=ID] [status=N]",
                    ":terminal bookmark ID LABEL | pins | timeline [JOB_ID] | failures | dismiss ID",
                    ":terminal compare LEFT RIGHT [trailing-space]",
                    ":terminal evidence-preview ID... [exclude=TEXT] → evidence-export NEW_FILE",
                    ":terminal environment capture NAME | environment diff BEFORE AFTER",
                    ":terminal record on NEW_FILE | record off | replay FILE",
                    ":terminal share NEW_FILE | share-read FILE — private, read-only, same-user snapshots",
                    ":terminal handoffs | handoff project SESSION | handoff artifact SESSION INDEX",
                    ":terminal job-link ID | acceptance [POLICY RESULTS] | selftest",
                    "No shell hook, command capture, recording, sharing or export is enabled automatically.",
                    "All hook metadata is an observation. It cannot authorize scheduler changes.",
                ],
                "Terminal evidence controls",
            )
    except (ValueError, OSError, KeyError, TypeError) as exc:
        if hasattr(app, "fail"):
            app.fail("Terminal: " + clean(exc))
        else:
            _show(app, [str(exc)], "Terminal action failed")
    return True


def preview_lines(bundle):
    """Every exported field remains reviewable; reject oversized previews."""
    lines = list(bundle["limitations"])
    for block in bundle["blocks"]:
        lines.append("Command block " + block["id"])
        for key, value in block.items():
            rendered = (
                value
                if isinstance(value, str)
                else json.dumps(value, ensure_ascii=False, allow_nan=False)
            )
            lines.append(key + ":")
            for line in str(rendered).splitlines() or [""]:
                lines.extend(
                    textwrap.wrap(
                        line,
                        width=4000,
                        replace_whitespace=False,
                        drop_whitespace=False,
                    )
                    or [""]
                )
    lines.append("Explicit environment summaries:")
    for line in _rows(bundle["environment"]):
        lines.extend(
            textwrap.wrap(
                line, width=4000, replace_whitespace=False, drop_whitespace=False
            )
            or [""]
        )
    if len(lines) > 4000:
        raise ValueError(
            "The complete preview exceeds 4000 lines. Select fewer command blocks before exporting."
        )
    return lines


def _metric_snapshot(app, block):
    """Pin only already-published local samples; never perform I/O or poll Slurm."""
    source = getattr(app, "store", None)
    if source is None or not hasattr(source, "series_publication"):
        return []
    result = []
    lower, upper = block["started"], block["ended"] or time.time()
    for identity in block["jobs"][:8]:
        values = source.series_publication(identity["id"], memory_only=True)
        for sample in values[-4000:]:
            stamp = sample.get("t")
            if (
                isinstance(stamp, (int, float))
                and not isinstance(stamp, bool)
                and math.isfinite(stamp)
                and lower <= stamp <= upper
            ):
                record = {
                    key: value
                    for key, value in sample.items()
                    if isinstance(value, (int, float, str)) and len(str(value)) <= 256
                }
                result.append({"job": identity["id"], "sample": record})
                if len(result) >= 128:
                    return result
    return result


def cli(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("hook", "report"):
        from .bridge import cli as bridge_cli

        return bridge_cli(argv)
    if argv and argv[0] == "selftest":
        from . import diagnostics as terminal_diagnostics

        return terminal_diagnostics.cli(argv[1:])
    parser = argparse.ArgumentParser(
        prog="raven terminal",
        description="Terminal hooks, read-only recordings, and capability diagnostics.",
        epilog="hook --shell bash|zsh|fish: print optional integration. report EVENT: send an owned local observation. selftest [--json]: run bounded local PTY checks. UI controls: :terminal evidence-help.",
    )
    parser.add_argument(
        "action",
        choices=("hook", "report", "selftest", "inspect", "replay", "share-read"),
    )
    parser.add_argument("path", nargs="?")
    args = parser.parse_args(argv)
    if not args.path:
        parser.error(args.action + " requires a path")
    try:
        if args.action == "share-read":
            print(read_share(args.path)["text"])
        else:
            if args.action == "replay" and sys.stdin.isatty() and sys.stdout.isatty():
                from . import playback as terminal_playback

                return terminal_playback.cli([args.path])
            header, events = read_recording(args.path)
            if args.action == "inspect":
                print(
                    json.dumps(
                        {
                            "header": header,
                            "events": len(events),
                            "duration": events[-1]["t"] if events else 0,
                            "execution": "none",
                        },
                        indent=2,
                    )
                )
            else:
                print(recording_text(events))
        return 0
    except (ValueError, OSError) as exc:
        print("raven terminal: " + clean(exc), file=sys.stderr)
        return 2
