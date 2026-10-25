"""Explicit, private lab/site workspaces above scheduler-native adapters.

Saving a workspace never connects to a cluster. Inventory is an explicit read
of supplied, already configured clients; an account association is evidence,
not authority to spend that account's allocation.
"""

from __future__ import annotations

import copy
import time

from .. import evidence as E
from . import _storage as S

SCHEMA = "raven.lab-workspace/v1"
SCHEDULERS = {"slurm", "kubernetes", "lsf", "openpbs", "pbspro", "htcondor", "flux"}
RESOURCE_FIELDS = {"account", "partition", "cpus", "memory_mib", "wall_seconds", "nodes", "gpus"}


def _resources(value, label):
    E.keys(value, set(), RESOURCE_FIELDS, label)
    for key, item in value.items():
        if key in {"account", "partition"}:
            E.text(item, label + " " + key, 256)
        elif type(item) is not int or not (0 if key == "gpus" else 1) <= item <= 2**31 - 1:
            raise ValueError(label + " resources require bounded integer native allocations")
    return copy.deepcopy(value)


def validate(document):
    """Preserve login, billing, scientific defaults and Heavy defaults separately."""
    E.canonical(document)
    E.keys(document, {"schema", "name", "sites"}, {"digest", "guidelines"}, "lab workspace")
    if document["schema"] != SCHEMA:
        raise ValueError("Expected " + SCHEMA)
    E.text(document["name"], "workspace name", 128)
    if "guidelines" in document:
        E.text(document["guidelines"], "lab guidelines", 4096, empty=True)
    sites = document["sites"]
    if not isinstance(sites, list) or not 1 <= len(sites) <= 32:
        raise ValueError("A lab workspace needs 1..32 explicitly configured sites")
    seen = set()
    for site in sites:
        E.keys(site, {"id", "scheduler", "login", "scope", "accounts", "job_defaults", "heavy_defaults"},
               {"profile", "lab", "project", "association_source", "connection"}, "lab site")
        token = S.name(site["id"])
        if token in seen or not isinstance(site["scheduler"], str) or site["scheduler"] not in SCHEDULERS:
            raise ValueError("Lab sites need unique IDs and a supported scheduler")
        seen.add(token)
        E.text(site["login"], "system login", 256)
        if not isinstance(site["scope"], dict) or not site["scope"] or len(site["scope"]) > 16:
            raise ValueError("Site scope must identify its configured scheduler authority")
        for key, item in site["scope"].items():
            E.text(key, "scope key", 128)
            E.text(item, "scope value", 1024)
            if any(secret in key.lower() for secret in ("token", "password", "secret", "credential", "private_key")):
                raise ValueError("Connection scope must not contain credentials")
        if site["scope"].get("user") != site["login"]:
            raise ValueError("The scheduler scope must name the selected system login explicitly")
        from ..schedulers.core import normalize_scope

        normalize_scope(site["scope"])
        if "connection" in site:
            connection = site["connection"]
            E.keys(connection, set(), {"host", "ssh_user"}, "explicit site transport")
            for key, item in connection.items():
                E.text(item, "connection " + key, 256)
            if connection.get("host") and site["scope"].get("host") != connection["host"]:
                raise ValueError("The saved scheduler scope must pin the chosen SSH host")
            if connection.get("ssh_user") and not connection.get("host"):
                raise ValueError("An SSH user requires an explicit connection host")
        accounts = site["accounts"]
        if not isinstance(accounts, list) or len(accounts) > 256 or len(set(map(str, accounts))) != len(accounts):
            raise ValueError("Site accounts must be a bounded, unique list")
        for account in accounts:
            E.text(account, "billing account association", 256)
        for key in ("profile", "lab", "project", "association_source"):
            if key in site:
                E.text(site[key], key, 1024)
        _resources(site["job_defaults"], "scientific job")
        _resources(site["heavy_defaults"], "Raven Heavy worker")
        for key in ("job_defaults", "heavy_defaults"):
            account = site[key].get("account")
            if account and account not in accounts:
                raise ValueError("A selected billing account must have an explicit association; login is not an account")
        if accounts and not site.get("association_source"):
            raise ValueError("Account associations need their discovery or user-declaration source")
    value = copy.deepcopy(document)
    actual = E.digest({key: item for key, item in value.items() if key != "digest"})
    if "digest" in value and value["digest"] != actual:
        raise ValueError("Lab workspace digest differs; refresh before saving")
    value["digest"] = actual
    return value


def load(project):
    with S.project_store(project, "labs") as folder:
        return validate(S.read(folder, "workspace.json"))


def save(project, document, *, expected_digest=None):
    """Atomically save reviewed preferences, detecting concurrent edits."""
    value = validate(document)
    with S.locked(project, "labs") as folder:
        try:
            current = validate(S.read(folder, "workspace.json"))
        except FileNotFoundError:
            current = None
        if current is not None and expected_digest != current["digest"]:
            raise ValueError("Lab workspace changed; load its digest before replacing it")
        if current is None and expected_digest is not None:
            raise ValueError("Lab workspace no longer exists; refresh before saving")
        return S.write(folder, "workspace.json", value)


def defaults(document, site_id, *, purpose="job", account=None):
    """Return an explicit draft; this never grants authority or submits a job."""
    value = validate(document)
    if not isinstance(purpose, str) or purpose not in {"job", "heavy"}:
        raise ValueError("Choose scientific job or Heavy worker defaults")
    site = next((site for site in value["sites"] if site["id"] == site_id), None)
    if site is None:
        raise ValueError("Choose a configured lab site")
    result = copy.deepcopy(site[purpose + "_defaults"])
    if account is not None:
        if account and account not in site["accounts"]:
            raise ValueError("Choose an associated billing account; system login is separate")
        if account:
            result["account"] = account
        else:
            result.pop("account", None)
    return {"site": site_id, "scheduler": site["scheduler"], "login": site["login"],
            "purpose": purpose, "resources": result,
            "source_digest": value["digest"], "review_required": True,
            "billing": result.get("account") or "scheduler default (not an unlimited-allocation claim)"}


def inventory(document, clients, *, sites=None, max_sites=8, max_records=10000,
              cancel=None, wall_seconds=30):
    """Query chosen supplied clients sequentially within an explicit read budget.

    Missing clients remain disconnected. Existing scheduler-native command
    bounds and cancellation apply; the outer wall budget stops subsequent site
    requests and never interrupts/replays a mutation (there are no mutations).
    """
    value = validate(document)
    if (type(max_sites) is not int or not 1 <= max_sites <= 32
            or type(max_records) is not int or not 1 <= max_records <= 10000):
        raise ValueError("Inventory requires bounded site and record budgets")
    E.finite(wall_seconds, "inventory wall budget", .001)
    if wall_seconds > 120:
        raise ValueError("Inventory wall budget must be at most 120 seconds")
    selected = [site["id"] for site in value["sites"]] if sites is None else sites
    if (not isinstance(selected, list) or any(not isinstance(item, str) for item in selected) or len(selected) > max_sites
            or len(set(selected)) != len(selected)
            or set(selected) - {site["id"] for site in value["sites"]}):
        raise ValueError("Select unique known sites within the explicit inventory budget")
    deadline = time.monotonic() + wall_seconds
    result, remaining = [], max_records
    for site in value["sites"]:
        if site["id"] not in selected:
            continue
        row = {"site": site["id"], "scheduler": site["scheduler"], "login": site["login"],
               "accounts": site["accounts"], "jobs": [], "status": "disconnected"}
        client = clients.get(site["id"])
        if client is None:
            row["reason"] = "Connect this site explicitly before collecting its inventory"
        elif (cancel is not None and cancel.is_set()) or time.monotonic() >= deadline or not remaining:
            row.update(status="deferred", reason="Explicit inventory budget ended; refresh selected sites")
        else:
            from ..schedulers.core import scope_digest

            # A display label or matching login must not silently bind another
            # authority; compare the full configured native scope.
            actual_scope = getattr(client, "scope_key", None)
            if getattr(client, "scheduler", None) != site["scheduler"]:
                row.update(status="blocked", reason="Connected native scheduler differs from the saved site")
            elif actual_scope != scope_digest(site["scope"]):
                row.update(status="blocked", reason="Connected scheduler scope differs from the saved site")
            else:
                try:
                    jobs = client.jobs(user=site["login"])
                    if isinstance(jobs, dict):
                        jobs = jobs.get("jobs", [])
                    if not isinstance(jobs, list):
                        raise ValueError("Scheduler inventory did not return a job list")
                    row.update(status="available", jobs=copy.deepcopy(jobs[:remaining]),
                               omitted=max(0, len(jobs) - remaining), collected_at=time.time())
                    remaining -= len(row["jobs"])
                except (OSError, RuntimeError, ValueError) as exc:
                    row.update(status="failed", reason=str(exc)[:4096])
        result.append(row)
    return {"schema": "raven.lab-inventory/v1", "workspace_digest": value["digest"],
            "sites": result, "records": max_records - remaining,
            "freshness": "Explicit per-site reads; this is not an atomic cross-cluster snapshot",
            "allocation_limits": "Unknown unless supplied by scheduler/site accounting; no limits are inferred"}


def inventory_from_workspace(project, *, sites=None, max_sites=8, max_records=10000,
                             wall_seconds=30, cancel=None):
    """Installed-command entry point: open only explicitly selected transports.

    A site without ``connection.host`` uses local native commands. Merely saving
    a host name in an identity does not authorize a background SSH connection.
    Connections are closed after this bounded read, including failures.
    """
    from ..scheduler_cli import client, close

    document = load(project)
    selected = [site["id"] for site in document["sites"]] if sites is None else sites
    if (not isinstance(selected, list) or any(not isinstance(item, str) for item in selected)
            or type(max_sites) is not int or not 1 <= max_sites <= 32
            or len(selected) > max_sites or len(set(selected)) != len(selected)
            or set(selected) - {site["id"] for site in document["sites"]}):
        raise ValueError("Select known sites within the explicit connection budget")
    clients = {}
    failures = {}
    try:
        for site in document["sites"]:
            if site["id"] not in selected or (cancel is not None and cancel.is_set()):
                continue
            descriptor = dict(name=site["scheduler"], scope=site["scope"], **site.get("connection", {}))
            try:
                clients[site["id"]] = client(descriptor, cancel=cancel)
            except (OSError, RuntimeError, ValueError) as exc:
                failures[site["id"]] = str(exc)[:4096]
        report = inventory(document, clients, sites=selected, max_sites=max_sites,
                           max_records=max_records, wall_seconds=wall_seconds, cancel=cancel)
        for row in report["sites"]:
            if row["site"] in failures:
                row.update(status="failed", reason=failures[row["site"]])
        return report
    finally:
        for scheduler in clients.values():
            close(scheduler)


def defaults_for_workspace(project, site_id, *, purpose="job", account=None):
    """Choose stored lab defaults without copying the workspace into a request."""
    return defaults(load(project), site_id, purpose=purpose, account=account)
