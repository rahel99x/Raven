#!/usr/bin/env python3
"""Inspect release archives without extracting or executing their contents.

Only the Python standard library is used. Checksums establish byte identity,
not publisher authenticity or a claim that external tools are bundled.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from email.parser import BytesParser
import hashlib
import gzip
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import struct
import sys
import tarfile
import tempfile
import uuid
import zipfile
import zlib

SCHEMA = "raven.release-inventory/v1"
MAX_ARTIFACT_BYTES = 128 * 1024 * 1024
MAX_EXPANDED_BYTES = 256 * 1024 * 1024
MAX_MEMBER_BYTES = 32 * 1024 * 1024
MAX_METADATA_BYTES = 1024 * 1024
MAX_MEMBERS = 20_000
MAX_ARTIFACTS = 16
MAX_INVENTORY_BYTES = 16 * 1024 * 1024
CHUNK = 64 * 1024
DEFAULT_LICENSE = Path(__file__).resolve().parents[1] / "LICENSE"
EXTERNAL_EXCLUSIONS = [
    "Python interpreter and operating-system libraries",
    "Slurm services and command-line tools",
    "MPI implementations and compiler toolchains",
    "GPU drivers, CUDA/ROCm runtimes and vendor tools",
    "Optional profilers, debuggers and py-spy",
    "User workloads, site plugins and separately installed integrations",
    "Development and build dependencies (declared separately in package metadata)",
]


def _safe_path(path):
    path = Path(os.path.abspath(path))
    for component in reversed((path, *path.parents)):
        try:
            mode = component.lstat().st_mode
        except FileNotFoundError:
            if component != path:
                raise
            continue
        if stat.S_ISLNK(mode):
            raise ValueError(f"Symlink paths are not accepted: {component}")
    return path


@contextmanager
def _file(path, limit):
    path = _safe_path(path)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
        raise ValueError(f"Not a bounded regular file: {path.name}")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError("Input changed while opening")
        yield stream
        after = os.fstat(stream.fileno())
        if (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError("Input changed during inspection")


def _digest(stream, expected=None):
    digest, size = hashlib.sha256(), 0
    while True:
        block = stream.read(CHUNK)
        if not block:
            break
        size += len(block)
        if size > (MAX_ARTIFACT_BYTES if expected is None else expected):
            raise ValueError("Input exceeds declared size or inspection budget")
        digest.update(block)
    if expected is not None and size != expected:
        raise ValueError("Truncated archive member")
    return digest.hexdigest(), size


def _member_name(name):
    if not isinstance(name, str) or not name or len(name) > 1024 or any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise ValueError("Invalid archive member name")
    if name.startswith("/") or "\\" in name or any(p in (".", "..", "") for p in name.rstrip("/").split("/")):
        raise ValueError("Unsafe archive member path")
    return name.rstrip("/")


def _zip_budget(stream, size):
    # Check the central-directory allocation before ZipFile constructs objects
    # for every entry. ZIP64/multipart formats are unnecessary for our limits.
    stream.seek(max(0, size - 65_557))
    tail = stream.read(65_557)
    offset = tail.rfind(b"PK\x05\x06")
    if offset < 0 or len(tail) - offset < 22:
        raise ValueError("Missing ZIP end record")
    _, disk, central_disk, count_disk, count, central_size, central_offset, comment = struct.unpack_from("<4s4H2IH", tail, offset)
    if disk or central_disk or count_disk != count or count == 65_535 or count > MAX_MEMBERS:
        raise ValueError("ZIP member-count or multipart budget exceeded")
    if central_size > 8 * 1024 * 1024 or central_offset + central_size > size or len(tail) - offset != 22 + comment:
        raise ValueError("ZIP central-directory budget or boundary exceeded")
    stream.seek(0)


class _ExpandedReader:
    def __init__(self, stream):
        self.stream, self.remaining = stream, MAX_EXPANDED_BYTES + MAX_MEMBERS * 1024

    def read(self, size=-1):
        if size < 0:
            size = self.remaining + 1
        data = self.stream.read(min(size, self.remaining + 1))
        self.remaining -= len(data)
        if self.remaining < 0:
            raise ValueError("Archive decompression budget exceeded")
        return data


def _license(path):
    with _file(path, MAX_METADATA_BYTES) as stream:
        data = stream.read()
    if not data.startswith(b"MIT License") or b"Permission is hereby granted, free of charge" not in data:
        raise ValueError("The reference license must contain the project's MIT license")
    return data


def inspect_artifact(path, license_bytes):
    path = Path(path)
    if not re.fullmatch(r"[A-Za-z0-9_.+-]+", path.name):
        raise ValueError("Artifact filenames must be printable portable names")
    kind = "wheel" if path.name.endswith(".whl") else "sdist" if path.name.endswith(".tar.gz") else None
    if kind is None:
        raise ValueError("Only .whl and .tar.gz release artifacts are supported")
    members, special, seen, expanded = [], {}, set(), 0
    with _file(path, MAX_ARTIFACT_BYTES) as stream:
        digest, size = _digest(stream)
        stream.seek(0)
        with ExitStack() as resources:
            if kind == "wheel":
                _zip_budget(stream, size)
                archive = resources.enter_context(zipfile.ZipFile(stream))
            else:
                expanded_stream = resources.enter_context(gzip.GzipFile(fileobj=stream))
                archive = resources.enter_context(tarfile.open(fileobj=_ExpandedReader(expanded_stream), mode="r|"))
            entries = archive.infolist() if kind == "wheel" else archive
            for index, entry in enumerate(entries):
                if index >= MAX_MEMBERS:
                    raise ValueError("Archive member-count budget exceeded")
                name = _member_name(entry.filename if kind == "wheel" else entry.name)
                if name in seen:
                    raise ValueError("Duplicate archive member")
                seen.add(name)
                if kind == "wheel":
                    mode = (entry.external_attr >> 16) & 0xFFFF
                    if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)) or entry.flag_bits & 1:
                        raise ValueError("Links, special or encrypted archive members are not accepted")
                    directory, member_size = entry.is_dir(), entry.file_size
                else:
                    if not entry.isfile() and not entry.isdir():
                        raise ValueError("Links or special archive members are not accepted")
                    directory, member_size = entry.isdir(), entry.size
                if directory:
                    continue
                expanded += member_size
                if not 0 <= member_size <= MAX_MEMBER_BYTES or expanded > MAX_EXPANDED_BYTES:
                    raise ValueError("Archive expanded-size budget exceeded")
                basename = PurePosixPath(name).name
                capture = basename in ("METADATA", "PKG-INFO", "WHEEL", "LICENSE")
                if capture and member_size > MAX_METADATA_BYTES:
                    raise ValueError("Package metadata budget exceeded")
                opened = archive.open(entry) if kind == "wheel" else archive.extractfile(entry)
                with opened as member:
                    if capture:
                        content = member.read(MAX_METADATA_BYTES + 1)
                        if len(content) != member_size:
                            raise ValueError("Truncated metadata member")
                        member_digest = hashlib.sha256(content).hexdigest()
                        special[name] = content
                    else:
                        member_digest, _ = _digest(member, member_size)
                members.append({"path": name, "size": member_size, "sha256": member_digest})
    if kind == "wheel":
        metadata_names = [n for n in special if n.endswith(".dist-info/METADATA")]
        license_names = [n for n in special if n.endswith(".dist-info/licenses/LICENSE") or n.endswith(".dist-info/LICENSE")]
    else:
        roots = {n.split("/")[0] for n in seen}
        if len(roots) != 1:
            raise ValueError("Source archive must have one top-level directory")
        root = next(iter(roots))
        metadata_names = [n for n in special if n == root + "/PKG-INFO"]
        license_names = [n for n in special if n == root + "/LICENSE"]
    if len(metadata_names) != 1 or len(license_names) != 1:
        raise ValueError("Artifact needs one primary package metadata and one core license file")
    if special[license_names[0]] != license_bytes:
        raise ValueError("Bundled MIT license differs from the reference license")
    metadata = BytesParser().parsebytes(special[metadata_names[0]])
    for field in ("Name", "Version", "License-Expression", "License", "Requires-Python"):
        if len(metadata.get_all(field, [])) > 1:
            raise ValueError(f"Duplicate package metadata field: {field}")
    if metadata.get("Name", "").lower().replace("_", "-") != "raven":
        raise ValueError("Artifact is not the raven package")
    version = metadata.get("Version", "")
    if not re.fullmatch(r"[A-Za-z0-9_.+!-]{1,128}", version):
        raise ValueError("Missing or invalid package version")
    if metadata.get("License-Expression", metadata.get("License", "")) != "MIT":
        raise ValueError("Package must declare the MIT license")
    requires = metadata.get_all("Requires-Dist", [])
    extras = metadata.get_all("Provides-Extra", [])
    runtime, optional = [], []
    for requirement in requires:
        match = re.search(r';\s*extra\s*==\s*[\'"]([^\'"]+)[\'"]\s*$', requirement)
        if match and match.group(1) in extras:
            optional.append(requirement)
        else:
            runtime.append(requirement)
    if runtime:
        raise ValueError("Raven release violates its dependency-free runtime policy: " + ", ".join(runtime))
    build = {"format": kind, "metadata_version": metadata.get("Metadata-Version", "unknown")}
    if kind == "wheel":
        wheel_path = metadata_names[0].rsplit("/", 1)[0] + "/WHEEL"
        if wheel_path not in special:
            raise ValueError("Wheel build metadata is missing")
        wheel_metadata = BytesParser().parsebytes(special[wheel_path])
        build.update(generator=wheel_metadata.get("Generator", "unknown"), wheel_version=wheel_metadata.get("Wheel-Version", "unknown"), tags=wheel_metadata.get_all("Tag", []))
    return {"filename": path.name, "sha256": digest, "size": size, "version": version,
            "license": {"expression": "MIT", "path": license_names[0], "sha256": hashlib.sha256(license_bytes).hexdigest()},
            "requires_python": metadata.get("Requires-Python", ""), "runtime_requires_dist": runtime,
            "optional_requires_dist": optional, "build": build, "members": sorted(members, key=lambda item: item["path"])}


def create_inventory(artifacts, license_path=DEFAULT_LICENSE, *, source_commit=None):
    if not 1 <= len(artifacts) <= MAX_ARTIFACTS or len({Path(p).name for p in artifacts}) != len(artifacts):
        raise ValueError("Provide 1–16 artifacts with distinct filenames")
    license_bytes = _license(license_path)
    records = [inspect_artifact(p, license_bytes) for p in artifacts]
    if len({r["version"] for r in records}) != 1:
        raise ValueError("All artifacts must have the same release version")
    if source_commit and not re.fullmatch(r"[a-fA-F0-9]{7,64}", source_commit):
        raise ValueError("Source commit must be a hexadecimal revision identifier")
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    created = datetime.fromtimestamp(int(epoch), timezone.utc) if epoch is not None else datetime.now(timezone.utc)
    return {"schema": SCHEMA, "name": "raven", "version": records[0]["version"],
            "created": created.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "source_commit": source_commit, "source_commit_verification": "Caller-supplied; not independently verified",
            "scope": "Bundled release archive bytes and declared package metadata; no signatures or publisher authentication",
            "external_components_excluded": EXTERNAL_EXCLUSIONS,
            "artifacts": sorted(records, key=lambda item: item["filename"])}


def spdx_document(inventory):
    fingerprint = hashlib.sha256(json.dumps(inventory, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    packages, relationships = [], []
    for index, artifact in enumerate(inventory["artifacts"], 1):
        identifier = f"SPDXRef-Artifact-{index}"
        packages.append({"SPDXID": identifier, "name": "raven", "versionInfo": artifact["version"],
                         "packageFileName": artifact["filename"], "downloadLocation": "NOASSERTION", "filesAnalyzed": False,
                         "checksums": [{"algorithm": "SHA256", "checksumValue": artifact["sha256"]}],
                         "licenseConcluded": "NOASSERTION", "licenseDeclared": "MIT", "copyrightText": "NOASSERTION",
                         "comment": "Distribution artifact; archive members are in the companion Raven inventory. Runtime Requires-Dist is empty. External interpreters, schedulers, MPI/GPU runtimes, optional tools and dev dependencies are excluded."})
        relationships.append({"spdxElementId": "SPDXRef-DOCUMENT", "relationshipType": "DESCRIBES", "relatedSpdxElement": identifier})
    return {"spdxVersion": "SPDX-2.3", "dataLicense": "CC0-1.0", "SPDXID": "SPDXRef-DOCUMENT",
            "name": f"raven-{inventory['version']}-distributions",
            "documentNamespace": "https://spdx.org/spdxdocs/raven-" + str(uuid.uuid5(uuid.NAMESPACE_URL, fingerprint)),
            "creationInfo": {"created": inventory["created"], "creators": ["Tool: Raven-release-inventory-v1"]},
            "comment": inventory["scope"], "packages": packages, "relationships": relationships}


def verify_inventory(path, artifacts, license_path=DEFAULT_LICENSE):
    with _file(path, MAX_INVENTORY_BYTES) as stream:
        expected = json.load(stream)
    if not isinstance(expected, dict) or expected.get("schema") != SCHEMA:
        raise ValueError("Unsupported release inventory")
    actual = create_inventory(artifacts, license_path, source_commit=expected.get("source_commit"))
    for key in ("name", "version", "artifacts", "external_components_excluded"):
        if actual[key] != expected.get(key):
            raise ValueError(f"Inventory verification failed: {key} changed")
    return actual


def verify_release_policy(path, artifacts, *, expected_version, expected_source, tag=None,
                          license_path=DEFAULT_LICENSE):
    """Check release consistency; cryptographic provenance is verified separately."""
    actual = verify_inventory(path, artifacts, license_path)
    if not re.fullmatch(r"[0-9a-f]{40}", expected_source):
        raise ValueError("Release policy requires the full expected source commit")
    if actual["source_commit"] != expected_source:
        raise ValueError("Inventory source commit does not match the tested revision")
    if actual["version"] != expected_version:
        raise ValueError("Artifact version does not match the installed/tested version")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:a[0-9]+|b[0-9]+|rc[0-9]+)?", expected_version):
        raise ValueError("Unsupported release version; use a three-part version with optional prerelease")
    if tag is not None and tag != "v" + expected_version:
        raise ValueError("Release tag must be v followed by the exact artifact version")
    kinds = {item["build"]["format"] for item in actual["artifacts"]}
    if kinds != {"wheel", "sdist"} or len(actual["artifacts"]) != 2:
        raise ValueError("Release policy requires exactly one wheel and one source archive")
    source_maps = {}
    for artifact in actual["artifacts"]:
        kind = artifact["build"]["format"]
        prefix = "" if kind == "wheel" else "raven-" + expected_version + "/"
        source_maps[kind] = {item["path"][len(prefix):]: item["sha256"]
                             for item in artifact["members"]
                             if item["path"].startswith(prefix + "raven/")}
    if not source_maps["wheel"] or source_maps["wheel"] != source_maps["sdist"]:
        raise ValueError("Wheel and source archive contain different Raven source bytes")
    return actual


def _write(path, content):
    path = _safe_path(path)
    if path.exists() and not path.is_file():
        raise ValueError("Output must be a regular file")
    temporary = None
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=".raven-inventory-", dir=path.parent)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="Inspect artifacts and write unsigned metadata")
    create.add_argument("--output", required=True)
    create.add_argument("--spdx")
    create.add_argument("--checksums")
    create.add_argument("--source-commit")
    verify = commands.add_parser("verify", help="Recompute hashes and archive metadata against an inventory")
    verify.add_argument("inventory")
    policy = commands.add_parser("policy", help="Check version, tag, source identity and wheel/source agreement")
    policy.add_argument("inventory")
    policy.add_argument("--expected-version", required=True)
    policy.add_argument("--expected-source", required=True)
    policy.add_argument("--tag")
    for command in (create, verify, policy):
        command.add_argument("--license", default=str(DEFAULT_LICENSE))
        command.add_argument("artifacts", nargs="+")
    args = parser.parse_args(argv)
    try:
        if args.command == "policy":
            verify_release_policy(args.inventory, args.artifacts, expected_version=args.expected_version,
                                  expected_source=args.expected_source, tag=args.tag, license_path=args.license)
            print("Release policy verified; authenticate provenance separately before trusting its source claim.")
            return 0
        if args.command == "verify":
            verify_inventory(args.inventory, args.artifacts, args.license)
            print("Release inventory verified; this is not signature verification.")
            return 0
        outputs = [p for p in (args.output, args.spdx, args.checksums) if p]
        resolved = [str(_safe_path(p)) for p in outputs]
        inputs = {str(_safe_path(p)) for p in [*args.artifacts, args.license]}
        if len(set(resolved)) != len(resolved) or set(resolved) & inputs:
            raise ValueError("Outputs must be distinct and must not overwrite inputs")
        inventory = create_inventory(args.artifacts, args.license, source_commit=args.source_commit)
        content = json.dumps(inventory, indent=2, sort_keys=True) + "\n"
        if len(content.encode("utf-8")) > MAX_INVENTORY_BYTES:
            raise ValueError("Release inventory output budget exceeded")
        _write(args.output, content)
        if args.spdx:
            _write(args.spdx, json.dumps(spdx_document(inventory), indent=2, sort_keys=True) + "\n")
        if args.checksums:
            _write(args.checksums, "".join(f"{a['sha256']}  {a['filename']}\n" for a in inventory["artifacts"]))
        print(f"Inspected {len(inventory['artifacts'])} unsigned release artifacts for {inventory['version']}.")
        return 0
    except (OSError, ValueError, OverflowError, EOFError, NotImplementedError,
            tarfile.TarError, zipfile.BadZipFile, zlib.error) as exc:
        print(f"Release inventory: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
