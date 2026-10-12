import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess

import pytest

from raven import provenance as p


def script(tmp_path):
    path = tmp_path / "run script's.sh"
    path.write_text("#!/bin/bash\necho science\n")
    return path


def signed(value):
    value["id"] = p._identity(value)
    value["checksum"] = p._checksum(value)
    return value


def test_capture_fingerprints_without_execution(tmp_path):
    path = script(tmp_path)
    passport = p.capture(tmp_path, path.name, resources={"cpus": 8}, parameters={"input size": 12})
    assert passport["script"]["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert passport["script"]["path"] == str(path)
    assert passport["script"]["size"] == path.stat().st_size
    assert passport["resources"] == {"cpus": 8}
    assert passport["parameters"] == {"input size": 12}
    assert passport["git"] == {"revision": None, "dirty": None, "changed_paths": None, "changed_paths_truncated": False}
    assert passport["modules"] is None and passport["container"] is None
    assert not (tmp_path / "science").exists()


def test_identity_excludes_timestamp_but_includes_evidence(tmp_path):
    path = script(tmp_path)
    left = p.capture(tmp_path, path)
    right = p.capture(tmp_path, path)
    assert left["id"] == right["id"]
    assert left["captured_at"] != right["captured_at"]
    assert p.diff(left, right) == []
    path.write_text("different content\n")
    different = p.capture(tmp_path, path)
    assert different["id"] != left["id"]
    assert "/script/sha256" in {change["path"] for change in p.diff(left, different)}


def test_only_declared_inputs_and_opt_in_hash(tmp_path, monkeypatch):
    data = tmp_path / "dataset.csv"
    data.write_text("1,2\n3,4\n")
    unrelated = tmp_path / "private.txt"
    unrelated.write_text("password data never read")
    metadata = p.capture(tmp_path, inputs=[data.name])
    assert metadata["inputs"][0]["sha256"] is None
    assert "private" not in json.dumps(metadata)
    hashed = p.capture(tmp_path, inputs=[{"path": data.name, "hash": True, "max_bytes": data.stat().st_size}])
    assert hashed["inputs"][0]["sha256"] == hashlib.sha256(data.read_bytes()).hexdigest()
    with pytest.raises(p.ValidationError, match="budget"):
        p.capture(tmp_path, inputs=[{"path": data.name, "hash": True, "max_bytes": 1}])
    monkeypatch.setattr(p, "MAX_INPUT_HASH_BYTES", 12)
    with pytest.raises(p.ValidationError, match="budget"):
        p.capture(tmp_path, inputs=[{"path": data.name, "hash": True}, {"path": data.name, "hash": True}])


def test_symlinks_and_explicit_outside_workdir(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    path = script(tmp_path)
    link = root / "linked.sh"
    link.symlink_to(path)
    passport = p.capture(root, script=link.name, inputs=["../" + path.name])
    assert passport["script"]["path"] == str(link)
    assert passport["script"]["resolved_path"] == str(path)
    assert passport["script"]["symlink"] is True
    assert passport["inputs"][0]["path"] == str(path)
    directory_link = tmp_path / "work-link"
    directory_link.symlink_to(root)
    assert p.capture(directory_link)["workdir"] == str(root)


@pytest.mark.parametrize("kind", ["missing", "directory", "fifo", "broken-symlink"])
def test_reject_nonregular_files_without_hanging(tmp_path, kind):
    path = tmp_path / "file"
    if kind == "directory":
        path.mkdir()
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind == "broken-symlink":
        path.symlink_to(tmp_path / "absent")
    with pytest.raises(p.ValidationError):
        p.capture(tmp_path, path)
    with pytest.raises(p.ValidationError):
        p.capture(tmp_path, inputs=[path])


def test_script_budget_and_mutation_detection(tmp_path, monkeypatch):
    path = script(tmp_path)
    monkeypatch.setattr(p, "MAX_SCRIPT_BYTES", 4)
    with pytest.raises(p.ValidationError, match="budget"):
        p.capture(tmp_path, path)
    monkeypatch.setattr(p, "MAX_SCRIPT_BYTES", 1024)
    original_read = p.os.read
    mutated = False
    def changing(fd, count):
        nonlocal mutated
        chunk = original_read(fd, count)
        if not mutated:
            mutated = True
            with path.open("a") as stream:
                stream.write("appended content\n")
        return chunk
    monkeypatch.setattr(p.os, "read", changing)
    with pytest.raises(p.ValidationError, match="changed during capture"):
        p.capture(tmp_path, path)


def test_git_dirty_revision_rename_and_untracked_policy(tmp_path):
    def git(*args):
        return subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True, text=True).stdout.strip()
    git("init", "-q")
    path = script(tmp_path)
    git("add", path.name)
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "initial")
    clean = p.capture(tmp_path, path)
    assert clean["git"]["revision"] == git("rev-parse", "HEAD")
    assert clean["git"]["dirty"] is False
    (tmp_path / "untracked.txt").write_text("not declared")
    assert p.capture(tmp_path)["git"]["dirty"] is False
    path.write_text("changed\n")
    assert p.capture(tmp_path)["git"]["changed_paths"] == [path.name]
    renamed = tmp_path / "renamed.sh"
    git("mv", path.name, renamed.name)
    assert p.capture(tmp_path)["git"]["changed_paths"] == [renamed.name]


def test_git_does_not_capture_origin_or_inherited_overrides(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "remote", "add", "origin", "https://username:password@example.invalid/repo.git"], check=True)
    monkeypatch.setenv("GIT_DIR", str(root / ".git"))
    value = p.capture(tmp_path)
    assert value["git"]["dirty"] is None
    assert "password" not in json.dumps(value)
    assert "origin" not in json.dumps(p.capture(root))


def test_git_timeout_is_explicit_unknown(tmp_path, monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])
    monkeypatch.setattr(p.subprocess, "run", timeout)
    assert p.capture(tmp_path)["git"]["dirty"] is None


def test_environment_explicit_allowlist_and_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("LOADEDMODULES", "python/3.12:cuda/12")
    monkeypatch.setenv("APPTAINER_CONTAINER", "/containers/science.sif")
    monkeypatch.setenv("OMP_NUM_THREADS", "8")
    monkeypatch.setenv("RAVEN_API_TOKEN", "secret")
    empty = p.capture(tmp_path)
    assert empty["environment"] == {}
    assert "secret" not in json.dumps(empty)
    selected = p.capture(tmp_path, environment_names=["LOADEDMODULES", "APPTAINER_CONTAINER", "OMP_NUM_THREADS", "RAVEN_UNSET_EXAMPLE"])
    assert selected["modules"] == ["python/3.12", "cuda/12"]
    assert selected["container"] == {"APPTAINER_CONTAINER": "/containers/science.sif"}
    assert selected["environment"]["RAVEN_UNSET_EXAMPLE"] is None
    assert "RAVEN_API_TOKEN" not in selected["environment"]


@pytest.mark.parametrize("name", ["TOKEN", "AWS_SECRET_ACCESS_KEY", "SSH_KEY", "AUTH", "GITHUB_TOKEN", "SECRET", "API_KEY", "PASSWORD", "bad-name", ""])
def test_secret_or_invalid_environment_names_rejected(tmp_path, name):
    with pytest.raises(p.ValidationError, match="environment"):
        p.capture(tmp_path, environment_names=[name])


def test_limits_and_secret_parameter_rejection(tmp_path, monkeypatch):
    monkeypatch.setenv("OMP_NUM_THREADS", "a" * 4097)
    with pytest.raises(p.ValidationError, match="4096"):
        p.capture(tmp_path, environment_names=["OMP_NUM_THREADS"])
    with pytest.raises(p.ValidationError, match="64"):
        p.capture(tmp_path, environment_names=("OMP_NUM_THREADS" for _ in range(100)))
    with pytest.raises(p.ValidationError, match="secret-like"):
        p.capture(tmp_path, parameters={"nested": [{"api_token": "x"}]})
    with pytest.raises(p.ValidationError, match="finite"):
        p.capture(tmp_path, parameters={"loss": float("nan")})
    with pytest.raises(p.ValidationError, match="JSON"):
        p.capture(tmp_path, resources={"cpus": object()})
    with pytest.raises(p.ValidationError, match="collections"):
        p.capture(tmp_path, inputs="a.csv")
    with pytest.raises(p.ValidationError, match="collections"):
        p.capture(tmp_path, environment_names="PATH")


def test_host_credentials_removed_and_job_id_validation(tmp_path):
    passport = p.capture(tmp_path, host="ssh://alice:password@login.example:22/anything?secret=x", job_id=123)
    assert passport["host"] == "ssh://login.example:22"
    assert passport["job_id"] == "123"
    assert p.capture(tmp_path, host="alice@login.example")["host"] == "login.example"
    for job_id in [True, -1, "123; cancel", "x", 1.2]:
        with pytest.raises(p.ValidationError, match="job ID"):
            p.capture(tmp_path, job_id=job_id)


def test_save_private_immutable_and_concurrent(tmp_path):
    passport = p.capture(tmp_path)
    directory = tmp_path / "passports"
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        targets = list(pool.map(lambda _: p.save(passport, directory), range(64)))
    assert len(set(targets)) == 1
    target = targets[0]
    assert target.name == passport["id"] + ".json"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert p.load(target) == passport
    original = target.read_bytes()
    equivalent = signed(dict(passport, captured_at="2000-01-01T00:00:00+00:00"))
    p.save(equivalent, directory)
    assert target.read_bytes() == original
    assert list(directory.iterdir()) == [target]


def test_save_refuses_existing_invalid_and_insecure_files(tmp_path):
    passport = p.capture(tmp_path)
    directory = tmp_path / "passports"
    target = p.save(passport, directory)
    target.chmod(0o644)
    with pytest.raises(p.ValidationError, match="private"):
        p.save(passport, directory)
    target.write_text("not JSON")
    with pytest.raises(p.ValidationError, match="malformed"):
        p.save(passport, directory)
    assert target.read_text() == "not JSON"


def test_load_rejects_symlink_and_nonregular(tmp_path):
    target = p.save(p.capture(tmp_path), tmp_path / "passports")
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(p.ValidationError):
        p.load(link)
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(p.ValidationError):
        p.load(fifo)


@pytest.mark.parametrize("data", [b"{", b"[]", b"null", b'{"a":1,"a":2}', b'{"x":NaN}', b'\xff', b"[" * 1500])
def test_load_rejects_malformed_json(tmp_path, data):
    target = tmp_path / "invalid.json"
    target.write_bytes(data)
    with pytest.raises(p.ValidationError):
        p.load(target)


def test_load_size_and_digest_tamper(tmp_path, monkeypatch):
    target = p.save(p.capture(tmp_path), tmp_path / "passports")
    changed = json.loads(target.read_text())
    changed["resources"]["cpus"] = 999
    target.write_text(json.dumps(changed))
    with pytest.raises(p.ValidationError, match="modified or corrupt"):
        p.load(target)
    monkeypatch.setattr(p, "MAX_PASSPORT_BYTES", 32)
    with pytest.raises(p.ValidationError, match="limit"):
        p.load(target)


def test_timestamp_tamper_does_not_evade_integrity_check(tmp_path):
    target = p.save(p.capture(tmp_path), tmp_path / "passports")
    changed = json.loads(target.read_text())
    changed["captured_at"] = "2000-01-01T00:00:00+00:00"
    target.write_text(json.dumps(changed))
    with pytest.raises(p.ValidationError, match="checksum"):
        p.load(target)


def test_save_conflict_and_publication_failure_cleanup(tmp_path, monkeypatch):
    left = p.capture(tmp_path, parameters={"size": 1})
    right = p.capture(tmp_path, parameters={"size": 2})
    directory = tmp_path / "passports"
    target = p.save(left, directory)
    target.write_text(json.dumps(right))
    with pytest.raises(p.ValidationError, match="conflicts"):
        p.save(left, directory)
    target.unlink()
    def fail_link(*args, **kwargs):
        raise OSError("publication failed")
    monkeypatch.setattr(p.os, "link", fail_link)
    with pytest.raises(OSError, match="publication failed"):
        p.save(left, directory)
    assert list(directory.iterdir()) == []


@pytest.mark.parametrize("field,value", [("version", 2), ("version", True), ("captured_at", "today"),
    ("captured_at", "2026-01-01T12:00:00"), ("workdir", "relative"), ("script", {}),
    ("inputs", [{}]), ("environment", {"PASSWORD": "oops"}), ("git", {}),
    ("resources", None), ("parameters", []), ("modules", [1]), ("container", {"UNKNOWN": "x"}),
    ("job_id", "oops"), ("host", 5)])
def test_signed_invalid_schema_is_rejected(tmp_path, field, value):
    passport = p.capture(tmp_path)
    passport[field] = value
    signed(passport)
    with pytest.raises(p.ValidationError):
        p.validate(passport)


def test_diff_json_pointer_missing_null_and_types(tmp_path):
    left = p.capture(tmp_path, parameters={"a/b~c": None, "truth": True, "list": [True]})
    right = p.capture(tmp_path, parameters={"truth": 1, "list": [1]})
    changes = {item["path"]: item for item in p.diff(left, right)}
    assert changes["/parameters/a~1b~0c"]["left_present"] is True
    assert changes["/parameters/a~1b~0c"]["right_present"] is False
    assert "/parameters/truth" in changes and "/parameters/list" in changes
    left_path = p.save(left, tmp_path / "passports")
    right_path = p.save(right, tmp_path / "passports")
    assert p.diff(left_path, right_path) == p.diff(left, right)


def test_no_aliasing_of_caller_resource_parameters(tmp_path):
    resources = {"nested": {"cpus": [8]}}
    passport = p.capture(tmp_path, resources=resources)
    resources["nested"]["cpus"].append(16)
    assert passport["resources"]["nested"]["cpus"] == [8]


def test_scientific_tokenizer_parameters_are_not_credential_names(tmp_path):
    passport = p.capture(tmp_path, parameters={"tokenizer": "example", "max_tokens": 256})
    assert passport["parameters"] == {"tokenizer": "example", "max_tokens": 256}


def test_empty_input_file_hash_and_pre_epoch_timestamp(tmp_path):
    target = tmp_path / "empty"
    target.touch()
    os.utime(target, (-1, -1))
    passport = p.capture(tmp_path, inputs=[{"path": target, "hash": True, "max_bytes": 0}])
    assert passport["inputs"][0]["sha256"] == hashlib.sha256(b"").hexdigest()
    assert passport["inputs"][0]["mtime_ns"] == -1000000000


def test_metadata_validation_is_pure_and_matches_capture_constraints(monkeypatch):
    def no_inspection(*args, **kwargs):
        raise AssertionError("metadata validation must not inspect files or Git")
    monkeypatch.setattr(p, "_file", no_inspection)
    monkeypatch.setattr(p, "_git", no_inspection)
    p.validate_metadata(resources={"cpus": 4}, parameters={"seed": 7},
                        inputs=["absent.csv", {"path": "absent.py", "hash": True, "max_bytes": 1024}])
    with pytest.raises(p.ValidationError, match="secret-like"):
        p.validate_metadata(parameters={"private_key": "x"})
    with pytest.raises(p.ValidationError, match="128"):
        p.validate_metadata(inputs=["a"] * 129)
    nested = {}
    for _ in range(30):
        nested = {"n": nested}
    with pytest.raises(p.ValidationError, match="deep"):
        p.validate_metadata(parameters=nested)


@pytest.mark.parametrize("value", [None, 1, "a", b"a", {"path": "a"}])
def test_invalid_input_collection_errors_are_clear(tmp_path, value):
    with pytest.raises(p.ValidationError, match="collection"):
        p.validate_metadata(inputs=value)
    with pytest.raises(p.ValidationError, match="collection"):
        p.capture(tmp_path, inputs=value)


@pytest.mark.parametrize("item", [{"path": "a", "hash": 1}, {"path": "a", "other": 2},
    {"hash": True}, {"path": "a", "max_bytes": -1}, {"path": "a", "max_bytes": True},
    {"path": "a", "max_bytes": p.MAX_INPUT_HASH_BYTES + 1}, {"path": ""}, "a\x00b"])
def test_invalid_input_declarations_fail_before_inspection(item):
    with pytest.raises(p.ValidationError):
        p.validate_metadata(inputs=[item])
