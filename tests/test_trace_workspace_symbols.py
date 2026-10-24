"""Build identity, source proof, cache bounds and actual ELF resolution."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess

import pytest

from raven.tracing import workspace_symbols as S


def fake_tools(
    monkeypatch,
    tmp_path,
    *,
    notes="Build ID: aabbccdd",
    resolved="solve\n/project/solver.c:3\n",
):
    binary = tmp_path / "explicit-selected.elf"
    binary.write_bytes(b"ELF test data")
    calls = []

    def run(argv, timeout, limit):
        calls.append(argv)
        assert timeout <= 2 and limit <= 128 << 10
        return notes if "--notes" in argv else resolved

    monkeypatch.setattr(S.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(S, "_process", run)
    return binary, calls


def symbol(**kwargs):
    return dict(name="recorded label", build_id="aabbccdd", elf_address=4096, **kwargs)


def test_only_explicit_build_checked_binary_is_resolved_and_cached(
    monkeypatch, tmp_path
):
    binary, calls = fake_tools(monkeypatch, tmp_path)
    cache = tmp_path / "private-cache"
    value = symbol(dso="/tmp/trace-supplied-executable", command="do not run me")
    result = S.resolve_symbol(value)
    assert result["status"] == "unavailable" and "explicitly" in result["reason"]
    assert not calls
    first = S.resolve_symbol(value, binary=binary, cache_directory=cache)
    assert first["status"] == "resolved" and first["frames"][0]["name"] == "solve"
    assert not first["cache_hit"]
    assert first["source"]["status"] == "unavailable"
    assert len(calls) == 2 and str(binary) in calls[0]
    assert not any(value["dso"] in argv for argv in calls)
    second = S.resolve_symbol(value, binary=binary, cache_directory=cache)
    assert second["cache_hit"] and len(calls) == 3  # Recheck ELF proof each time.
    assert cache.stat().st_mode & 0o777 == 0o700
    assert next(cache.iterdir()).stat().st_mode & 0o777 == 0o600


def test_wrong_binary_no_address_tool_or_source_is_used(monkeypatch, tmp_path):
    binary, calls = fake_tools(monkeypatch, tmp_path, notes="Build ID: deadbeef")
    result = S.resolve_symbol(symbol(), binary=binary, source_root=tmp_path)
    assert result["status"] == "unavailable" and "does not match" in result["reason"]
    assert len(calls) == 1


def test_source_excerpt_requires_content_proof_and_root_containment(
    monkeypatch, tmp_path
):
    binary, _ = fake_tools(monkeypatch, tmp_path)
    source = tmp_path / "solver.c"
    raw = b"// header\nint solve(void) {\n    return 42;\n}\n"
    source.write_bytes(raw)
    value = symbol(
        source_relative_path="solver.c",
        source_sha256=hashlib.sha256(raw).hexdigest(),
        line=3,
    )
    result = S.resolve_symbol(value, binary=binary, source_root=tmp_path)
    assert result["source"]["status"] == "verified"
    assert result["source"]["proof"]["method"] == "recorded-sha256"
    assert result["source"]["lines"][2] == dict(line=3, text="    return 42;")
    source.write_bytes(b"wrong revision\n")
    changed = S.resolve_symbol(value, binary=binary, source_root=tmp_path)
    assert changed["source"]["status"] == "unavailable"
    assert "does not match" in changed["source"]["reason"]
    outside = tmp_path.parent / (tmp_path.name + "-outside.c")
    outside.write_bytes(raw)
    try:
        value.update(source_relative_path=str(outside))
        result = S.resolve_symbol(value, binary=binary, source_root=tmp_path)
        assert result["source"]["status"] == "unavailable"
        assert "outside" in result["source"]["reason"]
    finally:
        outside.unlink()


def test_missing_symbols_and_unsafe_relocation_remain_unavailable(
    monkeypatch, tmp_path
):
    binary, calls = fake_tools(monkeypatch, tmp_path, resolved="??\n??:0\n")
    no_address = dict(build_id="aabbccdd", ip="0xffff1234")
    result = S.resolve_symbol(no_address, binary=binary)
    assert result["status"] == "unavailable" and "relocation" in result["reason"]
    assert not calls
    no_address.update(load_bias="0xffff0000")
    result = S.resolve_symbol(no_address, binary=binary)
    assert result["status"] == "unresolved" and result["elf_address"] == 0x1234
    assert "no symbol/debug mapping" in result["reason"]
    value = dict(build_id="aabbccdd", module_offset=0x12)
    assert "module_base_vaddr" in S.resolve_symbol(value, binary=binary)["reason"]
    value["module_base_vaddr"] = 0x4000
    assert S.resolve_symbol(value, binary=binary)["elf_address"] == 0x4012
    bare_hex = dict(build_id="aabbccdd", ip="7fa01234", load_bias="0x7fa00000")
    assert S.resolve_symbol(bare_hex, binary=binary)["elf_address"] == 0x1234


def test_tool_output_is_bounded_and_terminal_controls_are_safe(monkeypatch, tmp_path):
    binary, _ = fake_tools(
        monkeypatch, tmp_path, resolved="\x1b[31msolve\x1b[0m\n/tmp/bad\x07.c:7\n"
    )
    result = S.resolve_symbol(symbol(), binary=binary)
    assert result["frames"][0]["name"] == "solve"
    assert "\x07" not in result["frames"][0]["file"]
    monkeypatch.setattr(
        S,
        "_process",
        lambda *args: (_ for _ in ()).throw(
            S.CommandError("Command output exceeded the inspection limit.")
        ),
    )
    result = S.resolve_symbol(symbol(), binary=binary)
    assert result["status"] == "unavailable" and "inspection limit" in result["reason"]


def test_cache_symlink_and_nonprivate_directory_are_ignored(monkeypatch, tmp_path):
    binary, calls = fake_tools(monkeypatch, tmp_path)
    public = tmp_path / "public"
    public.mkdir(mode=0o755)
    public.chmod(0o755)
    for _ in range(2):
        result = S.resolve_symbol(symbol(), binary=binary, cache_directory=public)
        assert not result["cache_hit"]
    assert len(calls) == 4 and not list(public.iterdir())
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    link = tmp_path / "symlink"
    link.symlink_to(private, target_is_directory=True)
    assert S._cache_root(link) is None
    S.resolve_symbol(symbol(), binary=binary, cache_directory=private)
    item = next(private.iterdir())
    item.unlink()
    external = tmp_path / "external.json"
    external.write_text(json.dumps(dict(schema=S.SCHEMA, frames=[])))
    item.symlink_to(external)
    assert not S.resolve_symbol(symbol(), binary=binary, cache_directory=private)[
        "cache_hit"
    ]
    assert external.exists()


@pytest.mark.skipif(
    not all(
        shutil.which(name) for name in ("gcc", "readelf", "addr2line", "nm", "git")
    ),
    reason="Actual ELF/source qualification needs GCC binutils and Git",
)
def test_real_elf_build_id_demangled_function_and_git_source_proof(tmp_path):
    source = tmp_path / "solver.c"
    source.write_text(
        "__attribute__((noinline)) int solve(int x) { return x * 7; }\nint main(void) { return solve(2); }\n"
    )
    binary = tmp_path / "solver"
    subprocess.run(
        ["gcc", "-g", "-O0", "-Wl,--build-id=sha1", "-o", str(binary), str(source)],
        check=True,
    )
    notes = subprocess.check_output(["readelf", "--notes", str(binary)], text=True)
    build = re.search(r"Build ID:\s*([0-9a-f]+)", notes)[1]
    names = subprocess.check_output(["nm", "-n", str(binary)], text=True)
    address = int(re.search(r"^([0-9a-f]+) T solve$", names, re.MULTILINE)[1], 16)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "solver.c"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    revision = subprocess.check_output(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"], text=True
    ).strip()
    blob = subprocess.check_output(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD:solver.c"], text=True
    ).strip()
    value = dict(
        build_id=build,
        elf_address=address,
        source_relative_path="solver.c",
        line=1,
        source_revision=revision,
        source_git_blob=blob,
    )
    result = S.resolve_symbol(
        value, binary=binary, source_root=tmp_path, cache_directory=tmp_path / "cache"
    )
    assert result["status"] == "resolved", result
    assert result["frames"][0]["name"] == "solve" and result["frames"][0]["line"] == 1
    assert result["source"]["status"] == "verified", result
    assert result["source"]["proof"]["method"] == "recorded-git-blob-and-revision"
    source.write_text("modified\n")
    assert (
        S.resolve_symbol(value, binary=binary, source_root=tmp_path)["source"]["status"]
        == "unavailable"
    )


@pytest.mark.parametrize("address", [-1, 1 << 64, "0xnothex", True, "1; rm -rf"])
def test_addresses_are_not_commands(monkeypatch, tmp_path, address):
    binary, calls = fake_tools(monkeypatch, tmp_path)
    result = S.resolve_symbol(
        dict(build_id="aabbccdd", elf_address=address), binary=binary
    )
    assert result["status"] == "unavailable" and not calls
