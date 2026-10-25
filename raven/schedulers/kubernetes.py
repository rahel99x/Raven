"""Kubernetes batch/v1 Jobs with native requests, limits and UID preconditions.

The queue is an explicitly Raven-labelled subset in one context/namespace.
Labels are discovery hints, not authorization; the API server enforces RBAC.
"""
from __future__ import annotations

import copy
import re
from urllib.parse import quote, urlencode, urlsplit

from .. import evidence as E
from .core import MAX_INPUT_BYTES, Scheduler, stdin_command

_DNS = re.compile(r"[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?\Z")
_LABEL = "raven.dev/login"
_OWNER = "raven.dev/owner"


class KubernetesScheduler(Scheduler):
    scheduler = "kubernetes"
    operations = ("queue", "history", "detail", "submit", "cancel")

    def _namespace(self):
        value = self.scope.get("namespace", "")
        if not value or len(value) > 63 or not _DNS.fullmatch(value) or "." in value:
            raise ValueError("Kubernetes requires an explicit valid namespace")
        return value

    def _argv(self, *arguments):
        context = self.scope.get("context", "")
        if not context or context.startswith("-"):
            raise ValueError("Kubernetes requires an explicit context; ambient context changes are not followed")
        argv = ["kubectl", "--context=" + context, "--namespace=" + self._namespace(),
                "--request-timeout=" + str(self.timeout) + "s"]
        server = self.scope.get("uri")
        if server:
            parsed = urlsplit(server)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("Kubernetes server URI must be an explicit HTTP(S) API endpoint without credentials")
            argv.append("--server=" + server)
        return [*argv, *arguments]

    def _mutable(self):
        if not self.scope.get("uri"):
            raise ValueError("Pin the Kubernetes API server in scope.uri before a reviewed mutation")

    def _owner_label(self, owner):
        return E.digest({"login": owner})[:32]

    def _job_uri(self, name=None):
        uri = "/apis/batch/v1/namespaces/" + quote(self._namespace(), safe="") + "/jobs"
        return uri if name is None else uri + "/" + quote(name, safe="")

    def _record(self, raw):
        if not isinstance(raw, dict) or raw.get("apiVersion") != "batch/v1" or raw.get("kind") != "Job":
            raise ValueError("Expected a native batch/v1 Kubernetes Job")
        metadata = raw.get("metadata", {})
        if not isinstance(metadata, dict):
            raise ValueError("Kubernetes Job metadata must be an object")
        uid, name = metadata.get("uid"), metadata.get("name")
        E.text(uid, "Kubernetes UID", 128)
        if not isinstance(name, str) or not _DNS.fullmatch(name):
            raise ValueError("Kubernetes Job has no exact native name")
        namespace = metadata.get("namespace")
        if namespace != self._namespace():
            raise ValueError("Kubernetes response belongs to a different namespace")
        spec, status = raw.get("spec", {}), raw.get("status", {})
        if not isinstance(spec, dict) or not isinstance(status, dict):
            raise ValueError("Kubernetes Job spec and status must be objects")
        conditions = status.get("conditions", [])
        if not isinstance(conditions, list) or len(conditions) > 256:
            raise ValueError("Malformed Kubernetes conditions")
        if any(not isinstance(item, dict) or not isinstance(item.get("type"), str)
               or not isinstance(item.get("status"), str)
               or item["status"] not in {"True", "False", "Unknown"} for item in conditions):
            raise ValueError("Malformed Kubernetes condition fields")
        active = status.get("active", 0)
        E.integer(active, "Kubernetes active pods", 10**9)
        terminal = {condition.get("type") for condition in conditions if isinstance(condition, dict) and condition.get("status") == "True"}
        state = "COMPLETED" if "Complete" in terminal else "FAILED" if "Failed" in terminal else "RUNNING" if active else "PENDING"
        if metadata.get("deletionTimestamp"):
            state = "COMPLETING"
        annotations = metadata.get("annotations", {})
        if not isinstance(annotations, dict):
            raise ValueError("Kubernetes annotations must be an object")
        owner = annotations.get(_OWNER, "")
        labels = metadata.get("labels", {})
        if not isinstance(owner, str) or not isinstance(labels, dict) or labels.get(_LABEL) != self._owner_label(owner):
            owner = ""
        template = spec.get("template", {})
        if not isinstance(template, dict):
            raise ValueError("Kubernetes pod template must be an object")
        pod = template.get("spec", {})
        if not isinstance(pod, dict):
            raise ValueError("Kubernetes pod spec must be an object")
        containers = pod.get("containers", [])
        if (not isinstance(containers, list) or not 1 <= len(containers) <= 256
                or any(not isinstance(item, dict) or not isinstance(item.get("name"), str)
                       or not isinstance(item.get("image"), str) or not item["name"] or not item["image"]
                       or not isinstance(item.get("resources", {}), dict) for item in containers)):
            raise ValueError("Malformed Kubernetes container inventory")
        resources = {"unit": "Kubernetes native quantities; requests and limits are distinct",
                     "containers": [{"name": item.get("name"), "resources": copy.deepcopy(item.get("resources", {}))} for item in containers],
                     "init_containers": copy.deepcopy(pod.get("initContainers", [])),
                     "overhead": copy.deepcopy(pod.get("overhead", {})),
                     "parallelism": spec.get("parallelism", 1), "completions": spec.get("completions", 1),
                     "active_deadline_seconds": spec.get("activeDeadlineSeconds")}
        return self.record(self.identity(name, uid, namespace=namespace, uid=uid), name=name, user=owner,
                           state=state, native_state=state, resources=resources,
                           native={"job": raw, "owner_source": "Raven discovery labels; API server RBAC authorizes actions"},
                           submit=metadata.get("creationTimestamp", ""), start=status.get("startTime", ""),
                           end=status.get("completionTime", ""), partition=namespace)

    def jobs(self, *, history=False, user=""):
        owner = user or self.scope["user"]
        query = urlencode({"labelSelector": _LABEL + "=" + self._owner_label(owner), "limit": self.max_records})
        # Raw REST reads avoid discovery calls and kubectl's implicit aggregate
        # pagination. One native read corresponds to one selected API request.
        document = self.json(self._argv("get", "--raw=" + self._job_uri() + "?" + query))
        if not isinstance(document, dict) or not isinstance(document.get("items"), list):
            raise ValueError("Kubernetes response has no items array")
        metadata = document.get("metadata", {})
        if not isinstance(metadata, dict):
            raise ValueError("Kubernetes list metadata must be an object")
        if metadata.get("continue"):
            raise ValueError("Kubernetes record budget reached: the API returned a continuation. Narrow the selected namespace/owner or explicitly increase the record budget; no complete queue was published")
        result = []
        for raw in self._bounded_records(document["items"]):
            record = self._record(raw)
            if record["user"] != owner:
                raise ValueError("Kubernetes returned a mismatched Raven owner label")
            completed = record["state"] in {"COMPLETED", "FAILED"}
            if completed == history:
                result.append(record)
        return self._bounded_records(result)

    def detail(self, identity):
        target = self._identity(identity)
        if target.get("namespace") != self._namespace() or not _DNS.fullmatch(target["native_id"]):
            raise ValueError("Kubernetes detail requires an exact name and namespace")
        record = self._record(self.json(self._argv("get", "--raw=" + self._job_uri(target["native_id"]))))
        if record["identity"] != target:
            raise ValueError("Kubernetes Job was replaced; its UID differs from the selected attempt")
        return record

    def observe(self, native_id):
        """Read one current native name; return its authoritative UID attempt."""
        if not isinstance(native_id, str) or not _DNS.fullmatch(native_id):
            raise ValueError("Kubernetes observation requires an exact Job name")
        record = self._record(self.json(self._argv("get", "--raw=" + self._job_uri(native_id)), fresh=True))
        if record["id"] != native_id or record["user"] != self.scope["user"]:
            raise ValueError("Kubernetes observation does not identify this login's exact Job")
        return record

    def _manifest(self, specification):
        E.keys(specification, {"manifest"}, label="Kubernetes submission")
        manifest = copy.deepcopy(specification["manifest"])
        E.keys(manifest, {"apiVersion", "kind", "metadata", "spec"}, label="Kubernetes Job")
        if manifest["apiVersion"] != "batch/v1" or manifest["kind"] != "Job":
            raise ValueError("Only batch/v1 Jobs are accepted by this adapter")
        metadata = E.keys(manifest["metadata"], {"name"}, {"namespace", "labels", "annotations"}, "Kubernetes metadata")
        if not isinstance(metadata["name"], str) or len(metadata["name"]) > 63 or not _DNS.fullmatch(metadata["name"]):
            raise ValueError("Kubernetes submission needs an exact bounded Job name")
        if metadata.get("namespace", self._namespace()) != self._namespace():
            raise ValueError("Job manifest namespace differs from the reviewed connection")
        metadata["namespace"] = self._namespace()
        for field in ("labels", "annotations"):
            values = metadata.setdefault(field, {})
            if not isinstance(values, dict) or len(values) > 128 or any(not isinstance(key, str) or not isinstance(value, str) for key, value in values.items()):
                raise ValueError("Kubernetes labels and annotations must be bounded string mappings")
        metadata["labels"][_LABEL] = self._owner_label(self.scope["user"])
        metadata["annotations"][_OWNER] = self.scope["user"]
        spec = manifest["spec"]
        if not isinstance(spec, dict) or not isinstance(spec.get("template"), dict):
            raise ValueError("Job spec requires a native pod template")
        pod = spec["template"].get("spec", {})
        if (not isinstance(pod, dict) or not isinstance(pod.get("restartPolicy"), str)
                or pod["restartPolicy"] not in {"Never", "OnFailure"}):
            raise ValueError("Job template needs an explicit Never or OnFailure restart policy")
        containers = pod.get("containers")
        if not isinstance(containers, list) or not 1 <= len(containers) <= 256 or any(not isinstance(item, dict) or not item.get("name") or not item.get("image") for item in containers):
            raise ValueError("Job template requires named container images")
        raw = E.canonical(manifest)
        if len(raw) > MAX_INPUT_BYTES:
            raise ValueError("Kubernetes manifest exceeds the 96 KiB native input bound")
        return raw.decode()

    def _submission(self, specification):
        self._mutable()
        self._manifest(specification)
        return self._argv("create", "--raw=" + self._job_uri(), "--filename=-")

    def _submit_argv(self, specification, script_text):
        return stdin_command(self._submission(specification), self._manifest(specification))

    def _action(self, action, record):
        self._mutable()
        if action != "cancel":
            raise ValueError("Kubernetes does not expose Slurm hold/release semantics")
        if record["state"] not in {"PENDING", "RUNNING"}:
            raise ValueError("Only an active Kubernetes Job can be cancelled; removing retained history is a separate native operation")
        target = record["identity"]
        uid = target.get("uid")
        if not uid or uid != target["attempt"]:
            raise ValueError("Kubernetes cancellation requires a verified UID")
        uri = self._job_uri(target["native_id"])
        options = {"apiVersion": "v1", "kind": "DeleteOptions", "propagationPolicy": "Background",
                   "preconditions": {"uid": uid}}
        return stdin_command(self._argv("delete", "--raw=" + uri, "--filename=-"), E.canonical(options).decode())

    def _parse_submission(self, raw):
        record = self._record(E.loads(raw))
        return {"native_id": record["id"], "identity": record["identity"]}

    def _submission_result(self, raw, specification):
        record = self._record(E.loads(raw))
        expected = specification["manifest"]["metadata"]["name"]
        if record["id"] != expected or record["user"] != self.scope["user"]:
            raise ValueError("Kubernetes submission reply does not match the reviewed Job name and owner")
        return {"native_id": record["id"], "identity": record["identity"],
                "effective_resources": record["resources"]}

    def capabilities(self):
        result = super().capabilities()
        result["notes"] = ["Queue/history include Raven-labelled batch/v1 Jobs in exactly one context and namespace; deleted Jobs require retained Raven receipts or external Kubernetes accounting.",
                           "Discovery labels are not security credentials; API server RBAC is authoritative.",
                           "CPU requests/limits, millicores, memory quantities, init containers, GPU extended resources and pod parallelism remain native; no allocated-core estimate is invented.",
                           "Explicit context and namespace are required for reads. Pin scope.uri to the API server before submission or cancellation.",
                           "Cancellation sends DeleteOptions with a UID precondition. Suspend is not advertised as an equivalent of Slurm hold."]
        result["notes"].append("Raw namespaced reads avoid kubectl discovery and implicit pagination. A continuation reports an explicit incomplete-query error rather than silently increasing scheduler load.")
        result["missing"] = ["scheduler-wide fair share", "deleted-job accounting without an external source", "automatic compute telemetry", "Heavy allocation adapter"]
        return result
