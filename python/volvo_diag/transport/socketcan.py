"""SocketCAN transport (Linux).

Two modes:

* the kernel ISO-TP socket (``modprobe can_isotp``) — segmentation, flow
  control and timing are handled in the kernel, which is what you want;
* raw CAN plus :mod:`volvo_diag.protocol.isotp` when that module is missing.

Nothing here needs python-can; the kernel interface is used directly.
"""

from __future__ import annotations

import logging
import socket
import struct
import time

from ..protocol import isotp
from .base import EcuAddress, Transport, TransportError, TransportTimeout

log = logging.getLogger(__name__)

CAN_RAW = 1
CAN_ISOTP = 6
SOL_CAN_BASE = 100
SOL_CAN_ISOTP = SOL_CAN_BASE + CAN_ISOTP
CAN_ISOTP_OPTS = 1
CAN_EFF_FLAG = 0x80000000

RAW_FRAME = struct.Struct("=IB3x8s")


def _is_pending(response: bytes) -> bool:
    """7F <sid> 78 — requestCorrectlyReceived-ResponsePending."""
    return len(response) >= 3 and response[0] == 0x7F and response[2] == 0x78


class SocketCanTransport(Transport):
    def __init__(
        self,
        channel: str = "can0",
        *,
        prefer_kernel_isotp: bool = True,
        padding: int | None = 0x00,
    ) -> None:
        self.channel = channel
        self.prefer_kernel_isotp = prefer_kernel_isotp
        self.padding = padding
        self._raw: socket.socket | None = None
        self._isotp: dict[tuple[int, int], socket.socket] = {}
        self._opened = False

    # ---- lifecycle -----------------------------------------------------

    def open(self) -> None:
        if self._opened:
            return
        if not hasattr(socket, "AF_CAN"):
            raise TransportError("this Python has no SocketCAN support")
        self._opened = True

    def close(self) -> None:
        for sock in self._isotp.values():
            sock.close()
        self._isotp.clear()
        if self._raw is not None:
            self._raw.close()
            self._raw = None
        self._opened = False

    def describe(self) -> str:
        mode = "kernel isotp" if self.prefer_kernel_isotp else "raw can"
        return f"SocketCAN {self.channel} ({mode})"

    # ---- kernel ISO-TP -------------------------------------------------

    def _isotp_socket(self, address: EcuAddress) -> socket.socket | None:
        key = (address.tx_id, address.rx_id)
        existing = self._isotp.get(key)
        if existing is not None:
            return existing
        if not self.prefer_kernel_isotp:
            return None
        try:
            sock = socket.socket(socket.AF_CAN, socket.SOCK_DGRAM, CAN_ISOTP)
        except OSError as exc:
            log.info("kernel isotp unavailable (%s); falling back to raw CAN", exc)
            self.prefer_kernel_isotp = False
            return None
        try:
            tx = address.tx_id | (CAN_EFF_FLAG if address.extended else 0)
            rx = address.rx_id | (CAN_EFF_FLAG if address.extended else 0)
            sock.bind((self.channel, rx, tx))
        except OSError as exc:
            sock.close()
            raise TransportError(f"cannot bind isotp socket on {self.channel}: {exc}") from exc
        self._isotp[key] = sock
        return sock

    # ---- raw CAN -------------------------------------------------------

    def _raw_socket(self) -> socket.socket:
        if self._raw is None:
            sock = socket.socket(socket.AF_CAN, socket.SOCK_RAW, CAN_RAW)
            try:
                sock.bind((self.channel,))
            except OSError as exc:
                sock.close()
                raise TransportError(f"cannot bind {self.channel}: {exc}") from exc
            self._raw = sock
        return self._raw

    def _raw_request(self, address: EcuAddress, payload: bytes, timeout: float) -> bytes:
        sock = self._raw_socket()
        config = isotp.IsoTpConfig(
            tx_id=address.tx_id, rx_id=address.rx_id, padding=self.padding
        )
        frames = isotp.encode(payload, config)
        tx_id = address.tx_id | (CAN_EFF_FLAG if address.extended else 0)
        rx_id = address.rx_id | (CAN_EFF_FLAG if address.extended else 0)

        sock.send(RAW_FRAME.pack(tx_id, len(frames[0]), frames[0]))
        awaiting_flow_control = len(frames) > 1

        reassembler = isotp.Reassembler(config)
        deadline = time.monotonic() + timeout
        remaining_frames = frames[1:]
        pending_seen = 0

        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TransportTimeout(
                    f"no answer from {address} to {payload.hex().upper()} within {timeout:.1f}s"
                )
            sock.settimeout(left)
            try:
                data = sock.recv(RAW_FRAME.size)
            except socket.timeout:
                continue
            can_id, length, body = RAW_FRAME.unpack(data)
            body = body[:length]
            if can_id != rx_id:
                continue

            if awaiting_flow_control and isotp.is_flow_control(body):
                _, block_size, st_min = isotp.parse_flow_control(body)
                for index, frame in enumerate(remaining_frames):
                    sock.send(RAW_FRAME.pack(tx_id, len(frame), frame))
                    if st_min:
                        time.sleep(st_min / 1000)
                    if block_size and (index + 1) % block_size == 0:
                        break  # a further flow control frame is due
                awaiting_flow_control = False
                continue

            for message in reassembler.feed(body):
                if _is_pending(message):
                    # The ECU asked for more time; keep listening, do not resend.
                    pending_seen += 1
                    if pending_seen > 20:
                        raise TransportTimeout(f"{address} kept answering responsePending")
                    deadline = time.monotonic() + max(timeout, 0.5)
                    continue
                return message
            if reassembler.pending_flow_control:
                frame = reassembler.pending_flow_control
                sock.send(RAW_FRAME.pack(tx_id, len(frame), frame))
                reassembler.pending_flow_control = None

    def _isotp_request(
        self, sock: socket.socket, address: EcuAddress, payload: bytes, timeout: float
    ) -> bytes:
        sock.send(payload)
        deadline = time.monotonic() + timeout
        pending_seen = 0
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TransportTimeout(
                    f"no answer from {address} to {payload.hex().upper()} within {timeout:.1f}s"
                )
            sock.settimeout(left)
            try:
                response = sock.recv(4096)
            except socket.timeout as exc:
                raise TransportTimeout(f"no answer from {address}") from exc
            if _is_pending(response):
                pending_seen += 1
                if pending_seen > 20:
                    raise TransportTimeout(f"{address} kept answering responsePending")
                deadline = time.monotonic() + max(timeout, 0.5)
                continue
            return response

    # ---- interface -----------------------------------------------------

    def request(self, address: EcuAddress, payload: bytes, timeout: float = 1.0) -> bytes:
        if not self._opened:
            raise TransportError("transport is not open")
        sock = self._isotp_socket(address)
        if sock is not None:
            return self._isotp_request(sock, address, payload, timeout)
        return self._raw_request(address, payload, timeout)
