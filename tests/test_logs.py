"""Log parsing, request pairing and diffing."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from volvo_diag.logs import parser
from volvo_diag.logs.summarize import collect


def msg(data: str, *, rx_status: int = 0, proto: int = 6) -> dict:
    return {"proto": proto, "rx_status": rx_status, "tx_flags": 64, "ts": 0,
            "len": len(data) // 2, "extra": 0, "data": data}


def write_log(directory: Path, name: str, events: list[dict]) -> Path:
    path = directory / name
    with path.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps({"ev": "session", "t": 0, "pid": 1, "bits": 32}) + "\n")
        for index, event in enumerate(events, start=1):
            handle.write(json.dumps({**event, "n": index}) + "\n")
    return path


def exchange_log(directory: Path, name: str, pairs: list[tuple[str, str]]) -> Path:
    """Builds a log of request/response pairs on channel 3."""
    events = []
    clock = 0
    for request, response in pairs:
        clock += 1000
        events.append({"ev": "write", "mono": clock, "channel": 3, "requested": 1,
                       "written": 1, "result": 0, "msgs": [msg("000007E0" + request)]})
        if response:
            clock += 8000
            events.append({"ev": "read", "mono": clock, "channel": 3, "returned": 1,
                           "result": 0, "msgs": [msg("000007E8" + response)]})
    return write_log(directory, name, events)


class ParserTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_frames_split_can_id_from_payload(self):
        path = exchange_log(self.dir, "a.jsonl", [("22F190", "62F1905956")])
        frames = parser.load(path).frames
        self.assertEqual([f.direction for f in frames], ["tx", "rx"])
        self.assertEqual(frames[0].can_id, 0x7E0)
        self.assertEqual(frames[0].payload.hex().upper(), "22F190")
        self.assertEqual(frames[1].can_id, 0x7E8)

    def test_truncated_last_line_is_ignored(self):
        path = exchange_log(self.dir, "a.jsonl", [("22F190", "62F190AA")])
        with path.open("a", encoding="utf-8") as handle:
            handle.write('{"ev":"write","channel":3,')  # VIDA died mid-write
        log = parser.load(path)
        self.assertEqual(len(log.frames), 2)

    def test_request_keys(self):
        self.assertEqual(parser.request_key(bytes.fromhex("22D123")), "22D123")
        self.assertEqual(parser.request_key(bytes.fromhex("010C")), "010C")
        self.assertEqual(parser.request_key(bytes.fromhex("19020C")), "1902")
        self.assertEqual(parser.request_key(bytes.fromhex("3E00")), "3E00")
        # A DID write keeps the identifier but drops the payload.
        self.assertEqual(parser.request_key(bytes.fromhex("2ED12300FF")), "2ED123")

    def test_response_keys(self):
        self.assertEqual(parser.response_key(bytes.fromhex("62D12303F2")), "22D123")
        self.assertEqual(parser.response_key(bytes.fromhex("7F2231")), "22")
        self.assertIsNone(parser.response_key(bytes.fromhex("22D123")))  # a request, not an answer

    def test_pairing_and_latency(self):
        path = exchange_log(self.dir, "a.jsonl", [("22D123", "62D12303F2"), ("22D123", "7F2231")])
        exchanges = parser.pair(parser.load(path).frames)
        self.assertEqual(len(exchanges), 2)
        self.assertTrue(exchanges[0].ok)
        self.assertEqual(exchanges[0].data.hex().upper(), "03F2")
        self.assertEqual(exchanges[0].latency_us, 8000)
        self.assertFalse(exchanges[1].ok)
        self.assertEqual(exchanges[1].nrc, 0x31)

    def test_loopback_frames_are_not_answers(self):
        events = [
            {"ev": "write", "mono": 1000, "channel": 3, "result": 0,
             "msgs": [msg("000007E022F190")]},
            # The adapter echoes our own frame back with TX_MSG_TYPE set.
            {"ev": "read", "mono": 1200, "channel": 3, "result": 0,
             "msgs": [msg("000007E022F190", rx_status=1)]},
            {"ev": "read", "mono": 2000, "channel": 3, "result": 0,
             "msgs": [msg("000007E862F19041")]},
        ]
        path = write_log(self.dir, "a.jsonl", events)
        exchanges = parser.pair(parser.load(path).frames)
        self.assertEqual(len(exchanges), 1)
        self.assertEqual(exchanges[0].data.hex().upper(), "41")

    def test_collect_counts_and_rate(self):
        pairs = [("22D123", "62D1230001")] * 5
        path = exchange_log(self.dir, "a.jsonl", pairs)
        stats = collect(parser.pair(parser.load(path).frames))
        (entry,) = stats.values()
        self.assertEqual(entry.count, 5)
        self.assertEqual(entry.answered, 5)
        self.assertGreater(entry.rate_hz, 0)


class DiffTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_new_request_is_reported(self):
        from volvo_diag.logs import diff

        baseline = exchange_log(self.dir, "base.jsonl", [("010C", "410C0C00")] * 3)
        candidate = exchange_log(
            self.dir, "boost.jsonl",
            [("010C", "410C0C00"), ("22D123", "62D12303F2"), ("22D123", "62D12303F5")],
        )
        before = diff._by_key(baseline)
        after = diff._by_key(candidate)
        self.assertNotIn("22D123", before)
        self.assertIn("22D123", after)
        self.assertEqual(after["22D123"].count, 2)
        self.assertEqual(diff.main([str(baseline), str(candidate)]), 0)

    def test_response_length_change_is_visible(self):
        from volvo_diag.logs import diff

        baseline = exchange_log(self.dir, "base.jsonl", [("22D100", "62D1000001")])
        candidate = exchange_log(self.dir, "more.jsonl", [("22D100", "62D10000010002")])
        before = diff._by_key(baseline)
        after = diff._by_key(candidate)
        self.assertNotEqual(before["22D100"].response_lengths, after["22D100"].response_lengths)


if __name__ == "__main__":
    unittest.main()
