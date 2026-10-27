"""Tests for LAN client mode: the lan_token gate on every /api/ route, the
/api/lan status endpoint, and the settings merge that gives existing installs
the host/lan_token defaults.

Spins a real server.Handler on a temp data dir (mirroring
tests.test_console_network.AgentdServerTests conventions).

Run via tests/run.py.
"""

from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
import unittest.mock as mock
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402


class LanServerTests(unittest.TestCase):
    """Token auth, /api/lan shape, and settings merge on a temp data dir."""

    TOKEN = "s3cret-token"

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        data = root / "data"
        for d in ("learnings", "transcripts", "thumbs", "info", "progress"):
            (data / d).mkdir(parents=True)
        (root / "teach").mkdir()
        cls.root = root
        cls.data = data

        cls.patchers = [
            mock.patch.object(server, "SETTINGS_JSON", data / "settings.json"),
        ]
        for p in cls.patchers:
            p.start()

        cls.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)
        cls.httpd.server_close()
        for p in cls.patchers:
            p.stop()
        cls.tmp.cleanup()

    def setUp(self):
        self.write_settings({})

    def api(self, method, path, headers=None):
        """Send one request on a fresh connection and return (status, data)."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(method, path, headers=headers or {})
        resp = conn.getresponse()
        raw = resp.read()
        ctype = resp.getheader("Content-Type") or ""
        status = resp.status
        conn.close()
        if "application/json" in ctype:
            return status, json.loads(raw.decode("utf-8"))
        return status, raw

    def write_settings(self, settings: dict):
        (self.data / "settings.json").write_text(
            json.dumps(settings), encoding="utf-8")

    # -- token auth

    def test_state_401_without_token(self):
        """With lan_token set, an unauthenticated /api/state is rejected 401
        with the token_required error."""
        self.write_settings({"lan_token": self.TOKEN})
        status, body = self.api("GET", "/api/state")
        self.assertEqual(status, 401)
        self.assertEqual(body, {"error": "token_required"})

    def test_state_401_with_wrong_token(self):
        """A wrong token is rejected just like a missing one."""
        self.write_settings({"lan_token": self.TOKEN})
        status, body = self.api("GET", "/api/state",
                                {"x-ytl-token": "wrong"})
        self.assertEqual(status, 401)
        self.assertEqual(body, {"error": "token_required"})

    def test_state_200_with_header_token(self):
        """The correct x-ytl-token header unlocks the API."""
        self.write_settings({"lan_token": self.TOKEN})
        status, _ = self.api("GET", "/api/state",
                             {"x-ytl-token": self.TOKEN})
        self.assertEqual(status, 200)

    def test_state_200_with_query_token(self):
        """The correct ?token= query param unlocks the API too."""
        self.write_settings({"lan_token": self.TOKEN})
        status, _ = self.api("GET", f"/api/state?token={self.TOKEN}")
        self.assertEqual(status, 200)

    def test_empty_token_means_no_auth(self):
        """Empty lan_token (the default) keeps today's open behavior."""
        status, _ = self.api("GET", "/api/state")
        self.assertEqual(status, 200)

    def test_token_applies_to_post_routes(self):
        """The gate covers mutating routes, not just GETs."""
        self.write_settings({"lan_token": self.TOKEN})
        status, body = self.api("POST", "/api/settings")
        self.assertEqual(status, 401)
        self.assertEqual(body, {"error": "token_required"})
        status, _ = self.api("POST", "/api/settings",
                             {"x-ytl-token": self.TOKEN})
        self.assertEqual(status, 400)  # past the gate, fails on the empty body

    # -- /api/lan

    def test_lan_empty_urls_for_localhost(self):
        """host 127.0.0.1 -> no LAN urls (loopback-only serving)."""
        self.write_settings({"host": "127.0.0.1", "port": self.port})
        status, body = self.api("GET", "/api/lan")
        self.assertEqual(status, 200)
        self.assertEqual(body["host"], "127.0.0.1")
        self.assertEqual(body["urls"], [])
        self.assertFalse(body["token_set"])

    def test_lan_urls_for_wildcard_host(self):
        """host 0.0.0.0 -> one http://ip:port url per enumerated address."""
        self.write_settings({"host": "0.0.0.0", "port": self.port,
                             "lan_token": self.TOKEN})
        with mock.patch.object(server, "lan_ipv4_addresses",
                               return_value=["192.168.1.50", "10.0.0.7"]):
            status, body = self.api("GET", "/api/lan",
                                    {"x-ytl-token": self.TOKEN})
        self.assertEqual(status, 200)
        self.assertEqual(body["host"], "0.0.0.0")
        self.assertEqual(body["urls"],
                         [f"http://192.168.1.50:{self.port}",
                          f"http://10.0.0.7:{self.port}"])
        self.assertTrue(body["token_set"])

    def test_lan_never_leaks_token(self):
        """The response carries only token_set — never the token value."""
        self.write_settings({"host": "0.0.0.0", "lan_token": self.TOKEN})
        with mock.patch.object(server, "lan_ipv4_addresses",
                               return_value=["192.168.1.50"]):
            status, body = self.api("GET", "/api/lan",
                                    {"x-ytl-token": self.TOKEN})
        self.assertEqual(status, 200)
        self.assertNotIn("lan_token", body)
        self.assertNotIn(self.TOKEN, json.dumps(body))
        self.assertTrue(body["token_set"])

    # -- bind behavior (launch.py probes 127.0.0.1)

    def test_wildcard_bind_includes_loopback(self):
        """A 0.0.0.0 bind still serves loopback, so launch.py's 127.0.0.1
        probe keeps working when LAN mode is on."""
        httpd = server.ThreadingHTTPServer(("0.0.0.0", 0), server.Handler)
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request("GET", "/api/state")
            resp = conn.getresponse()
            resp.read()
            conn.close()
            self.assertEqual(resp.status, 200)
        finally:
            httpd.shutdown()
            thread.join(timeout=5)
            httpd.server_close()

    # -- settings merge

    def test_settings_merge_gains_lan_defaults(self):
        """A stored settings.json without host/lan_token gains the defaults,
        and existing keys are untouched."""
        self.write_settings({"port": 9000, "theme": "light"})
        settings = server.load_settings()
        self.assertEqual(settings["host"], "127.0.0.1")
        self.assertEqual(settings["lan_token"], "")
        self.assertEqual(settings["port"], 9000)
        self.assertEqual(settings["theme"], "light")

    # -- master vs client

    def test_client_endpoint_reports_master_on_loopback(self):
        """A browser on the server machine is the master."""
        status, body = self.api("GET", "/api/client")
        self.assertEqual(status, 200)
        self.assertTrue(body["master"])
        self.assertEqual(body["ip"], "127.0.0.1")

    def test_master_check_ip_logic(self):
        """Loopback addresses are master; LAN addresses are clients."""
        class Stub:
            def __init__(self, ip):
                self.client_address = (ip, 51234)
        is_master = server.Handler.is_master_client
        self.assertTrue(is_master(Stub("127.0.0.1")))
        self.assertTrue(is_master(Stub("::1")))
        self.assertFalse(is_master(Stub("192.168.1.50")))
        self.assertFalse(is_master(Stub("10.0.0.7")))


if __name__ == "__main__":
    unittest.main()
