"""Terminal workflows preserve explicit targets, review gates and shell data."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import json
import os
import shlex
import subprocess
import threading
import time

import pytest

from tower import terminal_workflows as W
from tower import operations as O, ops_cluster as C, ops_ui
from test_operations_sequences import operation_dashboard


@pytest.fixture
def binaries(monkeypatch):
    monkeypatch.setattr(W.shutil, "which", lambda name: name if name.startswith("/") else "/usr/bin/" + name)


@pytest.mark.parametrize("host", ["login", "cluster.example.edu", "alice@login", "lab-jump", "[::1]", "::1", "alice@[2001:db8::1]"])
def test_explicit_ssh_keeps_configuration_and_host_verification(host, binaries):
    spec = W.ssh_spec(host)
    assert spec.argv == ("/usr/bin/ssh", "-t", "--", host.replace("[", "").replace("]", ""))
    assert spec.metadata["host"] == host
    assert spec.metadata["shell"] == ""
    assert not any("StrictHostKeyChecking" in arg for arg in spec.argv)


@pytest.mark.parametrize("host", ["", "-oProxyCommand=id", "a\nid", "a;id", "a $(id)", "a/b", "@host", "a\x00", "x" * 254, "a@b@c", "[::1]:2222", "::1%a;id", "-bad@::1", None])
def test_ssh_rejects_argument_and_shell_injection(host, binaries):
    with pytest.raises(ValueError):
        W.ssh_spec(host)


def test_no_backend_implicit_remote_target(binaries, tmp_path):
    spec = W.local_spec(str(tmp_path), shell="/bin/bash")
    assert spec.kind == "local" and spec.metadata["destination"] == "local"
    assert spec.cwd == str(tmp_path)
    assert spec.argv == ("/bin/bash", "-i")


@pytest.mark.parametrize("hosts", [[], ["a"] * 2, list("abcde"), ["valid", "--evil"]])
def test_collection_all_or_nothing_preflight(hosts, binaries):
    with pytest.raises(ValueError):
        W.ssh_collection(hosts)


def test_collection_bounded_distinct_targets(binaries):
    specs = W.ssh_collection(["a", "b", "c", "d"])
    assert len(specs) == 4
    assert [spec.metadata["host"] for spec in specs] == list("abcd")


@pytest.mark.parametrize("name", ["lab", "lab_2026", "experiment-1"])
def test_tmux_is_explicit_durable_and_dedicated(name, binaries):
    spec = W.tmux_spec(name)
    assert spec.argv == ("/usr/bin/tmux", "-L", "tower-fusion", "new-session", "-A", "-s", name)
    assert spec.metadata["persistent"] is True
    assert spec.metadata["shell"] == ""  # an existing tmux client can contain a REPL


@pytest.mark.parametrize("name", ["-all", "a:b", "a.b", "$(id)", "a\nb", "", "x" * 65, "a b"])
def test_tmux_rejects_target_syntax(name, binaries):
    with pytest.raises(ValueError):
        W.tmux_spec(name)


@pytest.mark.parametrize("engine,target", [("docker", "analysis"), ("podman", "9abcdef"), ("apptainer", "/project/model image.sif"), ("singularity", "docker://library/python")])
def test_container_boundaries_and_argv(engine, target, binaries):
    spec = W.container_spec(engine, target, "/bin/bash")
    assert spec.argv[-3:] == (target, "/bin/bash", "-i")
    assert spec.metadata["destination"] == "container"
    assert "not assumed equivalent" in spec.metadata["path_scope"]
    assert "--" in spec.argv


@pytest.mark.parametrize("engine,target,shell", [("bad", "x", "/bin/sh"), ("docker", "a;id", "/bin/sh"), ("podman", "--privileged", "/bin/sh"), ("apptainer", "--bind", "/bin/sh"), ("docker", "x", "sh"), ("docker", "x", "/bin/"), ("docker", "x", "/bin/sh\nid")])
def test_container_unsafe_target_or_shell_rejected(engine, target, shell, binaries):
    with pytest.raises(ValueError):
        W.container_spec(engine, target, shell)


def test_missing_optional_tool_fails_before_open(monkeypatch):
    monkeypatch.setattr(W.shutil, "which", lambda name: None)
    with pytest.raises(ValueError, match="Install"):
        W.ssh_spec("cluster")


@pytest.mark.parametrize("shell", ["bash", "zsh", "sh", "dash", "ksh"])
@pytest.mark.parametrize("value", ["", "a b", "x'y", "$(touch PWNED)", "x;id", "x\\y", "😀 data", "--help"])
def test_shell_data_argument_roundtrip(shell, value):
    assert shlex.split(W.quote(value, shell)) == [value]


def test_fish_single_quote_and_backslash_are_escaped():
    assert W.quote("a'b\\c", "fish") == "'a\\'b\\\\c'"


@pytest.mark.parametrize("shell", ["python", "ipython", "vim", "", None])
def test_prepared_commands_reject_unknown_repl(shell):
    with pytest.raises(ValueError, match="known shell"):
        W.quote("path", shell)


def test_recipe_substitutions_cannot_become_shell_syntax(tmp_path):
    value = W.recipe("cat", "printf '%s' {argument}")
    payload = "a' ; touch " + str(tmp_path / "PWNED") + "; echo '"
    text = W.expand_recipe(value, {"argument": payload})
    result = subprocess.run(["/bin/sh", "-c", text], capture_output=True, text=True, timeout=3)
    assert result.returncode == 0 and result.stdout == payload
    assert not (tmp_path / "PWNED").exists()


@pytest.mark.parametrize("template", ["echo '{argument}'", 'echo "{argument}"', "echo prefix{argument}", "echo {argument}suffix", "echo {a.b}", "echo {a[0]}", "echo {a!r}", "echo {a:10}", "echo {}", "echo '{argument} '", "echo ` echo {argument} `", "echo $( echo {argument} )"])
def test_recipe_rejects_ambiguous_or_already_quoted_placeholder(template):
    with pytest.raises(ValueError):
        W.recipe("unsafe", template)


@pytest.mark.parametrize("params", [{}, {"a": "1", "other": "2"}, {"b": "2"}, None])
def test_recipe_requires_exact_parameters(params):
    with pytest.raises(ValueError):
        W.expand_recipe(W.recipe("run", "echo {a}"), params)


def test_recipe_file_is_versioned_private_exclusive_and_roundtrips(tmp_path):
    recipes = {"show": W.recipe("show", "cat {file}")}
    path = tmp_path / "recipes.json"
    W._save_recipes(str(path), recipes)
    assert W._load_recipes(str(path)) == recipes
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text())["schema"] == W.RECIPE_SCHEMA
    with pytest.raises(FileExistsError):
        W._save_recipes(str(path), {})


def test_recipe_import_refuses_fifo_without_blocking(tmp_path):
    path = tmp_path / "pipe"
    os.mkfifo(path)
    with pytest.raises(ValueError, match="regular JSON"):
        W._load_recipes(str(path))


def test_recipe_worker_subprocess_roundtrip_and_conflict(tmp_path):
    data = {"r": W.recipe("r", "cat {path}")}
    path = str(tmp_path / "recipes.json")
    assert W._recipe_io_process("export", path, data) == {}
    assert W._recipe_io_process("import", path, {}) == data
    with pytest.raises(ValueError):
        W._recipe_io_process("export", path, data)


def test_recipe_worker_timeout_does_not_retry(monkeypatch):
    calls = []
    def stalled(*args, **kwargs):
        calls.append(kwargs)
        raise subprocess.TimeoutExpired("recipe", 5)
    monkeypatch.setattr(W.subprocess, "run", stalled)
    with pytest.raises(ValueError, match="timed out"):
        W._recipe_io_process("import", "/unavailable/recipes.json", {})
    assert len(calls) == 1 and calls[0]["timeout"] == 5


def test_fish_escaped_quote_cannot_hide_placeholder_inside_template_quote():
    with pytest.raises(ValueError, match="unquoted"):
        W.recipe("unsafe", "echo 'quoted\\' {value} '", shell="fish")


@pytest.mark.parametrize("shell", ["/bin/bash", "/usr/bin/zsh", "/bin/sh", "/usr/bin/fish"])
def test_actual_shell_parameter_insertion_is_literal(shell, tmp_path):
    if not Path(shell).exists():
        pytest.skip("Optional shell is not installed")
    value = "weird ' \\ `touch " + str(tmp_path / "PWNED") + "` $(id); 😀"
    command = W.expand_recipe(W.recipe("literal", "printf '%s' {text}", shell=shell), {"text": value})
    result = subprocess.run([shell, "-c", command], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0 and result.stdout == value
    assert not (tmp_path / "PWNED").exists()


@pytest.mark.parametrize("data", [{"schema": "bad", "recipes": []}, {"schema": W.RECIPE_SCHEMA, "recipes": [None]}, {"schema": W.RECIPE_SCHEMA, "recipes": [{}]}, {"schema": W.RECIPE_SCHEMA, "recipes": [W.recipe("x", "true")] * 2}])
def test_recipe_import_rejects_invalid_records(tmp_path, data):
    path = tmp_path / "recipes.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        W._load_recipes(str(path))


@pytest.mark.parametrize("name", ["gpu", "amd-gpu", "environment", "python", "allocation"])
def test_prepared_diagnostics_are_single_line_readonly_and_not_executed(name):
    command = W.diagnostic(name, "42_3")
    assert "\n" not in command
    assert not any(arg in command for arg in ["salloc", "sbatch", "scancel", "--apply"])


def test_trace_diagnostic_uses_existing_inspect_interface():
    assert shlex.split(W.diagnostic("trace", "/project/trace file.json")) == ["tower", "trace", "inspect", "/project/trace file.json"]


@pytest.fixture
def app(monkeypatch):
    from tower import terminal_ui
    terminal = SimpleNamespace(active="s1", sessions={"s1": {"metadata": {"shell": "bash", "host": "local"}}}, focus_generation=1)
    result = SimpleNamespace(selected_id="42", mode="main", tab="jobs", opened=[], previews=[], messages=[], errors=[], terminal=terminal)
    result.say = result.messages.append
    result.fail = result.errors.append
    def open_session(app, **spec):
        app.opened.append(spec)
        return "opened-" + str(len(app.opened))
    monkeypatch.setattr(terminal_ui, "initialize", lambda app: terminal)
    monkeypatch.setattr(terminal_ui, "open_session", open_session)
    monkeypatch.setattr(terminal_ui, "prepare_input", lambda app, text, label: app.previews.append((text, label)))
    monkeypatch.setattr(terminal_ui, "cancel_prepared", lambda app: app.previews.clear(), raising=False)
    return result


def test_command_collection_validates_every_host_before_open(app, binaries):
    assert W.run_command(app, ["terminal", "ssh-many", "valid", "--bad"])
    assert not app.opened and app.errors


def test_ssh_does_not_inherit_slurm_host(app, binaries):
    app.cfg = {"host": "other-cluster"}
    W.run_command(app, ["terminal", "ssh", "explicit-cluster"])
    assert app.opened[0]["metadata"]["host"] == "explicit-cluster"


def test_diagnostic_only_prepares_and_binds_known_shell(app):
    W.run_command(app, ["terminal", "prepare", "allocation", "42_2"])
    assert shlex.split(app.previews[0][0])[-1] == "42_2"
    assert "confirm" in app.previews[0][1]
    assert not app.opened


def test_unknown_shell_never_receives_prepared_input(app):
    app.terminal.sessions["s1"]["metadata"]["shell"] = "python"
    W.run_command(app, ["terminal", "prepare", "gpu"])
    assert not app.previews and "known shell" in app.errors[0]


def test_recipes_use_exact_shell_and_argument_context(app):
    W.run_command(app, ["terminal", "recipe", "save", "cat", "cat {path}"])
    W.run_command(app, ["terminal", "recipe", "use", "cat", "path=a; rm dangerous"])
    assert shlex.split(app.previews[0][0]) == ["cat", "a; rm dangerous"]
    app.terminal.sessions["s1"]["metadata"]["shell"] = "fish"
    W.run_command(app, ["terminal", "recipe", "use", "cat", "path=hello"])
    assert len(app.previews) == 1 and "differs" in app.errors[-1]


def test_interactive_recipe_io_never_touches_file_on_ui_thread(app, monkeypatch):
    captured = []
    app.interactive = True
    app.research = SimpleNamespace(start_task=lambda work, done: captured.append((work, done)) or True)
    def forbidden(*args):
        raise AssertionError("The UI called file I/O")
    monkeypatch.setattr(W, "_recipe_io_process", forbidden)
    W.run_command(app, ["terminal", "recipe", "import", "/slow/share/file.json"])
    assert len(captured) == 1 and W._state(app)["io_pending"] == "import"
    assert not app.errors


def test_background_recipe_import_refuses_conflicting_edit(app):
    captured = []
    app.interactive = True
    app.research = SimpleNamespace(start_task=lambda work, done: captured.append((work, done)) or True)
    W.run_command(app, ["terminal", "recipe", "import", "/slow/file.json"])
    W.run_command(app, ["terminal", "recipe", "save", "new", "echo new"])
    captured[0][1]({"imported": W.recipe("imported", "echo imported")})
    assert set(W._state(app)["recipes"]) == {"new"}
    assert W._state(app)["io_pending"] is None
    assert "changed" in app.errors[-1]


def test_busy_recipe_worker_is_visible_and_does_not_mutate_state(app):
    app.interactive = True
    app.research = SimpleNamespace(start_task=lambda work, done: False)
    W.run_command(app, ["terminal", "recipe", "export", "/slow/file.json"])
    assert "busy" in app.errors[-1]
    assert not W._state(app).get("io_pending")


def test_sequence_only_advances_explicitly_and_does_not_cross_sessions(app):
    W.run_command(app, ["terminal", "sequence", "start", "gpu", "environment"])
    assert not app.previews
    W.run_command(app, ["terminal", "sequence", "next"])
    assert len(app.previews) == 1 and "nvidia-smi" in app.previews[0][0]
    app.terminal.active = "s2"
    W.run_command(app, ["terminal", "sequence", "next"])
    assert len(app.previews) == 1 and "cancelled" in app.errors[-1]
    assert W._state(app)["sequence"] is None


def test_sequence_cancel_clears_preview(app):
    W.run_command(app, ["terminal", "sequence", "start", "python"])
    W.run_command(app, ["terminal", "sequence", "next"])
    W.run_command(app, ["terminal", "sequence", "cancel"])
    assert not app.previews and W._state(app)["sequence"] is None


def test_remote_job_directory_never_uses_coincident_local_path(app):
    app.files = SimpleNamespace(remote=True)
    with pytest.raises(ValueError, match="remote"):
        W._selected_directory(app, "42")


def test_missing_and_ambiguous_job_directory_is_not_inferred(app):
    app.store = SimpleNamespace(lock=threading.RLock(), jobs=[SimpleNamespace(id="42", workdir="/a")], finished=[SimpleNamespace(id="42", workdir="/b")])
    with pytest.raises(ValueError, match="unambiguous"):
        W._selected_directory(app, "42")


class Backend:
    def __init__(self):
        self.record = {"JobId": "42", "UserId": "alice(1000)", "JobState": "RUNNING", "SubmitTime": "2026-10-01T12:00:00", "StartTime": "2026-10-01T12:01:00", "Restarts": "0", "NodeList": "node[1-2]"}
        self.nodes = "node1\nnode2\n"
        self.steps = "StepId=42.0 State=RUNNING Tasks=4\n"
        self.calls = []

    def run(self, argv, timeout=8):
        self.calls.append(tuple(argv))
        if argv[:4] == ["scontrol", "show", "job", "-o"]:
            return " ".join(k + "=" + v for k, v in self.record.items()), .001
        if argv[:3] == ["scontrol", "show", "hostnames"]:
            return self.nodes, .001
        if argv[:4] == ["scontrol", "show", "step", "-o"]:
            return self.steps, .001
        raise AssertionError("Unexpected scheduler action: " + str(argv))


@pytest.fixture
def ctx(tmp_path):
    return O.Context(slurm=SimpleNamespace(b=Backend(), user="alice"), files=O.LocalFiles(), selected="42", scope={"host": "local", "user": "alice"}, state_dir=str(tmp_path))


def prepare(ctx, **params):
    return C.run("allocation-shell", {"job_id": "42", "mode": "shell", "shell": "/bin/bash", **params}, ctx)


def test_review_default_is_embedded_but_fullscreen_remains_explicit(ctx):
    assert prepare(ctx)["plan"]["payload"]["display"] == "embedded"
    assert prepare(ctx, display="fullscreen")["plan"]["payload"]["display"] == "fullscreen"
    assert not any(call[0] in {"srun", "sattach", "salloc"} for call in ctx.slurm.b.calls)


def test_exact_node_is_inside_reviewed_allocation_and_rechecked(ctx):
    value = prepare(ctx, node="node2")
    receipt = C.apply("allocation-shell", value["plan"], ctx)["data"]
    assert "--nodelist=node2" in receipt["terminal_argv"]
    assert receipt["terminal_argv"][-2:] == ["--pty", "/bin/bash"]
    ctx.slurm.b.nodes = "node1\n"
    with pytest.raises(ValueError, match="not in this allocation"):
        C.revalidate_terminal(ctx, receipt["terminal_identity"])


@pytest.mark.parametrize("params", [{"node": "node3"}, {"node": "node[1-2]"}, {"node": "--all"}, {"node": "node1", "mode": "attach", "step_id": "0"}, {"task": "0"}, {"display": "new-allocation"}])
def test_ambiguous_or_unsupported_allocation_targets_fail_before_launch(ctx, params):
    with pytest.raises(ValueError):
        prepare(ctx, **params)


def test_attachment_rank_is_exact_bounded_and_revalidated(ctx):
    value = prepare(ctx, mode="attach", step_id="0", task="3")
    receipt = C.apply("allocation-shell", value["plan"], ctx)["data"]
    assert receipt["terminal_argv"] == ["sattach", "--input-filter=3", "--output-filter=3", "42.0"]
    ctx.slurm.b.steps = "StepId=42.0 State=RUNNING Tasks=2\n"
    with pytest.raises(ValueError, match="rank"):
        C.revalidate_terminal(ctx, receipt["terminal_identity"])


@pytest.mark.parametrize("rank", ["-1", "4", "9999999999", "all", "0;id"])
def test_outside_or_unsafe_rank_is_rejected(ctx, rank):
    with pytest.raises(ValueError):
        prepare(ctx, mode="attach", step_id="0", task=rank)


def test_reviewed_session_retains_exact_attempt_and_local_host(ctx):
    receipt = C.apply("allocation-shell", prepare(ctx)["plan"], ctx)["data"]
    spec = W.reviewed_spec(receipt["terminal_argv"], receipt["terminal_identity"])
    assert spec.kind == "allocation-shell"
    assert spec.metadata["identity"]["StartTime"] == ctx.slurm.b.record["StartTime"]
    assert spec.metadata["destination"] == "allocation"
    with pytest.raises(ValueError, match="differs"):
        W.reviewed_spec(["srun", "--jobid=43"], receipt["terminal_identity"])


def test_attachment_status_reads_collected_data_without_scheduler_io(ctx):
    receipt = C.apply("allocation-shell", prepare(ctx)["plan"], ctx)["data"]
    meta = W.reviewed_spec(receipt["terminal_argv"], receipt["terminal_identity"]).metadata
    app = SimpleNamespace(store=SimpleNamespace(lock=threading.RLock(), jobs=[SimpleNamespace(id="42", start=ctx.slurm.b.record["StartTime"], submit=ctx.slurm.b.record["SubmitTime"], state="RUNNING", limit="01:00:00", elapsed="00:10:00")]))
    count = len(ctx.slurm.b.calls)
    assert "remaining 50m" in W.attachment_status(app, meta)
    app.store.jobs[0].start = "new-start"
    assert "attempt changed" in W.attachment_status(app, meta)
    assert len(ctx.slurm.b.calls) == count


def _reviewed_ui(operation_dashboard, monkeypatch, *, display="embedded"):
    app = operation_dashboard.app
    argv = ["srun", "--jobid=81", "--overlap", "--exact", "--nodes=1", "--ntasks=1", "--cpus-per-task=1", "--immediate=5", "--pty", "/bin/bash"]
    identity = {"job_id": "81", "allocation_id": "81", "identity": {"StartTime": "start", "SubmitTime": "submit", "UserId": "alice"}, "mode": "shell", "step_id": "", "argv": argv, "display": display}
    def run(feature, values, ctx):
        return O.report(feature, "Prepared", status="review", plan=O.prepare_plan(ctx, feature, values, identity))
    monkeypatch.setattr(O, "run", run)
    monkeypatch.setattr(O, "apply", lambda feature, plan, ctx: O.report(feature, "Ready", data={"terminal_argv": argv, "terminal_identity": deepcopy(identity)}))
    validations = []
    def validate(ctx, receipt):
        validations.append(receipt)
        return list(argv)
    monkeypatch.setattr(C, "revalidate_terminal", validate)
    app.run_command("ops run allocation-shell job_id=81")
    app.run_command("ops review")
    operation_dashboard.draw()
    return app, validations


def test_operations_apply_embeds_only_after_review_and_immediate_revalidation(operation_dashboard, monkeypatch):
    from tower import terminal_ui
    app, validations = _reviewed_ui(operation_dashboard, monkeypatch)
    launches = []
    monkeypatch.setattr(terminal_ui, "open_session", lambda app, **spec: launches.append(spec) or "sid-81")
    assert not launches and not validations
    app.run_command("ops apply")
    assert len(validations) == 1 and len(launches) == 1
    assert launches[0]["metadata"]["job_id"] == "81"
    assert app.ops_state["result"]["data"]["terminal_session"] == "sid-81"
    assert app.mode == "main" and ops_ui.take_terminal(app) is None
    app.run_command("ops apply")
    assert len(launches) == 1  # the applied plan cannot be replayed


def test_operations_explicit_fullscreen_preserves_existing_handoff(operation_dashboard, monkeypatch):
    from tower import terminal_ui
    app, validations = _reviewed_ui(operation_dashboard, monkeypatch, display="fullscreen")
    monkeypatch.setattr(terminal_ui, "open_session", lambda *a, **k: pytest.fail("Fullscreen cannot create an embedded session"))
    app.run_command("ops apply")
    assert len(validations) == 1
    assert ops_ui.take_terminal(app)[0] == "srun"
    assert ops_ui.take_terminal(app) is None


def test_operation_never_opens_after_identity_failure(operation_dashboard, monkeypatch):
    from tower import terminal_ui
    app, validations = _reviewed_ui(operation_dashboard, monkeypatch)
    def stale(*args):
        raise ValueError("Job attempt changed")
    monkeypatch.setattr(C, "revalidate_terminal", stale)
    monkeypatch.setattr(terminal_ui, "open_session", lambda *a, **k: pytest.fail("Stale attempt cannot open"))
    app.run_command("ops apply")
    assert app.ops_state["result"]["status"] == "error"
    assert "attempt changed" in app.ops_state["result"]["summary"]


@pytest.mark.parametrize("change", ["page", "tab", "connection", "expired", "quit"])
def test_worker_completion_cannot_open_after_context_change(operation_dashboard, monkeypatch, change):
    from tower import terminal_ui
    app, validations = _reviewed_ui(operation_dashboard, monkeypatch)
    completions = []
    original = ops_ui._background
    def delayed(app, worker, done, **kwargs):
        ctx = ops_ui.context(app)
        value = worker(ctx)
        completions.append((done, value))
    monkeypatch.setattr(ops_ui, "_background", delayed)
    monkeypatch.setattr(terminal_ui, "open_session", lambda *a, **k: pytest.fail("Stale completion cannot open"))
    app.run_command("ops apply")
    done, result = completions[0]
    if change == "page":
        app.mode = "main"
    elif change == "tab":
        app.tab = "research"
    elif change == "connection":
        monkeypatch.setattr(ops_ui, "_connection_scope", lambda app: {"host": "different"})
    elif change == "expired":
        result["data"]["terminal_checked_at"] -= 3
    else:
        app.ops_state["quit_requested"] = True
    done(result)
    assert app.ops_state["terminal"] is None
    assert "skipped" in result["warnings"][-1]


@pytest.fixture
def broadcast(app, monkeypatch):
    from tower import terminal_ui
    for sid in ("s1", "s2", "s3", "s4"):
        app.terminal.sessions[sid] = {"label": sid, "status": "running", "kind": "local", "metadata": {"shell": "bash", "host": "local"}}
    app.sent = []
    app.terminal.service = SimpleNamespace(send=lambda sid, event: app.sent.append((sid, event)) or True)
    def prepare(app, text, label):
        app.terminal.preview = {"text": text, "label": label, "session": app.terminal.active}
        app.terminal.focus_generation += 1
    monkeypatch.setattr(terminal_ui, "prepare_input", prepare)
    return app


def test_broadcast_is_armed_explicit_one_shot_no_enter(broadcast):
    preview = W.prepare_broadcast(broadcast, "uname -s", ["s1", "s2"])
    assert not broadcast.sent and len(preview["targets"]) == 2
    result = W.deliver_broadcast(broadcast, preview)
    assert "without Enter" in result
    assert broadcast.sent == [(sid, {"type": "paste", "text": "uname -s"}) for sid in ("s1", "s2")]
    with pytest.raises(ValueError, match="already attempted"):
        W.deliver_broadcast(broadcast, preview)


@pytest.mark.parametrize("change", ["active", "page", "generation", "status", "metadata", "kind"])
def test_broadcast_preflight_all_targets_before_any_input(broadcast, change):
    preview = W.prepare_broadcast(broadcast, "hostname", ["s1", "s2"])
    if change == "active":
        broadcast.terminal.active = "s2"
    elif change == "page":
        broadcast.tab = "history"
    elif change == "generation":
        broadcast.terminal.focus_generation += 1
    else:
        broadcast.terminal.sessions["s2"][change] = "changed"
    with pytest.raises(ValueError):
        W.deliver_broadcast(broadcast, preview)
    assert not broadcast.sent


def test_broadcast_partial_queue_failure_reports_receipt_and_never_retries(broadcast):
    preview = W.prepare_broadcast(broadcast, "hostname", ["s1", "s2"])
    def send(sid, event):
        if sid == "s2":
            return False
        broadcast.sent.append((sid, event))
        return True
    broadcast.terminal.service.send = send
    with pytest.raises(ValueError, match="queued to s1"):
        W.deliver_broadcast(broadcast, preview)
    with pytest.raises(ValueError, match="already attempted"):
        W.deliver_broadcast(broadcast, preview)
    assert len(broadcast.sent) == 1


@pytest.mark.parametrize("targets", [[], ["s1"], ["s1", "s1"], ["s1", "s2", "s3", "s4", "s5"], ["s1", "missing"]])
def test_broadcast_limits_and_existing_target_validation(broadcast, targets):
    with pytest.raises(ValueError):
        W.prepare_broadcast(broadcast, "hostname", targets)
    assert not broadcast.sent


def test_broadcast_never_accepts_multiline_or_unknown_repl(broadcast):
    with pytest.raises(ValueError, match="control"):
        W.prepare_broadcast(broadcast, "hostname\n", ["s1", "s2"])
    broadcast.terminal.sessions["s2"]["metadata"]["shell"] = "python"
    with pytest.raises(ValueError, match="known shell"):
        W.prepare_broadcast(broadcast, "hostname", ["s1", "s2"])


@pytest.mark.parametrize("allowed", [True, False])
def test_managed_allocation_terminal_requires_collection_admission_after_revalidation(operation_dashboard, monkeypatch, allowed):
    from tower import terminal_ui, raven_collection, raven_execution
    app, validations = _reviewed_ui(operation_dashboard, monkeypatch)
    app.raven_execution = raven_execution.ExecutionRouter({"managed": True})
    launches, admissions = [], []
    def admit(backend, argv):
        assert len(validations) == 1
        admissions.append((backend, argv))
        if not allowed:
            raise ValueError("Step launch budget exhausted")
        return {"family": "step", "scope": "user-host-connection"}
    monkeypatch.setattr(raven_collection, "admit_terminal_launch", admit)
    monkeypatch.setattr(terminal_ui, "open_session", lambda app, **spec: launches.append(spec) or "sid")
    app.run_command("ops apply")
    assert len(admissions) == 1
    assert len(launches) == int(allowed)
    if allowed:
        assert app.ops_state["result"]["data"]["terminal_admission"]["family"] == "step"
    else:
        assert "Step launch budget exhausted" in str(app.ops_state["result"])
