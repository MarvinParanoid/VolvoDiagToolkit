# Windows install

There are two roles, and they can be two different machines:

- **The reading client** (`volvo-monitor`) — just needs Python + the VXDIAG
  J2534 driver. A modern **Windows 10** box is fine. See below and
  [dashboard.md](dashboard.md).
- **The VIDA machine** — where VIDA runs and where you build/register the
  logging proxy to reverse-engineer new parameters. Usually **Windows 7 SP1**.
  See [vida-proxy.md](vida-proxy.md) for the proxy, and the notes below.

## Reading client (Windows 10)

Match Python's bitness to the driver's: a 32-bit VXDIAG DLL needs 32-bit Python.

```powershell
# install Python (32-bit if the VXDIAG DLL is 32-bit), then, in the checkout:
pip install pyyaml            # the only runtime dependency
$env:PYTHONPATH="python"
python -m volvo_diag devices  # should list the VXDIAG J2534 driver
```

`pip install -e .` instead of the `PYTHONPATH` line installs a `volvo-monitor`
command. Then follow [dashboard.md](dashboard.md).

## Bitness first (on the VIDA machine)

```powershell
powershell -ExecutionPolicy Bypass -File scripts\list-j2534.ps1
```

This prints every registered J2534 driver, the path to its DLL and whether that
DLL is x86 or x64, plus the bitness of a running VIDA. Most VXDIAG installations
are x86 — but check, do not assume. A proxy of the wrong bitness fails to load
and the error VIDA shows says nothing useful.

## Building the proxy on Windows

With Visual Studio (2015, 2017, 2019 on 64-bit, or the standalone Build Tools):

```powershell
.\scripts\build-windows.ps1 -Arch Win32 -Test
.\scripts\install-proxy.ps1                 # elevated
```

Or cross-compile on Linux and copy the DLL over — see
[install-linux.md](install-linux.md). Registration and the proxy itself are in
[vida-proxy.md](vida-proxy.md).

## Windows 7 SP1 (the usual VIDA machine)

Three things differ there, and all three are handled:

**The C runtime.** mingw-w64 links against the Universal CRT, which Windows 7
only has once KB2999226 is installed. `list-j2534.ps1` reports whether
`ucrtbase.dll` is present and `install-proxy.ps1` refuses to register a DLL the
machine cannot load. The MSVC build has no such dependency at all — it links the
CRT statically (`/MT`).

Which of the two to use depends on the machine. Visual Studio 2019 needs a
64-bit Windows, and a 32-bit VIDA VM is usually short on memory anyway, so there
the practical answer is to cross-build on Linux and copy three files over:
`j2534proxy.dll`, `j2534-test.exe` and `fake_j2534.dll`. They are statically
linked and need no compiler, no runtime and no installer on the target — only
the UCRT, which `list-j2534.ps1` confirms.

**PowerShell 2.0 on .NET 3.5.** That is what Windows 7 SP1 ships with, so the
scripts avoid `[pscustomobject]`, `$PSScriptRoot`, `Is64BitOperatingSystem` and
`RegistryKey.OpenBaseKey`, and reach the registry through provider paths. They
also work unchanged on PowerShell 5.1 if the machine has been updated.

**A 32-bit Windows has no `Wow6432Node`**, so there is a single registry view and
`HKLM\SOFTWARE\PassThruSupport.04.04` is the only place to look. The scripts
detect this rather than listing every driver twice.

For the Python side, 3.8.10 is the last release that installs on Windows 7 —
that is the floor this project targets (no `match`, no runtime `int | None`).
The log analysis does not have to run there at all: the JSONL files are ordinary
files, so copy them off and analyse them anywhere. Only the reading client needs
a Windows box with the VXDIAG driver — and that can be the modern Windows 10 one
above, it does not have to be the Windows 7 VIDA machine.

Installing Python 3.8 on Windows 7 sometimes fails — see
[troubleshooting.md](troubleshooting.md).
