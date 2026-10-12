"""A small, safe expression language over the dashboard's data: alert rules, ``--wait-for`` and ``--eval``.

Expressions are Python syntax restricted to literals, names, attribute and item access, arithmetic, comparisons,
boolean operators, conditional expressions, comprehensions and calls to a fixed set of functions (``len min max sum
any all abs round int float str sorted`` and the string methods ``startswith endswith lower upper split strip
in``).  Nothing else parses: no imports, no dunder attributes, no lambdas, no assignments.

    state == "RUNNING" and cpu < 0.3 and elapsed > 600
    any(j.gpu is not None and j.gpu < 20 for j in running)
    n_pending > 5 or free_gpus.get("a100", 0) == 0
"""
from __future__ import annotations

import ast
import operator
from typing import Any, Callable, Dict, Iterable, Optional, Sequence


class ExprError(ValueError):
    pass


class NS(dict):
    """A namespace: ``ns.key`` and ``ns["key"]`` both work; a missing attribute is None (so ``j.gpu < 20`` on a
    CPU job compares None and reads as False rather than raising)."""

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        return self.get(name)


FUNCS: Dict[str, Callable] = {
    "len": len, "min": min, "max": max, "sum": sum, "any": any, "all": all, "abs": abs, "round": round, "int": int, "float": float, "str": str,
    "sorted": sorted, "bool": bool, "list": list, "set": set,
}
STR_METHODS = {"startswith", "endswith", "lower", "upper", "split", "strip", "replace", "count", "find", "isdigit"}
DICT_METHODS = {"get", "keys", "values", "items"}
BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
       ast.Mod: operator.mod}
CMP = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt, ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge,
       ast.In: lambda a, b: a in b, ast.NotIn: lambda a, b: a not in b, ast.Is: operator.is_, ast.IsNot: operator.is_not}
ALLOWED = (ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.BinOp, ast.UnaryOp, ast.Not, ast.USub, ast.UAdd, ast.Compare, ast.Name, ast.Load, ast.Store,
           ast.Constant, ast.Attribute, ast.Subscript, ast.Call, ast.IfExp, ast.List, ast.Tuple, ast.Dict, ast.Set, ast.GeneratorExp, ast.ListComp,
           ast.SetComp, ast.comprehension, ast.Slice, ast.keyword) + tuple(BIN) + tuple(CMP)


def _check(node: ast.AST) -> None:
    for n in ast.walk(node):
        if not isinstance(n, ALLOWED):
            raise ExprError(f"not allowed in an expression: {type(n).__name__}")
        if isinstance(n, ast.Attribute) and (n.attr.startswith("_")):
            raise ExprError(f"attribute not allowed: {n.attr}")
        if isinstance(n, ast.Name) and n.id.startswith("_"):
            raise ExprError(f"name not allowed: {n.id}")
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Name):
                if f.id not in FUNCS:
                    raise ExprError(f"unknown function {f.id}()")
            elif isinstance(f, ast.Attribute):
                if f.attr not in STR_METHODS | DICT_METHODS:
                    raise ExprError(f"method not allowed: .{f.attr}()")
            else:
                raise ExprError("only named functions can be called")
        if isinstance(n, ast.comprehension):
            valid = isinstance(n.target, ast.Name) or (
                isinstance(n.target, ast.Tuple) and all(isinstance(t, ast.Name) for t in n.target.elts)
            )
            if not valid:
                raise ExprError("comprehension targets must be names or a flat tuple of names")


class _Eval:
    def __init__(self, ns: Dict[str, Any]):
        self.ns = ns

    def run(self, node: ast.AST) -> Any:
        m = getattr(self, "n_" + type(node).__name__, None)
        if m is None:
            raise ExprError(f"not allowed in an expression: {type(node).__name__}")
        return m(node)

    def n_Expression(self, n):
        return self.run(n.body)

    def n_Constant(self, n):
        return n.value

    def n_Name(self, n):
        if n.id in self.ns:
            return self.ns[n.id]
        if n.id in ("None", "True", "False"):
            return {"None": None, "True": True, "False": False}[n.id]
        raise ExprError(f"unknown name '{n.id}'")

    def n_Attribute(self, n):
        obj = self.run(n.value)
        if isinstance(obj, dict):
            if n.attr in obj:
                return obj[n.attr]
            if n.attr in DICT_METHODS:
                return getattr(obj, n.attr)
            return None if isinstance(obj, NS) else self._missing(n.attr, obj)
        if isinstance(obj, str) and n.attr in STR_METHODS:
            return getattr(obj, n.attr)
        if obj is None:
            return None
        raise ExprError(f"no attribute '{n.attr}' on {type(obj).__name__}")

    @staticmethod
    def _missing(attr, obj):
        raise ExprError(f"no key '{attr}' (keys: {', '.join(sorted(map(str, obj))[:12])})")

    def n_Subscript(self, n):
        obj = self.run(n.value)
        if isinstance(n.slice, ast.Slice):
            lo = self.run(n.slice.lower) if n.slice.lower else None
            hi = self.run(n.slice.upper) if n.slice.upper else None
            step = self.run(n.slice.step) if n.slice.step else None
            return obj[lo:hi:step]
        key = self.run(n.slice)
        try:
            return obj[key]
        except (KeyError, IndexError, TypeError) as e:
            if isinstance(obj, NS):
                return None
            raise ExprError(f"{type(e).__name__}: {e}")

    def n_BoolOp(self, n):
        if isinstance(n.op, ast.And):
            v = True
            for x in n.values:
                v = self.run(x)
                if not v:
                    return v
            return v
        v = False
        for x in n.values:
            v = self.run(x)
            if v:
                return v
        return v

    def n_UnaryOp(self, n):
        v = self.run(n.operand)
        if isinstance(n.op, ast.Not):
            return not v
        if isinstance(n.op, ast.USub):
            return -v
        return +v

    def n_BinOp(self, n):
        a, b = self.run(n.left), self.run(n.right)
        try:
            return BIN[type(n.op)](a, b)
        except (TypeError, ZeroDivisionError) as e:
            raise ExprError(f"{type(e).__name__}: {e}")

    def n_Compare(self, n):
        left = self.run(n.left)
        for op, right_node in zip(n.ops, n.comparators):
            right = self.run(right_node)
            try:
                ok = CMP[type(op)](left, right)
            except TypeError:
                ok = False                                  # None < 3: unknown compares as False, never raises
            if not ok:
                return False
            left = right
        return True

    def n_IfExp(self, n):
        return self.run(n.body) if self.run(n.test) else self.run(n.orelse)

    def n_List(self, n):
        return [self.run(x) for x in n.elts]

    def n_Tuple(self, n):
        return tuple(self.run(x) for x in n.elts)

    def n_Set(self, n):
        return {self.run(x) for x in n.elts}

    def n_Dict(self, n):
        return {self.run(k): self.run(v) for k, v in zip(n.keys, n.values)}

    def n_Call(self, n):
        args = [self.run(a) for a in n.args]
        kwargs = {k.arg: self.run(k.value) for k in n.keywords}
        if isinstance(n.func, ast.Name):
            fn = FUNCS[n.func.id]
        else:
            fn = self.run(n.func)
            if fn is None:
                return None
            if not callable(fn):
                raise ExprError(f"not callable: .{n.func.attr}")
        try:
            return fn(*args, **kwargs)
        except (TypeError, ValueError) as e:
            raise ExprError(f"{type(e).__name__}: {e}")

    def _comp(self, gens, elt_fn):
        out = []

        def rec(i, ns):
            if i == len(gens):
                out.append(elt_fn(ns))
                return
            g = gens[i]
            it = _Eval(ns).run(g.iter)
            if it is None:
                return
            for item in it:
                ns2 = dict(ns)
                if isinstance(g.target, ast.Name):
                    ns2[g.target.id] = item
                else:
                    names = [t.id for t in g.target.elts]
                    values = list(item)
                    if len(names) != len(values):
                        raise ExprError(f"expected {len(names)} values to unpack, got {len(values)}")
                    for name, val in zip(names, values):
                        ns2[name] = val
                ev = _Eval(ns2)
                if all(ev.run(c) for c in g.ifs):
                    rec(i + 1, ns2)

        rec(0, self.ns)
        return out

    def n_GeneratorExp(self, n):
        return self._comp(n.generators, lambda ns: _Eval(ns).run(n.elt))

    n_ListComp = n_GeneratorExp

    def n_SetComp(self, n):
        return set(self.n_GeneratorExp(n))


class Expr:
    """A compiled expression; ``expr(ns)`` evaluates it over a namespace (a dict)."""

    def __init__(self, text: str):
        self.text = text.strip()
        if not self.text:
            raise ExprError("empty expression")
        try:
            self.tree = ast.parse(self.text, mode="eval")
        except SyntaxError as e:
            raise ExprError(f"syntax: {e.msg} at column {e.offset}")
        _check(self.tree)
        self.names = sorted({n.id for n in ast.walk(self.tree) if isinstance(n, ast.Name)} - set(FUNCS) - {"None", "True", "False"})

    def __call__(self, ns: Dict[str, Any]) -> Any:
        try:
            return _Eval(dict(ns)).run(self.tree)
        except ExprError:
            raise
        except (TypeError, ValueError, ArithmeticError, LookupError, AttributeError) as exc:
            raise ExprError(f"{type(exc).__name__}: {exc}") from exc

    def __repr__(self):
        return f"Expr({self.text!r})"


def evaluate(text: str, ns: Dict[str, Any]) -> Any:
    return Expr(text)(ns)


# ------------------------------------------------------------------------------------------------ namespaces over the snapshot
def job_ns(j, snap: dict, marks: Iterable[str] = (), tags: Optional[Dict[str, Sequence[str]]] = None) -> NS:
    """The fields of one queued job as plain values: durations in seconds, fractions in 0..1, GPU utilisation in %."""
    from .model import stamp
    lv = snap.get("live", {}).get(j.id)
    g = snap.get("gpu", {}).get(j.id) or []
    measured_gpu = [s.util for s in g if isinstance(s.util, (int, float))
                    and not isinstance(s.util, bool) and 0 <= s.util <= 100]
    gutil = sum(measured_gpu) / len(measured_gpu) if measured_gpu else None
    mean = None
    if g:
        key = f"{j.id}:" + getattr(g[0], "device_key", f"{g[0].node}:{g[0].index}")
        mean = snap.get("gpu_mean", {}).get(key)
    el, lim = j.elapsed_s, j.limit_s
    sub, start = stamp(j.submit), stamp(j.start)
    from . import clock
    now = clock.now()
    return NS(id=j.id, name=j.name, state=j.state, partition=j.partition, part=j.partition, pending=j.pending, running=(j.state == "RUNNING"),
              held=j.held, dep=j.dependency, dependency=j.dependency, reason=j.reason, priority=j.priority, nodes=j.nodes, cpus=j.cpus, gpus=j.gpus,
              gpu_type=j.gpu_type, nodelist=j.nodelist, hosts=list(j.hosts), elapsed=el if el is not None else 0.0, limit=lim,
              left=(lim - el) if (lim is not None and el is not None) else None, waited=(now - sub) if (sub and j.pending) else None,
              age=(now - sub) if sub else None, cpu=(lv.rate if lv and lv.rate is not None else (lv.avg if lv else None)), eff=(lv.avg if lv else None),
              rss=(lv.rss if lv else None), mem=((lv.rss / j.mem_bytes) if (lv and lv.rss is not None and j.mem_bytes) else None), mem_req=j.mem_bytes,
              gpu=gutil, gpu_mean=mean, account=j.account, qos=j.qos, submit=sub, start=start, est_start=stamp(j.est_start), command=j.command,
              marked=(j.id in set(marks)), tags=list((tags or {}).get(j.id, [])))


def finished_ns(f) -> NS:
    from .model import secs, stamp
    return NS(id=f.id, name=f.name, state=f.state, partition=f.partition, part=f.partition, elapsed=secs(f.elapsed) or 0.0, cpus=f.cpus, gpus=f.gpus,
              nodes=f.nodes, cpu_eff=f.cpu_eff, mem_eff=f.mem_eff, rss=f.rss, mem_req=f.req_mem, exit=f.exit, start=stamp(f.start), end=stamp(f.end),
              submit=stamp(f.submit), nodelist=f.nodelist, core_hours=f.core_hours, gpu_hours=f.gpu_hours, ok=(f.state == "COMPLETED"))


def cluster_ns(snap: dict, user: str = "", marks: Iterable[str] = (), tags: Optional[Dict[str, Sequence[str]]] = None) -> NS:
    """The whole snapshot as a namespace: ``jobs running pending finished n_running n_pending free_gpus partitions
    nodes account events now user`` plus every job's fields through ``jobs``."""
    from . import clock
    jobs = [job_ns(j, snap, marks, tags) for j in snap.get("jobs", [])]
    running = [j for j in jobs if not j["pending"]]
    pending = [j for j in jobs if j["pending"]]
    fin = [finished_ns(f) for f in snap.get("finished", [])]
    inv = snap.get("gpu_inventory", {})
    free = {t: v.get("free", 0) for t, v in inv.items()}
    parts = {p.name: NS(name=p.name, avail=p.avail, limit=p.limit, nodes=p.nodes, nodes_aiot=p.nodes_aiot, cpus_aiot=p.cpus_aiot,
                        gpus={t: dict(v) for t, v in p.gpus.items()}) for p in snap.get("partitions", [])}
    nodes = {n: NS(name=nd.name, state=nd.state, cpus=nd.cpus, alloc=nd.alloc, load=nd.load, mem_total=nd.mem_total, mem_free=nd.mem_free)
             for n, nd in snap.get("nodes", {}).items()}
    errors = [h.name for h in snap.get("health", {}).values() if h.error and h.enabled]
    return NS(jobs=jobs, running=running, pending=pending, finished=fin, n_running=len(running), n_pending=len(pending), n_jobs=len(jobs),
              n_finished=len(fin), free_gpus=NS(free), gpus_free=sum(free.values()), partitions=NS(parts), nodes=NS(nodes),
              account=NS(snap.get("account", {}) or {}), events=[NS(e) for e in snap.get("events", [])][-20:], now=clock.now(), user=user,
              source_errors=errors, cpus_running=sum(j["cpus"] for j in running), gpus_running=sum(j["gpus"] for j in running),
              by_id=NS({j["id"]: j for j in jobs}), by_name=NS({j["name"]: j for j in jobs}))
