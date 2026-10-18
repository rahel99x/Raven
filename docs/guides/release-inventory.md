# Release inventory and provenance

Use the release inventory to record the final wheel, source archive, license,
and bundled member checksums. Build and qualify the artifacts first. The
inventory generator uses the Python standard library and reads artifacts
without extracting them, executing package code, or contacting a service.

## Prerequisites

Use a development installation with the `build` package. Confirm the release
version in `pyproject.toml` and select the exact qualified wheel and source
archive. The examples below use Raven 7.0.0. Distribution filenames retain the
compatible `raven` package name.

## Generate the metadata

From the repository root, run:

```bash
python3 -m build
python3 scripts/release_inventory.py create \
  --output dist/release-inventory.json \
  --spdx dist/raven.spdx.json \
  --checksums dist/SHA256SUMS \
  dist/raven-7.0.0-py3-none-any.whl \
  dist/raven-7.0.0.tar.gz
```

Use the exact filenames for the release. Do not include old versions with a
wildcard. The output directory must already exist. Optional
`--source-commit HEX_REVISION` records a caller-supplied source revision. The
script does not prove that the artifacts came from that revision. Set
`SOURCE_DATE_EPOCH` to a Unix timestamp for reproducible metadata timestamps.
This does not make the build itself reproducible.

**Expected result:** The generator writes these three files:

- `release-inventory.json`: SHA-256 and size for each artifact and each regular
  archive member, package version, Python requirement, declared optional
  dependencies, core MIT license path and checksum, and wheel build metadata.
- `raven.spdx.json`: a minimal SPDX 2.3 document with one package entry per
  distribution artifact. It declares MIT from package metadata and records the
  artifact SHA-256. It does not claim an independent license audit. Member hashes
  remain in the companion inventory; SPDX `filesAnalyzed` is `false`.
- `SHA256SUMS`: the artifact checksums in the standard two-space filename format.

The script requires the packaged core license to match the repository's
`LICENSE` file exactly. Use `--license PATH` to select that reference explicitly
when you verify a release from another working directory. Raven's core must have
no runtime `Requires-Dist` entries. Optional development requirements remain in
the inventory. Complex dependency markers that do not match a declared extra are
rejected conservatively for review.

## Verify before publication

Validate the files again before publication:

```bash
python3 scripts/release_inventory.py verify dist/release-inventory.json \
  dist/raven-7.0.0-py3-none-any.whl \
  dist/raven-7.0.0.tar.gz
```

Verification recomputes both the archive hashes and the member inventory. It
rejects changed artifacts, changed member hashes, missing or different artifacts,
and a different core license. You can also use `sha256sum -c SHA256SUMS` from the
artifact directory. If you rebuild an artifact, regenerate all three metadata
files and verify them again. Publish them next to the exact qualified artifacts.

These are **unsigned checksums and inventory records**. They detect changes only
when the expected records come from a trusted source. They do not authenticate a
publisher, sign a release, establish provenance, scan for vulnerabilities, or
prove that code is safe. The SPDX schema was validated against the upstream
[SPDX 2.3 JSON schema](https://github.com/spdx/spdx-spec/blob/v2.3/schemas/spdx-schema.json).

## Check version and source consistency

Release tags use `v` followed by the exact package version, for example `v7.0.0`.
Never move an existing release tag or replace published bytes. Use a new patch
version for corrected artifacts; prereleases use `aN`, `bN`, or `rcN` suffixes.
No tag or release is created by the commands below.

```bash
python3 scripts/release_inventory.py policy dist/release-inventory.json \
  --expected-version 7.0.0 --expected-source FULL_TESTED_COMMIT \
  --tag v7.0.0 dist/raven-7.0.0-py3-none-any.whl dist/raven-7.0.0.tar.gz
```

Supply the complete 40-character source commit from the qualified revision.
The policy checks the tag/version, recorded commit, one wheel plus one source
archive, and identical bundled Raven source files. CI also compares this version
with the installed package metadata. This detects consistency errors; it cannot
authenticate a caller-supplied source claim.

## Verify authenticated build provenance

The `release-candidate` workflow reuses the complete Python 3.10–3.14 test and
packaging workflow, downloads its exact qualified artifacts, and checks the
release policy. A manual run defaults to a dry run. An explicitly selected
attestation run, or a separately authorized version tag, creates GitHub/Sigstore
build provenance using GitHub OIDC and verifies it before uploading the bundle.
The workflow creates neither a Git tag nor a GitHub/PyPI release. Fork PRs have
no signing token. This procedure does not retroactively sign historical binaries.

Select the repository, workflow and source commit independently of an untrusted
download. With a current GitHub CLI supporting `gh attestation verify`, run:

```bash
python3 scripts/verify_release.py dist/release-inventory.json \
  --repository rahel99x/Raven \
  --workflow .github/workflows/release-candidate.yml \
  --source FULL_TESTED_COMMIT --version 7.0.0 --tag v7.0.0 \
  --bundle /path/to/downloaded-attestation.jsonl \
  dist/raven-7.0.0-py3-none-any.whl dist/raven-7.0.0.tar.gz
```

The verifier checks archive/member hashes first, then requires the expected
repository, signer workflow, signer revision and source revision. It rejects
self-hosted-runner attestations under this release policy. An unavailable or
failing verifier is a failure, not a fallback to unsigned checksums. `--plan`
validates local consistency and prints the exact verification commands while
explicitly reporting that signatures were not verified.

Checksums establish byte identity. A signed Git tag authenticates a source
reference when its signing key is trusted. An artifact signature authenticates
the signed artifact. A build attestation binds artifact digests to a verified
workflow identity and provenance statement. None proves scientific correctness,
security, reproducible builds, or hardware/site compatibility. OIDC signing and
its hosted verification require an authorized workflow run; local dry-run tests
cannot establish that external gate.

## Scope and limits

The inventory includes the files bundled in the wheel and source archive,
including Raven's adapter source code where present. It explicitly excludes
external components that are not bundled: Python and system libraries, Slurm,
MPI implementations and compilers, GPU drivers and CUDA/ROCm runtimes, optional
profilers/debuggers such as `py-spy`, user workloads, site plugins, and separately
installed integrations. Build and development dependencies are declarations, not
inventoried installations. An installed site's environment needs its own
inventory; this release record must not be used as a substitute.

Inspection accepts at most 16 distinct artifacts of one release version. Each
artifact is limited to 128 MiB, 20,000 archive entries, 256 MiB of regular-file
content, and 32 MiB per member. Metadata is limited to 1 MiB per file. Additional
budgets cap ZIP directory allocation, total tar decompression including headers,
and the generated inventory at 16 MiB. These limits bound inspection work; they
are not a wall-clock deadline on slow storage.

The script refuses input/output symlinks, archive links, special files, encrypted
ZIP entries, duplicate member paths, unsafe paths, mixed versions, and outputs
that overwrite inputs. ZIP64 and multipart ZIP archives are outside its bounded
format support. It does not extract any archive member. An interrupted output
write uses a temporary file and atomic replacement for that one output; the
three output files are not a single transaction. Regenerate the complete set if
creation fails partway through.
