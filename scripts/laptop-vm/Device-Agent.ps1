# Device-Agent.ps1 — pairing-based reverse agent ("Connect device").
#
# Runs ON THE LAPTOP (PowerShell 5.1+). The master app shows a pairing code;
# the laptop runs ONE line:
#
#   irm http://<master>:8471/device-agent.ps1 -OutFile agent.ps1; .\agent.ps1 -MasterUrl http://<master>:8471 -Code <CODE>
#
# The agent pairs, then polls the master every few seconds for pre-defined
# tasks (install-stack / create-vm / start-beacon) and executes them LOCALLY,
# streaming each output line back to the master. install-stack needs admin
# (it enables the Windows hypervisor feature): the agent re-runs just that
# task elevated via Start-Process -Verb RunAs — one UAC click on the laptop —
# and tails the child's log file. Ctrl+C exits.
#
# If a task line carries [REBOOT-REQUIRED], reboot the laptop (the hypervisor
# feature needs it), then re-run the one line above.

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

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    return (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Send-Output([string]$taskId, [string]$line) {
    # Never throw from here: this runs inside .NET event handlers, and an
    # unhandled exception on those threads kills the whole agent process
    # (the PowerShell window just vanishes, e.g. while the master restarts).
    try {
        Invoke-RestMethod -Method Post -Uri "$MasterUrl/api/devices/$DeviceId/output" `
            -Headers @{ 'x-ytl-token' = $LanToken } `
            -Body (@{ token = $DeviceToken; task_id = $taskId; line = $line } | ConvertTo-Json) `
            -ContentType 'application/json' | Out-Null
    } catch {
        Write-Host "report failed (master unreachable?): $($_.Exception.Message)"
    }
}

function Finish-Task([string]$taskId, [int]$exitCode) {
    try {
        Invoke-RestMethod -Method Post -Uri "$MasterUrl/api/devices/$DeviceId/output" `
            -Headers @{ 'x-ytl-token' = $LanToken } `
            -Body (@{ token = $DeviceToken; task_id = $taskId; done = $true; exit_code = $exitCode } | ConvertTo-Json) `
            -ContentType 'application/json' | Out-Null
    } catch {
        Write-Host "finish report failed (master unreachable?): $($_.Exception.Message)"
    }
}

function Invoke-Script([string]$path, [string]$taskId, [string]$logFile) {
    # Run a task script with stdin closed: any interactive prompt gets EOF and
    # fails fast instead of hanging the agent forever (the cua installer used
    # to wait on "Type a number..." and starve the task).
    $script:TaskId = $taskId
    $script:TaskLog = $logFile
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = "powershell.exe"
    $psi.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$path`""
    $psi.RedirectStandardInput = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $p = [System.Diagnostics.Process]::Start($psi)
    if ($logFile) {
        $p.add_OutputDataReceived({ if ($null -ne $_.Data) { Add-Content -Path $script:TaskLog -Value $_.Data -Encoding UTF8 } })
        $p.add_ErrorDataReceived({ if ($null -ne $_.Data) { Add-Content -Path $script:TaskLog -Value $_.Data -Encoding UTF8 } })
    } else {
        $p.add_OutputDataReceived({ if ($null -ne $_.Data) { Send-Output $script:TaskId ([string]$_.Data) } })
        $p.add_ErrorDataReceived({ if ($null -ne $_.Data) { Send-Output $script:TaskId ([string]$_.Data) } })
    }
    $null = $p.Start()
    $p.StandardInput.Close()   # EOF: no prompt may wait for input
    $p.BeginOutputReadLine()
    $p.BeginErrorReadLine()
    $p.WaitForExit()
    return $p.ExitCode
}

$AgentLog = Join-Path ([Environment]::GetFolderPath("Desktop")) "ytl-agent.log"
function Trace([string]$msg) {
    Add-Content -Path $AgentLog -Value ("{0}  {1}" -f (Get-Date -Format "HH:mm:ss"), $msg) -Encoding UTF8
}

# Last-resort crash net: exceptions on background threads (event handlers)
# bypass the trap below and kill the process — this hook still sees them.
[AppDomain]::CurrentDomain.add_UnhandledException({
    try {
        Add-Content -Path $AgentLog -Value ("{0}  UNHANDLED: {1}" -f (Get-Date -Format "HH:mm:ss"), $_.ExceptionObject) -Encoding UTF8
    } catch {}
})
Trace ("agent starting, pid " + $PID + " admin=" + (Test-Admin))

function Invoke-Task($task) {
    Trace ("task " + $task.id + " [" + $task.kind + "] starting")
    $tmp = Join-Path $env:TEMP ("ytl-task-" + $task.id + ".ps1")
    Set-Content -Path $tmp -Value $task.script -Encoding UTF8
    Trace "task script written"
    if ($task.kind -eq 'install-stack' -and -not (Test-Admin)) {
        # Elevation v1: re-run just this task elevated and stream the child's
        # log back — the standard Start-Process -Verb RunAs + log-tail pattern.
        $log = Join-Path $env:TEMP ("ytl-task-" + $task.id + ".log")
        $codeFile = Join-Path $env:TEMP ("ytl-task-" + $task.id + ".exit")
        Remove-Item $log, $codeFile -Force -ErrorAction SilentlyContinue
        $wrapper = Join-Path $env:TEMP ("ytl-elevated-" + $task.id + ".ps1")
        Set-Content -Path $wrapper -Encoding UTF8 -Value @"
`$psi = New-Object System.Diagnostics.ProcessStartInfo
`$psi.FileName = 'powershell.exe'
`$psi.Arguments = '-NoProfile -ExecutionPolicy Bypass -File "$tmp"'
`$psi.RedirectStandardInput = `$true
`$psi.RedirectStandardOutput = `$true
`$psi.RedirectStandardError = `$true
`$psi.UseShellExecute = `$false
`$psi.CreateNoWindow = `$true
`$p = [System.Diagnostics.Process]::Start(`$psi)
`$p.add_OutputDataReceived({ if (`$null -ne `$_.Data) { Add-Content -Path '$log' -Value `$_.Data -Encoding UTF8 } })
`$p.add_ErrorDataReceived({ if (`$null -ne `$_.Data) { Add-Content -Path '$log' -Value `$_.Data -Encoding UTF8 } })
`$null = `$p.Start()
`$p.StandardInput.Close()
`$p.BeginOutputReadLine()
`$p.BeginErrorReadLine()
`$p.WaitForExit()
[System.IO.File]::WriteAllText('$codeFile', [string]`$p.ExitCode)
"@
        Write-Host "Task $($task.id) needs admin — accept the UAC prompt to continue."
        Trace "requesting elevation (UAC)"
        try {
            Start-Process powershell -Verb RunAs -Wait -ArgumentList `
                '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $wrapper
        } catch {
            Trace ("UAC failed: " + $_.Exception.Message)
            Send-Output $task.id "[error] elevation failed: $($_.Exception.Message)"
            Finish-Task $task.id 1
            return
        }
        Trace "elevated child returned"
        $exitCode = 1
        if (Test-Path $codeFile) { $exitCode = [int](Get-Content $codeFile -Raw).Trim() }
        $sent = 0
        if (Test-Path $log) {
            $lines = @(Get-Content $log -ErrorAction SilentlyContinue)
            if ($sent -lt $lines.Count) {
                $lines[$sent..($lines.Count - 1)] | ForEach-Object { Send-Output $task.id ([string]$_); $sent++ }
            }
        }
        Finish-Task $task.id $exitCode
        return
    }
    # Unprivileged tasks (and already-elevated agents) stream line by line.
    Trace "running task in-process"
    $exitCode = Invoke-Script -path $tmp -taskId $task.id -logFile $null
    Trace ("task exited with " + $exitCode)
    Finish-Task $task.id $exitCode
}

# ---- pair (a few friendly retries, then give up) ----

$hostname = $env:COMPUTERNAME
$pairBody = @{ code = $Code; hostname = $hostname } | ConvertTo-Json
$DeviceId = $null
$DeviceToken = $null
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
        if ($_.Exception.Response) { $status = [int]$_.Exception.Response.StatusCode }
        if ($status -eq 401 -and -not $LanToken) {
            $LanToken = Read-Host "This master requires its LAN token — enter it now"
        } elseif ($status -eq 401) {
            Write-Host "The master rejected the LAN token." -ForegroundColor Red
            exit 1
        } elseif ($status -eq 400) {
            Write-Host "Pairing code $Code was rejected (wrong or expired)." -ForegroundColor Red
            Write-Host "Press Connect device again on the master's CUA tab for a fresh code."
            exit 1
        } else {
            Write-Host "Master not reachable at $MasterUrl ($($_.Exception.Message)) — retrying in 5 s ($attempt/4)…"
            Start-Sleep -Seconds 5
        }
    }
}
if (-not $DeviceId) {
    Write-Host "Giving up — check the master app is running and MasterUrl is right." -ForegroundColor Red
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
        $res = Invoke-RestMethod -Uri "$MasterUrl/api/devices/$DeviceId/next?token=$DeviceToken" `
            -Headers @{ 'x-ytl-token' = $LanToken }
        if ($res.task) {
            Write-Host "Task $($res.task.id) [$($res.task.kind)] received."
            Invoke-Task $res.task
            Write-Host "Task $($res.task.id) finished."
            Trace ("task " + $res.task.id + " finished")
        }
    } catch {
        Write-Host "Poll failed: $($_.Exception.Message)"
    }
    Start-Sleep -Seconds $IntervalSeconds
}
