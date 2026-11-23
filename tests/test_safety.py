"""The read-only boundary: what may reach the bus without --enable-writes, and
that the commands refuse everything else *before* opening an adapter.

No hardware: transports/links are replaced by mocks that fail the test if they
are ever constructed for a refused request."""

import argparse
import io
import textwrap
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from volvo_diag import cli, safety
from volvo_diag.volvo import parameters as pdb


class AllowlistTest(unittest.TestCase):
    def test_uds_reads_pass(self):
        for hexs in ("22F190", "0100", "0902", "1902FF", "03", "3E00"):
            safety.check_uds_read(bytes.fromhex(hexs))

    def test_uds_writes_and_controls_refused(self):
        for hexs in ("04",          # OBD clear DTCs
                     "14FFFFFF",    # UDS clear DTCs
                     "1101",        # ECU reset
                     "1002",        # programming session
                     "2701",        # security access
                     "2EF19000",    # write DID
                     "2F123403",    # IO control
                     "3101FF00",    # routine control
                     "3B9000",      # KWP write by local id
                     "85 02".replace(" ", ""),
                     ""):
            with self.subTest(hexs), self.assertRaises(safety.UnsafeRequest):
                safety.check_uds_read(bytes.fromhex(hexs))

    def test_volvo_reads_pass(self):
        for hexs in ("11A6000501", "50B9FB", "11AE1B", "50BB00400006"):
            safety.check_volvo_read(bytes.fromhex(hexs))

    def test_volvo_writes_and_controls_refused(self):
        for svc in (0xA1, 0xA3, 0xA8, 0xAA, 0xAF, 0xB0, 0xB1, 0xB2, 0xB4, 0xB8, 0xBA, 0x10):
            with self.subTest(hex(svc)), self.assertRaises(safety.UnsafeRequest):
                safety.check_volvo_read(bytes([0x50, svc, 0x00]))
        with self.assertRaises(safety.UnsafeRequest):
            safety.check_volvo_read(b"\x50")


def _run(argv):
    err = io.StringIO()
    with redirect_stderr(err):
        rc = cli.main(argv)
    return rc, err.getvalue()


class ReadCommandTest(unittest.TestCase):
    """`read <hex>` used to send any payload (e.g. 14FFFFFF) with no gate."""

    def test_raw_write_refused_without_touching_transport(self):
        with mock.patch.object(cli, "build_transport",
                               side_effect=AssertionError("transport opened")):
            for hexs in ("14FFFFFF", "04", "1101", "2701", "2EF19000", "3101FF00"):
                for extra in ([], ["--enable-writes"]):   # the flag does not lift it
                    with self.subTest(hexs, extra=extra):
                        rc, err = _run([*extra, "--transport", "socketcan", "read", hexs])
                        self.assertEqual(rc, 2)
                        self.assertIn("refusing", err)

    def test_garbage_hex_is_a_clean_error(self):
        with mock.patch.object(cli, "build_transport",
                               side_effect=AssertionError("transport opened")):
            rc, err = _run(["--transport", "socketcan", "read", "not-hex"])
        self.assertEqual(rc, 2)

    def test_raw_read_still_reaches_the_ecu(self):
        transport = mock.MagicMock()
        transport.__enter__.return_value = transport
        transport.request.return_value = bytes.fromhex("62F19041")
        with mock.patch.object(cli, "build_transport", return_value=transport):
            rc, _ = _run(["--transport", "socketcan", "read", "22F190"])
        self.assertEqual(rc, 0)
        self.assertEqual(transport.request.call_args[0][1], bytes.fromhex("22F190"))


class ReadMemTest(unittest.TestCase):
    """read-mem used a 4-entry denylist; B0/B1 (IO control) and B2 (routine)
    went straight through."""

    def test_control_services_refused_before_opening(self):
        with mock.patch.object(cli, "open_volvo_ecm",
                               side_effect=AssertionError("bus opened")):
            for raw in ("50B0010203", "50B10102", "50B20101", "50A801", "50AF11",
                        "50B8F0AA", "50A301"):
                with self.subTest(raw):
                    rc, err = _run(["read-mem", "--ecu", "CEM", "--raw", raw])
                    self.assertEqual(rc, 2)
                    self.assertIn("refusing", err)

    def test_service_flag_also_checked(self):
        with mock.patch.object(cli, "open_volvo_ecm",
                               side_effect=AssertionError("bus opened")):
            rc, _ = _run(["read-mem", "--ecu", "CEM", "--addr", "4000", "--service", "B0"])
        self.assertEqual(rc, 2)


class DtcClearGateTest(unittest.TestCase):
    def test_clear_needs_flag(self):
        with mock.patch.object(cli, "open_reader",
                               side_effect=AssertionError("adapter opened")):
            rc, err = _run(["dtc", "--clear"])
        self.assertEqual(rc, 2)
        self.assertIn("--enable-writes", err)

    def test_clear_on_uds_path_is_refused_not_ignored(self):
        with mock.patch.object(cli, "build_transport",
                               side_effect=AssertionError("transport opened")):
            rc, err = _run(["--enable-writes", "--transport", "socketcan", "dtc", "--clear"])
        self.assertEqual(rc, 2)
        self.assertIn("nothing was cleared", err)

    def test_dim_text_needs_flag(self):
        with mock.patch.object(cli, "open_volvo_ecm",
                               side_effect=AssertionError("bus opened")):
            rc, err = _run(["dim-text", "--year", "facelift", "HI"])
        self.assertEqual(rc, 2)


class ClearSweepTest(unittest.TestCase):
    """A failing clear or re-read must neither abort the sweep nor be reported
    as 'cleared'."""

    def _sweep(self, clear, read_after):
        db = pdb.load_profile(pdb.default_path(), None)
        modules = [n for n, e in db.ecus.items() if e.is_volvo]
        calls = {"read": 0}

        def read_dtcs(group):
            calls["read"] += 1
            if calls["read"] % 2 == 0:      # every second read is the re-read
                return read_after(group)
            return [0x2A30]

        reader = cli._Reader("fake", lambda p: None, lambda: None,
                             read_dtcs=read_dtcs, clear_dtcs=clear)
        args = argparse.Namespace(clear=True, enable_writes=True)
        out = io.StringIO()
        with mock.patch.object(cli, "open_reader", return_value=reader), \
                mock.patch("sys.stdout", out):
            rc = cli._cmd_dtc_volvo(args, db)
        return rc, out.getvalue(), len(modules)

    def test_reread_failure_is_unverified_and_sweep_continues(self):
        def boom(group):
            raise cli.TransportError("silent")
        rc, out, n = self._sweep(lambda g: True, boom)
        self.assertEqual(rc, 0)
        self.assertEqual(out.count("unverified"), n)
        self.assertNotIn(": cleared", out)

    def test_clear_error_does_not_abort(self):
        def bad_clear(group):
            raise cli.TransportError("bus off")
        rc, out, n = self._sweep(bad_clear, lambda g: [])
        self.assertEqual(out.count("error (bus off)"), n)


class DefinitionRequestTest(unittest.TestCase):
    """A definition's request is polled automatically, so it must be a read."""

    def _load(self, request):
        with TemporaryDirectory() as d:
            path = Path(d) / "x.yaml"
            path.write_text(textwrap.dedent(f"""
                parameters:
                  p:
                    ecu: ECM
                    request: "{request}"
                    encoding: {{type: uint16_be}}
                """), encoding="utf-8")
            return pdb.load(path)

    def test_read_request_loads(self):
        self.assertIn("p", self._load("22F190").parameters)

    def test_write_request_rejected(self):
        for req in ("14FFFFFF", "04", "2EF19000", "3101FF00"):
            with self.subTest(req), self.assertRaises(pdb.DefinitionError):
                self._load(req)


if __name__ == "__main__":
    unittest.main()
