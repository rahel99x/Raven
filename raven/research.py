"""Bounded, demand-driven background reads for the research workspace."""

from __future__ import annotations

from .job_logs.paths import stdout_path
from collections import OrderedDict
import math
import os
import threading
import time
import copy

from . import clock
from .local_files import LocalFiles
from .planning_catalog import PLANNING_VIEWS
from .ui.catalog import RESEARCH_VIEWS as RESEARCH_VIEWS
from .research_text import clean as clean
from .research_context import capture_inputs, interaction_for
from .worker_scheduler import WorkerScheduler


def select_job(app, jid, *, view=None, record_back=True):
    """Compatibility entry point through the application's interaction port."""
    return interaction_for(app).select(app, jid, view=view, record_back=record_back)


def detach_manual_source(app):
    """Compatibility entry point through the application's interaction port."""
    return interaction_for(app).detach(app)


class ResearchHub:
    def __init__(
        self,
        cfg,
        files=None,
        *,
        demo=False,
        settings=None,
        slurm=None,
        worker_scheduler=None,
    ):
        from .execution.policy import router_for

        self.execution = router_for(cfg)
        self.files = files or LocalFiles()
        self.slurm = slurm
        self.demo = demo
        self.settings = dict(cfg.get("research", {}) or {})
        self.log_settings = dict(cfg.get("logs", {}) or {})
        self.settings.update(
            {k: v for k, v in (settings or {}).items() if v is not None}
        )
        for key in ("metrics_file", "contract", "workdir", "passport", "planning_file"):
            if not isinstance(self.settings.get(key, ""), str):
                raise ValueError(f"research.{key} must be a path string")
        interval = self.settings.get("interval", 5)
        if (
            isinstance(interval, bool)
            or not isinstance(interval, (int, float))
            or not 1 <= interval <= 86400
        ):
            raise ValueError(
                "research.interval must be finite and between 1 and 86400 seconds"
            )
        self.interval = float(interval)
        self._managed_refresh_interval = None
        from .cadence import validate_multiplier

        self.polling_multiplier = validate_multiplier(cfg.get("polling_multiplier", 1))
        self.lock = threading.RLock()
        self._owns_worker_scheduler = worker_scheduler is None
        self.worker_scheduler = worker_scheduler or WorkerScheduler(
            mode=cfg.get("worker_mode", "multi"), lanes=("research",)
        )
        self.pool = self.worker_scheduler.lane("research")
        self.future = None
        self.pending = None
        self.closed = False
        self.cache = OrderedDict()
        self.generation = 0
        self.metric_sampling = {}
        self.reader = None
        self.plan = None
        self.passport = None
        self.passport_diff = None
        self.forecasts = None
        self.planning_source = None
        self.planning_choices = None

    def close(self):
        with self.lock:
            self.closed = True
        self.pool.shutdown(wait=False, cancel_futures=True)
        if self._owns_worker_scheduler:
            self.worker_scheduler.shutdown(wait=False, cancel_futures=True)

    def configure(self, **values):
        with self.lock:
            self.settings.update(values)
            self.generation += 1
            self.metric_sampling.clear()
            self.cache.clear()
            self.planning_source = None
            self.planning_choices = None

    def rebind_sources(self, *, passport_changed=False, **values):
        """Refresh one attempt's declarations while retaining unrelated views.

        The publication generation still invalidates every in-flight reader.
        Completed unaffected snapshots move to that generation, so adding a
        workflow report does not trigger another metric parse or evidence read.
        """
        with self.lock:
            changed = {
                key for key, value in values.items() if self.settings.get(key) != value
            }
            if "workdir" in changed:
                self.configure(**values)
                return
            affected = set()
            for key, views in {
                "metrics_file": {"experiment"},
                "contract": {"artifacts", "evidence"},
                "passport": {"passport"},
                "submit_file": {"submit"},
            }.items():
                if key in changed:
                    affected.update(views)
            if passport_changed:
                affected.add("passport")
            if changed & {"planning_file", "planning_files", "planning_overrides"}:
                affected.update(name for name, _ in PLANNING_VIEWS)
            if changed - {
                "metrics_file",
                "contract",
                "passport",
                "submit_file",
                "planning_file",
                "planning_files",
                "planning_overrides",
            }:
                self.configure(**values)
                return
            cached = [
                (key, entry)
                for key, entry in self.cache.items()
                if key[1] not in affected
            ]
            sampling = self.metric_sampling if "experiment" not in affected else {}
            source, choices = self.planning_source, self.planning_choices
            self.configure(**values)
            self.cache.update(
                ((self.generation, key[1], key[2]), entry) for key, entry in cached
            )
            self.metric_sampling = {
                (*identity[:-1], self.generation): rate
                for identity, rate in sampling.items()
            }
            if not affected.intersection(name for name, _ in PLANNING_VIEWS):
                self.planning_source, self.planning_choices = source, choices

    def set_polling_multiplier(self, value):
        from .cadence import validate_multiplier

        value = validate_multiplier(value)
        with self.lock:
            self.polling_multiplier = value

    def set_refresh_interval(self, seconds, *, managed=False):
        """Apply a live read cadence without discarding a cache or active reader."""
        import math

        if (
            type(seconds) not in (int, float)
            or not math.isfinite(seconds)
            or not 0.1 <= seconds <= 31536000
        ):
            raise ValueError(
                "Research refresh must be finite and between .1 and 31536000 seconds"
            )
        with self.lock:
            self.interval = float(seconds)
            self._managed_refresh_interval = float(seconds) if managed else None

    def _refresh_interval(self, rate):
        from .cadence import interval

        remote = bool(getattr(self.files, "remote", False))
        requested = interval(
            self.interval,
            self.polling_multiplier,
            rate,
            source="research",
            file=True,
            remote=remote,
        )
        if self._managed_refresh_interval is not None:
            requested = max(0.5, requested, self._managed_refresh_interval)
        # Remote metadata cannot safely be sampled faster than its transport.
        # A profile change affects the next read without replacing file handles.
        if remote:
            from .cadence import REMOTE_FILE_FLOOR

            requested = max(REMOTE_FILE_FLOOR, requested)
        return requested

    def set_metric_sampling(self, requests):
        """Replace bounded metric-file polling demands without starting reads."""
        from .cadence import MAX_REQUESTS, source, validate_rate

        accepted = {}
        if not isinstance(requests, dict) or len(requests) > MAX_REQUESTS:
            raise ValueError("Metric sampling requests must be a bounded dictionary")
        for identity, value in requests.items():
            rate = validate_rate(value)
            if (
                source(identity) == "research"
                and len(identity) == 8
                and identity[7] == self.generation
                and rate > 1
            ):
                accepted[identity] = rate
        with self.lock:
            self.metric_sampling = accepted

    def refresh_interval(self, context=None):
        from .cadence import research_matches

        with self.lock:
            rate = max(
                (
                    value
                    for identity, value in self.metric_sampling.items()
                    if context
                    and context.get("view") == "experiment"
                    and research_matches(identity, context)
                ),
                default=1,
            )
            return self._refresh_interval(rate)

    def sampling_interval(self, identity):
        """Display the shared file-reader cadence, separate from producer timing."""
        with self.lock:
            rate = max(
                (
                    value
                    for key, value in self.metric_sampling.items()
                    if key[1] == identity[1]
                    and key[4:] == identity[4:]
                    and key[7] == self.generation
                ),
                default=1,
            )
            return self._refresh_interval(rate)

    def start_task(self, fn, completion):
        """Explicit UI commands share the single worker, without an unbounded queue."""
        from .execution.policy import background_allowed, bounded_background

        if not background_allowed(self.execution, fn):
            return False
        with self.lock:
            if self.closed or self.pending:
                return False
            try:
                self.future = self.pool.submit(bounded_background(self.execution, fn))
            except RuntimeError:
                return False
            self.pending = (self.future, completion)
            return True

    def cancel_task(self, completion):
        """Detach one automatic callback so an explicit command can take priority.

        A running read completes on the same single worker; no second worker or
        unbounded queue is created. Its unpublished result is discarded.
        """
        with self.lock:
            if not self.pending or self.pending[1] is not completion:
                return False
            self.pending[0].cancel()
            self.pending = None
            return True

    def poll_task(self):
        """Publish command state on the UI thread, never from a worker."""
        with self.lock:
            pending = self.pending
            if self.closed or not pending or not pending[0].done():
                return
            self.pending = None
        try:
            result = pending[0].result()
        except Exception as exc:
            result = exc
        pending[1](result)

    def context(self, snap, app):
        project_state = getattr(app, "project_state", {}) or {}
        binding = (
            project_state.get("binding") if isinstance(project_state, dict) else None
        )
        jid = (
            binding.get("job_id")
            if isinstance(binding, dict)
            else getattr(app, "research_job_id", None) or app.selected_id
        )
        jobs = (
            snap.get("jobs", [])
            + snap.get("finished", [])
            + list(snap.get("departed_jobs", {}).values())
        )
        job = next((j for j in jobs if j.id == jid), None)
        inputs = capture_inputs(project_state, getattr(app, "job_selection_state", {}))
        if (
            job is None
            and not jid
            and jobs
            and not binding
            and not inputs.selection_cleared
        ):
            job = jobs[0]
            jid = job.id
        project_logs = None
        if binding:
            project_logs = [dict(entry) for entry in inputs.project_logs]
        return {
            "view": getattr(app, "research_view", "experiment"),
            "jid": jid,
            "explicit_jid": getattr(app, "research_job_id", None),
            "job": job,
            "snap": snap,
            "settings": copy.deepcopy(self.settings),
            "log_settings": dict(
                getattr(app, "cfg", {}).get("logs", self.log_settings)
            ),
            "run_id": binding.get("run_id") if binding else None,
            "run_root": binding.get("run_root") if binding else None,
            "binding": copy.deepcopy(binding) if binding else None,
            "project_logs": project_logs,
            "project_warnings": list(project_state.get("run_warnings", []))
            if binding
            else [],
            "generation": self.generation,
        }

    def _key(self, context):
        jid = (
            context["jid"]
            if context["view"]
            in (
                "experiment",
                "evidence",
                "predict",
                "forecast",
                "blockers",
                "tradeoffs",
            )
            else None
        )
        return context["generation"], context["view"], jid

    def current(self, context):
        with self.lock:
            entry = self.cache.get(self._key(context))
            if entry:
                self.cache.move_to_end(self._key(context))
            return (
                entry[1]
                if entry
                else {
                    "status": "loading",
                    "summary": "Waiting for the background reader.",
                }
            )

    def request(self, context, *, wait=False, force=False):
        """At most one worker is active; frames only inspect published snapshots."""
        for _ in range(2 if wait else 1):
            with self.lock:
                if self.closed:
                    return self.current(context)
                if self.pending:
                    return self.current(context)
                entry = self.cache.get(self._key(context))
                if (
                    not force
                    and entry
                    and time.monotonic() - entry[0] < self.refresh_interval(context)
                ):
                    self.cache.move_to_end(self._key(context))
                    return entry[1]
                if self.future is None or self.future.done():
                    try:
                        from .execution.policy import bounded_background

                        self.future = self.pool.submit(
                            bounded_background(
                                self.execution, lambda: self._publish(context)
                            )
                        )
                    except RuntimeError:
                        return {
                            "status": "loading",
                            "summary": "The background queue is busy; this view will retry.",
                        }
                future = self.future
            if not wait:
                break
            # Remote reads are individually timed out. No such waits occur in a frame.
            future.result(timeout=90)
            force = False
        return self.current(context)

    def _publish(self, context):
        try:
            result = self._read(context)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            result = {"status": "error", "summary": clean(exc)}
        except Exception as exc:
            # A new data adapter cannot take down the curses loop.
            result = {
                "status": "error",
                "summary": f"{type(exc).__name__}: {clean(exc)}",
            }
        with self.lock:
            if not self.closed and context["generation"] == self.generation:
                self.cache[self._key(context)] = (time.monotonic(), result)
                if context["view"] == "tradeoffs":
                    self.planning_choices = result
                self.cache.move_to_end(self._key(context))
                while len(self.cache) > 16:
                    self.cache.popitem(last=False)
        return result

    def _path(self, value, context):
        if not value:
            return ""
        # Only the documented job ID token is expanded; arbitrary format code is not.
        value = str(value).replace("{job_id}", str(context["jid"] or "unknown"))
        workdir = context["settings"].get("workdir", "")
        if workdir and not os.path.isabs(value):
            value = os.path.join(workdir, value)
        return value if self.files.remote else os.path.expanduser(value)

    def _read(self, context):
        view, settings, snap = context["view"], context["settings"], context["snap"]
        if view == "operations":
            from .operations import catalog

            return {
                "status": "ok",
                "summary": "Research and cluster operations",
                "operations": catalog(),
            }
        if view in dict(PLANNING_VIEWS):
            return self._planning(context)
        if view == "experiment":
            path = self._path(settings.get("metrics_file"), context)
            if not path:
                return (
                    self._demo_metrics()
                    if self.demo
                    else {
                        "status": "empty",
                        "summary": "Attach a metrics stream with :metrics PATH or --metrics-file PATH.",
                    }
                )
            if self.reader is None or self.reader.files is not self.files:
                from .metrics import MetricReader

                self.reader = MetricReader(self.files)
            selected = settings.get("metric_stream", {})
            rank_stream = (
                selected.get("name")
                if isinstance(selected, dict) and selected.get("source") == path
                else None
            )
            options = {"rank_stream": rank_stream} if rank_stream else {}
            return (
                self.reader.read_confined(path, context["binding"], **options)
                if context.get("binding")
                else self.reader.read(path, **options)
            )
        if view == "arrays":
            from .arrays import summarize

            return {
                "status": "ok",
                "groups": summarize(snap.get("jobs", []), snap.get("finished", [])),
            }
        if view == "artifacts":
            from .artifacts import load_contract, validate_contract

            path = settings.get("contract", "")
            if not path:
                return {
                    "status": "empty",
                    "summary": "Attach a JSON output contract with :artifacts CONTRACT ROOT.",
                }
            if self.files.remote and not context.get("binding"):
                return {
                    "status": "incomplete",
                    "summary": "Output validation needs a local checkout on the job's cluster. Run Raven on the CARC login node.",
                }
            if context.get("binding"):
                from .projects import read_bound_artifacts

                return read_bound_artifacts(context["binding"], files=self.files)
            return validate_contract(
                load_contract(os.path.expanduser(path)),
                settings.get("workdir") or os.getcwd(),
                files=self.files,
            )
        if view == "passport":
            from .provenance import load

            path = settings.get("passport", "")
            if path:
                if self.files.remote and not context.get("binding"):
                    return {
                        "status": "incomplete",
                        "summary": "Passport inspection requires running Raven on the cluster.",
                    }
                if context.get("binding"):
                    from .projects import read_bound_passport

                    passport = read_bound_passport(
                        context["binding"], path, files=self.files
                    )
                else:
                    passport = load(os.path.expanduser(path))
                return {
                    "status": "ok",
                    "passport": passport,
                    "differences": self.passport_diff,
                }
            return (
                {
                    "status": "ok",
                    "passport": self.passport,
                    "differences": self.passport_diff,
                }
                if self.passport or self.passport_diff is not None
                else {
                    "status": "empty",
                    "summary": "Capture a run with :passport capture SCRIPT --workdir DIR.",
                }
            )
        if view == "submit":
            if context.get("binding"):
                path = settings.get("submit_file", "")
                if not path:
                    return {
                        "status": "empty",
                        "summary": "This run declares no submission report; prepare a batch script explicitly to submit a new job.",
                    }
                from .projects import read_bound_json
                from .submission import SCHEMA, _digest

                plan = read_bound_json(
                    context["binding"], path, key="submit", files=self.files
                )
                if (
                    isinstance(plan, dict)
                    and plan.get("schema") == "raven.launch-report/v1"
                ):
                    from .launching.results import validate

                    report = validate(plan, context["binding"])
                    return {
                        "status": "ok",
                        "launch_report": report,
                        "display_only": True,
                        "source": path,
                    }
                if (
                    not isinstance(plan, dict)
                    or plan.get("schema") != SCHEMA
                    or type(plan.get("valid")) is not bool
                    or not isinstance(plan.get("resources"), dict)
                    or not isinstance(plan.get("issues"), list)
                    or plan.get("plan_id") != _digest(plan)
                ):
                    raise ValueError(
                        "run submission report is not an intact native preflight plan"
                    )
                for issue in plan["issues"]:
                    if not isinstance(issue, dict) or not all(
                        isinstance(issue.get(key), str) for key in ("level", "message")
                    ):
                        raise ValueError(
                            "run submission report contains malformed issue records"
                        )
                return {
                    "status": "ok",
                    "plan": plan,
                    "display_only": True,
                    "source": path,
                }
            return (
                {"status": "ok", "plan": self.plan}
                if self.plan
                else {
                    "status": "empty",
                    "summary": "Prepare a batch script with :prepare SCRIPT --workdir DIR [resource flags].",
                }
            )
        if view == "evidence":
            from .investigate import investigate

            job = context["job"]
            if job is None:
                return {
                    "status": "empty",
                    "summary": "The selected run has no visible scheduler record; its project logs remain available in Logs."
                    if context.get("binding")
                    else "Select a job in Jobs or History, then open Evidence.",
                }
            details = snap.get("details", {}).get(job.id, {})
            if not details and self.slurm is not None:
                details = self.slurm.details(job.id)
            logs, additional, errors, coverage = self._evidence_logs(context, details)
            artifact_result = None
            if settings.get("contract"):
                artifact_result = self._read(dict(context, view="artifacts"))
            result = investigate(
                job,
                details=details,
                live=snap.get("live", {}).get(job.id),
                events=snap.get("events", []),
                artifact_results=artifact_result,
                logs=additional,
                **logs,
            )
            result.setdefault("limitations", []).extend(errors)
            result["coverage"] = coverage
            return result
        return {"status": "error", "summary": "Unknown research view."}

    def _evidence_logs(self, context, details):
        """Use the same job-scoped catalog as Logs on the existing worker.

        Minimal embedding adapters with only ``tail`` retain the scheduler-only
        interface. Real local/SSH backends and full adapters discover registered
        and job-scoped directory logs; reads are bounded across all sources.
        """
        from .investigate import MAX_LOG_BYTES, MAX_LOG_SOURCES, MAX_TOTAL_LOG_BYTES
        from .job_logs.catalog import build_catalog

        job, settings = context["job"], context["settings"]
        binding = context.get("binding") or {}
        paths = {
            "stdout": binding.get("stdout") or stdout_path(job, details, self.files),
            "stderr": binding.get("stderr")
            or stdout_path(job, details, self.files, "StdErr"),
        }
        if not self.files.remote:
            # Explicit legacy research adapters may supply relative paths;
            # retain their original semantics rather than inventing a root.
            for key, option in (("stdout", "StdOut"), ("stderr", "StdErr")):
                if not paths[key] and details.get(option):
                    paths[key] = details[option]
        errors = [
            f"{option} path not available"
            for key, option in (("stdout", "StdOut"), ("stderr", "StdErr"))
            if not paths[key]
        ]
        manifest = binding.get("log_manifest") or context.get(
            "log_settings", self.log_settings
        ).get("manifest_file", "")
        if manifest:
            manifest = str(manifest).replace("{job_id}", job.id)
            if not os.path.isabs(manifest):
                root = (
                    settings.get("workdir")
                    or details.get("WorkDir")
                    or getattr(job, "workdir", "")
                )
                if not root or (self.files.remote and not os.path.isabs(root)):
                    errors.append(
                        "Log manifest unavailable: selected job workdir is unknown."
                    )
                    manifest = ""
                else:
                    manifest = os.path.join(root, manifest)
        if binding:
            # The picker has already validated confinement. Re-reading its
            # manifest here could reintroduce rejected external declarations.
            entries = context.get("project_logs") or []
            errors.extend(
                clean(warning) for warning in context.get("project_warnings", [])[:64]
            )
        elif hasattr(self.files, "stat") and hasattr(self.files, "read"):
            catalog = build_catalog(
                job.id,
                paths["stdout"],
                paths["stderr"],
                manifest_file=manifest,
                files=self.files,
            )
            errors.extend(catalog.get("messages", []))
            entries = catalog.get("entries", [])
        else:
            entries = [
                {
                    "id": "scheduler." + key,
                    "path": path,
                    "label": key,
                    "group": "Scheduler",
                    "source": "scheduler",
                }
                for key, path in paths.items()
                if path
            ]
            if manifest:
                errors.append(
                    "This file adapter cannot inspect the registered log manifest."
                )
        logs, additional, used, inspected = {}, [], 0, 0
        failures = []
        # Preserve stdout/stderr ordering for simple adapters. Additional rank
        # and application logs are explicit and never borrow another job's paths.
        for entry in entries[:MAX_LOG_SOURCES]:
            remaining = MAX_TOTAL_LOG_BYTES - used
            if remaining <= 0:
                break
            path, limit = entry["path"], min(MAX_LOG_BYTES, remaining)
            try:
                before, metadata_error = None, None
                bound_log = bool(binding and entry.get("source") == "project")
                if bound_log:
                    from .projects import read_bound_tail

                    data, size, before = read_bound_tail(
                        binding, entry, limit, files=self.files
                    )
                elif hasattr(self.files, "snapshot_stat"):
                    try:
                        before = self.files.snapshot_stat(path)
                    except OSError as exc:
                        metadata_error = exc
                if not bound_log:
                    data, size = self._tail(path, limit)
                if metadata_error is not None:
                    raise metadata_error
                if not isinstance(data, bytes) or len(data) > limit:
                    raise ValueError(
                        "file adapter returned an oversized or invalid log excerpt"
                    )
                after = (
                    before
                    if bound_log
                    else self.files.snapshot_stat(path)
                    if before is not None
                    else None
                )
                if before is not None and before != after:
                    raise ValueError("log changed during inspection; refresh Evidence")
                if (
                    isinstance(size, bool)
                    or not isinstance(size, int)
                    or size < len(data)
                ):
                    raise ValueError("invalid log size metadata")
                used += len(data)
                inspected += 1
                truncated = size > len(data)
                if truncated:
                    # A tail can begin inside a physical line. Remove the
                    # incomplete prefix instead of citing it as a full line.
                    newline = data.find(b"\n")
                    if newline >= 0:
                        data = data[newline + 1 :]
                excerpt = {
                    "text": data,
                    "path": path,
                    "truncated": truncated,
                    "first_line": None if truncated else 1,
                    "file_identity": before,
                    "label": entry["label"],
                    "group": entry["group"],
                }
                key = (
                    "stdout"
                    if path == paths["stdout"]
                    else "stderr"
                    if path == paths["stderr"]
                    else None
                )
                if key:
                    logs[key] = excerpt
                else:
                    additional.append(dict(excerpt, source="log:" + entry["id"]))
            except (OSError, ValueError, TypeError) as exc:
                message = f"{entry['label']}: {clean(exc)}"
                failures.append(path)
                errors.append(message)
        omitted = max(0, len(entries) - inspected - len(failures))
        if omitted:
            errors.append(
                f"{omitted} catalogued log files were omitted by the {MAX_LOG_SOURCES}-file / 1 MiB inspection budget."
            )
        coverage = {
            "catalog_files": len(entries),
            "inspected_files": inspected,
            "omitted_files": omitted,
            "unavailable_files": len(failures),
            "bytes_examined": used,
            "budget_bytes": MAX_TOTAL_LOG_BYTES,
            "max_files": MAX_LOG_SOURCES,
            "manifest_file": manifest,
        }
        return logs, additional, errors, coverage

    def _planning(self, context):
        from .planning import analyze, demo_source, record, select_job
        from .planning_io import load_json

        settings, snap, view = context["settings"], context["snap"], context["view"]
        if self.execution.managed:
            blocked = self.execution.require(
                "research:" + view, worker_operation="planning"
            )
            if blocked is not None:
                return blocked
        source, job = None, context["job"]
        selected_file_id = None
        overrides = dict(settings.get("planning_overrides", {}).get(view, {}))
        path = settings.get("planning_files", {}).get(view) or settings.get(
            "planning_file", ""
        )
        if path:
            if self.files.remote and not context.get("binding"):
                return {
                    "status": "incomplete",
                    "summary": "Local planning files require running Raven on the cluster.",
                }
            max_bytes = 1048576
            if view == "scaling":
                from .scaling import MAX_BYTES

                max_bytes = MAX_BYTES
            with self.lock:
                cached = self.planning_source
            if (
                cached
                and cached[0] == path
                and cached[3] == max_bytes
                and time.monotonic() - cached[1] < self.refresh_interval()
            ):
                source = cached[2]
            else:
                if context.get("binding"):
                    from .projects import read_bound_json

                    key = (
                        view
                        if settings.get("planning_files", {}).get(view)
                        else "planning"
                    )
                    source = read_bound_json(
                        context["binding"],
                        path,
                        key=key,
                        max_bytes=max_bytes,
                        files=self.files,
                    )
                else:
                    source = load_json(path, max_bytes=max_bytes)
                with self.lock:
                    if not self.closed and context["generation"] == self.generation:
                        self.planning_source = (
                            path,
                            time.monotonic(),
                            source,
                            max_bytes,
                        )
            if (
                context.get("binding")
                and view in ("predict", "forecast", "blockers", "tradeoffs")
                and isinstance(source, dict)
                and source.get("job_id") is not None
                and str(source["job_id"]) != str(context["jid"])
            ):
                raise ValueError(
                    "planning job identity does not match the selected run"
                )
            if isinstance(source, list) and view in ("forecast", "blockers"):
                source = {"jobs": source}
            if (
                isinstance(source, dict)
                and "jobs" in source
                and view in ("predict", "forecast", "blockers", "tradeoffs")
            ):
                rows = source["jobs"]
                current = (
                    context["jid"]
                    if any(
                        str(record(j).get("id") or record(j).get("job_id"))
                        == str(context["jid"])
                        for j in rows
                    )
                    else overrides.get("job_id")
                )
                if context.get("binding"):
                    # A run-specific report must not fall back to a sibling job.
                    current = context["jid"]
                    if current is None:
                        raise ValueError(
                            "selected run has no scheduler job identity for this planning job list"
                        )
                    if source.get("job_id") is not None and str(
                        source["job_id"]
                    ) != str(current):
                        raise ValueError(
                            "planning job identity does not match the selected run"
                        )
                job = select_job(
                    rows,
                    current or source.get("job_id"),
                    pending=view in ("forecast", "blockers"),
                )
                selected_file_id = record(job).get("id") or record(job).get("job_id")
        elif self.demo and view in ("predict", "tradeoffs", "scaling", "workflow"):
            source = demo_source(job)
        observations = self.forecasts.observations() if self.forecasts else ()
        if (
            view == "predict"
            and context.get("binding")
            and isinstance(source, dict)
            and "query" not in source
        ):
            scientific = context["binding"].get("scientific_query")
            if isinstance(scientific, dict) and scientific:
                # Scientific cohort identity comes from the producer's run,
                # not a scheduler display name such as "batch.sh".
                source = dict(source, query=dict(record(job), **scientific))
        if selected_file_id is not None:
            overrides["job_id"] = selected_file_id
        if self.execution.managed:
            from .planning_input import planning_inputs

            receipt = self.execution.dispatch(
                "planning",
                planning_inputs(
                    view,
                    source,
                    snap=snap,
                    job=job,
                    observations=observations,
                    overrides=overrides,
                ),
                {"job_id": context.get("jid"), "run_id": context.get("run_id")},
            )
            if receipt.get("status") == "deferred":
                return receipt
            result = receipt["result"]
        else:
            result = analyze(
                view,
                source,
                snap=snap,
                job=job,
                observations=observations,
                overrides=overrides,
            )
        if view == "forecast" and getattr(self, "forecast_restore_warning", ""):
            result.setdefault("limitations", []).append(self.forecast_restore_warning)
        simulated = isinstance(source, dict) and source.get("simulated") is True
        result["source"] = (
            ("SIMULATED frozen observations: " if simulated else "") + path
            if path
            else "simulated planning observations"
            if source is not None and self.demo
            else "scheduler snapshot"
        )
        if isinstance(source, dict) and "jobs" in source:
            result["data_jobs"] = [record(j) for j in source["jobs"][:256]]
        return result

    def _tail(self, path, limit):
        if self.files.remote or type(self.files) is not LocalFiles:
            return self.files.tail(path, limit)
        from .artifacts import read_local_tail

        data, size, stable = read_local_tail(path, max_bytes=limit)
        if not stable:
            raise ValueError("log changed during inspection; retry the view")
        return data, size

    @staticmethod
    def _demo_metrics():
        now = clock.now()
        series = {"loss": [], "accuracy": [], "samples_per_second": []}
        for i in range(120):
            for name, value in (
                ("loss", 2.4 * math.exp(-i / 35) + 0.03),
                ("accuracy", 0.3 + 0.65 * (1 - math.exp(-i / 40))),
                ("samples_per_second", 120 + 15 * math.sin(i / 8)),
            ):
                series[name].append(
                    {"t": now - (119 - i) * 5, "value": value, "step": i}
                )
        return {
            "status": "ok",
            "path": "simulated application metrics",
            "series": series,
            "latest": {k: v[-1]["value"] for k, v in series.items()},
            "phase": "training",
            "progress": {
                "completed": 120,
                "total": 200,
                "unit": "steps",
                "fraction": 0.6,
                "eta_seconds": 400,
                "eta_source": "simulated_progress",
            },
            "errors": [],
            "records": 120,
        }
