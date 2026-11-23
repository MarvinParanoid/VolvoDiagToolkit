#!/usr/bin/env python3
"""H2: which ProtocolID did each channel's filters and messages carry, what
did the driver answer, and was every service written a read?

    python scripts/h2_proto_check.py LOG.jsonl [...]

Exit status:
  0  every write was a recognised read service, and the log is complete;
  1  a write carried a non-read service: STOP AND INVESTIGATE;
  3  inconclusive: writes that could not be classified (not a Volvo single
     frame or an ISO15765 message, e.g. ISO-TP continuation/flow-control or
     broadcast frames on a raw CAN channel), records on channels whose
     `connect` is not in the log (a capture that starts mid-session), or
     unreadable lines. Nothing unsafe was seen, but the audit is not complete.
"""
from __future__ import annotations

import collections
import json
import sys

# Read-only services: Volvo A6 family reads and UDS/OBD reads (see
# python/volvo_diag/safety.py). Anything else written is flagged.
READ = {"A5", "A6", "A7", "AE", "B9", "BB", "01", "02", "03", "05", "06", "07", "09",
        "0A", "19", "1A", "21", "22", "3E"}


ISO15765_ADDR_TYPE = 0x80                            # TxFlags: extended addressing


def service_of(proto: int, data: bytes, tx_flags: int = 0):
    """The service byte of one written message, or None if the message is not
    something whose service can be read off (never guessed)."""
    body = data[4:]                                  # drop the 4-byte CAN id
    if proto == 6:                                   # ISO15765: a whole message
        if tx_flags & ISO15765_ADDR_TYPE:            # [target address][service]...
            body = body[1:]
        return body[0] if body else None
    if len(body) > 2 and 0xC8 <= body[0] <= 0xCF:    # Volvo single frame
        return body[2]                               # [C8+n][comm][service]
    # Anything else on a raw CAN channel (ISO-TP first/consecutive/flow-control
    # frames, Volvo multi-frame parts, broadcast frames) carries no service byte
    # at a known position: unclassified, not "unsafe" and not "read".
    return None


def check(path: str, out=print) -> int:
    """Prints the report for one log; returns its exit status (0/1/3)."""
    chan: dict = {}                                  # channel -> (protocol, baud)
    tally: collections.Counter = collections.Counter()
    sent: collections.Counter = collections.Counter()
    orphans: collections.Counter = collections.Counter()   # ev on unknown channel
    bad_lines = 0
    for line in open(path, encoding="utf-8", errors="replace"):
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except ValueError:
            bad_lines += 1
            continue
        ev = r.get("ev")
        if ev == "write":
            # Audit the service of EVERY write, including writes on a channel
            # whose connect is missing: unknown context must never hide a
            # non-read service.
            for m in r.get("msgs") or []:
                svc = service_of(m["proto"], bytes.fromhex(m.get("data", "")),
                                 m.get("tx_flags", 0))
                sent[(f"{m['proto']:#06x}", None if svc is None else f"{svc:02X}")] += 1
        if ev in ("start_filter", "write", "read") and r.get("channel") not in chan:
            orphans[ev] += 1
            continue
        if ev == "connect":
            out(f"connect protocol={r['protocol']:#06x} ({r.get('protocol_name')}) "
                f"baud={r['baud']} flags={r['flags']} -> {r['result_name']} "
                f"channel={r['channel']}")
            if r["result"] == 0:
                chan[r["channel"]] = (r["protocol"], r["baud"])
        elif ev in ("start_filter", "write", "read"):
            cp, baud = chan[r["channel"]]
            if ev == "start_filter":
                protos = [r["mask"]["proto"]] if r.get("mask") else [None]
            else:
                protos = [m["proto"] for m in r.get("msgs") or []] or [None]
            for p in protos:
                tally[(baud, f"{cp:#06x}", ev, None if p is None else f"{p:#06x}",
                       r["result_name"])] += 1
    out(f"== {path}")
    out(f"{'baud':>7} {'channel':>8} {'event':<12} {'msg proto':>9}  result  (count)")
    for (baud, cp, ev, p, res), n in sorted(tally.items(), key=str):
        flag = "  <-- MISMATCH" if p not in (None, cp) else ""
        out(f"{baud:>7} {cp:>8} {ev:<12} {str(p):>9}  {res}  ({n}){flag}")
    out("services written (proto, service): count")
    unsafe = unclassified = 0
    for (proto, svc), n in sorted(sent.items(), key=str):
        if svc is None:
            unclassified += n
            note = "  <-- UNCLASSIFIED (no service byte at a known position)"
        elif svc not in READ:
            unsafe += n
            note = "  <-- NOT A READ SERVICE: STOP AND INVESTIGATE"
        else:
            note = ""
        out(f"  {proto} {svc if svc is not None else '--'}: {n}{note}")
    incomplete = []
    if not chan:
        incomplete.append("no successful connect in the log")
    if orphans:
        incomplete.append("records on channels with no logged connect: "
                          + ", ".join(f"{n} {ev}" for ev, n in sorted(orphans.items())))
    if bad_lines:
        incomplete.append(f"{bad_lines} unreadable line(s)")
    if unclassified:
        incomplete.append(f"{unclassified} write(s) could not be classified")
    for reason in incomplete:
        out(f"INCOMPLETE: {reason}")
    if unsafe:
        out("VERDICT: UNSAFE - a non-read service was written")
        return 1
    if incomplete:
        out("VERDICT: INCONCLUSIVE - no unsafe service seen, but the audit is incomplete")
        return 3
    out("VERDICT: only read services written")
    return 0


def main(argv=None) -> int:
    paths = (argv if argv is not None else sys.argv[1:])
    if not paths:
        print(__doc__)
        return 2
    codes = [check(p) for p in paths]
    return 1 if 1 in codes else (3 if 3 in codes else 0)


if __name__ == "__main__":
    sys.exit(main())
