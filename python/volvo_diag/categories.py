"""Grouping a parameter into a "Module · Subsystem" bucket for the dashboard
and the terminal monitor. A keyword heuristic over the ECU and the parameter
name — good enough to cluster hundreds of parameters into a browsable tree
without tagging every definition. Pure (no transport imports) so the web
preview can share it.
"""

from __future__ import annotations


def category_for(parameter) -> tuple:
    """`categorize` for a Parameter object — (sort_rank, "Module · Subsystem")."""
    return categorize(parameter.ecu, parameter.name, parameter.unit)


def categorize(ecu: str, name: str, unit: str) -> tuple:
    """Returns (sort_rank, "Module · Subsystem")."""
    ecu = (ecu or "").upper()
    n = (name or "").lower()
    u = unit or ""

    def kw(*words: str) -> bool:
        return any(w in n for w in words)

    # ---- ECM (engine), 0..19 ----------------------------------------------
    if ecu == "ECM":
        if kw("boost", "manifold", "turbo", "charge", "intercool", "bypass", "wastegate"):
            return (10, "ECM · Boost")
        if u == "kg/h" or kw("mass air", "air mass", "maf", "throttle", "atmospher", "swirl"):
            return (11, "ECM · Air & throttle")
        if kw("rail", "injection", "injector", "lambda", "fuel pressure", "quantity", "governor"):
            return (12, "ECM · Fuel & injection")
        if kw("fuel"):
            return (13, "ECM · Fuel system")
        if kw("egr"):
            return (14, "ECM · EGR")
        if kw("particulate", "dpf", "exhaust", "regener", "soot", "differential pressure"):
            return (15, "ECM · DPF & exhaust")
        if kw("glow", "preheat"):
            return (16, "ECM · Glow & start")
        if u == "degC" or kw("coolant", "temperature", " temp", "oil"):
            return (17, "ECM · Temperatures")
        if u in ("V", "A") or kw("voltage", "current", "battery", "supply", "alternator", "relay"):
            return (18, "ECM · Electrical")
        if kw("immo", "security", "key"):
            return (19, "ECM · Immobilizer")
        if kw("valve", "actuator", "pump", "fan", "lamp", "output", "power stage"):
            return (8, "ECM · Actuators")
        if kw("speed", "rpm", "load", "torque", "position", "crank", "cam"):
            return (7, "ECM · Engine")
        if kw("switch", "status", "state", "pedal", "clutch", "brake", "request"):
            return (9, "ECM · Status & switches")
        return (20, "ECM · Other")

    # ---- ABS (brakes), 20..29 ---------------------------------------------
    if ecu == "ABS":
        if kw("wheel", "speed"):
            return (21, "ABS · Wheel speeds")
        if kw("yaw", "lateral", "accel", "steer", "rotation"):
            return (22, "ABS · Vehicle dynamics")
        if kw("pressure", "brake", "master", "pedal"):
            return (23, "ABS · Brake pressure")
        if u in ("V", "A") or kw("voltage", "supply", "relay", "valve", "pump", "motor"):
            return (24, "ABS · Electrical & hydraulics")
        return (29, "ABS · Other")

    # ---- CEM (central electronics), 30..49 --------------------------------
    if ecu == "CEM":
        if kw("relay", "light", "lamp", "beam", "signal", "wiper", "washer", "horn",
               "indicator", "fog"):
            return (33, "CEM · Lighting & outputs")
        if u in ("V", "A") or kw("voltage", "current", "battery", "supply", "rheostat", "30-"):
            return (30, "CEM · Electrical")
        if u == "degC" or kw("temperature", " temp", "climate", "heater", "cabin"):
            return (31, "CEM · Climate")
        if kw("immo", "key", "lock", "alarm", "security", "steering column"):
            return (32, "CEM · Security & locking")
        if kw("door", "window", "mirror", "seat", "roof"):
            return (34, "CEM · Body & comfort")
        return (35, "CEM · Other")

    # ---- Infotainment / phone (low-speed), 56..59 -------------------------
    if ecu == "ICM":
        if kw("button", "remote control", "knob", "key"):
            return (56, "ICM · Controls")
        return (57, "ICM · States")
    if ecu == "BPM":
        return (58, "BPM · Bluetooth phone")

    # ---- DIM (instrument cluster), 50..59 ---------------------------------
    if ecu == "DIM":
        if kw("fuel", "level", "tank"):
            return (51, "DIM · Fuel & range")
        if u == "degC" or kw("temperature", " temp", "coolant"):
            return (52, "DIM · Temperatures")
        if kw("speed", "rpm", "odo", "distance", "trip"):
            return (53, "DIM · Speed & distance")
        if kw("lamp", "light", "warning", "indicator", "gauge"):
            return (54, "DIM · Indicators")
        return (55, "DIM · Other")

    return (90, f"{ecu or 'Other'} · Other")
