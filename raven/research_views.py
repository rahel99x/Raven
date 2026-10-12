"""Research cockpit rows: bounded charts, provenance and cited evidence."""
from __future__ import annotations

from . import charts, layout as L, scrollbars as S
from .model import short_duration
from .research import RESEARCH_VIEWS, clean
from . import analysis_ui

STATES = {"COMPLETED": "green", "RUNNING": "cyan", "PENDING": "yellow", "FAILED": "red",
          "TIMEOUT": "red", "OUT_OF_MEMORY": "red", "CANCELLED": "dim", "UNKNOWN": "dim"}


def render(views, snap, app, width, height):
    from . import chart_interaction, metric_live
    chart_mark = chart_interaction.mark(app)
    g = views.g
    text = lambda value: clean(value, g.ascii)
    row = lambda value, style="": [(text(value), style)]
    heading = lambda value: L.rule(g, width, text(value))
    view = app.research_view
    # The outer Jobs Details document owns inline Research scrolling.
    own_document = height is not None and not getattr(app, "research_document_mode", False)
    card_width = width
    from .control_rows import buttons
    nav, nav_hits = buttons(g, width, [(key, label, ("command", "view " + key)) for key, label in RESEARCH_VIEWS],
                            selected=view, group="research_nav", prefix="research-view:")
    if own_document and width >= 6:
        nav.append(L.clip_row([("    ", "")] + L.rule(g, max(0, width - 4), "Research / " + view), width))
    app.research_nav_rows = len(nav)
    hub = getattr(app, "research", None)
    if hub is None:
        return nav + [row(" Research services are unavailable.", "yellow")], nav_hits
    context = hub.context(snap, app)
    # The operations catalog is metadata, not a collector request.
    result = {"status": "ok"} if view == "operations" else hub.request(context)
    analysis = analysis_ui.initialize(app)
    if view == "experiment":
        app.analysis_result = result
        app.analysis_result_job = context.get("jid") or getattr(context.get("job"), "id", None)
        app.analysis_result_generation = context.get("generation")
        analysis_ui.observe_metrics(app, result, app.analysis_result_job)
    rows, hits = [], []
    paint_offset = None
    from .scrolling import viewport as scroll_viewport
    scroll_context = (view, context.get("jid"), context.get("generation"), width)
    job = context["job"]
    if view in ("experiment", "evidence"):
        rows.append(row(f" Job {job.id}  {job.name}  {job.state}" if job else " No job selected", "cyan+bold"))
    if view == "experiment":
        import shlex
        diagnostic_command = "telemetry" + (" " + shlex.quote(str(context.get("jid"))) if context.get("jid") else "")
        controls, control_hits = buttons(g, width,
            [("telemetry", "Metric sampling", ("command", diagnostic_command)),
             ("gpu-provider", "GPU source", ("command", "gpuprovider"))],
            group="research-tools", prefix="research-tools:")
        hits += [(y + len(rows), kind, data) for y, kind, data in control_hits]
        rows += controls
    from . import ops_ui
    if view in ops_ui.CONTEXT_TOOLS:
        controls, control_hits = ops_ui.contextual_controls(g, width, ops_ui.CONTEXT_TOOLS[view])
        hits += [(y + len(rows), kind, data) for y, kind, data in control_hits]
        rows += controls
    structured = ((view == "experiment" and "series" in result) or (view == "artifacts" and "outputs" in result)
                  or view in ("predict", "forecast", "blockers", "tradeoffs", "scaling", "workflow")
                  and any(key in result for key in ("metrics", "predicted_start", "evidence", "candidates", "points", "runs", "issues", "nodes")))
    if result.get("status") in ("loading", "empty", "error", "incomplete") and not structured:
        rows.append(row(" " + result.get("summary", "No data yet."), "red" if result.get("status") == "error" else "dim"))
    elif view == "operations":
        content, control_hits = ops_ui.catalog_rows(g, width)
        hits += [(y + len(rows), kind, data) for y, kind, data in control_hits]
        rows += content
    elif view == "experiment":
        rows.append(row(f" {result.get('path', '')}  |  {result.get('records', 0)} records  |  phase {result.get('phase') or 'unreported'}", "dim"))
        progress = result.get("progress") or {}
        if progress:
            fraction = progress.get("fraction")
            rows.append([(f" Progress {progress.get('completed', '?')}/{progress.get('total', '?')} {text(progress.get('unit', ''))}  ", "bold")] +
                        L.gradient_bar(g, fraction, max(4, min(30, width // 4))))
            eta = progress.get("eta_seconds")
            rows.append(row(" ETA " + (short_duration(eta) + " from reported progress" if eta is not None else "unavailable: report consistent progress samples"), "dim"))
        series = result.get("series", {})
        names = analysis_ui.dashboard_names(app, series)
        reported_jid = context.get("jid") or getattr(job, "id", None)
        live_running = analysis_ui.running_job(snap, reported_jid)
        source = result.get("path", "application metrics")
        identities = {name: analysis_ui.chart_key(app, name, source, interactive=False, jid=reported_jid, job=job)
                      for name in names}
        live_rows = {name: int(live_running and card_width >= metric_live.MIN_WIDTH and identities[name] is not None)
                     for name in names}
        rows.append(row(f" Dashboard {len(names)}/{len(series)} metrics  | :dashboard pin/hide/move/expand/color  | :chart METRIC", "dim"))
        # Offscreen charts reserve their document rows without rasterizing. This
        # makes 64-metric dashboards cost roughly the visible cards per frame.
        tail_rows = len(result.get("errors", [])[:8]) + int(bool(result.get("truncated"))) + int(bool(series) and not names)
        total_rows = len(rows) + sum((10 if name in analysis["expanded"] else 5) + 5 + live_rows[name] + int(name in analysis["pinned"]) for name in names) + tail_rows
        visible_height = max(0, height - len(nav)) if height is not None else total_rows
        if own_document and width >= 6 and total_rows > visible_height:
            # A pane that fits has no rail. Keep its full control width, in
            # particular the existing 24-cell minimum Live control row.
            card_width = width - 1
            live_rows = {name: int(live_running and card_width >= metric_live.MIN_WIDTH and identities[name] is not None)
                         for name in names}
            total_rows = len(rows) + sum((10 if name in analysis["expanded"] else 5) + 5 + live_rows[name] + int(name in analysis["pinned"]) for name in names) + tail_rows
        window = getattr(app, "research_document_window", None)
        if getattr(app, "research_document_mode", False) and window:
            render_start, render_end = window
        else:
            render_start = scroll_viewport(app, "research:document", app.research_scroll, total_rows, visible_height,
                                            context=scroll_context, immediate=height is None)
            paint_offset = render_start
            render_end = render_start + visible_height
        for name in names:
            height_ = 10 if name in analysis["expanded"] else 5
            if name in analysis["pinned"]:
                rows.append(row(" " + ("*" if g.ascii else "◆") + " Pinned " + name, "yellow+bold"))
            count = height_ + 5 + live_rows[name]
            if len(rows) < render_end and len(rows) + count > render_start:
                identity = identities[name]
                controls, _ = metric_live.controls(g, app, identity, card_width,
                    running=live_running, row=len(rows))
                rows.extend(controls)
                hits.append((len(rows), "research_metric", name))
                metadata = {}
                chart = analysis_ui.chart_rows(g, app, series[name], card_width, height_, name,
                    source, interactive=False, metadata=metadata, zoom_key=identity,
                    snapshot=snap, running=live_running)
                chart_interaction.record(app, identity, metadata, row=len(rows), scale=metadata.get("scale", "linear"))
                rows.extend(chart)
            else:
                hits.append((len(rows) + live_rows[name], "research_metric", name))
                rows.extend([[] for _ in range(count)])
        if series and not names:
            rows.append(row(" No visible metrics match. :dashboard search clears search; :dashboard show METRIC or reset restores cards.", "yellow"))
        for error in result.get("errors", [])[:8]:
            rows.append(row(" ! " + text(error), "yellow"))
        if result.get("truncated"):
            rows.append(row(" Bounded tail: earlier records are outside this inspection window.", "yellow"))
    elif view == "arrays":
        from . import array_manifest_ui
        from .job_selection import selected, cleared
        from .job_group_ui import fold_icon
        from .array_disclosure import publish
        from collections import Counter
        import shlex
        groups = result.get("groups", [])
        map_rows, map_hits = array_manifest_ui.controls(g, app, width)
        hits.extend((y + len(rows), kind, value) for y, kind, value in map_hits)
        rows.extend(map_rows)
        publish(app, groups)
        id_counts = Counter(group["id"] for group in groups)
        index = app.clamp_cursor("research", len(groups))
        selected_index = selected(app, "research", index)
        rows.append(row(" Array cohorts  |  solid cells are sampled task identities; unseen tasks stay unknown", "dim"))
        if not groups:
            rows.append(row(" No array records in this snapshot.", "dim"))
        for i, group in enumerate(groups):
            selected_ = i == selected_index
            opened = bool(selected_ and app.research_array_open)
            icon = fold_icon(not opened, ascii_=g.ascii)
            target = (group["id"], group.get("cluster", "")) if id_counts[group["id"]] > 1 else group["id"]
            hits.append((len(rows), "research_array", target))
            if width > 0:
                target = str(group["id"])
                cluster = str(group.get("cluster", ""))
                action = "close" if opened else "open"
                hits.append((len(rows), "control", {
                    "id": "research-array-disclosure:" + str(i) + ":" + target,
                    "label": action.title() + " tasks for array " + target + (" on " + text(cluster) if cluster else ""),
                    "left": 0, "right": 1,
                    "action": ("command", "array " + action + " " + shlex.quote(target) + " " + shlex.quote(cluster)),
                    "group": "research-arrays"}))
            total = group.get("total") if group.get("total_known") else "?"
            rows.append([(icon, "accent+bold+rev" if selected_ else "accent+bold")] +
                        row(f"{'>' if selected_ else ' '} {group['id']}  {group.get('name', '')}  total {total}  observed {group.get('observed', 0)}",
                            "rev+bold" if selected_ else "bold"))
            rows.extend(charts.stacked_bar(g, [(k, n, STATES.get(k, "magenta")) for k, n in group.get("states", {}).items()], width))
            cells = group.get("cells", [])[:max(0, width - 4)]
            rows.append([("   ", "")] + [(g.full if cell.get("observed") else g.empty, STATES.get(cell.get("state"), "dim")) for cell in cells])
            if selected_:
                duration = group.get("duration", {})
                rows.append(row(f" Failed {group.get('failures') or 'none'}  |  duration samples {duration.get('samples', 0)}  p95 {short_duration(duration.get('p95')) if duration.get('p95') is not None else '?'}", "yellow"))
                if group.get("outliers"):
                    rows.append(row(" Slow tail: " + ", ".join(f"{v['index']} ({short_duration(v['duration'])})" for v in group['outliers'][:8]), "dim"))
                for warning in group.get("warnings", [])[:4]:
                    rows.append(row(" ! " + text(warning), "yellow"))
                if app.research_array_open:
                    from .arrays import tasks
                    count = sum(s["count"] for s in group.get("_segments", []))
                    app.research_task_offset = min(app.research_task_offset, max(0, ((count - 1) // 24) * 24))
                    rows.append(heading(f"task page / offset {app.research_task_offset}"))
                    for task in tasks(group, offset=app.research_task_offset, limit=24):
                        entry = array_manifest_ui.label(app, group, task.get("index"))
                        manifest = array_manifest_ui.initialize(app)["manifest"]
                        suffix = (" | " + entry.id + " / " + entry.label) if entry else (
                            " | unmapped index" if manifest and manifest.matches(group["id"], group.get("cluster", "")) else "")
                        if entry and width:
                            hits.append((len(rows), "control", {"id": "arraymap-task:" + manifest.revision + ":" + str(entry.index),
                                "label": "Inspect " + entry.id, "left": 0, "right": width,
                                "action": ("command", "arraymap select " + manifest.revision + " " + str(entry.index)),
                                "group": "array-mapped-tasks"}))
                        rows.append(row(f"   {task.get('index')}  {task.get('state')}  elapsed {short_duration(task['duration']) if task.get('duration') is not None else '?'}" + suffix,
                                        STATES.get(task.get("state"), "dim")))
        rows.append(row(" :array retry ARRAYID SCRIPT --workdir DIR prepares only observed failed tasks.", "dim"))
    elif view == "evidence":
        rows.append(row(" " + result.get("summary", "Evidence unavailable"), "bold"))
        evidence = {e["id"]: dict(e, job=context.get("jid") or getattr(job, "id", None)) for e in result.get("evidence", [])}
        app.research_evidence = evidence
        analysis["evidence_job"] = context.get("jid") or getattr(job, "id", None)
        previous = analysis.get("evidence_ids", [])
        selected = previous[min(analysis.get("evidence_cursor", 0), len(previous) - 1)] if previous else None
        analysis["evidence_ids"] = list(evidence)
        if selected in evidence:
            analysis["evidence_cursor"] = list(evidence).index(selected)
        analysis["evidence_cursor"] = max(0, min(max(0, len(evidence) - 1), analysis.get("evidence_cursor", 0)))
        coverage = result.get("coverage") or {}
        if coverage:
            rows.append(row(f" Log coverage {coverage.get('inspected_files', 0)}/{coverage.get('catalog_files', 0)} files; {coverage.get('omitted_files', 0)} omitted within byte budget", "dim"))
        counts = {}
        for item in evidence.values():
            source = text(item.get("source", "unknown"))
            counts[source] = counts.get(source, 0) + 1
        rows.extend(charts.stacked_bar(g, [(source, count, "cyan" if "log" in source else "magenta") for source, count in counts.items()], width, title="Cited evidence"))
        for hypothesis in result.get("hypotheses", []):
            rows.append(heading(f"{hypothesis.get('name', '?')} / {hypothesis.get('confidence', 'weak')} evidence"))
            for ident in hypothesis.get("support", [])[:6]:
                e = evidence.get(ident, {})
                rows.append(row(f" [{ident}] {e.get('source', '')} {e.get('location', '')}: {e.get('text', '')}", "yellow"))
            for value in hypothesis.get("contradictions", [])[:4]:
                e = evidence.get(value, {}) if isinstance(value, str) else {}
                rows.append(row(f" Counter-evidence [{value}]: {e.get('source', '')} {e.get('location', '')} {e.get('text', '')}", "dim"))
            for value in hypothesis.get("next_checks", [])[:3]:
                rows.append(row(" Check: " + text(value), "cyan"))
        if not result.get("hypotheses"):
            for e in list(evidence.values())[:8]:
                rows.append(row(f" [{e['id']}] {e.get('text', '')}", "dim"))
        for limitation in result.get("limitations", [])[:12]:
            rows.append(row(" Unverified: " + text(limitation), "dim"))
        if evidence:
            rows.append(heading("evidence sources / arrows select, Enter opens cited log"))
            for i, e in enumerate(evidence.values()):
                hits.append((len(rows), "research_evidence", e["id"]))
                selected_ = analysis_ui.rows_selected(app, "evidence") and i == analysis["evidence_cursor"]
                position = f" line {e['line']}" if e.get("line") is not None else ""
                if e.get("line_basis") == "tail-relative":
                    position += " (tail-relative)"
                rows.append(row(f" {'>' if selected_ else ' '} [{e['id']}] {e.get('source', '')} {e.get('path') or e.get('location', '')}{position}", "rev+bold" if selected_ else "cyan"))
                rows.append(row("   " + e.get("text", ""), "yellow" if selected_ else "dim"))
    elif view == "artifacts":
        rows.append(row(" Output contract: " + text(result.get("summary", result.get("status", "?"))), "green" if result.get("valid") else "yellow"))
        counts = {}
        for item in result.get("outputs", []):
            status = item.get("status", "not_checked")
            counts[status] = counts.get(status, 0) + 1
        rows.extend(charts.stacked_bar(g, [(status, count, "green" if status == "valid" else "red" if status == "invalid" else "yellow") for status, count in counts.items()], width, title="Declared outputs"))
        for output in result.get("outputs", []):
            status = output.get("status", "?")
            rows.append(heading(f"{output.get('path', '?')} / {status}"))
            for check in output.get("checks", [])[:32]:
                if isinstance(check, dict):
                    outcome = check.get("status", "not_checked")
                    rows.append(row(f"   {outcome.upper()} {check.get('name', '')}: {check.get('message', '')}",
                                    "green" if outcome == "pass" else "red" if outcome == "fail" else "dim"))
                else:
                    rows.append(row("   " + text(check), "dim"))
            if output.get("stable") is False:
                rows.append(row("   File changed during inspection; this observation is incomplete.", "yellow"))
        for error in result.get("errors", [])[:12]:
            rows.append(row(" ! " + text(error), "yellow"))
        rows.append(row(" Contracts verify declared files and structure; they do not prove scientific correctness.", "dim"))
    elif view == "passport":
        passport = result.get("passport") or {}
        rows.extend(analysis_ui.passport_rows(g, passport, result.get("differences")))
    elif view == "submit":
        plan = result.get("plan") or {}
        from .shell_checks_ui import summary as shell_summary
        controls, control_hits = buttons(g, width,
            [("shellcheck", "Shell checks", ("command", "shellcheck"))],
            group="submit-tools", prefix="submit-tools:")
        hits += [(y + len(rows), kind, data) for y, kind, data in control_hits]
        rows += controls
        rows.append(row(" " + shell_summary(app, plan), "dim"))
        rows.append(row(" Preflight " + ("ready for review" if plan.get("valid") else "blocked by validation issues"), "green" if plan.get("valid") else "red"))
        levels = ("error", "warning", "info")
        rows.extend(charts.stacked_bar(g, [(level, sum(issue.get("level") == level for issue in plan.get("issues", [])), style) for level, style in zip(levels, ("red", "yellow", "cyan"))], width, title="Preflight observations"))
        rows.append(row(" Workdir: " + text(plan.get("workdir", "")), "dim"))
        command = text(plan.get("command", ""))
        # Keep an exact command available in CLI JSON; wrap display without losing characters.
        for i in range(0, len(command), max(1, width - 4)):
            rows.append(row("   " + command[i:i + max(1, width - 4)], "cyan"))
        rows.append(heading("requested resources"))
        for key, value in plan.get("resources", {}).items():
            rows.append(row(f"   {key}: {value}"))
        for issue in plan.get("issues", []):
            rows.append(row(f" {issue.get('level', 'warning')}: {issue.get('message', '')}", "red" if issue.get("level") == "error" else "yellow"))
        rows.append(row(" :submit opens confirmation. Preparation does not contact Slurm.", "dim"))
    else:
        from .planning_views import render as planning_render
        rows.extend(planning_render(views, app, result, width))
    # Scroll the entire bounded document. Visible hits follow the exact same slice.
    if getattr(app, "research_document_mode", False):
        app.research_rows = len(rows)
        chart_interaction.place_since(app, chart_mark, dy=len(nav),
            clip=(len(nav), 0, len(nav) + len(rows), width))
        return nav + rows, nav_hits + [(y + len(nav), kind, key) for y, kind, key in hits]
    avail = max(0, height - len(nav)) if height is not None else len(rows)
    offset = max(0, min(app.research_scroll, max(0, len(rows) - avail)))
    if (view == "arrays" and hits and height is not None and app.research_array_focus
            and not cleared(app, "research")):
        cohorts = [hit for hit in hits if hit[1] == "research_array"]
        selected = cohorts[min(app.cursor.get("research", 0), len(cohorts) - 1)][0]
        if avail and selected < offset:
            offset = selected
        elif avail and selected >= offset + avail:
            offset = max(0, selected - max(0, avail - 1))
        app.research_array_focus = False
    if (view == "evidence" and height is not None and analysis.get("evidence_focus")
            and analysis_ui.rows_selected(app, "evidence")):
        sources = [hit for hit in hits if hit[1] == "research_evidence"]
        if sources and avail:
            selected = sources[min(analysis["evidence_cursor"], len(sources) - 1)][0]
            if selected < offset:
                offset = selected
            elif selected >= offset + avail:
                offset = max(0, selected - avail + 2)
        analysis["evidence_focus"] = False
    app.research_scroll, app.research_rows = offset, len(rows)
    if paint_offset is None:
        paint_offset = scroll_viewport(app, "research:document", offset, len(rows), avail,
                                       context=scroll_context, immediate=height is None)
    paint_offset = max(0, min(paint_offset, max(0, len(rows) - avail)))
    visible_hits = [(y - paint_offset + len(nav), kind, key) for y, kind, key in hits if paint_offset <= y < paint_offset + avail]
    chart_interaction.place_since(app, chart_mark, dy=len(nav) - paint_offset,
        clip=(len(nav), 0, len(nav) + avail, width))
    output = nav + rows[paint_offset:paint_offset + avail]
    if own_document and avail > 0 and width >= 6:
        # Navigation retains its full hit map; the stable heading owns arrows.
        header = len(nav) - 1
        S.register(app, "research:document", (len(nav), 0, len(nav) + avail, width),
                   len(rows), avail, offset, paint_offset,
                   lambda value: setattr(app, "research_scroll", value),
                   context=scroll_context, header=(header, 0, width))
    return output, nav_hits + visible_hits
