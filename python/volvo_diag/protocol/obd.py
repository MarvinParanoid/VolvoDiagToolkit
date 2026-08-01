"""SAE J1979 (OBD-II) mode 01 and 09.

These are the only parameters that are standard across manufacturers, so they
are the honest starting point: whatever the D4164T answers here is known-good
without any reverse engineering. Everything Volvo-specific lives in
definitions/volvo/.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

MODE_CURRENT_DATA = 0x01
MODE_VEHICLE_INFO = 0x09


@dataclass(frozen=True)
class Pid:
    pid: int
    name: str
    unit: str
    length: int
    decode: Callable[[bytes], float]
    description: str = ""


def _u8(data: bytes) -> int:
    return data[0]


def _u16(data: bytes) -> int:
    return int.from_bytes(data[:2], "big")


PIDS: dict[int, Pid] = {
    0x04: Pid(0x04, "engine_load", "%", 1, lambda d: _u8(d) * 100 / 255),
    0x05: Pid(0x05, "coolant_temp", "degC", 1, lambda d: _u8(d) - 40),
    0x0B: Pid(0x0B, "intake_manifold_pressure", "kPa", 1, _u8,
              "absolute, 1 kPa resolution — too coarse for boost work"),
    0x0C: Pid(0x0C, "rpm", "rpm", 2, lambda d: _u16(d) / 4),
    0x0D: Pid(0x0D, "speed", "km/h", 1, _u8),
    0x0F: Pid(0x0F, "intake_air_temp", "degC", 1, lambda d: _u8(d) - 40),
    0x10: Pid(0x10, "maf", "g/s", 2, lambda d: _u16(d) / 100),
    0x11: Pid(0x11, "throttle", "%", 1, lambda d: _u8(d) * 100 / 255),
    0x1F: Pid(0x1F, "run_time", "s", 2, _u16),
    0x21: Pid(0x21, "distance_with_mil", "km", 2, _u16),
    0x2F: Pid(0x2F, "fuel_level", "%", 1, lambda d: _u8(d) * 100 / 255),
    0x31: Pid(0x31, "distance_since_clear", "km", 2, _u16),
    0x33: Pid(0x33, "barometric_pressure", "kPa", 1, _u8),
    0x5C: Pid(0x5C, "oil_temp", "degC", 1, lambda d: _u8(d) - 40),
    0x62: Pid(0x62, "engine_torque_actual", "%", 1, lambda d: _u8(d) - 125),
    # Diesel-specific PIDs, standardised but frequently unsupported on a 2007
    # Euro 4 car. Ask PID 0x00/0x60/0x80 first to find out.
    0x70: Pid(0x70, "boost_control", "raw", 9, lambda d: _u16(d[1:3]) / 32,
              "layout depends on the support byte; treat the raw bytes as authoritative"),
    0x73: Pid(0x73, "exhaust_pressure", "kPa", 5, lambda d: _u16(d[1:3]) / 128),
    0x7C: Pid(0x7C, "dpf_temp", "degC", 9, lambda d: _u16(d[1:3]) / 10 - 40),
    0x8B: Pid(0x8B, "dpf_delta_pressure", "kPa", 7, lambda d: _u16(d[3:5]) / 128 - 256,
              "bank 1 delta pressure, per J1979-DA"),
}

SUPPORT_PIDS = (0x00, 0x20, 0x40, 0x60, 0x80, 0xA0, 0xC0)


def request(pid: int, mode: int = MODE_CURRENT_DATA) -> bytes:
    return bytes([mode, pid])


def vin_request() -> bytes:
    return bytes([MODE_VEHICLE_INFO, 0x02])


def parse(pid: int, response: bytes, mode: int = MODE_CURRENT_DATA) -> bytes:
    """Strips the mode/PID echo from a positive response."""
    if len(response) < 2:
        raise ValueError(f"short response: {response.hex()}")
    if response[0] != mode + 0x40:
        raise ValueError(f"not a mode {mode:02X} response: {response.hex()}")
    if response[1] != pid:
        raise ValueError(f"asked for PID {pid:02X}, got {response[1]:02X}")
    return response[2:]


def decode(pid: int, data: bytes) -> float:
    entry = PIDS.get(pid)
    if entry is None:
        raise KeyError(f"PID 0x{pid:02X} has no decoder")
    if len(data) < entry.length:
        raise ValueError(
            f"PID 0x{pid:02X} needs {entry.length} bytes, got {len(data)}: {data.hex()}"
        )
    return entry.decode(data)


def supported_pids(base: int, bitmask: bytes) -> list[int]:
    """Expands the four-byte support bitmask returned by PID 0x00/0x20/..."""
    out = []
    value = int.from_bytes(bitmask[:4], "big")
    for bit in range(32):
        if value & (1 << (31 - bit)):
            out.append(base + bit + 1)
    return out


def parse_vin(payload: bytes) -> str:
    """Body of a 49 02 response: one message-count byte then ASCII."""
    if payload and payload[0] in (0x01, 0x00):
        payload = payload[1:]
    return payload.decode("ascii", errors="replace").strip("\x00 ")
