#!/usr/bin/env python3
"""Capture completed hosted Python CI without publishing or changing the checkout.

Use a fresh private directory. A successful python-ci.json is written last, only
when the workflow, all five complete jobs, their regression steps and the exact
checked-out tree are proved. Full logs remain local; named excerpts are explicit
subsets whose line ranges and complete-source hashes are retained.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time

REPO = "rahel99x/Raven"
CHECKOUT = Path("/workspace/Raven")
VERSIONS = {"3.10", "3.11", "3.12", "3.13", "3.14"}
SUITE_STEP = "Run isolated regression suite with two workers"
SHA = re.compile(r"[0-9a-f]{40}\Z")
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
TIMESTAMP = re.compile(r"^\ufeff?\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z ")
COUNTS = re.compile(r"(\d+) (passed|failed|skipped|errors?|warnings?|xfailed|xpassed|deselected|rerun)\b")
SUMMARY = re.compile(r"\bin\s+([0-9]+(?:\.[0-9]+)?)s\b")
MAX_LOG_BYTES = 64 << 20


class CaptureError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise CaptureError(message)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def json_bytes(value):
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode()


def atomic_new(path, raw):
    """Publish complete bytes exclusively; a failed retry cannot replace good data."""
    descriptor, temporary = tempfile.mkstemp(prefix=".capture-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        os.unlink(temporary)


def command(argv, *, timeout=120):
    result = subprocess.run(
        argv, cwd=CHECKOUT, capture_output=True, timeout=timeout,
        env={**os.environ, "GH_PAGER": "cat", "PAGER": "cat", "NO_COLOR": "1", "CLICOLOR": "0"},
    )
    require(result.returncode == 0, f"Command failed ({result.returncode}): {argv[0:3]}: {result.stderr.decode(errors='replace')[:800]}")
    return result.stdout


def api(endpoint, output, name):
    raw = command(["gh", "api", "-H", "Accept: application/vnd.github+json", endpoint])
    value = json.loads(raw)
    atomic_new(output / name, raw)
    return value


def log_records(raw, expected_job):
    require(0 < len(raw) <= MAX_LOG_BYTES, "Missing or oversized full job log")
    require(not raw.startswith(b"PK\x03\x04"), "The gh job log is an unexpanded ZIP")
    records = []
    for number, original in enumerate(raw.splitlines(keepends=True), 1):
        line = original.decode("utf-8").rstrip("\r\n")
        parts = line.split("\t", 2)
        require(len(parts) == 3 and parts[0] == expected_job,
                f"Unexpected or unscoped full-log row {number}")
        content = ANSI.sub("", TIMESTAMP.sub("", parts[2])).strip("\ufeff")
        records.append({"number": number, "step": parts[1], "content": content, "raw": original})
    require(any(row["step"] == "Complete job" for row in records), "Full job log has no completion step")
    return records


def parse_full_suite(raw, expected_job):
    records = log_records(raw, expected_job)
    suite = [row for row in records if row["step"] == SUITE_STEP]
    require(suite, "Missing exact isolated full-regression step")
    summaries = []
    for row in suite:
        content = row["content"].strip().strip("=").strip()
        duration = SUMMARY.search(content)
        if duration and re.search(r"\d+ (?:passed|failed|errors?)\b", content):
            count_pairs = COUNTS.findall(content)
            require(count_pairs and len({kind for _, kind in count_pairs}) == len(count_pairs), "Ambiguous pytest summary counts")
            counts = {"passed": 0, "failed": 0, "skipped": 0, "errors": 0, "warnings": 0, "xfailed": 0, "xpassed": 0, "deselected": 0, "rerun": 0}
            for value, kind in count_pairs:
                kind = {"error": "errors", "warning": "warnings"}.get(kind, kind)
                counts[kind] = int(value)
            summaries.append({**counts, "seconds": float(duration.group(1)), "summary": content, "summary_line": row["number"]})
    require(len(summaries) == 1, f"Expected one complete full-suite summary, got {len(summaries)}")
    checkout = []
    for index, row in enumerate(records):
        if "actions/checkout@" in row["step"] and row["content"].endswith("git log -1 --format=%H"):
            require(index + 1 < len(records), "Checkout log ends before checked-out revision")
            revision = records[index + 1]
            require(revision["step"] == row["step"] and SHA.fullmatch(revision["content"]), "Missing exact checkout revision after git log")
            checkout.append(revision["content"])
    require(len(checkout) == 1, f"Expected one checkout revision, got {len(checkout)}")
    result = {**summaries[0], "step": SUITE_STEP, "tested_merge": checkout[0],
              "source_log_sha256": digest(raw), "source_log_bytes": len(raw),
              "step_log_sha256": digest(b"".join(row["raw"] for row in suite)),
              "step_source_lines": [suite[0]["number"], suite[-1]["number"]]}
    return result, records, suite


def fetch_job_log(run_id, job, output):
    name = f"python-{job['name'][6:-1]}.full.log"
    attempts = []
    for attempt in range(1, 6):
        try:
            result = subprocess.run(
                ["gh", "run", "view", str(run_id), "--repo", REPO, "--job", str(job["id"]), "--log"],
                cwd=CHECKOUT, capture_output=True, timeout=180,
                env={**os.environ, "GH_PAGER": "cat", "PAGER": "cat", "NO_COLOR": "1", "CLICOLOR": "0",
                     "XDG_CACHE_HOME": str(output / f".gh-cache-attempt-{attempt}")},
            )
        except subprocess.TimeoutExpired as error:
            attempts.append({"attempt": attempt, "timed_out": True, "error": str(error)})
            if attempt < 5:
                time.sleep(min(attempt, 4))
                continue
            atomic_new(output / f"python-{job['name'][6:-1]}.download-attempts.json", json_bytes(attempts))
            raise CaptureError(f"Full-log download timed out after five attempts for {job['name']}") from error
        try:
            require(result.returncode == 0, f"gh exited {result.returncode}")
            parsed = parse_full_suite(result.stdout, job["name"])
        except (CaptureError, UnicodeError) as error:
            record = {"attempt": attempt, "exit": result.returncode, "error": str(error), "stdout_bytes": len(result.stdout), "stdout_sha256": digest(result.stdout)}
            if result.stderr:
                error_path = output / f"python-{job['name'][6:-1]}.attempt-{attempt}.stderr.txt"
                atomic_new(error_path, result.stderr)
                record["stderr_path"] = str(error_path)
                record["stderr_sha256"] = digest(result.stderr)
            attempts.append(record)
            if attempt < 5:
                time.sleep(min(attempt, 4))
                continue
            atomic_new(output / f"python-{job['name'][6:-1]}.download-attempts.json", json_bytes(attempts))
            raise CaptureError(f"Unable to retain complete {job['name']} log after five attempts: {error}") from error
        atomic_new(output / name, result.stdout)
        attempts.append({"attempt": attempt, "exit": 0, "complete": True, "bytes": len(result.stdout), "sha256": digest(result.stdout)})
        atomic_new(output / f"python-{job['name'][6:-1]}.download-attempts.json", json_bytes(attempts))
        return output / name, parsed
    raise AssertionError("Unreachable log retry state")


def excerpts(output, version, job, full_path, parsed):
    summary, records, suite = parsed
    step_path = output / f"python-{version}.full-suite-step.log"
    atomic_new(step_path, b"".join(row["raw"] for row in suite))
    keep = set(range(suite[0]["number"], min(suite[0]["number"] + 7, suite[-1]["number"] + 1)))
    keep.update({summary["summary_line"]})
    for index, row in enumerate(records):
        if "actions/checkout@" in row["step"] and row["content"].endswith("git log -1 --format=%H"):
            keep.update({row["number"], records[index + 1]["number"]})
        if row["step"] == "Complete job":
            keep.add(row["number"])
    chosen = [row for row in records if row["number"] in keep]
    excerpt_path = output / f"python-{version}.excerpt.log"
    excerpt_raw = b"".join(row["raw"] for row in chosen)
    atomic_new(excerpt_path, excerpt_raw)
    receipt = {"schema": "raven.hosted-python-log-excerpt/v1", "python": version,
               "url": job["html_url"], "job_id": job["id"], "job_conclusion": job.get("conclusion"), "is_complete_log": False,
               "source_log": str(full_path), "source_log_sha256": summary["source_log_sha256"],
               "source_log_bytes": summary["source_log_bytes"], "excerpt": str(excerpt_path),
               "excerpt_sha256": digest(excerpt_raw), "source_line_numbers": [row["number"] for row in chosen],
               "full_suite": summary, "full_suite_step_log": str(step_path),
               "limits": "Excerpt preserves selected original full-log rows. The full source log and complete regression step remain local; counts describe the one full-suite invocation and are not additive across interpreters."}
    atomic_new(output / f"python-{version}.excerpt.json", json_bytes(receipt))
    return receipt


def tree_receipt(sha, output, role):
    value = api(f"repos/{REPO}/git/commits/{sha}", output, f"{role}-commit-api.json")
    require(value["sha"] == sha and SHA.fullmatch(value["tree"]["sha"]), "Commit API receipt does not match requested object")
    local = subprocess.run(["git", "rev-parse", sha + "^{tree}"], cwd=CHECKOUT, capture_output=True)
    if local.returncode == 0:
        require(local.stdout.decode().strip() == value["tree"]["sha"], "Local commit tree and GitHub API disagree")
    return value


def capture(args, output):
    run = api(f"repos/{REPO}/actions/runs/{args.run}", output, "run-receipt.json")
    require(run["id"] == args.run, "Run receipt identity mismatch")
    require(run["status"] == "completed", "Workflow is not completed")
    require(run["event"] == "pull_request", "Expected pull_request workflow qualification")
    require(run["head_sha"] == args.head, "Workflow head differs from requested PR head")
    require(run["name"] == "tests" and run["path"].split("@", 1)[0] == ".github/workflows/tests.yml", "Not Raven's complete Python test workflow")
    pull_requests = run.get("pull_requests", [])
    require(len(pull_requests) == 1 and pull_requests[0]["head"]["sha"] == args.head and pull_requests[0]["base"]["ref"] == "main", "Run does not bind one requested PR head against main")
    jobs, page = [], 1
    attempt = run["run_attempt"]
    while True:
        data = api(f"repos/{REPO}/actions/runs/{args.run}/attempts/{attempt}/jobs?per_page=100&page={page}", output, f"jobs-page-{page}.json")
        jobs.extend(data["jobs"])
        if len(jobs) >= data["total_count"]:
            require(len(jobs) == data["total_count"], "Inconsistent complete jobs receipt")
            break
        require(data["jobs"] and page < 20, "Incomplete or unbounded jobs pagination")
        page += 1
    selected = []
    for job in jobs:
        match = re.fullmatch(r"test \((3\.(?:10|11|12|13|14))\)", job["name"])
        if match:
            selected.append((match.group(1), job))
    require(len(selected) == 5 and {version for version, _ in selected} == VERSIONS, "Expected exactly five distinct Python matrix jobs")
    python_jobs = []
    for version, listed in sorted(selected):
        job = api(f"repos/{REPO}/actions/jobs/{listed['id']}", output, f"python-{version}.job-receipt.json")
        require(job["id"] == listed["id"] and job["run_id"] == args.run and job["run_attempt"] == attempt and job["name"] == listed["name"], "Job receipt run/attempt identity mismatch")
        require(job["status"] == "completed", f"Python {version} job is not complete")
        suites = [step for step in job["steps"] if step["name"] == SUITE_STEP]
        require(len(suites) == 1 and suites[0]["status"] == "completed", "Missing one completed full-suite step receipt")
        full_path, parsed = fetch_job_log(args.run, job, output)
        excerpts(output, version, job, full_path, parsed)
        python_jobs.append({"python": version, "id": job["id"], "url": job["html_url"],
                            "status": job["status"], "conclusion": job["conclusion"],
                            "full_suite_step_conclusion": suites[0]["conclusion"], "full_suite": parsed[0],
                            "full_log": str(full_path), "excerpt_receipt": str(output / f"python-{version}.excerpt.json"),
                            "raw_job_receipt": str(output / f"python-{version}.job-receipt.json"),
                            "raw_job_receipt_sha256": digest((output / f"python-{version}.job-receipt.json").read_bytes())})
    merges = {job["full_suite"]["tested_merge"] for job in python_jobs}
    require(len(merges) == 1, "Matrix jobs tested different checkout revisions")
    merge = merges.pop()
    head_commit = tree_receipt(args.head, output, "pull-request-head")
    merge_commit = tree_receipt(merge, output, "tested-merge")
    head_tree, merge_tree = head_commit["tree"]["sha"], merge_commit["tree"]["sha"]
    require(merge != args.head and args.head in {parent["sha"] for parent in merge_commit["parents"]}, "Checkout is not a synthetic merge containing the requested PR head")
    require(head_tree == merge_tree, "PR head tree differs from the tested synthetic merge tree")
    result = {"schema": "raven.hosted-python-ci/v1", "run_id": args.run, "run_attempt": attempt,
              "conclusion": run["conclusion"], "status": run["status"], "url": run["html_url"],
              "pull_request_head": args.head, "tested_merge": merge, "head_tree": head_tree,
              "tested_merge_tree": merge_tree, "event": run["event"], "base_branch": "main",
              "captured_at": datetime.now(timezone.utc).isoformat(), "python_jobs": python_jobs,
              "raw_run_receipt": str(output / "run-receipt.json"),
              "raw_run_receipt_sha256": digest((output / "run-receipt.json").read_bytes()),
              "pull_request_head_commit_api_receipt": str(output / "pull-request-head-commit-api.json"),
              "tested_merge_commit_api_receipt": str(output / "tested-merge-commit-api.json"),
              "limits": "Five overlapping complete source regression invocations and subsequent job checks. No live GPU, production SSH or multi-node qualification is implied. Complete logs remain local; named excerpts are subsets bound to their full-log hashes."}
    atomic_new(output / "captured-diagnostic.json", json_bytes(result))
    require(run["conclusion"] == "success", f"Workflow conclusion is {run['conclusion']}; this is not acceptance")
    for job in python_jobs:
        require(job["conclusion"] == job["full_suite_step_conclusion"] == "success", f"Python {job['python']} job or full-suite step did not succeed")
        suite = job["full_suite"]
        require(suite["passed"] > 19000 and suite["failed"] == suite["errors"] == 0, f"Python {job['python']} lacks a complete successful full-suite result")
    atomic_new(output / "python-ci.json", json_bytes(result))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=int, required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    require(args.run > 0 and SHA.fullmatch(args.head), "Use a positive run ID and an exact lowercase 40-character head SHA")
    output = args.output.absolute()
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    try:
        result = capture(args, output)
    except Exception as error:
        atomic_new(output / "capture-status.json", json_bytes({"status": "failed-closed", "error": str(error), "accepted": False}))
        raise
    print(json.dumps({"accepted": True, "run_id": result["run_id"], "head": result["pull_request_head"], "tested_merge": result["tested_merge"], "tree": result["head_tree"], "receipt": str(output / "python-ci.json")}, indent=2))


if __name__ == "__main__":
    main()
