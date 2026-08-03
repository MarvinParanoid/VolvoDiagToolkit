"""sniff_summary: per-id frame count and distinct-payload count, used to flag
event/state frames (few distinct values) in a passive capture."""

import unittest

from volvo_diag.cli import sniff_diff, sniff_summary


class SniffSummaryTest(unittest.TestCase):
    def test_counts_and_distinct(self):
        frames = [
            (0x100, b"\x00"), (0x100, b"\x00"), (0x100, b"\x01"),   # 3x, 2 distinct (a door?)
            (0x200, b"\x01"), (0x200, b"\x02"), (0x200, b"\x03"),   # 3x, 3 distinct (counter-ish)
        ]
        s = sniff_summary(frames)
        self.assertEqual(s[0x100], (3, 2))
        self.assertEqual(s[0x200], (3, 3))

    def test_empty(self):
        self.assertEqual(sniff_summary([]), {})


class SniffDiffTest(unittest.TestCase):
    def test_finds_state_byte_ignoring_counters(self):
        # id 0x65 byte0 is a counter (varies within each capture); byte5 is the
        # door state: constant 0xFF closed, 0xFE open.
        before = [(0x65, bytes([0x85,0,0,0,0,0xFF,0,0])),
                  (0x65, bytes([0xC5,0,0,0,0,0xFF,0,0])),
                  (0x65, bytes([0x05,0,0,0,0,0xFF,0,0]))]
        after  = [(0x65, bytes([0x45,0,0,0,0,0xFE,0,0])),
                  (0x65, bytes([0x85,0,0,0,0,0xFE,0,0]))]
        self.assertEqual(sniff_diff(before, after), [(0x65, 5, 0xFF, 0xFE)])

    def test_no_change(self):
        f = [(0x10, b"\x01\x02")]
        self.assertEqual(sniff_diff(f, f), [])


if __name__ == "__main__":
    unittest.main()
