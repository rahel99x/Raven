"""Optional PMPI source adapter for explicitly supported WORLD collectives.

Generated source is compiled with the application's MPI compiler. It is not
loaded by Tower itself and makes no scheduler, payload, or device changes.
"""
from __future__ import annotations

import argparse


SOURCE = r'''/* Tower PMPI adapter: C bindings, MPI_COMM_WORLD, <=256 ranks.
 * Compile with the SAME MPI compiler/runtime as the application:
 * mpicc -std=c11 -D_GNU_SOURCE -fPIC -shared tower_mpi.c -o libtower_mpi.so
 * Set TOWER_MPI_COMM_GENERATION to the same nonempty identifier on every rank.
 * Supported: Barrier, scalar Bcast, scalar Allreduce with MPI_SUM.
 * No added collectives/barriers, no tensor/message payload access.
 * Unsupported communicators/types/operators/thread-multiple emit a gap once.
 */
#include <mpi.h>
#include <stdatomic.h>
#include "tower_trace.h"
static int tower_rank = -1, tower_size = 0, tower_supported = 0;
static _Atomic unsigned long long tower_sequence = 0;
static _Atomic int tower_gap_emitted = 0;
static char tower_members[1200], tower_generation[129];

static void tower_gap(const char *reason) {
    if (!atomic_exchange(&tower_gap_emitted, 1)) {
        char data[512];
        snprintf(data, sizeof(data), "{\"reason\":\"%s\",\"adapter\":\"pmpi-world\"}", reason);
        tower_trace_emit("gap", data);
    }
}

static void tower_mpi_initialize(int provided) {
    const char *generation = getenv("TOWER_MPI_COMM_GENERATION");
    if (!generation || !*generation || strlen(generation) > 128 ||
        strspn(generation, "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-") != strlen(generation)) {
        tower_gap("Missing or invalid shared communicator generation"); return;
    }
    if (provided == MPI_THREAD_MULTIPLE) {
        tower_gap("MPI_THREAD_MULTIPLE ordering is outside this adapter coverage"); return;
    }
    if (PMPI_Comm_rank(MPI_COMM_WORLD, &tower_rank) != MPI_SUCCESS ||
        PMPI_Comm_size(MPI_COMM_WORLD, &tower_size) != MPI_SUCCESS || tower_size < 1 || tower_size > 256) {
        tower_gap("WORLD identity unavailable or exceeds the 256-rank packet budget"); return;
    }
    strcpy(tower_generation, generation);
    size_t used = 0;
    tower_members[used++] = '[';
    for (int rank = 0; rank < tower_size; rank++) {
        int n = snprintf(tower_members + used, sizeof(tower_members) - used, "%s%d", rank ? "," : "", rank);
        if (n < 0 || (size_t)n >= sizeof(tower_members) - used) {
            tower_gap("Communicator member budget exceeded"); return;
        }
        used += (size_t)n;
    }
    tower_members[used++] = ']'; tower_members[used] = 0;
    tower_supported = 1;
    tower_trace_emit("mpi_initialize", "{\"coverage\":\"WORLD Barrier Bcast and scalar Allreduce SUM; C bindings only\"}");
}

static const char *tower_dtype(MPI_Datatype datatype) {
    if (datatype == MPI_FLOAT && sizeof(float) == 4) return "float32";
    if (datatype == MPI_DOUBLE && sizeof(double) == 8) return "float64";
    if (datatype == MPI_INT && sizeof(int) == 4) return "int32";
    if (datatype == MPI_UNSIGNED && sizeof(unsigned int) == 4) return "uint32";
    if (datatype == MPI_LONG_LONG_INT && sizeof(long long) == 8) return "int64";
    if (datatype == MPI_BYTE || datatype == MPI_UNSIGNED_CHAR) return "byte";
    return NULL;
}

static int tower_before(MPI_Comm comm, const char *dtype, int count,
                        unsigned long long *sequence) {
    if (!tower_supported) return 0;
    if (comm != MPI_COMM_WORLD || !dtype || count < 0) {
        tower_gap("Unsupported communicator datatype or count"); return 0;
    }
    *sequence = atomic_fetch_add(&tower_sequence, 1);
    return 1;
}

static void tower_collective(const char *operation, const char *dtype, int count,
                             int root, unsigned long long sequence, const char *phase) {
    char data[2800];
    int n = snprintf(data, sizeof(data),
        "{\"communicator\":\"MPI_COMM_WORLD\",\"generation\":\"%s\",\"sequence\":%llu,"
        "\"operation\":\"%s\",\"members\":%s,\"rank\":%d,\"dtype\":\"%s\","
        "\"count\":%d,\"root\":%d,\"reduction\":\"sum\",\"phase\":\"%s\"}",
        tower_generation, sequence, operation, tower_members, tower_rank, dtype, count, root, phase);
    if (n < 0 || (size_t)n >= sizeof(data)) { tower_gap("Collective metadata budget exceeded"); return; }
    if (tower_trace_emit("collective", data) < 0) tower_gap("Collective transport failed");
}

int MPI_Init(int *argc, char ***argv) {
    int result = PMPI_Init(argc, argv);
    if (result == MPI_SUCCESS) tower_mpi_initialize(MPI_THREAD_SINGLE);
    else tower_gap("MPI_Init failed");
    return result;
}

int MPI_Init_thread(int *argc, char ***argv, int required, int *provided) {
    int result = PMPI_Init_thread(argc, argv, required, provided);
    if (result == MPI_SUCCESS) tower_mpi_initialize(*provided);
    else tower_gap("MPI_Init_thread failed");
    return result;
}

int MPI_Finalize(void) {
    tower_trace_emit("mpi_finalize", "{}");
    return PMPI_Finalize();
}

int MPI_Barrier(MPI_Comm comm) {
    unsigned long long sequence = 0;
    int enabled = tower_before(comm, "byte", 0, &sequence);
    if (enabled) tower_collective("barrier", "byte", 0, 0, sequence, "enter");
    int result = PMPI_Barrier(comm);
    if (enabled && result == MPI_SUCCESS) tower_collective("barrier", "byte", 0, 0, sequence, "complete");
    else if (result != MPI_SUCCESS) tower_gap("MPI_Barrier failed");
    return result;
}

int MPI_Bcast(void *buffer, int count, MPI_Datatype datatype, int root, MPI_Comm comm) {
    unsigned long long sequence = 0;
    const char *dtype = tower_dtype(datatype);
    int enabled = tower_before(comm, dtype, count, &sequence);
    if (enabled) tower_collective("bcast", dtype, count, root, sequence, "enter");
    int result = PMPI_Bcast(buffer, count, datatype, root, comm);
    if (enabled && result == MPI_SUCCESS) tower_collective("bcast", dtype, count, root, sequence, "complete");
    else if (result != MPI_SUCCESS) tower_gap("MPI_Bcast failed");
    return result;
}

int MPI_Allreduce(const void *sendbuf, void *recvbuf, int count, MPI_Datatype datatype, MPI_Op op, MPI_Comm comm) {
    unsigned long long sequence = 0;
    const char *dtype = tower_dtype(datatype);
    int enabled = 0;
    if (op == MPI_SUM) enabled = tower_before(comm, dtype, count, &sequence);
    else tower_gap("Only MPI_SUM reduction operators are covered");
    if (enabled) tower_collective("allreduce", dtype, count, 0, sequence, "enter");
    int result = PMPI_Allreduce(sendbuf, recvbuf, count, datatype, op, comm);
    if (enabled && result == MPI_SUCCESS) tower_collective("allreduce", dtype, count, 0, sequence, "complete");
    else if (result != MPI_SUCCESS) tower_gap("MPI_Allreduce failed");
    return result;
}
'''


def main(argv=None):
    parser = argparse.ArgumentParser(prog="tower trace mpi")
    parser.add_argument("--source", action="store_true", help="Print the optional PMPI C adapter source")
    args = parser.parse_args(argv)
    if not args.source:
        parser.error("Use --source; compile explicitly with the application's MPI compiler")
    print(SOURCE)
    return 0
