"""Reading the JSONL produced by the J2534 proxy.

The proxy deliberately stores raw calls, so everything protocol-shaped —
splitting the CAN id off the payload, pairing requests with responses — is
done here, where it can be fixed without rebuilding a DLL.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Sequence

# Protocol ids that put a 4-byte big-endian CAN id in front of the payload.
CAN_PROTOCOLS = {5, 6}  # CAN, ISO15765

NEGATIVE_RESPONSE = 0x7F


@dataclass
class Frame:
    """One PASSTHRU_MSG, with the CAN id split off the payload."""

    index: int  # record number ("n") in the log
    direction: str  # "tx" or "rx"
    channel: int
    protocol: int
    can_id: int | None
    payload: bytes
    rx_status: int
    tx_flags: int
    device_us: int  # adapter timestamp
    host_us: int  # proxy timestamp, microseconds since the log was opened
    wall_ms: int
    result: int

    @property
    def is_tx_confirmation(self) -> bool:
        """ISO15765 loopback/TxDone indication rather than an ECU answer."""
        return bool(self.rx_status & 0x00000008) or bool(self.rx_status & 0x00000001)

    @property
    def service(self) -> int | None:
        return self.payload[0] if self.payload else None

    @property
    def is_negative(self) -> bool:
        return len(self.payload) >= 3 and self.payload[0] == NEGATIVE_RESPONSE

    def hex(self) -> str:
        return self.payload.hex().upper()

    def __str__(self) -> str:
        cid = f"{self.can_id:03X}" if self.can_id is not None else "----"
        return f"{self.direction.upper()} {cid} {self.hex()}"


@dataclass
class Log:
    path: Path
    session: dict = field(default_factory=dict)
    events: list[dict] = field(default_factory=list)

    @property
    def frames(self) -> list[Frame]:
        return list(iter_frames(self.events))

    def by_event(self, *names: str) -> list[dict]:
        wanted = set(names)
        return [e for e in self.events if e.get("ev") in wanted]

    @property
    def duration_s(self) -> float:
        stamps = [e["mono"] for e in self.events if "mono" in e]
        return (max(stamps) - min(stamps)) / 1e6 if len(stamps) > 1 else 0.0


def load(path: str | Path) -> Log:
    """Reads a proxy log. Truncated last lines (VIDA crashed) are skipped."""
    path = Path(path)
    log = Log(path=path)
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("ev") == "session":
                log.session = record
            log.events.append(record)
    return log


def load_many(paths: Iterable[str | Path]) -> list[Log]:
    return [load(p) for p in paths]


def _split(protocol: int, data: bytes) -> tuple[int | None, bytes]:
    if protocol in CAN_PROTOCOLS and len(data) >= 4:
        return int.from_bytes(data[:4], "big"), data[4:]
    return None, data


def iter_frames(events: Sequence[dict]) -> Iterator[Frame]:
    """Yields every PASSTHRU_MSG that crossed the API, in log order."""
    for event in events:
        name = event.get("ev")
        if name == "write":
            direction = "tx"
        elif name == "read":
            direction = "rx"
        else:
            continue
        for message in event.get("msgs") or []:
            protocol = message.get("proto", 0)
            data = bytes.fromhex(message.get("data", ""))
            can_id, payload = _split(protocol, data)
            yield Frame(
                index=event.get("n", 0),
                direction=direction,
                channel=event.get("channel", 0),
                protocol=protocol,
                can_id=can_id,
                payload=payload,
                rx_status=message.get("rx_status", 0),
                tx_flags=message.get("tx_flags", 0),
                device_us=message.get("ts", 0),
                host_us=event.get("mono", 0),
                wall_ms=event.get("t", 0),
                result=event.get("result", 0),
            )


def request_key(payload: bytes) -> str:
    """A stable name for a request, at the granularity that matters.

    ``22 D1 23`` keeps its identifier, ``01 0C`` keeps its PID, and anything
    with a payload the ECU treats as data (``2E``, ``31``) keeps only the part
    that selects *what* is being asked, not the argument.
    """
    if not payload:
        return ""
    sid = payload[0]
    if sid in (0x22, 0x2E, 0x2C) and len(payload) >= 3:  # DID based
        return f"{sid:02X}{payload[1]:02X}{payload[2]:02X}"
    if sid in (0x01, 0x02, 0x09, 0x19, 0x10, 0x11, 0x27, 0x28, 0x3E, 0x85) and len(payload) >= 2:
        return f"{sid:02X}{payload[1]:02X}"
    if sid == 0x31 and len(payload) >= 4:  # RoutineControl: sub-function + id
        return f"31{payload[1]:02X}{payload[2]:02X}{payload[3]:02X}"
    if sid == 0x21 and len(payload) >= 2:  # KWP readDataByLocalId
        return f"21{payload[1]:02X}"
    return f"{sid:02X}"


def response_key(payload: bytes) -> str | None:
    """The request key a positive response answers, or None."""
    if not payload:
        return None
    sid = payload[0]
    if sid == NEGATIVE_RESPONSE:
        return request_key(payload[1:2]) if len(payload) >= 2 else None
    if sid < 0x40:
        return None
    return request_key(bytes([sid - 0x40]) + payload[1:])


@dataclass
class Exchange:
    """A request and the answer that came back on the same channel."""

    key: str
    request: Frame
    response: Frame | None = None
    latency_us: int | None = None

    @property
    def ok(self) -> bool:
        return self.response is not None and not self.response.is_negative

    @property
    def nrc(self) -> int | None:
        if self.response is not None and self.response.is_negative:
            return self.response.payload[2]
        return None

    @property
    def data(self) -> bytes:
        """Response payload with the echoed service/identifier removed."""
        if not self.ok or self.response is None:
            return b""
        payload = self.response.payload
        sid = payload[0] - 0x40
        if sid in (0x22, 0x2C, 0x2E):
            return payload[3:]
        if sid in (0x01, 0x09, 0x19, 0x21):
            return payload[2:]
        return payload[1:]


def pair(frames: Iterable[Frame], *, window_us: int = 5_000_000) -> list[Exchange]:
    """Matches responses to requests per channel, oldest request first.

    Loopback echoes are ignored and a request stays open until an answer
    arrives or ``window_us`` elapses. A negative response only echoes the
    service, not the identifier, so it is matched on the service alone —
    otherwise every rejected ``22 D1 23`` would look unanswered.
    """
    pending: dict[int, list[Exchange]] = {}
    exchanges: list[Exchange] = []

    for frame in frames:
        if frame.direction == "tx":
            exchange = Exchange(key=request_key(frame.payload), request=frame)
            exchanges.append(exchange)
            pending.setdefault(frame.channel, []).append(exchange)
            continue

        if frame.is_tx_confirmation:
            continue  # our own frame looped back by the adapter
        payload = frame.payload
        if not payload:
            continue

        queue = pending.get(frame.channel)
        if not queue:
            continue
        queue[:] = [e for e in queue if frame.host_us - e.request.host_us <= window_us]

        if payload[0] == NEGATIVE_RESPONSE:
            if len(payload) < 3:
                continue
            if payload[2] == 0x78:
                continue  # responsePending: the real answer is still coming
            match = next(
                (e for e in queue if e.request.payload and e.request.payload[0] == payload[1]),
                None,
            )
        else:
            key = response_key(payload)
            if key is None:
                continue
            match = next((e for e in queue if e.key == key), None)

        if match is not None:
            match.response = frame
            match.latency_us = frame.host_us - match.request.host_us
            queue.remove(match)

    return exchanges
