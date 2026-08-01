"""A dependency-free web dashboard.

A background thread polls the ECU through a Backend and keeps the latest
readings in memory; a stdlib HTTP server serves a single self-contained page.
The page lets you pick which parameters to chart, switch between the CAN buses
(the engine/brakes 500k bus and the cabin 125k bus can't be read at once), and
read the car's programmed configuration. No third-party packages and ES5/XHR
only, so it runs in the Windows 7 / Python 3.8 VIDA guest's old browser as-is.

web.serve(backend, ...) drives any object implementing the Backend interface;
cli builds the real one over the Volvo A6 link, and FakeBackend here feeds
synthetic data so the page can be exercised without a car.
"""

from __future__ import annotations

import json
import math
import threading
import time
from abc import ABC, abstractmethod
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Backend(ABC):
    """What the dashboard needs from a data source. All methods are called from
    the server thread; implementations guard their own hardware state."""

    @abstractmethod
    def description(self) -> str: ...

    @abstractmethod
    def buses(self) -> list:
        """[{id, label, baudrate}] — the selectable CAN buses."""

    @abstractmethod
    def current_bus(self) -> str: ...

    @abstractmethod
    def switch_bus(self, bus_id: str) -> None:
        """Reopens the link on another bus. Raises on failure."""

    @abstractmethod
    def list_params(self) -> list:
        """[{key, name, unit, ecu, status, category}] for the current bus."""

    @abstractmethod
    def read_selected(self, keys: list) -> list:
        """Reads the given keys, returning a row dict per key (see _row)."""

    @abstractmethod
    def read_config(self) -> dict:
        """{identity:[{name,value}], car_config:[{name,value,raw}]} or {error}."""

    def close(self) -> None:  # pragma: no cover - optional
        pass


def _row(key, name, unit, ecu, status, category, ok, value=None, num=None, error="") -> dict:
    return {"key": key, "name": name, "unit": unit, "ecu": ecu, "status": status,
            "category": category, "ok": ok, "value": value, "num": num, "error": error}


# ---------------------------------------------------------------------------
# A synthetic backend, for exercising the page without a car.
# ---------------------------------------------------------------------------

class FakeBackend(Backend):
    _CATALOG = {
        "hs": [
            ("rpm", "Engine speed", "rpm", "ECM", "Engine", 820, 90),
            ("boost", "Boost pressure", "kPa", "ECM", "Boost", 148, 22),
            ("boost_desired", "Boost, desired", "kPa", "ECM", "Boost", 152, 20),
            ("maf", "Mass air flow", "kg/h", "ECM", "Air", 34, 9),
            ("clt", "Coolant temperature", "degC", "ECM", "Temperatures", 89, 1.2),
            ("iat", "Intake air temperature", "degC", "ECM", "Temperatures", 31, 0.6),
            ("rail", "Fuel rail pressure", "hPa", "ECM", "Fuel", 28450, 5200),
            ("egr", "EGR valve", "%", "ECM", "EGR", 18, 6),
            ("egt", "Exhaust/DPF temperature", "degC", "ECM", "DPF & exhaust", 247, 34),
            ("dpfp", "DPF differential pressure", "kPa", "ECM", "DPF & exhaust", 3.4, 1.1),
            ("whl_fl", "Wheel speed, front left", "km/h", "ABS", "ABS - wheels", 0, 0),
            ("whl_fr", "Wheel speed, front right", "km/h", "ABS", "ABS - wheels", 0, 0),
            ("yaw", "Yaw rate", "deg/s", "ABS", "ABS - dynamics", 0, 3),
        ],
        "ls": [
            ("v30", "30-supply", "V", "CEM", "CEM - electrical", 14.1, 0.2),
            ("amp", "Total current", "A", "CEM", "CEM - electrical", 41, 8),
            ("cabin", "In-car temperature", "degC", "CEM", "CEM - climate", 23, 0.4),
            ("outdoor", "Outdoor temperature", "degC", "CEM", "CEM - climate", 16, 0.2),
            ("fuel_lvl", "Fuel level", "%", "DIM", "DIM", 62, 0.5),
            ("odo", "Total distance", "km", "DIM", "DIM", 184207, 0),
            ("coolant_dim", "Coolant (cluster)", "degC", "DIM", "DIM", 89, 1.0),
        ],
    }

    def __init__(self) -> None:
        self._bus = "hs"
        self._t0 = time.monotonic()

    def description(self) -> str:
        return "FAKE synthetic data — Volvo V50 dashboard preview"

    def buses(self) -> list:
        return [{"id": "hs", "label": "500k — ECM + ABS", "baudrate": 500000},
                {"id": "ls", "label": "125k — CEM + DIM", "baudrate": 125000}]

    def current_bus(self) -> str:
        return self._bus

    def switch_bus(self, bus_id: str) -> None:
        if bus_id not in self._CATALOG:
            raise ValueError(f"unknown bus {bus_id}")
        self._bus = bus_id

    def list_params(self) -> list:
        return [{"key": k, "name": n, "unit": u, "ecu": e, "status": "verified", "category": c}
                for (k, n, u, e, c, _base, _amp) in self._CATALOG[self._bus]]

    def read_selected(self, keys: list) -> list:
        spec = {k: (n, u, e, c, base, amp)
                for (k, n, u, e, c, base, amp) in self._CATALOG[self._bus]}
        t = time.monotonic() - self._t0
        rows = []
        for k in keys:
            if k not in spec:
                continue
            n, u, e, c, base, amp = spec[k]
            num = round(base + amp * math.sin(t / 2.0 + hash(k) % 7), 3)
            rows.append(_row(k, n, u, e, "verified", c, True,
                             value=(f"{num:g}"), num=num))
        return rows

    def read_config(self) -> dict:
        if self._bus != "ls":
            return {"error": "Switch to the 125k bus to read CEM configuration.",
                    "need_bus": "ls"}
        return {
            "identity": [{"name": "VIN", "value": "YV1MW765292483015"},
                         {"name": "Chassis", "value": "483015"},
                         {"name": "Market code", "value": "EU008"},
                         {"name": "Structure week", "value": "200850"}],
            "car_config": [{"name": "Vehicle sub type", "value": "V50", "raw": 3},
                           {"name": "Doors", "value": "5 doors", "raw": 2},
                           {"name": "Gearbox", "value": "M66", "raw": 7},
                           {"name": "Particle Filter For Diesel", "value": "Fitted", "raw": 1},
                           {"name": "Cruise control", "value": "Yes", "raw": 2}],
        }


# ---------------------------------------------------------------------------
# Shared state between the poller and the server.
# ---------------------------------------------------------------------------

class _State:
    def __init__(self, backend: Backend) -> None:
        self.backend = backend
        self.lock = threading.Lock()
        self.started = time.monotonic()
        self.selection: list = []
        self.rows: list = []
        self.updated = 0.0
        self.stop = threading.Event()

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "uptime": round(time.monotonic() - self.started, 1),
                "age": round(time.monotonic() - self.updated, 1) if self.updated else None,
                "rows": list(self.rows),
            }


def _poller(state: _State, interval: float) -> None:
    while not state.stop.is_set():
        with state.lock:
            keys = list(state.selection)
        if keys:
            try:
                rows = state.backend.read_selected(keys)
            except Exception as exc:  # noqa: BLE001 — keep the poller alive
                rows = [_row(k, k, "", "", "error", "", False,
                             error=f"{type(exc).__name__}: {exc}") for k in keys]
            with state.lock:
                state.rows = rows
                state.updated = time.monotonic()
        else:
            with state.lock:
                state.rows = []
        if state.stop.wait(interval):
            break


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Volvo monitor</title>
<style>
 :root{
   color-scheme:dark;
   --bg:#0a0d12; --side:#0e131b; --panel:#151c26; --panel2:#111823;
   --edge:#232d3c; --edge-soft:#1a2230;
   --ink:#e9eef5; --muted:#7c8798; --dim:#586274; --value:#f6f9fc;
   --accent:#4bb5aa; --accent-soft:rgba(75,181,170,.14);
   --live:#4ea36b; --vida:#57b078; --db:#4b8fd6; --exp:#d79b3f; --cand:#9d80d6;
   --mono:ui-monospace,"Cascadia Mono","Segoe UI Mono",Consolas,Menlo,monospace;
   --sans:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
 }
 *{box-sizing:border-box}
 html,body{height:100%}
 body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 var(--sans);
      -webkit-font-smoothing:antialiased;display:grid;grid-template-columns:288px 1fr}
 /* ---- sidebar ---- */
 #side{background:var(--side);border-right:1px solid var(--edge);height:100vh;
       overflow-y:auto;display:flex;flex-direction:column}
 .brand{padding:16px 18px 12px;border-bottom:1px solid var(--edge-soft)}
 .brand .top{display:flex;align-items:center;gap:9px}
 .live{width:9px;height:9px;border-radius:50%;background:var(--live);flex:none;
       box-shadow:0 0 0 0 rgba(78,163,107,.6);animation:pulse 2s infinite}
 @keyframes pulse{70%{box-shadow:0 0 0 7px rgba(78,163,107,0)}100%{box-shadow:0 0 0 0 rgba(78,163,107,0)}}
 @media (prefers-reduced-motion:reduce){.live{animation:none}}
 .brand h1{font-size:14px;margin:0;font-weight:600;letter-spacing:.01em}
 .brand .sub{color:var(--muted);font-size:11px;margin-top:5px;font-family:var(--mono);
             word-break:break-word}
 .ctrls{padding:12px 14px;border-bottom:1px solid var(--edge-soft);display:flex;
        flex-direction:column;gap:9px}
 label.lbl{font-size:10px;text-transform:uppercase;letter-spacing:.13em;color:var(--dim);font-weight:600}
 select,input[type=text]{width:100%;background:var(--panel2);color:var(--ink);
        border:1px solid var(--edge);border-radius:8px;padding:8px 10px;font:13px var(--sans)}
 select:focus,input:focus{outline:none;border-color:var(--accent)}
 .tabs{display:flex;gap:6px}
 .tab{flex:1;text-align:center;padding:7px 0;border:1px solid var(--edge);border-radius:8px;
      background:var(--panel2);color:var(--muted);cursor:pointer;font-size:12px;font-weight:600;
      user-select:none}
 .tab.on{background:var(--accent-soft);border-color:var(--accent);color:var(--ink)}
 .plist{flex:1;overflow-y:auto;padding:6px 8px 20px}
 .grp{margin-top:10px}
 .grp h3{font-size:10px;text-transform:uppercase;letter-spacing:.12em;color:var(--dim);
         margin:8px 8px 4px;font-weight:600}
 .item{display:flex;align-items:center;gap:9px;padding:6px 8px;border-radius:7px;cursor:pointer}
 .item:hover{background:var(--panel2)}
 .item input{margin:0;accent-color:var(--accent);flex:none}
 .item .nm{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:13px}
 .item .vv{font-family:var(--mono);font-size:11px;color:var(--muted);font-variant-numeric:tabular-nums}
 .item.sel .nm{color:var(--value)}
 .hint{color:var(--dim);font-size:12px;padding:14px 12px;line-height:1.5}
 /* ---- main ---- */
 #main{height:100vh;overflow-y:auto;padding:18px 20px 40px}
 .bar{display:flex;align-items:baseline;gap:12px;margin-bottom:16px;flex-wrap:wrap}
 .bar h2{font-size:13px;margin:0;text-transform:uppercase;letter-spacing:.12em;color:var(--muted);font-weight:600}
 .bar .meta{color:var(--dim);font-size:12px;font-family:var(--mono);margin-left:auto;
            font-variant-numeric:tabular-nums}
 .bar .stale{color:var(--exp)}
 #cards{display:grid;gap:16px;grid-template-columns:repeat(auto-fill,minmax(340px,1fr))}
 .card{background:linear-gradient(180deg,#18202b,var(--panel));border:1px solid var(--edge);
       border-radius:14px;padding:14px 16px 8px;box-shadow:0 12px 30px -22px #000}
 .card .hd{display:flex;align-items:center;gap:8px;margin-bottom:2px}
 .card .hd .nm{font-size:13px;color:#cdd5e0;flex:1;min-width:0;overflow:hidden;
               text-overflow:ellipsis;white-space:nowrap}
 .card .hd .ecu{font-size:10px;color:var(--dim);border:1px solid var(--edge);border-radius:5px;
                padding:1px 6px;letter-spacing:.05em}
 .dot{width:8px;height:8px;border-radius:50%;flex:none;box-shadow:0 0 6px -1px currentColor}
 .s-verified-against-vida{background:var(--vida);color:var(--vida)}
 .s-verified{background:var(--db);color:var(--db)}
 .s-experimental,.s-discovered{background:var(--exp);color:var(--exp)}
 .s-candidate{background:var(--cand);color:var(--cand)}
 .s-error,.s-none{background:var(--dim);color:var(--dim);box-shadow:none}
 .card .val{font-family:var(--mono);font-variant-numeric:tabular-nums;font-weight:600;
            font-size:30px;color:var(--value);letter-spacing:-.01em;line-height:1.1;margin:2px 0 4px}
 .card .val .u{color:var(--muted);font-weight:400;font-size:14px;margin-left:7px}
 .card.bad .val{color:var(--dim);font-size:16px;font-weight:400}
 .card canvas{width:100%;height:120px;display:block}
 .card .ax{display:flex;justify-content:space-between;color:var(--dim);font-size:10px;
           font-family:var(--mono);margin-top:2px}
 .empty{color:var(--dim);padding:60px 20px;text-align:center;font-size:14px}
 /* ---- config view ---- */
 .cfg{max-width:820px}
 .cfg h2{font-size:12px;text-transform:uppercase;letter-spacing:.12em;color:var(--muted);
         margin:22px 0 10px;font-weight:600}
 .cfg .card2{background:var(--panel);border:1px solid var(--edge);border-radius:12px;overflow:hidden}
 .crow{display:grid;grid-template-columns:1fr auto;gap:12px;padding:9px 15px;
       border-top:1px solid var(--edge-soft)}
 .crow:first-child{border-top:0}
 .crow .k{color:#c6cdd8}
 .crow .v{font-family:var(--mono);color:var(--value);font-variant-numeric:tabular-nums}
 .crow .v.raw{color:var(--dim)}
 .btn{background:var(--accent-soft);border:1px solid var(--accent);color:var(--ink);
      border-radius:8px;padding:8px 14px;cursor:pointer;font:600 13px var(--sans)}
 .note{color:var(--exp);font-size:13px;margin:8px 0}
 .badge{display:inline-block;font-size:10px;color:var(--exp);border:1px solid var(--exp);
        border-radius:5px;padding:1px 6px;margin-left:8px;letter-spacing:.05em;vertical-align:middle}
 .hide{display:none!important}
</style></head><body>
<aside id="side">
  <div class="brand">
    <div class="top"><span class="live"></span><h1>Volvo monitor</h1></div>
    <div class="sub" id="desc">connecting…</div>
  </div>
  <div class="ctrls">
    <label class="lbl">CAN bus</label>
    <select id="bus"></select>
    <div class="tabs">
      <div class="tab on" id="tab-live" data-view="live">Live</div>
      <div class="tab" id="tab-config" data-view="config">Configuration</div>
    </div>
    <input type="text" id="search" placeholder="filter parameters…" class="live-only">
  </div>
  <div class="plist live-only" id="plist"></div>
  <div class="hint config-only hide">Configuration is read from the CEM on the
    125k bus. Open the Configuration view on the right and press Read.</div>
</aside>
<main id="main"></main>

<template id="tpl-main-live">
  <div class="bar"><h2>Live</h2><span class="meta" id="meta"></span></div>
  <div id="cards"></div>
  <div class="empty" id="empty">Tick parameters on the left to chart them.</div>
</template>

<script>
/* ES5 + XMLHttpRequest so the guest's old browser can render it. */
function $(id){return document.getElementById(id);}
function esc(s){return String(s).replace(/[&<>"]/g,function(c){
  return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}
function statusClass(s){return 's-'+String(s||'none').replace(/[^a-z-]/g,'');}
function xhr(method,url,body,cb){
  var x=new XMLHttpRequest();x.open(method,url,true);
  x.onreadystatechange=function(){if(x.readyState!==4)return;
    var d=null;try{d=x.responseText?JSON.parse(x.responseText):null;}catch(e){}
    cb(x.status===200,d);};
  if(body!=null){x.setRequestHeader('Content-Type','application/json');x.send(JSON.stringify(body));}
  else x.send();
}

var STATE={bus:'',params:[],sel:[],view:'live'};
var HIST={},HCAP=180;

function selKey(){return 'volvo.sel.'+STATE.bus;}
function loadSel(){try{return JSON.parse(localStorage.getItem(selKey()))||[];}catch(e){return [];}}
function saveSel(){try{localStorage.setItem(selKey(),JSON.stringify(STATE.sel));}catch(e){}}

/* ---------- sidebar parameter list ---------- */
function buildList(){
  var q=($('search').value||'').toLowerCase();
  var groups={},order=[],i,p;
  for(i=0;i<STATE.params.length;i++){p=STATE.params[i];
    if(q && p.name.toLowerCase().indexOf(q)<0 && p.key.toLowerCase().indexOf(q)<0) continue;
    if(!groups[p.category]){groups[p.category]=[];order.push(p.category);}
    groups[p.category].push(p);
  }
  var h='';
  for(i=0;i<order.length;i++){var lab=order[i],items=groups[lab],j;
    h+='<div class="grp"><h3>'+esc(lab)+'</h3>';
    for(j=0;j<items.length;j++){p=items[j];
      var on=STATE.sel.indexOf(p.key)>=0;
      h+='<label class="item'+(on?' sel':'')+'" data-key="'+esc(p.key)+'">'
        +'<input type="checkbox"'+(on?' checked':'')+'>'
        +'<span class="nm">'+esc(p.name)+'</span>'
        +'<span class="vv" id="vv-'+esc(p.key)+'">'+(p.unit?esc(p.unit):'')+'</span></label>';
    }
    h+='</div>';
  }
  $('plist').innerHTML=h||'<div class="hint">No parameters match.</div>';
  var labels=$('plist').getElementsByClassName('item');
  for(i=0;i<labels.length;i++){
    labels[i].getElementsByTagName('input')[0].onchange=(function(el){return function(){
      toggle(el.getAttribute('data-key'),this.checked);};})(labels[i]);
  }
}
function toggle(key,on){
  var i=STATE.sel.indexOf(key);
  if(on && i<0)STATE.sel.push(key);
  else if(!on && i>=0)STATE.sel.splice(i,1);
  saveSel();pushSel();buildList();renderCards();
}
function pushSel(){xhr('POST','select',STATE.sel,function(){});}

/* ---------- live cards ---------- */
function renderCards(){
  if(STATE.view!=='live')return;
  var order=STATE.sel,i,p,map={};
  for(i=0;i<STATE.params.length;i++)map[STATE.params[i].key]=STATE.params[i];
  $('empty').className='empty'+(order.length?' hide':'');
  var host=$('cards'),h='';
  for(i=0;i<order.length;i++){p=map[order[i]];if(!p)continue;
    h+='<div class="card" id="card-'+esc(p.key)+'">'
      +'<div class="hd"><span class="dot s-none" id="dot-'+esc(p.key)+'"></span>'
      +'<span class="nm">'+esc(p.name)+'</span>'
      +'<span class="ecu">'+esc(p.ecu)+'</span></div>'
      +'<div class="val" id="val-'+esc(p.key)+'">—</div>'
      +'<canvas id="cv-'+esc(p.key)+'"></canvas>'
      +'<div class="ax"><span id="mn-'+esc(p.key)+'"></span><span id="mx-'+esc(p.key)+'"></span></div>'
      +'</div>';
  }
  host.innerHTML=h;
}
function pushHist(key,num){
  if(num===null||num===undefined)return;
  var a=HIST[key]||(HIST[key]=[]);a.push(num);if(a.length>HCAP)a.shift();
}
function drawChart(key){
  var cv=$('cv-'+key);if(!cv)return;
  var a=HIST[key]||[];
  var ratio=window.devicePixelRatio||1;
  var w=cv.clientWidth||320,h=120;
  if(cv.width!==Math.floor(w*ratio)){cv.width=Math.floor(w*ratio);cv.height=Math.floor(h*ratio);}
  var ctx=cv.getContext('2d');ctx.setTransform(ratio,0,0,ratio,0,0);
  ctx.clearRect(0,0,w,h);
  var pad=6,min,max,i;
  if(a.length<2){return;}
  min=Math.min.apply(null,a);max=Math.max.apply(null,a);
  var rng=(max-min)||1;var pot=rng*0.12;min-=pot;max+=pot;rng=max-min;
  var X=function(i){return pad+i/(a.length-1)*(w-2*pad);};
  var Y=function(v){return h-pad-(v-min)/rng*(h-2*pad);};
  /* grid */
  ctx.strokeStyle='rgba(255,255,255,.045)';ctx.lineWidth=1;
  for(i=0;i<=3;i++){var gy=pad+i/3*(h-2*pad);ctx.beginPath();ctx.moveTo(pad,gy);ctx.lineTo(w-pad,gy);ctx.stroke();}
  /* area */
  ctx.beginPath();ctx.moveTo(X(0),h-pad);
  for(i=0;i<a.length;i++)ctx.lineTo(X(i),Y(a[i]));
  ctx.lineTo(X(a.length-1),h-pad);ctx.closePath();
  var g=ctx.createLinearGradient(0,pad,0,h);g.addColorStop(0,'rgba(75,181,170,.30)');
  g.addColorStop(1,'rgba(75,181,170,0)');ctx.fillStyle=g;ctx.fill();
  /* line */
  ctx.beginPath();
  for(i=0;i<a.length;i++){if(i)ctx.lineTo(X(i),Y(a[i]));else ctx.moveTo(X(i),Y(a[i]));}
  ctx.strokeStyle='#5bd0c3';ctx.lineWidth=1.6;ctx.lineJoin='round';ctx.stroke();
  /* endpoint */
  ctx.beginPath();ctx.arc(X(a.length-1),Y(a[a.length-1]),2.6,0,6.2832);
  ctx.fillStyle='#c8f5ee';ctx.fill();
  var fmt=function(v){return Math.abs(v)>=1000?Math.round(v):Math.round(v*100)/100;};
  $('mn-'+key).textContent=fmt(min);$('mx-'+key).textContent=fmt(max);
}
function applyData(d){
  var stale=d.age!==null&&d.age>3;
  if($('meta'))$('meta').innerHTML=STATE.sel.length+' selected · '+d.uptime+'s'
    +(stale?' · <span class="stale">stale '+d.age+'s</span>':'');
  var by={},i,r;
  for(i=0;i<d.rows.length;i++){r=d.rows[i];by[r.key]=r;}
  for(i=0;i<STATE.sel.length;i++){var key=STATE.sel[i];r=by[key];
    var vv=$('vv-'+key);
    if(!r){continue;}
    if(r.num!==null&&r.num!==undefined)pushHist(key,r.num);
    var val=$('val-'+key),card=$('card-'+key),dot=$('dot-'+key);
    if(card){card.className='card'+(r.ok?'':' bad');
      dot.className='dot '+statusClass(r.ok?r.status:'error');
      val.innerHTML=r.ok?(esc(r.value)+(r.unit?'<span class="u">'+esc(r.unit)+'</span>':''))
                        :('<span title="'+esc(r.error||'')+'">—</span>');
      drawChart(key);
    }
    if(vv)vv.textContent=r.ok?(r.value+(r.unit?' '+r.unit:'')):'—';
  }
}
function tick(){
  if(STATE.view!=='live'||!STATE.sel.length)return;
  xhr('GET','data',null,function(ok,d){if(ok&&d)applyData(d);
    else if($('meta'))$('meta').textContent='disconnected';});
}

/* ---------- configuration view ---------- */
function renderConfig(data){
  var h='';
  if(data.error){
    h+='<div class="note">'+esc(data.error)+'</div>';
    if(data.need_bus)h+='<button class="btn" onclick="switchBus(\''+data.need_bus+'\',true)">Switch bus & read</button>';
    $('main').innerHTML='<div class="bar"><h2>Configuration</h2></div><div class="cfg">'+h+'</div>';
    return;
  }
  h+='<h2>Vehicle identity <span class="badge">0xFB · verified</span></h2><div class="card2">';
  var i,f;
  for(i=0;i<(data.identity||[]).length;i++){f=data.identity[i];
    h+='<div class="crow"><span class="k">'+esc(f.name)+'</span><span class="v">'+esc(f.value)+'</span></div>';}
  h+='</div>';
  h+='<h2>Car configuration <span class="badge">0xFC · unverified</span></h2><div class="card2">';
  for(i=0;i<(data.car_config||[]).length;i++){f=data.car_config[i];
    var shown=f.value?esc(f.value):('0x'+('0'+(f.raw||0).toString(16)).slice(-2));
    h+='<div class="crow"><span class="k">'+esc(f.name)+'</span><span class="v'
      +(f.value?'':' raw')+'">'+shown+'</span></div>';}
  h+='</div>';
  $('main').innerHTML='<div class="bar"><h2>Configuration</h2>'
    +'<span class="meta"><button class="btn" onclick="loadConfig()">Re-read</button></span></div>'
    +'<div class="cfg">'+h+'</div>';
}
function loadConfig(){
  $('main').innerHTML='<div class="bar"><h2>Configuration</h2></div><div class="empty">Reading CEM…</div>';
  xhr('GET','config',null,function(ok,d){renderConfig(d||{error:'read failed'});});
}

/* ---------- view + bus wiring ---------- */
function setView(v){
  STATE.view=v;
  $('tab-live').className='tab'+(v==='live'?' on':'');
  $('tab-config').className='tab'+(v==='config'?' on':'');
  var i,els=document.getElementsByClassName('live-only');
  for(i=0;i<els.length;i++)els[i].className=els[i].className.replace(/ ?hide/,'')+(v==='live'?'':' hide');
  els=document.getElementsByClassName('config-only');
  for(i=0;i<els.length;i++)els[i].className=els[i].className.replace(/ ?hide/,'')+(v==='config'?'':' hide');
  if(v==='live'){
    $('main').innerHTML=$('tpl-main-live').innerHTML;renderCards();
  }else{loadConfig();}
}
function loadParams(cb){
  xhr('GET','params',null,function(ok,d){
    STATE.params=(ok&&d&&d.params)||[];
    STATE.sel=loadSel().filter(function(k){
      for(var i=0;i<STATE.params.length;i++)if(STATE.params[i].key===k)return true;return false;});
    pushSel();buildList();if(cb)cb();
  });
}
function switchBus(id,thenConfig){
  xhr('POST','bus',{id:id},function(ok){
    if(!ok)return;STATE.bus=id;HIST={};
    var sel=$('bus');for(var i=0;i<sel.options.length;i++)sel.options[i].selected=(sel.options[i].value===id);
    loadParams(function(){if(thenConfig){setView('config');}else{renderCards();}});
  });
}
function init(){
  xhr('GET','meta',null,function(ok,d){
    if(!ok||!d){$('desc').textContent='disconnected';return;}
    $('desc').textContent=d.description;STATE.bus=d.current_bus;
    var h='',i;for(i=0;i<d.buses.length;i++)h+='<option value="'+esc(d.buses[i].id)+'"'
      +(d.buses[i].id===d.current_bus?' selected':'')+'>'+esc(d.buses[i].label)+'</option>';
    $('bus').innerHTML=h;
    $('bus').onchange=function(){switchBus(this.value,false);};
    $('search').oninput=buildList;
    $('tab-live').onclick=function(){setView('live');};
    $('tab-config').onclick=function(){setView('config');};
    loadParams(function(){setView('live');});
  });
  setInterval(tick,__INTERVAL__);
}
init();
</script></body></html>
"""


def serve(backend: Backend, interval: float = 0.5,
          host: str = "127.0.0.1", port: int = 8080) -> None:
    """Polls the selected parameters in the background and serves the dashboard
    until interrupted."""
    state = _State(backend)
    thread = threading.Thread(target=_poller, args=(state, interval), daemon=True)
    thread.start()

    page = PAGE.replace("__INTERVAL__", str(int(interval * 1000))).encode("utf-8")

    def read_body(handler) -> object:
        length = int(handler.headers.get("Content-Length", 0) or 0)
        if not length:
            return None
        try:
            return json.loads(handler.rfile.read(length).decode("utf-8"))
        except Exception:  # noqa: BLE001
            return None

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # quiet
            pass

        def _json(self, obj, code: int = 200) -> None:
            body = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?", 1)[0].rstrip("/")
            if path in ("", "/index.html"):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)
            elif path == "/meta":
                self._json({"description": backend.description(),
                            "buses": backend.buses(), "current_bus": backend.current_bus()})
            elif path == "/params":
                try:
                    self._json({"params": backend.list_params()})
                except Exception as exc:  # noqa: BLE001
                    self._json({"params": [], "error": str(exc)}, 200)
            elif path == "/data":
                self._json(state.snapshot())
            elif path == "/config":
                try:
                    self._json(backend.read_config())
                except Exception as exc:  # noqa: BLE001
                    self._json({"error": f"{type(exc).__name__}: {exc}"})
            else:
                self.send_error(404)

        def do_POST(self):
            path = self.path.split("?", 1)[0].rstrip("/")
            body = read_body(self)
            if path == "/select":
                keys = body if isinstance(body, list) else []
                with state.lock:
                    state.selection = [str(k) for k in keys]
                self._json({"ok": True, "n": len(keys)})
            elif path == "/bus":
                bus_id = (body or {}).get("id") if isinstance(body, dict) else None
                try:
                    backend.switch_bus(str(bus_id))
                    with state.lock:
                        state.selection = []
                        state.rows = []
                    self._json({"ok": True, "current_bus": backend.current_bus()})
                except Exception as exc:  # noqa: BLE001
                    self._json({"ok": False, "error": str(exc)}, 200)
            else:
                self.send_error(404)

    server = ThreadingHTTPServer((host, port), Handler)
    print(backend.description())
    print(f"dashboard on http://{host}:{port}/   (ctrl-c to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        state.stop.set()
        server.shutdown()
        backend.close()
