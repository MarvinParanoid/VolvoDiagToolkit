"""DTC read (0xAE/0xEE), reversed from the 22-write-dts capture where the ECM
reported 0x2A30 (clogged particulate filter)."""

import unittest

from volvo_diag.protocol import volvo
from volvo_diag.volvo import dtc as dtcmod


class DtcProtocolTest(unittest.TestCase):
    def test_request_framing_matches_capture(self):
        # ECM (0x11) list request seen on the wire: CB 11 AE 1B 00...
        self.assertEqual(volvo.build_dtc_read(0x11).hex().upper(),
                         "CB11AE1B00000000")

    def test_ecm_list_reassembles_to_single_code(self):
        # The exact multi-frame answer captured on can id 0x00400021.
        frames = [
            (0x00400021, bytes.fromhex("8F11EE1B2A300000")),
            (0x00400021, bytes.fromhex("0900000000FFFFFF")),
            (0x00400021, bytes.fromhex("4EFFFFFFFFFFFF00")),
        ]
        block = volvo.reassemble_block(frames, 0x11, volvo.DTC_LIST,
                                       service=volvo.POSITIVE_DTC)
        self.assertEqual(volvo.parse_dtc_list(block), [0x2A30])

    def test_empty_module_yields_no_codes(self):
        # A module with no fault answers EE 1B 00 00 in a single frame.
        frames = [(0x00400003, bytes.fromhex("CB50EE1B00000000"))]
        block = volvo.reassemble_block(frames, 0x50, volvo.DTC_LIST,
                                       service=volvo.POSITIVE_DTC)
        self.assertEqual(volvo.parse_dtc_list(block), [])

    def test_multiple_codes_stop_at_terminator(self):
        block = bytes.fromhex("2A30 1200 0000 2A40".replace(" ", ""))
        self.assertEqual(volvo.parse_dtc_list(block), [0x2A30, 0x1200])


class DtcCatalogueTest(unittest.TestCase):
    def test_2a30_names_the_clogged_dpf(self):
        cat = dtcmod.load_catalogue("ECM")
        self.assertIn("clogged", dtcmod.describe(0x2A30, cat).lower())

    def test_unknown_code_is_empty(self):
        self.assertEqual(dtcmod.describe(0xDEAD, {}), "")


if __name__ == "__main__":
    unittest.main()
