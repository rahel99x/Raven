"""Private job-local Python startup hook; its bytes are bound into the plan."""
from __future__ import annotations

import hashlib
import os
import stat
import struct
from pathlib import Path

# Keep this source importable by older Python interpreters so unsupported
# applications still run. It never imports the Raven runtime below Python 3.10.
SITE_CUSTOMIZE = r'''"""Raven job-local attachment, chaining the site's existing startup module."""
import importlib.machinery
import importlib.util
import os
import sys

def _raven_startup():
    here = os.path.dirname(os.path.abspath(__file__))
    paths = [p for p in sys.path if os.path.abspath(p or os.getcwd()) != here]
    chained = False
    chain_error = ""
    try:
        spec = importlib.machinery.PathFinder.find_spec("sitecustomize", paths)
        if spec is not None and spec.loader is not None and spec.origin != __file__:
            module = importlib.util.module_from_spec(spec)
            sys.modules["sitecustomize"] = module
            chained = True
            spec.loader.exec_module(module)
    except Exception as exc:
        chain_error = type(exc).__name__ + ": " + str(exc)
        try:
            print("Error in chained sitecustomize: " + chain_error, file=sys.stderr)
        except Exception:
            pass
    if not os.environ.get("RAVEN_RUN_DIR") or not os.environ.get("RAVEN_LAUNCH_PLAN"):
        return
    if os.environ.get("RAVEN_TRACE_SKIP_AUTO") == "1":
        return
    if sys.version_info < (3, 10):
        try:
            print("Raven tracing unavailable: this interpreter is below Python 3.10; application runs unchanged", file=sys.stderr)
        except Exception:
            pass
        try:
            import subprocess
            subprocess.run([os.environ["RAVEN_REPORT_PYTHON"], "-I", os.environ["RAVEN_RUNTIME_BUNDLE"],
                "startup-gap", str(os.getpid()), str(os.getppid()), ".".join(str(x) for x in sys.version_info[:3])],
                timeout=2, check=False)
        except Exception:
            pass
        return
    try:
        # Keep the application name `raven` available for a vendored client or
        # older installation. Relative imports load our frozen runtime in a
        # private package without changing ordinary application precedence.
        import types
        name = "_raven_launch_runtime"
        if name not in sys.modules:
            package = types.ModuleType(name)
            package.__path__ = [os.environ["RAVEN_RUNTIME_BUNDLE"] + "/raven"]
            package.__package__ = name
            package.__spec__ = importlib.machinery.ModuleSpec(name, loader=None, is_package=True)
            sys.modules[name] = package
        from _raven_launch_runtime.launch_trace import auto_attach
        auto_attach(chained=chained, chain_error=chain_error)
    except Exception as exc:
        try:
            print("Raven automatic tracing unavailable; application runs unchanged: " + str(exc), file=sys.stderr)
        except Exception:
            pass

_raven_startup()
del _raven_startup
'''


def digest():
    return hashlib.sha256(SITE_CUSTOMIZE.encode()).hexdigest()


def prepare(run, plan, env, *, previous=None):
    """Expose the portable client and optionally the trace hook to descendants."""
    from . import reporting as R
    from .launch_runtime import _exclusive
    from .launch_trace import _diagnostic
    from .research_reporting import _lock

    old = env.get("PYTHONPATH", "")
    previous_site = env.pop("RAVEN_TRACE_SITE", "")
    bundle = plan["runtime_bundle"]
    paths = old.split(os.pathsep) if old else []
    previous = previous if isinstance(previous, dict) else {}
    previous_bundle = previous.get("RAVEN_RUNTIME_BUNDLE", "")
    if previous_bundle and previous_bundle in paths:
        try:
            from .launch_autotrace import verified_run
            previous_plan_path = previous["RAVEN_LAUNCH_PLAN"]
            previous_plan = R._read_json(previous_plan_path)
            verified_run(previous["RAVEN_RUN_DIR"], previous_plan)
            expected = R._under(previous_plan["project_root"],
                                ".raven/wrappers/" + previous_plan["token"] + "/runtime.pyz")
            if (str(expected) != previous_bundle or previous_plan.get("runtime_bundle") != previous_bundle
                    or str(expected.with_name("plan.json")) != previous_plan_path):
                raise ValueError("Inherited archive is not the prior launch's owned runtime")
            info = expected.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError("Inherited runtime archive is not private and owned")
            paths = [path for path in paths if path != previous_bundle]
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass  # Unverified user archives keep their original import order.
    # Nested launches can inherit a previous run's injection. Remove only the
    # exact advertised path when its private file and run receipt prove Raven
    # created it. Never remove arbitrary user/site paths by a naming guess.
    if previous_site and previous_site in paths:
        try:
            site = Path(previous_site)
            prior_run = site.parent.parent
            if (not site.is_absolute() or site.name != "site" or site.parent.name != ".raven-runtime"
                    or prior_run.parent.name != "runs"):
                raise ValueError("Not a Raven startup path")
            prior = R._read_json(R._under(prior_run, "run.json"))
            startup = prior.get("metadata", {}).get("automation", {}).get("python_startup", {})
            if (prior.get("run_id") != prior_run.name or startup.get("automatic") is not True
                    or startup.get("site_directory") != ".raven-runtime/site"):
                raise ValueError("No matching Raven startup receipt")
            source = R._under(prior_run, ".raven-runtime/site/sitecustomize.py")
            fd = os.open(source, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise ValueError("Startup file is not private and owned")
                raw = stream.read(65537)
            if len(raw) > 65536 or hashlib.sha256(raw).hexdigest() != startup.get("sitecustomize_sha256"):
                raise ValueError("Startup bytes do not match their Raven receipt")
            paths = [path for path in paths if path != previous_site]
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass  # Unknown paths retain the application's existing precedence.
    paths = [p for p in paths if p != bundle]
    # Preserve application paths' order; the application script directory also
    # retains CPython's normal precedence over PYTHONPATH entries.
    paths.append(bundle)
    env["RAVEN_CLIENT"] = "1"
    env.pop("RAVEN_TRACE_ATTACHED", None)
    env.pop("RAVEN_TRACE_SKIP_AUTO", None)
    metadata = {"client": True, "trace_requested": bool(plan.get("trace")),
                "original_pythonpath": old[:8192], "automatic": False}
    if plan.get("trace"):
        budget = R._under(run, ".raven-runtime/trace-budget", create_parent=True)
        _exclusive(budget, struct.pack("!QQ", 0, 0), mode=0o600)
    if plan.get("trace") and env.get("RAVEN_TRACE_AUTO", "1") != "0":
        try:
            if plan.get("sitecustomize_sha256") != digest():
                raise ValueError("Startup hook digest does not match the frozen launch plan")
            runtime = R._under(run, ".raven-runtime/site/.unused", create_parent=True).parent
            os.chmod(runtime, 0o700)
            _exclusive(runtime / "sitecustomize.py", SITE_CUSTOMIZE.encode(), mode=0o400)
            env["RAVEN_TRACE_SITE"] = str(runtime)
            paths.insert(0, str(runtime))
            metadata.update(automatic=True, sitecustomize_sha256=digest(),
                            site_directory=str(runtime.relative_to(run)),
                            coverage="Only interpreters that inherit startup hooks can attach; isolated, site-disabled, embedded, and environment-replaced processes may be unobserved.")
        except Exception as exc:
            _diagnostic(run, "Optional automatic tracing could not be prepared: " + str(exc))
            metadata["reason"] = str(exc)
    elif plan.get("trace"):
        metadata["reason"] = "RAVEN_TRACE_AUTO=0 selects explicit raven_exec attachment"
    env["PYTHONPATH"] = os.pathsep.join(paths)
    with _lock(run):
        value = R._read_json(run / "run.json")
        value["metadata"]["automation"]["python_startup"] = metadata
        R._atomic_json(run / "run.json", value, replace=True)


def finish(run, plan):
    """Report missing startup observations without inventing unseen processes."""
    if not plan.get("trace"):
        return
    from . import reporting as R
    from .launch_autotrace import record_gap
    try:
        index = R._read_json(Path(run) / "trace/index.json")
        attached = any(s.get("schema") == "raven.trace/v1" for s in index.get("sources", []))
    except (OSError, ValueError):
        attached = False
    if not attached:
        record_gap(Path(run), "No Python attachment was observed. The script may be non-Python, use -I/-E/-S, replace PYTHONPATH, use an unsupported interpreter, or finish before startup. No specific unseen process or module command has been inferred.",
                   process={"role": "unobserved", "launcher": "job script"}, status="unverified")
