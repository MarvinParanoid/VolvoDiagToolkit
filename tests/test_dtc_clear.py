"""Clearing DTCs (0xAF/0x11) — a WRITE, reversed from VIDA's clear sweep in the
22-write-dts capture (`CB 11 AF 11` -> `CB 11 EF 11` per module)."""

import unittest

from volvo_diag.protocol import volvo
from volvo_diag.transport.volvo_ecm import CanLink, VolvoEcm


class ClearProtocolTest(unittest.TestCase):
    def test_request_framing(self):
        self.assertEqual(volvo.build_dtc_clear(0x11).hex().upper(), "CB11AF1100000000")
        self.assertEqual(volvo.build_dtc_clear(0x50).hex().upper(), "CB50AF1100000000")

    def test_ack_recognition(self):
        ack = bytes.fromhex("CB11EF1100000000")           # EF 11 from ECM
        self.assertTrue(volvo.is_clear_ack(ack, 0x11))
        self.assertFalse(volvo.is_clear_ack(ack, 0x50))   # wrong module
        self.assertFalse(volvo.is_clear_ack(bytes.fromhex("CB11EE1100000000"), 0x11))  # not EF


class FakeLink(CanLink):
    """Answers a clear request (AF 11) with the module's EF 11 ack."""

    def __init__(self, ack=True):
        self.ack = ack
        self.sent = []
        self._opened = True

    def open(self): self._opened = True
    def close(self): self._opened = False

    def send(self, can_id, data, extended=True):
        self.sent.append(data)

    def receive(self, timeout):
        if self.ack and self.sent:
            group = volvo.payload_of(self.sent[-1])[0]
            yield 0x00400021, volvo.frame(bytes([group, volvo.POSITIVE_CLEAR_DTC,
                                                  volvo.DTC_CLEAR_SUB]))


class ClearEcmTest(unittest.TestCase):
    def test_clear_returns_true_on_ack(self):
        ecm = VolvoEcm(FakeLink(ack=True), group=0x11, timeout=0.3)
        self.assertTrue(ecm.clear_dtcs(0x11))

    def test_clear_returns_false_without_ack(self):
        ecm = VolvoEcm(FakeLink(ack=False), group=0x11, timeout=0.3)
        self.assertFalse(ecm.clear_dtcs(0x11))


if __name__ == "__main__":
    unittest.main()
