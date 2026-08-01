# Volvo Diagnostic Toolkit

> Unofficial and independent. Not affiliated with, authorised by or endorsed
> by Volvo Car Corporation. "Volvo" and "VIDA" are their trademarks and appear
> here only to describe what this software talks to.

Tools for finding out what a Volvo actually reports, and then reading it
without VIDA. Built around a Volvo V50 (P1) with the D4164T 1.6 diesel; the
engine-specific part is a data file, the rest is not Volvo-specific.

The immediate target is the DPF picture that VIDA shows and generic OBD does
not: actual and requested boost, DPF differential pressure, soot load,
exhaust temperature, regeneration state and distance since the last one.

VIDA is not being replaced. It is the reference: it knows the right questions,
and the proxy writes them down.

```
VIDA ──► j2534proxy.dll ──► VXDIAG driver ──► car
              │
              └──► JSONL log ──► summarize / diff ──► definitions/*.yaml
                                                            │
                              volvo-monitor ◄───────────────┘
```

## Layout

| path | what it is |
| --- | --- |
| [proxy/](proxy/) | the J2534 pass-through DLL that logs every call |
| [fake-j2534/](fake-j2534/) | a J2534 driver with a simulated ECM, for testing without a car |
| [test-client/](test-client/) | minimal J2534 application — checks a DLL before VIDA sees it |
| [python/volvo_diag/](python/volvo_diag/) | log analysis, transports, UDS/OBD, the monitor |
| [definitions/](definitions/) | parameter database as YAML, with provenance per entry |
| [scripts/](scripts/) | Windows build, registration and driver inventory (PowerShell 2.0 compatible) |
| [cmake/](cmake/) | mingw-w64 toolchain files for cross-building from Linux |
| [docs/method.md](docs/method.md) | how a parameter is actually found |

## Try it without a car

Everything except the vendor driver builds and runs on Linux, against the
simulated ECM in `fake-j2534/`:

```sh
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=RelWithDebInfo
cmake --build build
./build/proxy/proxy_core_test
PYTHONPATH=python python3 -m unittest discover -s tests -t .

# a J2534 session, through the proxy, into the fake ECM
export VOLVO_J2534_REAL_DLL=$PWD/build/fake-j2534/fake_j2534.so
export VOLVO_J2534_LOG_DIR=/tmp/volvo-logs
./build/test-client/j2534-test ./build/proxy/j2534proxy.so --request 22F190

# what the proxy recorded
PYTHONPATH=python python3 -m volvo_diag.logs.summarize /tmp/volvo-logs/*.jsonl

# the dashboard, reading the simulator through the same stack
PYTHONPATH=python python3 -m volvo_diag.cli \
    --transport j2534 --library ./build/proxy/j2534proxy.so \
    --definitions definitions/simulator monitor
```

The simulator's identifiers (`22 FE xx`) are **made up**. They live in
`definitions/simulator/` and are never loaded against a real car — the default
definition path is `definitions/volvo/`.

## On the car

### 1. Bitness first

```powershell
powershell -ExecutionPolicy Bypass -File scripts\list-j2534.ps1
```

This prints every registered J2534 driver, the path to its DLL and whether
that DLL is x86 or x64, plus the bitness of a running VIDA. Most VXDIAG
installations are x86 — but check, do not assume. A proxy of the wrong bitness
fails to load and the error VIDA shows says nothing useful.

### 2. Build and register

On the Windows machine, with Visual Studio:

```powershell
.\scripts\build-windows.ps1 -Arch Win32 -Test
.\scripts\install-proxy.ps1                 # elevated
```

Or cross-compile on Linux with mingw-w64 and copy the DLL over:

```sh
./scripts/build-mingw.sh i686        # or x86_64
```

That script also checks the two things that silently break a J2534 DLL: that
the exports are undecorated (`PassThruOpen`, not `_PassThruOpen@8`) and that
nothing outside the system DLLs has to be resolved at load time — VIDA's
machine has no `libwinpthread-1.dll`. With wine installed it then runs the
unit tests and a proxy→fake-driver session as a smoke test.

`install-proxy.ps1` adds a *second* J2534 entry and leaves the vendor
registration alone, so VIDA offers both and you choose per session.
`remove-proxy.ps1` takes it back out.

Some applications only list device names they already know and will ignore the
added entry. For those, `install-proxy.ps1 -InPlace` points the vendor entry's
own `FunctionLibrary` at the proxy and remembers the original path in a
`ProxiedLibrary` value; `remove-proxy.ps1` restores it. The application then
selects the same device it always did. If VIDA runs in a VM, take a snapshot
before either — reverting is faster than debugging.

Before starting VIDA, prove the chain works:

```powershell
build-Win32\test-client\RelWithDebInfo\j2534-test.exe `
    C:\ProgramData\volvo-toolkit\j2534proxy.dll
```

`PassThruOpen`, `PassThruReadVersion` and `READ_VBATT` should all return 0 and
the battery voltage should be real.

### Windows 7 SP1 (the usual VIDA machine)

Three things differ there, and all three are handled:

**The C runtime.** mingw-w64 links against the Universal CRT, which Windows 7
only has once KB2999226 is installed. `list-j2534.ps1` reports whether
`ucrtbase.dll` is present and `install-proxy.ps1` refuses to register a DLL
the machine cannot load. The MSVC build has no such dependency at all — it
links the CRT statically (`/MT`).

Which of the two to use depends on the machine. Visual Studio 2019 needs a
64-bit Windows, and a 32-bit VIDA VM is usually short on memory anyway, so
there the practical answer is to cross-build on Linux and copy three files
over: `j2534proxy.dll`, `j2534-test.exe` and `fake_j2534.dll`. They are
statically linked and need no compiler, no runtime and no installer on the
target — only the UCRT, which `list-j2534.ps1` confirms. Where Visual Studio
does fit (2015, 2017, 2019 on 64-bit, or the standalone Build Tools),
`build-windows.ps1` avoids even that dependency.

**PowerShell 2.0 on .NET 3.5.** That is what Windows 7 SP1 ships with, so the
scripts avoid `[pscustomobject]`, `$PSScriptRoot`, `Is64BitOperatingSystem`
and `RegistryKey.OpenBaseKey`, and reach the registry through provider paths.
They also work unchanged on PowerShell 5.1 if the machine has been updated.

**A 32-bit Windows has no `Wow6432Node`**, so there is a single registry view
and `HKLM\SOFTWARE\PassThruSupport.04.04` is the only place to look. The
scripts detect this rather than listing every driver twice.

For the Python side, 3.8.10 is the last release that installs on Windows 7 —
that is the floor this project targets (no `match`, no runtime `int | None`).
The log analysis does not have to run there at all: the JSONL files are
ordinary files, so copy them off and analyse them anywhere. Only
`volvo-monitor` needs to run on Windows, because the VXDIAG driver does.

### VIDA in a VM, adapter on a Linux host

A VXDIAG VCX is not a USB serial gadget: it is a USB Ethernet bridge in front
of a controller that runs its own DHCP server, and the vendor DLL talks to it
over IP. On Linux the kernel's `r8152` claims it, the host lands on the
adapter's private subnet, and VMware then cannot pass the USB device through
at all — "The connection for the USB device ... was unsuccessful. Driver
error."

Passing it through is the wrong fix anyway; the guest would need an RTL8152
driver, which Windows 7 does not ship. Bridge a virtual NIC onto the host
interface instead and the guest reaches the adapter directly:

```sh
./scripts/vmware-bridge.sh --show              # which interface is which
sudo ./scripts/vmware-bridge.sh enp8s0f3u2     # pin vmnet0 to the adapter
```

VMware's default bridge is "automatic" and picks whichever interface it likes,
usually Wi-Fi; this pins it. The VM needs an adapter on that vmnet, and needs
to be shut down while the script restarts VMware networking. Keep a second
adapter on NAT if the guest also needs internet.

### 3. Record, diff, define

See [docs/method.md](docs/method.md). Short version: one new parameter per
recording, then

```sh
python -m volvo_diag.logs.diff 01-baseline.jsonl 02-plus-boost.jsonl
python -m volvo_diag.logs.summarize 02-plus-boost.jsonl --track 22D123
```

and write what you find into `definitions/volvo/p1/d4164t.yaml` with its
status, source log and a raw sample.

### 4. Read it back

```sh
volvo-monitor devices
volvo-monitor info
volvo-monitor monitor --csv trip.csv
volvo-monitor dtc
volvo-monitor read boost_actual
volvo-monitor read 22F190          # raw request, any hex
```

Until a Volvo-specific definition exists, the dashboard falls back to the
closest standard OBD-II PID and labels the row `(OBD-II PID)`, so it is never
unclear which numbers are guesses.

## The log format

One JSON object per line, one per J2534 call:

```json
{"ev":"write","t":1785574084178,"mono":112,"tid":3051511632,"channel":8193,
 "requested":1,"written":1,"timeout":1000,
 "msgs":[{"proto":6,"rx_status":0,"tx_flags":64,"ts":0,"len":7,"extra":0,
          "data":"000007E022F190"}],
 "us":5,"result":0,"result_name":"STATUS_NOERROR","n":8}
```

The DLL stores raw calls and interprets nothing beyond decoding ioctl
structures; splitting CAN ids off payloads and pairing requests with responses
happens in Python, where it can be fixed without rebuilding anything.
Empty `ReadMsgs` polls are dropped by default (`log_empty_reads = 1` keeps
them) — see [proxy/j2534proxy.ini.example](proxy/j2534proxy.ini.example).

## Transports

| transport | use it for | state |
| --- | --- | --- |
| J2534 (`transport/j2534.py`) | VXDIAG and the proxy on Windows | the main path |
| SocketCAN (`transport/socketcan.py`) | CANable/Linux, kernel ISO-TP with a raw-CAN fallback | untested against the car |
| ELM327 (`transport/vlinker.py`) | vLinker over Bluetooth, for a phone app | untested against the car |

Which modules are reachable from the OBD connector, and which need a gateway
or another bus, is a question the proxy logs answer — stage 7 in
[docs/method.md](docs/method.md). Nothing here assumes it.

## Scope

Read-only, deliberately. No security access, no writing identifiers, no
routine control, no clearing adaptations, no forced regeneration. The proxy
will show you how VIDA does all of those; that is not a reason to do them from
a half-verified parameter database.

## Licence and disclaimer

[MIT](LICENSE).

Unofficial and independent, as stated at the top: no affiliation with Volvo
Car Corporation, and the trademarks are theirs.

This software talks to the diagnostic bus of a moving vehicle's engine
controller. It only reads, and it goes out of its way to say which of its
numbers are guesses — but it comes with no warranty of any kind, and a
diagnostic tool that is wrong about a DPF is a tool that can cost you an
expensive part. Verify against VIDA before you act on anything it tells you,
and do not use it while driving.
