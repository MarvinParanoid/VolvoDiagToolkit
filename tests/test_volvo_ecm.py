"""The Volvo A6 read path, exercised against real captured responses."""

from __future__ import annotations

import unittest
from pathlib import Path

from volvo_diag.protocol import volvo
from volvo_diag.transport.base import TransportTimeout
from volvo_diag.transport.volvo_ecm import ReplayLink, VolvoEcm
from volvo_diag.volvo import parameters as pdb

REPO = Path(__file__).resolve().parents[1]

# Real (identifier -> response frame) pairs captured from the car, group 0x11.
CAPTURED = {
    0x05: bytes.fromhex("CE11E600050B7600"),
    0x2E: bytes.fromhex("CE11E6002E000000"),
    0x3A: bytes.fromhex("CE11E6003A040100"),
    0x50: bytes.fromhex("CE11E60050000000"),
    0x63: bytes.fromhex("CE11E60063004100"),
    0x7E: bytes.fromhex("CE11E6007E03F200"),
    0x9E: bytes.fromhex("CE11E6009E092B00"),
    0xA7: bytes.fromhex("CE11E600A70E9300"),
    0xAE: bytes.fromhex("CE11E600AE000000"),
}


class ReadPathTest(unittest.TestCase):
    def setUp(self) -> None:
        self.link = ReplayLink(CAPTURED)
        self.link.open()
        self.ecm = VolvoEcm(self.link)

    def tearDown(self) -> None:
        self.link.close()

    def test_reads_raw_value_bytes(self):
        self.assertEqual(self.ecm.read_identifier(0x7E).hex().upper(), "03F2")
        self.assertEqual(self.ecm.read_identifier(0x3A).hex().upper(), "0401")

    def test_unanswered_identifier_times_out(self):
        with self.assertRaises(TransportTimeout):
            self.ecm.read_identifier(0x1234, timeout=0.05)

    def test_send_builds_the_exact_captured_request(self):
        # ReplayLink only answers if the request framing is right, so a
        # successful read is itself proof the request bytes match the car.
        request = volvo.build_read(0xAE)
        self.assertEqual(request.hex().upper(), "CD11A600AE010000")
        self.assertEqual(self.ecm.read_identifier(0xAE), b"\x00\x00")


class DatabaseReadTest(unittest.TestCase):
    """Read database parameters through the reader, end to end."""

    def setUp(self) -> None:
        self.db = pdb.load(REPO / "definitions" / "volvo" / "p1" / "d4164t.yaml")
        self.link = ReplayLink(CAPTURED)
        self.link.open()
        self.ecm = VolvoEcm(self.link)

    def tearDown(self) -> None:
        self.link.close()

    def test_boost_actual_reads_atmospheric_at_idle(self):
        value = self.ecm.read(self.db["boost_actual"])
        self.assertAlmostEqual(value, 102.5, places=1)  # 0x0401=1025 * 0.1 kPa

    def test_boost_requested(self):
        value = self.ecm.read(self.db["boost_requested"])
        self.assertAlmostEqual(value, 101.0, places=1)  # 0x03F2=1010 * 0.1

    def test_dpf_pressure_low_at_idle(self):
        value = self.ecm.read(self.db["dpf_differential_pressure"])
        self.assertAlmostEqual(value, 0.0, places=1)

    def test_reading_a_uds_parameter_through_the_volvo_reader_is_refused(self):
        # rpm is a standard-PID (UDS) entry; the Volvo reader must not accept it.
        from volvo_diag.transport.base import TransportError

        with self.assertRaises(TransportError):
            self.ecm.read(self.db["rpm"])


if __name__ == "__main__":
    unittest.main()


class IdentityReadTest(unittest.TestCase):
    """read_identity over a replayed multi-frame response."""

    IDENTITY = [bytes.fromhex(h) for h in [
        "9750F9FBFE003182", "1197783030310D0A", "125956314D573736", "1335323932343833",
        "143031350D0A3238", "1535313535303733", "160D0A3534350D0A", "173438333031350D",
    ]]

    def test_reads_vin_from_multiframe(self):
        link = ReplayLink({}, identity_frames=self.IDENTITY)
        link.open()
        try:
            fields = VolvoEcm(link, group=0x50, timeout=0.5).read_identity()
        finally:
            link.close()
        self.assertIn("YV1MW765292483015", fields)

    def test_read_block_checked_flags_a_dropped_frame(self):
        # A complete block reads clean; dropping a consecutive frame (the 0x13
        # sequence marker) leaves a gap that read_block_checked must report so a
        # caller re-reads instead of decoding shifted bytes.
        for frames, expect_ok in ((self.IDENTITY, True),
                                  (self.IDENTITY[:3] + self.IDENTITY[4:], False)):
            link = ReplayLink({}, identity_frames=frames)
            link.open()
            try:
                raw, ok = VolvoEcm(link, group=0x50, timeout=0.5).read_block_checked(
                    0xFB, group=0x50)
            finally:
                link.close()
            self.assertEqual(ok, expect_ok)
            self.assertTrue(raw)  # bytes still returned either way
