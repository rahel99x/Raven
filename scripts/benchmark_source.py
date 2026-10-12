"""Resolve and verify the package belonging to an explicitly selected checkout."""
from __future__ import annotations

import ast
import importlib
from pathlib import Path
import re
import sys

MAX_METADATA_BYTES = 64 << 10
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def namespace(source_root):
    """Read the static setuptools package list without executing checkout code.

    The bounded literal parser also works on Python 3.10. Metadata-free unit
    fixtures use the current namespace; an existing but unsupported declaration
    is rejected rather than silently importing a different application.
    """
    metadata = Path(source_root).resolve() / "pyproject.toml"
    try:
        with metadata.open("rb") as stream:
            raw = stream.read(MAX_METADATA_BYTES + 1)
    except FileNotFoundError:
        return "raven"
    if len(raw) > MAX_METADATA_BYTES:
        raise ValueError("Source checkout metadata exceeds the 64 KiB limit")
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeError as exc:
        raise ValueError("Source checkout metadata must be UTF-8") from exc
    sections, selected = [], None
    for line in lines:
        header = re.fullmatch(r"\s*\[([^\[\]]+)\]\s*(?:#.*)?", line)
        if header:
            selected = [] if header[1].strip() == "tool.setuptools" else None
            if selected is not None:
                sections.append(selected)
        elif selected is not None:
            selected.append(line)
    if len(sections) != 1:
        raise ValueError("Source checkout must declare one [tool.setuptools] section")
    declarations = [(index, match[1]) for index, line in enumerate(sections[0])
                    if (match := re.fullmatch(r"\s*packages\s*=\s*(.*)", line))]
    if len(declarations) != 1:
        raise ValueError("Source checkout must declare one static setuptools packages list")
    index, literal = declarations[0]
    value = None
    for tail in ["", *sections[0][index + 1:]]:
        literal += "\n" + tail
        try:
            value = ast.literal_eval(literal.strip())
        except (SyntaxError, ValueError):
            continue
        break
    if (not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], str)
            or not _IDENTIFIER.fullmatch(value[0])):
        raise ValueError("Source checkout must declare exactly one top-level package")
    return value[0]


def package_directory(source_root):
    root = Path(source_root).resolve()
    directory = (root / namespace(root)).resolve()
    if not directory.is_relative_to(root):
        raise ValueError("Declared benchmark package must remain beneath its source root")
    return directory


def _verify_module(module, directory):
    location = getattr(module, "__file__", None)
    if location is None or not Path(location).resolve().is_relative_to(directory):
        raise RuntimeError("Benchmark imported another checkout; comparison is invalid")


def load_module(source_root, suffix=""):
    """Import only the selected checkout, refusing a previously loaded copy."""
    root = Path(source_root).resolve()
    name = namespace(root)
    directory = package_directory(root)
    if not (directory / "__init__.py").is_file():
        raise ValueError("Source root must contain its declared application package")
    if not isinstance(suffix, str) or any(not _IDENTIFIER.fullmatch(part) for part in suffix.split(".") if suffix):
        raise ValueError("Benchmark module must be a relative package module name")
    for loaded_name, module in list(sys.modules.items()):
        if loaded_name == name or loaded_name.startswith(name + "."):
            _verify_module(module, directory)
    root_path = str(root)
    sys.path[:] = [root_path, *(entry for entry in sys.path if entry != root_path)]
    importlib.invalidate_caches()
    module = importlib.import_module(name + ("." + suffix if suffix else ""))
    _verify_module(module, directory)
    return module
