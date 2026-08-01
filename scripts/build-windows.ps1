<#
.SYNOPSIS
    Builds the proxy, the fake driver and the test client with MSVC.

.DESCRIPTION
    The MSVC build links the CRT statically (/MT), so the resulting DLL needs
    no Visual C++ redistributable and no Universal CRT update. On Windows 7
    SP1 that makes it the safer build; the mingw-w64 cross-build needs
    KB2999226 on the target.

    Visual Studio 2019 is the newest that installs on Windows 7 SP1; 2015 and
    2017 work too, as do the standalone Build Tools.

.PARAMETER Arch
    Win32 or x64. Must match the bitness reported by scripts\list-j2534.ps1
    for the vendor DLL - a 64-bit VIDA cannot load a 32-bit proxy and vice
    versa.

.EXAMPLE
    .\scripts\build-windows.ps1 -Arch Win32
    .\scripts\build-windows.ps1 -Arch Win32 -Test
#>

[CmdletBinding()]
param(
    [ValidateSet('Win32', 'x64')][string]$Arch = 'Win32',
    [ValidateSet('Debug', 'Release', 'RelWithDebInfo')][string]$Config = 'RelWithDebInfo',
    [switch]$Test
)

$ErrorActionPreference = 'Stop'
# $PSScriptRoot is empty in scripts before PowerShell 3.0, and Windows 7 SP1
# ships with 2.0.
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $scriptDir
$build = Join-Path $root "build-$Arch"

if (-not (Get-Command cmake -ErrorAction SilentlyContinue)) {
    throw 'cmake is not on PATH. Install "C++ CMake tools for Windows" with Visual Studio.'
}

Write-Host "configuring $Arch in $build" -ForegroundColor Cyan
cmake -S $root -B $build -A $Arch
if ($LASTEXITCODE -ne 0) { throw "cmake configure failed" }

Write-Host "building $Config" -ForegroundColor Cyan
cmake --build $build --config $Config
if ($LASTEXITCODE -ne 0) { throw "build failed" }

$proxy = Join-Path $build "proxy\$Config\j2534proxy.dll"
$fake  = Join-Path $build "fake-j2534\$Config\fake_j2534.dll"
$client = Join-Path $build "test-client\$Config\j2534-test.exe"

Write-Host ''
Write-Host 'built:' -ForegroundColor Green
foreach ($artifact in @($proxy, $fake, $client)) {
    if (Test-Path $artifact) { Write-Host "  $artifact" } else { Write-Host "  MISSING $artifact" }
}

# A static CRT is the whole point on Windows 7: confirm it rather than assume.
. (Join-Path $scriptDir 'j2534-common.ps1')
if (Test-Path $proxy) {
    $info = Get-PeInfo $proxy
    Write-Host ("  bitness {0}" -f $info.Bitness)
    if ($info.NeedsUcrt) {
        Write-Warning ('This build imports the Universal CRT, so it needs ' +
                       'KB2999226 on Windows 7/8.1. Check that CMAKE_MSVC_RUNTIME_LIBRARY ' +
                       'took effect (CMake 3.15 or newer is required for it).')
    }
}

if ($Test) {
    Write-Host ''
    Write-Host 'core unit tests' -ForegroundColor Cyan
    & (Join-Path $build "proxy\$Config\proxy_core_test.exe")

    Write-Host ''
    Write-Host 'test client against the fake driver (no adapter needed)' -ForegroundColor Cyan
    & $client $fake --request 22F190 --request 010C
}

Write-Host ''
Write-Host 'next:  .\scripts\install-proxy.ps1   (run as administrator)' -ForegroundColor Green
