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
import json
import tempfile
import threading
import time
import unittest
import unittest.mock as mock
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cua_client  # noqa: E402
import server  # noqa: E402


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
            {"name": "ytl-dev", "status": "running", "image": "ubuntu"},
            {"name": "ytl-old", "status": "stopped", "image": "ubuntu"},
        ])

    def test_list_parses_bare_json_array(self):
        out = json.dumps([{"name": "a", "state": "ready"}])
        proc = mock.Mock(returncode=0, stdout=out, stderr="")
        with mock.patch.object(cua_client.subprocess, "run", return_value=proc):
            sandboxes = cua_client.list_sandboxes("/fake/cua")
        self.assertEqual(sandboxes, [{"name": "a", "status": "running", "image": ""}])

    def test_list_parses_table_text(self):
        out = ("NAME        STATUS     IMAGE\n"
               "ytl-dev     running    ubuntu:latest\n"
               "ytl-old     stopped    ubuntu:latest\n")
        proc = mock.Mock(returncode=0, stdout=out, stderr="")
        with mock.patch.object(cua_client.subprocess, "run", return_value=proc):
            sandboxes = cua_client.list_sandboxes("/fake/cua")
        self.assertEqual(sandboxes, [
            {"name": "ytl-dev", "status": "running", "image": "ubuntu:latest"},
            {"name": "ytl-old", "status": "stopped", "image": "ubuntu:latest"},
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
                         ["sb", "create", "ubuntu", "--name", "ytl-dev"])
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
        self.assertEqual(seen[0], ["cua", "sb", "create", "ubuntu", "--name", "ytl-dev"])

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
                 {"name": "ytl-dev", "status": "running", "image": "ubuntu"},
                 {"name": "ytl-old", "status": "stopped", "image": "ubuntu"},
             ]):
            status, body = self.api("GET", "/api/network")
        self.assertEqual(status, 200)
        self.assertEqual([n["id"] for n in body["nodes"]],
                         ["local", "vm-ytl-dev", "vm-ytl-old"])
        vm = body["nodes"][1]
        self.assertEqual(vm["kind"], "vm")
        self.assertTrue(vm["online"])
        self.assertEqual(vm["sessions"], [])
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


if __name__ == "__main__":
    unittest.main()
