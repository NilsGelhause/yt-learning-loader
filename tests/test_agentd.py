"""Tests for the agentd sidecar integration (agentd_client + server wiring).

A stub sidecar — stdlib http.server in a thread on an ephemeral port —
stands in for agentd, so no Node, network access, or API key is needed.
The server-wiring tests spin a real server.Handler on a temp data dir,
mirroring ServerTeachAgentApiTests in test_all.py.

Run via tests/run.py.
"""

from __future__ import annotations

import http.client
import json
import re
import tempfile
import threading
import time
import unittest
import unittest.mock as mock
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agentd_client  # noqa: E402
import server  # noqa: E402


class FakeAgentProc:
    """Stand-in for subprocess.Popen result in the CLI-fallback test."""

    def __init__(self, cmd, **kwargs):
        self.cmd = cmd
        self.kwargs = kwargs
        self.pid = 424242
        self.stdout = iter(["cli line one\n"])
        self.stderr = iter([])
        self.returncode = 0

    def wait(self, timeout=None):
        return 0


class StubAgentd:
    """In-thread HTTP stub of the agentd sidecar (see agentd/src/server.ts)."""

    def __init__(self, key_configured: bool = True):
        self.jobs: dict[str, dict] = {}
        self.submitted: list[dict] = []  # decoded POST /jobs bodies, in order
        self.cancelled: list[str] = []
        self.key_configured = key_configured
        self.always_busy = False
        self.conversations: list[dict] = []  # served by GET /conversations
        stub = self

        class StubHandler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # keep stdout quiet
                pass

            def _send(self, obj, status: int = 200):
                body = json.dumps(obj).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/health":
                    return self._send({"ok": True, "keyConfigured": stub.key_configured,
                                       "model": "kimi-coding/kimi-for-coding"})
                if self.path == "/conversations":
                    return self._send({"conversations": stub.conversations})
                m = re.fullmatch(r"/jobs/([^/]+)", self.path)
                if m:
                    job = stub.jobs.get(m.group(1))
                    if not job:
                        return self._send({"error": "not found"}, 404)
                    return self._send({"status": job["status"], "lines": job["lines"],
                                       "exitCode": 0 if job["status"] == "done" else None,
                                       "finalText": job.get("finalText")})
                self._send({"error": "not found"}, 404)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                if self.path == "/jobs":
                    if stub.always_busy:
                        return self._send({"error": "busy"}, 409)
                    if not body.get("key") or not body.get("message"):
                        return self._send({"error": "key and message are required strings"}, 400)
                    job_id = f"stubjob-{len(stub.jobs) + 1}"
                    stub.jobs[job_id] = {"status": "running", "lines": []}
                    stub.submitted.append(dict(body, jobId=job_id))
                    return self._send({"jobId": job_id})
                m = re.fullmatch(r"/jobs/([^/]+)/cancel", self.path)
                if m and m.group(1) in stub.jobs:
                    stub.cancelled.append(m.group(1))
                    stub.jobs[m.group(1)]["status"] = "cancelled"
                    return self._send({"ok": True})
                self._send({"error": "not found"}, 404)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), StubHandler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def finish(self, job_id: str, status: str = "done",
               lines: list[dict] | None = None, final_text: str | None = None):
        job = self.jobs[job_id]
        job["status"] = status
        if lines is not None:
            job["lines"] = lines
        job["finalText"] = final_text

    def stop(self):
        self.httpd.shutdown()
        self.thread.join(timeout=5)
        self.httpd.server_close()


# ---------------------------------------------------------------- agentd_client

class AgentdClientTests(unittest.TestCase):
    """Pure client tests against the stub sidecar (no server.py)."""

    def setUp(self):
        self.stub = StubAgentd()
        self._old_base = agentd_client._BASE
        agentd_client._BASE = self.stub.base
        agentd_client.reset_cache()

    def tearDown(self):
        agentd_client._BASE = self._old_base
        agentd_client.reset_cache()
        self.stub.stop()

    def test_health_parses_response(self):
        """health() returns the decoded /health dict from a live sidecar."""
        h = agentd_client.health()
        self.assertIsInstance(h, dict)
        self.assertTrue(h["ok"])
        self.assertTrue(h["keyConfigured"])

    def test_health_failure_returns_none(self):
        """health() is None against a dead port and never raises."""
        agentd_client._BASE = "http://127.0.0.1:1"
        self.assertIsNone(agentd_client.health())

    def test_port_default_and_settings_override(self):
        """port() reads agentd_port from settings.json, else the default."""
        self.assertEqual(agentd_client.port(), 8472)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data").mkdir()
            (root / "data" / "settings.json").write_text(
                json.dumps({"agentd_port": 9999}), encoding="utf-8")
            with mock.patch.object(agentd_client, "ROOT", root):
                self.assertEqual(agentd_client.port(), 9999)

    def test_backend_durable_when_up_with_key(self):
        """backend() is 'durable' when /health is ok and a key is configured."""
        self.assertEqual(agentd_client.backend(), "durable")

    def test_backend_cli_when_key_not_configured(self):
        """backend() falls back to 'cli' when the sidecar has no key."""
        self.stub.key_configured = False
        agentd_client.reset_cache()
        self.assertEqual(agentd_client.backend(), "cli")

    def test_backend_cli_when_sidecar_down(self):
        """backend() is 'cli' when the sidecar doesn't answer at all."""
        agentd_client._BASE = "http://127.0.0.1:1"
        agentd_client.reset_cache()
        with mock.patch.object(agentd_client.shutil, "which", return_value=None):
            self.assertEqual(agentd_client.backend(), "cli")

    def test_submit_returns_job_id_and_posts_fields(self):
        """submit() posts key/cwd/message and returns the remote job id."""
        job_id = agentd_client.submit("teach:ws1", "/some/dir", "hello",
                                      new_session=True, request_id="req-1")
        self.assertEqual(job_id, "stubjob-1")
        body = self.stub.submitted[0]
        self.assertEqual(body["key"], "teach:ws1")
        self.assertEqual(body["cwd"], "/some/dir")
        self.assertEqual(body["message"], "hello")
        self.assertTrue(body["newSession"])
        self.assertEqual(body["requestId"], "req-1")

    def test_submit_without_new_session_omits_flag(self):
        """new_session=False leaves newSession out of the request body."""
        agentd_client.submit("k", "/d", "m")
        self.assertNotIn("newSession", self.stub.submitted[0])

    def test_submit_busy_raises_busy_error(self):
        """A 409 from the sidecar maps to BusyError."""
        self.stub.always_busy = True
        with self.assertRaises(agentd_client.BusyError):
            agentd_client.submit("k", "/d", "m")

    def test_submit_missing_fields_raises_http_error(self):
        """A 400 from the sidecar propagates as HTTPError (not BusyError)."""
        with self.assertRaises(urllib.error.HTTPError):
            agentd_client.submit("k", "/d", "")

    def test_poll_returns_job_and_unknown_is_none(self):
        """poll() returns the job dict, None for unknown ids/failures."""
        job_id = agentd_client.submit("k", "/d", "m")
        self.stub.finish(job_id, "done", lines=[{"t": "out", "x": "hi"}])
        snap = agentd_client.poll(job_id)
        self.assertEqual(snap["status"], "done")
        self.assertEqual(snap["lines"], [{"t": "out", "x": "hi"}])
        self.assertIsNone(agentd_client.poll("nope"))

    def test_cancel_proxies_to_sidecar(self):
        """cancel() posts to the sidecar and reports success/failure."""
        job_id = agentd_client.submit("k", "/d", "m")
        self.assertTrue(agentd_client.cancel(job_id))
        self.assertEqual(self.stub.cancelled, [job_id])
        self.assertFalse(agentd_client.cancel("nope"))

    def test_spawn_uses_shell_for_npx_shim(self):
        """_spawn runs npx through the shell (Windows .CMD shim) and probes
        /health afterwards."""
        popen = mock.MagicMock()
        with mock.patch.object(agentd_client.shutil, "which", return_value=r"C:\nodejs\npx.CMD"), \
             mock.patch.object(agentd_client.subprocess, "Popen", popen), \
             mock.patch.object(agentd_client, "health", return_value={"ok": True}):
            self.assertTrue(agentd_client._spawn())
        args, kwargs = popen.call_args
        self.assertEqual(args[0], "npx tsx src/server.ts")
        self.assertTrue(kwargs["shell"])
        self.assertEqual(kwargs["cwd"], str(agentd_client.AGENTD_DIR))

    def test_spawn_without_npx_is_safe_noop(self):
        """No npx on PATH -> _spawn returns False without spawning anything."""
        with mock.patch.object(agentd_client.shutil, "which", return_value=None), \
             mock.patch.object(agentd_client.subprocess, "Popen") as popen:
            self.assertFalse(agentd_client._spawn())
        popen.assert_not_called()


# ---------------------------------------------------------------- server wiring

class AgentdServerTests(unittest.TestCase):
    """server.py wiring against the stub sidecar: status modes, durable job
    streaming, cancel proxying, and the run_analysis durable branches."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        data = root / "data"
        for d in ("learnings", "transcripts", "thumbs", "info", "progress"):
            (data / d).mkdir(parents=True)
        (root / "teach").mkdir()
        (root / "skills").mkdir()
        static = root / "static"
        static.mkdir()
        (static / "index.html").write_text("<html>ok</html>", encoding="utf-8")
        cls.root = root
        cls.data = data

        videos = {
            "vidA": {"id": "vidA", "title": "Alpha Video", "channel": "Chan",
                     "url": "https://www.youtube.com/watch?v=vidA",
                     "published": "20240202", "learned": False, "added": "2024-01-01"},
        }
        (data / "videos.json").write_text(json.dumps({"videos": videos}), encoding="utf-8")
        (data / "learnings" / "vidA.json").write_text(json.dumps({
            "video_id": "vidA", "title": "Alpha Video", "summary": "Sum",
            "skills": [{"name": "Python"}],
        }), encoding="utf-8")
        ws1 = root / "teach" / "ws1"
        ws1.mkdir()
        (ws1 / "MISSION.md").write_text("# Mission: Ws One\n", encoding="utf-8")
        (data / "settings.json").write_text("{}", encoding="utf-8")

        cls.stub = StubAgentd()

        cls.patchers = [
            mock.patch.object(server, "VIDEOS_JSON", data / "videos.json"),
            mock.patch.object(server, "SETTINGS_JSON", data / "settings.json"),
            mock.patch.object(server, "JOBS_JSON", data / "jobs.json"),
            mock.patch.object(server, "PROFILE_SKILLS_JSON", data / "profile-skills.json"),
            mock.patch.object(server, "TEST_RESULTS_JSON", data / "test_results.json"),
            mock.patch.object(server, "LEARNINGS_DIR", data / "learnings"),
            mock.patch.object(server, "TRANSCRIPT_DIR", data / "transcripts"),
            mock.patch.object(server, "THUMB_DIR", data / "thumbs"),
            mock.patch.object(server, "INFO_DIR", data / "info"),
            mock.patch.object(server, "PROGRESS_DIR", data / "progress"),
            mock.patch.object(server, "TEACH_DIR", root / "teach"),
            mock.patch.object(server, "SKILLS_DIR", root / "skills"),
            mock.patch.object(server, "STATIC_DIR", static),
            mock.patch.object(server, "JOBS", {}),
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

    def tearDown(self):
        agentd_client.reset_cache()
        self.stub.key_configured = True
        self.stub.always_busy = False

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

    # -- /api/agent/status modes

    def test_agent_status_pi_durable_mode(self):
        """With the sidecar up and keyed, status reports mode pi-durable."""
        status, body = self.api("GET", "/api/agent/status")
        self.assertEqual(status, 200)
        self.assertTrue(body["available"])
        self.assertEqual(body["mode"], "pi-durable")

    def test_agent_status_kimi_cli_mode(self):
        """Without the sidecar, status reports mode kimi-cli when the CLI exists."""
        self.stub.key_configured = False
        agentd_client.reset_cache()
        with mock.patch.object(server, "find_kimi", return_value="/fake/kimi"):
            status, body = self.api("GET", "/api/agent/status")
        self.assertEqual(status, 200)
        self.assertTrue(body["available"])
        self.assertEqual(body["mode"], "kimi-cli")
        self.assertEqual(body["exe"], "/fake/kimi")

    def test_agent_status_unavailable_mode_none(self):
        """With neither backend available, mode is None and available false."""
        self.stub.key_configured = False
        agentd_client.reset_cache()
        with mock.patch.object(server, "find_kimi", return_value=None):
            status, body = self.api("GET", "/api/agent/status")
        self.assertEqual(status, 200)
        self.assertFalse(body["available"])
        self.assertIsNone(body["mode"])
        self.assertIsNone(body["exe"])

    # -- durable console jobs

    def test_agent_run_durable_streams_new_lines(self):
        """A durable run submits to the sidecar and appends only new poll
        lines into the job record, ending done with exit_code 0."""
        status, body = self.api("POST", "/api/agent/run",
                                {"workspace": "ws1", "message": "summarize"})
        self.assertEqual(status, 200)
        job_id = body["job_id"]
        submitted = self.wait_submission()
        self.assertEqual(submitted["key"], "teach:ws1")
        remote_id = submitted["jobId"]
        self.stub.jobs[remote_id]["lines"].append({"t": "out", "x": "first"})
        time.sleep(0.05)  # let the poll loop pick the line up
        self.stub.finish(remote_id, "done", lines=[
            {"t": "out", "x": "first"},
            {"t": "err", "x": "second"},
        ])
        job = self.wait_job(job_id)
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["exit_code"], 0)
        self.assertEqual([l["x"] for l in job["lines"]], ["first", "second"])
        self.assertEqual(job["lines"][1]["t"], "err")
        with server.AGENT_JOBS_LOCK:
            record = server.AGENT_JOBS[job_id]
        self.assertEqual(record["backend"], "durable")
        self.assertIn("agentd", record["cmd_preview"])

    def test_agent_run_durable_busy_maps_to_friendly_error(self):
        """A sidecar 409 at submit time settles the job as error with a
        friendly line (the endpoint-level 409 already guards the common case)."""
        self.stub.always_busy = True
        status, body = self.api("POST", "/api/agent/run",
                                {"workspace": "ws1", "message": "again"})
        self.assertEqual(status, 200)
        job = self.wait_job(body["job_id"])
        self.assertEqual(job["status"], "error")
        self.assertEqual(job["exit_code"], 1)
        self.assertTrue(any("still working" in l["x"] for l in job["lines"]))

    def test_agent_run_falls_back_to_cli_without_sidecar(self):
        """With the stub unkeyed and a kimi CLI present, runs use the CLI path."""
        self.stub.key_configured = False
        agentd_client.reset_cache()
        with mock.patch.object(server, "find_kimi", return_value="/fake/kimi"), \
             mock.patch.object(server.subprocess, "Popen", FakeAgentProc):
            status, body = self.api("POST", "/api/agent/run",
                                    {"workspace": "ws1", "message": "cli please",
                                     "new_session": True})
            self.assertEqual(status, 200)
            with server.AGENT_JOBS_LOCK:
                record = dict(server.AGENT_JOBS[body["job_id"]])
            self.assertEqual(record["backend"], "cli")
            job = self.wait_job(body["job_id"])
            self.assertEqual(job["status"], "done")
            self.assertIn("cli line one", [l["x"] for l in job["lines"]])

    def test_agent_cancel_proxies_to_sidecar(self):
        """Cancelling a durable job also cancels the remote sidecar job."""
        with server.AGENT_JOBS_LOCK:
            server.AGENT_JOBS["durjob"] = {
                "id": "durjob", "workspace": "ws1", "status": "running",
                "backend": "durable", "remote_id": "stubjob-1",
                "lines": [], "exit_code": None, "pid": None,
                "started": int(time.time()), "ended": None,
            }
        self.stub.jobs["stubjob-1"] = {"status": "running", "lines": []}
        status, _ = self.api("POST", "/api/agent/job/durjob/cancel")
        self.assertEqual(status, 200)
        self.assertEqual(self.stub.cancelled, ["stubjob-1"])
        self.assertEqual(self.stub.jobs["stubjob-1"]["status"], "cancelled")
        self.assertEqual(server.AGENT_JOBS["durjob"]["status"], "cancelled")

    def test_delete_workspace_cancels_durable_job(self):
        """The workspace-deletion sweep cancels durable jobs on the sidecar."""
        _, body = self.api("POST", "/api/teach/start", {"topic": "Durable Cancel Land"})
        ws_id = body["id"]
        with server.AGENT_JOBS_LOCK:
            server.AGENT_JOBS["fakejob9"] = {
                "id": "fakejob9", "workspace": ws_id, "status": "running",
                "backend": "durable", "remote_id": "stubjob-9",
                "lines": [], "exit_code": None, "pid": None,
                "started": int(time.time()), "ended": None,
            }
        self.stub.jobs["stubjob-9"] = {"status": "running", "lines": []}
        status, _ = self.api("DELETE", f"/api/teach/{ws_id}")
        self.assertEqual(status, 200)
        self.assertEqual(self.stub.cancelled, ["stubjob-9"])
        self.assertEqual(server.AGENT_JOBS["fakejob9"]["status"], "cancelled")

    # -- skill distillation

    def test_skill_distill_uses_durable_backend(self):
        """Turn-into-skill spawns a durable job keyed distill:<slug> even
        without the kimi CLI."""
        with mock.patch.object(server, "find_kimi", return_value=None):
            status, body = self.api("POST", "/api/skill/from_workspace",
                                    {"workspace": "ws1", "name": "Durable Skill",
                                     "distill": True})
        self.assertEqual(status, 200)
        job_id = body.get("job_id")
        self.assertTrue(job_id)
        submitted = self.wait_submission()
        self.assertTrue(submitted["key"].startswith("distill:"))
        self.assertTrue(submitted["newSession"])
        self.assertEqual(submitted["cwd"], str(server.ROOT))
        with server.AGENT_JOBS_LOCK:
            record = server.AGENT_JOBS[job_id]
        self.assertEqual(record["backend"], "durable")
        self.stub.finish(submitted["jobId"], "done")
        job = self.wait_job(job_id)
        self.assertEqual(job["status"], "done")

    # -- run_analysis durable branches

    def _run_analysis_thread(self, job: dict, ids: list[str]) -> threading.Thread:
        t = threading.Thread(target=server.run_analysis, args=(job, ids), daemon=True)
        t.start()
        return t

    def test_run_analysis_durable_happy_path(self):
        """Durable analysis submits one conversation per batch, consolidates
        every video, and finishes with the standard done message."""
        job = {"id": "analysisjob1"}
        consolidate = mock.MagicMock(return_value=False)
        with mock.patch.object(server, "consolidate_learnings_skills", consolidate), \
             mock.patch.object(server, "has_learnings", return_value=True):
            t = self._run_analysis_thread(job, ["v1", "v2"])
            submitted = self.wait_submission()
            self.assertEqual(submitted["key"], "analysis:analysisjob1:0")
            self.assertEqual(submitted["cwd"], str(server.ROOT))
            self.assertIn("data/learnings", submitted["message"])
            self.stub.finish(submitted["jobId"], "done")
            t.join(timeout=5)
        self.assertFalse(t.is_alive())
        self.assertEqual(consolidate.call_count, 2)
        self.assertEqual({c.args[0] for c in consolidate.call_args_list}, {"v1", "v2"})
        self.assertEqual(job["phase"], "done")
        self.assertEqual(job["message"], "Done. Scraped and analyzed 2 videos.")

    def test_run_analysis_durable_error_continues_with_progress(self):
        """A failed batch doesn't stop the run when a video already has
        learnings — the next batch is still submitted."""
        job = {"id": "analysisjob2"}
        ids = [f"v{i}" for i in range(7)]  # two batches of 6 + 1
        consolidate = mock.MagicMock(return_value=False)
        with mock.patch.object(server, "consolidate_learnings_skills", consolidate), \
             mock.patch.object(server, "has_learnings", return_value=True):
            t = self._run_analysis_thread(job, ids)
            first = self.wait_submission(1)
            self.stub.finish(first["jobId"], "error",
                             lines=[{"t": "err", "x": "401 account suspended"}])
            second = self.wait_submission(2)
            self.assertEqual(second["key"], "analysis:analysisjob2:1")
            self.stub.finish(second["jobId"], "done")
            t.join(timeout=5)
        self.assertFalse(t.is_alive())
        self.assertEqual(consolidate.call_count, 7)
        self.assertEqual(job["message"], "Done. Scraped and analyzed 7 videos.")

    def test_run_analysis_durable_error_without_progress_stops(self):
        """A failed batch with zero learnings stops the run with the standard
        'analysis stopped early' message."""
        job = {"id": "analysisjob3"}
        consolidate = mock.MagicMock(return_value=False)
        with mock.patch.object(server, "consolidate_learnings_skills", consolidate), \
             mock.patch.object(server, "has_learnings", return_value=False):
            t = self._run_analysis_thread(job, ["v1"])
            submitted = self.wait_submission()
            self.stub.finish(submitted["jobId"], "error",
                             lines=[{"t": "err", "x": "401 account suspended"}])
            t.join(timeout=5)
        self.assertFalse(t.is_alive())
        self.assertEqual(len(self.stub.submitted), 1)  # no second batch attempted
        self.assertEqual(job["phase"], "done")
        self.assertIn("analysis stopped early", job["message"])
        self.assertIn("0/1", job["message"])
