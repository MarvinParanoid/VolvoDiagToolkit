"""Web API safety: the clear-codes write is authorized on the server (not by the
page's confirm()), cross-site / DNS-rebinding requests are refused, and bad
requests can't wedge the single-threaded server. Uses a recording fake backend
on an ephemeral loopback port — no adapter."""

import http.client
import json
import socket
import threading
import time
import unittest

from volvo_diag import web


class RecordingBackend(web.FakeBackend):
    def __init__(self, writes):
        super().__init__()
        self._writes = writes
        self.clears = 0

    def writes_enabled(self):
        return self._writes

    def clear_dtcs(self):
        self.clears += 1          # a real backend would transmit AF 11 here
        return {"bus": "hs", "cleared": ["ECM"], "failed": []}


class Server:
    def __init__(self, backend, **kw):
        self.backend = backend
        self.httpd = web.make_server(backend, host="127.0.0.1", port=0, **kw)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        hdrs = {"Host": f"127.0.0.1:{self.port}"}
        hdrs.update(headers or {})
        conn.request(method, path, body=body, headers=hdrs)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        try:
            return resp.status, json.loads(data or b"null")
        except ValueError:
            return resp.status, None

    def post_json(self, path, obj=None, headers=None):
        h = {"Content-Type": "application/json"}
        h.update(headers or {})
        return self.request("POST", path, json.dumps({} if obj is None else obj), h)

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class WebSafetyTest(unittest.TestCase):
    def _server(self, writes=True, **kw):
        srv = Server(RecordingBackend(writes), **kw)
        self.addCleanup(srv.close)
        return srv

    def test_clear_refused_server_side_when_writes_disabled(self):
        srv = self._server(writes=False)
        status, body = srv.post_json("/dtc/clear")
        self.assertEqual(status, 403)
        self.assertIn("disabled", body["error"])
        self.assertEqual(srv.backend.clears, 0)

    def test_clear_allowed_same_origin_with_writes(self):
        srv = self._server(writes=True)
        status, body = srv.post_json(
            "/dtc/clear", headers={"Origin": f"http://127.0.0.1:{srv.port}"})
        self.assertEqual(status, 200)
        self.assertEqual(body["cleared"], ["ECM"])
        self.assertEqual(srv.backend.clears, 1)

    def test_cross_site_form_post_refused(self):
        # What an <form method=post> on any website would send.
        srv = self._server(writes=True)
        status, _ = srv.request("POST", "/dtc/clear", "",
                                {"Content-Type": "application/x-www-form-urlencoded",
                                 "Origin": "https://evil.example"})
        self.assertEqual(status, 415)
        status, _ = srv.request("POST", "/dtc/clear", "", {"Content-Type": "text/plain"})
        self.assertEqual(status, 415)
        self.assertEqual(srv.backend.clears, 0)

    def test_foreign_origin_refused_even_with_json(self):
        srv = self._server(writes=True)
        for origin in ("https://evil.example", "null"):
            status, _ = srv.post_json("/dtc/clear", headers={"Origin": origin})
            self.assertEqual(status, 403)
        self.assertEqual(srv.backend.clears, 0)

    def test_dns_rebinding_host_refused(self):
        srv = self._server(writes=True)
        evil = {"Host": f"attacker.example:{srv.port}",
                "Origin": f"http://attacker.example:{srv.port}"}
        status, _ = srv.post_json("/dtc/clear", headers=evil)
        self.assertEqual(status, 403)
        status, _ = srv.request("GET", "/config", headers={"Host": "attacker.example"})
        self.assertEqual(status, 403)
        self.assertEqual(srv.backend.clears, 0)

    def test_other_posts_also_guarded(self):
        srv = self._server()
        status, _ = srv.request("POST", "/bus", '{"id":"ls"}', {"Content-Type": "text/plain"})
        self.assertEqual(status, 415)
        self.assertEqual(srv.backend.current_bus(), "hs")
        status, body = srv.post_json("/bus", {"id": "ls"})
        self.assertEqual((status, body["ok"]), (200, True))

    def test_remote_client_check(self):
        self.assertTrue(web._is_loopback("127.0.0.1"))
        self.assertTrue(web._is_loopback("::1"))
        self.assertFalse(web._is_loopback("192.168.1.20"))
        self.assertFalse(web._is_loopback("not-an-ip"))

    def test_host_header_rules(self):
        ok = ["127.0.0.1:8080", "localhost:8080", "[::1]:8080", "192.168.1.5:8080",
              "localhost", "mybox:8080"]
        for h in ok[:-1]:
            self.assertTrue(web._host_allowed(h, "0.0.0.0"), h)
        self.assertTrue(web._host_allowed("mybox:8080", "mybox"))   # the --host name
        for h in ("", "evil.example", "evil.example:8080"):
            self.assertFalse(web._host_allowed(h, "0.0.0.0"), h)

    def test_bad_and_oversized_bodies(self):
        srv = self._server()
        status, _ = srv.request("POST", "/select", "x",
                                {"Content-Type": "application/json", "Content-Length": "-5"})
        self.assertEqual(status, 400)
        status, _ = srv.request("POST", "/select", "x",
                                {"Content-Type": "application/json",
                                 "Content-Length": str(web.MAX_BODY + 1)})
        self.assertEqual(status, 413)
        # the server is still serving afterwards
        self.assertEqual(srv.request("GET", "/meta")[0], 200)

    def test_stalled_client_does_not_wedge_server(self):
        old = web.REQUEST_TIMEOUT
        web.REQUEST_TIMEOUT = 0.5
        self.addCleanup(setattr, web, "REQUEST_TIMEOUT", old)
        srv = self._server()
        idle = socket.create_connection(("127.0.0.1", srv.port))   # sends nothing
        self.addCleanup(idle.close)
        t0 = time.monotonic()
        status, _ = srv.request("GET", "/meta")
        self.assertEqual(status, 200)
        self.assertLess(time.monotonic() - t0, 4)

    def test_clear_from_network_client_refused_by_default(self):
        from unittest import mock
        srv = self._server(writes=True)
        with mock.patch.object(web, "_is_loopback", return_value=False):
            status, body = srv.post_json("/dtc/clear")
        self.assertEqual(status, 403)
        self.assertIn("--allow-remote-writes", body["error"])
        self.assertEqual(srv.backend.clears, 0)

    def test_clear_from_network_client_with_explicit_opt_in(self):
        from unittest import mock
        srv = self._server(writes=True, allow_remote_writes=True)
        with mock.patch.object(web, "_is_loopback", return_value=False):
            status, _ = srv.post_json("/dtc/clear")
        self.assertEqual(status, 200)
        self.assertEqual(srv.backend.clears, 1)

    def test_page_posts_json(self):
        # The page itself must still work with the guard: every POST is JSON.
        self.assertIn("if(method==='POST'){x.setRequestHeader('Content-Type','application/json')",
                      web.PAGE)


class BackendClearTest(unittest.TestCase):
    """VolvoBackend.clear_dtcs used to send AF 11 and then raise AttributeError
    (self._miss did not exist), so a successful write was reported as failed."""

    def test_clear_reports_result_after_writing(self):
        import argparse
        from unittest import mock

        from volvo_diag.backend import VolvoBackend
        from volvo_diag.poll import PollScheduler
        from volvo_diag.volvo import parameters as pdb

        db = pdb.load_profile(pdb.default_path(), None)
        b = VolvoBackend.__new__(VolvoBackend)
        b.args = argparse.Namespace(enable_writes=True, transport="j2534")
        b.db = db
        b._bus = db.primary_bus().id
        b._ecm = mock.MagicMock()
        b._ecm.clear_dtcs.return_value = True
        b._poller = PollScheduler(lambda p, t: 0)
        b._poller._miss["x"] = 5
        result = b.clear_dtcs()
        self.assertNotIn("error", result)
        self.assertTrue(result["cleared"])
        self.assertTrue(b._ecm.clear_dtcs.called)
        self.assertEqual(b._poller._miss, {})


class DtcScanReportingTest(unittest.TestCase):
    """A module that does not answer the DTC read is reported, so an
    unverified clear can't show as "no codes"."""

    def test_silent_module_is_listed(self):
        import argparse
        from unittest import mock

        from volvo_diag.backend import VolvoBackend
        from volvo_diag.transport.base import TransportError
        from volvo_diag.volvo import parameters as pdb

        db = pdb.load_profile(pdb.default_path(), None)
        b = VolvoBackend.__new__(VolvoBackend)
        b.args = argparse.Namespace(enable_writes=False, transport="j2534")
        b.db = db
        b._bus = db.primary_bus().id
        b._ecm = mock.MagicMock()
        b._ecm.read_dtcs.side_effect = TransportError("silent")
        result = b.read_dtcs()
        self.assertEqual(result["dtcs"], [])
        self.assertTrue(result["no_answer"])

    def test_page_renders_silent_modules(self):
        self.assertIn("No answer from: ", web.PAGE)


if __name__ == "__main__":
    unittest.main()
