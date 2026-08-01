<#
.SYNOPSIS
    Lists this car's exact default ECU variant for every module, from CarCom's
    vehicle profile. Pins CEM (and all modules) precisely.

.DESCRIPTION
    T161_Profile carries direct FKs to the profile values (model/year/engine/
    ...). We resolved ours to Model=V50 (2767), Engine=D4164T (3005),
    Year=2007 (3083). This joins profiles matching those to
    T160_DefaultEcuVariant and reports each module's variant, ordered by
    folderLevel (most specific first = our variant). Read-only.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\carcom-defaults.ps1 `
        -OutDir "Z:\projects\volvo-toolkit\logs"
#>

[CmdletBinding()]
param(
    [string]$OutDir = ".",
    [int]$Model = 2767,
    [int]$Engine = 3005,
    [int]$Year = 3083,
    [string]$Server
)

$ErrorActionPreference = 'Continue'
$report = Join-Path $OutDir "carcom-defaults.txt"
$csv = Join-Path $OutDir "carcom-defaults.csv"
function W { param([string]$t = "") ; Write-Host $t ; Add-Content -LiteralPath $report -Value $t -Encoding UTF8 }
Set-Content -LiteralPath $report -Value "CarCom default variants $(Get-Date)  M=$Model E=$Engine Y=$Year" -Encoding UTF8

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

# Which profiles match our car (model + engine, year optional). Show what
# transmission/body each is, to disambiguate.
W ""
W "matching profiles (id | folderLevel | title):"
$profiles = Invoke-Sql $connString @"
SELECT id, folderLevel, description, title,
       fkT162_ProfileValue_Transmission AS trans, fkT162_ProfileValue_Body AS body
FROM T161_Profile
WHERE fkT162_ProfileValue_Model = $Model
  AND (fkT162_ProfileValue_Engine = $Engine OR fkT162_ProfileValue_Engine IS NULL)
  AND (fkT162_ProfileValue_Year = $Year OR fkT162_ProfileValue_Year IS NULL)
ORDER BY folderLevel DESC
"@
foreach ($r in $profiles.Rows) {
    W ("  {0} | lvl {1} | {2} {3} | trans {4} body {5}" -f $r["id"], $r["folderLevel"],
        $r["description"], $r["title"], $r["trans"], $r["body"])
}

# Default ECU variant per module for those profiles, most specific first.
W ""
W "default ECU variants for our car (module | ecutype | variantId | DiagNumber | lvl):"
$rows = Invoke-Sql $connString @"
SELECT t102.description AS module, t102.identifier AS ecutype,
       e.name AS ecuname, t100.id AS variantId, t100.identifier AS diagnum,
       t161.folderLevel AS lvl
FROM T161_Profile t161
JOIN T160_DefaultEcuVariant t160 ON t160.fkT161_Profile = t161.id
JOIN T100_EcuVariant t100 ON t100.id = t160.fkT100_EcuVariant
JOIN T101_Ecu e ON e.id = t100.fkT101_Ecu
JOIN T102_EcuType t102 ON t102.id = e.fkT102_EcuType
WHERE t161.fkT162_ProfileValue_Model = $Model
  AND (t161.fkT162_ProfileValue_Engine = $Engine OR t161.fkT162_ProfileValue_Engine IS NULL)
  AND (t161.fkT162_ProfileValue_Year = $Year OR t161.fkT162_ProfileValue_Year IS NULL)
ORDER BY t102.description, t161.folderLevel DESC, t100.id
"@
$cols = @("module","ecutype","ecuname","variantId","diagnum","lvl")
Set-Content -LiteralPath $csv -Value ($cols -join "`t") -Encoding UTF8
foreach ($r in $rows.Rows) {
    $vals = @(); foreach ($c in $cols) { $vals += ([string]$r[$c]) }
    W ("  " + ($vals -join " | "))
    Add-Content -LiteralPath $csv -Value ($vals -join "`t") -Encoding UTF8
}
W ""; W "done. The top variant per module is this car's."
