# Register-Nodes.ps1 — laptop-side beacon: registers this laptop's cua VMs
# with the master's Network tab.
#
# Runs `cua sb list --json` on an interval and POSTs every sandbox to the
# master's /api/nodes/register (token-gated like every /api/ route). The
# master merges fresh registrations into build_network(); entries that stop
# re-registering expire after 10 minutes there. New/deleted VMs need nothing
# extra — the next loop picks the list up as it is.
#
# Optional: when a sandbox publishes endpoints.agent (its in-guest agentd
# sidecar), the endpoint URL is passed through so the master can list that
# node's agent/sessions — but only if the master can actually reach it
# (a published guest port is reachable from the laptop, not necessarily from
# the master; reachability is your job: LAN IP / port-forward / relay).
# Without an endpoint the node is presence-only, which is enough for the
# Network tab.
#
# Plain window (runs until closed):
#   powershell -File .\Register-Nodes.ps1 -MasterUrl http://192.168.50.111:8471 -Token <your-LAN-token>
# One shot (for testing):
#   powershell -File .\Register-Nodes.ps1 -MasterUrl http://192.168.50.111:8471 -Token <t> -Once
# Background:
#   Start-Process powershell -ArgumentList '-File','.\Register-Nodes.ps1','-MasterUrl','http://...','-Token','<t>' -WindowStyle Hidden

[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$MasterUrl,
    [Parameter(Mandatory=$true)][string]$Token,
    [string]$HostLabel = $env:COMPUTERNAME,
    [int]$IntervalSeconds = 30,
    [switch]$Once
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Continue'

$LogFile = Join-Path ([Environment]::GetFolderPath("Desktop")) "ytl-beacon.log"

function Write-Log {
    <#
    .SYNOPSIS
      Append a timestamped line to the desktop log AND echo it to the
      console (the hidden beacon's only other output). Never log the token.
    #>
    param([string]$Message)
    $line = "{0}  {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
    Write-Host $line
}

Write-Log ("beacon starting: master={0} host={1} interval={2}s once={3}" -f
    $MasterUrl, $HostLabel, $IntervalSeconds, [bool]$Once)

$script:LastGoodNodes = $null
try {
    $stateFile = Join-Path $env:TEMP 'ytl-beacon\lastnodes.json'
    if (Test-Path $stateFile) {
        $loaded = Get-Content $stateFile -Raw | ConvertFrom-Json
        if ($loaded) {
            $script:LastGoodNodes = @($loaded)
            Write-Log ("loaded {0} last known node(s) from state" -f $script:LastGoodNodes.Count)
        }
    }
} catch {}

function Get-SandboxNodes {
    <#
    .SYNOPSIS
      cua sb list --json -> array of @{name; kind; state; endpoint?}.
      Any failure (cua missing, parse error) sets $script:CuaListOk = $false
      and outputs nothing : the loop logs it and retries without posting a
      bogus empty list. Note: a legitimately EMPTY sandbox list also outputs
      nothing (PowerShell unrolls empty arrays), so the caller must consult
      $script:CuaListOk, never $null -eq $nodes.
    #>
    try {
        # Embedded FIRST: while a QEMU VM runs, its runtime daemon owns the
        # per-user socket and the daemon-backed listing fails -- the embedded
        # runtime bypasses it. Fall back to the plain listing if embedded
        # exits non-zero or returns unparseable output.
        $raw = & cua sb list --json --embedded 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $raw) {
            $raw = & cua sb list --json 2>$null
        }
        if (-not $raw) { $script:CuaListOk = $false; $script:ListError = "no output (exit $LASTEXITCODE)"; return }
        $list = $raw | Out-String | ConvertFrom-Json
        if ($null -eq $list) { $script:CuaListOk = $false; $script:ListError = "unparseable output: $($raw | Out-String | Select-Object -First 1)".Trim(); return }
        if ($list -isnot [System.Array]) { $list = @($list) }
        $nodes = @()
        foreach ($sb in $list) {
            if (-not $sb.name) { continue }
            $node = @{
                name  = [string]$sb.name
                kind  = if ($sb.kind) { [string]$sb.kind } else { 'vm' }
                state = if ($sb.status) { [string]$sb.status } else { '' }
            }
            $agent = $null
            if ($sb.endpoints -and $sb.endpoints.PSObject.Properties['agent']) {
                $agent = $sb.endpoints.agent
            }
            if ($agent -and ([string]$agent).StartsWith('http')) {
                $node.endpoint = [string]$agent
            }
            $nodes += $node
        }
        $script:CuaListOk = $true
        $script:LastGoodNodes = $nodes
        # persist so a RESTARTED beacon (started while a VM runs and the list
        # already fails) still knows the last nodes
        try {
            $dir = Join-Path $env:TEMP 'ytl-beacon'
            New-Item -ItemType Directory -Force $dir | Out-Null
            $script:LastGoodNodes | ConvertTo-Json -Depth 4 |
                Set-Content -Path (Join-Path $dir 'lastnodes.json') -Encoding UTF8
        } catch {}
        return $nodes
    } catch {
        $script:CuaListOk = $false
        $script:ListError = $_.Exception.Message
        return
    }
}

function Send-Registration {
    <#
    .SYNOPSIS
      POST {host, nodes} to the master. Errors are swallowed on purpose: a
      master that is offline must not kill the beacon — it keeps trying.
    #>
    param([array]$Nodes)
    try {
        $body = @{ host = $HostLabel; nodes = $Nodes } | ConvertTo-Json -Depth 4
        Invoke-RestMethod -Method Post -Uri "$MasterUrl/api/nodes/register" `
            -Headers @{ 'x-ytl-token' = $Token } `
            -ContentType 'application/json' -Body $body -TimeoutSec 10 | Out-Null
        Write-Log ("registered {0} node(s) with {1}" -f $Nodes.Count, $MasterUrl)
    } catch {
        $httpStatus = $null
        $resp = $_.Exception.Response
        if ($resp -and $resp.StatusCode) {
            $httpStatus = "HTTP {0}" -f [int]$resp.StatusCode
        }
        if ($httpStatus) {
            Write-Log ("master unreachable ({0}) - will retry" -f $httpStatus)
        } else {
            Write-Log ("master unreachable ({0}) - will retry" -f $_.Exception.Message)
        }
    }
}

$script:CuaListOk = $true
while ($true) {
    $script:CuaListOk = $true
    $nodes = @(Get-SandboxNodes)
    if (-not $script:CuaListOk) {
        # While a QEMU VM runs, `cua sb list` can fail (daemon busy) — keep
        # registering the last known nodes so the master keeps the laptop's
        # VMs visible instead of letting them vanish.
        if ($script:LastGoodNodes) {
            Write-Log ("cua list failed — re-registering {0} last known node(s)" -f $script:LastGoodNodes.Count)
            Send-Registration -Nodes $script:LastGoodNodes
        } else {
            Write-Log ("cua list failed: " + $script:ListError)
        }
    } else {
        Send-Registration -Nodes $nodes
    }
    if ($Once) { break }
    Start-Sleep -Seconds $IntervalSeconds
}

Write-Log "beacon exited"
