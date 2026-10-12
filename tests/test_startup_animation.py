"""The welcome is an optional presentation, never an admission or input gate."""
from __future__ import annotations

import builtins
import concurrent.futures
from types import SimpleNamespace

import pytest

from raven import layout as L
from raven import startup
from raven.config import Config


def app(*, interactive=True, theme="default", ascii_=False, **config):
    cfg = Config(dict(config, ascii=ascii_))
    result = SimpleNamespace(cfg=cfg, interactive=interactive, theme=theme,
                             _ascii_cfg=ascii_, views_ref=None, messages=[], failures=[],
                             mode="main", tab="jobs", sampler=object(), research=object())
    result.say = result.messages.append
    result.fail = result.failures.append
    return result


def views(ascii_=False):
    return SimpleNamespace(g=L.Glyphs(ascii_))


@pytest.fixture
def clock(monkeypatch):
    value = [100.0]
    monkeypatch.setattr(startup.time, "monotonic", lambda: value[0])
    return value


def text(rows):
    return "\n".join(L.row_text(row) for _, _, row in rows or [])


def test_only_explicit_begin_admits_startup(clock):
    target = app()
    startup.initialize(target)
    assert not startup.active(target)
    assert startup.overlay(views(), {}, target, 100, 30) is None
    assert not target.startup_state["begun"]
    assert startup.begin(target)
    assert startup.active(target)
    assert target.startup_state["start"] == 100
    assert startup.overlay(views(), {}, target, 100, 30)


def test_deadline_does_not_restart_on_tick_or_repeated_begin(clock):
    target = app()
    assert startup.begin(target)
    clock[0] += .4
    startup.tick(target)
    assert startup.active(target)
    assert not startup.begin(target)
    assert target.startup_state["start"] == 100
    clock[0] = 100 + startup.DURATION
    assert not startup.active(target)
    assert startup.overlay(views(), {}, target, 100, 30) is None
    assert not startup.begin(target)


def test_persisted_disabled_preference_is_restored_before_launch(clock):
    source = app()
    assert startup.run_command(source, ["startup", "off"])
    saved = startup.save(source)
    assert saved == {"enabled": False}
    target = app()
    startup.restore(target, saved)
    assert target.cfg.get("startup_animation") is False
    assert not startup.begin(target)
    assert startup.overlay(views(), {}, target, 100, 30) is None
    assert target.startup_state["begun"]


def test_preview_replays_without_changing_disabled_launch_preference(clock):
    target = app(startup_animation=False)
    assert not startup.begin(target)
    assert startup.run_command(target, ["startup", "preview"])
    assert startup.active(target)
    assert startup.save(target) == {"enabled": False}
    clock[0] += startup.DURATION
    assert not startup.active(target)
    assert startup.run_command(target, ["startup", "on"])
    assert startup.save(target) == {"enabled": True}
    assert not startup.active(target)  # Enabling affects the next launch.


@pytest.mark.parametrize("interactive,theme,animation", [(False, "default", True),
                                                        (True, "reader", True),
                                                        (True, "default", False)])
def test_headless_and_reduced_motion_never_admit_even_preview(clock, interactive, theme, animation):
    target = app(interactive=interactive, theme=theme, animations=animation)
    assert not startup.begin(target)
    assert not startup.begin(target, preview=True)
    assert startup.run_command(target, ["startup", "preview"])
    assert not startup.active(target)
    assert "interactive terminal" in target.messages[-1]


@pytest.mark.parametrize("setting,value", [("animations", False), ("startup_animation", False)])
def test_disabling_preference_dismisses_inflight_welcome(clock, setting, value):
    target = app()
    assert startup.begin(target)
    if setting == "startup_animation":
        startup.run_command(target, ["startup", "off"])
    else:
        target.cfg.set(setting, value)
    assert not startup.active(target)


def test_reader_theme_switch_dismisses_immediately(clock):
    target = app()
    assert startup.begin(target)
    target.theme = "reader"
    assert startup.overlay(views(True), {}, target, 100, 30) is None


@pytest.mark.parametrize("key", ["q", "f10", "up", "down", "left", "right", ":", "esc", "enter", "ctrl-c"])
def test_first_key_dismisses_without_consuming_its_action(clock, key):
    target = app()
    assert startup.begin(target)
    assert startup.handle_key(target, key) is False
    assert not startup.active(target)


@pytest.mark.parametrize("button", ["left", "right", "middle", "wheel-up", "wheel-down", "drag"])
def test_deliberate_mouse_input_dismisses_without_consuming_its_action(clock, button):
    target = app()
    assert startup.begin(target)
    assert startup.handle_mouse(target, 4, 8, button=button) is False
    assert not startup.active(target)


@pytest.mark.parametrize("button", ["move", "motion", "hover", "release"])
def test_hover_does_not_interrupt_welcome(clock, button):
    target = app()
    assert startup.begin(target)
    assert startup.handle_mouse(target, 4, 8, button=button) is False
    assert startup.active(target)


@pytest.mark.parametrize("width,height", [(0, 0), (1, 1), (23, 40), (120, 5)])
def test_tiny_terminal_skips_without_clipping_global_toolbar(clock, width, height):
    target = app()
    assert startup.begin(target)
    assert startup.overlay(views(), {}, target, width, height) is None
    assert not startup.active(target)


@pytest.mark.parametrize("width,height", [(24, 6), (24, 12), (32, 8), (35, 12),
                                         (40, 20), (80, 24), (120, 40), (200, 60)])
@pytest.mark.parametrize("theme", ["default", "mono", "high", "cb", "dark", "light", "terminal"])
@pytest.mark.parametrize("ascii_", [False, True])
def test_every_frame_fits_body_and_keeps_toolbar_row_zero(clock, width, height, theme, ascii_):
    target = app(theme=theme, ascii_=ascii_, color=theme != "mono")
    assert startup.begin(target)
    for fraction in (0, .2, .5, .9):
        clock[0] = 100 + target.startup_state["duration"] * fraction
        rows = startup.overlay(views(ascii_), {}, target, width, height)
        assert rows
        for y, x, row in rows:
            assert 1 <= y < height
            assert 0 <= x < width
            assert L.vlen(L.row_text(row)) + x <= width
            assert all("\x1b" not in segment for segment, _ in row)
        if ascii_:
            assert text(rows).isascii()
        assert "RAVEN" in text(rows) or "█████" in text(rows)


def test_ascii_welcome_is_static_even_if_view_glyphs_are_not_yet_updated(clock):
    target = app(ascii_=True)
    assert startup.begin(target)
    first = startup.overlay(views(), {}, target, 80, 24)
    assert text(first).isascii()
    assert "RAVEN" in text(first)
    assert target.startup_state["duration"] == startup.ASCII_DURATION
    clock[0] += .3
    assert first == startup.overlay(views(), {}, target, 80, 24)


def test_unicode_sweep_changes_styles_without_fake_loading_progress(clock):
    target = app()
    assert startup.begin(target)
    first = startup.overlay(views(), {}, target, 100, 30)
    clock[0] += .5
    middle = startup.overlay(views(), {}, target, 100, 30)
    assert text(first) == text(middle)
    assert first != middle
    assert "█████" in text(first)
    assert "%" not in text(first)
    assert "Loading" not in text(first)


def test_welcome_never_reads_files_sleeps_or_admits_background_work(clock, monkeypatch):
    target = app()
    pending = object()
    target.research = SimpleNamespace(pending=pending, generation=77)
    target.sampler = SimpleNamespace(last_run={"jobs": 100}, kick=object())
    def forbidden(*args, **kwargs):
        pytest.fail("Welcome performed IO, waited, or created background work.")
    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(concurrent.futures, "ThreadPoolExecutor", forbidden)
    monkeypatch.setattr(startup.time, "sleep", forbidden)
    assert startup.begin(target)
    for _ in range(10):
        startup.tick(target)
        assert startup.overlay(views(), {}, target, 100, 30)
    assert target.research.pending is pending
    assert target.research.generation == 77
    assert target.sampler.last_run == {"jobs": 100}


@pytest.mark.parametrize("data", [None, [], True, {"enabled": "false"}, {"enabled": 0},
                                  {"enabled": 1}, {"enabled": None}])
def test_invalid_persisted_preference_does_not_change_the_default(data):
    target = app()
    startup.restore(target, data)
    assert startup.enabled(target) is True
    assert startup.save(target) == {"enabled": True}


@pytest.mark.parametrize("args", [["startup", "bogus"], ["startup", "on", "now"],
                                  ["startup", "ON"]])
def test_invalid_command_is_reviewable_and_does_not_change_preference(args):
    target = app()
    assert startup.run_command(target, args)
    assert startup.save(target) == {"enabled": True}
    assert target.failures == ["Use startup [on|off|toggle|preview]."]


def test_status_toggle_and_unknown_command():
    target = app()
    assert startup.run_command(target, ["startup"])
    assert target.messages[-1] == "Startup welcome on."
    assert startup.run_command(target, ["startup", "toggle"])
    assert target.messages[-1] == "Startup welcome off."
    assert target.cfg.get("startup_animation") is False
    assert not startup.run_command(target, ["other"])
    assert not startup.run_command(target, [])


@pytest.mark.parametrize("now", [True, "100", None, float("inf"), float("nan"), 10**1000])
def test_clock_rejects_invalid_explicit_values(now, monkeypatch):
    if now is None:
        monkeypatch.setattr(startup.time, "monotonic", lambda: float("inf"))
    with pytest.raises(ValueError, match="clock"):
        startup.begin(app(), now=now)


def test_saved_frame_never_restores_or_resumes(clock):
    source = app()
    assert startup.begin(source)
    target = app()
    startup.restore(target, dict(startup.save(source), begun=True, running=True, start=100, preview=True))
    assert not startup.active(target)
    assert target.startup_state["begun"] is False
    assert startup.begin(target)


def test_new_boolean_config_defaults_and_profile_override():
    cfg = Config({"profiles": {"quiet": {"startup_animation": False, "smooth_scrolling": False}}})
    assert cfg.get("startup_animation") is True
    assert cfg.get("smooth_scrolling") is True
    quiet = cfg.profile("quiet")
    assert quiet.get("startup_animation") is False
    assert quiet.get("smooth_scrolling") is False
