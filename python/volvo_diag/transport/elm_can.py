"""Raw Volvo A6 over an ELM327 (serial, or Bluetooth bound to a serial port).

Implements the CanLink interface the Volvo protocol uses by driving an ELM327 in
raw 29-bit CAN mode (`ATSP7` + `ATCAF0`): each send() sets the request header and
writes the eight data bytes; receive() reads the frames the adapter prints (with
headers on) and hands back (can_id, payload) for volvo.py to reassemble.

Slow and clone-dependent — run `volvo-monitor probe` on the adapter first. Only
the 500k powertrain bus the OBD connector exposes is reachable this way (no
vendor bus switching like the VXDIAG J2534 path).
"""

from __future__ import annotations

import logging
import time

from .base import TransportError
from .volvo_ecm import CanLink

log = logging.getLogger(__name__)

# After ATZ: echo/linefeeds off, spaces on (easy parsing), 29-bit CAN 500k,
# headers on, raw mode, request header priority byte 0, and a receive filter
# that accepts the 0x40xxxx diagnostic responses (mask second byte == 0x40).
_SETUP = ("ATE0", "ATL0", "ATS1", "ATSP7", "ATH1", "ATCAF0",
          "ATCP00", "ATCM00FF0000", "ATCF00400000")
_NON_DATA = ("NODATA", "CANERROR", "BUSERROR", "BUFFERFULL", "STOPPED",
             "UNABLETOCONNECT", "SEARCHING", "ERR", "?")


class ElmCanLink(CanLink):
    def __init__(self, port: str = "/dev/rfcomm0", *, baud: int = 38400,
                 extended: bool = True, serial=None) -> None:
        self.port = port
        self.baud = baud
        self.extended = extended
        self._ser = serial          # inject a fake for tests; else pyserial opens it
        self._own = serial is None
        self._header = None
        self._opened = False

    def describe(self) -> str:
        return f"ELM327 raw CAN {self.port}"

    def open(self) -> None:
        if self._opened:
            return
        if self._ser is None:
            try:
                import serial as pyserial  # noqa: PLC0415 — optional dependency
            except ImportError as exc:
                raise TransportError("pyserial required (pip install pyserial)") from exc
            self._ser = pyserial.Serial(self.port, self.baud, timeout=0.2)
        version = self._cmd("ATZ", timeout=3.0)
        if "ELM" not in version.upper():
            raise TransportError(f"no ELM327 on {self.port} (got {version.strip()!r})")
        for cmd in _SETUP:
            self._cmd(cmd)
        self._opened = True

    def _read_until_prompt(self, timeout: float) -> str:
        buf = bytearray()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            chunk = self._ser.read(64)
            if chunk:
                buf += chunk
                if b">" in buf:
                    break
        return bytes(buf).decode("latin-1")

    def _cmd(self, cmd: str, timeout: float = 2.0) -> str:
        self._ser.reset_input_buffer()
        self._ser.write((cmd + "\r").encode())
        return self._read_until_prompt(timeout)

    def send(self, can_id: int, data: bytes, extended: bool = True) -> None:
        if not self._opened:
            raise TransportError("link is not open")
        cp, hdr = (can_id >> 24) & 0xFF, f"{can_id & 0xFFFFFF:06X}"
        if self._header != (cp, hdr):          # header rarely changes (always 0x0FFFFE)
            self._cmd(f"ATCP{cp:02X}")
            self._cmd(f"ATSH{hdr}")
            self._header = (cp, hdr)
        self._ser.reset_input_buffer()
        self._ser.write((data.hex().upper() + "\r").encode())  # response read in receive()

    def receive(self, timeout: float):
        if not self._opened:
            raise TransportError("link is not open")
        text = self._read_until_prompt(timeout)
        for line in text.replace(">", "\n").splitlines():
            line = line.strip()
            up = line.upper().replace(" ", "")
            if not up or any(bad in up for bad in _NON_DATA):
                continue
            tokens = line.split() if " " in line else [up[i:i + 2] for i in range(0, len(up), 2)]
            try:
                raw = bytes(int(t, 16) for t in tokens)
            except ValueError:
                continue
            if len(raw) < 5:                    # 4-byte id + at least one data byte
                continue
            yield int.from_bytes(raw[:4], "big"), raw[4:]

    def close(self) -> None:
        if self._ser is not None and self._own:
            try:
                self._ser.close()
            except Exception:  # noqa: BLE001
                pass
        self._opened = False
