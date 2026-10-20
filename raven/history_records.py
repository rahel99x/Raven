"""Per-comparison normalization memo; never persisted or shared across snapshots."""
from collections.abc import Mapping
from dataclasses import fields, is_dataclass


class HistoryRecord(Mapping):
    def __init__(self, original):
        self.original = original
        self.memo = {}

    def __getitem__(self, key):
        if isinstance(self.original, Mapping):
            return self.original[key]
        try:
            return getattr(self.original, key)
        except AttributeError as exc:
            raise KeyError(key) from exc

    def __iter__(self):
        if isinstance(self.original, Mapping):
            return iter(self.original)
        return iter(field.name for field in fields(self.original)) if is_dataclass(self.original) else iter(())

    def __len__(self):
        if isinstance(self.original, Mapping):
            return len(self.original)
        return len(fields(self.original)) if is_dataclass(self.original) else 0


def check(cancel):
    if cancel is not None and cancel.is_set():
        raise ValueError("History analysis cancelled")
