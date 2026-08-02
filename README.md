# Volvo Diagnostic Toolkit

[![tests](https://github.com/MarvinParanoid/VolvoDiagToolkit/actions/workflows/tests.yml/badge.svg)](https://github.com/MarvinParanoid/VolvoDiagToolkit/actions/workflows/tests.yml)

> Unofficial and independent. Not affiliated with, authorised by or endorsed
> by Volvo Car Corporation. "Volvo" and "VIDA" are their trademarks and appear
> here only to describe what this software talks to.

Tools for finding out what a Volvo actually reports, and then reading it
without VIDA. Built around a Volvo V50 (P1) with the D4164T 1.6 diesel; the
engine-specific part is data files, the rest is not Volvo-specific.

The immediate target is the DPF/diesel picture that VIDA shows and generic OBD
does not: actual and requested boost, DPF differential pressure, exhaust
temperature, EGR, rail pressure — read live in a browser dashboard.

The legislated OBD stack gets no answer from this ECM: it is reached over raw
29-bit CAN with Volvo's own framing (the "A6" read service and the "B9"
identity/configuration block), reverse-engineered from real VIDA sessions
through the proxy and documented in [docs/volvo-protocol.md](docs/volvo-protocol.md).

Two ways the parameter database gets filled, both keeping VIDA as the source of
truth:

```
              ┌──► JSONL log ──► summarize / diff ─────────────┐
VIDA ─► j2534proxy.dll ─► VXDIAG ─► car                        │
              └──► connect/pins/protocol per bus ──────────┐   │
                                                           ▼   ▼
CarCom (VIDA's own SQL DB) ─► scripts/carcom-*.ps1 ─► definitions/*.yaml
                                                           │
                          volvo-monitor  (serve · monitor · config) ◄┘
```

- **From the proxy logs** — record a VIDA session, diff two recordings, decode
  what changed. Slow but needs nothing but the car.
- **From CarCom** — VIDA's parameter definitions live in a local SQL Server
  database. `scripts/carcom-*.ps1` pull the identifiers, scaling, value labels,
  DTC catalogue and per-bus connection details straight out of it, pinned to
  this exact car's ECU variants. This is how most of the database was built.

## Layout

| path | what it is |
| --- | --- |
| [proxy/](proxy/) | the J2534 pass-through DLL that logs every call |
| [fake-j2534/](fake-j2534/) | a J2534 driver with a simulated ECM, for testing without a car |
| [test-client/](test-client/) | minimal J2534 application — checks a DLL before VIDA sees it |
| [python/volvo_diag/](python/volvo_diag/) | the Volvo A6 protocol, transports, the monitor, the dashboard, config decode |
| [definitions/](definitions/) | parameter, DTC and configuration databases as YAML, with provenance per entry |
| [scripts/](scripts/) | Windows build/registration, driver inventory, and the `carcom-*.ps1` CarCom extractors (PowerShell 2.0 compatible) |
| [cmake/](cmake/) | mingw-w64 toolchain files for cross-building from Linux |
| [docs/method.md](docs/method.md) · [docs/volvo-protocol.md](docs/volvo-protocol.md) · [docs/carcom.md](docs/carcom.md) | how a parameter is found · the on-wire protocol · the CarCom extraction |

## Try it without a car

Everything except the vendor driver builds and runs on Linux, against the
simulated ECM in `fake-j2534/`:

```sh
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=RelWithDebInfo
cmake --build build
ctest --test-dir build            # the C++ core test
pip install -e ".[dev]" && pytest # the Python suite (what CI runs)

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

To preview the dashboard itself with no build and no adapter — synthetic data
on the real definitions — just run:

```sh
PYTHONPATH=python python3 -m volvo_diag serve --fake
```

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

The reading client is pure Python plus PyYAML and does **not** need the Windows 7
VIDA machine — it only needs the VXDIAG J2534 driver and access to the adapter,
so a modern Windows 10 box works well. Match Python's bitness to the driver's
(32-bit VXDIAG DLL → 32-bit Python). Run it installed (`pip install -e .` gives
the `volvo-monitor` command) or straight from a checkout:

```powershell
$env:PYTHONPATH="python"
python -m volvo_diag devices                      # find the registered J2534 driver
python -m volvo_diag read coolant_temperature     # one value, quick sanity check
python -m volvo_diag monitor                       # live table in the terminal
python -m volvo_diag serve --host 127.0.0.1 --port 8080   # the dashboard
python -m volvo_diag config                        # CEM identity + car configuration
python -m volvo_diag dump --ecu CEM                # back up a module's blocks to JSON
python -m volvo_diag read 22F190                   # raw UDS request, any hex
```

**The dashboard** (`serve`) is the main way to watch the car: a sidebar lists
every defined parameter grouped by *Module · Subsystem*, with search and a
pinned "selected" section; ticking one charts it live with a proper time-series
graph. A bus selector switches between the 500k powertrain bus (ECM, ABS, CEM)
and the 125k low-speed cabin bus (DIM and the other cabin modules) — a CAN link
is one baud rate, so they are read one at a time. A **Configuration** tab reads
the CEM's vehicle identity (VIN, chassis, market) and the ~99 coded car-config
options (gearbox, doors, particle filter, …). Open it in the guest's browser or
reach it from the host over the network with `--host 0.0.0.0`.

Every row carries a status colour — `verified-against-vida` down to `candidate`
— so it is never unclear which numbers are trusted and which are still guesses.

`dump` writes a module's identity/configuration blocks (VIN, car config, part
numbers) to a JSON file verbatim — a record of what the module held and a restore
point to keep before ever changing anything.

### What is defined so far

| module | bus | parameters | source |
| --- | --- | --- | --- |
| ECM (D4164T, Bosch EDC16C31) | 500k | boost, MAF, EGR, rail, DPF, temperatures, … | CarCom + verified against VIDA |
| ABS | 500k | wheel speeds, yaw, pressures | CarCom |
| CEM | 500k / 125k | electrical, climate, lighting, immobiliser states | CarCom |
| DIM | 125k | fuel, distance, cluster temperatures | CarCom |
| CEM configuration | — | VIN + 99 car-config options + installed-modules map | CarCom (`config-cem.yaml`) |
| ECM DTC catalogue | — | 154 fault-code → text entries | CarCom (`dtc-ecm.yaml`) |

## The VIDA machine

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
ordinary files, so copy them off and analyse them anywhere. Only the reading
client needs a Windows box with the VXDIAG driver — and that can be a modern
Windows 10, it does not have to be the Windows 7 VIDA machine.

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

Which modules are reachable on which bus was answered from the proxy logs: the
500k powertrain CAN reaches ECM, ABS and the CEM gateway; DIM and the cabin
modules only answer on the low-speed bus, which VXDIAG exposes as a separate
J2534 protocol (`32772`) at 125k with a vendor bus-selector config — that too
was lifted from a VIDA session and is handled in `transport/j2534.py`.

## Scope

Read-only, deliberately. No security access, no writing identifiers, no
routine control, no clearing adaptations, no forced regeneration. The proxy
will show you how VIDA does all of those; that is not a reason to do them from
a half-verified parameter database.

## References

Most of what this reads was reverse-engineered from VIDA sessions and pulled
from CarCom (VIDA's own SQL database) on this specific car — there is no
external crib sheet for the P1 A6 parameters, which is why the proxy and the
`carcom-*.ps1` extractors exist. The outside sources worth crediting:

- **[vtl/volvo-cem-cracker](https://github.com/vtl/volvo-cem-cracker)** —
  recovers the CEM security PIN via a timing side-channel on the CAN bus. The
  reference for *why configuration writes are gated* (a per-car 6-byte PIN that
  is not in CarCom) and why they stay out of scope here.
- **SAE J2534** ("PassThru") — the vendor-neutral diagnostic API the transport
  layer (`transport/j2534.py`) calls into the VXDIAG driver with.
- **VIDA / CarCom** — Volvo's own dealer software and its local database, used
  strictly as the reference: the proxy records what VIDA asks, and the
  extractors read definitions VIDA already ships. See
  [docs/carcom.md](docs/carcom.md) for the schema that was walked.

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
