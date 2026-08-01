"""What happened in a proxy log.

    python -m volvo_diag.logs.summarize session.jsonl
    python -m volvo_diag.logs.summarize session.jsonl --csv requests.csv
    python -m volvo_diag.logs.summarize session.jsonl --track 22D123
"""

from __future__ import annotations

import argparse
import csv
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .parser import Exchange, Frame, Log, load, pair

NRC_NAMES = {
    0x10: "generalReject",
    0x11: "serviceNotSupported",
    0x12: "subFunctionNotSupported",
    0x13: "incorrectMessageLength",
    0x22: "conditionsNotCorrect",
    0x24: "requestSequenceError",
    0x31: "requestOutOfRange",
    0x33: "securityAccessDenied",
    0x35: "invalidKey",
    0x78: "responsePending",
}


@dataclass
class RequestStats:
    key: str
    can_id: int | None
    channel: int
    count: int = 0
    answered: int = 0
    negative: int = 0
    nrcs: dict[int, int] = field(default_factory=dict)
    response_lengths: set[int] = field(default_factory=set)
    request_lengths: set[int] = field(default_factory=set)
    first_us: int = 0
    last_us: int = 0
    example_request: str = ""
    example_response: str = ""
    latencies: list[int] = field(default_factory=list)

    @property
    def rate_hz(self) -> float:
        span = (self.last_us - self.first_us) / 1e6
        return (self.count - 1) / span if span > 0 and self.count > 1 else 0.0

    @property
    def median_latency_ms(self) -> float:
        return statistics.median(self.latencies) / 1000 if self.latencies else 0.0


def collect(exchanges: list[Exchange]) -> dict[tuple[int, int | None, str], RequestStats]:
    stats: dict[tuple[int, int | None, str], RequestStats] = {}
    for exchange in exchanges:
        request = exchange.request
        ident = (request.channel, request.can_id, exchange.key)
        entry = stats.get(ident)
        if entry is None:
            entry = RequestStats(key=exchange.key, can_id=request.can_id, channel=request.channel)
            entry.first_us = request.host_us
            entry.example_request = request.hex()
            stats[ident] = entry

        entry.count += 1
        entry.last_us = request.host_us
        entry.request_lengths.add(len(request.payload))
        if exchange.latency_us is not None:
            entry.latencies.append(exchange.latency_us)
        if exchange.response is not None:
            if exchange.response.is_negative:
                entry.negative += 1
                nrc = exchange.nrc or 0
                entry.nrcs[nrc] = entry.nrcs.get(nrc, 0) + 1
            else:
                entry.answered += 1
                entry.response_lengths.add(len(exchange.response.payload))
                if not entry.example_response:
                    entry.example_response = exchange.response.hex()
    return stats


def describe_nrcs(entry: RequestStats) -> str:
    parts = []
    for nrc, count in sorted(entry.nrcs.items(), key=lambda kv: -kv[1]):
        parts.append(f"{NRC_NAMES.get(nrc, f'0x{nrc:02X}')}x{count}")
    return ",".join(parts)


def print_header(log: Log) -> None:
    session = log.session
    print(f"file        {log.path}")
    if session:
        print(f"proxy       {session.get('proxy_version')} ({session.get('bits')}-bit)")
        print(f"real dll    {session.get('real_dll')}")
        if session.get("tag"):
            print(f"tag         {session['tag']}")
    print(f"records     {len(log.events)}   duration {log.duration_s:.1f} s")


def print_topology(log: Log) -> None:
    connects = log.by_event("connect")
    if connects:
        print("\nchannels")
        for event in connects:
            print(
                f"  ch {event.get('channel')}  {event.get('protocol_name')} "
                f"({event.get('protocol')})  baud {event.get('baud')}  "
                f"flags {event.get('flags')}  -> {event.get('result_name')}"
            )

    filters = log.by_event("start_filter")
    if filters:
        print("\nfilters")
        for event in filters:
            flow = (event.get("flow_control") or {}).get("data", "")
            pattern = (event.get("pattern") or {}).get("data", "")
            mask = (event.get("mask") or {}).get("data", "")
            print(
                f"  ch {event.get('channel')}  {event.get('filter_name'):<12} "
                f"id {event.get('filter_id')}  mask {mask}  pattern {pattern}  fc {flow}"
            )

    ioctls = log.by_event("ioctl")
    if ioctls:
        counts: dict[str, int] = defaultdict(int)
        configs: list[str] = []
        for event in ioctls:
            counts[event.get("ioctl_name", "?")] += 1
            if event.get("ioctl_name") == "SET_CONFIG" and event.get("input"):
                for item in event["input"]:
                    line = f"    ch {event.get('channel')}  {item['name']} = {item['value']}"
                    if line not in configs:
                        configs.append(line)
        print("\nioctls")
        for name, count in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"  {name:<32} {count}")
        for line in configs:
            print(line)


def print_requests(stats: dict, limit: int) -> None:
    rows = sorted(stats.values(), key=lambda s: (-s.count, s.key))
    print(f"\nrequests ({len(rows)} distinct)")
    print(
        f"  {'ch':>3} {'can id':>7} {'request':<10} {'count':>6} {'Hz':>6} "
        f"{'ok':>5} {'neg':>4} {'ms':>6}  response"
    )
    for entry in rows[:limit]:
        cid = f"{entry.can_id:03X}" if entry.can_id is not None else "-"
        lengths = "/".join(str(n) for n in sorted(entry.response_lengths)) or "-"
        note = describe_nrcs(entry)
        example = entry.example_response[:40] or note
        print(
            f"  {entry.channel:>3} {cid:>7} {entry.key:<10} {entry.count:>6} "
            f"{entry.rate_hz:>6.1f} {entry.answered:>5} {entry.negative:>4} "
            f"{entry.median_latency_ms:>6.1f}  len {lengths:<8} {example}"
        )
    if len(rows) > limit:
        print(f"  ... {len(rows) - limit} more (use --limit)")


def print_unanswered(exchanges: list[Exchange]) -> None:
    missing: dict[str, int] = defaultdict(int)
    for exchange in exchanges:
        if exchange.response is None:
            missing[exchange.key] += 1
    if missing:
        print("\nno response seen")
        for key, count in sorted(missing.items(), key=lambda kv: -kv[1])[:20]:
            print(f"  {key:<10} {count}")


def candidate_decodings(data: bytes) -> list[str]:
    """Raw bytes read as the encodings a parameter definition can express."""
    out = []
    if len(data) >= 1:
        out.append(f"u8={data[0]}")
        out.append(f"i8={int.from_bytes(data[:1], 'big', signed=True)}")
    if len(data) >= 2:
        u16 = int.from_bytes(data[:2], "big")
        out.append(f"u16={u16}")
        out.append(f"u16*0.1={u16 * 0.1:.1f}")
        out.append(f"i16={int.from_bytes(data[:2], 'big', signed=True)}")
    if len(data) >= 4:
        out.append(f"u32={int.from_bytes(data[:4], 'big')}")
    return out


def track(exchanges: list[Exchange], key: str, out: Path | None) -> None:
    """Time series of one request's answers — the raw material for stage 5."""
    selected = [e for e in exchanges if e.key.upper() == key.upper() and e.ok]
    if not selected:
        print(f"no successful exchange with key {key}")
        return

    print(f"\n{key}: {len(selected)} answers")
    rows = []
    for exchange in selected:
        seconds = exchange.request.host_us / 1e6
        data = exchange.data
        rows.append((seconds, data.hex().upper(), *candidate_decodings(data)))

    for row in rows[:20]:
        print(f"  t={row[0]:8.3f}  {row[1]:<20} {'  '.join(row[2:])}")
    if len(rows) > 20:
        print(f"  ... {len(rows) - 20} more")

    changing = {row[1] for row in rows}
    print(f"  distinct values: {len(changing)}")

    if out:
        with out.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["t_s", "raw_hex", "u8", "i8", "u16", "u16x0.1", "i16", "u32"])
            for row in rows:
                values = {item.split("=")[0]: item.split("=")[1] for item in row[2:]}
                writer.writerow(
                    [
                        f"{row[0]:.3f}",
                        row[1],
                        values.get("u8", ""),
                        values.get("i8", ""),
                        values.get("u16", ""),
                        values.get("u16*0.1", ""),
                        values.get("i16", ""),
                        values.get("u32", ""),
                    ]
                )
        print(f"  wrote {out}")


def write_csv(stats: dict, path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["channel", "can_id", "request", "count", "rate_hz", "answered", "negative",
             "nrcs", "req_len", "resp_len", "example_request", "example_response"]
        )
        for entry in sorted(stats.values(), key=lambda s: (-s.count, s.key)):
            writer.writerow(
                [
                    entry.channel,
                    f"{entry.can_id:03X}" if entry.can_id is not None else "",
                    entry.key,
                    entry.count,
                    f"{entry.rate_hz:.2f}",
                    entry.answered,
                    entry.negative,
                    describe_nrcs(entry),
                    "/".join(str(n) for n in sorted(entry.request_lengths)),
                    "/".join(str(n) for n in sorted(entry.response_lengths)),
                    entry.example_request,
                    entry.example_response,
                ]
            )
    print(f"\nwrote {path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("log", type=Path)
    parser.add_argument("--limit", type=int, default=40, help="rows in the request table")
    parser.add_argument("--csv", type=Path, help="write the request table as CSV")
    parser.add_argument("--track", help="dump the answers of one request key over time")
    parser.add_argument("--track-csv", type=Path, help="write --track output as CSV")
    parser.add_argument("--frames", action="store_true", help="dump every frame")
    args = parser.parse_args(argv)

    if not args.log.exists():
        print(f"no such log: {args.log}", file=sys.stderr)
        return 2

    log = load(args.log)
    frames: list[Frame] = log.frames
    exchanges = pair(frames)
    stats = collect(exchanges)

    print_header(log)
    print_topology(log)
    print(f"\nframes      {len(frames)}  "
          f"tx {sum(1 for f in frames if f.direction == 'tx')}  "
          f"rx {sum(1 for f in frames if f.direction == 'rx')}")
    print_requests(stats, args.limit)
    print_unanswered(exchanges)

    if args.frames:
        print("\nframes")
        for frame in frames:
            print(f"  t={frame.host_us / 1e6:8.3f} ch{frame.channel} {frame}")
    if args.track:
        track(exchanges, args.track, args.track_csv)
    if args.csv:
        write_csv(stats, args.csv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
