import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

import pytest

from tower.trace_analysis import audit_collectives
from tower.trace_mpi import SOURCE
from tower.trace_native import HEADER
from tower.trace_protocol import discover_trace, read_trace


APP = r'''#include <mpi.h>
#include <stdio.h>
#include <string.h>
int main(int argc, char **argv) {
    int provided = MPI_THREAD_SINGLE;
    int multiple = argc > 1 && !strcmp(argv[1], "multiple");
    if (multiple) MPI_Init_thread(&argc, &argv, MPI_THREAD_MULTIPLE, &provided);
    else MPI_Init(&argc, &argv);
    int rank, input = 3, output = 0;
    MPI_Comm_rank(MPI_COMM_WORLD, &rank);
    MPI_Bcast(&input, 1, MPI_INT, 0, MPI_COMM_WORLD);
    int unsupported = argc > 1 && !strcmp(argv[1], "maximum");
    MPI_Allreduce(&input, &output, 1, MPI_INT, unsupported ? MPI_MAX : MPI_SUM, MPI_COMM_WORLD);
    MPI_Barrier(MPI_COMM_WORLD);
    if (argc > 1 && !strcmp(argv[1], "self")) MPI_Barrier(MPI_COMM_SELF);
    printf("rank=%d result=%d provided=%d\n", rank, output, provided);
    MPI_Finalize();
    return output == (unsupported ? 3 : 6) ? 0 : 1;
}
'''


@pytest.fixture(scope="module")
def mpi_program(tmp_path_factory):
    compiler, runner = shutil.which("mpicc"), shutil.which("mpirun")
    if not compiler or not runner:
        pytest.skip("Real MPI compiler/runtime unavailable")
    root = tmp_path_factory.mktemp("trace-mpi-toolchain")
    (root / "tower_trace.h").write_text(HEADER)
    (root / "tower_mpi.c").write_text(SOURCE)
    (root / "app.c").write_text(APP)
    include = os.environ.get("TOWER_TEST_MPI_INCLUDE")
    if include:
        compiler_args = [shutil.which("cc") or "cc", "-I" + include]
        link = shlex.split(os.environ.get("TOWER_TEST_MPI_LINK", "-l:libmpi.so.40"))
    else:
        compiler_args, link = [compiler], []
    library, program = root / "libtower_mpi.so", root / "app"
    args = compiler_args + ["-std=c11", "-D_GNU_SOURCE", "-Wall", "-Wextra", "-Werror", "-shared", "-fPIC",
                            str(root / "tower_mpi.c"), *link, "-o", str(library)]
    compiled = subprocess.run(args, capture_output=True, text=True, timeout=30)
    if compiled.returncode and not include and ("mpi.h" in compiled.stderr or "cannot find -lmpi" in compiled.stderr):
        pytest.skip("MPI runtime exists but development headers/libraries are not installed")
    assert compiled.returncode == 0, compiled.stderr
    compiled = subprocess.run(compiler_args + [str(root / "app.c"), *link, "-o", str(program)], capture_output=True, text=True, timeout=30)
    assert compiled.returncode == 0, compiled.stderr
    return runner, library, program


@pytest.mark.parametrize("mode", ["supported", "maximum", "self", "multiple", "missing-generation"])
def test_real_two_rank_mpi_protocol_capture(tmp_path, mpi_program, mode):
    runner, library, program = mpi_program
    command = [runner, "-n", "2", sys.executable, "-m", "tower.trace_recorder", "native",
               "--project", str(tmp_path), "--job-id", "123", "--attempt", "mpi-test", "--",
               "env", "LD_PRELOAD=" + str(library)]
    if mode != "missing-generation":
        command.append("TOWER_MPI_COMM_GENERATION=verified-test-world")
    command += [str(program), mode]
    env = dict(os.environ)
    env.pop("TOWER_MPI_COMM_GENERATION", None)
    if os.getuid() == 0:
        env.update(OMPI_ALLOW_RUN_AS_ROOT="1", OMPI_ALLOW_RUN_AS_ROOT_CONFIRM="1")
    result = subprocess.run(command, capture_output=True, text=True, env=env, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "rank=0 result=" in result.stdout and "rank=1 result=" in result.stdout
    traces = [read_trace(path) for path in discover_trace(tmp_path, "123", "mpi-test")]
    assert len(traces) == 2
    events = [event for trace in traces for event in trace["events"]]
    audit = audit_collectives(events)
    if mode == "supported":
        assert all(trace["complete"] for trace in traces)
        assert audit["status"] == "MATCH", audit
        assert len(audit["data"]["collectives"]) == 3
        assert {finding["operation"] for finding in audit["data"]["collectives"]} == {"ALLREDUCE", "BCAST", "BARRIER"}
    else:
        assert not any(trace["complete"] for trace in traces)
        assert any(event["kind"] == "gap" for event in events)
        assert audit["status"] != "MATCH"


def test_source_command_prints_only_explicit_adapter():
    result = subprocess.run([sys.executable, "-m", "tower.trace_recorder", "mpi", "--source"], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0
    assert "PMPI_Allreduce" in result.stdout and "MPI_THREAD_MULTIPLE" in result.stdout
    assert "MPI_Send(" not in result.stdout
