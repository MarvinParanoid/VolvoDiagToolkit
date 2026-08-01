<#
.SYNOPSIS
    Lists every J2534 driver registered on this machine and, crucially, the
    bitness of each DLL.

.DESCRIPTION
    Stage 1 of the project depends on one fact that must be measured rather
    than assumed: whether VIDA and the vendor driver are x86 or x64. A proxy
    of the wrong bitness cannot be loaded at all, and the failure looks like
    "device not found" rather than anything informative.

    Also reports whether this machine has the Universal CRT, which a
    mingw-w64 build of the proxy needs and Windows 7 does not have by default.

    Runs on the stock Windows 7 SP1 PowerShell 2.0.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\list-j2534.ps1
#>

[CmdletBinding()]
param()

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $scriptDir 'j2534-common.ps1')

$os = Get-OsBitness
$process = Get-ProcessBitness

Write-Host ("Windows        {0} {1}" -f [Environment]::OSVersion.Version, $os)
Write-Host ("PowerShell     {0} ({1} process)" -f $PSVersionTable.PSVersion, $process)
if ($os -eq 'x64' -and $process -eq 'x64') {
    Write-Host '               a 32-bit VIDA reads the 32-bit view only' -ForegroundColor Gray
}
elseif ($os -eq 'x64') {
    Write-Host '               registry access is redirected to the 32-bit view' -ForegroundColor Gray
}
$ucrt = Test-UcrtPresent
Write-Host ("Universal CRT  {0}" -f $(if ($ucrt) { 'present (ucrtbase.dll)' }
                                      else { 'ABSENT' })) `
    -ForegroundColor $(if ($ucrt) { 'Gray' } else { 'Yellow' })

$devices = @(Get-PassThruDevices)
if ($devices.Count -eq 0) {
    Write-Host ''
    Write-Host "No J2534 drivers are registered under HKLM\$PassThruSubKey."
    Write-Host 'Install the VXDIAG (or other adapter) driver first.'
    exit 1
}

foreach ($device in $devices) {
    Write-Host ''
    Write-Host ("{0}  [{1} view: {2}]" -f $device.Key, $device.View, $device.ViewNote) `
        -ForegroundColor Cyan
    Write-Host ("  vendor      {0}" -f $device.Vendor)
    Write-Host ("  library     {0}" -f $device.FunctionLibrary)
    Write-Host ("  bitness     {0}" -f $device.Bitness) -ForegroundColor Yellow
    Write-Host ("  CAN {0}   ISO15765 {1}" -f $device.CAN, $device.ISO15765)
}

Write-Host ''
Write-Host 'Running diagnostic applications:' -ForegroundColor Cyan
$seen = $false
foreach ($process in @(Get-Process | Where-Object {
             $_.ProcessName -match 'vida|dice|vxdiag|passthru' })) {
    $seen = $true
    $path = $null
    try { $path = $process.MainModule.FileName } catch { }
    $bitness = if ($path) { (Get-PeInfo $path).Bitness } else { 'unknown (run elevated)' }
    Write-Host ("  {0,-24} pid {1,-8} {2}" -f $process.ProcessName, $process.Id, $bitness)
}
if (-not $seen) { Write-Host '  none (start VIDA to see which bitness it runs as)' }

$bitnesses = @($devices | ForEach-Object { $_.Bitness } | Sort-Object -Unique)
Write-Host ''
Write-Host 'Build the proxy with the SAME bitness as the library above:' -ForegroundColor Green
if ($bitnesses -contains 'x86') {
    Write-Host '  x86 ->  .\scripts\build-windows.ps1 -Arch Win32'
    Write-Host '          (or ./scripts/build-mingw.sh i686 when cross-building on Linux)'
}
if ($bitnesses -contains 'x64') {
    Write-Host '  x64 ->  .\scripts\build-windows.ps1 -Arch x64'
}

# On a 64-bit Windows the drivers must be visible to a 32-bit VIDA, and from a
# 64-bit PowerShell the 32-bit view is a different key that is easy to miss.
if ($os -eq 'x64' -and $process -eq 'x64') {
    $in32 = @($devices | Where-Object { $_.View -eq '32-bit' })
    if ($in32.Count -eq 0) {
        Write-Host ''
        Write-Host 'Nothing is registered in the 32-bit view.' -ForegroundColor Yellow
        Write-Host 'VIDA runs as x86 and normally reads that view, so either it finds its'
        Write-Host 'adapter some other way, or this listing is incomplete. Settle it by'
        Write-Host 'running the same script from the 32-bit PowerShell:'
        Write-Host '  C:\Windows\SysWOW64\WindowsPowerShell\v1.0\powershell.exe ^'
        Write-Host '      -ExecutionPolicy Bypass -File scripts\list-j2534.ps1'
        Write-Host 'and install the proxy from whichever PowerShell shows the driver VIDA uses.'
    }
}

if (-not $ucrt) {
    Write-Host ''
    Write-Host 'This machine has no Universal CRT.' -ForegroundColor Yellow
    Write-Host 'A mingw-w64 build of the proxy will fail to load here. Either:'
    Write-Host '  * install KB2999226 (Update for Universal C Runtime), or'
    Write-Host '  * build with MSVC (build-windows.ps1 links the CRT statically).'
    Write-Host 'install-proxy.ps1 checks the DLL you give it and refuses a mismatch.'
}
