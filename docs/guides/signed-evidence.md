# Sign and verify a portable evidence bundle

A signature proves that a trusted key signed the exact exported bytes. It does
not prove the scientific result is correct, that a scheduler independently
confirmed the declared attempt, or that any recorded time is trustworthy.
Unsigned export remains available and has no OpenSSH dependency.

## Export with an explicit signing key

In Raven, open `:ops research-portable`. Select the source, format, exact cluster,
job and attempt, and a new destination directory as usual. To sign the export,
fill in **Optional SSH signing key** and **Signing principal**. Leave both blank
to export without a signature.

Use either an owned private key with mode `0600`, or its `.pub` file with the
corresponding key already loaded in your SSH agent. The public-file/agent route
supports passphrase-protected keys without collecting a passphrase in Raven.
Raven does not automatically choose a key, modify your agent, or upload evidence.

**Inspect / prepare** displays the principal and SHA256 public-key fingerprint
and checks OpenSSH's signing support. It does not sign anything. Verify that
identity, then select **Review action → Confirm apply**. A changed key or source
invalidates the review. Errors do not silently produce an unsigned substitute.
Correct the input and inspect again. Cancellation before publication leaves no
completed bundle; each OpenSSH command has a ten-second deadline.

The normal RO-Crate or portable export is accompanied by:

- `signed-manifest.json`: canonical manifest binding the schema, exact attempt,
  publication receipt, signer description and each exported artifact's digest.
- `signed-manifest.sig`: detached OpenSSH signature, using the namespace
  `raven.evidence.v1`.

Private key contents and the local private-key path are never copied into the
bundle. Its public key and fingerprint are descriptive; they do not authorize
that signer for a recipient.

## Verify with an independently trusted key

Obtain the public key or verify its SHA256 fingerprint through a channel you
already trust. Create an allowed-signers file **outside the bundle**, for example:

```text
researcher@example.edu namespaces="raven.evidence.v1" ssh-ed25519 AAAA...trusted-public-key...
```

The principal and key above are placeholders; use the real principal and complete
public key. Do not turn a key supplied by the bundle into a trust decision without
independent verification. Revocation is handled by updating your independent
allowlist; certificate-authority/validity restrictions supported by OpenSSH can
also be used there.

With Raven installed:

```bash
python -m raven.evidence_verify '/path/to/signed bundle' \
  --allowed-signers '/path/to/independent allowed signers' \
  --principal researcher@example.edu
```

Without installing Raven, obtain `raven/evidence_verify.py` from a trusted Raven
release, then run that file on Linux with Python 3.10+ and OpenSSH:

```bash
python -I -S /trusted/tools/evidence_verify.py '/path/to/signed bundle' \
  --allowed-signers '/path/to/independent allowed signers' \
  --principal researcher@example.edu
```

Do not execute a verifier found inside an unverified bundle. The standalone file
uses only Python's standard library and `ssh-keygen`; it imports no Raven modules.
It first verifies the detached signature against your allowlist and chosen
principal, then checks all declared artifact bytes, receipt and attempt identity.
It rejects incomplete publications and symlinked artifacts and reports extra
unsigned files separately. Success applies to the listed verified files only.
The independent allowlist and its directory ancestry must be owned by you or the
administrator and protected against group/world writes. A system-owned sticky
temporary directory is permitted as an ancestor; symlinked ancestors are not.
Linux descriptor paths pin the key and signature while OpenSSH reads them.

Verification bounds the manifest to 256 KiB, each artifact to 16 MiB, the declared
payload to 64 MiB, and JSON nesting to 64 levels. It rejects unsupported size or
complexity rather than silently truncating evidence. OpenSSH is a trusted local
executable; its captured output is checked after the process exits, not streamed
through a hard memory cap. Each invocation has a ten-second deadline.

To check only the signature using OpenSSH directly:

```bash
ssh-keygen -Y verify \
  -f '/path/to/independent allowed signers' \
  -I researcher@example.edu -n raven.evidence.v1 \
  -s '/path/to/signed bundle/signed-manifest.sig' \
  < '/path/to/signed bundle/signed-manifest.json'
```

This last command authenticates the manifest bytes. It does not by itself hash
its listed artifacts; use the standalone verifier for the complete bundle check.
