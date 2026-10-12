"""Editor register delivery treats copied content as data and fails safely."""
from pathlib import Path
from types import SimpleNamespace
import os
import socket
import stat
import subprocess

import pytest

from raven import clipboard, editor_yank as E, toolbar, layout as L
from raven.config import Config


def executable(tmp_path, name):
    path = tmp_path / name
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o700)
    return str(path)


def target(tmp_path, kind="Vim"):
    return E.Target(kind, executable(tmp_path, "editor"), "SAFE")


def test_nvim_detects_owned_local_socket_and_resolved_executable(tmp_path, monkeypatch):
    path = str(tmp_path / "nvim.sock")
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(path)
        editor = executable(tmp_path, "nvim")
        monkeypatch.setattr(E.shutil, "which", lambda name: editor if name == "nvim" else None)
        found, message = E.discover({"NVIM": path})
        assert found == E.Target("Neovim", editor, path) and not message


@pytest.mark.parametrize("address", ["127.0.0.1:6666", "relative/socket", "/tmp/missing", "\x00bad", ""])
def test_nvim_rejects_network_relative_and_invalid_addresses(tmp_path, monkeypatch, address):
    editor = executable(tmp_path, "nvim")
    monkeypatch.setattr(E.shutil, "which", lambda name: editor if name == "nvim" else None)
    assert E.discover({"NVIM": address})[0] is None


def test_socket_symlink_and_regular_file_are_rejected(tmp_path):
    path = tmp_path / "socket"
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(str(path))
        link = tmp_path / "alias"
        link.symlink_to(path)
        assert not E._socket(str(link))
    file = tmp_path / "regular"
    file.write_text("x")
    assert not E._socket(str(file))


@pytest.mark.parametrize("servers, requested, expected", [
    (b"ONE\n", None, "ONE"), (b"ONE\nTWO\n", None, None),
    (b"ONE\nTWO\n", "TWO", "TWO"), (b"ONE\n", "OTHER", None),
    (b"", None, None), (b"\xff", None, None)])
def test_vim_requires_unambiguous_live_server(tmp_path, monkeypatch, servers, requested, expected):
    editor = executable(tmp_path, "vim")
    monkeypatch.setattr(E.shutil, "which", lambda name: editor if name == "vim" else None)
    seen = []
    monkeypatch.setattr(E.subprocess, "run", lambda argv, **kw: seen.append((argv, kw)) or SimpleNamespace(returncode=0, stdout=servers))
    found, message = E.discover({} if requested is None else {"RAVEN_VIM_SERVER": requested})
    assert (found.server if found else None) == expected
    assert seen[0][0] == [editor, "--serverlist"]
    assert seen[0][1]["timeout"] < 1


def test_delivery_only_sets_registers_and_never_embeds_payload_in_command(tmp_path, monkeypatch):
    content = "private ' \" | execute('quit!')\nπ\r\nlast\n"
    source = tmp_path / "source.txt"
    source.write_bytes(content.encode())
    captured = []
    def run(argv, **kw):
        captured.append((argv, kw))
        expression = argv[-1]
        assert content not in expression
        assert "setreg" in expression and "readfile" in expression
        temporary = expression.split("readfile('", 1)[1].split("'", 1)[0]
        assert Path(temporary).read_bytes() == content.encode()
        assert stat.S_IMODE(os.stat(temporary).st_mode) == 0o600
        return SimpleNamespace(returncode=0, stdout=b"0\n")
    monkeypatch.setattr(E.subprocess, "run", run)
    success, message = E.send_file(str(source), target=target(tmp_path))
    assert success and "registers 0 and unnamed" in message
    assert captured[0][0][1:4] == ["--servername", "SAFE", "--remote-expr"]
    assert captured[0][1]["timeout"] < 1
    temporary = captured[0][0][-1].split("readfile('", 1)[1].split("'", 1)[0]
    assert not Path(temporary).exists()
    assert source.read_bytes() == content.encode()


@pytest.mark.parametrize("payload", [b"\x00secret", b"\xff", b"x" * (E.MAX_BYTES + 1)])
def test_nonpreservable_payload_never_reaches_editor(tmp_path, monkeypatch, payload):
    source = tmp_path / "source"
    source.write_bytes(payload)
    monkeypatch.setattr(E.subprocess, "run", lambda *a, **kw: pytest.fail("editor saw invalid payload"))
    success, message = E.send_file(str(source), target=target(tmp_path))
    assert not success and "preserv" in message


def test_source_symlink_and_fifo_never_reach_editor(tmp_path, monkeypatch):
    source = tmp_path / "file"
    source.write_text("private")
    link = tmp_path / "link"
    link.symlink_to(source)
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    monkeypatch.setattr(E.subprocess, "run", lambda *a, **kw: pytest.fail("nonregular source reached editor"))
    editor = target(tmp_path)
    assert not E.send_file(str(link), target=editor)[0]
    assert not E.send_file(str(fifo), target=editor)[0]


@pytest.mark.parametrize("failure", ["timeout", "error-text", "exit"])
def test_editor_failure_is_bounded_and_cleans_private_payload(tmp_path, monkeypatch, failure):
    source = tmp_path / "source"
    source.write_text("secret")
    paths = []
    def run(argv, **kw):
        paths.append(argv[-1].split("readfile('", 1)[1].split("'", 1)[0])
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, kw["timeout"])
        return SimpleNamespace(returncode=1 if failure == "exit" else 0, stdout=b"E247: error")
    monkeypatch.setattr(E.subprocess, "run", run)
    assert not E.send_file(str(source), target=target(tmp_path))[0]
    assert all(not Path(path).exists() for path in paths)


def test_cancel_prevents_discovery_and_delivery(tmp_path, monkeypatch):
    monkeypatch.setattr(E, "discover", lambda *a, **kw: pytest.fail("cancelled delivery did discovery"))
    assert not E.send_file(str(tmp_path / "missing"), cancel=lambda: True)[0]


def test_clipboard_yank_success_preserves_export_and_skips_other_transports(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(E, "send_file", lambda path, **kw: seen.append(Path(path).read_text()) or (True, "Yanked complete selection into Vim registers."))
    monkeypatch.setattr(clipboard, "osc52", lambda *a: pytest.fail("successful yank also copied"))
    monkeypatch.setattr(clipboard, "local_tool", lambda *a: pytest.fail("successful yank also copied"))
    message = clipboard.copy("one\ntwo\n", str(tmp_path), destination="yank")
    assert seen == ["one\ntwo\n"] and "Yanked" in message
    assert (tmp_path / "clipboard.txt").read_text() == "one\ntwo\n"


def test_clipboard_yank_failure_keeps_copy_fallback_and_message(tmp_path, monkeypatch):
    monkeypatch.setattr(E, "send_file", lambda *a, **kw: (False, "No running editor; clipboard copying remains available."))
    captured = []
    monkeypatch.setattr(clipboard, "osc52", lambda text, tty: captured.append(text) or True)
    result = clipboard.copy("one\ntwo", str(tmp_path), destination="yank", use_tools=False)
    assert captured == ["one\ntwo"] and "copied 2 lines" in result and "No running editor" in result


def test_copy_file_yank_works_with_clipboard_transports_disabled(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.write_text("one\ntwo")
    monkeypatch.setattr(E, "send_file", lambda *a, **kw: (True, "Yanked complete selection into Neovim registers."))
    result = clipboard.copy_file(str(source), destination="yank", use_tools=False, use_osc52=False)
    assert result["methods"] == ["Yanked complete selection into Neovim registers."]


def test_clipboard_preferences_are_captured_independently_per_app():
    first = SimpleNamespace(cfg={"clipboard": {"destination": "yank", "osc52": False}})
    second = SimpleNamespace(cfg={"clipboard": {"destination": "copy", "tools": False}})
    before = clipboard.options(first)
    first.cfg["clipboard"]["destination"] = "copy"
    assert before == dict(use_osc52=False, use_tools=True, destination="yank")
    assert clipboard.options(second) == dict(use_osc52=True, use_tools=False, destination="copy")


def test_toolbar_switch_is_mouse_selectable_and_does_no_editor_probe(monkeypatch):
    app = SimpleNamespace(cfg={}, mode="main", tab="analytics", width=120, height=40, messages=[])
    app.say = app.messages.append
    monkeypatch.setattr(E, "discover", lambda *a: pytest.fail("toggle probed a process"))
    toolbar.render_bar(SimpleNamespace(g=L.Glyphs(False)), app, 120)
    hit = next(h for h in app.toolbar_state["hits"] if h[3] == "copy-mode")
    assert toolbar.handle_mouse(app, hit[0], hit[1], button="left")
    assert E.mode(app) == "yank"
    toolbar.render_bar(SimpleNamespace(g=L.Glyphs(False)), app, 120)
    assert any(c["id"] == "toolbar:copy-mode" for c in toolbar.control_descriptors(app))
    assert "Yank" in L.row_text(toolbar.render_bar(SimpleNamespace(g=L.Glyphs(False)), app, 120))


def test_real_config_toolbar_switch_uses_supported_config_api():
    app = SimpleNamespace(cfg=Config(), mode="main", tab="analytics", width=120, height=40, messages=[])
    app.say = app.messages.append
    toolbar.render_bar(SimpleNamespace(g=L.Glyphs(False)), app, 120)
    hit = next(h for h in app.toolbar_state["hits"] if h[3] == "copy-mode")
    assert toolbar.handle_mouse(app, hit[0], hit[1], button="left")
    assert app.cfg.get("clipboard.destination") == "yank"
    assert clipboard.options(app)["destination"] == "yank"
    assert toolbar.handle_mouse(app, hit[0], hit[1], button="left")
    assert app.cfg.get("clipboard.destination") == "copy"
