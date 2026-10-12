"""Allocation Setup tests use fixtures only; no Slurm jobs are requested."""
from __future__ import annotations

import copy
import errno
import json
import os
from pathlib import Path
import stat
import threading
from types import SimpleNamespace

import pytest

from raven import raven_config as C
from raven import setup_config as S
from raven.platform_session import wrap_backend
from raven.raven_allocation import AllocationManager
from raven.raven_collection import classify
from raven.slurm import CommandError


def environment(tmp_path):
    return {"HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_STATE_HOME": str(tmp_path / "state")}


def resolved(tmp_path, document=None, **kwargs):
    env = environment(tmp_path)
    if document is not None:
        path = tmp_path / "config.json"
        path.write_text(json.dumps(document))
        kwargs["config_path"] = str(path)
    return C.resolve(env=env, system_policy_paths=[], **kwargs)


class Source:
    def __init__(self, user="alice", denied=False):
        self.user, self.denied, self.calls = user, denied, []

    def run(self, argv, timeout=8.):
        self.calls.append(list(argv))
        if argv == ["id", "-un"]:
            return self.user + "\n", .001
        if argv[0] == "sacctmgr":
            if self.denied:
                raise CommandError("Association information is restricted")
            return (f"{self.user}|personal|cluster-a|personal||normal|normal\n"
                    f"{self.user}|personal|cluster-a|lab-x|gpu|gpu,normal|gpu\n"
                    f"{self.user}|personal|cluster-b|other-lab||normal|normal\n"
                    "someone_else|wrong|cluster-a|wrong||normal|normal\n"), .001
        if argv[0] == "sinfo":
            return "main*\ngpu\nmain*\n", .001
        if argv == ["scontrol", "show", "config"]:
            return "ClusterName             = cluster-a\n", .001
        raise AssertionError(argv)


def governed(tmp_path, config, source):
    cfg = C.to_raven_config(config)
    cfg["collection_cache_dir"] = str(tmp_path / "collection")
    return wrap_backend(source, cfg, {"cluster": "fixture", "user": source.user}, None)


def test_constructor_does_no_io(tmp_path, monkeypatch):
    config = resolved(tmp_path)
    monkeypatch.setattr(S, "_read_snapshot", lambda _: pytest.fail("constructor read configuration"))
    service = S.AllocationSetup(config, env=environment(tmp_path))
    assert not service.path.exists()


def test_discovery_through_governed_connection_and_cached(tmp_path, monkeypatch):
    config = resolved(tmp_path)
    source = Source()
    monkeypatch.setattr(S.pwd, "getpwuid", lambda _: SimpleNamespace(pw_name="alice"))
    backend = governed(tmp_path, config, source)
    try:
        service = S.AllocationSetup(config, backend, env=environment(tmp_path))
        result = service.discover()
        assert result["errors"] == []
        assert result["username"] == "alice"
        assert result["cluster"] == "cluster-a"
        assert result["partitions"] == ["gpu", "main"]
        assert [row["account"] for row in result["accounts"]] == ["personal", "lab-x", "other-lab"]
        assert result["accounts"][0]["default"] is True
        assert result["accounts"][1]["qos"] == ["gpu", "normal"]
        assert len(source.calls) == 3
        # Returned state is detached from the cache; a UI edit cannot corrupt
        # another selection or change the identity used in later queries.
        result["accounts"].clear()
        assert len(service.discover()["accounts"]) == 3
        assert len(source.calls) == 3
        assert all(classify(command).read_only for command in source.calls)
    finally:
        backend.close()


def test_explicit_refresh_retries_same_governed_cache_without_bypassing_policy(tmp_path, monkeypatch):
    config = resolved(tmp_path)
    source = Source()
    monkeypatch.setattr(S.pwd, "getpwuid", lambda _: SimpleNamespace(pw_name="alice"))
    backend = governed(tmp_path, config, source)
    requests, run = [], backend.run
    def counted(argv, timeout=8.):
        requests.append(list(argv))
        return run(argv, timeout)
    monkeypatch.setattr(backend, "run", counted)
    try:
        service = S.AllocationSetup(config, backend, env=environment(tmp_path))
        first = service.discover()
        assert len(requests) == len(source.calls) == 3
        service.discover()
        assert len(requests) == len(source.calls) == 3
        refreshed = service.discover(refresh=True)
        assert refreshed["accounts"] == first["accounts"]
        assert refreshed["errors"] == []
        assert len(requests) == 6
        assert requests[:3] == requests[3:]
        # The governed cache remains authoritative: no fresh command or quota
        # bypass is used just because the user clicked Refresh in Setup.
        assert len(source.calls) == 3
        service.discover()
        assert len(requests) == 6
    finally:
        backend.close()


def test_remote_identity_uses_connection_not_local_user_or_alias(tmp_path):
    config = resolved(tmp_path, {"schema": C.SCHEMA,
                               "connection": {"transport": "ssh", "host": "hpc", "user": "alias-user"}})
    source = Source(user="actual_remote")
    backend = governed(tmp_path, config, source)
    try:
        result = S.AllocationSetup(config, backend, env=environment(tmp_path)).discover()
        assert result["username"] == "actual_remote"
        assert source.calls[0] == ["id", "-un"]
        query = next(command for command in source.calls if command[0] == "sacctmgr")
        assert "name=actual_remote" in query
        assert "name=alias-user" not in query
    finally:
        backend.close()


def test_restricted_associations_keep_manual_and_default_account_available(tmp_path, monkeypatch):
    config = resolved(tmp_path)
    source = Source(denied=True)
    monkeypatch.setattr(S.pwd, "getpwuid", lambda _: SimpleNamespace(pw_name="alice"))
    backend = governed(tmp_path, config, source)
    try:
        service = S.AllocationSetup(config, backend, env=environment(tmp_path))
        result = service.discover()
        assert result["accounts"] == []
        assert "restricted" in " ".join(result["errors"])
        assert service.stage({"account": "assigned-lab"})["effective"]["allocation"]["account"] == "assigned-lab"
        default = service.stage({"account": ""})
        assert "site limits still apply" in " ".join(default["warnings"])
    finally:
        backend.close()


def test_standalone_discovery_never_contacts_slurm(tmp_path, monkeypatch):
    config = resolved(tmp_path, site="desktop")
    source = Source()
    monkeypatch.setattr(S.pwd, "getpwuid", lambda _: SimpleNamespace(pw_name="alice"))
    backend = governed(tmp_path, config, source)
    try:
        service = S.AllocationSetup(config, backend, env=environment(tmp_path))
        assert service.discover()["username"] == "alice"
        assert source.calls == []
        with pytest.raises(ValueError, match="standalone"):
            service.stage({"account": "lab"})
    finally:
        backend.close()


@pytest.mark.parametrize("text", ["alice|lab|cluster|missing\n", "x" * (S.MAX_DISCOVERY + 1),
                                  "alice|lab|cluster|lab||normal|normal\n" * 1025,
                                  "alice|lab|cluster|lab;rm||normal|normal\n"])
def test_malformed_or_oversized_associations_are_not_suggestions(text):
    with pytest.raises(ValueError):
        S.parse_associations(text, "alice")


def test_associations_filter_other_users_and_preserve_partition_scope():
    result = S.parse_associations("alice|lab|a|lab|gpu|normal,gpu|gpu|\n"
                                 "alice|lab|a|lab|cpu|normal|normal|\n"
                                 "other|other|a|other||normal|normal|\n", "alice")
    assert [(row["account"], row["partition"]) for row in result] == [("lab", "cpu"), ("lab", "gpu")]


@pytest.mark.parametrize("changes", [{"cpus_per_task": 0}, {"cpus_per_task": 1.2},
                                    {"cpus_per_task": True}, {"memory_mib": 127},
                                    {"walltime_seconds": 604801}, {"nodes": 2}, {"tasks": 2},
                                    {"account": "a b"}, {"qos": "--bad"}, {"account": "a\nb"},
                                    {"gpus": -1}, {"adapter": "desktop"}])
def test_stage_rejects_invalid_launch_resources(tmp_path, changes):
    service = S.AllocationSetup(resolved(tmp_path), env=environment(tmp_path))
    with pytest.raises(ValueError):
        service.stage(changes)
    assert not service.path.exists()


def test_save_creates_private_config_and_updates_only_light_preferences(tmp_path):
    config = resolved(tmp_path)
    manager = AllocationManager(config)
    service = S.AllocationSetup(config, manager=manager, env=environment(tmp_path))
    draft = service.stage({"account": "lab-x", "partition": "main", "cpus_per_task": 4,
                           "memory_mib": 8192, "walltime_seconds": 1200})
    assert manager.config["allocation"]["account"] == ""
    result = service.save(draft)
    assert stat.S_IMODE(service.path.stat().st_mode) == 0o600
    assert manager.config["allocation"]["account"] == "lab-x"
    assert manager.snapshot()["state"] == "light"
    assert manager._process is None
    assert result["resolved"].source == str(service.path)
    reloaded = C.resolve(env=environment(tmp_path), system_policy_paths=[])
    assert reloaded.data["allocation"] == result["allocation"]
    assert reloaded.data["execution"]["startup"] == "light"


def test_save_preserves_other_profiles_and_unrelated_values(tmp_path):
    original = {"schema": C.SCHEMA, "profile": "carc", "ui": {"theme": "amber"},
                "allocation": {"account": "base", "cpus_per_task": 8},
                "profiles": {"carc": {"allocation": {"account": "lab-x", "partition": "main"}},
                             "uci": {"allocation": {"account": "lab-y", "partition": "standard"}}}}
    config = resolved(tmp_path, original)
    service = S.AllocationSetup(config, env=environment(tmp_path))
    service.save(service.stage({"account": "lab-z", "memory_mib": 16384}))
    expected = copy.deepcopy(original)
    expected["profiles"]["carc"]["allocation"].update(account="lab-z", memory_mib=16384)
    assert json.loads(service.path.read_text()) == expected
    result = C.resolve(config_path=str(service.path), env=environment(tmp_path), system_policy_paths=[])
    assert result.data["allocation"]["cpus_per_task"] == 8
    assert result.data["allocation"]["account"] == "lab-z"


def test_command_line_allocation_override_remains_authoritative(tmp_path):
    config = resolved(tmp_path, cli_overrides={"allocation": {"account": "fixed"}})
    service = S.AllocationSetup(config, env=environment(tmp_path))
    with pytest.raises(ValueError, match="command-line"):
        service.stage({"account": "lab"})
    result = service.save(service.stage({"partition": "main", "account": "fixed"}))
    assert result["allocation"]["account"] == "fixed"
    assert "account" not in json.loads(service.path.read_text())["allocation"]
    assert result["resolved"].origins["allocation.account"] == "CLI"
    with pytest.raises(ValueError, match="command-line"):
        service.stage({"account": "lab"})


def test_captured_site_restrictions_cannot_be_bypassed(tmp_path):
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"schema": C.POLICY_SCHEMA, "restrictions": {
        "allowed_accounts": ["lab-x"], "require_account": True,
        "ceilings": {"allocation.cpus_per_task": 4}}}))
    config = C.resolve(env=environment(tmp_path), system_policy_paths=[str(policy)])
    service = S.AllocationSetup(config, env=environment(tmp_path))
    with pytest.raises(ValueError, match="policy"):
        service.stage({"account": "elsewhere"})
    with pytest.raises(ValueError, match="account"):
        service.stage({"account": ""})
    stage = service.stage({"account": "lab-x", "cpus_per_task": 8})
    assert stage["effective"]["allocation"]["cpus_per_task"] == 4
    assert "site policy" in " ".join(stage["warnings"])
    result = service.save(stage)
    assert json.loads(service.path.read_text())["allocation"]["cpus_per_task"] == 4
    assert result["resolved"].restrictions == config.restrictions


@pytest.mark.parametrize("state", ["preparing", "pending", "starting", "heavy", "draining", "reconciling"])
def test_active_allocation_is_not_reconfigured(tmp_path, state):
    config = resolved(tmp_path)
    manager = AllocationManager(config)
    manager._state["state"] = state
    original = copy.deepcopy(manager.config)
    service = S.AllocationSetup(config, manager=manager, env=environment(tmp_path))
    with pytest.raises(ValueError, match="Return to Light"):
        service.save(service.stage({"account": "lab"}))
    assert not service.path.exists()
    assert manager.config == original


def test_changed_source_is_not_overwritten(tmp_path):
    config = resolved(tmp_path, {"schema": C.SCHEMA, "ui": {"theme": "terminal"}})
    service = S.AllocationSetup(config, env=environment(tmp_path))
    draft = service.stage({"account": "lab"})
    changed = json.dumps({"schema": C.SCHEMA, "ui": {"theme": "amber"}})
    service.path.write_text(changed)
    with pytest.raises(ValueError, match="Configuration changed.*relaunch Raven"):
        service.save(draft)
    assert service.path.read_text() == changed


@pytest.mark.parametrize("existing", [False, True])
def test_directory_fsync_failure_is_a_saved_durability_warning(tmp_path, monkeypatch, existing):
    config = resolved(tmp_path, {"schema": C.SCHEMA} if existing else None)
    manager = AllocationManager(config)
    service = S.AllocationSetup(config, manager=manager, env=environment(tmp_path))
    fsync = S.os.fsync
    def unsupported_directory_sync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "Directory fsync is unavailable")
        return fsync(fd)
    monkeypatch.setattr(S.os, "fsync", unsupported_directory_sync)
    result = service.save(service.stage({"account": "lab-x"}))
    assert json.loads(service.path.read_text())["allocation"]["account"] == "lab-x"
    assert manager.config["allocation"]["account"] == "lab-x"
    assert service.resolved.data["allocation"]["account"] == "lab-x"
    assert result["resolved"].data["allocation"]["account"] == "lab-x"
    assert "Settings were saved" in " ".join(result["warnings"])
    assert "directory durability" in " ".join(result["warnings"])
    assert manager.snapshot()["state"] == "light"
    assert manager._process is None
    # A durability warning must not invalidate a successfully published file
    # token or force the next edit to use an outdated runtime configuration.
    service.save(service.stage({"partition": "main"}))
    assert manager.config["allocation"]["partition"] == "main"


def test_file_fsync_failure_before_publication_leaves_preferences_unchanged(tmp_path, monkeypatch):
    config = resolved(tmp_path, {"schema": C.SCHEMA, "allocation": {"account": "old"}})
    manager = AllocationManager(config)
    service = S.AllocationSetup(config, manager=manager, env=environment(tmp_path))
    before = service.path.read_bytes()
    def cannot_sync(_):
        raise OSError(errno.EIO, "Unable to sync temporary configuration")
    monkeypatch.setattr(S.os, "fsync", cannot_sync)
    with pytest.raises(OSError, match="Unable to sync"):
        service.save(service.stage({"account": "lab-x"}))
    assert service.path.read_bytes() == before
    assert manager.config["allocation"]["account"] == "old"
    assert service.resolved.data["allocation"]["account"] == "old"


def test_post_publication_external_replacement_is_not_adopted_as_our_baseline(tmp_path, monkeypatch):
    config = resolved(tmp_path, {"schema": C.SCHEMA})
    service = S.AllocationSetup(config, env=environment(tmp_path))
    replace = S.os.replace
    external = {"schema": C.SCHEMA, "allocation": {"account": "external"}}
    def another_writer_after_publication(source, destination):
        replace(source, destination)
        alternate = tmp_path / "another-config.json"
        alternate.write_text(json.dumps(external))
        replace(alternate, destination)
    monkeypatch.setattr(S.os, "replace", another_writer_after_publication)
    service.save(service.stage({"account": "lab-x"}))
    with pytest.raises(ValueError, match="Configuration changed.*relaunch Raven"):
        service.save(service.stage({"partition": "main"}))
    assert json.loads(service.path.read_text()) == external


def test_new_destination_is_not_overwritten_if_created_during_edit(tmp_path):
    service = S.AllocationSetup(resolved(tmp_path), env=environment(tmp_path))
    draft = service.stage({"account": "lab"})
    service.path.parent.mkdir(parents=True)
    original = '{"schema":"raven.config/v1"}\n'
    service.path.write_text(original)
    with pytest.raises(ValueError, match="Configuration changed"):
        service.save(draft)
    assert service.path.read_text() == original


def test_symlink_source_is_never_replaced(tmp_path):
    actual = tmp_path / "actual.json"
    actual.write_text('{"schema":"raven.config/v1"}')
    config = resolved(tmp_path)
    service = S.AllocationSetup(config, env=environment(tmp_path))
    service.path.parent.mkdir(parents=True)
    service.path.symlink_to(actual)
    with pytest.raises(OSError):
        service.stage({"account": "lab"})
    assert actual.read_text() == '{"schema":"raven.config/v1"}'
    assert service.path.is_symlink()


def test_toml_save_preserves_profile_values(tmp_path):
    pytest.importorskip("tomllib")
    path = tmp_path / "config.toml"
    path.write_text('schema = "raven.config/v1"\nprofile = "lab.site"\n'
                    '[ui]\ntheme = "terminal"\n[profiles."lab.site".allocation]\naccount = "lab"\n'
                    '[profiles.other.allocation]\naccount = "other"\n')
    config = resolved(tmp_path, config_path=str(path))
    service = S.AllocationSetup(config, env=environment(tmp_path))
    result = service.save(service.stage({"account": "new-lab"}))
    assert "comments and formatting" in " ".join(result["warnings"])
    loaded = C.read_file(path)
    assert loaded["profiles"]["lab.site"]["allocation"]["account"] == "new-lab"
    assert loaded["profiles"]["other"]["allocation"]["account"] == "other"
    assert C.resolve(str(path), env=environment(tmp_path), system_policy_paths=[]).data["allocation"]["account"] == "new-lab"


def test_save_revalidates_draft_instead_of_trusting_candidate(tmp_path):
    service = S.AllocationSetup(resolved(tmp_path), env=environment(tmp_path))
    draft = service.stage({"account": "lab"})
    draft["effective"]["execution"]["allow_login_heavy"] = True
    result = service.save(draft)
    assert result["data"]["execution"]["allow_login_heavy"] is False


@pytest.mark.parametrize("method,args", [("request", ()), ("request_reuse_review", ("123",))])
def test_manager_requests_fail_fast_during_background_config_save(tmp_path, method, args):
    manager = AllocationManager(resolved(tmp_path))
    entered, done = threading.Event(), threading.Event()
    def save_owns_lock():
        with manager._lock:
            entered.set()
            assert done.wait(5)
    thread = threading.Thread(target=save_owns_lock)
    thread.start()
    try:
        assert entered.wait(5)
        # The method must return before releasing the background writer, so
        # this also detects an accidental blocking acquire in the UI path.
        with pytest.raises(RuntimeError, match="settings are busy"):
            getattr(manager, method)(*args)
    finally:
        done.set()
        thread.join(5)
    assert manager._process is None


def test_remote_username_discovery_command_has_exact_read_only_grammar():
    assert classify(["id", "-un"]).read_only is True
    assert classify(["id", "-un"]).endpoint == "local"
    for command in (["id", "-un", "someone"], ["id", "--name"], ["sh", "-c", "id -un"]):
        assert classify(command) is None
