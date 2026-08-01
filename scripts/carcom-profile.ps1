<#
.SYNOPSIS
    Finds this car's exact ECU variants via CarCom's vehicle profile
    (model + year + engine -> default ECU variant per module). This pins the
    CEM (and every other module) precisely, instead of guessing a variant -
    which matters because identifier meanings differ between variants.

.DESCRIPTION
    Discovery + lookup. Dumps the profile tables and vadis_GetDefaultEcuVariants
    so the join is known, then lists profile values for a V50 / D4164T / 2007
    and the default ECM + CEM variants for matching profiles. Read-only.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\carcom-profile.ps1 `
        -OutDir "Z:\projects\volvo-toolkit\logs"
#>

[CmdletBinding()]
param(
    [string]$OutDir = ".",
    [string]$Model = "V50",
    [string]$Engine = "D4164",
    [string]$Year = "2007",
    [string]$Server
)

$ErrorActionPreference = 'Continue'
$report = Join-Path $OutDir "carcom-profile.txt"
function W { param([string]$t = "") ; Write-Host $t ; Add-Content -LiteralPath $report -Value $t -Encoding UTF8 }
Set-Content -LiteralPath $report -Value "CarCom profile $(Get-Date)  $Model $Engine $Year" -Encoding UTF8

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
    param([string]$Title, [string]$Query)
    W ""; W "==== $Title"
    try {
        $t = Invoke-Sql $connString $Query
        $cols = @(); foreach ($c in $t.Columns) { $cols += $c.ColumnName }
        W ("  " + ($cols -join " | "))
        foreach ($row in $t.Rows) {
            $vals = @(); foreach ($c in $cols) { $vals += (([string]$row[$c]) -replace "`r"," " -replace "`n"," ") }
            W ("  " + ($vals -join " | "))
        }
        W ("  ({0} rows)" -f $t.Rows.Count)
    } catch { W "  (error: $($_.Exception.Message))" }
}

# 1. How VIDA maps a profile to ECU variants.
W ""; W "==== PROC vadis_GetDefaultEcuVariants"
try {
    $d = Invoke-Sql $connString "SELECT OBJECT_DEFINITION(OBJECT_ID('dbo.vadis_GetDefaultEcuVariants')) AS def"
    W ([string]$d.Rows[0][0])
} catch { W "  (error: $($_.Exception.Message))" }

# 2. Profile tables structure.
Dump "T161_Profile columns" @"
SELECT COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_NAME='T161_Profile' ORDER BY ORDINAL_POSITION
"@
Dump "tables linking Profile <-> ProfileValue" @"
SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES
WHERE TABLE_NAME LIKE '%Profile%' ORDER BY TABLE_NAME
"@

# 3. Profile values that match our car.
Dump "profile values: model like '$Model'" @"
SELECT pv.id, pv.identifier, pv.description, t.identifier AS valtype
FROM T162_ProfileValue pv JOIN T163_ProfileValueType t ON t.id = pv.fkT163_ProfileValueType
WHERE t.identifier = 'model_id' AND pv.description LIKE '%$Model%'
"@
Dump "profile values: engine like '$Engine'" @"
SELECT pv.id, pv.identifier, pv.description, t.identifier AS valtype
FROM T162_ProfileValue pv JOIN T163_ProfileValueType t ON t.id = pv.fkT163_ProfileValueType
WHERE t.identifier = 'engine' AND (pv.description LIKE '%$Engine%' OR pv.identifier LIKE '%$Engine%')
"@
Dump "profile values: year '$Year'" @"
SELECT pv.id, pv.identifier, pv.description, t.identifier AS valtype
FROM T162_ProfileValue pv JOIN T163_ProfileValueType t ON t.id = pv.fkT163_ProfileValueType
WHERE t.identifier = 'model_year' AND (pv.identifier = '$Year' OR pv.description LIKE '%$Year%')
"@

W ""; W "done. Send this back; the profile join tells us the exact CEM/ECM variants."
