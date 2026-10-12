"""CUDA visibility proof: real pointer writes and an isolated native child.

The fake library exports only driver inspection functions. No CUDA hardware,
toolkit, runtime package, device context, or job-process attachment is needed.
"""
from __future__ import annotations

import contextlib
import ctypes
import io
import json
import os
import shutil
import subprocess
import sys

import pytest

from raven import gpu_collectors


UUIDS = ["GPU-00010203-0405-0607-0809-0a0b0c0d0e0f",
         "GPU-10111213-1415-1617-1819-1a1b1c1d1e1f"]


def number(value):
    return value.value if isinstance(value, ctypes.c_int) else value


class Function:
    """ctypes-shaped callable: production can declare argtypes/restype."""
    def __init__(self, operation):
        self.operation = operation

    def __call__(self, *args):
        return self.operation(*args)


class Driver:
    def __init__(self, *, count=2, failure="", duplicate=False, zero=False,
                 legacy=False):
        self.count = count
        self.failure = failure
        self.duplicate = duplicate
        self.zero = zero
        self.calls = []
        self.cuInit = Function(self.init)
        self.cuDeviceGetCount = Function(self.get_count)
        self.cuDeviceGet = Function(self.get_device)
        setattr(self, "cuDeviceGetUuid" if legacy else "cuDeviceGetUuid_v2",
                Function(self.get_uuid))

    def record(self, name, *args):
        self.calls.append((name, *args))
        return 999 if self.failure == name else 0

    def init(self, flags):
        assert number(flags) == 0
        return self.record("init")

    def get_count(self, output):
        ctypes.cast(output, ctypes.POINTER(ctypes.c_int))[0] = self.count
        return self.record("count")

    def get_device(self, output, ordinal):
        ordinal = number(ordinal)
        assert 0 <= ordinal < self.count
        # Handles are deliberately different from ordinals.
        ctypes.cast(output, ctypes.POINTER(ctypes.c_int))[0] = 17 + ordinal
        return self.record("device", ordinal)

    def get_uuid(self, output, handle):
        ordinal = number(handle) - 17
        assert 0 <= ordinal < self.count
        start = 0 if self.duplicate else ordinal * 16
        value = (bytes(16) if self.zero else bytes([ordinal]) * 16 if ordinal >= 2
                 else bytes((start + i) % 256 for i in range(16)))
        ctypes.memmove(output, value, 16)
        return self.record("uuid", ordinal)


def invoke(monkeypatch, driver, *, visibility="0,1"):
    loaded = []

    def load(name, *args, **kwargs):
        loaded.append(name)
        if isinstance(driver, Exception):
            raise driver
        return driver

    monkeypatch.setattr(ctypes, "CDLL", load)
    if visibility is None:
        monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
    else:
        monkeypatch.setenv("CUDA_VISIBLE_DEVICES", visibility)
    output, error = io.StringIO(), io.StringIO()
    code = 0
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
        try:
            exec(gpu_collectors.CUDA_VISIBILITY_SOURCE, {"__name__": "__main__"})
        except SystemExit as exc:
            code = exc.code
    return code, output.getvalue(), error.getvalue(), loaded


@pytest.mark.parametrize("legacy", [False, True])
def test_driver_resolves_visible_ordinals_to_canonical_uuids_without_contexts(monkeypatch, legacy):
    driver = Driver(legacy=legacy)
    code, output, error, loaded = invoke(monkeypatch, driver)
    assert code == 0 and not error
    assert json.loads(output) == UUIDS
    assert loaded == ["libcuda.so.1"]
    assert driver.calls == [("init",), ("count",), ("device", 0),
                            ("uuid", 0), ("device", 1), ("uuid", 1)]


def test_cuda_runtime_ordinals_are_remapped_not_raw_visibility_numbers(monkeypatch):
    driver = Driver(count=2)
    code, output, _, _ = invoke(monkeypatch, driver, visibility="7,3")
    assert code == 0 and json.loads(output) == UUIDS
    assert [call for call in driver.calls if call[0] == "device"] == [
        ("device", 0), ("device", 1)]


@pytest.mark.parametrize("operation", ["init", "count", "device", "uuid"])
def test_driver_error_never_returns_partial_identity(monkeypatch, operation):
    driver = Driver(failure=operation)
    code, output, error, _ = invoke(monkeypatch, driver)
    assert code != 0 and not output and error.strip()
    assert "999" in error
    failed = next(index for index, call in enumerate(driver.calls) if call[0] == operation)
    assert failed == len(driver.calls) - 1


@pytest.mark.parametrize("count", [-1, 0, 257, 2**31 - 1])
def test_driver_count_is_bounded_before_enumeration(monkeypatch, count):
    driver = Driver(count=count)
    code, output, error, _ = invoke(monkeypatch, driver)
    assert code != 0 and not output and error.strip()
    assert driver.calls == [("init",), ("count",)]


def test_maximum_supported_driver_count_remains_bounded_and_complete(monkeypatch):
    driver = Driver(count=256)
    code, output, error, _ = invoke(monkeypatch, driver,
        visibility=",".join(str(index) for index in range(256)))
    assert code == 0 and not error
    assert len(set(json.loads(output))) == 256
    assert len(driver.calls) == 2 + 2 * 256


@pytest.mark.parametrize("options", [{"duplicate": True}, {"zero": True}])
def test_driver_rejects_nonunique_or_zero_uuid_proof(monkeypatch, options):
    code, output, error, _ = invoke(monkeypatch, Driver(**options))
    assert code != 0 and not output and error.strip()


@pytest.mark.parametrize("visibility", [None, "", "-1", "GPU-deadbeef", "MIG-GPU-a/1/0",
    "0-1", "0;echo unsafe", "0\n1", "1" * 20000])
def test_invalid_or_non_numeric_visibility_never_initializes_cuda(monkeypatch, visibility):
    driver = Driver()
    code, output, error, loaded = invoke(monkeypatch, driver, visibility=visibility)
    assert code != 0 and not output and error.strip()
    assert not loaded and not driver.calls


@pytest.mark.parametrize("driver", [OSError("libcuda.so.1: missing"), object()])
def test_missing_driver_or_entry_point_has_bounded_diagnostic(monkeypatch, driver):
    code, output, error, _ = invoke(monkeypatch, driver)
    assert code != 0 and not output and error.strip()
    assert len(error) < 1000 and "Traceback" not in error


C_DRIVER = r'''
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
static int failure(const char *name) {
    const char *mode = getenv("RAVEN_TEST_CUDA_FAIL");
    return mode && strcmp(mode, name) == 0 ? 999 : 0;
}
int cuInit(unsigned int flags) {
    const char *audit = getenv("RAVEN_TEST_CUDA_AUDIT");
    if (audit) {
        FILE *file = fopen(audit, "a");
        if (file) { fputs("init\n", file); fclose(file); }
    }
    if (getenv("RAVEN_TEST_CUDA_DELAY")) usleep(3000000);
    return failure("init");
}
int cuDeviceGetCount(int *output) { *output = 2; return failure("count"); }
int cuDeviceGet(int *output, int ordinal) {
    if (ordinal < 0 || ordinal > 1) return 101;
    *output = 17 + ordinal;
    return failure("device");
}
#ifdef LEGACY_UUID
int cuDeviceGetUuid(unsigned char *output, int handle) {
#else
int cuDeviceGetUuid_v2(unsigned char *output, int handle) {
#endif
    if (handle < 17 || handle > 18) return 101;
    for (int i = 0; i < 16; ++i) output[i] = (handle - 17) * 16 + i;
    return failure("uuid");
}
/* A regression that creates a device context must fail this test process. */
int cuCtxCreate(void *out, unsigned int flags, int device) { abort(); }
int cuCtxCreate_v2(void *out, unsigned int flags, int device) { abort(); }
int cuDevicePrimaryCtxRetain(void *out, int device) { abort(); }
'''


@pytest.fixture(params=[False, True], ids=["uuid-v2", "legacy-uuid"])
def cuda_library(tmp_path, request):
    compiler = shutil.which("gcc") or shutil.which("cc")
    if compiler is None:
        pytest.skip("C compiler is unavailable for the CUDA driver ABI integration test")
    source = tmp_path / "driver.c"
    source.write_text(C_DRIVER)
    args = [compiler, "-shared", "-fPIC", "-O2", str(source), "-o", str(tmp_path / "libcuda.so.1")]
    if request.param:
        args.append("-DLEGACY_UUID")
    result = subprocess.run(args, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    return tmp_path


def native_env(library, **updates):
    # The child must only use this fixture library, even on a CUDA-equipped host.
    env = dict(os.environ, LD_LIBRARY_PATH=str(library), CUDA_VISIBLE_DEVICES="7,3")
    for key in list(env):
        if key.startswith("RAVEN_TEST_CUDA_"):
            del env[key]
    env.update(updates)
    return env


def test_real_child_uses_cuda_driver_abi_without_toolkit_or_import_hooks(cuda_library):
    poison = cuda_library / "ctypes.py"
    poison.write_text("raise RuntimeError('project module imported')\n")
    result = subprocess.run([sys.executable, "-I", "-S", "-c", gpu_collectors.CUDA_VISIBILITY_SOURCE],
        env=native_env(cuda_library, PYTHONPATH=str(cuda_library)), cwd=cuda_library,
        capture_output=True, text=True, timeout=3)
    assert result.returncode == 0, result.stderr
    assert not result.stderr and json.loads(result.stdout) == UUIDS


@pytest.mark.parametrize("failure", ["init", "count", "device", "uuid"])
def test_real_child_driver_errors_exit_cleanly(cuda_library, failure):
    result = subprocess.run([sys.executable, "-I", "-S", "-c", gpu_collectors.CUDA_VISIBILITY_SOURCE],
        env=native_env(cuda_library, RAVEN_TEST_CUDA_FAIL=failure),
        capture_output=True, text=True, timeout=3)
    assert result.returncode != 0 and not result.stdout
    assert "999" in result.stderr and "Traceback" not in result.stderr


@pytest.fixture
def native_probe(cuda_library):
    inventory = "\n".join(f"{index + 3}, 34, 2335, 24564, RTX, {uuid}, 00000000:0{index + 1}:00.0, [N/A]"
                          for index, uuid in enumerate(UUIDS))
    # Listing a second vendor reproduces the namespace ambiguity on the
    # reported Fedora workstation, independently of the test host hardware.
    for name, output in (("nvidia-smi", inventory), ("amd-smi", "[]")):
        binary = cuda_library / name
        binary.write_text("#!" + sys.executable + "\nprint(" + repr(output) + ")\n")
        binary.chmod(0o755)
    audit = cuda_library / "initializations"
    env = native_env(cuda_library, PATH=str(cuda_library), SLURM_JOB_ID="506",
        SLURM_JOB_GPUS="0,1", SLURM_STEP_GPUS="", SLURMD_NODENAME="fedora-test",
        ROCR_VISIBLE_DEVICES="", CUDA_DEVICE_ORDER="",
        RAVEN_TEST_CUDA_AUDIT=str(audit))

    def probe(*, cache=None, budget=4, resolve_visibility=True, require_scope=True, **updates):
        command = [sys.executable] + gpu_collectors.probe_command("auto", budget=budget,
            cache=cache, resolve_visibility=resolve_visibility)[1:]
        result = subprocess.run(command, env=dict(env, **updates), cwd=cuda_library,
            capture_output=True, text=True, timeout=6)
        assert result.returncode == 0, result.stderr
        return gpu_collectors.parse_probe_output(result.stdout, job_id="506",
            require_scope=require_scope), json.loads(result.stdout)

    return probe, audit


def test_real_probe_resolves_mixed_vendor_numeric_scope_and_reuses_identity_cache(native_probe):
    probe, audit = native_probe
    (samples, cache, errors), payload = probe()
    assert not errors
    assert [sample.uuid for sample in samples] == UUIDS
    assert {sample.vendor for sample in samples} == {"nvidia"}
    assert all(sample.util == 34 and sample.used == 2335 for sample in samples)
    assert payload["cuda_visibility"]["uuids"] == UUIDS
    assert audit.read_text().splitlines() == ["init"]
    (second, next_cache, errors), _ = probe(cache=cache)
    assert not errors and [sample.uuid for sample in second] == UUIDS
    assert next_cache == cache
    # Vendor counters refresh while the stable CUDA UUID proof stays cached.
    assert audit.read_text().splitlines() == ["init"]


@pytest.mark.parametrize("change", [{"CUDA_VISIBLE_DEVICES": "3,7"},
                                    {"CUDA_DEVICE_ORDER": "PCI_BUS_ID"}])
def test_real_probe_scope_changes_resolve_cuda_again(native_probe, change):
    probe, audit = native_probe
    (_, cache, _), _ = probe()
    (samples, next_cache, errors), _ = probe(cache=cache, **change)
    assert not errors and [sample.uuid for sample in samples] == UUIDS
    assert cache["fedora-test"]["_cuda_visibility"]["key"] != next_cache["fedora-test"]["_cuda_visibility"]["key"]
    assert audit.read_text().splitlines() == ["init", "init"]


def test_real_probe_changed_inventory_invalidates_cached_cuda_proof(native_probe):
    probe, audit = native_probe
    (_, cache, _), _ = probe()
    tool = audit.parent / "nvidia-smi"
    tool.write_text(tool.read_text().replace("00000000:01:00.0", "00000000:09:00.0"))
    (samples, next_cache, errors), _ = probe(cache=cache)
    assert not errors and [sample.uuid for sample in samples] == UUIDS
    assert cache["fedora-test"]["_cuda_visibility"]["key"] != next_cache["fedora-test"]["_cuda_visibility"]["key"]
    assert audit.read_text().splitlines() == ["init", "init"]


def test_real_probe_cuda_initialization_is_killed_at_shared_deadline(native_probe):
    probe, audit = native_probe
    (samples, _, errors), payload = probe(budget=1.25, RAVEN_TEST_CUDA_DELAY="1")
    assert not samples
    assert "timed out" in payload["cuda_visibility_error"]
    assert any("timed out" in error for error in errors)
    assert audit.read_text().splitlines() == ["init"]


def test_real_probe_driver_failure_never_becomes_zero_gpu_metrics(native_probe):
    probe, _ = native_probe
    (samples, cache, errors), payload = probe(RAVEN_TEST_CUDA_FAIL="uuid")
    assert not samples
    assert not cache.get("fedora-test", {}).get("_cuda_visibility")
    assert "999" in payload["cuda_visibility_error"]
    assert any("999" in error for error in errors)


@pytest.mark.parametrize("visibility", ["", ",".join(UUIDS)])
def test_real_probe_initializes_cuda_only_for_explicit_numeric_visibility(native_probe, visibility):
    probe, audit = native_probe
    (samples, _, errors), payload = probe(CUDA_VISIBLE_DEVICES=visibility)
    assert not audit.exists()
    assert "cuda_visibility" not in payload
    if visibility:
        assert not errors and [sample.uuid for sample in samples] == UUIDS
    else:
        assert not samples and errors


@pytest.mark.parametrize("cached", [False, True])
def test_host_diagnostics_disable_cuda_resolution_despite_inherited_allocation(native_probe, cached):
    probe, audit = native_probe
    cache = None
    if cached:
        (_, cache, _), _ = probe()
        audit.unlink()
    (samples, _, errors), payload = probe(cache=cache, resolve_visibility=False, require_scope=False)
    assert not audit.exists()
    assert "cuda_visibility" not in payload and "cuda_visibility_error" not in payload
    assert not errors and [sample.uuid for sample in samples] == UUIDS
