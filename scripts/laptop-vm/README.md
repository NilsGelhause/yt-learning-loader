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
   AND the laptop's VMs (the beacon        (the beacon registers them with
   below registers them, presence-only     the master — presence-only unless
   unless an agent endpoint is reachable)  an agent endpoint is reachable)
```

## Setup on the laptop

### Option A (recommended) — Connect device from the master's CUA tab

No manual kit copying: the master drives the whole setup over a pairing-based
reverse agent (`devices.py` + `Device-Agent.ps1`).

1. On the master, open the **CUA Spaces** tab → **Connect device**. A card
   shows an 8-char pairing code (valid 10 min) and one PowerShell line.
2. On the laptop, paste that one line into PowerShell and press Enter:
   ```powershell
   irm http://<master>:8471/device-agent.ps1 -OutFile agent.ps1; .\agent.ps1 -MasterUrl http://<master>:8471 -Code <CODE>
   ```
   (If the master has a LAN token, the agent asks for it once.) The agent
   pairs and polls the master every 5 s.
3. Back on the master, the new device appears under **Devices**. Click
   **Install VM stack** — the laptop shows one UAC prompt, then installs
   cua/QEMU/shim; when it ends with `[REBOOT-REQUIRED]`, reboot the laptop and
   re-run the same one-liner (the agent does not survive a reboot yet).
4. Then **Create VM** / **Create 3 VMs** and finally **Start beacon** — all
   from the master. Every task streams into the shared job transcript.

### Option B — manual kit scripts

Copy this folder (`scripts/laptop-vm`) to the laptop, then (~30 minutes,
mostly downloads):

1. **Admin PowerShell** in that folder:
   ```powershell
   powershell -ExecutionPolicy Bypass -File .\Install-VmStack.ps1
   ```
   It installs/repairs everything we fixed the hard way on the master:
   `cua` CLI, the Windows Hypervisor Platform feature (**reboot when it says
   so**), the QEMU runtime, the QEMU keymaps packaging bug, and the qemu shim
   (`-cpu host` WHPX crash + daemon-CWD VNC bug). Safe to re-run.
2. After the reboot, create your first VM:
   ```powershell
   powershell -ExecutionPolicy Bypass -File .\New-LaptopVm.ps1 -MasterUrl http://192.168.50.111:8471 -Count 1
   ```
   (`-Count` up to 5. Each VM defaults to ~4 GB RAM — mind the laptop's RAM.)
3. **Start the beacon** so the laptop's VMs appear in the master's
   Network tab:
   ```powershell
   powershell -File .\Register-Nodes.ps1 -MasterUrl http://192.168.50.111:8471 -Token <your-LAN-token>
   ```
   Keep the window open, or run it in the background via Task Scheduler or
   `Start-Process powershell -ArgumentList ... -WindowStyle Hidden`. It
   registers every VM on the laptop every 30 s and auto-detects new/deleted
   ones; if the master is offline it keeps retrying. Optional: when a VM
   publishes `endpoints.agent` (an in-guest agentd sidecar), the beacon
   passes the endpoint URL through, and the master lists that node's agent
   and sessions too — provided the master can actually reach that URL
   (reachability from the master is your job: LAN IP / port-forward / relay).
   Without a reachable endpoint the node is presence-only, which is enough
   for the Network tab. Use `-Once` for a single test registration.

### What the laptop needs — checklist

| Program / piece | Required? | How it gets there |
|---|---|---|
| `cua` CLI | **yes** | `Install-VmStack.ps1` runs the official installer |
| QEMU | **yes** | `cua runtime setup` inside the prep script (if missing) |
| Windows Hypervisor Platform feature | **yes** | enabled by the prep script via DISM (**reboot**) |
| Shim + keymaps fix | **yes** | applied by the prep script |
| Node.js on the laptop | **no** — guests ship Node 24; nothing on the host needs it | — |
| Git for Windows | only if you `git clone` the repo (else copy the folder via USB/cloud) | git-scm.com |
| VNC viewer (e.g. RealVNC/TightVNC) | optional — only if you don't use the Cua Spaces app to see desktops | vendor site |
| Cua Spaces app | optional — alternative desktop viewer/streamer | offered by the cua installer |
| OBS Studio | optional — for recording VM desktops later | obsproject.com |

Hardware/firmware: virtualization (VT-x) enabled in BIOS/UEFI (almost always on by
default), and ~4 GB free RAM per VM you plan to run (5 VMs ⇒ a 24–32 GB laptop).

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
