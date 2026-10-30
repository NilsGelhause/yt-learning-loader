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

    # -- install-agent (agentd sidecar into a laptop VM)

    def test_install_agent_script_markers(self):
        """The generated laptop script ships the tgz (guest curl), keys
        (base64 via node), starts and health checks the guest — LF-only,
        key base64'd never printed, NO `cua sb cp` anywhere (it fails on
        the recreated QEMU VM) and NO double quotes in any exec argument
        (PS 5.1 strips them; the JS uses backtick template literals)."""
        script = devices.build_task_script(
            "install-agent", {"master_url": "http://m:8471",
                              "vm_name": "laptop-vm-1", "kimi_key": "sk-test"})
        for marker in ("agentd.tgz", "setsid", "0o600", "0o755",
                       "Contains('ok')", "Contains('true')", "node -e",
                       "curl -sL -o /home/cua/agentd.tgz",
                       "ToBase64String", "Buffer.from", "chmodSync",
                       "'KIMI_API_KEY=' + $key",
                       "-replace \"`r`n\", \"`n\""):
            self.assertIn(marker, script)
        # guest here-string must not carry CR (CRLF start script crashed sh)
        self.assertNotIn("\r", script)
        # transport is exec-only; no cp, and no double quote inside any
        # guest command (PS 5.1 strips/mangles them on native arg passing)
        self.assertNotIn("cua sb cp", script)
        for line in script.split("\n"):
            s = line.strip()
            if not s.startswith("cua sb exec"):
                continue
            arg = s.split("-- ", 1)[1]
            if arg.startswith('"') and arg.endswith('"'):
                arg = arg[1:-1]
            self.assertNotIn('"', arg)
        # health check runs INSIDE the guest; nothing parses endpoints
        self.assertIn("cua sb exec $name -- 'curl -s -m 8 http://127.0.0.1:8472/health'",
                      script)
        self.assertNotIn("endpoints", script.lower())

    def test_install_agent_locked_until_stack_done(self):
        """install-agent needs the cua stack on the laptop: 409 first, then
        200 once stack_done is set (with a key available)."""
        device_id, _ = self.pair_device()
        status, body = self.api("POST", f"/api/devices/{device_id}/task",
                                {"kind": "install-agent",
                                 "params": {"name": "laptop-vm-1"}})
        self.assertEqual(status, 409)
        self.assertIn("Install VM stack", body["error"])
        devices.DEVICES[device_id]["stack_done"] = True
        with mock.patch.object(server, "kimi_api_key", return_value="sk-test"):
            status, body = self.api("POST", f"/api/devices/{device_id}/task",
                                    {"kind": "install-agent",
                                     "params": {"name": "laptop-vm-1"}})
        self.assertEqual(status, 200)
        self.assertTrue(body["task_id"])

    def test_install_agent_bad_vm_name(self):
        """VM names must match the remote-node name rules."""
        device_id, _ = self.pair_device()
        devices.DEVICES[device_id]["stack_done"] = True
        status, body = self.api("POST", f"/api/devices/{device_id}/task",
                                {"kind": "install-agent",
                                 "params": {"name": "Bad_Name!"}})
        self.assertEqual(status, 400)
        self.assertIn("vm name", body["error"])

    def test_install_agent_requires_api_key(self):
        """No KIMI_API_KEY in agentd/.env → a clear 400, no task queued."""
        device_id, _ = self.pair_device()
        devices.DEVICES[device_id]["stack_done"] = True
        with mock.patch.object(server, "kimi_api_key", return_value=""):
            status, body = self.api("POST", f"/api/devices/{device_id}/task",
                                    {"kind": "install-agent",
                                     "params": {"name": "laptop-vm-1"}})
        self.assertEqual(status, 400)
        self.assertIn("KIMI_API_KEY", body["error"])
        dev = devices.DEVICES[device_id]
        self.assertEqual(dev["tasks"], {})

    def test_install_agent_label(self):
        """The queued task reads 'install agent <vm>'."""
        device_id, _ = self.pair_device()
        devices.DEVICES[device_id]["stack_done"] = True
        with mock.patch.object(server, "kimi_api_key", return_value="sk-test"):
            _, body = self.api("POST", f"/api/devices/{device_id}/task",
                               {"kind": "install-agent",
                                "params": {"name": "laptop-vm-1"}})
        task_id = body["task_id"]
        task = devices.DEVICES[device_id]["tasks"][task_id]
        self.assertEqual(task["label"], "install agent laptop-vm-1")

    # -- expose-agent (reverse-relay the in-guest sidecar to the master)

    def test_expose_agent_script_markers(self):
        """The generated admin script: reserves a relay index via the
        beacon's endpoints.json map (client port 18473+i, dial = client-1),
        pushes a guest start script carrying YTL_RELAY_OUT (LF-only),
        restarts the guest sidecar (kill via node /proc SIGKILL + curl
        health verify — pkill/pgrep are missing on the guest image) and
        No portproxy / firewall / port-forward anywhere. ASCII-only."""
        script = devices.build_task_script(
            "expose-agent", {"master_url": "http://m:8471",
                             "vm_name": "laptop-vm-1",
                             "master_lan_ip": "192.168.50.111"})
        for marker in ("YTL_RELAY_OUT", "192.168.50.111", "endpoints.json",
                       "18473 + $i", "0..9", "dialPort = $clientPort - 1",
                       "SIGKILL", "readdirSync(``/proc``)",
                       "curl -s -m 8 http://127.0.0.1:8472/health",
                       "Contains('\"ok\":true')", "node -e",
                       "WindowsBuiltInRole]::Administrator",  # admin gate
                       "Get-Command cua", "TcpListener",
                       "start-agentd.sh", "-replace \"`r`n\", \"`n\"",
                       "ToBase64String", "Buffer.from", "chmodSync",
                       "agent exposed at http://$masterIp`:$clientPort -- relay dial $masterIp`:$dialPort"):
            self.assertIn(marker, script)
        # the reverse relay replaces all host-side publishing machinery
        for gone in ("portproxy", "advfirewall", "port-forward", "28472",
                     "qemu-system", "Get-NetTCPConnection", "firewall rule",
                     "pgrep", "pkill", "NO_PROCESS"):
            self.assertNotIn(gone, script)
        # transport is exec-only: no `cua sb cp` anywhere (the start script
        # goes base64+node like install-agent).
        self.assertNotIn("cua sb cp", script)
        script.encode("ascii")  # PS 5.1 ANSI fallback: generated = ASCII only

    def test_expose_agent_locked_until_stack_done(self):
        """expose-agent touches cua + netsh: 409 until the stack is proven."""
        device_id, _ = self.pair_device()
        status, body = self.api("POST", f"/api/devices/{device_id}/task",
                                {"kind": "expose-agent",
                                 "params": {"name": "laptop-vm-1"}})
        self.assertEqual(status, 409)
        self.assertIn("Install VM stack", body["error"])

    def test_expose_agent_bad_vm_name(self):
        """VM names must match the remote-node name rules."""
        device_id, _ = self.pair_device()
        devices.DEVICES[device_id]["stack_done"] = True
        status, body = self.api("POST", f"/api/devices/{device_id}/task",
                                {"kind": "expose-agent",
                                 "params": {"name": "Bad_Name!"}})
        self.assertEqual(status, 400)
        self.assertIn("vm name", body["error"])

    def test_expose_agent_label_and_queued_script(self):
        """The queued task reads 'expose agent <vm>' and carries the script."""
        device_id, token = self.pair_device()
        devices.DEVICES[device_id]["stack_done"] = True
        _, body = self.api("POST", f"/api/devices/{device_id}/task",
                           {"kind": "expose-agent",
                            "params": {"name": "laptop-vm-1"}})
        task_id = body["task_id"]
        task = devices.DEVICES[device_id]["tasks"][task_id]
        self.assertEqual(task["label"], "expose agent laptop-vm-1")
        status, body = self.api("GET", f"/api/devices/{device_id}/next?token={token}")
        self.assertEqual(status, 200)
        self.assertEqual(body["task"]["kind"], "expose-agent")
        self.assertIn("YTL_RELAY_OUT", body["task"]["script"])

    def test_expose_agent_no_endpoints_parsing(self):
        """The binding lesson holds: the published port is fixed by the
        forward, never parsed from cua sb list output."""
        script = devices.build_task_script(
            "expose-agent", {"master_url": "http://m:8471",
                             "vm_name": "laptop-vm-1"})
        self.assertNotIn("sb list", script)
        self.assertNotIn("endpoints.agent", script)

    def test_restart_agent_script_full_lifecycle(self):
        """restart-agent = kill + WAL clean + rewrite start script + start +
        curl-health verify, all through cua sb exec. The relay target comes
        from the master context, never from the guest."""
        script = devices.build_task_script(
            "restart-agent", {"master_url": "http://m:8471",
                              "vm_name": "laptop-vm-1",
                              "relay_target": "192.168.50.111:18472"})
        for marker in ("kill stale sidecar", "drop stale WAL files",
                       "write start script", "start + verify",
                       "YTL_RELAY_OUT=192.168.50.111:18472",
                       "harness.sqlite", "SIGKILL",
                       "curl -s -m 8 http://127.0.0.1:8472/health",
                       "sidecar restarted on"):
            self.assertIn(marker, script)
        # no pkill/pgrep on the guest image; exec-only transport
        for gone in ("pkill", "pgrep", "cua sb cp"):
            self.assertNotIn(gone, script)
        script.encode("ascii")

    def test_restart_agent_without_relay_target(self):
        """No registry entry (e.g. first install) -> script without relay."""
        script = devices.build_task_script(
            "restart-agent", {"master_url": "http://m:8471",
                              "vm_name": "laptop-vm-1"})
        self.assertNotIn("YTL_RELAY_OUT", script)
        script.encode("ascii")

    def test_restart_agent_bad_vm_name(self):
        device_id, _ = self.pair_device()
        devices.DEVICES[device_id]["stack_done"] = True
        status, body = self.api("POST", f"/api/devices/{device_id}/task",
                                {"kind": "restart-agent",
                                 "params": {"name": "Bad_Name!"}})
        self.assertEqual(status, 400)

    def test_kit_agentd_tgz_served(self):
        """/kit/agentd.tgz is a real gzipped tar of agentd/: 200, the right
        content type, parseable, with package.json and without node_modules,
        .env or *.log."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        conn.request("GET", "/kit/agentd.tgz",
                     headers={"x-ytl-token": self.TOKEN})
        resp = conn.getresponse()
        raw = resp.read()
        ctype = resp.getheader("Content-Type") or ""
        status = resp.status
        conn.close()
        self.assertEqual(status, 200)
        self.assertIn("application/gzip", ctype)
        import io
        import tarfile
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tf:
            names = tf.getnames()
        self.assertIn("agentd/package.json", names)
        self.assertFalse(any("node_modules" in n for n in names),
                         "node_modules must be excluded")
        self.assertFalse(any(n.rsplit("/", 1)[-1] == ".env" for n in names),
                         ".env must be excluded")
        self.assertFalse(any(n.endswith(".log") for n in names),
                         "*.log must be excluded")


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

    def test_offline_device_interrupts_running_tasks(self):
        """A device that stops polling (agent closed) immediately fails its
        running tasks on the next listing — no 10-minute wait."""
        import devices as devmod
        device_id, token = self.pair_device()
        devices.DEVICES[device_id]["stack_done"] = True
        _, body = self.api("POST", f"/api/devices/{device_id}/task",
                           {"kind": "install-stack"})
        task_id = body["task_id"]
        # agent fetched it (running), sent one line, then went silent
        status, body = self.api("GET",
                                f"/api/devices/{device_id}/next?token={token}")
        self.assertEqual(status, 200)
        self.assertEqual(body["task"]["id"], task_id)
        self.api("POST", f"/api/devices/{device_id}/output",
                 {"token": token, "task_id": task_id, "line": "working…"})
        devices.DEVICES[device_id]["last_seen"] = (
            __import__("time").time() - devmod.DEVICE_OFFLINE_GRACE - 5)
        status, body = self.api("GET", "/api/devices")
        self.assertEqual(status, 200)
        dev = next(d for d in body["devices"] if d["id"] == device_id)
        self.assertFalse(dev["online"])
        self.assertEqual(dev["tasks"][0]["status"], "error")
        _, job = self.api("GET", f"/api/agent/job/{task_id}")
        self.assertEqual(job["status"], "error")

    def test_remove_device_offline_only(self):
        """Offline devices can be forgotten; online ones are refused."""
        device_id, token = self.pair_device()
        devices.DEVICES[device_id]["stack_done"] = True
        # online -> 409
        status, _ = self.api("POST", f"/api/devices/{device_id}/remove", {})
        self.assertEqual(status, 409)
        # goes offline -> 200 and gone
        devices.DEVICES[device_id]["last_seen"] = __import__("time").time() - 120
        status, _ = self.api("POST", f"/api/devices/{device_id}/remove", {})
        self.assertEqual(status, 200)
        _, listing = self.api("GET", "/api/devices")
        self.assertEqual(listing["devices"], [])
        # unknown id -> 404
        status, _ = self.api("POST", f"/api/devices/{device_id}/remove", {})
        self.assertEqual(status, 404)

    def test_create_vm_auto_chains_start_beacon(self):
        """A successfully finished create-vm task starts the beacon on that
        device automatically — no manual click needed."""
        device_id, token = self.pair_device()
        devices.DEVICES[device_id]["stack_done"] = True
        self.write_settings({"lan_token": self.TOKEN, "host": "0.0.0.0",
                             "port": self.port})
        _, body = self.api("POST", f"/api/devices/{device_id}/task",
                           {"kind": "create-vm", "params": {"count": 1}})
        task_id = body["task_id"]
        self.api("GET", f"/api/devices/{device_id}/next?token={token}")
        self.api("POST", f"/api/devices/{device_id}/output",
                 {"token": token, "task_id": task_id, "done": True, "exit_code": 0})
        _, listing = self.api("GET", "/api/devices")
        dev = next(d for d in listing["devices"] if d["id"] == device_id)
        kinds = [t["kind"] for t in dev["tasks"]]
        self.assertIn("start-beacon", kinds)
        chained = next(t for t in dev["tasks"] if t["kind"] == "start-beacon")
        self.assertIn("auto", chained["label"])

    def test_failed_create_vm_does_not_chain_beacon(self):
        """A failed create-vm leaves the beacon alone."""
        device_id, token = self.pair_device()
        devices.DEVICES[device_id]["stack_done"] = True
        self.write_settings({"lan_token": self.TOKEN, "host": "0.0.0.0",
                             "port": self.port})
        _, body = self.api("POST", f"/api/devices/{device_id}/task",
                           {"kind": "create-vm", "params": {"count": 1}})
        task_id = body["task_id"]
        self.api("GET", f"/api/devices/{device_id}/next?token={token}")
        self.api("POST", f"/api/devices/{device_id}/output",
                 {"token": token, "task_id": task_id, "done": True, "exit_code": 1})
        _, listing = self.api("GET", "/api/devices")
        dev = next(d for d in listing["devices"] if d["id"] == device_id)
        self.assertNotIn("start-beacon", [t["kind"] for t in dev["tasks"]])
