<#
.SYNOPSIS
    Removes the proxy's J2534 registration.

.DESCRIPTION
    Undoes either form of install-proxy.ps1:

      * the added entry is deleted;
      * an in-place install has the vendor entry's FunctionLibrary restored
        from the ProxiedLibrary value it saved.

    Either way VIDA ends up talking to the vendor driver exactly as before.

    Runs on the stock Windows 7 SP1 PowerShell 2.0.

.PARAMETER Purge
    Also delete the installed DLL, its configuration and the logs.

.EXAMPLE
    .\scripts\remove-proxy.ps1
    .\scripts\remove-proxy.ps1 -Purge
#>

[CmdletBinding()]
param(
    [string]$EntryName = 'Volvo Toolkit Logging Proxy',
    [string]$InstallDir = "$env:ProgramData\volvo-toolkit",
    [switch]$Purge
)

$ErrorActionPreference = 'Stop'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $scriptDir 'j2534-common.ps1')

Assert-Administrator

$removed = $false

# 1. The added entry.
foreach ($rootInfo in Get-PassThruRoots) {
    $path = Join-Path $rootInfo.Path $EntryName
    if (Test-Path -LiteralPath $path) {
        Remove-Item -LiteralPath $path -Recurse -Force
        Write-Host "removed $path" -ForegroundColor Green
        $removed = $true
    }
}

# 2. Any vendor entry whose library was swapped in place. Restoring is driven
#    by ProxiedLibrary, and only when FunctionLibrary really does point at a
#    proxy - a vendor entry that was never touched must not be rewritten.
foreach ($device in @(Get-PassThruDevices)) {
    if ($device.Key -eq $EntryName) { continue }
    $values = Get-ItemProperty -LiteralPath $device.RegistryPath -ErrorAction SilentlyContinue
    if (-not $values -or -not $values.ProxiedLibrary) { continue }
    if ($values.FunctionLibrary -eq $values.ProxiedLibrary) { continue }
    if ((Split-Path -Leaf $values.FunctionLibrary) -ne 'j2534proxy.dll') {
        Write-Warning ("$($device.Key) has a ProxiedLibrary value but its FunctionLibrary " +
                       "is $($values.FunctionLibrary), which is not our proxy. Left alone.")
        continue
    }

    New-ItemProperty -LiteralPath $device.RegistryPath -Name 'FunctionLibrary' `
        -Value $values.ProxiedLibrary -PropertyType String -Force | Out-Null
    Remove-ItemProperty -LiteralPath $device.RegistryPath -Name 'ProxiedLibrary' -Force
    Write-Host ("restored {0} -> {1}" -f $device.Key, $values.ProxiedLibrary) -ForegroundColor Green
    $removed = $true
}

if (-not $removed) {
    Write-Host 'nothing to undo: no added entry and no in-place install found'
}

if ($Purge) {
    if (Test-Path -LiteralPath $InstallDir) {
        Write-Host "deleting $InstallDir (including any logs in it)" -ForegroundColor Yellow
        Remove-Item -LiteralPath $InstallDir -Recurse -Force
    }
}
else {
    Write-Host "left $InstallDir in place (use -Purge to delete the DLL and the logs)"
}
