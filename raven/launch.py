"""Read-only existing-script planning and explicit workload launch routing."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import stat
import sys

from . import launch_features as F, launch_settings as S

MAX_SCRIPT = 8 << 20
MAX_ARGUMENTS = 4096
MAX_ARGUMENT_BYTES = 1 << 20
# Both entry-point layers use this exact boundary, including dashboard values.
GLOBAL_VALUES = frozenset("--site --profile --config --host --ssh-user --account --site-policy --allocation --interface --user --interval --rate --days --tab --research-view --metrics-file --contract --workdir --passport --planning-file --record --replay --speed --run --eval --wait-for --timeout --poll --alert --width --ui-trace --workers --gpu-check-output".split())
GLOBAL_OPTIONAL = frozenset(("--gpu-check", "--report", "--write-config"))
KNOWN_COMMANDS = frozenset(("run", "report", "trace", "runtime", "terminal", "migrate", "config", "doctor", "qualify", "launch", "export-data", "setup", "query", "update", "revert"))
_ALIASES = {"A": "account", "J": "job-name", "D": "chdir", "o": "output", "e": "error", "a": "array", "d": "dependency", "p": "partition", "q": "qos", "t": "time", "N": "nodes", "n": "ntasks", "c": "cpus-per-task", "G": "gpus"}
_FLAGS = {"exclusive", "oversubscribe", "requeue", "no-requeue", "hold", "parsable", "contiguous", "spread-job", "use-min-nodes", "verbose", "quiet", "overcommit", "no-kill", "no-requeue"}
_FORBIDDEN = {"wrap", "wait", "test-only", "help", "version", "clusters", "export-file", "het-group", "batch", "usage"}
_KNOWN_OPTIONS = set(_ALIASES.values()) | _FLAGS | {"export", "mem", "mem-per-cpu", "mem-per-gpu", "gres", "constraint", "signal", "open-mode", "begin", "deadline", "mail-user", "mail-type", "comment", "licenses", "reservation", "wckey", "nice", "ntasks-per-node", "ntasks-per-core", "ntasks-per-socket", "gpus-per-node", "gpus-per-task", "gpus-per-socket", "gpu-bind", "gpu-freq", "cpus-per-gpu", "distribution", "get-user-env", "input", "kill-on-invalid-dep", "nodelist", "exclude", "acctg-freq", "threads-per-core", "hint", "time-min", "tmp", "reboot", "propagate", "no-kill", "wait-all-nodes", "mcs-label", "network", "prefer", "priority"}
_ENV_KEYS = {"JOB_NAME": "job-name", "WDIR": "chdir", "CHDIR": "chdir", "OUTPUT": "output", "ERROR": "error", "EXPORT": "export", "EXPORT_ENV": "export", "ACCOUNT": "account", "DEPENDENCY": "dependency", "ARRAY_INX": "array", "PARTITION": "partition", "QOS": "qos", "TIMELIMIT": "time", "MEM_PER_NODE": "mem", "MEM_PER_CPU": "mem-per-cpu", "CPUS_PER_TASK": "cpus-per-task", "NODES": "nodes", "NTASKS": "ntasks", "GPUS": "gpus", "GRES": "gres", "SIGNAL": "signal"}


def command_boundary(argv):
    """Return the first unambiguous command/script operand, never an option value."""
    i = 0
    while i < len(argv):
        token = argv[i]
        if token == "--":
            i += 1
            if i == len(argv):
                return None
            token = argv[i]
        elif token in GLOBAL_VALUES:
            i += 2
            continue
        elif token in GLOBAL_OPTIONAL:
            i += 2 if i + 1 < len(argv) and not argv[i + 1].startswith("-") else 1
            continue
        elif token.startswith("-"):
            i += 1
            continue
        if token in KNOWN_COMMANDS:
            return i, token
        if token.endswith((".sh", ".bash")) or token.startswith(("./", "../", "/")) or Path(token).is_file():
            return i, "script"
        return None
    return None


def parser():
    ap = argparse.ArgumentParser(prog="raven launch", allow_abbrev=False,
        description="Run an existing Bash/POSIX-shell script once with retained reporting. Defaults to Slurm; --local is explicit.",
        epilog="Research: " + "; ".join(item["name"] + " (" + ", ".join(item["aliases"]) + ")" for item in F.FEATURES) + ". all requests integrations only; it does not enable GPU/tracing or execute Operations. Use -- before application arguments.")
    ap.add_argument("script", help="Existing UTF-8 Bash or POSIX-shell script inside its project")
    ap.add_argument("--research", "--r", "--re", nargs="*", action="append", default=None, metavar="FEATURE", help="Research integrations; no values selects Experiment/Evidence and declared context")
    ap.add_argument("--project", metavar="PATH", help="Explicit reporting/export root; independent of scheduler WorkDir")
    ap.add_argument("--recipe", metavar="NAME", help="Named project launch settings (not the monitoring --profile)")
    ap.add_argument("--local", action="store_true", help="Explicitly execute here without Slurm")
    ap.add_argument("--account", help="Workload submission account; does not alter monitoring/Heavy preferences")
    ap.add_argument("--sbatch", action="append", default=[], metavar="TOKEN", help="One exact scheduler token, e.g. --sbatch=--mem=8G; repeatable")
    for name, explanation in (("gpu", "Request supported device recording inside the job; no allocation is added"), ("cpu-memory", "Record supported CPU and memory process measurements"), ("trace", "Request application tracing through the explicit raven_exec hook")):
        group = ap.add_mutually_exclusive_group()
        group.add_argument("--" + name, dest=name.replace("-", "_"), action="store_true", default=None, help=explanation)
        group.add_argument("--no-" + name, dest=name.replace("-", "_"), action="store_false", help="Explicitly disable " + name + " recording")
    ap.add_argument("--trace-adapter", choices=("python-monitoring", "python-legacy-profile", "native-cooperative", "mpi"))
    ap.add_argument("--metrics-file", help="Declared project-relative native application JSONL producer")
    ap.add_argument("--checkpoint-advice", help="Project-relative checkpoint duration/exposure JSON; read-only launch advice")
    ap.add_argument("--artifact", action="append", default=None, help="Explicit project-relative output path; repeatable")
    ap.add_argument("--research-source", action="append", default=None, metavar="VIEW=PATH", help="Bind a native Research report file without a manual manifest")
    ap.add_argument("--parameter", action="append", default=None, metavar="KEY=VALUE", help="Scientific parameter; JSON value or literal string")
    ap.add_argument("--workers", type=int, help="Declared scientific worker count, distinct from allocated CPUs")
    ap.add_argument("--problem-size", type=float, help="Declared scientific problem size")
    ap.add_argument("--repeat", type=int, help="Declared experiment repeat index")
    export = ap.add_mutually_exclusive_group()
    export.add_argument("--export-on-finish", nargs="?", const="default", metavar="PRESET", help="Capture an export preset for each exact completed attempt")
    export.add_argument("--no-export-on-finish", action="store_true", help="Explicitly disable saved automatic exports")
    ap.add_argument("--check", action="store_true", help="Read-only plan: no files, submission, probes, or Heavy allocation")
    ap.add_argument("--json", action="store_true", help="Machine-readable plan or receipt; diagnostics stay on stderr")
    ap.add_argument("--receipt-file", metavar="PATH", help="Exclusively create a JSON receipt; required for --local --json to preserve workload stdout")
    return ap


def parse(argv):
    argv = list(argv)
    marker = argv.index("--") if "--" in argv else len(argv)
    args = parser().parse_args(argv[:marker])
    args.raven_argv = argv
    args.script_args = argv[marker + 1:] if marker < len(argv) else []
    if len(args.script_args) > MAX_ARGUMENTS or sum(len(x.encode()) for x in args.script_args) > MAX_ARGUMENT_BYTES or any("\x00" in x for x in args.script_args):
        raise ValueError("Script arguments exceed the 4096-argument/1 MiB budget or contain NUL")
    return args


def _read_script(path):
    from .projects import _root
    _, parent = _root(str(path.parent))
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SCRIPT:
                raise ValueError("Script must be a regular file of at most 8 MiB; symlinks are refused")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                data = stream.read(MAX_SCRIPT + 1)
            if len(data) > MAX_SCRIPT or b"\x00" in data:
                raise ValueError("Script exceeds 8 MiB or contains NUL")
            return data
        finally:
            os.close(fd)
    finally:
        os.close(parent)


def _interpreter(data):
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("The supported shell script contract requires UTF-8 text") from exc
    first = text.partition("\n")[0]
    if not first.startswith("#!"):
        raise ValueError("Script needs an explicit supported #!/bin/bash or #!/bin/sh shebang")
    raw = first[2:].strip()
    words = raw.split()
    if not words or not words[0].startswith("/"):
        raise ValueError("Shebang must name an absolute interpreter")
    if Path(words[0]).name == "env":
        if len(words) != 2 or words[1] not in ("bash", "sh"):
            raise ValueError("Supported env shebangs are /usr/bin/env bash or sh; env -S and assignments are unsupported")
    elif Path(words[0]).name in ("bash", "sh"):
        if len(words) > 2 or len(words) == 2 and not re.fullmatch(r"-[efuvx]+", words[1]):
            raise ValueError("Shell shebang options support one combined -e/-f/-u/-v/-x argument; login and arbitrary interpreter options require an explicit application hook")
    else:
        raise ValueError("Only Bash/POSIX-shell scripts are supported; Python/zsh/csh scripts need a shell entry point")
    return words, text


def _script_header(text):
    lines = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            if line.startswith("#SBATCH") and (len(line) == 7 or line[7].isspace()):
                lines.append(line)
            elif stripped.startswith("#SBATCH"):
                # Slurm accepts leading whitespace before its directive marker.
                lines.append(line)
            continue
        break
    return lines


def _options(tokens, *, source):
    result = {}
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if not token.startswith("-") or token == "-":
            raise ValueError("Unsupported sbatch positional/replacement script or heterogeneous separator: " + token)
        if token.startswith("--"):
            key, sep, value = token[2:].partition("=")
            if not re.fullmatch(r"[a-z][a-z0-9-]*", key):
                raise ValueError("Invalid sbatch option " + token)
        else:
            key = _ALIASES.get(token[1:2], "")
            value, sep = token[2:], bool(token[2:])
            if not key:
                raise ValueError("Unsupported short sbatch option " + token + "; use an exact long option")
        if key in _FORBIDDEN or key not in _KNOWN_OPTIONS and any(option.startswith(key) for option in _FORBIDDEN | _KNOWN_OPTIONS):
            raise ValueError("Unsupported submission-mode sbatch option: --" + key)
        if not sep:
            if key in _FLAGS:
                value = True
            elif source == "command line":
                raise ValueError("Scheduler values require --sbatch=--option=value; script arguments follow --")
            else:
                i += 1
                if i >= len(tokens):
                    raise ValueError("Missing sbatch value for --" + key)
                value = tokens[i]
        if value == "" or isinstance(value, str) and ("\x00" in value or "\n" in value or len(value) > 8192):
            raise ValueError("Invalid or empty sbatch value for --" + key)
        result[key] = value
        i += 1
    return result


def effective_scheduler(header, options, environ, defaults=None, account=None):
    """Metadata follows Slurm precedence; original directive lines stay intact."""
    effective, sources = {}, {}
    # Slurm's environment spelling is mostly mechanical, with a few aliases.
    # Recognize every translated option we already support, even when the
    # script has no directive for it. Otherwise a generated default becomes a
    # command-line override of the user's environment.
    environment_aliases = dict(_ENV_KEYS, DEBUG="verbose", TIMEMIN="time-min")
    optional_environment = {"exclusive", "no-kill", "get-user-env", "nice"}
    conflicting_defaults = ({"mem", "mem-per-cpu", "mem-per-gpu"},
                            {"cpus-per-task", "cpus-per-gpu"},
                            {"exclusive", "oversubscribe"},
                            {"requeue", "no-requeue"})
    inherited = set()

    def environment_flag(value):
        # Slurm's no-argument environment options enable on empty/yes/nonzero
        # integer, and reset on all other strings (including "0" and "no").
        numeric = re.fullmatch(r"\s*[+-]?\d+", value)
        return value == "" or value.lower() == "yes" or bool(numeric and int(value))
    if len(options) > 256 or any(not isinstance(item, str) or len(item) > 4096 or "\x00" in item for item in options):
        raise ValueError("Scheduler options exceed 256 tokens or 4096 characters per token")
    for line in header:
        try:
            tokens = shlex.split(line.lstrip()[7:], comments=True, posix=True)
        except ValueError as exc:
            raise ValueError("Cannot safely preview #SBATCH quoting: " + str(exc)) from exc
        for key, value in _options(tokens, source="script directive").items():
            effective[key], sources[key] = value, "script directive"
    for env_key, value in environ.items():
        if not isinstance(env_key, str) or not env_key.startswith("SBATCH_"):
            continue
        suffix = env_key[7:]
        key = environment_aliases.get(suffix, suffix.lower().replace("_", "-"))
        if key not in _KNOWN_OPTIONS and key not in _FORBIDDEN and key not in effective:
            continue  # Unknown site/plugin environment is preserved, not interpreted.
        if not isinstance(value, str) or len(value) > 8192 or any(char in value for char in "\x00\r\n"):
            raise ValueError("Invalid scheduler environment value for " + env_key)
        if key in _FORBIDDEN:
            if key in {"wait", "test-only", "help", "version", "usage"} and not environment_flag(value):
                continue  # An explicitly disabled flag cannot change submission mode.
            raise ValueError("Unset " + env_key + " before launching: this submission mode is unsupported")
        if key in _FLAGS - optional_environment or key == "reboot":
            interpreted = environment_flag(value)
            if key == "verbose":
                interpreted = int(value) if re.fullmatch(r"\s*[+-]?\d+", value) else 0
        elif key == "exclusive":
            if value.lower() not in {"", "exclusive", "oversubscribe", "user", "mcs", "topo"}:
                raise ValueError(env_key + " requires empty, exclusive, oversubscribe, user, mcs, or topo")
            interpreted = True if value.lower() in {"", "exclusive"} else value.lower()
        elif key == "no-kill":
            if value.lower() not in {"", "set", "off", "no"}:
                raise ValueError(env_key + " requires empty, set, off, or no")
            interpreted = value.lower() in {"", "set"}
        elif key == "get-user-env":
            if value and not re.fullmatch(r"(?:\d+[sSlL]?|[sSlL])", value):
                raise ValueError(env_key + " requires an optional timeout and S/L mode")
            interpreted = value or True
        elif key == "nice":
            if value and not re.fullmatch(r"[+-]?\d+", value):
                raise ValueError(env_key + " requires an optional integer adjustment")
            interpreted = value or True
        else:
            if not value:
                raise ValueError(env_key + " requires a nonempty value; unset it to use a project default")
            interpreted = value
        if key in inherited and effective[key] != interpreted:
            raise ValueError("Conflicting environment aliases for --" + key + "; keep one SBATCH setting")
        inherited.add(key)
        effective[key], sources[key] = interpreted, "environment " + env_key
    explicit = _options(options, source="command line")
    if account is not None:
        if not account or len(account) > 128 or not account.isprintable():
            raise ValueError("Workload account must be nonempty printable text of at most 128 characters")
        if explicit.get("account", account) != account:
            raise ValueError("--account conflicts with --sbatch=--account; select one workload account")
        explicit["account"] = account
    forwarded = list(options)
    if account is not None:
        forwarded.append("--account=" + account)
    for key, value in explicit.items():
        effective[key], sources[key] = value, "command line"
    for raw, value in (defaults or {}).items():
        key = raw.removeprefix("--")
        competing = next((group for group in conflicting_defaults if key in group), {key})
        if not competing.intersection(effective):
            token = "--" + key + ("" if value is True else "=" + str(value))
            _options([token], source="command line")
            forwarded.append(token)
            effective[key], sources[key] = value, "project resource default"
    return effective, sources, forwarded


def _project(script, explicit, cwd, associations):
    from .projects import _root
    if explicit:
        root, source = Path(os.path.abspath(os.path.join(str(cwd), os.path.expanduser(explicit)))), "explicit --project"
    else:
        matches = set()
        for item in associations[:256]:
            if isinstance(item, dict) and item.get("script") == str(script) and isinstance(item.get("project_root"), str):
                matches.add(item["project_root"])
        if len(matches) > 1:
            raise ValueError("Script has ambiguous Raven associations; choose --project explicitly")
        if matches:
            root, source = Path(matches.pop()), "existing Raven association"
        else:
            ancestors = (script.parent, *script.parent.parents)
            if len(ancestors) > 64:
                raise ValueError("Project discovery exceeds 64 ancestors; choose --project explicitly")
            root, source = script.parent, "script directory (non-Git fallback)"
            git = None
            for parent in ancestors:
                descriptor = parent / ".raven" / "project.json"
                if descriptor.exists():
                    from .projects import _read
                    _, parent_fd = _root(str(parent))
                    try:
                        declaration = json.loads(_read(parent_fd, ".raven/project.json", 65536)[0])
                    finally:
                        os.close(parent_fd)
                    if not isinstance(declaration, dict) or declaration.get("schema") != "raven.project/v1":
                        raise ValueError("Invalid Raven project descriptor: " + str(descriptor))
                    root, source = parent, "nearest Raven project"
                    break
                if (parent / ".raven" / "automation.json").exists():
                    S.load_settings(parent)
                    root, source = parent, "nearest Raven project settings"
                    break
                marker = parent / ".git"
                if git is None and ((marker.is_dir() and (marker / "HEAD").is_file()) or marker.is_file()):
                    git = parent
            else:
                if git is not None:
                    root, source = git, "Git worktree root"
    root_text, fd = _root(str(root))
    os.close(fd)
    root = Path(root_text)
    try:
        relative = str(script.relative_to(root))
    except ValueError as exc:
        raise ValueError("Script must be inside the selected project's root; choose a consistent --project") from exc
    from .projects import relative_path
    relative_path(relative)
    return root, source, relative


def _pairs(values):
    result = {}
    for text in values or []:
        key, sep, value = text.partition("=")
        if not sep or not key or len(key) > 128:
            raise ValueError("Expected a bounded KEY=VALUE declaration")
        try:
            result[key] = json.loads(value)
        except ValueError:
            result[key] = value
    return result


def _declared_paths(settings, root):
    from .reporting import _under
    paths = []
    for key in ("application_metrics", "array_manifest", "checkpoint_advice"):
        if settings.get(key):
            paths.append((key, settings[key]))
    for key, value in settings.get("research_sources", {}).items():
        F.normalize_features([key])
        paths.append(("Research " + key, value))
    for item in settings.get("artifacts", []):
        paths.append(("artifact", item.get("path") if isinstance(item, dict) else item))
    for label, value in paths:
        if not isinstance(value, str):
            raise ValueError(label + " requires an explicit relative path")
        # Missing outputs and parent directories are allowed; any existing
        # component must still be a real project directory, never a symlink.
        if Path(value).is_absolute() or ".." in Path(value).parts:
            raise ValueError(label + " must be project-relative without traversal")
        from .projects import relative_path
        relative_path(value)
        path = root
        for component in Path(value).parts:
            path /= component
            if path.is_symlink():
                raise ValueError(label + " cannot use symlinks")


def _completion_request(value, scope=None):
    """Capture preference metadata only after checking its filesystem scope."""
    if value is False:
        return False
    import getpass
    request = json.loads(json.dumps(value, allow_nan=False))
    if (request.get("range_policy", "full") not in ("full", "full-recorded-run")
            or request.get("start") is not None or request.get("end") is not None):
        raise ValueError("Completion exports for future launches require the full recorded run; chart or explicit timestamps belong to an existing attempt. Save a full-range preset or use export-data on that attempt.")
    destination = request.get("destination", {})
    if not isinstance(destination, dict):
        raise ValueError("Completion export destination must be an object")
    if destination.get("mode") == "absolute":
        host, owner = destination.get("host"), destination.get("user")
        host, owner = (scope or {}).get("host", host), (scope or {}).get("user", owner)
        if host not in (None, "", "local", "localhost", socket.gethostname(), socket.getfqdn()):
            raise ValueError("Saved absolute export destination belongs to another filesystem host; choose a destination on this submission host")
        if owner is not None and owner != getpass.getuser():
            raise ValueError("Saved absolute export destination belongs to another user; choose a destination for this account")
    destination.pop("user", None)
    return request


def resolve_plan(argv, *, cwd=None, environ=None, associations=(), connection=None, captured_settings=None):
    """Resolve a JSON-serializable plan. No scheduler calls or filesystem writes."""
    args = parse(argv) if not isinstance(argv, argparse.Namespace) else argv
    env = dict(os.environ if environ is None else environ)
    cwd = Path(os.path.abspath(str(cwd or os.getcwd())))
    script = Path(os.path.abspath(os.path.join(str(cwd), os.path.expanduser(args.script))))
    receipt_path = ""
    if args.receipt_file:
        from .projects import _root
        receipt = Path(os.path.abspath(os.path.join(str(cwd), os.path.expanduser(args.receipt_file))))
        _, receipt_fd = _root(str(receipt.parent))
        try:
            try:
                os.stat(receipt.name, dir_fd=receipt_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise ValueError("Receipt file already exists; choose a new --receipt-file path")
        finally:
            os.close(receipt_fd)
        receipt_path = str(receipt)
    if args.local and args.json and not args.check and not receipt_path:
        raise ValueError("--local --json requires --receipt-file PATH; workload stdout remains unchanged")
    if connection and connection.get("transport") == "none" and not args.local:
        raise ValueError("This site is a standalone workstation. Explicitly choose --local to execute here, or select a Slurm site before submitting.")
    if connection and connection.get("transport") not in (None, "local", "none"):
        host = connection.get("host") or "SUBMISSION_HOST"
        command = shlex.join(["raven", "launch", *getattr(args, "raven_argv", [args.script])])
        raise ValueError("Remote workload launch is unsupported. Run on submission host " + host + ": " + command + ". No local fallback was started.")
    data = _read_script(script)
    interpreter, text = _interpreter(data)
    user = S.load_user_settings(env)
    root, project_source, relative = _project(script, args.project, cwd, [*associations, *user.get("associations", [])])
    explicit = {}
    if args.research is not None:
        values = [value for group in args.research for value in (group or F.DEFAULT_FEATURES)]
        explicit["features"] = F.normalize_features(values)
    for key in ("gpu", "cpu_memory", "trace", "trace_adapter"):
        if getattr(args, key) is not None:
            explicit[key] = getattr(args, key)
    if args.metrics_file is not None:
        explicit["application_metrics"] = args.metrics_file
    if args.checkpoint_advice is not None:
        explicit["checkpoint_advice"] = args.checkpoint_advice
    if args.artifact is not None:
        explicit["artifacts"] = args.artifact
    if args.research_source is not None:
        bindings = {}
        for item in args.research_source:
            name, sep, path = item.partition("=")
            if not sep:
                raise ValueError("Research bindings use VIEW=PATH")
            bindings[F.normalize_features([name])[0]] = path
        explicit["research_sources"] = bindings
    if args.parameter is not None:
        explicit["parameters"] = _pairs(args.parameter)
    context = {}
    for name in ("workers", "problem_size", "repeat"):
        value = getattr(args, name)
        if value is not None:
            if value < (1e-9 if name == "problem_size" else 1) or not __import__("math").isfinite(value):
                raise ValueError(name + " must be a finite positive declaration")
            context[name] = value
    if context:
        explicit["scientific_context"] = context
    saved = S.load_settings(root)
    if args.no_export_on_finish:
        explicit["auto_export"] = False
        explicit["auto_export_scope"] = {}
    elif args.export_on_finish is not None:
        explicit["auto_export_scope"] = {}
        name = args.export_on_finish
        if name == "default":
            name = saved.get("default_export_preset") or user.get("default_export_preset")
        presets = dict(user["export_presets"], **saved["export_presets"])
        if name:
            if name not in presets:
                raise ValueError("Unknown completion-export preset: " + name)
            explicit["auto_export"] = _completion_request(presets[name])
        else:
            explicit["auto_export"] = {"include": ["all"], "destination": {"mode": "project", "path": "", "host": socket.gethostname()}, "layout": "category"}
    if captured_settings is None:
        settings, setting_sources = S.resolve_settings(root, recipe=args.recipe, user_defaults=user, explicit=explicit)
    else:
        # Run again starts from the validated retained settings, without
        # silently adopting newly saved defaults or mutating project recipes.
        settings = S._launch(captured_settings)
        settings.update(explicit)
        setting_sources = {key: "retained run settings" for key in settings}
        setting_sources.update({key: "explicit launch option" for key in explicit})
    if settings.get("auto_export") is not None and settings.get("auto_export") is not False:
        settings["auto_export"] = _completion_request(settings["auto_export"], settings.get("auto_export_scope"))
    settings.setdefault("features", list(F.DEFAULT_FEATURES))
    if args.research is not None and any(not group for group in args.research):
        settings["features"] = F.normalize_features([*settings["features"], *settings.get("research_sources", {})])
    if settings.get("trace_adapter") and settings.get("trace") is False and "trace_adapter" in explicit:
        raise ValueError("--trace-adapter conflicts with --no-trace")
    _declared_paths(settings, root)
    header = _script_header(text)
    effective, option_sources, sbatch = effective_scheduler(header, args.sbatch, env, settings.get("sbatch_defaults"), args.account)
    for option, source in tuple(option_sources.items()):
        if source == "project resource default":
            option_sources[option] = "resource default (" + setting_sources.get("sbatch_defaults", "project setting") + ")"
    if settings.get("array_manifest"):
        if args.local or not effective.get("array"):
            raise ValueError("A declared array manifest requires a Slurm --array option or directive")
        from .projects import _read, _root
        from .array_manifest import parse as parse_array
        _, array_fd = _root(str(root))
        try:
            declaration = json.loads(_read(array_fd, settings["array_manifest"], 4 << 20)[0])
        finally:
            os.close(array_fd)
        if not isinstance(declaration, dict):
            raise ValueError("Array manifest must be a native object")
        parse_array(json.dumps(dict(declaration, array_id="1", cluster=""), allow_nan=False).encode())
    if args.local and (args.sbatch or args.account):
        raise ValueError("--local cannot accept workload --account or --sbatch options")
    name = effective.get("job-name", script.name)
    if not isinstance(name, str) or not name or len(name) > 128 or not name.isprintable():
        raise ValueError("Job name must be printable and at most 128 characters for the reporting contract")
    if "job-name" not in effective:
        sbatch.append("--job-name=" + name)
        option_sources["job-name"] = "original script filename"
    workdir = str(cwd)
    if not args.local and "chdir" in effective:
        workdir = os.path.abspath(os.path.join(str(cwd), str(effective["chdir"])))
    # Local execution mirrors an ordinary shell invocation from the caller's
    # current directory. Slurm validates an alternate WorkDir on its host.
    plan = {"schema": "raven.launch-plan/v1", "project_root": str(root), "project_source": project_source,
            "script": str(script), "script_relative": relative, "script_sha256": hashlib.sha256(data).hexdigest(),
            "script_size": len(data), "submit_dir": str(cwd), "workdir": workdir, "filesystem_host": socket.gethostname(),
            "execution": "local" if args.local else "slurm", "interpreter": interpreter, "script_args": list(args.script_args),
            "sbatch_header": header, "sbatch_options": sbatch, "effective_options": effective, "option_sources": option_sources,
            "name": name, "account": effective.get("account", ""), "account_source": option_sources.get("account", "scheduler default"),
            "export_policy": effective.get("export", "ALL"), "features": settings["features"], "gpu": settings.get("gpu", False), "trace": settings.get("trace", False),
            "settings": settings, "setting_sources": setting_sources, "recipe": args.recipe or "", "auto_export": settings.get("auto_export", False),
            "readiness": F.readiness(settings["features"], settings, execution="local" if args.local else "slurm", gpu=settings.get("gpu", False), trace=settings.get("trace", False), effective_options=effective),
            "compatibility": ["$0 and BASH_SOURCE identify the immutable script snapshot; source-relative programs need RAVEN_SOURCE_SCRIPT or an explicit application hook.", "Static preview is not Slurm acceptance; shared project and reporting Python must be visible on compute nodes."], "mutations": 0}
    plan["receipt_file"] = receipt_path
    if settings.get("checkpoint_advice"):
        from .checkpoint_advisor import advise
        from .evidence import loads
        from .projects import _root, _read
        _, advice_fd = _root(str(root))
        try:
            advice_data, _ = _read(advice_fd, settings["checkpoint_advice"], 1 << 20)
        finally:
            os.close(advice_fd)
        plan["checkpoint_advice"] = advise(loads(advice_data))
    # Use the producer validators before creating an intent.
    from .reporting import _encoded, _parameters, _resources
    _parameters(settings.get("parameters", {}))
    _resources(settings.get("resources", {}))
    _encoded(plan)
    return plan


def format_plan(plan):
    fields = (("Script", plan["script"]), ("Job's project", plan["project_root"] + " (" + plan["project_source"] + ")"), ("Working directory", plan["workdir"]), ("Execution", "Local (explicit)" if plan["execution"] == "local" else "Slurm batch submission"), ("Filesystem host", plan["filesystem_host"]), ("Workload account", (plan["account"] or "Scheduler default") + " (" + plan["account_source"] + ")"))
    lines = [label + ":" + " " * max(1, 21 - len(label)) + str(value) for label, value in fields]
    lines.append("")
    labels = {"ready": "Ready", "needs-input": "Needs input", "will-verify": "Will verify at job start", "unsupported": "Unsupported"}
    for row in plan["readiness"]:
        lines.append(row["label"] + ": " + labels.get(row["status"], row["status"]) + " — " + row["reason"])
    lines += ["", *plan["compatibility"]]
    if plan.get("checkpoint_advice"):
        from .checkpoint_advisor import review_lines
        lines.extend(review_lines(plan["checkpoint_advice"]))
    return "\n".join(lines)


def _write_receipt(path, receipt):
    """Publish one private complete receipt without replacing any existing name."""
    import uuid
    from .projects import _root
    target = Path(path)
    _, parent = _root(str(target.parent))
    temporary = ".raven-receipt-" + uuid.uuid4().hex
    created = False
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        created = True
        with os.fdopen(fd, "wb") as stream:
            stream.write(json.dumps(receipt, ensure_ascii=True, allow_nan=False, indent=2).encode() + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, target.name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
        os.fsync(parent)
    finally:
        if created:
            os.unlink(temporary, dir_fd=parent)
        os.close(parent)


def main(argv=None, *, connection=None):
    try:
        args = parse(sys.argv[1:] if argv is None else argv)
        plan = resolve_plan(args, connection=connection)
        if args.check:
            print(json.dumps(plan, indent=2, ensure_ascii=True, allow_nan=False) if args.json else format_plan(plan))
            return 0
        if plan["execution"] == "slurm" and shutil.which("sbatch") is None:
            raise ValueError("Slurm sbatch is unavailable. Run this command on your submission host, or explicitly use --local where local computation is appropriate. Nothing was executed.")
        from .launch_runtime import execute_plan
        receipt = execute_plan(plan)
        if plan["receipt_file"]:
            try:
                _write_receipt(plan["receipt_file"], receipt)
            except (OSError, ValueError) as exc:
                print("Raven launch: workload already executed/submitted; receipt file could not be published: " + str(exc) + "; do not resubmit. Receipt: " + json.dumps(receipt, ensure_ascii=True), file=sys.stderr)
                return int(receipt.get("exit_code", 0)) if plan["execution"] == "local" else 2
            if plan["execution"] == "local":
                print("Raven receipt: " + plan["receipt_file"], file=sys.stderr)
        if args.json and plan["execution"] != "local":
            print(json.dumps(receipt, indent=2, ensure_ascii=True, allow_nan=False))
        elif not args.json:
            job = receipt.get("job_id", "")
            output = sys.stderr if plan["execution"] == "local" else sys.stdout
            print("Submitted job " + str(job) if job else "Run: " + str(receipt.get("run_id", receipt.get("run", ""))), file=output)
            print("Launch: " + str(receipt.get("launch_path", receipt.get("token", ""))), file=output)
            for row in plan["readiness"]:
                print(row["label"] + ": " + row["status"] + " — " + row["reason"], file=output)
        return int(receipt.get("exit_code", 0)) if plan["execution"] == "local" else 0
    except (OSError, ValueError, RuntimeError) as exc:
        print("Raven launch: " + str(exc), file=sys.stderr)
        return 2


def dispatch(argv, *, resolved=None):
    """Return None for existing commands; otherwise handle once without Heavy."""
    boundary = command_boundary(argv)
    if boundary is None or boundary[1] not in ("launch", "script", "export-data"):
        return None
    index, kind = boundary
    prefix = list(argv[:index])
    if prefix[-1:] == ["--"]:
        prefix.pop()
    from .raven_launch import _parser
    options, unknown = _parser().parse_known_args(prefix)
    if unknown:
        raise ValueError("Dashboard-only or unknown options before workload command: " + " ".join(unknown) + "; put workload options after launch")
    if options.help:
        parser().print_help()
        return 0
    if options.version:
        from . import __version__
        print("Raven " + __version__)
        return 0
    if options.allocation or options.interface != "local" or options.plan or options.write_config is not None:
        raise ValueError("Workload launch/export is independent of Heavy allocation and bootstrap mutations; use launch --check")
    if kind == "export-data":
        from .data_export_cli import main as export_main
        arguments = list(argv[index + 1:])
        explicit_host = any(token == "--host" or token.startswith("--host=") for token in arguments)
        if options.host:
            if explicit_host:
                raise ValueError("Specify export filesystem --host once, after export-data")
            arguments.append("--host=" + options.host)
            explicit_host = True
        if not explicit_host and not any(token in ("--help", "-h") for token in arguments):
            if resolved is None:
                from .raven_config import resolve
                resolved = resolve(options.config, site=options.site, legacy_profile=options.profile, launcher="light", site_policy_path=options.site_policy)
            connection = resolved.data.get("connection", {}) if hasattr(resolved, "data") else resolved.get("connection", {})
            if connection.get("transport") not in (None, "local"):
                host = connection.get("host", "SUBMISSION_HOST")
                command = shlex.join(["raven", "export-data", *arguments, "--host", "local"])
                raise ValueError("Saved connection selects remote host " + host + "; remote exports are unsupported. Run on that filesystem host: " + command + ". To intentionally export a local project here, explicitly pass export-data --host local.")
        if options.json and "--json" not in arguments:
            arguments.append("--json")
        return export_main(arguments)
    # Resolve transport without configuring a collection manager, importing the
    # dashboard, or admitting Heavy. Global account remains monitoring-only.
    if resolved is None:
        from .raven_config import resolve
        overrides = {"connection": {"transport": "ssh", "host": options.host}} if options.host else None
        resolved = resolve(options.config, site=options.site, legacy_profile=options.profile, launcher="light", site_policy_path=options.site_policy, cli_overrides=overrides)
    connection = resolved.data.get("connection", {}) if hasattr(resolved, "data") else resolved.get("connection", {})
    arguments = list(argv[index + (0 if kind == "script" else 1):])
    if options.json and "--json" not in arguments[:arguments.index("--") if "--" in arguments else len(arguments)]:
        arguments.insert(0, "--json")
    return main(arguments, connection=connection)
