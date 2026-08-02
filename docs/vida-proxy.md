# The VIDA logging proxy

VIDA is not being replaced — it is the reference. A transparent J2534 proxy sits
between VIDA and the real VXDIAG driver, forwards every call unchanged, and
writes each one to a JSONL log. That log is where the protocol and the parameter
definitions come from.

```
VIDA ──► j2534proxy.dll ──► VXDIAG driver ──► car
              │
              └──► JSONL log
```

Build it on the VIDA machine or cross-compile it — see
[install-windows.md](install-windows.md) and [install-linux.md](install-linux.md).

## Registering it

```powershell
.\scripts\install-proxy.ps1        # elevated
```

`install-proxy.ps1` adds a *second* J2534 entry and leaves the vendor
registration alone, so VIDA offers both and you choose per session.
`remove-proxy.ps1` takes it back out.

Some applications only list device names they already know and will ignore the
added entry. For those, `install-proxy.ps1 -InPlace` points the vendor entry's
own `FunctionLibrary` at the proxy and remembers the original path in a
`ProxiedLibrary` value; `remove-proxy.ps1` restores it. The application then
selects the same device it always did. **If VIDA runs in a VM, take a snapshot
before either** — reverting is faster than debugging.

Before starting VIDA, prove the chain works:

```powershell
build-Win32\test-client\RelWithDebInfo\j2534-test.exe `
    C:\ProgramData\volvo-toolkit\j2534proxy.dll
```

`PassThruOpen`, `PassThruReadVersion` and `READ_VBATT` should all return 0 and
the battery voltage should be real.

## Configuration

The proxy reads `j2534proxy.ini` (next to the DLL, or at
`%VOLVO_J2534_PROXY_CONFIG%`); `install-proxy.ps1` writes it. The key one is
`session_tag`, appended to each log's file name — set it per experiment
(`20-dtc-read`, `21-car-config`, …) so recordings are easy to tell apart. See
[proxy/j2534proxy.ini.example](../proxy/j2534proxy.ini.example) for the rest.

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
happens in Python, where it can be fixed without rebuilding anything. Empty
`ReadMsgs` polls are dropped by default (`log_empty_reads = 1` keeps them).

What to do with a recording — record, diff, decode — is
[adding-vehicle.md](adding-vehicle.md); the framing it reveals is
[volvo-protocol.md](volvo-protocol.md).
