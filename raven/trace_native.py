"""Private inherited socket transport for cooperative native instrumentation.

Each process/thread owns a stream. There is no public network listener, ptrace,
or symbol injection. The application must call the opt-in SDK.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import uuid

from .trace_recorder import Recorder, default_job_id
from . import trace_protocol as P


HEADER = r'''/* Raven cooperative C/C++ SDK, Linux/POSIX, no automatic instrumentation.
 * raven trace native --project ROOT --job-id ID --attempt ATTEMPT -- ./program
 * Calls can block under collector backpressure. Do not use inside signal
 * handlers, allocator hooks, or locks needed by a collector callback.
 * Return 0 on accepted transport write, -1 on failure, 1 when not enabled.
 * Transport acceptance is NOT a durable acknowledgement.
 */
#ifndef RAVEN_TRACE_H
#define RAVEN_TRACE_H
#include <sys/socket.h>
#include <sys/syscall.h>
#include <unistd.h>
#include <time.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
static inline long raven_trace_tid(void) { return (long)syscall(SYS_gettid); }
static inline const char *raven_trace_process_start(void) {
    static __thread char ticks[32];
    static __thread long owner;
    if (owner == (long)getpid() && ticks[0]) return ticks;
    char buffer[4096];
    FILE *file = fopen("/proc/self/stat", "r");
    if (!file) return NULL;
    char *result = fgets(buffer, sizeof(buffer), file);
    fclose(file);
    if (!result) return NULL;
    char *cursor = strrchr(buffer, ')');
    if (!cursor) return NULL;
    cursor += 1;
    for (int field = 3; field <= 22; field++) {
        while (*cursor == ' ') cursor++;
        char *finish = strchr(cursor, ' ');
        if (!finish) return NULL;
        if (field == 22) {
            size_t n = (size_t)(finish - cursor);
            if (!n || n >= sizeof(ticks)) return NULL;
            memcpy(ticks, cursor, n); ticks[n] = 0; owner = (long)getpid();
            return ticks;
        }
        cursor = finish + 1;
    }
    return NULL;
}
static inline int raven_trace_stream(char *out, size_t size) {
    const char *session = getenv("RAVEN_TRACE_SESSION");
    const char *start = raven_trace_process_start();
    if (!session || strlen(session) != 32 || !start) return -1;
    int n = snprintf(out, size, "%s-%ld-%s-%ld", session, (long)getpid(), start, raven_trace_tid());
    return n > 0 && (size_t)n < size ? 0 : -1;
}
static inline int raven_trace_emit(const char *kind, const char *data_json) {
    const char *value = getenv("RAVEN_TRACE_FD");
    if (!value) return 1;
    char *end = NULL;
    long fd = strtol(value, &end, 10);
    if (!*value || *end || fd < 0 || fd > 2147483647L) { errno = EINVAL; return -1; }
    if (!kind || !data_json || strlen(kind) > 64 || strspn(kind, "abcdefghijklmnopqrstuvwxyz_0123456789") != strlen(kind)) {
        errno = EINVAL; return -1;
    }
    char stream[128], packet[4096];
    struct timespec now;
    if (raven_trace_stream(stream, sizeof(stream)) || clock_gettime(CLOCK_MONOTONIC, &now)) return -1;
    int n = snprintf(packet, sizeof(packet),
        "{\"stream\":\"%s\",\"pid\":%ld,\"process_start\":\"%s\",\"thread\":\"%ld\",\"kind\":\"%s\",\"mono_ns\":%lld,\"data\":%s}",
        stream, (long)getpid(), raven_trace_process_start(), raven_trace_tid(), kind,
        (long long)now.tv_sec * 1000000000LL + now.tv_nsec, data_json);
    if (n < 0 || (size_t)n >= sizeof(packet)) { errno = EMSGSIZE; return -1; }
    ssize_t sent;
    do { sent = send((int)fd, packet, (size_t)n, MSG_NOSIGNAL); } while (sent < 0 && errno == EINTR);
    return sent == n ? 0 : -1;
}
#endif
'''


def collect(command, *, project, job_id, attempt=None, cluster=None,
            max_mib=64, max_streams=64, slurm_submit=None, slurm_start=None):
    if not command or not 1 <= max_streams <= 256 or not 1 <= max_mib <= 65536:
        raise ValueError("Native trace needs a command and bounded storage/stream limits")
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    session = uuid.uuid4().hex
    env = dict(os.environ, RAVEN_TRACE_FD=str(child.fileno()), RAVEN_TRACE_SESSION=session)
    recorders = {}
    process = None
    issues = []
    total_bytes = 0
    last_flush = time.monotonic()
    try:
        process = subprocess.Popen(command, env=env, pass_fds=(child.fileno(),))
        child.close()
        parent.settimeout(.1)
        exited_at = None
        while True:
            if time.monotonic() - last_flush >= .05:
                for recorder in recorders.values():
                    if recorder.sequence != recorder.committed_seq:
                        recorder.flush()
                last_flush = time.monotonic()
            code = process.poll()
            if code is not None and exited_at is None:
                exited_at = time.monotonic()
            if exited_at is not None and time.monotonic() - exited_at > 2:
                issues.append("Descendants retained the socket after launcher exit; capture ended with unknown tail")
                break
            try:
                raw, ancillary, flags, address = parent.recvmsg(4096)
            except socket.timeout:
                continue
            if not raw:
                break
            total_bytes += len(raw)
            if total_bytes > max_mib << 20:
                if "Native session input budget reached; later events discarded" not in issues:
                    issues.append("Native session input budget reached; later events discarded")
                continue  # drain transport so an opted-in program can finish
            try:
                if flags & socket.MSG_TRUNC:
                    raise ValueError("Native packet exceeds 4096 bytes")
                value = json.loads(raw)
                if not isinstance(value, dict) or set(value) != {"stream", "pid", "process_start", "thread", "kind", "mono_ns", "data"}:
                    raise ValueError("Native packet has invalid fields")
                pid, thread = value["pid"], value["thread"]
                if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0 or not isinstance(thread, str) or not thread.isdigit():
                    raise ValueError("Native actor identity is invalid")
                start = value["process_start"]
                if not isinstance(start, str) or not start.isdigit() or len(start) > 24:
                    raise ValueError("Native process start identity is invalid")
                expected_stream = f"{session}-{pid}-{start}-{thread}"
                if value["stream"] != expected_stream:
                    raise ValueError("Native stream identity does not match its process/thread")
                event = {key: value[key] for key in ("kind", "mono_ns", "thread", "data")}
                P.validate_event(event)
                recorder = recorders.get(expected_stream)
                if recorder is None:
                    if len(recorders) >= max_streams:
                        raise ValueError("Native stream limit reached")
                    from .trace_recorder import _process_start as _process_start
                    recorder = Recorder(project, job_id=job_id, attempt=attempt or "native-" + session,
                        cluster=cluster, stream_id=expected_stream, max_bytes=max_mib << 20, initial_mono_ns=event["mono_ns"], background_flush=False,
                        coverage={"adapter": "cooperative-native-socket", "scope": "Explicit SDK events in this process/thread; uninstrumented calls and device execution are unknown",
                                  "transport": "Private inherited socket; writes may block; transport acceptance is not durable acknowledgement"},
                        identity_extra={"pid": pid, "process_start": start,
                            **({"slurm_submit": slurm_submit} if slurm_submit else {}),
                            **({"slurm_start": slurm_start} if slurm_start else {})})
                    recorders[expected_stream] = recorder
                    print(f"Raven trace: {recorder.path}", file=sys.stderr)
                recorder.emit(event["kind"], event["data"], thread=thread, mono_ns=event["mono_ns"])
            except (ValueError, TypeError, UnicodeError, OSError) as exc:
                reason = f"Rejected native event: {exc}"
                if reason not in issues and len(issues) < 16:
                    issues.append(reason)
        returncode = process.wait()
        if returncode != 0:
            issues.append(f"Native command exited with status {returncode}; final source tail may be incomplete")
    except BaseException:
        # Do not kill application descendants or alter scheduler state.
        issues.append("Native collector interrupted; application tail is unknown")
        raise
    finally:
        child.close()
        parent.close()
        for recorder in recorders.values():
            if issues:
                recorder._fail("PARTIAL", "; ".join(issues)[:2048])
            recorder.close()
        for issue in issues:
            print("Raven trace: " + issue, file=sys.stderr)
    if not recorders:
        print("Raven trace: no SDK events received; uninstrumented commands cannot be traced by this adapter", file=sys.stderr)
    return returncode


def main(argv=None):
    from .raven_execution import cli_admission
    blocked = cli_admission(["trace", "native", *(sys.argv[1:] if argv is None else argv)])
    if blocked is not None:
        print(json.dumps(blocked, indent=2))
        return 3
    parser = argparse.ArgumentParser(prog="raven trace native")
    parser.add_argument("--sdk-header", action="store_true", help="Print the standalone C/C++ cooperative SDK header")
    parser.add_argument("--project", default=os.getcwd())
    parser.add_argument("--job-id", default=default_job_id())
    parser.add_argument("--attempt", default=os.environ.get("RAVEN_RUN_ID"))
    parser.add_argument("--cluster", default=os.environ.get("SLURM_CLUSTER_NAME", "local"))
    parser.add_argument("--slurm-submit")
    parser.add_argument("--slurm-start")
    parser.add_argument("--max-mib", default=64, type=int)
    parser.add_argument("--max-streams", default=64, type=int)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.sdk_header:
        print(HEADER)
        return 0
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("Provide -- command [args] or --sdk-header")
    return collect(command, project=args.project, job_id=args.job_id, attempt=args.attempt,
        cluster=args.cluster, max_mib=args.max_mib, max_streams=args.max_streams,
        slurm_submit=args.slurm_submit, slurm_start=args.slurm_start)
