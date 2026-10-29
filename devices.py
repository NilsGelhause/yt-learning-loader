"""Pairing-based reverse device agent ("Connect device").

Windows has no remote-management protocol on by default, so a second laptop
prepares itself: the master UI shows a short-lived pairing code, the laptop
runs one line in PowerShell (download Device-Agent.ps1, pair with the code),
and the agent then polls THIS module's data via the server every few seconds
for pre-defined tasks (install-stack, create-vm, start-beacon, open-desktop,
install-agent, expose-agent). Task output
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

TASK_KINDS = ("install-stack", "create-vm", "start-beacon", "open-desktop",
              "install-agent", "expose-agent")

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
    """Mirror the task into the server's AGENT_JOBS with the regular job shape
    so /api/agent/job/<id> streams device tasks into the existing transcript
    UI. Uses the sink injected by server.py — see JOB_SINK above."""
    if not JOB_SINK:
        return
    jobs, lock = JOB_SINK
    with lock:
        job = jobs.get(task_id)
        if job is None:
            job = {
                "id": task_id, "workspace": None, "status": task["status"],
                "backend": "device", "device_id": task.get("device_id"),
                "cmd_preview": task.get("label") or task["kind"],
                "lines": [], "exit_code": None,
                "started": int(task.get("created") or time.time()), "ended": None,
            }
            jobs[task_id] = job
        job["status"] = task["status"]
        job["lines"] = list(task["lines"])
        job["exit_code"] = task.get("exit_code")
        if task.get("ended"):
            job["ended"] = int(task["ended"])


JOB_SINK = None
"""(jobs_dict, lock) injected by server.py at startup. NEVER `import server`
here: in production server.py runs as __main__, so a fresh import would
create a SECOND module object with a separate AGENT_JOBS dict and the
transcript mirror would vanish (the job-404 toasts)."""


def set_job_sink(jobs, lock):
    global JOB_SINK
    JOB_SINK = (jobs, lock)


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
            # New-LaptopVm.ps1 exits 1 when any VM failed to create (and prints
            # the failing VM + cua output); echo a clear final line either way.
            "$code = $LASTEXITCODE",
            "if ($code -eq 0) {",
            "    Write-Output '[OK] VMs created on the laptop'",
            "} else {",
            "    Write-Output '[FAILED] see the lines above — run the create on the laptop to see the full error'",
            "}",
            "exit $code",
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
            # idempotent: stop any previous beacon before starting a new one",
            "Get-CimInstance Win32_Process -Filter \"Name='powershell.exe'\" |",
            "    Where-Object { $_.CommandLine -like '*Register-Nodes.ps1*' } |",
            "    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }",
            "Start-Process powershell -ArgumentList '-NoProfile','-WindowStyle','Hidden',"
            "'-ExecutionPolicy','Bypass','-File',$script,'-MasterUrl',"
            f"{_ps(master)},'-Token',{_ps(token)} -WindowStyle Hidden",
            "Write-Output 'beacon started (hidden window, re-registers VMs every 30 s)'",
            "",
        ])
    if kind == "open-desktop":
        vm = str(ctx.get("vm_name") or "")
        return "\n".join([
            "$ErrorActionPreference = 'Stop'",
            f"$name = {_ps(vm)}",
            "$exe = (Get-Command cua -ErrorAction SilentlyContinue).Source",
            "if (-not $exe) { Write-Output '[error] cua CLI not found on this device'; exit 1 }",
            "Start-Process -FilePath $exe -ArgumentList 'sb','view',$name | Out-Null",
            'Write-Output "desktop viewer started on THIS machine\'s browser (the viewer runs where cua runs)"',
            "exit 0",
            "",
        ])
    if kind == "install-agent":
        # Runs ON THE LAPTOP: the agentd sidecar is pushed INTO one of its
        # VMs so the VM becomes a full agent node. The KIMI_API_KEY comes
        # device-bound in ctx, is staged to a one-line ASCII temp file,
        # ships with `cua sb cp` into a chmod-600 guest .env and is NEVER
        # printed. The health check runs INSIDE the guest (the embedded
        # listing shows empty endpoints, so nothing host-side is parsed),
        # matched with plain .Contains — no regex escapes. The guest start
        # script is built as a here-string and written LF-only (CRLF in a
        # copied sh script crashed the guest with ': not found').
        vm = str(ctx.get("vm_name") or "")
        key = str(ctx.get("kimi_key") or "")
        return "\n".join([
            "$ErrorActionPreference = 'Stop'",
            tmp,
            f"$name = {_ps(vm)}",
            "$tgz = Join-Path $tmp 'agentd.tgz'",
            f"Invoke-RestMethod -Uri {_ps(f'{master}/kit/agentd.tgz')} -OutFile $tgz",
            "$exe = (Get-Command cua -ErrorAction SilentlyContinue).Source",
            "if (-not $exe) { Write-Output '[error] cua CLI not found on this device'; exit 1 }",
            # 1. ship the kit into the guest and unpack it
            "Write-Output \"step: copy agentd.tgz into ${name}\"",
            "cua sb cp $tgz \"${name}:/home/cua/agentd.tgz\"",
            "if ($LASTEXITCODE -ne 0) { Write-Output '[error] cua sb cp agentd.tgz failed'; exit 1 }",
            "cua sb exec $name -- 'mkdir -p /home/cua/agentd && tar xzf /home/cua/agentd.tgz -C /home/cua'",
            "if ($LASTEXITCODE -ne 0) { Write-Output '[error] unpack failed'; exit 1 }",
            # 2. the API key: staged locally, copied in, chmod 600 — the value
            #    only ever exists in the temp file and the guest's .env
            "Write-Output \"step: install the API key (chmod 600, never printed)\"",
            "$keyFile = Join-Path $tmp 'agentd.env'",
            f"Set-Content -Path $keyFile -Value {_ps('KIMI_API_KEY=' + key)} -Encoding ascii",
            "cua sb cp $keyFile \"${name}:/home/cua/agentd/.env\"",
            "if ($LASTEXITCODE -ne 0) { Write-Output '[error] key copy failed'; exit 1 }",
            "Remove-Item $keyFile -Force -ErrorAction SilentlyContinue",
            "cua sb exec $name -- 'chmod 600 /home/cua/agentd/.env'",
            "if ($LASTEXITCODE -ne 0) { Write-Output '[error] chmod failed'; exit 1 }",
            # 3. dependencies (full npm ci — tsx is a devDependency)
            "Write-Output \"step: npm ci in the VM\"",
            "cua sb exec $name -- 'cd /home/cua/agentd && npm ci --no-audit --no-fund'",
            "if ($LASTEXITCODE -ne 0) { Write-Output '[error] npm ci failed'; exit 1 }",
            # 4. start script (mirrors server.py's START_AGENTD_SH) + launch
            "Write-Output \"step: start the sidecar detached\"",
            "$startSh = Join-Path $tmp 'start-agentd.sh'",
            "$here = @'",
            "#!/bin/sh",
            "# re-runnable after a VM restart (processes die on restart, files survive)",
            "mkdir -p /home/cua/agentd-data",
            "export YTL_AGENT_PORT=8472",
            "export YTL_AGENT_HOST=0.0.0.0",
            "export YTL_AGENT_DB=/home/cua/agentd-data/harness.sqlite",
            "cd /home/cua/agentd",
            "setsid nohup npx tsx src/server.ts >>/home/cua/agentd-data/server.log 2>&1 </dev/null &",
            "'@",
            "[IO.File]::WriteAllText($startSh, ($here -replace \"`r`n\", \"`n\"))",
            "cua sb cp $startSh \"${name}:/home/cua/start-agentd.sh\"",
            "if ($LASTEXITCODE -ne 0) { Write-Output '[error] start-script copy failed'; exit 1 }",
            "cua sb exec $name -- 'sh /home/cua/start-agentd.sh'",
            "if ($LASTEXITCODE -ne 0) { Write-Output '[error] start failed'; exit 1 }",
            "Start-Sleep -Seconds 4",
            # 5. health check INSIDE the guest — no endpoints parsing
            "Write-Output \"step: health check inside the guest\"",
            "$health = cua sb exec $name -- 'curl -s -m 8 http://127.0.0.1:8472/health' 2>$null",
            "if ($health -and $health.Contains('ok') -and $health.Contains('true')) {",
            "    Write-Output \"agent installed on ${name} — healthy\"",
            "    exit 0",
            "}",
            "Write-Output \"[error] sidecar not healthy in ${name} — server.log tail:\"",
            "cua sb exec $name -- 'tail -40 /home/cua/agentd-data/server.log 2>/dev/null' 2>$null",
            "exit 1",
            "",
        ])
    if kind == "expose-agent":
        # Runs ON THE LAPTOP (elevated): makes the in-guest agentd sidecar
        # reachable from the MASTER via the master's reverse relay. cua on
        # Windows publishes no host port into QEMU guests and
        # `cua sb port-forward` cannot tunnel into them — but the guest CAN
        # dial out (slirp NAT), so the guest sidecar is pointed at the
        # master's relay dial port (18472+i) and the master's paired client
        # port (18473+i) speaks HTTP to the sidecar. The stable per-VM client
        # port lives in %TEMP%\ytl-beacon\endpoints.json (the map the beacon
        # reads to rewrite registered node endpoints). Verification is guest
        # process presence (pgrep), matched with plain .Contains — no regex
        # escapes. ASCII only (PS 5.1 fallback).
        vm = str(ctx.get("vm_name") or "")
        master_ip = str(ctx.get("master_lan_ip") or "127.0.0.1")
        return "\n".join([
            "$ErrorActionPreference = 'Stop'",
            f"$name = {_ps(vm)}",
            f"$masterIp = {_ps(master_ip)}",
            # 0. the task reconfigures the guest through cua: the CLI must be
            #    there and the device agent runs elevated (fail loudly)
            "$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)",
            "if (-not $isAdmin) {",
            "    Write-Output '[error] admin rights required - run the device agent elevated'",
            "    exit 1",
            "}",
            "$exe = (Get-Command cua -ErrorAction SilentlyContinue).Source",
            "if (-not $exe) { Write-Output '[error] cua CLI not found on this device'; exit 1 }",
            # 1. reserve a relay index: client port 18473+i on the master,
            #    dial port = client-1; index i is the first slot 0..9 neither
            #    present in the beacon map nor bound locally
            "Write-Output \"step: reserve a relay index\"",
            "$mapFile = Join-Path $env:TEMP 'ytl-beacon\\endpoints.json'",
            "$ports = @{}",
            "if (Test-Path $mapFile) {",
            "    try {",
            "        $raw = Get-Content $mapFile -Raw | ConvertFrom-Json",
            "        foreach ($prop in $raw.PSObject.Properties) { $ports[$prop.Name] = [int]$prop.Value }",
            "    } catch { }",
            "}",
            "$clientPort = 0",
            "if ($ports.ContainsKey($name)) { $clientPort = $ports[$name] }",
            "if ($clientPort -le 0) {",
            "    foreach ($i in 0..9) {",
            "        $candidate = 18473 + $i",
            "        if ($ports.Values -contains $candidate) { continue }",
            "        $probe = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Any, $candidate)",
            "        try {",
            "            $probe.Start()",
            "            $clientPort = $candidate",
            "            $probe.Stop()",
            "            break",
            "        } catch { }",
            "    }",
            "}",
            "if ($clientPort -le 0) { Write-Output '[error] no free relay client port (18473..18482)'; exit 1 }",
            "$dialPort = $clientPort - 1",
            "$ports[$name] = $clientPort",
            "New-Item -ItemType Directory -Force -Path (Split-Path $mapFile) | Out-Null",
            "($ports | ConvertTo-Json) | Set-Content -Path $mapFile -Encoding ascii",
            # 2. push the guest start script: the install-agent exports plus
            #    YTL_RELAY_OUT, so the sidecar dials OUT to the master relay
            #    (here-string written LF-only — CRLF crashed sh before)
            "Write-Output \"step: point the guest sidecar at the master relay\"",
            "$startSh = Join-Path $env:TEMP 'start-agentd.sh'",
            "$relayTarget = \"$masterIp`:$dialPort\"",
            "$here = @'",
            "#!/bin/sh",
            "mkdir -p /home/cua/agentd-data",
            "export YTL_AGENT_PORT=8472",
            "export YTL_AGENT_HOST=127.0.0.1",
            "export YTL_AGENT_DB=/home/cua/agentd-data/harness.sqlite",
            "export YTL_RELAY_OUT=__RELAY_TARGET__",
            "cd /home/cua/agentd",
            "setsid nohup npx tsx src/server.ts >>/home/cua/agentd-data/server.log 2>&1 </dev/null &",
            "'@",
            "$here = $here.Replace('__RELAY_TARGET__', $relayTarget)",
            "[IO.File]::WriteAllText($startSh, ($here -replace \"`r`n\", \"`n\"))",
            "cua sb cp $startSh \"${name}:/home/cua/start-agentd.sh\"",
            "if ($LASTEXITCODE -ne 0) { Write-Output '[error] start-script copy failed'; exit 1 }",
            # 3. restart the guest sidecar with the relay env (pkill may find
            #    nothing on a fresh VM — its exit code is ignored)
            "Write-Output \"step: restart the guest sidecar\"",
            "cua sb exec $name -- 'pkill -f \"[t]sx src/server.ts\"; sh /home/cua/start-agentd.sh'",
            "Start-Sleep -Seconds 4",
            # 4. verify the guest process runs — no endpoints parsing
            "Write-Output \"step: verify the guest process\"",
            "$pg = cua sb exec $name -- 'pgrep -f \"[t]sx src/server.ts\" || echo NO_PROCESS' 2>$null",
            "if ($pg -and -not $pg.Contains('NO_PROCESS')) {",
            "    Write-Output \"agent exposed at http://$masterIp`:$clientPort -- relay dial $masterIp`:$dialPort\"",
            "    exit 0",
            "}",
            "Write-Output \"[error] sidecar not running in ${name} - server.log tail:\"",
            "cua sb exec $name -- 'tail -20 /home/cua/agentd-data/server.log 2>/dev/null' 2>$null",
            "exit 1",
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
    if JOB_SINK:
        jobs, lock = JOB_SINK
        with lock:
            for jid, job in list(jobs.items()):
                if (job.get("backend") == "device"
                        and job.get("device_id") == device_id):
                    del jobs[jid]
    return None
