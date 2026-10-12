"""Clone and resubmit: the job's original submit line (sacct's SubmitLine on Slurm 23.02+) or an sbatch command
rebuilt from its record, with resource overrides (yours or the advisor's) applied as command-line flags, which
win over the script's #SBATCH lines; previewed with ``sbatch --test-only`` before it is sent."""
from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .model import Finished, Job, fint

# flag -> (short forms, long forms); the value may follow with '=' or as the next word
FLAGS = {
    "mem": ((), ("--mem",)), "cpus": (("-c",), ("--cpus-per-task",)), "time": (("-t",), ("--time",)), "partition": (("-p",), ("--partition",)),
    "gres": ((), ("--gres",)), "name": (("-J",), ("--job-name",)), "dependency": (("-d",), ("--dependency",)), "array": (("-a",), ("--array",)),
    "output": (("-o",), ("--output",)), "error": (("-e",), ("--error",)), "account": (("-A",), ("--account",)), "qos": (("-q",), ("--qos",)),
    "nodes": (("-N",), ("--nodes",)), "ntasks": (("-n",), ("--ntasks",)), "gpus": ((), ("--gpus",)), "chdir": (("-D",), ("--chdir",)),
    "mem_per_cpu": ((), ("--mem-per-cpu",)), "constraint": (("-C",), ("--constraint",)), "wrap": ((), ("--wrap",)),
}
ALIASES = {"c": "cpus", "cpus-per-task": "cpus", "t": "time", "p": "partition", "J": "name", "job-name": "name", "d": "dependency", "a": "array",
           "o": "output", "e": "error", "A": "account", "q": "qos", "N": "nodes", "n": "ntasks", "D": "chdir", "mem-per-cpu": "mem_per_cpu", "C": "constraint"}

# Optional long-option values use '=VALUE' in sbatch, so these never consume the
# next word. In particular, the following word can be the batch script.
SWITCHES = {
    "--contiguous", "--exclusive", "--get-user-env", "--help", "--hold", "--ignore-pbs", "--nice", "--no-kill",
    "--no-requeue", "--overcommit", "--oversubscribe", "--parsable", "--propagate", "--quiet", "--reboot", "--requeue",
    "--spread-job", "--test-only", "--usage", "--use-min-nodes", "--verbose", "--version", "--wait",
}
VALUE_OPTIONS = {
    "--acctg-freq", "--batch", "--bb", "--bbf", "--begin", "--chdir", "--cluster-constraint", "--clusters", "--comment",
    "--container", "--container-id", "--core-spec", "--cores-per-socket", "--cpu-freq", "--cpus-per-gpu", "--deadline",
    "--delay-boot", "--distribution", "--exclude", "--export", "--export-file", "--extra", "--extra-node-info", "--gid",
    "--gpu-bind", "--gpu-freq", "--gpus-per-node", "--gpus-per-socket", "--gpus-per-task", "--hint", "--input", "--kill-on-invalid-dep",
    "--licenses", "--mail-type", "--mail-user", "--mcs-label", "--mem-bind", "--mem-per-gpu", "--mincpus", "--network",
    "--nodelist", "--ntasks-per-core", "--ntasks-per-gpu", "--ntasks-per-node", "--ntasks-per-socket", "--open-mode",
    "--power", "--prefer", "--priority", "--profile", "--reservation", "--signal", "--sockets-per-node", "--switches",
    "--thread-spec", "--threads-per-core", "--time-min", "--tmp", "--tres-bind", "--tres-per-task", "--uid", "--wait-all-nodes", "--wckey",
}
SHORT_SWITCHES = set("HhkOQrsuvVW")
SHORT_VALUES = set("aAbBcCdDeiIJLmMnNoOpqStwx") | {short[1:] for shorts, _ in FLAGS.values() for short in shorts}


def canonical(flag: str) -> Optional[str]:
    f = flag.lstrip("-")
    if f in FLAGS:
        return f
    return ALIASES.get(f)


def split_flags(argv: Sequence[str]) -> Tuple[List[Tuple[Optional[str], Optional[str], str]], List[str]]:
    """sbatch argv (without 'sbatch') -> ([(canonical key or None, value, original flag)], the script and its args)."""
    out: List[Tuple[Optional[str], Optional[str], str]] = []
    rest: List[str] = []
    i = 0
    argv = list(argv)
    while i < len(argv):
        a = argv[i]
        if a == "--":
            out.append((None, None, "--"))
            rest = argv[i + 1:]
            break
        if a.startswith("--"):
            if "=" in a:
                flag, val = a.split("=", 1)
                out.append((canonical(flag), val, flag))
            else:
                key = canonical(a)
                if a in SWITCHES:
                    out.append((key, None, a))
                elif key is not None or a in VALUE_OPTIONS:
                    if i + 1 >= len(argv):
                        raise ValueError(f"{a} needs a value")
                    out.append((key, argv[i + 1], a))
                    i += 1
                else:
                    raise ValueError(f"unrecognized sbatch option {a}; use {a}=VALUE for a site-specific value option")
        elif a.startswith("-") and len(a) > 1:
            pos = 1
            while pos < len(a):
                flag = "-" + a[pos]
                if a[pos] in SHORT_SWITCHES:
                    out.append((canonical(flag), None, flag))
                    pos += 1
                    continue
                if a[pos] not in SHORT_VALUES:
                    raise ValueError(f"unrecognized sbatch option {flag}")
                if pos + 1 < len(a):                        # -c8, or -vc8
                    val = a[pos + 1:]
                elif i + 1 < len(argv):
                    i += 1
                    val = argv[i]
                else:
                    raise ValueError(f"{flag} needs a value")
                out.append((canonical(flag), val, flag))
                break
        else:
            rest = argv[i:]
            break
        i += 1
    return out, rest


def apply_overrides(argv: Sequence[str], overrides: Dict[str, str]) -> List[str]:
    """The same command with each override's flag replaced or appended (``--mem=12G`` form)."""
    flags, rest = split_flags(argv)
    done = set()
    out: List[str] = []
    separator = False
    for key, val, orig in flags:
        if orig == "--":
            separator = True
            continue
        if (key == "mem_per_cpu" and "mem" in overrides) or (key == "mem" and "mem_per_cpu" in overrides):
            continue
        if key in overrides:
            if key not in done:
                out.append(f"{FLAGS[key][1][0]}={overrides[key]}")
                done.add(key)
            continue
        if val is None:
            out.append(orig)
        elif orig.startswith("--"):
            out.append(f"{orig}={val}")
        else:
            out += [orig, val]
    for key, v in overrides.items():
        if key not in done and v not in (None, ""):
            out.append(f"{FLAGS[key][1][0]}={v}")
    return out + (["--"] if separator else []) + rest


def flags_of(argv: Sequence[str]) -> Dict[str, str]:
    """canonical key -> value of the flags present."""
    d: Dict[str, str] = {}
    for key, val, _ in split_flags(argv)[0]:
        if key and val is not None:
            d[key] = val
    return d


@dataclass
class Clone:
    id: str
    name: str = ""
    workdir: str = ""
    argv: List[str] = field(default_factory=list)        # the sbatch command without 'sbatch'
    source: str = ""                                     # "submit line" | "record"
    notes: List[str] = field(default_factory=list)
    probe: str = ""                                      # what sbatch --test-only said

    def command(self) -> str:
        return "sbatch " + " ".join(shlex.quote(a) for a in self.argv)

    def flags(self) -> Dict[str, str]:
        return flags_of(self.argv)


def parse_submit_info(text: str) -> Dict[str, str]:
    """sacct -j ID -P -n -o JobID,SubmitLine,WorkDir,JobName,Partition,Account,QOS,ReqCPUS,ReqMem,Timelimit,NNodes,ReqTRES: the job line."""
    for line in text.splitlines():
        f = line.split("|")
        if len(f) >= 12 and "." not in f[0]:
            return dict(id=f[0], submit_line=f[1], workdir=f[2], name=f[3], partition=f[4], account=f[5], qos=f[6], cpus=f[7], mem=f[8], limit=f[9], nodes=f[10], tres=f[11])
    return {}


def gres_from_tres(tres: str) -> str:
    """'cpu=8,mem=32G,node=1,billing=8,gres/gpu=1,gres/gpu:a100=1' -> 'gpu:a100:1'."""
    typed, plain = "", ""
    for part in (tres or "").split(","):
        if part.startswith("gres/gpu:"):
            t, n = part[len("gres/gpu:"):].split("=", 1)
            typed = f"gpu:{t}:{n}"
        elif part.startswith("gres/gpu="):
            plain = f"gpu:{part.split('=', 1)[1]}"
    return typed or plain


def output_pattern(path: str, jid: str, name: str) -> str:
    """'/x/logs/train-12480001.out' -> '/x/logs/%x-%j.out' so the clone gets its own log."""
    if not path:
        return ""
    base = jid.split("_")[0]
    out = path.replace(base, "%j")
    if name and name in out:
        out = out.replace(name, "%x", 1)
    return out


def build(jid: str, info: Dict[str, str], details: Dict[str, str], job: Optional[Job] = None, fin: Optional[Finished] = None, overrides: Optional[Dict[str, str]] = None) -> Clone:
    """The clone of ``jid`` from sacct's submit info (preferred), else from scontrol's details and the record."""
    c = Clone(id=jid)
    line = (info or {}).get("submit_line", "").strip()
    c.workdir = (info or {}).get("workdir") or details.get("WorkDir", "") or (fin.workdir if fin else "")
    c.name = (info or {}).get("name") or details.get("JobName", "") or (job.name if job else (fin.name if fin else ""))
    if line:
        words = shlex.split(line)
        if words and words[0].endswith("sbatch"):
            words = words[1:]
        c.argv, c.source = words, "submit line"
    else:
        c.source = "record"
        d = details or {}
        part = d.get("Partition") or (info or {}).get("partition") or (job.partition if job else (fin.partition if fin else ""))
        acct = d.get("Account") or (info or {}).get("account") or (job.account if job else "")
        qos = d.get("QOS") or (info or {}).get("qos") or (job.qos if job else "")
        nodes = d.get("NumNodes") or (info or {}).get("nodes") or str(job.nodes if job else (fin.nodes if fin else 1))
        cpus = d.get("CPUs/Task") or (info or {}).get("cpus") or str(job.cpus if job else (fin.cpus if fin else 1))
        mem = d.get("MinMemoryNode") or d.get("MinMemoryCPU") or (info or {}).get("mem") or (job.mem_req if job else "")
        limit = d.get("TimeLimit") or (info or {}).get("limit") or (job.limit if job else (fin.limit if fin else ""))
        gres = (d.get("TresPerNode", "").replace("gres/", "").replace("gres:", "") or gres_from_tres(d.get("TRES", "") or (info or {}).get("tres", ""))
                or (f"gpu:{job.gpu_type}:{job.gpus // max(job.nodes, 1)}" if job and job.gpus and job.gpu_type else (f"gpu:{job.gpus // max(job.nodes, 1)}" if job and job.gpus else "")))
        script = d.get("Command", "")
        argv = ["-J", c.name]
        if part:
            argv += ["-p", part]
        if acct:
            argv += ["-A", acct]
        if qos and qos not in ("normal", "(null)"):
            argv += ["-q", qos]
        if nodes and nodes != "1":
            argv += ["-N", nodes.split("-")[0]]
        if cpus:
            argv += ["-c", str(fint(cpus) or cpus)]
        if mem and mem not in ("0", "0M", "(null)"):
            argv.append(f"--mem={mem}" if not d.get("MinMemoryCPU") or d.get("MinMemoryNode") else f"--mem-per-cpu={mem}")
        if limit and limit not in ("UNLIMITED", "Partition_Limit"):
            argv += ["-t", limit]
        if gres:
            argv.append(f"--gres={gres}")
        out = output_pattern(d.get("StdOut", ""), jid, c.name)
        if out:
            argv.append(f"--output={out}")
        err = output_pattern(d.get("StdErr", ""), jid, c.name)
        if err and err != out:
            argv.append(f"--error={err}")
        if script and script not in ("(null)", ""):
            argv.append(script.split()[0] if " " in script else script)
            if " " in script:
                argv += script.split()[1:]
        else:
            c.notes.append("the script path is unknown (no Command in scontrol and no submit line in sacct): add it")
        c.argv = argv
    if "_" in jid and "array" not in flags_of(c.argv):
        c.notes.append("one task of an array job: add --array=<ids> to resubmit several")
    present = flags_of(c.argv)
    if "dependency" in present:
        c.notes.append(f"keeps the dependency {present['dependency']}; override with --dependency= to drop it")
    if overrides:
        c.argv = apply_overrides(c.argv, {k: v for k, v in overrides.items() if k in FLAGS})
        if overrides.get("script"):
            # --wrap and a batch script are mutually exclusive.
            if "wrap" in flags_of(c.argv):
                c.argv = apply_overrides(c.argv, {"wrap": ""})
                c.argv = [arg for arg in c.argv if arg != "--wrap="]
            flags, rest = split_flags(c.argv)
            c.argv = c.argv[:len(c.argv) - len(rest)] + [overrides["script"]]
            c.notes = [n for n in c.notes if "script path" not in n]
    return c


def script_of(argv: Sequence[str]) -> List[str]:
    """The script and its arguments (what follows the flags)."""
    return split_flags(argv)[1]


def has_submission(argv: Sequence[str]) -> bool:
    """sbatch accepts either a batch script or --wrap without a script."""
    return bool(script_of(argv)) or "wrap" in flags_of(argv)


def parse_override_args(args: Sequence[str]) -> Tuple[Dict[str, str], List[str], List[str]]:
    """':resubmit 123 --mem 12G --time=03:00:00 -c 4 --advised' -> (overrides, words that are not flags, switches)."""
    over: Dict[str, str] = {}
    words: List[str] = []
    switches: List[str] = []
    i = 0
    args = list(args)
    while i < len(args):
        a = args[i]
        if a == "--":
            words.extend(args[i + 1:])
            break
        if a.startswith("-") and len(a) > 1:
            if a in ("--advised", "--advise", "--advice"):
                switches.append("advised")
            elif a == "--script" or a.startswith("--script="):
                if "=" in a:
                    over["script"] = a.split("=", 1)[1]
                elif i + 1 < len(args):
                    over["script"] = args[i + 1]
                    i += 1
                else:
                    raise ValueError("--script needs a value")
            elif "=" in a:
                flag, val = a.split("=", 1)
                key = canonical(flag)
                if key:
                    over[key] = val
                else:
                    raise ValueError(f"unknown override {flag}")
            else:
                key = canonical(a) if len(a) <= 2 or a.startswith("--") else canonical(a[:2])
                if key and len(a) > 2 and not a.startswith("--"):
                    over[key] = a[2:]
                elif key and i + 1 < len(args):
                    if args[i + 1].startswith("-"):
                        raise ValueError(f"{a} needs a value")
                    over[key] = args[i + 1]
                    i += 1
                elif key:
                    raise ValueError(f"{a} needs a value")
                else:
                    raise ValueError(f"unknown override {a}")
        else:
            words.append(a)
        i += 1
    return over, words, switches


def parse_submitted(text: str) -> Optional[str]:
    """'Submitted batch job 12480099' -> '12480099'."""
    for tok in text.replace("\n", " ").split():
        if tok.isdigit() and "Submitted" in text:
            return tok
    return None
