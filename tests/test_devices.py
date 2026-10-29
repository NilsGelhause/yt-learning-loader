"""Tests for Connect device: the pairing-based reverse device agent
(devices.py + the /api/devices routes + /device-agent.ps1 + /kit serving).

Covers pair-code lifecycle (mint/expiry/single-use), device registration and
token auth, the queue/next/output round-trip with a fake device loop
(including the mirror into AGENT_JOBS that feeds the job transcript UI),
task-script building for the three pre-defined kinds, the UI task endpoint,
the devices list shape, persistence, and the token gate.

Spins a real server.Handler on a temp data dir (mirroring
tests.test_lan.LanServerTests conventions).

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

import devices  # noqa: E402
import server  # noqa: E402


class DeviceServerTests(unittest.TestCase):
    """Pairing, auth, task round-trip, task scripts, list shape, persistence."""

    TOKEN = "s3cret-device-token"

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
            mock.patch.object(devices, "DEVICES_JSON", data / "devices.json"),
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
        self.write_settings({"lan_token": self.TOKEN, "host": "127.0.0.1",
                             "port": self.port})
        devices.DEVICES.clear()
        devices.PAIR_CODES.clear()

    # -- helpers

    def write_settings(self, settings: dict):
        (self.data / "settings.json").write_text(
            json.dumps(settings), encoding="utf-8")

    def api(self, method, path, body=None, headers=None):
        """Send one request on a fresh connection and return (status, data)."""
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        hdrs = dict(headers or {})
        if payload is not None:
            hdrs["Content-Type"] = "application/json"
        hdrs["x-ytl-token"] = self.TOKEN
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(method, path, body=payload, headers=hdrs)
        resp = conn.getresponse()
        raw = resp.read()
        ctype = resp.getheader("Content-Type") or ""
        status = resp.status
        conn.close()
        if "application/json" in ctype:
            return status, json.loads(raw.decode("utf-8"))
        return status, raw

    def pair_device(self, hostname="LAPTOP-TEST"):
        """Mint a code and pair a device — returns (device_id, token)."""
        _, body = self.api("POST", "/api/devices/pair-codes", {})
        code = body["code"]
        status, body = self.api("POST", "/api/devices/pair",
                                {"code": code, "hostname": hostname})
        self.assertEqual(status, 200)
        return body["device_id"], body["token"]

    # -- pair codes

    def test_pair_code_shape_and_expiry_hint(self):
        """A fresh code is 8 chars from the safe alphabet, valid ~10 min."""
        status, body = self.api("POST", "/api/devices/pair-codes", {})
        self.assertEqual(status, 200)
        self.assertRegex(body["code"], r"^[ABCDEFGHJKMNPQRSTUVWXYZ23456789]{8}$")
        self.assertGreater(body["expires_in"], 540)
        self.assertLessEqual(body["expires_in"], 600)

    def test_pair_rejects_bad_code(self):
        """A made-up code pairs nothing."""
        status, body = self.api("POST", "/api/devices/pair",
                                {"code": "NOPE1234", "hostname": "x"})
        self.assertEqual(status, 400)
        self.assertIn("code", body["error"])

    def test_pair_code_is_single_use(self):
        """The same code cannot register two devices."""
        _, body = self.api("POST", "/api/devices/pair-codes", {})
        code = body["code"]
        status, _ = self.api("POST", "/api/devices/pair",
                             {"code": code, "hostname": "one"})
        self.assertEqual(status, 200)
        status, _ = self.api("POST", "/api/devices/pair",
                             {"code": code, "hostname": "two"})
        self.assertEqual(status, 400)

    def test_expired_code_is_rejected(self):
        """A code past its TTL pairs nothing."""
        _, body = self.api("POST", "/api/devices/pair-codes", {})
        code = body["code"]
        devices.PAIR_CODES[code] = time.time() - 1  # wind the clock past it
        status, _ = self.api("POST", "/api/devices/pair",
                             {"code": code, "hostname": "late"})
        self.assertEqual(status, 400)

    def test_register_returns_device_id_and_token(self):
        """A good code yields a device id and an opaque token."""
        device_id, token = self.pair_device()
        self.assertTrue(device_id)
        self.assertTrue(token)
        self.assertNotEqual(device_id, token)

    # -- auth

    def test_next_rejects_wrong_token(self):
        """The device token is required on /next — wrong ones get 401."""
        device_id, _ = self.pair_device()
        status, body = self.api("GET", f"/api/devices/{device_id}/next?token=wrong")
        self.assertEqual(status, 401)
        self.assertIn("auth", body["error"])

    def test_output_rejects_wrong_token(self):
        device_id, _ = self.pair_device()
        status, _ = self.api("POST", f"/api/devices/{device_id}/output",
                             {"token": "wrong", "task_id": "x", "line": "hi"})
        self.assertEqual(status, 401)

    def test_device_routes_behind_lan_gate(self):
        """Without the LAN token even /api/devices is off-limits."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/api/devices")
        resp = conn.getresponse()
        resp.read()
        conn.close()
        self.assertEqual(resp.status, 401)

    # -- full round trip with a fake device loop

    def test_queue_next_output_round_trip(self):
        """Pair → queue create-vm → device fetches it → streams lines → done,
        with the AGENT_JOBS mirror feeding /api/agent/job/<id>."""
        device_id, token = self.pair_device()
        devices.DEVICES[device_id]["stack_done"] = True
        status, body = self.api("POST", f"/api/devices/{device_id}/task", {
            "kind": "create-vm", "params": {"count": 2, "name_prefix": "demo"}})
        self.assertEqual(status, 200)
        task_id = body["task_id"]

        # mirror exists right after queueing (status pending → starting shape)
        status, job = self.api("GET", f"/api/agent/job/{task_id}")
        self.assertEqual(status, 200)
        self.assertEqual(job["backend"], "device")
        self.assertEqual(job["workspace"], None)
        self.assertEqual(job["cmd_preview"], "create 2 VM(s)")

        # the device picks the task up
        status, body = self.api("GET", f"/api/devices/{device_id}/next?token={token}")
        self.assertEqual(status, 200)
        task = body["task"]
        self.assertEqual(task["id"], task_id)
        self.assertEqual(task["kind"], "create-vm")
        self.assertIn("New-LaptopVm.ps1", task["script"])

        # nothing else pending now
        _, body = self.api("GET", f"/api/devices/{device_id}/next?token={token}")
        self.assertIsNone(body["task"])

        # fake device streams two lines, then done
        self.api("POST", f"/api/devices/{device_id}/output",
                 {"token": token, "task_id": task_id, "line": "creating demo-1…"})
        self.api("POST", f"/api/devices/{device_id}/output",
                 {"token": token, "task_id": task_id, "line": "done"})
        status, job = self.api("GET", f"/api/agent/job/{task_id}")
        self.assertEqual(status, 200)
        self.assertEqual(job["status"], "running")
        self.assertEqual([l["x"] for l in job["lines"]], ["creating demo-1…", "done"])

        _, body = self.api("POST", f"/api/devices/{device_id}/output",
                           {"token": token, "task_id": task_id,
                            "done": True, "exit_code": 0})
        self.assertEqual(body, {"ok": True})
        status, job = self.api("GET", f"/api/agent/job/{task_id}")
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["exit_code"], 0)
        self.assertIsNotNone(job["ended"])

    def test_reboot_marker_flagged_on_task_and_list(self):
        """A [REBOOT-REQUIRED] line flags the task (shown under the device)."""
        device_id, token = self.pair_device()
        _, body = self.api("POST", f"/api/devices/{device_id}/task",
                           {"kind": "install-stack"})
        task_id = body["task_id"]
        self.api("POST", f"/api/devices/{device_id}/output",
                 {"token": token, "task_id": task_id,
                  "line": "[REBOOT-REQUIRED] reboot the laptop",
                  "done": True, "exit_code": 42})
        _, listing = self.api("GET", "/api/devices")
        dev = listing["devices"][0]
        flagged = [t for t in dev["tasks"] if t["id"] == task_id]
        self.assertTrue(flagged[0]["reboot_required"])
        self.assertEqual(flagged[0]["status"], "error")  # exit 42 ≠ 0

    def test_done_with_nonzero_exit_is_error(self):
        device_id, token = self.pair_device()
        devices.DEVICES[device_id]["stack_done"] = True
        _, body = self.api("POST", f"/api/devices/{device_id}/task",
                           {"kind": "create-vm"})
        task_id = body["task_id"]
        self.api("POST", f"/api/devices/{device_id}/output",
                 {"token": token, "task_id": task_id, "done": True, "exit_code": 1})
        _, job = self.api("GET", f"/api/agent/job/{task_id}")
        self.assertEqual(job["status"], "error")
        self.assertEqual(job["exit_code"], 1)

    # -- task kinds and scripts

    def test_install_stack_script_markers(self):
        script = devices.build_task_script(
            "install-stack", {"master_url": "http://192.168.1.5:8471"})
        self.assertIn("Install-VmStack.ps1", script)
        self.assertIn("QemuShim.cs", script)
        self.assertIn("http://192.168.1.5:8471/kit/", script)
        self.assertIn("[REBOOT-REQUIRED]", script)

    def test_create_vm_script_uses_params(self):
        script = devices.build_task_script(
            "create-vm", {"master_url": "http://m:8471", "count": 3,
                          "name_prefix": "demo"})
        self.assertIn("New-LaptopVm.ps1", script)
        self.assertIn("-Count 3", script)
        self.assertIn("'demo'", script)
        self.assertIn("'http://m:8471'", script)

    def test_beacon_script_carries_token_device_bound(self):
        script = devices.build_task_script(
            "start-beacon", {"master_url": "http://m:8471", "lan_token": "tk123"})
        self.assertIn("Register-Nodes.ps1", script)
        self.assertIn("'tk123'", script)
        self.assertIn("Start-Process", script)

    def test_unknown_kind_raises(self):
        with self.assertRaises(ValueError):
            devices.build_task_script("format-c", {})

    def test_task_endpoint_rejects_unknown_kind(self):
        device_id, _ = self.pair_device()
        status, body = self.api("POST", f"/api/devices/{device_id}/task",
                                {"kind": "format-c"})
        self.assertEqual(status, 400)
        self.assertIn("kind", body["error"])

    def test_task_endpoint_unknown_device_404(self):
        status, _ = self.api("POST", "/api/devices/nope/task",
                             {"kind": "create-vm"})
        self.assertEqual(status, 404)

    def test_beacon_task_requires_lan_token(self):
        """start-beacon without a configured LAN token is rejected with 400."""
        self.write_settings({"lan_token": "", "host": "127.0.0.1",
                             "port": self.port})
        device_id, _ = self.pair_device()
        devices.DEVICES[device_id]["stack_done"] = True
        status, body = self.api("POST", f"/api/devices/{device_id}/task",
                                {"kind": "start-beacon"})
        self.assertEqual(status, 400)
        self.assertIn("token", body["error"])

    # -- list + persistence

    def test_devices_list_shape_and_online_freshness(self):
        """The list carries hostname/online/tasks; online follows last_seen."""
        device_id, token = self.pair_device(hostname="LAPTOP-A")
        devices.DEVICES[device_id]["stack_done"] = True
        _, body = self.api("POST", f"/api/devices/{device_id}/task",
                           {"kind": "create-vm"})
        _, listing = self.api("GET", "/api/devices")
        self.assertEqual(len(listing["devices"]), 1)
        dev = listing["devices"][0]
        self.assertEqual(dev["hostname"], "LAPTOP-A")
        self.assertTrue(dev["online"])
        self.assertEqual(dev["tasks"][0]["status"], "pending")
        self.assertEqual(dev["tasks"][0]["kind"], "create-vm")

        # a stale last_seen reads as offline
        devices.DEVICES[device_id]["last_seen"] = time.time() - 120
        _, listing = self.api("GET", "/api/devices")
        self.assertFalse(listing["devices"][0]["online"])

    def test_persistence_file_written(self):
        """Devices and their tasks land in data/devices.json."""
        device_id, _ = self.pair_device(hostname="LAPTOP-P")
        devices.DEVICES[device_id]["stack_done"] = True
        self.api("POST", f"/api/devices/{device_id}/task", {"kind": "create-vm"})
        raw = json.loads((self.data / "devices.json").read_text("utf-8"))
        self.assertIn(device_id, raw)
        self.assertEqual(raw[device_id]["hostname"], "LAPTOP-P")
        self.assertEqual(len(raw[device_id]["tasks"]), 1)

    def test_devices_cleared_between_tests(self):
        """Each test starts with a clean registry (no cross-test devices)."""
        _, listing = self.api("GET", "/api/devices")
        self.assertEqual(listing["devices"], [])

    # -- kit + agent script serving

    def test_kit_files_served_as_text(self):
        """/kit/<file> serves the laptop-vm scripts as text/plain."""
        status, body = self.api("GET", "/kit/Install-VmStack.ps1")
        self.assertEqual(status, 200)
        self.assertIn("text/plain", self.headers_of("/kit/Install-VmStack.ps1"))
        self.assertIn(b"QemuShim", body)

    def headers_of(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", path, headers={"x-ytl-token": self.TOKEN})
        resp = conn.getresponse()
        resp.read()
        ctype = resp.getheader("Content-Type") or ""
        conn.close()
        return ctype

    def test_kit_traversal_rejected(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/kit/..%2F..%2Fserver.py",
                     headers={"x-ytl-token": self.TOKEN})
        resp = conn.getresponse()
        resp.read()
        conn.close()
        self.assertIn(resp.status, (400, 404))

    def test_device_agent_script_served(self):
        """/device-agent.ps1 serves the agent (text/plain, no LAN gate — the
        laptop needs it before it has any token)."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/device-agent.ps1")
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8")
        ctype = resp.getheader("Content-Type") or ""
        conn.close()
        self.assertEqual(resp.status, 200)
        self.assertIn("text/plain", ctype)
        self.assertIn("param", raw)
        self.assertIn("MasterUrl", raw)

    # -- wizard gating (stack_done)

    def test_create_vm_locked_until_stack_installed(self):
        """create-vm/beacon tasks are rejected with 409 until an install-stack
        task finished successfully on that device."""
        device_id, _ = self.pair_device()
        for kind in ("create-vm", "start-beacon"):
            status, body = self.api("POST", f"/api/devices/{device_id}/task",
                                    {"kind": kind, "params": {"count": 1}})
            self.assertEqual(status, 409, f"{kind} should be locked")
            self.assertIn("Install VM stack", body["error"])

    def test_stack_done_unlocks_after_successful_install(self):
        """A successful install-stack (no reboot flag) sets stack_done; the
        devices list exposes it and follow-up tasks are accepted."""
        device_id, token = self.pair_device()
        status, body = self.api("POST", f"/api/devices/{device_id}/task",
                                {"kind": "install-stack"})
        self.assertEqual(status, 200)
        task_id = body["task_id"]
        self.api("POST", f"/api/devices/{device_id}/output",
                 {"token": token, "task_id": task_id, "done": True, "exit_code": 0})
        status, body = self.api("GET", "/api/devices")
        dev = next(d for d in body["devices"] if d["id"] == device_id)
        self.assertTrue(dev["stack_done"])
        status, _ = self.api("POST", f"/api/devices/{device_id}/task",
                             {"kind": "create-vm", "params": {"count": 1}})
        self.assertEqual(status, 200)

    def test_reboot_required_keeps_device_locked(self):
        """An install that ends with [REBOOT-REQUIRED] does NOT unlock the
        device — the laptop must reboot and the stack re-verify."""
        device_id, token = self.pair_device()
        _, body = self.api("POST", f"/api/devices/{device_id}/task",
                           {"kind": "install-stack"})
        task_id = body["task_id"]
        self.api("POST", f"/api/devices/{device_id}/output",
                 {"token": token, "task_id": task_id,
                  "line": "done [REBOOT-REQUIRED]", "done": True, "exit_code": 0})
        status, body = self.api("GET", "/api/devices")
        dev = next(d for d in body["devices"] if d["id"] == device_id)
        self.assertFalse(dev["stack_done"])
        status, _ = self.api("POST", f"/api/devices/{device_id}/task",
                             {"kind": "create-vm", "params": {"count": 1}})
        self.assertEqual(status, 409)
