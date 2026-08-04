"""ElmObdTransport._parse: turn an ELM327's hex text into bytes, including the
multi-frame form where a total-length header ("014") precedes the data."""

import unittest

from volvo_diag.transport.elm_obd import ElmError, ElmObdTransport


class ParseTest(unittest.TestCase):
    def test_single_line(self):
        self.assertEqual(ElmObdTransport._parse("41 0C 1A F8\r>").hex().upper(), "410C1AF8")

    def test_colon_indexed_multiframe_without_length_header(self):
        raw = ElmObdTransport._parse("0:410C1AF8\r1:00000000\r>")
        self.assertEqual(raw.hex().upper(), "410C1AF800000000")

    def test_strips_multiframe_length_header(self):
        # 014 = 20 bytes; a mode-09 VIN response 49 02 01 + "YV1MW765292483015".
        reply = ("014\r0:490201595631\r1:4D573736353239\r2:32343833303135\r>")
        raw = ElmObdTransport._parse(reply)
        self.assertEqual(raw[:3].hex().upper(), "490201")   # was 01 44 90.. before the fix
        self.assertIn(b"YV1MW765292483015", raw)

    def test_length_header_not_stripped_when_it_does_not_match(self):
        # a 3-char first line whose value isn't the body byte-length stays as data
        # ("ABC" = 2748 != 4 bytes), so it is not mistaken for a length header.
        raw = ElmObdTransport._parse("ABC\r0:41051234\r>")
        self.assertTrue(raw.hex().upper().startswith("ABC4"))

    def test_error_word_raises(self):
        with self.assertRaises(ElmError):
            ElmObdTransport._parse("UNABLE TO CONNECT\r>")


if __name__ == "__main__":
    unittest.main()
