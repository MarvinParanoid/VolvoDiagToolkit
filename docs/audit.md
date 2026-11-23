# Technical audit & hardening — 2026-11-23

Scope: the whole repository at `9a5884c` (Python client, transports, protocol,
dashboard, C++ J2534 proxy, fake driver, test client, tests, CI, docs).
No command was sent to a real vehicle or adapter during this work; everything
was exercised against mocks, fake serial ports, the fake J2534 driver and
sanitizer harnesses.

## 1. Executive summary

The code base is in good shape for an experimental tool. The protocol framing
is careful and well documented, the A6/B9 reassembly handles dropped frames,
the J2534 layer cleans up after a failed open, and the existing suite (159
tests, C++ core test, end-to-end proxy test) was green. Its weak spots were at
the **safety boundary**: the places where a payload the user typed, or a
browser request, reaches the bus.

- **P0:** `read <hex>` (documented as "raw UDS request, any hex") sent *any*
  payload with no gate. That includes `14FFFFFF`/`04` (clear DTCs), `11 xx`
  (ECU reset), `27 xx` (security access), `2E` (write) and `31` (routine).
  `--enable-writes` was not consulted.
- **P1:** `read-mem` used a four-entry *denylist*, so IO control (`B0`/`B1`),
  routines (`B2`) and periodic transmission (`A8`) went through. The dashboard
  had no protection against cross-site requests or DNS rebinding, and no
  restriction on network clients, so with `--enable-writes` any web page or
  LAN host could clear codes. A web clear *always* crashed **after** sending
  `AF 11` and reported the successful write as an error. The ELM327 raw link
  ignored every AT reply, including the one that sets the request CAN id.
- **P2:**
  - Python transport/protocol: serial-port leaks on failed ELM init; an
    ELM-OBD responsePending path that re-sent the request; "no codes"
    reported for modules that answered negatively.
  - Dashboard server: a stalled client could hang it.
  - C++ proxy (found with ASan/TSan harnesses): an overread of the caller's
    buffer, a data race on the error string, wrong `GET_CONFIG` logging, and
    infinite recursion if the proxy is configured to load itself.

All P0/P1 and the straightforward P2 defects are fixed, each with a
regression test, and each test was confirmed to fail on the original code.
The docs now describe the write-capable operations accurately.

| priority | found | fixed | left (documented) |
| --- | ---: | ---: | ---: |
| P0 | 1 | 1 | 0 |
| P1 | 5 | 5 | 0 |
| P2 | 11 | 10 | 1 |
| P3 | 16 | 7 | 9 |

(Counts include the acceptance review in §9, which fixed P3-7 and two
compatibility/reporting gaps in the first round's own changes.)

Five further **potential risks** (§5) need hardware or design input and are
not counted as defects.

## 2. Audit scope and method

| area | files | how |
| --- | --- | --- |
| Protocol | `protocol/{volvo,isotp,uds,obd}.py`, `volvo/*.py` | read; captured-frame tests |
| Transports | `transport/{base,j2534,socketcan,elm_can,elm_obd,volvo_ecm}.py` | read; fake serial ports with scripted AT replies, mocked links |
| Safety boundary | `cli.py`, `backend.py`, `web.py`, definitions loader | traced every path that transmits; mocked transports that fail the test if opened |
| Web API | `web.py` | real `HTTPServer` on an ephemeral loopback port with a recording backend |
| C++ proxy | `proxy/src/*`, `fake-j2534/`, `test-client/` | read; ASan+UBSan and TSan builds; a misbehaving "spec driver" plus a `dlopen` harness (out of tree, in a scratch directory) |
| Tests/CI/docs | `tests/`, `.github/workflows/tests.yml`, `docs/`, `README.md` | read; ran everything locally, also on Python 3.8 |

### Initial hypotheses

| # | hypothesis | verdict |
| --- | --- | --- |
| 1 | Web DTC clearing relies on client-side confirmation | **Partly rejected.** `VolvoBackend.clear_dtcs` did check `--enable-writes` server-side. But the HTTP layer did not, any page could forge the request (no CSRF/Origin/Host checks), and the write path itself crashed (P1-4). |
| 2 | Network-exposed dashboard lacks access control | **Confirmed** (P1-3). No authentication, `--host 0.0.0.0` documented, clear accepted from any client. |
| 3 | Raw requests bypass write restrictions | **Confirmed** (P0-1, P1-1, P2-1). |
| 4 | ELM327 init does not validate AT responses | **Confirmed** (P1-5). Raw link: never checked. OBD: logged a warning and carried on. |
| 5 | ELM327 init errors leak serial resources | **Confirmed** (P2-2), in both ELM transports and their callers. |
| 6 | CLI DTC clearing differs from docs | **Confirmed, minor** (P3-4). The README said "confirmed each time" but the CLI does not prompt. `--clear` was silently ignored on the UDS path. A failed re-read aborted the sweep (P2-5). |
| 7 | Docs misdescribe write-capable operations | **Confirmed** (P3-4). `dim-text` is a write behind the same flag but was undocumented as such. `AF` was listed as "reference only". |

## 3. Findings

Line numbers refer to the audited commit `9a5884c`.

### Fixed

| id | pri | finding | evidence (file:line @9a5884c) | fix |
| --- | --- | --- | --- | --- |
| P0-1 | P0 | `read <hex>` transmits any UDS/OBD payload: clear (`14`, `04`), reset (`11`), session (`10`), security (`27`), write (`2E`/`3B`), IO (`2F`), routine (`31`). Not gated by anything. | `python/volvo_diag/cli.py:309` | New allowlist `safety.check_uds_read` (read services only). Refused before the transport is built; `--enable-writes` does **not** lift it. Bad hex is a clean error instead of a traceback. |
| P1-1 | P1 | `read-mem` guard is a denylist `{B8,BA,AF,A3}`. `B0`/`B1` IO control, `B2` routine, `A8` periodic, `AA`, `B4`, `A1` all pass, both via `--raw` and `--service`. | `cli.py:951`, `:985` | Allowlist `safety.VOLVO_READ_SERVICES = {A5,A6,A7,AE,B9,BB}`, checked before the bus is opened. Malformed input is a clean error. |
| P1-2 | P1 | Dashboard POSTs had no CSRF or DNS-rebinding defence. Any website open in the same browser could `POST /dtc/clear` (a form post needs no preflight) or `/bus`. | `web.py:786-810` | Every POST must be `application/json` (forces a CORS preflight the server never grants) with a same-origin or absent `Origin`. The `Host` must be localhost, an IP literal or `--host`. The page now always sends JSON. |
| P1-3 | P1 | No server-side write check in the HTTP layer, and no restriction on network clients. With `--host 0.0.0.0 --enable-writes`, anyone on the LAN could clear codes. | `web.py:804` | `/dtc/clear` re-checks `writes_enabled()` (403). It accepts only loopback clients unless the new `serve --allow-remote-writes` is given. Startup warns when bound to a non-loopback address. |
| P1-4 | P1 | Web clear sent `AF 11` to every module, then raised `AttributeError` (`self._miss` does not exist on `VolvoBackend`). The user saw an error for a write that had happened, and might retry it. | `backend.py:276` | `PollScheduler.reset_backoff()`. Test asserts the result is returned. |
| P1-5 | P1 | `ElmCanLink` ignored every setup reply (`ATSP7`, `ATCAF0`, filters) and the `ATCP`/`ATSH` replies in `send()`. A clone answering `?` was reported ready, and a rejected `ATSH` sent the frame under the previous/default CAN id. `ElmObdTransport` only warned and never checked it was an ELM. | `elm_can.py:122`, `:148`; `elm_obd.py:78`, `:126` | Every setup and header/filter command must answer `OK` (a `?` fails), else `TransportError`. Header cache invalidated on failure, and the frame is not sent. OBD: requires "ELM" in the `ATZ`/`ATI` reply; `ATSH`/`ATCRA` must be acknowledged (4-byte `ATSH` falls back to `ATCP`+3-byte `ATSH` for v1.x firmware); custom flow control is best-effort (§9). |
| P2-1 | P2 | A parameter definition's `request:` was never checked, though it is polled automatically (`monitor`/`record`/`serve`). A `request: "04"` would clear codes every cycle. | `volvo/parameters.py:432` | Load-time `DefinitionError` unless it is a read service. All shipped definitions pass. |
| P2-2 | P2 | A failed ELM `open()` left the serial/RFCOMM port open: no `close()` on failure, and callers (`backend._open`, `open_reader`) dropped the object. `close()` kept the closed port object, so a later `open()` wrote to a closed port. | `elm_can.py:116-124`, `:88`; `elm_obd.py:59-85`; `backend.py:72` | `open()` releases the port on any exception (including disconnects mid-setup). `close()` drops the handle. `switch_bus` clears the stale link first. |
| P2-3 | P2 | ELM-OBD: the ELM prints `7F xx 78` and then the real answer in one reply. `_parse` joined them into one corrupt payload that still looked "pending", and the code then sent a bare CR, which on an ELM327 **repeats (re-sends) the last request**. | `elm_obd.py:134`, `:185` | Pending lines are dropped in `_parse`. Several separate answers raise. On pending it only *listens* and never re-sends. |
| P2-4 | P2 | `VolvoEcm.read_dtcs` returned `[]` ("no codes") when no `[group, EE, 1B]` frame came back, e.g. on a `7F` reject or another module's answer. After a CLI clear that printed "cleared". | `transport/volvo_ecm.py:160-167` | Raises `TransportError` unless the module's own DTC answer is present. The captured empty answer `EE 1B 00 00` still yields `[]`. |
| P2-5 | P2 | CLI clear sweep: a `TransportError` from the clear or re-read aborted the whole sweep half-way. | `cli.py:253-259` | Per-module handling: `error (…)` / `ack, re-read failed (unverified)`. Never "cleared" without a successful re-read. |
| P2-6 | P2 | Single-threaded dashboard: no socket timeout, so one idle connection froze it. A negative `Content-Length` made `rfile.read(-1)` block. No body size limit. | `web.py:716-723`, `:814` | 10 s handler timeout, Content-Length validated (400), 64 KiB cap (413). |
| P2-7 | P2 | Proxy `ReadMsgs` trusted the driver's `*pNumMsgs`. A driver returning more than requested made the logger read past the caller's buffer (ASan heap-buffer-overflow). | `proxy/src/exports.cpp:118`, `:128` | Count capped at `requested`. |
| P2-8 | P2 | `g_proxy_error` (`std::string`) written and read from every host thread without a lock. TSan reported a race; possible heap corruption in VIDA. | `proxy/src/real_driver.cpp:196-199`, `exports.cpp:263` | Mutex-guarded, and `proxy_error()` returns a copy. TSan is clean. |
| P2-9 | P2 | `GET_CONFIG` (J2534-1: the list is filled *in pInput*, pOutput NULL) was logged as the pre-call input and `output: null`, so captures lost every config value. The fake driver read `pOutput`, which hid it. | `exports.cpp:293`; `fake-j2534/fake_driver.cpp:493` | Output logged from `pInput` after the call. Fake driver follows the spec. |
| P2-10 | P2 | `real_dll` pointing at the proxy itself made each call recurse until the stack overflowed: a crash inside VIDA instead of an error. | `real_driver.cpp:76` (and the POSIX `dlopen` path) | Detected (same module handle / same image base). Load error "real_dll points at the proxy itself". |
| P3-1 | P3 | After a call to a missing vendor entry point, `GetLastError` kept returning that stale proxy message instead of the vendor's text for later vendor failures. | `exports.cpp:27-33`, `:261` | The proxy error is cleared whenever a call is forwarded to the vendor. |
| P3-2 | P3 | `ReadMsgs` hard errors were dropped (0 msgs with `log_empty_reads=0`) or logged with `requested` fake empty messages. | `exports.cpp:115-128` | Errors are always logged, with messages only for `NOERROR`/`TIMEOUT`/`BUFFER_EMPTY`/`BUFFER_OVERFLOW` (VXDIAG returns data with `ERR_TIMEOUT`). |
| P3-3 | P3 | Test client: `--request` over 4124 bytes overflowed `tx.Data` (ASan stack overflow). | `test-client/main.cpp:205` | Length check. |
| P3-4 | P3 | Docs: README "confirmed each time" (the CLI does not prompt); `--enable-writes` help says DTC clearing is the only write (it also gates `dim-text`); `AF` "reference only" in `volvo-protocol.md` and `protocol/volvo.py`; `read 22F190 # raw UDS request, any hex`. | `README.md:113-115`, `cli.py:1178`, `docs/volvo-protocol.md:74`, `docs/dashboard.md:19` | All corrected. The network-exposure and raw-request rules are documented in `dashboard.md`. |
| P3-5 | P3 | `dtc --clear` on the UDS path silently read codes and did nothing. | `cli.py:264-` | Refused with "nothing was cleared" (exit 2). |
| P3-7 | P3 | `IsoTpError` from raw SocketCAN reassembly was not a `TransportError`, so a stray out-of-order frame became a traceback rather than a read error. | `socketcan.py:158` | Wrapped in `TransportError` (§9). |
| P3-6 | P3 | `data.need_bus` was concatenated unescaped into an `onclick` attribute (server data, so low risk). | `web.py:576` | Escaped and restricted to `[A-Za-z0-9_-]`. |

### Not fixed (documented)

| id | pri | finding | evidence | why not / recommendation |
| --- | --- | --- | --- | --- |
| P2-11 | P2 | SocketCAN raw ISO-TP sender: after the first block it clears `awaiting_flow_control`, so with `block_size > 0` the remaining frames are never sent. FC `WAIT`/`OVERFLOW` status is ignored. | `transport/socketcan.py:146-156` | Only affects requests longer than 7 bytes. The tool only sends short reads, so no current path triggers it. Fix when multi-frame requests are needed, with a test against a `vcan` loopback. |
| P3-8 | P3 | `--elm-baud` is ignored for the UDS ELM transport (fixed 115200, while the raw path uses 38400). | `cli.py:50` | Passing it through would change the effective baud for existing users. Needs a maintainer decision. |
| P3-9 | P3 | `DisableThreadLibraryCalls` is called from a DLL linked to the static CRT (`/MT`, `-static`), which Microsoft documents against. | `proxy/src/dllmain.cpp:14` | Remove it. Needs a Windows build to verify (no MinGW/MSVC here). |
| P3-10 | P3 | No `try/catch` in the `extern "C"` exports, so a `std::bad_alloc` (huge log rendering on 32-bit) would unwind into VIDA. | `proxy/src/exports.cpp` | Add a catch-all per export that returns `ERR_FAILED`. |
| P3-11 | P3 | `json_escape` writes non-UTF-8 bytes raw (e.g. `FormatMessageA` text in a non-English code page). | `proxy/src/logger.cpp:257` | The parser uses `errors="replace"`, so nothing breaks. `\u00XX`-escape bytes ≥ 0x80. |
| P3-12 | P3 | `fwrite` results unchecked: a full disk leaves a half line merged with the next record. | `logger.cpp:166` | Check and drop the line. |
| P3-13 | P3 | The ini parser cuts at any `#`/`;` (e.g. `C:\C#Tools\…`) and strips quotes per end. | `proxy/src/config.cpp:54` | Only treat them as comments at line start or after whitespace. |
| P3-14 | P3 | A missing vendor entry point returns `ERR_DEVICE_NOT_CONNECTED` (`ERR_NOT_SUPPORTED` would be accurate), and logs a `proxy_error` line on every call (floods the log while VIDA polls). | `exports.cpp:26-32` | Behavioural change visible to VIDA; leave until checked against VIDA. |
| P3-15 | P3 | `fake_driver` and the Python side assume `unsigned long` matches `c_ulong` (8 bytes on Linux). Fine for the test build, but Linux logs are not byte-compatible with Windows ones. | `proxy/include/j2534.h` | Informational. |
| P3-16 | P3 | CI runs ruff as informational only; there is no type check. `mypy --ignore-missing-imports python/volvo_diag` reports 50 errors in 7 files, all pre-existing (51 before this work; none new). | `.github/workflows/tests.yml` | Make ruff blocking; add mypy once those are fixed. |

## 4. Changes in behaviour (compatibility)

- `read <hex>` now refuses anything but read services, with or without
  `--enable-writes`. Research use of e.g. `read 1003` or `read 2701` no longer
  works. **Alternatives** considered: (a) gate them behind `--enable-writes`,
  rejected because the README defines that flag as DTC clearing only; (b) a
  separate `--allow-raw-writes` flag, deferred as a scope expansion for the
  maintainer to decide.
- `read-mem` accepts only `A5 A6 A7 AE B9 BB`. Freeze-frame reads (`AB`–`AD`)
  are deliberately not allowlisted until their sub-functions are confirmed
  read-only.
- Definitions whose `request:` is not a read service now fail to load.
- Dashboard: POSTs need JSON (the shipped page sends it), the `Host` header must
  be localhost, an IP literal or the `--host` value (reaching it by a custom
  hostname such as `mypc.local` is now refused), and clearing over the network
  needs `--allow-remote-writes`.
- ELM adapters that answer `?` to any setup command now fail `open()` instead
  of "working" in a wrong mode. That is correct, but a lenient clone that worked
  by accident may now be refused: run `probe` to see which command it rejects.
- ELM-OBD: the ECU header (`ATSH`, or `ATCP`+`ATSH` on v1.x) and receive
  filter (`ATCRA`) must be acknowledged, so an adapter that cannot set them
  fails the request instead of sending to the functional/default address.
  Custom flow-control (`ATFCSH/SD/SM`) stays optional, as before.
- ELM-OBD no longer re-sends a request after `7F xx 78`. If the adapter printed
  only the pending line, the read times out rather than being repeated.
- A Volvo DTC read with no matching answer is now an error rather than "no
  codes". The dashboard's Codes tab lists such modules ("No answer from: …"),
  and `/dtc` returns them as `no_answer`.
- Proxy logs: `GET_CONFIG` output now carries values, failed `ReadMsgs` are
  logged, and `GetLastError` reports the vendor's message after a forwarded
  call. The JSONL schema is unchanged.

No API used by the existing tests changed. `web.serve` gained an optional
`allow_remote_writes` argument, and `web.make_server` was split out for testing.

## 5. Potential risks requiring hardware validation

1. **J2534 low-speed sends:** `J2534CanLink.send` tags every message
   `ProtocolID = CAN (5)`, even on the 125k `CAN_LS (0x8004)` channel, while the
   filter code says that channel rejects protocol-5 messages
   (`transport/j2534.py:523` vs `:474-486`). If the VXDIAG enforces this, every
   125k request (DTC reads on the cabin bus, `dim-text`, `read-mem` there) fails
   with a driver error. The README reports DIM reads working, so the driver may
   tolerate it. Needs one on-car check, then send with `self.protocol`.
2. **Proxy `WriteMsgs` with a bad caller count:** the logger renders
   `*pNumMsgs` messages *before* forwarding, so a caller passing one message
   with `NumMsgs = 2` makes the proxy overread (ASan) even where the driver
   would return `ERR_EXCEEDED_LIMIT` (`exports.cpp:142`). It is the caller's
   bug and VIDA is not known to do it. Cap the rendered count.
3. **Unbounded `SBYTE_ARRAY.NumOfBytes`** in ioctl logging when
   `max_data_bytes = 0` (default) (`logger.cpp:336`, `:360`).
4. **Loader-lock hang at process exit:** `logger::stop()` takes the log mutex in
   `DLL_PROCESS_DETACH` (`dllmain.cpp:18`). If another thread was killed while
   holding it, VIDA hangs on exit. When `lpReserved != NULL`, flush without
   the lock instead.
5. **ELM327 clone behaviour:** the stricter `OK` checks and the pending-line
   parsing are tested against scripted replies only. A real vLinker/clone
   capture should confirm the reply formats (especially `ATCM`/`ATCF` and the
   `7F xx 78` line layout).

## 6. Tests added or modified

| file | what it pins down |
| --- | --- |
| `tests/test_safety.py` (new) | UDS/Volvo allowlists. `read <hex>` refuses writes with and without `--enable-writes` and never builds a transport. `read-mem` refuses `B0/B1/B2/A8/AF/B8/A3` via `--raw` and `--service`. The `dtc --clear` and `dim-text` gates. UDS `--clear` refused. The clear sweep survives clear/re-read errors and never claims "cleared" unverified. Write requests in definitions fail to load. |
| `tests/test_transport_hardening.py` (new) | ELM raw link: each rejected setup command fails `open()` and closes the port; a non-ELM device or a disconnect mid-setup closes the port; a rejected `ATSH` never sends the frame; close→reopen gets a fresh port. ELM OBD: rejected protocol/header, non-ELM. Pending-line parsing; no re-send on pending. `read_dtcs`: a negative answer or another module's answer is an error; the captured empty and `2A30` answers still decode. |
| `tests/test_web_safety.py` (new) | Real server on an ephemeral port: the write is refused server-side when disabled; same-origin clear works; cross-site form posts (415), foreign/`null` Origin (403), and DNS-rebinding Host (403, GET and POST) are refused; a non-loopback client is refused unless `allow_remote_writes`; negative/oversized bodies are handled; an idle client cannot wedge the server; the page posts JSON. `VolvoBackend.clear_dtcs` returns its result after writing. |
| `tests/test_integration.py` (extended) | Through ctypes → proxy → fake driver: the `GET_CONFIG` result is logged; a failed `ReadMsgs` is logged with no invented messages; `real_dll` = proxy gives an error, not a crash (child process). |

Every new test was run against the original code. **32 tests (57 counting
subtests) fail on `9a5884c`**: the Python tests against the original package,
and the three integration tests against a fresh build of the original C++. All
pass after the fixes. The 16 new tests that also pass on the original code pin
behaviour that was already correct and must stay so. Examples: the existing
`--enable-writes` gates, the captured DTC decodes, a good ELM opening, and a
raw *read* still reaching the ECU (which guards against over-blocking).

## 7. Verification results

| check | environment | result |
| --- | --- | --- |
| baseline `pytest` before changes | Python 3.14, C++ built into `build/` | 159 passed |
| `pytest` (full suite, incl. integration) | Python 3.14 | **217 passed, 51 subtests passed** (after §9) |
| `pytest` (full suite) | Python 3.8 (via `uv`) — the project's floor | **217 passed** (after §9) |
| C++ build (`-Wall -Wextra`) + `ctest` | GCC, Linux, RelWithDebInfo | 0 warnings, `proxy_core` passed |
| ASan + UBSan scenarios (over-count read, `GET_CONFIG`, stale error, failed read, self-load) | out-of-tree build + harness | all clean after the fix (the harness's own leak excepted) |
| TSan, 3 threads × 20 000 `ReadMsgs`/`GetLastError` with a missing driver | out-of-tree build | no race reported |
| `ruff check .` | ruff (current) | clean |
| `mypy` on the package | mypy, `--ignore-missing-imports` | 50 errors, all pre-existing (51 on `9a5884c`; compared by message: none new, one fixed) — P3-16 |

**Not verified:**
- **Windows (MSVC/MinGW) build:** no toolchain here. The `LoadLibraryExA`
  self-load check and `FreeLibrary` call compile only on Windows.
- **Real adapters/vehicle:** prohibited for this task.
- **The 32-bit ABI.**

## 8. Remaining recommendations

1. Validate the risks in §5 on the bench, starting with the low-speed `ProtocolID`.
2. Decide whether research workflows need an explicit, separately named opt-in
   for raw non-read requests (§4). If so, keep it out of `--enable-writes` and
   log every such request.
3. Consider a startup token for the dashboard (printed on the console, required
   on POST) if remote use with `--allow-remote-writes` becomes common. Origin/Host
   checks do not authenticate LAN clients.
4. Make ruff blocking in CI and add mypy once its existing errors are fixed. Add
   a Windows (MinGW cross) build job so `real_driver.cpp`'s Windows branch is
   compiled in CI.
5. Fix the remaining C++ P3s: catch-all in the exports, UTF-8 escaping,
   `fwrite` checks, ini comment parsing, and capped `WriteMsgs`/`SBYTE_ARRAY`
   rendering.
6. When multi-frame requests are needed, fix the SocketCAN flow-control sender
   (P2-11) and test it on `vcan`.

## 9. Acceptance review (second pass)

This pass checked the change set independently: the diff, the safety boundary,
compatibility, the Windows build, and the remaining findings.

### Diff findings and additional fixes

1. **Compatibility regression in P1-5 (fixed).** `ElmObdTransport._select`
   required `OK` for `ATSH18DA10F1`, the 4-byte header form. That form exists
   only in ELM327 v2.x firmware; v1.x adapters (most clones say "v1.5") set a
   29-bit header as `ATCP 18` + `ATSH DA10F1`. Those adapters had worked before
   by falling back to the default functional header, and would have become
   unusable. The flow-control commands (`ATFCSH/SD/SM`), which are optional
   tuning, had also been made mandatory.
   - Now: on rejection the code falls back to `ATCP`+3-byte `ATSH`, and
     `ATCRA` must still be acknowledged.
   - User flow-control mode is only selected when its header and data were
     accepted; otherwise the adapter keeps automatic flow control.
   - Tests: `ElmObdHeaderCompatTest`.
2. **Unverified clears looked verified in the dashboard (fixed).**
   `VolvoBackend.read_dtcs` dropped modules whose DTC read failed. After a
   clear, the re-scan could show "No trouble codes" for a module that never
   answered. `/dtc` now returns `no_answer`, and the page lists those
   modules. Tests: `DtcScanReportingTest`.
3. **P3-7 fixed:** SocketCAN ISO-TP framing errors are wrapped in
   `TransportError`. Test: `SocketCanFramingErrorTest`.

The new tests fail against the original code (6 of 7). The seventh pins the
unchanged v2.x behaviour.

Everything else in the diff was confirmed as described in §3:
- **`read <hex>`:** an allowlist of service bytes, checked before any
  transport is built; `--enable-writes` is not consulted.
- **Definitions:** the same allowlist is applied at load; all 443 shipped
  parameters load.
- **`read-mem`:** an allowlist, checked on the final payload whether it comes
  from `--raw` or `--service`.
- **Web:** the server-side `writes_enabled()` and loopback checks come before
  `clear_dtcs()`; Host/Origin/Content-Type checks run before the body is read.
- **No hidden writes:** no other code path builds a UDS/OBD write payload
  (grep over `python/volvo_diag`).

The `.def` files and `j2534.h` are unchanged.

### Host validation

| Host header | allowed |
| --- | --- |
| `localhost`, `localhost:8080`, `LOCALHOST:8080` | yes |
| `127.0.0.1:8080`, `[::1]:8080`, `[::1]` | yes |
| LAN IP (`192.168.1.5:8080`, `10.0.0.7`), link-local `[fe80::1%25eth0]` | yes |
| the `--host` value (e.g. `mypc.local`) | yes |
| any other name (`mypc.local` when not the bind host, `evil.com`, `127.0.0.1.nip.io`, `localhost.`) | no (403) |

Rejecting unknown hostnames is the DNS-rebinding defence itself: the Origin
check alone cannot stop rebinding, because Origin and Host then name the same
attacker domain. So it stays. A hostname can still be used without any new
flag by binding to it (`serve --host mypc.local`), as now documented in
`dashboard.md`.

### Windows build verification

No MinGW/MSVC cross toolchain is available (`mingw-w64-gcc` would be a
1.2 GB install), so **the Windows DLL was not built or run**. Static checks
done instead:

- **Windows branches, type-checked on the host compiler.** `clang++ -std=c++17
  -fsyntax-only -Wall -Wextra -D_WIN32` with a stub `windows.h` that declares
  the Win32 types and functions with their SDK signatures. `real_driver.cpp`
  (including the new `LoadLibraryExA`/`FreeLibrary` self-load guard),
  `dllmain.cpp` and `exports.cpp` pass with no diagnostics. `logger.cpp`,
  which this work does not touch, needs `direct.h` and was not checked.
- **i686 ABI.** `clang++ -target i686-pc-windows-msvc -fsyntax-only` with
  static asserts: `unsigned long` is 4 bytes, `sizeof(PASSTHRU_MSG) == 4152`,
  `Data` is at offset 24, `SCONFIG`/`SCONFIG_LIST`/`SBYTE_ARRAY` are 8 bytes,
  and `PassThruOpen` is `__stdcall`.
- **Exports.** All 14 v04.04 exports in `j2534proxy.def` are defined in
  `exports.cpp`, and their signatures are unchanged.

A real `cmake -DCMAKE_TOOLCHAIN_FILE=cmake/mingw-i686.cmake` build plus a load
test in VIDA remains outstanding.

### Remaining findings, re-assessed

| id | priority | release-blocking | note |
| --- | --- | --- | --- |
| P2-11 SocketCAN multi-block send | P2 (confirmed) | no | Only affects requests over 7 bytes to an ECU that sets `BS > 0`. No built-in path sends such requests, but a raw multi-DID `read` could. The fix needs flow-control state handling and a `vcan` test, which is not trivial. |
| Risk 1: 125k `ProtocolID` | risk | no | Hardware-dependent; the README reports DIM reads working on the car. One bench check is needed before changing it. |
| Risks 2–4 (C++ `WriteMsgs` rendering, `SBYTE_ARRAY`, loader lock) | risk | no | Need a misbehaving caller, a garbage count, or a killed thread; none is a known VIDA behaviour. |
| P3-8 `--elm-baud` | P3 | no | Fixing it changes the effective baud for current users, so it needs a maintainer decision. |
| P3-9 `DisableThreadLibraryCalls` | P3 | no | A one-line removal, but it changes Windows runtime behaviour that cannot be tested here. |
| P3-10 to P3-16 | P3 | no | Unchanged; logging robustness and CI hygiene. |
