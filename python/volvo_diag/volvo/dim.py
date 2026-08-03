"""Driving the instrument-cluster (DIM) text line — experimental, a WRITE.

On P1/P2 the DIM's text row is the phone/message display: you show text by
broadcasting frames that spoof the phone module (PHM) on the 125k cabin bus, plus
a few screen-control frames to the DIM's LCD id. This is the mirror of the read
side — nothing here is a diagnostic request; it is raw broadcast injection.

Framing and command bytes are lifted from the working P2 device Vaizer/DDFE
(`printOnLCD`). **The CAN ids are P2** (PHM `0x00C00008`, LCD `0x0220200E`, and
they vary by model year); the P1 V50 ids are not yet known and must be supplied
(captured on-car). So this module is wired but dormant until you pass real ids.
"""

from __future__ import annotations

import time

# Known (phone/PHM text id, DIM/LCD control id) pairs by model year — from
# andrewgabler/VolvoDIM + Vaizer/DDFE. Our V50 is 2007 = facelift, so try that
# first; the others are fallbacks to probe on-car.
PRESETS = {
    "2001": (0x00400008, 0x00C0200E),
    "2002": (0x00C00008, 0x0220200E),
    "facelift": (0x01800008, 0x02A0240E),
}

# Text is a 2x16 (32-char) string sent as five 8-byte frames to the PHM id, each
# led by a sequence marker; the rest is ASCII. From DDFE's printOnLCD.
_MARKERS = (0xA7, 0x21, 0x22, 0x23, 0x65)

# Screen control, sent to the LCD id (P2 pre-facelift values from DDFE).
LCD_ENABLE_1 = bytes([0xC0, 0, 0, 0, 0, 0, 0, 0x05])
LCD_ENABLE_2 = bytes([0xC0, 0, 0, 0, 0, 0, 0, 0x00])
LCD_CLEAR = bytes([0xE1, 0xFE, 0, 0, 0, 0, 0, 0])
LCD_DISABLE = bytes([0x00, 0, 0, 0, 0, 0, 0, 0x04])

MAX_LEN = 32


def encode_text(text: str) -> list:
    """A 32-char string as the five 8-byte PHM frames the DIM expects.

    Layout (marker + ASCII): `A7 00 c0..c5`, `21 c6..c12`, `22 c13..c19`,
    `23 c20..c26`, `65 c27..c31 00 00`. Longer text is truncated, shorter padded
    with spaces; non-printable characters become spaces."""
    b = [ord(c) if 32 <= ord(c) < 127 else 0x20 for c in text[:MAX_LEN].ljust(MAX_LEN)]
    return [
        bytes([0xA7, 0x00, *b[0:6]]),
        bytes([0x21, *b[6:13]]),
        bytes([0x22, *b[13:20]]),
        bytes([0x23, *b[20:27]]),
        bytes([0x65, *b[27:32], 0x00, 0x00]),
    ]


class DimWriter:
    """Broadcasts DIM text/screen frames over a CanLink (the 125k cabin bus).
    A WRITE — inject only on your own car, behind an explicit opt-in."""

    def __init__(self, link, phm_id: int, lcd_id: int, gap: float = 0.02) -> None:
        self.link = link
        self.phm_id = phm_id
        self.lcd_id = lcd_id
        self.gap = gap

    def _send(self, can_id: int, data: bytes) -> None:
        self.link.send(can_id, data, extended=True)
        time.sleep(self.gap)

    def enable(self) -> None:
        for f in (LCD_ENABLE_1, LCD_ENABLE_2, LCD_CLEAR):
            self._send(self.lcd_id, f)

    def clear(self) -> None:
        self._send(self.lcd_id, LCD_CLEAR)

    def disable(self) -> None:
        self._send(self.lcd_id, LCD_DISABLE)

    def show(self, text: str) -> None:
        """Clear the row, then write `text` (one static message)."""
        self._send(self.lcd_id, LCD_CLEAR)
        for f in encode_text(text):
            self._send(self.phm_id, f)
