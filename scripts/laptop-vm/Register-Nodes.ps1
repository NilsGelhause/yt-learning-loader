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

function Get-SandboxNodes {
    <#
    .SYNOPSIS
      cua sb list --json -> array of @{name; kind; state; endpoint?}.
      Any failure (cua missing, parse error) -> @() : the loop just retries.
    #>
    try {
        $raw = & cua sb list --json 2>$null
        if (-not $raw) { return @() }
        $list = $raw | Out-String | ConvertFrom-Json
        if ($list -isnot [System.Array]) { $list = @($list) }
        $nodes = @()
        foreach ($sb in $list) {
            if (-not $sb.name) { continue }
            $node = @{
                name  = [string]$sb.name
                kind  = if ($sb.kind) { [string]$sb.kind } else { 'vm' }
                state = if ($sb.status) { [string]$sb.status } else { '' }
            }
            $agent = $sb.endpoints.agent
            if ($agent -and ([string]$agent).StartsWith('http')) {
                $node.endpoint = [string]$agent
            }
            $nodes += $node
        }
        return $nodes
    } catch {
        return @()
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
        Write-Host ("{0}  registered {1} node(s) with {2}" -f
            (Get-Date -Format 'HH:mm:ss'), $Nodes.Count, $MasterUrl)
    } catch {
        Write-Host ("{0}  master unreachable - will retry" -f (Get-Date -Format 'HH:mm:ss'))
    }
}

while ($true) {
    $nodes = Get-SandboxNodes
    Send-Registration -Nodes $nodes
    if ($Once) { break }
    Start-Sleep -Seconds $IntervalSeconds
}
