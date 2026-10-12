"""Demand-driven shell analysis modal; rendering performs no file or tool I/O."""
from __future__ import annotations

import threading

from . import layout as L, shell_checks
from .research import clean

MAX_DOCUMENT_ROWS = 8192


def initialize(app):
    state = getattr(app, "shell_checks_state", None)
    if not isinstance(state, dict):
        state = app.shell_checks_state = {"path": "", "expected": None, "result": None,
            "running": False, "error": "", "cancel": threading.Event(), "generation": 0,
            "callback": None, "scroll": 0, "page": 1, "count": 0, "focus": "rerun",
            "hits": [], "control_hits": [], "return_mode": "main", "document": None, "pressed": None}
    return state


def command_names():
    return ["shellcheck"]


def summary(app, plan=None):
    """A label for a captured plan; deliberately does not claim current disk state."""
    state = initialize(app)
    plan = plan if isinstance(plan, dict) else getattr(getattr(app, "research", None), "plan", None)
    if state["running"]:
        return "Shell checks: analyzing captured script"
    result = state["result"]
    if not result:
        return "Shell checks: not run (optional)"
    if isinstance(plan, dict) and (result.get("sha256") != plan.get("script_sha256") or result.get("path") != plan.get("script")):
        return "Shell checks: current plan has no matching checked snapshot"
    return "Shell snapshot: " + result["status"] + " / " + result.get("sha256", "")[:12]


def _cancel(app):
    state = initialize(app)
    state["cancel"].set()
    state["generation"] += 1
    hub = getattr(app, "research", None)
    detach = getattr(hub, "cancel_task", None)
    if state["callback"] is not None and callable(detach):
        detach(state["callback"])
    state.update(running=False, callback=None, pressed=None)


def close(app):
    _cancel(app)


def _leave(app):
    state = initialize(app)
    _cancel(app)
    app.mode = state["return_mode"]
    state["hits"] = []


def _start(app, path, expected=None, *, refresh=False):
    state = initialize(app)
    if state["running"]:
        app.fail("Shell checks are already running; Cancel stops the request.")
        return
    if getattr(getattr(app, "files", None), "remote", False) or getattr(getattr(getattr(app, "research", None), "files", None), "remote", False):
        app.fail("Shell checks use local files and tools. Run Raven on the cluster to check a remote script; SSH profile paths are not read locally.")
        return
    if getattr(app, "replay", None):
        app.fail("Shell checks are unavailable during replay; recorded paths may refer to another system.")
        return
    hub = getattr(app, "research", None)
    if hub is None:
        app.fail("The shell-check worker is unavailable.")
        return
    event = threading.Event()
    generation = state["generation"] + 1

    def work():
        return shell_checks.check_script(path, expected_sha256=expected, cancel=event.is_set, refresh=refresh)

    def done(value):
        if state["generation"] != generation or event.is_set():
            return
        state.update(running=False, callback=None, document=None)
        if isinstance(value, Exception):
            state.update(error=clean(value), result=None)
        else:
            state.update(result=value, error="")
        if not getattr(app, "interactive", True):
            app.research_result = value if not isinstance(value, Exception) else {"status": "error", "summary": clean(value)}
            app.command_ok = not isinstance(value, Exception) and value.get("status") in {"checked", "findings", "partial"}
        # A late publication never enters a tab, opens a modal, or steals focus.
        if getattr(app, "mode", "main") == "shell_checks":
            if state["error"]:
                app.fail("Shell checks: " + state["error"])
            else:
                app.say(summary(app))

    interactive = getattr(app, "interactive", True)
    if interactive and not hub.start_task(work, done):
        app.fail("The research worker is busy; rerun shell checks when it finishes.")
        return
    if getattr(app, "mode", "main") != "shell_checks":
        state["return_mode"] = "execution" if getattr(app, "mode", "main") == "execution" else "main"
    state.update(path=path, expected=expected, running=True, error="", result=None,
                 generation=generation, cancel=event, callback=done, document=None,
                 scroll=0, focus="rerun", hits=[], pressed=None)
    app.mode = "shell_checks"
    app.say("Analyzing a local script snapshot in the background; the script is never executed.")
    if not interactive:
        try:
            done(work())
        except Exception as exc:
            done(exc)


def run_command(app, args):
    if not args or args[0] != "shellcheck":
        return False
    state = initialize(app)
    if len(args) > 2:
        app.fail("Use :shellcheck [SCRIPT] or :shellcheck refresh. Quote paths that contain spaces.")
        return True
    if len(args) == 2 and args[1] == "refresh":
        if state["path"]:
            _start(app, state["path"], state["expected"], refresh=True)
            return True
        args = args[:1]
    if len(args) == 2:
        _start(app, args[1])
    else:
        state_form = getattr(app, "execution_state", {})
        plan = state_form.get("plan") if getattr(app, "mode", "main") == "execution" else getattr(getattr(app, "research", None), "plan", None)
        open_for_plan(app, plan)
    return True


def open_for_plan(app, plan):
    """Open checks for an explicitly selected immutable submission snapshot."""
    if not isinstance(plan, dict) or not isinstance(plan.get("script"), str) or not plan.get("script_sha256"):
        app.fail("Prepare or validate a submission plan first, or use :shellcheck SCRIPT.")
        return
    _start(app, plan["script"], plan["script_sha256"])


def _scroll(app, value):
    state = initialize(app)
    from .scrollbars import resume
    resume(app, "modal:shell-checks")
    state["scroll"] = max(0, min(max(0, state["count"] - state["page"]), value))


def handle_key(app, key):
    if getattr(app, "mode", None) != "shell_checks":
        return False
    state = initialize(app)
    if key in {"esc", "q"}:
        _leave(app)
    elif key in {"tab", "btab", "left", "right"}:
        state["focus"] = "back" if state["focus"] == "rerun" else "rerun"
    elif key in {"up", "k", "down", "j", "pgup", "pgdn", "home", "end"}:
        step = state["page"] if key in {"pgup", "pgdn"} else 1
        value = 0 if key == "home" else state["count"] if key == "end" else state["scroll"] + (step if key in {"down", "j", "pgdn"} else -step)
        _scroll(app, value)
    elif key == "c" and state["running"]:
        _cancel(app)
        state.update(error="Analysis cancelled. Rerun to inspect the script again.", document=None)
    elif key == "r":
        if not state["running"]:
            _start(app, state["path"], state["expected"], refresh=True)
    elif key in {"enter", "space"} and state["focus"] == "rerun":
        if state.get("hit_generation") != (state["generation"], state["running"]):
            return True  # a changed action must be visible before activation
        if state["running"]:
            handle_key(app, "c")
        else:
            _start(app, state["path"], state["expected"], refresh=True)
    elif key in {"enter", "space"} and state["focus"] == "back":
        _leave(app)
    return True


def _document(state, width, ascii_):
    token = (id(state["result"]), state["error"], state["running"], width, ascii_)
    if state["document"] and state["document"][0] == token:
        return state["document"][1]
    from .execution_ui import _wrap
    result = state["result"] or {}
    texts = [("Local file: " + clean(state["path"]), "accent"),
             ("Scope: this script only; dependencies and runtime behavior are not validated.", "muted")]
    if state["running"]:
        texts.append(("Checking captured bytes... Cancel stops the analyzer; Esc returns.", "accent"))
    elif state["error"]:
        texts.append((state["error"], "error"))
    elif result:
        texts += [("Result: " + result["status"] + (" (cached)" if result.get("cached") else ""), "accent+bold"),
                  ("SHA256: " + result.get("sha256", ""), "muted")]
        for check in result.get("checks", []):
            texts.append((check["tool"] + ": " + check["status"] + (" / " + check["detail"] if check["detail"] else ""),
                          "warning" if check["status"] in {"unavailable", "failed"} else "text"))
        for tool in result.get("tools", []):
            texts.append((tool["name"] + ": " + clean(tool["path"] or "not installed") + " / " + tool["version"], "muted"))
        texts.append((result.get("note", ""), "muted"))
        findings = result.get("findings", [])
        texts.append((f"Findings: {len(findings)}", "accent+bold"))
        for item in findings:
            location = f"line {item['line']}:{item['column']}" if item["line"] else "general"
            label = f"{location}  {item['level'].upper()}  {item['code'] or item['tool']}"
            style = "error" if item["level"] == "error" else "warning" if item["level"] == "warning" else "text"
            texts.append((label + "  " + item["message"], style))
            if item.get("excerpt"):
                texts.append(("  | " + item["excerpt"], "muted"))
    rows = []
    if width < 8:
        rows = [[(" Widen the terminal to inspect shell-check evidence.", "warning")]]
    else:
        for text, style in texts:
            for part in _wrap(text, max(1, width - 1), ascii_=ascii_):
                rows.append([(" " + part, style)])
                if len(rows) >= MAX_DOCUMENT_ROWS:
                    rows[-1] = [(" More evidence is available at a wider terminal width or through :shellcheck in --run output.", "warning")]
                    break
            if len(rows) >= MAX_DOCUMENT_ROWS:
                break
    state["document"] = token, rows
    return rows


def overlay(views, snap, app, width, height):
    if getattr(app, "mode", None) != "shell_checks":
        return None
    state = initialize(app)
    state["hits"] = []
    state["control_hits"] = []
    state["screen"] = (width, height)
    from . import modal_scrollbars as B
    rows = _document(state, max(1, width - 8), views.g.ascii)
    from .control_rows import buttons, place_hits
    controls, control_hits = buttons(views.g, max(1, width - 8),
        [("rerun", "[Cancel]" if state["running"] else "[Rerun]", ("key", "c" if state["running"] else "r")),
         ("back", "[Back]", ("key", "esc"))], selected=state["focus"], group="shell-checks", prefix="shell-checks:")
    page = max(1, height - 6 - len(controls))
    state.update(page=page, count=len(rows))
    context = (state["generation"], id(state["result"]), width, height)
    target, painted = B.window(app, "modal:shell-checks", state["scroll"], len(rows), page, context=context)
    state["scroll"] = target
    content = rows[painted:painted + page]
    content.append([(" Arrows / PgUp / PgDn scroll; r reruns; Esc back", "muted")])
    control_hits = [(row + len(content), kind, value) for row, kind, value in control_hits]
    content += controls
    rendered = L.box(views.g, content, width, height, "Shell checks", min_width=30)
    rendered = B.boxed(app, "modal:shell-checks", rendered, start=0, count=len(rows), page=min(page, len(rows)),
                       target=target, painted=painted, setter=lambda value: state.update(scroll=value), context=context, header=-1)
    state["control_hits"] = place_hits(control_hits, rendered[1:-1])
    state["hits"] = [(y, value["left"], value["right"], value["id"].rsplit(":", 1)[-1]) for y, _, value in state["control_hits"]]
    state["hit_generation"] = state["generation"], state["running"]
    return rendered


def handle_mouse(app, y, x, button="left", shift=False):
    if getattr(app, "mode", None) != "shell_checks":
        return False
    state = initialize(app)
    if state.get("hit_generation") != (state["generation"], state["running"]):
        return True
    if button in {"wheel-up", "wheel_up", "wheel-down", "wheel_down"}:
        handle_key(app, "up" if button in {"wheel-up", "wheel_up"} else "down")
        return True
    hit = next((item[3] for item in state["hits"] if item[0] == y and item[1] <= x < item[2]), None)
    if button in {"motion", "drag"}:
        if hit:
            state["focus"] = hit
    elif button == "press":
        state["pressed"] = hit
    elif button == "release":
        pressed = state["pressed"]
        state["pressed"] = None
        if hit and hit == pressed:
            state["focus"] = hit
            handle_key(app, "enter")
    elif button in {"left", "double"} and hit:
        state["focus"] = hit
        handle_key(app, "enter")
    # Every modal event is consumed, including right clicks and inert motion.
    return True
