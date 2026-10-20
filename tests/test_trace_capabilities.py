"""Capability prerequisites are descriptive, not simulated hardware qualification."""
import json
import socket
import subprocess
from types import SimpleNamespace

import pytest

from raven import trace_capabilities as C


def host():
    return dict(os="Linux", architecture="x86_64", python="3.12.0", implementation="CPython",
                posix=True, monitoring_api=True, profile_api=True, native_transport=True,
                proc_identity=True, cpu_placement={
                    "schema": "raven.cpu-placement-coverage/v1", "status": "available",
                    "source": "linux-sched_getcpu", "reason": "", "semantics": "point observations",
                    "topology_source": "linux-sysfs", "topology_cpus": 4, "topology_limit": 8192,
                    "topology_status": "complete"}, tools={name: False for name in C._TOOLS},
                visible_pmus={name: False for name in C._PMUS})


def report(monkeypatch, **changes):
    evidence = host()
    evidence.update(changes)
    monkeypatch.setattr(C, "_probe", lambda: evidence)
    return C.describe()


def adapters(value):
    return {adapter["id"]: adapter for adapter in value["adapters"]}


@pytest.mark.parametrize("architecture", ["x86_64", "aarch64", "arm64", "armv7l", "ppc64le", "riscv64"])
def test_viewer_and_python_capture_do_not_gate_on_cpu_vendor(monkeypatch, architecture):
    value = report(monkeypatch, architecture=architecture)
    assert value["viewer"]["architecture_independent"]
    assert adapters(value)["python-monitoring"]["status"] == "runtime-present"
    assert adapters(value)["software-cpu-placement"]["status"] == "explicit-opt-in"
    assert all(not row["implemented"] and row["status"] == "not-implemented" for row in value["hardware"])
    assert "does not record, benchmark or qualify" in value["qualification"]


def test_legacy_runtime_requires_explicit_consent_and_discloses_fidelity(monkeypatch):
    value = report(monkeypatch, python="3.11.2", monitoring_api=False)
    found = adapters(value)
    assert found["python-monitoring"]["status"] == "unavailable"
    assert found["python-legacy-profile"]["status"] == "explicit-opt-in"
    assert "cannot be distinguished" in found["python-legacy-profile"]["scope"]
    assert "--trace-adapter python-legacy-profile" in C.render(value)


def test_mac_arm_does_not_claim_linux_native_or_hardware_capture(monkeypatch):
    value = report(monkeypatch, os="Darwin", architecture="arm64", proc_identity=False)
    found = adapters(value)
    assert found["python-monitoring"]["status"] == "runtime-present"
    assert found["cooperative-native-socket"]["status"] == "unavailable"
    assert found["pmpi-world"]["status"] == "unavailable"
    assert found["linux-process-counters"]["status"] == "unavailable"
    assert found["software-cpu-placement"]["status"] == "unavailable"
    assert "identities remain unknown" in value["identity"]
    assert "another operating system" in value["qualification"]


def test_non_posix_runtime_is_not_reported_ready(monkeypatch):
    value = report(monkeypatch, os="Windows", architecture="ARM64", posix=False,
                   native_transport=False, proc_identity=False)
    assert all(row["status"] == "unavailable" for row in value["adapters"])
    assert value["viewer"]["architecture_independent"]


@pytest.mark.parametrize("missing", ["proc_identity", "native_transport"])
def test_linux_native_requires_transport_and_identity_prerequisites(monkeypatch, missing):
    value = report(monkeypatch, **{missing: False})
    assert adapters(value)["cooperative-native-socket"]["status"] == "unavailable"
    assert adapters(value)["pmpi-world"]["status"] == "unavailable"


def test_present_tools_and_pmus_never_become_claimed_capture_adapters(monkeypatch):
    value = report(monkeypatch, visible_pmus={name: True for name in C._PMUS},
                   tools={name: True for name in C._TOOLS})
    hardware = {row["id"]: row for row in value["hardware"]}
    assert hardware["intel-pt"]["visible_pmus"] == ["intel_pt"]
    assert hardware["amd-ibs"]["visible_pmus"] == ["ibs_op", "ibs_fetch"]
    assert hardware["arm-coresight"]["visible_pmus"] == ["cs_etm"]
    assert all(row["status"] == "not-implemented" for row in hardware.values())
    assert "sampling cannot reconstruct every call" in hardware["amd-ibs"]["scope"]
    assert "branding alone proves no availability" in hardware["arm-coresight"]["scope"]
    assert "not recording permission or qualification" in C.render(value)


def test_probe_reads_only_fixed_metadata_and_never_executes_or_connects(monkeypatch):
    from raven import trace_cpu
    calls = []
    def forbidden(*args, **kwargs):
        raise AssertionError("Capability reporting must not execute programs or connect sockets")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(C.platform, "system", lambda: "Linux")
    monkeypatch.setattr(C.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(C.platform, "python_version", lambda: "3.12.0")
    monkeypatch.setattr(C.platform, "python_implementation", lambda: "CPython")
    def present(path):
        calls.append(path)
        return True
    monkeypatch.setattr(C.os.path, "isdir", present)
    monkeypatch.setattr(C.os.path, "isfile", present)
    monkeypatch.setattr(C.shutil, "which", lambda name: calls.append(name))
    def cpu_capabilities():
        calls.append("cpu-placement-prerequisites")
        return host()["cpu_placement"]
    monkeypatch.setattr(trace_cpu, "capabilities", cpu_capabilities)
    value = C.describe()
    assert set(calls) == {"/proc/self/stat", "cpu-placement-prerequisites", *C._TOOLS,
                          *("/sys/bus/event_source/devices/" + name for name in C._PMUS)}
    assert not any(value["host"]["tools"].values())


@pytest.mark.parametrize("topology_status", ["partial", "unavailable"])
def test_cpu_point_observations_do_not_require_complete_socket_core_topology(monkeypatch, topology_status):
    cpu = dict(host()["cpu_placement"], topology_status=topology_status, topology_cpus=0)
    value = report(monkeypatch, cpu_placement=cpu)
    assert adapters(value)["software-cpu-placement"]["status"] == "explicit-opt-in"
    assert value["host"]["cpu_placement"] == cpu
    assert "do not establish continuous residency" in adapters(value)["software-cpu-placement"]["scope"]
    assert "Topology: " + topology_status in C.render(value)


def test_cpu_probe_failure_is_unavailable_with_actionable_reason(monkeypatch):
    cpu = dict(host()["cpu_placement"], status="unavailable", reason="CPU placement probe failed: AttributeError")
    value = report(monkeypatch, cpu_placement=cpu)
    assert adapters(value)["software-cpu-placement"]["status"] == "unavailable"
    assert cpu["reason"] in C.render(value)


def test_cli_json_and_readable_output_are_complete(monkeypatch, capsys):
    expected = report(monkeypatch)
    assert C.main(["--json"]) == 0
    assert json.loads(capsys.readouterr().out) == expected
    assert C.main([]) == 0
    rendered = capsys.readouterr().out
    assert "local interface host" in rendered
    for item in expected["adapters"] + expected["hardware"]:
        assert item["name"] in rendered and item["scope"] in rendered


def test_trace_entrypoint_dispatches_capabilities_without_heavy(monkeypatch, capsys):
    from raven import trace_recorder
    expected = report(monkeypatch)
    assert trace_recorder.main(["capabilities", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == expected


def test_managed_interface_admits_capability_metadata_without_a_worker():
    from raven.raven_execution import cli_admission
    assert cli_admission(["trace", "capabilities", "--json"], SimpleNamespace(managed=True)) is None
