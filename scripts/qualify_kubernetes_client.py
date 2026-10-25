#!/usr/bin/env python3
"""Exercise an actual kubectl against an isolated loopback HTTP fixture.

This qualifies command/wire behavior only. No Kubernetes API server, scheduler,
admission policy, authentication, RBAC, compute node, or production site is used.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from raven import evidence  # noqa: E402
from raven.schedulers.kubernetes import KubernetesScheduler  # noqa: E402
from raven.slurm import Backend  # noqa: E402


def qualify(kubectl):
    binary = Path(kubectl).absolute()
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise ValueError("Choose an explicit executable kubectl binary")
    version = json.loads(
        subprocess.check_output([str(binary), "version", "--client", "--output=json"], timeout=10)
    )
    wire = []
    login = "wire-researcher"
    owner_label = evidence.digest({"login": login})[:32]

    def job(name, uid, *, finished=False):
        return {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": name,
                "uid": uid,
                "namespace": "lab",
                "resourceVersion": "1",
                "creationTimestamp": "2026-10-25T00:00:00Z",
                "labels": {"raven.dev/login": owner_label},
                "annotations": {"raven.dev/owner": login},
            },
            "spec": {
                "parallelism": 1,
                "template": {
                    "spec": {
                        "restartPolicy": "Never",
                        "containers": [
                            {
                                "name": "main",
                                "image": "example.invalid/fixture:v1",
                                "resources": {
                                    "requests": {"cpu": "500m", "memory": "256Mi"},
                                    "limits": {"cpu": "1", "memory": "512Mi"},
                                },
                            }
                        ],
                    }
                },
            },
            "status": {
                "active": 0 if finished else 1,
                "conditions": [{"type": "Complete", "status": "True"}] if finished else [],
            },
        }

    state = {
        "jobs": {
            "experiment": job("experiment", "uid-original"),
            "finished": job("finished", "uid-finished", finished=True),
        },
        "replace_after_detail": False,
        "errors": [],
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def response(self, value, status=200):
            raw = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def record(self, body=None):
            parsed = urlsplit(self.path)
            wire.append(
                {
                    "method": self.command,
                    "path": parsed.path,
                    "query": parse_qs(parsed.query),
                    "body": body,
                    "content_type": self.headers.get("Content-Type"),
                }
            )
            return parsed.path

        def do_GET(self):
            path = self.record()
            if path == "/api":
                self.response(
                    {"kind": "APIVersions", "versions": ["v1"], "serverAddressByClientCIDRs": []}
                )
            elif path == "/apis":
                self.response(
                    {
                        "kind": "APIGroupList",
                        "groups": [
                            {
                                "name": "batch",
                                "versions": [{"groupVersion": "batch/v1", "version": "v1"}],
                                "preferredVersion": {"groupVersion": "batch/v1", "version": "v1"},
                            }
                        ],
                    }
                )
            elif path in {"/api/v1", "/apis/batch/v1"}:
                self.response(
                    {
                        "kind": "APIResourceList",
                        "apiVersion": "v1",
                        "groupVersion": "v1" if path == "/api/v1" else "batch/v1",
                        "resources": []
                        if path == "/api/v1"
                        else [
                            {
                                "name": "jobs",
                                "singularName": "job",
                                "namespaced": True,
                                "kind": "Job",
                                "verbs": ["get", "list", "create", "delete"],
                            }
                        ],
                    }
                )
            elif path == "/apis/batch/v1/namespaces/lab/jobs":
                selector = parse_qs(urlsplit(self.path).query).get("labelSelector", [])
                if selector != ["raven.dev/login=" + owner_label]:
                    state["errors"].append(
                        "Queue request did not carry the scoped Raven owner selector"
                    )
                self.response(
                    {
                        "apiVersion": "batch/v1",
                        "kind": "JobList",
                        "metadata": {"resourceVersion": "1"},
                        "items": list(state["jobs"].values()),
                    }
                )
            elif path.startswith("/apis/batch/v1/namespaces/lab/jobs/"):
                name = path.rsplit("/", 1)[-1]
                current = state["jobs"].get(name)
                if current is None:
                    self.response(
                        {
                            "kind": "Status",
                            "apiVersion": "v1",
                            "status": "Failure",
                            "reason": "NotFound",
                            "code": 404,
                        },
                        404,
                    )
                else:
                    self.response(copy.deepcopy(current))
                    if state["replace_after_detail"]:
                        current["metadata"]["uid"] = "uid-replacement"
            else:
                state["errors"].append("Unexpected GET " + path)
                self.response(
                    {
                        "kind": "Status",
                        "apiVersion": "v1",
                        "status": "Failure",
                        "reason": "NotFound",
                        "code": 404,
                    },
                    404,
                )

        def body(self):
            if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
                chunks, size = [], 0
                while True:
                    header = self.rfile.readline(128)
                    if len(header) >= 128 or not header.endswith(b"\r\n"):
                        raise ValueError("Invalid bounded HTTP chunk header")
                    length = int(header.split(b";", 1)[0], 16)
                    if not length:
                        if self.rfile.readline(1024) != b"\r\n":
                            raise ValueError("Unexpected HTTP chunk trailers")
                        break
                    size += length
                    if size > 96 << 10:
                        raise ValueError("Unexpected chunked body size")
                    chunk = self.rfile.read(length)
                    if len(chunk) != length or self.rfile.read(2) != b"\r\n":
                        raise ValueError("Incomplete HTTP chunk")
                    chunks.append(chunk)
                return json.loads(b"".join(chunks))
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 <= size <= 96 << 10:
                raise ValueError("Unexpected request body size")
            return json.loads(self.rfile.read(size))

        def do_POST(self):
            body = self.body()
            path = self.record(body)
            if path != "/apis/batch/v1/namespaces/lab/jobs":
                state["errors"].append("Unexpected POST " + path)
            created = copy.deepcopy(body)
            created["metadata"].update(
                uid="uid-created", resourceVersion="1", creationTimestamp="2026-10-25T00:00:01Z"
            )
            created["status"] = {"active": 0, "conditions": []}
            state["jobs"][created["metadata"]["name"]] = created
            self.response(created, 201)

        def do_DELETE(self):
            body = self.body()
            path = self.record(body)
            name = path.rsplit("/", 1)[-1]
            current = state["jobs"].get(name)
            uid = body.get("preconditions", {}).get("uid")
            if current is None or uid != current["metadata"]["uid"]:
                self.response(
                    {
                        "apiVersion": "v1",
                        "kind": "Status",
                        "status": "Failure",
                        "reason": "Conflict",
                        "message": "UID precondition rejected replacement Job",
                        "code": 409,
                    },
                    409,
                )
                return
            del state["jobs"][name]
            self.response(
                {
                    "apiVersion": "v1",
                    "kind": "Status",
                    "status": "Success",
                    "details": {"name": name, "uid": uid},
                    "code": 200,
                }
            )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    previous = {name: os.environ.get(name) for name in ("PATH", "KUBECONFIG")}
    try:
        with tempfile.TemporaryDirectory(prefix="raven-kubernetes-wire-") as temporary:
            root = Path(temporary)
            endpoint = "http://127.0.0.1:" + str(server.server_port)
            config = {
                "apiVersion": "v1",
                "kind": "Config",
                "clusters": [{"name": "fixture", "cluster": {"server": "http://127.0.0.1:9"}}],
                "contexts": [
                    {
                        "name": "pinned",
                        "context": {
                            "cluster": "fixture",
                            "user": "fixture",
                            "namespace": "wrong-namespace",
                        },
                    }
                ],
                "users": [{"name": "fixture", "user": {}}],
                "current-context": "nonexistent-ambient-context",
            }
            config_path = root / "config.json"
            config_path.write_text(json.dumps(config))
            config_path.chmod(0o600)
            # Only cache isolation is added. The adapter's native flags, stdin
            # payload, explicit endpoint and UID delete condition remain intact.
            wrapper = root / "kubectl"
            wrapper.write_text(
                "#!/bin/sh\nexec "
                + shlex.quote(str(binary))
                + " --cache-dir="
                + shlex.quote(str(root / "cache"))
                + ' "$@"\n'
            )
            wrapper.chmod(0o700)
            os.environ["KUBECONFIG"] = str(config_path)
            os.environ["PATH"] = str(root) + os.pathsep + (previous["PATH"] or "")
            client = KubernetesScheduler(
                Backend(),
                {
                    "cluster": "wire-fixture",
                    "user": login,
                    "context": "pinned",
                    "namespace": "lab",
                    "uri": endpoint,
                },
                journal_dir=root / "actions",
            )
            queue, history = client.jobs(), client.jobs(history=True)
            assert [row["id"] for row in queue] == ["experiment"]
            assert [row["id"] for row in history] == ["finished"]
            assert queue[0]["resources"]["containers"][0]["resources"]["requests"]["cpu"] == "500m"
            selected = client.detail(queue[0]["identity"])
            assert selected["identity"] == queue[0]["identity"]
            manifest = copy.deepcopy(job("created", "unused"))
            for key in ("uid", "resourceVersion", "creationTimestamp"):
                manifest["metadata"].pop(key)
            manifest.pop("status")
            submit_plan = client.review("submit", submission={"manifest": manifest})
            submitted = client.apply(submit_plan, submit_plan["digest"])
            assert (
                submitted["status"] == "accepted" and submitted["identity"]["uid"] == "uid-created"
            )
            cancel_plan = client.review("cancel", identity=selected["identity"])
            cancelled = client.apply(cancel_plan, cancel_plan["digest"])
            assert cancelled["status"] == "accepted" and "experiment" not in state["jobs"]
            # A replacement after fresh detail must survive the actual HTTP
            # DELETE. This proves stdin DeleteOptions reaches the wire.
            state["jobs"]["experiment"] = job("experiment", "uid-original")
            race_plan = client.review("cancel", identity=selected["identity"])
            state["replace_after_detail"] = True
            conflicted = client.apply(race_plan, race_plan["digest"])
            assert conflicted["status"] == "unknown"
            assert state["jobs"]["experiment"]["metadata"]["uid"] == "uid-replacement"
            # Isolate the durable claim from identity rejection: return matching
            # evidence on the next detail read and require token consumption to
            # block another HTTP mutation independently of that fresh read.
            state["replace_after_detail"] = False
            state["jobs"]["experiment"]["metadata"]["uid"] = "uid-original"
            try:
                client.apply(race_plan, race_plan["digest"])
            except ValueError as exc:
                assert "already attempted" in str(exc)
            else:
                raise AssertionError("Uncertain mutation token was replayed")
            deletes = [request for request in wire if request["method"] == "DELETE"]
            assert len(deletes) == 2 and all(
                request["body"]["preconditions"]["uid"] == "uid-original" for request in deletes
            )
            assert not state["errors"], state["errors"]
            checks = {
                "actual_kubectl": True,
                "explicit_context_overrode_ambient": True,
                "explicit_server_overrode_kubeconfig": True,
                "namespace_and_owner_scoped_queue": True,
                "native_requests_limits_preserved": True,
                "detail_uid_preserved": True,
                "reviewed_create_body_and_response": True,
                "uid_delete_body_on_wire": True,
                "replacement_uid_rejected_by_server": True,
                "replacement_preserved": True,
                "uncertain_mutation_not_replayed": True,
            }
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    source_root = Path(__file__).resolve().parents[1]
    sources = [
        "raven/schedulers/core.py",
        "raven/schedulers/kubernetes.py",
        "scripts/qualify_kubernetes_client.py",
    ]
    return {
        "schema": "raven.kubernetes-client-wire-qualification/v1",
        "client_version": version["clientVersion"]["gitVersion"],
        "client_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "checks": checks,
        "http_requests": wire,
        "source_hashes": {
            name: hashlib.sha256((source_root / name).read_bytes()).hexdigest() for name in sources
        },
        "qualification": "Actual kubectl against loopback HTTP fixture only; no live Kubernetes scheduler, RBAC, credentials, admission, compute, GPU or remote-site qualification.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--kubectl", required=True, help="Explicit locally trusted kubectl executable"
    )
    parser.add_argument("--output", help="Write a new private evidence JSON file")
    args = parser.parse_args()
    result = qualify(args.kubectl)
    raw = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        descriptor = os.open(
            args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(descriptor, "w") as stream:
            stream.write(raw)
    print(raw)


if __name__ == "__main__":
    main()
