"""Plugins: Python files in ``~/.config/raven/plugins/`` (or listed under ``plugins`` in the config) that define
``setup(api)``.  The API registers palette commands, job flags, extra tabs, event hooks and sampled sources; a
failing plugin is reported on the Sources tab and never stops the dashboard.

    # ~/.config/raven/plugins/hello.py
    def setup(api):
        api.command("hello", lambda app, args: f"hello {' '.join(args) or app.user}", "hello [name]")
        api.flag(lambda job, snap, app: "big" if job.cpus >= 32 else None, style="magenta")
        api.tab("hello", "Hello", lambda snap, app, width, height: [[("   hi there", "bold")]])
        api.on_event(lambda ev: None)
        api.source("mysrc", 60.0, lambda slurm, store: None)
"""
from __future__ import annotations

import importlib.util
import os
import traceback
from typing import Callable, Dict, List, Optional, Tuple


class PluginAPI:
    def __init__(self):
        self.commands: Dict[str, Tuple[Callable, str]] = {}
        self.flags: List[Tuple[Callable, str]] = []
        self.tabs: List[Tuple[str, str, Callable]] = []
        self.event_hooks: List[Callable] = []
        self.sources: List[Tuple[str, float, Callable]] = []
        self.loaded: List[str] = []
        self.modules: Dict[str, object] = {}
        self.errors: Dict[str, str] = {}
        self._current = ""

    def command(self, name: str, fn: Callable, hint: str = "") -> None:
        """``fn(app, args) -> message or None`` runs when the palette gets ``name ...``."""
        self.commands[name] = (fn, hint or name)

    def flag(self, fn: Callable, style: str = "magenta") -> None:
        """``fn(job, snap, app) -> text or None``: an extra entry in the FLAGS column."""
        self.flags.append((fn, style))

    def tab(self, key: str, title: str, render: Callable) -> None:
        """``render(snap, app, width, height) -> rows`` becomes a tab after the built-in ones."""
        self.tabs.append((key, title, render))

    def on_event(self, fn: Callable) -> None:
        """``fn(event_dict)`` on every transition and action."""
        self.event_hooks.append(fn)

    def source(self, name: str, interval: float, fn: Callable) -> None:
        """``fn(slurm, store)`` sampled every ``interval`` seconds in the sampler's pool, with health like any source."""
        self.sources.append((name, float(interval), fn))


def plugin_dir(config_path: str) -> str:
    base = os.path.dirname(config_path) if config_path else ""
    if not base:
        from .config import default_path
        base = os.path.dirname(default_path())
    return os.path.join(base, "plugins")


def discover(config_path: str, extra: List[str]) -> List[str]:
    paths: List[str] = []
    d = plugin_dir(config_path)
    if os.path.isdir(d):
        paths += sorted(os.path.join(d, f) for f in os.listdir(d) if f.endswith(".py") and not f.startswith("_"))
    for p in extra or []:
        p = os.path.expanduser(p)
        if os.path.isdir(p):
            paths += sorted(os.path.join(p, f) for f in os.listdir(p) if f.endswith(".py") and not f.startswith("_"))
        elif os.path.exists(p):
            paths.append(p)
    return paths


def load(paths: List[str], api: Optional[PluginAPI] = None) -> PluginAPI:
    api = api or PluginAPI()
    for path in paths:
        name = os.path.splitext(os.path.basename(path))[0]
        api._current = name
        try:
            spec = importlib.util.spec_from_file_location(f"raven_plugin_{name}", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            setup = getattr(mod, "setup", None)
            if setup is None:
                raise RuntimeError("no setup(api) function")
            setup(api)
            api.loaded.append(name)
            api.modules[name] = mod
        except Exception as e:                            # a broken plugin is reported, not fatal
            api.errors[name] = f"{type(e).__name__}: {e}\n" + traceback.format_exc(limit=2).strip().splitlines()[-1]
    api._current = ""
    return api
