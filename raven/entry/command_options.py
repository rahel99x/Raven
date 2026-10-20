"""Command grammar shared by entrypoints and domain services; no UI owners."""

from __future__ import annotations
import argparse

RESEARCH_COMMANDS = (
    "metric",
    "metrics",
    "passport",
    "validate",
    "artifacts",
    "prepare",
    "submit",
    "investigate",
    "array",
)
PLANNING_COMMANDS = (
    "predict",
    "forecast",
    "blockers",
    "tradeoffs",
    "scaling",
    "workflow",
    "choose",
    "checkpoint-advice",
)


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def parser(name):
    return Parser(prog=name, add_help=False, allow_abbrev=False)


def sbatch_value_option(token):
    """True for supported sbatch flags whose following argv item is a literal value."""
    from ..submission import SHORT, VALUE_OPTIONS

    return (
        token.startswith("--")
        and token[2:] in VALUE_OPTIONS
        or len(token) == 2
        and token[0] == "-"
        and token[1] in SHORT
    )


def command_value_option(command, token):
    options = {
        "metric": {"--value", "--step", "--phase", "--completed", "--total", "--unit"},
        "passport": {"--workdir", "--output-dir", "--env", "--input"},
        "prepare": {
            "--workdir",
            "--passport-dir",
            "--declared-input",
            "--declared-output",
            "--parameter",
        },
        "submit": {
            "--workdir",
            "--passport-dir",
            "--declared-input",
            "--declared-output",
            "--parameter",
        },
        "array": {"--workdir", "--indices", "--limit"},
        "resubmit": {"--script"},
        "predict": {
            "--file",
            "--coverage",
            "--name",
            "--partition",
            "--account",
            "--qos",
            "--cpus",
            "--nodes",
            "--gpus",
        },
        "forecast": {"--file", "--coverage"},
        "blockers": {"--file", "--coverage"},
        "tradeoffs": {"--choose", "--script", "--workdir"},
        "choose": {"--file", "--workdir"},
        "scaling": {"--workdir", "--mode", "--baseline"},
        "workflow": {"--workdir"},
    }
    return (
        token in options.get(command, set())
        or command in ("prepare", "submit", "array", "resubmit")
        and sbatch_value_option(token)
    )
