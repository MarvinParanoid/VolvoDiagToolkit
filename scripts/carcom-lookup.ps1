<#
.SYNOPSIS
    Looks up which ECU (type + variant) a part or hardware number belongs to,
    and where that ECU sits (bus, comm address). Read-only. Use it to identify
    a module you have in hand before pulling its configuration.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File "Z:\projects\volvo-toolkit\scripts\carcom-lookup.ps1" `
        -Number 31350022 -OutDir "Z:\projects\volvo-toolkit\logs"
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Number,
    [string]$OutDir = ".",
    [string]$Server
)

$ErrorActionPreference = 'Continue'
if (-not (Test-Path -LiteralPath $OutDir)) { New-Item -ItemType Directory -Force -Path $OutDir | Out-Null }
$report = Join-Path $OutDir "carcom-lookup.txt"
function W { param([string]$t = "") ; Write-Host $t ; Add-Content -LiteralPath $report -Value $t -Encoding UTF8 }
Set-Content -LiteralPath $report -Value "CarCom lookup '$Number' $(Get-Date)" -Encoding UTF8

function Invoke-Sql {
    param([string]$ConnString, [string]$Query)
    $conn = New-Object System.Data.SqlClient.SqlConnection $ConnString
    $conn.Open()
    try {
        $cmd = $conn.CreateCommand(); $cmd.CommandText = $Query; $cmd.CommandTimeout = 120
        $a = New-Object System.Data.SqlClient.SqlDataAdapter $cmd
        $t = New-Object System.Data.DataTable; [void]$a.Fill($t); return ,$t
    } finally { $conn.Close() }
}

$servers = if ($Server) { @($Server) } else { @('.\VIDA', 'localhost\VIDA') }
$connString = $null
foreach ($srv in $servers) {
    $cs = "Server=$srv;Database=carcom;Integrated Security=SSPI;Connect Timeout=5;"
    try { [void](Invoke-Sql $cs "SELECT 1"); $connString = $cs; W "connected: $srv"; break } catch { }
}
if (-not $connString) { W "could not connect"; exit 1 }

function Dump { param([string]$Title, [string]$Query)
    W ""; W "==== $Title"
    try {
        $t = Invoke-Sql $connString $Query
        $cols = @(); foreach ($c in $t.Columns) { $cols += $c.ColumnName }
        W ("  " + ($cols -join " | "))
        foreach ($row in $t.Rows) {
            $v = @(); foreach ($c in $cols) { $v += (([string]$row[$c]) -replace "`r"," " -replace "`n"," ") }
            W ("  " + ($v -join " | "))
        }
        W ("  ({0} rows)" -f $t.Rows.Count)
    } catch { W "  (error: $($_.Exception.Message))" }
}

# Normalise: match the digits ignoring spaces/suffix letters.
$digits = ($Number -replace '[^0-9]', '')

# 1. Variants whose diagnostic (part) number contains these digits.
Dump "ECU variants matching '$Number' (by diagnostic number)" @"
SELECT v.id AS variantId, v.identifier AS diagnum, t.identifier AS ecutype, t.description AS module
FROM T100_EcuVariant v
JOIN T101_Ecu e ON e.id = v.fkT101_Ecu
JOIN T102_EcuType t ON t.id = e.fkT102_EcuType
WHERE REPLACE(REPLACE(v.identifier,' ',''),'-','') LIKE '%$digits%'
ORDER BY t.identifier, v.id
"@

# 2. Where each matching ECU type lives on the car (bus, comm address).
Dump "connection for matching ECU types" @"
SELECT DISTINCT t.identifier AS ecutype, t.description AS module,
       b.identifier AS bus, cfg.busRate, cfg.commAddress AS commaddr, cfg.canidRx AS canid
FROM T100_EcuVariant v
JOIN T101_Ecu e ON e.id = v.fkT101_Ecu
JOIN T102_EcuType t ON t.id = e.fkT102_EcuType
LEFT JOIN T120_Config_EcuVariant cev ON cev.fkT100_EcuVariant = v.id
LEFT JOIN T121_Config cfg ON cfg.id = cev.fkT121_Config
LEFT JOIN T123_Bus b ON b.id = cfg.fkT123_Bus
WHERE REPLACE(REPLACE(v.identifier,' ',''),'-','') LIKE '%$digits%'
"@

W ""; W "done. Send carcom-lookup.txt back."
