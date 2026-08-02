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

# --- Known A6-family service bytes (KWP2000-style; positive response = +0x40) ---
# Consolidated from our own captures and corroborated by external P1 dumps
# (Alfaa123 "Codes.txt", Tigo2000 "ECU-Commands.txt"). Full family, for decoding:
#   A1 keep-alive           A5/A6/A7 read data (by offset/identifier/address)
#   A3 security access       A8/A9 start/stop transmission
#   AA define-dynamic        AB–AD freeze frame
#   AE read DTC              AF clear DTC
#   B0/B1 IO control         B2 control routine        B4 define R/W ECU data
#   B8/BA write data block   B9/BB read data block
# This toolkit is READ-ONLY: only the read services (A6, B9, AE) are driven. The
# write/control bytes below are named for reference and log decoding only.
SERVICE_SECURITY = 0xA3       # security access (send PIN); reference, not implemented
SERVICE_CLEAR_DTC = 0xAF      # clears stored DTCs — a write; reference, not implemented
SERVICE_WRITE = 0xB8          # write data block (config); reversed + documented, not driven

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


# Trouble codes are read with the 0xAE service, answered with 0xEE. The byte
# after the service is a sub-function: 0x1B lists the module's active codes (each
# a 2-byte Volvo code, 0x0000 terminates), 0x31 returns a code with its status
# byte. Reversed from a capture where the ECM reported 0x2A30 (clogged DPF).
SERVICE_DTC = 0xAE
POSITIVE_DTC = SERVICE_DTC + POSITIVE_OFFSET  # 0xEE
DTC_LIST = 0x1B
DTC_WITH_STATUS = 0x31


def build_dtc_read(group: int = GROUP_LIVE_DATA, sub: int = DTC_LIST,
                   arg: bytes = b"") -> bytes:
    """The CAN payload that reads trouble codes from a module (comm address
    `group`). `sub` selects the operation; `arg` carries a code for the
    per-code sub-functions (snapshot 0x70, extended 0x18)."""
    return frame(bytes([group, SERVICE_DTC, sub]) + arg)


def parse_dtc_list(block: bytes) -> list:
    """The active 2-byte codes from a reassembled 0xAE/0x1B answer, in order.
    The list is terminated by a 0x0000 entry; trailing padding is ignored."""
    codes = []
    for i in range(0, len(block) - 1, 2):
        code = (block[i] << 8) | block[i + 1]
        if code == 0x0000:
            break
        codes.append(code)
    return codes


# Clearing DTCs is a WRITE: service 0xAF, sub 0x11, answered with 0xEF. Reversed
# from VIDA's own clear sweep in the 22-write-dts capture (`CB 11 AF 11` ->
# `CB 11 EF 11` per module). Corroborated by external P1 dumps.
DTC_CLEAR_SUB = 0x11
POSITIVE_CLEAR_DTC = SERVICE_CLEAR_DTC + POSITIVE_OFFSET  # 0xEF


def build_dtc_clear(group: int = GROUP_LIVE_DATA) -> bytes:
    """The CAN payload that clears a module's stored trouble codes (`AF 11`)."""
    return frame(bytes([group, SERVICE_CLEAR_DTC, DTC_CLEAR_SUB]))


def is_clear_ack(payload: bytes, group: int) -> bool:
    """True if `payload` is the positive clear-DTC acknowledgement (`EF 11`) from
    the module at comm address `group`."""
    if not is_single_frame(payload):
        return False
    body = payload_of(payload)
    return (len(body) >= 3 and body[0] == group
            and body[1] == POSITIVE_CLEAR_DTC and body[2] == DTC_CLEAR_SUB)


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
