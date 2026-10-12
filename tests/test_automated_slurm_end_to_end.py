"""The installed entry layers launch a real shell through a simulated sbatch.

This models acceptance/fast-start ordering; it is not a real Slurm pilot.
"""
import csv
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from raven import projects
from raven.research import ResearchHub


@pytest.fixture
def lab(tmp_path):
    root = tmp_path / "LabX project"
    scratch = tmp_path / "scratch work"
    binaries = tmp_path / "bin"
    for path in (root, scratch, binaries):
        path.mkdir()
    batch = root / "experiment.sh"
    batch.write_text("""#!/bin/bash
#SBATCH --job-name=lab-experiment
#SBATCH --output=experiment-%j.out
set -eu
printf '%s\\n' "scientific output"
"$RAVEN_REPORT_PYTHON" "$RAVEN_REPORT_HELPER" metric --run "$RAVEN_RUN_DIR" --value loss=0.125 --step 1
"$RAVEN_REPORT_PYTHON" -c 'import json,os,sys; json.dump({"argv":sys.argv[1:],"cwd":os.getcwd(),"account":os.environ.get("SLURM_JOB_ACCOUNT")},open(os.path.join(os.environ["RAVEN_RUN_DIR"],"outputs","observed.json"),"w"))' "$@"
""")
    fake = binaries / "sbatch"
    fake.write_text("#!" + sys.executable + "\n" + r'''
import json, os, pathlib, subprocess, sys
args = sys.argv[1:]
batch_index = next(i for i,arg in enumerate(args) if not arg.startswith("-"))
batch, workload_args = args[batch_index], args[batch_index+1:]
root = pathlib.Path(os.environ["FAKE_PROJECT"])
with (root/"submissions.jsonl").open("a") as out:
    out.write(json.dumps(args)+"\n")
env = dict(os.environ, SLURM_JOB_ID="8123", SLURM_CLUSTER_NAME="lab-cluster",
           SLURM_RESTART_COUNT="0", SLURM_SUBMIT_DIR=os.getcwd(),
           SLURM_JOB_ACCOUNT="LabX", SLURM_JOB_NAME="lab-experiment")
workdir = next((arg.split("=",1)[1] for arg in args[:batch_index] if arg.startswith("--chdir=")), os.getcwd())
with (root/"scheduler-stdout.log").open("wb") as stdout, (root/"scheduler-stderr.log").open("wb") as stderr:
    result = subprocess.run([batch,*workload_args],cwd=workdir,env=env,stdout=stdout,stderr=stderr)
(root/"scheduler-application-exit.json").write_text(json.dumps(result.returncode))
print("accepted but response incomplete" if os.environ.get("FAKE_AMBIGUOUS") else "8123;lab-cluster")
''')
    fake.chmod(0o700)
    env = {key:value for key,value in os.environ.items() if not key.startswith(("RAVEN_", "SLURM_", "SBATCH_"))}
    env.update(PATH=str(binaries)+os.pathsep+env.get("PATH",""),FAKE_PROJECT=str(root),
               XDG_CONFIG_HOME=str(tmp_path/"config"),XDG_STATE_HOME=str(tmp_path/"state"))
    return root,scratch,batch,env


def invoke(lab, *options):
    root,scratch,batch,env = lab
    return subprocess.run([sys.executable,"-m","raven","launch",str(batch),"--project",str(root),
                           "--account","LabX","--sbatch=--chdir="+str(scratch),
                           "--research","experiment","submit","--export-on-finish","--json",
                           *options, "--", "", "a b", "λ", "$(literal)", "--dash"],
                          cwd=root,env=env,capture_output=True,text=True,timeout=30)


def test_cli_fast_start_preserves_args_and_reopens_exact_attempt(lab):
    root,scratch,_,_ = lab
    result = invoke(lab)
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["job_id"] == "8123"
    submissions = [json.loads(row) for row in (root/"submissions.jsonl").read_text().splitlines()]
    assert len(submissions) == 1
    run = Path(receipt["run_dir"])
    observed = json.loads((run/"outputs/observed.json").read_text())
    assert observed == {"argv":["","a b","λ","$(literal)","--dash"],"cwd":str(scratch),"account":"LabX"}
    assert (root/"scheduler-stdout.log").read_text() == "scientific output\n"
    assert json.loads((root/"scheduler-application-exit.json").read_text()) == 0
    manifest = json.loads((run/"run.json").read_text())
    assert manifest["state"] == "COMPLETED"
    assert manifest["metadata"]["raven_launch"]["submission_token"] == receipt["token"]
    # A fresh reader attaches after both launcher and supervisor have exited.
    found = projects.discover_job([str(root)],"8123",cluster="lab-cluster",restart_count=0)
    assert found["selected"]["binding"]["run_id"] == run.name
    hub = ResearchHub({})
    try:
        binding = found["selected"]["binding"]
        viewed = hub._read({"view":"submit","snap":{},"binding":binding,
                           "settings":{"submit_file":binding["submit_file"]}})
        assert viewed["launch_report"]["receipt"]["run_id"] == run.name
        assert viewed["display_only"] is True
    finally:
        hub.close()
    bundles = list((root/"exports").rglob("manifest.json"))
    assert len(bundles) == 1
    with (bundles[0].parent/"application-metrics.csv").open() as stream:
        sample, = list(csv.DictReader(stream))
    assert sample["job_id"] == "8123" and sample["run_id"] == run.name
    assert sample["value"] == "0.125"


def test_ambiguous_acceptance_after_start_never_resubmits(lab):
    root,_,_,env = lab
    env["FAKE_AMBIGUOUS"] = "1"
    result = invoke(lab)
    assert result.returncode != 0
    assert len((root/"submissions.jsonl").read_text().splitlines()) == 1
    intents = list((root/".raven/launches").glob("*.json"))
    assert len(intents) == 1
    intent = json.loads(intents[0].read_text())
    assert intent["status"] == "unknown"
    assert intent["observed_job_id"] == "8123"
    assert intent["observed_run_id"]
    assert (root/"runs"/intent["observed_run_id"]/"summary.json").exists()
