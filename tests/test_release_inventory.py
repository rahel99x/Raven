import hashlib
import importlib.util
import io
import json
from pathlib import Path
import stat
import tarfile
import zipfile

import pytest

SPEC = importlib.util.spec_from_file_location("raven_release_inventory", Path(__file__).resolve().parents[1] / "scripts/release_inventory.py")
R = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(R)
LICENSE = R.DEFAULT_LICENSE.read_bytes()


def wheel(tmp_path, *, version="6.0.0", license_bytes=LICENSE, extra=(), runtime="", members=None):
    path = tmp_path / f"raven-{version}-py3-none-any.whl"
    metadata = f'Metadata-Version: 2.4\nName: raven\nVersion: {version}\nLicense-Expression: MIT\nRequires-Python: >=3.10\nProvides-Extra: dev\nRequires-Dist: pytest>=8; extra == "dev"\n{runtime}\n'
    prefix = f"raven-{version}.dist-info/"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(prefix + "METADATA", metadata)
        z.writestr(prefix + "WHEEL", "Wheel-Version: 1.0\nGenerator: test-builder\nTag: py3-none-any\n")
        z.writestr(prefix + "licenses/LICENSE", license_bytes)
        z.writestr("raven/__init__.py", "VERSION = '6.0.0'\n")
        for name, value in (members or {}).items():
            z.writestr(name, value)
        for info, value in extra:
            z.writestr(info, value)
    return path


def sdist(tmp_path, *, special=None, source=b"pass\n"):
    path = tmp_path / "raven-6.0.0.tar.gz"
    metadata = b'Metadata-Version: 2.4\nName: raven\nVersion: 6.0.0\nLicense-Expression: MIT\nRequires-Python: >=3.10\n\n'
    with tarfile.open(path, "w:gz") as archive:
        for name, content in (("PKG-INFO", metadata), ("LICENSE", LICENSE), ("raven/__init__.py", source)):
            entry = tarfile.TarInfo("raven-6.0.0/" + name)
            entry.size = len(content)
            archive.addfile(entry, io.BytesIO(content))
        if special:
            archive.addfile(special)
    return path


def test_both_artifacts_inventory_hashes_license_dependency_and_spdx(tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1000000000")
    artifacts = [wheel(tmp_path), sdist(tmp_path)]
    value = R.create_inventory(artifacts, source_commit="abcdef0123456789")
    assert value["version"] == "6.0.0" and value["created"] == "2001-09-09T01:46:40Z"
    assert value["source_commit_verification"].startswith("Caller-supplied")
    assert "MPI" in " ".join(value["external_components_excluded"])
    for artifact in value["artifacts"]:
        assert artifact["sha256"] == hashlib.sha256((tmp_path / artifact["filename"]).read_bytes()).hexdigest()
        assert not artifact["runtime_requires_dist"]
        assert artifact["license"]["sha256"] == hashlib.sha256(LICENSE).hexdigest()
        assert len(artifact["members"]) >= 3
    document = R.spdx_document(value)
    assert document["spdxVersion"] == "SPDX-2.3" and document["dataLicense"] == "CC0-1.0"
    assert len(document["packages"]) == 2
    assert all(p["licenseDeclared"] == "MIT" and not p["filesAnalyzed"] for p in document["packages"])
    assert document == R.spdx_document(value)


def test_cli_outputs_and_verification(tmp_path, capsys):
    artifacts = [wheel(tmp_path), sdist(tmp_path)]
    output, spdx, sums = [tmp_path / name for name in ("inventory.json", "sbom.json", "SHA256SUMS")]
    assert R.main(["create", "--output", str(output), "--spdx", str(spdx), "--checksums", str(sums), *map(str, artifacts)]) == 0
    assert len(sums.read_text().splitlines()) == 2
    assert json.loads(spdx.read_text())["spdxVersion"] == "SPDX-2.3"
    assert R.main(["verify", str(output), *map(str, artifacts)]) == 0
    assert "not signature verification" in capsys.readouterr().out


def test_tampered_archive_rejected_against_inventory(tmp_path):
    artifact = wheel(tmp_path)
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps(R.create_inventory([artifact])))
    with zipfile.ZipFile(artifact, "a") as archive:
        archive.writestr("unexpected.py", "changed")
    with pytest.raises(ValueError, match="artifacts changed"):
        R.verify_inventory(inventory, [artifact])


def test_tampered_inventory_member_hash_rejected(tmp_path):
    artifact = wheel(tmp_path)
    value = R.create_inventory([artifact])
    value["artifacts"][0]["members"][0]["sha256"] = "0" * 64
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="artifacts changed"):
        R.verify_inventory(path, [artifact])


def test_filesystem_symlinks_rejected_for_input_parent_and_output(tmp_path):
    artifact = wheel(tmp_path)
    link = tmp_path / "alias.whl"
    link.symlink_to(artifact)
    with pytest.raises(ValueError, match="Symlink"):
        R.create_inventory([link])
    directory = tmp_path / "alias"
    directory.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="Symlink"):
        R.create_inventory([directory / artifact.name])
    output = tmp_path / "output.json"
    output.symlink_to(artifact)
    assert R.main(["create", "--output", str(output), str(artifact)]) == 2
    assert zipfile.is_zipfile(artifact)


@pytest.mark.parametrize("name", ["../escape", "/absolute", "x/../../escape", "x\\evil", "x\nname", "x//evil"])
def test_archive_traversal_and_controls_rejected(tmp_path, name):
    artifact = wheel(tmp_path, members={name: "bad"})
    with pytest.raises(ValueError, match="archive member"):
        R.create_inventory([artifact])


def test_archive_symlink_rejected(tmp_path):
    member = zipfile.ZipInfo("raven/link")
    member.create_system = 3
    member.external_attr = (stat.S_IFLNK | 0o777) << 16
    with pytest.raises(ValueError, match="Links"):
        R.create_inventory([wheel(tmp_path, extra=[(member, "target")])])


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE])
def test_sdist_links_and_special_files_rejected(tmp_path, kind):
    member = tarfile.TarInfo("raven-6.0.0/link")
    member.type, member.linkname = kind, "target"
    with pytest.raises(ValueError, match="Links"):
        R.create_inventory([sdist(tmp_path, special=member)])


def test_duplicate_archive_members_rejected(tmp_path):
    with pytest.warns(UserWarning):
        artifact = wheel(tmp_path, extra=[("raven/__init__.py", "duplicate")])
    with pytest.raises(ValueError, match="Duplicate"):
        R.create_inventory([artifact])


@pytest.mark.parametrize("limit", ["MAX_ARTIFACT_BYTES", "MAX_EXPANDED_BYTES", "MAX_MEMBER_BYTES", "MAX_MEMBERS", "MAX_METADATA_BYTES"])
def test_each_size_budget_is_enforced(tmp_path, monkeypatch, limit):
    artifact = wheel(tmp_path)
    monkeypatch.setattr(R, limit, 1)
    with pytest.raises(ValueError):
        R.create_inventory([artifact])


def test_core_license_and_dependency_contract_checked(tmp_path):
    with pytest.raises(ValueError, match="license differs"):
        R.create_inventory([wheel(tmp_path, license_bytes=b"not MIT")])
    with pytest.raises(ValueError, match="dependency-free"):
        R.create_inventory([wheel(tmp_path, runtime="Requires-Dist: unsafe-runtime>=1\n")])


def test_multiple_versions_and_overwriting_inputs_refused(tmp_path):
    first = wheel(tmp_path)
    second = wheel(tmp_path, version="6.0.1")
    with pytest.raises(ValueError, match="same release version"):
        R.create_inventory([first, second])
    original = first.read_bytes()
    assert R.main(["create", "--output", str(first), str(first)]) == 2
    assert first.read_bytes() == original


def test_invalid_archive_is_a_clean_cli_failure(tmp_path):
    artifact = tmp_path / "broken.whl"
    artifact.write_bytes(b"invalid")
    assert R.main(["create", "--output", str(tmp_path / "out.json"), str(artifact)]) == 2
    assert not (tmp_path / "out.json").exists()


def release_pair(tmp_path):
    artifacts = [wheel(tmp_path), sdist(tmp_path, source=b"VERSION = '6.0.0'\n")]
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps(R.create_inventory(artifacts, source_commit="a" * 40)))
    return inventory, artifacts


def test_release_policy_binds_version_tag_revision_and_source_members(tmp_path):
    inventory, artifacts = release_pair(tmp_path)
    value = R.verify_release_policy(inventory, artifacts, expected_version="6.0.0",
                                    expected_source="a" * 40, tag="v6.0.0")
    assert value["version"] == "6.0.0"


@pytest.mark.parametrize("change,reason", [
    ({"expected_version": "6.0.1"}, "version"),
    ({"expected_source": "b" * 40}, "revision"),
    ({"expected_source": "abcdef0"}, "full expected"),
    ({"tag": "v6.0.1"}, "tag"),
])
def test_release_policy_refuses_wrong_expected_identity(tmp_path, change, reason):
    inventory, artifacts = release_pair(tmp_path)
    options = {"expected_version": "6.0.0", "expected_source": "a" * 40, "tag": "v6.0.0"}
    options.update(change)
    with pytest.raises(ValueError, match=reason):
        R.verify_release_policy(inventory, artifacts, **options)


def test_release_policy_rejects_different_source_even_with_valid_inventory(tmp_path):
    artifacts = [wheel(tmp_path), sdist(tmp_path)]
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps(R.create_inventory(artifacts, source_commit="a" * 40)))
    with pytest.raises(ValueError, match="different Raven source"):
        R.verify_release_policy(inventory, artifacts, expected_version="6.0.0", expected_source="a" * 40)


def test_provenance_verifier_uses_caller_identity_and_no_shell(tmp_path, monkeypatch):
    import sys
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    import verify_release as verifier
    inventory, artifacts = release_pair(tmp_path)
    called = []
    monkeypatch.setattr(verifier.subprocess, "run", lambda command, **kw: called.append((command, kw)))
    arguments = [str(inventory), *map(str, artifacts), "--repository", "rahel99x/Raven",
                 "--source", "a" * 40, "--version", "6.0.0", "--bundle", str(tmp_path / "bundle.jsonl")]
    assert verifier.main(arguments + ["--plan"]) == 0 and called == []
    assert verifier.main(arguments) == 0 and len(called) == 2
    for command, options in called:
        assert command[:3] == ["gh", "attestation", "verify"]
        assert command[command.index("--signer-workflow") + 1] == "rahel99x/Raven/.github/workflows/release-candidate.yml"
        assert command[command.index("--signer-digest") + 1] == "a" * 40
        assert command[command.index("--source-digest") + 1] == "a" * 40
        assert "--deny-self-hosted-runners" in command
        assert options["check"] and options["timeout"] == 90 and "shell" not in options


def test_provenance_verifier_does_not_ignore_signature_failure(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    import verify_release as verifier
    inventory, artifacts = release_pair(tmp_path)
    def rejected(command, **kwargs):
        raise verifier.subprocess.CalledProcessError(1, command)
    monkeypatch.setattr(verifier.subprocess, "run", rejected)
    assert verifier.main([str(inventory), *map(str, artifacts), "--repository", "rahel99x/Raven",
                          "--source", "a" * 40, "--version", "6.0.0"]) == 2


def test_zip_directory_budget_checked_before_constructing_zipfile(tmp_path, monkeypatch):
    artifact = wheel(tmp_path)
    data = bytearray(artifact.read_bytes())
    position = data.rfind(b"PK\x05\x06")
    R.struct.pack_into("<H", data, position + 8, 30000)
    R.struct.pack_into("<H", data, position + 10, 30000)
    artifact.write_bytes(data)
    def forbidden(*args, **kwargs):
        pytest.fail("ZipFile must not allocate an oversized directory")
    monkeypatch.setattr(R.zipfile, "ZipFile", forbidden)
    with pytest.raises(ValueError, match="member-count"):
        R.create_inventory([artifact])


def test_stream_decompression_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "MAX_EXPANDED_BYTES", 10)
    monkeypatch.setattr(R, "MAX_MEMBERS", 1)
    stream = R._ExpandedReader(io.BytesIO(b"x" * 2000))
    with pytest.raises(ValueError, match="decompression budget"):
        stream.read(2000)


def test_generated_inventory_budget_prevents_partial_publication(tmp_path, monkeypatch):
    artifact = wheel(tmp_path)
    output = tmp_path / "output.json"
    monkeypatch.setattr(R, "MAX_INVENTORY_BYTES", 1)
    assert R.main(["create", "--output", str(output), str(artifact)]) == 2
    assert not output.exists()


def test_duplicate_version_metadata_rejected(tmp_path):
    artifact = wheel(tmp_path, runtime="Version: 9.9.9\n")
    with pytest.raises(ValueError, match="Duplicate package metadata"):
        R.create_inventory([artifact])


def test_same_output_paths_and_nonregular_inputs_refused(tmp_path):
    artifact = wheel(tmp_path)
    output = tmp_path / "out.json"
    assert R.main(["create", "--output", str(output), "--spdx", str(output), str(artifact)]) == 2
    assert not output.exists()
    directory = tmp_path / "directory.whl"
    directory.mkdir()
    with pytest.raises(ValueError, match="regular file"):
        R.create_inventory([directory])


def test_spdx_uses_schema_field_names(tmp_path):
    document = R.spdx_document(R.create_inventory([wheel(tmp_path)]))
    assert set(document) == {"spdxVersion", "dataLicense", "SPDXID", "name", "documentNamespace", "creationInfo", "comment", "packages", "relationships"}
    assert document["comment"].endswith("no signatures or publisher authentication")
