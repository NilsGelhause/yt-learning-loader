"""Tests for the Console tab's standalone agent endpoint and the Network tab's
/api/network view, plus the settings merge that gives existing installs the two
new default tabs.

Reuses the stub sidecar from tests.test_agentd; the server-wiring tests spin a
real server.Handler on a temp data dir, mirroring AgentdServerTests.

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

import agentd_client  # noqa: E402
import server  # noqa: E402
from tests.test_agentd import FakeAgentProc, StubAgentd  # noqa: E402


class AgentdListConversationsTests(unittest.TestCase):
    """The list_conversations() client call against the stub sidecar."""

    def setUp(self):
        self.stub = StubAgentd()
        self._old_base = agentd_client._BASE
        agentd_client._BASE = self.stub.base
        agentd_client.reset_cache()

    def tearDown(self):
        agentd_client._BASE = self._old_base
        agentd_client.reset_cache()
        self.stub.stop()

    def test_list_conversations_parses_response(self):
        """list_conversations() returns the decoded conversation list."""
        self.stub.conversations = [
            {"key": "teach:ws1", "id": "c1", "cwd": "/app/teach/ws1",
             "busy": True, "lastStatus": "running"},
        ]
        convs = agentd_client.list_conversations()
        self.assertEqual(convs, self.stub.conversations)

    def test_list_conversations_none_when_down(self):
        """list_conversations() is None on any failure (never raises)."""
        agentd_client._BASE = "http://127.0.0.1:1"
        self.assertIsNone(agentd_client.list_conversations())


class ConsoleNetworkServerTests(unittest.TestCase):
    """server.py wiring: POST /api/agent/console/run and GET /api/network
    against the stub sidecar on a temp data dir."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        data = root / "data"
        for d in ("learnings", "transcripts", "thumbs", "info", "progress"):
            (data / d).mkdir(parents=True)
        (root / "teach").mkdir()
        (root / "teach" / "my-ws").mkdir()
        (root / "skills").mkdir()
        static = root / "static"
        static.mkdir()
        (static / "index.html").write_text("<html>ok</html>", encoding="utf-8")
        cls.root = root
        cls.data = data
        (data / "settings.json").write_text("{}", encoding="utf-8")

        cls.stub = StubAgentd()

        cls.patchers = [
            mock.patch.object(server, "SETTINGS_JSON", data / "settings.json"),
            mock.patch.object(server, "AGENT_CONVERSATIONS_JSON",
                              data / "agent" / "conversations.json"),
            mock.patch.object(server, "TEACH_DIR", root / "teach"),
            mock.patch.object(server, "AGENT_JOBS", {}),
            mock.patch.object(server, "AGENT_POLL_INTERVAL", 0.01),
            mock.patch.object(server.agentd_client, "_BASE", cls.stub.base),
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
        agentd_client.reset_cache()
        self.stub.jobs.clear()
        self.stub.submitted.clear()
        self.stub.cancelled.clear()
        self.stub.conversations = []
        conv_file = self.data / "agent" / "conversations.json"
        if conv_file.exists():
            conv_file.unlink()

    def tearDown(self):
        agentd_client.reset_cache()
        self.stub.key_configured = True
        self.stub.always_busy = False
        with server.AGENT_JOBS_LOCK:
            server.AGENT_JOBS.clear()

    def api(self, method, path, body=None, want_json=True):
        """Send one request on a fresh connection and return (status, data)."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"} if payload is not None else {}
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        ctype = resp.getheader("Content-Type") or ""
        status = resp.status
        conn.close()
        if want_json and "application/json" in ctype:
            return status, json.loads(raw.decode("utf-8"))
        return status, raw

    def wait_job(self, job_id, timeout: float = 5) -> dict:
        """Poll /api/agent/job/<id> until the job settles; return the snapshot."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status, job = self.api("GET", f"/api/agent/job/{job_id}")
            self.assertEqual(status, 200)
            if job["status"] in ("done", "error", "cancelled"):
                return job
            time.sleep(0.02)
        self.fail(f"job {job_id} did not settle")

    def wait_submission(self, count: int = 1, timeout: float = 5) -> dict:
        """Wait until the stub has seen `count` job submissions; return the last."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(self.stub.submitted) >= count:
                return self.stub.submitted[count - 1]
            time.sleep(0.02)
        self.fail(f"sidecar saw no submission in {timeout}s")

    def write_conversations(self, conv: dict):
        path = self.data / "agent" / "conversations.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(conv), encoding="utf-8")

    # -- POST /api/agent/console/run

    def test_console_run_durable_submits_main_key(self):
        """The console run submits key console:main with cwd=ROOT and streams
        the job to completion like a teach job."""
        status, body = self.api("POST", "/api/agent/console/run",
                                {"message": "what does scraper.py do?"})
        self.assertEqual(status, 200)
        job_id = body["job_id"]
        submitted = self.wait_submission()
        self.assertEqual(submitted["key"], "console:main")
        self.assertEqual(submitted["cwd"], str(server.ROOT))
        self.assertNotIn("newSession", submitted)
        self.stub.finish(submitted["jobId"], "done",
                         lines=[{"t": "out", "x": "it scrapes channels"}])
        job = self.wait_job(job_id)
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["exit_code"], 0)
        self.assertIsNone(job["workspace"])
        self.assertEqual([l["x"] for l in job["lines"]], ["it scrapes channels"])
        with server.AGENT_JOBS_LOCK:
            record = server.AGENT_JOBS[job_id]
        self.assertEqual(record["key"], "console:main")
        self.assertEqual(record["backend"], "durable")
        self.assertIn("agentd", record["cmd_preview"])

    def test_console_run_new_session_flag_passed_through(self):
        """new_session: true maps to the sidecar's newSession flag."""
        status, body = self.api("POST", "/api/agent/console/run",
                                {"message": "start over", "new_session": True})
        self.assertEqual(status, 200)
        submitted = self.wait_submission()
        self.assertTrue(submitted["newSession"])
        self.stub.finish(submitted["jobId"], "done")
        self.wait_job(body["job_id"])

    def test_console_run_busy_returns_409(self):
        """A console job still running makes the endpoint 409 without
        clobbering the running job."""
        with server.AGENT_JOBS_LOCK:
            server.AGENT_JOBS["consolebusy"] = {
                "id": "consolebusy", "workspace": None, "key": "console:main",
                "status": "running", "backend": "durable", "lines": [],
                "exit_code": None, "pid": None,
                "started": int(time.time()), "ended": None,
            }
        status, body = self.api("POST", "/api/agent/console/run",
                                {"message": "second message"})
        self.assertEqual(status, 409)
        self.assertIn("still working", body["error"])
        self.assertEqual(self.stub.submitted, [])
        with server.AGENT_JOBS_LOCK:
            self.assertEqual(server.AGENT_JOBS["consolebusy"]["status"], "running")

    def test_console_run_falls_back_to_cli(self):
        """Without the sidecar, the console runs kimi -p in ROOT."""
        self.stub.key_configured = False
        agentd_client.reset_cache()
        procs = []

        def fake_popen(cmd, **kwargs):
            proc = FakeAgentProc(cmd, **kwargs)
            procs.append(proc)
            return proc

        with mock.patch.object(server, "find_kimi", return_value="/fake/kimi"), \
             mock.patch.object(server.subprocess, "Popen", fake_popen):
            status, body = self.api("POST", "/api/agent/console/run",
                                    {"message": "cli please", "new_session": True})
            self.assertEqual(status, 200)
            with server.AGENT_JOBS_LOCK:
                record = dict(server.AGENT_JOBS[body["job_id"]])
            self.assertEqual(record["backend"], "cli")
            self.assertEqual(record["key"], "console:main")
            self.assertEqual(procs[0].kwargs["cwd"], str(server.ROOT))
            self.assertEqual(procs[0].cmd, ["/fake/kimi", "-p", "cli please"])
            job = self.wait_job(body["job_id"])
            self.assertEqual(job["status"], "done")
            self.assertIn("cli line one", [l["x"] for l in job["lines"]])

    def test_console_run_503_without_any_backend(self):
        """Neither sidecar nor CLI available -> 503."""
        self.stub.key_configured = False
        agentd_client.reset_cache()
        with mock.patch.object(server, "find_kimi", return_value=None):
            status, body = self.api("POST", "/api/agent/console/run",
                                    {"message": "hello?"})
        self.assertEqual(status, 503)
        self.assertIn("error", body)

    def test_console_run_requires_message(self):
        """A missing message is a 400."""
        status, _ = self.api("POST", "/api/agent/console/run", {})
        self.assertEqual(status, 400)

    # -- GET /api/network

    def test_network_merges_sidecar_with_file(self):
        """Online node: sidecar data wins (busy honored), file-only keys are
        appended as idle, and titles derive from the key prefixes."""
        self.write_conversations({
            "teach:my-ws": {"id": "c1", "cwd": "/app/teach/my-ws"},
            "console:main": {"id": "c2", "cwd": "/app"},
            "distill:docker-basics": {"id": "c3", "cwd": "/app"},
            "analysis:job9:0": {"id": "c4", "cwd": "/app"},
        })
        self.stub.conversations = [
            {"key": "teach:my-ws", "id": "c1", "cwd": "/app/teach/my-ws",
             "busy": True, "lastStatus": "running"},
            {"key": "console:main", "id": "c2", "cwd": "/app",
             "busy": False, "lastStatus": "done"},
        ]
        status, body = self.api("GET", "/api/network")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["nodes"]), 1)
        node = body["nodes"][0]
        self.assertEqual(node["id"], "local")
        self.assertEqual(node["name"], "local")
        self.assertEqual(node["kind"], "local")
        self.assertTrue(node["online"])
        sessions = {s["key"]: s for s in node["sessions"]}
        self.assertEqual(set(sessions), {
            "teach:my-ws", "console:main",
            "distill:docker-basics", "analysis:job9:0",
        })
        teach = sessions["teach:my-ws"]
        self.assertEqual(teach["type"], "teach")
        self.assertEqual(teach["title"], "my-ws")
        self.assertTrue(teach["busy"])
        self.assertEqual(teach["status"], "running")
        self.assertEqual(teach["cwd"], "/app/teach/my-ws")
        console = sessions["console:main"]
        self.assertEqual(console["type"], "console")
        self.assertEqual(console["title"], "Console")
        self.assertFalse(console["busy"])
        self.assertEqual(console["status"], "done")
        distill = sessions["distill:docker-basics"]
        self.assertEqual(distill["type"], "distill")
        self.assertEqual(distill["title"], "docker-basics")
        self.assertFalse(distill["busy"])
        self.assertIsNone(distill["status"])
        analysis = sessions["analysis:job9:0"]
        self.assertEqual(analysis["type"], "analysis")
        self.assertEqual(analysis["title"], "Analysis job9:0")

    def test_network_sidecar_down_uses_file(self):
        """Offline node: sessions still listed from conversations.json,
        all idle with status None."""
        self.write_conversations({
            "teach:my-ws": {"id": "c1", "cwd": "/app/teach/my-ws"},
        })
        with mock.patch.object(agentd_client, "list_conversations", return_value=None):
            status, body = self.api("GET", "/api/network")
        self.assertEqual(status, 200)
        node = body["nodes"][0]
        self.assertFalse(node["online"])
        self.assertEqual(len(node["sessions"]), 1)
        session = node["sessions"][0]
        self.assertEqual(session["key"], "teach:my-ws")
        self.assertEqual(session["title"], "my-ws")
        self.assertFalse(session["busy"])
        self.assertIsNone(session["status"])

    def test_network_hides_teach_sessions_of_deleted_workspaces(self):
        """A teach conversation whose workspace directory is gone is not
        listed (transcript stays in storage; the view only hides it)."""
        self.write_conversations({
            "teach:my-ws": {"id": "c1", "cwd": "/app/teach/my-ws"},
            "teach:ghost-ws": {"id": "c2", "cwd": "/app/teach/ghost-ws"},
            "console:main": {"id": "c3", "cwd": "/app"},
        })
        status, body = self.api("GET", "/api/network")
        self.assertEqual(status, 200)
        sessions = {s["key"]: s for s in body["nodes"][0]["sessions"]}
        self.assertIn("teach:my-ws", sessions)      # workspace dir exists
        self.assertNotIn("teach:ghost-ws", sessions)  # workspace deleted
        self.assertIn("console:main", sessions)     # non-teach unaffected

    def test_network_empty_without_file_or_sidecar(self):
        """No conversations.json and no sidecar -> one offline node, no sessions."""
        with mock.patch.object(agentd_client, "list_conversations", return_value=None):
            status, body = self.api("GET", "/api/network")
        self.assertEqual(status, 200)
        self.assertEqual(body["nodes"], [{
            "id": "local", "name": "local", "kind": "local",
            "online": False, "sessions": [],
        }])

    # -- settings merge for the two new default tabs

    def test_settings_merge_appends_new_default_tabs(self):
        """A stored settings.json without console/network gains them, and the
        user's visibility/order edits survive."""
        stored_tabs = []
        for t in server.DEFAULT_SETTINGS["tabs"]:
            if t["id"] in ("console", "network"):
                continue
            entry = dict(t)
            if entry["id"] == "videos":
                entry["visible"] = False  # a user edit to preserve
            stored_tabs.append(entry)
        stored = {"theme": "light", "tabs": stored_tabs}
        self.write_settings(stored)
        settings = server.load_settings()
        ids = [t["id"] for t in settings["tabs"]]
        self.assertEqual(ids[:len(stored_tabs)], [t["id"] for t in stored_tabs])
        self.assertIn("console", ids)
        self.assertIn("network", ids)
        self.assertEqual(ids[-2:], ["console", "network"])
        videos = next(t for t in settings["tabs"] if t["id"] == "videos")
        self.assertFalse(videos["visible"])  # user edit preserved
        self.assertEqual(settings["theme"], "light")
        for i, t in enumerate(settings["tabs"]):
            self.assertEqual(t["order"], i)  # order normalized to position
        self.assertEqual(len(ids), len(set(ids)))  # no duplicates

    def test_settings_merge_idempotent(self):
        """Loading already-merged settings twice changes nothing."""
        self.test_settings_merge_appends_new_default_tabs()
        first = server.load_settings()
        second = server.load_settings()
        self.assertEqual(first["tabs"], second["tabs"])

    def write_settings(self, settings: dict):
        (self.data / "settings.json").write_text(
            json.dumps(settings), encoding="utf-8")
