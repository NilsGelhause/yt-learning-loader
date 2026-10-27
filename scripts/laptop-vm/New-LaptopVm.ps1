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

Write-Host "Planning $Count VM(s) named $Name-1 .. $Name-$Count" -ForegroundColor Cyan
Write-Host "Master (open inside each VM's browser): $MasterUrl"
Write-Host "Check the laptop can handle it: each VM defaults to 4 GB RAM / 2-4 CPUs.`n"

for ($i = 1; $i -le $Count; $i++) {
    $vm = "{0}-{1}" -f $Name, $i
    $exists = & cua sb list --json 2>$null | ConvertFrom-Json | Where-Object { $_.name -eq $vm }
    if ($exists) {
        Write-Host "[$vm] already exists — skipping." -ForegroundColor Yellow
        continue
    }
    Write-Host "[$vm] creating (first run downloads the image, can take minutes) ..."
    & cua sb create ubuntu --kind vm --runtime qemu --name $vm --service agent=8472 --json
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[$vm] creation FAILED — run 'cua doctor' / 'cua runtime doctor' for why." -ForegroundColor Red
        continue
    }
    Write-Host ("[{0}] ready. View: Cua Spaces app, or 'cua sb port-forward {0} 5900:{1}' + VNC viewer." -f $vm, (5900 + $i)) -ForegroundColor Green
    Write-Host ("[{0}] Inside the VM: open a browser at {1} , enter the LAN token." -f $vm, $MasterUrl)
}

Write-Host "`nList:   cua sb list`nRemove: cua sb rm <name> --force"
Write-Host "Scale: re-run with -Count 5 (max 5 per laptop by design; each needs ~4 GB free RAM)."
