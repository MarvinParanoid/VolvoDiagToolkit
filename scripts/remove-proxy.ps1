<#
.SYNOPSIS
    Removes the proxy's J2534 registration.

.DESCRIPTION
    Deletes only the entry install-proxy.ps1 created. The vendor driver's own
    registration is never touched, so VIDA keeps working exactly as before.

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
foreach ($root in Get-PassThruRoots) {
    $path = Join-Path $root $EntryName
    if (Test-Path -LiteralPath $path) {
        Remove-Item -LiteralPath $path -Recurse -Force
        Write-Host "removed $path" -ForegroundColor Green
        $removed = $true
    }
}

if (-not $removed) {
    Write-Host "no registry entry named '$EntryName' was found — nothing to do"
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
