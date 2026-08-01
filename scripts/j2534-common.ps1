<#
    Shared helpers for the J2534 scripts.

    Written for Windows PowerShell 2.0 on .NET 3.5 - the stock combination on
    Windows 7 SP1, which is where VIDA usually lives. That rules out
    [pscustomobject], $PSScriptRoot, [Environment]::Is64BitOperatingSystem and
    [Microsoft.Win32.RegistryKey]::OpenBaseKey, so the registry is reached
    through provider paths instead.

    Dot-source it:  . (Join-Path $scriptDir 'j2534-common.ps1')
#>

$PassThruSubKey = 'SOFTWARE\PassThruSupport.04.04'

function Get-ProcessBitness {
    if ([IntPtr]::Size -eq 8) { 'x64' } else { 'x86' }
}

function Get-OsBitness {
    # PROCESSOR_ARCHITEW6432 is only set inside a 32-bit process on a 64-bit
    # Windows, which is exactly the case that has to be told apart. Testing for
    # HKLM:\SOFTWARE\Wow6432Node cannot do it: the key answers the same either
    # way, because WOW64 does not redirect a path that already names it.
    if ($env:PROCESSOR_ARCHITEW6432) { return 'x64' }
    if ($env:PROCESSOR_ARCHITECTURE -eq 'AMD64' -or
        $env:PROCESSOR_ARCHITECTURE -eq 'IA64') { return 'x64' }
    return 'x86'
}

function Get-PassThruRoots {
    <# Registry provider paths that can hold PassThru registrations, each
       labelled with the view it *physically* is - which depends on the bitness
       of this PowerShell, not just on the path.

       This matters because a 32-bit VIDA only ever sees the 32-bit view. #>
    $os = Get-OsBitness
    $process = Get-ProcessBitness
    $roots = @()

    if ($os -eq 'x86') {
        $roots += New-Object PSObject -Property @{
            Path = "HKLM:\$PassThruSubKey"; View = '32-bit'; Native = $true
            Note = 'the only view on a 32-bit Windows'
        }
        return $roots
    }

    if ($process -eq 'x86') {
        # Redirected: HKLM\SOFTWARE is physically HKLM\SOFTWARE\Wow6432Node.
        $roots += New-Object PSObject -Property @{
            Path = "HKLM:\$PassThruSubKey"; View = '32-bit'; Native = $false
            Note = 'redirected - this is the view a 32-bit VIDA reads'
        }
        # The 64-bit view cannot be reached from a 32-bit process without
        # RegistryKey.OpenBaseKey, which needs .NET 4.
        return $roots
    }

    $roots += New-Object PSObject -Property @{
        Path = "HKLM:\$PassThruSubKey"; View = '64-bit'; Native = $true
        Note = 'a 32-bit application cannot see this'
    }
    $roots += New-Object PSObject -Property @{
        Path = "HKLM:\SOFTWARE\Wow6432Node\$PassThruSubKey"; View = '32-bit'
        Native = $false; Note = 'the view a 32-bit VIDA reads'
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
    foreach ($rootInfo in Get-PassThruRoots) {
        $root = $rootInfo.Path
        if (-not (Test-Path $root)) { continue }
        $view = $rootInfo.View
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
                ViewNote        = $rootInfo.Note
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
        throw 'Run this from an elevated PowerShell - it writes to HKLM.'
    }
}
