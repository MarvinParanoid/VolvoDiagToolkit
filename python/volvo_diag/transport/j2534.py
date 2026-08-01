"""J2534 PassThru transport (Windows, ctypes).

Works with any vendor DLL — VXDIAG, the proxy from proxy/, or the fake driver
from fake-j2534/. Pass a path or let it pick a registered device.
"""

from __future__ import annotations

import ctypes
import logging
import platform
import time
from ctypes import POINTER, byref, c_char_p, c_long, c_ubyte, c_ulong, c_void_p
from dataclasses import dataclass

from .base import EcuAddress, Transport, TransportError, TransportTimeout

log = logging.getLogger(__name__)

MSG_DATA_SIZE = 4128

# Protocols
CAN = 5
ISO15765 = 6

# Ioctls
GET_CONFIG = 0x01
SET_CONFIG = 0x02
READ_VBATT = 0x03
CLEAR_TX_BUFFER = 0x07
CLEAR_RX_BUFFER = 0x08
CLEAR_MSG_FILTERS = 0x0A

# Config parameters
DATA_RATE = 0x01
LOOPBACK = 0x03
ISO15765_BS = 0x1E
ISO15765_STMIN = 0x1F

# Filters
PASS_FILTER = 0x01
FLOW_CONTROL_FILTER = 0x03

# Flags
ISO15765_FRAME_PAD = 0x00000040
CAN_29BIT_ID = 0x00000100

# Return codes
STATUS_NOERROR = 0x00
ERR_TIMEOUT = 0x09
ERR_BUFFER_EMPTY = 0x10

# Codes that mean "nothing more arrived", not "something broke". The
# specification says an empty read returns ERR_BUFFER_EMPTY; a VXDIAG returns
# ERR_TIMEOUT, and returns it even when it did put a message in the buffer.
EMPTY_READ_CODES = (ERR_BUFFER_EMPTY, ERR_TIMEOUT)

# RxStatus bits
TX_MSG_TYPE = 0x00000001
START_OF_MESSAGE = 0x00000002
TX_DONE = 0x00000008

ERROR_NAMES = {
    0x01: "ERR_NOT_SUPPORTED", 0x02: "ERR_INVALID_CHANNEL_ID",
    0x03: "ERR_INVALID_PROTOCOL_ID", 0x04: "ERR_NULL_PARAMETER",
    0x05: "ERR_INVALID_IOCTL_VALUE", 0x06: "ERR_INVALID_FLAGS", 0x07: "ERR_FAILED",
    0x08: "ERR_DEVICE_NOT_CONNECTED", 0x09: "ERR_TIMEOUT", 0x0A: "ERR_INVALID_MSG",
    0x0B: "ERR_INVALID_TIME_INTERVAL", 0x0C: "ERR_EXCEEDED_LIMIT",
    0x0D: "ERR_INVALID_MSG_ID", 0x0E: "ERR_DEVICE_IN_USE", 0x0F: "ERR_INVALID_IOCTL_ID",
    0x10: "ERR_BUFFER_EMPTY", 0x11: "ERR_BUFFER_FULL", 0x12: "ERR_BUFFER_OVERFLOW",
    0x13: "ERR_PIN_INVALID", 0x14: "ERR_CHANNEL_IN_USE", 0x15: "ERR_MSG_PROTOCOL_ID",
    0x16: "ERR_INVALID_FILTER_ID", 0x17: "ERR_NO_FLOW_CONTROL", 0x18: "ERR_NOT_UNIQUE",
    0x19: "ERR_INVALID_BAUDRATE", 0x1A: "ERR_INVALID_DEVICE_ID",
}


class PASSTHRU_MSG(ctypes.Structure):
    _fields_ = [
        ("ProtocolID", c_ulong),
        ("RxStatus", c_ulong),
        ("TxFlags", c_ulong),
        ("Timestamp", c_ulong),
        ("DataSize", c_ulong),
        ("ExtraDataIndex", c_ulong),
        # c_ubyte, not c_char: a c_char array stops at the first NUL byte and
        # every CAN id starts with two of them.
        ("Data", c_ubyte * MSG_DATA_SIZE),
    ]


class SCONFIG(ctypes.Structure):
    _fields_ = [("Parameter", c_ulong), ("Value", c_ulong)]


class SCONFIG_LIST(ctypes.Structure):
    _fields_ = [("NumOfParams", c_ulong), ("ConfigPtr", POINTER(SCONFIG))]


class J2534Error(TransportError):
    def __init__(self, function: str, code: int, detail: str = "") -> None:
        name = ERROR_NAMES.get(code, f"0x{code:02X}")
        message = f"{function} failed: {name}"
        if detail:
            message += f" — {detail}"
        super().__init__(message)
        self.code = code


@dataclass
class DeviceEntry:
    name: str
    library: str
    bits: str = ""

    def __str__(self) -> str:
        return f"{self.name}  ->  {self.library}"


def registered_devices() -> list[DeviceEntry]:
    """Reads HKLM\\SOFTWARE\\PassThruSupport.04.04.

    A 32-bit Python sees the WOW6432Node view automatically, which is exactly
    the view a 32-bit VIDA sees — that is the point of running both as x86.
    """
    if platform.system() != "Windows":
        return []
    import winreg  # noqa: PLC0415 — Windows only

    entries: list[DeviceEntry] = []
    for hive, access in (
        (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_READ),
        (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_READ | winreg.KEY_WOW64_32KEY),
        (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_READ | winreg.KEY_WOW64_64KEY),
    ):
        try:
            root = winreg.OpenKey(hive, r"SOFTWARE\PassThruSupport.04.04", 0, access)
        except OSError:
            continue
        with root:
            index = 0
            while True:
                try:
                    name = winreg.EnumKey(root, index)
                except OSError:
                    break
                index += 1
                try:
                    with winreg.OpenKey(root, name, 0, access) as key:
                        library, _ = winreg.QueryValueEx(key, "FunctionLibrary")
                        vendor = ""
                        try:
                            vendor, _ = winreg.QueryValueEx(key, "Vendor")
                        except OSError:
                            pass
                    label = f"{vendor} {name}".strip()
                    entry = DeviceEntry(name=label, library=library)
                    if not any(e.library.lower() == library.lower() for e in entries):
                        entries.append(entry)
                except OSError:
                    continue
    return entries


class J2534Transport(Transport):
    """One ISO15765 channel, one flow-control filter per ECU address."""

    def __init__(
        self,
        library: str | None = None,
        *,
        baudrate: int = 500_000,
        device_name: str | None = None,
        st_min: int = 0,
        block_size: int = 0,
        padding: bool = True,
    ) -> None:
        self.library = library or self._autodetect()
        self.baudrate = baudrate
        self.device_name = device_name
        self.st_min = st_min
        self.block_size = block_size
        self.padding = padding

        self._dll: ctypes.CDLL | None = None
        self._device = c_ulong(0)
        self._channel = c_ulong(0)
        self._filters: dict[int, int] = {}
        self._opened = False

    # ---- lifecycle -----------------------------------------------------

    @staticmethod
    def _autodetect() -> str:
        devices = registered_devices()
        if not devices:
            raise J2534Error("autodetect", 0x08, "no J2534 driver registered on this machine")
        log.info("using J2534 device %s", devices[0])
        return devices[0].library

    def _bind(self) -> None:
        if platform.system() == "Windows":
            self._dll = ctypes.WinDLL(self.library)
        else:
            # Only meaningful against the POSIX build of the proxy/fake driver.
            self._dll = ctypes.CDLL(self.library)

        def prototype(name, restype, *argtypes, required=True):
            try:
                fn = getattr(self._dll, name)
            except AttributeError:
                if required:
                    raise J2534Error(name, 0x01, f"{self.library} does not export {name}")
                return None
            fn.restype = restype
            fn.argtypes = list(argtypes)
            return fn

        self._open = prototype("PassThruOpen", c_long, c_void_p, POINTER(c_ulong))
        self._close = prototype("PassThruClose", c_long, c_ulong)
        self._connect = prototype("PassThruConnect", c_long, c_ulong, c_ulong, c_ulong, c_ulong,
                                  POINTER(c_ulong))
        self._disconnect = prototype("PassThruDisconnect", c_long, c_ulong)
        self._read = prototype("PassThruReadMsgs", c_long, c_ulong, POINTER(PASSTHRU_MSG),
                               POINTER(c_ulong), c_ulong)
        self._write = prototype("PassThruWriteMsgs", c_long, c_ulong, POINTER(PASSTHRU_MSG),
                                POINTER(c_ulong), c_ulong)
        self._start_filter = prototype("PassThruStartMsgFilter", c_long, c_ulong, c_ulong,
                                       POINTER(PASSTHRU_MSG), POINTER(PASSTHRU_MSG),
                                       POINTER(PASSTHRU_MSG), POINTER(c_ulong))
        self._read_version = prototype("PassThruReadVersion", c_long, c_ulong, c_char_p, c_char_p,
                                       c_char_p, required=False)
        self._get_last_error = prototype("PassThruGetLastError", c_long, c_char_p, required=False)
        self._ioctl = prototype("PassThruIoctl", c_long, c_ulong, c_ulong, c_void_p, c_void_p)

    def _last_error(self) -> str:
        if not self._get_last_error:
            return ""
        buffer = ctypes.create_string_buffer(128)
        self._get_last_error(buffer)
        return buffer.value.decode("latin-1", errors="replace")

    def _call(self, function, name: str, *args) -> None:
        code = function(*args)
        if code != STATUS_NOERROR:
            raise J2534Error(name, code, self._last_error())

    def open(self) -> None:
        if self._opened:
            return
        self._bind()
        self._call(self._open, "PassThruOpen", None, byref(self._device))
        self._opened = True
        try:
            self._call(
                self._connect, "PassThruConnect",
                self._device, c_ulong(ISO15765), c_ulong(0), c_ulong(self.baudrate),
                byref(self._channel),
            )
            self._configure()
        except Exception:
            self.close()
            raise

    def _configure(self) -> None:
        params = (SCONFIG * 3)(
            SCONFIG(LOOPBACK, 0),
            SCONFIG(ISO15765_BS, self.block_size),
            SCONFIG(ISO15765_STMIN, self.st_min),
        )
        config = SCONFIG_LIST(len(params), params)
        code = self._ioctl(self._channel, SET_CONFIG, byref(config), None)
        if code != STATUS_NOERROR:  # not fatal: some drivers only accept a subset
            log.warning("SET_CONFIG rejected (%s)", ERROR_NAMES.get(code, code))

    def close(self) -> None:
        if not self._opened:
            return
        try:
            if self._channel.value:
                self._disconnect(self._channel)
            self._close(self._device)
        finally:
            self._channel = c_ulong(0)
            self._filters.clear()
            self._opened = False

    def version(self) -> tuple[str, str, str]:
        if not self._read_version:
            return ("", "", "")
        firmware = ctypes.create_string_buffer(80)
        dll = ctypes.create_string_buffer(80)
        api = ctypes.create_string_buffer(80)
        self._call(self._read_version, "PassThruReadVersion", self._device, firmware, dll, api)
        return tuple(b.value.decode("latin-1", "replace") for b in (firmware, dll, api))

    def battery_millivolts(self) -> int | None:
        value = c_ulong(0)
        if self._ioctl(self._device, READ_VBATT, None, byref(value)) != STATUS_NOERROR:
            return None
        return value.value

    def describe(self) -> str:
        return f"J2534 {self.library}"

    # ---- messaging -----------------------------------------------------

    def _tx_flags(self, address: EcuAddress) -> int:
        flags = ISO15765_FRAME_PAD if self.padding else 0
        if address.extended:
            flags |= CAN_29BIT_ID
        return flags

    @staticmethod
    def _make(protocol: int, flags: int, can_id: int, payload: bytes = b"") -> PASSTHRU_MSG:
        msg = PASSTHRU_MSG()
        msg.ProtocolID = protocol
        msg.TxFlags = flags
        data = can_id.to_bytes(4, "big") + payload
        msg.Data[: len(data)] = data
        msg.DataSize = len(data)
        return msg

    def _ensure_filter(self, address: EcuAddress) -> None:
        if address.tx_id in self._filters:
            return
        flags = self._tx_flags(address)
        mask = self._make(ISO15765, flags, 0xFFFFFFFF)
        pattern = self._make(ISO15765, flags, address.rx_id)
        flow = self._make(ISO15765, flags, address.tx_id)
        filter_id = c_ulong(0)
        self._call(
            self._start_filter, "PassThruStartMsgFilter",
            self._channel, c_ulong(FLOW_CONTROL_FILTER), byref(mask), byref(pattern),
            byref(flow), byref(filter_id),
        )
        self._filters[address.tx_id] = filter_id.value
        log.debug("flow control filter %d for %s", filter_id.value, address)

    def request(self, address: EcuAddress, payload: bytes, timeout: float = 1.0) -> bytes:
        if not self._opened:
            raise TransportError("transport is not open")
        self._ensure_filter(address)

        message = self._make(ISO15765, self._tx_flags(address), address.tx_id, payload)
        count = c_ulong(1)
        self._call(self._write, "PassThruWriteMsgs", self._channel, byref(message), byref(count),
                   c_ulong(int(timeout * 1000)))

        deadline = time.monotonic() + max(timeout, 0.05)
        pending_seen = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TransportTimeout(
                    f"no answer from {address} to {payload.hex().upper()} within {timeout:.1f}s"
                )
            response = self._read_one(address, remaining)
            if response is None:
                continue
            if len(response) >= 3 and response[0] == 0x7F and response[2] == 0x78:
                # responsePending: the ECU asked for more time.
                pending_seen += 1
                if pending_seen > 20:
                    raise TransportTimeout(f"{address} kept answering responsePending")
                deadline = max(deadline, time.monotonic() + max(timeout, 0.5))
                continue
            return response

    def _read_one(self, address: EcuAddress, timeout: float) -> bytes | None:
        """One ReadMsgs call. Returns a payload, or None if nothing usable came."""
        buffer = (PASSTHRU_MSG * 8)()
        count = c_ulong(len(buffer))
        code = self._read(self._channel, buffer, byref(count), c_ulong(int(timeout * 1000)))
        if code != STATUS_NOERROR and code not in EMPTY_READ_CODES:
            raise J2534Error("PassThruReadMsgs", code, self._last_error())

        # Deliberately after the error check and not inside an else: a VXDIAG
        # reports ERR_TIMEOUT while still having filled in one message, so the
        # buffer has to be examined whatever the code says. Unused slots are
        # zeroed, and a DataSize below 5 is skipped below.
        for index in range(min(count.value, len(buffer))):
            msg = buffer[index]
            data = bytes(msg.Data[: msg.DataSize])
            if msg.RxStatus & (TX_MSG_TYPE | TX_DONE):
                continue  # our own frame, looped back
            if msg.RxStatus & START_OF_MESSAGE:
                continue  # first-frame indication, the payload follows
            if len(data) < 5:
                continue
            can_id = int.from_bytes(data[:4], "big")
            if can_id != address.rx_id:
                log.debug("ignoring frame from %03X while waiting for %03X", can_id, address.rx_id)
                continue
            return data[4:]
        return None


# ---------------------------------------------------------------------------
# Raw CAN link for the Volvo proprietary protocol.
#
# The P1 engine is not reachable over ISO15765; VIDA drops to raw 29-bit CAN
# and does the framing itself. This link exposes exactly that: connect the
# device as a plain CAN channel and move single frames, leaving the Volvo
# framing to volvo_diag.protocol.volvo.
# ---------------------------------------------------------------------------

from .volvo_ecm import CanLink  # noqa: E402  (placed here to avoid an import cycle)


class J2534CanLink(CanLink):
    def __init__(self, library: str | None = None, *, baudrate: int = 500_000,
                 extended: bool = True) -> None:
        self._t = J2534Transport(library, baudrate=baudrate)
        self.extended = extended
        self._channel = c_ulong(0)
        self._opened = False

    def describe(self) -> str:
        return f"J2534 raw CAN {self._t.library}"

    def open(self) -> None:
        if self._opened:
            return
        self._t._bind()
        self._t._call(self._t._open, "PassThruOpen", None, byref(self._t._device))
        self._t._opened = True
        try:
            self._t._call(
                self._t._connect, "PassThruConnect",
                self._t._device, c_ulong(CAN), c_ulong(CAN_29BIT_ID if self.extended else 0),
                c_ulong(self._t.baudrate), byref(self._channel),
            )
            self._pass_all_filter()
        except Exception:
            self.close()
            raise
        self._opened = True

    def _pass_all_filter(self) -> None:
        # Receiving on a CAN channel needs at least one filter; mask 0 / pattern
        # 0 lets everything through, and the Volvo layer picks out its answer by
        # the echoed identifier.
        flags = CAN_29BIT_ID if self.extended else 0
        mask = self._t._make(CAN, flags, 0x00000000)
        pattern = self._t._make(CAN, flags, 0x00000000)
        filter_id = c_ulong(0)
        self._t._call(
            self._t._start_filter, "PassThruStartMsgFilter",
            self._channel, c_ulong(PASS_FILTER), byref(mask), byref(pattern),
            None, byref(filter_id),
        )

    def close(self) -> None:
        if not self._t._opened:
            self._opened = False
            return
        try:
            if self._channel.value:
                self._t._disconnect(self._channel)
            self._t._close(self._t._device)
        finally:
            self._channel = c_ulong(0)
            self._t._opened = False
            self._opened = False

    def send(self, can_id: int, data: bytes, extended: bool = True) -> None:
        if not self._opened:
            raise TransportError("link is not open")
        flags = CAN_29BIT_ID if (extended and self.extended) else 0
        message = self._t._make(CAN, flags, can_id, data)
        count = c_ulong(1)
        self._t._call(self._t._write, "PassThruWriteMsgs", self._channel, byref(message),
                      byref(count), c_ulong(200))

    def receive(self, timeout: float):
        if not self._opened:
            raise TransportError("link is not open")
        buffer = (PASSTHRU_MSG * 16)()
        count = c_ulong(len(buffer))
        code = self._t._read(self._channel, buffer, byref(count),
                             c_ulong(max(1, int(timeout * 1000))))
        if code != STATUS_NOERROR and code not in EMPTY_READ_CODES:
            raise J2534Error("PassThruReadMsgs", code, self._t._last_error())
        for index in range(min(count.value, len(buffer))):
            msg = buffer[index]
            raw = bytes(msg.Data[: msg.DataSize])
            if msg.RxStatus & (TX_MSG_TYPE | TX_DONE) or len(raw) < 4:
                continue  # our own frame looped back, or a runt
            yield int.from_bytes(raw[:4], "big"), raw[4:]

    def version(self) -> tuple[str, str, str]:
        return self._t.version()

    def battery_millivolts(self) -> int | None:
        return self._t.battery_millivolts()
