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

Every live parameter fits in one single frame. The identity/configuration
block (VIN, part numbers, programmed values) is read with the 0xB9 service and
comes back multi-frame; that framing is handled at the bottom of this module.
"""

from __future__ import annotations

from dataclasses import dataclass

# The broadcast identifier every request is sent to.
REQUEST_CAN_ID = 0x0FFFFE

# Services. The positive response is the request service plus 0x40, exactly as
# in KWP2000/UDS.
SERVICE_READ = 0xA6
# Identity / configuration read (part numbers, VIN, programmed values). The
# answer is multi-frame ASCII.
SERVICE_IDENTITY = 0xB9
POSITIVE_OFFSET = 0x40
POSITIVE_READ = SERVICE_READ + POSITIVE_OFFSET  # 0xE6
POSITIVE_IDENTITY = SERVICE_IDENTITY + POSITIVE_OFFSET  # 0xF9

# The identity sub-identifier VIDA reads for the full block.
IDENTITY_ALL = 0xFB

# Banks seen so far. These are comm addresses: 0x11 = ECM, 0x50 = CEM.
GROUP_LIVE_DATA = 0x11
GROUP_IDENTITY = 0x50

# Single-frame length marker: byte0 = LENGTH_BASE + payload_length.
LENGTH_BASE = 0xC8
MAX_SINGLE_FRAME_PAYLOAD = 7  # a classic CAN frame is 8 bytes, minus the marker

# Multi-frame markers (the identity block). A first frame's high nibble is 0x9;
# consecutive frames' high nibble is 0x1, low nibble a 0..7 sequence counter.
FIRST_FRAME_NIBBLE = 0x9
CONSECUTIVE_FRAME_NIBBLE = 0x1


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


# ---------------------------------------------------------------------------
# Identity / configuration block (multi-frame).
#
# VIDA reads a module's identity with the 0xB9 service; the answer is a
# multi-frame stream of CRLF-separated ASCII fields - VIN, part numbers,
# software levels, emission class. Captured and decoded from a real session
# (see docs/volvo-protocol.md).
# ---------------------------------------------------------------------------


def build_identity(group: int = GROUP_IDENTITY, identifier: int = IDENTITY_ALL) -> bytes:
    """The 8-byte CAN payload that reads a module's identity block."""
    return frame(bytes([group, SERVICE_IDENTITY, identifier & 0xFF]))


def is_first_frame(data: bytes) -> bool:
    return bool(data) and (data[0] >> 4) == FIRST_FRAME_NIBBLE


def is_consecutive_frame(data: bytes) -> bool:
    return bool(data) and (data[0] >> 4) == CONSECUTIVE_FRAME_NIBBLE


def reassemble_identity(frames: list) -> bytes:
    """Joins the payload of a multi-frame identity response.

    `frames` is the ordered CAN payloads received. The first frame (0x9x) is a
    header; the data lives in the consecutive frames (0x1x), each carrying seven
    bytes after its one-byte sequence marker.
    """
    data = bytearray()
    for f in frames:
        if is_consecutive_frame(f):
            data.extend(f[1:])
    return bytes(data)


def is_response_canid(can_id: int) -> bool:
    """Diagnostic block responses come back on 0x40xxxx (and 0x60xxxx for the
    gateway); everything else on the bus is normal traffic."""
    return (can_id >> 16) in (0x40, 0x60)


def reassemble_block(frames: list, group: int, identifier: int,
                     service: int = POSITIVE_IDENTITY) -> bytes:
    """Joins a multi-frame block response into raw bytes, first-frame data
    included so byte offsets are absolute.

    `frames` is the ordered list of (can_id, data) received after the request.
    The first frame is the one whose payload echoes [_, group, service, id]; the
    block is that frame's data plus every following frame on the *same* CAN id,
    each after its one-byte sequence marker. Keying on the CAN id rather than the
    frame-control nibble makes this robust to the two different multi-frame
    numberings the modules use — the identity block (first 0x9x, consecutive
    0x1x) and the low-speed configuration block (first 0x8x, consecutive 0x0x).
    Verified against a captured 0xFB response (byte 0 is the length marker, the
    VIN lands at its catalogued offset) and a captured 0xFC response (the car
    configuration options decode at their offsets).
    """
    block_id = None
    data = bytearray()
    id_lo = identifier & 0xFF
    for can_id, frame in frames:
        if block_id is None:
            if (len(frame) >= 4 and frame[1] == group and frame[2] == service
                    and frame[3] == id_lo):
                block_id = can_id
                data.extend(frame[4:])  # after [control, commAddr, service, id]
        elif can_id == block_id:
            data.extend(frame[1:])      # after the one-byte sequence marker
    return bytes(data)


def identity_fields(payload: bytes) -> list:
    """The reassembled identity split into its CRLF-separated ASCII fields,
    with the leading record marker and any all-zero padding fields dropped."""
    text = payload.decode("latin-1")
    fields = []
    for raw in text.replace("\r", "\n").split("\n"):
        field = raw.strip("\x00 ")
        # keep only printable, non-empty, non-padding fields
        if field and set(field) != {"0"} and all(32 <= ord(c) < 127 for c in field):
            fields.append(field)
    # the first surviving field is a record marker like "x001"; drop it if so
    if fields and len(fields[0]) <= 4:
        fields = fields[1:]
    return fields
