<#
.SYNOPSIS
  Creates N GUI Ubuntu QEMU VMs on this laptop, ready to join the master as clients.

.DESCRIPTION
  Creates up to 5 desktop Ubuntu VMs (ghcr.io/trycua/linux:24.04) via cua.
  Each VM boots to a graphical desktop. Run Install-VmStack.ps1 first.

  After creation, per VM you get:
    - view/record: the Cua Spaces app, or `cua sb port-forward <name> 5900:<p>`
      and point a VNC viewer at 127.0.0.1:<p> (what you record later).
    - join the master: inside the VM's browser open -MasterUrl and enter the
      LAN token. From laptop-hosted VMs the master's LAN IP is the target
      (10.0.2.2 would be the laptop itself).

  Exit code: 0 when every requested VM exists afterwards (created or already
  present), 1 when cua is missing or any creation failed. The device agent
  relies on this exit code to decide whether to auto-chain the beacon task.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\New-LaptopVm.ps1 -MasterUrl http://192.168.50.111:8471 -Count 3
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$MasterUrl,
    [ValidateRange(1, 5)][int]$Count = 1,
    [string]$Name = "laptop-vm"
)

$ErrorActionPreference = "Stop"
# The install task may have added QEMU to the Machine PATH after this shell
# was started — running processes never see that, so prepend it if present.
foreach ($qemuDir in @("C:\Program Files\qemu", "C:\Program Files (x86)\qemu")) {
    if ((Test-Path (Join-Path $qemuDir "qemu-system-x86_64.exe")) -and
        ($env:Path -notlike "*$qemuDir*")) {
        $env:Path = "$qemuDir;" + $env:Path
    }
}

Write-Host "Planning $Count VM(s) named $Name-1 .. $Name-$Count" -ForegroundColor Cyan
Write-Host "Master (open inside each VM's browser): $MasterUrl"
Write-Host "Check the laptop can handle it: each VM defaults to 4 GB RAM / 2-4 CPUs.`n"

if (-not (Get-Command cua -ErrorAction SilentlyContinue)) {
    Write-Host "ERROR: the 'cua' CLI is not on PATH on this laptop." -ForegroundColor Red
    Write-Host "Run Install-VmStack.ps1 here first (it installs cua and the QEMU runtime)."
    exit 1
}

$failed = @()

for ($i = 1; $i -le $Count; $i++) {
    $vm = "{0}-{1}" -f $Name, $i
    $exists = & cua sb list --json 2>$null | ConvertFrom-Json | Where-Object { $_.name -eq $vm }
    if ($exists) {
        $state = [string]$exists.state
        if ($state -eq "running") {
            Write-Host "[$vm] already exists and is running — skipping." -ForegroundColor Yellow
            continue
        }
        # A stopped/suspended QEMU VM cannot be resumed (its disk would be
        # reused but the sandbox stays unusable) — replace it with a fresh one.
        Write-Host "[$vm] exists but is '$state' — removing and recreating..." -ForegroundColor Yellow
        & cua sb rm $vm --force *>&1 | ForEach-Object { Write-Host "  $_" }
        if ($LASTEXITCODE -ne 0) {
            Write-Host "[$vm] could not remove the stale VM — remove it by hand: cua sb rm $vm --force" -ForegroundColor Red
            $failed += $vm
            continue
        }
    }
    Write-Host "[$vm] creating (first run downloads the image, can take minutes) ..."
    $log = Join-Path $env:TEMP ("cua-create-{0}.log" -f $vm)
    & cua sb create ubuntu --kind vm --runtime qemu --name $vm --service agent=8472 --json *>&1 | Tee-Object -FilePath $log
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[$vm] creation FAILED — full cua output:" -ForegroundColor Red
        Get-Content $log -ErrorAction SilentlyContinue | ForEach-Object { Write-Host "  $_" }
        Write-Host ("[{0}] run this on the laptop to see the full error: cua sb create ubuntu --kind vm --name {0}" -f $vm) -ForegroundColor Yellow
        Write-Host "[$vm] QEMU VMs on a fresh machine usually fail here because of the shim/WHPX/keymaps — Install-VmStack.ps1 fixes those." -ForegroundColor Yellow
        $failed += $vm
        continue
    }
    Write-Host ("[{0}] ready. View: Cua Spaces app, or 'cua sb port-forward {0} 5900:{1}' + VNC viewer." -f $vm, (5900 + $i)) -ForegroundColor Green
    Write-Host ("[{0}] Inside the VM: open a browser at {1} , enter the LAN token." -f $vm, $MasterUrl)
}

Write-Host "`nList:   cua sb list`nRemove: cua sb rm <name> --force"
Write-Host "Scale: re-run with -Count 5 (max 5 per laptop by design; each needs ~4 GB free RAM)."

if ($failed.Count -gt 0) {
    Write-Host ("`nFAILED: {0} of {1} VM(s) could not be created: {2}" -f $failed.Count, $Count, ($failed -join ", ")) -ForegroundColor Red
    exit 1
}
exit 0
