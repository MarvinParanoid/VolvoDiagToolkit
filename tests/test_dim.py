"""DIM text framing (experimental cluster display) — the pure encoder, testable
offline. The CAN ids are still P2/unknown-for-P1, but the frame layout is fixed."""

import unittest

from volvo_diag.volvo import dim


class EncodeTextTest(unittest.TestCase):
    def test_five_frames_of_eight_bytes(self):
        frames = dim.encode_text("hi")
        self.assertEqual(len(frames), 5)
        self.assertTrue(all(len(f) == 8 for f in frames))

    def test_markers_and_ascii_layout(self):
        # 32 distinct-ish chars so we can trace positions
        s = "ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"   # exactly 32
        f = dim.encode_text(s)
        self.assertEqual([fr[0] for fr in f], [0xA7, 0x21, 0x22, 0x23, 0x65])
        self.assertEqual(f[0][1], 0x00)                      # frame 0: A7 00 then 6 chars
        self.assertEqual(bytes(f[0][2:8]).decode(), "ABCDEF")
        self.assertEqual(bytes(f[1][1:8]).decode(), "GHIJKLM")
        self.assertEqual(bytes(f[4][1:6]).decode(), "12345")  # last 5 chars
        self.assertEqual(f[4][6:8], b"\x00\x00")

    def test_padding_and_truncation(self):
        self.assertEqual(bytes(dim.encode_text("hi")[0][2:8]).decode(), "hi    ")  # padded
        long = "X" * 40
        f = dim.encode_text(long)
        self.assertEqual(bytes(f[0][2:8]), b"XXXXXX")          # truncated to 32, no overflow

    def test_non_printable_becomes_space(self):
        f = dim.encode_text("a\tb")
        self.assertEqual(chr(f[0][3]), " ")                    # tab -> space


class DimWriterTest(unittest.TestCase):
    class FakeLink:
        def __init__(self):
            self.sent = []

        def send(self, can_id, data, extended=True):
            self.sent.append((can_id, bytes(data)))

    def test_show_clears_then_broadcasts_text_to_phm(self):
        link = self.FakeLink()
        w = dim.DimWriter(link, phm_id=0x00C00008, lcd_id=0x0220200E, gap=0)
        w.show("test")
        ids = [cid for cid, _ in link.sent]
        self.assertEqual(ids[0], 0x0220200E)                   # LCD clear first
        self.assertEqual(ids[1:], [0x00C00008] * 5)            # then 5 text frames to PHM

    def test_facelift_preset(self):
        # our V50 (2007) candidate id pair
        self.assertEqual(dim.PRESETS["facelift"], (0x01800008, 0x02A0240E))


if __name__ == "__main__":
    unittest.main()
