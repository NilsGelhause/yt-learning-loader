# Device-Agent.ps1 — pairing-based reverse agent ("Connect device").
#
# Runs ON THE LAPTOP (PowerShell 5.1+). The master app shows a pairing code;
# the laptop runs ONE line:
#
#   irm http://<master>:8471/device-agent.ps1 -OutFile agent.ps1; .\agent.ps1 -MasterUrl http://<master>:8471 -Code <CODE> [-Token <LAN token>]
#
# The agent pairs, then polls the master every few seconds for pre-defined
# tasks (install-stack / create-vm / start-beacon) and executes them LOCALLY.
#
# Robustness design (learned the hard way):
#   * Tasks run as DETACHED child processes (Start-Process, no .NET event
#     handlers, no WaitForExit, no jobs). The agent cannot be killed by task
#     machinery, and closing the agent window no longer kills a running task.
#   * Child output is redirected to log FILES by cmd (also stdin < NUL, so
#     no interactive prompt can ever hang). The poll loop forwards new lines.
#   * install-stack re-runs itself elevated via UAC (one click), same pattern.
#
# If a task line carries [REBOOT-REQUIRED], reboot the laptop, then re-run
# the one line above.
#
# Diagnosis: every step is appended to ytl-agent.log on the Desktop.

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$MasterUrl,
    [Parameter(Mandatory = $true)][string]$Code,
    [int]$IntervalSeconds = 5,
    [string]$Token = ""   # master LAN token; prompted for when the master asks
)

$ErrorActionPreference = 'Stop'
$MasterUrl = $MasterUrl.TrimEnd('/')
$LanToken = $Token

$AgentLog = Join-Path ([Environment]::GetFolderPath("Desktop")) "ytl-agent.log"
function Trace([string]$msg) {
    try {
        Add-Content -Path $AgentLog -Value ("{0}  {1}" -f (Get-Date -Format "HH:mm:ss"), $msg) -Encoding UTF8
    } catch {}
}

# Last-resort crash net: exceptions on background threads bypass the trap.
[AppDomain]::CurrentDomain.add_UnhandledException({
    try {
        Add-Content -Path $AgentLog -Value ("{0}  UNHANDLED: {1}" -f (Get-Date -Format "HH:mm:ss"), $_.ExceptionObject) -Encoding UTF8
    } catch {}
})

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    return (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Send-Output([string]$taskId, [string]$line) {
    try {
        Invoke-RestMethod -Method Post -Uri "$MasterUrl/api/devices/$DeviceId/output" `
            -Headers @{ 'x-ytl-token' = $LanToken } `
            -Body (@{ token = $DeviceToken; task_id = $taskId; line = $line } | ConvertTo-Json) `
            -ContentType 'application/json' | Out-Null
    } catch {
        Trace ("report failed: " + $_.Exception.Message)
    }
}

function Finish-Task([string]$taskId, [int]$exitCode) {
    try {
        Invoke-RestMethod -Method Post -Uri "$MasterUrl/api/devices/$DeviceId/output" `
            -Headers @{ 'x-ytl-token' = $LanToken } `
            -Body (@{ token = $DeviceToken; task_id = $taskId; done = $true; exit_code = $exitCode } | ConvertTo-Json) `
            -ContentType 'application/json' | Out-Null
    } catch {
        Trace ("finish report failed: " + $_.Exception.Message)
    }
}

# ---------------------------------------------------------------- tasks
# A running task = a detached child process; cmd redirects its output to
# <base>.out / <base>.err and the launcher writes the exit code to
# <base>.done when it finishes. The poll loop streams the logs and reports
# completion. One task at a time.

$Streaming = @{}   # taskId -> @{ out, err, done, sent }

function Task-Paths([string]$taskId) {
    $base = Join-Path $env:TEMP ("ytl-task-" + $taskId)
    return @{ out = "$base.out"; err = "$base.err"; done = "$base.done";
              script = "$base.ps1"; launcher = "$base.run.ps1" }
}

function Start-Task($task) {
    Write-Host "Running task $($task.id) locally — progress appears here and in the master."
    $p = Task-Paths $task.id
    Set-Content -Path $p.script -Value $task.script -Encoding UTF8
    # launcher: run the task, then ALWAYS drop the exit code in the done file
    Set-Content -Path $p.launcher -Encoding UTF8 -Value @"
`$global:LASTEXITCODE = 0
& '$($p.script)'
[System.IO.File]::WriteAllText('$($p.done)', [string]`$global:LASTEXITCODE)
"@
    Remove-Item $p.out, $p.err, $p.done -Force -ErrorAction SilentlyContinue

    # cmd line: powershell runs the launcher, streams go to files, stdin EOF.
    $inner = 'powershell -NoProfile -ExecutionPolicy Bypass -File "' + $p.launcher + '"'
    $cmdArgs = '/d /c ' + $inner + ' > "' + $p.out + '" 2> "' + $p.err + '" < NUL'

    if ($task.kind -eq 'install-stack' -and -not (Test-Admin)) {
        Write-Host "Task $($task.id) needs admin — accept the UAC prompt to continue."
        Trace ("task " + $task.id + " starting elevated (UAC)")
        try {
            Start-Process cmd.exe -Verb RunAs -Wait -ArgumentList $cmdArgs -WindowStyle Hidden
        } catch {
            Trace ("UAC failed: " + $_.Exception.Message)
            Send-Output $task.id "[error] elevation failed: $($_.Exception.Message)"
            Finish-Task $task.id 1
            return
        }
        # The elevated child ran synchronously; the done file is already
        # (or very soon) there — the streamer finishes the task.
    } else {
        Trace ("task " + $task.id + " starting detached child")
        Start-Process cmd.exe -ArgumentList $cmdArgs -WindowStyle Hidden | Out-Null
    }
    $Streaming[$task.id] = @{ out = $p.out; err = $p.err; done = $p.done; sent = 0 }
}

function Stream-Tasks {
    foreach ($taskId in @($Streaming.Keys)) {
        $st = $Streaming[$taskId]
        foreach ($f in @($st.out, $st.err)) {
            if (Test-Path $f) {
                $lines = @(Get-Content $f -ErrorAction SilentlyContinue)
                if ($lines.Count -gt $st.sent) {
                    $pending = $lines.Count - $st.sent
                    $show = $pending
                    if ($pending -gt 200) { $show = 200 }
                    for ($i = $st.sent; $i -lt $st.sent + $show; $i++) {
                        Send-Output $taskId ([string]$lines[$i])
                        Write-Host ("  " + $lines[$i])
                    }
                    if ($pending -gt $show) {
                        Write-Host ("  … (" + ($pending - $show) + " more lines)")
                    }
                    $st.sent = $lines.Count
                }
            }
        }
        if (Test-Path $st.done) {
            $code = 1
            try { $code = [int]((Get-Content $st.done -Raw).Trim()) } catch {}
            Finish-Task $taskId $code
            Write-Host ("Task " + $taskId + " finished (exit " + $code + ")")
            if ($code -ne 0) {
                Write-Host "  check the master's transcript for details." -ForegroundColor Yellow
            }
            Trace ("task " + $taskId + " finished, exit " + $code)
            $Streaming.Remove($taskId)
        }
    }
}

# ---- pair (a few friendly retries, then give up) ----

$hostname = $env:COMPUTERNAME
$pairBody = @{ code = $Code; hostname = $hostname } | ConvertTo-Json
$DeviceId = $null
$DeviceToken = $null
Trace ("agent starting, pid " + $PID + " admin=" + (Test-Admin))
for ($attempt = 1; $attempt -le 4 -and -not $DeviceId; $attempt++) {
    try {
        $headers = @{}
        if ($LanToken) { $headers['x-ytl-token'] = $LanToken }
        $res = Invoke-RestMethod -Method Post -Uri "$MasterUrl/api/devices/pair" `
            -Body $pairBody -ContentType 'application/json' -Headers $headers
        $DeviceId = $res.device_id
        $DeviceToken = $res.token
    } catch {
        $status = $null
        if ($_.ErrorDetails -and $_.ErrorDetails.Message) {
            try { $status = ($_.ErrorDetails.Message | ConvertFrom-Json).error } catch {}
        }
        if ($status -eq 'token_required' -and -not $LanToken) {
            $LanToken = Read-Host "This master requires its LAN token - enter it now"
        } elseif ($status -eq 'token_required') {
            Write-Host "The master rejected the LAN token." -ForegroundColor Red
            exit 1
        } elseif ($_.Exception.Response -and $_.Exception.Response.StatusCode.value__ -eq 400) {
            Write-Host "Pairing code $Code was rejected (wrong or expired)." -ForegroundColor Red
            Write-Host "Press Connect device again on the master's CUA tab for a fresh code."
            exit 1
        } else {
            Write-Host "Master not reachable at $MasterUrl ($($_.Exception.Message)) — retrying in 5 s ($attempt/4)..."
            Start-Sleep -Seconds 5
        }
    }
}
if (-not $DeviceId) {
    Write-Host "Giving up — check the master app is running and MasterUrl is right." -ForegroundColor Red
    Trace "pairing gave up"
    exit 1
}

Write-Host "Paired as $hostname (device $DeviceId). Polling every $IntervalSeconds s — Ctrl+C to exit."
if (-not (Test-Admin)) {
    Write-Host "Not running as admin: install-stack tasks will show one UAC prompt each."
}

# ---- poll loop ----
trap {
    Trace ("TRAP: " + $_.Exception.Message + " | " + $_.ScriptStackTrace)
    Write-Host "AGENT ERROR: $($_.Exception.Message) — see $AgentLog"
    continue
}

while ($true) {
    try {
        Stream-Tasks
        if ($Streaming.Count -eq 0) {
            $res = Invoke-RestMethod -Uri "$MasterUrl/api/devices/$DeviceId/next?token=$DeviceToken" `
                -Headers @{ 'x-ytl-token' = $LanToken }
            if ($res.task) {
                Write-Host "Task $($res.task.id) [$($res.task.kind)] received."
                Trace ("task " + $res.task.id + " [" + $res.task.kind + "] received")
                Start-Task $res.task
            }
        }
    } catch {
        Write-Host "Poll failed: $($_.Exception.Message)"
        Trace ("poll failed: " + $_.Exception.Message)
    }
    Start-Sleep -Seconds $IntervalSeconds
}
