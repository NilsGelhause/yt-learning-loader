"""Thin stdlib HTTP client for the agentd sidecar (Pi Durable agent harness).

The sidecar (agentd/, `npx tsx src/server.ts`) keeps long-lived model
conversations on 127.0.0.1:<agentd_port> (default 8472, setting
`agentd_port`). server.py prefers it over the kimi CLI when it is up AND a
key is configured, and falls back to the CLI otherwise — every function
here is safe to call when the sidecar or Node is missing (returns
None/False/"cli", never raises).

Tests override _BASE to point at a stub sidecar on an ephemeral port.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
AGENTD_DIR = ROOT / "agentd"
BOOT_LOG = ROOT / "data" / "agent" / "agentd.boot.log"
DEFAULT_PORT = 8472
PROBE_TIMEOUT = 2    # seconds for a single /health or /jobs/<id> call
STARTUP_TIMEOUT = 10  # seconds to wait for a freshly spawned sidecar
CACHE_TTL = 5.0       # seconds the ensure_running() decision is cached

_BASE: str | None = None  # test override, e.g. "http://127.0.0.1:<port>"

_cache_lock = threading.Lock()
_cache: dict = {"until": 0.0, "ok": False}


class BusyError(Exception):
    """The sidecar rejected a job because its conversation is busy (409)."""


def reset_cache() -> None:
    """Drop the ensure_running() cache (tests flip the stub up/down)."""
    with _cache_lock:
        _cache["until"] = 0.0


def port() -> int:
    """agentd_port from data/settings.json, else the default."""
    try:
        data = json.loads((ROOT / "data" / "settings.json").read_text(encoding="utf-8"))
        return int(data.get("agentd_port") or DEFAULT_PORT)
    except Exception:
        return DEFAULT_PORT


def _base() -> str:
    return _BASE or f"http://127.0.0.1:{port()}"


def health() -> dict | None:
    """GET /health -> dict, or None on any failure (never raises)."""
    try:
        with urllib.request.urlopen(_base() + "/health", timeout=PROBE_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _spawn() -> bool:
    """Launch `npx tsx src/server.ts` detached from agentd/. False when
    Node/npx is missing or the spawn fails — a no-op safe call. shell=True
    because on Windows npx is a .CMD shim CreateProcess can't run directly."""
    if not shutil.which("npx"):
        return False
    try:
        BOOT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(BOOT_LOG, "ab") as log:
            subprocess.Popen(
                "npx tsx src/server.ts",
                cwd=str(AGENTD_DIR), stdout=log, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, close_fds=True, shell=True,
                creationflags=(getattr(subprocess, "DETACHED_PROCESS", 0)
                               | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)),
            )
    except Exception:
        return False
    deadline = time.monotonic() + STARTUP_TIMEOUT
    while time.monotonic() < deadline:
        if health():
            return True
        time.sleep(0.5)
    return False


def ensure_running() -> bool:
    """True when /health answers (spawning the sidecar first if needed).
    Cached ~5s; never raises."""
    with _cache_lock:
        if time.monotonic() < _cache["until"]:
            return _cache["ok"]
    ok = bool(health()) or _spawn()
    with _cache_lock:
        _cache["until"] = time.monotonic() + CACHE_TTL
        _cache["ok"] = ok
    return ok


def backend() -> str:
    """"durable" when the sidecar is up with a key configured, else "cli"."""
    h = health() if ensure_running() else None
    if h and h.get("ok") and h.get("keyConfigured"):
        return "durable"
    return "cli"


def submit(key: str, cwd: str, message: str, new_session: bool = False,
           request_id: str | None = None) -> str:
    """POST /jobs -> remote job id. Raises BusyError on 409."""
    body: dict = {"key": key, "cwd": str(cwd), "message": message}
    if new_session:
        body["newSession"] = True
    if request_id:
        body["requestId"] = request_id
    req = urllib.request.Request(
        _base() + "/jobs", data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 409:
            raise BusyError("busy") from e
        raise
    return str(data["jobId"])


def poll(job_id: str) -> dict | None:
    """GET /jobs/<id> -> dict, or None on 404/failure (never raises)."""
    try:
        with urllib.request.urlopen(f"{_base()}/jobs/{job_id}", timeout=PROBE_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def cancel(job_id: str) -> bool:
    """POST /jobs/<id>/cancel. False when the sidecar doesn't answer."""
    try:
        req = urllib.request.Request(
            f"{_base()}/jobs/{job_id}/cancel", data=b"{}",
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT):
            return True
    except Exception:
        return False


def list_conversations() -> list | None:
    """GET /conversations -> list of conversation dicts (key/id/cwd/busy/
    lastStatus), or None on any failure (never raises) — the Network tab's
    source of live session state."""
    try:
        with urllib.request.urlopen(_base() + "/conversations", timeout=PROBE_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        convs = data.get("conversations") if isinstance(data, dict) else None
        return convs if isinstance(convs, list) else None
    except Exception:
        return None
