"""Reading the P1 engine over Volvo's proprietary A6 protocol.

The legislated OBD stack (ISO15765 request/response to 0x7E0) does not reach
this ECM, so this path does not use the Transport interface. It talks raw
29-bit CAN frames: one request broadcast to 0x0FFFFE, and whichever reply frame
echoes the identifier is the answer. That framing lives in
volvo_diag.protocol.volvo; here is the link abstraction and the read loop.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from typing import Iterator

from ..protocol import volvo
from ..volvo.parameters import Parameter
from .base import TransportError, TransportTimeout

log = logging.getLogger(__name__)


class CanLink(ABC):
    """A raw CAN interface: send a frame, receive frames.

    Deliberately narrower than Transport — no ISO-TP, no addressing pairs, just
    frames on a bus, which is all the Volvo protocol needs.
    """

    def __enter__(self) -> "CanLink":
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @abstractmethod
    def open(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def send(self, can_id: int, data: bytes, extended: bool = True) -> None: ...

    @abstractmethod
    def receive(self, timeout: float) -> Iterator[tuple[int, bytes]]:
        """Yields (can_id, data) frames seen within `timeout` seconds."""

    def describe(self) -> str:
        return self.__class__.__name__


class VolvoEcm:
    """One ECM addressed over the Volvo A6 read service."""

    def __init__(self, link: CanLink, group: int = volvo.GROUP_LIVE_DATA,
                 timeout: float = 1.0) -> None:
        self.link = link
        self.group = group
        self.timeout = timeout

    def read_identifier(self, identifier: int, group: int | None = None,
                        timeout: float | None = None) -> bytes:
        """Reads one identifier and returns its value bytes (after the frame
        header, group, service and echoed identifier are stripped)."""
        bank = self.group if group is None else group
        request = volvo.build_read(identifier, bank)
        self.link.send(volvo.REQUEST_CAN_ID, request)

        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TransportTimeout(
                    f"no answer to Volvo read {bank:02X}/{identifier:04X}"
                )
            for _can_id, data in self.link.receive(remaining):
                if volvo.matches(data, identifier, bank):
                    return volvo.parse_response(data).data

    def read(self, parameter: Parameter):
        """Reads a database parameter, returning the decoded physical value."""
        if not parameter.is_volvo or parameter.identifier is None:
            raise TransportError(f"{parameter.key} is not a Volvo-protocol parameter")
        value_bytes = self.read_identifier(
            parameter.identifier, parameter.group or self.group
        )
        return parameter.decode_value(value_bytes)

    def read_identity(self, group: int | None = None, timeout: float | None = None) -> list:
        """Reads a module's identity/configuration block and returns its ASCII
        fields (VIN, part numbers, software levels, ...).

        The answer is multi-frame, so this collects frames until the bus goes
        quiet rather than matching a single reply.
        """
        bank = self.group if group is None else group
        self.link.send(volvo.REQUEST_CAN_ID, volvo.build_identity(bank))

        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        frames: list = []
        # After the first frame arrives, a short gap means the stream is done.
        idle_deadline = deadline
        while time.monotonic() < (idle_deadline if frames else deadline):
            got = False
            for _can_id, data in self.link.receive(0.2):
                if volvo.is_first_frame(data) or volvo.is_consecutive_frame(data):
                    frames.append(data)
                    got = True
            if got:
                idle_deadline = time.monotonic() + 0.3
        if not frames:
            raise TransportTimeout(f"no identity answer from module {bank:02X}")
        return volvo.identity_fields(volvo.reassemble_identity(frames))


class ReplayLink(CanLink):
    """A CanLink backed by a table of captured responses.

    Feed it {identifier: response_frame_bytes} and it answers reads exactly as
    the car did. Used to test the read path against real captured data without
    a car, and handy for offline development.
    """

    def __init__(self, responses: dict[int, bytes], response_can_id: int = 0x400021,
                 identity_frames: list | None = None) -> None:
        self._responses = dict(responses)
        self._response_can_id = response_can_id
        self._identity_frames = list(identity_frames or [])
        self._queue: list[tuple[int, bytes]] = []
        self._opened = False

    def open(self) -> None:
        self._opened = True

    def close(self) -> None:
        self._opened = False
        self._queue.clear()

    def send(self, can_id: int, data: bytes, extended: bool = True) -> None:
        if not self._opened:
            raise TransportError("link is not open")
        if can_id != volvo.REQUEST_CAN_ID or not volvo.is_single_frame(data):
            return
        payload = volvo.payload_of(data)
        if len(payload) >= 3 and payload[1] == volvo.SERVICE_IDENTITY:
            for f in self._identity_frames:
                self._queue.append((self._response_can_id, f))
        elif len(payload) >= 4 and payload[1] == volvo.SERVICE_READ:
            identifier = (payload[2] << 8) | payload[3]
            reply = self._responses.get(identifier)
            if reply is not None:
                self._queue.append((self._response_can_id, reply))

    def receive(self, timeout: float) -> Iterator[tuple[int, bytes]]:
        while self._queue:
            yield self._queue.pop(0)
