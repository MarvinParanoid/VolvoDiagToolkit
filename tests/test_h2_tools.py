"""The H2 helper scripts (scripts/h2_proto_check.py, scripts/h2_sanitize.py):
synthetic proxy logs only. The sanitizer must remove identifiers without
corrupting the protocol fields needed for debugging."""

import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
VIN = "YV1SYNTH0T0912345"   # synthetic: never put a real VIN in the repo
FYON = "1234567"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


check_mod = _load("h2_proto_check")
san = _load("h2_sanitize")


def rec(**kw):
    return json.dumps(kw)


def msg(can_id, payload_hex, proto=5):
    return {"proto": proto, "data": f"{can_id:08X}" + payload_hex.replace(" ", "").upper()}


def connect(channel, protocol, baud):
    return rec(ev="connect", protocol=protocol, protocol_name="?", flags="0x100", baud=baud,
               channel=channel, result=0, result_name="STATUS_NOERROR")


def identity_frames():
    """A B9 FB answer whose VIN is split so a frame holds a single VIN byte."""
    # 10 pad bytes: frame 0 carries body[0:4], frame 1 body[4:11] -> its last
    # byte is VIN[0] alone; the next frame continues the VIN.
    body = b"\x00" * 10 + VIN.encode() + b"EU008" + b"\x00" * 3
    frames = [bytes([0x9F, 0x50, 0xF9, 0xFB]) + body[:4]]
    rest, seq = body[4:], 0
    while rest:
        frames.append(bytes([0x10 | seq]) + rest[:7])
        rest, seq = rest[7:], (seq + 1) % 8
    return frames


class ProtoCheckTest(unittest.TestCase):
    def _run(self, lines):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.jsonl"
            p.write_text("\n".join(lines) + "\n", encoding="utf-8")
            out = io.StringIO()
            with redirect_stdout(out):
                rc = check_mod.main([str(p)])
        return rc, out.getvalue()

    def test_low_speed_mismatch_is_flagged(self):
        rc, out = self._run([
            connect(7, 0x8004, 125000),
            rec(ev="start_filter", channel=7, mask={"proto": 0x8004},
                result=0, result_name="STATUS_NOERROR"),
            rec(ev="write", channel=7, msgs=[msg(0x0FFFFE, "CB51B9FB00000000")],
                result=0x15, result_name="ERR_MSG_PROTOCOL_ID"),
        ])
        self.assertEqual(rc, 0)                       # a read, so no safety stop
        self.assertIn("0x8004 write           0x0005  ERR_MSG_PROTOCOL_ID  (1)  <-- MISMATCH",
                      out)
        self.assertIn("0x0005 B9: 1", out)

    def test_matching_tags_are_not_flagged(self):
        rc, out = self._run([
            connect(7, 0x8004, 125000),
            rec(ev="write", channel=7, msgs=[msg(0x0FFFFE, "CB51B9FB00000000", 0x8004)],
                result=0, result_name="STATUS_NOERROR"),
        ])
        self.assertEqual(rc, 0)
        self.assertNotIn("MISMATCH", out)

    def test_non_read_service_fails(self):
        for payload in ("CB11AF1100000000",            # clear DTC
                        "CC51B80A01000000",            # write block
                        "CC50B0010200000000"):         # IO control
            rc, out = self._run([
                connect(1, 5, 500000),
                rec(ev="write", channel=1, msgs=[msg(0x0FFFFE, payload)],
                    result=0, result_name="STATUS_NOERROR"),
            ])
            self.assertEqual(rc, 1, payload)
            self.assertIn("NOT A READ SERVICE", out)

    def test_isotp_continuation_frames_are_unclassified_not_unsafe(self):
        rc, out = self._run([
            connect(1, 5, 500000),
            rec(ev="write", channel=1, msgs=[msg(0x7E0, "1014 2E F1 90 59 56 31"),   # ISO-TP FF
                                             msg(0x7E0, "21 4D 57 37 36 35 32 39"),  # CF
                                             msg(0x7E0, "30 00 00 00 00 00 00 00")],  # FC
                result=0, result_name="STATUS_NOERROR"),
        ])
        self.assertEqual(rc, 3)
        self.assertIn("UNCLASSIFIED", out)
        self.assertNotIn("NOT A READ SERVICE", out)
        self.assertIn("VERDICT: INCONCLUSIVE", out)

    def test_unsafe_write_on_unknown_channel_still_caught(self):
        rc, out = self._run([
            rec(ev="write", channel=9, msgs=[msg(0x0FFFFE, "CB11AF1100000000")],
                result=0, result_name="STATUS_NOERROR"),
        ])
        self.assertEqual(rc, 1)
        self.assertIn("INCOMPLETE: no successful connect", out)
        self.assertIn("VERDICT: UNSAFE", out)

    def test_incomplete_logs_are_inconclusive(self):
        rc, out = self._run([
            connect(1, 5, 500000),
            "{not json",
            rec(ev="read", channel=4, msgs=[msg(0x400021, "CE11E6003A03F200")],
                result=0, result_name="STATUS_NOERROR"),
        ])
        self.assertEqual(rc, 3)
        self.assertIn("1 unreadable line(s)", out)
        self.assertIn("records on channels with no logged connect: 1 read", out)

    def test_extended_addressing_service_byte(self):
        rc, out = self._run([
            connect(2, 6, 500000),
            rec(ev="write", channel=2,
                msgs=[dict(msg(0x18DA10F1, "F122F190", 6), tx_flags=0x80)],
                result=0, result_name="STATUS_NOERROR"),
        ])
        self.assertEqual(rc, 0, out)
        self.assertIn("0x0006 22: 1", out)

    def test_iso15765_reads_pass(self):
        rc, _ = self._run([
            connect(2, 6, 500000),
            rec(ev="write", channel=2, msgs=[msg(0x7E0, "22F190", 6), msg(0x7E0, "3E00", 6)],
                result=0, result_name="STATUS_NOERROR"),
        ])
        self.assertEqual(rc, 0)


class SanitizeTest(unittest.TestCase):
    def _log(self):
        frames = identity_frames()
        lines = [
            rec(ev="session", real_dll="C:\\Users\\alice\\VX\\VXDIAG_J2534.dll",
                config="missing:C:\\Users\\alice\\build\\j2534proxy.ini", tag="h2-02"),
            rec(ev="open", name="VXDIAG-SN-4711", device=1, result=0),
            connect(1, 5, 500000),
            rec(ev="write", channel=1, msgs=[msg(0x0FFFFE, "CB50B9FB00000000")], result=0),
            rec(ev="read", channel=1, msgs=[msg(0x400003, f.hex()) for f in frames], result=0),
            # an ECU serial-number block (B9 F8) and an ordinary A6 reading
            rec(ev="read", channel=1, msgs=[msg(0x400021, "8C11F9F8A1B2C3D4"),
                                            msg(0x400021, "10E5F60718293A4B"),
                                            msg(0x400021, "CE11E6003A03F200")], result=0),
        ]
        return lines, frames

    def test_vin_split_across_frames_is_masked_and_framing_kept(self):
        lines, frames = self._log()
        out = san.sanitize_jsonl(lines, san.identifiers(VIN, [], []))
        reads = json.loads(out[4])["msgs"]
        joined = b"".join(bytes.fromhex(m["data"])[5:] for m in reads)
        self.assertNotIn(VIN.encode(), joined)
        self.assertNotIn(VIN[-6:].encode(), joined)
        self.assertIn(b"X" * 17, joined)
        self.assertIn(b"EU008", joined)               # non-identifying data kept
        for m, f in zip(reads, frames):
            data = bytes.fromhex(m["data"])
            self.assertEqual(len(data), 4 + len(f))   # same length
            self.assertEqual(data[:5], bytes.fromhex("00400003") + f[:1])  # id + ctrl kept
        self.assertEqual(bytes.fromhex(reads[0]["data"])[4:8], frames[0][:4])  # header kept
        self.assertEqual(frames[1][-1:], VIN[:1].encode())       # the 1-byte split case
        self.assertEqual(bytes.fromhex(reads[1]["data"])[-1:], b"X")

    def test_serial_block_masked_other_answers_untouched(self):
        lines, _ = self._log()
        out = san.sanitize_jsonl(lines, san.identifiers(VIN, [], []))
        serial, cont, a6 = (m["data"] for m in json.loads(out[5])["msgs"])
        self.assertEqual(serial, "004000218C11F9F858585858")   # header kept, data masked
        self.assertEqual(cont, "0040002110585858585858585858"[:len(cont)])
        self.assertEqual(a6, "00400021CE11E6003A03F200")       # live value intact

    def test_paths_device_name_and_request_untouched(self):
        lines, _ = self._log()
        out = san.sanitize_jsonl(lines, san.identifiers(VIN, [], []))
        session, opened, conn, write = (json.loads(x) for x in out[:4])
        self.assertNotIn("alice", out[0])
        self.assertIn("C:\\Users\\<user>\\VX\\VXDIAG_J2534.dll", session["real_dll"])
        self.assertEqual(opened["name"], "<device>")
        self.assertEqual(conn["protocol"], 5)
        self.assertEqual(write["msgs"][0]["data"], "000FFFFECB50B9FB00000000")

    def test_labelled_identifiers_from_transcripts(self):
        transcript = f"Vehicle identity\n  VIN    {VIN}\n  FYON   {FYON}\n  Chassis {VIN[-6:]}\n"
        idents = san.identifiers(VIN, [], [transcript])
        self.assertIn(FYON, idents)
        self.assertNotIn(FYON, san.mask_str(transcript, idents))
        # ...and the same FYON is then masked inside a log's data stream
        lines = [connect(1, 5, 500000),
                 rec(ev="read", channel=1,
                     msgs=[msg(0x400003, "CF50F9FB" + FYON.encode().hex()[:8]),
                           msg(0x400003, "10" + FYON.encode().hex()[8:] + "0000")])]
        out = san.sanitize_jsonl(lines, idents)
        self.assertNotIn(FYON.encode(), b"".join(
            bytes.fromhex(m["data"]) for m in json.loads(out[1])["msgs"]))

    def test_numeric_identifier_never_breaks_hex(self):
        # the chassis "912345" must not be replaced inside a hex field as text
        lines = [connect(1, 5, 500000),
                 rec(ev="read", channel=1, msgs=[msg(0x400021, "CE11E6912345FF00")])]
        out = san.sanitize_jsonl(lines, san.identifiers(VIN, [], []))
        data = json.loads(out[1])["msgs"][0]["data"]
        bytes.fromhex(data)                            # still valid hex
        self.assertEqual(data, "00400021CE11E6912345FF00")

    def test_dump_backup(self):
        dump = {"vin": VIN, "reader": "J2534 raw CAN C:\\Users\\alice\\proxy.dll",
                "blocks": {"FB": ("00" * 5 + VIN.encode().hex()), "F8": "A1B2C3D4",
                           "F5": "33313234"}}
        out = san.sanitize_dump(dump, san.identifiers(VIN, [], []))
        self.assertEqual(out["vin"], "X" * 17)
        self.assertNotIn("alice", out["reader"])
        self.assertEqual(out["blocks"]["F8"], "58585858")
        self.assertEqual(out["blocks"]["F5"], "33313234")
        self.assertNotIn(VIN.encode().hex(), out["blocks"]["FB"])

    def test_cli_reports_clean(self):
        lines, _ = self._log()
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "h2-02.jsonl"
            src.write_text("\n".join(lines) + "\n", encoding="utf-8")
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = san.main(["--vin", VIN, "--out", str(Path(d) / "clean"), str(src)])
            report = buf.getvalue()
            self.assertEqual(rc, 0, report)
            # never claims the file is clean, and names what it passed through
            self.assertNotIn("clean\n", report)
            self.assertIn("no known identifier left (unknown ones may remain)", report)
            self.assertIn("NOT a guarantee", report)

    def test_immobiliser_blocks_and_uds_serial_masked(self):
        lines = [connect(1, 5, 500000),
                 rec(ev="read", channel=1, msgs=[
                     msg(0x400003, "CC50F90112345678"),        # B9 01 immo PIN
                     msg(0x400003, "CC50F9F533313234")]),      # B9 F5 part number
                 connect(2, 6, 500000),
                 rec(ev="read", channel=2, msgs=[
                     msg(0x7E8, "62F18C4142433132", 6),        # ECU serial
                     msg(0x7E8, "62F18733313234", 6)])]        # spare part number
        seen = {"b9_blocks": set(), "uds_dids": set()}
        out = san.sanitize_jsonl(lines, san.identifiers(VIN, [], []), seen)
        pin, part = (m["data"] for m in json.loads(out[1])["msgs"])
        serial, spare = (m["data"] for m in json.loads(out[3])["msgs"])
        self.assertEqual(pin, "00400003CC50F90158585858")
        self.assertEqual(part, "00400003CC50F9F533313234")
        self.assertEqual(serial, "000007E862F18C5858585858")
        self.assertEqual(spare, "000007E862F18733313234")
        self.assertEqual(seen, {"b9_blocks": {"F5"}, "uds_dids": {"F187"}})


if __name__ == "__main__":
    unittest.main()
