"""raven: parsers on recorded output, layout fitting, configuration, the store's transitions, the controller driven
through the simulated cluster (marks, confirmations, actions, filters, sorts, tabs, overlays, mouse), one-frame and
JSON output, and the curses screen through a pseudo-terminal."""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from raven import layout as L                                   # noqa: E402
from raven.actions import Actions                               # noqa: E402
from raven.config import Config, DEFAULTS                       # noqa: E402
from raven.controller import App                                # noqa: E402
from raven.model import Job, Store, compact, gres_gpus, hms, secs   # noqa: E402
from raven.sampler import Sampler                               # noqa: E402
from raven.slurm import FakeBackend, Slurm, parse_gpu_inventory, parse_jobs, parse_nvsmi, parse_sacct   # noqa: E402
from raven.views import Views                                   # noqa: E402
from raven import screen                                        # noqa: E402

DASH = str(ROOT / "raven")


# ------------------------------------------------------------------------------------------------ parsers
def test_parsers_on_recorded_output():
    jobs = parse_jobs("12477369|rb-setup|gpu|RUNNING|17:21|1:30:00|1|8|gres/gpu:a100:1|a01-05|32G|2026-09-30T13:46:01|2026-09-30T13:30:00|None|12000|(null)|lab_01|normal|2026-09-30T15:16:01|/home1/x/rb_setup.sbatch\n"
                      "12480003|rb2-controls|gpu|PENDING|0:00|23:00:00|2|8|gres/gpu:a100:2|(Dependency)|32G|N/A|2026-09-30T14:00:00|Dependency|10100|afterok:12480001|lab_01|normal|N/A|/home1/x/rb2.sbatch\n")
    assert [j.id for j in jobs] == ["12477369", "12480003"]
    j = jobs[0]
    assert (j.cpus, j.gpus, j.gpu_type, j.nodelist, j.mem_bytes, j.pending, j.elapsed_s, j.limit_s) == (8, 1, "a100", "a01-05", 32 * 1024 ** 3, False, 1041, 5400)
    p = jobs[1]
    assert p.pending and p.gpus == 4 and p.nodelist == "" and p.dependency == "afterok:12480001" and p.priority == 10100 and p.reason == "Dependency"
    fin = parse_sacct("1|rb-gpucheck|COMPLETED|00:03:12|4|00:01:30|16G||2026-09-30T12:06:48|2026-09-30T12:10:00|gpu|1|0:0|cpu=4,mem=16G,node=1,gres/gpu=1|a01-05\n"
                      "1.batch|batch|COMPLETED|00:03:12|4|00:01:30||1.1G|2026-09-30T12:06:48|2026-09-30T12:10:00|gpu|1|0:0||a01-05\n"
                      "2|rb-x|CANCELLED by 1000|00:10:00|8|00:00:00|4000Mc||2026-09-30T12:10:00|2026-09-30T12:20:00|main|1|0:0|cpu=8|e01\n"
                      "3|running|RUNNING|00:01:00|1|00:00:00|1G||2026-09-30T12:00:00|Unknown|main|1|0:0||e02\n")
    assert [f.id for f in fin] == ["2", "1"] and fin[1].gpus == 1 and abs(fin[1].cpu_eff - 90 / (192 * 4)) < 1e-9 and abs(fin[1].mem_eff - 1.1 / 16) < 1e-6
    assert fin[0].state == "CANCELLED" and fin[0].req_mem == 8 * 4000 * 1024 ** 2 and fin[0].core_hours == pytest.approx(8 / 6)
    tot, per = parse_gpu_inventory("a01-05 gpu gpu:a100:2(S:0-1) gpu:a100:2(IDX:0-1) mix\na01-05 debug gpu:a100:2(S:0-1) gpu:a100:2(IDX:0-1) mix\n"
                                   "a01-06 gpu gpu:a100:2(S:0-1) gpu:a100:0(IDX:N/A) drain\nb01-01 gpu gpu:a40:4(S:0-1) gpu:a40:3(IDX:0-2) mix\n")
    assert tot == {"a100": dict(total=4, used=2, down=2, free=0), "a40": dict(total=4, used=3, down=0, free=1)}          # a01-05 counted once
    assert per["debug"]["a100"]["total"] == 2 and per["gpu"]["a100"]["total"] == 4
    s = parse_nvsmi("1: 0, 71, 12390, 40960, NVIDIA A100-SXM4-40GB\n1: 1, 3, 500, 40960, NVIDIA A100-SXM4-40GB\n", labelled=True)
    assert [(x.node, x.index, x.util) for x in s] == [("task1", 0, 71.0), ("task1", 1, 3.0)]
    assert gres_gpus("gres/gpu:a100:2(S:0-1)") == ("a100", 2) and gres_gpus("gres:gpu:1") == ("", 1) and secs("1-02:03:04") == 93784 and hms(93784) == "1-02:03:04"
    assert compact(45) == "45s" and compact(725) == "12m" and compact(3 * 3600 + 240) == "3h04m" and compact(90000) == "1d01h"


# ------------------------------------------------------------------------------------------------ layout and config
def test_layout_fits_drops_and_clips(monkeypatch):
    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.delenv("NO_COLOR", raising=False)
    cols = [L.Column("a", "A", 2, 10), L.Column("b", "B", 4, 40, flex=True), L.Column("c", "C", 3, 5, ">")]
    rows = [dict(a="xx", b="a very long job name indeed", c="1"), dict(a="y", b="short", c="12345")]
    w, kept = L.fit_columns(cols, rows, 100)
    assert w == {"a": 2, "b": 27, "c": 5} and len(kept) == 3
    w, kept = L.fit_columns(cols, rows, 20)
    assert w == {"a": 2, "b": 9, "c": 5} and sum(w.values()) + 4 == 20
    w, kept = L.fit_columns(cols, rows, 12, droppable=("c",))
    assert [c.key for c in kept] == ["a", "b"] and w == {"a": 2, "b": 8}
    w, kept = L.fit_columns(cols, rows, 5, droppable=("c",), floor=2)
    assert [c.key for c in kept] == ["a", "b"] and w == {"a": 2, "b": 2}
    assert L.cut("abcdefgh", 5) == "abcd…" and L.cut("abcdefgh", 5, ascii_=True) == "abcd~" and L.cut("日本語テキスト", 7) == "日本語…" and L.vlen("日本") == 4
    rows_t, kept = L.table(cols, rows, 40, cursor=1, marks={0}, mark_char="*")
    assert L.row_text(rows_t[0]).split() == ["A", "B", "C"] and L.row_text(rows_t[1]).startswith(" *  xx") and all(s == "rev" for _, s in rows_t[2])
    g = L.Glyphs(True)
    assert L.bar(g, 0.5, 10) == ("#####-----", "green") and L.bar(g, None, 4)[1] == "dim" and L.spark(g, [0, 0.5, 1.0], 3) == ".=@"
    text = L.to_text([[("ab", "bold"), ("cd", "")]], 3, color=True)
    assert text == "\x1b[1mab\x1b[0mc"


def test_config_merges_files_and_maps_keys(tmp_path):
    cfg = Config()
    assert cfg["intervals"]["jobs"] == 2.0 and cfg.keymap()["c"] == "cancel" and cfg.keymap()["down"] == "down" and cfg.get("thresholds.cpu") == 0.3
    toml = sys.version_info >= (3, 11)
    p = tmp_path / ("c.toml" if toml else "settings.json")
    p.write_text('log_lines = 20\n[intervals]\njobs = 5.0\n[keys]\ncancel = ["X"]\n' if toml else
                 json.dumps({"log_lines": 20, "intervals": {"jobs": 5.0}, "keys": {"cancel": ["X"]}}))
    cfg = Config.load(str(p))
    assert cfg["log_lines"] == 20 and cfg["intervals"]["jobs"] == 5.0 and cfg["intervals"]["gpu"] == 5.0 and cfg.keymap()["X"] == "cancel" and "c" not in cfg.keymap()
    j = tmp_path / "c.json"
    j.write_text(json.dumps({"history_days": 7}))
    assert Config.load(str(j))["history_days"] == 7
    out = Config.write_default(str(tmp_path / "d" / ("config.toml" if toml else "config.json")))
    assert Config.load(out)["intervals"]["jobs"] == DEFAULTS["intervals"]["jobs"]
    with pytest.raises(FileNotFoundError):
        Config.load(str(tmp_path / "missing.toml"))


# ------------------------------------------------------------------------------------------------ the store
def test_store_transitions_and_events(tmp_path):
    st = Store(state_dir=str(tmp_path / "state"))
    a = Job(id="1", name="a", partition="p", state="PENDING", reason="Priority")
    b = Job(id="2", name="b", partition="p", state="RUNNING")
    assert st.apply_jobs([a, b]) == []                               # the first listing is not a transition
    a2 = Job(id="1", name="a", partition="p", state="RUNNING")
    c = Job(id="3", name="c", partition="p", state="PENDING", reason="JobHeldUser")
    ev = st.apply_jobs([a2, c])
    assert [e["kind"] for e in ev] == ["started", "queued", "finished"] and ev[2]["job"] == "2"
    ev = st.apply_jobs([a2, Job(id="3", name="c", partition="p", state="PENDING", reason="Priority")])
    assert [e["kind"] for e in ev] == ["released"]
    ev = st.apply_jobs([a2])
    assert [e["kind"] for e in ev] == ["left"]
    st2 = Store(state_dir=str(tmp_path / "state"))                    # events persist across restarts
    assert [e["text"] for e in st2.events][-1] == "left queue 3 c" and st2.events[-1]["old"]
    st.save_ui(dict(tab="history"))
    assert st2.load_ui()["tab"] == "history"


# ------------------------------------------------------------------------------------------------ the controller on the simulated cluster
def make_app(tmp_path, t0=None):
    cfg = Config()
    backend = FakeBackend("alex", t0=t0)
    slurm = Slurm(backend, "alex")
    store = Store(state_dir=None, persist=False)
    sampler = Sampler(slurm, store, cfg["intervals"], cfg["gpu_types"], account="lab_01")
    actions = Actions(slurm, store)
    views = Views(L.Glyphs(True), cfg)
    app = App(store, sampler, actions, cfg, "alex", ascii_=True)
    sampler.round(wait=True)
    return backend, store, sampler, actions, views, app


def complete_action(app):
    """Drive the real worker completion as the terminal loop would."""
    deadline = time.monotonic() + 5
    hub = getattr(app, "research", None)
    while hub is not None and hub.pending and time.monotonic() < deadline:
        hub.poll_task()
        time.sleep(.005)
    assert hub is None or hub.pending is None, "scheduler action did not finish"


def without_resubmit_marker(text):
    return re.sub(r" --comment=raven-resubmit-[0-9a-f]{32}", "", text)


def test_controller_marks_confirms_and_acts(tmp_path):
    backend, store, sampler, actions, views, app = make_app(tmp_path)
    W, H = 160, 45
    rows, hits = views.compose(store.snapshot(), app, W, H, actions)
    assert app.visible_ids[:3] == ["12480002_[0-7]", "12480003", "12480005"] and app.selected_id == "12480002_[0-7]" and len(rows) == H
    # Mark exact running and pending jobs independently of their batch ranks.
    for jid in ("12477369", "12480002_[0-7]"):
        app.cursor["jobs"] = app.visible_ids.index(jid)
        app.sync_selection()
        app.handle("space")
    assert app.marks == {"12477369", "12480002_[0-7]"}
    app.handle("c")
    assert app.mode == "confirm" and [j.id for j in app.confirm["jobs"]] == ["12480002_[0-7]", "12477369"]
    ov = views.overlay(store.snapshot(), app, W, H)
    assert any("Cancel 2 jobs?" in L.row_text(r) for _, _, r in ov)
    app.handle("n")
    assert app.mode == "main" and app.message == "kept" and not any(c[0] == "scancel" for c in backend.calls)
    app.handle("c"); app.handle("y")
    complete_action(app)
    assert ["scancel", "12480002_[0-7]", "12477369"] in backend.calls and app.marks == set() and app.message.startswith("cancel 12480002_[0-7], 12477369: sent")
    assert [e for e in store.events if e["kind"] == "action"][-1]["ok"]
    sampler.round(time.time() + 100, wait=True)
    assert {j.id for j in store.jobs} == {"12480001", "12480003", "12480005"}
    assert [e["kind"] for e in store.events][-2:] == ["finished", "left"]
    # hold the selected pending job, then release it
    views.compose(store.snapshot(), app, W, H, actions)
    app.cursor["jobs"] = app.visible_ids.index("12480003")
    views.compose(store.snapshot(), app, W, H, actions)
    app.handle("h"); app.handle("y")
    complete_action(app)
    assert ["scontrol", "hold", "12480003"] in backend.calls
    sampler.round(time.time() + 200, wait=True)
    views.compose(store.snapshot(), app, W, H, actions)
    assert store.job("12480003").held and [e["kind"] for e in store.events][-1] == "held"
    app.handle("h")
    assert app.confirm["action"] == "release"
    app.handle("y")
    complete_action(app)
    assert ["scontrol", "release", "12480003"] in backend.calls
    # hold on a running job is refused with a reason, top on a running job too
    app.cursor["jobs"] = app.visible_ids.index("12480001")
    views.compose(store.snapshot(), app, W, H, actions)
    app.handle("h")
    assert app.mode == "main" and app.message == "hold applies to pending jobs"
    app.handle("t")
    assert app.mode == "main" and app.message == "top applies to pending jobs"
    app.handle("R"); app.handle("y")
    complete_action(app)
    assert ["scontrol", "requeue", "12480001"] in backend.calls


def test_controller_filter_sort_tabs_overlays_and_mouse(tmp_path):
    backend, store, sampler, actions, views, app = make_app(tmp_path)
    W, H = 160, 45
    app.handle("/")
    for ch in "rb2":
        app.handle(ch)
    app.handle("enter")
    views.compose(store.snapshot(), app, W, H, actions)
    assert app.filter == "rb2" and app.visible_ids == ["12480003"]
    app.handle("esc")
    views.compose(store.snapshot(), app, W, H, actions)
    assert app.filter == "" and len(app.visible_ids) == 5
    app.handle("s")
    assert app.sort["jobs"] == "name"
    views.compose(store.snapshot(), app, W, H, actions)
    # Names order equal-ranked batches; pending batches still precede running.
    assert app.visible_ids == ["12480002_[0-7]", "12480003", "12480005", "12477369", "12480001"]
    app.handle("S")
    views.compose(store.snapshot(), app, W, H, actions)
    assert app.visible_ids == ["12480005", "12480003", "12480002_[0-7]", "12480001", "12477369"]
    app.handle("s"); app.handle("S")
    app.handle("enter")
    assert app.mode == "details" and app.detail_id == app.selected_id
    sampler.select(app.selected_id); sampler.round(wait=True)
    ov = views.overlay(store.snapshot(), app, W, H)
    text = "\n".join(L.row_text(r) for _, _, r in ov)
    assert "JobState" in text and "StdOut" in text
    app.handle("esc")
    app.handle("?")
    ov = views.overlay(store.snapshot(), app, W, H)
    assert any("keys" in L.row_text(r) for _, _, r in ov)
    app.handle("q")
    assert app.mode == "main" and not app.quit
    for key, tab in (("2", "cluster"), ("3", "history"), ("4", "nodes"), ("5", "log"), ("6", "sources"), ("tab", "research"), ("tab", "traces"), ("tab", "jobs"), ("btab", "traces"), ("btab", "research"), ("btab", "sources")):
        app.handle(key)
        assert app.tab == tab
        rows, hits = views.compose(store.snapshot(), app, W, H, actions)
        assert len(rows) == H and all(L.vlen(L.row_text(r)) <= W for r in rows)
    app.handle("1")
    rows, hits = views.compose(store.snapshot(), app, W, H, actions)
    y = [h for h in hits if h[2] == "12480005"][0][0]
    app.click(y, app.workspace_main_rect.x + 5, hits)
    assert app.cursor["jobs"] == app.visible_ids.index("12480005")
    tab_y, left, _, _ = next(hit for hit in app.tab_hits if hit[3] == "history")
    app.click(tab_y, left + 1, hits)                                   # the painted tab bar
    assert app.tab == "history"
    app.handle("1"); app.handle("l")
    assert app.tab == "log" and app.log_job == "12480005"
    app.handle("x")                                                     # only meaningful on the sources tab
    app.handle("6"); app.handle("home"); app.handle("x")
    assert store.health["account"].enabled is False
    app.handle("q")
    assert app.quit


def test_flags_thresholds_and_once_outputs(tmp_path, monkeypatch):
    # This assertion checks an exact displayed percentage. Keep the simulated
    # GPU at its initial observation while worker scheduling varies under load.
    monkeypatch.setattr(FakeBackend, "now", lambda self: 0.0)
    backend, store, sampler, actions, views, app = make_app(tmp_path)
    text = screen.once_text(app, views, store, actions, 180, False, tab="jobs")
    row = next(l for l in text.splitlines() if "12477369" in l)
    assert "!cpu" in row and "!mem" in row and " 95 " in row                 # a GPU job at 30 % cpu, 19 % memory after 17 min
    row = next(l for l in text.splitlines() if "12480003" in l)
    assert "dep" in row and "Dependency" in row
    head = next(l for l in text.splitlines() if l.lstrip().startswith("PROG"))
    assert head.split() == ["PROG", "JOBID", "NAME", "PART", "ST", "^", "NODES", "CPU", "GPU", "ELAPSED/LIMIT", "LEFT/WAIT", "CPU%", "EFF", "MEM%", "GPU%", "FLAGS", "TAGS", "INFO"]
    narrow = screen.once_text(app, views, store, actions, 100, False, tab="jobs")
    head = next(l for l in narrow.splitlines() if l.lstrip().startswith("PROG"))
    assert "FLAGS" not in head and "INFO" in head and all(L.vlen(l) <= 100 for l in narrow.splitlines())
    hist = screen.once_text(app, views, store, actions, 160, False, tab="history")
    assert "core-hours" in hist and "OUT_OF_MEMORY" in hist and "0:125" in hist
    cl = screen.once_text(app, views, store, actions, 160, False, tab="cluster")
    assert "a40 1/4 (4 down)" in cl and "fair share 0.421000" in cl and "lab_01 right now" in cl
    js = json.loads(screen.once_json(store))
    assert {j["id"] for j in js["jobs"]} >= {"12480001", "12477369"} and js["health"]["jobs"]["calls"] >= 1 and "finished" in js


# ------------------------------------------------------------------------------------------------ the processes
def test_cli_once_json_and_watch_from_the_simulated_cluster():
    import selectors
    import signal

    env = dict(os.environ, COLUMNS="150", USER="alex")
    r = subprocess.run([sys.executable, DASH, "--user", "alex", "--fake", "--once", "--ascii", "--no-color", "--no-state", "--width", "150"], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "Raven Light/slurm - alex" in r.stdout and "running 2 (40 cpus, 1 gpus)" in r.stdout and "-- selected" in r.stdout
    assert all(label in r.stdout.splitlines()[3] for label in ("Jobs", "History", "Analytics", "Research", "Log"))
    assert "[Launch]" in r.stdout.splitlines()[0] and "Queue:" in r.stdout.splitlines()[0]
    r = subprocess.run([sys.executable, DASH, "--user", "alex", "--fake", "--json", "--no-state"], env=env, capture_output=True, text=True)
    assert r.returncode == 0 and json.loads(r.stdout)["account"]["account"] == "lab_01"
    output = {"stdout": bytearray(), "stderr": bytearray()}
    title = b"Raven Light/slurm - alex"
    with selectors.DefaultSelector() as ready:
        process = subprocess.Popen(
            [sys.executable, DASH, "--user", "alex", "--fake", "--watch", "--ascii", "--no-state", "--interval", "0.5"],
            env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            bufsize=0, start_new_session=True,
        )
        try:
            ready.register(process.stdout, selectors.EVENT_READ, "stdout")
            ready.register(process.stderr, selectors.EVENT_READ, "stderr")

            def receive_until(predicate, deadline, label):
                while not predicate():
                    remaining = deadline - time.monotonic()
                    assert remaining > 0 and ready.get_map(), (label, process.poll(), output)
                    for key, _ in ready.select(remaining):
                        data = os.read(key.fd, 65536)
                        if not data:
                            ready.unregister(key.fileobj)
                            continue
                        output[key.data].extend(data)
                        assert sum(map(len, output.values())) <= 512 * 1024, "Watch output exceeded fixture bound"

            # Startup/import time is separate from repaint cadence. A pipe may
            # coalesce frames, so exact sleep values are checked in test_palette.
            receive_until(lambda: title in output["stdout"], time.monotonic() + 10, "First watch frame")
            receive_until(lambda: output["stdout"].count(title) >= 2, time.monotonic() + 3, "Second watch frame")
            os.killpg(process.pid, signal.SIGINT)
            shutdown = time.monotonic() + 5
            receive_until(lambda: not ready.get_map(), shutdown, "Watch shutdown")
            process.wait(timeout=max(0, shutdown - time.monotonic()))
            assert process.returncode == 0, output
            assert output["stdout"].count(title) >= 2 and b"\x1b" not in output["stdout"] and b"Traceback" not in output["stderr"]
        finally:
            if process.poll() is None:
                for signum in (signal.SIGTERM, signal.SIGKILL):
                    try:
                        os.killpg(process.pid, signum)
                    except ProcessLookupError:
                        pass
                    try:
                        process.wait(timeout=2)
                        break
                    except subprocess.TimeoutExpired:
                        continue
                assert process.poll() is not None, "Owned watch process did not exit after SIGKILL"
            process.stdout.close()
            process.stderr.close()


@pytest.mark.skipif(os.name != "posix", reason="needs a POSIX pseudo-terminal")
def test_curses_screen_through_a_pseudo_terminal(tmp_path):
    import codecs
    import select
    import signal
    import struct

    pty = pytest.importorskip("pty")
    fcntl = pytest.importorskip("fcntl")
    termios = pytest.importorskip("termios")
    pyte = pytest.importorskip("pyte")
    config = tmp_path / "config.json"
    # Check the exported payload independently of the host's clipboard tools.
    config.write_text(json.dumps({"clipboard": {"osc52": False, "tools": False}}))
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 45, 150, 0, 0))
    env = dict(os.environ, COLUMNS="150", LINES="45", TERM="xterm-256color", USER="alex",
               XDG_CACHE_HOME=str(tmp_path / "cache"), RAVEN_EXPORT_DIR=str(tmp_path / "durable-exports"))
    process = subprocess.Popen(
        [sys.executable, DASH, "--fake", "--no-state", "--ascii", "--interval", "0.5",
         "--config", str(config)],
        env=env, cwd=tmp_path, stdin=slave, stdout=slave, stderr=slave,
        start_new_session=True,
    )
    os.close(slave)
    display = pyte.Screen(150, 45)
    stream = pyte.Stream(display)
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    output = bytearray()
    deadline = time.monotonic() + 30

    def rendered():
        return "\n".join(display.display)

    def until(predicate, label, *, allow_exit=False):
        while time.monotonic() < deadline:
            if predicate():
                return
            if not allow_exit:
                assert process.poll() is None, (label, process.returncode, rendered(), output[-3000:])
            if select.select([master], [], [], .02)[0]:
                try:
                    data = os.read(master, 65536)
                except OSError:
                    continue
                output.extend(data)
                del output[:-512 * 1024]
                stream.feed(decoder.decode(data))
        pytest.fail(label + " timed out\n" + rendered() + "\n" + repr(output[-3000:]))

    try:
        # A page transition must be painted before its next action. Fixed
        # sleeps did not establish that v/j/y followed the Cluster frame under
        # suite load; stripping ANSI also loses cursor-addressed text.
        until(lambda: "12477369" in rendered(), "Initial populated Jobs frame")
        os.write(master, b"j c")
        until(lambda: "Cancel 1 job?" in rendered(), "Cancellation confirmation")
        assert "12477369" in rendered()
        os.write(master, b"y")
        until(lambda: "CANCELLING" in rendered(), "Cancellation feedback")
        os.write(master, b"?")
        until(lambda: "keys" in rendered(), "Keys help")
        os.write(master, b"\x1b")
        until(lambda: "Cancel 1 job?" not in rendered() and "keys" not in rendered(), "Help closed")
        os.write(master, b"2")
        until(lambda: "partitions" in rendered(), "Cluster frame")
        os.write(master, b"v")
        until(lambda: "Select rendered lines" in rendered(), "Rendered-line selection")
        os.write(master, b"jy")
        copied = tmp_path / "durable-exports" / "clipboard.txt"
        until(lambda: copied.exists() and "copied 2 lines" in rendered(), "Two-line copy receipt")
        assert copied.read_text().count("\n") == 2
        os.write(master, b":tab history\r")
        until(lambda: "core-hours" in rendered().lower(), "History summary")
        os.write(master, b"7")
        until(lambda: "job series" in rendered(), "Analytics frame")
        os.write(master, b"q")
        until(lambda: process.poll() is not None, "Normal shutdown", allow_exit=True)
        assert process.returncode == 0, output[-3000:]
        assert b"Traceback" not in output
    finally:
        if process.poll() is None:
            try:
                os.write(master, b"q")
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=2)
        os.close(master)


# ------------------------------------------------------------------------------------------------ charts, selection, exports, palette, analytics
def test_charts_resample_bars_histogram_and_gantt():
    from raven import charts
    g = L.Glyphs(True)
    assert charts.resample([1, 2, 3, 4], 2) == [1.5, 3.5] and charts.resample([1, None, 3], 3) == [1, None, 3] and charts.resample([5], 3) == [None, None, 5]
    rows = charts.vbar_chart(g, [0, 25, 50, 75, 100], 40, 4, hi=100, unit="%", title="t", times=(0, 7200))
    assert len(rows) == 1 + 4 + 1 + 1 and "100%" in L.row_text(rows[1]) and "0%" in L.row_text(rows[4]) and all(L.vlen(L.row_text(r)) <= 40 for r in rows)
    body = [L.row_text(r)[10:] for r in rows[1:5]]
    assert body[3].rstrip().endswith("#") and body[0].rstrip().endswith("#") and body[0].strip() == "#"      # the 100 column reaches the top row only
    short = charts.time_axis(1000.0, 1030.0, 30)
    assert L.row_text(short).strip().count(":") == 2                                                            # one HH:MM:SS label for a short span
    h = charts.histogram(g, [1, 2, 7, 20, 20, 200], [0, 5, 10, 100], 60)
    assert [L.row_text(r).split()[0] for r in h] == ["0-5", "5-10", "10-100", ">="] and L.row_text(h[0]).rstrip().endswith("2") and L.row_text(h[3]).rstrip().endswith("1")
    rows = charts.hbar_rows(g, [("a", 2.0, "cyan"), ("b", 1.0, "")], 50, unit="h")
    assert abs(L.row_text(rows[0]).count("#") - 2 * L.row_text(rows[1]).count("#")) <= 1 and all(L.vlen(L.row_text(r)) <= 50 for r in rows)
    gl = charts.gantt(g, [dict(name="x", id="1", state="COMPLETED", submit=0, start=100, end=200), dict(name="y", id="2", state="PENDING", submit=150, start=None, end=None)], 0, 300, 70)
    assert len(gl) == 3 and all(L.vlen(L.row_text(r)) <= 70 for r in gl) and "#" in L.row_text(gl[1]) and "-" in L.row_text(gl[2]) and "completed" in L.row_text(gl[1])


def test_series_persist_and_reload(tmp_path):
    from raven.model import Live
    st = Store(state_dir=str(tmp_path / "s"))
    binding = dict(run_root="/project", run_id="run", attempt="a1", job_id="7", submission_token="token", cluster="fixture")
    st.bind_series_attempt("7", binding)
    st.apply_live("7", Live(cpu_time=10.0, rss=1e9, avg=0.5, rate=0.6, t=1000.0))
    st.apply_live("7", Live(cpu_time=20.0, rss=2e9, avg=0.55, rate=0.7, t=1010.0))
    st.apply_gpu("7", [__import__("raven.model", fromlist=["GpuSample"]).GpuSample(node="n", index=0, util=80.0, used=100.0, total=1000.0)])
    assert len(st.series_of("7")) == 3 and Path(st._series_path("7")).exists()
    st.close()
    st2 = Store(state_dir=str(tmp_path / "s"))                          # a verified new session sees the file
    st2.bind_series_attempt("7", binding)
    s = st2.series_of("7")
    assert [x["k"] for x in s] == ["live", "live", "gpu"] and s[1]["cpu"] == 0.7 and s[2]["gpu"]["n:0"][0] == 80.0 and st2.series_jobs() == ["7"]
    st2.apply_live("7", Live(cpu_time=30.0, rss=3e9, avg=0.6, rate=0.8, t=1020.0))
    assert len(st2.series_of("7")) == 4


def make_rich_app(tmp_path, rounds=4):
    """The simulated cluster after several sampling rounds, with a state directory for exports and clipboard files."""
    cfg = Config({"clipboard": {"osc52": False, "tools": False}})
    backend = FakeBackend("alex", speed=30.0)
    slurm = Slurm(backend, "alex")
    store = Store(state_dir=str(tmp_path / "state"))
    sampler = Sampler(slurm, store, cfg["intervals"], cfg["gpu_types"], account="lab_01")
    actions = Actions(slurm, store)
    views = Views(L.Glyphs(True), cfg)
    app = App(store, sampler, actions, cfg, "alex", ascii_=True)
    app.views_ref = views
    now = time.time()
    for k in range(rounds):
        sampler.last_run["live"] = sampler.last_run["gpu"] = 0.0
        sampler.round(now + 12 * k, wait=True)
    return backend, store, sampler, actions, views, app


def test_selection_copy_and_exports(tmp_path, monkeypatch):
    monkeypatch.setenv("RAVEN_EXPORT_DIR", str(tmp_path / "durable-exports"))
    from raven import text_selection

    backend, store, sampler, actions, views, app = make_rich_app(tmp_path)
    # Keep a sampled running job first for the copy/export fixture. Its four
    # selected Main rows must exclude the adjacent running-job Details pane.
    app.run_command("grouping sort-max")
    W, H = 150, 44
    cache = screen._FrameCache()
    cache.rebuild(app, views, store, actions, W, H)                    # publish the same pane ownership as the real UI
    hits = cache.hits
    y0 = next(y for y, kind, _ in hits if kind == "job")
    app.click(y0, app.workspace_main_rect.x + 3, hits)                                             # ordinary clicks select jobs
    cache.rebuild(app, views, store, actions, W, H)                    # refresh Details and the selection context
    hits = cache.hits
    pane = next(pane for pane in text_selection.initialize(app)["panes"] if pane.key == "jobs")
    assert pane.rect.top == y0 and pane.rect.bottom > y0 + 3
    # ASCII test rows let us independently slice terminal columns. The copied
    # bytes must contain exactly these four Main rows, excluding adjacent Details.
    expected = "".join(L.row_text(cache.rows[y])[pane.rect.left:pane.rect.right].rstrip() + "\n"
                       for y in range(y0, y0 + 4))
    assert "rb1-allrank" in "\n".join(L.row_text(cache.rows[y])[pane.rect.right:] for y in range(y0, y0 + 4))
    app.handle("v")                                                  # explicit text mode owns Shift-click instead of job marking
    app.click(y0 + 2, app.workspace_main_rect.x + 3, hits, shift=True)
    selection = text_selection.initialize(app)["selection"]
    assert selection["key"] == "jobs" and (selection["anchor"], selection["end"]) == (0, 2)
    assert app.sel_anchor is None                                    # a pane range must not become a screen-wide range
    feedback = text_selection.feedback(app, ascii_=True)
    assert [y for y, _, _ in feedback] == list(range(y0, y0 + 3))
    assert all(x == pane.rect.left and L.vlen(L.row_text(row)) <= pane.rect.right - x
               and row[-1] == (">", "warning+bold+bg:surface-raised") for _, x, row in feedback)
    app.handle("down")                                                 # arrows extend the selection
    assert text_selection.initialize(app)["selection"]["end"] == 3
    app.handle("y")
    for future in app.clipboard_pending: future.result(timeout=3)
    from raven.clipboard import poll
    poll(app)
    text = (tmp_path / "durable-exports" / "clipboard.txt").read_text()
    assert text == expected and "rb1-allrank" not in text
    assert text.count("\n") == 4 and "12480001" in text and "12477369" in text and app.message.startswith("copied 4 lines")
    assert text_selection.initialize(app)["selection"] is None and app.sel_anchor is None
    app.handle("v"); app.handle("V")
    selection = text_selection.initialize(app)["selection"]
    assert selection["key"] == "jobs"
    assert (selection["anchor"], selection["end"]) == (pane.top, pane.top + pane.page - 1)
    app.handle("esc")
    assert text_selection.initialize(app)["selection"] is None and app.sel_anchor is None
    app.handle("E"); path_t = app.message.split()[-1]
    app.handle("C"); path_c = app.message.split()[-1]
    app.handle("space"); app.handle("J"); path_j = app.message.split()[-1]
    exported = Path(path_t).read_text()
    assert exported.splitlines()[0].split()[:8] == ["x", "File", "Edit", "View", "Setup", "Help", "Themes", "[Launch]"]
    assert exported.splitlines()[1].startswith(" Raven Light/unconfigured - alex") and "-- selected" in exported
    assert Path(path_c).read_text().splitlines()[0].startswith("id,name,part") and "12477369" in Path(path_c).read_text()
    js = json.loads(Path(path_j).read_text())
    assert js[0]["id"] == "12480001" and js[0]["job"]["name"] == "rb1-allrank" and len(js[0]["series"]) >= 3 and "live" in js[0]
    app.handle("u"); app.handle("3"); app.handle("J")                    # no marks: the history cursor's job
    assert json.loads(Path(app.message.split()[-1]).read_text())[0]["finished"]["name"]
    app.handle("5"); app.handle("C")
    assert app.message.startswith("no table to export")
    kinds = [e["kind"] for e in store.events]
    assert kinds.count("export") == 4 and "copy" in kinds


def test_command_palette_and_analytics_views(tmp_path):
    backend, store, sampler, actions, views, app = make_rich_app(tmp_path)
    # Rapid fake sampling rounds can share one rounded wall timestamp. This
    # test exercises interactive time bounds, so supply a deterministic span.
    for series in store.series.values():
        first = time.time() - 12 * (len(series) - 1)
        for index, point in enumerate(series):
            point["t"] = first + 12 * index
    W, H = 150, 44
    views.compose(store.snapshot(), app, W, H, actions)

    def cmd(line):
        app.handle(":")
        for ch in line:
            app.handle(ch if ch != " " else "space")
        app.handle("enter")

    app.handle(":"); assert app.mode == "palette" and "commands:" in app.palette_hint()
    for ch in "canc":
        app.handle(ch)
    app.handle("tab")
    assert app.palette_edit == "cancel " and app.palette_hint().startswith("cancel [ids")
    app.handle("esc")
    cmd("cancel 12480003"); assert app.mode == "confirm" and [j.id for j in app.confirm["jobs"]] == ["12480003"]
    app.handle("y"); assert ["scancel", "12480003"] in backend.calls
    cmd("filter rb1"); views.compose(store.snapshot(), app, W, H, actions); assert app.filter == "rb1" and all("rb1" in i or i.startswith("12480001") or i.startswith("12480002") for i in app.visible_ids)
    cmd("filter"); cmd("sort name"); assert app.sort["jobs"] == "name"
    cmd("sort bogus"); assert app.message.startswith("sort keys here")
    cmd("days 7"); assert app.analytics_days_value() == 7 and sampler.history_days == 7
    views.compose(store.snapshot(), app, W, H, actions)
    app.cursor["jobs"] = app.visible_ids.index("12480001")
    app.sync_selection()  # Analytics sample assertions need this running job.
    selected_job = app.selected_id
    cmd("tab analytics"); assert app.tab == "analytics" and app.analytics_job == selected_job
    cmd("view timeline"); assert app.analytics_view == "timeline"
    cmd("theme mono"); assert app.theme == "mono"
    cmd("gpu off"); assert app.gpu is False
    cmd("source account off"); assert store.health["account"].enabled is False
    cmd("mark all"); assert len(app.marks) >= 1
    cmd("unmark"); assert not app.marks
    cmd("nonsense"); assert app.message == "unknown command 'nonsense'"
    cmd("export csv"); assert app.message.startswith("no table") or app.message.startswith("exported")
    # the analytics views render inside the screen at several sizes
    for view in ("job", "history", "timeline"):
        app.analytics_view = view
        for (w, h) in ((150, 44), (100, 30), (80, 24)):
            rows, _ = views.compose(store.snapshot(), app, w, h, actions)
            assert len(rows) == h and all(L.vlen(L.row_text(r)) <= w for r in rows), (view, w, h)
    app.analytics_view = "job"
    rows, _ = views.compose(store.snapshot(), app, 150, 44, actions)
    text = "\n".join(L.row_text(r) for r in rows)
    assert "cpu per core" in text and "cpu samples" in text
    # The sidebar reduces chart width, so the independently scrollable Details
    # document can place its memory chart below CPU. Verify actual navigation
    # reaches that measured series instead of assuming a fixed raster row.
    cmd("focus details")
    for _ in range(8):
        if "memory of the request" in text:
            break
        app.handle("pgdn")
        rows, _ = views.compose(store.snapshot(), app, 150, 44, actions)
        text = "\n".join(L.row_text(r) for r in rows)
    assert "memory of the request" in text
    cmd("focus main")
    cmd("density compact")  # Use native series navigation rather than scrolling a chart panel.
    app.handle("home"); assert app.analytics_job == "12477369"               # series follow the scheduler feed, independently of table sorts
    app.handle("down"); assert app.analytics_job == "12480001"
    app.handle("up"); assert app.analytics_job == "12477369"
    rows, _ = views.compose(store.snapshot(), app, 150, 44, actions)
    from raven import chart_interaction
    # Additional GPU efficiency cards are reached through the real document
    # controls; they do not have to fit beside CPU and memory in one frame.
    for _ in range(16):
        if any(plot.key[2].startswith("gpu:") for plot in chart_interaction.initialize(app)["plots"]):
            break
        app.handle("pgdn")
        rows, _ = views.compose(store.snapshot(), app, 150, 44, actions)
    assert any(plot.key[2].startswith("gpu:") for plot in chart_interaction.initialize(app)["plots"])
    gpu_keys = {f"gpu:{sample.device_key}:" for sample in store.gpu["12477369"]}
    assert any(any(plot.key[2].startswith(key) for key in gpu_keys)
               for plot in chart_interaction.initialize(app)["plots"])
    app.analytics_view = "history"
    text = "\n".join(L.row_text(r) for r, in zip(views.compose(store.snapshot(), app, 150, 44, actions)[0:1]) for r in r)
    assert "jobs per day" in text and "core-hours per partition" in text and "queue wait" in text and "cpu efficiency" in text
    app.handle("3"); app.cursor["history"] = 0; app.handle("enter")
    assert app.tab == "analytics" and app.analytics_view == "job" and app.analytics_job == store.finished[0].id
    cmd("quit"); assert app.quit


def test_cli_csv_from_the_simulated_cluster():
    env = dict(os.environ, COLUMNS="150", USER="alex")
    r = subprocess.run([sys.executable, DASH, "--fake", "--csv", "--no-state", "--tab", "history"], env=env, capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.splitlines()[0].startswith("id,name,state") and "rb-gpucheck" in r.stdout
    r = subprocess.run([sys.executable, DASH, "--fake", "--once", "--ascii", "--no-color", "--no-state", "--width", "140", "--tab", "analytics"], env=env, capture_output=True, text=True)
    assert r.returncode == 0 and "job series" in r.stdout and "cpu per core" in r.stdout


# ------------------------------------------------------------------------------------------------ the log buffer and tab
def test_log_buffer_reads_incrementally_bounds_pages_and_reloads(tmp_path):
    from raven.logs import LogBuffer, LogSession
    p = tmp_path / "job.out"
    p.write_text("".join(f"line {i}\n" for i in range(100)))
    buf = LogBuffer(str(p), max_bytes=1 << 20)
    assert buf.refresh() and buf.total == 100 and buf.refresh() is False                  # a second refresh reads nothing
    with open(p, "a") as f:
        f.write("line 100\npartial")
    assert buf.refresh() and buf.total == 102 and buf.partial == "partial" and buf.lines[-1] == "line 100"
    with open(p, "a") as f:
        f.write(" done\n")
    assert buf.refresh() and buf.total == 102 and buf.lines[-1] == "partial done" and buf.partial == ""
    lines, start = buf.window(None, 10)
    assert start == 92 and lines[-1] == "partial done"                                        # following: the last page
    lines, start = buf.window(0, 10)
    assert (start, lines[0]) == (0, "line 0")
    lines, start = buf.window(500, 10)
    assert start == 92 and len(lines) == 10                                                   # a page is always full and never past the end
    assert buf.clamp_top(-5, 10) == 0 and buf.clamp_top(5, 200) is None
    assert buf.find("line 5") == 5 and buf.find("line 5", 5) == 50 and buf.find("LINE 9.", backwards=True) == 99 and buf.find("nothing") is None and buf.count("line 9") == 11
    p.write_text("fresh 0\nfresh 1\n")                                                        # truncation (or a replaced file) reloads
    assert buf.refresh() and buf.total == 2 and buf.lines[0] == "fresh 0"
    big = tmp_path / "big.out"
    big.write_bytes(b"".join(b"%06d abcdefghij\n" % i for i in range(20000)))                # 340 KB
    b2 = LogBuffer(str(big), max_bytes=100_000)
    b2.refresh()
    assert b2.truncated and b2.skipped_bytes > 0 and b2.lines[0].endswith("abcdefghij") and len(b2.lines[0]) == 17 and b2.lines[-1].startswith("019999")
    sess = LogSession(max_bytes=1 << 20)
    b = sess.buffer(str(p))
    sess.page = 1
    assert sess.following and b is sess.buffer(str(p))
    sess.scroll(-1, b)
    assert sess.top == 0 and not sess.following
    sess.scroll(+5, b)
    assert sess.following                                                                      # reaching the end follows again


def _history_content_rows(app, rows):
    """Keep viewport styles and cell boundaries while excluding the history dock."""
    rect = app.history_browser_content_rect
    result = []
    for row in rows[rect.y:rect.y + rect.height]:
        cropped, position = [], 0
        for text, style in row:
            characters = []
            for character in text:
                cells = 1 if character.isascii() else L.vlen(character)
                if rect.x <= position and position + cells <= rect.x + rect.width:
                    characters.append(character)
                elif cells and position < rect.x + rect.width and position + cells > rect.x:
                    characters.append(" " * (min(position + cells, rect.x + rect.width) - max(position, rect.x)))
                position += cells
            if characters:
                cropped.append(("".join(characters), style))
        result.append(cropped)
    return result


def test_log_tab_pages_scroll_and_search_through_the_controller(tmp_path):
    backend, store, sampler, actions, views, app = make_rich_app(tmp_path, rounds=1)
    logdir = tmp_path / "logs"
    logdir.mkdir()
    cwd = os.getcwd()
    os.chdir(tmp_path)                                               # the simulated scontrol says StdOut=<cwd>/logs/<name>-<id>.out
    try:
        path = logdir / "rb1-allrank-12480001.out"
        path.write_text("".join(f"window {i} done\n" for i in range(300)) + "Traceback: boom\n")
        W, H = 120, 30
        def render_log():
            rows, hits = views.compose(store.snapshot(), app, W, H, actions)
            return _history_content_rows(app, rows), hits
        views.compose(store.snapshot(), app, W, H, actions)
        app.cursor["jobs"] = app.visible_ids.index("12480001")
        views.compose(store.snapshot(), app, W, H, actions)
        app.handle("l")
        sampler.select("12480001"); sampler.round(wait=True)
        rows, _ = render_log()
        page = app.logs.page                                         # the page the view laid out (screen minus footer, header, rule and status)
        body = [L.row_text(r).rstrip() for r in rows if L.row_text(r).startswith(" window") or L.row_text(r).startswith(" Traceback")]
        assert len(body) == page and body[-1] == " Traceback: boom" and app.logs.following
        status = next(L.row_text(r) for r in rows if "lines" in L.row_text(r) and "of 301" in L.row_text(r))
        assert f"lines {302 - page}-301 of 301" in status and "following" in status
        for _ in range(5):
            app.handle("up")
        rows, _ = render_log()
        body = [L.row_text(r).rstrip() for r in rows if L.row_text(r).startswith(" window")]
        assert len(body) == page - 1 and body[0] == f" window {301 - page} done" and not app.logs.following
        assert app.logs.cursor == 295  # arrows move the logical cursor before scrolling at the viewport edge
        cursor_row = next(r for r in rows if r and r[-1][0] == ">" and {"cyan", "bold"} <= set(r[-1][1].split("+")))
        assert L.row_text(cursor_row[:-1]).strip() == "window 295 done"
        app.handle("pgup"); app.handle("pgup")
        app.handle("home")
        rows, _ = render_log()
        body = [L.row_text(r) for r in rows if L.row_text(r).startswith(" window")]
        assert len(body) == page and body[0].rstrip().endswith(">") and app.logs.top == 0
        assert L.row_text(next(r for r in rows if r and r[-1][0] == ">" and {"cyan", "bold"} <= set(r[-1][1].split("+")))[:-1]).strip() == "window 0 done"
        for _ in range(50):
            app.handle("up")                                          # bounded at the top
        assert app.logs.top == 0
        with open(path, "a") as f:                                   # the file grows while paused: the view stays put
            f.write("appended\n")
        rows, _ = render_log()
        assert L.row_text(rows[-1]).startswith(" window") and app.logs.top == 0
        app.handle("end")
        rows, _ = render_log()
        assert app.logs.following and L.row_text(rows[-1]).rstrip() == " appended"
        app.handle("/")
        for ch in "window 12 done":
            app.handle(ch if ch != " " else "space")
        app.handle("enter")
        complete_action(app)
        assert app.logs.search == "window 12 done" and app.message.startswith("1 lines match")
        rows, _ = render_log()
        hl = [r for r in rows if any("sel" in s.split("+") for _, s in r)]
        assert len(hl) == 1 and L.row_text(hl[0][:-1]).strip() == "window 12 done" and not app.logs.following
        app.handle("f")
        assert app.logs.following
        app.handle("f")
        assert not app.logs.following and app.logs.top == max(0, 302 - app.logs.page)
        app.handle(":")
        for ch in "find Traceback":
            app.handle(ch if ch != " " else "space")
        app.handle("enter")
        complete_action(app)
        assert app.logs.match == 300
        for _ in range(400):
            app.handle("down")                                        # bounded cursor while paused
        assert app.logs.cursor == 301 and not app.logs.following
        app.handle("end")
        assert app.logs.following
    finally:
        os.chdir(cwd)


# ------------------------------------------------------------------------------------------------ layer A: expressions, record / replay, remote, profiles, plugins, scripted mode, themes
def test_expressions_evaluate_safely_over_the_snapshot(tmp_path):
    from raven.expr import Expr, ExprError, NS, cluster_ns
    ns = NS(running=[NS(id="1", cpu=0.1, gpu=None, name="rb1"), NS(id="2", cpu=0.9, gpu=50.0, name="rb2")], n_pending=3, free=NS(a100=2))
    assert Expr("any(j.cpu < 0.3 for j in running)")(ns) is True and Expr("[j.id for j in running if j.gpu is not None and j.gpu < 60]")(ns) == ["2"]
    assert Expr('n_pending > 2 and free.get("a100", 0) > 0')(ns) is True and Expr("free.a40")(ns) is None and Expr('running[0].name.startswith("rb")')(ns)
    assert Expr("max(j.cpu for j in running)")(ns) == 0.9 and Expr("running[0].gpu < 20")(ns) is False and Expr("1 if n_pending else 0")(ns) == 1
    assert Expr("running[0].cpu + free.a100 * 2")(ns) == pytest.approx(4.1) and Expr("a + b").names == ["a", "b"] and Expr("sorted(j.name for j in running)[0]")(ns) == "rb1"
    for bad in ('__import__("os")', "running.__class__", "lambda: 1", 'open("x")', "x = 1", "print(1)", "2 ** 99", "import os", "running[0].cpu.__class__"):
        with pytest.raises(ExprError):
            Expr(bad)
    with pytest.raises(ExprError):
        Expr("nope")(ns)
    with pytest.raises(ExprError):
        Expr("1 / 0")(ns)
    backend, store, sampler, actions, views, app = make_app(tmp_path)
    c = cluster_ns(store.snapshot(), "alex", marks={"12477369"})
    assert c["n_running"] == 2 and c["n_pending"] == 3 and {j["id"] for j in c["running"]} == {"12477369", "12480001"} and c["free_gpus"]["a100"] == 2
    j = c["by_id"]["12477369"]
    assert j["gpus"] == 1 and j["marked"] and abs(j["elapsed"] - 1041) < 5 and j["left"] > 4000 and j["cpu"] is not None and 0 < j["mem"] < 1
    p = c["by_id"]["12480003"]
    assert p["pending"] and p["dep"] == "afterok:12480001" and p["waited"] > 100 and c["by_id"]["12480002_[0-7]"]["reason"] == "Priority"
    assert Expr('[j.id for j in pending if j.reason == "Dependency"]')(c) == ["12480003"] and Expr("finished[0].ok in (True, False)")(c)
    assert app.evaluate("n_running") == "2" and app.evaluate("[j for j in running]") == "['12477369', '12480001']" and app.evaluate("bogus(").startswith("eval:")


def test_recording_and_replay_reproduce_the_session(tmp_path):
    from raven import clock
    from raven.record import ReplayBackend, RecordingBackend, read_recording
    from raven.slurm import CommandError
    p = str(tmp_path / "rec.jsonl.gz")
    fb = FakeBackend("alex", speed=200.0)
    rb = RecordingBackend(fb, p, meta=dict(user="alex"))
    slurm = Slurm(rb, "alex")
    first = [j.id for j in slurm.jobs()]
    time.sleep(0.3)
    second = [j.id for j in slurm.jobs()]
    rb.call(["scancel", "12480003"])
    rb.close()
    header, entries = read_recording(p)
    assert header["user"] == "alex" and sum("cmd" in e for e in entries) >= 2 and any("call" in e for e in entries)
    rp = ReplayBackend(p, paused=True)
    s2 = Slurm(rp, "alex")
    assert [j.id for j in s2.jobs()] == first and rp.call(["scancel", "1"]) == (False, "replay: actions are disabled")
    rp.clock.seek(rp.t1)
    assert [j.id for j in s2.jobs()] == second and rp.clock.frac == 1.0
    with pytest.raises(CommandError):
        s2.node("a01-05")
    rp.clock.toggle_pause(); rp.clock.set_speed(5.0)
    assert not rp.clock.paused and rp.clock.speed == 5.0 and "answers to" in rp.summary()
    # the whole dashboard runs against the recording, with the clock on the recording's time
    env = dict(os.environ, COLUMNS="150", USER="nobody")
    r = subprocess.run([sys.executable, DASH, "--replay", p, "--paused", "--once", "--ascii", "--no-color", "--width", "150"], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "Raven Light/slurm - alex" in r.stdout and "REPLAY" in r.stdout and "paused" in r.stdout and "12480001" in r.stdout
    assert time.strftime("%H:%M", time.localtime(rp.t0)) in r.stdout
    assert clock.now() == pytest.approx(time.time(), abs=2)          # the replay clock never leaks into the test process


def test_ssh_backend_and_remote_files_through_a_local_shell(tmp_path):
    from raven.remote import RemoteFiles, SshBackend
    from raven.logs import LogBuffer
    from raven.slurm import CommandError

    def runner(argv, timeout):                                    # "ssh -- host <command>": run the command in a local shell instead
        assert argv[0] == "ssh" and argv[-3] == "--" and argv[-2] == "alex@login.example" and "ControlMaster=auto" in argv
        r = subprocess.run(["sh", "-c", argv[-1]], capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr

    sb = SshBackend("login.example", user="alex", opts=["-F", "none"], runner=runner)
    assert sb.argv(["squeue", "-o", "%i|%j"])[-1] == "squeue -o '%i|%j'"
    assert sb.run(["echo", "hi there"])[0] == "hi there\n" and sb.call(["true"]) == (True, "") and sb.call(["false"])[0] is False
    with pytest.raises(CommandError):
        sb.run(["sh", "-c", "exit 3"])
    off = SshBackend("down.example", runner=lambda argv, t: (255, "", "ssh: connect to host down.example port 22: No route to host"))
    with pytest.raises(CommandError, match="No route"):
        off.run(["squeue"])
    rf = RemoteFiles(sb)
    f = tmp_path / "x.out"
    f.write_text("abcdef\nghij\n")
    assert rf.stat(str(f))[0] == 12 and rf.read(str(f), 3, 4) == b"def\n" and rf.exists(str(f)) and not rf.exists(str(f) + ".nope")
    assert rf.tail(str(f), 5) == (b"ghij\n", 12) and rf.less_argv(str(f))[:2] == ["ssh", "-t"] and rf.less_argv(str(f))[-3:-1] == ["--", "alex@login.example"] and "x.out" in rf.listdir(str(tmp_path))
    rf.min_refresh = 0.0
    buf = LogBuffer(str(f), files=rf)
    assert buf.refresh() and buf.lines == ["abcdef", "ghij"]
    f.write_text("abcdef\nghij\nklm\n")
    assert buf.refresh() and buf.lines[-1] == "klm" and buf.refresh() is False
    rf.min_refresh = 100.0
    f.write_text("abcdef\nghij\nklm\nnew\n")
    assert buf.refresh() is False                                  # a remote file is not re-checked every frame
    env = dict(os.environ, COLUMNS="150", USER="alex")
    r = subprocess.run([sys.executable, DASH, "--user", "alex", "--host", "nowhere.invalid", "--once", "--no-state", "--ascii", "--no-color", "--width", "150"], env=env,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and "alex@nowhere.invalid" in r.stdout
    assert "Notifications" in r.stdout
    # The one-shot report retains either the transport error or the reason that
    # shared admission deferred collection before another connection attempt.
    assert "ssh" in r.stdout or "Raven collection deferred:" in r.stdout


def test_profiles_merge_and_switch(tmp_path):
    toml = sys.version_info >= (3, 11)
    p = tmp_path / ("c.toml" if toml else "c.json")
    p.write_text('history_days = 2\n[profiles.carc]\naccount = "lab_01"\nhistory_days = 7\ntheme = "mono"\n[profiles.other]\nhost = "other.example"\n' if toml else
                 json.dumps({"history_days": 2, "profiles": {"carc": {"account": "lab_01", "history_days": 7, "theme": "mono"}, "other": {"host": "other.example"}}}))
    cfg = Config.load(str(p))
    assert cfg.profiles() == ["carc", "other"] and cfg.profile("carc")["history_days"] == 7 and cfg["history_days"] == 2 and cfg.profile("carc").profile_name == "carc"
    with pytest.raises(KeyError):
        cfg.profile("nope")
    env = dict(os.environ, COLUMNS="150", USER="alex")
    r = subprocess.run([sys.executable, DASH, "--user", "alex", "--fake", "--config", str(p), "--profile", "carc", "--once", "--ascii", "--no-color", "--no-state", "--width", "150", "--tab", "history"],
                       env=env, capture_output=True, text=True)
    assert r.returncode == 0 and "alex [carc]" in r.stdout and "last 7 days" in r.stdout
    backend, store, sampler, actions, views, app = make_app(tmp_path)
    app.cfg = cfg
    app.run_command("profile other")
    assert app.switch_profile == "other" and app.quit
    app.quit, app.switch_profile = False, None
    app.run_command("profile nope")
    assert app.message.startswith("profile <carc|other>") and not app.quit


def test_plugins_add_commands_flags_tabs_hooks_and_sources(tmp_path):
    from raven import plugins
    d = tmp_path / "plugins"
    d.mkdir()
    (d / "hello.py").write_text(
        "seen = []\n"
        "def setup(api):\n"
        "    api.command('hello', lambda app, args: 'hello ' + (' '.join(args) or app.user), 'hello [name]')\n"
        "    api.flag(lambda job, snap, app: 'big' if job.cpus >= 32 else None, style='magenta')\n"
        "    api.tab('hello', 'Hello', lambda snap, app, width, height: [[('   hi there ' + str(len(snap['jobs'])), 'bold')]])\n"
        "    api.on_event(lambda ev: seen.append(ev['kind']))\n"
        "    api.source('ticker', 1.0, lambda slurm, store: store.event('tick', 'tick'))\n")
    (d / "broken.py").write_text("def setup(api):\n    raise RuntimeError('nope')\n")
    (d / "nosetup.py").write_text("x = 1\n")
    api = plugins.load(plugins.discover("", [str(d)]))
    assert api.loaded == ["hello"] and set(api.errors) == {"broken", "nosetup"} and "nope" in api.errors["broken"]
    cfg = Config({"plugins": [str(d)]})
    backend = FakeBackend("alex")
    slurm = Slurm(backend, "alex")
    store = Store(state_dir=None, persist=False)
    sampler = Sampler(slurm, store, cfg["intervals"], cfg["gpu_types"], account="lab_01")
    for name, interval, fn in api.sources:
        sampler.add_source(name, interval, fn)
    sampler.hooks.extend(api.event_hooks)
    actions = Actions(slurm, store, emit=sampler.emit)
    views = Views(L.Glyphs(True), cfg, plugins=api)
    app = App(store, sampler, actions, cfg, "alex", ascii_=True)
    app.plugins, app.views_ref = api, views
    sampler.round(wait=True)
    assert [e["kind"] for e in store.events].count("tick") == 1 and store.health["ticker"].calls == 1
    text = screen.once_text(app, views, store, actions, 160, False, tab="jobs")
    row = next(l for l in text.splitlines() if "12480001" in l)
    assert "big" in row
    app.run_command("hello world"); assert app.message == "hello world"
    app.run_command("hell"); assert app.message == "hello alex"                # a unique prefix (hel would also match help)
    assert "hello" in app.palette_hint() and app.palette_complete("hell") == "hello "
    app.run_command("tab hello")
    rows, _ = views.compose(store.snapshot(), app, 120, 30, actions)
    assert app.tab == "hello" and any("hi there 5" in L.row_text(r) for r in rows) and any(h[3] == "hello" for h in app.tab_hits)
    app.run_command("cancel 12480003"); app.handle("y")
    complete_action(app)
    assert "action" in api.modules["hello"].seen                  # the hook saw the action
    from raven.views import TABS
    TABS[:] = [t for t in TABS if t[0] != "hello"]                             # leave the module as the other tests expect it
    r = subprocess.run([sys.executable, DASH, "--fake", "--no-state", "--once", "--ascii", "--no-color", "--width", "150", "--config", str(tmp_path / "none.json")],
                       env=dict(os.environ, USER="alex"), capture_output=True, text=True)
    assert r.returncode == 2 or r.returncode == 1                             # a missing --config is an error, as before
    cfgj = tmp_path / "c.json"
    cfgj.write_text(json.dumps({"plugins": [str(d)]}))
    # Arbitrary in-process plugins belong to the explicit legacy library API.
    # Public Raven launchers reject them until they have admission contracts.
    legacy_entry = "from raven.cli import main; raise SystemExit(main())"
    r = subprocess.run([sys.executable, "-c", legacy_entry, "--fake", "--no-state", "--once", "--ascii", "--no-color", "--width", "160", "--config", str(cfgj)],
                       env=dict(os.environ, USER="alex"), capture_output=True, text=True)
    assert r.returncode == 0 and "Hello" in r.stdout and "big" in r.stdout and "plugin broken failed" in r.stdout


def test_scripted_mode_runs_evaluates_and_waits():
    env = dict(os.environ, COLUMNS="150", USER="alex")
    run = lambda *a: subprocess.run([sys.executable, DASH, "--fake", "--no-state", *a], env=env, capture_output=True, text=True)
    r = run("run", "cancel", "12480003")
    assert r.returncode == 3 and "add --yes" in r.stdout
    r = run("run", "cancel", "12480003", "--yes")
    assert r.returncode == 0 and "cancel 12480003: sent" in r.stdout and "scancel 12480003 rb2-controls: ok" in r.stdout
    r = run("--run", "hold 999", "--yes")
    assert r.returncode == 1 and "no such job" in r.stdout
    r = run("--eval", "[j.name for j in running]")
    assert r.returncode == 0 and r.stdout.strip() == "['rb-setup', 'rb1-allrank']"
    r = run("--eval", "n_pending")
    assert r.stdout.strip() == "3"
    r = run("--wait-for", "n_running == 2", "--timeout", "5")
    assert r.returncode == 0 and r.stdout.strip() == "True"
    r = run("--wait-for", "n_running == 7", "--timeout", "1.2", "--poll", "0.5")
    assert r.returncode == 2 and "still false" in r.stderr
    r = run("--eval", "bogus(")
    assert r.returncode == 1 and r.stdout.startswith("eval:")
    r = run("run", "export", "csv", "--tab", "history")
    assert r.returncode == 0 and "exported" in r.stdout


def test_themes_map_styles_and_reader_mode_drops_glyphs(tmp_path):
    base = dict(bold=1, dim=2, rev=4, under=8, sel=16)
    colors = dict(green=32, red=64, yellow=128, blue=256, magenta=512, sel=1024)
    sa = screen.style_attr
    assert sa("green", "default", base, colors, 1) == 32 and sa("green", "cb", base, colors, 1) == 256 and sa("red", "cb", base, colors, 1) == 128
    assert sa("red", "mono", base, colors, 1) == 1 and sa("sel", "reader", base, colors, 1) == 16 and sa("sel", "default", base, colors, 1) == 1024
    assert sa("green", "high", base, colors, 1) == 33 and sa("bold+dim", "reader", base, colors, 1) == 3 and sa("cyan", "mono", base, colors, 1) == 0
    backend, store, sampler, actions, views = make_app(tmp_path)[:5]
    cfg = Config()
    views = Views(L.Glyphs(False), cfg)
    app = App(store, sampler, actions, cfg, "alex")
    app.views_ref = views
    seen = []
    from raven.controller import THEMES
    for _ in range(len(THEMES)):
        app.handle("T"); seen.append(app.theme)
    assert seen == THEMES[1:] + THEMES[:1]
    app.run_command("theme reader")
    assert views.g.ascii and views.g.full == "#"
    text = screen.once_text(app, views, store, actions, 150, False, tab="jobs")
    assert "─" not in text and "█" not in text and "·" not in text
    app.run_command("theme default")
    assert not views.g.ascii
    app.run_command("theme nope"); assert app.message == "theme <" + "|".join(THEMES) + ">"


# ------------------------------------------------------------------------------------------------ layer B: group, queue weather, allocation, node map, steps, GPU trace
def test_layer_b_parsers_on_recorded_output():
    from raven.slurm import (parse_group, parse_pending_ahead, parse_test_only, parse_sreport, parse_tres_mins, parse_steps, parse_sacct_steps, parse_node_map, parse_gpu_trace)
    g = parse_group("1|bob|md|gpu|RUNNING|02:00:00|1-00:00:00|2|16|gres/gpu:a100:2|None|9000|a02-[01-02]|2026-09-30T10:00:00|2026-09-30T10:10:00\n"
                    "2|carol|dft|main|PENDING|0:00|2-00:00:00|4|64|N/A|Priority|7000|(Priority)|2026-09-30T11:00:00|N/A\n")
    assert [(j.user, j.gpus, j.nodelist, j.pending, j.start) for j in g] == [("bob", 4, "a02-[01-02]", False, "2026-09-30T10:10:00"), ("carol", 0, "", True, "")]
    a = parse_pending_ahead("gpu|8|gres/gpu:a100:1|1\ngpu,main|32|N/A|2\nmain|16|N/A|1\n")
    assert a["gpu"] == dict(jobs=2, cpus=40, gpus=1, by_type={"a100": 1}) and a["main"]["jobs"] == 2 and a["main"]["cpus"] == 48
    assert parse_test_only("sbatch: Job 123 to start at 2026-10-01T07:00:00 using 8 processors on nodes a01-05 in partition gpu") == ("2026-10-01T07:00:00", "a01-05")
    assert parse_test_only("sbatch: error: Batch job submission failed: Invalid partition")[0] is None
    r = parse_sreport("lab_01||cpu|38400\nlab_01||gres/gpu|4100\nlab_01|alex|cpu|12400\n")
    assert r[""] == {"cpu": 38400.0, "gpu": 4100.0} and r["alex"]["cpu"] == 12400.0
    assert parse_tres_mins("lab_01||cpu=6000000,gres/gpu=600000|\nlab_01|alex|cpu=60|\n") == {"cpu": 100000.0, "gpu": 10000.0}
    st = parse_steps("1.extern|00:00:00|1200K|0|n1|1200K|2|00:00:00|0|n1\n1.batch|01:00:00|4G|0|n1|3G|1|01:00:00|0|n1\n1.0|00:50:00|2G|3|n2|1G|8|00:20:00|3|n2\n")
    assert [(s.name, s.ntasks, s.min_cpu_task, s.min_cpu_node) for s in st] == [("extern", 2, "0", "n1"), ("batch", 1, "0", "n1"), ("0", 8, "3", "n2")]
    assert st[2].min_cpu == 1200 and st[2].rss == 2 * 1024 ** 3 and st[2].rss_task == "3"
    fs = parse_sacct_steps("9|rb-x|COMPLETED|00:03:12|00:01:30||||1|0:0|a01-05\n9.batch|batch|COMPLETED|00:03:12|00:01:30|1.1G|a01-05|0|1|0:0|a01-05\n")
    assert [(s.name, s.state, s.rss > 0) for s in fs] == [("rb-x", "COMPLETED", False), ("batch", "COMPLETED", True)]
    nm = parse_node_map("a01-05 gpu gpu:a100:2(S:0-1) gpu:a100:2(IDX:0-1) mix 24/40/0/64 257000 65536\na01-05 debug gpu:a100:2(S:0-1) gpu:a100:2(IDX:0-1) mix 24/40/0/64 257000 65536\n"
                        "e06-02 main (null) (null) down* 0/0/64/64 257000 0\nb01-02 gpu gpu:a40:4(S:0-1) gpu:a40:0(IDX:N/A) drain 0/0/64/64 515000 0\n")
    assert len(nm) == 3 and nm["a01-05"].partitions == ["gpu", "debug"] and (nm["a01-05"].gpus, nm["a01-05"].gpus_used, nm["a01-05"].cpus_alloc, nm["a01-05"].cpus) == (2, 2, 24, 64)
    assert nm["e06-02"].down and nm["b01-02"].down and nm["b01-02"].gpus_used == 0 and nm["a01-05"].mem == 257000.0
    tr = parse_gpu_trace(b"2026/10/01 06:00:01.123, 0, 85, 12000\n2026/10/01 06:00:01.125, 1, 3, 500\nbad line\n2026/10/01 06:01:01.123, 0, 90, 12000\n")
    assert [(r["index"], r["util"]) for r in tr] == [(0, 85.0), (1, 3.0), (0, 90.0)] and tr[2]["t"] - tr[0]["t"] == 60


def test_group_weather_budget_map_steps_and_trace_through_the_dashboard(tmp_path):
    from raven.slurm import parse_steps
    cwd = os.getcwd()
    os.chdir(tmp_path)                                               # the simulated WorkDir is the current directory
    try:
        (tmp_path / "logs").mkdir()
        (tmp_path / "logs" / "gpu-util-12477369.csv").write_text("".join(f"2026/10/01 06:{i:02d}:01.000, 0, {80 if i % 3 else 5}, 12000\n" for i in range(30)))
        backend, store, sampler, actions, views, app = make_rich_app(tmp_path, rounds=2)
        app.run_command("density compact")  # Full-width scientific metadata remains available.
        W, H = 150, 44
        # the account's jobs
        assert len(store.group) == 10 and {j.user for j in store.group} == {"alex", "bob", "carol"} and store.account["running"] == 5 and store.account["cpus"] == 136
        app.handle("8")
        rows, hits = views.compose(store.snapshot(), app, W, H, actions)
        text = "\n".join(L.row_text(r) for r in rows)
        assert app.tab == "group" and "3 users, 5 running, 5 pending" in text and "carol" in text and "md-run" in text
        assert len([hit for hit in hits if hit[1] == "group"]) == 10
        assert app.group_ids[0] == "12477369"                        # sorted by user: alex first
        app.handle("s"); rows, _ = views.compose(store.snapshot(), app, W, H, actions)
        assert app.sort["group"] == "state" and app.group_ids[0] in ("12477369", "12480001")
        app.run_command("sort priority"); views.compose(store.snapshot(), app, W, H, actions)
        assert app.group_ids[0] == "12477369" and app.group_ids[-1] == "12470005"
        app.run_command("filter bob"); views.compose(store.snapshot(), app, W, H, actions)
        assert all(store.group[[j.id for j in store.group].index(i)].user == "bob" for i in app.group_ids) and len(app.group_ids) == 3
        app.run_command("filter")
        rows, hits = views.compose(store.snapshot(), app, W, H, actions)
        y = [h for h in hits if h[2] == "12470003"][0][0]
        app.click(y, app.workspace_main_rect.x + 4, hits); assert app.group_selected() == "12470003"
        app.handle("i")                                               # details of someone else's running job: scontrol in the fake answers only mine
        assert app.mode == "details" and app.detail_id == "12470003"
        app.handle("esc")
        app.handle("C"); assert Path(app.message.split()[-1]).read_text().splitlines()[0].startswith("id,user,name") and "carol" in Path(app.message.split()[-1]).read_text()
        # queue weather and the allocation on the cluster tab
        assert store.pending_ahead["gpu"]["jobs"] == 11 and len(store.weather) == 4 and all(w["ok"] and w["est"] for w in store.weather)
        assert store.budget["used"]["cpu"] == 38400.0 and store.budget["limit"]["gpu"] == 10000.0 and store.budget["week"]["gpu"] == 260.0
        app.handle("2")
        text = "\n".join(L.row_text(r) for r in views.compose(store.snapshot(), app, 170, 60, actions)[0])
        assert "queue weather" in text and "11 pending jobs asking 104 cpus, 13 gpus (a100 8  a40 5)" in text and "gpu:a100:1, 01:00:00 would start in" in text
        assert "main      a job of 16 cpus, 64G, 01:00:00 would start" in text and "allocation of lab_01" in text and "38,400 of 100,000 (38%)" in text and "burning 300/day" in text and "bob 20,000 cpu-h" in text
        wide = "\n".join(L.row_text(r) for r in views.compose(store.snapshot(), app, 220, 60, actions)[0])
        assert "allocation cpu 38% gpu 41%" in wide
        # the node map
        assert len(store.nodemap) == 15 and store.nodemap["e05-12"].partitions == ["main", "debug"]
        app.handle("4"); app.handle("right")
        rows, _ = views.compose(store.snapshot(), app, W, H, actions)
        text = "\n".join(L.row_text(r) for r in rows)
        assert app.nodes_view == "map" and "gpu: 7 nodes, 1 idle, 1 down/drained" in text and "10/20 gpus in use" in text and "1 running mine" in text
        assert "*= a01-05" in text and "x b01-02" in text and "- l01-01" in text and all(L.vlen(L.row_text(r)) <= W for r in rows)
        app.handle("left"); assert app.nodes_view == "mine"
        app.save(); assert store.load_ui()["nodes_view"] == "mine"
        # steps of a running job (the simulated sstat has batch and extern only: inject a multi-task step) and the slowest rank
        store.steps["12480001"] = parse_steps("12480001.extern|00:00:00|1200K|0|e05-12|1200K|1|00:00:00|0|e05-12\n12480001.batch|01:00:00|4G|0|e05-12|3G|1|01:00:00|0|e05-12\n"
                                              "12480001.0|00:50:00|2G|3|e05-13|1G|8|00:20:00|3|e05-13\n")
        app.handle("1"); views.compose(store.snapshot(), app, W, H, actions)
        app.cursor["jobs"] = app.visible_ids.index("12480001")
        rows, _ = views.compose(store.snapshot(), app, W, 60, actions)
        text = "\n".join(L.row_text(r) for r in rows)
        assert "step 0        8 tasks" in text and "slowest rank 3@e05-13 at 40% of the mean" in text and "peak 2.0 GB on task 3@e05-13" in text
        app.handle("enter")
        ov = views.overlay(store.snapshot(), app, W, H)
        text = "\n".join(L.row_text(r) for _, _, r in ov)
        assert "steps" in text and "SLOWEST RANK" in text and "task 3@e05-13 00:20:00" in text
        app.handle("esc")
        # the GPU trace the job writes itself: the selected panel and the analytics chart
        sampler.select("12477369"); sampler.round(wait=True)
        sampler.last_run["trace"] = 0.0; sampler.round(wait=True)
        assert len(store.trace["12477369"]) == 30
        app.cursor["jobs"] = app.visible_ids.index("12477369")
        rows, _ = views.compose(store.snapshot(), app, 170, 60, actions)
        text = "\n".join(L.row_text(r) for r in rows)
        assert "trace" in text and "gpu0 55% (now 80%)" in text and "29 min in the job's own nvidia-smi log, 33% of samples idle" in text
        app.analytics_job, app.analytics_view = "12477369", "job"
        app.handle("7")
        app.run_command("history-dock off")  # This assertion inspects the full scientific chart title.
        rows, _ = views.compose(store.snapshot(), app, 150, 50, actions)
        from raven import chart_interaction
        for _ in range(24):
            trace_plot = next((plot for plot in chart_interaction.initialize(app)["plots"]
                               if plot.key[2] == "gpu-trace:0:rate"), None)
            if trace_plot:
                break
            app.handle("pgdn")
            rows, _ = views.compose(store.snapshot(), app, 150, 50, actions)
        assert trace_plot is not None
        assert trace_plot.x_bounds == (store.trace["12477369"][0]["t"], store.trace["12477369"][-1]["t"])
        # a finished job's details: sacct -j on demand
        app.handle("3"); app.cursor["history"] = [f.id for f in store.finished].index("12476001")
        app.handle("i")
        assert app.mode == "details" and app.detail_id == "12476001"
        app.tick(); sampler.round(wait=True)
        ov = views.overlay(store.snapshot(), app, W, H)
        text = "\n".join(L.row_text(r) for _, _, r in ov)
        assert "rb-gpucheck" in text and "COMPLETED" in text and "batch" in text and "1.1 GB" in text and "workdir" in text
    finally:
        os.chdir(cwd)


def test_cli_group_csv_and_cluster_once():
    env = dict(os.environ, COLUMNS="150", USER="alex")
    r = subprocess.run([sys.executable, DASH, "--fake", "--csv", "--no-state", "--tab", "group"], env=env, capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.splitlines()[0].startswith("id,user,name") and "carol" in r.stdout and r.stdout.count("\n") == 11
    r = subprocess.run([sys.executable, DASH, "--fake", "--once", "--ascii", "--no-color", "--no-state", "--width", "170", "--tab", "cluster"], env=env, capture_output=True, text=True)
    assert r.returncode == 0 and "partitions" in r.stdout and "fair share" in r.stdout
    assert "queue weather" not in r.stdout and "would start in" not in r.stdout
    r = subprocess.run([sys.executable, DASH, "--fake", "--no-state", "--eval", "len(set(j.name for j in jobs))"], env=env, capture_output=True, text=True)
    assert r.stdout.strip() == "5"


# ------------------------------------------------------------------------------------------------ layer C: advisor, alerts, comparison, dependency chains, tags and pins
def test_advisor_suggests_from_finished_and_running_jobs():
    from raven import advisor
    from raven.model import Finished, Job, Live, nbytes
    f = Finished(id="1", name="rb3", state="COMPLETED", elapsed="01:00:00", cpus=8, cpu_time=4 * 3600 * 0.3, req_mem=nbytes("64G"), rss=nbytes("9.1G"), end="2026-10-01T06:00:00", limit="12:00:00")
    a = advisor.advise_finished(f)
    assert a.flags() == "--mem=12G --cpus-per-task=2 --time=01:30:00" and a.wasted_core_hours == pytest.approx(8 * 0.85, abs=0.01) and "peak 9.1 GB of 64.0 GB" in a.summary()
    o = Finished(id="2", name="rb3", state="OUT_OF_MEMORY", elapsed="00:41:03", cpus=8, cpu_time=3 * 3600, req_mem=nbytes("32G"), rss=nbytes("31.9G"), end="2026-10-01T05:00:00", limit="02:00:00")
    assert advisor.advise_finished(o).mem_suggest == "64G" and "doubled" in advisor.advise_finished(o).summary()
    t = Finished(id="3", name="rbT", state="TIMEOUT", elapsed="01:30:00", cpus=8, cpu_time=8 * 5400 * 0.9, req_mem=nbytes("32G"), rss=nbytes("30G"), end="2026-10-01T05:00:00", limit="01:30:00")
    at = advisor.advise_finished(t)
    assert at.time_suggest == "03:00:00" and at.cpus_suggest is None and at.mem_suggest == "38G"
    ok = Finished(id="4", name="fine", state="COMPLETED", elapsed="01:00:00", cpus=4, cpu_time=4 * 3600 * 0.9, req_mem=nbytes("8G"), rss=nbytes("7G"), end="2026-10-01T05:00:00", limit="02:00:00")
    assert advisor.advise_finished(ok).flags() == ""                                            # nothing to change: within 15 % and 60 % of the limit
    j = Job(id="9", name="rb3", partition="main", state="RUNNING", elapsed="00:30:00", limit="12:00:00", cpus=8, mem_req="64G")
    r = advisor.advise_running(j, Live(cpu_time=100.0, rss=nbytes("5G"), avg=0.2), [], [f, o])
    assert r.partial and r.mem_peak == nbytes("31.9G") and r.mem_suggest == "40G" and r.time_suggest == "01:30:00" and r.cpus_suggest == 3 and "so far" in r.notes
    names = advisor.advise_names([f, o, t, ok])
    assert [a.name for a in names] == ["rb3", "rbT", "fine"] and names[0].id == "2 runs" and names[0].mem_suggest == "64G" and "1 out of memory" in names[0].notes
    assert advisor.round_mem(nbytes("700M")) == "1G" and advisor.round_mem(nbytes("1.3G")) == "1.5G" and advisor.round_time(3600 * 1.3) == "01:30:00"


def test_alert_rules_fire_rate_limit_and_report_errors(tmp_path):
    from raven.alerts import AlertEngine
    backend, store, sampler, actions, views, app = make_rich_app(tmp_path, rounds=2)
    rang = []
    seen = []
    eng = AlertEngine([dict(name="low cpu", when="running and cpu is not None and cpu < 0.5", every=0, actions=["bell", "event"]),
                       dict(name="gpu job", when="running and gpus > 0", every=1, actions=["notify"]),
                       dict(name="queue", when="n_pending >= 3", scope="cluster", every=0),
                       dict(name="broken", when="running and (", actions=["event"])], store, user="alex", notify=seen.append, bell=lambda: rang.append(1))
    assert "broken" in eng.errors and [e["kind"] for e in store.events].count("alert_error") == 1
    fired = eng.check(store.snapshot())
    assert {f["rule"] for f in fired} == {"low cpu", "gpu job", "queue"} and [f["job"] for f in fired if f["rule"] == "low cpu"] == ["12477369"]
    assert rang == [1, 1] and seen and seen[0]["kind"] == "alert" and eng.active_count() == 3 and "low cpu 12477369" in eng.active_text()
    assert eng.check(store.snapshot()) == []                                                   # every = 0: once per job; every = 1: not yet
    time.sleep(1.1)
    again = eng.check(store.snapshot())
    assert [f["rule"] for f in again] == ["gpu job"]                                           # the rate limit passed for that rule only
    store.alerts = eng
    sampler.last_run["jobs"] = 0.0
    sampler.round(wait=True)                                                                   # the sampler checks after each jobs round
    rows, _ = views.compose(store.snapshot(), app, 170, 44, actions)
    text = "\n".join(L.row_text(r) for r in rows)
    # The next queue round starts another GPU job. Its alert joins the three
    # retained notices, while the repeated alert for rb-setup is deduplicated.
    assert "Moderate 4" in text and "Critical 1" in text
    assert any(item.text == "gpu job: 12480005 rb5-selector" for item in app.notifications.snapshot().notices)
    assert next(item for item in app.notifications.snapshot().notices
                if item.text == "gpu job: 12477369 rb-setup").occurrences == 2
    assert any(item.text == "low cpu: 12477369 rb-setup" for item in app.notifications.snapshot().notices)
    assert any(e["text"] == "low cpu: 12477369 rb-setup" and e["kind"] == "alert" for e in store.events)
    r = eng.add("running and cpus >= 32", name="big")
    assert eng.check(store.snapshot())[0]["job"] == "12480001" and not r.error
    env = dict(os.environ, COLUMNS="150", USER="alex")
    r = subprocess.run([sys.executable, DASH, "--fake", "--no-state", "--once", "--ascii", "--no-color", "--width", "170", "--alert", "running and cpus >= 32", "--alert", "bogus("],
                       env=env, capture_output=True, text=True)
    assert r.returncode == 0 and "alert 1: 12480001 rb1-allrank" in r.stdout and "alert rule: alert 2" in r.stdout


def test_dependency_graph_tab_and_chain_actions(tmp_path):
    from raven.deps import DepGraph, parse_dependency
    assert parse_dependency("afterok:123:456(unfulfilled),afterany:789?singleton") == [("afterok", "123"), ("afterok", "456"), ("afterany", "789"), ("singleton", "")]
    assert parse_dependency("after:12+5") == [("after", "12")] and parse_dependency("(null)") == []
    jobs = [Job(id="1", name="a", partition="p", state="RUNNING"), Job(id="2", name="b", partition="p", state="PENDING", dependency="afterok:1"),
            Job(id="3", name="c", partition="p", state="PENDING", dependency="afterany:2"), Job(id="4", name="d", partition="p", state="PENDING", dependency="afterok:1:9"),
            Job(id="5", name="a", partition="p", state="PENDING", dependency="singleton"), Job(id="6", name="z", partition="p", state="RUNNING")]
    g = DepGraph(jobs, {"9": "old completed"})
    assert g.roots() == ["1", "9"] and g.downstream("1") == ["2", "4", "5", "3"] and g.upstream("3") == ["2", "1"] and g.blocked_by("4") == ["afterok 1 a (running)", "afterok 9 (old completed)"]
    assert g.trees()[0][:3] == [(0, "", "1"), (1, "afterok", "2"), (2, "afterany", "3")] and g.trees()[1] == [(0, "", "9"), (1, "afterok*", "4")]
    backend, store, sampler, actions, views, app = make_rich_app(tmp_path, rounds=1)
    W, H = 150, 40
    app.handle("9")
    app.run_command("history-dock off")  # Inspect the full dependency explanation and chain actions.
    rows, hits = views.compose(store.snapshot(), app, W, H, actions)
    text = "\n".join(L.row_text(r) for r in rows)
    assert app.tab == "deps" and "2 edges among 3 jobs" in text and "afterok -> 12480003 rb2-controls" in text and "afterany -> 12480002_[0-7]" in text and "2 independent jobs" in text
    assert app.dep_ids == ["12480001", "12480003", "12480002_[0-7]"] and app.selected_id == "12480001"
    app.handle("down"); views.compose(store.snapshot(), app, W, H, actions)
    assert app.selected_id == "12480003"
    rows, _ = views.compose(store.snapshot(), app, W, H, actions)
    assert "waits for afterok 12480001 rb1-allrank (running)" in "\n".join(L.row_text(r) for r in rows)
    app.handle("c")
    assert app.mode == "confirm" and [j.id for j in app.confirm["jobs"]] == ["12480003", "12480002_[0-7]"]        # the job and everything downstream
    app.handle("n")
    app.handle("1"); views.compose(store.snapshot(), app, W, H, actions)
    app.cursor["jobs"] = app.visible_ids.index("12480001")
    app.sync_selection()  # Inspect the prerequisite rather than the first batch.
    rows, _ = views.compose(store.snapshot(), app, 170, 50, actions)
    text = "\n".join(L.row_text(r) for r in rows)
    assert "2 jobs wait for this one: 12480003(rb2-controls) 12480002_[0-7](rb1-panel)" in text                      # the selected panel of 12480001
    app.cursor["jobs"] = app.visible_ids.index("12480003"); rows, _ = views.compose(store.snapshot(), app, 170, 50, actions)
    text = "\n".join(L.row_text(r) for r in rows)
    assert "waits for afterok 12480001 rb1-allrank (running)" in text and "1 job wait for it: 12480002_[0-7](rb1-panel)" in text
    app.run_command("chain cancel 12480001")
    assert app.mode == "confirm" and [j.id for j in app.confirm["jobs"]] == ["12480001", "12480003", "12480002_[0-7]"]
    app.handle("y")
    assert ["scancel", "12480001", "12480003", "12480002_[0-7]"] in backend.calls
    app.run_command("chain hold 12480005"); assert app.mode == "confirm" and [j.id for j in app.confirm["jobs"]] == ["12480005"]
    app.handle("n")
    app.run_command("chain fly"); assert app.message.startswith("chain <cancel|hold|release>")
    app.handle("9")
    rows, hits = views.compose(store.snapshot(), app, W, H, actions)
    app.click(hits[0][0], app.history_browser_content_rect.x + 5, hits)
    assert app.cursor["deps"] == 0


def test_tags_pins_notes_filter_and_compare(tmp_path):
    backend, store, sampler, actions, views, app = make_rich_app(tmp_path, rounds=3)
    app.run_command("density compact")  # Optional tag columns fit the full-width table.
    W, H = 190, 44
    views.compose(store.snapshot(), app, W, H, actions)
    app.cursor["jobs"] = app.visible_ids.index("12480001")
    app.sync_selection()  # Keep this job's tags and note in the Details pane.
    app.run_command("tag 12480001 urgent paper"); assert app.message == "tagged 12480001: #urgent #paper"
    app.run_command("pin 12480003"); assert app.message == "pinned 12480003" and store.pinned("12480003")
    app.run_command("note 12480001 rerun with 32 cores"); assert store.tags["12480001"]["note"] == "rerun with 32 cores"
    rows, hits = views.compose(store.snapshot(), app, W, H, actions)
    text = "\n".join(L.row_text(r) for r in rows)
    pinned_y = next(y for y, kind, jid in hits if kind == "job" and jid == "12480003")
    pinned_row = L.row_text(rows[pinned_y])[app.workspace_main_rect.x:app.workspace_main_rect.x + app.workspace_main_rect.width]
    assert app.visible_ids[0] == "12480003" and pinned_row.lstrip().startswith("^") and "12480003" in pinned_row
    assert "urgent paper" in text and "rerun with 32 cores" in text
    app.run_command("filter #paper"); views.compose(store.snapshot(), app, W, H, actions); assert app.visible_ids == ["12480001"]
    app.run_command("filter"); views.compose(store.snapshot(), app, W, H, actions)
    app.cursor["jobs"] = app.visible_ids.index("12480001"); views.compose(store.snapshot(), app, W, H, actions)
    rows, _ = views.compose(store.snapshot(), app, W, H, actions)
    assert "tags #urgent #paper" in "\n".join(L.row_text(r) for r in rows)
    app.handle("p"); assert store.pinned("12480001") and app.message == "pinned 12480001"
    app.handle("p"); assert not store.pinned("12480001")
    app.run_command("untag 12480001 urgent"); assert store.tags_of("12480001") == ["paper"]
    app.run_command("tag 12476001 old"); views.compose(store.snapshot(), app, W, H, actions)             # a finished job
    app.handle("3"); rows, _ = views.compose(store.snapshot(), app, W, H, actions)
    assert "old" in next(L.row_text(r) for r in rows if "12476001" in L.row_text(r))
    app.run_command("filter #old"); views.compose(store.snapshot(), app, W, H, actions)
    app.run_command("filter")
    # A restart includes the shutdown publication barrier. The interactive
    # writer is asynchronous; keep this Store for the later comparison checks.
    store.configure_persistence_writer(None)
    st2 = Store(state_dir=str(tmp_path / "state"))                                                    # tags survive a restart
    assert st2.tags_of("12480001") == ["paper"] and st2.pinned("12480003") and st2.tags["12480001"]["note"] == "rerun with 32 cores"
    from raven.expr import Expr, cluster_ns
    assert Expr('[j.id for j in jobs if "paper" in j.tags]')(cluster_ns(store.snapshot(), "alex", tags=store.tag_map())) == ["12480001"]
    # compare
    app.handle("1"); app.marks = {"12480001", "12477369"}
    app.run_command("compare")
    assert app.tab == "analytics" and app.analytics_view == "compare" and app.compare_ids == ["12477369", "12480001"]
    app.run_command("history-dock off")  # Keep the legacy full-width comparison and Advisor assertions.
    rows, _ = views.compose(store.snapshot(), app, 160, 50, actions)
    text = "\n".join(L.row_text(r) for r in rows)
    assert "compare 2 jobs" in text and "CPU MEAN" in text and "cpu per core, aligned" in text and "memory (GB), aligned" in text and "gpu utilisation, aligned" in text
    assert text.count("rb-setup") >= 3 and all(L.vlen(L.row_text(r)) <= 160 for r in rows) and len(rows) == 50
    app.run_command("compare clear"); assert app.message == "compare <ids> or mark jobs first" or app.compare_ids == []
    app.marks = set(); app.compare_ids = []
    rows, _ = views.compose(store.snapshot(), app, 160, 50, actions)
    assert "mark two or more jobs" in "\n".join(L.row_text(r) for r in rows)
    # the advisor view and command
    app.analytics_view = "advisor"
    rows, _ = views.compose(store.snapshot(), app, 160, 50, actions)
    text = "\n".join(L.row_text(r) for r in rows)
    assert "advisor: what the jobs" in text and "rb3-identity" in text and "1 out of memory" in text and "running jobs so far" in text and "--mem" in text
    assert app.advise("12475990").startswith("12475990 rb-setup (timeout): --mem 3G") and "--time=03:00:00" in app.advise("rb-setup") and app.advise("nope").startswith("no finished run")
    app.run_command("advise 12480001"); assert app.message.startswith("12480001 rb1-allrank so far: --mem")
    env = dict(os.environ, COLUMNS="150", USER="alex")
    r = subprocess.run([sys.executable, DASH, "--fake", "--no-state", "run", "advise", "rb3-identity"], env=env, capture_output=True, text=True)
    assert r.returncode == 0 and "--mem=64G" in r.stdout


# ------------------------------------------------------------------------------------------------ layer D: clone and resubmit
def test_resubmit_flags_overrides_and_clone_building():
    from raven.resubmit import apply_overrides, build, flags_of, output_pattern, parse_override_args, parse_submit_info, parse_submitted, split_flags
    argv = ["-J", "rb1", "-p", "main", "--mem=64G", "-c", "32", "-t", "12:00:00", "--gres=gpu:a100:1", "--output=logs/%x-%j.out", "jobs/rb1.sbatch", "--extra"]
    flags, rest = split_flags(argv)
    assert [k for k, _, _ in flags] == ["name", "partition", "mem", "cpus", "time", "gres", "output"] and rest == ["jobs/rb1.sbatch", "--extra"]
    assert apply_overrides(argv, {"mem": "12G", "time": "03:00:00", "cpus": "4", "nodes": "2"}) == \
        ["-J", "rb1", "-p", "main", "--mem=12G", "--cpus-per-task=4", "--time=03:00:00", "--gres=gpu:a100:1", "--output=logs/%x-%j.out", "--nodes=2", "jobs/rb1.sbatch", "--extra"]
    assert flags_of(["-c8", "--mem", "4G", "x.sh"]) == {"cpus": "8", "mem": "4G"}
    assert parse_override_args(["123", "--mem", "12G", "--time=03:00:00", "-c4", "--advised", "-p", "gpu"]) == ({"mem": "12G", "time": "03:00:00", "cpus": "4", "partition": "gpu"}, ["123"], ["advised"])
    assert output_pattern("/x/logs/rb1-allrank-12480001.out", "12480001", "rb1-allrank") == "/x/logs/%x-%j.out"
    info = parse_submit_info("12480001|sbatch --mem=64G jobs/rb1_allrank.sbatch|/home/x|rb1-allrank|main|anak|normal|32|64G|12:00:00|1|cpu=32,mem=64G,node=1\n12480001.batch|||||||||||\n")
    c = build("12480001", info, {}, overrides={"mem": "33G"})
    assert without_resubmit_marker(c.command()) == "sbatch --mem=33G jobs/rb1_allrank.sbatch" and c.source == "submit line" and c.workdir == "/home/x" and any("does not preserve argument boundaries" in note for note in c.notes)
    d = dict(JobName="rb2", Partition="gpu", Account="anak", NumNodes="1", MinMemoryNode="32G", TimeLimit="23:00:00", TresPerNode="gres/gpu:a100:1", Command="/home/x/rb2.sbatch",
             StdOut="/home/x/logs/rb2-12480003.out", StdErr="/home/x/logs/rb2-12480003.out", WorkDir="/home/x", Dependency="afterok:12480001")
    d["CPUs/Task"] = "8"
    c2 = build("12480003", {}, d, overrides={"time": "04:00:00"})
    assert without_resubmit_marker(c2.command()) == "sbatch -J rb2 -p gpu -A anak -c 8 --mem=32G --time=04:00:00 --gres=gpu:a100:1 --output=/home/x/logs/%x-%j.out /home/x/rb2.sbatch" and c2.source == "record"
    c3 = build("12480002_3", {}, dict(JobName="arr", Partition="main"), overrides={})
    assert any("array" in n for n in c3.notes) and any("script path is unknown" in n for n in c3.notes)
    assert parse_submitted("Submitted batch job 12480099\n") == "12480099" and parse_submitted("sbatch: error: invalid partition") is None
    c4 = build("7", {}, dict(JobName="x", Partition="main"), overrides={"script": "run.sh", "mem": "2G"})
    assert c4.argv[-1] == "run.sh" and "--mem=2G" in c4.argv and not any("script path" in n for n in c4.notes)
    assert parse_override_args(["--script", "a.sh", "--mem=1G"])[0] == {"script": "a.sh", "mem": "1G"}


def test_resubmit_through_the_dashboard_and_scripted(tmp_path):
    backend, store, sampler, actions, views, app = make_rich_app(tmp_path, rounds=2)
    W, H = 150, 44
    views.compose(store.snapshot(), app, W, H, actions)
    app.cursor["jobs"] = app.visible_ids.index("12480001"); views.compose(store.snapshot(), app, W, H, actions)
    app.handle("A")
    assert app.mode == "palette" and app.palette_edit == "resubmit 12480001 "
    for ch in "--mem 12G -c 4":
        app.handle(ch if ch != " " else "space")
    app.handle("enter")
    complete_action(app)
    assert app.mode == "confirm" and app.confirm["action"] == "resubmit"
    c = app.confirm["clone"]
    assert without_resubmit_marker(c.command()) == "sbatch --mem=12G --cpus-per-task=4 jobs/rb1_allrank.sbatch" and c.source == "submit line" and c.probe.startswith("sbatch --test-only: would start")
    ov = views.overlay(store.snapshot(), app, W, H)
    text = "\n".join(L.row_text(r) for _, _, r in ov)
    assert "Resubmit 12480001 rb1-allrank?" in text and "--mem=12G --cpus-per-task=4" in text and "would start" in text and "cpus 4  mem 12G" in text
    app.handle("y")
    complete_action(app)
    assert app.message == "resubmitted 12480001 as 12480100"
    assert any(call[:2] == ["sh", "-c"] and without_resubmit_marker(call[2]).endswith("&& sbatch --test-only --mem=12G --cpus-per-task=4 jobs/rb1_allrank.sbatch") for call in backend.calls)
    sent = [c for c in backend.calls if c[:2] == ["sh", "-c"] and "sbatch" in c[2]]
    assert sent and without_resubmit_marker(sent[-1][2]).endswith("&& sbatch --mem=12G --cpus-per-task=4 jobs/rb1_allrank.sbatch") and sent[-1][2].startswith("cd ")
    ev = [e for e in store.events if e.get("action") == "resubmit"][-1]
    assert ev["ok"] and ev["new_id"] == "12480100" and "[sbatch --mem=12G" in ev["text"]
    sampler.round(wait=True)
    assert store.job("12480100") and store.job("12480100").pending and store.job("12480100").cpus == 4 and store.tags_of("12480100") == ["from-12480001"]
    # a finished job from its record, with the advisor's flags
    app.run_command("resubmit 12475990 --advised")
    complete_action(app)
    c = app.confirm["clone"]
    assert c.source == "submit line" and "--mem=3G" in c.argv and "--time=03:00:00" in c.argv and "--cpus-per-task=1" in c.argv and c.argv[-1].endswith("rb_setup.sbatch")
    app.handle("n"); assert app.message == "kept"
    app.run_command("resubmit 999")
    complete_action(app)
    assert app.message.startswith("resubmit: no job 999")
    env = dict(os.environ, COLUMNS="150", USER="alex")
    r = subprocess.run([sys.executable, DASH, "--fake", "--no-state", "run", "resubmit", "12475990", "--mem", "4G", "--time", "02:00:00"], env=env, capture_output=True, text=True)
    assert r.returncode == 3 and "would run in" in r.stdout and "--mem=4G" in r.stdout and "--time=02:00:00" in r.stdout and "add --yes" in r.stdout
    r = subprocess.run([sys.executable, DASH, "--fake", "--no-state", "run", "resubmit", "12480001", "--advised", "--yes"], env=env, capture_output=True, text=True)
    assert r.returncode == 0 and "resubmitted 12480001 as 12480100" in r.stdout and "[sbatch --mem=33G jobs/rb1_allrank.sbatch]" in without_resubmit_marker(r.stdout)


# ------------------------------------------------------------------------------------------------ layer E: the terminal report, replay controls, log extras
def test_terminal_report_from_the_screen_and_the_command_line(tmp_path):
    from raven import report
    backend, store, sampler, actions, views, app = make_rich_app(tmp_path, rounds=3)
    app.run_command("tag 12480001 paper")
    sampled_calls = list(backend.calls)
    page = report.build(store.snapshot(), app, views, actions)
    assert backend.calls == sampled_calls
    assert "RAVEN | COMPLETE TERMINAL REPORT" in page and page.isascii()
    assert "12480001" in page and "rb1-allrank" in page and "queue weather" in page
    assert "allocation of lab_01" in page and "cluster map" in page and "afterok -> 12480003" in page and "#paper" in page and "advisor" in page
    assert "<script" not in page and "<svg" not in page and "\x1b" not in page
    app.run_command("export report")
    assert "background" in app.message and app.research.pending
    app.research.pending[0].result(timeout=30)
    app.tick()
    path = app.message.split()[-1]
    assert path.endswith(".txt") and "09 / SOURCES" in Path(path).read_text() and [e for e in store.events if e["kind"] == "export"][-1]["text"].endswith(path)
    env = dict(os.environ, COLUMNS="150", USER="alex")
    out = tmp_path / "report.txt"
    r = subprocess.run([sys.executable, DASH, "--fake", "--no-state", "--report", str(out)], env=env, capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.strip() == str(out) and "[DEMO]" in out.read_text() and "03 / HISTORY" in out.read_text()
    r = subprocess.run([sys.executable, DASH, "--fake", "--no-state", "--report"], env=env, capture_output=True, text=True)
    assert r.returncode == 0 and "RAVEN | COMPLETE TERMINAL REPORT" in r.stdout and "09 / SOURCES" in r.stdout and r.stdout.isascii()


def test_replay_controls_scrub_bar_and_palette(tmp_path):
    from raven import clock
    from raven.record import ReplayBackend, RecordingBackend
    p = str(tmp_path / "rec.jsonl")
    fb = FakeBackend("alex", speed=100.0)
    rb = RecordingBackend(fb, p, meta=dict(user="alex"))
    slurm = Slurm(rb, "alex")
    for _ in range(3):
        slurm.jobs(); time.sleep(0.25)
    rb.close()
    rp = ReplayBackend(p, paused=True)
    clock.set_source(rp.clock.now)
    try:
        cfg = Config()
        store = Store(state_dir=None, persist=False)
        s2 = Slurm(rp, "alex")
        sampler = Sampler(s2, store, cfg["intervals"], cfg["gpu_types"])
        views = Views(L.Glyphs(True), cfg)
        app = App(store, sampler, None, cfg, "alex", ascii_=True)
        app.replay = rp
        sampler.round(wait=True)
        rows, _ = views.compose(store.snapshot(), app, 150, 40, None)
        text = "\n".join(L.row_text(r) for r in rows)
        assert "REPLAY" in text and "paused" in text and " replay " in L.row_text(rows[3]) and "x1" in L.row_text(rows[3]) and rp.clock.frac == 0.0
        app.handle(">"); assert rp.clock.now() == rp.t1 and app.message == "60 s forward"            # the recording is shorter than a minute: clamped to its end
        app.handle("<"); assert rp.clock.now() == rp.t0
        app.handle("}"); app.handle("}"); assert rp.clock.speed == 4.0 and app.message == "speed x4"
        app.handle("{"); assert rp.clock.speed == 2.0
        app.handle("|"); assert not rp.clock.paused and app.message == "playing"
        app.handle("|"); assert rp.clock.paused
        app.run_command("replay seek 50%"); assert abs(rp.clock.frac - 0.5) < 0.01 and app.message.startswith("at ")
        app.run_command("replay speed 10"); assert rp.clock.speed == 10.0
        hhmm = time.strftime("%H:%M:%S", time.localtime(rp.t0))
        app.run_command(f"replay seek {hhmm}"); assert abs(rp.clock.now() - rp.t0) < 1.5
        app.run_command("replay seek nonsense"); assert app.message.startswith("replay seek <")
        app.run_command("replay"); assert app.message.startswith("replay <pause")
        rows, _ = views.compose(store.snapshot(), app, 150, 40, None)
        assert "x10" in L.row_text(rows[3])
    finally:
        clock.reset()
    app2 = App(Store(state_dir=None, persist=False), None, None, Config(), "alex", ascii_=True)
    app2.handle("|"); assert app2.message.startswith("not replaying")


def test_log_extras_wrap_stderr_other_files_and_bookmarks(tmp_path):
    backend, store, sampler, actions, views, app = make_rich_app(tmp_path, rounds=1)
    logdir = tmp_path / "logs"
    logdir.mkdir()
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        path = logdir / "rb1-allrank-12480001.out"
        path.write_text("".join(f"line {i} " + ("x" * (150 if i == 7 else 0)) + "\n" for i in range(40)))
        (logdir / "rb1-allrank-12480001_0.out").write_text("task zero\n")
        (logdir / "gpu-util-12480001.csv").write_text("2026/10/01 06:00:01.000, 0, 50, 100\n")
        W, H = 100, 24
        def render_log():
            rows, hits = views.compose(store.snapshot(), app, W, H, actions)
            rect = app.history_browser_content_rect
            return _history_content_rows(app, rows), [(y - rect.y, kind, value) for y, kind, value in hits
                                                      if rect.y <= y < rect.y + rect.height]
        views.compose(store.snapshot(), app, W, H, actions)
        app.cursor["jobs"] = app.visible_ids.index("12480001"); views.compose(store.snapshot(), app, W, H, actions)
        app.handle("l"); sampler.select("12480001"); sampler.round(wait=True)
        rows, _ = render_log()
        page = app.logs.page
        body = [L.row_text(r) for r in rows if re.match(r"^[ *]line \d", L.row_text(r))]
        assert len(body) == page and body[-1].strip() == "line 39" and "O files" in L.row_text(app.frame_rows[-1])
        # wrap: the long line 7 takes two rows and the page still has `page` rows ending at the last line
        app.handle("home")
        rows, _ = render_log()
        assert any(L.row_text(r).rstrip().endswith("~") or "x" * 60 in L.row_text(r) for r in rows) and not any(L.row_text(r).strip() == "x" * 50 for r in rows)
        app.handle("w"); assert app.logs.wrap and app.message == "long lines wrapped"
        rows, hits = render_log()
        first = next(y for y, kind, value in hits if kind == "log_line" and value == "0")
        body = [L.row_text(r) for r in rows[first:first + page]]
        assert len(body) == page and L.row_text(rows[first][:-1]).strip() == "line 0" and rows[first][-1][0] == ">" and {"cyan", "bold"} <= set(rows[first][-1][1].split("+")) and sum(1 for b in body if b.startswith(" x")) >= 1 and "wrapped" in L.row_text(rows[first - 1])
        app.handle("end"); rows, _ = render_log()
        assert L.row_text(rows[-1]).strip() == "line 39" and app.logs.following
        app.handle("w"); assert not app.logs.wrap
        # bookmarks: on the current line, then jump, then persisted
        app.handle("m"); assert app.message == "bookmark set at line 40" and app.logs.bookmarks[str(path)] == [39]
        app.handle("home"); app.handle("m"); assert app.logs.bookmarks[str(path)] == [0, 39]
        rows, hits = render_log()
        first = next(y for y, kind, value in hits if kind == "log_line" and value == "0")
        assert L.row_text(rows[first]).startswith("*line 0") and "2 bookmarks" in L.row_text(rows[first - 1])
        app.handle("'"); assert app.message == "bookmark at line 40" and not app.logs.following and app.logs.top == 39 - page + 1 or app.logs.top is not None
        app.handle("'"); assert app.message == "bookmark at line 1" and app.logs.top == 0             # wraps around
        app.handle("m"); assert app.logs.bookmarks[str(path)] == [39] and app.message == "bookmark removed at line 1"
        app.save(); assert store.load_ui()["bookmarks"] == {str(path): [39]} and store.load_ui()["log_wrap"] is False
        app2 = App(store, None, None, Config(), "alex", ascii_=True)
        assert app2.logs.bookmarks == {str(path): [39]}
        # the other files of the job: array task and the GPU trace; then stderr (the same file here)
        app.handle("o"); assert app.logs.file_index == 1 and app.message.endswith("gpu-util-12480001.csv")
        rows, _ = render_log()
        assert any("o: 2 other files" in L.row_text(r) for r in rows)
        assert any("file 2/3" in L.row_text(r) for r in rows) and any("2026/10/01 06:00:01.000, 0, 50, 100" in L.row_text(r) for r in rows)
        app.handle("o"); rows, _ = render_log()
        assert any("file 3/3" in L.row_text(r) for r in rows) and any(L.row_text(r).strip() == "task zero" for r in rows)
        app.handle("o"); assert app.logs.file_index == 0
        app.handle("e"); assert app.logs.which == "err"
        rows, _ = render_log()
        assert any("stderr (the same file as stdout)" in L.row_text(r) for r in rows)
        app.handle("e"); assert app.logs.which == "out"
        app.run_command("wrap"); assert app.logs.wrap
        app.handle("home"); app.run_command("bookmark"); assert app.logs.bookmarks[str(path)] == [0, 39]
    finally:
        os.chdir(cwd)
