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


def _monitor_frames(ser, seconds: float = 1.5) -> int:
    """Passively watch the bus with ATMA and count the frame lines seen. The P1
    500k bus is busy at idle, so a nonzero count proves the adapter is on the
    right bus at the right baud; zero means wrong OBD pins/baud (or the bus is
    asleep) — which separates a wiring problem from a request/filter one."""
    ser.reset_input_buffer()
    ser.write(b"ATMA\r")
    buf = bytearray()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        chunk = ser.read(256)
        if chunk:
            buf += chunk
    ser.write(b"\r")            # any character stops ATMA
    time.sleep(0.15)
    ser.read(512)              # drain the prompt
    lines = bytes(buf).decode("latin-1").replace("\r", "\n").split("\n")
    hexset = set("0123456789ABCDEFabcdef ")
    return sum(1 for ln in lines
               if len(ln.strip()) >= 4 and "ATMA" not in ln.upper()
               and set(ln.strip()) <= hexset)


def probe(port: str, baud: int = 38400, reads: int = 10) -> dict:
    # Shared opener: a Bluetooth MAC ("AA:BB:..@ch") connects an RFCOMM socket,
    # a device path opens pyserial. The report path used to open pyserial
    # directly, so a MAC failed before the AT sequence even ran.
    from .transport.elm_can import open_serial  # noqa: PLC0415

    ser = open_serial(port, baud, timeout=0.2)
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

        # Passive check first: is the adapter even on the 500k bus? (ATMA ignores
        # filters, so it sees everything the bus carries.)
        report["bus_frames"] = _monitor_frames(ser)

        # Match the real transport's receive filter (any 0x0040xxxx response),
        # not a single exact id — a too-tight ATCRA can drop a valid answer.
        step("_", "ATCM00FF0000")
        step("_", "ATCF00400000")

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
    saw_bus = report.get("bus_frames", 0) > 0
    if not report["critical_ok"]:
        report["verdict"] = "NOT SUITABLE"
    elif report["responded"]:
        report["verdict"] = "SUITABLE"
    elif saw_bus:
        report["verdict"] = ("INCONCLUSIVE — on the 500k bus (sees traffic) but the "
                             "ECM did not answer the A6 read: request/filter/gateway, not wiring")
    else:
        report["verdict"] = ("NOT ON THE 500k BUS — no passive traffic seen: wrong OBD "
                             "pins/baud for the A6 bus (or the bus is asleep — key in II?)")
    return report


def format_report(report: dict) -> str:
    lines = [f"{'ELM327 version':<28} {report['version']}"]
    seen = set()
    for s in report["steps"]:
        if s["name"] == "_" or s["name"] in seen:
            continue
        seen.add(s["name"])
        lines.append(f"{s['name']:<28} {'OK' if s['ok'] else 'FAIL'}")
    if "bus_frames" in report:
        n = report["bus_frames"]
        lines.append(f"{'Passive 500k traffic':<28} "
                     f"{f'{n} frames seen' if n else 'none — wrong bus/baud?'}")
    lines.append(f"{'Expected ECM response':<28} {'OK' if report['responded'] else 'no answer'}")
    lines.append(f"{report['reads_ok']}/{report['reads']} reads successful")
    if report.get("avg_ms") is not None:
        lines.append(f"{'Average round trip':<28} {report['avg_ms']} ms")
        lines.append(f"{'Suggested polling rate':<28} ~{report['reads_per_s']} reads/s")
    lines.append("")
    lines.append(f"verdict: {report['verdict']}")
    return "\n".join(lines) + "\n"
