<#
.SYNOPSIS
    Dumps CarCom's addressing map for every ECU: bus, protocol, comm/CAN
    addresses, and whether the module sits behind a gateway. This answers
    "which modules are reachable from the OBD connector directly, and which
    need routing" - stage 7 of the project.

.DESCRIPTION
    From T121_Config (the same table that gave the ECM its 0x0FFFFE / commAddress
    0x11 / 29-bit addressing). Bus and protocol are emitted as their numeric
    ids plus the T123_Bus / T122_Protocol lookup tables, so nothing depends on
    guessing a column name. Read-only.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\carcom-modules.ps1 `
        -OutDir "Z:\projects\volvo-toolkit\logs"
#>

[CmdletBinding()]
param(
    [string]$OutDir = ".",
    [string]$Server
)

$ErrorActionPreference = 'Continue'
$report = Join-Path $OutDir "carcom-modules.txt"
$csv = Join-Path $OutDir "carcom-modules.csv"
function W { param([string]$t = "") ; Write-Host $t ; Add-Content -LiteralPath $report -Value $t -Encoding UTF8 }
Set-Content -LiteralPath $report -Value "CarCom module map $(Get-Date)" -Encoding UTF8

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

function Dump {
    param([string]$Title, [string]$Query, [string]$CsvPath)
    W ""; W "==== $Title"
    try {
        $t = Invoke-Sql $connString $Query
        $cols = @(); foreach ($c in $t.Columns) { $cols += $c.ColumnName }
        W ("  " + ($cols -join " | "))
        if ($CsvPath) { Set-Content -LiteralPath $CsvPath -Value ($cols -join "`t") -Encoding UTF8 }
        foreach ($row in $t.Rows) {
            $vals = @(); foreach ($c in $cols) { $vals += (([string]$row[$c]) -replace "`r"," " -replace "`n"," ") }
            W ("  " + ($vals -join " | "))
            if ($CsvPath) { Add-Content -LiteralPath $CsvPath -Value ($vals -join "`t") -Encoding UTF8 }
        }
        W ("  ({0} rows)" -f $t.Rows.Count)
    } catch { W "  (error: $($_.Exception.Message))" }
}

# Lookups: interpret the bus / protocol numbers in the map below.
Dump "T123_Bus (all columns)" "SELECT * FROM T123_Bus ORDER BY id"
Dump "T122_Protocol (all columns)" "SELECT * FROM T122_Protocol ORDER BY id"

# The map: one row per (module, distinct addressing). Bus/protocol as ids;
# gateway shown as its CAN address (null = reachable directly).
Dump "module addressing map" @"
SELECT DISTINCT
  et.identifier                                  AS ecutype,
  e.name                                         AS module,
  cfg.fkT123_Bus                                 AS busId,
  cfg.fkT122_Protocol                            AS protoId,
  cfg.busRate                                    AS busRate,
  cfg.addressSize                                AS addrBits,
  cfg.commAddress                                AS commAddr,
  cfg.canAddress                                 AS canAddr,
  cfg.physicalAddress                            AS physAddr,
  cfg.functionalAddress                          AS funcAddr,
  cfg.canIdTX                                    AS canTx,
  cfg.canIdRX                                    AS canRx,
  CASE WHEN cfg.fkT121_Config_Gateway IS NULL THEN 'direct' ELSE 'gateway' END AS routing,
  gw.canAddress                                  AS gwCanAddr,
  gw.commAddress                                 AS gwCommAddr
FROM T101_Ecu e
JOIN T102_EcuType et ON et.id = e.fkT102_EcuType
JOIN T100_EcuVariant v ON v.fkT101_Ecu = e.id
JOIN T120_Config_EcuVariant cev ON cev.fkT100_EcuVariant = v.id
JOIN T121_Config cfg ON cfg.id = cev.fkT121_Config
LEFT JOIN T121_Config gw ON gw.id = cfg.fkT121_Config_Gateway
ORDER BY e.name, busId
"@ $csv

W ""; W "wrote carcom-modules.csv"
W "done."
