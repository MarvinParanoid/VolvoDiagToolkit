"""ISO-TP framing, UDS and OBD encoding."""

from __future__ import annotations

import unittest

from volvo_diag.protocol import isotp, obd, uds


class IsoTpTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = isotp.IsoTpConfig(tx_id=0x7E0, rx_id=0x7E8, padding=0x00)

    def test_single_frame(self):
        frames = isotp.encode(bytes.fromhex("22F190"), self.config)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].hex().upper(), "0322F19000000000")

    def test_multi_frame_round_trip(self):
        payload = bytes(range(40))
        frames = isotp.encode(payload, self.config)
        self.assertEqual(frames[0][:2].hex().upper(), "1028")  # first frame, 0x028 = 40 bytes

        reassembler = isotp.Reassembler(self.config)
        received = []
        for frame in frames:
            received.extend(reassembler.feed(frame))
        self.assertEqual(received, [payload])

    def test_first_frame_asks_for_flow_control(self):
        frames = isotp.encode(bytes(range(20)), self.config)
        reassembler = isotp.Reassembler(self.config)
        list(reassembler.feed(frames[0]))
        self.assertEqual(reassembler.pending_flow_control[:3].hex().upper(), "300000")

    def test_out_of_order_consecutive_frame_raises(self):
        frames = isotp.encode(bytes(range(20)), self.config)
        reassembler = isotp.Reassembler(self.config)
        list(reassembler.feed(frames[0]))
        with self.assertRaises(isotp.IsoTpError):
            list(reassembler.feed(frames[2]))

    def test_flow_control_parsing(self):
        status, block_size, st_min = isotp.parse_flow_control(bytes.fromhex("300A14"))
        self.assertEqual((status, block_size, st_min), (0, 10, 20))
        # 0xF1..0xF9 are sub-millisecond separations.
        self.assertEqual(isotp.parse_flow_control(bytes.fromhex("3000F1"))[2], 1)


class UdsTest(unittest.TestCase):
    def test_read_data_by_identifier(self):
        self.assertEqual(uds.read_data_by_identifier(0xD123).hex().upper(), "22D123")

    def test_check_strips_the_echo(self):
        request = uds.read_data_by_identifier(0xF190)
        response = bytes.fromhex("62F190") + b"YV1MW7546A2000001"
        self.assertEqual(uds.check(request, response), b"YV1MW7546A2000001")

    def test_negative_response_raises_with_a_reason(self):
        request = uds.read_data_by_identifier(0xD123)
        with self.assertRaises(uds.NegativeResponse) as caught:
            uds.check(request, bytes.fromhex("7F2231"))
        self.assertEqual(caught.exception.nrc, 0x31)
        self.assertIn("requestOutOfRange", str(caught.exception))

    def test_answer_to_a_different_identifier_is_rejected(self):
        request = uds.read_data_by_identifier(0xD123)
        with self.assertRaises(uds.UnexpectedResponse):
            uds.check(request, bytes.fromhex("62D1240001"))

    def test_dtc_parsing(self):
        dtcs = uds.parse_dtcs(bytes.fromhex("1103002F"))
        self.assertEqual(len(dtcs), 1)
        self.assertEqual(dtcs[0].code, 0x110300)
        self.assertTrue(dtcs[0].confirmed)


class ObdTest(unittest.TestCase):
    def test_rpm(self):
        data = obd.parse(0x0C, bytes.fromhex("410C0CE4"))
        self.assertEqual(obd.decode(0x0C, data), 825.0)

    def test_temperature_offset(self):
        data = obd.parse(0x05, bytes.fromhex("410559"))
        self.assertEqual(obd.decode(0x05, data), 49)

    def test_wrong_pid_is_rejected(self):
        with self.assertRaises(ValueError):
            obd.parse(0x0C, bytes.fromhex("410B60"))

    def test_support_bitmask(self):
        # 0xBE1FA813 -> the classic "everything a petrol car has" mask.
        pids = obd.supported_pids(0x00, bytes.fromhex("BE1FA813"))
        self.assertIn(0x0C, pids)
        self.assertIn(0x01, pids)
        self.assertNotIn(0x02, pids)

    def test_vin(self):
        self.assertEqual(obd.parse_vin(b"\x01YV1MW7546A2000001"), "YV1MW7546A2000001")


if __name__ == "__main__":
    unittest.main()
