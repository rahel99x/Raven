"""Immutable render values reuse unchanged histories without hiding corrections."""
from collections import deque

import pytest

from raven.history_snapshots import HistoryDeque
from raven.model import Store


def test_render_snapshot_reuses_immutable_values_and_isolates_mapping():
    store = Store(persist=False)
    store.hist_cpu["1"].extend([.1, .2, .3])
    store.hist_gpu["1:node:0"].extend([.7, .8])
    first = store.render_snapshot()
    second = store.render_snapshot()
    assert first["hist_cpu"]["1"] is second["hist_cpu"]["1"]
    assert first["hist_gpu"]["1:node:0"] is second["hist_gpu"]["1:node:0"]
    with pytest.raises(TypeError):
        first["hist_cpu"]["1"][0] = 99
    first["hist_cpu"].clear()
    assert second["hist_cpu"]["1"] == (.1, .2, .3)
    mutable = store.snapshot()
    mutable["hist_cpu"]["1"].append(99)
    assert store.render_snapshot()["hist_cpu"]["1"] == (.1, .2, .3)


@pytest.mark.parametrize("action", [
    lambda q: q.append(4), lambda q: q.appendleft(4),
    lambda q: q.extend([4, 5]), lambda q: q.extendleft([4, 5]),
    lambda q: q.pop(), lambda q: q.popleft(), lambda q: q.clear(),
    lambda q: q.reverse(), lambda q: q.rotate(1),
    lambda q: q.remove(2), lambda q: q.insert(1, 4),
    lambda q: q.__setitem__(1, 9), lambda q: q.__delitem__(1),
    lambda q: q.__iadd__([4]), lambda q: q.__imul__(2),
])
def test_all_mutations_publish_new_revision_and_retain_previous_values(action):
    values = HistoryDeque([1, 2, 3], maxlen=8)
    initial = values.immutable()
    revision = values.revision
    action(values)
    assert values.revision > revision
    assert values.immutable() == tuple(values)
    assert values.immutable() != initial
    assert initial == (1, 2, 3)
    assert values.immutable() is values.immutable()


def test_correction_at_same_length_and_endpoints_is_not_missed():
    store = Store(persist=False)
    store.hist_cpu["1"].extend([1, 2, 3])
    store.hist_cpu["2"].extend([4, 5, 6])
    before = store.render_snapshot()
    store.hist_cpu["1"][1] = 20
    after = store.render_snapshot()
    assert after["hist_cpu"]["1"] == (1, 20, 3)
    assert before["hist_cpu"]["1"] == (1, 2, 3)
    assert after["hist_cpu"]["2"] is before["hist_cpu"]["2"]


def test_untracked_replacement_retains_complete_correction_detection():
    store = Store(persist=False)
    store.hist_cpu = {"1": deque([1, 2, 3])}
    first = store.render_snapshot()
    store.hist_cpu["1"][1] = 20
    second = store.render_snapshot()
    assert second["hist_cpu"]["1"] == (1, 20, 3)
    assert first["hist_cpu"]["1"] == (1, 2, 3)
    del store.hist_cpu["1"]
    assert not store.render_snapshot()["hist_cpu"]


def test_partial_failed_extend_invalidates_published_values():
    values = HistoryDeque([1, 2])
    before = values.immutable()
    def broken():
        yield 3
        raise RuntimeError("producer failed")
    with pytest.raises(RuntimeError):
        values.extend(broken())
    assert values.immutable() == (1, 2, 3) and before == (1, 2)


def test_bounded_rollover_cannot_reuse_old_snapshot():
    values = HistoryDeque([1, 2, 3], maxlen=3)
    before = values.immutable()
    values.append(4)
    assert values.immutable() == (2, 3, 4) and before == (1, 2, 3)


def test_unchanged_native_snapshot_does_not_traverse_samples(monkeypatch):
    store = Store(persist=False)
    for jid in range(200):
        store.hist_cpu[str(jid)].extend(range(600))
    before = store.render_snapshot()
    def forbid(_):
        pytest.fail("Unchanged history must reuse its published tuple")
    monkeypatch.setattr(HistoryDeque, "__iter__", forbid)
    after = store.render_snapshot()
    assert all(after["hist_cpu"][jid] is values for jid, values in before["hist_cpu"].items())
