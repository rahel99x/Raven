"""Canonical log implementations retain the former public module identities."""

import importlib

import pytest


@pytest.mark.parametrize(
    ("legacy", "canonical"),
    [
        ("logs", "buffer"),
        ("history_log_export", "history_export"),
        ("log_bundle", "bundle"),
        ("log_catalog", "catalog"),
        ("log_copy", "copy"),
        ("log_divergence", "divergence"),
        ("log_match_index", "match_index"),
        ("log_paths", "paths"),
        ("log_presentation", "presentation"),
        ("log_scan", "scan"),
        ("log_text", "text"),
        ("log_tools", "tools"),
        ("log_workbench", "workbench"),
    ],
)
def test_legacy_log_module_is_the_actual_canonical_module(legacy, canonical):
    old = importlib.import_module("raven." + legacy)
    new = importlib.import_module("raven.job_logs." + canonical)
    assert old is new
    assert old.__file__ == new.__file__


def test_cross_family_logbuffer_class_identity_is_shared():
    from raven.logs import LogBuffer as old
    from raven.job_logs.buffer import LogBuffer as new

    assert old is new
