"""The generated controls reference follows actual, editable operation contracts."""
import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("operation_reference_generator", ROOT / "scripts/generate_operation_reference.py")
reference = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reference)


def test_all_registered_operations_and_fields_are_documented():
    document = reference.render()
    for operation in reference.catalog():
        command = reference.code(":ops " + operation["key"])
        assert document.count(command) == 1
        section = document.split(command, 1)[1].split("\n### ", 1)[0]
        for field in operation["fields"]:
            assert reference.code(field["key"]) in section
            for choice in field.get("choices", []):
                assert reference.code(choice) in section
    assert reference.DEFAULT_OUTPUT.read_text(encoding="utf-8") == document


def test_literal_text_cannot_break_a_reference_table():
    document = reference.render([{"key": "sample", "title": "Example", "summary": "a < b", "fields": [
        {"key": "path", "label": "one|two\n<script>", "required": True, "default": "`literal`", "choices": ["left|right"]}
    ]}], [])
    assert "one&#124;two &lt;script&gt;" in document
    assert "<code>&#96;literal&#96;</code>" in document
    assert "<code>left&#124;right</code>" in document
    assert "a &lt; b" in document


def test_check_detects_changed_reference_without_writing(tmp_path):
    path = tmp_path / "reference.md"
    assert reference.main(["--output", str(path)]) == 0
    assert reference.main(["--output", str(path), "--check"]) == 0
    path.write_text("old reference\n")
    assert reference.main(["--output", str(path), "--check"]) == 1
    assert path.read_text() == "old reference\n"
