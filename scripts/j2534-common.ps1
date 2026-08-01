<#
    Shared helpers for the J2534 scripts.

    Written for Windows PowerShell 2.0 on .NET 3.5 — the stock combination on
    Windows 7 SP1, which is where VIDA usually lives. That rules out
    [pscustomobject], $PSScriptRoot, [Environment]::Is64BitOperatingSystem and
    [Microsoft.Win32.RegistryKey]::OpenBaseKey, so the registry is reached
    through provider paths instead.

    Dot-source it:  . (Join-Path $scriptDir 'j2534-common.ps1')
#>

$PassThruSubKey = 'SOFTWARE\PassThruSupport.04.04'

function Test-Wow64Host {
    # A 32-bit Windows has no Wow6432Node, and therefore only one registry view.
    Test-Path 'HKLM:\SOFTWARE\Wow6432Node'
}

function Get-PassThruRoots {
    <# Registry provider paths that can hold PassThru registrations, most
       relevant first. On 32-bit Windows that is a single path. #>
    $roots = @("HKLM:\$PassThruSubKey")
    if (Test-Wow64Host) {
        # From 64-bit PowerShell this is how a 32-bit application's view is
        # addressed; a 32-bit VIDA reads exactly these keys.
        $roots += "HKLM:\SOFTWARE\Wow6432Node\$PassThruSubKey"
    }
    return $roots
}

function Get-PeInfo {
    <# Machine type and whether the file imports the Universal CRT.
       Returns an object with Bitness and NeedsUcrt. #>
    param([string]$Path)

    $info = New-Object PSObject -Property @{
        Path = $Path; Bitness = 'missing'; NeedsUcrt = $false
    }
    if (-not $Path -or -not (Test-Path -LiteralPath $Path)) { return $info }

    try {
        $bytes = [System.IO.File]::ReadAllBytes($Path)
    }
    catch {
        $info.Bitness = "unreadable: $($_.Exception.Message)"
        return $info
    }
    if ($bytes.Length -lt 0x40) { $info.Bitness = 'not a PE file'; return $info }

    $peOffset = [BitConverter]::ToInt32($bytes, 0x3C)
    if ($peOffset -le 0 -or ($peOffset + 6) -ge $bytes.Length -or
        [BitConverter]::ToUInt32($bytes, $peOffset) -ne 0x00004550) {
        $info.Bitness = 'not a PE file'
        return $info
    }

    switch ([BitConverter]::ToUInt16($bytes, $peOffset + 4)) {
        0x014C  { $info.Bitness = 'x86' }
        0x8664  { $info.Bitness = 'x64' }
        0xAA64  { $info.Bitness = 'ARM64' }
        default { $info.Bitness = 'unknown machine type' }
    }

    # Imported DLL names are plain ASCII in the file, so the UCRT dependency can
    # be spotted without walking the import directory. IndexOf on a .NET string
    # rather than a PowerShell loop: a vendor DLL can be several megabytes and
    # PowerShell 2.0 iterates about as fast as it sounds.
    $text = [System.Text.Encoding]::ASCII.GetString($bytes)
    $info.NeedsUcrt = ($text.IndexOf('api-ms-win-crt-runtime') -ge 0)
    return $info
}

function Test-UcrtPresent {
    # ucrtbase.dll ships with Windows 10; on 7/8.1 it arrives with KB2999226.
    Test-Path (Join-Path $env:SystemRoot 'System32\ucrtbase.dll')
}

function Get-PassThruDevices {
    <# Every registered J2534 driver, with the bitness of its DLL. #>
    $devices = @()
    foreach ($root in Get-PassThruRoots) {
        if (-not (Test-Path $root)) { continue }
        $view = if ($root -match 'Wow6432Node') { '32-bit view' } else { 'native view' }
        foreach ($child in (Get-ChildItem -Path $root -ErrorAction SilentlyContinue)) {
            $values = Get-ItemProperty -Path $child.PSPath -ErrorAction SilentlyContinue
            if (-not $values) { continue }
            $library = $values.FunctionLibrary
            $pe = Get-PeInfo $library
            $devices += New-Object PSObject -Property @{
                Key             = $child.PSChildName
                RegistryPath    = $child.PSPath
                RegistryRoot    = $root
                View            = $view
                Vendor          = $values.Vendor
                Name            = $values.Name
                FunctionLibrary = $library
                Bitness         = $pe.Bitness
                NeedsUcrt       = $pe.NeedsUcrt
                CAN             = $values.CAN
                ISO15765        = $values.ISO15765
            }
        }
    }
    return $devices
}

function Assert-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal $identity
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Run this from an elevated PowerShell — it writes to HKLM.'
    }
}
