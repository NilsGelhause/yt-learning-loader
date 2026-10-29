"""Learning webapp backend. Python stdlib only (http.server, json, urllib,
subprocess, threading, mimetypes, re, uuid, time, pathlib, os, sys, socket)
plus the local stdlib-only agentd_client for the Pi Durable sidecar.

Serves static/index.html plus a JSON API over the scraper's data layout:
data/videos.json (index), data/learnings/, data/transcripts/, data/thumbs/,
data/info/, data/progress/, data/jobs.json, data/settings.json,
data/profile-skills.json, teach/<id>/ workspaces and skills/<slug>/SKILL.md.

Run: ./.venv/Scripts/python.exe server.py
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
import difflib
import io
import tarfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import agentd_client
import cua_client
import devices
import vm_nodes

ROOT = Path(__file__).parent
DATA = ROOT / "data"
LEARNINGS_DIR = DATA / "learnings"
TRANSCRIPT_DIR = DATA / "transcripts"
THUMB_DIR = DATA / "thumbs"
INFO_DIR = DATA / "info"
PROGRESS_DIR = DATA / "progress"
VIDEOS_JSON = DATA / "videos.json"
SETTINGS_JSON = DATA / "settings.json"
JOBS_JSON = DATA / "jobs.json"
PROFILE_SKILLS_JSON = DATA / "profile-skills.json"
TEACH_DIR = ROOT / "teach"
SKILLS_DIR = ROOT / "skills"
AGENT_CONVERSATIONS_JSON = DATA / "agent" / "conversations.json"
REMOTE_NODES_JSON = DATA / "remote_nodes.json"
HIDDEN_SESSIONS_JSON = DATA / "hidden_sessions.json"
KIT_DIR = ROOT / "scripts" / "laptop-vm"

KIMI_SKILLS_DIR = Path.home() / ".kimi-code" / "skills"
"""User-scope Kimi Code skills — copying a skill here makes it loadable in any
Kimi Code session via /skill:<slug>."""

PREVIEW_TEXT_EXT = {".py", ".js", ".css", ".html", ".json", ".md", ".bat",
                    ".txt", ".log", ".csv", ".toml"}
PREVIEW_CHARS = 20_000        # first-load preview cap
PREVIEW_FULL_CHARS = 200_000  # "show full file" hard cap
STATIC_DIR = ROOT / "static"

# ---------------------------------------------------------------- app structure
# Powers the "Structure" tab: an interactive map of this project.

STRUCTURE_EXCLUDE = {".venv", ".git", "__pycache__", "node_modules", ".out-of-scope"}
STRUCTURE_MAX_ENUM = 25  # dirs with more children show a count instead
STRUCTURE_MAX_FILE_BYTES = 200_000

STRUCTURE_DESCRIPTIONS = {
    "server.py": "The backend. A stdlib-only HTTP server: serves the UI, the JSON API (videos, learnings, teach workspaces, agent console, tests), drives scrape jobs and the auto-analysis that generates learnings for newly added videos.",
    "scraper.py": "The YouTube scraper. Lists channels via yt-dlp, then pulls metadata and transcripts through YouTube's Innertube API (which dodges the rate-limiting that breaks normal subtitle downloads) and writes thumbnails.",
    "launch.py": "The desktop-icon launcher. Runs the test suite, prints an overview, and only then starts the server and opens the browser.",
    "start.bat": "What the desktop shortcut points at — runs launch.py with the project's virtualenv Python.",
    "static/index.html": "The app's shell: the browser-style tab bar, the screen container, and script loading order.",
    "static/styles.css": "The whole design system: dark/light themes, tokens, panes, cards, chips, console, tree — one stylesheet.",
    "static/js/core.js": "Shared frontend plumbing: the api() fetch wrapper, toasts, DOM builder, date/duration formatters, mermaid rendering with fallback, the mini-markdown renderer, and the video/learnings caches.",
    "static/js/app.js": "Boot sequence: load settings, apply theme/accent, init mermaid, build the tab bar, route to the active tab.",
    "static/js/tabs-chrome.js": "The browser-imitating tab bar itself: hash routing, drag-to-reorder, per-tab color popovers, hide/show.",
    "static/js/tab-overview.js": "Tab 1 — the 3-pane learning inbox: scrollable video rail with search + skill filter, learnings pane, details pane, learned toggles.",
    "static/js/tab-teach.js": "Tab 2 — the teaching environment: From-Video and Freestyle modes, the live agent console (streams the kimi CLI), lesson browser with saved progress, turn-into-skill.",
    "static/js/tab-visualize.js": "Tab 3 — per-video mermaid diagram of the video's concepts, with key points.",
    "static/js/tab-relations.js": "Tab 4 — the cross-video skill graph: multi-select, search + skill filter, shared-skills legend.",
    "static/js/tab-videos.js": "Tab 5 — add videos/channels (with progress, ETA and the AI-analysis phase), sortable video table.",
    "static/js/tab-profile.js": "Tab 6 — your nils-profile.md rendered nicely, with structured editing of technical skills and raw-markdown mode.",
    "static/js/tab-learned.js": "Tab 7 — learned videos, their completed learnings, created skills, unmark.",
    "static/js/tab-skills.js": "Tab 8 — My Skills: skills you know (from learned videos) vs skills you can still learn, with teach-me jumps.",
    "static/js/tab-tests.js": "Tab 9 — the test suite: run tests live, searchable/sortable/groupable results with plain-language descriptions.",
    "static/js/tab-console.js": "Tab 11 — a standalone agent console with one persistent conversation for the whole app (no workspace): ask anything, the agent reads the app's files to answer.",
    "static/js/tab-network.js": "Tab 12 — the agent network view: one bordered card per machine (this one, plus cua sandboxes as VM nodes) with its durable-agent sessions indented inside, color-coded session-type chips, busy/idle/error status and connection hints.",
    "static/js/tab-cua.js": "Tab 13 — CUA Spaces: create Ubuntu VMs (cua sandboxes) that act as LAN clients, watch create/remove jobs stream, open a raw per-VM terminal (persistent cua sb exec bash), and connect a second Windows laptop as a device (pairing code + one PowerShell line, then Install VM stack / Create VMs / Start beacon buttons).",
    "static/js/tab-settings.js": "Tab 10 — appearance, scraping defaults, the tab manager (reorder/colors/visibility), data stats, profile path.",
    "cua_client.py": "The cua (trycua/cua) CLI client: locate the CLI, report its version, list sandboxes — tolerant of CLI drift, stdlib only. Sandboxes live outside the repo; nothing here installs or vendors cua.",
    "vm_nodes.py": "VM node agent discovery: the in-guest agentd sidecar endpoint per sandbox (published endpoints.agent, else a managed cua sb port-forward on a loopback port from a fixed range), cached /health probes, and per-node /conversations — the wiring behind 'agents inside VMs'.",
    "devices.py": "Connect device: pairing-based reverse agent for a second Windows laptop — short-lived pairing codes, device registry (persisted to data/devices.json), the three pre-built device task scripts (install VM stack / create VMs / start beacon) and the mirror of device tasks into AGENT_JOBS so the job transcript UI streams them.",
    "static/vendor/mermaid.min.js": "Vendored copy of mermaid v11 (diagram rendering) so the app works fully offline.",
    "tests/test_all.py": "The test suite — server endpoints, scraper helpers, workspace/agent flows, the skill taxonomy guard, file-preview API — against temp data dirs.",
    "tests/test_agentd.py": "The agentd integration tests — a stub sidecar (stdlib http.server on an ephemeral port) verifies the client, the durable/CLI backend selection, job streaming, cancel proxying, and the run_analysis durable branches.",
    "tests/test_console_network.py": "The Console/Network tab tests — the standalone console agent endpoint (durable/CLI/503/409 paths) and the network view (sidecar-online merge, offline fallback, settings tab merge) against the same stub sidecar.",
    "agentd_client.py": "The agentd sidecar client: health probe, auto-spawn of `npx tsx src/server.ts` (agentd/), backend selection (Pi Durable vs kimi CLI), and job submit/poll/call-cancel over HTTP on 127.0.0.1:8472.",
    "tests/run.py": "The test runner: discovers the suite, writes the JSON report the Tests tab displays, prints the console overview.",
    "tests/__init__.py": "Makes tests/ an importable package (required by the runner's discovery).",
    "data/videos.json": "The master index: every known video with title, channel, dates, duration and the learned flag.",
    "data/learnings": "One JSON per video: summary, skills, learnings with timestamps, key points, mermaid diagram — generated by the AI agent.",
    "data/transcripts": "One JSON per video: timestamped caption segments downloaded via the Innertube API.",
    "data/thumbs": "Downloaded video thumbnails (maxres → sd → hq fallback).",
    "data/info": "Raw Innertube player responses (metadata source for the index).",
    "data/settings.json": "App settings: theme, accent, scraping defaults, tab order/colors/visibility, port.",
    "data/jobs.json": "Scrape/analysis job history (last 20).",
    "data/test_results.json": "The latest test-run report shown in the Tests tab.",
    "data/profile-skills.json": "Skills added to your profile through the app.",
    "data/progress": "Teach-tab learning checklists per video (legacy location; workspace state lives in teach/<ws>/.app_state.json).",
    "teach": "Teaching workspaces — one directory per video or freestyle topic: MISSION.md, lessons/, reference/, learning-records/, assets/ and .app_state.json (your lesson progress).",
    "skills": "Skills you turned into reusable SKILL.md files (from videos or finished workspaces).",
    "nils-profile.md": "Your developer profile — the app reads it for the Profile tab and writes to it when you add skills.",
}


def _structure_symbols(path: Path) -> list[str]:
    """Top-level function/class names in a .py/.js file (cheap regex)."""
    try:
        head = path.read_text(encoding="utf-8", errors="replace")[:STRUCTURE_MAX_FILE_BYTES]
    except OSError:
        return []
    if path.suffix == ".py":
        found = re.findall(r"^(?:def|class)\s+([A-Za-z_]\w+)", head, re.MULTILINE)
    elif path.suffix == ".js":
        found = re.findall(r"^\s*(?:async\s+)?function\s+([A-Za-z_$][\w$]*)", head, re.MULTILINE)
    else:
        return []
    return found[:12]


# Lines that matter when reading a file cold: definitions with signatures,
# module constants, and route/config assignments. Max 5 per file.
_PY_HIGHLIGHT_RE = re.compile(
    r"^(?:class\s+\w+.*|def\s+\w+\(.*|@app\.|[A-Z][A-Z0-9_]{2,}\s*=.*|\w+\s*=\s*re\.compile.*|"
    r"[A-Z_]+\s*=\s*\{.*|\w+_RE\s*=.*)$"
)
_JS_HIGHLIGHT_RE = re.compile(
    r"^(?:\s*(?:async\s+)?function\s+\w+.*|const\s+[A-Z][A-Z0-9_]{2,}.*|"
    r"\s*(?:const|let)\s+(?:DEFAULT|SETTINGS|STATE|App)\w*.*)$"
)


def _structure_highlights(path: Path) -> list[str]:
    """Up to 5 trimmed, human-readable 'important lines' of a code file.
    Definitions (class/def/function with signature) come first, module-level
    constants and route/config assignments fill the remaining slots."""
    if path.suffix not in (".py", ".js"):
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")[:STRUCTURE_MAX_FILE_BYTES]
    except OSError:
        return []
    pattern = _PY_HIGHLIGHT_RE if path.suffix == ".py" else _JS_HIGHLIGHT_RE
    def_re = re.compile(r"^(?:\s*(?:async\s+)?def\s+\w+\(.*|\s*class\s+\w+.*|\s*(?:async\s+)?function\s+\w+.*)$")
    defs: list[str] = []
    consts: list[str] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("//"):
            continue
        if not pattern.match(line):
            continue
        cleaned = " ".join(stripped.split())[:140]
        if def_re.match(line):
            if cleaned not in defs:
                defs.append(cleaned)
        else:
            if cleaned not in consts:
                consts.append(cleaned)
    return (defs + consts)[:5]


def build_structure(dir_path: Path, rel: str = "") -> dict:
    """Walk the project into a JSON tree for the Structure tab."""
    entries = []
    try:
        children = sorted(dir_path.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except OSError:
        return {"name": dir_path.name, "type": "dir", "children": []}
    for child in children:
        if child.name in STRUCTURE_EXCLUDE or child.name.endswith(".pyc"):
            continue
        child_rel = f"{rel}/{child.name}" if rel else child.name
        node = {
            "name": child.name,
            "path": child_rel.replace(os.sep, "/"),
            "type": "dir" if child.is_dir() else "file",
            "description": STRUCTURE_DESCRIPTIONS.get(child_rel.replace(os.sep, "/"), ""),
        }
        if child.is_dir():
            try:
                count = sum(1 for _ in child.iterdir())
            except OSError:
                count = 0
            if count > STRUCTURE_MAX_ENUM:
                node["child_count"] = count
            else:
                node["children"] = build_structure(child, child_rel)["children"]
        else:
            try:
                node["size"] = child.stat().st_size
            except OSError:
                node["size"] = 0
            if child.suffix in (".py", ".js", ".css", ".html", ".json", ".md", ".bat", ".txt") \
                    and node["size"] <= STRUCTURE_MAX_FILE_BYTES:
                try:
                    node["lines"] = child.read_text(encoding="utf-8", errors="replace").count("\n") + 1
                except OSError:
                    pass
                node["symbols"] = _structure_symbols(child)
                node["highlights"] = _structure_highlights(child)
        entries.append(node)
    return {"name": dir_path.name, "type": "dir", "children": entries}
PYTHON_EXE = ROOT / ".venv" / "Scripts" / "python.exe"
SCRAPER = ROOT / "scraper.py"
TEST_RUNNER = ROOT / "tests" / "run.py"
TEST_RESULTS_JSON = DATA / "test_results.json"
TEST_RUN_LOCK = threading.Lock()
AUDIT_LOG = DATA / "audit.log"

# ---------------------------------------------------------------- agent (kimi CLI / agentd)
# The app can drive two agent backends: the agentd sidecar (Pi Durable
# harness, long-lived conversations) when it is up with a key configured,
# else the local `kimi` CLI (kimi -p). Teach workspaces get a console, and
# finished scrapes get auto-analyzed.
AGENT_JOBS: dict[str, dict] = {}
AGENT_JOBS_LOCK = threading.Lock()
devices.set_job_sink(AGENT_JOBS, AGENT_JOBS_LOCK)
# devices.py must never `import server` (this module is __main__ in
# production — a fresh import would create a second AGENT_JOBS registry).
AGENT_MAX_LINES = 800
CUA_JOB_TIMEOUT = 900  # cua create/remove may download images; beyond this, kill
PROVISION_STEP_TIMEOUT = 600  # per-step ceiling for Install agent (npm ci ~10s; image hiccups happen)
AGENT_POLL_INTERVAL = 1.0  # seconds between sidecar job polls

# ---------------------------------------------------------------- cua terminals
# A persistent per-sandbox raw shell: `cua sb exec -i <name> -- bash` stays
# alive as long as the host process lives, stdin reaches the guest bash, and
# every stdout/stderr line is guest output. One open terminal per sandbox;
# terms die with their proc, and a poll finding one idle for
# CUA_TERM_IDLE_SECONDS kills the bash (a forgotten pane must not hold the
# sandbox's exec session open forever).
CUA_TERMS: dict[str, dict] = {}
CUA_TERMS_LOCK = threading.Lock()
CUA_TERM_MAX_LINES = 800
CUA_TERM_IDLE_SECONDS = 15 * 60


def _term_append(term: dict, tag: str, text: str) -> None:
    with CUA_TERMS_LOCK:
        term["lines"].append({"t": tag, "x": text[:1000]})
        if len(term["lines"]) > CUA_TERM_MAX_LINES:
            del term["lines"][: len(term["lines"]) - CUA_TERM_MAX_LINES]


def _term_mark_closed(term: dict, exit_code) -> None:
    with CUA_TERMS_LOCK:
        if term["status"] == "closed":
            # a kill makes the real exit code observable a beat later — the
            # reaper's mark fills it in once wait() has reaped the proc
            if exit_code is not None and term["exit_code"] is None:
                term["exit_code"] = exit_code
            return
        term["status"] = "closed"
        term["exit_code"] = exit_code
        term["closed"] = int(time.time())


def _term_reap_idle(term: dict) -> None:
    """Kill the bash when nobody has sent or polled for longer than
    CUA_TERM_IDLE_SECONDS (lazy: checked on every poll/send)."""
    if time.monotonic() - term["last_active"] <= CUA_TERM_IDLE_SECONDS:
        return
    try:
        term["proc"].kill()
    except Exception:  # noqa: BLE001
        pass


def _term_reader(term: dict, stream, tag: str) -> None:
    try:
        for line in stream:
            # newline="" keeps guest \r\n intact on read — drop the \r here
            _term_append(term, tag, line.rstrip("\n").rstrip("\r"))
    except Exception:  # noqa: BLE001
        pass


def run_cua_term(term_id: str) -> None:
    """Reaper: wait for the bash proc and record how the session ended."""
    term = CUA_TERMS[term_id]
    try:
        code = term["proc"].wait()
    except Exception:  # noqa: BLE001
        code = None
    _term_mark_closed(term, code)


def close_term(term: dict) -> None:
    """Idempotent close: kill the bash (best effort) and mark the term closed.
    The exit code may still be unknown right after a kill — the reaper thread
    records it once the proc is reaped."""
    try:
        term["proc"].kill()
    except Exception:  # noqa: BLE001
        pass
    try:
        code = term["proc"].poll()
    except Exception:  # noqa: BLE001
        code = None
    _term_mark_closed(term, code)


def open_term(name: str):
    """Spawn a persistent bash inside sandbox `name`. Returns term_id, or a
    (status, message) tuple when the name/sandbox is bad, a terminal for this
    sandbox is already open, or the spawn failed (no registry entry then)."""
    if not cua_client.valid_name(name):
        return 400, "bad name — use lowercase letters, digits and dashes (max 40 chars)"
    try:
        sandboxes = cua_client.list_sandboxes()
    except Exception:  # noqa: BLE001
        sandboxes = []
    if not any(isinstance(s, dict) and s.get("name") == name for s in sandboxes):
        return 404, f"sandbox {name} not found"
    try:
        proc = subprocess.Popen(
            ["cua", "sb", "exec", "-i", name, "--", "bash"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
        )
    except Exception as e:  # noqa: BLE001
        return 500, f"could not start terminal: {type(e).__name__}: {e}"
    try:
        # Windows text mode would translate the \n we send into \r\n and the
        # guest bash would see "pwd\r" — force raw newlines on stdin.
        proc.stdin.reconfigure(newline="")
    except (AttributeError, OSError, ValueError):
        pass
    term_id = uuid.uuid4().hex[:12]
    term = {"id": term_id, "name": name, "status": "open", "lines": [],
            "proc": proc, "exit_code": None, "opened": int(time.time()),
            "closed": None, "last_active": time.monotonic()}
    with CUA_TERMS_LOCK:
        for existing in CUA_TERMS.values():
            if existing["name"] == name and existing["status"] == "open":
                break
        else:
            existing = None
            CUA_TERMS[term_id] = term
    if existing is not None:  # lost the race — put ours down quietly
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
        return 409, f"a terminal for {name} is already open"
    threading.Thread(target=_term_reader, args=(term, proc.stdout, "out"),
                     daemon=True).start()
    threading.Thread(target=_term_reader, args=(term, proc.stderr, "err"),
                     daemon=True).start()
    threading.Thread(target=run_cua_term, args=(term_id,), daemon=True).start()
    return term_id


def find_kimi() -> str | None:
    """Absolute path to the kimi CLI executable, or None if not installed."""
    import shutil

    try:
        return shutil.which("kimi")
    except Exception:
        return None


def safe_ws_name(name: str) -> bool:
    """Workspace ids are directory names under teach/ — keep them tame."""
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,80}", name or ""))

# RLock: guards every JSON file read/write; reentrant because index writes
# take the lock and then call read_json/write_json_atomic which also take it.
LOCK = threading.RLock()

DEFAULT_SETTINGS = {
    "port": 8471,
    "agentd_port": 8472,
    "host": "127.0.0.1",
    "lan_token": "",
    "channel_default_limit": 30,
    "request_delay": 0.8,
    "accent": "#7c5cff",
    "theme": "dark",
    "profile_path": "../nils-profile.md",
    "tabs": [
        {"id": "overview", "label": "Overview", "color": "#7c5cff", "order": 0, "visible": True},
        {"id": "teach", "label": "Teach Me", "color": "#f472b6", "order": 1, "visible": True},
        {"id": "visualize", "label": "Visualization", "color": "#22d3ee", "order": 2, "visible": True},
        {"id": "relations", "label": "Relationships", "color": "#a3e635", "order": 3, "visible": True},
        {"id": "videos", "label": "Videos", "color": "#fbbf24", "order": 4, "visible": True},
        {"id": "profile", "label": "Profile", "color": "#fb7185", "order": 5, "visible": True},
        {"id": "learned", "label": "Learned", "color": "#34d399", "order": 6, "visible": True},
        {"id": "skills", "label": "My Skills", "color": "#e879f9", "order": 7, "visible": True},
        {"id": "tests", "label": "Tests", "color": "#60a5fa", "order": 8, "visible": True},
        {"id": "structure", "label": "Structure", "color": "#f97316", "order": 9, "visible": True},
        {"id": "settings", "label": "Settings", "color": "#94a3b8", "order": 10, "visible": True},
        {"id": "console", "label": "Console", "color": "#c084fc", "order": 11, "visible": True},
        {"id": "network", "label": "Network", "color": "#facc15", "order": 12, "visible": True},
        {"id": "cua", "label": "CUA Spaces", "color": "#2dd4bf", "order": 13, "visible": True},
    ],
}

DEFAULT_TAB_BY_ID = {t["id"]: t for t in DEFAULT_SETTINGS["tabs"]}

LEVEL_HEADINGS = {
    "strong": "### Strong / Professional Experience",
    "basic": "### Basic / Touched",
    "weak": "### Weak / Gaps (be patient here, explain more)",
}

# 12 distinguishable colors for relation graphs (cycled if more videos).
PALETTE = [
    "#7c5cff", "#f472b6", "#22d3ee", "#a3e635", "#fbbf24", "#34d399",
    "#fb7185", "#94a3b8", "#60a5fa", "#f97316", "#e879f9", "#4ade80",
]


# ---------------------------------------------------------------- files

def read_json(path: Path, default):
    with LOCK:
        try:
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return default


def write_json_atomic(path: Path, obj) -> None:
    with LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)


# ---------------------------------------------------------------- settings

def load_settings() -> dict:
    saved = read_json(SETTINGS_JSON, None)
    settings = {k: v for k, v in DEFAULT_SETTINGS.items() if k != "tabs"}
    if isinstance(saved, dict):
        for k, v in saved.items():
            if k != "tabs" and k in DEFAULT_SETTINGS:
                settings[k] = v
    # tabs: merge by id, keep saved order; append unknown saved, then missing defaults
    saved_tabs = saved.get("tabs") if isinstance(saved, dict) else None
    tabs = []
    if isinstance(saved_tabs, list):
        for t in saved_tabs:
            if not isinstance(t, dict) or not isinstance(t.get("id"), str):
                continue
            merged = dict(DEFAULT_TAB_BY_ID.get(t["id"], {}))
            merged.update(t)
            tabs.append(merged)
    known = {t["id"] for t in tabs}
    for t in DEFAULT_SETTINGS["tabs"]:
        if t["id"] not in known:
            tabs.append(dict(t))
    for i, t in enumerate(tabs):  # normalize order to array position
        t["order"] = i
    settings["tabs"] = tabs
    return settings


def save_settings(settings: dict) -> None:
    write_json_atomic(SETTINGS_JSON, settings)


# ---------------------------------------------------------------- lan

_LAN_CACHE: tuple[float, list[str]] | None = None
_LAN_CACHE_TTL = 60.0


def lan_ipv4_addresses() -> list[str]:
    """Non-loopback IPv4 addresses of this machine that LAN clients can
    actually reach — physical-LAN adapters only (vEthernet/WSL/Hyper-V host
    switches like 172.27.160.1 are machine-internal, offering them sends
    devices to a dead address). Falls back to getaddrinfo when the adapter
    query fails. Cached ~1 min: the adapter query shells out to powershell,
    far too slow to run per request."""
    global _LAN_CACHE
    if _LAN_CACHE and time.monotonic() - _LAN_CACHE[0] < _LAN_CACHE_TTL:
        return _LAN_CACHE[1]
    addrs: set[str] = set()
    try:  # adapter-aware enumeration; one powershell call, cached by the OS
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-NetIPAddress -AddressFamily IPv4 | Where-Object {"
             " $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*'"
             " -and (Get-NetAdapter -InterfaceIndex $_.InterfaceIndex -ErrorAction SilentlyContinue)"
             ".Name -notmatch '^(vEthernet|WSL|Loopback|Hyper-V)' }"
             " | Select-Object -ExpandProperty IPAddress"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=15, stdin=subprocess.DEVNULL)
        if out.returncode == 0:
            for line in (out.stdout or "").splitlines():
                line = line.strip()
                if line:
                    addrs.add(line)
    except (OSError, subprocess.SubprocessError):
        pass
    if not addrs:
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                addrs.add(info[4][0])
        except OSError:
            pass
        try:  # UDP connect finds the address of the default route without traffic
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 80))
                addrs.add(s.getsockname()[0])
        except OSError:
            pass
    result = sorted(a for a in addrs if not a.startswith("127."))
    _LAN_CACHE = (time.monotonic(), result)
    return result


def lan_urls(port: int) -> list[str]:
    """http:// URLs LAN clients can use to reach the app (empty when none found)."""
    return [f"http://{ip}:{port}" for ip in lan_ipv4_addresses()]


# ---------------------------------------------------------------- remote nodes

# Nodes registered by other hosts over the LAN — the laptop-vm beacon on the
# second laptop POSTs its cua sandboxes here (see scripts/laptop-vm). Presence
# works without any agent on the remote node; an `endpoint` (its in-guest
# agentd sidecar URL) additionally buys agent/sessions info when the master
# can reach it. Entries that stop re-registering expire after REMOTE_NODE_TTL.
REMOTE_NODES: dict[str, dict] = {}
"""name -> {host, name, kind, state, endpoint, seen} — `seen` is a time.time()
stamp, refreshed by every registration from the node's host."""

REMOTE_NODES_LOCK = threading.Lock()
REMOTE_NODE_TTL = 600.0       # registrations older than 10 min are pruned
REMOTE_NODE_MAX = 20          # most remote nodes merged into /api/network
REMOTE_ONLINE_WINDOW = 90.0   # seen within this = present (beacon interval + slack)
REMOTE_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,60}")


def _remote_nodes_prune(now: float | None = None) -> None:
    """Drop entries whose last registration is older than REMOTE_NODE_TTL.
    Caller must hold REMOTE_NODES_LOCK."""
    now = time.time() if now is None else now
    for name in [n for n, e in REMOTE_NODES.items()
                 if now - float(e.get("seen") or 0) > REMOTE_NODE_TTL]:
        REMOTE_NODES.pop(name, None)


def load_remote_nodes() -> None:
    """Restore the registry from data/remote_nodes.json (called at startup)."""
    data = read_json(REMOTE_NODES_JSON, {})
    if not isinstance(data, dict):
        return
    with REMOTE_NODES_LOCK:
        for name, entry in data.items():
            if not isinstance(name, str) or not isinstance(entry, dict):
                continue
            REMOTE_NODES[name] = {
                "host": str(entry.get("host") or "remote"),
                "name": str(entry.get("name") or name),
                "kind": str(entry.get("kind") or "vm"),
                "state": str(entry.get("state") or ""),
                "endpoint": (str(entry["endpoint"])
                             if isinstance(entry.get("endpoint"), str) else None),
                "seen": float(entry.get("seen") or 0),
            }
        _remote_nodes_prune()


def register_remote_nodes(host: str, nodes: list) -> int:
    """Upsert registrations (bad names skipped, malformed endpoints dropped —
    presence still registered). Returns the number of registered nodes."""
    now = time.time()
    with REMOTE_NODES_LOCK:
        _remote_nodes_prune(now)
        registered = 0
        for raw in nodes:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name") or "")
            if not REMOTE_NAME_RE.fullmatch(name):
                continue
            endpoint = raw.get("endpoint")
            if endpoint is not None:
                endpoint = str(endpoint)
                if not (endpoint.startswith(("http://", "https://"))
                        and len(endpoint) < 200):
                    endpoint = None
            REMOTE_NODES[name] = {
                "host": host, "name": name,
                "kind": str(raw.get("kind") or "vm"),
                "state": str(raw.get("state") or ""),
                "endpoint": endpoint,
                "seen": now,
            }
            registered += 1
        write_json_atomic(REMOTE_NODES_JSON, REMOTE_NODES)
    return registered


def _remote_health(base: str) -> dict | None:
    """GET <base>/health -> dict; None on any failure (never raises)."""
    try:
        with urllib.request.urlopen(base.rstrip("/") + "/health",
                                    timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None


def _remote_conversations(base: str) -> list | None:
    """GET <base>/conversations -> list; None on any failure (never raises)."""
    try:
        with urllib.request.urlopen(base.rstrip("/") + "/conversations",
                                    timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        convs = data.get("conversations") if isinstance(data, dict) else None
        return convs if isinstance(convs, list) else None
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------- videos

def load_index() -> dict:
    data = read_json(VIDEOS_JSON, {"videos": {}})
    if not isinstance(data, dict) or not isinstance(data.get("videos"), dict):
        return {"videos": {}}
    return data


def save_index(index: dict) -> None:
    write_json_atomic(VIDEOS_JSON, index)


def has_transcript(vid: str) -> bool:
    return (TRANSCRIPT_DIR / f"{vid}.json").exists()


def has_learnings(vid: str) -> bool:
    return (LEARNINGS_DIR / f"{vid}.json").exists()


SHORT_MAX_SECONDS = 180  # YouTube counts videos up to 3 minutes as Shorts


def video_object(vid: str, video: dict) -> dict:
    obj = dict(video)
    obj["has_transcript"] = has_transcript(vid)
    obj["has_learnings"] = has_learnings(vid)
    obj["thumbnail_url"] = f"/api/thumb/{vid}"
    # YouTube treats videos up to 3 minutes as Shorts; duration is the only
    # signal the scraper reliably has for every video.
    try:
        duration = int(video.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0
    obj["is_short"] = 0 < duration <= SHORT_MAX_SECONDS
    return obj


def sorted_videos() -> list[dict]:
    index = load_index()
    vids = sorted(
        index["videos"],
        key=lambda v: str(index["videos"][v].get("published") or ""),
        reverse=True,
    )
    return [video_object(v, index["videos"][v]) for v in vids]


# ---------------------------------------------------------------- profile

def profile_path() -> Path:
    p = load_settings().get("profile_path") or DEFAULT_SETTINGS["profile_path"]
    path = Path(p)
    if not path.is_absolute():
        path = (ROOT / path).resolve()
    return path


def profile_excerpt(max_chars: int = 1500) -> str:
    """Trimmed learner profile for seeding teach-workspace MISSION.md files.
    Header + who-I-am + skills + how-I-work sections, blockquoted prose dropped."""
    p = profile_path()
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    kept, total = [], 0
    skip_prefixes = ("> ", "---")
    for line in lines:
        if line.strip() in skip_prefixes or line.startswith(skip_prefixes):
            continue
        kept.append(line)
        total += len(line) + 1
        if total >= max_chars:
            break
    return "\n".join(kept).strip()


def update_profile_markdown(md: str, skill: str, level) -> str:
    """Insert/replace a skill bullet under the right ### subsection of
    ## 2. Technical Skills. Create the subsection if a level is given but
    the heading is missing."""
    bullet = f"- **{skill}**" if level == "strong" else f"- {skill}"
    lines = md.split("\n")
    sec_start = next(
        (i for i, l in enumerate(lines) if l.strip() == "## 2. Technical Skills"), None
    )
    if sec_start is None:
        lines += ["", "## 2. Technical Skills"]
        sec_start = len(lines) - 1
    sec_end = next(
        (i for i in range(sec_start + 1, len(lines)) if lines[i].startswith("## ")),
        len(lines),
    )

    def subsections():
        return [i for i in range(sec_start + 1, sec_end) if lines[i].startswith("### ")]

    if level in LEVEL_HEADINGS:
        want = LEVEL_HEADINGS[level]
        idx = next((i for i in subsections() if lines[i].strip() == want), None)
        if idx is None:
            lines.insert(sec_end, want)
            idx = sec_end
        nxt = next(
            (i for i in range(idx + 1, len(lines)) if lines[i].startswith(("### ", "## "))),
            len(lines),
        )
        existing = next(
            (i for i in range(idx + 1, nxt) if skill.lower() in lines[i].lower() and lines[i].lstrip().startswith("-")),
            None,
        )
        if existing is not None:
            lines[existing] = bullet
        else:
            lines.insert(idx + 1, bullet)
    else:
        # no level: update in place if the skill already exists anywhere in
        # the section; otherwise file it under the first subsection.
        hit = next(
            (i for i in range(sec_start + 1, sec_end)
             if skill.lower() in lines[i].lower() and lines[i].lstrip().startswith("-")),
            None,
        )
        if hit is not None:
            lines[hit] = bullet
        else:
            subs = subsections()
            if subs:
                lines.insert(subs[0] + 1, bullet)
            else:
                lines.insert(sec_start + 1, LEVEL_HEADINGS["strong"])
                lines.insert(sec_start + 2, bullet)
    return "\n".join(lines)


# ---------------------------------------------------------------- jobs

JOBS: dict[str, dict] = {}


def persist_jobs() -> None:
    jobs = sorted(JOBS.values(), key=lambda j: j["started"])[-20:]
    write_json_atomic(JOBS_JSON, {"jobs": jobs})


def restore_jobs() -> None:
    data = read_json(JOBS_JSON, {"jobs": []})
    for j in data.get("jobs", []):
        if isinstance(j, dict) and j.get("id"):
            if j.get("status") == "running":  # stale after a restart
                j["status"] = "error"
                j["message"] = "interrupted by server restart"
            JOBS[j["id"]] = j


def job_update(job: dict, **fields) -> None:
    with LOCK:
        job.update(fields)
        persist_jobs()


def parse_progress(line: str, job: dict, settings: dict) -> None:
    m = re.match(r"Found (\d+) videos", line)
    if m:
        job["total"] = int(m.group(1))
    if re.match(r"(?:enriched|transcript)\s+\S+", line):
        job["done_count"] += 1
    remaining = max(0, job["total"] - job["done_count"])
    job["eta_seconds"] = int(remaining * (3.0 + float(settings.get("request_delay", 0.8))))


def run_job(job: dict, commands: list[list[str]]) -> None:
    last_line = ""
    try:
        before = set(load_index().get("videos", {}))
        for cmd in commands:
            proc = subprocess.Popen(
                [str(PYTHON_EXE), str(SCRAPER), *cmd],
                cwd=str(ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            for raw in proc.stdout:
                line = raw.strip()
                if not line:
                    continue
                last_line = line
                with LOCK:
                    parse_progress(line, job, load_settings())
                    persist_jobs()
            proc.wait()
            if proc.returncode != 0:
                err_tail = (proc.stderr.read() or "")[-500:]
                msg = err_tail.strip() or f"scraper exited with code {proc.returncode}"
                job_update(job, status="error", message=msg, eta_seconds=None)
                return
        job_update(job, status="done", message=last_line or "done", eta_seconds=None)
        # auto-analyze new videos so the app is populated without manual steps
        after = set(load_index().get("videos", {}))
        fresh = [v for v in (after - before) if not has_learnings(v)]
        if fresh:
            run_analysis(job, sorted(fresh))
    except Exception as e:  # noqa: BLE001 - report any failure into the job
        job_update(job, status="error", message=f"{type(e).__name__}: {e}", eta_seconds=None)


# ---------------------------------------------------------------- url validation

VIDEO_URL_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?(?:youtube\.com/watch\?.*v=|youtu\.be/|youtube\.com/shorts/)",
    re.IGNORECASE,
)
CHANNEL_URL_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?youtube\.com/(@|channel/UC|\S+/videos)",
    re.IGNORECASE,
)


def classify_urls(urls: list, channel) -> tuple[list[str], list[str]]:
    video_urls, channel_urls = [], []
    if channel:
        if not CHANNEL_URL_RE.match(channel):
            raise ValueError(f"not a channel URL: {channel}")
        channel_urls.append(channel)
    for u in urls or []:
        if VIDEO_URL_RE.match(u):
            video_urls.append(u)
        elif CHANNEL_URL_RE.match(u):
            channel_urls.append(u)
        else:
            raise ValueError(f"not a recognized YouTube URL: {u}")
    return video_urls, channel_urls


# ---------------------------------------------------------------- teach

def create_workspace(vid: str, video: dict | None, learnings: dict | None,
                     focus: str | None = None, mission_title: str | None = None):
    ws = TEACH_DIR / vid
    created = not ws.exists()
    ws.mkdir(parents=True, exist_ok=True)
    for d in ("lessons", "reference", "learning-records", "assets"):
        (ws / d).mkdir(exist_ok=True)

    title = mission_title or (video or {}).get("title") or (learnings or {}).get("title") or vid
    url = (video or {}).get("url") or f"https://www.youtube.com/watch?v={vid}"
    profile = profile_excerpt()

    mission = ws / "MISSION.md"
    if not mission.exists():
        interview_section = [
            "",
            "## Learner intent — STATUS: NOT INTERVIEWED",
            "",
            "The material is this video, but the learner's personal intent is not yet",
            "confirmed. Your FIRST job is to interview the learner: ask why they want to",
            "learn this material, what they already know about the topic, and what success",
            "looks like for them — grounded in the video's actual content. Then update this",
            "section with their answers (set STATUS: CONFIRMED) before building lessons.",
            "",
            "**The learner profile below is background only.** Use it to tailor your",
            "questions and lessons to their level — it is NOT the mission. Do not infer",
            "the learner's intent from it; interview the learner first.",
        ]
        if learnings:
            skills = learnings.get("skills") or []
            body = [f"# Mission: {title}", ""]
            summary = (learnings.get("summary") or "").strip()
            if summary:
                body += [summary, ""]
            if focus:
                skill_row = next((s for s in skills if s.get("name") == focus), None)
                body += [
                    f"## Focus skill: {focus}",
                    "",
                    (skill_row or {}).get("description") or f"Master the skill '{focus}'.",
                    "",
                ]
            body += [
                "## Skills you will build",
                "",
                *[f"- **{s.get('name', '')}** — {s.get('description', '')}" for s in skills],
                "",
                "Work through every lesson in `lessons/` and check yourself against the key points.",
            ] + interview_section
        elif mission_title:
            body = [
                f"# Mission: {mission_title}",
                "",
                "**STATUS: DRAFT — not yet confirmed with the learner.**",
                "The topic is known, but the mission has not been interviewed:",
                "ask the learner why they want to learn this, what success looks like,",
                "and what they already know — then update this file with the confirmed mission.",
                "",
                "**The learner profile below is background only.** Use it to tailor your",
                "questions and later lessons to their level — it is NOT the mission.",
                "Do not infer or finalize the mission from it; interview the learner first.",
                "",
                f"The learner wants to deeply learn: {mission_title}.",
            ]
        else:
            body = [
                f"# Mission: {title}",
                "",
                "Learn this video thoroughly: watch it, take notes in NOTES.md,",
                "and build a lesson in `lessons/` that would teach the material to your past self.",
            ] + interview_section
        if profile:
            body += [
                "",
                "## Learner profile",
                "",
                "Teach to THIS learner — match their level, background and preferences:",
                "",
                "```",
                profile,
                "```",
            ]
        mission.write_text("\n".join(body) + "\n", encoding="utf-8")

    resources = ws / "RESOURCES.md"
    if not resources.exists():
        if video is None and learnings is None:
            # freestyle topic workspace — no video exists; a placeholder link
            # sends the agent on a hunt for a nonexistent video
            resources.write_text(
                "# Resources\n\n"
                "This is a freestyle topic workspace — there is NO video or transcript.\n"
                "Populate this file with high-trust sources as the teach skill describes.\n",
                encoding="utf-8",
            )
        else:
            resources.write_text(
                f"# Resources\n\n- Video: {url}\n"
                f"- Transcript: `../../data/transcripts/{vid}.json` (relative to this workspace; the app root is ../..)"
                + (f" — MISSING, run the scraper first\n" if not has_transcript(vid) else "\n"),
                encoding="utf-8",
            )

    notes = ws / "NOTES.md"
    if not notes.exists():
        notes.write_text(f"# Notes — {title}\n\nScratchpad. Dump ideas and questions here.\n", encoding="utf-8")

    return f"teach/{vid}", created


# ---------------------------------------------------------------- skills

def kebab(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:40].strip("-") or "video"


def skill_distill_prompt(ws: str, slug: str, name: str) -> str:
    """Prompt for the agent job that turns a baseline workspace SKILL.md into
    a distilled, loadable skill. Runs with cwd = app root."""
    return (
        f"You are distilling a finished teaching workspace into a reusable skill for a coding agent.\n"
        f"Source material: teach/{ws}/ — MISSION.md (the confirmed mission), lessons/*.html, "
        f"learning-records/*.md, NOTES.md. Read them.\n"
        f"Target: overwrite skills/{slug}/SKILL.md (the current directory is the app root). "
        f"The file exists with placeholder content — replace it entirely.\n"
        "CRITICAL — the skill's subject is the TOPIC the workspace taught (see MISSION.md), "
        "NOT the teaching process. The agent will load this skill while DOING that topic, so "
        "distill the subject-matter practices: what to actually do, decide, and check when "
        "working in this topic. Do NOT write about creating teach/ workspaces, writing lesson "
        "HTML, or this app itself — process/meta content is only in scope if the workspace's "
        "topic IS teaching or agent workflows.\n"
        "Requirements:\n"
        f"- YAML frontmatter: name: {name}; description: 1-2 sentences saying WHAT this skill "
        "provides and WHEN an agent should load it — concrete trigger situations related to the "
        "topic (e.g. \"before writing prompts for an agentic task\"). The description is how the "
        "agent decides to load the skill, so make it specific, not generic.\n"
        "- Body, under 120 lines, imperative and actionable:\n"
        f"  # {name}\n"
        "  ## When to use — 3-6 concrete trigger situations in the topic\n"
        "  ## Core practices — the distilled doing-knowledge from the lessons: what to actually DO, "
        "ordered by importance; the key rules and patterns, not lesson prose\n"
        "  ## Pitfalls — 4-8 mistakes this skill prevents\n"
        "  ## Quick reference — the 5-10 highest-signal patterns, as short bullets\n"
        f"- End with ## Deeper dive — links to the workspace files (teach/{ws}/lessons/…) for depth.\n"
        "- Keep it self-contained: a reader should be able to apply the skill without opening the lessons.\n"
        "Write the file, then reply with one line: DISTILLED skills/" + slug + "/SKILL.md"
    )


def skill_md(learnings: dict, video: dict, vid: str) -> str:
    title = video.get("title") or learnings.get("title") or vid
    url = video.get("url") or f"https://www.youtube.com/watch?v={vid}"
    summary = (learnings.get("summary") or "").strip()
    what = learnings.get("what_you_learn") or []
    points = learnings.get("key_points") or []
    lines = [
        "---",
        f"name: {title}",
        f'description: "Learnings from {title} — {url}"',
        f"video_id: {vid}",
        "---",
        "",
        f"# {title}",
        "",
    ]
    if summary:
        lines += [summary, ""]
    if what:
        lines += ["## What You Learn", "", *[f"- {w}" for w in what], ""]
    for item in learnings.get("learnings") or []:
        lines += [f"## {item.get('title', 'Learning')}", ""]
        detail = (item.get("detail") or "").strip()
        if detail:
            lines += [detail, ""]
    if points:
        lines += ["## Key Points", "", *[f"- {p}" for p in points], ""]
    lines += ["## Source", "", url, ""]
    return "\n".join(lines)


def scan_skills() -> list[dict]:
    out = []
    if not SKILLS_DIR.is_dir():
        return out
    for d in sorted(SKILLS_DIR.iterdir()):
        f = d / "SKILL.md"
        if not d.is_dir() or not f.exists():
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        name, video_id = None, None
        description, source_ws = None, None
        m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
        if m:
            fm = m.group(1)
            nm = re.search(r"^name:\s*(.+)$", fm, re.MULTILINE)
            vm = re.search(r"^video_id:\s*(\S+)", fm, re.MULTILINE)
            dm = re.search(r'^description:\s*"(.+)"$', fm, re.MULTILINE) or \
                 re.search(r"^description:\s*(.+)$", fm, re.MULTILINE)
            sm = re.search(r"^source_workspace:\s*(\S+)", fm, re.MULTILINE)
            name = nm.group(1).strip().strip('"') if nm else None
            video_id = vm.group(1).strip() if vm else None
            description = dm.group(1).strip().strip('"') if dm else None
            source_ws = sm.group(1).strip() if sm else None
        if not name:
            hm = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
            name = hm.group(1).strip() if hm else d.name
        installed, installed_stale = False, False
        inst = KIMI_SKILLS_DIR / d.name / "SKILL.md"
        if inst.is_file():
            installed = True
            try:
                installed_stale = inst.read_text(encoding="utf-8") != text
            except OSError:
                installed_stale = True
        out.append({"slug": d.name, "name": name, "video_id": video_id,
                    "description": description, "source_workspace": source_ws,
                    "installed": installed, "installed_stale": installed_stale,
                    "path": f"skills/{d.name}/SKILL.md"})
    return out


# ---------------------------------------------------------------- relations

def sanitize_label(s: str) -> str:
    return re.sub(r'["()\[\]{}]', "", s)


def build_relations(ids: list[str]) -> dict:
    skills: dict[str, list[str]] = {}
    for vid in ids:
        data = read_json(LEARNINGS_DIR / f"{vid}.json", None)
        if isinstance(data, dict):
            names = [str(s.get("name", "")).lower() for s in data.get("skills") or [] if s.get("name")]
            if names:
                skills[vid] = sorted(set(names))
    shared = []
    mermaid = ""
    note = None
    if len(skills) < 2:
        note = "fewer than 2 videos with learnings; nothing to relate"
    else:
        index = load_index()
        vids = [v for v in ids if v in skills]
        lines = ["graph LR"]
        for i, vid in enumerate(vids):
            title = sanitize_label(str(index["videos"].get(vid, {}).get("title") or vid))[:30]
            lines.append(f'  {vid}["{title}"]')
        for i, a in enumerate(vids):
            for b in vids[i + 1:]:
                common = sorted(set(skills[a]) & set(skills[b]))
                if common:
                    shared.append([a, b])
                    label = sanitize_label(", ".join(common))
                    lines.append(f'  {a} ---|"{label}"| {b}')
        for i, vid in enumerate(vids):
            color = PALETTE[i % len(PALETTE)]
            lines.append(f"  classDef c{i} fill:{color},color:#fff;")
            lines.append(f"  class {vid} c{i};")
        mermaid = "\n".join(lines)
    result = {"mermaid": mermaid, "skills": skills, "shared": shared}
    if note:
        result["note"] = note
    return result


# ---------------------------------------------------------------- skills library

def build_skills_library() -> dict:
    """Aggregate skills across all videos: 'known' from learned videos,
    'learnable' from unlearned ones. Each skill maps to its videos."""
    index = load_index()
    known: dict[str, list[dict]] = {}
    learnable: dict[str, list[dict]] = {}

    def bucket(target: dict, name: str, video: dict):
        target.setdefault(name, []).append({
            "id": video["id"], "title": video.get("title") or video["id"],
            "url": video.get("url"), "learned": bool(video.get("learned")),
        })

    for vid, video in index["videos"].items():
        learnings = read_json(LEARNINGS_DIR / f"{vid}.json", None)
        if not isinstance(learnings, dict):
            continue
        target = known if video.get("learned") else learnable
        for s in learnings.get("skills") or []:
            name = (s.get("name") or "").strip()
            if name:
                bucket(target, name, video)

    def shape(d: dict) -> list[dict]:
        out = [{"name": k, "count": len(v), "videos": v} for k, v in d.items()]
        return sorted(out, key=lambda e: (-e["count"], e["name"].lower()))

    return {"known": shape(known), "learnable": shape(learnable)}


# ---------------------------------------------------------------- skill taxonomy
# Future learnings (from newly scraped videos) must not re-bloat the library:
# freshly generated skill names are normalized onto the existing taxonomy —
# exact matches adopt the established spelling, close fuzzy matches reuse the
# existing skill, and only genuinely distinct names are kept.

SKILL_MATCH_CUTOFF = 0.82


def _skill_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()


def _skill_match(key: str, canon: dict) -> str | None:
    """Best canonical name for a skill key, or None. Exact match first, then
    token containment (multi-token subset either way), then fuzzy ratio —
    all against SKILL_MATCH_CUTOFF (containment scores 1.0)."""
    if key in canon:
        return canon[key]
    kt = set(key.split())
    best, best_score = None, 0.0
    for ck, display in canon.items():
        ct = set(ck.split())
        containment = min(len(kt), len(ct)) >= 2 and (kt <= ct or ct <= kt)
        score = 1.0 if containment else difflib.SequenceMatcher(None, key, ck).ratio()
        if score >= SKILL_MATCH_CUTOFF and score > best_score:
            best, best_score = display, score
    return best


def normalize_skills_to_taxonomy(skills: list, existing_names: list) -> list:
    """Map freshly generated skill names onto the existing taxonomy.

    Returns a new skills list: canonical spelling for exact/fuzzy matches,
    distinct new names kept as-is, in-file duplicates merged (longest
    description wins). Never mutates the input."""
    canon: dict[str, str] = {}
    for n in existing_names:
        canon.setdefault(_skill_key(n), n)
    out: list[dict] = []
    seen: set[str] = set()
    for s in skills or []:
        name = str(s.get("name") or "").strip()
        if not name:
            continue
        key = _skill_key(name)
        match = _skill_match(key, canon) if len(key) >= 4 else None
        final = match or name
        fkey = _skill_key(final)
        if fkey in seen:
            prev = next(o for o in out if _skill_key(o["name"]) == fkey)
            d_old = str(prev.get("description") or "")
            d_new = str(s.get("description") or "")
            if len(d_new) > len(d_old):
                prev["description"] = s.get("description")
            continue
        seen.add(fkey)
        out.append({"name": final, "description": s.get("description")})
    return out


def existing_skill_names(exclude_vid: str | None = None) -> list:
    """All skill names across the learnings corpus (one file optionally
    excluded — the one being normalized)."""
    names: list[str] = []
    for f in LEARNINGS_DIR.glob("*.json"):
        if exclude_vid and f.stem == exclude_vid:
            continue
        data = read_json(f, None)
        if isinstance(data, dict):
            for s in data.get("skills") or []:
                n = str(s.get("name") or "").strip()
                if n:
                    names.append(n)
    return names


def consolidate_learnings_skills(vid: str) -> bool:
    """Normalize one video's skills against the rest of the corpus. Returns
    True when the learnings file was rewritten."""
    path = LEARNINGS_DIR / f"{vid}.json"
    data = read_json(path, None)
    if not isinstance(data, dict):
        return False
    skills = data.get("skills") or []
    normalized = normalize_skills_to_taxonomy(skills, existing_skill_names(vid))
    if len(normalized) == len(skills) and [
            _skill_key(str(s.get("name") or "")) for s in skills
    ] == [_skill_key(str(s.get("name") or "")) for s in normalized]:
        return False
    data["skills"] = normalized
    write_json_atomic(path, data)
    return True


# ---------------------------------------------------------------- teach workspaces

def list_workspaces() -> list[dict]:
    """Every directory under teach/ with title, lesson count and progress."""
    out = []
    if not TEACH_DIR.is_dir():
        return out
    for d in sorted(TEACH_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not d.is_dir():
            continue
        ws_id = d.name
        title = ws_id
        mission = d / "MISSION.md"
        if mission.is_file():
            try:
                first = mission.read_text(encoding="utf-8", errors="replace").splitlines()[0]
                title = first.lstrip("# ").strip() or ws_id
            except Exception:
                pass
        lessons = sorted(p.name for p in (d / "lessons").glob("*.html")) if (d / "lessons").is_dir() else []
        state = read_json(d / ".app_state.json", {"completed": []})
        completed = [c for c in (state.get("completed") or []) if c in lessons]
        index = load_index()
        out.append({
            "id": ws_id,
            "title": title,
            "is_video": ws_id in index["videos"],
            "video_id": ws_id if ws_id in index["videos"] else None,
            "lessons": [{"file": f} for f in lessons],
            "completed": completed,
            "progress": round(len(completed) / len(lessons), 2) if lessons else 0,
        })
    return out


def workspace_state_path(ws: str) -> Path:
    return TEACH_DIR / ws / ".app_state.json"


def workspace_lesson_titles(ws_dir: Path) -> list[tuple[str, str]]:
    """[(filename, title)] for a workspace's lessons (title from <title>/<h1>)."""
    import html.parser

    class TitleGrab(html.parser.HTMLParser):
        def __init__(self):
            super().__init__()
            self.title = ""
            self._in_title = False
            self._h1 = ""
            self._in_h1 = False

        def handle_starttag(self, tag, attrs):
            self._in_title = tag == "title"
            self._in_h1 = tag == "h1" and not self._h1

        def handle_endtag(self, tag):
            if tag == "title":
                self._in_title = False
            if tag == "h1":
                self._in_h1 = False

        def handle_data(self, data):
            if self._in_title:
                self.title += data
            if self._in_h1:
                self._h1 += data

    lessons_dir = ws_dir / "lessons"
    out = []
    if not lessons_dir.is_dir():
        return out
    for f in sorted(lessons_dir.glob("*.html")):
        title = ""
        try:
            grabber = TitleGrab()
            grabber.feed(f.read_text(encoding="utf-8", errors="replace")[:20000])
            title = (grabber.title or grabber._h1 or "").strip()
        except Exception:
            pass
        out.append((f.name, title or f.stem))
    return out


# ---------------------------------------------------------------- agent jobs

_AGENT_NOISE_START = re.compile(r"^\[unexpected\] RuntimeError: workspace \S+ is not materialized")
_AGENT_NOISE_CONT = re.compile(r"^\s+(at\s|\}|\{)|^\s+code:")


def agent_noise_filter():
    """Stateful per-job filter dropping known-benign CLI noise: the multi-line
    'workspace ... is not materialized' RuntimeError the kimi CLI prints at
    checkpoint end (after the work itself succeeded). Returns line -> line|None."""
    skipping = False

    def filt(line: str):
        nonlocal skipping
        if not skipping:
            if _AGENT_NOISE_START.match(line):
                skipping = True
                return None
            return line
        if _AGENT_NOISE_CONT.match(line) or line.strip() in ("}", "{"):
            return None
        skipping = False
        return filt(line)  # first real line after the block — reprocess it

    return filt


def run_agent_job(job_id: str, cwd: Path, message: str, new_session: bool, exe: str) -> None:
    """Run an agent job in a thread, streaming output lines into the job
    record for the frontend console to poll. Backend ("durable" or "cli")
    comes from the job record; the CLI path is the historical fallback."""
    job = AGENT_JOBS[job_id]
    if job.get("backend") == "durable":
        run_agent_job_durable(job_id, cwd, message, new_session)
        return
    # flag order matters: `kimi -p --continue "msg"` parses the prompt as a
    # subcommand — the prompt must directly follow -p.
    cmd = [exe, "-p", message]
    if not new_session:
        cmd.append("--continue")
    job["cmd_preview"] = "kimi -p " + ("<msg>" if new_session else "<msg> --continue") + " | " + message[:60]
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
        )
        job["pid"] = proc.pid
        with AGENT_JOBS_LOCK:
            job["status"] = "running"
        # interleave stdout/stderr as they arrive, via two reader threads
        import queue

        q: queue.Queue = queue.Queue()

        def reader(stream, tag):
            for line in stream:
                q.put((tag, line.rstrip("\n")))
            q.put((None, None))

        threading.Thread(target=reader, args=(proc.stdout, "out"), daemon=True).start()
        threading.Thread(target=reader, args=(proc.stderr, "err"), daemon=True).start()
        open_streams = 2
        filt = agent_noise_filter()
        while open_streams:
            tag, line = q.get()
            if tag is None:
                open_streams -= 1
                continue
            line = filt(line)
            if line is None:
                continue
            with AGENT_JOBS_LOCK:
                job["lines"].append({"t": tag, "x": line[:1000]})
                if len(job["lines"]) > AGENT_MAX_LINES:
                    del job["lines"][: len(job["lines"]) - AGENT_MAX_LINES]
        proc.wait()
        with AGENT_JOBS_LOCK:
            job["exit_code"] = proc.returncode
            job["status"] = "done" if proc.returncode == 0 else "error"
    except Exception as e:  # noqa: BLE001
        with AGENT_JOBS_LOCK:
            job["status"] = "error"
            job["lines"].append({"t": "err", "x": f"{type(e).__name__}: {e}"})
    finally:
        with AGENT_JOBS_LOCK:
            job["ended"] = int(time.time())


def vm_task_instructions(vm: str, task: str) -> str:
    """System instructions for a vmtask:<vm>:<session> conversation: the agent
    thinks on this host but must run every command ON the VM via the cua CLI.
    Syntax verified against cua 0.9.0 (`cua sb exec --help`, live exec on a
    running sandbox): `cua sb exec <name> -- <command>` joins the command into
    one guest shell line; `-i` forwards stdin for tee/base64 pipelines."""
    return (
        f'You are a task agent. Your commands run ON the Ubuntu machine "{vm}" '
        "via the `cua` CLI on this host — NEVER run anything directly on this host.\n"
        "Rules:\n"
        f"- Every shell command: cua sb exec {vm} -- <command>   (example: cua sb exec {vm} -- uname -a)\n"
        f"- Read a file: cua sb exec {vm} -- cat <path>\n"
        f"- Write a text file: printf '%s' '<content>' | cua sb exec -i {vm} -- tee <path> >/dev/null\n"
        f"- Binary/multi-line content: base64-encode it locally, then: "
        f"cua sb exec {vm} -- 'echo <b64> | base64 -d > <path>'\n"
        "- Stay inside that VM. Do not touch this host's files.\n"
        "- If a command fails because the VM is stopped, report that instead of retrying forever.\n"
        "Work on this task until done, then summarize the outcome.\n"
        f"Task: {task}"
    )


def run_agent_job_durable(job_id: str, cwd: Path, message: str, new_session: bool) -> None:
    """Drive an agentd sidecar job: submit, then poll ~1s and append only NEW
    lines into the job record with the same caps as the CLI path. When the job
    carries a `node` (a cua sandbox name), the conversation lives on THAT
    machine's in-guest sidecar: submit/poll/cancel against its endpoint and
    work in the guest home — a dead node agent errors the job with a clear
    line instead of silently running locally."""
    job = AGENT_JOBS[job_id]
    key = job.get("key") or f"teach:{job['workspace']}"
    job["cmd_preview"] = "agentd " + ("<msg> (new session)" if new_session else "<msg> (continue)") \
        + " | " + message[:60]
    base = None
    node = job.get("node")
    if node:
        endpoint = vm_nodes.node_endpoint(node)
        if not endpoint or not vm_nodes.node_healthy(node):
            with AGENT_JOBS_LOCK:
                job["status"] = "error"
                job["exit_code"] = 1
                job["ended"] = int(time.time())
                job["lines"].append({"t": "err", "x":
                    f"node '{node}' has no reachable agentd agent — "
                    "open CUA Spaces and press Install agent on that VM"})
            return
        base = endpoint
        job["base"] = base
        cwd = "/home/cua"  # guest home (POSIX on purpose — Path would mangle it on Windows)
    try:
        with AGENT_JOBS_LOCK:
            job["status"] = "running"
        try:
            remote_id = agentd_client.submit(key, str(cwd), message, new_session,
                                             instructions=job.get("instructions"),
                                             base=base)
        except agentd_client.BusyError:
            with AGENT_JOBS_LOCK:
                job["status"] = "error"
                job["exit_code"] = 1
                job["lines"].append({"t": "err", "x":
                    "the agent is still working on your previous message — send again when it finishes"})
            return
        job["remote_id"] = remote_id
        seen = 0
        while True:
            snap = agentd_client.poll(remote_id, base=base)
            if snap is None:
                time.sleep(AGENT_POLL_INTERVAL)
                continue
            lines = snap.get("lines") or []
            with AGENT_JOBS_LOCK:
                for ln in lines[seen:]:
                    job["lines"].append({"t": "out" if ln.get("t") == "out" else "err",
                                         "x": str(ln.get("x", ""))[:1000]})
                    if len(job["lines"]) > AGENT_MAX_LINES:
                        del job["lines"][: len(job["lines"]) - AGENT_MAX_LINES]
                seen = len(lines)
                status = snap.get("status")
                if status in ("done", "error", "cancelled"):
                    job["exit_code"] = 0 if status == "done" else 1
                    job["status"] = status
                    break
            time.sleep(AGENT_POLL_INTERVAL)
    except Exception as e:  # noqa: BLE001
        with AGENT_JOBS_LOCK:
            job["status"] = "error"
            job["lines"].append({"t": "err", "x": f"{type(e).__name__}: {e}"})
    finally:
        with AGENT_JOBS_LOCK:
            job["ended"] = int(time.time())


DOCKER_DESKTOP = [
    r"C:\Program Files\Docker\Docker\Docker Desktop.exe",
    r"C:\Program Files (x86)\Docker\Docker\Docker Desktop.exe",
]
DOCKER_PIPE = r"\\.\pipe\dockerDesktopLinuxEngine"


def docker_engine_running() -> bool:
    # os.path.exists() cannot be used here: a live engine pipe makes
    # os.stat() raise ERROR_PIPE_BUSY (231), which exists() reports as
    # missing. The pipe existing at all — busy or not — means up.
    try:
        os.stat(DOCKER_PIPE)
        return True
    except OSError as e:
        return getattr(e, "winerror", None) == 231  # ERROR_PIPE_BUSY


def ensure_container_engine(job: dict, timeout: int = 150) -> bool:
    """Create VM uses the container engine; when Docker Desktop is not
    running (e.g. after a reboot) start it and wait — the user should never
    have to do this by hand. Streams progress lines into the job record."""
    def push(text: str) -> None:
        with AGENT_JOBS_LOCK:
            job["lines"].append({"t": "out", "x": text[:1000]})
            if len(job["lines"]) > AGENT_MAX_LINES:
                del job["lines"][: len(job["lines"]) - AGENT_MAX_LINES]

    if docker_engine_running():
        return True
    exe = next((c for c in DOCKER_DESKTOP if os.path.exists(c)), None)
    if not exe:
        push("container engine not running and Docker Desktop not found — "
             "start it manually, then retry")
        return False
    push("container engine is off — starting Docker Desktop, please wait…")
    try:
        subprocess.Popen([exe], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        push(f"could not start Docker Desktop: {e}")
        return False
    deadline = time.time() + timeout
    started = time.time()
    next_beat = 20.0
    while time.time() < deadline:
        if docker_engine_running():
            push("container engine is up — creating the VM")
            return True
        if time.time() - started >= next_beat:
            elapsed = int(time.time() - started)
            push(f"still waiting for the container engine… ({elapsed}s)")
            next_beat += 20.0
            # the pipe can lag the CLI: once we're waiting anyway, a cua
            # listing that answers is just as good a readiness signal
            if cua_client.list_sandboxes():
                push("container engine is up — creating the VM")
                return True
        time.sleep(2)
    push("Docker Desktop did not become ready in time — start it manually, then retry")
    return False


CUA_VIEW_PROCS: dict[str, subprocess.Popen] = {}
"""Sandbox name -> running `cua sb view` process (one per sandbox)."""


def _drain_cua_view(proc: subprocess.Popen, name: str) -> None:
    """Keep the viewer's stdout drained so it can never block on a full
    pipe; reap it when it exits."""
    try:
        for _ in proc.stdout:
            pass
    except Exception:  # noqa: BLE001
        pass
    CUA_VIEW_PROCS.pop(name, None)


def run_cua_create_job(job_id: str, argv: list[str]) -> None:
    """Create jobs: make sure the container engine is up first, then run."""
    if not ensure_container_engine(AGENT_JOBS[job_id]):
        with AGENT_JOBS_LOCK:
            AGENT_JOBS[job_id]["status"] = "error"
            AGENT_JOBS[job_id]["exit_code"] = 1
            AGENT_JOBS[job_id]["ended"] = int(time.time())
        return
    run_cua_job(job_id, argv)


def run_cua_job(job_id: str, argv: list[str]) -> None:
    """Run a cua CLI command (`sb create …`, `sb rm …`) in a thread, streaming
    output lines into the job record like an agent job — image downloads take
    minutes, so the CUA Spaces tab polls /api/agent/job/<id> for progress.
    Stdin is closed so a confirmation prompt errors out instead of hanging,
    and a watchdog kills commands that exceed CUA_JOB_TIMEOUT."""
    job = AGENT_JOBS[job_id]
    job["cmd_preview"] = "cua " + " ".join(argv)
    try:
        proc = subprocess.Popen(
            ["cua", *argv], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace",
        )
        job["pid"] = proc.pid
        with AGENT_JOBS_LOCK:
            job["status"] = "running"
        import queue

        q: queue.Queue = queue.Queue()

        def reader(stream, tag):
            for line in stream:
                q.put((tag, line.rstrip("\n")))
            q.put((None, None))

        threading.Thread(target=reader, args=(proc.stdout, "out"), daemon=True).start()
        threading.Thread(target=reader, args=(proc.stderr, "err"), daemon=True).start()
        open_streams = 2
        deadline = time.time() + CUA_JOB_TIMEOUT
        while open_streams:
            try:
                tag, line = q.get(timeout=5)
            except queue.Empty:
                if proc.poll() is None and time.time() > deadline:
                    with AGENT_JOBS_LOCK:
                        job["lines"].append(
                            {"t": "err", "x": f"cua {' '.join(argv)} timed out — killed"})
                    proc.kill()
                continue
            if tag is None:
                open_streams -= 1
                continue
            with AGENT_JOBS_LOCK:
                job["lines"].append({"t": tag, "x": line[:1000]})
                if len(job["lines"]) > AGENT_MAX_LINES:
                    del job["lines"][: len(job["lines"]) - AGENT_MAX_LINES]
        proc.wait()
        with AGENT_JOBS_LOCK:
            job["exit_code"] = proc.returncode
            job["status"] = "done" if proc.returncode == 0 else "error"
    except Exception as e:  # noqa: BLE001
        with AGENT_JOBS_LOCK:
            job["status"] = "error"
            job["lines"].append({"t": "err", "x": f"{type(e).__name__}: {e}"})
    finally:
        with AGENT_JOBS_LOCK:
            job["ended"] = int(time.time())


# ---------------------------------------------------------------- Install agent
# Push the agentd sidecar into a sandbox guest so the VM becomes an agent
# node (see vm_nodes.py): pack agentd/, ship it with the cua CLI, npm ci,
# start it detached on 0.0.0.0:8472, verify /health from the host. The guest
# filesystem survives suspend/resume AND a full restart — but a restart kills
# processes, so after one the start script must be re-run (press Install
# agent again; it is idempotent). The API key travels only in a chmod-600
# .env and is never printed.

START_AGENTD_SH = """#!/bin/sh
# re-runnable after a VM restart (processes die on restart, files survive)
mkdir -p /home/cua/agentd-data
export YTL_AGENT_PORT=8472
export YTL_AGENT_HOST=0.0.0.0
export YTL_AGENT_DB=/home/cua/agentd-data/harness.sqlite
cd /home/cua/agentd
setsid nohup npx tsx src/server.ts >>/home/cua/agentd-data/server.log 2>&1 </dev/null &
"""


# ---------------------------------------------------------------- /kit/agentd.tgz
# The install-agent device task downloads this and unpacks it inside a VM.
# Built on the fly from agentd/ (stdlib tarfile → gzipped bytes, ~60 s cache);
# node_modules, data, .env and *.log never leave the host. LAN-public like the
# rest of /kit — the sensitive API key rides the task script, not this archive.

_AGENTD_TGZ: dict = {}


def _agentd_tgz_filter(ti: tarfile.TarInfo):
    """tarfile add() filter: drop node_modules/, data/, .env and *.log."""
    parts = ti.name.split("/")
    base = parts[-1]
    if "node_modules" in parts or "data" in parts:
        return None
    if base == ".env" or base.endswith(".log"):
        return None
    return ti


def build_agentd_tgz() -> bytes:
    """Gzipped tar of agentd/, cached for ~60 s at module level."""
    now = time.time()
    if now - _AGENTD_TGZ.get("ts", 0) < 60:
        return _AGENTD_TGZ["bytes"]
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        tf.add(str(ROOT / "agentd"), arcname="agentd",
               filter=_agentd_tgz_filter)
    data = buf.getvalue()
    _AGENTD_TGZ["bytes"] = data
    _AGENTD_TGZ["ts"] = now
    return data


def _job_line(job: dict, tag: str, text: str) -> None:
    with AGENT_JOBS_LOCK:
        job["lines"].append({"t": tag, "x": text[:1000]})
        if len(job["lines"]) > AGENT_MAX_LINES:
            del job["lines"][: len(job["lines"]) - AGENT_MAX_LINES]


def _provision_step(job: dict, argv: list[str], label: str,
                    timeout: int = PROVISION_STEP_TIMEOUT) -> bool:
    """Run one provisioning command, streaming its output into the job.
    False (job marked error) on non-zero exit or timeout."""
    _job_line(job, "out", f"$ {label}")
    try:
        proc = subprocess.Popen(
            argv, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
        )
        job["pid"] = proc.pid
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
            _job_line(job, "err", f"{label} timed out after {timeout}s — killed")
            return False
    except Exception as e:  # noqa: BLE001
        _job_line(job, "err", f"{type(e).__name__}: {e}")
        return False
    for line in (out or "").splitlines():
        if line.strip():
            _job_line(job, "out", line.strip())
    if proc.returncode != 0:
        for line in (err or "").splitlines()[-5:]:
            if line.strip():
                _job_line(job, "err", line.strip())
        _job_line(job, "err", f"{label} exited with code {proc.returncode}")
        return False
    return True


def kimi_api_key() -> str:
    """The KIMI_API_KEY value from agentd/.env ('' when unset or unreadable).
    Device-bound: the install-agent task script embeds it, staged to a
    one-line temp file on the laptop and never printed."""
    env_path = ROOT / "agentd" / ".env"
    try:
        lines = env_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    line = next((ln.strip() for ln in lines
                 if ln.strip().startswith("KIMI_API_KEY=")), None)
    return line.split("=", 1)[1].strip() if line else ""


def _stage_key_env() -> Path | None:
    """Temp file holding ONLY the KIMI_API_KEY line from agentd/.env
    (CR-stripped), ready to ship into the guest. None when no key is set."""
    env_path = ROOT / "agentd" / ".env"
    try:
        lines = env_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    key_line = next((ln.strip() for ln in lines
                     if ln.strip().startswith("KIMI_API_KEY=")), None)
    if not key_line:
        return None
    import tempfile

    fd, tmp = tempfile.mkstemp(prefix="ytl-key-", suffix=".env")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(key_line.replace("\r", "") + "\n")
    return Path(tmp)


def run_provision_job(job_id: str, name: str) -> None:
    """The Install agent job: the 5 provisioning steps in order, each bounded
    by PROVISION_STEP_TIMEOUT, streamed into the job record."""
    job = AGENT_JOBS[job_id]
    exe = cua_client.find_cua()
    if not exe:
        with AGENT_JOBS_LOCK:
            job["status"] = "error"
            job["exit_code"] = 1
            job["ended"] = int(time.time())
        return

    def cua(*args: str) -> list[str]:
        return [exe, "sb", *args]

    def guest(*args: str) -> list[str]:
        # exec joins the args into one guest sh -c line (verified against cua)
        return cua("exec", name, "--", *args)

    ok = True
    tar_dir = key_tmp = start_tmp = None
    try:
        with AGENT_JOBS_LOCK:
            job["status"] = "running"
        # 1. pack agentd/ on the host (skip node_modules and the local .env)
        import tempfile

        tar_dir = Path(tempfile.mkdtemp(prefix="ytl-provision-"))
        tar_path = tar_dir / "agentd.tgz"
        if ok:
            ok = _provision_step(
                job, ["tar", "czf", str(tar_path), "--exclude", "node_modules",
                      "--exclude", ".env", "-C", str(ROOT), "agentd"],
                "pack agentd/")
        # 2. ship + unpack into the guest
        if ok:
            ok = _provision_step(job, cua("cp", str(tar_path), f"{name}:/home/cua/agentd.tgz"),
                                 "copy agentd.tgz into the VM")
        if ok:
            ok = _provision_step(
                job, guest("mkdir -p /home/cua/agentd && "
                           "tar xzf /home/cua/agentd.tgz -C /home/cua"),
                "unpack agentd in the VM")
        # 3. the API key — its own chmod-600 .env in the guest, never printed
        key_tmp = _stage_key_env() if ok else None
        if ok and key_tmp is None:
            _job_line(job, "err", "agentd/.env has no KIMI_API_KEY — set one before installing")
            ok = False
        if ok:
            ok = _provision_step(job, cua("cp", str(key_tmp), f"{name}:/home/cua/agentd/.env"),
                                 "copy the API key into the VM (chmod 600)")
        if ok:
            ok = _provision_step(job, guest("chmod 600 /home/cua/agentd/.env"),
                                 "chmod 600 the key file")
        # 4. dependencies (full npm ci — tsx is a devDependency)
        if ok:
            ok = _provision_step(
                job, guest("cd /home/cua/agentd && npm ci --no-audit --no-fund"),
                "npm ci in the VM")
        # 5. start script + launch + a peek at the log
        start_tmp = None
        if ok:
            fd, start_tmp_s = tempfile.mkstemp(prefix="ytl-start-", suffix=".sh")
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(START_AGENTD_SH)
            start_tmp = Path(start_tmp_s)
            ok = _provision_step(job, cua("cp", str(start_tmp), f"{name}:/home/cua/start-agentd.sh"),
                                 "copy the start script")
        if ok:
            ok = _provision_step(
                job, guest("sh /home/cua/start-agentd.sh && sleep 4 && "
                           "tail -2 /home/cua/agentd-data/server.log"),
                "start the sidecar in the VM")
        # 6. verify from the host and report the endpoint
        if ok:
            vm_nodes.reset_cache()
            endpoint = vm_nodes.node_endpoint(name)
            health = vm_nodes.node_health(name)
            if health and health.get("ok"):
                _job_line(job, "out",
                          f"agent ready at {endpoint}"
                          + (" (key configured)" if health.get("keyConfigured") else
                             " — but no API key is configured; re-install after adding one"))
                with AGENT_JOBS_LOCK:
                    job["status"] = "done"
                    job["exit_code"] = 0
            else:
                _job_line(job, "err",
                          f"sidecar did not answer at {endpoint or '?'} — "
                          "check /home/cua/agentd-data/server.log inside the VM")
                with AGENT_JOBS_LOCK:
                    job["status"] = "error"
                    job["exit_code"] = 1
        else:
            with AGENT_JOBS_LOCK:
                job["status"] = "error"
                job["exit_code"] = 1
    except Exception as e:  # noqa: BLE001
        with AGENT_JOBS_LOCK:
            job["status"] = "error"
            job["exit_code"] = 1
        _job_line(job, "err", f"{type(e).__name__}: {e}")
    finally:
        with AGENT_JOBS_LOCK:
            job["ended"] = int(time.time())
        import shutil

        for tmp in (key_tmp, start_tmp):
            if tmp:
                try:
                    tmp.unlink()
                except OSError:
                    pass
        if tar_dir:
            shutil.rmtree(tar_dir, ignore_errors=True)


def agentd_run_to_end(key: str, message: str, cwd: Path = ROOT) -> str:
    """Submit an agentd job and block until it settles. Returns the final
    status ("done" | "error" | "cancelled"); raises BusyError on 409."""
    remote_id = agentd_client.submit(key, str(cwd), message)
    while True:
        snap = agentd_client.poll(remote_id)
        if snap and snap.get("status") in ("done", "error", "cancelled"):
            return snap["status"]
        time.sleep(AGENT_POLL_INTERVAL)


# ---------------------------------------------------------------- network view
# One node per machine — the local one plus a VM node per cua sandbox when the
# cua CLI is installed — its durable-agent conversations listed beneath.
# Session data comes from the sidecar when it answers; the conversations.json
# fallback keeps the view useful while it is down.

def session_type_title(key: str) -> tuple[str, str]:
    """Split a conversation key into (type, title): "teach:my-ws" ->
    ("teach", "my-ws"), "analysis:<job>:<batch>" -> ("analysis", "Analysis <job>:<batch>")."""
    prefix, sep, rest = key.partition(":")
    if not sep:
        return "console", key
    if prefix == "console":
        return "console", "Console"
    if prefix == "analysis":
        return "analysis", f"Analysis {rest}"
    if prefix == "vmtask":
        # vmtask:<vm>:<session> — the Network tab groups these under the VM
        # node's own listing, so the title is just the session part.
        _vm, sep2, sess = rest.partition(":")
        return "vmtask", sess if sep2 else rest
    return prefix, rest


HIDDEN_SESSIONS_LOCK = threading.Lock()


def load_hidden_sessions() -> list[str]:
    """Conversation keys the user hid from the Network tab. v1 is view-only:
    the transcripts stay in storage, the listing just filters them out."""
    data = read_json(HIDDEN_SESSIONS_JSON, [])
    if not isinstance(data, list):
        return []
    return [k for k in data if isinstance(k, str)]


def hide_session(key: str) -> int:
    """Add a conversation key to the hidden list, persist, return the count."""
    with HIDDEN_SESSIONS_LOCK:
        hidden = load_hidden_sessions()
        if key not in hidden:
            hidden.append(key)
            write_json_atomic(HIDDEN_SESSIONS_JSON, hidden)
        return len(hidden)


def build_network() -> dict:
    """Nodes + sessions for the Network tab. The sidecar is authoritative when
    online (it knows which conversations are busy); file data fills the rest
    with busy=False, status=None. Teach sessions vanish when their workspace
    is deleted — the transcript stays in storage, but there is nothing to show.
    Sessions the user hid (hidden_sessions.json) are filtered out everywhere —
    local, VM-node and remote-node listings — and only counted."""
    hidden = set(load_hidden_sessions())
    conv_file = read_json(AGENT_CONVERSATIONS_JSON, {})
    file_sessions: dict[str, dict] = {}
    if isinstance(conv_file, dict):
        for key, val in conv_file.items():
            if not isinstance(key, str) or not isinstance(val, dict):
                continue
            file_sessions[key] = {"id": str(val.get("id") or ""),
                                  "cwd": str(val.get("cwd") or "")}

    def teach_workspace_exists(title: str) -> bool:
        ws_dir = (TEACH_DIR / title).resolve()
        return (str(ws_dir).startswith(str(TEACH_DIR.resolve()) + os.sep)
                and ws_dir.is_dir())

    sidecar = agentd_client.list_conversations()  # None when the sidecar is down
    sessions: list[dict] = []
    seen: set[str] = set()
    online = sidecar is not None
    if online:
        for c in sidecar:
            if not isinstance(c, dict):
                continue
            key = str(c.get("key") or "")
            if not key or key in seen or key in hidden:
                continue
            seen.add(key)
            stype, title = session_type_title(key)
            if stype == "teach" and not teach_workspace_exists(title):
                continue
            sessions.append({
                "key": key, "type": stype, "title": title,
                "busy": bool(c.get("busy")),
                "status": c.get("lastStatus") if c.get("lastStatus") in
                          ("running", "done", "error", "cancelled") else None,
                "cwd": str(c.get("cwd") or file_sessions.get(key, {}).get("cwd") or ""),
            })
    for key in sorted(file_sessions):
        if key in seen or key in hidden:
            continue
        stype, title = session_type_title(key)
        if stype == "teach" and not teach_workspace_exists(title):
            continue
        sessions.append({
            "key": key, "type": stype, "title": title,
            "busy": False, "status": None,
            "cwd": file_sessions[key]["cwd"],
        })
    # cua sandboxes double as LAN clients: surface them as VM nodes so the
    # network view answers "which machines can use the app right now". Fully
    # best-effort — no cua CLI, or a listing hiccup, just means no VM nodes.
    # A VM with a healthy in-guest agent (see vm_nodes.py) is online even when
    # nothing else knows it, and lists ITS OWN live sessions; otherwise the
    # node falls back to the sandbox run state and stays session-less.
    try:
        sandboxes = cua_client.list_sandboxes() if cua_client.find_cua() else []
    except Exception:  # noqa: BLE001
        sandboxes = []
    vm_node_list: list[dict] = []
    node_session_keys: dict[str, set] = {}

    def conv_session_entries(convs: list) -> tuple[list[dict], set]:
        """Sidecar /conversations -> session entries in the network shape
        (same merge for a local VM's sidecar and a remote node's endpoint)."""
        entries: list[dict] = []
        keys: set[str] = set()
        for c in convs:
            if not isinstance(c, dict):
                continue
            key = str(c.get("key") or "")
            if not key or key in keys or key in hidden:
                continue
            keys.add(key)
            stype, title = session_type_title(key)
            if stype == "teach" and not teach_workspace_exists(title):
                continue
            entries.append({
                "key": key, "type": stype, "title": title,
                "busy": bool(c.get("busy")),
                "status": c.get("lastStatus") if c.get("lastStatus") in
                          ("running", "done", "error", "cancelled") else None,
                "cwd": str(c.get("cwd") or ""),
            })
        return entries, keys

    for sb in sandboxes[:10]:
        try:
            name, status = str(sb.get("name") or ""), str(sb.get("status") or "")
        except AttributeError:
            continue
        if not name:
            continue
        healthy = vm_nodes.node_healthy(name)
        node_agent = False
        node_sessions: list[dict] = []
        if healthy:
            node_sessions, node_keys = conv_session_entries(
                vm_nodes.node_conversations(name) or [])
            node_session_keys[name] = node_keys
            health = vm_nodes.node_health(name) or {}
            node_agent = bool(health.get("keyConfigured"))
        node = {"id": f"vm-{name}", "name": name, "kind": "vm",
                "online": healthy if healthy else status == "running",
                "vm_state": status, "agent": node_agent, "sessions": node_sessions}
        flavor = {"vm": "qemu", "container": "container"}.get(
            str(sb.get("kind") or "").lower())
        if flavor:
            node["flavor"] = flavor
        vm_node_list.append(node)
    # vmtask:<vm>:<session> conversations on the LOCAL sidecar belong to the
    # VM they run on: move them under its node (unless the node's own sidecar
    # already lists that key). A session whose VM is gone (deleted sandbox)
    # stays on the local node.
    vm_by_name = {n["name"]: n for n in vm_node_list}
    local_sessions: list[dict] = []
    for s in sessions:
        parts = s["key"].split(":")
        target = vm_by_name.get(parts[1]) if (len(parts) >= 3
                                              and parts[0] == "vmtask") else None
        if target and s["key"] in node_session_keys.get(parts[1], set()):
            continue  # already listed from the node's own sidecar
        (target["sessions"] if target else local_sessions).append(s)
    nodes = [{"id": "local", "name": "local", "kind": "local",
              "online": online, "sessions": local_sessions}, *vm_node_list]
    # Remote nodes registered by other hosts (the laptop-vm beacon). One card
    # per fresh registration, unless it shadows a local node (same name or
    # id) — the local cua listing wins. Presence needs only a recent seen;
    # agent/sessions additionally need a reachable endpoint from the master.
    with REMOTE_NODES_LOCK:
        _remote_nodes_prune()
        fresh_remotes = sorted(REMOTE_NODES.values(),
                               key=lambda e: str(e.get("name")) )[:REMOTE_NODE_MAX]
    now = time.time()
    local_ids = {n["id"] for n in nodes} | {n["name"] for n in nodes}
    for entry in fresh_remotes:
        name = str(entry.get("name") or "")
        if not name or name in local_ids or f"remote-{name}" in local_ids:
            continue
        endpoint = entry.get("endpoint")
        health = _remote_health(endpoint) if endpoint else None
        endpoint_ok = bool(health and health.get("ok"))
        fresh = now - float(entry.get("seen") or 0) <= REMOTE_ONLINE_WINDOW
        node_sessions = []
        if endpoint_ok:
            node_sessions, _ = conv_session_entries(
                _remote_conversations(endpoint) or [])
        local_ids.add(f"remote-{name}")
        nodes.append({
            "id": f"remote-{name}", "name": name, "kind": "vm", "remote": True,
            "host": str(entry.get("host") or "remote"),
            "flavor": "qemu" if str(entry.get("kind") or "").lower()
                      in ("vm", "qemu") else "container",
            "vm_state": str(entry.get("state") or ""),
            "online": fresh and (endpoint_ok if endpoint else True),
            "agent": endpoint_ok,
            "has_endpoint": bool(endpoint),
            "sessions": node_sessions,
        })
    return {"nodes": nodes, "hidden_count": len(hidden)}



# ---------------------------------------------------------------- auto-analysis

def analysis_prompt(ids: list[str], example_id: str) -> str:
    return (
        "You are the learnings generator for a local learning app. For EACH of these "
        f"YouTube video ids ({', '.join(ids)}): read `data/transcripts/<id>.json` "
        "(JSON array of {start, text} caption segments; noisy, fix mishearings) and the "
        "title/duration from `data/videos.json`, then WRITE `data/learnings/<id>.json`. "
        f"Use `data/learnings/{example_id}.json` as the exact format example — same schema, "
        "same quality: summary, what_you_learn, skills (Title Case, 1-4 words, reuse names "
        "that already exist in other learnings files for the same concepts — open a few "
        "data/learnings/*.json and prefer the established name over inventing a variant; "
        "only add a new skill when the concept is genuinely distinct), learnings with "
        "time_hint mm:ss, key_points, and a mermaid flowchart TD diagram with quoted labels "
        'containing no ( ) [ ] { } " : ; characters. Set "generated" to today. '
        "Write valid JSON for EVERY id. Do not ask questions; process all ids, then reply "
        "with one line per id: <id> OK."
    )


def run_analysis(job: dict, ids: list[str]) -> None:
    """After a scrape, generate learnings for new videos via the AI agent:
    the agentd sidecar when available, else the kimi CLI."""
    exe = None
    durable = agentd_client.backend() == "durable"
    if not durable:
        exe = find_kimi()
    if not ids:
        job_update(job, phase="done", analysis_total=0, analysis_done=0)
        return
    if not durable and not exe:
        job_update(job, phase="done", analysis_total=len(ids), analysis_done=0,
                   message=f"scraped OK — learnings pending for {len(ids)} videos (no AI agent available)")
        return
    job_update(job, phase="analyzing", analysis_total=len(ids), analysis_done=0,
               message=f"Analyzing {len(ids)} videos with the AI agent…")
    example = next((p.stem for p in LEARNINGS_DIR.glob("*.json")), None)
    batch_size = 6
    done = 0
    for i in range(0, len(ids), batch_size):
        batch = ids[i : i + batch_size]
        if durable:
            # one conversation per batch: the sidecar has no subprocess
            # timeout, and a per-batch key keeps retries on the same history
            status = agentd_run_to_end(f"analysis:{job['id']}:{i // batch_size}",
                                       analysis_prompt(batch, example or batch[0]))
            batch_ok = status == "done"
        else:
            proc = subprocess.run(
                [exe, "-p", analysis_prompt(batch, example or batch[0])],
                cwd=str(ROOT), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=1800,
            )
            batch_ok = proc.returncode == 0
        for vid in batch:
            consolidate_learnings_skills(vid)
        done = sum(1 for vid in ids if has_learnings(vid))
        job_update(job, analysis_done=done,
                   message=f"Analyzing videos with the AI agent… ({done}/{len(ids)})",
                   eta_seconds=int((len(ids) - done) / max(1, batch_size) * 240))
        if not batch_ok:
            if durable and done > 0:
                continue  # partial progress — worth trying the next batch
            job_update(job, phase="done",
                       message=f"scraped OK — analysis stopped early ({done}/{len(ids)} analyzed)")
            return
    job_update(job, phase="done", analysis_done=done,
               message=f"Done. Scraped and analyzed {done} videos.")


# ---------------------------------------------------------------- http

class Handler(BaseHTTPRequestHandler):
    server_version = "LearningServer/1.0"

    def log_message(self, fmt, *args):  # keep stdout quiet
        pass

    def audit_log(self, method: str, path: str) -> None:
        """Append mutating API calls to data/audit.log so destructive events
        (like a workspace deletion) can be traced after the fact."""
        try:
            line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {method} {path}\n"
            with LOCK:
                with open(AUDIT_LOG, "a", encoding="utf-8") as f:
                    f.write(line)
        except OSError:
            pass

    # -- helpers

    def send_json(self, obj, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, status: int, message: str) -> None:
        self.send_json({"error": message}, status)

    def lan_token_ok(self) -> bool:
        """Empty lan_token = open access (default). Otherwise the client must
        send the token as x-ytl-token header or ?token= query param."""
        token = load_settings().get("lan_token") or ""
        if not token:
            return True
        if self.headers.get("x-ytl-token") == token:
            return True
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        return (query.get("token") or [""])[0] == token

    def read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return None
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("request body is not valid JSON")

    def serve_file(self, path: Path, content_type: str | None = None) -> None:
        if not path.is_file():
            self.send_error_json(404, "not_found")
            return
        data = path.read_bytes()
        ctype = content_type or mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        # no-cache: browsers must revalidate every load, so UI updates are
        # never masked by a stale cached script (static file, no etag/last-mod
        # machinery here — a fresh GET is cheap and local).
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def serve_static(self, rel: str) -> None:
        try:
            target = (STATIC_DIR / rel).resolve()
            if not str(target).startswith(str(STATIC_DIR.resolve()) + os.sep):
                self.send_error_json(403, "forbidden")
                return
        except OSError:
            self.send_error_json(404, "not_found")
            return
        self.serve_file(target)

    # -- dispatch

    def do_GET(self):
        try:
            self.route("GET")
        except BrokenPipeError:
            pass
        except Exception as e:  # noqa: BLE001 - never kill the server
            self.send_error_json(500, f"{type(e).__name__}: {e}")

    def do_POST(self):
        try:
            self.route("POST")
        except BrokenPipeError:
            pass
        except Exception as e:  # noqa: BLE001
            self.send_error_json(500, f"{type(e).__name__}: {e}")

    def do_PUT(self):
        try:
            self.route("PUT")
        except BrokenPipeError:
            pass
        except Exception as e:  # noqa: BLE001
            self.send_error_json(500, f"{type(e).__name__}: {e}")

    def do_DELETE(self):
        try:
            self.route("DELETE")
        except BrokenPipeError:
            pass
        except Exception as e:  # noqa: BLE001
            self.send_error_json(500, f"{type(e).__name__}: {e}")

    def route(self, method: str) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path.startswith("/api/") and not self.lan_token_ok():
            return self.send_error_json(401, "token_required")

        if method != "GET" and path.startswith("/api/"):
            self.audit_log(method, path)

        if method == "GET":
            if path == "/" or path == "/index.html":
                return self.serve_static("index.html")
            if path == "/cua-spaces-infographic.html":
                return self.serve_file(ROOT / "cua-spaces-infographic.html", "text/html")
            if path == "/device-agent.ps1":
                return self.serve_kit_file("Device-Agent.ps1")
            if path == "/kit/agentd.tgz":
                return self.serve_agentd_tgz()
            m = re.fullmatch(r"/kit/([A-Za-z0-9.-]+)", path)
            if m:
                return self.serve_kit_file(m.group(1))
            if path.startswith("/static/"):
                return self.serve_static(path[len("/static/"):])
            if path == "/api/state":
                return self.api_state()
            if path == "/api/videos":
                return self.send_json({"videos": sorted_videos()})
            if path == "/api/skills":
                return self.send_json({"skills": scan_skills()})
            if path == "/api/relations":
                return self.api_relations(query)
            if path == "/api/settings":
                return self.send_json(load_settings())
            if path == "/api/lan":
                return self.api_lan()
            if path == "/api/client":
                return self.api_client()
            if path == "/api/profile":
                return self.api_profile()
            if path == "/api/profile/skills":
                return self.send_json({"skills": read_json(PROFILE_SKILLS_JSON, {"skills": []}).get("skills", [])})
            if path == "/api/test-results":
                return self.api_test_results()
            if path == "/api/skills/library":
                return self.send_json(build_skills_library())
            if path == "/api/teach/workspaces":
                return self.send_json({"workspaces": list_workspaces()})
            if path == "/api/app-structure":
                return self.send_json(build_structure(ROOT))
            if path == "/api/file":
                return self.api_file_preview(query)
            m = re.fullmatch(r"/teach/([A-Za-z0-9][A-Za-z0-9._-]*)/(.+)", path)
            if m:
                return self.api_workspace_file(m.group(1), m.group(2))
            if path == "/api/agent/status":
                return self.api_agent_status()
            if path == "/api/network":
                return self.send_json(build_network())
            if path == "/api/cua/status":
                return self.api_cua_status()
            if path == "/api/vm-nodes":
                return self.api_vm_nodes()
            if path == "/api/devices":
                return self.api_devices()
            m = re.fullmatch(r"/api/devices/([^/]+)/next", path)
            if m:
                return self.api_device_next(m.group(1), query)
            m = re.fullmatch(r"/api/cua/term/([^/]+)", path)
            if m:
                return self.api_cua_term_get(m.group(1))
            m = re.fullmatch(r"/api/teach/([^/]+)/state", path)
            if m:
                return self.api_get_ws_state(m.group(1))
            m = re.fullmatch(r"/api/agent/job/([^/]+)", path)
            if m:
                return self.api_agent_job(m.group(1))
            m = re.fullmatch(r"/api/video/([^/]+)", path)
            if m:
                return self.api_video(m.group(1))
            m = re.fullmatch(r"/api/job/([^/]+)", path)
            if m:
                return self.api_job(m.group(1))
            m = re.fullmatch(r"/api/learnings/([^/]+)", path)
            if m:
                return self.api_get_learnings(m.group(1))
            m = re.fullmatch(r"/api/transcript/([^/]+)", path)
            if m:
                return self.api_transcript(m.group(1))
            m = re.fullmatch(r"/api/thumb/([^/]+)", path)
            if m:
                return self.api_thumb(m.group(1))
            m = re.fullmatch(r"/api/teach/([^/]+)/lessons", path)
            if m:
                return self.api_lessons(m.group(1))
            if path == "/api/teach/lesson":
                return self.api_lesson(query)
            m = re.fullmatch(r"/api/progress/([^/]+)", path)
            if m:
                return self.api_get_progress(m.group(1))

        if method == "POST":
            m = re.fullmatch(r"/api/video/([^/]+)/learned", path)
            if m:
                return self.api_set_learned(m.group(1))
            if path == "/api/videos/add":
                return self.api_add_videos()
            if path == "/api/teach/start":
                return self.api_teach_start()
            if path == "/api/skill/create":
                return self.api_skill_create()
            m = re.fullmatch(r"/api/progress/([^/]+)", path)
            if m:
                return self.api_set_progress(m.group(1))
            if path == "/api/settings":
                return self.api_update_settings()
            if path == "/api/profile/skills":
                return self.api_profile_skills()
            if path == "/api/shutdown":
                return self.api_shutdown()
            if path == "/api/tests/run":
                return self.api_tests_run()
            if path == "/api/agent/run":
                return self.api_agent_run()
            if path == "/api/agent/console/run":
                return self.api_agent_console_run()
            if path == "/api/cua/sandboxes":
                return self.api_cua_create()
            if path == "/api/cua/sandboxes/remove":
                return self.api_cua_remove()
            if path == "/api/cua/sandboxes/view":
                return self.api_cua_view()
            if path == "/api/cua/provision":
                return self.api_cua_provision()
            if path == "/api/cua/term/open":
                return self.api_cua_term_open()
            m = re.fullmatch(r"/api/cua/term/([^/]+)/send", path)
            if m:
                return self.api_cua_term_send(m.group(1))
            m = re.fullmatch(r"/api/cua/term/([^/]+)/close", path)
            if m:
                return self.api_cua_term_close(m.group(1))
            if path == "/api/agent/vm-task":
                return self.api_agent_vm_task()
            if path == "/api/nodes/register":
                return self.api_nodes_register()
            if path == "/api/network/sessions/hide":
                return self.api_network_session_hide()
            if path == "/api/devices/pair-codes":
                return self.api_device_pair_codes()
            if path == "/api/devices/pair":
                return self.api_device_pair()
            m = re.fullmatch(r"/api/devices/([^/]+)/output", path)
            if m:
                return self.api_device_output(m.group(1))
            m = re.fullmatch(r"/api/devices/([^/]+)/task", path)
            if m:
                return self.api_device_task(m.group(1))
            m = re.fullmatch(r"/api/devices/([^/]+)/remove", path)
            if m:
                return self.api_device_remove(m.group(1))
            if path == "/api/skill/from_workspace":
                return self.api_skill_from_workspace()
            if path == "/api/skill/install":
                return self.api_skill_install()
            m = re.fullmatch(r"/api/teach/([^/]+)/state", path)
            if m:
                return self.api_set_ws_state(m.group(1))
            m = re.fullmatch(r"/api/agent/job/([^/]+)/cancel", path)
            if m:
                return self.api_agent_cancel(m.group(1))

        if method == "PUT":
            m = re.fullmatch(r"/api/learnings/([^/]+)", path)
            if m:
                return self.api_put_learnings(m.group(1))
            if path == "/api/profile":
                return self.api_put_profile()

        if method == "DELETE":
            m = re.fullmatch(r"/api/video/([^/]+)", path)
            if m:
                return self.api_delete_video(m.group(1), query)
            m = re.fullmatch(r"/api/teach/([^/]+)", path)
            if m:
                return self.api_delete_workspace(m.group(1))

        if path.startswith("/api/"):
            return self.send_error_json(404, "not_found")
        self.send_error_json(404, "not_found")

    # -- endpoints

    def api_nodes_register(self):
        """Remote host (the laptop-vm beacon) registers its VM nodes."""
        try:
            body = self.read_body()
        except ValueError as e:
            return self.send_error_json(400, str(e))
        if not isinstance(body, dict) or not isinstance(body.get("nodes"), list):
            return self.send_error_json(400, "body must be {host?, nodes: [...]}")
        host = str(body.get("host") or "remote")[:80]
        registered = register_remote_nodes(host, body["nodes"])
        self.send_json({"ok": True, "registered": registered})

    def api_network_session_hide(self):
        """Hide a session from the Network tab (v1: view-only — the transcript
        stays in storage; a restore UI comes later)."""
        try:
            body = self.read_body()
        except ValueError as e:
            return self.send_error_json(400, str(e))
        key = body.get("key") if isinstance(body, dict) else None
        if not isinstance(key, str) or not key or len(key) > 100:
            return self.send_error_json(
                400, "key must be a non-empty string of at most 100 chars")
        self.send_json({"ok": True, "hidden": hide_session(key)})

    # -- devices (Connect device: pairing-based reverse agent, see devices.py)

    def serve_kit_file(self, name: str):
        """Serve a file from scripts/laptop-vm/ (Device-Agent.ps1 + the kit
        the device task scripts download). Plain-text, no-cache, LAN-public by
        design — the sensitive bits (device token, LAN token) never ride here."""
        if not re.fullmatch(r"[A-Za-z0-9.-]+", name):
            return self.send_error_json(400, "bad filename")
        target = (KIT_DIR / name).resolve()
        if not str(target).startswith(str(KIT_DIR.resolve()) + os.sep) or not target.is_file():
            return self.send_error_json(404, "not_found")
        self.serve_file(target, "text/plain")

    def serve_agentd_tgz(self):
        """GET /kit/agentd.tgz — gzipped tar of agentd/ for the install-agent
        device task (built on the fly, ~60 s cache; see build_agentd_tgz)."""
        try:
            data = build_agentd_tgz()
        except OSError:
            return self.send_error_json(404, "agentd/ not found")
        self.send_response(200)
        self.send_header("Content-Type", "application/gzip")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def api_device_pair_codes(self):
        """Mint a pairing code for the Connect-device card (UI button)."""
        code, expires = devices.new_pair_code()
        self.send_json({"code": code, "expires_in": int(expires - time.time())})

    def api_device_pair(self):
        """Device-agent entry point: consume a code, get device_id + token."""
        try:
            body = self.read_body()
        except ValueError as e:
            return self.send_error_json(400, str(e))
        if not isinstance(body, dict):
            return self.send_error_json(400, 'body must be {"code": ..., "hostname": ...}')
        code = str(body.get("code") or "")
        hostname = str(body.get("hostname") or "device")[:80]
        result = devices.register_device(code, hostname)
        if not result:
            return self.send_error_json(400, "invalid or expired pairing code")
        device_id, token = result
        self.send_json({"device_id": device_id, "token": token})

    def api_devices(self):
        self.send_json({"devices": devices.list_devices()})

    def api_device_next(self, device_id: str, query: dict):
        """The device long-polls (well, short-polls) for its next task. Auth is
        the device token in ?token= — devices don't have a header habit."""
        token = (query.get("token") or [""])[0]
        if not devices.device_seen(device_id, token):
            return self.send_error_json(401, "device auth failed")
        self.send_json({"task": devices.next_task(device_id, token)})

    def api_device_output(self, device_id: str):
        try:
            body = self.read_body()
        except ValueError as e:
            return self.send_error_json(400, str(e))
        if not isinstance(body, dict):
            return self.send_error_json(400, "body must be a JSON object")
        ok = devices.push_task_output(
            device_id, str(body.get("token") or ""), str(body.get("task_id") or ""),
            line=body.get("line"), done=bool(body.get("done")),
            exit_code=body.get("exit_code"), needs_admin=bool(body.get("needs_admin")))
        if not ok:
            return self.send_error_json(401, "device auth failed")
        # Auto-chain: VMs were created on the laptop -> start its beacon so the
        # new VMs show up in the master's Network tab without a manual click.
        # Runs BEFORE the response so the next poll already sees the new task.
        if body.get("done") and not body.get("exit_code"):
            try:
                task = devices.DEVICES.get(device_id, {}).get("tasks", {}).get(
                    str(body.get("task_id") or ""), {})
                if (task.get("kind") == "create-vm" and not task.get("beacon_chained")):
                    task["beacon_chained"] = True
                    ctx = self._device_task_ctx()
                    if ctx["lan_token"]:
                        script = devices.build_task_script("start-beacon", ctx)
                        devices.queue_task(device_id, "start-beacon", script,
                                           "start beacon (auto)")
            except Exception:  # noqa: BLE001 - chaining is best-effort
                pass
        self.send_json({"ok": True})

    def _device_task_ctx(self) -> dict:
        """Device-reachable master URL (first LAN url) + the LAN token, which
        only ever travels device-bound inside a built task script. Also the
        bare master LAN IP — the guest's relay dial-out target."""
        settings = load_settings()
        port = int(settings.get("port", 8471))
        urls = lan_urls(port)
        master = urls[0] if urls else f"http://{settings.get('host') or '127.0.0.1'}:{port}"
        master_lan_ip = "127.0.0.1"
        if urls:
            master_lan_ip = urls[0].removeprefix("http://").split(":", 1)[0]
        return {"master_url": master, "lan_token": settings.get("lan_token") or "",
                "master_lan_ip": master_lan_ip}

    def api_device_remove(self, device_id: str):
        """Forget a device — only when it is offline (its agent is closed)."""
        with devices.DEVICES_LOCK:
            dev = devices.DEVICES.get(device_id)
            online = bool(dev and time.time() - (dev.get("last_seen") or 0)
                          < devices.ONLINE_WINDOW)
        err = devices.remove_device(device_id, online)
        if err:
            return self.send_error_json(409 if online else 404, err)
        self.send_json({"ok": True})

    def api_device_task(self, device_id: str):
        """Queue a pre-defined task from the UI. Only the three known kinds —
        no arbitrary remote commands."""
        with devices.DEVICES_LOCK:
            if device_id not in devices.DEVICES:
                return self.send_error_json(404, "device not found")
        try:
            body = self.read_body()
        except ValueError as e:
            return self.send_error_json(400, str(e))
        if not isinstance(body, dict):
            return self.send_error_json(400, "body must be a JSON object")
        kind = str(body.get("kind") or "")
        if kind not in devices.TASK_KINDS:
            return self.send_error_json(400, f"unknown task kind — one of {', '.join(devices.TASK_KINDS)}")
        # Wizard gating: creating VMs / running the beacon needs the cua stack
        # the install-stack task puts on the device.
        if kind != "install-stack":
            with devices.DEVICES_LOCK:
                if not devices.DEVICES[device_id].get("stack_done"):
                    return self.send_error_json(
                        409, "run 'Install VM stack' on this device first — "
                             "it is still missing cua/QEMU")
        params = body.get("params") if isinstance(body.get("params"), dict) else {}
        ctx = self._device_task_ctx()
        if kind == "create-vm":
            ctx["count"] = int(params.get("count") or 1)
            ctx["name_prefix"] = str(params.get("name_prefix") or "laptop-vm")[:40]
        if kind == "open-desktop":
            vm = str(params.get("name") or "")
            if not REMOTE_NAME_RE.fullmatch(vm):
                return self.send_error_json(400, "bad vm name")
            ctx["vm_name"] = vm
        if kind == "install-agent":
            vm = str(params.get("name") or "")
            if not REMOTE_NAME_RE.fullmatch(vm):
                return self.send_error_json(400, "bad vm name")
            key = kimi_api_key()
            if not key:
                return self.send_error_json(
                    400, "agentd/.env has no KIMI_API_KEY — set one before installing")
            ctx["vm_name"] = vm
            ctx["kimi_key"] = key
        if kind == "expose-agent":
            # Points the in-guest agentd sidecar at the master's reverse
            # relay (dial-out, slirp NAT) and verifies the guest process —
            # no host-side portproxy needed.
            vm = str(params.get("name") or "")
            if not REMOTE_NAME_RE.fullmatch(vm):
                return self.send_error_json(400, "bad vm name")
            ctx["vm_name"] = vm
        if kind == "start-beacon" and not ctx["lan_token"]:
            return self.send_error_json(400, "start-beacon needs a LAN token — set one in Settings → LAN / other devices")
        try:
            script = devices.build_task_script(kind, ctx)
        except (ValueError, TypeError) as e:
            return self.send_error_json(400, str(e))
        label = str(body.get("label") or "").strip() or {
            "install-stack": "install VM stack",
            "create-vm": f"create {ctx.get('count', 1)} VM(s)",
            "start-beacon": "start beacon",
            "open-desktop": f"open desktop viewer for {ctx.get('vm_name', '')}",
            "install-agent": f"install agent {ctx.get('vm_name', '')}",
            "expose-agent": f"expose agent {ctx.get('vm_name', '')}",
        }[kind]
        task_id = devices.queue_task(device_id, kind, script, label)
        self.send_json({"task_id": task_id})


    def api_lan(self):
        settings = load_settings()
        host = str(settings.get("host") or "127.0.0.1")
        port = int(settings.get("port", 8471))
        urls = lan_urls(port) if host in ("0.0.0.0", "::") else []
        self.send_json({"host": host, "urls": urls, "token_set": bool(settings.get("lan_token"))})

    def is_master_client(self) -> bool:
        """True when the browser runs on the machine that serves the app.
        Only the master may stop the server — LAN clients get the button greyed."""
        return self.client_address[0] in ("127.0.0.1", "::1", "localhost")

    def api_client(self):
        self.send_json({"master": self.is_master_client(),
                        "ip": self.client_address[0]})

    def api_state(self):
        settings = load_settings()
        index = load_index()
        videos = index["videos"]
        stats = {
            "total": len(videos),
            "learned": sum(1 for v in videos.values() if v.get("learned")),
            "with_transcript": sum(1 for v in videos if has_transcript(v)),
            "with_learnings": sum(1 for v in videos if has_learnings(v)),
        }
        self.send_json({"settings": settings, "tabs": settings["tabs"], "stats": stats})

    def api_video(self, vid: str):
        index = load_index()
        video = index["videos"].get(vid)
        if not video:
            return self.send_error_json(404, "not_found")
        obj = video_object(vid, video)
        obj["learnings"] = read_json(LEARNINGS_DIR / f"{vid}.json", None)
        self.send_json(obj)

    def api_set_learned(self, vid: str):
        body = self.read_body()
        if not isinstance(body, dict) or not isinstance(body.get("learned"), bool):
            return self.send_error_json(400, "body must be {\"learned\": bool}")
        with LOCK:
            index = load_index()
            if vid not in index["videos"]:
                return self.send_error_json(404, "not_found")
            index["videos"][vid]["learned"] = body["learned"]
            save_index(index)
            video = dict(index["videos"][vid])
        self.send_json(video_object(vid, video))

    def api_delete_video(self, vid: str, query: dict):
        with LOCK:
            index = load_index()
            if vid not in index["videos"]:
                return self.send_error_json(404, "not_found")
            del index["videos"][vid]
            save_index(index)
        if query.get("purge") == ["1"]:
            for p in (
                INFO_DIR / f"{vid}.info.json",
                LEARNINGS_DIR / f"{vid}.json",
                TRANSCRIPT_DIR / f"{vid}.json",
                THUMB_DIR / f"{vid}.jpg",
            ):
                try:
                    p.unlink()
                except OSError:
                    pass
        self.send_json({"ok": True})

    def api_add_videos(self):
        body = self.read_body()
        if not isinstance(body, dict):
            return self.send_error_json(400, "body must be a JSON object")
        try:
            video_urls, channel_urls = classify_urls(
                body.get("urls") or [], body.get("channel")
            )
        except ValueError as e:
            return self.send_error_json(400, str(e))
        if not video_urls and not channel_urls:
            return self.send_error_json(400, "no valid video or channel URLs")
        settings = load_settings()
        commands = []
        for ch in channel_urls:
            limit = body.get("limit") or settings.get("channel_default_limit", 30)
            commands.append(["channel", ch, "--limit", str(int(limit))])
        if video_urls:
            commands.append(["videos", *video_urls])
        kind = "channel" if channel_urls and not video_urls else "videos" if video_urls and not channel_urls else "mixed"
        default_total = len(video_urls) or int(body.get("limit") or settings.get("channel_default_limit", 30))
        job = {
            "id": uuid.uuid4().hex[:12],
            "status": "running",
            "kind": kind,
            "total": default_total,
            "done_count": 0,
            "message": "starting",
            "eta_seconds": None,
            "started": int(time.time()),
            "phase": "scraping",
            "analysis_total": 0,
            "analysis_done": 0,
        }
        JOBS[job["id"]] = job
        persist_jobs()
        t = threading.Thread(target=run_job, args=(job, commands), daemon=True)
        t.start()
        self.send_json({"job_id": job["id"]})

    def api_job(self, job_id: str):
        job = JOBS.get(job_id)
        if not job:
            data = read_json(JOBS_JSON, {"jobs": []})
            job = next((j for j in data.get("jobs", []) if j.get("id") == job_id), None)
        if not job:
            return self.send_error_json(404, "not_found")
        self.send_json({k: job.get(k) for k in
                        ("id", "status", "kind", "total", "done_count", "message",
                         "eta_seconds", "started", "phase", "analysis_total", "analysis_done")})

    def api_get_learnings(self, vid: str):
        data = read_json(LEARNINGS_DIR / f"{vid}.json", None)
        if data is None:
            return self.send_error_json(404, "not_found")
        self.send_json(data)

    def api_put_learnings(self, vid: str):
        body = self.read_body()
        if not isinstance(body, dict):
            return self.send_error_json(400, "body must be a JSON object")
        if body.get("video_id") != vid:
            return self.send_error_json(400, "video_id mismatch")
        write_json_atomic(LEARNINGS_DIR / f"{vid}.json", body)
        self.send_json(body)

    def api_transcript(self, vid: str):
        data = read_json(TRANSCRIPT_DIR / f"{vid}.json", None)
        if not isinstance(data, list):
            return self.send_error_json(404, "not_found")
        self.send_json({"segments": data})

    def api_thumb(self, vid: str):
        f = THUMB_DIR / f"{vid}.jpg"
        if not f.is_file():
            return self.send_error_json(404, "not_found")
        self.serve_file(f, "image/jpeg")

    def api_teach_start(self):
        body = self.read_body()
        if not isinstance(body, dict):
            return self.send_error_json(400, "body must be a JSON object")
        topic = (body.get("topic") or "").strip()
        vid = (body.get("video_id") or "").strip()
        focus = (body.get("focus_skill") or "").strip() or None
        if topic:
            ws_id = f"free-{kebab(topic)}"
            if not safe_ws_name(ws_id):
                return self.send_error_json(400, "topic produces an unsafe workspace name")
            workspace, created = create_workspace(ws_id, None, None, mission_title=topic)
            return self.send_json({"workspace": workspace, "created": created, "id": ws_id})
        if not vid:
            return self.send_error_json(400, "body needs {\"video_id\": ...} or {\"topic\": ...}")
        index = load_index()
        video = index["videos"].get(vid)
        if not video:
            return self.send_error_json(404, "video not found")
        learnings = read_json(LEARNINGS_DIR / f"{vid}.json", None)
        if focus and isinstance(learnings, dict):
            names = {s.get("name") for s in learnings.get("skills") or []}
            if focus not in names:
                return self.send_error_json(400, f"skill '{focus}' not in this video's learnings")
        workspace, created = create_workspace(vid, video, learnings, focus=focus)
        self.send_json({"workspace": workspace, "created": created, "id": vid})

    def api_get_ws_state(self, ws: str):
        if not safe_ws_name(ws):
            return self.send_error_json(400, "bad workspace id")
        data = read_json(workspace_state_path(ws), {"completed": []})
        completed = data.get("completed") if isinstance(data, dict) else []
        self.send_json({"completed": completed if isinstance(completed, list) else []})

    def api_set_ws_state(self, ws: str):
        if not safe_ws_name(ws):
            return self.send_error_json(400, "bad workspace id")
        body = self.read_body()
        if not isinstance(body, dict) or not isinstance(body.get("completed"), list):
            return self.send_error_json(400, "body must be {\"completed\": [...]}")
        write_json_atomic(workspace_state_path(ws), {"completed": body["completed"]})
        self.send_json({"completed": body["completed"]})

    def api_skill_from_workspace(self):
        body = self.read_body()
        if not isinstance(body, dict) or not body.get("workspace"):
            return self.send_error_json(400, "body must be {\"workspace\": ...}")
        ws = str(body["workspace"])
        if not safe_ws_name(ws):
            return self.send_error_json(400, "bad workspace id")
        ws_dir = TEACH_DIR / ws
        if not ws_dir.is_dir():
            return self.send_error_json(404, "workspace not found")
        mission = ws_dir / "MISSION.md"
        title = ws
        if mission.is_file():
            try:
                title = mission.read_text(encoding="utf-8", errors="replace").splitlines()[0].lstrip("# ").strip() or ws
            except Exception:
                pass
        name = (body.get("name") or "").strip() or title
        slug = kebab(name)
        path = SKILLS_DIR / slug / "SKILL.md"
        lessons = workspace_lesson_titles(ws_dir)
        # A workspace named after a video IS that video's workspace — turning
        # it into a skill means the material was learned: mark the video
        # learned and tag the skill with its video id.
        index = load_index()
        is_video = ws in index["videos"]
        if is_video:
            index["videos"][ws]["learned"] = True
            save_index(index)
        lines = [
            "---",
            f"name: {name}",
            f"description: \"Personal skill built from the teaching workspace '{title}' (teach/{ws})\"",
            f"source_workspace: {ws}",
        ]
        if is_video:
            lines.append(f"video_id: {ws}")
        lines += [
            "---",
            "",
            f"# {name}",
            "",
            f"Built from teaching workspace `teach/{ws}` (mission: {title}).",
            "",
        ]
        if mission.is_file():
            lines += ["## Mission", "", mission.read_text(encoding="utf-8", errors="replace").strip(), ""]
        if lessons:
            lines += ["## Lessons", ""]
            lines += [f"{i}. [{t}](../teach/{ws}/lessons/{f})" for i, (f, t) in enumerate(lessons, 1)]
            lines += ["", "Work through the lessons in order; each is a self-contained HTML page.", ""]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines), encoding="utf-8")

        response: dict = {"slug": slug, "path": f"skills/{slug}/SKILL.md"}
        if body.get("distill"):
            job_id = self.spawn_skill_distill(ws, slug, name)
            if job_id:
                response["job_id"] = job_id
        self.send_json(response)

    def spawn_skill_distill(self, ws: str, slug: str, name: str):
        """Agent job that rewrites the baseline SKILL.md with distilled,
        loadable content. Returns job_id, or None if no agent is available."""
        durable = agentd_client.backend() == "durable"
        exe = find_kimi()
        if not durable and not exe:
            return None
        with AGENT_JOBS_LOCK:
            for existing in AGENT_JOBS.values():
                if (existing.get("workspace") == ws
                        and existing.get("status") in ("starting", "running")):
                    return None  # caller tells the user to wait for the running job
        prompt = skill_distill_prompt(ws, slug, name)
        job_id = uuid.uuid4().hex[:12]
        with AGENT_JOBS_LOCK:
            AGENT_JOBS[job_id] = {
                "id": job_id, "workspace": ws, "status": "starting",
                "backend": "durable" if durable else "cli",
                "key": f"distill:{slug}",
                "lines": [], "exit_code": None, "pid": None,
                "started": int(time.time()), "ended": None,
            }
        threading.Thread(
            target=run_agent_job,
            args=(job_id, ROOT, prompt, True, exe),
            daemon=True,
        ).start()
        return job_id

    def api_file_preview(self, query: dict):
        """First N chars of a text file under the app root, for the Structure
        tab's peek-inside preview. Binary/unknown extensions are refused."""
        rel = (query.get("path") or [""])[0]
        full = (query.get("full") or [""])[0] == "1"
        if not rel or "\\" in rel or rel.startswith("/") or ".." in rel.split("/"):
            return self.send_error_json(400, "bad path")
        target = (ROOT / rel).resolve()
        if not str(target).startswith(str(ROOT.resolve()) + os.sep) or not target.is_file():
            return self.send_error_json(404, "not_found")
        if target.suffix.lower() not in PREVIEW_TEXT_EXT:
            return self.send_error_json(415, "not a previewable text file")
        try:
            size = target.stat().st_size
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return self.send_error_json(500, str(e))
        limit = PREVIEW_FULL_CHARS if full else PREVIEW_CHARS
        truncated = len(text) > limit
        self.send_json({
            "path": rel, "size": size, "chars": len(text),
            "truncated": truncated, "full": full,
            "content": text[:limit] if truncated else text,
        })

    def api_skill_install(self):
        """Copy a created skill into ~/.kimi-code/skills so any Kimi Code
        session can load it via /skill:<slug>."""
        import shutil
        body = self.read_body()
        if not isinstance(body, dict) or not body.get("slug"):
            return self.send_error_json(400, "body must be {\"slug\": ...}")
        slug = str(body["slug"])
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,80}", slug):
            return self.send_error_json(400, "bad slug")
        src = (SKILLS_DIR / slug).resolve()
        if not str(src).startswith(str(SKILLS_DIR.resolve()) + os.sep) or not src.is_dir():
            return self.send_error_json(404, "skill not found")
        skill_file = src / "SKILL.md"
        if not skill_file.is_file():
            return self.send_error_json(404, "skill has no SKILL.md")
        dst = KIMI_SKILLS_DIR / slug
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dst, dirs_exist_ok=True)
        self.send_json({"slug": slug, "installed_to": str(dst),
                        "invoke": f"/skill:{slug}"})

    def api_agent_status(self):
        exe = find_kimi()
        durable = agentd_client.backend() == "durable"
        mode = "pi-durable" if durable else ("kimi-cli" if exe else None)
        self.send_json({"available": durable or bool(exe), "exe": exe, "mode": mode})

    def api_agent_run(self):
        body = self.read_body()
        if not isinstance(body, dict) or not body.get("message"):
            return self.send_error_json(400, "body must be {\"workspace\": ..., \"message\": ...}")
        ws = str(body.get("workspace") or "")
        if not safe_ws_name(ws):
            return self.send_error_json(400, "bad workspace id")
        ws_dir = (TEACH_DIR / ws).resolve()
        if not str(ws_dir).startswith(str(TEACH_DIR.resolve()) + os.sep) or not ws_dir.is_dir():
            return self.send_error_json(404, "workspace not found")
        exe = find_kimi()
        durable = agentd_client.backend() == "durable"
        if not durable and not exe:
            return self.send_error_json(503, "kimi CLI not found on PATH")
        with AGENT_JOBS_LOCK:
            for existing in AGENT_JOBS.values():
                if (existing.get("workspace") == ws
                        and existing.get("status") in ("starting", "running")):
                    return self.send_error_json(
                        409, "the agent is still working on your previous message — "
                             "your text stays in the input box; send it again when the job finishes")
        job_id = uuid.uuid4().hex[:12]
        with AGENT_JOBS_LOCK:
            AGENT_JOBS[job_id] = {
                "id": job_id, "workspace": ws, "status": "starting",
                "backend": "durable" if durable else "cli",
                "lines": [], "exit_code": None, "pid": None,
                "started": int(time.time()), "ended": None,
            }
        threading.Thread(
            target=run_agent_job,
            args=(job_id, ws_dir, str(body["message"]), bool(body.get("new_session")), exe),
            daemon=True,
        ).start()
        self.send_json({"job_id": job_id})

    def api_agent_console_run(self):
        """The Console tab's standalone agent: one persistent conversation for
        the whole app (key console:main, cwd ROOT), no workspace attached.
        Optional `node` (a sandbox name) routes the conversation to that VM's
        in-guest sidecar — key stays console:main, but each node has its OWN
        console:main conversation; the local one is untouched."""
        body = self.read_body()
        if not isinstance(body, dict) or not body.get("message"):
            return self.send_error_json(400, 'body must be {"message": ...}')
        node = str(body.get("node") or "").strip() or None
        if node and not cua_client.valid_name(node):
            return self.send_error_json(400, "bad node name — use lowercase letters, digits and dashes (max 40 chars)")
        if node:
            endpoint = vm_nodes.node_endpoint(node)
            if not endpoint or not vm_nodes.node_healthy(node):
                return self.send_error_json(
                    503, f"node '{node}' has no healthy agentd agent — "
                         "open CUA Spaces and press Install agent on that VM")
            durable, exe = True, None  # the node's own sidecar is the backend
        else:
            exe = find_kimi()
            durable = agentd_client.backend() == "durable"
            if not durable and not exe:
                return self.send_error_json(503, "no AI agent available (agentd sidecar down, kimi CLI not found)")
        with AGENT_JOBS_LOCK:
            for existing in AGENT_JOBS.values():
                if (existing.get("key") == "console:main"
                        and existing.get("node") == node
                        and existing.get("status") in ("starting", "running")):
                    return self.send_error_json(
                        409, "the agent is still working on your previous message — "
                             "your text stays in the input box; send it again when the job finishes")
        job_id = uuid.uuid4().hex[:12]
        with AGENT_JOBS_LOCK:
            AGENT_JOBS[job_id] = {
                "id": job_id, "workspace": None, "key": "console:main",
                "node": node,
                "status": "starting",
                "backend": "durable" if durable else "cli",
                "lines": [], "exit_code": None, "pid": None,
                "started": int(time.time()), "ended": None,
            }
        threading.Thread(
            target=run_agent_job,
            args=(job_id, ROOT, str(body["message"]), bool(body.get("new_session")), exe),
            daemon=True,
        ).start()
        self.send_json({"job_id": job_id})

    # -- cua (CUA Spaces tab): create/remove LAN-client VMs via the cua CLI.
    # Sandboxes live on the machine, outside the repo — cua_client only
    # locates/parses; long-running commands stream as agent-style jobs.

    def api_cua_status(self):
        exe = cua_client.find_cua()
        installed = bool(exe)
        note = ""
        if not installed:
            if sys.platform.startswith("win"):
                note = ("cua is not installed. On Windows, sandboxes (gVisor "
                        "containers) need WSL2 — install both with: "
                        "irm https://cua.ai/install.ps1 | iex")
            else:
                note = ("cua is not installed — install it with: "
                        "curl -fsSL https://cua.ai/install.sh | bash")
        self.send_json({
            "installed": installed,
            "version": cua_client.version(exe) if installed else None,
            "sandboxes": cua_client.list_sandboxes(exe) if installed else [],
            "note": note,
        })

    def spawn_cua_job(self, name: str, argv: list[str]):
        """Record + thread for one cua create/remove. Returns job_id, or a
        (status, message) tuple when a job for this sandbox is still running."""
        key = f"cua:{name}"
        with AGENT_JOBS_LOCK:
            for existing in AGENT_JOBS.values():
                if (existing.get("key") == key
                        and existing.get("status") in ("starting", "running")):
                    return 409, (f"a create/remove for {name} is still running — "
                                 "wait for it to finish")
            job_id = uuid.uuid4().hex[:12]
            AGENT_JOBS[job_id] = {
                "id": job_id, "workspace": None, "key": key,
                "status": "starting", "backend": "cli-style",
                "lines": [], "exit_code": None, "pid": None,
                "started": int(time.time()), "ended": None,
            }
        runner = run_cua_create_job if ("create" in argv) else run_cua_job
        threading.Thread(target=runner, args=(job_id, argv), daemon=True).start()
        return job_id

    def api_cua_create(self):
        body = self.read_body()
        if not isinstance(body, dict):
            return self.send_error_json(400, 'body must be {"name": ...}')
        if not cua_client.find_cua():
            return self.send_error_json(503, "cua CLI not found on PATH — install it first (see the CUA Spaces tab)")
        raw = str(body.get("name") or "").strip()
        if not raw:
            # no name given: claim the first free ytl-vm-N slot
            taken = {s.get("name") for s in cua_client.list_sandboxes()}
            n = 1
            while f"ytl-vm-{n}" in taken:
                n += 1
            raw = f"vm-{n}"
        name = cua_client.full_name(raw)
        if not cua_client.valid_name(name):
            return self.send_error_json(400, "bad name — use lowercase letters, digits and dashes (max 40 chars)")
        result = self.spawn_cua_job(name, cua_client.create_argv(name))
        if isinstance(result, tuple):
            return self.send_error_json(result[0], result[1])
        self.send_json({"job_id": result})

    def api_cua_view(self):
        """Desktop viewer: starts `cua sb view <name>`, captures the Viewer
        URL it prints, keeps the process serving, and returns the URL so the
        UI opens it in the user's browser. One viewer per sandbox (a second
        click replaces the first)."""
        body = self.read_body()
        if not isinstance(body, dict) or not body.get("name"):
            return self.send_error_json(400, 'body must be {"name": ...}')
        exe = cua_client.find_cua()
        if not exe:
            return self.send_error_json(503, "cua CLI not found on PATH — install it first (see the CUA Spaces tab)")
        name = cua_client.full_name(str(body["name"]))
        if not cua_client.valid_name(name):
            return self.send_error_json(400, "bad name — use lowercase letters, digits and dashes (max 40 chars)")
        if not any(s.get("name") == name for s in cua_client.list_sandboxes()):
            return self.send_error_json(404, f"sandbox {name} not found")
        help_run = None
        try:
            help_run = subprocess.run([exe, "sb", "view", "--help"],
                                      capture_output=True, timeout=30)
            help_text = (help_run.stdout or b"").decode("utf-8", "replace")
        except (OSError, subprocess.SubprocessError):
            help_text = ""
        if (help_run is None or help_run.returncode != 0
                or not re.search(r"\bsandbox view\b", help_text)):
            return self.send_error_json(503, "this cua version has no `sb view` desktop viewer — update cua")
        old = CUA_VIEW_PROCS.pop(name, None)
        if old and old.poll() is None:
            try:
                old.kill()
            except OSError:
                pass
        try:
            proc = subprocess.Popen([exe, "sb", "view", name],
                                    stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL,
                                    text=True, encoding="utf-8", errors="replace")
        except OSError as e:
            return self.send_error_json(500, f"could not start the viewer: {e}")
        url = None
        try:
            deadline = time.time() + 20
            while time.time() < deadline:
                line = proc.stdout.readline()
                if not line:
                    break
                m = re.search(r"https?://\S+", line)
                if m:
                    url = m.group(0)
                    break
        except OSError:
            pass
        if not url:
            try:
                proc.kill()
            except OSError:
                pass
            return self.send_error_json(502, "the viewer did not report a URL — run `cua sb view "
                                             + name + "` in a terminal to see why")
        CUA_VIEW_PROCS[name] = proc
        threading.Thread(target=_drain_cua_view, args=(proc, name), daemon=True).start()
        self.send_json({"ok": True, "url": url,
                        "note": "viewer serves ~1 h; clicking again restarts it"})
    def api_vm_nodes(self):
        """The CUA tab's per-VM agent state: name, whether the in-guest
        sidecar answers /health (online), whether it has a key (agent), and
        its endpoint URL."""
        nodes = []
        for n in vm_nodes.list_nodes():
            health = vm_nodes.node_health(n["name"]) if n["online"] else None
            nodes.append({
                "name": n["name"],
                "online": n["online"],
                "agent": bool(health and health.get("ok") and health.get("keyConfigured")),
                "endpoint": n["endpoint"],
            })
        self.send_json({"nodes": nodes})

    def api_cua_provision(self):
        """Install agent: push the agentd sidecar into a sandbox guest so the
        VM becomes an agent node. Streams the provisioning steps as a job."""
        body = self.read_body()
        if not isinstance(body, dict) or not body.get("name"):
            return self.send_error_json(400, 'body must be {"name": ...}')
        if not cua_client.find_cua():
            return self.send_error_json(503, "cua CLI not found on PATH — install it first (see the CUA Spaces tab)")
        name = cua_client.full_name(str(body["name"]))
        if not cua_client.valid_name(name):
            return self.send_error_json(400, "bad name — use lowercase letters, digits and dashes (max 40 chars)")
        key = f"provision:{name}"
        with AGENT_JOBS_LOCK:
            for existing in AGENT_JOBS.values():
                if (existing.get("key") == key
                        and existing.get("status") in ("starting", "running")):
                    return self.send_error_json(
                        409, f"Install agent for {name} is already running — wait for it to finish")
            job_id = uuid.uuid4().hex[:12]
            AGENT_JOBS[job_id] = {
                "id": job_id, "workspace": None, "key": key,
                "status": "starting", "backend": "cli-style",
                "lines": [], "exit_code": None, "pid": None,
                "started": int(time.time()), "ended": None,
            }
        threading.Thread(target=run_provision_job, args=(job_id, name), daemon=True).start()
        self.send_json({"job_id": job_id})

    def api_cua_remove(self):
        body = self.read_body()
        if not isinstance(body, dict) or not body.get("name"):
            return self.send_error_json(400, 'body must be {"name": ...}')
        if not cua_client.find_cua():
            return self.send_error_json(503, "cua CLI not found on PATH — install it first (see the CUA Spaces tab)")
        name = cua_client.full_name(str(body["name"]))
        if not cua_client.valid_name(name):
            return self.send_error_json(400, "bad name — use lowercase letters, digits and dashes (max 40 chars)")
        result = self.spawn_cua_job(name, cua_client.remove_argv(name))
        if isinstance(result, tuple):
            return self.send_error_json(result[0], result[1])
        self.send_json({"job_id": result})

    # -- cua terminals: a persistent raw bash inside one sandbox, streamed
    # line-by-line to an inline terminal pane in the CUA Spaces tab.

    def api_cua_term_open(self):
        body = self.read_body()
        if not isinstance(body, dict) or not body.get("name"):
            return self.send_error_json(400, 'body must be {"name": ...}')
        if not cua_client.find_cua():
            return self.send_error_json(503, "cua CLI not found on PATH — install it first (see the CUA Spaces tab)")
        name = cua_client.full_name(str(body["name"]))
        result = open_term(name)
        if isinstance(result, tuple):
            return self.send_error_json(result[0], result[1])
        self.send_json({"term_id": result})

    def api_cua_term_get(self, term_id: str):
        with CUA_TERMS_LOCK:
            term = CUA_TERMS.get(term_id)
        if not term:
            return self.send_error_json(404, "no such terminal")
        _term_reap_idle(term)
        with CUA_TERMS_LOCK:
            term["last_active"] = time.monotonic()
            snap = {"status": term["status"], "name": term["name"],
                    "exit_code": term["exit_code"],
                    "lines": list(term["lines"][-CUA_TERM_MAX_LINES:])}
        self.send_json(snap)

    def api_cua_term_send(self, term_id: str):
        body = self.read_body()
        if not isinstance(body, dict) or not isinstance(body.get("line"), str):
            return self.send_error_json(400, 'body must be {"line": ...}')
        with CUA_TERMS_LOCK:
            term = CUA_TERMS.get(term_id)
        if not term:
            return self.send_error_json(404, "no such terminal")
        _term_reap_idle(term)
        if term["status"] != "open":
            return self.send_error_json(410, "terminal is closed")
        try:
            term["proc"].stdin.write(body["line"] + "\n")
            term["proc"].stdin.flush()
        except Exception as e:  # noqa: BLE001
            _term_mark_closed(term, term["proc"].poll())
            return self.send_error_json(410, f"terminal is closed ({type(e).__name__})")
        with CUA_TERMS_LOCK:
            term["last_active"] = time.monotonic()
        self.send_json({"ok": True})

    def api_cua_term_close(self, term_id: str):
        with CUA_TERMS_LOCK:
            term = CUA_TERMS.get(term_id)
        if not term:
            return self.send_error_json(404, "no such terminal")
        close_term(term)
        self.send_json({"ok": True})

    # -- delegate-a-task v1: the agent thinks in the master's durable
    # conversation, but every command it runs executes ON the VM via
    # `cua sb exec <vm> -- …`. Requires the sidecar (the kimi CLI fallback
    # can't carry per-conversation instructions).

    def maybe_resume_vm(self, name: str) -> None:
        """Best-effort wake of a suspended/stopped sandbox before delegating:
        cua has no `sb start`, so a non-running VM gets `sb resume`. Synchronous,
        bounded, and fully error-tolerant — the instructions tell the agent to
        report a stopped VM instead of retrying forever."""
        try:
            exe = cua_client.find_cua()
            if not exe:
                return
            sb = next((s for s in cua_client.list_sandboxes(exe)
                       if isinstance(s, dict) and s.get("name") == name), None)
            if not sb or str(sb.get("status") or "") == "running":
                return
            subprocess.run([exe, *cua_client.resume_argv(name)],
                           capture_output=True, timeout=30)
        except Exception:  # noqa: BLE001
            pass

    def api_agent_vm_task(self):
        """Delegate a task for a VM. Without `node`: v1 exec-delegation — the
        agent thinks in this machine's durable conversation and runs every
        command ON the VM via `cua sb exec`. With `node`: native — the whole
        task runs on the VM's own in-guest sidecar (no cua-exec instructions,
        cwd /home/cua), so the conversation and its work live on that VM."""
        body = self.read_body()
        if not isinstance(body, dict):
            return self.send_error_json(400, 'body must be {"vm": ..., "task": ...}')
        vm = str(body.get("vm") or "")
        if not cua_client.valid_name(vm):
            return self.send_error_json(400, "bad vm name — use lowercase letters, digits and dashes (max 40 chars)")
        task = str(body.get("task") or "").strip()
        if not task:
            return self.send_error_json(400, 'body must be {"vm": ..., "task": ...}')
        node = str(body.get("node") or "").strip() or None
        if node and not cua_client.valid_name(node):
            return self.send_error_json(400, "bad node name — use lowercase letters, digits and dashes (max 40 chars)")
        if node:
            endpoint = vm_nodes.node_endpoint(node)
            if not endpoint or not vm_nodes.node_healthy(node):
                return self.send_error_json(
                    503, f"node '{node}' has no healthy agentd agent — "
                         "open CUA Spaces and press Install agent on that VM")
        elif agentd_client.backend() != "durable":
            return self.send_error_json(503, "VM tasks need the Pi Durable sidecar")
        self.maybe_resume_vm(vm)
        session = str(body.get("session") or "").strip() or uuid.uuid4().hex[:8]
        key = f"vmtask:{vm}:{session}"
        with AGENT_JOBS_LOCK:
            for existing in AGENT_JOBS.values():
                if (existing.get("key") == key
                        and existing.get("status") in ("starting", "running")):
                    return self.send_error_json(
                        409, "a task with this session is still running on that VM — "
                             "wait for it to finish")
        job_id = uuid.uuid4().hex[:12]
        with AGENT_JOBS_LOCK:
            AGENT_JOBS[job_id] = {
                "id": job_id, "workspace": None, "key": key,
                "node": node,
                "status": "starting", "backend": "durable",
                "instructions": None if node else vm_task_instructions(vm, task),
                "lines": [], "exit_code": None, "pid": None,
                "started": int(time.time()), "ended": None,
            }
        threading.Thread(
            target=run_agent_job,
            args=(job_id, ROOT, task, False, None),
            daemon=True,
        ).start()
        self.send_json({"job_id": job_id})

    def api_agent_job(self, job_id: str):
        with AGENT_JOBS_LOCK:
            job = AGENT_JOBS.get(job_id)
            if not job:
                return self.send_error_json(404, "not_found")
            snapshot = {k: job.get(k) for k in
                        ("id", "workspace", "status", "exit_code", "started", "ended", "cmd_preview", "backend")}
            snapshot["lines"] = list(job["lines"])
        self.send_json(snapshot)

    def api_agent_cancel(self, job_id: str):
        with AGENT_JOBS_LOCK:
            job = AGENT_JOBS.get(job_id)
            if not job:
                return self.send_error_json(404, "not_found")
            pid = job.get("pid")
            remote_id = job.get("remote_id") if job.get("backend") == "durable" else None
            base = job.get("base")
            job["status"] = "cancelled"
        if remote_id:
            agentd_client.cancel(remote_id, base=base)
        if pid:
            try:
                import signal

                os.kill(pid, signal.SIGTERM)
            except Exception:
                pass
        self.send_json({"ok": True})

    def api_delete_workspace(self, ws: str):
        if not safe_ws_name(ws):
            return self.send_error_json(400, "bad workspace id")
        ws_dir = (TEACH_DIR / ws).resolve()
        if not str(ws_dir).startswith(str(TEACH_DIR.resolve()) + os.sep) or not ws_dir.is_dir():
            return self.send_error_json(404, "workspace not found")
        # cancel any running agent job in this workspace first
        remote_ids = []
        with AGENT_JOBS_LOCK:
            for job in AGENT_JOBS.values():
                if job.get("workspace") == ws and job.get("status") in ("starting", "running"):
                    pid = job.get("pid")
                    if job.get("backend") == "durable" and job.get("remote_id"):
                        remote_ids.append(job["remote_id"])
                    job["status"] = "cancelled"
                    if pid:
                        try:
                            import signal

                            os.kill(pid, signal.SIGTERM)
                        except Exception:
                            pass
        for remote_id in remote_ids:
            agentd_client.cancel(remote_id)
        import shutil

        try:
            shutil.rmtree(ws_dir)
        except OSError as e:
            return self.send_error_json(409, f"could not delete workspace (it may be in use): {e}")
        self.send_json({"ok": True})

    def api_lessons(self, vid: str):
        if ".." in vid:
            return self.send_error_json(400, "bad id")
        lessons_dir = (TEACH_DIR / vid / "lessons").resolve()
        if not str(lessons_dir).startswith(str(TEACH_DIR.resolve()) + os.sep):
            return self.send_error_json(400, "bad id")
        files = []
        if lessons_dir.is_dir():
            files = sorted(f.name for f in lessons_dir.iterdir() if f.is_file() and f.suffix == ".html")
        self.send_json({"lessons": [{"file": f} for f in files]})

    def api_lesson(self, query: dict):
        vid = (query.get("video") or [""])[0]
        name = (query.get("file") or [""])[0]
        if not vid or not name or ".." in vid or ".." in name or os.sep in name or "/" in name:
            return self.send_error_json(400, "bad video or file")
        lessons_dir = (TEACH_DIR / vid / "lessons").resolve()
        target = lessons_dir / name
        if not str(target).startswith(str((TEACH_DIR / vid).resolve()) + os.sep):
            return self.send_error_json(400, "bad file")
        if not target.is_file():
            return self.send_error_json(404, "not_found")
        self.serve_file(target, "text/html")

    def api_workspace_file(self, ws: str, sub: str):
        """Serve any file inside a teach workspace under /teach/<ws>/<path> so
        lesson HTML's relative asset links (../assets/…) resolve correctly."""
        if ".." in sub or sub.startswith("/") or "\\" in sub:
            return self.send_error_json(400, "bad path")
        ws_root = (TEACH_DIR / ws).resolve()
        if not str(ws_root).startswith(str(TEACH_DIR.resolve()) + os.sep) or not ws_root.is_dir():
            return self.send_error_json(404, "workspace not found")
        target = (ws_root / sub).resolve()
        if not str(target).startswith(str(ws_root) + os.sep) or not target.is_file():
            return self.send_error_json(404, "not_found")
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if target.suffix == ".js":
            content_type = "text/javascript"
        self.serve_file(target, content_type)

    def api_get_progress(self, vid: str):
        data = read_json(PROGRESS_DIR / f"{vid}.json", {"completed": []})
        completed = data.get("completed") if isinstance(data, dict) else []
        self.send_json({"completed": completed if isinstance(completed, list) else []})

    def api_set_progress(self, vid: str):
        body = self.read_body()
        if not isinstance(body, dict) or not isinstance(body.get("completed"), list):
            return self.send_error_json(400, "body must be {\"completed\": [...]}")
        write_json_atomic(PROGRESS_DIR / f"{vid}.json", {"completed": body["completed"]})
        self.send_json({"completed": body["completed"]})

    def api_skill_create(self):
        body = self.read_body()
        if not isinstance(body, dict) or not body.get("video_id"):
            return self.send_error_json(400, "body must be {\"video_id\": ...}")
        vid = str(body["video_id"])
        learnings = read_json(LEARNINGS_DIR / f"{vid}.json", None)
        if not isinstance(learnings, dict):
            return self.send_error_json(400, f"no learnings for {vid}")
        index = load_index()
        video = index["videos"].get(vid, {})
        slug = f"{kebab(str(video.get('title') or learnings.get('title') or vid))}-{vid}"
        slug = slug[:60]
        path = SKILLS_DIR / slug / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(skill_md(learnings, video, vid), encoding="utf-8")
        self.send_json({"slug": slug, "path": f"skills/{slug}/SKILL.md"})

    def api_relations(self, query: dict):
        raw = (query.get("ids") or [""])[0]
        ids = [i for i in raw.split(",") if i]
        if not 1 <= len(ids) <= 40:
            return self.send_error_json(400, "ids must contain 1-40 video ids")
        self.send_json(build_relations(ids))

    def api_update_settings(self):
        body = self.read_body()
        if not isinstance(body, dict):
            return self.send_error_json(400, "body must be a JSON object")
        settings = load_settings()
        old_port = settings.get("port")
        for k, v in body.items():
            if k == "tabs":
                if not isinstance(v, list) or not all(
                    isinstance(t, dict) and isinstance(t.get("id"), str) for t in v
                ):
                    return self.send_error_json(400, "tabs must be an array of objects with ids")
                settings["tabs"] = v
            elif k in DEFAULT_SETTINGS:
                settings[k] = v
        save_settings(settings)
        resp = dict(settings)
        if body.get("port") is not None and body.get("port") != old_port:
            resp["note"] = "restart required"
        self.send_json(resp)

    def api_profile(self):
        p = profile_path()
        if not p.is_file():
            return self.send_error_json(404, "not_found")
        self.send_json({"markdown": p.read_text(encoding="utf-8"), "path": str(p)})

    def api_profile_skills(self):
        body = self.read_body()
        if not isinstance(body, dict) or not body.get("skill"):
            return self.send_error_json(400, "body must be {\"skill\": ...}")
        skill = str(body["skill"])
        level = body.get("level")
        if level not in (None, "strong", "basic", "weak"):
            return self.send_error_json(400, "level must be strong|basic|weak|null")
        p = profile_path()
        if not p.is_file():
            return self.send_error_json(404, "profile not found")
        md = p.read_text(encoding="utf-8")
        updated = update_profile_markdown(md, skill, level)
        p.write_text(updated, encoding="utf-8")
        store = read_json(PROFILE_SKILLS_JSON, {"skills": []})
        store.setdefault("skills", []).append({
            "skill": skill,
            "level": level,
            "source_video": body.get("source_video"),
            "date": time.strftime("%Y-%m-%d"),
        })
        write_json_atomic(PROFILE_SKILLS_JSON, store)
        self.send_json({"markdown": updated})

    def api_put_profile(self):
        body = self.read_body()
        if not isinstance(body, dict) or not isinstance(body.get("markdown"), str):
            return self.send_error_json(400, "body must be {\"markdown\": ...}")
        p = profile_path()
        if not p.parent.is_dir():
            return self.send_error_json(404, "profile directory not found")
        with LOCK:
            p.write_text(body["markdown"], encoding="utf-8")
        self.send_json({"ok": True, "path": str(p)})

    def api_shutdown(self):
        self.send_json({"ok": True, "message": "server shutting down"})
        threading.Timer(0.4, self.server.shutdown).start()

    def api_test_results(self):
        data = read_json(TEST_RESULTS_JSON, None)
        if not isinstance(data, dict):
            return self.send_json({"ran_at": None, "duration": 0, "total": 0,
                                   "passed": 0, "failed": 0, "tests": [],
                                   "note": "tests have not been run yet"})
        self.send_json(data)

    def api_tests_run(self):
        if not TEST_RUN_LOCK.acquire(blocking=False):
            return self.send_error_json(409, "a test run is already in progress")
        try:
            proc = subprocess.run(
                [str(PYTHON_EXE), str(TEST_RUNNER)],
                cwd=str(ROOT), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=600,
            )
            data = read_json(TEST_RESULTS_JSON, None)
            if not isinstance(data, dict):
                return self.send_error_json(500, "test runner produced no results",
                                            )
            data["runner_stderr"] = (proc.stderr or "")[-2000:]
            self.send_json(data)
        except subprocess.TimeoutExpired:
            self.send_error_json(500, "test run timed out")
        finally:
            TEST_RUN_LOCK.release()


# ---------------------------------------------------------------- reverse relay
# cua on Windows publishes no host port into QEMU guests and
# `cua sb port-forward` cannot tunnel into them — but the guest CAN dial out
# (slirp NAT). So the direction is reversed: the in-guest agentd sidecar
# dials OUT to a relay listener on the master (RELAY_BASE_DIAL+i); the
# master's local HTTP clients connect to the paired client port
# (RELAY_BASE_CLIENT+i) and both ends are piped. All existing master-side
# node logic (vm_nodes health/conversations, Console "Run on",
# build_network) then works unchanged over http://<master-ip>:18473+i.
# Pure in-memory plumbing: no persistence, no registry, inert when unused.

RELAY_BASE_DIAL = 18472
RELAY_BASE_CLIENT = 18473
RELAY_COUNT = 10


def relay_index_for(client_port: int) -> int:
    """Client port (RELAY_BASE_CLIENT+i) -> relay index i."""
    return int(client_port) - RELAY_BASE_CLIENT


_RELAY_OFF = b"HTTP/1.1 502 Relay Off\r\nContent-Length: 0\r\n\r\n"


class _Relay:
    """One relay index: a dial-in listener (guest) + a client listener
    (master-side HTTP). The most recent dial-in owns the channel; a new
    dial-in replaces it (old socket closed). Clients are serialized: each
    client connection pipes both ways against the current channel, and the
    channel itself survives the client (closing it per request would force
    the guest into a reconnect backoff on every health probe)."""

    SHUTTLE_TIMEOUT = 1.0  # lets a session's shuttles notice "session over"

    def __init__(self, index: int, dial_port: int, client_port: int):
        self.index = index
        self.dial_port = dial_port
        self.client_port = client_port
        self.lock = threading.Lock()
        self.guest: socket.socket | None = None
        self.session_lock = threading.Lock()
        self.listeners: list[socket.socket] = []

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        self.listeners.append(self._listen(self.dial_port, self._run_dial))
        self.listeners.append(self._listen(self.client_port, self._run_client))

    def close(self) -> None:
        for srv in self.listeners:
            try:
                srv.close()
            except OSError:
                pass
        with self.lock:
            guest, self.guest = self.guest, None
        if guest is not None:
            self._close(guest)

    def _listen(self, port: int, handler) -> socket.socket:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(("0.0.0.0", port))
            srv.listen(16)
        except OSError:
            srv.close()
            raise
        threading.Thread(target=self._accept_loop, args=(srv, handler),
                         daemon=True).start()
        return srv

    def _accept_loop(self, srv: socket.socket, handler) -> None:
        while True:
            try:
                conn, _addr = srv.accept()
            except OSError:
                return  # listener closed
            threading.Thread(target=self._guarded, args=(handler, conn),
                             daemon=True).start()

    @staticmethod
    def _guarded(handler, conn: socket.socket) -> None:
        try:
            handler(conn)
        except OSError:
            pass  # a peer vanished mid-handshake — never crash the listener
        except Exception as e:  # noqa: BLE001 - the listener must survive
            print(f"relay error: {e}")
        finally:
            try:
                conn.close()
            except OSError:
                pass

    @staticmethod
    def _close(conn: socket.socket) -> None:
        try:
            conn.close()
        except OSError:
            pass

    # -- guest dial-in ------------------------------------------------------
    def _run_dial(self, conn: socket.socket) -> None:
        conn.settimeout(self.SHUTTLE_TIMEOUT)
        with self.lock:
            old, self.guest = self.guest, conn
        if old is not None:
            self._close(old)  # newest dial-in wins
        # Keep the handler (and ownership of the socket) alive until the
        # guest hangs up or a newer dial-in replaces this channel — _guarded
        # closes the socket only once this returns.
        while True:
            try:
                if conn.recv(1, socket.MSG_PEEK) == b"":
                    return  # guest hung up (or was closed by replacement)
            except socket.timeout:
                with self.lock:
                    if self.guest is not conn:
                        return  # replaced
            except OSError:
                return
            time.sleep(0.05)  # data pending: let the shuttles consume it

    # -- client sessions ------------------------------------------------------
    def _run_client(self, conn: socket.socket) -> None:
        # Both sockets get a short recv timeout: a parked recv must poll
        # `alive` — on Windows shutdown() does NOT wake a blocked recv on
        # another thread.
        conn.settimeout(self.SHUTTLE_TIMEOUT)
        with self.session_lock:  # one client at a time on the channel
            with self.lock:
                guest = self.guest
            if guest is None:
                # fast-fail: probe -> node offline. Shutdown the write side
                # and drain the client's bytes first — closing a socket with
                # unread received data makes Windows answer with RST, which
                # would destroy the 502 the client is already reading.
                conn.sendall(_RELAY_OFF)
                try:
                    conn.shutdown(socket.SHUT_WR)
                    while conn.recv(1024):
                        pass
                except OSError:
                    pass
                return
            alive = threading.Event()
            alive.set()
            guest_broken = threading.Event()
            threads = [
                threading.Thread(target=self._shuttle,
                                 args=(conn, guest, alive, False, guest_broken),
                                 daemon=True),
                threading.Thread(target=self._shuttle,
                                 args=(guest, conn, alive, True, guest_broken),
                                 daemon=True),
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            if guest_broken.is_set():
                # the channel itself died (VM suspended / agentd gone): drop
                # it so the next client fails fast instead of hanging
                with self.lock:
                    if self.guest is guest:
                        self.guest = None
                self._close(guest)

    def _shuttle(self, src: socket.socket, dst: socket.socket,
                 alive: threading.Event, src_is_guest: bool,
                 guest_broken: threading.Event) -> None:
        broke = False  # True only when src (the guest channel) itself failed
        try:
            while alive.is_set():
                try:
                    chunk = src.recv(65536)
                except socket.timeout:
                    continue  # check alive, then keep reading the channel
                except (OSError, ValueError):
                    broke = True  # closed under us (e.g. channel replaced)
                    break
                if not chunk:
                    broke = True  # src's peer sent FIN — src is dead
                    break
                try:
                    dst.sendall(chunk)
                except (OSError, ValueError):
                    break  # dst vanished mid-session; src is fine
        finally:
            alive.clear()
            if src_is_guest and broke:
                guest_broken.set()
            if src_is_guest:
                # Unblock the sibling shuttle parked in recv on the client
                # socket. The shared guest channel is never touched: a FIN
                # to the guest would kill its persistent pipe.
                try:
                    dst.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass


def start_relays(dial_base: int = RELAY_BASE_DIAL,
                 client_base: int = RELAY_BASE_CLIENT,
                 count: int = RELAY_COUNT) -> list[_Relay]:
    """Bind `count` relay index pairs and return the started handles.
    Bases are injectable so tests can use ephemeral port ranges."""
    relays = []
    for i in range(count):
        relay = _Relay(i, dial_base + i, client_base + i)
        try:
            relay.start()
        except OSError as e:
            print(f"reverse relay: index {i} unavailable ({e}) — skipped")
            relay.close()
            continue
        relays.append(relay)
    return relays


def main() -> None:
    restore_jobs()
    load_remote_nodes()
    relays = start_relays()
    if relays:
        print(f"reverse relay up: guest dial {RELAY_BASE_DIAL}.."
              f"{RELAY_BASE_DIAL + RELAY_COUNT - 1}, clients "
              f"{RELAY_BASE_CLIENT}..{RELAY_BASE_CLIENT + RELAY_COUNT - 1}")
    try:
        agentd_client.ensure_running()  # best-effort: the CLI fallback covers absence
    except Exception as e:  # noqa: BLE001
        print(f"agentd sidecar not started ({e}) — falling back to the kimi CLI")
    # Warm the cua daemon in the background so the first Network/CUA request
    # after startup doesn't eat the daemon's cold-start (takes ~10-20 s).
    threading.Thread(target=cua_client.list_sandboxes, daemon=True).start()
    settings = load_settings()
    port = int(settings.get("port", 8471))
    host = str(settings.get("host") or "127.0.0.1")
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Serving on http://127.0.0.1:{port}")
    if host in ("0.0.0.0", "::"):
        print("LAN mode — other devices can reach this app at:")
        for url in lan_urls(port):
            print(f"  {url}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
