# Troubleshooting

## The adapter

**Only one program can hold the VXDIAG at a time.** Close VIDA / VX Manager
before running the client, and don't run two clients at once. `dump`/`serve`
opening the device while VIDA has it will fail.

**`devices` finds nothing.** Install the VXDIAG driver (VX Manager). The client
reads `HKLM\SOFTWARE\PassThruSupport.04.04`; a 32-bit Python sees the 32-bit
(WOW6432Node) view automatically, which is the one to match a 32-bit DLL.

**The proxy DLL will not load in VIDA.** Almost always a bitness mismatch or a
missing runtime — run `scripts\list-j2534.ps1`. A 32-bit VIDA needs a 32-bit
proxy; a mingw build needs the UCRT (KB2999226). See
[install-windows.md](install-windows.md).

## VIDA in a VM, adapter on a Linux host

A VXDIAG VCX is not a USB serial gadget: it is a USB Ethernet bridge in front of
a controller that runs its own DHCP server, and the vendor DLL talks to it over
IP. On Linux the kernel's `r8152` claims it, the host lands on the adapter's
private subnet, and VMware then cannot pass the USB device through at all — "The
connection for the USB device … was unsuccessful. Driver error."

Passing it through is the wrong fix anyway; the guest would need an RTL8152
driver, which Windows 7 does not ship. Bridge a virtual NIC onto the host
interface instead and the guest reaches the adapter directly:

```sh
./scripts/vmware-bridge.sh --show              # which interface is which
sudo ./scripts/vmware-bridge.sh enp8s0f3u2     # pin vmnet0 to the adapter
```

VMware's default bridge is "automatic" and picks whichever interface it likes,
usually Wi-Fi; this pins it. The VM needs an adapter on that vmnet, and must be
shut down while the script restarts VMware networking. Keep a second adapter on
NAT if the guest also needs internet.

## Installing Python 3.8 on Windows 7

3.8.10 is the last Python that runs on Windows 7 — do not go newer. Its installer
is SHA-2 signed and checks for a couple of updates.

- **"Setup failed … Windows 7 Service Pack 1 and all applicable updates are
  required."** Read the installer's log (link in the dialog). It usually names
  the exact update, most often **KB2533623** (secure DLL loading). Install
  `Windows6.1-KB2533623-x86.msu` (x64 on a 64-bit OS), reboot, retry. The
  Universal C Runtime (KB2999226) may also be needed — the log says `CRTInstalled`.
- **`.msu` install fails with `0x80070422`.** The Windows Update service is
  disabled. Enable and start it: `services.msc` → *Windows Update* → Startup
  *Manual* → Start; or, as admin, `sc config wuauserv start= demand` then
  `net start wuauserv`.
- Prefer not to fight it? The **reading client does not need Windows 7** — run it
  on a modern Windows 10 box instead (see [dashboard.md](dashboard.md)).

## Running the client

**`pip` is not recognized.** Python is not on PATH (reopen the terminal after
installing with "Add to PATH"), or use `python -m pip …` / `py -m pip …`.

**`No module named volvo_diag`.** Set the path first — in PowerShell
`$env:PYTHONPATH="python"` (not cmd's `set PYTHONPATH=python`), or `pip install -e .`.

**Values flicker in the dashboard.** Occasional bus timeouts; the page keeps the
last value dimmed rather than blanking, and reads recover on the next tick.

**Configuration tab: "CEM did not answer".** Ignition must be on (key position
II); the cabin bus wakes with the key. CEM configuration reads on either bus.

**`serve` used to exit silently under load.** Fixed — the client clears the
driver's receive FIFO before each request (a full FIFO made the VXDIAG driver
crash) and does all adapter I/O on one thread. If it still dies, capture it with
`python -X faulthandler -m volvo_diag serve … 2> crash.txt` and read `crash.txt`.
