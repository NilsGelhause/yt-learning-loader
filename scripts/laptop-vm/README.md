# Laptop VM kit — turn a second Windows laptop into a VM host for the master

Your master PC runs the yt learning loader app + Pi Durable harness. This kit
turns another Windows laptop into a host for up to **5 GUI Ubuntu VMs** that
join the master as **LAN clients** — each with a real desktop you can watch
and, later, record.

## Topology

```
master PC (192.168.50.111)                laptop (any Windows)
├─ server.py :8471  ◄─────────────────────┤  VMs in browsers point here
├─ agentd :8472 (master brain)            ├─ laptop-vm-1 … laptop-vm-5
└─ Network tab lists master sessions      └─ QEMU VMs, Ubuntu 24.04 desktops
   (laptop VMs are pure clients today —
    remote-node listing is a later step)
```

## Setup on the laptop (30 minutes, mostly downloads)

1. **Copy this folder** (`scripts/laptop-vm`) to the laptop.
2. **Admin PowerShell** in that folder:
   ```powershell
   powershell -ExecutionPolicy Bypass -File .\Install-VmStack.ps1
   ```
   It installs/repairs everything we fixed the hard way on the master:
   `cua` CLI, the Windows Hypervisor Platform feature (**reboot when it says
   so**), the QEMU runtime, the QEMU keymaps packaging bug, and the qemu shim
   (`-cpu host` WHPX crash + daemon-CWD VNC bug). Safe to re-run.
3. After the reboot, create your first VM:
   ```powershell
   powershell -ExecutionPolicy Bypass -File .\New-LaptopVm.ps1 -MasterUrl http://192.168.50.111:8471 -Count 1
   ```
   (`-Count` up to 5. Each VM defaults to ~4 GB RAM — mind the laptop's RAM.)

## Using a VM

- **See the desktop / record later**: open the **Cua Spaces app** on the
  laptop and pick the Space (live stream), or:
  `cua sb port-forward laptop-vm-1 5900:5901` and point a VNC viewer at
  `127.0.0.1:5901`. Screen recordings (OBS, or the app's stream) capture what
  the agent does on the desktop.
- **Join the master**: inside the VM, open a browser →
  `http://192.168.50.111:8471` → enter the LAN token. The VM is now a client:
  power button greyed out, sessions it starts run on the **master** (like any
  browser). Note: inside laptop-hosted VMs the master's LAN IP is the target —
  `10.0.2.2` means "the laptop", not the master.

## Scaling, lifecycle

- More VMs: re-run `New-LaptopVm.ps1` with a higher `-Count` (max 5).
- Stop/resume: `cua sb suspend <name>` / `cua sb resume <name>`.
- Delete: `cua sb rm <name> --force`.
- The master's Windows firewall rule for port 8471 and the LAN token gate
  must stay in place (they are, on the master).

## Troubleshooting (all three bit us on the master)

| Symptom | Fix |
|---|---|
| VM "paused", `WHPX: Unexpected VP exit code 4` | shim missing — re-run `Install-VmStack.ps1` step 5 |
| `Could not open 'en-us': Permission denied` | keymaps bug — re-run the script (step 4) |
| qemu not found | `cua runtime setup`, then re-run the script |
| Can't reach the master from the VM | master's firewall rule / VM NAT: use the master's LAN IP |
