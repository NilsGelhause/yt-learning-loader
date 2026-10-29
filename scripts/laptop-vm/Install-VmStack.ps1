#Requires -RunAsAdministrator
<#
.SYNOPSIS
  Prepares a Windows laptop to host cua QEMU VMs (with desktops) that join the
  yt-learning-loader master as LAN clients.

.DESCRIPTION
  Runs the three fixups we discovered the hard way on the master PC:
    1. Ensures the `cua` CLI is installed (runs the official installer if not).
    2. Enables the Windows Hypervisor Platform feature (WHPX) and reboots if needed.
    3. Provisions the QEMU runtime (`cua runtime setup`) if qemu is missing.
    4. Repairs the QEMU packaging bug (keymaps in share/keymaps instead of
       share/qemu/keymaps) — without it, VNC dies with "Could not open 'en-us'".
    5. Installs the qemu shim (QemuShim.cs next to this script): rewrites
       `-cpu host` -> `-cpu qemu64` (WHPX crash fix) and forces a neutral
       working directory (cua's daemon spawns qemu in C:\WINDOWS\system32,
       which breaks VNC keymap loading).

  Safe to re-run: every step checks before acting.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\Install-VmStack.ps1
#>
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$QemuDir = "C:\Program Files\qemu"
$ShimSrc = Join-Path $PSScriptRoot "QemuShim.cs"

function Step($msg) { Write-Host "`n== $msg" -ForegroundColor Cyan }

# --- 1. cua CLI -------------------------------------------------------------
Step "cua CLI"
$cua = Get-Command cua -ErrorAction SilentlyContinue
if ($cua) {
    Write-Host "found: " + $cua.Source
} else {
    Write-Host "not found — downloading the official installer (CLI only, no prompts) ..."
    $inst = Join-Path $env:TEMP "cua-install.ps1"
    Invoke-RestMethod -Uri "https://cua.ai/install.ps1" -OutFile $inst
    # -CliOnly installs only the cua CLI: no interactive menu, no sign-in
    powershell -NoProfile -ExecutionPolicy Bypass -File $inst -CliOnly -Yes
    $env:Path = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [Environment]::GetEnvironmentVariable("Path","User")
    if (-not (Get-Command cua -ErrorAction SilentlyContinue)) {
        throw "cua still not on PATH — open a NEW terminal and re-run this script."
    }
    Write-Host "cua installed."
}

# --- 2. Windows Hypervisor Platform ----------------------------------------
Step "Windows Hypervisor Platform (WHPX)"
$feat = dism /online /get-featureinfo /featurename:HypervisorPlatform 2>$null | Out-String
if ($feat -match "State : Enabled") {
    Write-Host "already enabled."
} elseif ($feat -match "State : Enable Pending") {
    Write-Host "enabled but pending — a reboot is still required." -ForegroundColor Yellow
    $global:needsReboot = $true
} else {
    dism /online /enable-feature /featurename:HypervisorPlatform /all /norestart | Out-Null
    Write-Host "ENABLED — a reboot is required before VMs will start." -ForegroundColor Yellow
    $global:needsReboot = $true
}

# --- 3. QEMU runtime ---------------------------------------------------------
Step "QEMU runtime"
$doctor = & cua runtime doctor 2>&1 | Out-String
if ($doctor -match "qemu\s+ok") {
    Write-Host "qemu present."
} else {
    Write-Host "provisioning via 'cua runtime setup' ..."
    & cua runtime setup
    $doctor = & cua runtime doctor 2>&1 | Out-String
    if ($doctor -match "qemu\s+ok") {
        Write-Host "qemu ready."
    } else {
        Write-Host "qemu still not ready after 'cua runtime setup' — see 'cua runtime doctor'." -ForegroundColor Yellow
    }
}

# --- 4. keymaps packaging bug -------------------------------------------------
Step "QEMU keymaps"
$kmSrc = Join-Path $QemuDir "share\keymaps"
$kmDst = Join-Path $QemuDir "share\qemu\keymaps"
if ((Test-Path $kmSrc) -and -not (Test-Path (Join-Path $kmDst "en-us"))) {
    New-Item -ItemType Directory -Force $kmDst | Out-Null
    Copy-Item "$kmSrc\*" $kmDst -Recurse -Force
    Write-Host "copied keymaps into share\qemu\keymaps."
} elseif (Test-Path (Join-Path $kmDst "en-us")) {
    Write-Host "already fixed."
} else {
    Write-Host "share\keymaps not found (nonstandard qemu layout?) — check manually."
}

# --- 5. qemu shim -------------------------------------------------------------
Step "qemu shim (-cpu host WHPX fix + CWD fix)"
$real = Join-Path $QemuDir "qemu-system-x86_64.real.exe"
$shimInstalled = Join-Path $QemuDir "qemu-system-x86_64.exe"
if ((Test-Path $real) -and (Test-Path $shimInstalled)) {
    Write-Host "shim already installed."
} elseif (-not (Test-Path $shimInstalled) -and -not (Test-Path $real)) {
    Write-Host "qemu-system-x86_64.exe not found at $QemuDir — install QEMU first (step 3)."
} else {
    # Partial state from an earlier run: only the original exe (fresh install)
    # or only the .real.exe (shim lost) — both are repaired here.
    $tmp = Join-Path $env:TEMP "qemu-shim-build"
    New-Item -ItemType Directory -Force $tmp | Out-Null
    Add-Type -TypeDefinition (Get-Content -Raw $ShimSrc) -OutputAssembly (Join-Path $tmp "qemu-system-x86_64.exe") -ReferencedAssemblies System.dll
    if ((Test-Path $shimInstalled) -and -not (Test-Path $real)) {
        Rename-Item $shimInstalled "qemu-system-x86_64.real.exe"
        Write-Host "original renamed to qemu-system-x86_64.real.exe."
    }
    Copy-Item (Join-Path $tmp "qemu-system-x86_64.exe") $shimInstalled -Force
    Write-Host "shim installed."
}

# --- done ---------------------------------------------------------------------
Write-Host "`n== Done." -ForegroundColor Green
if ($global:needsReboot) {
    Write-Host "REBOOT NOW, then continue with New-LaptopVm.ps1." -ForegroundColor Yellow
} else {
    Write-Host "Next:  powershell -ExecutionPolicy Bypass -File .\New-LaptopVm.ps1 -MasterUrl http://192.168.50.111:8471 -Count 1"
}
