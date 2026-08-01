"""Volvo's proprietary diagnostic protocol on P1 (the "A6" read service).

This is what VIDA actually uses to read the D4164T engine. The legislated OBD
addressing (0x7E0/0x7DF, ISO15765) gets no answer from this ECM; the engine is
reached over raw 29-bit CAN with Volvo's own framing, captured and decoded from
a real VIDA session (see docs/volvo-protocol.md).

Frame layout, one request one response:

    request  (CAN id 0x0FFFFE, 29-bit, 500 kbaud)
        C8+n  group  A6  id_hi  id_lo  01   00 ...        padded to 8 bytes
        └ len └ bank └ read service  └ 16-bit identifier

    response (CAN id 0x40xxxx, 29-bit)
        C8+n  group  E6  id_hi  id_lo  <data...>          E6 = A6 + 0x40
                     └ positive response

`group` (0x11 for live engine data, 0x50 for identity blocks) selects a bank of
identifiers; it is echoed in the response. Values are big-endian, usually two
bytes.

Only the single-frame form is implemented here — every live parameter fits in
one frame. The multi-frame transport VIDA uses for the identity strings is a
separate, more involved framing and is left for later.
"""

from __future__ import annotations

from dataclasses import dataclass

# The broadcast identifier every request is sent to.
REQUEST_CAN_ID = 0x0FFFFE

# Services. The positive response is the request service plus 0x40, exactly as
# in KWP2000/UDS.
SERVICE_READ = 0xA6
POSITIVE_OFFSET = 0x40
POSITIVE_READ = SERVICE_READ + POSITIVE_OFFSET  # 0xE6

# Banks seen so far.
GROUP_LIVE_DATA = 0x11
GROUP_IDENTITY = 0x50

# Single-frame length marker: byte0 = LENGTH_BASE + payload_length.
LENGTH_BASE = 0xC8
MAX_SINGLE_FRAME_PAYLOAD = 7  # a classic CAN frame is 8 bytes, minus the marker


class VolvoProtocolError(Exception):
    pass


class NegativeResponse(VolvoProtocolError):
    """The ECM answered, but not with a positive read (e.g. a 0x7F reject)."""

    def __init__(self, service: int, detail: str = "") -> None:
        self.service = service
        super().__init__(detail or f"non-positive response, service 0x{service:02X}")


def frame(payload: bytes) -> bytes:
    """Wraps a payload in the single-frame marker and pads to 8 CAN bytes."""
    if not 1 <= len(payload) <= MAX_SINGLE_FRAME_PAYLOAD:
        raise VolvoProtocolError(f"payload of {len(payload)} bytes does not fit one frame")
    body = bytes([LENGTH_BASE + len(payload)]) + payload
    return body + bytes(8 - len(body))


def is_single_frame(data: bytes) -> bool:
    return bool(data) and LENGTH_BASE <= data[0] <= LENGTH_BASE + MAX_SINGLE_FRAME_PAYLOAD


def payload_of(data: bytes) -> bytes:
    """The meaningful bytes of a single frame, with marker and padding removed."""
    if not is_single_frame(data):
        raise VolvoProtocolError(f"not a Volvo single frame: {data[:1].hex()}")
    length = data[0] - LENGTH_BASE
    if 1 + length > len(data):
        raise VolvoProtocolError(f"frame claims {length} bytes, only {len(data) - 1} present")
    return data[1 : 1 + length]


def build_read(identifier: int, group: int = GROUP_LIVE_DATA) -> bytes:
    """The 8-byte CAN payload that reads one identifier."""
    if not 0 <= identifier <= 0xFFFF:
        raise VolvoProtocolError(f"identifier 0x{identifier:X} is not 16-bit")
    payload = bytes([group, SERVICE_READ, (identifier >> 8) & 0xFF, identifier & 0xFF, 0x01])
    return frame(payload)


@dataclass(frozen=True)
class Response:
    group: int
    service: int
    identifier: int
    data: bytes  # the value bytes after the echoed identifier

    @property
    def positive(self) -> bool:
        return self.service == POSITIVE_READ

    def u16(self) -> int:
        if len(self.data) < 2:
            raise VolvoProtocolError(f"expected 2 data bytes, got {self.data.hex()}")
        return int.from_bytes(self.data[:2], "big")


def parse_response(data: bytes) -> Response:
    """Decodes a response CAN frame into its parts.

    Raises NegativeResponse if the service is not the positive read, and
    VolvoProtocolError on a malformed or multi-frame frame.
    """
    payload = payload_of(data)
    if len(payload) < 4:
        raise VolvoProtocolError(f"response payload too short: {payload.hex()}")
    group = payload[0]
    service = payload[1]
    identifier = (payload[2] << 8) | payload[3]
    body = payload[4:]
    response = Response(group=group, service=service, identifier=identifier, data=body)
    if not response.positive:
        raise NegativeResponse(service, f"service 0x{service:02X} to id 0x{identifier:04X}")
    return response


def matches(data: bytes, identifier: int, group: int = GROUP_LIVE_DATA) -> bool:
    """True if a received frame is the positive response to (group, identifier).

    The transport reads every frame on the bus and uses this to pick out the
    answer to the request it just sent.
    """
    if not is_single_frame(data):
        return False
    payload = payload_of(data)
    return (
        len(payload) >= 4
        and payload[0] == group
        and payload[1] == POSITIVE_READ
        and ((payload[2] << 8) | payload[3]) == identifier
    )
