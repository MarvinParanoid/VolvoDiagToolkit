"""Transport failure handling: ELM327 init validation and resource cleanup,
responsePending handling, and DTC reads that must not invent "no codes".
Fake serial ports only — no hardware."""

import unittest

from volvo_diag.protocol import volvo
from volvo_diag.transport.base import EcuAddress, TransportError, TransportTimeout
from volvo_diag.transport.elm_can import ElmCanLink
from volvo_diag.transport.elm_obd import ElmError, ElmObdTransport
from volvo_diag.transport.volvo_ecm import CanLink, VolvoEcm


class ScriptedElm:
    """A fake ELM327 serial port. `answers` maps a command (upper case) to the
    text it prints before the prompt; anything else answers OK. Records what was
    written and whether close() was called."""

    def __init__(self, answers=None, version="ELM327 v1.5", raise_on=None):
        self.answers = {"ATZ": version, "ATI": version}
        self.answers.update(answers or {})
        self.raise_on = raise_on
        self.sent = []
        self.closed = False
        self._out = bytearray()

    def reset_input_buffer(self):
        self._out.clear()

    def write(self, data):
        cmd = data.decode("latin-1").strip().upper()
        self.sent.append(cmd)
        if self.raise_on and cmd == self.raise_on:
            raise OSError("device disconnected")
        reply = self.answers.get(cmd, "OK")
        self._out += (reply + "\r\r>").encode()

    def flush(self):
        pass

    def read(self, n):
        chunk = bytes(self._out[:n])
        del self._out[:n]
        return chunk

    def close(self):
        self.closed = True


def _owned_elm_can(fake):
    """An ElmCanLink that 'opened' `fake` itself, so it owns (and must close) it."""
    import volvo_diag.transport.elm_can as mod
    orig = mod.open_serial
    mod.open_serial = lambda *a, **k: fake
    try:
        link = ElmCanLink("fake")
        return link, lambda: setattr(mod, "open_serial", orig)
    except Exception:
        mod.open_serial = orig
        raise


class ElmCanInitTest(unittest.TestCase):
    def test_rejected_setup_command_fails_open_and_closes_port(self):
        for bad in ("ATSP7", "ATCAF0", "ATCM00FF0000", "ATCF00400000", "ATCP00"):
            with self.subTest(bad):
                fake = ScriptedElm({bad: "?"})
                link, restore = _owned_elm_can(fake)
                try:
                    with self.assertRaises(TransportError):
                        link.open()
                finally:
                    restore()
                self.assertTrue(fake.closed, "serial port leaked after failed open")
                with self.assertRaises(TransportError):
                    link.send(volvo.REQUEST_CAN_ID, volvo.build_read(5))

    def test_non_elm_closes_port(self):
        fake = ScriptedElm(version="garbage")
        link, restore = _owned_elm_can(fake)
        try:
            with self.assertRaises(TransportError):
                link.open()
        finally:
            restore()
        self.assertTrue(fake.closed)

    def test_disconnect_during_setup_closes_port(self):
        fake = ScriptedElm(raise_on="ATH1")
        link, restore = _owned_elm_can(fake)
        try:
            with self.assertRaises(OSError):
                link.open()
        finally:
            restore()
        self.assertTrue(fake.closed)

    def test_rejected_header_refuses_to_send_the_frame(self):
        fake = ScriptedElm({"ATSH0FFFFE": "?"})
        link = ElmCanLink("fake", serial=fake)
        link.open()
        with self.assertRaises(TransportError):
            link.send(volvo.REQUEST_CAN_ID, volvo.build_read(5))
        frame_hex = volvo.build_read(5).hex().upper()
        self.assertNotIn(frame_hex, fake.sent)   # never went out under a wrong id

    def test_close_then_reopen_gets_a_fresh_port(self):
        ports = [ScriptedElm(), ScriptedElm()]
        import volvo_diag.transport.elm_can as mod
        orig = mod.open_serial
        mod.open_serial = lambda *a, **k: ports.pop(0)
        try:
            link = ElmCanLink("fake")
            link.open()
            link.close()
            link.open()     # used to write to the closed port object
        finally:
            mod.open_serial = orig
        self.assertEqual(ports, [])


class ElmObdInitTest(unittest.TestCase):
    def _transport(self, fake):
        import volvo_diag.transport.elm_can as mod
        t = ElmObdTransport("fake", read_timeout=0.05)
        orig = mod.open_serial
        mod.open_serial = lambda *a, **k: fake
        self.addCleanup(setattr, mod, "open_serial", orig)
        return t

    def test_ok_adapter_opens(self):
        t = self._transport(ScriptedElm())
        t.open()
        self.assertIsNotNone(t._serial)

    def test_rejected_protocol_fails_and_closes(self):
        fake = ScriptedElm({"ATSP7": "?"})
        t = self._transport(fake)
        with self.assertRaises(ElmError):
            t.open()
        self.assertTrue(fake.closed)
        self.assertIsNone(t._serial)

    def test_not_an_elm_fails(self):
        fake = ScriptedElm(version="hello")
        t = self._transport(fake)
        with self.assertRaises(ElmError):
            t.open()
        self.assertTrue(fake.closed)

    def test_rejected_header_does_not_send_request(self):
        fake = ScriptedElm({"ATCRA18DAF110": "?"})
        t = self._transport(fake)
        t.open()
        with self.assertRaises(ElmError):
            t.request(EcuAddress.obd(0), bytes.fromhex("22F190"))
        self.assertNotIn("22F190", fake.sent)


class ElmObdHeaderCompatTest(unittest.TestCase):
    """v1.x ELM327s (most clones) reject the 4-byte ATSH form; they must still
    be addressed correctly (ATCP + 3-byte ATSH), and optional flow-control
    tuning must not make a working adapter unusable."""

    def _request(self, fake):
        import volvo_diag.transport.elm_can as mod
        orig = mod.open_serial
        mod.open_serial = lambda *a, **k: fake
        self.addCleanup(setattr, mod, "open_serial", orig)
        t = ElmObdTransport("fake", read_timeout=0.05)
        t.open()
        return t.request(EcuAddress.obd(0), bytes.fromhex("22F190"), timeout=0.1)

    def test_v1_adapter_falls_back_to_cp_plus_short_header(self):
        fake = ScriptedElm({"ATSH18DA10F1": "?", "22F190": "62F19041"})
        self.assertEqual(self._request(fake).hex().upper(), "62F19041")
        i = fake.sent.index("22F190")
        self.assertIn("ATCP18", fake.sent[:i])
        self.assertIn("ATSHDA10F1", fake.sent[:i])

    def test_v2_adapter_uses_long_header_only(self):
        fake = ScriptedElm({"22F190": "62F19041"})
        self._request(fake)
        self.assertIn("ATSH18DA10F1", fake.sent)
        self.assertNotIn("ATCP18", fake.sent)

    def test_header_rejected_both_ways_refuses(self):
        fake = ScriptedElm({"ATSH18DA10F1": "?", "ATSHDA10F1": "?"})
        with self.assertRaises(ElmError):
            self._request(fake)
        self.assertNotIn("22F190", fake.sent)

    def test_flow_control_rejection_is_not_fatal(self):
        fake = ScriptedElm({"ATFCSH18DA10F1": "?", "22F190": "62F19041"})
        self.assertEqual(self._request(fake).hex().upper(), "62F19041")
        self.assertNotIn("ATFCSM1", fake.sent)   # never user FC mode without a header


class SocketCanFramingErrorTest(unittest.TestCase):
    def test_out_of_order_frame_is_a_transport_error(self):
        from volvo_diag.transport import socketcan as sc

        class FakeSock:
            def __init__(self):
                self.rx = [
                    sc.RAW_FRAME.pack(0x7E8, 8, bytes.fromhex("100A62F190414243")),
                    sc.RAW_FRAME.pack(0x7E8, 8, bytes.fromhex("2544454647484900")),
                ]
            def send(self, data): pass
            def settimeout(self, t): pass
            def recv(self, n):
                return self.rx.pop(0)

        t = sc.SocketCanTransport("vcan0", prefer_kernel_isotp=False)
        t._opened = True
        t._raw = FakeSock()
        with self.assertRaises(TransportError):
            t.request(EcuAddress.obd(0), bytes.fromhex("22F190"), timeout=0.5)


class J2534ProtocolTagTest(unittest.TestCase):
    """The PASSTHRU_MSG ProtocolID of every frame J2534CanLink sends must match
    the channel it was connected with: 5 (CAN) on 500k, 0x8004 on the VXDIAG
    125k low-speed channel. The 125k case used to send 5 (H2.1)."""

    def _sent_protocol(self, protocol):
        from unittest import mock

        from volvo_diag.transport import j2534 as j

        link = j.J2534CanLink.__new__(j.J2534CanLink)
        link._t = mock.MagicMock()
        link._t._make = j.J2534Transport._make
        written = []
        link._t._call.side_effect = lambda fn, name, ch, msg, *a: written.append(
            (name, msg._obj.ProtocolID, bytes(msg._obj.Data[:msg._obj.DataSize])))
        link.extended, link.protocol, link._opened = True, protocol, True
        link._channel = j.c_ulong(1)
        frame = volvo.build_identity(0x51, 0xFB)
        link.send(volvo.REQUEST_CAN_ID, frame)
        (name, proto, data), = written
        self.assertEqual(name, "PassThruWriteMsgs")
        # framing untouched: 4-byte id + the 8 Volvo bytes
        self.assertEqual(data, volvo.REQUEST_CAN_ID.to_bytes(4, "big") + frame)
        return proto

    def test_low_speed_channel_tags_with_its_protocol(self):
        from volvo_diag.transport.j2534 import CAN_LS
        self.assertEqual(self._sent_protocol(CAN_LS), 0x8004)

    def test_high_speed_channel_still_tags_can(self):
        from volvo_diag.transport.j2534 import CAN
        self.assertEqual(self._sent_protocol(CAN), 5)


class ElmPendingTest(unittest.TestCase):
    def test_parse_drops_pending_line_before_answer(self):
        data = ElmObdTransport._parse("7F2278\r62F19041\r>")
        self.assertEqual(data.hex().upper(), "62F19041")

    def test_parse_pending_only(self):
        self.assertEqual(ElmObdTransport._parse("7F2278\r>").hex().upper(), "7F2278")

    def test_parse_pending_then_multiframe(self):
        reply = "7F2278\r014\r0:490201595631\r1:4D573736353239\r2:32343833303135\r>"
        self.assertEqual(ElmObdTransport._parse(reply)[:3].hex().upper(), "490201")

    def test_parse_two_separate_answers_is_an_error(self):
        with self.assertRaises(ElmError):
            ElmObdTransport._parse("410C1AF8\r410C1B00\r>")

    def test_pending_never_resends_the_request(self):
        fake = ScriptedElm({"22F190": "7F2278"})
        import volvo_diag.transport.elm_can as mod
        orig = mod.open_serial
        mod.open_serial = lambda *a, **k: fake
        try:
            t = ElmObdTransport("fake", read_timeout=0.05)
            t.open()
            with self.assertRaises(TransportTimeout):
                t.request(EcuAddress.obd(0), bytes.fromhex("22F190"), timeout=0.1)
        finally:
            mod.open_serial = orig
        # a bare CR would make the ELM repeat (re-send) the last request
        self.assertEqual(fake.sent.count("22F190"), 1)
        self.assertNotIn("", fake.sent)


class OneShotLink(CanLink):
    """Answers the next request with a fixed list of (can_id, frame)."""

    def __init__(self, frames):
        self.frames = frames
        self._pending = []

    def open(self): pass
    def close(self): pass

    def send(self, can_id, data, extended=True):
        self._pending = list(self.frames)

    def receive(self, timeout):
        while self._pending:
            yield self._pending.pop(0)


class DtcReadTest(unittest.TestCase):
    def test_negative_answer_is_an_error_not_no_codes(self):
        ecm = VolvoEcm(OneShotLink([(0x00400021, bytes.fromhex("CB117FAE1100 0000".replace(" ", "")))]),
                       timeout=0.3)
        with self.assertRaises(TransportError):
            ecm.read_dtcs(0x11)

    def test_other_modules_answer_is_not_no_codes(self):
        ecm = VolvoEcm(OneShotLink([(0x00400003, bytes.fromhex("CB50EE1B00000000"))]),
                       timeout=0.3)
        with self.assertRaises(TransportError):
            ecm.read_dtcs(0x11)        # the CEM's answer, not the ECM's

    def test_real_empty_answer_is_no_codes(self):
        ecm = VolvoEcm(OneShotLink([(0x00400003, bytes.fromhex("CB50EE1B00000000"))]),
                       timeout=0.3)
        self.assertEqual(ecm.read_dtcs(0x50), [])

    def test_captured_code_still_read(self):
        frames = [(0x00400021, bytes.fromhex("8F11EE1B2A300000")),
                  (0x00400021, bytes.fromhex("0900000000FFFFFF"))]
        self.assertEqual(VolvoEcm(OneShotLink(frames), timeout=0.3).read_dtcs(0x11), [0x2A30])


if __name__ == "__main__":
    unittest.main()
