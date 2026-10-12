"""Published-geometry gestures: no source reads, stale captures, or wrong jobs."""
from collections import OrderedDict
from dataclasses import replace
import math
from types import SimpleNamespace

import pytest

from raven import analysis_ui as A, chart_interaction as C, layout as L
from raven.interaction import Rect


@pytest.fixture
def app():
    value = SimpleNamespace(mode="main", tab="analytics", width=120, height=40,
                            selected_id="101", analytics_job="101", analytics_view="job",
                            toolbar_state={}, project_state={}, job_panel_state={}, analysis_state={},
                            research=SimpleNamespace(generation=3), messages=[])
    value.say = value.messages.append
    del value.analysis_state
    A.initialize(value)
    C.begin_frame(value, 120, 40)
    return value


def metadata(**changes):
    return {"plot_rect": (1, 10, 11, 60), "x_bounds": (0., 100.), "y_bounds": (0., 200.),
            "valid": True, **changes}


def publish(app, identity=("job", "101", "CPU"), *, bounds=None, row=0, column=0, scale="linear"):
    C.record(app, identity, metadata(**(bounds or {})), row=row, column=column, scale=scale)
    return C.publish(app, app.width, app.height)[-1]


def drag(app, start=(2, 15), end=(8, 45), *, shift=False):
    assert C.handle_mouse(app, *start, button="press", shift=shift)
    assert C.handle_mouse(app, *end, button="drag")
    assert C.handle_mouse(app, *end, button="release")


def test_crosshair_has_cell_accurate_thin_braille_axes_and_theme_accent(app):
    plot = publish(app)
    assert C.hover(app, 5, 30)
    overlay = C.feedback(app)
    assert (5, 30, [("⢼", "accent")]) in overlay
    assert all(plot.visible.contains(y, x) for y, x, _ in overlay)
    assert {char for _, _, row in overlay for char, _ in row} == {"⠤", "⢸", "⢼"}
    assert any(y == 5 and x != 30 for y, x, _ in overlay)
    assert any(x == 30 and y != 5 for y, x, _ in overlay)
    assert len(overlay) == (plot.visible.right - plot.visible.left) + (plot.visible.bottom - plot.visible.top) - 1
    assert all("bold" not in style for _, _, row in overlay for _, style in row)
    assert any(row[0][0] == "." for _, _, row in C.feedback(app, ascii_=True))
    assert not C.hover(app, 0, 30)  # title is not part of the graph
    assert C.feedback(app) == []


@pytest.mark.parametrize("point", [(1, 9), (0, 10), (11, 10), (2, 60), (-1, -1), (39, 119)])
def test_margins_axes_and_other_sections_are_not_captured(app, point):
    publish(app)
    assert not C.handle_mouse(app, *point, button="press")
    assert not C.active(app)


@pytest.mark.parametrize("start,end", [((2,15),(8,45)), ((8,45),(2,15)), ((2,45),(8,15)), ((8,15),(2,45))])
def test_box_zoom_all_drag_directions_maps_x_and_inverted_y(app, start, end):
    plot = publish(app)
    drag(app, start, end, shift=True)
    bounds = C.bounds(app, plot.key)
    assert bounds["x"] == pytest.approx((100*5/49, 100*35/49))
    assert bounds["y"] == pytest.approx((200*2/9, 200*8/9))
    assert C.initialize(app)["revision"] == 1
    assert not C.autofit(app, plot.key)
    assert not C.active(app)


def test_drag_is_a_preview_until_release_and_crosshair_never_modifies_source(app, monkeypatch):
    plot = publish(app)
    for name in ("braille_chart", "vbar_chart"):
        monkeypatch.setattr(C.charts, name, lambda *args, **kwargs: pytest.fail("hover rerasterized a metric"))
    app.store = SimpleNamespace(snapshot=lambda: pytest.fail("mouse took a scheduler snapshot"))
    app.research.current = lambda *args: pytest.fail("mouse accessed a metric source")
    app.research.request = lambda *args: pytest.fail("mouse requested source work")
    for i in range(1000):
        C.hover(app, 2+i%8, 11+i%45)
    assert C.handle_mouse(app, 2, 15, button="press")
    assert C.handle_mouse(app, 8, 45, button="motion")  # held bit omitted by some terminals
    assert C.bounds(app, plot.key) is None
    assert C.initialize(app)["revision"] == 0
    assert all(plot.visible.contains(y,x) for y,x,_ in C.feedback(app))
    assert C.handle_mouse(app, 8, 45, button="release")
    assert C.bounds(app, plot.key)


@pytest.mark.parametrize("end", [(2,15),(8,15),(8,16),(-1,40),(14,40),(8,5),(8,65)])
def test_tiny_and_outside_release_cancel_without_accidental_zoom(app,end):
    plot = publish(app)
    assert C.handle_mouse(app,2,15,button="press")
    assert C.handle_mouse(app,*end,button="drag")
    assert C.handle_mouse(app,*end,button="release")
    assert C.bounds(app,plot.key) is None
    assert not C.active(app)
    assert C.initialize(app)["revision"] == 0


@pytest.mark.parametrize("attribute,value", [("mode","confirm"),("tab","history"),("selected_id","102"),
                                            ("analytics_job","102"),("research_job_id","102"),
                                            ("width",121),("height",41),("analytics_view","queue")])
def test_context_change_cancels_and_consumes_pending_release(app,attribute,value):
    plot = publish(app)
    C.handle_mouse(app,2,15,button="press")
    setattr(app,attribute,value)
    assert C.handle_mouse(app,8,45,button="release")
    assert not C.active(app) and C.bounds(app,plot.key) is None
    assert C.feedback(app) == []


@pytest.mark.parametrize("change", ["menu","toolbar_panel","generation","job_panel","project","run","attempt","source"])
def test_source_view_and_overlay_changes_cancel_capture(app,change):
    plot=publish(app)
    C.handle_mouse(app,2,15,button="press")
    if change == "menu": app.toolbar_state["menu"]="View"
    elif change == "toolbar_panel": app.toolbar_state["panel"]="help"
    elif change == "generation": app.research.generation += 1
    elif change == "job_panel": app.job_panel_state["mode"]="quick"
    elif change == "project": app.project_state["root"]="/other"
    elif change in ("run","attempt"):
        app.project_state["binding"]={"job_id":"101","run_id":"r2", "attempt":2}
    else:
        C.begin_frame(app,120,40)
        publish(app,identity=("job","101","other exact source"))
    C.handle_mouse(app,8,45,button="release")
    assert C.bounds(app,plot.key) is None and not C.active(app)


@pytest.mark.parametrize("change", ["plot_rect","x_bounds","y_bounds"])
def test_live_geometry_or_axis_change_cancels_drag_instead_of_mapping_old_bounds(app,change):
    original=publish(app)
    C.handle_mouse(app,2,15,button="press")
    C.begin_frame(app,120,40)
    changed={"plot_rect":(2,10,12,60),"x_bounds":(1.,101.),"y_bounds":(0.,201.)}[change]
    publish(app,bounds={change:changed})
    assert not C.active(app)
    assert C.bounds(app,original.key) is None


def test_unchanged_live_frame_keeps_capture_then_commits_same_scope(app):
    plot=publish(app)
    C.handle_mouse(app,2,15,button="press")
    C.begin_frame(app,120,40)
    publish(app)
    assert C.active(app)
    C.handle_mouse(app,8,45,button="release")
    assert C.bounds(app,plot.key)


def test_timeout_and_escape_do_not_commit(app):
    plot=publish(app)
    C.handle_mouse(app,2,15,button="press")
    capture=C.initialize(app)["capture"]
    C.tick(app,now=capture["last"]+C.CAPTURE_TIMEOUT+.001)
    assert not C.active(app)
    C.handle_mouse(app,2,15,button="press")
    assert C.handle_key(app,"esc")
    assert C.bounds(app,plot.key) is None
    assert not C.active(app)


@pytest.mark.parametrize("button", ["wheel-up","wheel-down"])
def test_other_gestures_cancel_capture_and_remain_available_to_native_handlers(app,button):
    plot=publish(app)
    C.handle_mouse(app,2,15,button="press")
    assert not C.handle_mouse(app,8,45,button=button)
    assert not C.active(app) and C.bounds(app,plot.key) is None


def test_new_press_replaces_missing_release_and_drag_never_escapes_visible_plot(app):
    plot=publish(app)
    C.handle_mouse(app,2,15,button="press")
    C.handle_mouse(app,4,25,button="press")
    assert C.initialize(app)["capture"]["start"] == (4,25)
    C.handle_mouse(app,99,999,button="drag")
    assert all(plot.visible.contains(y,x) for y,x,_ in C.feedback(app))
    C.handle_mouse(app,99,999,button="release")
    assert C.bounds(app,plot.key) is None


def test_log_zoom_uses_log10_coordinates_and_does_not_rewrite_measurements(app):
    plot=publish(app,bounds={"y_bounds":(-3.,3.)},scale="log")
    drag(app)
    value=C.bounds(app,plot.key,scale="log")
    assert value["y"] == pytest.approx((-3+6*2/9,-3+6*8/9))
    assert C.bounds(app,plot.key,scale="linear") is None


@pytest.mark.parametrize("bounds", [(-1e308,1e308),(1e-310,2e-310),(-1e-310,1e-310),(1e300,1.0000001e300)])
def test_zoom_interpolation_is_finite_for_extreme_axes(app,bounds):
    plot=publish(app,bounds={"x_bounds":bounds,"y_bounds":bounds})
    drag(app)
    value=C.bounds(app,plot.key)
    assert all(math.isfinite(number) for axis in value.values() for number in axis)
    assert all(axis[0]<axis[1] for axis in value.values())


def test_undo_reset_are_local_to_exact_source_and_history_is_bounded(app):
    first=publish(app)
    drag(app)
    original=C.bounds(app,first.key)
    C.begin_frame(app,120,40)
    publish(app,bounds={"x_bounds":original["x"],"y_bounds":original["y"]})
    drag(app)
    assert C.bounds(app,first.key)!=original
    assert C.undo(app,first.key)
    assert C.bounds(app,first.key)==original
    assert C.undo(app,first.key)
    assert C.bounds(app,first.key) is None
    second=("job","102","CPU")
    C.begin_frame(app,120,40)
    publish(app,second)
    drag(app)
    assert C.bounds(app,second) and C.bounds(app,first.key) is None
    assert C.reset(app,second)
    assert C.bounds(app,second) is None
    for i in range(C.MAX_ZOOMS+10):
        plot=replace(first,key=("source",str(i)))
        C._apply(app,plot,{"x":(1.,2.),"y":(3.,4.)})
    assert len(C.initialize(app)["zoom"])==C.MAX_ZOOMS
    for i in range(C.MAX_UNDO+10):
        C._apply(app,first,{"x":(1.,2.),"y":(3.,4.)})
    assert len(C.initialize(app)["zoom"][first.key]["undo"])==C.MAX_UNDO


def test_key_controls_only_capture_when_pointer_is_over_a_current_graph(app):
    plot=publish(app)
    drag(app)
    assert C.handle_key(app,"u")
    assert C.bounds(app,plot.key) is None
    assert not C.handle_key(app,"up")
    C.hover(app,0,0)
    assert not C.handle_key(app,"0")
    assert C.run_command(app,["chartzoom","undo"])
    assert not C.run_command(app,["unrelated"])
    assert C.run_command(app,["chartzoom","invalid"])
    assert "chartzoom undo|reset" in app.messages[-1]


def test_project_key_uses_only_matching_exact_run_attempt(app):
    app.project_state={"root":"/project", "binding":{"project_root":"/project", "job_id":"101", "run_id":"run-A", "attempt":2}}
    one=C.key(app,"loss","metrics.jsonl","101")
    app.project_state["binding"]["attempt"]=3
    assert one!=C.key(app,"loss","metrics.jsonl","101")
    unrelated=C.key(app,"loss","metrics.jsonl","102")
    app.project_state["binding"]["run_id"]="run-B"
    assert unrelated==C.key(app,"loss","metrics.jsonl","102")
    assert C.key(app,"loss","x"*(C.MAX_KEY_TEXT+1),"101") is None


def test_nested_scroll_dock_and_split_placements_keep_full_axis_mapping(app):
    first=C.mark(app)
    C.record(app,("job","101","CPU"),metadata())
    C.place_since(app,first,dy=-4,clip=(0,0,8,70))
    C.place_since(app,first,dy=10,dx=40,clip=(10,40,20,120))
    plot=C.publish(app,120,40)[0]
    assert plot.rect==Rect(7,50,17,100)
    assert plot.visible==Rect(10,50,17,100)
    C.handle_mouse(app,10,55,button="press")
    C.handle_mouse(app,15,85,button="release")
    assert C.bounds(app,plot.key)["y"]==pytest.approx((200*1/9,200*6/9))


def test_discarded_width_candidates_and_offscreen_plots_are_never_published(app):
    marker=C.mark(app)
    C.record(app,("discarded",),metadata())
    discarded=C.take_since(app,marker)
    C.record(app,("visible",),metadata())
    chosen=C.take_since(app,marker)
    assert C.publish(app,120,40)==()
    C.put_records(app,chosen)
    assert C.publish(app,120,40)[0].key==("visible",)
    assert discarded[0].key!=("visible",)
    C.begin_frame(app,120,40)
    C.put_records(app,C.map_records(chosen,{i:i+100 for i in range(1,11)},clip=(0,0,40,120)))
    assert C.publish(app,120,40)==()


@pytest.mark.parametrize("kind",["missing-middle","wrapped-middle","reordered"])
def test_noncontiguous_plot_row_mapping_is_rejected(app,kind):
    C.record(app,("plot",),metadata())
    records=C.take_since(app,0)
    mapping={i:i+5 for i in range(1,11)}
    if kind=="missing-middle":mapping.pop(6)
    elif kind=="wrapped-middle":mapping[6]+=1
    else:mapping[6]=2
    assert C.map_records(records,mapping)==()


def test_sticky_header_mapping_clips_only_removed_edges_preserving_coordinates(app):
    C.record(app,("plot",),metadata())
    records=C.take_since(app,0)
    mapped=C.map_records(records,{i:i+5 for i in range(4,9)},dx=30,dy=2,clip=(0,0,40,120))
    assert mapped[0].rect==Rect(8,40,18,90)
    assert mapped[0].visible==Rect(11,40,16,90)


def test_same_source_curve_and_area_capture_matches_its_actual_geometry(app):
    C.record(app,("same",),metadata())
    C.record(app,("same",),metadata(),row=15)
    C.publish(app,120,40)
    C.handle_mouse(app,17,15,button="press")
    assert C.active(app)
    C.handle_mouse(app,23,45,button="release")
    assert C.bounds(app,("same",))


def test_menu_blocking_preserves_pending_records_for_cached_close_feedback(app):
    C.record(app,("plot",),metadata())
    app.toolbar_state["menu"]="View"
    assert C.publish(app,120,40)==()
    app.toolbar_state["menu"]=None
    assert len(C.publish(app,120,40))==1
    assert C.hover(app,5,30)


def test_modal_and_startup_allow_only_the_visible_chart_layer(app,monkeypatch):
    C.record(app,("body",),metadata())
    C.record(app,("modal",),metadata(),layer=1)
    app.mode="analysis";app.analysis_state["modal"]="chart"
    assert C.publish(app,120,40)[0].key==("modal",)
    app.analysis_state["modal"]="inspect"
    assert C.publish(app,120,40)==()
    app.mode="main"
    app.startup_state={"running":True}
    from raven import startup
    monkeypatch.setattr(startup,"active",lambda app:True)
    assert C.publish(app,120,40)==()


@pytest.mark.parametrize("change", [{"x_bounds":(0,0)}, {"x_bounds":(float("inf"),2)},
                                    {"y_bounds":(2,1)}, {"plot_rect":(1,10,2,60)},
                                    {"plot_rect":(1,10,11,12)}, {"plot_rect":(1,10,1000,60)},
                                    {"valid":False},{"x_bounds":None},{"has_data":False}])
def test_invalid_unsupported_or_tiny_graphs_do_not_capture(app,change):
    assert C.record(app,("bad",),metadata(**change)) is None
    assert C.publish(app,120,40)==()


def test_registry_and_feedback_memory_are_bounded(app):
    for i in range(C.MAX_PLOTS+100):
        C.record(app,("plot",str(i)),metadata())
    assert len(C.publish(app,120,40))==C.MAX_PLOTS
    assert len(C.initialize(app)["pending"])==C.MAX_PLOTS
    C.hover(app,5,30)
    assert len(C.feedback(app))<=C.MAX_OVERLAY_CELLS


@pytest.mark.parametrize("ascii_",[False,True])
def test_chart_cards_read_source_scoped_box_without_fabricating_samples(app,ascii_):
    points=[{"t":float(t),"value":t+1.} for t in range(11)]
    source="/project/runs/101/metrics.jsonl"
    identity=A.chart_key(app,"loss",source,interactive=False)
    A.initialize(app)["axes"]["loss"]={"mode":"log"}
    C._apply(app,replace(publish(app),key=identity,scale="log"),{"x":(2.,8.),"y":(0.,2.)})
    metadata_out={}
    rows=A.chart_rows(L.Glyphs(ascii_),app,points,80,5,"loss",source,interactive=False,metadata=metadata_out)
    assert metadata_out["x_bounds"]==(2.,8.) and metadata_out["y_bounds"]==(0.,2.)
    assert metadata_out["scale"]=="log"
    assert "7/11 samples" in L.row_text(rows[-1])
    assert points==[{"t":float(t),"value":t+1.} for t in range(11)]
    alternate={}
    A.chart_rows(L.Glyphs(ascii_),app,points,80,5,"loss","/other/metrics.jsonl",interactive=False,metadata=alternate)
    assert alternate["x_bounds"]==(0.,10.)
    # An interval with no observations stays an empty graph at that exact range.
    C._apply(app,replace(publish(app),key=identity,scale="log"),{"x":(3.1,3.9),"y":(0.,2.)})
    empty={}
    rows=A.chart_rows(L.Glyphs(ascii_),app,points,80,5,"loss",source,interactive=False,metadata=empty)
    assert empty["x_bounds"]==(3.1,3.9) and "0/11 samples" in L.row_text(rows[-1])


@pytest.mark.parametrize("point", [(None,1),(1,None),(True,1),(1,False),(1.5,2),(1,"2")])
def test_malformed_pointer_reports_cancel_without_raising_or_committing(app,point):
    plot=publish(app)
    assert not C.hover(app,*point)
    assert not C.handle_mouse(app,*point,button="press")
    C.handle_mouse(app,2,15,button="press")
    assert C.handle_mouse(app,*point,button="release")
    assert not C.active(app) and C.bounds(app,plot.key) is None


def test_inspector_records_exact_modal_box_and_keyboard_samples_follow_box_zoom(app):
    app.mode="analysis"
    app.analysis_state.update(modal="chart",chart_job="101",metric="loss",chart_events=False)
    app.analysis_result={"path":"reported.jsonl", "series":{
        "loss":[{"t":float(t),"value":float(t)} for t in range(11)],
        "accuracy":[{"t":float(t),"value":float(t)/10} for t in range(11)]}}
    app.analysis_result_job="101"
    app.analysis_result_generation=3
    app.store=SimpleNamespace(snapshot=lambda:pytest.fail("keyboard reread published series"))
    C.begin_frame(app,120,40)
    rendered=A.overlay(SimpleNamespace(g=L.Glyphs(False)),{},app,120,40)
    plot=C.publish(app,120,40)[0]
    assert plot.layer==1
    assert all(rendered[0][0]<y<rendered[-1][0] for y in range(plot.visible.top,plot.visible.bottom))
    C.handle_mouse(app,plot.visible.top+1,plot.visible.left+8,button="press")
    C.handle_mouse(app,plot.visible.bottom-2,plot.visible.right-9,button="release")
    bounds=C.bounds(app,plot.key)
    assert 0<bounds["x"][0]<bounds["x"][1]<10
    C.begin_frame(app,120,40)
    zoomed=A.overlay(SimpleNamespace(g=L.Glyphs(False)),{},app,120,40)
    labels="\n".join(L.row_text(row) for _,_,row in zoomed)
    assert "Time zoom t=" in labels and "zoomed Y" in labels and "fits observed interval" in labels
    assert "Zoom x1 |" not in labels
    C.publish(app,120,40)
    assert A.handle_key(app,"home")
    points=A._chart_visible_points(app,app.analysis_result["series"],"loss")
    assert all(bounds["x"][0]<=p["t"]<=bounds["x"][1] for p in points)
    assert A.handle_key(app,"tab")
    assert app.analysis_state["metric"]=="accuracy"
    assert len(A._chart_visible_points(app,app.analysis_result["series"],"accuracy"))==11


def test_comparison_elapsed_curves_publish_in_the_visible_modal_with_exact_jobs(app):
    from raven.model import Job
    app.mode="analysis"
    app.analysis_state.update(modal="diff",diff_ids=["101","102"],shared_scale=True)
    jobs={jid:Job(jid,"same name","cpu","RUNNING",cpus=2) for jid in ("101","102")}
    app.job_record=lambda jid,snap:jobs[jid]
    app.store=SimpleNamespace(series={jid:[{"t":100.+t,"k":"live","cpu":t/10,"rss":2**30}
                                          for t in range(11)] for jid in jobs})
    C.begin_frame(app,120,80)
    app.height=80
    rendered=A.overlay(SimpleNamespace(g=L.Glyphs(False)),{},app,120,80)
    plots=C.publish(app,120,80)
    assert len(plots)>=2 and all(p.layer==1 for p in plots)
    assert {p.key[1] for p in plots}=={"101","102"}
    plot=plots[0]
    assert plot.x_bounds==(0,10.)
    C.handle_mouse(app,plot.visible.top,plot.visible.left+8,button="press")
    C.handle_mouse(app,plot.visible.bottom-1,plot.visible.right-9,button="release")
    assert C.bounds(app,plot.key)
    assert all(C.bounds(app,p.key) is None for p in plots[1:])
    assert rendered[0][0]<=plot.visible.top<rendered[-1][0]


@pytest.mark.parametrize("change", ["job", "mode", "view"])
def test_history_native_capture_rejects_a_changed_details_source(app, change):
    app.tab = "history"
    app.job_panel_state.update(mode="analytics", analytics_view="job")
    C.begin_frame(app, app.width, app.height)
    identity = ("resource-cpu", "101", "cpu", "session", "scheduler:attempt")
    plot = publish(app, identity=identity)
    assert C.handle_mouse(app, 2, 15, button="press")
    if change == "job":
        app.selected_id = "102"
    elif change == "mode":
        app.job_panel_state["mode"] = "logs"
    else:
        app.job_panel_state["analytics_view"] = "compare"
    assert C.handle_mouse(app, 8, 45, button="release")
    assert not C.active(app) and C.bounds(app, plot.key) is None


def test_history_native_capture_survives_unrelated_report_refresh(app):
    app.tab = "history"
    app.job_panel_state.update(mode="analytics", analytics_view="job")
    C.begin_frame(app, app.width, app.height)
    identity = ("resource-cpu", "101", "cpu", "session", "scheduler:attempt")
    plot = publish(app, identity=identity)
    assert C.handle_mouse(app, 2, 15, button="press")
    app.research.generation += 1
    app.project_state["root"] = "/unrelated/refresh"
    assert C.handle_mouse(app, 8, 45, button="release")
    assert C.bounds(app, plot.key) is not None
