"""Post-drive analysis of a recorded trip.

`volvo-monitor record trip.csv` writes one row per sample (a `t` column in
seconds, then one column per parameter). This turns that CSV into a report aimed
at the diesel/DPF picture: boost tracking, exhaust/DPF temperature, DPF pressure,
warm-up, and probable regeneration windows — as text and, optionally, a
self-contained HTML page with charts.
"""

from __future__ import annotations

import csv
import html
import json
from pathlib import Path

# role -> (label, exact column keys preferred, substring fallbacks)
ROLES = [
    ("rpm", "Engine speed", ["rpm", "ecm_engine_speed_rpm_sensor"], ["rpm", "engine_speed"]),
    ("boost", "Boost, actual", ["boost_actual", "ecm_boost_pressure"],
     ["boost_pressure", "manifold_pressure"]),
    ("boost_req", "Boost, requested", ["boost_requested", "ecm_boost_pressure_desired_value"],
     ["boost_desired", "boost_requested", "boost_setpoint"]),
    ("egt", "Exhaust / DPF temperature",
     ["exhaust_temperature", "dpf_temperature_upstream", "ecm_temperature_sensor_particulate_filter"],
     ["exhaust", "egt", "dpf_temp"]),
    ("dpf_p", "DPF differential pressure",
     ["dpf_differential_pressure", "ecm_pressure_sensor_particulate_filter"],
     ["differential_pressure", "particulate_filter_pressure"]),
    ("maf", "Mass air flow", ["maf", "ecm_mass_air_flow_sensor"], ["mass_air_flow", "mass_air"]),
    ("coolant", "Coolant temperature",
     ["coolant_temperature", "ecm_engine_coolant_temperature"], ["coolant_temperature"]),
    ("rail", "Fuel rail pressure", ["fuel_rail_pressure"], ["rail_pressure", "fuel_pressure"]),
    ("regen", "Regeneration active", ["regeneration_active"], ["regeneration"]),
]

EGT_REGEN_C = 450.0      # sustained exhaust temp above this looks like a regen
REGEN_MIN_S = 20.0       # ... for at least this long
WARM_C = 88.0
WARMING_C = 70.0


def load_trip(path: str | Path):
    """Returns (times, columns) — times a list of seconds, columns a dict of
    key -> list aligned with times (None where the sample was missing)."""
    with Path(path).open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader, [])
        if not header or header[0] != "t":
            raise ValueError("not a trip CSV (first column must be 't')")
        keys = header[1:]
        times: list = []
        cols: dict = {k: [] for k in keys}
        for row in reader:
            if not row:
                continue
            try:
                times.append(float(row[0]))
            except ValueError:
                continue
            for i, key in enumerate(keys, start=1):
                cell = row[i] if i < len(row) else ""
                try:
                    cols[key].append(float(cell))
                except (ValueError, TypeError):
                    cols[key].append(None)
    return times, cols


def detect_roles(cols: dict) -> dict:
    roles, used = {}, set()
    keys = list(cols)
    for role, _label, exact, subs in ROLES:
        chosen = None
        for k in exact:
            if k in cols and k not in used:
                chosen = k
                break
        if not chosen:
            for k in keys:
                if k in used:
                    continue
                low = k.lower()
                if any(s in low for s in subs):
                    chosen = k
                    break
        if chosen:
            roles[role] = chosen
            used.add(chosen)
    return roles


def _clean(series):
    return [v for v in series if v is not None]


def _stats(series):
    vals = _clean(series)
    if not vals:
        return None
    return {"min": min(vals), "max": max(vals), "mean": sum(vals) / len(vals),
            "last": vals[-1], "n": len(vals)}


def _time_to(times, series, threshold):
    """Seconds from the start until `series` first reaches `threshold`."""
    for t, v in zip(times, series):
        if v is not None and v >= threshold:
            return round(t - times[0], 1)
    return None


def _regen_windows(times, egt):
    """Contiguous stretches with EGT above the regen threshold, long enough."""
    windows, start = [], None
    for i, (t, v) in enumerate(zip(times, egt)):
        hot = v is not None and v >= EGT_REGEN_C
        if hot and start is None:
            start = i
        elif not hot and start is not None:
            windows.append((start, i - 1))
            start = None
    if start is not None:
        windows.append((start, len(times) - 1))
    out = []
    for a, b in windows:
        dur = times[b] - times[a]
        if dur >= REGEN_MIN_S:
            peak = max(v for v in egt[a:b + 1] if v is not None)
            out.append({"start_s": round(times[a] - times[0], 1),
                        "duration_s": round(dur, 1), "peak": round(peak, 1)})
    return out


def analyze(times, cols, roles):
    report = {"duration_s": round(times[-1] - times[0], 1) if times else 0,
              "samples": len(times), "stats": {}, "warmup": {}, "boost": {}, "regen": []}
    for role, key in roles.items():
        s = _stats(cols[key])
        if s:
            report["stats"][role] = s

    if "coolant" in roles:
        c = cols[roles["coolant"]]
        report["warmup"] = {"to_70C_s": _time_to(times, c, WARMING_C),
                            "to_88C_s": _time_to(times, c, WARM_C)}

    if "boost" in roles and "boost_req" in roles:
        act, req = cols[roles["boost"]], cols[roles["boost_req"]]
        devs = [abs(a - r) for a, r in zip(act, req) if a is not None and r is not None]
        if devs:
            report["boost"] = {"max_dev": round(max(devs), 1),
                               "mean_dev": round(sum(devs) / len(devs), 1)}

    if "regen" in roles:
        r = cols[roles["regen"]]
        active = [i for i, v in enumerate(r) if v]
        if active:
            report["regen"] = [{"start_s": round(times[active[0]] - times[0], 1),
                                "duration_s": round(times[active[-1]] - times[active[0]], 1),
                                "peak": None, "source": "flag"}]
    if not report["regen"] and "egt" in roles:
        report["regen"] = [dict(w, source="egt>%.0fC" % EGT_REGEN_C)
                           for w in _regen_windows(times, cols[roles["egt"]])]
    return report


def format_text(report, roles) -> str:
    lines = [f"trip: {report['duration_s']:.0f} s, {report['samples']} samples", ""]
    label = {r[0]: r[1] for r in ROLES}
    lines.append(f"{'parameter':<28} {'min':>10} {'max':>10} {'mean':>10} {'last':>10}")
    for role, s in report["stats"].items():
        lines.append(f"{label.get(role, role):<28} {s['min']:>10.1f} {s['max']:>10.1f} "
                     f"{s['mean']:>10.1f} {s['last']:>10.1f}")
    w = report.get("warmup") or {}
    if w.get("to_70C_s") is not None or w.get("to_88C_s") is not None:
        lines += ["", f"warm-up: to 70 C {w.get('to_70C_s')} s, to 88 C {w.get('to_88C_s')} s"]
    if report.get("boost"):
        b = report["boost"]
        lines += ["", f"boost tracking: max deviation {b['max_dev']}, mean {b['mean_dev']}"]
    if report.get("regen"):
        lines += ["", "probable regeneration:"]
        for r in report["regen"]:
            peak = f", peak {r['peak']} C" if r.get("peak") else ""
            lines.append(f"  at {r['start_s']} s for {r['duration_s']} s ({r['source']}){peak}")
    elif "egt" in roles:
        lines += ["", "no regeneration detected"]
    return "\n".join(lines) + "\n"


# groups of roles drawn on one chart
_CHARTS = [
    ("Boost — actual vs requested", ["boost", "boost_req"]),
    ("Exhaust / DPF temperature", ["egt"]),
    ("DPF differential pressure", ["dpf_p"]),
    ("Engine speed & mass air flow", ["rpm", "maf"]),
    ("Coolant temperature", ["coolant"]),
]


def format_html(times, cols, roles, report) -> str:
    label = {r[0]: r[1] for r in ROLES}
    t0 = times[0] if times else 0
    rel = [round(t - t0, 2) for t in times]
    charts = []
    for title, group in _CHARTS:
        series = [{"name": label.get(r, r), "data": cols[roles[r]]}
                  for r in group if r in roles]
        if series:
            charts.append({"title": title, "series": series})
    regen = [{"start": r["start_s"], "dur": r["duration_s"]} for r in report.get("regen", [])]
    data = {"t": rel, "charts": charts, "regen": regen}
    summary = html.escape(format_text(report, roles))
    return _HTML.replace("__DATA__", json.dumps(data)).replace("__SUMMARY__", summary)


_HTML = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Trip report</title>
<style>
 :root{color-scheme:dark;--bg:#0a0d12;--panel:#151c26;--edge:#232d3c;--ink:#e9eef5;--muted:#7c8798}
 body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,sans-serif;padding:22px}
 h1{font-size:18px;margin:0 0 4px} .sub{color:var(--muted);font-size:12px;margin-bottom:18px}
 pre{background:var(--panel);border:1px solid var(--edge);border-radius:10px;padding:14px;
     overflow-x:auto;font:12px/1.5 ui-monospace,Consolas,monospace}
 .card{background:var(--panel);border:1px solid var(--edge);border-radius:12px;padding:14px 16px;margin:14px 0}
 .card h2{font-size:13px;margin:0 0 8px;color:#cdd5e0}
 canvas{width:100%;height:180px;display:block}
 .legend{display:flex;gap:16px;font-size:12px;color:var(--muted);margin-top:6px;flex-wrap:wrap}
 .legend span::before{content:'';display:inline-block;width:10px;height:2px;margin-right:6px;vertical-align:middle}
</style></head><body>
<h1>Trip report</h1><div class="sub">generated by volvo-monitor analyze</div>
<pre>__SUMMARY__</pre>
<div id="charts"></div>
<script>
var D=__DATA__, COLORS=['#5bd0c3','#d79b3f','#9d80d6','#57b078'];
function draw(cv, series, regen){
  var r=window.devicePixelRatio||1, w=cv.clientWidth||600, h=180;
  cv.width=w*r; cv.height=h*r; var c=cv.getContext('2d'); c.setTransform(r,0,0,r,0,0);
  c.clearRect(0,0,w,h); var pad=6, n=D.t.length; if(n<2)return;
  var xs=D.t, xmin=xs[0], xmax=xs[n-1], xr=(xmax-xmin)||1;
  var lo=Infinity, hi=-Infinity, i, k, v;
  for(k=0;k<series.length;k++)for(i=0;i<n;i++){v=series[k].data[i]; if(v!=null){if(v<lo)lo=v; if(v>hi)hi=v;}}
  if(lo===Infinity)return; var yr=(hi-lo)||1; lo-=yr*0.08; hi+=yr*0.08; yr=hi-lo;
  var X=function(i){return pad+(xs[i]-xmin)/xr*(w-2*pad);}, Y=function(v){return h-pad-(v-lo)/yr*(h-2*pad);};
  // regen shading
  c.fillStyle='rgba(215,155,63,.12)';
  for(k=0;k<regen.length;k++){var a=pad+(regen[k].start)/xr*(w-2*pad),
    ww=(regen[k].dur)/xr*(w-2*pad); c.fillRect(a,pad,Math.max(2,ww),h-2*pad);}
  c.strokeStyle='rgba(255,255,255,.05)';
  for(i=0;i<=3;i++){var gy=pad+i/3*(h-2*pad);c.beginPath();c.moveTo(pad,gy);c.lineTo(w-pad,gy);c.stroke();}
  for(k=0;k<series.length;k++){c.beginPath();c.strokeStyle=COLORS[k%COLORS.length];c.lineWidth=1.4;
    var started=false;
    for(i=0;i<n;i++){v=series[k].data[i]; if(v==null){started=false;continue;}
      if(started)c.lineTo(X(i),Y(v)); else{c.moveTo(X(i),Y(v));started=true;}}
    c.stroke();}
}
var host=document.getElementById('charts');
for(var ci=0;ci<D.charts.length;ci++){var ch=D.charts[ci];
  var card=document.createElement('div');card.className='card';
  var leg=''; for(var s=0;s<ch.series.length;s++)leg+='<span style="color:'+COLORS[s%4]+
    '"><span style="background:'+COLORS[s%4]+'"></span>'+ch.series[s].name+'</span>';
  card.innerHTML='<h2>'+ch.title+'</h2><canvas></canvas><div class="legend">'+leg+'</div>';
  host.appendChild(card); draw(card.querySelector('canvas'), ch.series, D.regen);
}
</script></body></html>"""
