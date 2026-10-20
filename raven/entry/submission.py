"""Existing-script preparation argument service, independent of UI dispatch."""

from __future__ import annotations
from .command_options import parser, sbatch_value_option


def script_args(args, *, required=True):
    """The script comes first; the remaining unknown arguments are exact sbatch argv."""
    args = list(args)
    script = args.pop(0) if args and not args[0].startswith("-") else None
    p = parser("prepare SCRIPT --workdir DIR [sbatch flags]")
    p.add_argument("--workdir")
    p.add_argument("--passport-dir")
    p.add_argument("--declared-input", dest="input", action="append", default=[])
    p.add_argument("--declared-output", dest="output", action="append", default=[])
    p.add_argument("--parameter", action="append", default=[])
    workbench = {
        "--workdir",
        "--passport-dir",
        "--declared-input",
        "--declared-output",
        "--parameter",
    }
    own, overrides = [], []
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--":
            overrides.extend(args[index + 1 :])
            break
        if sbatch_value_option(token) and index + 1 < len(args):
            overrides.extend(args[index : index + 2])
            index += 2
        elif token.split("=", 1)[0] in workbench:
            own.append(token)
            index += 1
            if "=" not in token and index < len(args):
                own.append(args[index])
                index += 1
        else:
            overrides.append(token)
            index += 1
    opts = p.parse_args(own)
    if required and not script:
        raise ValueError("a batch script is required before resource flags")
    parameters = {}
    for value in opts.parameter:
        key, sep, val = value.partition("=")
        if not sep or not key or key in parameters:
            raise ValueError("parameters must be unique NAME=VALUE pairs")
        parameters[key] = val
    return script, opts, overrides, parameters


def prepare_args(args):
    from ..submission import prepare

    script, opts, overrides, parameters = script_args(args)
    plan = prepare(
        script,
        workdir=opts.workdir,
        overrides=overrides,
        parameters=parameters,
        inputs=opts.input,
        outputs=opts.output,
    )
    return plan, opts
