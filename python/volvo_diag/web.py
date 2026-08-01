"""A dependency-free web dashboard.

A background thread polls the ECU through the same reader the terminal monitor
uses and keeps the latest readings in memory; a stdlib HTTP server serves a
single self-contained page that polls `/data` for JSON and redraws. No third-
party packages, so it runs on the Windows 7 / Python 3.8 VIDA machine as-is —
open it in the guest's browser, or reach it from the host over the network.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _State:
    """Latest reading per parameter, shared between the poller and the server."""

    def __init__(self, description: str) -> None:
        self.description = description
        self.lock = threading.Lock()
        self.started = time.monotonic()
        self.rows: list = []          # ordered list of row dicts
        self.updated = 0.0
        self.stop = threading.Event()

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "description": self.description,
                "uptime": round(time.monotonic() - self.started, 1),
                "age": round(time.monotonic() - self.updated, 1) if self.updated else None,
                "rows": list(self.rows),
            }


def _poller(state: _State, read_one, params, category_fn, interval: float) -> None:
    while not state.stop.is_set():
        rows = []
        for parameter in params:
            try:
                reading = read_one(parameter)
            except Exception as exc:  # noqa: BLE001 — keep the poller alive
                from .volvo.ecm import Reading

                reading = Reading(parameter, error=f"{type(exc).__name__}: {exc}")
            rank, label = category_fn(parameter)
            num = None
            if reading.ok:
                value = parameter.format(reading.value)
                ok, error = True, ""
                if isinstance(reading.value, (int, float)) and not isinstance(reading.value, bool):
                    num = round(float(reading.value), 4)
            else:
                value, ok, error = None, False, reading.error
            rows.append({
                "key": parameter.key,
                "name": parameter.name,
                "value": value,
                "num": num,
                "unit": parameter.unit,
                "status": parameter.status,
                "ecu": parameter.ecu,
                "rank": rank,
                "group": label,
                "ok": ok,
                "error": error,
            })
        rows.sort(key=lambda r: (r["rank"], r["name"]))
        with state.lock:
            state.rows = rows
            state.updated = time.monotonic()
        if state.stop.wait(interval):
            break


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Volvo monitor</title>
<style>
 /* Instrument-cluster dark: a blue-black ground, values as bright tabular
    read-outs, module state carried by a small status light. Deliberately a
    single dark theme - this is a car's instrument panel. */
 :root {
   color-scheme: dark;
   --bg:#0b0e13; --panel:#141a23; --edge:#222b39; --edge-soft:#1b2230;
   --ink:#eaeef4; --muted:#7c8798; --value:#f4f7fb; --accent:#46b1a6;
   --live:#4ea36b; --ok-vida:#57b078; --ok-db:#4b8fd6; --exp:#d79b3f;
   --cand:#9d80d6; --none:#3a414d;
   --mono:ui-monospace,"SF Mono","Cascadia Mono","Segoe UI Mono",Menlo,Consolas,monospace;
   --sans:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
 }
 * { box-sizing:border-box; }
 body { margin:0; background:var(--bg); color:var(--ink); font:14px/1.45 var(--sans);
        -webkit-font-smoothing:antialiased; }
 header { position:sticky; top:0; z-index:2; background:rgba(11,14,19,.92);
          backdrop-filter:blur(6px); border-bottom:1px solid var(--edge);
          padding:13px 22px; display:flex; gap:14px; align-items:center; flex-wrap:wrap; }
 .live { width:9px; height:9px; border-radius:50%; background:var(--live);
         box-shadow:0 0 0 0 rgba(78,163,107,.6); animation:pulse 2s infinite; flex:none; }
 @keyframes pulse { 70%{ box-shadow:0 0 0 7px rgba(78,163,107,0); } 100%{ box-shadow:0 0 0 0 rgba(78,163,107,0); } }
 @media (prefers-reduced-motion:reduce){ .live{ animation:none; } }
 header h1 { font-size:14px; margin:0; font-weight:600; letter-spacing:.01em; }
 header .meta { color:var(--muted); font-size:12px; font-family:var(--mono);
                margin-left:auto; font-variant-numeric:tabular-nums; }
 header .stale { color:var(--exp); }
 main { padding:20px 22px 40px; display:grid; gap:20px;
        grid-template-columns:repeat(auto-fill,minmax(360px,1fr)); align-items:start; }
 .group h2 { font-size:11px; text-transform:uppercase; letter-spacing:.14em;
             color:var(--muted); margin:0 0 9px 2px; font-weight:600; }
 .card { background:linear-gradient(180deg,#161d27,var(--panel));
         border:1px solid var(--edge); border-radius:12px; overflow:hidden;
         box-shadow:0 1px 0 rgba(255,255,255,.02) inset, 0 10px 24px -18px #000; }
 .row { display:grid; grid-template-columns:1fr 74px auto 8px; gap:12px; align-items:center;
        padding:10px 14px; border-top:1px solid var(--edge-soft); }
 .row:first-child { border-top:0; }
 .row .name { color:#c6cdd8; min-width:0; overflow:hidden; text-overflow:ellipsis;
              white-space:nowrap; align-self:baseline; }
 .row .spark { width:74px; height:22px; display:block; opacity:.9; }
 .row .val { font-family:var(--mono); font-variant-numeric:tabular-nums;
             font-weight:600; font-size:16px; color:var(--value); text-align:right;
             letter-spacing:-.01em; }
 .row .val .u { color:var(--muted); font-weight:400; font-size:11px; margin-left:5px;
                letter-spacing:0; }
 .row.bad .val { color:#586274; font-weight:400; font-size:13px; }
 .dot { width:8px; height:8px; border-radius:50%; align-self:center; flex:none;
        box-shadow:0 0 6px -1px currentColor; }
 .s-verified-against-vida { background:var(--ok-vida); color:var(--ok-vida); }
 .s-verified { background:var(--ok-db); color:var(--ok-db); }
 .s-experimental,.s-discovered { background:var(--exp); color:var(--exp); }
 .s-candidate { background:var(--cand); color:var(--cand); }
 .s-none { background:var(--none); color:var(--none); box-shadow:none; }
</style></head><body>
<header><span class="live"></span><h1 id="desc">Volvo monitor</h1>
 <span class="meta" id="meta"></span></header>
<main id="main"></main>
<script>
 /* Plain ES5 + XMLHttpRequest, so the guest's old browser can render it too. */
 function esc(s){ return String(s).replace(/[&<>]/g, function(c){
   return {'&':'&amp;','<':'&lt;','>':'&gt;'}[c]; }); }
 function statusClass(s){ return 's-' + String(s || 'none').replace(/[^a-z-]/g,''); }
 var HIST = {}, HCAP = 90;   /* per-parameter value history for the sparklines */
 function pushHist(key, num){
   if (num === null || num === undefined) return;
   var a = HIST[key] || (HIST[key] = []);
   a.push(num); if (a.length > HCAP) a.shift();
 }
 function spark(canvas){
   var key = canvas.getAttribute('data-key'), a = HIST[key];
   var ctx = canvas.getContext('2d'), w = canvas.width, h = canvas.height;
   ctx.clearRect(0, 0, w, h);
   if (!a || a.length < 2) return;
   var min = Math.min.apply(null, a), max = Math.max.apply(null, a), rng = (max - min) || 1;
   var X = function(i){ return 1 + i / (a.length - 1) * (w - 2); };
   var Y = function(v){ return h - 2 - (v - min) / rng * (h - 4); };
   var i;
   ctx.beginPath(); ctx.moveTo(X(0), h);
   for (i = 0; i < a.length; i++) ctx.lineTo(X(i), Y(a[i]));
   ctx.lineTo(X(a.length - 1), h); ctx.closePath();
   ctx.fillStyle = 'rgba(70,177,166,.13)'; ctx.fill();
   ctx.beginPath();
   for (i = 0; i < a.length; i++){ if (i) ctx.lineTo(X(i), Y(a[i])); else ctx.moveTo(X(i), Y(a[i])); }
   ctx.strokeStyle = '#46b1a6'; ctx.lineWidth = 1.25; ctx.stroke();
   ctx.beginPath(); ctx.arc(X(a.length - 1), Y(a[a.length - 1]), 1.7, 0, 6.2832);
   ctx.fillStyle = '#9fe6dd'; ctx.fill();
 }
 function draw(d){
   document.getElementById('desc').textContent = d.description;
   var stale = d.age !== null && d.age > 3;
   document.getElementById('meta').innerHTML = d.rows.length + ' params · ' + d.uptime + 's'
     + (stale ? ' · <span class="stale">stale ' + d.age + 's</span>' : '');
   var order = [], groups = {}, i, r;
   for (i = 0; i < d.rows.length; i++){
     r = d.rows[i];
     if (!groups[r.group]){ groups[r.group] = []; order.push(r.group); }
     groups[r.group].push(r);
   }
   var html = '';
   for (i = 0; i < order.length; i++){
     var label = order[i], rows = groups[label], j;
     html += '<section class="group"><h2>' + esc(label) + '</h2><div class="card">';
     for (j = 0; j < rows.length; j++){
       r = rows[j];
       pushHist(r.key, r.num);
       var val = r.ok
         ? esc(r.value) + (r.unit ? '<span class="u">' + esc(r.unit) + '</span>' : '')
         : '—';
       var canvas = (r.num !== null && r.num !== undefined)
         ? '<canvas class="spark" width="74" height="22" data-key="' + esc(r.key) + '"></canvas>'
         : '<span></span>';
       html += '<div class="row' + (r.ok ? '' : ' bad') + '" title="' + esc(r.status) + '">'
         + '<span class="name">' + esc(r.name) + '</span>'
         + canvas
         + '<span class="val">' + val + '</span>'
         + '<span class="dot ' + statusClass(r.status) + '"></span></div>';
     }
     html += '</div></section>';
   }
   document.getElementById('main').innerHTML = html;
   var cvs = document.getElementsByClassName('spark');
   for (i = 0; i < cvs.length; i++) spark(cvs[i]);
 }
 function tick(){
   var x = new XMLHttpRequest();
   x.open('GET', 'data', true);
   x.onreadystatechange = function(){
     if (x.readyState !== 4) return;
     if (x.status === 200){ try { draw(JSON.parse(x.responseText)); } catch(e){} }
     else { document.getElementById('meta').textContent = 'disconnected'; }
   };
   x.send();
 }
 tick(); setInterval(tick, 500);
</script></body></html>
"""


def serve(description: str, read_one, params, category_fn, interval: float = 0.5,
          host: str = "127.0.0.1", port: int = 8080) -> None:
    """Polls in the background and serves the dashboard until interrupted."""
    state = _State(description)
    thread = threading.Thread(
        target=_poller, args=(state, read_one, params, category_fn, interval), daemon=True
    )
    thread.start()

    page = PAGE.encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # quiet
            pass

        def _send(self, body: bytes, content_type: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.rstrip("/") in ("", "/index.html"):
                self._send(page, "text/html; charset=utf-8")
            elif self.path.rstrip("/") == "/data":
                body = json.dumps(state.snapshot()).encode("utf-8")
                self._send(body, "application/json")
            else:
                self.send_error(404)

    server = ThreadingHTTPServer((host, port), Handler)
    where = f"http://{host}:{port}/"
    print(f"{description}")
    print(f"dashboard on {where}   (ctrl-c to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        state.stop.set()
        server.shutdown()
