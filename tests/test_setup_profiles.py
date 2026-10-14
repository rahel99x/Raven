"""Site setup never submits jobs; fixtures cover persistence and unit semantics."""
import copy
import json
import os
from pathlib import Path
import stat
import sys

import pytest

from raven import launch_settings as L
from raven import raven_config as C
from raven import setup_profiles as P


def environment(tmp_path):
    return {"HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_STATE_HOME": str(tmp_path / "state")}


def service(tmp_path, document=None, **options):
    env = environment(tmp_path)
    if document is not None:
        path = tmp_path / "config.json"
        path.write_text(json.dumps(document))
        options["config_path"] = str(path)
    return P.ProfileSetup(C.resolve(env=env, system_policy_paths=[], **options), env=env)


@pytest.mark.parametrize("value,expected", [(8192, 8192), ("8192", 8192), ("8 GiB", 8192),
                                          ("8G", 8192), ("1.5 GiB", 1536), ("8 GB", 7630),
                                          ("1 B", 1), ("1025 KiB", 2)])
def test_memory_units_are_explicit_and_round_up(value, expected):
    assert P.parse_memory_mib(value) == expected


@pytest.mark.parametrize("value", [True, 1.5, "0", "-1GiB", "inf", "1e300 GiB", "8 GiB extra", "1 iB", "9" * 65])
def test_memory_rejects_invalid_or_unbounded_input(value):
    with pytest.raises(ValueError):
        P.parse_memory_mib(value)


@pytest.mark.parametrize("value,expected", [(3600, 3600), ("3600", 3600), ("1h", 3600),
                                          ("1h 30m", 5400), ("1.5h", 5400), ("01:30:00", 5400),
                                          ("1-01:30:00", 91800), ("30:00", 1800), ("0.1s", 1)])
def test_duration_input(value, expected):
    assert P.parse_duration_seconds(value) == expected


@pytest.mark.parametrize("value", [False, "0", "1h 2h", "1h later", "1:60:00", "1-24:00:00", "2-01:30", "1e9s", "400d"])
def test_invalid_durations(value):
    with pytest.raises(ValueError):
        P.parse_duration_seconds(value)


def test_review_reads_but_never_creates_state_or_calls_scheduler(tmp_path, monkeypatch):
    setup = service(tmp_path)
    monkeypatch.setattr(P.S, "_atomic_write", lambda *args: pytest.fail("review wrote settings"))
    draft = setup.stage("LabX GPU experiments", "local-slurm", heavy={"account": "LabX", "memory_mib": "8 GiB", "walltime_seconds": "1h"})
    assert draft["profile"] == "labx-gpu-experiments"
    assert draft["effective"]["allocation"]["memory_mib"] == 8192
    assert draft["effective"]["allocation"]["walltime_seconds"] == 3600
    assert draft["effective"]["execution"]["startup"] == "light"
    assert draft["effective"]["allocation"]["account"] == "LabX"
    assert list(tmp_path.iterdir()) == []


def test_save_named_default_roundtrips_without_rebinding_active_config(tmp_path):
    setup = service(tmp_path)
    original = setup.resolved.as_dict()
    saved = setup.save(setup.stage("LabX GPU experiments", "ssh-slurm", host="lab-hpc", user="alice",
                                   worker_python="/shared/raven/bin/python", heavy={"account": "LabX", "partition": "gpu"}))
    config = C.resolve(env=environment(tmp_path), system_policy_paths=[])
    assert config.data["profile"] == saved["profile"] == "labx-gpu-experiments"
    assert config.data["ui"]["profile_label"] == "LabX GPU experiments"
    assert config.data["connection"]["user"] == "alice"
    assert config.data["allocation"]["account"] == "LabX"
    assert setup.resolved.data == original
    assert config.data["execution"]["startup"] == "light"
    assert Path(saved["path"]).stat().st_mode & 0o777 == 0o600
    assert "--site labx-gpu-experiments" in saved["command"]
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize("source,target", [("standalone", "local-slurm"), ("standalone", "ssh-slurm"),
                                          ("ssh-slurm", "standalone"), ("local-slurm", "standalone")])
def test_switching_kind_resets_execution_transport_and_adapter(tmp_path, source, target):
    setup = service(tmp_path)
    source_args = {"host": "first-host", "user": "alice"} if source == "ssh-slurm" else {}
    setup.save(setup.stage("First", source, **source_args))
    second_args = {"host": "second-host", "user": "bob"} if target == "ssh-slurm" else {}
    setup.save(setup.stage("Second", target, **second_args))
    second = C.resolve(env=environment(tmp_path), system_policy_paths=[])
    C.validate(second.data)
    assert P._kind(second.data) == target
    assert second.data["allocation"]["account"] == ""
    assert second.data["connection"]["host"] == second_args.get("host", "")
    first = C.resolve(site="first", env=environment(tmp_path), system_policy_paths=[])
    assert P._kind(first.data) == source


def test_new_profile_does_not_inherit_another_profiles_billing(tmp_path):
    document = C.merge(C.DESKTOP, {"schema": C.SCHEMA, "allocation": {"account": "private-lab"}})
    setup = service(tmp_path, document)
    setup.save(setup.stage("Shared workstation", "local-slurm"))
    result = C.resolve(config_path=str(setup.path), env=environment(tmp_path), system_policy_paths=[])
    assert result.data["allocation"]["account"] == ""
    assert result.data["execution"]["heavy_requires_allocation"] is True
    assert result.data["collection"]["slurm_enabled"] is True
    assert result.data["connection"]["transport"] == "local"


@pytest.mark.parametrize("document,profile", [({"schema": C.SCHEMA, "profile": "desktop"}, "desktop"),
    ({"schema": C.SCHEMA, "profile": "my-desktop", "profiles": {"my-desktop": {"site": {"kind": "desktop"}}}}, "my-desktop")])
def test_listing_builtin_and_partial_named_desktop_matches_resolver(tmp_path, document, profile):
    setup = service(tmp_path, document)
    selected = next(row for row in setup.list_profiles() if row["profile"] == profile)
    assert selected["kind"] == "standalone"
    assert selected["connection"]["transport"] == setup.resolved.data["connection"]["transport"] == "none"
    assert selected["heavy"]["adapter"] == setup.resolved.data["allocation"]["adapter"] == "desktop"
    saved = setup.save(setup.stage(profile, "standalone"))
    reloaded = C.resolve(config_path=saved["path"], site=profile, env=environment(tmp_path), system_policy_paths=[])
    assert reloaded.data["connection"]["transport"] == "none"


def test_edit_preserves_unrelated_preferences_other_profiles_and_ssh_options(tmp_path):
    doc = {"schema": C.SCHEMA, "profile": "lab", "ui": {"theme": "amber"}, "profiles": {
        "lab": {"ui": {"profile_label": "Lab X"}, "connection": {"transport": "ssh", "host": "hpc", "user": "alice", "ssh_options": ["-p", "2222"], "credential_reference": "agent"},
                "allocation": {"account": "LabX", "gpus": 1}, "collection": {"history_seconds": 120}},
        "other": {"allocation": {"account": "LabY"}}}}
    setup = service(tmp_path, doc)
    setup.save(setup.stage("Lab X", "ssh-slurm", host="hpc", user="alice", heavy={"memory_mib": "8 GiB"}))
    saved = json.loads(setup.path.read_text())
    assert saved["profiles"]["other"] == doc["profiles"]["other"]
    assert saved["ui"] == doc["ui"]
    assert saved["profiles"]["lab"]["collection"]["history_seconds"] == 120
    assert saved["profiles"]["lab"]["connection"] == C.merge(C.DEFAULTS["connection"], doc["profiles"]["lab"]["connection"])
    assert saved["profiles"]["lab"]["allocation"]["gpus"] == 1


def test_duplicate_slug_does_not_overwrite_different_friendly_name(tmp_path):
    setup = service(tmp_path)
    setup.save(setup.stage("Lab-X", "local-slurm"))
    with pytest.raises(ValueError, match="existing identifier"):
        setup.stage("Lab X", "local-slurm")


def test_omitted_identity_fields_preserve_existing_connection_but_not_new_targets(tmp_path):
    setup = service(tmp_path)
    setup.save(setup.stage("Lab X", "ssh-slurm", host="hpc", user="alice", cluster="cluster-a",
                           worker_python="/shared/raven/bin/python"))
    draft = setup.stage("Lab X", "ssh-slurm", host="hpc", user="alice", heavy={"account": "LabX"})
    assert draft["effective"]["site"]["cluster_id"] == "cluster-a"
    assert draft["effective"]["execution"]["worker_python"] == "/shared/raven/bin/python"
    changed = setup.stage("Lab X", "ssh-slurm", host="other", user="bob")
    assert changed["effective"]["site"]["cluster_id"] == ""
    assert changed["effective"]["execution"]["worker_python"] == ""


def test_save_refuses_stale_external_edit(tmp_path):
    setup = service(tmp_path, {"schema": C.SCHEMA, "ui": {"theme": "amber"}})
    draft = setup.stage("Workstation", "local-slurm")
    changed = {"schema": C.SCHEMA, "ui": {"theme": "green"}}
    setup.path.write_text(json.dumps(changed))
    with pytest.raises(ValueError, match="changed"):
        setup.save(draft)
    assert json.loads(setup.path.read_text()) == changed


def test_symlink_is_not_a_setup_destination(tmp_path):
    setup = service(tmp_path)
    setup.path.parent.mkdir(parents=True)
    outside = tmp_path / "other.json"
    outside.write_text(json.dumps({"schema": C.SCHEMA}))
    setup.path.symlink_to(outside)
    with pytest.raises(OSError):
        setup.stage("Workstation", "local-slurm")


def test_captured_policy_applies_to_new_sites_and_units(tmp_path):
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"schema": C.POLICY_SCHEMA, "restrictions": {
        "allowed_accounts": ["LabX"], "allow_desktop": False, "ceilings": {"allocation.memory_mib": 8192}}}))
    resolved = C.resolve(env=environment(tmp_path), system_policy_paths=[str(policy)])
    setup = P.ProfileSetup(resolved, env=environment(tmp_path))
    with pytest.raises(ValueError, match="forbid"):
        setup.stage("Standalone", "standalone")
    with pytest.raises(ValueError, match="policy"):
        setup.stage("Other lab", "local-slurm", heavy={"account": "LabY"})
    draft = setup.stage("Lab X", "local-slurm", heavy={"account": "LabX", "memory_mib": "16 GiB"})
    assert draft["effective"]["allocation"]["memory_mib"] == 8192
    assert draft["sources"]["allocation.memory_mib"] == "site policy"
    saved = setup.save(draft)
    persisted = json.loads(Path(saved["path"]).read_text())
    assert persisted["profiles"]["lab-x"]["allocation"]["memory_mib"] == 8192


def test_new_remote_discovery_does_not_query_existing_connection(tmp_path, monkeypatch):
    setup = service(tmp_path)
    monkeypatch.setattr(setup._discovery, "discover", lambda **kw: pytest.fail("queried wrong host"))
    result = setup.discover(setup.stage("HPC", "ssh-slurm", host="hpc"))
    assert result["accounts"] == []
    assert "connect" in result["errors"][0]


def test_existing_connection_discovery_reuses_governed_service(tmp_path, monkeypatch):
    setup = service(tmp_path)
    calls = []
    monkeypatch.setattr(setup._discovery, "discover", lambda **kw: calls.append(kw) or {"username": "alice", "accounts": []})
    assert setup.discover(setup.stage("Local", "local-slurm"), refresh=True)["username"] == "alice"
    assert calls == [{"refresh": True}]


@pytest.mark.parametrize("args", [{"host": "-oProxyCommand=bad"}, {"host": "alice@hpc"}, {"host": "hpc", "user": "--bad"}, {"host": "hpc", "worker_python": "python3"}])
def test_ssh_fields_are_separate_safe_and_explicit(tmp_path, args):
    setup = service(tmp_path)
    with pytest.raises(ValueError):
        setup.stage("Cluster", "ssh-slurm", **args)
    assert list(tmp_path.iterdir()) == []


def test_standalone_resource_limits_go_to_the_actual_local_worker(tmp_path):
    setup = service(tmp_path)
    draft = setup.stage("Standalone", "standalone", heavy={"cpus_per_task": 4, "memory_mib": "8 GiB", "walltime_seconds": "2h"})
    assert draft["effective"]["resources"]["heavy"]["max_workers"] == 4
    assert draft["effective"]["resources"]["heavy"]["memory_mib"] == 8192
    assert draft["effective"]["resources"]["heavy"]["wall_seconds"] == 7200


def test_workload_recipe_is_separate_portable_and_preserves_exports(tmp_path):
    defaults = P.workload_defaults(account="LabX", cpus="8", memory="8 GiB", time_limit="1h", gpus="1")
    assert defaults == {"account": "LabX", "cpus-per-task": "8", "mem": "8192M", "time": "01:00:00", "gpus": "1"}
    L.save_settings(tmp_path, {"export_presets": {"Figures": {}}})
    snapshot = L.load_settings(tmp_path)
    saved = P.save_project_recipe(tmp_path, "lab-gpu", {"sbatch_defaults": defaults, "cpu_memory": True}, expected=snapshot)
    assert saved["recipes"]["lab-gpu"]["sbatch_defaults"] == defaults
    assert saved["export_presets"] == {"Figures": {}}
    assert not (tmp_path / "config").exists()
    from raven.launch import effective_scheduler
    options, sources, _ = effective_scheduler(["#SBATCH --account=OtherLab", "#SBATCH --mem=4G"], [], {}, defaults)
    assert options["account"] == "OtherLab"
    assert options["mem"] == "4G"
    assert sources["account"] == "script directive"
    assert sources["cpus-per-task"] == "project resource default"


def test_recipe_save_rejects_stale_review_and_invalid_names(tmp_path):
    snapshot = L.load_settings(tmp_path)
    L.update_settings(tmp_path, {"launch": {"gpu": True}})
    with pytest.raises(ValueError, match="changed"):
        P.save_project_recipe(tmp_path, "lab", {"gpu": False}, expected=snapshot)
    assert L.load_settings(tmp_path)["launch"]["gpu"] is True
    assert L.load_settings(tmp_path)["recipes"] == {}
    with pytest.raises(ValueError):
        P.save_project_recipe(tmp_path, "invalid name", {})


@pytest.mark.skipif(sys.version_info < (3, 11), reason="TOML read needs Python 3.11")
def test_named_profiles_preserve_toml_values(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('schema = "raven.config/v1"\n[ui]\ntheme = "amber"\n')
    setup = P.ProfileSetup(C.resolve(config_path=str(path), env=environment(tmp_path), system_policy_paths=[]), env=environment(tmp_path))
    setup.save(setup.stage("Workstation", "local-slurm"))
    saved = C.resolve(config_path=str(path), env=environment(tmp_path), system_policy_paths=[])
    assert saved.data["ui"]["theme"] == "amber"
    assert saved.data["ui"]["profile_label"] == "Workstation"
