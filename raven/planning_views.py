"""Terminal planning visuals with explicit assumptions and unavailable values."""
from __future__ import annotations

import math
import time

from . import charts, layout as L
from .research import clean
from .planning import PLANNING_VIEWS


def finite(value):
    try:
        return value is not None and not isinstance(value, bool) and math.isfinite(value)
    except (TypeError, ValueError, OverflowError):
        return False


def number(value, unit=""):
    if not finite(value):
        return "unavailable"
    return charts.fmt_num(value) + (" " + unit if unit else "")


def timestamp(value):
    if not finite(value):
        return "unavailable"
    try:
        return time.strftime("%m-%d %H:%M:%S", time.localtime(value))
    except (ValueError, OverflowError, OSError):
        return number(value, "epoch seconds")


def interval(g, metric, width):
    """A solid observed range and a separate estimate marker, without an inferred range."""
    lower, middle, upper = (metric.get(k) for k in ("lower", "estimate", "upper"))
    if not all(finite(v) for v in (lower, middle, upper)) or not 0 <= lower <= middle <= upper:
        return [[("   Interval unavailable; " + clean(metric.get("status", "insufficient evidence"), g.ascii), "dim")]]
    span = max(0, min(60, width - 8))
    if not span:
        return []
    scale = max(upper, 1e-300)
    position = lambda value: min(span - 1, max(0, round(value / scale * (span - 1))))
    a, m, b = (position(v) for v in (lower, middle, upper))
    ribbon = [("   ", "")]
    for x in range(span):
        ribbon.append((g.mark if x == m else g.full if a <= x <= b else g.empty,
                       "magenta+bold" if x == m else "cyan" if a <= x <= b else "dim"))
    return [ribbon, [(f"   {number(lower)}  ..  {number(middle)}  ..  {number(upper)} {clean(metric.get('unit', ''), g.ascii)}", "dim")]]


def render(views, app, result, width):
    g, view = views.g, app.research_view
    row = lambda value, style="": [(clean(value, g.ascii), style)]
    heading = lambda value: L.rule(g, width, clean(value, g.ascii))
    out = [row(f" {dict(PLANNING_VIEWS)[view]}  |  {result.get('status', 'unknown')}", "cyan+bold")]
    if result.get("source"):
        out.append(row(" Data: " + clean(result["source"], g.ascii), "dim"))
    if result.get("job_id"):
        out.append(row(" Job " + clean(result["job_id"], g.ascii), "bold"))
    app.research_data_jobs = result.get("data_jobs", [])
    if view == "predict":
        out.append(row(" Comparable completed runs; uncertainty is conditional on this cohort.", "dim"))
        cohort = result.get("cohort", {})
        out.append(row(" Cohort: " + clean(cohort, g.ascii), "dim"))
        labels = {"runtime_seconds": "Runtime", "memory_bytes": "Memory observation", "cpu_seconds": "CPU consumption"}
        for key, value in result.get("metrics", {}).items():
            out.append(heading(labels.get(key, key)))
            out.append(row(f"   estimate {number(value.get('estimate'), value.get('unit', ''))} | {value.get('samples', 0)} samples | {value.get('method', 'unavailable')}", "bold"))
            out.extend(interval(g, value, width))
            out.append(row(f"   calibration {value.get('calibration_samples', 0)} | observed coverage {number(value.get('observed_coverage'))}", "dim"))
            if value.get("scope") or value.get("memory_scope"):
                out.append(row("   scope: " + clean(value.get("scope", value.get("memory_scope")), g.ascii), "yellow"))
        for value in result.get("censored", [])[:8]:
            out.append(row(" Lower-bound evidence: " + clean(value, g.ascii), "yellow"))
    elif view == "forecast":
        out.append(row(" Scheduler point: " + timestamp(result.get("predicted_start")), "bold"))
        out.append(row(" Start interval: " + timestamp(result.get("lower_start")) + " .. " + timestamp(result.get("upper_start")), "cyan"))
        out.append(row(f" Method {result.get('method', 'unavailable')} | observed runs {result.get('samples', 0)} | revisions {result.get('revisions', 0)}", "dim"))
        calibration = result.get("calibration", {})
        out.append(row(f" Calibration {calibration.get('count', 0)} | target coverage {number(calibration.get('requested_coverage'))} | observed {number(calibration.get('empirical_coverage'))}", "dim"))
        lo, mid, hi = (result.get(k) for k in ("lower_start", "predicted_start", "upper_start"))
        if all(finite(v) for v in (lo, mid, hi)) and hi >= lo:
            out.extend(interval(g, {"lower": 0, "estimate": max(0, mid-lo), "upper": hi-lo, "unit": "seconds within start interval"}, width))
        else:
            out.append(row(" A scheduler point without calibration does not imply a reliable interval.", "yellow"))
        for value in result.get("evidence", [])[:12]:
            out.append(row(" Evidence: " + clean(value, g.ascii), "dim"))
    elif view == "blockers":
        out.append(row(" " + result.get("summary", "No blocker explanation available."), "bold"))
        evidence = {e.get("id"): e for e in result.get("evidence", [])}
        for item in result.get("blockers", []):
            out.append(heading(f"{item.get('title', item.get('code', '?'))} / {item.get('status', 'unknown')}"))
            out.append(row("   " + clean(item.get("summary", ""), g.ascii)))
            for ident in item.get("support", [])[:4]:
                e = evidence.get(ident, {})
                out.append(row(f"   [{ident}] {e.get('source', '')}: {e.get('text', e.get('value', ''))}", "yellow"))
            for check in item.get("checks", [])[:3]:
                out.append(row("   Check: " + clean(check, g.ascii), "cyan"))
        deps = result.get("dependencies", {})
        if deps.get("tree"):
            out.append(heading(f"dependencies / {deps.get('operator', '?')} / {deps.get('status', 'unknown')}"))
            for node in deps["tree"][:128]:
                depth = node.get("depth", 0)
                depth = max(0, min(12, int(depth))) if finite(depth) else 0
                out.append(row("   " + "  "*depth + f"{g.arrow} {node.get('job_id')} {node.get('kind', '')} {node.get('state', '?')} {node.get('status', '?')}", "yellow" if node.get("external") else ""))
        placement = result.get("placement", {})
        out.append(row(f" Placement screening: {placement.get('status', 'not_checked')} | eligible nodes {placement.get('eligible_nodes', 'unknown')}", "dim"))
    elif view == "tradeoffs":
        out.append(row(" Compare completion and reservation costs for the same work; unknowns cannot win.", "dim"))
        for candidate in result.get("candidates", []):
            pareto = candidate.get("pareto") or {}
            frontier = "frontier" if pareto.get("frontier") else pareto.get("status", "uncertain")
            out.append(heading(f"{candidate.get('index', '?')} / {candidate.get('label', '?')} / {frontier}"))
            for key in ("runtime_seconds", "completion_seconds", "reserved_core_hours", "reserved_gpu_hours"):
                metric = candidate.get("metrics", {}).get(key, {})
                out.append(row(f"   {key}: {number(metric.get('estimate'), metric.get('unit', ''))}  [{metric.get('basis', metric.get('status', 'unavailable'))}]", "cyan" if metric.get("estimate") is not None else "dim"))
            for value in candidate.get("risks", [])[:8]:
                out.append(row("   Risk: " + clean(value, g.ascii), "yellow+bold"))
            for value in (candidate.get("assumptions", []) + candidate.get("limitations", []))[:8]:
                out.append(row("   " + clean(value, g.ascii), "yellow"))
        items = [(clean(c.get("label", "?"), g.ascii), c.get("metrics", {}).get("runtime_seconds", {}).get("estimate"), "cyan") for c in result.get("candidates", [])]
        out.extend(charts.hbar_rows(g, items[:16], width, label_w=18, unit="s"))
        out.append(row(" :choose INDEX SCRIPT --workdir DIR prepares a candidate for the Submit view.", "dim"))
    elif view == "scaling":
        for issue in result.get("issues", [])[:24]:
            out.append(row(" Issue: " + clean(issue.get("message", issue) if isinstance(issue, dict) else issue, g.ascii), "red"))
        if "runs" in result:
            out.append(row(f" Review-only scaling plan | {result.get('run_count', 0)} runs | {result.get('mode', 'strong')} scaling", "bold"))
            for item in result["runs"][:128]:
                plan = item.get("submission", {})
                out.append(row(f"   {item.get('configuration')} repeat {item.get('repeat')} | {'ready' if plan.get('valid') else 'blocked'}", "green" if plan.get("valid") else "yellow"))
                out.append(row("   " + clean(plan.get("command", ""), g.ascii), "dim"))
            out.append(row(" Review every command and its output/parameter contract before executing.", "yellow"))
        else:
            points = result.get("points", [])
            out.append(row(f" {result.get('mode', 'strong')} scaling | baseline {result.get('baseline', '?')} | repeat ranges are observed spread", "dim"))
            out.extend(charts.hbar_rows(g, [(f"{p.get('workers')} workers", p.get("runtime", {}).get("median"), "cyan") for p in points], width, unit="s"))
            out.extend(charts.hbar_rows(g, [(f"{p.get('workers')} workers", p.get("speedup"), "green") for p in points], width, unit="x", label_w=12))
            for point in points:
                out.append(row(f"   {point.get('workers')} workers | {point.get('samples', 0)} runs | speedup {number(point.get('speedup'))} | efficiency {number(point.get('efficiency'))}", "bold"))
                measured = point.get("runtime", {})
                out.extend(interval(g, {"estimate": measured.get("median"), "lower": measured.get("lower"),
                                        "upper": measured.get("upper"), "unit": "s observed repeat spread"}, width))
                efficiency = point.get("efficiency")
                if finite(efficiency) and 0 <= efficiency <= 1:
                    out.append([("   efficiency ", "dim")] + L.gradient_bar(g, efficiency, max(0, min(32, width - 18))))
            out.append(row(" Strong scaling fixes problem size; weak scaling fixes work per worker.", "dim"))
    elif view == "workflow":
        makespan = result.get("makespan", {})
        out.append(row(f" Ideal completion: {number(makespan.get('estimate'), 's')} | {number(makespan.get('lower'))} .. {number(makespan.get('upper'))}", "bold"))
        out.append(row(" Critical path: " + (" -> ".join(result.get("critical_path", [])) or "unavailable"), "cyan"))
        span, label_w = max(1, width - min(24, width//3) - 5), min(24, width//3)
        scale = makespan.get("estimate")
        if finite(scale) and scale > 0:
            out.append(charts.time_axis(0, scale, span, " " * (label_w + 3), elapsed=True))
        for node in result.get("nodes", [])[:512]:
            start, finish = node.get("earliest_start"), node.get("earliest_finish")
            if isinstance(start, dict): start = start.get("estimate")
            if isinstance(finish, dict): finish = finish.get("estimate")
            prefix = " * " if node.get("critical") else "   "
            line = [(prefix + L.pad(L.cut(clean(node.get("id", "?"), g.ascii), label_w, g.ascii), label_w), "bold" if node.get("critical") else "dim")]
            if finite(scale) and scale > 0 and finite(start) and finite(finish):
                a, b = (max(0, min(span, round(v/scale*span))) for v in (start, finish))
                line.extend([(" "*a, ""), (g.full*max(0,b-a), "cyan+bold" if node.get("critical") else "green")])
            else:
                line.append((" ? duration unavailable", "dim"))
            out.append(line)
            out.append(row(f"   slack {number(node.get('slack') if not isinstance(node.get('slack'), dict) else node['slack'].get('estimate'), 's')} | dependencies {', '.join(node.get('depends_on', [])) or 'none'}", "dim"))
        envelope = result.get("resource_envelope", {})
        out.append(row(f" Peak overlap: {envelope.get('peak_cpus', '?')} CPUs / {envelope.get('peak_gpus', '?')} GPUs", "cyan"))
        out.append(row(" Unlimited-capacity dependency model; scheduler queue delays are not included.", "yellow"))
        plans = result.get("plans", [])
        if plans:
            out.append(heading("script preflights / symbolic dependencies / review only"))
            for plan in plans[:64]:
                dependencies = ", ".join(plan.get("symbolic_dependencies", [])) or "none"
                out.append(row(f"   {plan.get('workflow_node_id', '?')} | {'preflight passed' if plan.get('valid') else 'preflight blocked'} | depends on {dependencies}", "cyan" if plan.get("valid") else "yellow"))
                out.append(row("   " + clean(plan.get("command", ""), g.ascii), "dim"))
                for issue in plan.get("issues", [])[:3]:
                    out.append(row("   " + clean(issue.get("message", ""), g.ascii), "yellow"))
            if len(plans) > 64:
                out.append(row(" First 64 preflights shown; CLI JSON contains all bounded plans.", "dim"))
            out.append(row(" Symbolic dependencies require explicit orchestration with real scheduler IDs.", "yellow"))
    for value in (result.get("limitations", []) + result.get("warnings", []) + result.get("limits", []))[:24]:
        out.append(row(" Note: " + clean(value, g.ascii), "yellow"))
    if result.get("summary") and view != "blockers":
        out.append(row(" " + clean(result["summary"], g.ascii), "dim"))
    return out
