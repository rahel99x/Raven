"""Terminal capabilities and command-line preferences must agree with the alias."""
from types import SimpleNamespace

from raven import cli
from raven.config import Config
from raven.controller import App
from raven.model import Store
from raven.views import Views
from raven.layout import Glyphs


def test_glyph_preferences_override_old_config_and_alias_in_argument_order(monkeypatch):
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setattr(cli.sys, "stdout", SimpleNamespace(encoding="utf-8"))
    cfg = Config()
    assert not cli.ascii_mode(cli.parse([]), cfg)
    cfg.set("ascii", True)
    assert cli.ascii_mode(cli.parse([]), cfg)
    assert not cli.ascii_mode(cli.parse(["--unicode"]), cfg)
    assert cli.ascii_mode(cli.parse(["--unicode", "--ascii"]), cfg)
    assert not cli.ascii_mode(cli.parse(["--ascii", "--unicode"]), cfg)
    assert cli.parse(["--unicode", "run", "refresh", "--ascii"]).ascii


def test_dumb_terminal_and_unencodable_output_fallback(monkeypatch):
    cfg = Config()
    monkeypatch.setenv("TERM", "dumb")
    monkeypatch.setattr(cli.sys, "stdout", SimpleNamespace(encoding="utf-8"))
    assert cli.ascii_mode(cli.parse([]), cfg)
    assert not cli.ascii_mode(cli.parse(["--unicode"]), cfg)
    monkeypatch.setattr(cli.sys, "stdout", SimpleNamespace(encoding="ascii"))
    assert cli.ascii_mode(cli.parse(["--unicode"]), cfg)
    cfg.set("theme", "reader")
    monkeypatch.setattr(cli.sys, "stdout", SimpleNamespace(encoding="utf-8"))
    assert cli.ascii_mode(cli.parse(["--unicode"]), cfg)


def test_reader_theme_changes_navigation_glyphs_and_restores_blocks():
    cfg = Config()
    app = App(Store(persist=False), None, None, cfg, "scientist", ascii_=False)
    views = Views(Glyphs(False), cfg)
    app.views_ref = views
    app.set_theme("reader")
    assert views.g.ascii and app.keys_help("up") == "Up/k"
    app.set_theme("default")
    assert not views.g.ascii and app.keys_help("up") == "↑/k"
