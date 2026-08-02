"""Raw Volvo A6 over an ELM327, driven by a fake serial port so the framing and
parsing are exercised without hardware."""

import unittest

from volvo_diag.protocol import volvo
from volvo_diag.transport.base import TransportError
from volvo_diag.transport.elm_can import ElmCanLink
from volvo_diag.transport.volvo_ecm import VolvoEcm


class FakeElm:
    """Minimal ELM327: answers AT commands with OK, ATZ with a version, and the
    ECM coolant request (id 0x0005) with a real captured-style response line."""

    def __init__(self, version="ELM327 v1.5"):
        self.version = version
        self._out = bytearray()
        self.sent = []

    def reset_input_buffer(self):
        self._out.clear()

    def write(self, data):
        cmd = data.decode("latin-1").strip().upper()
        self.sent.append(cmd)
        if cmd == "ATZ":
            self._out += (self.version + "\r\r>").encode()
        elif cmd == "CD11A60005010000":                 # A6 read, group 0x11, id 0x0005
            self._out += b"00 40 00 21 CE 11 E6 00 05 0B 76\r\r>"
        else:
            self._out += b"OK\r\r>"

    def read(self, n):
        chunk = bytes(self._out[:n])
        del self._out[:n]
        return chunk

    def close(self):
        pass


class ElmCanLinkTest(unittest.TestCase):
    def _open(self, **kw):
        link = ElmCanLink("fake", serial=FakeElm(**kw))
        link.open()
        return link

    def test_setup_sends_raw_can_commands(self):
        link = self._open()
        for cmd in ("ATSP7", "ATCAF0", "ATH1"):
            self.assertIn(cmd, link._ser.sent)

    def test_rejects_non_elm(self):
        link = ElmCanLink("fake", serial=FakeElm(version="garbage"))
        with self.assertRaises(TransportError):
            link.open()

    def test_reads_coolant_end_to_end(self):
        ecm = VolvoEcm(self._open(), group=0x11, timeout=0.5)
        # read_identifier returns the value bytes after the echoed id
        self.assertEqual(ecm.read_identifier(0x0005, 0x11), bytes.fromhex("0B76"))

    def test_receive_parses_id_and_payload(self):
        link = self._open()
        link.send(volvo.REQUEST_CAN_ID, volvo.build_read(0x0005, 0x11))
        frames = list(link.receive(0.5))
        self.assertEqual(len(frames), 1)
        can_id, payload = frames[0]
        self.assertEqual(can_id, 0x00400021)
        self.assertEqual(payload[:6].hex(), "ce11e600050b")


if __name__ == "__main__":
    unittest.main()
