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
import time
from abc import ABC, abstractmethod
from http.server import BaseHTTPRequestHandler, HTTPServer

from .categories import categorize


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

    def read_dtcs(self) -> dict:
        """{bus, dtcs:[{ecu, code, text}]} for the modules on the current bus,
        or {error}. Default: unsupported."""
        return {"error": "trouble codes are not available on this transport"}

    def last_stats(self) -> dict:
        """Stats from the most recent read_selected cycle: {cycle_ms, rate,
        selected, timeouts}. Empty if the backend doesn't track them."""
        return {}

    def clear_dtcs(self) -> dict:
        """WRITE: clear stored trouble codes on the current bus. Default:
        unsupported."""
        return {"error": "clearing codes is not available on this transport"}

    def close(self) -> None:  # pragma: no cover - optional
        pass


# CarCom stores ASCII-safe unit text (kept as-is in the definitions so the
# terminal works on legacy Windows code pages); prettify it for the UTF-8 page.
_PRETTY_UNITS = {"degC": "°C", "deg/s": "°/s", "deg": "°",
                 "m/s2": "m/s²", "ohm": "Ω", "micros": "µs"}


def _pretty_unit(unit: str) -> str:
    return _PRETTY_UNITS.get(unit, unit) if unit else unit


def _row(key, name, unit, ecu, status, category, ok, value=None, num=None, error="",
         age=None) -> dict:
    # age = seconds since this value was last freshly read (0 = read this cycle,
    # None = caller doesn't track it). The page charts only fresh points and dims
    # stale ones.
    return {"key": key, "name": name, "unit": _pretty_unit(unit), "ecu": ecu,
            "status": status, "category": category, "ok": ok, "value": value,
            "num": num, "error": error, "age": age}


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
        return [{"key": k, "name": n, "unit": u, "ecu": e, "status": "verified",
                 "category": categorize(e, n, u)[1]}
                for (k, n, u, e, _c, _base, _amp) in self._CATALOG[self._bus]]

    def read_selected(self, keys: list) -> list:
        spec = {k: (n, u, e, c, base, amp)
                for (k, n, u, e, c, base, amp) in self._CATALOG[self._bus]}
        t = time.monotonic() - self._t0
        rows = []
        for k in keys:
            if k not in spec:
                continue
            n, u, e, _c, base, amp = spec[k]
            num = round(base + amp * math.sin(t / 2.0 + hash(k) % 7), 3)
            rows.append(_row(k, n, u, e, "verified", categorize(e, n, u)[1], True,
                             value=(f"{num:g}"), num=num))
        return rows

    def read_config(self) -> dict:
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

    def read_dtcs(self) -> dict:
        if getattr(self, "_cleared", False):
            return {"bus": self._bus, "dtcs": []}
        return {"bus": self._bus, "dtcs": [
            {"ecu": "ECM", "code": "2A30",
             "text": "Error indicating a clogged particle filter"}]}

    def clear_dtcs(self) -> dict:
        self._cleared = True
        return {"bus": self._bus, "cleared": ["ECM"], "failed": []}


# ---------------------------------------------------------------------------
# Shared state between the poller and the server.
# ---------------------------------------------------------------------------

class _State:
    """The whole dashboard runs on ONE thread (see serve): the HTTP server is
    single-threaded and reads the adapter inside the request handler. The VXDIAG
    driver crashes if touched from any thread other than the one that opened the
    device, so there are deliberately no background threads here."""

    def __init__(self, backend: Backend) -> None:
        self.backend = backend
        self.started = time.monotonic()
        self.selection: list = []
        self.updated = 0.0


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
 .plist{flex:1;overflow-y:auto;padding:4px 8px 20px}
 .ptools{display:flex;align-items:center;gap:10px;padding:6px 8px 4px;color:var(--dim);
         font-size:11px}
 .ptools a{color:var(--muted);cursor:pointer;text-decoration:none}
 .ptools a:hover{color:var(--accent)}
 .grp{margin-top:2px}
 .ghead{display:flex;align-items:center;gap:7px;padding:7px 8px;border-radius:7px;cursor:pointer;
        user-select:none}
 .ghead:hover{background:var(--panel2)}
 .ghead .chev{color:var(--dim);font-size:10px;width:9px;flex:none;transition:transform .12s}
 .ghead.open .chev{transform:rotate(90deg)}
 .ghead .gn{flex:1;font-size:11px;text-transform:uppercase;letter-spacing:.1em;color:var(--muted);
            font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
 .ghead .cnt{font-size:10px;color:var(--dim);font-family:var(--mono);
             background:var(--panel2);border-radius:9px;padding:1px 7px}
 .ghead.sel .gn{color:var(--accent)}
 .ghead.sel .cnt{background:var(--accent-soft);color:#bfeee7}
 .gbody{padding-left:4px}
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
 .card.faint .val{opacity:.45;transition:opacity .25s}
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
 .crow .code{font-family:var(--mono);color:#ff6b6b;font-weight:600}
 .btn{background:var(--accent-soft);border:1px solid var(--accent);color:var(--ink);
      border-radius:8px;padding:8px 14px;cursor:pointer;font:600 13px var(--sans)}
 .btn.danger{background:rgba(255,107,107,.12);border-color:#ff6b6b;color:#ff9a9a}
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
      <div class="tab" id="tab-dtc" data-view="dtc">Codes</div>
    </div>
    <input type="text" id="search" placeholder="filter parameters…" class="live-only">
  </div>
  <div class="plist live-only" id="plist"></div>
  <div class="hint config-only hide">Configuration is read from the CEM on the
    500k bus (the toolkit switches to it automatically). Press Read on the right.</div>
  <div class="hint dtc-only hide">Trouble codes are swept from every module on
    the current bus. Switch the bus to scan the other half.</div>
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
var LAST={};   /* last good {value,unit,status} per key, to ride out a miss */

function selKey(){return 'volvo.sel.'+STATE.bus;}
function loadSel(){try{return JSON.parse(localStorage.getItem(selKey()))||[];}catch(e){return [];}}
function saveSel(){try{localStorage.setItem(selKey(),JSON.stringify(STATE.sel));}catch(e){}}
/* per-group collapsed state (persisted); groups start collapsed. */
var COLL={};
function collKey(){return 'volvo.coll.'+STATE.bus;}
function loadColl(){try{COLL=JSON.parse(localStorage.getItem(collKey()))||{};}catch(e){COLL={};}}
function saveColl(){try{localStorage.setItem(collKey(),JSON.stringify(COLL));}catch(e){}}
function isOpen(g){return COLL[g]===false;}  /* default: closed */

function itemHtml(p){
  var on=STATE.sel.indexOf(p.key)>=0;
  return '<label class="item'+(on?' sel':'')+'" data-key="'+esc(p.key)+'">'
    +'<input type="checkbox"'+(on?' checked':'')+'>'
    +'<span class="nm">'+esc(p.name)+'</span>'
    +'<span class="vv" id="vv-'+esc(p.key)+'">'+(p.unit?esc(p.unit):'')+'</span></label>';
}
/* ---------- sidebar parameter list ---------- */
function buildList(){
  var q=($('search').value||'').toLowerCase();
  var map={},i,p;
  for(i=0;i<STATE.params.length;i++)map[STATE.params[i].key]=STATE.params[i];
  var h='<div class="ptools"><a id="exp-all">expand all</a><a id="col-all">collapse all</a>'
    +'<span style="margin-left:auto">'+STATE.params.length+' params</span></div>';
  /* pinned: currently selected, always open */
  if(STATE.sel.length){
    h+='<div class="grp"><div class="ghead sel open" data-grp="__sel__">'
      +'<span class="chev">&#9654;</span><span class="gn">★ Selected</span>'
      +'<span class="cnt">'+STATE.sel.length+'</span></div><div class="gbody">';
    for(i=0;i<STATE.sel.length;i++){p=map[STATE.sel[i]];if(p)h+=itemHtml(p);}
    h+='</div></div>';
  }
  /* grouped, respecting search filter */
  var groups={},order=[];
  for(i=0;i<STATE.params.length;i++){p=STATE.params[i];
    if(q && p.name.toLowerCase().indexOf(q)<0 && p.key.toLowerCase().indexOf(q)<0) continue;
    if(!groups[p.category]){groups[p.category]=[];order.push(p.category);}
    groups[p.category].push(p);
  }
  for(i=0;i<order.length;i++){var lab=order[i],items=groups[lab],j;
    var open=q?true:isOpen(lab);var nsel=0;
    for(j=0;j<items.length;j++)if(STATE.sel.indexOf(items[j].key)>=0)nsel++;
    h+='<div class="grp"><div class="ghead'+(open?' open':'')+(nsel?' sel':'')
      +'" data-grp="'+esc(lab)+'"><span class="chev">&#9654;</span>'
      +'<span class="gn">'+esc(lab)+'</span>'
      +'<span class="cnt">'+(nsel?nsel+'/':'')+items.length+'</span></div>';
    if(open){h+='<div class="gbody">';
      for(j=0;j<items.length;j++)h+=itemHtml(items[j]);
      h+='</div>';}
    h+='</div>';
  }
  $('plist').innerHTML=h+(order.length?'':'<div class="hint">No parameters match.</div>');
  bindList();
}
function bindList(){
  var i,labels=$('plist').getElementsByClassName('item');
  for(i=0;i<labels.length;i++){
    labels[i].getElementsByTagName('input')[0].onchange=(function(el){return function(){
      toggle(el.getAttribute('data-key'),this.checked);};})(labels[i]);
  }
  var heads=$('plist').getElementsByClassName('ghead');
  for(i=0;i<heads.length;i++){
    heads[i].onclick=(function(el){return function(){
      var g=el.getAttribute('data-grp');if(g==='__sel__')return;
      COLL[g]=!(COLL[g]===false)?false:true;saveColl();buildList();};})(heads[i]);
  }
  if($('exp-all'))$('exp-all').onclick=function(){setAll(false);};
  if($('col-all'))$('col-all').onclick=function(){setAll(true);};
}
function setAll(closed){
  var i;for(i=0;i<STATE.params.length;i++)COLL[STATE.params[i].category]=closed;
  saveColl();buildList();
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
  var s=d.stats||{},by={},i,r,oldest=0;
  for(i=0;i<d.rows.length;i++){r=d.rows[i];by[r.key]=r;if((r.age||0)>oldest)oldest=r.age;}
  if($('meta')){
    var m=STATE.sel.length+' selected';
    if(s.rate)m+=' · '+s.rate+' Hz';
    if(s.cycle_ms!=null)m+=' · '+Math.round(s.cycle_ms)+' ms';
    if(s.timeouts)m+=' · <span class="stale">'+s.timeouts+' timeout'+(s.timeouts>1?'s':'')+'</span>';
    if(oldest>1.5)m+=' · oldest '+Math.round(oldest*1000)+' ms';
    $('meta').innerHTML=m;
  }
  for(i=0;i<STATE.sel.length;i++){var key=STATE.sel[i];r=by[key];
    var vv=$('vv-'+key);
    if(!r){continue;}
    var val=$('val-'+key),card=$('card-'+key),dot=$('dot-'+key);
    /* age = seconds since a fresh read; only chart fresh points, dim stale ones */
    var fresh=(r.age||0)<0.05,stale=(r.age||0)>1.5;
    if(card){
      if(r.ok){
        LAST[key]={value:r.value,unit:r.unit,status:r.status};
        if(fresh&&r.num!==null&&r.num!==undefined)pushHist(key,r.num);
        card.className='card'+(stale?' faint':'');
        dot.className='dot '+statusClass(stale?'error':r.status);
        val.innerHTML=esc(r.value)+(r.unit?'<span class="u">'+esc(r.unit)+'</span>':'');
        drawChart(key);
      } else if(LAST[key]){
        /* a transient miss (bus timeout): keep the last good value, dimmed,
           and leave the chart untouched rather than blinking to a dash */
        card.className='card faint';
        dot.className='dot '+statusClass('error');
        val.innerHTML=esc(LAST[key].value)+(LAST[key].unit?'<span class="u">'+esc(LAST[key].unit)+'</span>':'');
      } else {
        card.className='card bad';
        dot.className='dot s-error';
        val.innerHTML='<span title="'+esc(r.error||'')+'">—</span>';
      }
    }
    if(vv)vv.textContent=r.ok?(r.value+(r.unit?' '+r.unit:'')):(LAST[key]?LAST[key].value+(LAST[key].unit?' '+LAST[key].unit:''):'—');
  }
}
/* Self-chaining poll: the next /data is scheduled only after the previous one
   returns, so requests can never queue up and the UI never falls behind. */
function pollLoop(){
  if(STATE.view!=='live'||!STATE.sel.length){setTimeout(pollLoop,__INTERVAL__);return;}
  var t0=(new Date()).getTime();
  xhr('GET','data',null,function(ok,d){
    if(ok&&d)applyData(d);
    else if($('meta'))$('meta').textContent='disconnected';
    var gap=__INTERVAL__-((new Date()).getTime()-t0);
    setTimeout(pollLoop,gap<30?30:gap);
  });
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
function busBaud(id){var i;for(i=0;i<STATE.buses.length;i++)if(STATE.buses[i].id===id)return STATE.buses[i].baudrate;return 0;}
function bus125(){var i;for(i=0;i<STATE.buses.length;i++)if(STATE.buses[i].baudrate===125000)return STATE.buses[i].id;return 'ls';}
function loadConfig(){
  /* CEM is the gateway and answers on either bus, so just read it. */
  $('main').innerHTML='<div class="bar"><h2>Configuration</h2></div><div class="empty">Reading CEM…</div>';
  xhr('GET','config',null,function(ok,d){
    if(ok&&d)renderConfig(d);
    else renderConfig({error:'CEM did not answer. Check the car is on (ignition II).'});
  });
}

/* ---------- trouble-codes view ---------- */
function loadDtc(){
  $('main').innerHTML='<div class="bar"><h2>Trouble codes</h2></div><div class="empty">Scanning modules…</div>';
  xhr('GET','dtc',null,function(ok,d){
    if(ok&&d)renderDtc(d);
    else renderDtc({error:'scan failed. Check the car is on (ignition II).'});
  });
}
function renderDtc(data){
  if(data.error){
    $('main').innerHTML='<div class="bar"><h2>Trouble codes</h2></div><div class="cfg"><div class="note">'+esc(data.error)+'</div></div>';
    return;
  }
  var list=data.dtcs||[],h='',i,f;
  if(!list.length){
    h='<div class="note">No trouble codes on the '+esc(data.bus||'')+' bus.</div>';
  }else{
    h='<div class="card2">';
    for(i=0;i<list.length;i++){f=list[i];
      h+='<div class="crow"><span class="k"><b>'+esc(f.ecu)+'</b> <span class="code">'+esc(f.code)+'</span></span>'
        +'<span class="v">'+esc(f.text)+'</span></div>';}
    h+='</div>';
  }
  /* Clear is a WRITE — offer it only when there are codes, behind a confirm. */
  var clearBtn=list.length?'<button class="btn danger" onclick="clearDtc()">Clear codes</button> ':'';
  $('main').innerHTML='<div class="bar"><h2>Trouble codes <span class="badge">0xAE</span></h2>'
    +'<span class="meta">'+clearBtn+'<button class="btn" onclick="loadDtc()">Re-scan</button></span></div>'
    +'<div class="cfg">'+h+'</div>';
}
function clearDtc(){
  if(!confirm('Clear stored trouble codes on this bus? This writes to the car (AF 11).'))return;
  $('main').innerHTML='<div class="bar"><h2>Trouble codes</h2></div><div class="empty">Clearing…</div>';
  xhr('POST','dtc/clear',null,function(ok,d){
    if(ok&&d&&!d.error){
      var msg='Cleared: '+((d.cleared||[]).join(', ')||'none')
        +((d.failed&&d.failed.length)?' · no ack: '+d.failed.join(', '):'');
      alert(msg);
    } else alert((d&&d.error)||'clear failed');
    loadDtc();
  });
}

/* ---------- view + bus wiring ---------- */
function toggleOnly(cls,show){var els=document.getElementsByClassName(cls),i;
  for(i=0;i<els.length;i++)els[i].className=els[i].className.replace(/ ?hide/,'')+(show?'':' hide');}
function setView(v){
  STATE.view=v;
  var tabs=['live','config','dtc'],t;
  for(t=0;t<tabs.length;t++)$('tab-'+tabs[t]).className='tab'+(v===tabs[t]?' on':'');
  toggleOnly('live-only',v==='live');
  toggleOnly('config-only',v==='config');
  toggleOnly('dtc-only',v==='dtc');
  if(v==='live'){$('main').innerHTML=$('tpl-main-live').innerHTML;renderCards();}
  else if(v==='config'){loadConfig();}
  else{loadDtc();}
}
function loadParams(cb){
  xhr('GET','params',null,function(ok,d){
    STATE.params=(ok&&d&&d.params)||[];
    STATE.sel=loadSel().filter(function(k){
      for(var i=0;i<STATE.params.length;i++)if(STATE.params[i].key===k)return true;return false;});
    loadColl();pushSel();buildList();if(cb)cb();
  });
}
function syncBusSel(cur){var sel=$('bus');for(var i=0;i<sel.options.length;i++)sel.options[i].selected=(sel.options[i].value===cur);STATE.bus=cur;}
function switchBus(id,thenConfig){
  xhr('POST','bus',{id:id},function(ok,d){
    if(!ok||!d||!d.ok){
      /* the low-speed bus can be rejected by the adapter; the backend rolled
         back to the working bus, so tell the user and resync the selector */
      alert((d&&d.error)||'bus switch failed');
      syncBusSel((d&&d.current_bus)||STATE.bus);
      return;
    }
    HIST={};syncBusSel(d.current_bus||id);
    loadParams(function(){if(thenConfig){setView('config');}else{renderCards();}});
  });
}
function init(){
  xhr('GET','meta',null,function(ok,d){
    if(!ok||!d){$('desc').textContent='disconnected';return;}
    $('desc').textContent=d.description;STATE.bus=d.current_bus;STATE.buses=d.buses||[];
    var h='',i;for(i=0;i<d.buses.length;i++)h+='<option value="'+esc(d.buses[i].id)+'"'
      +(d.buses[i].id===d.current_bus?' selected':'')+'>'+esc(d.buses[i].label)+'</option>';
    $('bus').innerHTML=h;
    $('bus').onchange=function(){switchBus(this.value,false);};
    $('search').oninput=buildList;
    $('tab-live').onclick=function(){setView('live');};
    $('tab-config').onclick=function(){setView('config');};
    $('tab-dtc').onclick=function(){setView('dtc');};
    loadParams(function(){setView('live');});
  });
  pollLoop();
}
init();
</script></body></html>
"""


def serve(backend: Backend, interval: float = 0.5,
          host: str = "127.0.0.1", port: int = 8080) -> None:
    """Serves the dashboard (HTTP in a background thread) while polling the
    selected parameters on the main thread, which is the only thread the VXDIAG
    adapter works on."""
    state = _State(backend)

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
                # Read the selected parameters right here, on the server's one
                # and only thread — the adapter is read where it was opened.
                keys = list(state.selection)
                if keys:
                    try:
                        rows = backend.read_selected(keys)
                    except Exception as exc:  # noqa: BLE001 — surface, don't crash
                        rows = [_row(k, k, "", "", "error", "", False,
                                     error=f"{type(exc).__name__}: {exc}") for k in keys]
                    state.updated = time.monotonic()
                else:
                    rows = []
                self._json({
                    "uptime": round(time.monotonic() - state.started, 1),
                    "age": round(time.monotonic() - state.updated, 1) if state.updated else None,
                    "rows": rows,
                    "stats": backend.last_stats(),
                })
            elif path == "/config":
                try:
                    self._json(backend.read_config())
                except Exception as exc:  # noqa: BLE001
                    self._json({"error": f"{type(exc).__name__}: {exc}"})
            elif path == "/dtc":
                try:
                    self._json(backend.read_dtcs())
                except Exception as exc:  # noqa: BLE001
                    self._json({"error": f"{type(exc).__name__}: {exc}"})
            else:
                self.send_error(404)

        def do_POST(self):
            path = self.path.split("?", 1)[0].rstrip("/")
            body = read_body(self)
            if path == "/select":
                keys = body if isinstance(body, list) else []
                state.selection = [str(k) for k in keys]
                self._json({"ok": True, "n": len(keys)})
            elif path == "/bus":
                bus_id = (body or {}).get("id") if isinstance(body, dict) else None
                try:
                    backend.switch_bus(str(bus_id))
                    state.selection = []
                    self._json({"ok": True, "current_bus": backend.current_bus()})
                except Exception as exc:  # noqa: BLE001
                    # switch_bus rolls back to the working bus; report where we
                    # actually ended up so the page can resync its selector.
                    self._json({"ok": False, "error": str(exc),
                                "current_bus": backend.current_bus()}, 200)
            elif path == "/dtc/clear":
                try:
                    self._json(backend.clear_dtcs())    # a WRITE
                except Exception as exc:  # noqa: BLE001
                    self._json({"error": f"{type(exc).__name__}: {exc}"})
            else:
                self.send_error(404)

    # Single-threaded on purpose: HTTPServer handles one request at a time on the
    # main thread, so every adapter call happens on the thread that opened it.
    server = HTTPServer((host, port), Handler)
    print(backend.description())
    print(f"dashboard on http://{host}:{port}/   (ctrl-c to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        server.server_close()
        backend.close()
