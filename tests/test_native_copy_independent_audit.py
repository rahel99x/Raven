"""Independent data-integrity checks for native publication ownership."""
import struct

import pytest

from raven import native_series_cache as N


@pytest.mark.parametrize("bits", ["0000000000000000", "8000000000000000", "7ff0000000000000", "fff0000000000000",
                                  "7ff8000000000001", "7ff80000000000ab", "fff8000000000001", "7ff0000000000001"])
def test_publication_clone_preserves_all_ieee_bits(bits):
    value = struct.unpack(">d", bytes.fromhex(bits))[0]
    source = [{"metric": value, "typed": [True, 1, 1.0]}]
    cloned = N.owned_snapshot(source)
    assert struct.pack(">d", cloned[0]["metric"]).hex() == bits
    assert [type(item) for item in cloned[0]["typed"]] == [bool, int, float]
    assert cloned is not source and cloned[0] is not source[0]


def test_mutable_alias_graph_and_self_reference_survive_copy():
    inner = {"value": [1, 2]}
    pair = (inner,)
    source = [inner, inner, pair, pair]
    inner["self"] = inner
    inner["root"] = source
    cloned = N.owned_snapshot(source)
    assert cloned[0] is cloned[1]
    assert cloned[2] is cloned[3]
    assert cloned[2][0] is cloned[0]
    assert cloned[0]["self"] is cloned[0]
    assert cloned[0]["root"] is cloned
    cloned[0]["value"].append(3)
    assert inner["value"] == [1, 2]


def test_bytearray_fallback_retains_aliases_without_reducer_execution():
    binary = bytearray(b"data")
    source = [{"metadata": binary, "alias": binary, "metric": [1.0]}]
    assert N.signature(source) is None
    cloned = N.owned_snapshot(source)
    assert cloned[0]["metadata"] is cloned[0]["alias"]
    assert cloned[0]["metadata"] is not binary
    cloned[0]["metadata"][0] = ord("X")
    assert binary == b"data"
    cloned[0]["metric"][0] = 2.0
    assert source[0]["metric"] == [1.0]


def test_nan_used_as_mapping_key_and_value_keeps_identity_semantics():
    key = float("nan")
    source = [{"metadata": {key: "mapped", "alias": key}}]
    cloned = N.owned_snapshot(source)
    assert cloned[0]["metadata"][cloned[0]["metadata"]["alias"]] == "mapped"
