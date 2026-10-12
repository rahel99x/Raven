"""Optional PMPI source adapter for explicitly supported WORLD collectives.

Generated source is compiled with the application's MPI compiler. It is not
loaded by Raven itself and makes no scheduler, payload, or device changes.
"""
from __future__ import annotations

import argparse


SOURCE = r'''/* Raven PMPI adapter: C bindings, MPI_COMM_WORLD, <=256 ranks.
 * Compile with the SAME MPI compiler/runtime as the application:
 * mpicc -std=c11 -D_GNU_SOURCE -fPIC -shared raven_mpi.c -o libraven_mpi.so
 * Set RAVEN_MPI_COMM_GENERATION to the same nonempty identifier on every rank.
 * Supported: Barrier, scalar Bcast, scalar Allreduce with MPI_SUM.
 * No added collectives/barriers, no tensor/message payload access.
 * Unsupported communicators/types/operators/thread-multiple emit a gap once.
 */
#include <mpi.h>
#include <stdatomic.h>
#include "raven_trace.h"
static int raven_rank = -1, raven_size = 0, raven_supported = 0;
static _Atomic unsigned long long raven_sequence = 0;
static _Atomic int raven_gap_emitted = 0;
static char raven_members[1200], raven_generation[129];

static void raven_gap(const char *reason) {
    if (!atomic_exchange(&raven_gap_emitted, 1)) {
        char data[512];
        snprintf(data, sizeof(data), "{\"reason\":\"%s\",\"adapter\":\"pmpi-world\"}", reason);
        raven_trace_emit("gap", data);
    }
}

static void raven_mpi_initialize(int provided) {
    const char *generation = getenv("RAVEN_MPI_COMM_GENERATION");
    if (!generation || !*generation || strlen(generation) > 128 ||
        strspn(generation, "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-") != strlen(generation)) {
        raven_gap("Missing or invalid shared communicator generation"); return;
    }
    if (provided == MPI_THREAD_MULTIPLE) {
        raven_gap("MPI_THREAD_MULTIPLE ordering is outside this adapter coverage"); return;
    }
    if (PMPI_Comm_rank(MPI_COMM_WORLD, &raven_rank) != MPI_SUCCESS ||
        PMPI_Comm_size(MPI_COMM_WORLD, &raven_size) != MPI_SUCCESS || raven_size < 1 || raven_size > 256) {
        raven_gap("WORLD identity unavailable or exceeds the 256-rank packet budget"); return;
    }
    strcpy(raven_generation, generation);
    size_t used = 0;
    raven_members[used++] = '[';
    for (int rank = 0; rank < raven_size; rank++) {
        int n = snprintf(raven_members + used, sizeof(raven_members) - used, "%s%d", rank ? "," : "", rank);
        if (n < 0 || (size_t)n >= sizeof(raven_members) - used) {
            raven_gap("Communicator member budget exceeded"); return;
        }
        used += (size_t)n;
    }
    raven_members[used++] = ']'; raven_members[used] = 0;
    raven_supported = 1;
    raven_trace_emit("mpi_initialize", "{\"coverage\":\"WORLD Barrier Bcast and scalar Allreduce SUM; C bindings only\"}");
}

static const char *raven_dtype(MPI_Datatype datatype) {
    if (datatype == MPI_FLOAT && sizeof(float) == 4) return "float32";
    if (datatype == MPI_DOUBLE && sizeof(double) == 8) return "float64";
    if (datatype == MPI_INT && sizeof(int) == 4) return "int32";
    if (datatype == MPI_UNSIGNED && sizeof(unsigned int) == 4) return "uint32";
    if (datatype == MPI_LONG_LONG_INT && sizeof(long long) == 8) return "int64";
    if (datatype == MPI_BYTE || datatype == MPI_UNSIGNED_CHAR) return "byte";
    return NULL;
}

static int raven_before(MPI_Comm comm, const char *dtype, int count,
                        unsigned long long *sequence) {
    if (!raven_supported) return 0;
    if (comm != MPI_COMM_WORLD || !dtype || count < 0) {
        raven_gap("Unsupported communicator datatype or count"); return 0;
    }
    *sequence = atomic_fetch_add(&raven_sequence, 1);
    return 1;
}

static void raven_collective(const char *operation, const char *dtype, int count,
                             int root, unsigned long long sequence, const char *phase) {
    char data[2800];
    int n = snprintf(data, sizeof(data),
        "{\"communicator\":\"MPI_COMM_WORLD\",\"generation\":\"%s\",\"sequence\":%llu,"
        "\"operation\":\"%s\",\"members\":%s,\"rank\":%d,\"dtype\":\"%s\","
        "\"count\":%d,\"root\":%d,\"reduction\":\"sum\",\"phase\":\"%s\"}",
        raven_generation, sequence, operation, raven_members, raven_rank, dtype, count, root, phase);
    if (n < 0 || (size_t)n >= sizeof(data)) { raven_gap("Collective metadata budget exceeded"); return; }
    if (raven_trace_emit("collective", data) < 0) raven_gap("Collective transport failed");
}

int MPI_Init(int *argc, char ***argv) {
    int result = PMPI_Init(argc, argv);
    if (result == MPI_SUCCESS) raven_mpi_initialize(MPI_THREAD_SINGLE);
    else raven_gap("MPI_Init failed");
    return result;
}

int MPI_Init_thread(int *argc, char ***argv, int required, int *provided) {
    int result = PMPI_Init_thread(argc, argv, required, provided);
    if (result == MPI_SUCCESS) raven_mpi_initialize(*provided);
    else raven_gap("MPI_Init_thread failed");
    return result;
}

int MPI_Finalize(void) {
    raven_trace_emit("mpi_finalize", "{}");
    return PMPI_Finalize();
}

int MPI_Barrier(MPI_Comm comm) {
    unsigned long long sequence = 0;
    int enabled = raven_before(comm, "byte", 0, &sequence);
    if (enabled) raven_collective("barrier", "byte", 0, 0, sequence, "enter");
    int result = PMPI_Barrier(comm);
    if (enabled && result == MPI_SUCCESS) raven_collective("barrier", "byte", 0, 0, sequence, "complete");
    else if (result != MPI_SUCCESS) raven_gap("MPI_Barrier failed");
    return result;
}

int MPI_Bcast(void *buffer, int count, MPI_Datatype datatype, int root, MPI_Comm comm) {
    unsigned long long sequence = 0;
    const char *dtype = raven_dtype(datatype);
    int enabled = raven_before(comm, dtype, count, &sequence);
    if (enabled) raven_collective("bcast", dtype, count, root, sequence, "enter");
    int result = PMPI_Bcast(buffer, count, datatype, root, comm);
    if (enabled && result == MPI_SUCCESS) raven_collective("bcast", dtype, count, root, sequence, "complete");
    else if (result != MPI_SUCCESS) raven_gap("MPI_Bcast failed");
    return result;
}

int MPI_Allreduce(const void *sendbuf, void *recvbuf, int count, MPI_Datatype datatype, MPI_Op op, MPI_Comm comm) {
    unsigned long long sequence = 0;
    const char *dtype = raven_dtype(datatype);
    int enabled = 0;
    if (op == MPI_SUM) enabled = raven_before(comm, dtype, count, &sequence);
    else raven_gap("Only MPI_SUM reduction operators are covered");
    if (enabled) raven_collective("allreduce", dtype, count, 0, sequence, "enter");
    int result = PMPI_Allreduce(sendbuf, recvbuf, count, datatype, op, comm);
    if (enabled && result == MPI_SUCCESS) raven_collective("allreduce", dtype, count, 0, sequence, "complete");
    else if (result != MPI_SUCCESS) raven_gap("MPI_Allreduce failed");
    return result;
}
'''


def main(argv=None):
    parser = argparse.ArgumentParser(prog="raven trace mpi")
    parser.add_argument("--source", action="store_true", help="Print the optional PMPI C adapter source")
    args = parser.parse_args(argv)
    if not args.source:
        parser.error("Use --source; compile explicitly with the application's MPI compiler")
    print(SOURCE)
    return 0
