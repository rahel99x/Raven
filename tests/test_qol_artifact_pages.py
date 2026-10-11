from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from tower import artifact_pages as A, layout as L, project_ui as U
from tower.remote import LocalFiles


def read(root, name="output.txt", **kwargs):
    format_ = kwargs.pop("format_", "text")
    return A.read_page(str(root), dict(path=name, format=format_), **kwargs)


def test_text_pages_cover_all_source_lines_without_duplicates(tmp_path):
    expected = [f"line {i} 界" for i in range(400)]
    (tmp_path / "output.txt").write_text("\n".join(expected) + "\n")
    result = []; offset = row = 0
    while True:
        page = read(tmp_path, offset=offset, row=row)
        result.extend(page["lines"])
        assert page["bytes_read"] <= A.PAGE_BYTES + 3
        if not page["has_next"]:
            break
        assert page["next_offset"] > offset
        offset, row = page["next_offset"], page["next_row"]
    assert result == expected and row == 384


def test_large_line_continuations_keep_utf8_and_exact_byte_adjacency(tmp_path):
    value = "界" * 100000 + "\nnext\n"
    path = tmp_path / "output.txt"; path.write_text(value)
    first = read(tmp_path)
    second = read(tmp_path, offset=first["next_offset"], row=first["next_row"])
    assert first["next_offset"] % 3 == 0
    assert first["lines"][0] + second["lines"][0] == "界" * 100000
    assert second["lines"][1] == "next"


def test_csv_quoted_newlines_and_header_remain_across_adjacent_pages(tmp_path):
    path = tmp_path / "output.csv"
    path.write_text('id,message\n' + "".join(f'{i},"hello\nworld {i}"\n' for i in range(300)))
    pages = []; offset = row = 0
    while True:
        page = read(tmp_path, path.name, format_="csv", offset=offset, row=row)
        assert page["header"] == ["id", "message"] and page["lines"][0] == "id | message"
        pages.extend(page["records"])
        if not page["has_next"]:
            break
        offset, row = page["next_offset"], page["next_row"]
    assert len(pages) == 300 and [int(values[0]) for _, values in pages] == list(range(300))
    assert all("\n" in values[1] for _, values in pages)


def test_csv_long_columns_and_global_numeric_sort_before_pagination(tmp_path):
    values = list(range(300, 0, -1))
    path = tmp_path / "output.csv"
    path.write_text("id,long\n" + "".join(f"{i},{'x' * 1000}\n" for i in values))
    first = read(tmp_path, path.name, format_="csv", sort=(0, "asc"), columns=[0])
    second = read(tmp_path, path.name, format_="csv", row=first["next_row"], offset=first["next_offset"], sort=(0, "asc"), columns=[0])
    assert first["lines"][0] == "id" and first["lines"][1] == "1"
    assert first["lines"][-1] == "128" and second["lines"][1] == "129"
    assert "global" in first["summary"]
    assert first["records"][0][0] == 299


def test_csv_sort_refuses_incomplete_dataset_above_budget(tmp_path):
    path = tmp_path / "output.csv"
    with path.open("wb") as stream:
        stream.write(b"id\n")
        stream.truncate(A.SORT_BYTES + 1)
    with pytest.raises(ValueError, match="Global CSV sorting"):
        read(tmp_path, path.name, format_="csv", sort=(0, "asc"))


@pytest.mark.parametrize("bad", [[], [20], [True], [-1]])
def test_column_selection_rejects_invalid_or_empty_sets(tmp_path, bad):
    (tmp_path / "output.csv").write_text("id,value\n1,2\n")
    with pytest.raises(ValueError, match="columns"):
        read(tmp_path, "output.csv", format_="csv", columns=bad)


def test_json_expandable_pointer_preserves_declared_tree(tmp_path):
    (tmp_path / "output.json").write_text(json.dumps({"worker": {"rank": 2}, "rows": [1, 2]}))
    open_ = read(tmp_path, "output.json", format_="json")
    folded = read(tmp_path, "output.json", format_="json", collapsed=["/worker"])
    assert any(node["node"] == "/worker/rank" for node in open_["nodes"])
    assert not any(node["node"] == "/worker/rank" for node in folded["nodes"])
    assert any("{...}" in node["text"] for node in folded["nodes"])


def test_large_json_tree_pages_cover_nodes_beyond_first_display_window(tmp_path):
    (tmp_path / "output.json").write_text(json.dumps({str(i): i for i in range(5000)}))
    row = 0
    observed = []
    while True:
        page = read(tmp_path, "output.json", format_="json", row=row)
        observed.extend(node["node"] for node in page["nodes"])
        if not page["has_next"]:
            break
        row = page["next_row"]
    assert len(observed) == 5001 and observed[0] == "" and observed[-1] == "/4999"
    assert len(set(observed)) == len(observed)


@pytest.mark.parametrize("kind", ["symlink", "parent_symlink", "fifo", "directory"])
def test_declared_reader_refuses_special_paths_without_leaking(tmp_path, kind):
    secret = tmp_path / "secret"; secret.write_text("private sentinel")
    root = tmp_path / "root"; root.mkdir()
    name = "output.txt"
    if kind == "symlink": (root / name).symlink_to(secret)
    elif kind == "parent_symlink":
        (root / "parent").symlink_to(tmp_path, target_is_directory=True); name = "parent/secret"
    elif kind == "fifo": os.mkfifo(root / name)
    else: (root / name).mkdir()
    with pytest.raises((ValueError, OSError)):
        read(root, name)


def test_mutation_during_page_read_discards_snapshot(tmp_path, monkeypatch):
    path = tmp_path / "output.txt"; path.write_text("original\n")
    original = A._read
    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        path.write_text("private replacement\n")
        return result
    monkeypatch.setattr(A, "_read", changed)
    with pytest.raises(ValueError, match="changed"):
        read(tmp_path)


def test_remote_adapter_never_falls_back_to_local_artifact(tmp_path):
    (tmp_path / "output.txt").write_text("private sentinel")
    with pytest.raises(ValueError, match="unsupported SSH project reader"):
        read(tmp_path, files=SimpleNamespace(remote=True))


class Hub:
    files = LocalFiles()
    settings = {}
    def __init__(self): self.pending = None
    def start_task(self, worker, done):
        self.pending = worker, done; return True
    def finish(self):
        worker, done = self.pending; self.pending = None
        try: result = worker()
        except Exception as exc: result = exc
        done(result)


def ui(root, name, format_):
    result = SimpleNamespace(project_state={}, research=Hub(), mode="project_outputs", width=100, height=24, keymap={}, message="")
    result.say = result.fail = lambda text: setattr(result, "message", text)
    state = U.initialize(result)
    state["tree"] = dict(root=str(root))
    node = dict(path=name, specification=dict(path=name, format=format_), directory=False)
    U._preview(result, node); result.research.finish()
    return result


def test_csv_ui_page_back_global_sort_and_sticky_header(tmp_path):
    (tmp_path / "output.csv").write_text("id,name\n" + "".join(f"{i},item\n" for i in range(300, 0, -1)))
    result = ui(tmp_path, "output.csv", "csv")
    U.handle_key(result, "]"); result.research.finish()
    assert result.project_state["preview_page"] == 1
    U.handle_key(result, "["); result.research.finish()
    assert result.project_state["preview"]["records"][0][1][0] == "300"
    U.handle_key(result, "enter"); result.research.finish()
    assert result.project_state["preview"]["records"][0][1][0] == "1"
    U.handle_key(result, "pgdn")
    rows = U.overlay(SimpleNamespace(g=L.Glyphs(False)), {}, result, 100, 24)
    assert "[id ^] | name" in "\n".join(L.row_text(row) for _, _, row in rows)
    U.run_command(result, ["artifact", "columns", "name"]); result.research.finish()
    assert result.project_state["preview"]["displayed_header"] == ["name"]
    U.run_command(result, ["artifact", "sort", "id", "off"]); result.research.finish()
    assert result.project_state["preview"]["records"][0][1][0] == "300"


def test_json_ui_enter_folds_and_expands_selected_node(tmp_path):
    (tmp_path / "output.json").write_text('{"worker":{"rank":2}}')
    result = ui(tmp_path, "output.json", "json")
    assert len(result.project_state["preview"]["nodes"]) == 3
    U.handle_key(result, "enter")
    assert len(result.project_state["preview"]["nodes"]) == 1
    U.handle_key(result, "enter")
    assert len(result.project_state["preview"]["nodes"]) == 3
    U.run_command(result, ["artifact", "text"]); result.research.finish()
    assert result.project_state["preview"]["format"] == "text"
    U.run_command(result, ["artifact", "structured"]); result.research.finish()
    assert result.project_state["preview"]["format"] == "json"


def test_page_identity_change_preserves_old_published_view(tmp_path):
    path = tmp_path / "output.txt"; path.write_text("first\n" * 300)
    result = ui(tmp_path, path.name, "text")
    previous = result.project_state["preview"]
    path.write_text("new\n" * 300)
    U.handle_key(result, "]"); result.research.finish()
    assert result.project_state["preview"] is previous
    assert "changed between pages" in result.message
