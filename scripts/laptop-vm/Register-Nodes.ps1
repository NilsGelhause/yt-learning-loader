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
# node's agent/sessions. A QEMU guest port is not reachable from the master
# directly — so when the expose-agent device task has mapped this sandbox in
# %TEMP%\ytl-beacon\endpoints.json ({name: clientPort}), the registered URL
# is rewritten to http://<master-host>:<clientPort> where <master-host> is
# derived from this script's own -MasterUrl parameter: the guest sidecar
# dials OUT to the master's reverse relay (slirp NAT allows outbound), and
# the relay's paired client port speaks HTTP to the sidecar. Sandboxes
# without a map entry keep the reported URL (often loopback) — presence is
# still registered, reachability is just the master's problem then.
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
$script:MasterHost = $null
$script:PortMap = @{}
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

function Get-MasterHost {
    <#
    .SYNOPSIS
      Host part of -MasterUrl (scheme and port stripped) — the address the
      master's reverse relay listens on. Pure string ops, no regex: find the
      '//', take the substring after it, then cut at the first ':' or '/'.
    #>
    $u = [string]$MasterUrl
    $i = $u.IndexOf('//')
    if ($i -ge 0) { $u = $u.Substring($i + 2) }
    $end = $u.Length
    $ci = $u.IndexOf(':')
    $si = $u.IndexOf('/')
    if ($ci -ge 0 -and $ci -lt $end) { $end = $ci }
    if ($si -ge 0 -and $si -lt $end) { $end = $si }
    if ($end -le 0) { return $null }
    return $u.Substring(0, $end)
}

function Get-EndpointPortMap {
    <#
    .SYNOPSIS
      Reads %TEMP%\ytl-beacon\endpoints.json ({sandboxName: lanPort}) which
      the expose-agent device task writes/maintains. Returns a hashtable;
      missing or broken file yields an empty map (endpoints stay as reported).
    #>
    $map = @{}
    try {
        $mapFile = Join-Path $env:TEMP 'ytl-beacon\endpoints.json'
        if (Test-Path $mapFile) {
            $raw = Get-Content $mapFile -Raw -ErrorAction SilentlyContinue | ConvertFrom-Json
            if ($raw) {
                foreach ($prop in $raw.PSObject.Properties) {
                    $map[$prop.Name] = [int]$prop.Value
                }
            }
        }
    } catch {}
    return $map
}

function Get-SandboxNodes {
    <#
    .SYNOPSIS
      cua sb list --json -> array of @{name; kind; state; endpoint?}.
      Any failure (cua missing, parse error) sets $script:CuaListOk = $false
      and outputs nothing : the loop logs it and retries without posting a
      bogus empty list. Note: a legitimately EMPTY sandbox list also outputs
      nothing (PowerShell unrolls empty arrays), so the caller must consult
      $script:CuaListOk, never $null -eq $nodes.
      When endpoints.agent is present the URL is rewritten to the master's
      relay client address when (and only when) the expose-agent task mapped
      this sandbox in endpoints.json; a per-cycle cache keeps the map +
      master host to one lookup each. No map entry -> the reported URL is
      kept as-is.
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
        # one lookup per cycle, shared by every sandbox below
        $script:MasterHost = Get-MasterHost
        $script:PortMap = Get-EndpointPortMap
        $rewrites = 0
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
                $endpoint = [string]$agent
                if ($script:MasterHost -and $script:PortMap.ContainsKey($node.name)) {
                    $endpoint = "http://{0}:{1}" -f $script:MasterHost, $script:PortMap[$node.name]
                    $rewrites++
                }
                $node.endpoint = $endpoint
            }
            $nodes += $node
        }
        if ($rewrites -gt 0) {
            Write-Log ("rewrote {0} agent endpoint(s) to the master relay address" -f $rewrites)
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
