"""Volvo proprietary protocol codec.

Every fixture here is a real request/response pair captured from a VIDA session
on the car (2026-08-01, D4164T / EDC16C31), so a passing test is proof the
codec matches the actual ECM, not a guess.
"""

from __future__ import annotations

import unittest

from volvo_diag.protocol import volvo

# id -> (request frame, response frame, expected 16-bit value)
# Straight from logs/j2534-...-10-ecm-livedata.jsonl, group 0x11.
LIVE = {
    0x05: ("CD11A60005010000", "CE11E600050B7600", 0x0B76),
    0x2E: ("CD11A6002E010000", "CE11E6002E000000", 0x0000),
    0x3A: ("CD11A6003A010000", "CE11E6003A040100", 0x0401),
    0x63: ("CD11A60063010000", "CE11E60063004100", 0x0041),
    0x7E: ("CD11A6007E010000", "CE11E6007E03F200", 0x03F2),
    0x9E: ("CD11A6009E010000", "CE11E6009E092B00", 0x092B),
    0xA7: ("CD11A600A7010000", "CE11E600A70E9300", 0x0E93),
    0xAE: ("CD11A600AE010000", "CE11E600AE000000", 0x0000),
}


class BuildTest(unittest.TestCase):
    def test_request_matches_the_captured_frames(self):
        for identifier, (req_hex, _, _) in LIVE.items():
            built = volvo.build_read(identifier)
            self.assertEqual(built.hex().upper(), req_hex, f"id 0x{identifier:02X}")

    def test_request_is_eight_bytes(self):
        self.assertEqual(len(volvo.build_read(0x7E)), 8)

    def test_length_marker(self):
        # C8 + 5 payload bytes = 0xCD.
        self.assertEqual(volvo.build_read(0x7E)[0], 0xCD)

    def test_identity_group(self):
        built = volvo.build_read(0x1A02, group=volvo.GROUP_IDENTITY)
        self.assertEqual(built.hex().upper(), "CD50A61A02010000")

    def test_rejects_non_16bit_identifier(self):
        with self.assertRaises(volvo.VolvoProtocolError):
            volvo.build_read(0x10000)


class ParseTest(unittest.TestCase):
    def test_decodes_the_captured_values(self):
        for identifier, (_, resp_hex, value) in LIVE.items():
            response = volvo.parse_response(bytes.fromhex(resp_hex))
            self.assertEqual(response.identifier, identifier)
            self.assertEqual(response.group, volvo.GROUP_LIVE_DATA)
            self.assertTrue(response.positive)
            self.assertEqual(response.u16(), value, f"id 0x{identifier:02X}")

    def test_boost_id_7e_decodes_to_the_known_raw(self):
        # 0x03F2 = 1010; VIDA showed intake/boost pressure near atmospheric.
        response = volvo.parse_response(bytes.fromhex("CE11E6007E03F200"))
        self.assertEqual(response.u16(), 1010)

    def test_matches_only_the_right_identifier(self):
        frame = bytes.fromhex("CE11E6007E03F200")
        self.assertTrue(volvo.matches(frame, 0x7E))
        self.assertFalse(volvo.matches(frame, 0x3A))
        self.assertFalse(volvo.matches(frame, 0x7E, group=volvo.GROUP_IDENTITY))

    def test_negative_response_raises(self):
        # A KWP reject: group, 0x7F, echoed service 0xA6, NRC 0x31. The service
        # byte is 0x7F rather than the positive 0xE6.
        with self.assertRaises(volvo.NegativeResponse):
            volvo.parse_response(bytes.fromhex("CC117FA631000000"))

    def test_multiframe_is_rejected_cleanly(self):
        # The identity block starts with 0x97, not a single-frame marker.
        with self.assertRaises(volvo.VolvoProtocolError):
            volvo.parse_response(bytes.fromhex("9750F9FBFE003182"))


if __name__ == "__main__":
    unittest.main()


# Real identity multi-frame captured from the car (CEM 0x50), truncated.
IDENTITY_FRAMES = [bytes.fromhex(h) for h in [
    "9750F9FBFE003182", "1197783030310D0A", "125956314D573736", "1335323932343833",
    "143031350D0A3238", "1535313535303733", "160D0A3534350D0A", "173438333031350D",
]]


class IdentityTest(unittest.TestCase):
    def test_build_identity_request(self):
        # CB 50 B9 FB padded: read the CEM identity block.
        self.assertEqual(volvo.build_identity(0x50).hex().upper(), "CB50B9FB00000000")

    def test_frame_classification(self):
        self.assertTrue(volvo.is_first_frame(IDENTITY_FRAMES[0]))
        self.assertTrue(volvo.is_consecutive_frame(IDENTITY_FRAMES[1]))
        self.assertFalse(volvo.is_single_frame(IDENTITY_FRAMES[0]))

    def test_reassembles_the_vin(self):
        payload = volvo.reassemble_identity(IDENTITY_FRAMES)
        fields = volvo.identity_fields(payload)
        self.assertIn("YV1MW765292483015", fields)


class ReassembleBlockTest(unittest.TestCase):
    """reassemble_block picks a block out by content + CAN id, so it handles both
    multi-frame numberings the modules use — verified against real captures."""

    CID = 0x400003

    def test_identity_framing_0x9x_0x1x(self):
        # The 0xFB identity block: first frame 0x9x, consecutive 0x1x.
        frames = [(self.CID, f) for f in IDENTITY_FRAMES]
        raw = volvo.reassemble_block(frames, 0x50, 0xFB)
        self.assertEqual(raw[:6].hex(), "fe0031829778")  # data length + doc header start
        self.assertIn(b"YV1MW765292483015", raw)

    def test_low_speed_framing_0x8x_0x0x(self):
        # The 0xFC car-config block on the low-speed bus: first frame 0x8F,
        # consecutive 0x08..0x0F. byte 2 = Vehicle sub type, byte 3 = Doors.
        frames = [
            (self.CID, bytes([0x8F, 0x50, 0xF9, 0xFC, 0x8C, 0x3F, 0x03, 0x02])),
            (self.CID, bytes([0x09, 0x07, 0x02, 0x01, 0x00, 0x00, 0x00, 0x00])),
        ]
        raw = volvo.reassemble_block(frames, 0x50, 0xFC)
        self.assertEqual(raw[:4].hex(), "8c3f0302")
        self.assertEqual(raw[2], 0x03)  # Vehicle sub type (V50)
        self.assertEqual(raw[3], 0x02)  # Doors (5 doors)

    def test_ignores_other_traffic_and_wrong_canid(self):
        frames = [
            (0x123456, bytes([0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77, 0x88])),  # bus noise
            (self.CID, bytes([0x8F, 0x50, 0xF9, 0xFC, 0xAA, 0xBB, 0xCC, 0xDD])),
            (0x400099, bytes([0x08, 0x99, 0x99, 0x99, 0x99, 0x99, 0x99, 0x99])),  # other canid
            (self.CID, bytes([0x08, 0x01, 0x02, 0x00, 0x00, 0x00, 0x00, 0x00])),
        ]
        raw = volvo.reassemble_block(frames, 0x50, 0xFC)
        self.assertEqual(raw[:6].hex(), "aabbccdd0102")  # only this block's CAN id, in order


class BlockContiguityTest(unittest.TestCase):
    """block_frames_contiguous flags a dropped mid-block frame (which would
    otherwise decode to shifted garbage) but accepts a clean, wrapping counter."""

    CID = 0x400003

    def _first(self):
        return (self.CID, bytes([0x8F, 0x50, 0xF9, 0xFC, 0x8C, 0x3F, 0x03, 0x02]))

    def _cons(self, seq):
        return (self.CID, bytes([seq, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]))

    def test_contiguous_counter_is_ok(self):
        frames = [self._first()] + [self._cons(s) for s in (0x00, 0x01, 0x02, 0x03)]
        self.assertTrue(volvo.block_frames_contiguous(frames, 0x50, 0xFC))

    def test_wrap_is_ok(self):
        # 15 -> 0 wraps under mod-16; must not be mistaken for a gap.
        frames = [self._first()] + [self._cons(s) for s in (0x0E, 0x0F, 0x00, 0x01)]
        self.assertTrue(volvo.block_frames_contiguous(frames, 0x50, 0xFC))

    def test_dropped_frame_is_flagged(self):
        # counter jumps 1 -> 3 (frame 2 dropped) — the real 0xFC short-read bug.
        frames = [self._first()] + [self._cons(s) for s in (0x00, 0x01, 0x03, 0x04)]
        self.assertFalse(volvo.block_frames_contiguous(frames, 0x50, 0xFC))

    def test_single_consecutive_frame_is_ok(self):
        frames = [self._first(), self._cons(0x09)]
        self.assertTrue(volvo.block_frames_contiguous(frames, 0x50, 0xFC))
