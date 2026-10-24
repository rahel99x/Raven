"""Private loopback viewing preserves exact time, ownership and read-only scope."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pytest

from raven.tracing import workspace_browser as B

BASE = (1 << 62) + 17


class Index:
    """A deterministic query port; actual journal integration is checked below."""

    def __init__(self):
        self.lock = threading.RLock()
        self.identity = {"job_id": "42", "attempt": "attempt-1", "cluster": "local"}
        self.closed = False
        self.extra = ""
        self.rows = [
            {
                "id": "src:1",
                "source_id": "src",
                "clock_id": "clock",
                "thread": "main",
                "depth": 0,
                "kind": "python",
                "name": "outer",
                "start_ns": BASE,
                "end_ns": BASE + 1000000,
                "duration_ns": 1000000,
                "self_ns": 600000,
                "first_seq": 1,
                "last_seq": 4,
                "file": "solver.py",
                "line": 4,
                "data": {},
            },
            {
                "id": "src:2",
                "source_id": "src",
                "clock_id": "clock",
                "thread": "worker",
                "depth": 0,
                "kind": "python",
                "name": "<img src=x onerror=alert(1)>",
                "start_ns": BASE + 200000,
                "end_ns": BASE + 600000,
                "duration_ns": 400000,
                "self_ns": 400000,
                "first_seq": 2,
                "last_seq": 3,
                "file": "solver.py",
                "line": 8,
                "data": {"note": "verified"},
            },
        ]

    def catalog(self):
        return {
            "schema": "raven.trace-workspace/v1",
            "identity": dict(self.identity),
            "sources": [
                {
                    "id": "src",
                    "identity": {
                        **self.identity,
                        "node": "node-1",
                        "pid": 2,
                        "role": "main",
                    },
                    "clock": {"id": "clock"},
                    "status": "COMPLETE",
                    "coverage": {"adapter": "fixture", "extra": self.extra},
                    "read_seq": 4,
                    "committed_seq": 4,
                    "warnings": [],
                }
            ],
            "bounds": [
                {"clock_id": "clock", "start_ns": BASE, "end_ns": BASE + 1000000}
            ],
            "warnings": [],
            "caught_up": True,
        }

    def query(self, **filters):
        rows = [dict(row) for row in self.rows]
        if filters.get("search"):
            rows = [
                row for row in rows if filters["search"].lower() in row["name"].lower()
            ]
        if filters.get("thread"):
            rows = [row for row in rows if row["thread"] == filters["thread"]]
        rows = [
            row
            for row in rows
            if row["end_ns"] >= filters.get("start_ns", 0)
            and row["start_ns"] <= filters.get("end_ns", (1 << 63) - 1)
        ]
        return {
            **self.catalog(),
            "intervals": rows[: filters.get("limit", 1000)],
            "markers": [],
            "lanes": [],
            "truncated": False,
            "range": {"start_ns": BASE, "end_ns": BASE + 1000000, "clock_id": "clock"},
        }

    def detail(self, ident):
        return next((dict(row) for row in self.rows if row["id"] == ident), None)

    def overview(self, **filters):
        return {
            "domains": [
                {
                    "clock_id": "clock",
                    "start_ns": BASE,
                    "end_ns": BASE + 1000000,
                    "bins": [{"start_ns": BASE, "end_ns": BASE + 1000000, "count": 2}],
                }
            ],
            "bins": filters.get("bins", 256),
        }

    def functions(self, **filters):
        return {
            "functions": [
                {
                    "source_id": row["source_id"],
                    "name": row["name"],
                    "kind": row["kind"],
                    "file": row["file"],
                    "line": row["line"],
                    "count": 1,
                    "inclusive_ns": row["duration_ns"],
                    "self_ns": row["self_ns"],
                    "min_ns": row["duration_ns"],
                    "max_ns": row["duration_ns"],
                    "self_complete": True,
                }
                for row in self.rows
            ],
            "scope": "Recorded fixture intervals",
            "limitations": [],
            "truncated": False,
        }


@pytest.fixture
def viewer():
    index = Index()
    with B.start(index) as service:
        yield service
    assert not index.closed


def request(viewer, path, *, token=True, headers=None, method="GET"):
    fields = {"Authorization": "Bearer " + viewer.token} if token else {}
    fields.update(headers or {})
    req = Request(viewer.origin + path, headers=fields, method=method)
    try:
        response = urlopen(req, timeout=3)
    except HTTPError as exc:
        response = exc
    with response:
        return response.status, dict(response.headers), response.read()


def test_static_page_contains_application_only_and_strict_csp(viewer):
    status, headers, body = request(viewer, "/", token=False)
    assert status == 200
    assert b"Raven Trace Workspace" in body
    assert b"attempt-1" not in body and viewer.token.encode() not in body
    assert (
        b"<script src=" not in body
        and b"innerHTML" not in body
        and b"eval(" not in body
    )
    assert headers["Cache-Control"] == "no-store"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert headers["X-Frame-Options"] == "DENY"
    assert "script-src 'sha256-" in headers["Content-Security-Policy"]
    assert "connect-src 'self'" in headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert "Access-Control-Allow-Origin" not in headers


@pytest.mark.parametrize("path", ["/", "/?", "/api/catalog", "/api/catalog?"])
def test_empty_query_is_valid_without_relaxing_nonempty_query_validation(viewer, path):
    assert request(viewer, path)[0] == 200


@pytest.mark.parametrize(
    "headers",
    [
        {"Authorization": "Bearer wrong"},
        {"Authorization": "Basic something"},
    ],
)
def test_private_api_rejects_wrong_credentials(viewer, headers):
    assert request(viewer, "/api/catalog", headers=headers)[0] == 401
    assert request(viewer, "/api/catalog", token=False)[0] == 401


@pytest.mark.parametrize(
    "headers",
    [
        {"Host": "attacker.invalid"},
        {"Host": "localhost"},
        {"Origin": "https://attacker.invalid"},
        {"Origin": "null"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},
    ],
)
def test_loopback_host_and_browser_origin_are_exact(viewer, headers):
    assert request(viewer, "/api/catalog", headers=headers)[0] == 403


def test_same_origin_authenticated_queries_preserve_large_integer_ns(viewer):
    status, _, body = request(viewer, "/api/catalog", headers={"Origin": viewer.origin})
    assert status == 200
    catalog = json.loads(body)
    assert catalog["result"]["bounds"][0]["start_ns"] == str(BASE)
    status, _, body = request(viewer, "/api/detail?id=src%3A2")
    assert status == 200
    detail = json.loads(body)["result"]
    assert detail["start_ns"] == str(BASE + 200000)
    assert detail["duration_ns"] == "400000"
    assert detail["name"].startswith("<img")
    assert (
        json.loads(request(viewer, "/api/overview?bins=32")[2])["result"]["bins"] == 32
    )
    assert (
        json.loads(request(viewer, "/api/functions?limit=5")[2])["result"]["functions"][
            0
        ]["inclusive_ns"]
        == "1000000"
    )


@pytest.mark.parametrize(
    "suffix",
    [
        "limit=0",
        "limit=2001",
        "limit=1e2",
        "limit=-1",
        "limit=1&limit=2",
        "limit=true",
        "start_ns=8&end_ns=7",
        "start_ns=18446744073709551616",
        "start_ns=1.5",
        "source_ids=missing",
        "source_ids=src,src",
        "clock_id=missing",
        "projection=raw",
        "path=/etc/passwd",
        "bare",
        "&",
        "search=value&",
        "search=value&&kind=call",
    ],
)
def test_query_bounds_and_unknown_fields_are_rejected(viewer, suffix):
    assert request(viewer, "/api/query?" + suffix)[0] == 400


@pytest.mark.parametrize(
    "method", ["POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]
)
def test_server_has_no_control_or_mutation_routes(viewer, method):
    assert request(viewer, "/api/catalog", method=method)[0] == 405


def test_route_and_output_budgets_fail_explicitly(viewer):
    assert request(viewer, "/etc/passwd")[0] == 404
    assert request(viewer, "/api/detail?id=missing")[0] == 404
    assert request(viewer, "/api/overview?bins=10000")[0] == 400
    viewer.index.extra = "x" * (B.MAX_RESPONSE_BYTES + 1)
    status, _, body = request(viewer, "/api/catalog")
    assert status == 413 and b"byte budget" in body


def test_old_session_never_reads_a_replaced_attempt(viewer):
    assert request(viewer, "/api/catalog")[0] == 200
    viewer.index.identity["attempt"] = "attempt-2"
    status, _, body = request(viewer, "/api/catalog")
    assert status == 409 and b"identity changed" in body


def test_close_revokes_service_and_keeps_caller_index_open():
    index = Index()
    first = B.start(index)
    second = B.start(index)
    assert first.token != second.token
    assert first.session_id != second.session_id
    assert request(first, "/api/catalog")[0] == 200
    first.close()
    first.close()
    with pytest.raises((URLError, OSError)):
        urlopen(first.origin + "/", timeout=1)
    assert request(second, "/api/catalog")[0] == 200
    second.close()
    assert not index.closed


def test_browser_open_request_reports_headless_and_ssh_without_launch(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("headless viewing tried to open a graphical browser")

    monkeypatch.setattr(B.subprocess, "Popen", forbidden)
    assert "SSH host" in B._open("http://127.0.0.1:1/", {"SSH_CONNECTION": "yes"})
    assert "No graphical desktop" in B._open("http://127.0.0.1:1/", {})


def test_wire_rejects_nonfinite_values_and_keeps_timestamp_units():
    assert json.loads(
        B._json({"at_ns": BASE, "count": BASE, "small": 4, "flag": True})
    ) == {"at_ns": str(BASE), "count": str(BASE), "small": 4, "flag": True}
    with pytest.raises(ValueError):
        B._json({"duration_ns": float("nan")})


def browser_prerequisites():
    node = shutil.which("node")
    chromium = os.environ.get("RAVEN_BROWSER_EXECUTABLE") or next(
        (
            shutil.which(name)
            for name in (
                "chromium",
                "chromium-browser",
                "google-chrome",
                "google-chrome-stable",
            )
            if shutil.which(name)
        ),
        None,
    )

    def unavailable(reason):
        if os.environ.get("RAVEN_REQUIRE_BROWSER") == "1":
            pytest.fail(reason)
        pytest.skip(reason)

    if not node or not chromium:
        unavailable("real browser qualification needs Node and Chromium")
    if not os.path.isfile(chromium) or not os.access(chromium, os.X_OK):
        unavailable("the configured Chromium executable is unavailable")
    result = subprocess.run(
        [node, "-e", "require.resolve('playwright')"], capture_output=True, timeout=10
    )
    if result.returncode:
        unavailable("real browser qualification needs the Playwright Node package")
    return node, chromium


def test_real_browser_range_selection_hover_live_fit_and_hostile_labels(
    viewer, tmp_path
):
    node, chromium = browser_prerequisites()
    script = tmp_path / "browser.cjs"
    script.write_text(r"""
const {chromium}=require('playwright');
(async()=>{
 const browser=await chromium.launch({executablePath:process.argv[2],headless:true,args:['--no-sandbox','--disable-dev-shm-usage']});
 const page=await browser.newPage({viewport:{width:1200,height:1000}});const errors=[];
 page.on('pageerror',error=>errors.push(error.message));
 try{
  await page.goto(process.argv[3]);
  await page.locator('#event_body button').first().waitFor();
  if(await page.locator('#event_body button').count()!==2)throw Error('Expected both recorded threads');
  if(await page.locator('img').count())throw Error('Hostile label created DOM');
  await page.locator('#event_body button').filter({hasText:'outer'}).click();
  await page.waitForFunction(()=>document.getElementById('details').dataset.intervalId==='src:1');
  if(!(await page.locator('#details').innerText()).includes('4611686018427387921 ns'))throw Error('Absolute nanoseconds lost precision');
  await page.locator('#compare').click();
  await page.locator('#event_body button').nth(1).click();
  if(!(await page.locator('#comparison').innerText()).includes('-600 µs'))throw Error('Duration comparison wrong');
  await page.locator('#timeline').scrollIntoViewIfNeeded();const canvas=await page.locator('#timeline').boundingBox();await page.mouse.move(canvas.x+canvas.width*.55,canvas.y+47);
  await page.locator('#tooltip:not([hidden])').waitFor();
  if(!(await page.locator('#tooltip').innerText()).includes('Recorded duration:'))throw Error('No hover timing');
  await page.locator('#live').click();
  await page.waitForFunction(()=>document.getElementById('live').getAttribute('aria-pressed')==='true');
  await page.locator('#fit').click();
  await page.waitForFunction(()=>document.getElementById('live').getAttribute('aria-pressed')==='false');
  await page.locator('#search').fill('outer');
  await page.waitForFunction(()=>document.querySelectorAll('#event_body button').length===1);
  await page.locator('#search').fill('');await page.waitForFunction(()=>document.querySelectorAll('#event_body button').length===2);
  const before=await page.locator('#window').innerText();
  await page.locator('#zoom_in').click();await page.waitForTimeout(200);
  const zoomed=await page.locator('#window').innerText();if(before===zoomed)throw Error('Zoom did not change range');
  await page.locator('#details').click();if((await page.locator('#window').innerText())!==zoomed)throw Error('Unrelated details click moved timeline');
  let heldTreeResolve,releaseTree;const heldTree=new Promise(resolve=>heldTreeResolve=resolve);
  await page.route('**/api/tree?*',async route=>{if(!releaseTree){releaseTree=()=>route.continue();heldTreeResolve()}else await route.continue()});
  await page.locator('#tree_section summary').click();await heldTree;
  await page.locator('#tree_direction').selectOption('1');await page.waitForFunction(()=>document.getElementById('tree_scope').textContent.startsWith('bottom-up'));
  await releaseTree();await page.waitForTimeout(150);
  if(!(await page.locator('#tree_scope').innerText()).startsWith('bottom-up'))throw Error('Late top-down response replaced newer tree selection');
  await page.waitForFunction(()=>Number(document.getElementById('flame').dataset.visibleNodes)>0);
  await page.locator('#event_body button').filter({hasText:'outer'}).click();
  await page.waitForFunction(()=>document.getElementById('details').dataset.intervalId==='src:1');
  await page.locator('#view_link').click();const exactLink=await page.locator('#view_link_value').inputValue();
  if(!exactLink.includes('event=src%3A1')||!exactLink.includes('token='))throw Error('View link omitted exact event or credential');
  const linked=await browser.newPage();await linked.goto(exactLink);await linked.waitForFunction(()=>document.getElementById('details').dataset.intervalId==='src:1');
  if((await linked.locator('#window').innerText())!==(await page.locator('#window').innerText()))throw Error('View link did not restore exact range');
  await linked.close();
  if(errors.length)throw Error(errors.join('\n'));
  console.log(JSON.stringify({passed:true,threads:2,exactNs:true,hover:true,selection:true,liveFit:true,search:true,errors}));
 }finally{await browser.close()}
})().catch(error=>{console.error(error);process.exit(1)});
""")
    result = subprocess.run(
        [node, str(script), chromium, viewer.url],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["passed"] is True


def test_real_journal_index_arrivals_and_completion_are_visible_without_scheduler(
    tmp_path,
):
    from raven.tracing.capture import Recorder
    from raven.tracing.workspace_index import TraceIndex

    (tmp_path / "project").mkdir(mode=0o700)
    base = time.monotonic_ns()
    recorder = Recorder(
        tmp_path / "project",
        job_id="42",
        attempt="attempt-1",
        cluster="local",
        initial_mono_ns=base,
        background_flush=False,
    )
    try:
        recorder.emit(
            "call",
            {"frame": "first", "function": "first"},
            mono_ns=base + 10,
            thread="main",
        )
        recorder.emit("return", {"frame": "first"}, mono_ns=base + 20, thread="main")
        recorder.flush()
        with TraceIndex(tmp_path / "cache" / "index.sqlite") as index:
            index.refresh(
                [recorder.path],
                expected={"job_id": "42", "attempt": "attempt-1", "cluster": "local"},
                max_events=1024,
                max_seconds=1,
            )
            with B.start(index) as service:
                catalog = json.loads(request(service, "/api/catalog")[2])["result"]
                assert catalog["sources"][0]["status"] == "LIVE"
                first = json.loads(request(service, "/api/query?search=first")[2])[
                    "result"
                ]
                assert [row["name"] for row in first["intervals"]] == ["first"]
                assert first["intervals"][0]["duration_ns"] == "10"
                recorder.emit(
                    "call",
                    {"frame": "later", "function": "later"},
                    mono_ns=base + 30,
                    thread="worker",
                )
                recorder.emit(
                    "return", {"frame": "later"}, mono_ns=base + 50, thread="worker"
                )
                recorder.flush()
                index.refresh([recorder.path], max_events=1024, max_seconds=1)
                later = json.loads(request(service, "/api/query?search=later")[2])[
                    "result"
                ]
                assert [row["name"] for row in later["intervals"]] == ["later"]
                assert later["intervals"][0]["duration_ns"] == "20"
                detail = json.loads(
                    request(
                        service,
                        "/api/detail?" + urlencode({"id": later["intervals"][0]["id"]}),
                    )[2]
                )["result"]
                assert detail["start_ns"] == str(base + 30)
                recorder.close()
                index.refresh([recorder.path], max_events=1024, max_seconds=1)
                completed = json.loads(request(service, "/api/catalog")[2])["result"]
                assert completed["sources"][0]["status"] == "COMPLETE"
                assert (
                    json.loads(
                        request(
                            service, "/api/detail?" + urlencode({"id": detail["id"]})
                        )[2]
                    )["result"]["id"]
                    == detail["id"]
                )
    finally:
        recorder.close()


def test_registered_baseline_identity_and_comparison_context_are_bounded():
    index, baseline = Index(), Index()
    baseline.identity["attempt"] = "baseline-attempt"
    with B.start(
        index,
        baseline=baseline,
        comparison_context={
            "schema": "raven.trace-comparison-context/v1",
            "workload": "same-input",
        },
    ) as service:
        catalog = json.loads(request(service, "/api/catalog")[2])["result"]
        assert catalog["comparison"] == {
            "configured": True,
            "baseline_identity": baseline.identity,
        }
        baseline.identity["attempt"] = "different-baseline"
        assert request(service, "/api/catalog")[0] == 409
    assert not baseline.closed and not index.closed
    with pytest.raises(ValueError, match="object"):
        B.start(index, comparison_context="not-an-object")
    with pytest.raises(ValueError):
        B.start(index, comparison_context={"bad": float("nan")})
    with pytest.raises(ValueError):
        B.start(index, comparison_context={"huge": "x" * 100000})


def test_function_range_requests_forward_exact_clock_scope(viewer):
    calls = []
    original = viewer.index.functions

    def recorded(**values):
        calls.append(values)
        return original(**values)

    viewer.index.functions = recorded
    status, _, _ = request(
        viewer,
        "/api/functions?clock_id=clock&start_ns=7&end_ns=9&search=outer&thread=main",
    )
    assert status == 200
    assert calls == [
        {
            "clock_id": "clock",
            "start_ns": 7,
            "end_ns": 9,
            "search": "outer",
            "thread": "main",
            "limit": 100,
        }
    ]
    assert request(viewer, "/api/functions?clock_id=unrelated")[0] == 400


def test_actual_imported_gpu_and_flow_points_reach_browser_canvas(tmp_path):
    node, chromium = browser_prerequisites()
    from raven.tracing.activity_import import import_activity
    from raven.tracing.workspace_index import TraceIndex

    project = tmp_path / "gpu-project"
    project.mkdir(mode=0o700)
    source = tmp_path / "activities.json"
    source.write_text(
        json.dumps(
            {
                "traceEvents": [
                    {
                        "ph": "X",
                        "ts": 4,
                        "dur": 2,
                        "cat": "cuda_runtime",
                        "name": "launch",
                        "pid": 1,
                        "tid": 2,
                    },
                    {
                        "ph": "s",
                        "ts": 6,
                        "cat": "launch",
                        "id": "flow-one",
                        "name": "launch-link",
                        "pid": 1,
                        "tid": 2,
                        "args": {"node": "node-a"},
                    },
                    {
                        "ph": "X",
                        "ts": 8,
                        "dur": 4,
                        "cat": "kernel",
                        "name": "gpu-a-kernel",
                        "pid": 2,
                        "tid": 3,
                        "args": {
                            "node": "node-a",
                            "device": 0,
                            "device_uuid": "GPU-A",
                            "stream": 1,
                        },
                    },
                    {
                        "ph": "f",
                        "ts": 9,
                        "cat": "launch",
                        "id": "flow-one",
                        "name": "launch-link",
                        "pid": 2,
                        "tid": 3,
                        "args": {"node": "node-a"},
                    },
                    {
                        "ph": "C",
                        "ts": 9.5,
                        "name": "memory",
                        "pid": 2,
                        "tid": 3,
                        "args": {"bytes": 4096},
                    },
                    {
                        "ph": "X",
                        "ts": 10,
                        "dur": 3,
                        "cat": "kernel",
                        "name": "gpu-b-kernel",
                        "pid": 4,
                        "tid": 5,
                        "args": {
                            "node": "node-b",
                            "device": 0,
                            "device_uuid": "GPU-B",
                            "stream": 2,
                        },
                    },
                ]
            }
        )
    )
    imported = import_activity(
        source, project=project, job_id="42", attempt="attempt-1", cluster="local"
    )
    with TraceIndex(tmp_path / "gpu-cache" / "index.sqlite") as index:
        index.refresh([imported["manifest"]], max_events=1024, max_seconds=1)
        with B.start(index) as service:
            status, _, body = request(service, "/api/flows")
            assert status == 200
            flow = json.loads(body)["result"]
            assert len(flow["links"]) == 1
            assert flow["links"][0]["duration_ns"] == "3000"
            query = json.loads(request(service, "/api/query?projection=gpu")[2])[
                "result"
            ]
            assert {row["name"] for row in query["intervals"]} == {
                "gpu-a-kernel",
                "gpu-b-kernel",
            }
            assert {row["node"] for row in query["lanes"]} == {"node-a", "node-b"}
            filtered = json.loads(
                request(
                    service,
                    "/api/query?node=node-b&process=4&thread=5&kind=gpu_kernel&limit=1",
                )[2]
            )["result"]
            assert [row["name"] for row in filtered["intervals"]] == ["gpu-b-kernel"]
            assert not filtered["truncated"]
            script = tmp_path / "gpu-browser.cjs"
            script.write_text(r"""
const {chromium}=require('playwright');
(async()=>{const browser=await chromium.launch({executablePath:process.argv[2],headless:true,args:['--no-sandbox','--disable-dev-shm-usage']});
try{const page=await browser.newPage({viewport:{width:1400,height:1000}});const errors=[];page.on('pageerror',error=>errors.push(error.message));
await page.goto(process.argv[3]);await page.locator('#event_body button').first().waitFor();
await page.locator('#projection').selectOption('gpu');await page.waitForFunction(()=>document.querySelectorAll('#event_body button').length===2);
if(!(await page.locator('#lane_body').innerText()).includes('node-a')||!(await page.locator('#lane_body').innerText()).includes('node-b'))throw Error('Reported node hierarchy missing');
await page.locator('#filters_section summary').click();
await page.locator('#filter_node').selectOption('node-b');await page.waitForFunction(()=>document.querySelectorAll('#event_body button').length===1&&document.getElementById('event_body').textContent.includes('gpu-b-kernel'));
await page.locator('#filter_process').selectOption('4');await page.locator('#filter_thread').selectOption('5');await page.locator('#filter_kind').selectOption('gpu_kernel');
const deviceValue=await page.locator('#filter_device_id option').filter({hasText:'GPU-B'}).getAttribute('value');await page.locator('#filter_device_id').selectOption(deviceValue);
await page.waitForFunction(()=>document.querySelectorAll('#event_body button').length===1&&document.getElementById('event_body').textContent.includes('gpu-b-kernel'));
if(!(await page.locator('#scope_path').innerText()).includes('node-b'))throw Error('Scope breadcrumb missing');
await page.locator('#clear_filters').click();await page.waitForFunction(()=>document.querySelectorAll('#event_body button').length===2);
await page.locator('#analysis_section summary').click();
await page.waitForFunction(()=>document.querySelectorAll('#counter_select option').length===2);
await page.locator('#counter_select').selectOption({index:1});
await page.waitForFunction(()=>Number(document.getElementById('counterplot').dataset.visiblePoints)>0);
if(!(await page.locator('#analysis_report').innerText()).includes('performance cause unproved')&&!(await page.locator('#analysis_report').innerText()).includes('Suggested checks'))throw Error('Evidence-linked analysis missing');
await page.locator('#flows').check();
await page.waitForFunction(()=>Number(document.getElementById('timeline').dataset.visibleFlows)>0);
await page.waitForFunction(()=>document.querySelectorAll('#flow_body button').length===2);
const names=await page.locator('#event_body').innerText();if(!names.includes('flow event'))throw Error('Explicit flow endpoint observations not accessible');
await page.locator('#flow_section summary').click();
let heldDetailResolve,releaseDetail;const heldDetail=new Promise(resolve=>heldDetailResolve=resolve);
await page.route('**/api/detail?*',async route=>{if(!releaseDetail){releaseDetail=()=>route.continue();heldDetailResolve()}else await route.continue()});
await page.locator('#flow_body button').first().click();await heldDetail;
await page.locator('#event_body button').filter({hasText:'gpu-b-kernel'}).click();
await page.waitForFunction(()=>document.getElementById('details').textContent.includes('gpu-b-kernel'));
await releaseDetail();await page.waitForTimeout(200);
if(!(await page.locator('#details').innerText()).includes('gpu-b-kernel'))throw Error('Late flow navigation overwrote newer kernel selection');
if(errors.length)throw Error(errors.join('\n'));
console.log(JSON.stringify({passed:true,gpuNodes:2,explicitArrows:true,eventsAccessible:true}));
}finally{await browser.close()}})().catch(error=>{console.error(error);process.exit(1)});
""")
            result = subprocess.run(
                [node, str(script), chromium, service.url],
                capture_output=True,
                text=True,
                timeout=45,
            )
            assert result.returncode == 0, result.stdout + result.stderr
            assert json.loads(result.stdout)["explicitArrows"] is True


def test_real_calibrated_browser_chooses_reference_explicitly_and_preserves_raw_detail(
    tmp_path,
):
    node, chromium = browser_prerequisites()
    from raven.tracing.capture import Recorder
    from raven.tracing.workspace_index import TraceIndex

    recordings = []
    for name, anchor in (("node-a", 100), ("node-b", 5000)):
        project = tmp_path / name
        project.mkdir(mode=0o700)
        metadata = {
            "schema": "raven.trace-clock/v1",
            "kind": "monotonic",
            "id": "monotonic",
            "calibration": {
                "schema": "raven.trace-clock-calibration/v1",
                "reference_id": "run-reference",
                "source_anchor_ns": anchor,
                "reference_anchor_ns": 1000,
                "rate_num": 1,
                "rate_den": 1,
                "uncertainty_ns": 5,
                "valid_start_ns": 0,
                "valid_end_ns": 10000,
            },
        }
        rec = Recorder(
            project,
            job_id="42",
            attempt="same-attempt",
            cluster="local",
            coverage={
                "adapter": "cooperative",
                "scope": "deterministic clock fixture",
                "clock": metadata,
            },
            initial_mono_ns=0,
            background_flush=False,
        )
        rec.identity["node"] = name
        rec.emit("call", {"frame": "f", "function": name}, thread="one", mono_ns=anchor)
        rec.emit("return", {"frame": "f"}, thread="one", mono_ns=anchor + 20)
        rec.emit("stream_end", {}, mono_ns=anchor + 21)
        rec.state = "CLOSED"
        rec.flush()
        rec.close()
        recordings.append(rec.path)
    with TraceIndex(tmp_path / "cache" / "index.sqlite") as index:
        for _ in range(10):
            catalog = index.refresh(recordings, max_events=1024, max_seconds=1)
            if catalog["caught_up"]:
                break
        with B.start(index) as service:
            status, _, body = request(service, "/api/query?clock_id=run-reference")
            assert status == 200, (body, index.catalog())
            rows = json.loads(body)["result"]["intervals"]
            assert {row["name"] for row in rows} == {"node-a", "node-b"}
            assert {row["start_ns"] for row in rows} == {"1000"}
            script = tmp_path / "calibrated-browser.cjs"
            script.write_text(r"""
const {chromium}=require('playwright');
(async()=>{const browser=await chromium.launch({executablePath:process.argv[2],headless:true,args:['--no-sandbox','--disable-dev-shm-usage']});
try{const page=await browser.newPage();const errors=[];page.on('pageerror',error=>errors.push(error.message));
await page.goto(process.argv[3]);await page.locator('#event_body button').first().waitFor();
if(await page.locator('#event_body button').count()!==1)throw Error('Default view mixed raw clock domains');
await page.locator('#clock').selectOption('aligned:run-reference');
await page.waitForFunction(()=>document.querySelectorAll('#event_body button').length===2);
await page.locator('#event_body button').filter({hasText:'node-b'}).click();
await page.waitForFunction(()=>document.getElementById('details').textContent.includes('Raw clock:'));
await page.waitForTimeout(200);
const inspector=await page.locator('#details').innerText();
if(!inspector.includes('Clock: run-reference')||!inspector.includes('raw range 5000–5020 ns')||!inspector.includes('Alignment uncertainty: ±5 ns'))throw Error('Reference selection lost raw coordinates or uncertainty');
await page.locator('#view_link').click();if(!(await page.locator('#view_link_value').inputValue()).includes('clock=aligned%3Arun-reference'))throw Error('Shared view lost explicit reference choice');
if(errors.length)throw Error(errors.join('\n'));console.log(JSON.stringify({passed:true,rawDefault:true,calibratedSelection:true}));
}finally{await browser.close()}})().catch(error=>{console.error(error);process.exit(1)});
""")
            result = subprocess.run(
                [node, str(script), chromium, service.url],
                check=False,
                capture_output=True,
                text=True,
                timeout=45,
            )
            assert result.returncode == 0, result.stdout + result.stderr
            assert json.loads(result.stdout)["calibratedSelection"]


def test_comparison_and_analysis_routes_keep_registered_metadata_and_bounded_scope():
    index, baseline = Index(), Index()
    baseline.identity["attempt"] = "reference-attempt"
    descriptor = {
        "workload": "same",
        "input": "same",
        "build": "same",
        "scope": "whole-run",
        "elapsed_ns": 100,
        "elapsed_provenance": "fixture timing",
        "resources": {"count": 1, "unit": "logical-cpu", "model": "same"},
    }
    context = {
        "schema": "raven.trace-comparison-context/v1",
        "provenance": "fixture declaration",
        "workload_comparable": True,
        "inputs_comparable": True,
        "builds_comparable": True,
        "current": descriptor,
        "baseline": descriptor,
    }
    with B.start(index, baseline=baseline, comparison_context=context) as service:
        status, _, body = request(
            service, "/api/analysis?selected_id=src%3A1&clock_id=clock"
        )
        assert status == 200
        result = json.loads(body)["result"]
        assert result["comparison"]["status"] == "comparable"
        assert result["comparison"]["metrics"][1]["value"] == 1
        assert result["causal_slice"]["selected_id"] == "src:1"
        assert result["hints"]["hints"][0]["evidence_ids"]
        assert request(service, "/api/analysis?path=/etc/passwd")[0] == 400
        assert request(service, "/api/tree?bottom_up=unknown")[0] == 400
        assert request(service, "/api/tree?clock_id=clock&bottom_up=1")[0] == 200
    with pytest.raises(ValueError, match="schema"):
        B.start(index, comparison_context={"schema": "untrusted-action/v1"})


def test_real_browser_causal_graph_follows_explicit_dependencies_and_preserves_selection(
    tmp_path,
):
    node, chromium = browser_prerequisites()
    index = Index()
    index.rows[0].update(end_ns=BASE + 100000, duration_ns=100000, self_ns=100000)
    index.rows[1]["data"] = {"depends_on": ["src:1"]}
    with B.start(index) as service:
        script = tmp_path / "causal-browser.cjs"
        script.write_text(r"""
const {chromium}=require('playwright');
(async()=>{const browser=await chromium.launch({executablePath:process.argv[2],headless:true,args:['--no-sandbox','--disable-dev-shm-usage']});
try{const page=await browser.newPage();const errors=[];page.on('pageerror',error=>errors.push(error.message));
await page.goto(process.argv[3]);await page.locator('#event_body button').first().waitFor();
await page.locator('#event_body button').nth(1).click();await page.locator('#analysis_section summary').click();
await page.waitForFunction(()=>Number(document.querySelector('.causal-graph svg')?.dataset.observedNodes)>=2);
const graph=page.locator('.causal-graph svg');if(await graph.locator('line').count()!==1)throw Error('Explicit dependency edge not drawn');
if(await graph.locator('img').count())throw Error('Hostile graph label created HTML');
await graph.getByRole('button',{name:'Inspect outer',exact:true}).click();
await page.waitForFunction(()=>document.getElementById('details').dataset.intervalId==='src:1');
if(!(await page.locator('#details').innerText()).includes('outer'))throw Error('Causal graph navigation selected wrong recorded event');
if(errors.length)throw Error(errors.join('\n'));console.log(JSON.stringify({passed:true,explicitGraph:true,navigation:true}));
}finally{await browser.close()}})().catch(error=>{console.error(error);process.exit(1)});
""")
        result = subprocess.run(
            [node, str(script), chromium, service.url],
            check=False,
            capture_output=True,
            text=True,
            timeout=45,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert json.loads(result.stdout)["explicitGraph"]


def test_retry_later_reader_errors_keep_verified_data_and_recovery_clear_status(viewer):
    viewer.index.viewer_read_error = "recording changed; retry later"
    status, _, body = request(viewer, "/api/catalog")
    assert status == 200
    assert (
        json.loads(body)["result"]["viewer_read_error"]
        == "recording changed; retry later"
    )
    assert json.loads(request(viewer, "/api/query")[2])["result"]["intervals"]
    viewer.index.viewer_read_error = ""
    assert (
        "viewer_read_error"
        not in json.loads(request(viewer, "/api/catalog")[2])["result"]
    )


def test_execution_filters_are_validated_and_forwarded_before_query_limit(viewer):
    calls = []
    original = viewer.index.query

    def recorded(**values):
        calls.append(values)
        return original(**values)

    viewer.index.query = recorded
    values = {
        "node": "node-a",
        "process": "4",
        "thread": "5",
        "logical_cpu": "2",
        "device_id": "GPU-B",
        "kind": "gpu_kernel",
        "task": "work",
        "rank": "0",
        "limit": "1",
    }
    status, _, body = request(viewer, "/api/query?" + urlencode(values))
    assert status == 200, body
    assert calls == [{**values, "logical_cpu": 2, "limit": 1}]
    for value in ("-1", "2147483648", "1e2", "false"):
        assert request(viewer, "/api/query?logical_cpu=" + value)[0] == 400
    assert request(viewer, "/api/query?capacity=/tmp/forbidden")[0] == 400


def test_owner_registered_capacity_enables_cpu_metric_even_in_gpu_perspective():
    index = Index()
    index.rows = [
        {
            **index.rows[0],
            "kind": "scheduling",
            "self_ns": None,
            "data": {"state": "running", "logical_cpu": 0},
        }
    ]
    context = {
        "schema": "raven.trace-resource-context/v1",
        "identity": index.identity,
        "provenance": "explicit deterministic allocation fixture",
        "epochs": [
            {
                "clock_id": "clock",
                "start_ns": BASE,
                "end_ns": BASE + 1000000,
                "logical_cpu_count": 2,
                "unit": "logical-cpu",
                "coverage_complete": True,
                "gpu_coverage_complete": False,
                "source_ids": ["src"],
                "provenance": "fixture",
            }
        ],
    }
    with B.start(index, capacity=context) as service:
        status, _, body = request(service, "/api/query?projection=gpu")
        assert status == 200, body
        result = json.loads(body)["result"]
        assert result["intervals"] == []
        metrics = {row["name"]: row for row in result["metrics"]["metrics"]}
        assert metrics["cpu_allocation_utilization"]["value"] == 0.5
        assert int(metrics["cpu_allocation_utilization"]["numerator"]) == 1000000
        assert int(metrics["cpu_allocation_utilization"]["denominator"]) == 2000000
    invalid = {**context, "execute": "untrusted action"}
    with pytest.raises(ValueError, match="field"):
        B.start(index, capacity=invalid)


def test_actual_sample_recording_reaches_browser_with_unknown_weight_denominator(
    tmp_path,
):
    node, chromium = browser_prerequisites()
    from raven.tracing.capture import Recorder
    from raven.tracing.workspace_index import TraceIndex

    project = tmp_path / "sample-project"
    project.mkdir(mode=0o700)
    with Recorder(
        project,
        job_id="42",
        attempt="samples",
        cluster="local",
        background_flush=False,
        coverage={
            "adapter": "fixture-sample",
            "scope": "Deterministic sample fixture; no hardware sampling qualification",
        },
    ) as recorder:
        for frames in ([{"name": "outer"}, {"name": "leaf"}], []):
            recorder.emit(
                "cpu_sample",
                {
                    "frames": frames,
                    "stack_order": "root-to-leaf",
                    "event": "cycles",
                    "weight": 2,
                    "weight_unit": "event-count",
                    "logical_cpu": 0,
                },
            )
    with TraceIndex(tmp_path / "cache" / "index.sqlite") as index:
        for _ in range(10):
            if index.refresh([recorder.path], max_events=1024, max_seconds=1)[
                "caught_up"
            ]:
                break
        with B.start(index) as service:
            result = json.loads(request(service, "/api/query")[2])["result"]
            samples = result["metrics"]["sample_statistics"]
            assert samples["observations"] == 2 and samples["unavailable_stacks"] == 1
            assert all(row["inclusive_share"] == 0.5 for row in samples["groups"])
            script = tmp_path / "samples-browser.cjs"
            script.write_text(r"""
const {chromium}=require('playwright');
(async()=>{const browser=await chromium.launch({executablePath:process.argv[2],headless:true,args:['--no-sandbox','--disable-dev-shm-usage']});
try{const page=await browser.newPage();const errors=[];page.on('pageerror',error=>errors.push(error.message));
await page.goto(process.argv[3]);await page.locator('#samples_section summary').click();
await page.waitForFunction(()=>document.querySelectorAll('#sample_body tr').length===2);
const scope=await page.locator('#sample_scope').innerText(),rows=await page.locator('#sample_body').innerText();
if(!scope.includes('1 unknown stacks')||!rows.includes('50.00% observed inclusive share')||!rows.includes('unknown stack weight 2'))throw Error('Observed shares lost unknown stack denominator');
if(await page.locator('#sample_body meter').count()!==2)throw Error('Sample share plot missing');
if(errors.length)throw Error(errors.join('\n'));console.log(JSON.stringify({passed:true,observedShares:true,unknownWeightPreserved:true}));
}finally{await browser.close()}})().catch(error=>{console.error(error);process.exit(1)});
""")
            result = subprocess.run(
                [node, str(script), chromium, service.url],
                check=False,
                capture_output=True,
                text=True,
                timeout=45,
            )
            assert result.returncode == 0, result.stdout + result.stderr
            assert json.loads(result.stdout)["unknownWeightPreserved"]


@pytest.mark.parametrize("prior_finished", [None, False])
def test_real_browser_owner_completion_freezes_once_and_explicit_resume_accepts_late_data(
    tmp_path, prior_finished
):
    node, chromium = browser_prerequisites()

    class PublishedLifecycle(Index):
        def __init__(self):
            super().__init__()
            self.finished = prior_finished
            self.live = True

        def catalog(self):
            result = super().catalog()
            result.update(job_finished=self.finished, collection_following=True)
            result["sources"][0]["status"] = "LIVE" if self.live else "COMPLETE"
            result["bounds"][0]["end_ns"] = max(row["end_ns"] for row in self.rows)
            return result

    index = PublishedLifecycle()
    process = None
    with B.start(index) as service:
        script = tmp_path / "completion-browser.cjs"
        script.write_text(r"""
const {chromium}=require('playwright');
(async()=>{const browser=await chromium.launch({executablePath:process.argv[2],headless:true,args:['--no-sandbox','--disable-dev-shm-usage']});
try{const page=await browser.newPage();const errors=[];page.on('pageerror',error=>errors.push(error.message));
await page.goto(process.argv[3]);await page.locator('#event_body button').filter({hasText:'outer'}).waitFor();
await page.locator('#delta').fill('0.0003');await page.locator('#delta').dispatchEvent('change');
await page.locator('#event_body button').filter({hasText:'outer'}).click();
await page.waitForFunction(()=>document.getElementById('details').dataset.intervalId==='src:1');
await page.locator('#live').click();await page.waitForFunction(()=>document.getElementById('live').getAttribute('aria-pressed')==='true');
const held=await page.locator('#window').innerText();console.log(JSON.stringify({ready:true}));
await page.waitForFunction(()=>document.getElementById('live').getAttribute('aria-pressed')==='false');
if((await page.locator('#window').innerText())!==held)throw Error('Job completion moved the chosen viewport');
if(await page.locator('#details').getAttribute('data-interval-id')!=='src:1')throw Error('Job completion lost pinned selection');
console.log(JSON.stringify({paused:true}));await page.waitForTimeout(1200);
if((await page.locator('#window').innerText())!==held)throw Error('Late data moved the completed Explore viewport');
await page.locator('#live').click();await page.locator('#event_body button').filter({hasText:'late_after_completion'}).waitFor();
await page.waitForTimeout(1200);
if(await page.locator('#live').getAttribute('aria-pressed')!=='true')throw Error('Known completion repeatedly overrode an explicit Resume Live');
const completed=await browser.newPage();await completed.goto(process.argv[3]);await completed.locator('#event_body button').first().waitFor();
if(await completed.locator('#live').getAttribute('aria-pressed')!=='false')throw Error('An initially completed job opened in Live');await completed.close();
if(errors.length)throw Error(errors.join('\n'));console.log(JSON.stringify({passed:true,completionPreserved:true,explicitResume:true}));
}finally{await browser.close()}})().catch(error=>{console.error(error);process.exit(1)});
""")
        process = subprocess.Popen(
            [node, str(script), chromium, service.url],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            import select

            for stage in ("ready", "paused"):
                available, _, _ = select.select([process.stdout], [], [], 8)
                assert available, (
                    "Browser did not publish its lifecycle acknowledgement"
                )
                line = process.stdout.readline()
                assert line and json.loads(line)[stage]
                with index.lock:
                    if stage == "ready":
                        index.finished, index.live = True, False
                    else:
                        index.rows.append(
                            {
                                **index.rows[0],
                                "id": "src:late",
                                "name": "late_after_completion",
                                "start_ns": BASE + 2000000,
                                "end_ns": BASE + 2100000,
                                "duration_ns": 100000,
                                "self_ns": 100000,
                            }
                        )
            output, error = process.communicate(timeout=10)
            assert process.returncode == 0, error
            assert json.loads(output)["completionPreserved"]
        finally:
            if process.poll() is None:
                import signal

                os.killpg(process.pid, signal.SIGKILL)
                process.communicate(timeout=3)
