"""Explicit, bounded launch and command workflows for embedded terminals.

This module never runs a scheduler command from a hover, render, or job change.
It prepares argv vectors and editable command previews; reviewed allocation
actions continue through the Operations workbench and its one-use identity gate.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import copy
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import stat
import string
import subprocess
import sys

MAX_COLLECTION = 4
MAX_RECIPES = 32
MAX_COMMAND = 8192
RECIPE_SCHEMA = "raven.terminal-recipes/v1"
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_HOST = re.compile(
    r"(?:[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}@)?[A-Za-z0-9][A-Za-z0-9_.-]{0,252}\Z"
)
_JOB = re.compile(r"[0-9]{1,20}(?:_[0-9]{1,10})?(?:\+[0-9]{1,6})?\Z")
_SHELLS = {"bash", "zsh", "fish", "sh", "dash", "ksh"}


@dataclass(frozen=True)
class LaunchSpec:
    argv: tuple[str, ...]
    label: str
    kind: str
    cwd: str | None = None
    metadata: dict = field(default_factory=dict)

    def arguments(self):
        value = asdict(self)
        value["argv"] = list(self.argv)
        return value


def _text(value, label, limit=4096, *, empty=False):
    if (
        not isinstance(value, str)
        or len(value) > limit
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        raise ValueError(label + " must be bounded text without control characters")
    if not empty and not value.strip():
        raise ValueError(label + " is required")
    return value


def _name(value, label="Name"):
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise ValueError(
            label + " must use 1–64 letters, digits, underscores, or hyphens"
        )
    return value


def _executable(value):
    _text(value, "Executable")
    found = shutil.which(value)
    if not found:
        raise ValueError("Install or configure the executable first: " + value)
    return found


def shell_kind(shell):
    name = os.path.basename(str(shell))
    if name not in _SHELLS:
        raise ValueError("Prepared input requires a known shell, not a REPL or editor")
    return name


def quote(value, shell="bash"):
    """Quote one data argument, including Fish's different single-quote rules."""
    value = _text(value, "Argument", MAX_COMMAND, empty=True)
    kind = shell_kind(shell)
    if kind == "fish":
        return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"
    return shlex.quote(value)


def local_spec(cwd=None, shell=None, *, label=None):
    shell = _executable(shell or os.environ.get("SHELL") or "/bin/sh")
    # Unknown user shells can run. Only prepared shell insertion restricts them.
    if cwd is not None:
        cwd = os.path.realpath(os.path.expanduser(_text(cwd, "Working directory")))
        if not os.path.isdir(cwd) or not os.access(cwd, os.R_OK | os.X_OK):
            raise ValueError("The local working directory is not accessible")
    return LaunchSpec(
        (shell, "-i"),
        label or "local:" + (os.path.basename(cwd) if cwd else "shell"),
        "local",
        cwd,
        {
            "host": socket.gethostname(),
            "shell": os.path.basename(shell),
            "destination": "local",
            "persistent": False,
        },
    )


def ssh_spec(host):
    if not isinstance(host, str):
        raise ValueError(
            "Use one SSH host alias or user@host; options and commands are separate actions"
        )
    destination = host
    if ":" in host:
        _text(host, "SSH host", 320)
        user, separator, address = host.rpartition("@")
        if not separator:
            address = host
        elif not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}", user):
            raise ValueError("Invalid SSH user")
        if address.startswith("[") and address.endswith("]"):
            address = address[1:-1]
        if "%" in address and not re.fullmatch(
            r"[A-Za-z0-9_.-]{1,32}", address.partition("%")[2]
        ):
            raise ValueError("Invalid IPv6 interface scope")
        try:
            address = str(ipaddress.IPv6Address(address))
        except ValueError as exc:
            raise ValueError(
                "Use a valid IPv6 address without a port or command suffix"
            ) from exc
        # ssh accepts an unbracketed IPv6 destination; brackets belong to URI/scp syntax.
        destination = (user + "@" if separator else "") + address
    elif not _HOST.fullmatch(host):
        raise ValueError(
            "Use one SSH host alias or user@host; options and commands are separate actions"
        )
    return LaunchSpec(
        (_executable("ssh"), "-t", "--", destination),
        "ssh:" + host,
        "ssh",
        metadata={
            "host": host,
            "destination": "remote",
            "persistent": False,
            "shell": "",
            "delivery": "No automatic reconnect or command replay; inspect the remote prompt before input.",
        },
    )


def ssh_collection(hosts):
    if not isinstance(hosts, (list, tuple)) or not 1 <= len(hosts) <= MAX_COLLECTION:
        raise ValueError(f"Choose 1–{MAX_COLLECTION} explicit SSH destinations")
    if len(set(hosts)) != len(hosts):
        raise ValueError("A session collection cannot contain duplicate destinations")
    specs = tuple(ssh_spec(host) for host in hosts)
    if len({spec.argv[-1] for spec in specs}) != len(specs):
        raise ValueError(
            "A session collection cannot contain duplicate normalized destinations"
        )
    return specs


def tmux_spec(name):
    name = _name(name, "tmux session")
    # A dedicated server socket avoids silently attaching to unrelated sessions.
    # Closing this PTY only closes its client; tmux owns durable descendants.
    return LaunchSpec(
        (_executable("tmux"), "-L", "raven-fusion", "new-session", "-A", "-s", name),
        "tmux:" + name,
        "tmux",
        metadata={
            "host": socket.gethostname(),
            "destination": "local",
            "persistent": True,
            "tmux_socket": "raven-fusion",
            "tmux_session": name,
            "shell": "",
            "detach": "Ctrl-B then D",
        },
    )


def container_spec(engine, target, shell="/bin/sh"):
    if engine not in {"podman", "docker", "apptainer", "singularity"}:
        raise ValueError("Choose podman, docker, apptainer, or singularity")
    target = _text(target, "Container or image", 4096)
    shell = _text(shell, "Container shell", 4096)
    if not shell.startswith("/") or shell.endswith("/"):
        raise ValueError("Container shell must be an absolute executable path")
    if target.startswith("-"):
        raise ValueError("Container target cannot start with an option prefix")
    argv = [_executable(engine), "exec"]
    if engine in {"docker", "podman"}:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", target):
            raise ValueError("Use an existing container name or ID")
        argv.extend(["-it", "--", target, shell, "-i"])
    else:
        # The image is a single argv element; no shell command or mount guessing.
        argv.extend(["--", target, shell, "-i"])
    return LaunchSpec(
        tuple(argv),
        engine + ":" + os.path.basename(target),
        "container",
        metadata={
            "host": socket.gethostname(),
            "destination": "container",
            "engine": engine,
            "container": target,
            "shell": os.path.basename(shell),
            "persistent": False,
            "path_scope": "container; host paths and mounts are not assumed equivalent",
        },
    )


def reviewed_spec(argv, identity):
    """Adapt an already applied and freshly revalidated allocation receipt."""
    if not isinstance(identity, dict) or not isinstance(argv, list) or not argv:
        raise ValueError("An exact reviewed allocation receipt is required")
    if argv != identity.get("argv") or argv[0] not in {"srun", "sattach"}:
        raise ValueError("Allocation command differs from its reviewed receipt")
    job_id = identity.get("job_id", "")
    if not _JOB.fullmatch(job_id) or not isinstance(identity.get("identity"), dict):
        raise ValueError("Allocation receipt has no exact job identity")
    for argument in argv:
        _text(argument, "Allocation argument")
    kind = "allocation-shell" if identity.get("mode") == "shell" else "step-attach"
    return LaunchSpec(
        tuple(argv),
        kind + ":" + job_id,
        kind,
        metadata={
            "host": identity.get("node")
            or "allocation:" + identity.get("allocation_id", job_id),
            "transport_host": socket.gethostname(),
            "destination": "allocation",
            "job_id": job_id,
            "identity": dict(identity["identity"]),
            "node": identity.get("node", ""),
            "step_id": identity.get("step_id", ""),
            "task": identity.get("task", ""),
            "shell": os.path.basename(argv[-1]) if kind == "allocation-shell" else "",
            "persistent": False,
            "resource_notice": "Shares the allocation; no new allocation is created.",
        },
    )


def attachment_status(app, metadata):
    """Use already collected queue evidence; never poll from a terminal paint."""
    if not metadata.get("job_id"):
        return ""
    store = getattr(app, "store", None)
    if store is None:
        return "Allocation status unavailable"
    with store.lock:
        record = next(
            (r for r in store.jobs if str(getattr(r, "id", "")) == metadata["job_id"]),
            None,
        )
    if record is None:
        return "Allocation no longer in the queue; attachment is not transferred to another attempt"
    identity = metadata.get("identity", {})
    if (getattr(record, "start", "") and identity.get("StartTime") != record.start) or (
        getattr(record, "submit", "") and identity.get("SubmitTime") != record.submit
    ):
        return "Allocation attempt changed; open a newly reviewed session"
    from ..model import secs, short_duration

    limit, elapsed = (
        secs(getattr(record, "limit", "")),
        secs(getattr(record, "elapsed", "")),
    )
    remaining = (
        ""
        if limit is None or elapsed is None
        else "; remaining " + short_duration(max(0, limit - elapsed))
    )
    return str(getattr(record, "state", "unknown")) + remaining


def diagnostic(name, job_id="", shell="bash"):
    """Small read-only diagnostics, returned as text without a trailing Enter."""
    if name == "gpu":
        argv = [
            "nvidia-smi",
            "--query-gpu=index,name,utilization.gpu,memory.used,memory.total",
            "--format=csv",
        ]
    elif name == "amd-gpu":
        argv = ["rocm-smi", "--showuse", "--showmemuse"]
    elif name == "environment":
        # Deliberately excludes environment values, tokens and credentials.
        argv = ["uname", "-srmo"]
    elif name == "python":
        argv = ["python", "--version"]
    elif name == "allocation":
        if not isinstance(job_id, str) or not _JOB.fullmatch(job_id):
            raise ValueError("Choose one exact job or array task for this diagnostic")
        argv = ["scontrol", "show", "job", "-o", job_id]
    elif name == "trace":
        path = _text(job_id, "Trace manifest path")
        if path.startswith("-"):
            raise ValueError("Use an absolute or ./ relative trace manifest path")
        argv = ["raven", "trace", "inspect", path]
    else:
        raise ValueError(
            "Diagnostics: gpu, amd-gpu, environment, python, allocation, trace"
        )
    return " ".join(quote(value, shell) for value in argv)


def recipe(name, command, *, shell="bash", description=""):
    _name(name, "Recipe name")
    command = _text(command, "Recipe command", MAX_COMMAND)
    shell = shell_kind(shell)
    description = _text(description, "Description", 512, empty=True)
    fields = []
    quoted = None
    escaped = False
    previous_tail = ""
    parts = list(string.Formatter().parse(command))
    for index, (literal, key, format_spec, conversion) in enumerate(parts):
        for character in literal:
            if escaped:
                escaped = False
            elif character == "\\" and (quoted != "'" or shell == "fish"):
                escaped = True
            elif character in {"'", '"'}:
                if quoted is None:
                    quoted = character
                elif quoted == character:
                    quoted = None
        if key is not None:
            if (
                not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,31}", key)
                or format_spec
                or conversion
            ):
                raise ValueError(
                    "Recipe placeholders must be simple {name} arguments without formatting or attribute access"
                )
            # Each substitution is already quoted as a complete shell argument.
            # Wrapping it in template quotes would undo that protection.
            tail = literal[-1:] or previous_tail
            after = parts[index + 1][0][:1] if index + 1 < len(parts) else ""
            if (
                quoted
                or escaped
                or (tail and not tail.isspace())
                or (after and not after.isspace())
            ):
                raise ValueError(
                    "Placeholders must be separate unquoted arguments, such as cat {path}"
                )
            fields.append(key)
            previous_tail = "x"
        elif literal:
            previous_tail = literal[-1:]
    if len(set(fields)) > 16:
        raise ValueError("A recipe can have at most 16 parameters")
    if fields and any(token in command for token in ("`", "$(", "<(", ">(")):
        raise ValueError(
            "Parameterized recipes cannot contain nested shell substitutions"
        )
    return {
        "name": name,
        "command": command,
        "shell": shell,
        "description": description,
        "parameters": sorted(set(fields)),
    }


def expand_recipe(value, parameters):
    checked = recipe(
        value["name"],
        value["command"],
        shell=value["shell"],
        description=value.get("description", ""),
    )
    if not isinstance(parameters, dict) or set(parameters) != set(
        checked["parameters"]
    ):
        raise ValueError(
            "Provide exactly these recipe parameters: "
            + ", ".join(checked["parameters"])
        )
    result = checked["command"].format_map(
        {key: quote(text, checked["shell"]) for key, text in parameters.items()}
    )
    return _text(result, "Expanded recipe", MAX_COMMAND)


def _state(app):
    owner = getattr(app, "_chart_owner", app)
    state = getattr(owner, "terminal_workflow_state", None)
    if not isinstance(state, dict):
        state = owner.terminal_workflow_state = {
            "recipes": {},
            "sequence": None,
            "broadcast": None,
        }
    return state


def _configured_shell(app):
    cfg = getattr(app, "cfg", {})
    data = getattr(cfg, "data", cfg)
    terminal = data.get("terminal", {}) if isinstance(data, dict) else {}
    return terminal.get("shell") or None if isinstance(terminal, dict) else None


def _terminal(app):
    from . import sessions as terminal_sessions
    from .session_state import initialize

    state = initialize(app)
    sid = state.active if hasattr(state, "active") else state.get("active")
    if not sid:
        raise ValueError("Open and select a terminal session first")
    return terminal_sessions, state, sid


def _metadata(state, sid):
    # UI keeps launch metadata even before the broker's first screen arrives.
    sessions = (
        state.sessions if hasattr(state, "sessions") else state.get("sessions", {})
    )
    session = sessions.get(sid, {})
    return (
        session.get("metadata", {})
        if isinstance(session, dict)
        else getattr(session, "metadata", {})
    )


def _prepare(app, text, label, *, shell=None):
    ui, state, sid = _terminal(app)
    metadata = _metadata(state, sid)
    actual = shell_kind(metadata.get("shell", ""))
    if shell is not None and actual != shell_kind(shell):
        raise ValueError("Recipe shell differs from the selected terminal shell")
    ui.prepare_input(
        app, text, label=label + " — confirm this session is at its shell prompt"
    )
    return sid


def prepare_broadcast(app, text, targets):
    """Arm one visible no-Enter insertion; ordinary typing is never broadcast."""
    text = _text(text, "Broadcast input", MAX_COMMAND)
    if (
        not isinstance(targets, (list, tuple))
        or not 2 <= len(targets) <= MAX_COLLECTION
        or len(set(targets)) != len(targets)
    ):
        raise ValueError("Choose 2–4 distinct terminal session IDs for one broadcast")
    ui, state, active = _terminal(app)
    records = state.sessions
    frozen = []
    for sid in targets:
        record = records.get(sid, {})
        if not record or record.get("status") != "running":
            raise ValueError(
                "Every broadcast target must be an existing running session"
            )
        metadata = record.get("metadata", {})
        shell_kind(metadata.get("shell", ""))
        if record.get("kind") == "step-attach":
            raise ValueError(
                "Application step attachments cannot receive a shell broadcast"
            )
        frozen.append(
            {
                "id": sid,
                "metadata": copy.deepcopy(metadata),
                "kind": record.get("kind"),
                "label": _text(record.get("label", sid), "Session label", 256),
                "host": _text(metadata.get("host", "unknown"), "Session host", 256),
            }
        )
    ui.prepare_input(
        app,
        text,
        label=f"Armed insertion into {len(targets)} shells; confirm every target is at its prompt",
    )
    preview = state.preview
    preview.update(
        broadcast=True,
        targets=frozen,
        page=getattr(app, "tab", ""),
        generation=state.focus_generation,
    )
    return preview


def deliver_broadcast(app, preview):
    """Validate all recipients first, then queue once; never retry a partial send."""
    _ui, state, active = _terminal(app)
    if (
        not isinstance(preview, dict)
        or not preview.get("broadcast")
        or active != preview.get("session")
        or getattr(app, "tab", "") != preview.get("page")
        or state.focus_generation != preview.get("generation")
    ):
        raise ValueError("Broadcast focus changed; prepare a new explicit insertion")
    targets = preview.get("targets", ())
    if not 2 <= len(targets) <= MAX_COLLECTION or len(
        {r.get("id") for r in targets}
    ) != len(targets):
        raise ValueError("Broadcast recipients changed")
    if preview.get("attempted"):
        raise ValueError(
            "This broadcast was already attempted; inspect each session before retrying"
        )
    text = _text(preview.get("text"), "Broadcast input", MAX_COMMAND)
    from ..execution.admission import helper_admission

    blocked = helper_admission(
        app, text, {"helper_kind": "generated", "targets": targets}
    )
    if blocked is not None:
        raise ValueError(blocked["summary"])
    for frozen in targets:
        current = state.sessions.get(frozen["id"], {})
        if (
            current.get("status") != "running"
            or current.get("metadata") != frozen["metadata"]
            or current.get("kind") != frozen["kind"]
        ):
            raise ValueError(
                "A broadcast destination changed or exited; no input was sent"
            )
    preview["attempted"] = True
    delivered = []
    for frozen in targets:
        try:
            accepted = state.service.send(frozen["id"], {"type": "paste", "text": text})
        except (OSError, ValueError, RuntimeError):
            accepted = False
        if not accepted:
            # Queued bytes cannot be recalled. Do not hide a partial receipt.
            raise ValueError(
                "Broadcast stopped; input queued to "
                + (", ".join(delivered) or "no sessions")
                + ". Nothing was replayed. Inspect destinations before another action."
            )
        delivered.append(frozen["label"])
    return (
        "Input queued without Enter to "
        + ", ".join(delivered)
        + "; broadcast disarmed."
    )


def _selected_directory(app, job_id):
    if not _JOB.fullmatch(job_id):
        raise ValueError("Use one exact job ID")
    if getattr(getattr(app, "files", None), "remote", False):
        raise ValueError(
            "A remote job directory cannot be opened as a local directory; use an explicit SSH session"
        )
    store = app.store
    with store.lock:
        matches = [
            r
            for r in (*store.jobs, *store.finished)
            if str(getattr(r, "id", "")) == job_id
        ]
        paths = {str(getattr(r, "workdir", "")) for r in matches}
    if len(paths) != 1 or not next(iter(paths), ""):
        raise ValueError(
            "The selected job has no unambiguous declared working directory"
        )
    return next(iter(paths))


def _save_recipes(path, values):
    document = (
        json.dumps(
            {"schema": RECIPE_SCHEMA, "recipes": list(values.values())},
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    if len(document.encode("utf-8")) > (MAX_COMMAND + 1024) * MAX_RECIPES:
        raise ValueError(
            "Recipe export exceeds the file size budget; reduce the stored commands"
        )
    target = Path(os.path.expanduser(_text(path, "Recipe file")))
    # Never replace an existing file or follow its final symlink.
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(document)
    except BaseException:
        target.unlink(missing_ok=True)
        raise


def _load_recipes(path):
    target = Path(os.path.expanduser(_text(path, "Recipe file")))
    fd = os.open(target, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("Recipe imports require a regular JSON file")
        raw = stream.read((MAX_COMMAND + 1024) * MAX_RECIPES + 1)
    if len(raw) > (MAX_COMMAND + 1024) * MAX_RECIPES:
        raise ValueError("Recipe file exceeds the bounded recipe budget")
    data = json.loads(raw)
    if (
        not isinstance(data, dict)
        or data.get("schema") != RECIPE_SCHEMA
        or not isinstance(data.get("recipes"), list)
    ):
        raise ValueError("Use a raven.terminal-recipes/v1 document")
    if len(data["recipes"]) > MAX_RECIPES:
        raise ValueError("The recipe limit is 32")
    result = {}
    for item in data["recipes"]:
        if (
            not isinstance(item, dict)
            or not {"name", "command", "shell"} <= item.keys()
        ):
            raise ValueError("Invalid recipe record")
        value = recipe(
            item["name"],
            item["command"],
            shell=item["shell"],
            description=item.get("description", ""),
        )
        if value["name"] in result:
            raise ValueError("Duplicate recipe name")
        result[value["name"]] = value
    return result


def _recipe_io_process(action, path, recipes):
    """Keep slow or unavailable project mounts outside the UI and its process."""
    request = json.dumps(
        {"action": action, "path": path, "recipes": recipes}, ensure_ascii=False
    )
    try:
        result = subprocess.run(
            [sys.executable, "-m", "raven.terminal.workflows", "recipe-io"],
            input=request,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError(
            "Recipe file operation timed out; inspect the destination before retrying an export"
        ) from exc
    if len(result.stdout) > (MAX_COMMAND + 2048) * MAX_RECIPES:
        raise ValueError("Recipe helper returned too much data")
    try:
        response = json.loads(result.stdout)
    except (ValueError, UnicodeError) as exc:
        raise ValueError(
            "Recipe file helper failed; no commands were executed"
        ) from exc
    if not isinstance(response, dict) or not response.get("ok") or result.returncode:
        raise ValueError(
            str(response.get("error", "Recipe file helper failed"))[:512]
            if isinstance(response, dict)
            else "Invalid recipe helper response"
        )
    return response.get("recipes", {})


def _recipe_file_action(app, action, path):
    state = _state(app)
    if state.get("io_pending"):
        raise ValueError("A recipe file operation is already running")
    path = _text(path, "Recipe file")
    original = copy.deepcopy(state["recipes"])

    def work():
        return _recipe_io_process(action, path, original)

    def done(value):
        state["io_pending"] = None
        if isinstance(value, Exception):
            app.fail("Terminal recipe " + action + ": " + str(value))
            return
        try:
            if action == "import":
                if state["recipes"] != original:
                    raise ValueError(
                        "Recipes changed while the import was pending; import again"
                    )
                if (
                    not isinstance(value, dict)
                    or len(set(value) | set(original)) > MAX_RECIPES
                ):
                    raise ValueError("Import would exceed the 32 recipe limit")
                if set(value) & set(original):
                    raise ValueError("Delete conflicting recipe names before importing")
                # Revalidate the child reply before publishing any record.
                validated = {}
                for key, item in value.items():
                    valid = recipe(
                        item["name"],
                        item["command"],
                        shell=item["shell"],
                        description=item.get("description", ""),
                    )
                    if key != valid["name"]:
                        raise ValueError("Invalid imported recipe identity")
                    validated[key] = valid
                state["recipes"].update(validated)
            app.say("Recipe " + action + " completed; no commands executed.")
        except (ValueError, TypeError, KeyError) as exc:
            app.fail("Terminal recipe " + action + ": " + str(exc))

    if getattr(app, "interactive", False):
        hub = getattr(app, "research", None)
        if hub is None or not hub.start_task(work, done):
            raise ValueError(
                "The file-operation worker is busy; retry when its current task finishes"
            )
        state["io_pending"] = action
        app.say(
            "Recipe "
            + action
            + " is running in the background (five-second file deadline)."
        )
    else:
        try:
            result = work()
        except Exception as exc:
            result = exc
        done(result)


def _recipe_helper_main():
    try:
        raw = sys.stdin.buffer.read((MAX_COMMAND + 2048) * MAX_RECIPES + 1)
        if len(raw) > (MAX_COMMAND + 2048) * MAX_RECIPES:
            raise ValueError("Recipe request exceeds its size limit")
        request = json.loads(raw)
        if request.get("action") == "import":
            recipes = _load_recipes(request["path"])
        elif request.get("action") == "export":
            recipes = request["recipes"]
            if not isinstance(recipes, dict) or len(recipes) > MAX_RECIPES:
                raise ValueError("Invalid recipe export")
            checked = {
                key: recipe(
                    value["name"],
                    value["command"],
                    shell=value["shell"],
                    description=value.get("description", ""),
                )
                for key, value in recipes.items()
            }
            if any(key != value["name"] for key, value in checked.items()):
                raise ValueError("Recipe name does not match its identity")
            _save_recipes(request["path"], checked)
            recipes = {}
        else:
            raise ValueError("Unknown recipe file action")
        print(json.dumps({"ok": True, "recipes": recipes}, ensure_ascii=False))
        return 0
    except (ValueError, TypeError, KeyError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)[:512]}))
        return 2


def command_names():
    return [
        "ssh",
        "ssh-many",
        "tmux",
        "container",
        "cwd",
        "job-cwd",
        "job",
        "prepare",
        "recipe",
        "sequence",
        "broadcast",
    ]


def help_rows():
    return [
        "terminal ssh HOST | ssh-many HOST... (at most four explicit destinations)",
        "terminal tmux NAME (durable dedicated server; Ctrl-B D detaches)",
        "terminal container podman|docker|apptainer|singularity TARGET [SHELL]",
        "terminal cwd PATH | job-cwd JOBID (new local shell; no implicit cd)",
        "terminal job JOBID [node=HOST] [step=N task=N] [display=embedded|fullscreen]",
        "terminal prepare gpu|amd-gpu|environment|python|allocation [JOBID] | trace PATH",
        "terminal recipe save NAME COMMAND [shell=bash|zsh|fish|sh]",
        "terminal recipe use NAME [parameter=value...] | show NAME | delete NAME",
        "terminal recipe export PATH | import PATH | list",
        "terminal sequence start NAME... | next | cancel (review and insert each step; never auto-executes)",
        "terminal broadcast prepare TEXT SID SID... | cancel (2–4 explicit shells; one insertion, no Enter)",
    ]


def run_command(app, args):
    if (
        not args
        or args[0] != "terminal"
        or len(args) < 2
        or args[1] not in command_names()
    ):
        return False
    from . import sessions as terminal_sessions

    app = getattr(app, "_chart_owner", app)
    state = _state(app)
    command, values = args[1], args[2:]
    try:
        if command == "ssh" and len(values) == 1:
            terminal_sessions.open_session(app, **ssh_spec(values[0]).arguments())
        elif command == "ssh-many":
            # Validate the complete collection before starting its first child.
            specs = ssh_collection(values)
            opened = []
            for spec in specs:
                sid = terminal_sessions.open_session(app, **spec.arguments())
                if sid is None:
                    app.say(
                        f"Opened {len(opened)} of {len(specs)} sessions; remaining connections were not started."
                    )
                    break
                opened.append(sid)
        elif command == "tmux" and len(values) == 1:
            terminal_sessions.open_session(app, **tmux_spec(values[0]).arguments())
        elif command == "container" and len(values) in {2, 3}:
            terminal_sessions.open_session(app, **container_spec(*values).arguments())
        elif command == "cwd" and len(values) == 1:
            terminal_sessions.open_session(
                app, **local_spec(values[0], shell=_configured_shell(app)).arguments()
            )
        elif command == "job-cwd" and len(values) == 1:
            terminal_sessions.open_session(
                app,
                **local_spec(
                    _selected_directory(app, values[0]),
                    shell=_configured_shell(app),
                    label="job-directory:" + values[0],
                ).arguments(),
            )
        elif command == "job" and values:
            if not _JOB.fullmatch(values[0]):
                raise ValueError("Use one exact job or array task ID")
            options = {}
            for option in values[1:]:
                key, separator, value = option.partition("=")
                if (
                    not separator
                    or key not in {"node", "step", "task", "shell", "display"}
                    or key in options
                ):
                    raise ValueError(
                        "Options: node=HOST, step=N, task=N, shell=/path, display=embedded|fullscreen"
                    )
                options[key] = value
            from .ports import navigation

            fields = {
                "job_id": values[0],
                "mode": "attach" if "step" in options else "shell",
            }
            fields.update(
                {
                    "step_id" if key == "step" else key: value
                    for key, value in options.items()
                }
            )
            navigation(app).open_allocation(app, "allocation-shell", fields)
        elif command == "prepare" and len(values) in {1, 2}:
            _ui, terminal, sid = _terminal(app)
            shell = shell_kind(_metadata(terminal, sid).get("shell", ""))
            job_id = (
                values[1]
                if len(values) == 2
                else ""
                if values[0] == "trace"
                else str(getattr(app, "selected_id", "") or "")
            )
            _prepare(
                app, diagnostic(values[0], job_id, shell), "Diagnostic: " + values[0]
            )
        elif command == "recipe":
            recipes = state["recipes"]
            if values == ["list"]:
                app.say(
                    "Terminal recipes: "
                    + (", ".join(sorted(recipes)) or "none; use recipe save")
                )
            elif len(values) in {3, 4} and values[0] == "save":
                shell = "bash"
                if len(values) == 4:
                    if not values[3].startswith("shell="):
                        raise ValueError(
                            "The fourth recipe argument must be shell=NAME"
                        )
                    shell = values[3][6:]
                value = recipe(values[1], values[2], shell=shell)
                if value["name"] not in recipes and len(recipes) >= MAX_RECIPES:
                    raise ValueError("Delete a recipe before adding more than 32")
                recipes[value["name"]] = value
                app.say(
                    "Recipe saved for this Raven session. Use recipe export PATH to retain it."
                )
            elif len(values) == 2 and values[0] in {"export", "import"}:
                _recipe_file_action(app, values[0], values[1])
            elif len(values) >= 2 and values[0] in {"show", "use", "delete"}:
                value = recipes.get(values[1])
                if value is None:
                    raise ValueError("Unknown recipe")
                if values[0] == "use":
                    parameters = {}
                    for item in values[2:]:
                        key, separator, text = item.partition("=")
                        if not separator or key in parameters:
                            raise ValueError(
                                "Use each recipe parameter once as name=value"
                            )
                        parameters[key] = text
                    _prepare(
                        app,
                        expand_recipe(value, parameters),
                        "Recipe: " + value["name"],
                        shell=value["shell"],
                    )
                elif len(values) != 2:
                    raise ValueError("Use recipe show NAME or recipe delete NAME")
                elif values[0] == "show":
                    app.say(
                        value["name"] + " [" + value["shell"] + "]: " + value["command"]
                    )
                else:
                    del recipes[values[1]]
                    app.say("Recipe deleted.")
            else:
                raise ValueError("Use recipe save/use/show/delete/list/export/import")
        elif command == "sequence":
            if values == ["cancel"]:
                state["sequence"] = None
                terminal_sessions.cancel_prepared(app)
                app.say(
                    "Diagnostic sequence cancelled. Already delivered input cannot be retracted."
                )
            elif len(values) >= 2 and values[0] == "start":
                if len(values) > 9:
                    raise ValueError("A diagnostic sequence has at most eight steps")
                _ui, terminal, sid = _terminal(app)
                shell = shell_kind(_metadata(terminal, sid).get("shell", ""))
                job = str(getattr(app, "selected_id", "") or "")
                if "trace" in values[1:]:
                    raise ValueError(
                        "Trace inspection requires an explicit path; use terminal prepare trace PATH separately"
                    )
                commands = [(name, diagnostic(name, job, shell)) for name in values[1:]]
                state["sequence"] = {"sid": sid, "commands": commands, "index": 0}
                app.say(
                    "Sequence prepared. Use terminal sequence next to review each command; no step runs automatically."
                )
            elif values == ["next"]:
                sequence = state["sequence"]
                if sequence is None:
                    raise ValueError("Start a diagnostic sequence first")
                _ui, terminal, sid = _terminal(app)
                if sid != sequence["sid"]:
                    state["sequence"] = None
                    raise ValueError(
                        "Terminal changed; the diagnostic sequence was cancelled"
                    )
                index = sequence["index"]
                if index >= len(sequence["commands"]):
                    state["sequence"] = None
                    app.say(
                        "All diagnostic steps were prepared. No further commands remain."
                    )
                else:
                    name, text = sequence["commands"][index]
                    _prepare(
                        app,
                        text,
                        f"Step {index + 1}/{len(sequence['commands'])}: {name}",
                    )
                    sequence["index"] += 1
            else:
                raise ValueError("Use sequence start NAME..., next, or cancel")
        elif command == "broadcast":
            if values == ["cancel"]:
                terminal_sessions.cancel_prepared(app)
                app.say("Broadcast disarmed; ordinary input has one recipient.")
            elif len(values) >= 4 and values[0] == "prepare":
                prepare_broadcast(app, values[1], values[2:])
            else:
                raise ValueError(
                    "Use terminal broadcast prepare TEXT SID SID... or broadcast cancel"
                )
        else:
            raise ValueError("Invalid terminal workflow arguments; use terminal help")
    except (ValueError, OSError, KeyError, TypeError) as exc:
        app.fail("Terminal: " + str(exc))
    return True


if __name__ == "__main__":
    raise SystemExit(_recipe_helper_main() if sys.argv[1:] == ["recipe-io"] else 2)
