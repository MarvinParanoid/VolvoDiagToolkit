"""Hardware capability probe for an ELM327 on the Volvo A6 protocol.

Cheap ELM327 clones vary wildly: many cannot do raw 29-bit CAN, refuse `ATCAF0`,
or drop frames. Before trusting one, this runs the exact sequence the A6 read
path needs — 29-bit CAN at 500k, raw mode, a custom request id, an A6 read to the
ECM — times it, and repeats the read to gauge stability. Serial only (a Bluetooth
ELM must be bound to a serial port first, e.g. rfcomm on Linux).
"""

from __future__ import annotations

import time

from .protocol import volvo

# steps that must pass for raw A6 to be possible at all
CRITICAL = {"29-bit raw CAN", "raw mode (CAF0)", "custom request id"}


def _txn(ser, cmd: str, timeout: float = 2.0) -> str:
    ser.reset_input_buffer()
    ser.write((cmd + "\r").encode())
    buf = bytearray()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        chunk = ser.read(64)
        if chunk:
            buf += chunk
            if b">" in buf:
                break
    return bytes(buf).decode("latin-1")


def _words(reply: str) -> list:
    return reply.replace("\r", " ").replace(">", " ").split()


def probe(port: str, baud: int = 38400, reads: int = 10) -> dict:
    try:
        import serial  # noqa: PLC0415 — optional dependency
    except ImportError as exc:
        raise RuntimeError("pyserial is required (pip install pyserial)") from exc

    ser = serial.Serial(port, baud, timeout=0.2)
    report = {"version": "", "steps": [], "reads": reads, "reads_ok": 0, "latencies": []}

    def step(name: str, cmd: str, want: str = "OK") -> str:
        reply = _txn(ser, cmd)
        ok = want.upper() in reply.upper().replace(" ", "")
        report["steps"].append({"name": name, "ok": ok, "reply": " ".join(_words(reply))})
        return reply

    try:
        ver = step("AT interface", "ATZ", want="ELM")
        report["version"] = next((w for w in _words(ver) if "V" in w.upper() and any(c.isdigit() for c in w)),
                                 "ELM327")
        step("AT interface", "ATE0")     # echo off (re-uses the AT-interface line)
        step("_", "ATL0")
        step("_", "ATS0")
        step("29-bit raw CAN", "ATSP7")
        step("_", "ATH1")
        step("raw mode (CAF0)", "ATCAF0")
        step("_", "ATCP00")
        step("custom request id", "ATSH0FFFFE")
        step("_", "ATCRA400021")

        request = volvo.build_read(0x0005, 0x11).hex().upper()  # ECM coolant read
        reply = ""
        for _ in range(reads):
            t0 = time.monotonic()
            reply = _txn(ser, request, timeout=1.0)
            dt = (time.monotonic() - t0) * 1000
            if "11E60005" in reply.upper().replace(" ", ""):    # echoed group/service/id
                report["reads_ok"] += 1
                report["latencies"].append(dt)
        report["response_sample"] = " ".join(_words(reply))
    finally:
        ser.close()

    lat = report["latencies"]
    report["avg_ms"] = round(sum(lat) / len(lat)) if lat else None
    report["reads_per_s"] = max(1, round(1000 / report["avg_ms"])) if report.get("avg_ms") else 0
    named = {s["name"]: s["ok"] for s in report["steps"] if s["name"] != "_"}
    report["critical_ok"] = all(named.get(n) for n in CRITICAL)
    report["responded"] = report["reads_ok"] > 0
    report["verdict"] = ("SUITABLE" if report["critical_ok"] and report["responded"]
                         else "NOT SUITABLE" if not report["critical_ok"]
                         else "INCONCLUSIVE (interface ok, no ECM answer — ignition on? right bus?)")
    return report


def format_report(report: dict) -> str:
    lines = [f"{'ELM327 version':<28} {report['version']}"]
    seen = set()
    for s in report["steps"]:
        if s["name"] == "_" or s["name"] in seen:
            continue
        seen.add(s["name"])
        lines.append(f"{s['name']:<28} {'OK' if s['ok'] else 'FAIL'}")
    lines.append(f"{'Expected ECM response':<28} {'OK' if report['responded'] else 'no answer'}")
    lines.append(f"{report['reads_ok']}/{report['reads']} reads successful")
    if report.get("avg_ms") is not None:
        lines.append(f"{'Average round trip':<28} {report['avg_ms']} ms")
        lines.append(f"{'Suggested polling rate':<28} ~{report['reads_per_s']} reads/s")
    lines.append("")
    lines.append(f"verdict: {report['verdict']}")
    return "\n".join(lines) + "\n"
