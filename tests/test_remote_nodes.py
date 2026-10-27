"""Tests for remote-node listing: POST /api/nodes/register (the laptop-vm
beacon's target) and the merge of fresh registrations into build_network().

Conventions mirror tests.test_console_network: a real server.Handler on an
ephemeral port with patched path globals, plus StubAgentd instances as remote
agent endpoints.

Run via tests/run.py.
"""

from __future__ import annotations

import http.client
import json
import tempfile
import threading
import time
import unittest
import unittest.mock as mock
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402
from tests.test_agentd import StubAgentd  # noqa: E402


class RemoteNodeServerTests(unittest.TestCase):
    """server.py wiring for the remote-node registry and network merge."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        data = root / "data"
        for d in ("learnings", "transcripts", "thumbs", "info", "progress"):
            (data / d).mkdir(parents=True)
        (root / "teach").mkdir()
        static = root / "static"
        static.mkdir()
        (static / "index.html").write_text("<html>ok</html>", encoding="utf-8")
        cls.root = root
        cls.data = data
        (data / "settings.json").write_text("{}", encoding="utf-8")

        cls.patchers = [
            mock.patch.object(server, "SETTINGS_JSON", data / "settings.json"),
            mock.patch.object(server, "REMOTE_NODES_JSON", data / "remote_nodes.json"),
            mock.patch.object(server, "AGENT_CONVERSATIONS_JSON",
                              data / "agent" / "conversations.json"),
            mock.patch.object(server, "TEACH_DIR", root / "teach"),
            mock.patch.object(server.cua_client, "find_cua", return_value=None),
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
        with server.REMOTE_NODES_LOCK:
            server.REMOTE_NODES.clear()
        registry = self.data / "remote_nodes.json"
        if registry.exists():
            registry.unlink()

    def tearDown(self):
        with server.REMOTE_NODES_LOCK:
            server.REMOTE_NODES.clear()
        registry = self.data / "remote_nodes.json"
        if registry.exists():
            registry.unlink()

    def api(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        hdrs = dict(headers or {})
        if payload is not None:
            hdrs["Content-Type"] = "application/json"
        conn.request(method, path, body=payload, headers=hdrs)
        resp = conn.getresponse()
        raw = resp.read()
        status = resp.status
        conn.close()
        return status, json.loads(raw.decode("utf-8"))

    def register(self, nodes, host="laptop-2", headers=None):
        return self.api("POST", "/api/nodes/register",
                        {"host": host, "nodes": nodes}, headers=headers)

    def remote_node(self, name, **kw):
        node = {"name": name, "kind": "vm", "state": "running"}
        node.update(kw)
        return node

    # -- POST /api/nodes/register

    def test_register_garbage_is_400(self):
        """A body without a list of nodes is rejected."""
        for body in (None, {}, {"nodes": "laptop-vm-1"},
                      {"nodes": {"name": "x"}}, ["not-a-dict"]):
            status, _ = self.api("POST", "/api/nodes/register", body)
            self.assertEqual(status, 400, body)

    def test_register_invalid_json_is_400(self):
        """Non-JSON bodies are a 400, not a 500."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", "/api/nodes/register", body=b"{nope",
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        resp.read()
        self.assertEqual(resp.status, 400)
        conn.close()

    def test_register_bad_names_skipped_and_counted(self):
        """Entries whose name does not match the pattern are skipped; the
        response counts only what registered."""
        status, body = self.register([
            self.remote_node("laptop-vm-1"),
            self.remote_node("Bad_Name"),
            self.remote_node("-lead"),
            self.remote_node(""),
            "not-a-dict",
            self.remote_node("x" * 62),  # too long
            self.remote_node("ok-box"),
        ])
        self.assertEqual(status, 200)
        self.assertEqual(body, {"ok": True, "registered": 2})
        with server.REMOTE_NODES_LOCK:
            self.assertEqual(sorted(server.REMOTE_NODES), ["laptop-vm-1", "ok-box"])

    def test_register_bad_endpoint_dropped_presence_kept(self):
        """A malformed endpoint is dropped but the node still registers as
        presence-only."""
        status, body = self.register([
            self.remote_node("e1", endpoint="ftp://nope"),
            self.remote_node("e2", endpoint="http://" + "x" * 220),
            self.remote_node("e3", endpoint="http://192.168.50.7:8472"),
        ])
        self.assertEqual(body["registered"], 3)
        with server.REMOTE_NODES_LOCK:
            self.assertIsNone(server.REMOTE_NODES["e1"]["endpoint"])
            self.assertIsNone(server.REMOTE_NODES["e2"]["endpoint"])
            self.assertEqual(server.REMOTE_NODES["e3"]["endpoint"],
                             "http://192.168.50.7:8472")

    def test_register_upsert_and_host_stamp(self):
        """Re-registering a name updates the entry (no duplicates) and
        re-stamps host/seen; host defaults to 'remote'."""
        self.register([self.remote_node("vm-a")], host="laptop-2")
        with server.REMOTE_NODES_LOCK:
            first_seen = server.REMOTE_NODES["vm-a"]["seen"]
        time.sleep(0.02)
        status, body = self.register([self.remote_node("vm-a", state="suspended")],
                                     host="laptop-3")
        self.assertEqual(body["registered"], 1)
        with server.REMOTE_NODES_LOCK:
            self.assertEqual(list(server.REMOTE_NODES), ["vm-a"])
            entry = server.REMOTE_NODES["vm-a"]
        self.assertEqual(entry["host"], "laptop-3")
        self.assertEqual(entry["state"], "suspended")
        self.assertGreater(entry["seen"], first_seen)

        status, body = self.api("POST", "/api/nodes/register",
                                {"nodes": [self.remote_node("vm-b")]})
        self.assertEqual(status, 200)
        with server.REMOTE_NODES_LOCK:
            self.assertEqual(server.REMOTE_NODES["vm-b"]["host"], "remote")

    def test_register_persists_registry(self):
        """Each batch is written to data/remote_nodes.json."""
        self.register([self.remote_node("vm-a"), self.remote_node("vm-b")])
        registry = self.data / "remote_nodes.json"
        self.assertTrue(registry.exists())
        data = json.loads(registry.read_text(encoding="utf-8"))
        self.assertEqual(sorted(data), ["vm-a", "vm-b"])
        self.assertEqual(data["vm-a"]["host"], "laptop-2")
        self.assertIsInstance(data["vm-a"]["seen"], float)

    def test_register_prunes_stale_entries(self):
        """An entry whose seen is older than the TTL is pruned on the next
        read/write of the registry."""
        self.register([self.remote_node("fresh")])
        with server.REMOTE_NODES_LOCK:
            server.REMOTE_NODES["stale"] = {
                "host": "laptop-2", "name": "stale", "kind": "vm",
                "state": "running", "endpoint": None,
                "seen": time.time() - server.REMOTE_NODE_TTL - 1,
            }
        self.register([self.remote_node("fresh-2")])
        with server.REMOTE_NODES_LOCK:
            self.assertEqual(sorted(server.REMOTE_NODES), ["fresh", "fresh-2"])
        # and a stale entry never reaches the network view
        with server.REMOTE_NODES_LOCK:
            server.REMOTE_NODES["stale"] = {
                "host": "laptop-2", "name": "stale", "kind": "vm",
                "state": "running", "endpoint": None,
                "seen": time.time() - server.REMOTE_NODE_TTL - 1,
            }
        _, body = self.api("GET", "/api/network")
        self.assertNotIn("remote-stale", [n["id"] for n in body["nodes"]])

    def test_register_behind_token_gate(self):
        """With a LAN token set, registration needs the token like every
        other /api/ route."""
        (self.data / "settings.json").write_text(
            json.dumps({"lan_token": "sekrit"}), encoding="utf-8")
        try:
            status, _ = self.register([self.remote_node("vm-a")])
            self.assertEqual(status, 401)
            status, body = self.register([self.remote_node("vm-a")],
                                         headers={"x-ytl-token": "sekrit"})
            self.assertEqual(status, 200)
            self.assertEqual(body["registered"], 1)
        finally:
            (self.data / "settings.json").write_text("{}", encoding="utf-8")

    # -- build_network() merge

    def test_fresh_registration_appears_as_remote_node(self):
        """A fresh registration shows up as remote-<name> with remote, host,
        flavor (qemu for vm/qemu kinds) and vm_state."""
        self.register([self.remote_node("laptop-vm-1", state="running")])
        _, body = self.api("GET", "/api/network")
        remotes = [n for n in body["nodes"] if n.get("remote")]
        self.assertEqual(len(remotes), 1)
        node = remotes[0]
        self.assertEqual(node["id"], "remote-laptop-vm-1")
        self.assertEqual(node["name"], "laptop-vm-1")
        self.assertEqual(node["kind"], "vm")
        self.assertEqual(node["host"], "laptop-2")
        self.assertEqual(node["flavor"], "qemu")
        self.assertEqual(node["vm_state"], "running")
        self.assertTrue(node["online"])       # fresh, no endpoint → presence
        self.assertFalse(node["agent"])
        self.assertEqual(node["sessions"], [])

    def test_remote_flavor_container_for_other_kinds(self):
        """A registration with kind 'container' maps to flavor 'container'."""
        self.register([self.remote_node("box-1", kind="container")])
        _, body = self.api("GET", "/api/network")
        node = [n for n in body["nodes"] if n.get("remote")][0]
        self.assertEqual(node["flavor"], "container")

    def test_remote_shadowing_local_name_ignored(self):
        """A remote registration whose name matches a local cua sandbox does
        not produce a second card."""
        with mock.patch.object(server.cua_client, "find_cua",
                               return_value="/fake/cua"), \
             mock.patch.object(server.cua_client, "list_sandboxes", return_value=[
                 {"name": "ytl-x", "status": "running", "image": "", "kind": "vm"}]):
            self.register([self.remote_node("ytl-x")])
            _, body = self.api("GET", "/api/network")
        self.assertEqual([n["id"] for n in body["nodes"]], ["local", "vm-ytl-x"])

    def test_remote_endpoint_healthy_lists_conversations(self):
        """A fresh registration with a reachable endpoint merges the
        endpoint's /conversations in the same shape as local VM nodes."""
        stub = StubAgentd()
        try:
            stub.conversations = [
                {"key": "console:main", "id": "c1", "cwd": "/home/cua",
                 "busy": True, "lastStatus": "running"},
            ]
            self.register([self.remote_node("laptop-vm-1",
                                            endpoint=stub.base)])
            _, body = self.api("GET", "/api/network")
            node = [n for n in body["nodes"] if n.get("remote")][0]
            self.assertTrue(node["online"])
            self.assertTrue(node["agent"])
            self.assertTrue(node["has_endpoint"])
            self.assertEqual(len(node["sessions"]), 1)
            sess = node["sessions"][0]
            self.assertEqual(sess["key"], "console:main")
            self.assertEqual(sess["type"], "console")
            self.assertEqual(sess["title"], "Console")
            self.assertTrue(sess["busy"])
            self.assertEqual(sess["status"], "running")
            self.assertEqual(sess["cwd"], "/home/cua")
        finally:
            stub.stop()

    def test_remote_endpoint_unreachable_still_listed(self):
        """A fresh registration with a dead endpoint stays listed with
        online=False — presence is independent of the endpoint."""
        self.register([self.remote_node("laptop-vm-1",
                                        endpoint="http://127.0.0.1:1")])
        _, body = self.api("GET", "/api/network")
        node = [n for n in body["nodes"] if n.get("remote")][0]
        self.assertFalse(node["online"])
        self.assertFalse(node["agent"])
        self.assertEqual(node["sessions"], [])

    def test_remote_seen_too_old_is_offline_but_listed(self):
        """Within the TTL but beyond the 90s online window: still listed
        (presence), just offline."""
        self.register([self.remote_node("laptop-vm-1")])
        with server.REMOTE_NODES_LOCK:
            server.REMOTE_NODES["laptop-vm-1"]["seen"] = (
                time.time() - server.REMOTE_ONLINE_WINDOW - 5)
        _, body = self.api("GET", "/api/network")
        node = [n for n in body["nodes"] if n.get("remote")][0]
        self.assertFalse(node["online"])

    def test_remote_gone_stale_is_dropped(self):
        """Older than the TTL: pruned from the registry and the network view."""
        self.register([self.remote_node("laptop-vm-1")])
        with server.REMOTE_NODES_LOCK:
            server.REMOTE_NODES["laptop-vm-1"]["seen"] = (
                time.time() - server.REMOTE_NODE_TTL - 1)
        _, body = self.api("GET", "/api/network")
        self.assertNotIn("remote-laptop-vm-1", [n["id"] for n in body["nodes"]])
        with server.REMOTE_NODES_LOCK:
            self.assertNotIn("laptop-vm-1", server.REMOTE_NODES)

    def test_remote_nodes_capped_at_20(self):
        """At most REMOTE_NODE_MAX remote cards are merged."""
        self.register([self.remote_node(f"vm-{i:02d}") for i in range(25)])
        _, body = self.api("GET", "/api/network")
        remotes = [n for n in body["nodes"] if n.get("remote")]
        self.assertEqual(len(remotes), server.REMOTE_NODE_MAX)

    def test_no_registrations_no_remote_nodes(self):
        """An empty registry adds nothing to the network view."""
        _, body = self.api("GET", "/api/network")
        self.assertEqual([n["id"] for n in body["nodes"]], ["local"])
