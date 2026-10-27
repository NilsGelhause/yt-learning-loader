"""Tests for the CUA Spaces feature: cua_client (find/version/list parsing),
the /api/cua/* endpoints (status, create/remove jobs), and VM nodes in the
/api/network view.

No cua CLI is installed or required — shutil.which / subprocess are patched,
and create/remove run against a fake Popen, mirroring the conventions in
tests.test_lan (real server.Handler on a temp data dir, no token set).

Run via tests/run.py.
"""

from __future__ import annotations

import http.client
import io
import json
import os
import tempfile
import threading
import time
import unittest
import unittest.mock as mock
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agentd_client  # noqa: E402
import cua_client  # noqa: E402
import server  # noqa: E402
from tests.test_agentd import StubAgentd  # noqa: E402


def fake_proc(stdout_lines=("",), returncode=0):
    """A stand-in for subprocess.Popen result streaming canned lines."""
    class FakeProc:
        def __init__(self, cmd, **kwargs):
            self.cmd = cmd
            self.kwargs = kwargs
            self.pid = 424242
            self.stdout = iter(list(stdout_lines))
            self.stderr = iter([])
            self.returncode = returncode

        def wait(self, timeout=None):
            return returncode
    return FakeProc


class CuaClientTests(unittest.TestCase):
    """Pure unit tests for the cua CLI client — no server, no real CLI."""

    def setUp(self):
        cua_client.reset_cache()

    tearDown = setUp

    def test_find_cua_none_when_not_on_path(self):
        with mock.patch.object(cua_client.shutil, "which", return_value=None):
            self.assertIsNone(cua_client.find_cua())

    def test_find_cua_caches_for_a_few_seconds(self):
        with mock.patch.object(cua_client.shutil, "which",
                               return_value="/fake/cua") as which:
            self.assertEqual(cua_client.find_cua(), "/fake/cua")
            cua_client.find_cua()
            self.assertEqual(which.call_count, 1)
            cua_client.reset_cache()
            cua_client.find_cua()
            self.assertEqual(which.call_count, 2)

    def test_version_parsed_loosely(self):
        proc = mock.Mock(returncode=0, stdout="cua 1.2.3\n", stderr="")
        with mock.patch.object(cua_client.subprocess, "run", return_value=proc):
            self.assertEqual(cua_client.version("/fake/cua"), "1.2.3")

    def test_version_none_on_failure(self):
        with mock.patch.object(cua_client.subprocess, "run",
                               side_effect=OSError("nope")):
            self.assertIsNone(cua_client.version("/fake/cua"))
        with mock.patch.object(cua_client, "find_cua", return_value=None):
            self.assertIsNone(cua_client.version(None))

    def test_list_parses_json_object(self):
        out = json.dumps({"sandboxes": [
            {"name": "ytl-dev", "status": "active", "image": "ubuntu"},
            {"name": "ytl-old", "status": "stopped", "image": "ubuntu"},
        ]})
        proc = mock.Mock(returncode=0, stdout=out, stderr="")
        with mock.patch.object(cua_client.subprocess, "run", return_value=proc):
            sandboxes = cua_client.list_sandboxes("/fake/cua")
        self.assertEqual(sandboxes, [
            {"name": "ytl-dev", "status": "running", "image": "ubuntu", "kind": "",
             "endpoints": {}},
            {"name": "ytl-old", "status": "stopped", "image": "ubuntu", "kind": "",
             "endpoints": {}},
        ])

    def test_list_keeps_agent_endpoint(self):
        """endpoints.agent (the in-guest sidecar URL) survives normalization
        so vm_nodes can route to a VM's own agent."""
        out = json.dumps([{"name": "ytl-dev", "status": "ready", "kind": "vm",
                           "endpoints": {"agent": "http://127.0.0.1:56246"}}])
        proc = mock.Mock(returncode=0, stdout=out, stderr="")
        with mock.patch.object(cua_client.subprocess, "run", return_value=proc):
            sandboxes = cua_client.list_sandboxes("/fake/cua")
        self.assertEqual(sandboxes[0]["endpoints"],
                         {"agent": "http://127.0.0.1:56246"})

    def test_list_parses_bare_json_array(self):
        out = json.dumps([{"name": "a", "state": "ready"}])
        proc = mock.Mock(returncode=0, stdout=out, stderr="")
        with mock.patch.object(cua_client.subprocess, "run", return_value=proc):
            sandboxes = cua_client.list_sandboxes("/fake/cua")
        self.assertEqual(sandboxes, [{"name": "a", "status": "running",
                                      "image": "", "kind": "", "endpoints": {}}])

    def test_list_parses_table_text(self):
        out = ("NAME        STATUS     IMAGE\n"
               "ytl-dev     running    ubuntu:latest\n"
               "ytl-old     stopped    ubuntu:latest\n")
        proc = mock.Mock(returncode=0, stdout=out, stderr="")
        with mock.patch.object(cua_client.subprocess, "run", return_value=proc):
            sandboxes = cua_client.list_sandboxes("/fake/cua")
        self.assertEqual(sandboxes, [
            {"name": "ytl-dev", "status": "running", "image": "ubuntu:latest", "kind": "",
             "endpoints": {}},
            {"name": "ytl-old", "status": "stopped", "image": "ubuntu:latest", "kind": "",
             "endpoints": {}},
        ])

    def test_list_empty_on_garbage_output(self):
        proc = mock.Mock(returncode=0, stdout="<html>404</html>", stderr="")
        with mock.patch.object(cua_client.subprocess, "run", return_value=proc):
            self.assertEqual(cua_client.list_sandboxes("/fake/cua"), [])

    def test_list_tries_next_variant_when_command_fails(self):
        failing = mock.Mock(returncode=2, stdout="", stderr="unknown command")
        ok = mock.Mock(returncode=0, stdout="[]", stderr="")
        with mock.patch.object(cua_client.subprocess, "run",
                               side_effect=[failing, ok]) as run:
            self.assertEqual(cua_client.list_sandboxes("/fake/cua"), [])
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[1].args[0][:3], ["/fake/cua", "sb", "ls"])

    def test_list_empty_without_exe(self):
        with mock.patch.object(cua_client, "find_cua", return_value=None):
            self.assertEqual(cua_client.list_sandboxes(None), [])

    def test_name_validation_and_prefixing(self):
        self.assertTrue(cua_client.valid_name("ytl-dev-1"))
        self.assertFalse(cua_client.valid_name("Dev Box"))
        self.assertFalse(cua_client.valid_name(""))
        self.assertEqual(cua_client.full_name("Dev"), "ytl-dev")
        self.assertEqual(cua_client.full_name("ytl-dev"), "ytl-dev")  # idempotent
        self.assertEqual(cua_client.create_argv("ytl-dev"),
                         ["sb", "create", "ubuntu", "--name", "ytl-dev",
                          "--service", "agent=8472"])
        self.assertEqual(cua_client.remove_argv("ytl-dev"), ["sb", "rm", "ytl-dev", "--force"])


class CuaApiTests(unittest.TestCase):
    """server.py wiring: /api/cua/* on a real Handler with a temp data dir."""

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
            mock.patch.object(server, "AGENT_JOBS", {}),
            mock.patch.object(server.cua_client, "find_cua", return_value="/fake/cua"),
            mock.patch.object(server.cua_client, "version", return_value="1.2.3"),
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

    def tearDown(self):
        with server.AGENT_JOBS_LOCK:
            server.AGENT_JOBS.clear()

    def api(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        all_headers = dict(headers or {})
        if payload is not None:
            all_headers["Content-Type"] = "application/json"
        conn.request(method, path, body=payload, headers=all_headers)
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

    def wait_job(self, job_id, timeout: float = 5) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status, job = self.api("GET", f"/api/agent/job/{job_id}")
            self.assertEqual(status, 200)
            if job["status"] in ("done", "error", "cancelled"):
                return job
            time.sleep(0.02)
        self.fail(f"job {job_id} did not settle")

    # -- GET /api/cua/status

    def test_status_installed(self):
        with mock.patch.object(server.cua_client, "list_sandboxes",
                               return_value=[{"name": "ytl-dev", "status": "running", "image": "ubuntu"}]):
            status, body = self.api("GET", "/api/cua/status")
        self.assertEqual(status, 200)
        self.assertTrue(body["installed"])
        self.assertEqual(body["version"], "1.2.3")
        self.assertEqual(body["sandboxes"], [
            {"name": "ytl-dev", "status": "running", "image": "ubuntu"}])
        self.assertEqual(body["note"], "")

    def test_status_not_installed_shows_install_command(self):
        with mock.patch.object(server.cua_client, "find_cua", return_value=None):
            status, body = self.api("GET", "/api/cua/status")
        self.assertEqual(status, 200)
        self.assertFalse(body["installed"])
        self.assertIsNone(body["version"])
        self.assertEqual(body["sandboxes"], [])
        self.assertIn("cua.ai", body["note"])

    def test_status_not_installed_note_is_platform_specific(self):
        with mock.patch.object(server.cua_client, "find_cua", return_value=None), \
             mock.patch("sys.platform", "darwin"):
            _, body = self.api("GET", "/api/cua/status")
        self.assertIn("install.sh", body["note"])

    def test_status_gated_by_lan_token(self):
        self.write_settings({"lan_token": "s3cret"})
        status, body = self.api("GET", "/api/cua/status")
        self.assertEqual(status, 401)
        self.assertEqual(body, {"error": "token_required"})

    # -- POST /api/cua/sandboxes (create) and /remove

    def test_create_spawns_streaming_job(self):
        with mock.patch.object(server.subprocess, "Popen",
                               fake_proc(["pulling image…\n", "created ytl-dev\n"])):
            status, body = self.api("POST", "/api/cua/sandboxes", {"name": "Dev"})
        self.assertEqual(status, 200)
        job_id = body["job_id"]
        job = self.wait_job(job_id)
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["exit_code"], 0)
        self.assertEqual([l["x"] for l in job["lines"]],
                         ["pulling image…", "created ytl-dev"])
        with server.AGENT_JOBS_LOCK:
            record = server.AGENT_JOBS[job_id]
        self.assertEqual(record["key"], "cua:ytl-dev")
        self.assertEqual(record["backend"], "cli-style")
        self.assertIsNone(record["workspace"])

    def test_create_uses_cli_argv(self):
        seen = []

        class Proc(fake_proc()):
            def __init__(self, cmd, **kwargs):
                seen.append(cmd)
                super().__init__(cmd, **kwargs)

        with mock.patch.object(server.subprocess, "Popen", Proc):
            _, body = self.api("POST", "/api/cua/sandboxes", {"name": "dev"})
        self.wait_job(body["job_id"])
        self.assertEqual(seen[0], ["cua", "sb", "create", "ubuntu", "--name", "ytl-dev",
                                   "--service", "agent=8472"])

    def test_create_rejects_bad_name(self):
        status, _ = self.api("POST", "/api/cua/sandboxes", {"name": "Dev Box!!"})
        self.assertEqual(status, 400)

    def test_create_requires_name(self):
        status, _ = self.api("POST", "/api/cua/sandboxes", {})
        self.assertEqual(status, 400)

    def test_create_503_without_cua(self):
        with mock.patch.object(server.cua_client, "find_cua", return_value=None):
            status, body = self.api("POST", "/api/cua/sandboxes", {"name": "dev"})
        self.assertEqual(status, 503)
        self.assertIn("error", body)

    def test_create_409_while_sandbox_busy(self):
        with server.AGENT_JOBS_LOCK:
            server.AGENT_JOBS["cuabusy"] = {
                "id": "cuabusy", "workspace": None, "key": "cua:ytl-dev",
                "status": "running", "backend": "cli-style", "lines": [],
                "exit_code": None, "pid": None,
                "started": int(time.time()), "ended": None,
            }
        status, body = self.api("POST", "/api/cua/sandboxes", {"name": "dev"})
        self.assertEqual(status, 409)
        self.assertIn("still running", body["error"])

    def test_remove_spawns_job_with_cli_argv(self):
        seen = []

        class Proc(fake_proc()):
            def __init__(self, cmd, **kwargs):
                seen.append(cmd)
                super().__init__(cmd, **kwargs)

        with mock.patch.object(server.subprocess, "Popen", Proc):
            status, body = self.api("POST", "/api/cua/sandboxes/remove",
                                    {"name": "ytl-dev"})
        self.assertEqual(status, 200)
        job = self.wait_job(body["job_id"])
        self.assertEqual(job["status"], "done")
        self.assertEqual(seen[0], ["cua", "sb", "rm", "ytl-dev", "--force"])

    def test_remove_rejects_bad_name(self):
        status, _ = self.api("POST", "/api/cua/sandboxes/remove",
                             {"name": "../escape"})
        self.assertEqual(status, 400)

    def test_create_job_failure_marks_error(self):
        with mock.patch.object(server.subprocess, "Popen",
                               fake_proc(["boom\n"], returncode=1)):
            _, body = self.api("POST", "/api/cua/sandboxes", {"name": "dev"})
        job = self.wait_job(body["job_id"])
        self.assertEqual(job["status"], "error")
        self.assertEqual(job["exit_code"], 1)

    # -- VM nodes in GET /api/network

    def test_network_includes_vm_nodes(self):
        with mock.patch.object(server.cua_client, "find_cua", return_value="/fake/cua"), \
             mock.patch.object(server.cua_client, "list_sandboxes", return_value=[
                 {"name": "ytl-dev", "status": "running", "image": "ubuntu", "kind": "vm"},
                 {"name": "ytl-old", "status": "stopped", "image": "ubuntu", "kind": "container"},
             ]):
            status, body = self.api("GET", "/api/network")
        self.assertEqual(status, 200)
        self.assertEqual([n["id"] for n in body["nodes"]],
                         ["local", "vm-ytl-dev", "vm-ytl-old"])
        vm = body["nodes"][1]
        self.assertEqual(vm["kind"], "vm")
        self.assertTrue(vm["online"])
        self.assertEqual(vm["sessions"], [])
        self.assertEqual(vm["flavor"], "qemu")
        self.assertEqual(body["nodes"][2]["flavor"], "container")
        self.assertFalse(body["nodes"][2]["online"])

    def test_network_vm_nodes_capped_at_ten(self):
        many = [{"name": f"vm{i}", "status": "running", "image": ""}
                for i in range(15)]
        with mock.patch.object(server.cua_client, "find_cua", return_value="/fake/cua"), \
             mock.patch.object(server.cua_client, "list_sandboxes", return_value=many):
            _, body = self.api("GET", "/api/network")
        self.assertEqual(len(body["nodes"]), 11)  # local + 10

    def test_network_without_cua_has_only_local(self):
        with mock.patch.object(server.cua_client, "find_cua", return_value=None):
            status, body = self.api("GET", "/api/network")
        self.assertEqual(status, 200)
        self.assertEqual([n["id"] for n in body["nodes"]], ["local"])

    def test_network_survives_listing_failure(self):
        with mock.patch.object(server.cua_client, "find_cua", return_value="/fake/cua"), \
             mock.patch.object(server.cua_client, "list_sandboxes",
                               side_effect=RuntimeError("cli drift")):
            status, body = self.api("GET", "/api/network")
        self.assertEqual(status, 200)
        self.assertEqual([n["id"] for n in body["nodes"]], ["local"])

    # -- GET /api/vm-nodes

    def test_vm_nodes_endpoint_shape(self):
        with mock.patch.object(server.vm_nodes, "list_nodes", return_value=[
                {"name": "ytl-dev", "online": True,
                 "endpoint": "http://127.0.0.1:56246"}]), \
             mock.patch.object(server.vm_nodes, "node_health",
                               return_value={"ok": True, "keyConfigured": True}):
            status, body = self.api("GET", "/api/vm-nodes")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"nodes": [
            {"name": "ytl-dev", "online": True, "agent": True,
             "endpoint": "http://127.0.0.1:56246"}]})

    def test_vm_nodes_agent_false_without_key(self):
        with mock.patch.object(server.vm_nodes, "list_nodes", return_value=[
                {"name": "ytl-dev", "online": True, "endpoint": None}]), \
             mock.patch.object(server.vm_nodes, "node_health",
                               return_value={"ok": True, "keyConfigured": False}):
            _, body = self.api("GET", "/api/vm-nodes")
        self.assertEqual(body["nodes"][0]["agent"], False)

    # -- POST /api/cua/provision (Install agent)

    def test_provision_409_while_running(self):
        with server.AGENT_JOBS_LOCK:
            server.AGENT_JOBS["provbusy"] = {
                "id": "provbusy", "workspace": None, "key": "provision:ytl-dev",
                "status": "running", "backend": "cli-style", "lines": [],
                "exit_code": None, "pid": None,
                "started": int(time.time()), "ended": None,
            }
        status, body = self.api("POST", "/api/cua/provision", {"name": "dev"})
        self.assertEqual(status, 409)
        self.assertIn("already running", body["error"])

    def test_provision_rejects_bad_name(self):
        status, _ = self.api("POST", "/api/cua/provision", {"name": "Bad Name!!"})
        self.assertEqual(status, 400)

    def test_provision_streams_steps_and_reports_endpoint(self):
        """With every subprocess call faked, the job walks the provisioning
        steps in order and the last line reports the agent endpoint."""
        cmds = []

        class Proc:
            def __init__(self, cmd, **kwargs):
                self.cmd = cmd
                self.kwargs = kwargs
                self.pid = 424242
                self.returncode = 0

            def communicate(self, timeout=None):
                cmds.append(list(self.cmd))
                return ("step output\n", "")

            def kill(self):
                pass

        key_tmp = self.data / "key.env"
        key_tmp.write_text("KIMI_API_KEY=test-key\n", encoding="utf-8")
        with mock.patch.object(server.subprocess, "Popen", Proc), \
             mock.patch.object(server, "_stage_key_env", return_value=key_tmp), \
             mock.patch.object(server.vm_nodes, "node_endpoint",
                               return_value="http://127.0.0.1:9"), \
             mock.patch.object(server.vm_nodes, "node_health",
                               return_value={"ok": True, "keyConfigured": True}):
            status, body = self.api("POST", "/api/cua/provision", {"name": "dev"})
            self.assertEqual(status, 200)
            job = self.wait_job(body["job_id"], timeout=10)
        self.assertEqual(job["status"], "done")
        # the full provisioning sequence: tar, cp, exec-unpack, cp-key, chmod,
        # npm ci, cp-start-script, start — against /fake/cua
        flat = [a for cmd in cmds for a in cmd]
        joined = " ".join(flat)
        self.assertEqual(cmds[0][:2], ["tar", "czf"])
        self.assertIn("cp", flat)
        self.assertIn("npm ci --no-audit --no-fund", joined)
        self.assertIn("start-agentd.sh", joined)
        last_lines = [l["x"] for l in job["lines"][-3:]]
        self.assertTrue(any("agent ready at http://127.0.0.1:9" in x for x in last_lines),
                        f"endpoint missing from tail: {last_lines}")
        # the key material never appears in the streamed lines
        streamed = "\n".join(l["x"] for l in job["lines"])
        self.assertNotIn("test-key", streamed)

    def test_provision_marks_error_when_a_step_fails(self):
        class Proc:
            def __init__(self, cmd, **kwargs):
                self.cmd = cmd
                self.kwargs = kwargs
                self.pid = 424242
                self.returncode = 1 if "npm" in " ".join(cmd) else 0

            def communicate(self, timeout=None):
                return ("", "npm exploded") if self.returncode else ("", "")

            def kill(self):
                pass

        key_tmp = self.data / "key.env"
        key_tmp.write_text("KIMI_API_KEY=test-key\n", encoding="utf-8")
        with mock.patch.object(server.subprocess, "Popen", Proc), \
             mock.patch.object(server, "_stage_key_env", return_value=key_tmp):
            _, body = self.api("POST", "/api/cua/provision", {"name": "dev"})
            job = self.wait_job(body["job_id"], timeout=10)
        self.assertEqual(job["status"], "error")
        self.assertIn("npm ci in the VM exited with code 1",
                      [l["x"] for l in job["lines"]])

    # -- VM nodes with a healthy in-guest agent

    def test_network_vm_node_prefers_agent_health_over_state(self):
        """A suspended sandbox with a healthy in-guest agent is online, keeps
        its raw state in vm_state, and lists ITS OWN sidecar sessions."""
        with mock.patch.object(server.cua_client, "find_cua", return_value="/fake/cua"), \
             mock.patch.object(server.cua_client, "list_sandboxes", return_value=[
                 {"name": "ytl-dev", "status": "suspended", "image": "",
                  "kind": "vm", "endpoints": {"agent": "http://127.0.0.1:56246"}}]), \
             mock.patch.object(server.vm_nodes, "node_healthy", return_value=True), \
             mock.patch.object(server.vm_nodes, "node_health",
                               return_value={"ok": True, "keyConfigured": True}), \
             mock.patch.object(server.vm_nodes, "node_conversations", return_value=[
                 {"key": "console:main", "id": "c9", "cwd": "/home/cua",
                  "busy": False, "lastStatus": "done"}]):
            status, body = self.api("GET", "/api/network")
        self.assertEqual(status, 200)
        vm = body["nodes"][1]
        self.assertEqual(vm["id"], "vm-ytl-dev")
        self.assertTrue(vm["online"])
        self.assertEqual(vm["vm_state"], "suspended")
        self.assertTrue(vm["agent"])
        self.assertEqual([s["key"] for s in vm["sessions"]], ["console:main"])
        self.assertEqual(vm["sessions"][0]["cwd"], "/home/cua")
        self.assertEqual(vm["sessions"][0]["status"], "done")

    def test_network_vm_node_without_agent_keeps_today_behavior(self):
        """No healthy agent -> online follows the sandbox run state, no
        sessions, agent False, and the raw state is still in vm_state."""
        with mock.patch.object(server.cua_client, "find_cua", return_value="/fake/cua"), \
             mock.patch.object(server.cua_client, "list_sandboxes", return_value=[
                 {"name": "ytl-dev", "status": "running", "image": "", "kind": "vm"}]), \
             mock.patch.object(server.vm_nodes, "node_healthy", return_value=False):
            _, body = self.api("GET", "/api/network")
        vm = body["nodes"][1]
        self.assertTrue(vm["online"])          # sandbox running
        self.assertFalse(vm["agent"])
        self.assertEqual(vm["vm_state"], "running")
        self.assertEqual(vm["sessions"], [])


class VmTaskApiTests(unittest.TestCase):
    """POST /api/agent/vm-task against the stub sidecar: validation, key
    shape, instructions, session reuse, backend gating, busy guard, and the
    best-effort resume of a stopped VM."""

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

        cls.stub = StubAgentd()

        cls.patchers = [
            mock.patch.object(server, "SETTINGS_JSON", data / "settings.json"),
            mock.patch.object(server, "AGENT_JOBS", {}),
            mock.patch.object(server, "AGENT_POLL_INTERVAL", 0.01),
            mock.patch.object(server.agentd_client, "_BASE", cls.stub.base),
            mock.patch.object(server.cua_client, "find_cua", return_value="/fake/cua"),
            mock.patch.object(server.cua_client, "list_sandboxes", return_value=[]),
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
        cls.stub.stop()
        cls.tmp.cleanup()

    def setUp(self):
        (self.data / "settings.json").write_text("{}", encoding="utf-8")
        agentd_client.reset_cache()
        self.stub.jobs.clear()
        self.stub.submitted.clear()
        self.stub.conversations = []
        with server.AGENT_JOBS_LOCK:
            server.AGENT_JOBS.clear()

    def tearDown(self):
        agentd_client.reset_cache()
        self.stub.key_configured = True
        with server.AGENT_JOBS_LOCK:
            server.AGENT_JOBS.clear()

    def api(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"} if payload is not None else {}
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        status = resp.status
        conn.close()
        return status, json.loads(raw.decode("utf-8"))

    def wait_job(self, job_id, timeout: float = 5) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status, job = self.api("GET", f"/api/agent/job/{job_id}")
            self.assertEqual(status, 200)
            if job["status"] in ("done", "error", "cancelled"):
                return job
            time.sleep(0.02)
        self.fail(f"job {job_id} did not settle")

    def wait_submission(self, count: int = 1, timeout: float = 5) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(self.stub.submitted) >= count:
                return self.stub.submitted[count - 1]
            time.sleep(0.02)
        self.fail(f"sidecar saw no submission in {timeout}s")

    def test_vm_task_requires_task_and_valid_vm(self):
        status, _ = self.api("POST", "/api/agent/vm-task", {"vm": "ytl-dev"})
        self.assertEqual(status, 400)
        status, _ = self.api("POST", "/api/agent/vm-task",
                             {"vm": "Bad Name!", "task": "do x"})
        self.assertEqual(status, 400)
        status, _ = self.api("POST", "/api/agent/vm-task",
                             {"vm": "ytl-dev", "task": "   "})
        self.assertEqual(status, 400)

    def test_vm_task_submits_key_instructions_and_streams(self):
        status, body = self.api("POST", "/api/agent/vm-task",
                                {"vm": "ytl-dev", "task": "list /etc"})
        self.assertEqual(status, 200)
        submitted = self.wait_submission()
        parts = submitted["key"].split(":")
        self.assertEqual(parts[:2], ["vmtask", "ytl-dev"])
        self.assertEqual(len(parts[2]), 8)  # generated session id
        self.assertEqual(submitted["cwd"], str(server.ROOT))
        self.assertIn("cua sb exec ytl-dev --", submitted["instructions"])
        self.assertIn("list /etc", submitted["message"])
        self.assertNotIn("newSession", submitted)
        with server.AGENT_JOBS_LOCK:
            record = server.AGENT_JOBS[body["job_id"]]
        self.assertEqual(record["key"], submitted["key"])
        self.assertEqual(record["workspace"], None)
        self.assertEqual(record["backend"], "durable")
        self.stub.finish(submitted["jobId"], "done",
                         lines=[{"t": "out", "x": "task done"}])
        job = self.wait_job(body["job_id"])
        self.assertEqual(job["status"], "done")
        self.assertEqual([l["x"] for l in job["lines"]], ["task done"])

    def test_vm_task_session_reuse_resubmits_same_key(self):
        _, b1 = self.api("POST", "/api/agent/vm-task",
                         {"vm": "ytl-dev", "task": "first", "session": "s1"})
        first = self.wait_submission()
        self.assertEqual(first["key"], "vmtask:ytl-dev:s1")
        self.stub.finish(first["jobId"], "done")
        self.wait_job(b1["job_id"])
        _, b2 = self.api("POST", "/api/agent/vm-task",
                         {"vm": "ytl-dev", "task": "second", "session": "s1"})
        second = self.wait_submission(2)
        self.assertEqual(second["key"], "vmtask:ytl-dev:s1")
        self.assertIn("second", second["message"])
        self.stub.finish(second["jobId"], "done")
        self.wait_job(b2["job_id"])

    def test_vm_task_503_without_durable_backend(self):
        self.stub.key_configured = False
        agentd_client.reset_cache()
        status, body = self.api("POST", "/api/agent/vm-task",
                                {"vm": "ytl-dev", "task": "do x"})
        self.assertEqual(status, 503)
        self.assertIn("durable", body["error"].lower())
        self.assertEqual(self.stub.submitted, [])

    def test_vm_task_409_while_session_busy(self):
        with server.AGENT_JOBS_LOCK:
            server.AGENT_JOBS["vmbusy"] = {
                "id": "vmbusy", "workspace": None, "key": "vmtask:ytl-dev:s1",
                "status": "running", "backend": "durable", "lines": [],
                "exit_code": None, "pid": None,
                "started": int(time.time()), "ended": None,
            }
        status, body = self.api("POST", "/api/agent/vm-task",
                                {"vm": "ytl-dev", "task": "again", "session": "s1"})
        self.assertEqual(status, 409)
        self.assertIn("still running", body["error"])
        self.assertEqual(self.stub.submitted, [])

    def test_vm_task_resumes_stopped_vm(self):
        with mock.patch.object(server.cua_client, "list_sandboxes",
                               return_value=[{"name": "ytl-dev", "status": "suspended",
                                              "image": "", "kind": "vm"}]), \
             mock.patch.object(server.subprocess, "run") as run:
            status, body = self.api("POST", "/api/agent/vm-task",
                                    {"vm": "ytl-dev", "task": "wake up"})
        self.assertEqual(status, 200)
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], ["/fake/cua", "sb", "resume", "ytl-dev"])
        submitted = self.wait_submission()
        self.stub.finish(submitted["jobId"], "done")
        self.wait_job(body["job_id"])

    def test_vm_task_leaves_running_vm_alone(self):
        with mock.patch.object(server.cua_client, "list_sandboxes",
                               return_value=[{"name": "ytl-dev", "status": "running",
                                              "image": "", "kind": "vm"}]), \
             mock.patch.object(server.subprocess, "run") as run:
            status, body = self.api("POST", "/api/agent/vm-task",
                                    {"vm": "ytl-dev", "task": "just run"})
        self.assertEqual(status, 200)
        run.assert_not_called()
        submitted = self.wait_submission()
        self.stub.finish(submitted["jobId"], "done")
        self.wait_job(body["job_id"])

    # -- node set: the task runs natively on the VM's own sidecar

    def test_vm_task_on_node_runs_natively_in_guest(self):
        """node=<sandbox> drops the cua-exec instructions and submits to the
        VM's own sidecar, working in the guest home."""
        with mock.patch.object(server.vm_nodes, "node_endpoint",
                               return_value=self.stub.base), \
             mock.patch.object(server.vm_nodes, "node_healthy", return_value=True):
            status, body = self.api("POST", "/api/agent/vm-task",
                                    {"vm": "ytl-dev", "task": "list /etc",
                                     "node": "ytl-dev", "session": "s1"})
            self.assertEqual(status, 200)
            submitted = self.wait_submission()
            self.stub.finish(submitted["jobId"], "done",
                             lines=[{"t": "out", "x": "etc etc"}])
            job = self.wait_job(body["job_id"])
        self.assertEqual(submitted["key"], "vmtask:ytl-dev:s1")
        self.assertEqual(submitted["cwd"], "/home/cua")
        self.assertNotIn("instructions", submitted)
        self.assertIn("list /etc", submitted["message"])
        with server.AGENT_JOBS_LOCK:
            record = server.AGENT_JOBS[body["job_id"]]
        self.assertEqual(record["node"], "ytl-dev")
        self.assertIsNone(record["instructions"])
        self.assertEqual(job["status"], "done")
        self.assertEqual([l["x"] for l in job["lines"]], ["etc etc"])

    def test_vm_task_on_node_needs_healthy_agent(self):
        with mock.patch.object(server.vm_nodes, "node_endpoint", return_value=None):
            status, body = self.api("POST", "/api/agent/vm-task",
                                    {"vm": "ytl-dev", "task": "hi", "node": "ytl-dev"})
        self.assertEqual(status, 503)
        self.assertIn("Install agent", body["error"])
        self.assertEqual(self.stub.submitted, [])

    def test_vm_task_on_node_ignores_local_backend_state(self):
        """Node-native tasks need only the node's sidecar, not the local
        durable backend."""
        self.stub.key_configured = False
        agentd_client.reset_cache()
        with mock.patch.object(server.vm_nodes, "node_endpoint",
                               return_value=self.stub.base), \
             mock.patch.object(server.vm_nodes, "node_healthy", return_value=True):
            status, body = self.api("POST", "/api/agent/vm-task",
                                    {"vm": "ytl-dev", "task": "native",
                                     "node": "ytl-dev", "session": "s2"})
            self.assertEqual(status, 200)
            submitted = self.wait_submission()
            self.stub.finish(submitted["jobId"], "done")
            self.wait_job(body["job_id"])
        self.assertEqual(submitted["key"], "vmtask:ytl-dev:s2")


class _FakeStdin:
    """Records what the server wrote to the guest bash's stdin."""

    def __init__(self):
        self.writes = []

    def write(self, s):
        self.writes.append(s)
        return len(s)

    def flush(self):
        pass


class FakeTermProc:
    """Long-lived stand-in for `cua sb exec -i <name> -- bash` (the host
    process that keeps a guest bash alive). stdout/stderr are real pipes the
    test writes to — the server's reader threads consume them for real;
    wait() blocks until kill()/finish() like a live process."""

    def __init__(self, cmd, **kwargs):
        self.cmd = list(cmd)
        self.kwargs = kwargs
        self.pid = 4242
        self.returncode = None
        self.stdin = _FakeStdin()
        self._r_out, self._w_out = os.pipe()
        self._r_err, self._w_err = os.pipe()
        self.stdout = io.TextIOWrapper(io.FileIO(self._r_out, "rb"),
                                       encoding="utf-8", errors="replace")
        self.stderr = io.TextIOWrapper(io.FileIO(self._r_err, "rb"),
                                       encoding="utf-8", errors="replace")
        self._ended = threading.Event()

    def guest_print(self, text, err=False):
        os.write(self._w_err if err else self._w_out, text.encode("utf-8") + b"\n")

    def _close_ends(self):
        for fd in (self._w_out, self._w_err):
            try:
                os.close(fd)
            except OSError:
                pass

    def kill(self):
        self.returncode = -9
        self._close_ends()
        self._ended.set()

    def finish(self, code=0):
        self.returncode = code
        self._close_ends()
        self._ended.set()

    def poll(self):
        return self.returncode if self._ended.is_set() else None

    def wait(self, timeout=None):
        self._ended.wait(timeout)
        return self.returncode


class CuaTermApiTests(unittest.TestCase):
    """The /api/cua/term/* endpoints: a persistent raw bash per sandbox.

    Real Handler on a temp data dir (like CuaApiTests); `cua sb exec` is
    faked by FakeTermProc pipes, cua_client is patched to one fake sandbox."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        data = root / "data"
        for d in ("learnings", "transcripts", "thumbs", "info", "progress"):
            (data / d).mkdir(parents=True)
        (root / "teach").mkdir()
        cls.data = data
        cls.procs = []
        cls.popen_patcher = mock.patch.object(server.subprocess, "Popen")
        cls.mock_popen = cls.popen_patcher.start()
        cls.mock_popen.side_effect = lambda cmd, **kw: cls._make_proc(cmd, kw)
        cls.patchers = [
            mock.patch.object(server, "SETTINGS_JSON", data / "settings.json"),
            mock.patch.object(server, "CUA_TERMS", {}),
            mock.patch.object(server.cua_client, "find_cua", return_value="/fake/cua"),
            mock.patch.object(server.cua_client, "list_sandboxes",
                              side_effect=lambda exe=None: cls.sandboxes),
        ]
        for p in cls.patchers:
            p.start()
        cls.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def _make_proc(cls, cmd, kw):
        proc = FakeTermProc(cmd, **kw)
        cls.procs.append(proc)
        return proc

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)
        cls.httpd.server_close()
        for p in cls.patchers:
            p.stop()
        cls.popen_patcher.stop()
        cls.tmp.cleanup()

    sandboxes = [{"name": "ytl-dev", "status": "running", "image": "ubuntu"}]

    def setUp(self):
        self.write_settings({})
        type(self).sandboxes = [{"name": "ytl-dev", "status": "running", "image": "ubuntu"}]

    def tearDown(self):
        with server.CUA_TERMS_LOCK:
            terms = list(server.CUA_TERMS.values())
            server.CUA_TERMS.clear()
        for t in terms:
            try:
                t["proc"].finish(0)
            except Exception:  # noqa: BLE001
                pass

    def api(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"} if payload is not None else {}
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        status = resp.status
        conn.close()
        return status, json.loads(raw.decode("utf-8"))

    def write_settings(self, settings: dict):
        (self.data / "settings.json").write_text(
            json.dumps(settings), encoding="utf-8")

    def open_term(self):
        status, body = self.api("POST", "/api/cua/term/open", {"name": "ytl-dev"})
        self.assertEqual(status, 200)
        return body["term_id"]

    def wait_lines(self, term_id, count, timeout: float = 5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status, snap = self.api("GET", f"/api/cua/term/{term_id}")
            self.assertEqual(status, 200)
            if len(snap["lines"]) >= count:
                return snap
            time.sleep(0.02)
        self.fail(f"term {term_id} never reached {count} lines")

    def wait_status(self, term_id, wanted, timeout: float = 5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status, snap = self.api("GET", f"/api/cua/term/{term_id}")
            self.assertEqual(status, 200)
            if snap["status"] == wanted:
                return snap
            time.sleep(0.02)
        self.fail(f"term {term_id} never became {wanted}")

    def wait_exit(self, term_id, code, timeout: float = 5):
        """After a kill the reaper fills the real exit code in a beat later."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status, snap = self.api("GET", f"/api/cua/term/{term_id}")
            self.assertEqual(status, 200)
            if snap["exit_code"] == code:
                return snap
            time.sleep(0.02)
        self.fail(f"term {term_id} never recorded exit {code}")

    # -- open

    def test_term_open_rejects_bad_name(self):
        status, body = self.api("POST", "/api/cua/term/open", {"name": "Bad Name!!"})
        self.assertEqual(status, 400)
        self.assertIn("bad name", body["error"])
        self.assertEqual(server.CUA_TERMS, {})

    def test_term_open_unknown_sandbox_404(self):
        type(self).sandboxes = []
        status, body = self.api("POST", "/api/cua/term/open", {"name": "dev"})
        self.assertEqual(status, 404)
        self.assertIn("not found", body["error"])
        self.assertEqual(server.CUA_TERMS, {})

    def test_term_spawn_failure_is_friendly_and_leaves_no_registry_entry(self):
        self.mock_popen.side_effect = OSError("no cua")
        try:
            status, body = self.api("POST", "/api/cua/term/open", {"name": "dev"})
        finally:
            self.mock_popen.side_effect = lambda cmd, **kw: self._make_proc(cmd, kw)
        self.assertEqual(status, 500)
        self.assertIn("could not start terminal", body["error"])
        self.assertEqual(server.CUA_TERMS, {})

    def test_term_open_409_when_already_open(self):
        self.open_term()
        status, body = self.api("POST", "/api/cua/term/open", {"name": "dev"})
        self.assertEqual(status, 409)
        self.assertIn("already open", body["error"])

    # -- send / poll round-trip

    def test_term_open_send_poll_roundtrip(self):
        term_id = self.open_term()
        proc = self.procs[-1]
        self.assertEqual(proc.cmd, ["cua", "sb", "exec", "-i", "ytl-dev", "--", "bash"])
        self.assertEqual(proc.kwargs.get("stdin"), server.subprocess.PIPE)

        status, _ = self.api("POST", f"/api/cua/term/{term_id}/send", {"line": "uname -a"})
        self.assertEqual(status, 200)
        self.assertEqual(proc.stdin.writes, ["uname -a\n"])

        proc.guest_print("Linux ytl-dev 6.8.0-31-generic #1 SMP x86_64 GNU/Linux")
        snap = self.wait_lines(term_id, 1)
        self.assertEqual(snap["status"], "open")
        self.assertEqual(snap["name"], "ytl-dev")
        self.assertEqual(snap["lines"][0],
                         {"t": "out", "x": "Linux ytl-dev 6.8.0-31-generic #1 SMP x86_64 GNU/Linux"})

        proc.guest_print("/home/cua")
        snap = self.wait_lines(term_id, 2)
        self.assertEqual([l["x"] for l in snap["lines"]],
                         ["Linux ytl-dev 6.8.0-31-generic #1 SMP x86_64 GNU/Linux",
                          "/home/cua"])
        proc.guest_print("boom", err=True)
        snap = self.wait_lines(term_id, 3)
        self.assertEqual(snap["lines"][2], {"t": "err", "x": "boom"})

    def test_term_send_unknown_id_404(self):
        status, _ = self.api("POST", "/api/cua/term/nope/send", {"line": "ls"})
        self.assertEqual(status, 404)

    def test_term_send_requires_line(self):
        term_id = self.open_term()
        status, _ = self.api("POST", f"/api/cua/term/{term_id}/send", {})
        self.assertEqual(status, 400)

    # -- closing: bash exits, idle timeout, close endpoint

    def test_term_closed_when_bash_exits(self):
        term_id = self.open_term()
        self.procs[-1].finish(0)
        snap = self.wait_status(term_id, "closed")
        self.assertEqual(snap["exit_code"], 0)
        status, body = self.api("POST", f"/api/cua/term/{term_id}/send", {"line": "ls"})
        self.assertEqual(status, 410)
        self.assertIn("closed", body["error"])

    def test_term_idle_timeout_kills_the_bash(self):
        term_id = self.open_term()
        proc = self.procs[-1]
        with mock.patch.object(server, "CUA_TERM_IDLE_SECONDS", -1):
            self.wait_status(term_id, "closed")
        self.assertEqual(proc.returncode, -9)  # killed, not exited

    def test_term_close_endpoint(self):
        term_id = self.open_term()
        proc = self.procs[-1]
        status, body = self.api("POST", f"/api/cua/term/{term_id}/close")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"ok": True})
        self.wait_status(term_id, "closed")
        self.wait_exit(term_id, -9)   # killed, not exited
        status, _ = self.api("POST", f"/api/cua/term/{term_id}/send", {"line": "ls"})
        self.assertEqual(status, 410)
        # idempotent: closing again is fine
        status, body = self.api("POST", f"/api/cua/term/{term_id}/close")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"ok": True})

    def test_term_close_unknown_id_404(self):
        status, _ = self.api("POST", "/api/cua/term/nope/close")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()