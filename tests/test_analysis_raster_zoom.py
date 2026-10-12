"""Zoom clips original curve segments without inventing observed samples."""
import copy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from raven import analysis_ui as A, chart_interaction as C, charts, layout as L
from raven.interaction import Rect


def app_and_key(axis="linear"):
    app=SimpleNamespace(selected_id="101",tab="research",research_job_id="101",
                        research=SimpleNamespace(generation=1),project_state={},mode="main",
                        toolbar_state={},job_panel_state={},width=100,height=40)
    A.initialize(app)
    if axis=="log":app.analysis_state["axes"]["loss"]={"mode":"log"}
    key=A.chart_key(app,"loss","reported.jsonl",interactive=False)
    return app,key


def box(app,key,x,y,scale="linear"):
    plot=C.Plot(key,Rect(0,0,4,20),Rect(0,0,4,20),(0.,100.),(0.,100.),scale)
    C._apply(app,plot,{"x":x,"y":y})


def draw(app,points,key,ascii_=False,interactive=False):
    metadata={}
    rows=A.chart_rows(L.Glyphs(ascii_),app,points,70,8,"loss","reported.jsonl",
                      interactive=interactive,zoom_key=key,metadata=metadata,snapshot={})
    return rows,metadata


def ink(rows,metadata,ascii_=False):
    top,left,bottom,right=metadata["plot_rect"]
    allowed=set("/\\-:|+") if ascii_ else set(charts.BRAILLE[1:])
    return [[ch in allowed for ch in L.row_text(row)[left:right]] for row in rows[top:bottom]]


@pytest.mark.parametrize("ascii_",[False,True])
@pytest.mark.parametrize("interactive",[False,True])
@pytest.mark.parametrize("axis",["linear","log"])
def test_between_observations_zoom_preserves_curve_without_claiming_samples(ascii_,interactive,axis):
    app,key=app_and_key(axis)
    # No sample falls within x=3..7. Its two adjacent measured endpoints still
    # define the same observed curve, clipped into that interval for display.
    values=[1.,10.,100.,1000.] if axis=="log" else [0.,10.,20.,30.]
    points=[{"t":float(t),"value":v} for t,v in zip((0,10,20,30),values)]
    original=copy.deepcopy(points)
    box(app,key,(3.,7.),(0.,1.) if axis=="log" else (0.,10.),axis)
    rows,metadata=draw(app,points,key,ascii_,interactive)
    measured=ink(rows,metadata,ascii_)
    assert any(any(row) for row in measured)
    assert all(any(row[x] for row in measured) for x in range(len(measured[0])))
    assert metadata["x_bounds"]==(3.,7.)
    assert "0/4 samples" in L.row_text(rows[-1])
    assert "awaiting samples" in L.row_text(rows[0])
    assert not any("t=" in L.row_text(row) and "value " in L.row_text(row) for row in rows)
    assert points==original
    if interactive:assert app.analysis_state["chart_visible"]["points"]==[]


@pytest.mark.parametrize("ascii_",[False,True])
@pytest.mark.parametrize("kind",["missing-before","missing-after","explicit-interior-gap","outage","nonpositive-log"])
def test_neighbor_retention_never_bridges_missing_or_outage_evidence(ascii_,kind):
    axis="log" if kind=="nonpositive-log" else "linear"
    app,key=app_and_key(axis)
    times=[0,10,20,30]
    values=[0.,10.,20.,30.]
    window=(3.,7.)
    if kind=="missing-before":values[0]=None
    elif kind=="missing-after":values[1]=None
    elif kind=="explicit-interior-gap":
        times=[0,5,10,20,30];values=[0.,None,10.,20.,30.]
    elif kind=="outage":
        times=[0,1,2,1000,1001,1002];values=[0.,1.,2.,3.,4.,5.];window=(20.,30.)
    else:values=[0.,10.,100.,1000.]
    points=[{"t":float(t),"value":v} for t,v in zip(times,values)]
    box(app,key,window,(0.,1.) if axis=="log" else (0.,10.),axis)
    rows,metadata=draw(app,points,key,ascii_)
    assert not any(any(row) for row in ink(rows,metadata,ascii_))
    assert metadata["x_bounds"]==window


@pytest.mark.parametrize("ascii_",[False,True])
@pytest.mark.parametrize("window",[(-20.,-10.),(40.,50.),(3.,7.)])
def test_truly_empty_or_one_sided_observations_stay_empty(ascii_,window):
    app,key=app_and_key()
    points=[] if window==(3.,7.) else [{"t":0.,"value":0.},{"t":10.,"value":10.}]
    box(app,key,window,(0.,10.))
    rows,metadata=draw(app,points,key,ascii_)
    assert not any(any(row) for row in ink(rows,metadata,ascii_))
    assert metadata["x_bounds"]==window


@pytest.mark.parametrize("ascii_",[False,True])
def test_visible_header_cursor_and_statistics_exclude_raster_neighbors(ascii_):
    app,key=app_and_key()
    app.analysis_state["chart_events"]=False
    points=[{"t":0.,"value":1000.},{"t":10.,"value":10.},{"t":20.,"value":20.},{"t":30.,"value":2000.}]
    box(app,key,(5.,25.),(0.,2000.))
    rows,metadata=draw(app,points,key,ascii_,interactive=True)
    assert "2/4 samples" in L.row_text(rows[-1])
    assert "max 20" in L.row_text(rows[0]) and "2000" not in L.row_text(rows[0])
    exact="\n".join(L.row_text(row) for row in rows if "value " in L.row_text(row))
    assert "value 10.0" in exact and "t=10.0" in exact
    assert [point["t"] for point in app.analysis_state["chart_visible"]["points"]]==[10.,20.]
    assert any(any(row) for row in ink(rows,metadata,ascii_))


@pytest.mark.parametrize("ascii_",[False,True])
def test_cached_card_detects_bracketing_neighbor_correction_and_true_gap(ascii_,monkeypatch):
    app,key=app_and_key()
    points=[{"t":0.,"value":0.},{"t":10.,"value":10.},{"t":20.,"value":20.},{"t":30.,"value":30.}]
    box(app,key,(3.,7.),(0.,10.))
    calls=[];raster=A.charts.braille_chart
    monkeypatch.setattr(A.charts,"braille_chart",lambda *args,**kwargs:(calls.append(1),raster(*args,**kwargs))[1])
    rows,metadata=draw(app,points,key,ascii_)
    repeated,_=draw(app,points,key,ascii_)
    assert rows==repeated and len(calls)==1
    points[0]["value"]=5.
    corrected,_=draw(app,points,key,ascii_)
    assert corrected!=rows and len(calls)==2
    points[0]["value"]=None
    missing,missing_meta=draw(app,points,key,ascii_)
    assert not any(any(row) for row in ink(missing,missing_meta,ascii_))
    assert "0/4 samples" in L.row_text(missing[-1]) and len(calls)==3


@pytest.mark.parametrize("ascii_",[False,True])
def test_elapsed_comparison_keeps_crossing_neighbors_but_exact_job_headers_remain_empty(ascii_):
    from raven.model import Job
    app,_=app_and_key()
    app.mode="analysis"
    app.analysis_state.update(modal="diff",diff_ids=["101","102"])
    jobs={jid:Job(jid,"same name","cpu","RUNNING",cpus=2) for jid in ("101","102")}
    app.job_record=lambda jid,snap:jobs[jid]
    app.store=SimpleNamespace(series={jid:[{"t":100.+t,"k":"live","cpu":t/100}
                                          for t in (0,10,20,30)] for jid in jobs})
    key=C.key(app,"CPU per core (%)","Raven session resource samples","101",
              scope="comparison-elapsed",attempt=100.)
    box(app,key,(3.,7.),(0.,10.))
    C.begin_frame(app,100,40)
    rows=A._job_diff_rows(L.Glyphs(ascii_),{},app,70)
    records=C.take_since(app,0)
    selected=next(plot for plot in records if plot.key==key)
    geometry={"plot_rect":(selected.rect.top,selected.rect.left,selected.rect.bottom,selected.rect.right)}
    assert any(any(row) for row in ink(rows,geometry,ascii_))
    assert selected.x_bounds==(3.,7.) and selected.y_bounds==(0.,10.)
    assert "awaiting samples" in L.row_text(rows[selected.rect.top-1])
    other=next(plot for plot in records if plot.key[1]=="102" and plot.key[2]=="CPU per core (%)")
    assert other.x_bounds==(0,30.) and "last 30%" in L.row_text(rows[other.rect.top-1])


def test_time_window_auto_y_scale_ignores_large_outside_neighbors():
    app,key=app_and_key()
    app.analysis_state.update(zoom=2.,pan=.5,chart_events=False)
    points=[{"t":0.,"value":1000.},{"t":10.,"value":10.},{"t":20.,"value":20.},{"t":30.,"value":2000.}]
    rows,metadata=draw(app,points,key,interactive=True)
    assert metadata["x_bounds"]==(7.5,22.5)
    assert metadata["y_bounds"]==(0.,21.)
    assert "2/4 samples" in L.row_text(rows[-1])
    assert "max 20" in L.row_text(rows[0])
