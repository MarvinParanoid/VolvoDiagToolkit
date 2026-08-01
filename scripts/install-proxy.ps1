<#
.SYNOPSIS
    Registers the logging proxy as an additional J2534 device.

.DESCRIPTION
    The genuine driver is left registered and untouched. The proxy is added
    as a second entry, so VIDA offers both and you choose per session which
    one to use. Nothing about the car's configuration changes.

    The proxy's own configuration (which real DLL to forward to, where to put
    the logs) is written next to the copied DLL as j2534proxy.ini.

    Before touching the registry it checks the two things that make a J2534
    DLL unloadable: a bitness that differs from the driver it wraps, and a
    Universal CRT dependency on a machine without the UCRT (Windows 7).

    Runs on the stock Windows 7 SP1 PowerShell 2.0.

.PARAMETER Device
    Registry key name of the driver to wrap. Omit to pick from a list.

.PARAMETER ProxyDll
    Path to the built j2534proxy.dll. Defaults to the newest one under the
    repository.

.EXAMPLE
    .\scripts\install-proxy.ps1
    .\scripts\install-proxy.ps1 -Device VXDIAG -LogDir D:\volvo-logs
#>

[CmdletBinding()]
param(
    [string]$Device,
    [string]$ProxyDll,
    [string]$InstallDir = "$env:ProgramData\volvo-toolkit",
    [string]$LogDir = "$env:ProgramData\volvo-toolkit\logs",
    [string]$EntryName = 'Volvo Toolkit Logging Proxy',
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $scriptDir
. (Join-Path $scriptDir 'j2534-common.ps1')

Assert-Administrator

# ---- pick the driver to wrap -------------------------------------------

$candidates = @(Get-PassThruDevices | Where-Object { $_.Key -ne $EntryName })
if ($candidates.Count -eq 0) {
    throw 'No J2534 driver is registered. Install the adapter driver first.'
}

if ($Device) {
    $target = @($candidates | Where-Object { $_.Key -like "*$Device*" })[0]
    if (-not $target) { throw "No registered driver matches '$Device'." }
}
elseif ($candidates.Count -eq 1) {
    $target = $candidates[0]
}
else {
    Write-Host 'Registered J2534 drivers:'
    for ($i = 0; $i -lt $candidates.Count; $i++) {
        Write-Host ("  [{0}] {1}  ({2}, {3})  {4}" -f $i, $candidates[$i].Key,
                    $candidates[$i].View, $candidates[$i].Bitness,
                    $candidates[$i].FunctionLibrary)
    }
    $choice = Read-Host 'Wrap which one?'
    $target = $candidates[[int]$choice]
}

Write-Host ("wrapping {0}  ({1}, {2})" -f $target.Key, $target.Bitness, $target.FunctionLibrary) `
    -ForegroundColor Cyan

# ---- locate and vet the proxy ------------------------------------------

if (-not $ProxyDll) {
    $ProxyDll = @(Get-ChildItem -Path $root -Filter 'j2534proxy.dll' -Recurse `
                      -ErrorAction SilentlyContinue |
                  Sort-Object LastWriteTime -Descending |
                  Select-Object -First 1 -ExpandProperty FullName)[0]
}
if (-not $ProxyDll -or -not (Test-Path -LiteralPath $ProxyDll)) {
    throw 'j2534proxy.dll not found — run scripts\build-windows.ps1 first, or pass -ProxyDll.'
}

$proxy = Get-PeInfo $ProxyDll
if ($proxy.Bitness -ne $target.Bitness) {
    $flag = if ($target.Bitness -eq 'x86') { 'Win32' } else { 'x64' }
    throw ("Bitness mismatch: the proxy is $($proxy.Bitness), $($target.Key) is " +
           "$($target.Bitness). Rebuild with -Arch $flag.")
}

if ($proxy.NeedsUcrt -and -not (Test-UcrtPresent)) {
    $message = @"
This proxy imports the Universal CRT (api-ms-win-crt-*) and this machine has
no ucrtbase.dll, so VIDA would fail to load it with an unhelpful error.

  * install KB2999226 (Update for Universal C Runtime in Windows), or
  * rebuild with MSVC: scripts\build-windows.ps1 links the CRT statically.

Pass -Force to register it anyway.
"@
    if ($Force) { Write-Warning $message } else { throw $message }
}

# ---- install ------------------------------------------------------------

if (-not (Test-Path -LiteralPath $InstallDir)) {
    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
}
if (-not (Test-Path -LiteralPath $LogDir)) {
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
}

$installedDll = Join-Path $InstallDir 'j2534proxy.dll'
Copy-Item -LiteralPath $ProxyDll -Destination $installedDll -Force

$stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
$ini = @"
; Written by scripts\install-proxy.ps1 on $stamp
real_dll = $($target.FunctionLibrary)
log_dir = $LogDir
session_tag =
logging = 1
flush_each = 1
max_data_bytes = 0
log_empty_reads = 0
log_get_last_error = 0
"@
Set-Content -LiteralPath (Join-Path $InstallDir 'j2534proxy.ini') -Value $ini -Encoding ASCII

# ---- register -----------------------------------------------------------

$destinationPath = Join-Path $target.RegistryRoot $EntryName
if (Test-Path -LiteralPath $destinationPath) {
    Remove-Item -LiteralPath $destinationPath -Recurse -Force
}
New-Item -Path $destinationPath -Force | Out-Null

# Copy every capability flag from the real driver: VIDA decides what it can do
# with the device from these values, and the proxy can do exactly what the
# driver behind it can.
#
# Written with New-ItemProperty rather than (Get-Item ...).SetValue(): the key
# the provider hands back is opened read-only, and SetValue on it fails even
# when elevated.
$source = Get-Item -LiteralPath $target.RegistryPath
foreach ($valueName in $source.GetValueNames()) {
    if ($valueName -eq '') { continue }
    New-ItemProperty -LiteralPath $destinationPath -Name $valueName `
        -Value $source.GetValue($valueName) `
        -PropertyType $source.GetValueKind($valueName) -Force | Out-Null
}
$source.Close()

New-ItemProperty -LiteralPath $destinationPath -Name 'Name' -Value $EntryName `
    -PropertyType String -Force | Out-Null
New-ItemProperty -LiteralPath $destinationPath -Name 'Vendor' -Value 'volvo-toolkit' `
    -PropertyType String -Force | Out-Null
New-ItemProperty -LiteralPath $destinationPath -Name 'FunctionLibrary' -Value $installedDll `
    -PropertyType String -Force | Out-Null
# Remembered so remove-proxy.ps1 can report what this entry was wrapping.
New-ItemProperty -LiteralPath $destinationPath -Name 'ProxiedLibrary' `
    -Value $target.FunctionLibrary -PropertyType String -Force | Out-Null

Write-Host ''
Write-Host 'installed' -ForegroundColor Green
Write-Host ("  entry     {0}" -f $EntryName)
Write-Host ("  registry  {0}" -f $destinationPath)
Write-Host ("  proxy     {0}  ({1})" -f $installedDll, $proxy.Bitness)
Write-Host ("  forwards  {0}" -f $target.FunctionLibrary)
Write-Host ("  logs      {0}" -f $LogDir)
Write-Host ''
Write-Host 'Check it before starting VIDA:'
Write-Host ("  j2534-test.exe `"{0}`"" -f $installedDll)
Write-Host ''
Write-Host ('Then start VIDA and pick "' + $EntryName + '" as the communication tool.')
