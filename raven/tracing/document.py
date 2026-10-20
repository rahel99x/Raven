"""Private offline trace HTML generation; no desktop/UI request effects."""

from __future__ import annotations
import base64
import hashlib
import json
import os
from pathlib import Path
import uuid

MAX_OUTPUT_BYTES = 32 << 20

_STYLE = r"""
:root { color-scheme: dark; font: 15px system-ui,sans-serif; background:#12131b; color:#e8e9f0 }
body { margin:0 auto; padding:1.2rem; max-width:1600px } h1 { font-size:1.6rem; margin:.2rem 0 }
p { line-height:1.5 } button,input,select { font:inherit; color:inherit; background:#232638;
 border:1px solid #666b89; border-radius:5px; padding:.45rem .65rem }
button { cursor:pointer } button:hover { background:#343950 } :focus-visible { outline:3px solid #ff8cdc; outline-offset:2px }
.controls { display:flex; align-items:center; flex-wrap:wrap; gap:.5rem; margin:1rem 0 }
.controls label { display:flex; align-items:center; gap:.4rem } input { max-width:18rem }
.muted { color:#b7bdd3 } .warning { color:#ffd28a } #warnings { white-space:pre-wrap }
#viewport { height:480px; overflow:auto; border:1px solid #666b89; border-radius:6px; background:#171a25 }
#surface { position:relative; min-height:480px } canvas { display:block; position:sticky; top:0; width:100%; height:480px; touch-action:none }
#details { white-space:pre-wrap; overflow-wrap:anywhere; background:#1c2030; padding:.8rem; min-height:4rem; border-radius:6px }
#results,#markers { width:100%; border-collapse:collapse } th,td { padding:.4rem; text-align:left; border-bottom:1px solid #343950; overflow-wrap:anywhere }
#results button,#markers button { text-align:left; max-width:100%; overflow-wrap:anywhere }
small { display:block; margin-top:.6rem } summary { cursor:pointer } #provenance { white-space:pre-wrap; overflow-wrap:anywhere }
@media (max-width:600px) { body { padding:.6rem } th:nth-child(3),td:nth-child(3) { display:none } }
"""

_SCRIPT = r"""
'use strict';
const model = JSON.parse(document.getElementById('trace-data').textContent);
const byId = id => document.getElementById(id);
const canvas = byId('timeline'), ctx = canvas.getContext('2d'), viewport = byId('viewport');
let intervals = model.intervals, markers = model.markers;
const end = Math.max(model.plot_end_ms, .000001), rowHeight = 23, header = 36;
let left = 0, right = end, selected = null, lanes = [], filtered = [], filteredMarkers = [], page = 0, markerPage = 0, drag = null, drawn = [], raf = 0, layoutKey = '';
const safe = value => value == null ? 'unknown' : String(value);
const elapsed = ms => ms >= 1000 ? (ms/1000).toFixed(3)+' s' : ms >= 1 ? ms.toFixed(3)+' ms' : (ms*1000).toFixed(3)+' µs';
const duration = item => elapsed(item.plot_end_ms-item.plot_start_ms);
const gutterFor = width => byId('view').value==='cores'?Math.min(320,Math.max(160,width*.3)):Math.min(175,Math.max(100,width*.22));
function text(id, value) { byId(id).textContent = value; }
text('identity', 'Job '+safe(model.identity.job_id)+' · attempt '+safe(model.identity.attempt)+' · PID '+safe(model.identity.pid));
text('coverage', safe(model.coverage.adapter)+' · '+safe(model.coverage.scope)+' · '+model.status+' · '+model.retained_events+' retained events · '+intervals.length+' matched intervals · verified '+model.read_seq+' / '+model.committed_seq+' committed'+(model.caught_up?' · caught up':' · unread committed events remain'));
const limits = ['Verified retained wall-time intervals from this process only. No CPU samples, hardware branch trace, GPU kernel durations, or cross-process clock alignment are inferred.',
 'This file is a fixed snapshot; export again to include later events. Unmatched boundaries remain markers and are not assigned invented durations.'];
text('warnings', [...model.warnings, 'Unpaired frame events: '+model.unpaired_frame_events+' · evicted events: '+model.evicted_events, ...limits].join('\n'));
text('provenance', JSON.stringify({identity:model.identity,coverage:model.coverage,status:model.status,origin_ns:model.origin_ns,
 read_seq:model.read_seq,committed_seq:model.committed_seq,sha256:model.sha256}, null, 2));
for (const thread of model.threads) { const option = document.createElement('option'); option.value=thread; option.textContent=thread; byId('thread').append(option); }
function lower(items, value, getter) { let low=0, high=items.length; while(low<high) { const mid=(low+high)>>>1; if(getter(items[mid])<value) low=mid+1; else high=mid; } return low; }
function rebuild() {
 const thread=byId('thread').value, query=byId('search').value.toLocaleLowerCase(), cores=byId('view').value==='cores';
 intervals=cores?model.cpu.observations:model.intervals;markers=cores?[]:model.markers;
 text('view-note',cores?'CPU placement: '+model.cpu.status+'. '+model.cpu.warnings.join(' ')+' Only queried placements are shown. Query brackets do not establish CPU residency between events. Socket/core/CPU IDs are the operating system’s actual IDs.':'Recorded physical-thread call nesting and cooperative span lanes. Wall-time intervals do not imply CPU execution throughout the interval.');
 text('duration-heading',cores?'Query bracket':'Wall time');byId('marker-section').hidden=cores;
 const groups=new Map();
 filtered=intervals.filter(item=>(!thread||item.thread===thread)&&(!query||(item.name+' '+safe(item.file)).toLocaleLowerCase().includes(query)));
 filteredMarkers=markers.filter(item=>(!thread||item.thread===thread)&&(!query||(item.name+' '+item.kind).toLocaleLowerCase().includes(query)));
 const nextLayout=JSON.stringify([byId('view').value,thread]);
 if(nextLayout!==layoutKey) {
 layoutKey=nextLayout;
 for(const item of intervals) {
  if(thread&&item.thread!==thread) continue;
  const key=JSON.stringify([item.lane_key||item.thread,item.kind,item.depth]);
  if(!groups.has(key)) groups.set(key,{thread:item.lane_key||item.thread,label:item.lane_label||item.thread+' · '+item.kind+' '+item.depth,kind:item.kind,depth:item.depth,order:item.lane_order||0,items:[],ends:[]});
  groups.get(key).items.push(item);
 }
 lanes=[...groups.values()].sort((a,b)=>a.order-b.order||a.thread.localeCompare(b.thread)||a.kind.localeCompare(b.kind)||a.depth-b.depth);
 for(const lane of lanes) { lane.items.sort((a,b)=>a.plot_start_ms-b.plot_start_ms);let max=-Infinity; for(const item of lane.items) { max=Math.max(max,item.plot_end_ms); lane.ends.push(max); } }
 byId('surface').style.height=Math.max(viewport.clientHeight,header+lanes.length*rowHeight)+'px';
 }
 page=0; markerPage=0; renderResults(); renderMarkers(); schedule();
}
function renderResults() {
 const body=byId('result-body'); body.replaceChildren();
 const maxPage=Math.max(0,Math.ceil(filtered.length/50)-1); page=Math.min(maxPage,Math.max(0,page));
 text('matches', filtered.length+' matching intervals · page '+(page+1)+' / '+(maxPage+1));
 byId('previous').disabled=page===0; byId('next').disabled=page===maxPage;
 for(const item of filtered.slice(page*50,(page+1)*50)) {
  const row=document.createElement('tr'), name=document.createElement('td'), button=document.createElement('button');
  button.textContent=item.name; button.addEventListener('click',()=>select(item,true)); name.append(button); row.append(name);
  for(const value of [duration(item),item.thread,item.lane_label||item.kind+' / '+item.depth]) { const cell=document.createElement('td'); cell.textContent=value; row.append(cell); }
  body.append(row);
 }
}
function selectMarker(marker) {
 text('details','Observed marker: '+marker.name+'\nKind: '+marker.kind+' · thread: '+marker.thread+'\nRelative time: '+marker.start_ns+' ns · sequence: '+marker.seq+'\nNo interval duration is inferred from this marker.');
}
function renderMarkers() {
 const body=byId('marker-body');body.replaceChildren();const max=Math.max(0,Math.ceil(filteredMarkers.length/50)-1);markerPage=Math.min(max,Math.max(0,markerPage));
 text('marker-count',filteredMarkers.length+' matching markers · page '+(markerPage+1)+' / '+(max+1));
 byId('markers-previous').disabled=markerPage===0;byId('markers-next').disabled=markerPage===max;
 for(const marker of filteredMarkers.slice(markerPage*50,(markerPage+1)*50)) {
  const row=document.createElement('tr'),cell=document.createElement('td'),button=document.createElement('button');button.textContent=marker.name;button.addEventListener('click',()=>selectMarker(marker));cell.append(button);row.append(cell);
  for(const value of [elapsed(marker.plot_start_ms),marker.thread,marker.kind]){const item=document.createElement('td');item.textContent=value;row.append(item);}body.append(row);
 }
}
function select(item, focus=false) {
 selected=item;
 if(item.kind==='placement') {
 text('details','CPU placement observation: '+item.name+'\n'+item.lane_label+' · thread '+item.thread+'\nQuery bracket: '+item.start_ns+'–'+item.end_ns+' ns relative to this process’s retained origin'+
 '\nRecorded sequence: '+item.first_seq+' · source: '+item.source+'\nThe marker shows a queried placement. Execution, migrations and idle time between observations remain unknown.');
 } else text('details', item.name+'\n'+item.kind+' · thread '+item.thread+' · depth '+item.depth+'\nFile: '+safe(item.file)+':'+safe(item.line)+
 '\nRelative start: '+item.start_ns+' ns · end: '+item.end_ns+' ns · duration: '+item.duration_ns+' ns'+
 '\nRecorded sequence: '+item.first_seq+'–'+item.last_seq+' · exit: '+safe(item.exit));
 if(focus) { const span=Math.max(item.plot_end_ms-item.plot_start_ms,end/10000,.000001); setWindow(item.plot_start_ms-span*.15,item.plot_end_ms+span*.15); }
 schedule();
}
function setWindow(a,b) {
 let span=Math.max(end/1000000,Math.min(end,b-a)); a=Math.min(Math.max(0,a),Math.max(0,end-span)); left=a;right=a+span; schedule();
}
function zoom(factor,center=(left+right)/2) { const ratio=(center-left)/(right-left), span=(right-left)*factor; setWindow(center-span*ratio,center+span*(1-ratio)); }
function schedule() { if(!raf) raf=requestAnimationFrame(()=>{raf=0;draw();}); }
function draw() {
 const width=viewport.clientWidth, height=viewport.clientHeight, ratio=window.devicePixelRatio||1;
 if(canvas.width!==Math.round(width*ratio)||canvas.height!==Math.round(height*ratio)) { canvas.width=Math.round(width*ratio);canvas.height=Math.round(height*ratio);canvas.style.height=height+'px'; }
 ctx.setTransform(ratio,0,0,ratio,0,0);ctx.clearRect(0,0,width,height);ctx.font='12px system-ui';drawn=[];
 const gutter=gutterFor(width), plot=Math.max(1,width-gutter), scale=plot/(right-left);
 const query=byId('search').value.toLocaleLowerCase(), top=viewport.scrollTop;
 const first=Math.max(0,Math.floor(top/rowHeight)-1), last=Math.min(lanes.length,first+Math.ceil(height/rowHeight)+3);
 ctx.save();ctx.beginPath();ctx.rect(0,header,width,height-header);ctx.clip();
 for(let i=first;i<last;i++) {
  const lane=lanes[i], y=header+i*rowHeight-top;ctx.fillStyle=i%2?'#1b1f2c':'#171a25';ctx.fillRect(0,y,width,rowHeight);
  ctx.fillStyle='#b7bdd3';ctx.save();ctx.beginPath();ctx.rect(3,y,gutter-8,rowHeight);ctx.clip();ctx.fillText(lane.label,5,y+16);ctx.restore();
  ctx.save();ctx.beginPath();ctx.rect(gutter,header,plot,height-header);ctx.clip();
  for(let j=lower(lane.ends,left,x=>x);j<lane.items.length;j++) {
   const item=lane.items[j];if(item.plot_start_ms>right)break;
   const x=Math.max(gutter,gutter+(item.plot_start_ms-left)*scale), stop=Math.min(width,gutter+(item.plot_end_ms-left)*scale), w=Math.max(2,stop-x);
   const match=!query||(item.name+' '+safe(item.file)).toLocaleLowerCase().includes(query);
   let hash=0;for(const char of item.name)hash=((hash<<5)-hash+char.charCodeAt(0))|0;
   ctx.fillStyle=item===selected?'#ff8cdc':!match?'#343847':'hsl('+(item.kind==='python'?240+Math.abs(hash)%55:22+Math.abs(hash)%35)+' 55% 57%)';
   if(item.kind==='placement') {
    const mid=gutter+(item.plot_at_ms-left)*scale;ctx.strokeStyle=ctx.fillStyle;ctx.beginPath();ctx.moveTo(x,y+rowHeight/2);ctx.lineTo(x+w,y+rowHeight/2);ctx.stroke();
    ctx.beginPath();ctx.moveTo(mid,y+4);ctx.lineTo(mid+5,y+rowHeight/2);ctx.lineTo(mid,y+rowHeight-4);ctx.lineTo(mid-5,y+rowHeight/2);ctx.closePath();ctx.fill();
   } else {ctx.fillRect(x,y+2,w,rowHeight-4);ctx.strokeStyle='#12131b';ctx.strokeRect(x,y+2,w,rowHeight-4);}
   if(w>28&&item.kind!=='placement'){ctx.save();ctx.beginPath();ctx.rect(x+3,y+2,w-6,rowHeight-4);ctx.clip();ctx.fillStyle='#fff';ctx.fillText(item.name,x+4,y+16);ctx.restore();}
   const hitTop=Math.max(header,y+2),hitBottom=Math.min(height,y+rowHeight-2),mid=gutter+((item.plot_at_ms??item.plot_start_ms)-left)*scale;
   const hitLeft=item.kind==='placement'?Math.max(gutter,Math.min(x,mid-5)):x,hitRight=Math.min(width,item.kind==='placement'?Math.max(x+w,mid+5):x+w);
   if(hitBottom>hitTop&&hitRight>hitLeft)drawn.push({x:hitLeft,y:hitTop,w:hitRight-hitLeft,h:hitBottom-hitTop,item});
  }
  ctx.restore();
 }
 ctx.restore();ctx.fillStyle='#171a25';ctx.fillRect(0,0,width,header);ctx.fillStyle='#b7bdd3';ctx.fillText('Thread · source / depth',5,21);
 for(let n=0;n<=5;n++){const x=gutter+plot*n/5;ctx.fillStyle='#b7bdd3';ctx.fillText(elapsed(left+(right-left)*n/5),Math.min(width-65,x+3),15);ctx.strokeStyle='#3a4055';ctx.beginPath();ctx.moveTo(x,header-8);ctx.lineTo(x,height);ctx.stroke();}
 const thread=byId('thread').value;
 for(let i=lower(markers,left,item=>item.plot_start_ms);i<markers.length;i++) { const marker=markers[i];if(marker.plot_start_ms>right)break;if(thread&&marker.thread!==thread)continue;
  const x=gutter+(marker.plot_start_ms-left)*scale;ctx.fillStyle='#ffd28a';ctx.beginPath();ctx.moveTo(x,20);ctx.lineTo(x-4,28);ctx.lineTo(x+4,28);ctx.fill();drawn.push({x:x-5,y:18,w:10,h:14,marker}); }
 text('window',elapsed(left)+' – '+elapsed(right)+' · visible window '+elapsed(right-left));
}
byId('fit').addEventListener('click',()=>setWindow(0,end));
byId('zoom-in').addEventListener('click',()=>zoom(.5));byId('zoom-out').addEventListener('click',()=>zoom(2));
byId('pan-left').addEventListener('click',()=>setWindow(left-(right-left)*.4,right-(right-left)*.4));
byId('pan-right').addEventListener('click',()=>setWindow(left+(right-left)*.4,right+(right-left)*.4));
byId('view').addEventListener('change',()=>{selected=null;viewport.scrollTop=0;text('details','Select a call or CPU observation to inspect its recorded evidence.');rebuild();});
byId('thread').addEventListener('change',()=>{viewport.scrollTop=0;rebuild();});byId('search').addEventListener('input',rebuild);
byId('previous').addEventListener('click',()=>{page--;renderResults();});byId('next').addEventListener('click',()=>{page++;renderResults();});
byId('markers-previous').addEventListener('click',()=>{markerPage--;renderMarkers();});byId('markers-next').addEventListener('click',()=>{markerPage++;renderMarkers();});
viewport.addEventListener('scroll',schedule);window.addEventListener('resize',schedule);
canvas.addEventListener('wheel',event=>{if(event.ctrlKey||event.metaKey){event.preventDefault();const rect=canvas.getBoundingClientRect(),gutter=gutterFor(rect.width);zoom(event.deltaY<0?.8:1.25,left+Math.max(0,Math.min(1,(event.clientX-rect.left-gutter)/(rect.width-gutter)))*(right-left));}},{passive:false});
canvas.addEventListener('pointerdown',event=>{if(event.button!==0)return;canvas.focus();canvas.setPointerCapture(event.pointerId);drag={id:event.pointerId,x:event.clientX,left,right,moved:false};});
canvas.addEventListener('pointermove',event=>{if(!drag)return;const delta=event.clientX-drag.x;if(Math.abs(delta)>3)drag.moved=true;if(drag.moved){const gutter=gutterFor(canvas.clientWidth);const shift=delta/(canvas.clientWidth-gutter)*(drag.right-drag.left);setWindow(drag.left-shift,drag.right-shift);}});
canvas.addEventListener('pointerup',event=>{if(!drag)return;const moved=drag.moved;drag=null;if(canvas.hasPointerCapture(event.pointerId))canvas.releasePointerCapture(event.pointerId);if(moved)return;
 const rect=canvas.getBoundingClientRect(),x=event.clientX-rect.left,y=event.clientY-rect.top;const hit=[...drawn].reverse().find(item=>x>=item.x&&x<=item.x+item.w&&y>=item.y&&y<=item.y+item.h);
 if(hit&&hit.item)select(hit.item);else if(hit&&hit.marker)selectMarker(hit.marker);});
canvas.addEventListener('pointercancel',()=>{drag=null;});canvas.addEventListener('lostpointercapture',()=>{drag=null;});
canvas.addEventListener('keydown',event=>{if(event.key==='+'||event.key==='=')zoom(.5);else if(event.key==='-')zoom(2);else if(event.key==='Home')setWindow(0,end);else if(event.key==='ArrowLeft')setWindow(left-(right-left)*.2,right-(right-left)*.2);else if(event.key==='ArrowRight')setWindow(left+(right-left)*.2,right+(right-left)*.2);else if(event.key==='Enter'&&selected)select(selected,true);else return;event.preventDefault();});
rebuild();
"""


def _browser_model(snapshot):
    from .timeline import build

    if not snapshot.get("events") and not snapshot.get("timeline"):
        raise ValueError("No verified retained events are available to export")
    model = snapshot.get("timeline") or build(snapshot)
    # Plot in relative milliseconds. Preserve exact nanosecond provenance as
    # strings: JavaScript cannot represent arbitrary 64-bit timestamps exactly.
    result = dict(
        model,
        origin_ns=str(model["origin_ns"]),
        plot_end_ms=model["end_ns"] / 1e6,
        caught_up=bool(snapshot.get("caught_up", True)),
    )
    result["intervals"] = [
        dict(
            item,
            start_ns=str(item["start_ns"]),
            end_ns=str(item["end_ns"]),
            duration_ns=str(item["duration_ns"]),
            plot_start_ms=item["start_ns"] / 1e6,
            plot_end_ms=item["end_ns"] / 1e6,
        )
        for item in model["intervals"]
    ]
    result["markers"] = [
        dict(item, start_ns=str(item["start_ns"]), plot_start_ms=item["start_ns"] / 1e6)
        for item in model["markers"]
    ]
    cpu = model.get(
        "cpu",
        {
            "status": "not-recorded",
            "observations": [],
            "lanes": [],
            "warnings": ["CPU placement was not recorded for this trace."],
        },
    )
    lanes = {lane["key"]: lane["label"] for lane in cpu["lanes"]}
    lane_order = {lane["key"]: index for index, lane in enumerate(cpu["lanes"])}
    result["cpu"] = dict(
        cpu,
        observations=[
            dict(
                item,
                kind="placement",
                depth=0,
                file="",
                line=None,
                first_seq=item["seq"],
                last_seq=item["seq"],
                exit="observed",
                lane_label=lanes.get(item["lane_key"], "Unknown CPU placement"),
                lane_order=lane_order.get(item["lane_key"], 0),
                start_ns=str(item["start_ns"]),
                end_ns=str(item["end_ns"]),
                at_ns=str(item["at_ns"]),
                duration_ns=str(item["end_ns"] - item["start_ns"]),
                plot_start_ms=item["start_ns"] / 1e6,
                plot_end_ms=item["end_ns"] / 1e6,
                plot_at_ms=item["at_ns"] / 1e6,
            )
            for item in cpu["observations"]
        ],
    )
    if result["cpu"]["observations"]:
        result["plot_end_ms"] = max(
            result["plot_end_ms"],
            max(item["plot_end_ms"] for item in result["cpu"]["observations"]),
        )

    def safe_integers(value):
        if type(value) is int and abs(value) > (1 << 53) - 1:
            return str(value)
        if isinstance(value, dict):
            return {key: safe_integers(item) for key, item in value.items()}
        if isinstance(value, list):
            return [safe_integers(item) for item in value]
        return value

    return safe_integers(result)


def render(snapshot, *, max_bytes=MAX_OUTPUT_BYTES):
    """Return bounded HTML bytes; all recorded strings remain inert JSON/text."""
    model = _browser_model(snapshot)
    data = json.dumps(model, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    data = data.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    digest = base64.b64encode(hashlib.sha256(_SCRIPT.encode()).digest()).decode()
    document = (
        """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'sha256-"""
        + digest
        + """'; style-src 'unsafe-inline'; connect-src 'none'; img-src 'none'; base-uri 'none'; form-action 'none'">
<title>Raven · Recorded call timeline</title><style>"""
        + _STYLE
        + """</style></head><body>
<h1>Recorded call timeline</h1><p id="identity"></p><p id="coverage" class="muted"></p>
<details><summary>Coverage and source limitations</summary><p id="warnings" class="warning"></p></details>
<div class="controls" aria-label="Timeline controls"><label>View <select id="view"><option value="calls">Call intervals</option><option value="cores">CPU placement observations</option></select></label><button id="fit">Fit all</button><button id="zoom-in">Zoom in</button><button id="zoom-out">Zoom out</button><button id="pan-left">Pan left</button><button id="pan-right">Pan right</button>
<label>Thread <select id="thread"><option value="">All threads</option></select></label><label>Find function <input id="search" type="search" placeholder="Function or source file"></label></div>
<p id="view-note" class="muted"></p><p id="window" class="muted" aria-live="polite"></p>
<div id="viewport"><div id="surface"><canvas id="timeline" tabindex="0" role="img" aria-label="Recorded nested call timeline. Use the matching interval table below for accessible inspection." aria-describedby="help">Use the matching interval table below to inspect all recorded calls.</canvas></div></div>
<small id="help" class="muted">Drag to pan; Ctrl/Command + wheel to zoom. Focus the timeline: +/− zoom, ←/→ pan, Home fits all, Enter fits the selected call. Scroll vertically for other stack levels. Amber triangles are observed event boundaries or gaps.</small>
<h2>Selected interval or marker</h2><div id="details" role="status" aria-live="polite">Select a call in the timeline or matching interval table.</div>
<h2>Matching intervals</h2><p id="matches" aria-live="polite"></p><div class="controls"><button id="previous">Previous page</button><button id="next">Next page</button></div>
<table id="results"><thead><tr><th>Function / span</th><th id="duration-heading">Wall time</th><th>Thread</th><th>Source / depth</th></tr></thead><tbody id="result-body"></tbody></table>
<div id="marker-section"><h2>Observed markers</h2><p id="marker-count" aria-live="polite"></p><div class="controls"><button id="markers-previous">Previous markers</button><button id="markers-next">Next markers</button></div>
<table id="markers"><thead><tr><th>Marker</th><th>Relative time</th><th>Thread</th><th>Kind</th></tr></thead><tbody id="marker-body"></tbody></table></div>
<details><summary>Verified snapshot provenance</summary><pre id="provenance"></pre></details>
<script id="trace-data" type="application/json">"""
        + data
        + """</script><script>"""
        + _SCRIPT
        + """</script></body></html>"""
    )
    raw = document.encode("utf-8")
    if len(raw) > max_bytes:
        raise ValueError(
            "HTML trace exceeds its output byte budget; export a smaller retained prefix"
        )
    return raw


def export(snapshot, output, *, max_bytes=MAX_OUTPUT_BYTES, cancel=None):
    """Publish one new private file atomically, without following/replacing it."""

    def check():
        if cancel is not None and cancel.is_set():
            raise InterruptedError("Trace browser export cancelled")

    check()
    raw = render(snapshot, max_bytes=max_bytes)
    check()
    output = Path(output).expanduser().absolute()
    directory = os.open(
        output.parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    temporary = ".raven-trace-" + uuid.uuid4().hex + ".tmp"
    try:
        fd = os.open(
            temporary,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory,
        )
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            check()
            os.link(
                temporary,
                output.name,
                src_dir_fd=directory,
                dst_dir_fd=directory,
                follow_symlinks=False,
            )
            os.fsync(directory)
        finally:
            os.unlink(temporary, dir_fd=directory)
    finally:
        os.close(directory)
    return {
        "path": str(output),
        "format": "html",
        "bytes": len(raw),
        "read_seq": snapshot["read_seq"],
        "source_status": snapshot["status"],
        "warnings": list(snapshot["warnings"]),
    }
