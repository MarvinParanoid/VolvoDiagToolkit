"""ISO 15765-2 in software.

A J2534 device does this itself, so this module is for the raw-CAN transports
(SocketCAN without the kernel isotp module, or a CAN dongle). Keeping it here
also makes the framing testable without any hardware.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

SINGLE_FRAME = 0x0
FIRST_FRAME = 0x1
CONSECUTIVE_FRAME = 0x2
FLOW_CONTROL = 0x3

FC_CONTINUE = 0x0
FC_WAIT = 0x1
FC_OVERFLOW = 0x2


class IsoTpError(Exception):
    pass


@dataclass
class IsoTpConfig:
    tx_id: int
    rx_id: int
    padding: int | None = 0x00  # None = do not pad short frames
    block_size: int = 0  # 0 = the sender may send everything at once
    st_min: int = 0  # milliseconds between consecutive frames
    tx_dl: int = 8  # classic CAN


def _pad(data: bytes, config: IsoTpConfig) -> bytes:
    if config.padding is None or len(data) >= config.tx_dl:
        return data
    return data + bytes([config.padding]) * (config.tx_dl - len(data))


def encode(payload: bytes, config: IsoTpConfig) -> list[bytes]:
    """Splits a payload into CAN frame data fields.

    Only the frames the sender emits are produced; the flow control frame the
    receiver sends back is handled by the caller.
    """
    if not payload:
        raise IsoTpError("empty payload")

    max_single = config.tx_dl - 1
    if len(payload) <= max_single:
        return [_pad(bytes([len(payload)]) + payload, config)]

    if len(payload) > 0xFFF:
        raise IsoTpError(f"payload of {len(payload)} bytes needs the extended length format")

    frames = [bytes([0x10 | (len(payload) >> 8), len(payload) & 0xFF]) + payload[: config.tx_dl - 2]]
    rest = payload[config.tx_dl - 2 :]
    index = 1
    while rest:
        chunk = rest[: config.tx_dl - 1]
        rest = rest[config.tx_dl - 1 :]
        frames.append(_pad(bytes([0x20 | (index & 0x0F)]) + chunk, config))
        index += 1
    return frames


def flow_control_frame(config: IsoTpConfig, status: int = FC_CONTINUE) -> bytes:
    return _pad(bytes([0x30 | status, config.block_size, config.st_min]), config)


class Reassembler:
    """Feeds in CAN frame payloads, yields complete ISO-TP messages.

    ``pending_flow_control`` is set after a first frame; the caller must send
    it before the sender will continue.
    """

    def __init__(self, config: IsoTpConfig) -> None:
        self.config = config
        self._buffer = bytearray()
        self._expected = 0
        self._next_index = 1
        self.pending_flow_control: bytes | None = None

    def reset(self) -> None:
        self._buffer.clear()
        self._expected = 0
        self._next_index = 1
        self.pending_flow_control = None

    def feed(self, data: bytes) -> Iterator[bytes]:
        if not data:
            return
        kind = data[0] >> 4

        if kind == SINGLE_FRAME:
            length = data[0] & 0x0F
            if length == 0 or length + 1 > len(data):
                raise IsoTpError(f"malformed single frame: {data.hex()}")
            self.reset()
            yield bytes(data[1 : 1 + length])

        elif kind == FIRST_FRAME:
            if len(data) < 3:
                raise IsoTpError(f"truncated first frame: {data.hex()}")
            self.reset()
            self._expected = ((data[0] & 0x0F) << 8) | data[1]
            self._buffer.extend(data[2:])
            self._next_index = 1
            self.pending_flow_control = flow_control_frame(self.config)

        elif kind == CONSECUTIVE_FRAME:
            if self._expected == 0:
                return  # a fragment of a message we never saw the start of
            index = data[0] & 0x0F
            if index != (self._next_index & 0x0F):
                raise IsoTpError(
                    f"consecutive frame out of order: got {index}, want {self._next_index & 0x0F}"
                )
            self._next_index += 1
            self._buffer.extend(data[1:])
            if len(self._buffer) >= self._expected:
                message = bytes(self._buffer[: self._expected])
                self.reset()
                yield message

        elif kind == FLOW_CONTROL:
            return  # handled by the sender side


def is_flow_control(data: bytes) -> bool:
    return bool(data) and (data[0] >> 4) == FLOW_CONTROL


def parse_flow_control(data: bytes) -> tuple[int, int, int]:
    """(status, block_size, st_min_ms)"""
    if not is_flow_control(data) or len(data) < 3:
        raise IsoTpError(f"not a flow control frame: {data.hex()}")
    st_min = data[2]
    if 0xF1 <= st_min <= 0xF9:  # 100..900 microseconds, rounded up to 1 ms
        millis = 1
    elif st_min > 0x7F:
        millis = 0
    else:
        millis = st_min
    return data[0] & 0x0F, data[1], millis
