"""Only document markers may select a published example's schema."""

import importlib.util
from pathlib import Path


SPEC = importlib.util.spec_from_file_location(
    "raven_schema_audit",
    Path(__file__).resolve().parents[1] / "scripts/validate_operation_schemas.py",
)
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)


def test_nested_definition_cannot_replace_top_level_document_schema():
    schema = {
        "properties": {"schema": {"const": "policy/v1"}},
        "$defs": {
            "profile": {"properties": {"schema": {"const": "config/v1"}}}
        },
    }
    assert audit.markers(schema) == ["policy/v1"]


def test_top_level_alternative_documents_remain_discoverable():
    schema = {
        "oneOf": [
            {"properties": {"schema": {"const": "first/v1"}}},
            {"properties": {"schema": {"const": "second/v1"}}},
        ]
    }
    assert audit.markers(schema) == ["first/v1", "second/v1"]
