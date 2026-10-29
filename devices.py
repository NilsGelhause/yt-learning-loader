"""Pairing-based reverse device agent ("Connect device").

Windows has no remote-management protocol on by default, so a second laptop
prepares itself: the master UI shows a short-lived pairing code, the laptop
runs one line in PowerShell (download Device-Agent.ps1, pair with the code),
and the agent then polls THIS module's data via the server every few seconds
for pre-defined tasks (install-stack, create-vm, start-beacon). Task output
streams back line by line and is mirrored into server.AGENT_JOBS so the
existing /api/agent/job/<id> transcript endpoint shows device tasks like any
other job. The device never accepts arbitrary commands from the wire: a task
is only ever one of the pre-built kinds from build_task_script.

State: DEVICES (id -> {hostname, token, paired_at, last_seen, tasks}) is
persisted to data/devices.json after every mutation; PAIR_CODES (code ->
expiry) lives in memory only.

Run via tests/run.py (see tests/test_devices.py).
"""

from __future__ import annotations

import json
import secrets
import threading
import time
import uuid
from pathlib import Path

DATA = Path(__file__).parent / "data"
DEVICES_JSON = DATA / "devices.json"

DEVICES_LOCK = threading.Lock()
DEVICES: dict[str, dict] = {}
"""id -> {hostname, token, paired_at, last_seen,
         tasks: {task_id: {kind, label, script, status, lines, exit_code,
                           created, ended, reboot_required}}}"""

PAIR_CODES: dict[str, float] = {}
"""code -> time.time() expiry. In-memory only: a code dies with the master."""

PAIR_TTL = 600.0            # a pairing code lives 10 minutes
ONLINE_WINDOW = 20.0        # last_seen younger than this = online
TASK_MAX_LINES = 800        # same cap as agent jobs
TASK_LINE_CHARS = 1000

TASK_KINDS = ("install-stack", "create-vm", "start-beacon")

# Crockford-ish alphabet: no I, L, O, 0, 1 — unambiguous over the phone.
_PAIR_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def _load_devices() -> dict:
    try:
        data = json.loads(DEVICES_JSON.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {}


DEVICES = _load_devices()


def _save_devices() -> None:
    tmp = DEVICES_JSON.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(DEVICES, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(DEVICES_JSON)


# ---------------------------------------------------------------- pair codes

def new_pair_code() -> tuple[str, float]:
    """Mint a single-use 8-char pairing code, valid for PAIR_TTL seconds."""
    now = time.time()
    with DEVICES_LOCK:
        for code, expiry in list(PAIR_CODES.items()):
            if expiry <= now:
                del PAIR_CODES[code]
        while True:
            code = "".join(secrets.choice(_PAIR_ALPHABET) for _ in range(8))
            if code not in PAIR_CODES:
                break
        expires = now + PAIR_TTL
        PAIR_CODES[code] = expires
        return code, expires


def register_device(code: str, hostname: str):
    """Consume a pairing code and create a device. Returns (device_id, token)
    or None for a wrong/expired code. The token authenticates every later
    device request."""
    now = time.time()
    with DEVICES_LOCK:
        expiry = PAIR_CODES.get(code)
        if expiry is None or expiry <= now:
            return None
        del PAIR_CODES[code]  # single use
        device_id = uuid.uuid4().hex[:12]
        token = uuid.uuid4().hex
        DEVICES[device_id] = {
            "hostname": str(hostname or "device")[:80],
            "token": token,
            "paired_at": now,
            "last_seen": now,
            "stack_done": False,
            "tasks": {},
        }
        _save_devices()
        return device_id, token


# ---------------------------------------------------------------- tasks

def device_seen(device_id: str, token: str) -> bool:
    """Authenticate a device and refresh its last_seen."""
    with DEVICES_LOCK:
        dev = DEVICES.get(device_id)
        if not dev or dev.get("token") != token:
            return False
        dev["last_seen"] = time.time()
        _save_devices()
        return True


def queue_task(device_id: str, kind: str, script: str, label: str) -> str:
    """Queue a pre-built task script on a device (oldest pending runs first)."""
    task_id = uuid.uuid4().hex[:12]
    with DEVICES_LOCK:
        dev = DEVICES[device_id]
        dev["tasks"][task_id] = {
            "device_id": device_id,
            "kind": kind, "label": label, "script": script,
            "status": "pending", "lines": [], "exit_code": None,
            "created": time.time(), "updated": time.time(), "ended": None,
            "reboot_required": False,
        }
        _save_devices()
        _mirror_to_agent_jobs(task_id, dev["tasks"][task_id])
    return task_id


def next_task(device_id: str, token: str):
    """Oldest pending task for an authenticated device, marked running.
    Returns {id, kind, script} or None (bad auth or nothing pending)."""
    with DEVICES_LOCK:
        dev = DEVICES.get(device_id)
        if not dev or dev.get("token") != token:
            return None
        dev["last_seen"] = time.time()
        pending = [(t.get("created", 0), tid)
                   for tid, t in dev["tasks"].items() if t.get("status") == "pending"]
        if not pending:
            _save_devices()
            return None
        task_id = min(pending)[1]
        task = dev["tasks"][task_id]
        task["status"] = "running"
        task["updated"] = time.time()
        _save_devices()
        _mirror_to_agent_jobs(task_id, task)
        return {"id": task_id, "kind": task["kind"], "script": task["script"]}


def push_task_output(device_id: str, token: str, task_id: str, line=None,
                     done: bool = False, exit_code=None, needs_admin: bool = False) -> bool:
    """Append one output line and/or mark a task done. needs_admin is reserved
    for a future agent-driven elevation request (v1 elevates install-stack
    unconditionally). Returns False on bad auth or unknown task."""
    with DEVICES_LOCK:
        dev = DEVICES.get(device_id)
        if not dev or dev.get("token") != token:
            return False
        dev["last_seen"] = time.time()
        task = dev["tasks"].get(task_id)
        if not task:
            return False
        task["updated"] = time.time()
        if line:
            text = str(line)[:TASK_LINE_CHARS]
            task["lines"].append({"t": "out", "x": text})
            if len(task["lines"]) > TASK_MAX_LINES:
                del task["lines"][: len(task["lines"]) - TASK_MAX_LINES]
            if "[REBOOT-REQUIRED]" in text:
                task["reboot_required"] = True
        if done:
            task["exit_code"] = exit_code
            task["status"] = "done" if not exit_code else "error"
            task["ended"] = time.time()
            # Wizard gating: only a successful install-stack (no pending
            # reboot) unlocks the create-vm / beacon tasks for this device.
            if task.get("kind") == "install-stack":
                dev["stack_done"] = (not exit_code
                                     and not task.get("reboot_required"))
        _save_devices()
        _mirror_to_agent_jobs(task_id, task)
        return True


def _mirror_to_agent_jobs(task_id: str, task: dict) -> None:
    """Mirror the task into server.AGENT_JOBS with the regular job shape so
    /api/agent/job/<id> streams device tasks into the existing transcript UI."""
    try:
        import server
    except Exception:  # noqa: BLE001 - devices.py must also work standalone
        return
    with server.AGENT_JOBS_LOCK:
        job = server.AGENT_JOBS.get(task_id)
        if job is None:
            job = {
                "id": task_id, "workspace": None, "status": task["status"],
                "backend": "device", "device_id": task.get("device_id"),
                "cmd_preview": task.get("label") or task["kind"],
                "lines": [], "exit_code": None,
                "started": int(task.get("created") or time.time()), "ended": None,
            }
            server.AGENT_JOBS[task_id] = job
        job["status"] = task["status"]
        job["lines"] = list(task["lines"])
        job["exit_code"] = task.get("exit_code")
        if task.get("ended"):
            job["ended"] = int(task["ended"])


TASK_STALE_SECONDS = 600.0


DEVICE_OFFLINE_GRACE = 90.0


def _reap_stale_tasks() -> None:
    """Running tasks that cannot make progress are marked errored so the UI
    unlocks: (a) the device went offline (agent closed / laptop shut) — its
    running tasks are interrupted at once; (b) no output for
    TASK_STALE_SECONDS (e.g. the laptop was closed mid-install)."""
    now = time.time()
    changed = False
    for dev in DEVICES.values():
        device_offline = now - (dev.get("last_seen") or 0) > DEVICE_OFFLINE_GRACE
        for tid, task in dev.get("tasks", {}).items():
            if task.get("status") != "running":
                continue
            interrupted = None
            if device_offline:
                interrupted = "[device went offline — task interrupted; re-run it]"
            elif now - (task.get("updated") or 0) > TASK_STALE_SECONDS:
                interrupted = "[timed out — no output for 10 min]"
            if interrupted:
                task["status"] = "error"
                task["exit_code"] = task.get("exit_code") or 1
                task["ended"] = now
                task["lines"].append({"t": "out", "x": interrupted})
                _mirror_to_agent_jobs(tid, task)
                changed = True
    if changed:
        _save_devices()


def list_devices() -> list[dict]:
    _reap_stale_tasks()
    now = time.time()
    with DEVICES_LOCK:
        out = []
        for device_id, dev in DEVICES.items():
            tasks = dev.get("tasks", {})
            out.append({
                "id": device_id,
                "hostname": dev.get("hostname", ""),
                "admin": None,  # the agent does not report its elevation state
                "paired_at": dev.get("paired_at"),
                "last_seen": dev.get("last_seen"),
                "online": now - (dev.get("last_seen") or 0) < ONLINE_WINDOW,
                "stack_done": bool(dev.get("stack_done")),
                "tasks": [
                    {"id": tid, "kind": t.get("kind"), "label": t.get("label"),
                     "status": t.get("status"),
                     "reboot_required": bool(t.get("reboot_required"))}
                    for tid, t in list(tasks.items())[-20:]
                ],
            })
        out.sort(key=lambda d: d["paired_at"] or 0)
        return out


# ---------------------------------------------------------------- task scripts

def _ps(value) -> str:
    """PowerShell-single-quote a value for interpolation into a task script."""
    return "'" + str(value).replace("'", "''") + "'"


def build_task_script(kind: str, ctx: dict) -> str:
    """Pre-built PowerShell a device will execute for `kind`. ctx carries
    master_url (device-reachable master URL) and, device-bound, lan_token.
    Unknown kinds raise ValueError — the device never sees arbitrary scripts
    from the wire."""
    master = str(ctx.get("master_url") or "").rstrip("/")
    tmp = "$tmp = Join-Path $env:TEMP ('ytl-kit-' + [guid]::NewGuid().ToString('N').Substring(0, 8))\nNew-Item -ItemType Directory -Force -Path $tmp | Out-Null"
    download = ("[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12\n"
                "Invoke-WebRequest -UseBasicParsing -Uri {url} -OutFile {dest}")
    if kind == "install-stack":
        # The install script enables the Windows hypervisor feature via DISM,
        # which needs admin: the DEVICE AGENT re-runs this task elevated (its
        # own UAC prompt). The log is scanned for the installer's reboot
        # message; when found, the task exits with the [REBOOT-REQUIRED]
        # marker the UI watches for.
        return "\n".join([
            "$ErrorActionPreference = 'Stop'",
            tmp,
            download.format(url=_ps(f"{master}/kit/Install-VmStack.ps1"),
                            dest="(Join-Path $tmp 'Install-VmStack.ps1')"),
            download.format(url=_ps(f"{master}/kit/QemuShim.cs"),
                            dest="(Join-Path $tmp 'QemuShim.cs')"),
            "$log = Join-Path $tmp 'install.log'",
            "powershell -ExecutionPolicy Bypass -File (Join-Path $tmp 'Install-VmStack.ps1') *>&1 | Tee-Object -FilePath $log",
            "$code = $LASTEXITCODE",
            "if (Select-String -Path $log -Pattern 'REBOOT NOW|reboot is required' -Quiet) {",
            "    Write-Output '[REBOOT-REQUIRED] reboot the laptop, then re-run the connect line'",
            "    exit 42",
            "}",
            "exit $code",
            "",
        ])
    if kind == "create-vm":
        count = int(ctx.get("count") or 1)
        prefix = str(ctx.get("name_prefix") or "laptop-vm")
        return "\n".join([
            "$ErrorActionPreference = 'Stop'",
            tmp,
            download.format(url=_ps(f"{master}/kit/New-LaptopVm.ps1"),
                            dest="(Join-Path $tmp 'New-LaptopVm.ps1')"),
            f"powershell -ExecutionPolicy Bypass -File (Join-Path $tmp 'New-LaptopVm.ps1') "
            f"-MasterUrl {_ps(master)} -Count {count} -Name {_ps(prefix)}",
            "exit $LASTEXITCODE",
            "",
        ])
    if kind == "start-beacon":
        token = str(ctx.get("lan_token") or "")
        return "\n".join([
            "$ErrorActionPreference = 'Stop'",
            "$dir = Join-Path $env:TEMP 'ytl-beacon'",
            "New-Item -ItemType Directory -Force -Path $dir | Out-Null",
            "$script = Join-Path $dir 'Register-Nodes.ps1'",
            download.format(url=_ps(f"{master}/kit/Register-Nodes.ps1"),
                            dest="$script"),
            "Start-Process powershell -ArgumentList '-NoProfile','-WindowStyle','Hidden',"
            "'-ExecutionPolicy','Bypass','-File',$script,'-MasterUrl',"
            f"{_ps(master)},'-Token',{_ps(token)} -WindowStyle Hidden",
            "Write-Output 'beacon started (hidden window, re-registers VMs every 30 s)'",
            "",
        ])
    raise ValueError(f"unknown task kind: {kind}")


def remove_device(device_id: str, online: bool) -> str | None:
    """Forget a device (and its task history). Only offline devices may be
    removed — an online one is still connected and must be unpaired by
    closing its agent. Returns None on success or an error string."""
    with DEVICES_LOCK:
        dev = DEVICES.get(device_id)
        if not dev:
            return "device not found"
        if online:
            return "device is still online — close its agent on the laptop first"
        del DEVICES[device_id]
        _save_devices()
    try:
        import server
        with server.AGENT_JOBS_LOCK:
            for jid, job in list(server.AGENT_JOBS.items()):
                if job.get("backend") == "device" and job.get("device_id") == device_id:
                    del server.AGENT_JOBS[jid]
    except Exception:  # noqa: BLE001 - mirror cleanup is best-effort
        pass
    return None
