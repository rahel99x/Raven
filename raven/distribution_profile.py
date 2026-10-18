"""Explicit prototype packaging boundary; the ordinary Raven wheel is complete."""
from __future__ import annotations

from functools import lru_cache
import importlib.util
import json
from pathlib import Path

API = 1


@lru_cache(maxsize=1)
def profile():
    path = Path(__file__).with_name("_distribution_profile.json")
    if not path.exists():
        return {"profile": "complete", "api": API, "optional_modules": [], "operations": {}}
    with path.open() as stream:
        raw = stream.read((256 << 10) + 1)
    if len(raw.encode()) > 256 << 10:
        raise ValueError("Distribution profile exceeds its bound")
    value = json.loads(raw)
    if (not isinstance(value, dict) or value.get("profile") != "core-experiment" or type(value.get("api")) is not int or value.get("api") != API
            or not isinstance(value.get("optional_modules"), list) or len(value["optional_modules"]) > 128
            or not all(isinstance(name, str) and name.startswith("raven.") and name.replace(".", "_").isidentifier()
                       for name in value["optional_modules"])
            or not isinstance(value.get("operations"), dict)
            or len(value["operations"]) > 256
            or not all(isinstance(key, str) and 0 < len(key) <= 128 and isinstance(module, str)
                       and module in value["optional_modules"] for key, module in value["operations"].items())):
        raise ValueError("Unsupported experimental core/lab distribution profile")
    return value


def absent(module):
    value = profile()
    return module in value["optional_modules"] and importlib.util.find_spec(module) is None


def missing_operation(feature):
    module = profile()["operations"].get(feature)
    return bool(module and absent(module))


def unavailable(feature):
    return ("Operation " + feature + " requires the matching experimental Raven lab wheel. "
            "Install both wheels from the same reviewed build; user data and configuration are retained.")
