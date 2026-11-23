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
        try:
            time.sleep(0.2)
            reset = self._command("ATZ", timeout=max(self.read_timeout, 3.0))
            for command in (
                "ATE0",                 # no echo
                "ATL0",                 # no line feeds
                "ATS0",                 # no spaces in the answers
                "ATH0",                 # no headers: we filter by ATCRA instead
                "ATCAF1",               # let the adapter assemble ISO-TP
                f"ATSP{self.protocol}",
            ):
                self._expect_ok(command)
            version = self._command("ATI")
            if "ELM" not in (reset + version).upper():
                raise ElmError(f"no ELM327 on {self.port} (ATZ {reset!r}, ATI {version!r})")
            log.info("adapter: %s", version.strip())
        except BaseException:
            self.close()   # a failed open() must not leave the port open
            raise

    def _expect_ok(self, command: str) -> None:
        """Runs an AT command and raises unless the adapter answered OK."""
        reply = self._command(command)
        words = reply.upper().split()
        if "OK" not in words or "?" in words:
            raise ElmError(f"adapter rejected {command} (answered {reply!r})")

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
        # A rejected header/filter would send the request to the wrong ECU or
        # accept another module's answer, so both must be acknowledged.
        self._address = None
        if width == 8 and not self._ok(f"ATSH{tx:08X}"):
            # The 4-byte ATSH form is ELM327 v2.x only; v1.x (most clones) set a
            # 29-bit header as priority byte (ATCP) + 3-byte ATSH.
            self._expect_ok(f"ATCP{tx >> 24:02X}")
            self._expect_ok(f"ATSH{tx & 0xFFFFFF:06X}")
        elif width != 8:
            self._expect_ok(f"ATSH{tx:0{width}X}")
        self._expect_ok(f"ATCRA{rx:0{width}X}")
        # Custom flow control is tuning, not correctness: without it the adapter
        # answers first frames with its automatic flow control. Switch to the
        # user mode (FCSM1) only if both its header and data were accepted, or
        # the adapter would be left in user mode with no FC header.
        if self._ok(f"ATFCSH{tx:0{width}X}") and self._ok("ATFCSD300000"):
            if not self._ok("ATFCSM1"):
                log.warning("adapter rejected ATFCSM1; using automatic flow control")
        else:
            log.warning("adapter rejected custom flow control; using automatic flow control")
        self._address = address

    def _ok(self, command: str) -> bool:
        try:
            self._expect_ok(command)
        except ElmError:
            return False
        return True

    @staticmethod
    def _parse(reply: str) -> bytes:
        """ELM answers are hex text, sometimes prefixed with a line counter.

        A reply can hold more than one *message*: an ELM327 prints a
        `7F xx 78` (responsePending) line and keeps waiting, so the real answer
        follows on the next line. Those pending lines are dropped here; joining
        them onto the answer used to yield a corrupt payload that still began
        with 7F..78. If only pending lines arrived, the 7F xx 78 is returned.
        Several separate (non colon-indexed) answers are ambiguous and raise."""
        lines = []
        for line in reply.splitlines():
            line = line.strip().replace(" ", "").replace(">", "")
            if line and line not in ("SEARCHING...", "OK"):
                lines.append(line)
        pending = [ln for ln in lines if ":" not in ln and len(ln) == 6
                   and ln.upper().startswith("7F") and ln.upper().endswith("78")]
        rest = [ln for ln in lines if ln not in pending]
        if pending and not rest:
            return bytes.fromhex(pending[-1])
        if pending:
            reply = "\n".join(rest)
        indexed = any(":" in ln for ln in rest)
        if not indexed and len(rest) > 1 and all(len(ln) > 3 for ln in rest):
            raise ElmError(f"several separate answers in one reply: {rest!r}")
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
                # Do NOT answer with a bare CR: on an ELM327 that repeats the
                # last command, i.e. re-sends the request to the ECU. Instead
                # keep listening for the real answer the ECU promised.
                if time.monotonic() > deadline:
                    raise TransportTimeout(f"{address} kept answering responsePending")
                reply = self._listen(timeout)
                continue
            return data

    def _listen(self, timeout: float) -> str:
        """Reads whatever the adapter prints next, without sending anything."""
        if self._serial is None:
            raise TransportError("transport is not open")
        deadline = time.monotonic() + timeout
        buffer = bytearray()
        while time.monotonic() < deadline:
            chunk = self._serial.read(64)
            if chunk:
                buffer.extend(chunk)
                if PROMPT in buffer:
                    break
        return buffer.replace(PROMPT, b"").decode("ascii", errors="replace").strip()
