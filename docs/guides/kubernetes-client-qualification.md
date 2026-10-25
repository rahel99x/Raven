# Qualify Kubernetes client request semantics

Raven's native adapter can be tested with an actual `kubectl` binary against an
isolated loopback HTTP fixture. This checks the client request path, native Job
JSON, reviewed submission, UID-conditioned deletion, replacement races, and
durable mutation-token handling. It does **not** qualify a Kubernetes scheduler,
authentication, RBAC, TLS, admission, storage, compute node, GPU, or real site.

Use an installed, locally trusted `kubectl` or obtain the binary for your operating
system and CPU from the [official installation guide](https://kubernetes.io/docs/tasks/tools/).
Verify its published checksum before executing a download. The repository does
not download or execute a client implicitly.

From the Raven checkout:

```bash
python scripts/qualify_kubernetes_client.py \
  --kubectl /absolute/path/to/kubectl \
  --output /new/path/kubernetes-client-wire.json
```

The output file must be new. It contains the client version and SHA256, adapter
source hashes, individual checks, and the actual HTTP requests observed by the
fixture. It contains synthetic local identities and no real kubeconfig, token,
certificate, or authorization header. The fixture temporarily supplies its own
private kubeconfig and client cache in the qualification process, then restores
that process's environment. Existing Kubernetes configuration is not modified.

The fixture deliberately gives the ambient current context, context namespace,
and kubeconfig server conflicting values. The adapter must use the explicitly
reviewed context, namespace, and server. It also replaces a Job after the last
detail response and before deletion; the actual HTTP DELETE must contain the
original UID precondition, and the fixture must reject the replacement. A
separate matching detail response then confirms the consumed durable token alone
prevents another mutation after an uncertain response.

The same qualification is available as an optional regression test:

```bash
RAVEN_KUBECTL=/absolute/path/to/kubectl \
  python -m pytest -q tests/test_kubernetes_client_wire.py
```

Without a client, the test explicitly skips. Native-format fixture tests remain
independent. A passing loopback result is useful evidence for command and wire
correctness; it must not be described as live Kubernetes deployment qualification.
