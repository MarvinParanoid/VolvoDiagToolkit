# H2 — Hardware smoke test (VXDIAG / Volvo V50)

Read-only validation of commit `20813fb` on the real car, with a focus on the
125k low-speed CAN path.

| | |
| --- | --- |
| **Status** | Phase 1–2 done on Linux. H2.1 (offline history review) done. **Phase 3 (car) NOT RUN — needs explicit human approval.** |
| **Hardware results** | none yet; every hardware row below is NOT RUN |
| **125k ProtocolID** | **STRONGLY SUPPORTED** defect, **fixed** in commit `fix(j2534): correct low-speed CAN protocol tagging` (§6). **Not hardware-confirmed**: confirmation needs H2-05 on the car. |
| **Code changes** (same commit) | `J2534CanLink.send` tags messages with the channel protocol (§6); the fake driver models the 0x8004 channel and J2534-1 ProtocolID checks; the Windows `ctypes` loader in `ProxySelfLoadTest`; `scripts/h2_proto_check.py` and `scripts/h2_sanitize.py`; tests |
| **Verdict so far** | **BLOCKED** on hardware and on a Windows build environment. Not a defect. |

Legend for every statement in this document:
- **[static]**: read from the code.
- **[mock]**: shown with mocks, the fake driver or synthetic logs.
- **[history]**: from git history or docs.
- **[hw]**: observed on the car. Nothing in this document is [hw] yet.

---

## 1. Environment

| item | value |
| --- | --- |
| Repository | `20813fb` (HEAD, working tree clean at preflight) |
| Preflight machine | Linux, GCC, Python 3.14 + 3.8 (via `uv`); **no MinGW/MSVC** |
| Target car | Volvo V50 1.6D (D4164T, Bosch EDC16C31), P1, 2008 |
| Target adapter | VXDIAG SE / NANO, vendor J2534 v04.04 DLL (usually 32-bit) |
| Target PC | Windows (7 SP1 VIDA VM or Windows 10) — **to be filled in at test time** |
| Python on target | 3.8.x, **same bitness as the VXDIAG DLL** (32-bit for a 32-bit DLL) |

Fill in at test time: Windows version, Python version/bitness, VXDIAG DLL path,
file version and bitness, adapter firmware/DLL/API version (from H2-01), and
battery voltage.

## 2. Preflight findings (Phase 1)

### 2.1 Read-only commands usable on the car [static]

| command | bus | frames it sends (29-bit, to `0x0FFFFE` unless noted) | used in |
| --- | --- | --- | --- |
| `devices` | — | none (reads the registry only) | H2-01 |
| `j2534-test.exe <dll>` | 500k ISO15765 | `22F190`, `010C`, `3E00` to `0x7E0` (standard reads; the P1 ECM ignores OBD) | H2-01 |
| `read <key>` | 500k | `C D 11 A6 <id> 01` (A6 read, one id) | H2-03 |
| `monitor --params … --once` | 500k | A6 reads of the listed keys | H2-03 |
| `dtc` (**no** `--clear`) | 500k | `CB <comm> AE 1B` per Volvo module (ABS 01, ECM 11, CEM 50, DIM 51, ICM 54, BPM 7C) | H2-04 |
| `dump --ecu ECM --blocks FB,F5` | 500k | `CB 11 B9 FB`, `CB 11 B9 F5` | H2-02 |
| `dump --ecu DIM --blocks FB` | **125k** (from the profile) | `CB 51 B9 FB` | H2-05 |
| `config` | 500k | `CB 50 B9 FB`, `CB 50 B9 FC` (up to 4 attempts each) | H2-06 |
| `serve` (no flags) | 500k / 125k | A6 polls, AE 1B on the Codes tab, B9 FB/FC on the Configuration tab | H2-07/08 |

**Never** used in H2: `dtc --clear`, `dim-text`, `read-mem`, `discover` (it sweeps
the A6 id space), `verify`, `read <hex>`, `sniff`, and any `--enable-writes` /
`--allow-remote-writes`.

What happens to the modules on the *other* bus [static]:
- `dtc` and `identify` always open the 500k bus (`open_reader` takes no bus
  profile), so DIM/ICM/BPM simply time out there. That is expected, not a
  failure.
- The **only** CLI paths onto 125k are `dump --ecu DIM|ICM|BPM` (the bus is
  picked from `vehicle.yaml`) and the dashboard's bus selector.

### 2.2 Bus and protocol selection [static]

From `definitions/volvo/p1/vehicle.yaml` and `transport/j2534.py`
(`J2534CanLink`):

| step | 500k `hs` | 125k `ls` |
| --- | --- | --- |
| `PassThruConnect` protocol / flags / baud | `5` (CAN) / `CAN_29BIT_ID` / 500000 | `0x8004` (VXDIAG vendor ID) / `CAN_29BIT_ID` / 125000 |
| vendor `SET_CONFIG` | — | `0x8001 = 779` (bus selector) alone first, then `DATA_RATE=125000, LOOPBACK=0, BIT_SAMPLE_POINT=68` |
| pass-all filter `PASSTHRU_MSG.ProtocolID` | `5` | `0x8004` (changed in `b2577a5`; see below) |
| **`WriteMsgs` `PASSTHRU_MSG.ProtocolID`** | `5` | was **`5`** (hard-coded `CAN` at `20813fb`); **now `0x8004`** (channel protocol, §6) |
| framing | raw CAN; Volvo single/multi-frame done in Python, **not** ISO15765 | same |
| `ReadMsgs` | ProtocolID not checked | same |

### 2.3 The suspected 125k protocol-tag mismatch

> **Superseded by §6 (H2.1).** This is the preflight analysis as written
> before the history review. The README point below turned out to be invalid
> evidence, the mismatch is now classified **STRONGLY SUPPORTED**, and it is
> fixed (§6.5); hardware confirmation is still pending.

- **[static]** `J2534CanLink.send` builds every message with `self._t._make(CAN, …)`,
  where `_make` sets `ProtocolID = 5`. On a channel opened with `0x8004`, the
  message therefore carries the wrong channel protocol.
- **[mock]** Captured with a stubbed driver call: a 500k link writes
  `ProtocolID 0x0005`; **a 125k link also writes `0x0005`**.
- **[spec]** J2534-1 v04.04 says a message's `ProtocolID` must match the
  channel's, otherwise `PassThruWriteMsgs` / `PassThruStartMsgFilter` return
  `ERR_MSG_PROTOCOL_ID` (0x15). `0x8004` is a vendor-reserved ID, so whether
  VXDIAG applies that rule to it is driver-specific.
- **[history]** Commit `b2577a5` ("Fix low-speed CAN", 2026-08-02 19:55) records
  that **this driver rejected a pass filter tagged `5` on the `0x8004` channel**
  ("the connect fails"). That fix changed the filter only; no commit has changed
  `send()`.
- **[history]** The README lists DIM as read on the reference car, and the
  "low-speed dump" commit `4e8baf5` exists. But it is from 13:11, **before** the
  filter fix. Neither proves that the *current* code sends successfully on
  125k. Per the H2 brief, no earlier DIM success is assumed.

**Assessment:** likely but unproven. If VXDIAG validates write messages the way it
validated the filter, every 125k request fails at `WriteMsgs` with
`ERR_MSG_PROTOCOL_ID`, and **nothing reaches the bus**. That makes the failure
safe, but it breaks DIM/ICM/BPM reads and the dashboard's 125k view. If
VXDIAG does not check, the current code works. Only the adapter can say, and
H2-00/H2-05 are designed to find out without sending anything but reads
(§5).

### 2.4 Logging and diagnostics available

- **Proxy JSONL.** Every J2534 call: `connect.protocol`, `start_filter.mask.proto`,
  each `write`/`read` message's `proto` and `data`, and `result_name`. These
  are exactly the fields the 125k question needs. (`GET_CONFIG` and
  failed-read logging were fixed in `20813fb`.)
- **The toolkit can load the proxy directly.** `--library <proxy dll>` plus
  environment variables (`VOLVO_J2534_REAL_DLL`, `VOLVO_J2534_LOG_DIR`,
  `VOLVO_J2534_SESSION_TAG`) override any ini. **No registry change, no
  `install-proxy.ps1`, no file copied into VIDA/VXDIAG directories.**
- **`-v` / `-vv`** turns on toolkit logging to stderr.
- **Two helper scripts** in `scripts/`, covered by `tests/test_h2_tools.py` [mock]:
  - `h2_proto_check.py`: ProtocolID per channel, driver results, and an audit
    of **every service byte written**. Exit status 1 on anything that is not
    a read.
  - `h2_sanitize.py`: protocol-aware masking of the VIN, chassis, FYON, ECU
    serial blocks, user paths and device names (§4.4).

### 2.5 Windows build and DLL requirements [static]

- The proxy **must have the same bitness as the VXDIAG DLL**, and so must
  Python when the toolkit loads it. Check with
  `scripts\list-j2534.ps1`; most VXDIAG installs are x86.
- MSVC: `scripts\build-windows.ps1 -Arch Win32 -Test` (static CRT, runs on
  Windows 7 without extras). Output:
  `build-Win32\proxy\RelWithDebInfo\j2534proxy.dll`,
  `build-Win32\test-client\RelWithDebInfo\j2534-test.exe`,
  `build-Win32\fake-j2534\RelWithDebInfo\fake_j2534.dll`.
- MinGW (Linux): `./scripts/build-mingw.sh i686` needs the UCRT on Windows 7
  (KB2999226).

## 3. Windows build (Phase 2)

| check | result |
| --- | --- |
| Linux build of proxy + fake driver + test client | **PASS**: 0 warnings (`-Wall -Wextra`) [mock] |
| `ctest` (`proxy_core`) | **PASS** (1/1) [mock] |
| `pytest`, including ctypes → proxy → fake-driver integration | **PASS**: 217 passed + 51 subtests [mock] |
| `ruff check .` | **PASS** |
| ASan/UBSan/TSan proxy scenarios (in `20813fb` acceptance) | **PASS** [mock] |
| Windows branches type-check (clang, `-D_WIN32`, stub `windows.h`) | **PASS** (acceptance §9). **A static check, not a build.** |
| i686 ABI static asserts (`PASSTHRU_MSG` = 4152 B, `__stdcall`) | **PASS** (static) |
| **Windows DLL build** | **PENDING**: no MSVC/MinGW on this machine |
| Export check on the built DLL | **PENDING** |
| Self-load guard on Windows | **PENDING** (works on Linux: integration test `ProxySelfLoadTest`) |
| Architecture vs VIDA/VXDIAG | **PENDING** |

### 3.1 Windows commands (run on the target before any car test)

PowerShell, in the checkout, **not elevated**. None of these change the
registry or touch VIDA/VXDIAG files.

```powershell
# 0. Bitness of the registered drivers and of VIDA (read-only)
powershell -ExecutionPolicy Bypass -File scripts\list-j2534.ps1

# 1. Build (MSVC). -Test runs ctest + the test client against the FAKE driver.
.\scripts\build-windows.ps1 -Arch Win32 -Test        # use -Arch x64 only for an x64 VXDIAG DLL
$B = "build-Win32"
$PROXY = (Resolve-Path "$B\proxy\RelWithDebInfo\j2534proxy.dll").Path
$FAKE  = (Resolve-Path "$B\fake-j2534\RelWithDebInfo\fake_j2534.dll").Path
$TEST  = (Resolve-Path "$B\test-client\RelWithDebInfo\j2534-test.exe").Path

# 2. Architecture: must match the VXDIAG DLL from step 0 (14C = x86, 8664 = x64)
"{0:X}" -f [BitConverter]::ToUInt16([IO.File]::ReadAllBytes($PROXY), [BitConverter]::ToInt32([IO.File]::ReadAllBytes($PROXY), 0x3C) + 4)

# 3. Exports: expect the 14 PassThru* names (from a VS Developer prompt)
dumpbin /exports $PROXY | Select-String PassThru

# 4. Proxy -> FAKE driver, no hardware: Open/ReadVersion/READ_VBATT return 0
$env:VOLVO_J2534_REAL_DLL = $FAKE
$env:VOLVO_J2534_LOG_DIR  = "$PWD\h2-logs"
$env:VOLVO_J2534_SESSION_TAG = "h2-build-fake"
& $TEST $PROXY

# 5. Self-load guard: must print a non-zero PassThruOpen and the text
#    "real_dll points at the proxy itself" - and must NOT crash
$env:VOLVO_J2534_REAL_DLL = $PROXY
$env:VOLVO_J2534_SESSION_TAG = "h2-build-self"
& $TEST $PROXY

# 6. Python suite on the target interpreter (integration tests use build*\)
pip install pyyaml pytest
python -m pytest -q
Remove-Item Env:VOLVO_J2534_REAL_DLL, Env:VOLVO_J2534_SESSION_TAG
```

Pass criteria:
- step 1 green;
- step 2 matches the VXDIAG DLL;
- step 3 lists all 14 exports, undecorated (the `.def` file strips `_Name@N`);
- step 4 returns `0` for all three calls;
- step 5 returns non-zero with the self-load message and no crash;
- step 6 shows no failures (the integration tests may skip on Windows if the
  build dir isn't found).

## 4. Safety rules and log handling (Phase 3 preparation)

### 4.1 Rules (all tests)

1. **Human approval per test.** Run tests one at a time, in order, with a
   person at the car. Stop on anything unexpected.
2. **Never pass** `--enable-writes`, `--allow-remote-writes`, `--clear`, and
   never use `dim-text`, `read-mem`, `discover`, `verify`, `read <hex>`.
3. **VIDA is closed** during the toolkit tests. One application per adapter.
4. **Car state:** parked, handbrake on, ignition position II, engine off unless
   a test says "engine at idle". A battery charger or maintainer is
   recommended; abort below 12.0 V (engine off).
5. **Every toolkit call goes through the proxy** (§4.2), so the frames sent
   are independently auditable.
6. **After every test**, run `python scripts\h2_proto_check.py <log>`.
   - Exit status **1** (`VERDICT: UNSAFE`): stop H2 and investigate before
     anything else.
   - Exit status **3** (`VERDICT: INCONCLUSIVE`): the audit could not
     classify something. Stop the test and inspect it before continuing; H2
     only sends Volvo single frames and ISO15765 reads, so this is unexpected.

**Stop conditions** — stop the current test and do not continue H2 until
investigated:
- a warning lamp or message appears or changes on the cluster;
- the cluster or radio resets, flickers, chimes or reboots;
- a module behaves differently afterwards;
- an unexpected J2534 error, a process crash or a hang;
- `NOT A READ SERVICE` in the audit;
- a write to a channel or ID you did not expect.

### 4.2 Session setup (once per PowerShell window)

```powershell
$env:PYTHONPATH = "python"
$VX    = "<path to the VXDIAG J2534 DLL, from list-j2534.ps1>"
$PROXY = (Resolve-Path "build-Win32\proxy\RelWithDebInfo\j2534proxy.dll").Path
$TEST  = (Resolve-Path "build-Win32\test-client\RelWithDebInfo\j2534-test.exe").Path
$env:VOLVO_J2534_REAL_DLL = $VX
$env:VOLVO_J2534_LOG_DIR  = "$PWD\h2-logs"
New-Item -ItemType Directory -Force h2-logs | Out-Null
Add-Content .git\info\exclude "h2-logs/"     # local-only ignore: logs never get staged
function H2([string]$tag) { $env:VOLVO_J2534_SESSION_TAG = $tag }   # one log per test
```

Each test also writes its console output to `h2-logs\<test>.txt` via
`Tee-Object`. **The JSONL logs, `dump-*.json` files and console output contain
the VIN.** Never commit them (`*.jsonl` and `/logs/` are already gitignored).

### 4.3 Frame and ProtocolID audit — `scripts/h2_proto_check.py`

```powershell
python scripts\h2_proto_check.py (Get-ChildItem h2-logs\*h2-05*.jsonl).FullName
```

The script prints:
- every `connect` (protocol, baud, result);
- a table of filter, write and read counts per channel, with the message
  `ProtocolID` and driver result, where `<-- MISMATCH` marks a message whose
  tag differs from its channel;
- every service byte written, **including writes on channels whose connect is
  missing from the log**;
- `INCOMPLETE:` lines;
- a `VERDICT`.

| exit | verdict | when |
| --- | --- | --- |
| 0 | only read services written | every write is a read from the allowlist, and the log is complete |
| 1 | **UNSAFE** | a write carried a service outside the read allowlist |
| 3 | **INCONCLUSIVE** | any of: a write with no service byte at a known position (ISO-TP first/consecutive/flow-control frames or broadcast frames on a raw CAN channel; these are *unclassified*, never guessed as a service); records on a channel with no logged `connect` (a capture starting mid-session); unreadable lines; no successful connect at all |

The service byte is read from `[C8+n][comm][service]` for a Volvo single frame
on a raw CAN channel (5 / 0x8004), and from the first byte of an ISO15765
message (the second with extended addressing, `TxFlags & 0x80`).

### 4.4 Sanitizing before sharing — `scripts/h2_sanitize.py`

```powershell
python scripts\h2_sanitize.py --vin <VIN> --out h2-logs\clean `
    (Get-ChildItem h2-logs\*.jsonl, h2-logs\*.txt, h2-logs\*.json).FullName
# extra identifiers (e.g. an adapter serial seen on the label / VX Manager):
#   add  --mask <string>  (repeatable)
```

What it masks:

| identifier | where | how |
| --- | --- | --- |
| VIN, chassis (last 6 characters) | everywhere | given by `--vin` |
| FYON, Package identity, Car configuration document number, Chassis, "… serial" values | console transcripts (`config`, `dump` output) | **auto-collected** from labelled lines, then masked in every file |
| the same identifiers inside CAN data | proxy JSONL | matched on each CAN id's **reassembled payload stream**, so they are found even when split across frames (including a single byte at a frame boundary) |
| ECU / BCM serial numbers | B9 answers for blocks `F8`/`E8` on `0x40xxxx`/`0x60xxxx` | whole block's data bytes |
| immobiliser PIN / security code | B9 answers for blocks `01`/`02` (never read by the toolkit; may appear in VIDA captures) | whole block's data bytes |
| ECU serial / VIN over UDS | ISO15765 `62 F18C` / `62 F190` answers | data bytes after the DID |
| user names in paths | `session.real_dll`, `config`, `dll`, dump `reader` | `C:\Users\<user>\…`, `/home/<user>/…` |
| device name passed to `PassThruOpen` | `open.name` | `<device>` |
| adapter serial | not returned by any J2534 call the toolkit makes | pass it with `--mask` if it appears anywhere |

What it preserves for debugging:
- CAN ids, and each frame's first byte (length marker / frame control);
- the B9 answer headers `[ctrl][comm][F9][block]`;
- protocol numbers, results, timing, and the request frames;
- message lengths. Masked bytes become `58` ('X'), so hex stays valid and
  offsets don't move.

Adapter firmware/DLL/API versions are kept: they are model versions, not
serials.

The script **masks known identifiers only and never reports a file as
"clean"**. Per file it prints either `RESIDUAL [...]` (exit 1) or
`no known identifier left (unknown ones may remain)`. It then lists what it
passed through unmasked (`B9 blocks ['F5', 'FB', …]`, `UDS DIDs [...]`) so you
can judge those yourself. A numeric identifier can be reported as residual by
coincidence (the same digits inside hex). Review the output in any case.

Share only `h2-logs\clean\`. Mocked validation: `tests/test_h2_tools.py`,
which covers the 1-byte split, serial and immobiliser blocks, the UDS
serial, labelled FYON, a numeric chassis inside hex, a dump JSON, user paths,
and the no-"clean" reporting. The earlier inline regex
version leaked a VIN byte at a 1-byte frame boundary; that is fixed here.

## 5. Test matrix and procedures (Phase 3, AWAITING APPROVAL)

Record for every test, in §8:
- the command;
- adapter firmware, DLL and API version (from H2-01);
- module, bus and baud;
- the sanitized request/response frames (from the proxy log);
- the decoded result;
- the duration (`Measure-Command`);
- any error or timeout text;
- the `h2_proto_check` output;
- PASS / FAIL / INCONCLUSIVE.

### H2-00 — Existing VIDA 125k capture (no car, no adapter)

**H2.1 result: NOT RUN — the capture is not available.** See §6. No `.jsonl`
log exists on the analysis machine, in git history or in any branch; captures
are gitignored by design. If the 2026-08-02 VIDA log (the DIM/ICM
configuration "write-clock" session) is found, run:

```powershell
python scripts\h2_proto_check.py <path to the 2026-08-02 VIDA *.jsonl>
```

Read the `125000 0x8004 write` row:
- **`msg proto 0x8004`** → VIDA tags writes with the channel protocol. Together
  with the filter rejection, this makes the §6 fix **CONFIRMED** at the API
  level.
- **`0x0005` with `STATUS_NOERROR`** → the driver accepts mixed tags for
  writes. Revert the §6 fix and re-assess.

Also look at whether `read` rows on the same channel carry `0x400051`-type
answers after those writes, i.e. whether the ECU answered and not just the API.
The audit **will** show `B8` writes: those are VIDA's own configuration changes
from that session, expected in this file only.

### H2-01 — Detect the adapter and open a connection

Engine off, ignition II, VIDA closed.

```powershell
H2 "h2-01"
python -m volvo_diag devices | Tee-Object h2-logs\h2-01-devices.txt
& $TEST $PROXY | Tee-Object h2-logs\h2-01-open.txt       # proxy -> VXDIAG
```

- **Sends:** `22F190`, `010C`, `3E00` on 500k ISO15765 to `0x7E0`. These are
  standard reads; the P1 ECM normally does not answer them.
- **Expected:** `devices` lists the VXDIAG entry. `PassThruOpen`,
  `PassThruReadVersion`, `READ_VBATT` and `PassThruConnect` return `0`, with
  firmware/DLL/API versions printed and a plausible battery voltage (12–14.8 V).
  The three requests may time out; that is not a failure.
- **Failure indicators:**
  - any non-zero return from Open/Connect;
  - `LoadLibrary failed` (a bitness mismatch);
  - "real_dll is not configured" (`$env:VOLVO_J2534_REAL_DLL` not set);
  - a battery voltage of 0.
- **Record:** the versions; they are the adapter identification for every
  later test.

### H2-02 — ECM identification (500k)

```powershell
H2 "h2-02"
Measure-Command { python -m volvo_diag --transport j2534 --library $PROXY dump --ecu ECM --blocks FB,F5 --out h2-logs\h2-02-ecm.json | Tee-Object h2-logs\h2-02.txt }
```

- **Sends:** `CB 11 B9 FB`, `CB 11 B9 F5`.
- **Expected:** `0xFB: N bytes`; the `0xF5` part numbers
  (`part 3xxxxxxx rev …`); "wrote … VIN YV1…".
- **Failure indicators:**
  - `no answer to block 11/0xFB`;
  - `0 bytes`;
  - an `incomplete block`;
  - a VIN that doesn't match the car.
- **Cross-check:** the VIN against the car's plate. Part numbers against VIDA,
  if available.

### H2-03 — Known ECM parameters (500k)

```powershell
H2 "h2-03"
python -m volvo_diag --transport j2534 --library $PROXY read coolant_temperature | Tee-Object h2-logs\h2-03a.txt
python -m volvo_diag --transport j2534 --library $PROXY monitor --once --no-group --params rpm,coolant_temperature,boost_actual,boost_requested,dpf_differential_pressure,exhaust_temperature,fuel_rail_pressure,battery_voltage | Tee-Object h2-logs\h2-03b.txt
# engine at idle (parked, handbrake on), then repeat the monitor line
```

- **Sends:** A6 reads `CD 11 A6 00 05 01` …, one per key. All ids are
  `verified-against-vida` except `rpm` and `battery_voltage` (`verified`).
- **Expected, engine off:** rpm 0, coolant ≈ ambient/engine temperature,
  boost ≈ barometric (~100 kPa), battery voltage within 0.3 V of H2-01.
- **Expected, at idle:** rpm ≈ 800–900, rail pressure ≈ 250–350 bar, boost
  ≈ barometric.
- **Failure indicators:**
  - `-- (no answer)` on a `verified-against-vida` id;
  - physically impossible values;
  - a disagreement with VIDA or the cluster (coolant, rpm) beyond rounding.

### H2-04 — DTC read without clearing (500k)

```powershell
H2 "h2-04"
python -m volvo_diag --transport j2534 --library $PROXY dtc | Tee-Object h2-logs\h2-04.txt
```

- **Sends:** `CB <comm> AE 1B` for ABS 01, ECM 11, CEM 50, DIM 51, ICM 54,
  BPM 7C. **No `AF`** (verify in the audit).
- **Expected:**
  - ABS/ECM/CEM show `no codes` or a list of 4-digit codes with text;
  - DIM/ICM/BPM show `no DTC list answer` / `no answer`, because they are not
    on 500k;
  - the summary line reads `N active code(s) total`, with no
    "clear requested".
- **Failure indicators:**
  - a 500k module erroring while VIDA shows it present;
  - codes that differ from VIDA's list;
  - any `AF` in the audit, which means **stop H2**.

### H2-05 — DIM identification on 125k (confirms the §6 fix)

```powershell
H2 "h2-05"
Measure-Command { python -m volvo_diag --transport j2534 --library $PROXY -v dump --ecu DIM --blocks FB --out h2-logs\h2-05-dim.json | Tee-Object h2-logs\h2-05.txt }
python scripts\h2_proto_check.py (Get-ChildItem h2-logs\*h2-05*.jsonl).FullName
```

- **Sends:** the vendor selector `0x8001=779` and timing config, a pass filter
  tagged `0x8004`, then `CB 51 B9 FB`. **With the §6 fix** the write is
  tagged `0x8004`.
- **Outcomes** with the fixed code (all are read-only):

  | outcome | console | proto check | meaning |
  | --- | --- | --- | --- |
  | **B (expected)** | `0xFB: N bytes`, `wrote …` | `125000 0x8004 write 0x8004 STATUS_NOERROR`, and `read` rows with answers from `0x40xxxx` | **PASS.** The DIM answered on 125k; the fix works on the car. |
  | **A′** | `0xFB: PassThruWriteMsgs failed: ERR_MSG_PROTOCOL_ID …` | `write 0x8004 ERR_MSG_PROTOCOL_ID` | The driver wants something else on this channel. FAIL. Stop and compare with the VIDA capture; do not try other tags on the car. |
  | **C** | `0xFB: no answer to block 51/0xFB` | `write 0x8004 STATUS_NOERROR`, no `0x40xxxx` reads | The API accepted the write but nothing answered. INCONCLUSIVE: a successful API call is not proof the ECU got the request. Check the ignition, the `ioctl SET_CONFIG` results (bus selector), and the connect/filter results. |

- **Also check** in the log:
  - `connect protocol=0x8004 baud=125000 -> STATUS_NOERROR`;
  - both `ioctl SET_CONFIG` results;
  - the pass filter `0x8004 STATUS_NOERROR`;
  - no `MISMATCH` rows.

### H2-06 — CEM identification (500k)

```powershell
H2 "h2-06"
python -m volvo_diag --transport j2534 --library $PROXY config | Tee-Object h2-logs\h2-06.txt
```

- **Sends:** `CB 50 B9 FB` and `CB 50 B9 FC`, up to 4 attempts each.
- **Expected:** the identity (VIN, chassis, market code, structure week), the
  car configuration with ~99 options, and plausible decodes for this car:
  **diesel**, manual gearbox, DPF fitted.
- **Failure indicators:**
  - "incomplete block" on every attempt;
  - a decode that contradicts the car (the known dropped-frame symptom is
    "Fuel: Petrol" on this diesel);
  - no answer.

### H2-07 — Dashboard polling

```powershell
H2 "h2-07"
python -m volvo_diag --transport j2534 --library $PROXY serve --host 127.0.0.1 --port 8080
# second window:
Invoke-RestMethod http://127.0.0.1:8080/meta     # writes must be False
```

In the browser at `http://127.0.0.1:8080/`:
1. Tick rpm, coolant, boost, battery voltage and watch for 2 minutes (engine
   off, then idle).
2. Open **Codes**. There must be **no Clear button**, and the note "Clearing is
   a write…" must appear if codes are present.
3. Open **Configuration**: the CEM identity appears.
4. **Only if H2-05 = B:** switch the bus to **125k** and tick
   `engine_speed`, `engine_coolant_temperature`, `total_fuel_level` (DIM,
   `verified`).

- **Expected:**
  - `/meta` shows `writes: False`;
  - values update every ~0.2–0.5 s;
  - the header shows the cycle time and few or no timeouts;
  - the Codes and Configuration tabs agree with H2-04/H2-06;
  - on 125k, the DIM values agree with the cluster.
- **Failure indicators:**
  - values freeze;
  - the cycle time grows unbounded;
  - the server or process crashes (note the VXDIAG FIFO crash in
    `troubleshooting.md`);
  - a Clear button is visible;
  - any non-read service in the audit.
- If H2-05 is not B, skip step 4 and mark it NOT RUN.

## 5A. Optional stress tests (separate approval; not part of the smoke test)

These deliberately disturb the adapter or the driver. They don't change the
vehicle state, but the VXDIAG driver has crashed the host process before
(`troubleshooting.md`). Run them only after H2-01 … H2-07 have passed and
been recorded, with **separate approval**, and stop on the first failure. A
failure here says nothing about the read path's correctness.

### S-01 — USB disconnect and reconnect

Engine off. The dashboard from H2-07 is polling the 500k parameters.

1. Unplug the **USB** cable of the VXDIAG. Leave it on the OBD socket.
2. Wait 10 s and watch the page and the console.
3. Plug it back in and wait 10 s.
4. Switch the bus away and back (500k → 125k → 500k), which reopens the
   link. If H2-05 was not B, use **Ctrl-C** and restart `serve` instead.
5. Ctrl-C; confirm the process exits within a few seconds.

- **Expected:**
  - rows turn dimmed or `—` with an error while unplugged;
  - no hang, and the HTTP server stays responsive;
  - after the reopen or restart, values resume;
  - Ctrl-C exits cleanly.
- **Failure indicators:**
  - the process hangs and needs killing;
  - a crash inside the driver;
  - no recovery after a reopen or restart;
  - the adapter needs a power cycle.
- **Note:** the toolkit has no automatic reconnect [static]. Recovery by bus
  switch or restart is the designed behaviour. Record what you see.

## 6. 125k CAN investigation (Phase 4 / H2.1)

### 6.1 Historical evidence search

- **Searched:** the whole local filesystem (excluding `/proc`, `/sys`) for
  `*.jsonl` and for VIDA/VXDIAG/J2534 capture names; git history (all
  branches, all added files); stashes; mounted shares.
- **Found:** no proxy capture of any kind. Captures are gitignored on purpose
  (`*.jsonl`, `/logs/`, because they contain the VIN), so the 2026-08-02 VIDA
  log exists, if at all, only on the VIDA machine.
- **Usable instead:** commit messages, code comments and docs written while
  those captures were analysed.

### 6.2 Timeline from git history

| time (2026, +0200) | commit | 125k state of `J2534CanLink` | evidence |
| --- | --- | --- | --- |
| 08-01 19:14 | `226d4f4` Add DIM reading | **no 125k channel at all**: connect `CAN (5)` at `--baudrate`; filter and writes tagged 5 | definitions only (`dim.yaml` "read with --baudrate 125000"). Without the bus selector no cabin module answers (code comment in `0252f58`), so this is not evidence of a working DIM read |
| 08-01 23:46 | `0252f58` Try to fix config reading | connect **`0x8004`** + bus selector 779 + sample point 68, "reverse-engineered from a VIDA session"; **filter and writes still tagged 5** | first 125k channel |
| 08-02 12:35 | `6714ae4` README | — | README starts listing "ECM, ABS, CEM, **DIM**" for the reference car |
| 08-02 19:55 | `b2577a5` Fix low-speed CAN | **filter → `0x8004`**; **writes unchanged (5)** | comment: "*on the 125k low-speed channel (0x8004) a filter tagged plain CAN (5) is rejected and the connect fails, which is why the low-speed bus switch used to error out. VIDA tags it with the channel protocol, so mirror that.*" |
| 08-02 20:19 | `7cfddb1` DTS codes in dashboard | — | "*CEM … on the 125k bus it stays silent (confirmed in the **write-clock capture**)*": the VIDA 125k capture existed and was analysed |
| 11-23 | `20813fb` | writes still 5 | audit flags it as a risk |

### 6.3 Per-operation ProtocolID on the 125k channel

| operation | VIDA (reference) | toolkit `0252f58` | toolkit `b2577a5`…`20813fb` | toolkit now (H2.1 fix) | source |
| --- | --- | --- | --- | --- | --- |
| `PassThruConnect` | `0x8004`, 125000 | `0x8004` | `0x8004` | `0x8004` | [history] `0252f58` |
| `SET_CONFIG` | `0x8001=779` alone, then rate/loopback/sample point | same | same | same | [history] `0252f58` |
| `StartMsgFilter` (pass) | **`0x8004`** | 5 → **rejected by the VXDIAG**, connect fails | `0x8004` | `0x8004` | [history] `b2577a5` (observed on hardware by the author) |
| `WriteMsgs` | **not recorded** in any surviving text | 5 (never reached: filter failed) | **5** | **`0x8004`** | [static] |
| `ReadMsgs` | n/a (the driver tags received messages) | — | ProtocolID not checked | not checked | [static] |
| write accepted, and did the ECU answer? | yes: VIDA read and wrote DIM/ICM blocks on 125k | no (connect failed) | **no record of any toolkit 125k read after `b2577a5`** | unknown: needs H2-05 | [history] |

### 6.4 Assessment: **STRONGLY SUPPORTED** (not CONFIRMED)

For the defect:
1. **[hw/history]** The VXDIAG driver *does* check that a message's
   ProtocolID matches the channel's on `0x8004`. It rejected a `5`-tagged
   filter there (`b2577a5`).
2. **[history]** VIDA, the only application known to work on this channel,
   tags its 125k messages with `0x8004` (its filter, per `b2577a5`).
3. **[spec]** J2534-1 v04.04 requires message and channel ProtocolID to
   match for `WriteMsgs` as for filters (`ERR_MSG_PROTOCOL_ID`).
4. **[history]** Nothing in the repository shows a toolkit write tagged `5`
   ever succeeding on `0x8004`:
   - every DIM/125k success claim predates the point where the link could
     even open;
   - the README's DIM claim (12:35) predates the filter fix (19:55);
   - "Add DIM reading" predates the 125k channel itself.

   The audit's earlier "the README reports DIM reads working" was therefore
   **not valid counter-evidence**.

Against, or missing:
- No observed `WriteMsgs` result on `0x8004` with either tag. VIDA's write
  tag is inferred, not recorded.
- It is possible in principle that VXDIAG checks filters but not writes.

Not CONFIRMED, because confirming needs H2-00 (VIDA's recorded write tag) or
H2-05.

### 6.5 Fix (committed; not yet confirmed on hardware)

The evidence is sufficient for the smallest correction. On 500k it is a no-op,
because `self.protocol == CAN`. Frame bytes, framing, requests and timing are
unchanged; only the API tag moves from 5 to the channel's protocol.

```diff
--- a/python/volvo_diag/transport/j2534.py   (J2534CanLink.send)
-        message = self._t._make(CAN, flags, can_id, data)
+        message = self._t._make(self.protocol, flags, can_id, data)
```

Why fixing before the car run is the safer order:
- With the old tag, the likely outcome is `ERR_MSG_PROTOCOL_ID`, with nothing
  on the bus.
- With the new tag, the same read-only frames VIDA sends reach the DIM.
- If the fix is wrong, H2-05 shows outcome A′ (an API error) or C, again with
  nothing harmful sent.

Consequences to be aware of:
- **`dim-text`** (an experimental write, gated by `--enable-writes`) uses the
  same `send()`. On 125k it may now actually reach the bus where it
  previously failed at the API. That is not a policy change (it is still
  gated) but a functional one. It stays out of H2.
- `dump --ecu DIM|ICM|BPM`, the dashboard's 125k bus and the Codes tab on 125k
  all use this path.

### 6.6 Regression tests

| test | what it pins | on `20813fb` code | now |
| --- | --- | --- | --- |
| `J2534ProtocolTagTest.test_low_speed_channel_tags_with_its_protocol` | a 125k link writes `ProtocolID 0x8004`, with the frame bytes (id + 8 Volvo bytes) unchanged | **FAIL** (5) | pass |
| `J2534ProtocolTagTest.test_high_speed_channel_still_tags_can` | a 500k link still writes 5 | pass | pass |
| `LowSpeedChannelIntegrationTest.test_low_speed_link_opens_and_reads` | proxy → fake driver, opened exactly like the `ls` bus (0x8004, selector, sample point): an A6 read succeeds | **FAIL** (`ERR_MSG_PROTOCOL_ID`) | pass |
| `LowSpeedChannelIntegrationTest.test_high_speed_link_unchanged` | 500k end to end still reads | pass | pass |
| `LowSpeedChannelIntegrationTest.test_fake_enforces_protocol_like_vxdiag` | a 5-tagged write on 0x8004 is refused with `ERR_MSG_PROTOCOL_ID` | pass | pass |

The fake driver now accepts `0x8004` and enforces the J2534-1 ProtocolID match
on `WriteMsgs` and filters, as the VXDIAG did for the filter. That makes the
fake **model the observed rule**. The integration failure on old code is a
reproduction by construction, **not new evidence** about the adapter.

### 6.7 Hardware observation still required

**H2-05 outcome B**, recorded in the proxy log:
- `connect 0x8004 → STATUS_NOERROR`;
- the pass filter `0x8004 → STATUS_NOERROR`;
- `write 0x8004 → STATUS_NOERROR`;
- **and** `read` messages from `0x40xxxx` carrying `[ctrl] 51 F9 FB …`, i.e.
  the DIM's own answer, not just an accepted API call.

The DIM block decoding must also be sane (a non-empty block; part
numbers/VIN consistent with the car). Then run H2-07 step 4 (live DIM values
match the cluster), and H2-02/H2-03 as the 500k regression check.

Optionally, and without the car: H2-00 on the VIDA capture, if it can be
recovered from the VIDA machine.

## 7. Rollback

Nothing in H2 is installed, registered or written to the car, so rollback is
simple:

0. **Code:** if H2-05 shows the fix is wrong (outcome A′), restore the one
   line in `J2534CanLink.send` (`self._t._make(CAN, …)`), together with the
   matching tests and fake-driver checks. Do it in a new commit; never rewrite
   history. Re-run the tests.

1. Ctrl-C any toolkit process. Unplug the adapter USB if a process hangs.
2. Close PowerShell, or `Remove-Item Env:VOLVO_J2534_*`. The proxy
   configuration lived only in environment variables.
3. Delete `build-Win32\` if desired. **No registry entry was created**
   (`install-proxy.ps1` is not used in H2) and no VIDA/VXDIAG file was copied
   or replaced.
4. If `install-proxy.ps1` was run anyway: `scripts\remove-proxy.ps1`
   (elevated) restores the registry. In a VIDA VM, revert the snapshot taken
   beforehand.
5. Vehicle: only read services are sent, so there is no vehicle state to roll
   back. If a cluster or module behaved oddly (a stop condition), switch the
   ignition off, remove the key for 2 minutes, then check in VIDA before any
   further test.
6. If an `NOT A READ SERVICE` line ever appears, keep the log unmodified,
   check the module states and DTCs in VIDA, and report it as a safety
   incident. H2 is then **REJECTED** until it is explained.

## 8. Results (Phase 5)

| test | status | adapter fw/dll/api | module / bus | duration | result / notes |
| --- | --- | --- | --- | --- | --- |
| H2-00 VIDA 125k capture | NOT RUN | — | — | — | capture not available (H2.1 search, §6.1) |
| H2-01 adapter open | NOT RUN | | — / 500k | | |
| H2-02 ECM identification | NOT RUN | | ECM 0x11 / 500k | | |
| H2-03 ECM parameters | NOT RUN | | ECM 0x11 / 500k | | |
| H2-04 DTC read | NOT RUN | | all / 500k | | |
| H2-05 DIM identification | NOT RUN | | DIM 0x51 / 125k | | outcome B / A′ / C (with the §6 fix) |
| H2-06 CEM identification | NOT RUN | | CEM 0x50 / 500k | | |
| H2-07 dashboard | NOT RUN | | ECM / 500k (+ DIM / 125k) | | |
| S-01 USB disconnect/reconnect (optional, §5A) | NOT RUN | | ECM / 500k | | separate approval |
| Windows build (§3.1) | PENDING | — | — | — | |

**Verified hardware behaviour:** none.
**Static analysis:** §2, §6.
**Mocked results:** §2.3, §3, the helper validation in §4.
**Assumptions:**
- the VXDIAG DLL is 32-bit;
- the DIM answers `B9 FB` on 125k (VIDA did read DIM blocks there; see
  `volvo-protocol.md`);
- the ignition must be in position II for the cabin modules.

Regressions discovered: no hardware run, so none on the car. The review
found:
- a Windows-only **test** defect from `20813fb`: `ProxySelfLoadTest` loaded
  the proxy with `ctypes.CDLL`, which unbalances the stack against
  `__stdcall` on 32-bit Windows. It now uses `WinDLL` there;
- the earlier inline sanitizer leaked a VIN byte at a 1-byte frame boundary
  (§4.4).

Code changes (commit `fix(j2534): correct low-speed CAN protocol tagging`):
- **Runtime:** `python/volvo_diag/transport/j2534.py`: `J2534CanLink.send`
  tags messages with the channel protocol (§6.5).
- **Test infrastructure:**
  - `fake-j2534/fake_driver.cpp` accepts the `0x8004` channel and enforces
    the ProtocolID match;
  - `tests/test_integration.py` adds `LowSpeedChannelIntegrationTest` and the
    `WinDLL` fix;
  - `tests/test_transport_hardening.py` adds `J2534ProtocolTagTest`.
- **Tools:** `scripts/h2_proto_check.py`, `scripts/h2_sanitize.py`, covered by
  `tests/test_h2_tools.py`.
- **Docs:** this file.

Verification (Linux, final, H2.2):
- the C++ build has 0 warnings, and `ctest` passes 1/1;
- `pytest` 238 passed + 51 subtests on Python 3.14, and 238 passed on 3.8;
- J2534 integration (`tests/test_integration.py`, proxy → fake driver)
  passes 18/18;
- `ruff` clean;
- `mypy` clean on the two scripts; the package has 50 errors, the same as
  `20813fb`, none new;
- `git diff --check` clean;
- the proxy is unchanged since the `20813fb` sanitizer runs.

## 9. Remaining risks

1. **125k `WriteMsgs` ProtocolID**: fixed on strong but indirect evidence
   (§6). Unconfirmed on hardware until H2-05 = B. The fix also lets the gated
   `dim-text` reach the 125k bus.
2. **Windows DLL** not built here, so the self-load guard and exports are
   unverified on Windows (§3).
3. **VXDIAG driver robustness** under disconnect (optional S-01, §5A). The driver has
   crashed the process before (`troubleshooting.md`).
4. **Remaining audit items** (`docs/audit.md` §5/§9): SocketCAN multi-block
   send, the proxy's `WriteMsgs` rendering with a bad caller count, the
   loader-lock hang at exit. None is exercised by H2 except the loader lock,
   which matters only when VIDA exits with the proxy loaded.

## 10. Next steps

1. Try to recover the 2026-08-02 VIDA capture from the VIDA machine and run
   H2-00 (files only).
2. On the Windows machine: §3.1 build and checks. Record the results in §3.
3. With approval, H2-01 → H2-07 in order, stopping on any stop condition.
   S-01 (§5A) only afterwards, with separate approval.
   Record in §8.
4. The §6 fix is committed but **not hardware-confirmed**. H2-05 = B (or
   H2-00 showing VIDA writes tagged `0x8004`) confirms it. If H2-05 = A′, roll
   back (§7 step 0) and compare with the VIDA capture.
5. Update the verdict:
   - **ACCEPTED** if all tests pass, including H2-05 (before or after the fix);
   - **ACCEPTED WITH LIMITATIONS** if some rows remain NOT RUN with reasons;
   - **REJECTED** on any non-read service or other safety finding.
