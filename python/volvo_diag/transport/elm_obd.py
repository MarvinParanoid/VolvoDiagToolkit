"""ELM327 in standard-OBD mode — the transport behind `--transport elm` for
UDS / OBD-II ECUs (vLinker, OBDLink and generic ELM327 clones are all compatible
*devices*; the public transport name is just `elm`).

The adapter does ISO-TP itself when CAN auto-formatting is on, so this is the
simplest transport — and the least trustworthy: clones drop frames, mangle long
responses and ignore timing settings. Use it for a phone app, not for reverse
engineering. For the Volvo raw-A6 protocol the same `elm` device is driven in
raw 29-bit mode by `elm_can.py` (`ElmCanLink`) instead.

Requires pyserial. Bluetooth SPP shows up as a normal serial port
(/dev/rfcomm0, COMx).
"""

from __future__ import annotations

import logging
import time

from .base import EcuAddress, Transport, TransportError, TransportTimeout

log = logging.getLogger(__name__)

PROMPT = b">"


class ElmError(TransportError):
    pass


class ElmObdTransport(Transport):
    #: An ELM327 has one header at a time; switching ECUs costs two AT commands.
    single_target = True

    def __init__(
        self,
        port: str = "/dev/rfcomm0",
        *,
        baudrate: int = 115_200,
        # ISO 15765-4 CAN 29-bit / 500 kbaud — Volvo P1 answers standard OBD on
        # 29-bit (confirmed on the car: Car Scanner reports "29 bit ID, 500 kbaud";
        # forcing 11-bit "6" gets UNABLE TO CONNECT). Set "6" for an 11-bit car.
        protocol: str = "7",
        read_timeout: float = 2.0,
    ) -> None:
        self.port = port
        self.baudrate = baudrate
        self.protocol = protocol
        self.read_timeout = read_timeout
        self._serial = None
        self._address: EcuAddress | None = None

    # ---- lifecycle -----------------------------------------------------

    def open(self) -> None:
        if self._serial is not None:
            return
        # Shared opener: a device path (/dev/rfcomm0, COMx) uses pyserial, a
        # Bluetooth MAC ("AA:BB:..@ch") connects an RFCOMM socket — so standard
        # OBD works over a BT ELM without binding an rfcomm node first.
        from .elm_can import open_serial  # noqa: PLC0415
        try:
            self._serial = open_serial(self.port, self.baudrate, self.read_timeout)
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise TransportError("the ELM327 transport needs pyserial") from exc
        time.sleep(0.2)
        for command, expect in (
            ("ATZ", None),          # reset
            ("ATE0", "OK"),         # no echo
            ("ATL0", "OK"),         # no line feeds
            ("ATS0", "OK"),         # no spaces in the answers
            ("ATH0", "OK"),         # no headers: we filter by ATCRA instead
            ("ATCAF1", "OK"),       # let the adapter assemble ISO-TP
            (f"ATSP{self.protocol}", "OK"),
        ):
            reply = self._command(command)
            if expect and expect not in reply:
                log.warning("%s answered %r", command, reply)

        version = self._command("ATI")
        log.info("adapter: %s", version.strip())

    def close(self) -> None:
        if self._serial is not None:
            try:
                self._serial.close()
            finally:
                self._serial = None
        self._address = None

    def describe(self) -> str:
        return f"ELM327 {self.port}"

    # ---- plumbing ------------------------------------------------------

    def _command(self, text: str, timeout: float | None = None) -> str:
        if self._serial is None:
            raise TransportError("transport is not open")
        self._serial.reset_input_buffer()
        self._serial.write(text.encode("ascii") + b"\r")
        self._serial.flush()

        deadline = time.monotonic() + (timeout or self.read_timeout)
        buffer = bytearray()
        while time.monotonic() < deadline:
            chunk = self._serial.read(64)
            if chunk:
                buffer.extend(chunk)
                if PROMPT in buffer:
                    break
            elif buffer:
                break
        return buffer.replace(PROMPT, b"").decode("ascii", errors="replace").strip()

    def _select(self, address: EcuAddress) -> None:
        if self._address == address:
            return
        tx, rx, width = address.tx_id, address.rx_id, 3
        if self.protocol == "7":
            # 29-bit ISO 15765-4: map the 11-bit OBD pair (0x7E0+i / 0x7E8+i) to
            # its 29-bit physical form 0x18DA{EE}F1 / 0x18DAF1{EE} (ECM EE=0x10).
            idx = tx - 0x7E0 if 0x7E0 <= tx <= 0x7E7 else 0
            tx = 0x18DA00F1 | ((0x10 + idx) << 8)
            rx = 0x18DAF100 | (0x10 + idx)
            width = 8
        self._command(f"ATSH{tx:0{width}X}")
        self._command(f"ATCRA{rx:0{width}X}")
        self._command(f"ATFCSH{tx:0{width}X}")
        self._command("ATFCSD300000")
        self._command("ATFCSM1")
        self._address = address

    @staticmethod
    def _parse(reply: str) -> bytes:
        """ELM answers are hex text, sometimes prefixed with a line counter."""
        cleaned = []
        for line in reply.splitlines():
            line = line.strip().replace(" ", "").replace(">", "")  # drop the prompt char
            if not line or line in ("SEARCHING...", "OK"):
                continue
            upper = line.upper()
            for error in ("NODATA", "CANERROR", "BUSERROR", "UNABLETOCONNECT",
                          "BUFFERFULL", "STOPPED", "ERROR", "?"):
                if upper.startswith(error):
                    raise ElmError(f"adapter said {line!r}")
            if ":" in line:  # "0:0562F190..." multi-line form
                line = line.split(":", 1)[1]
            cleaned.append(line)
        # A multi-frame ISO-TP answer (CAF on, headers off) is prefixed with its
        # total byte count as a short hex line ("014" = 20 bytes) that is NOT
        # colon-indexed. Left in, its odd nibble count shifts every byte after it
        # and the payload decodes to garbage (mode 09 VIN "not a mode 09 response").
        # Drop it only when it matches the body length, so it can't false-strip.
        if len(cleaned) >= 2 and 1 <= len(cleaned[0]) <= 3:
            body = "".join(cleaned[1:])
            try:
                if int(cleaned[0], 16) == len(body) // 2:
                    cleaned = cleaned[1:]
            except ValueError:
                pass
        text = "".join(cleaned)
        if len(text) % 2:
            text = text[:-1]
        try:
            return bytes.fromhex(text)
        except ValueError as exc:
            raise ElmError(f"cannot read {reply!r} as hex") from exc

    # ---- interface -----------------------------------------------------

    def request(self, address: EcuAddress, payload: bytes, timeout: float = 1.0) -> bytes:
        if self._serial is None:
            raise TransportError("transport is not open")
        self._select(address)

        deadline = time.monotonic() + max(timeout, self.read_timeout)
        reply = self._command(payload.hex().upper(), timeout=timeout)
        while True:
            data = self._parse(reply)
            if not data:
                raise TransportTimeout(f"no answer from {address} to {payload.hex().upper()}")
            if len(data) >= 3 and data[0] == 0x7F and data[2] == 0x78:
                if time.monotonic() > deadline:
                    raise TransportTimeout(f"{address} kept answering responsePending")
                reply = self._command("", timeout=timeout)  # a bare CR repeats the read
                continue
            return data
