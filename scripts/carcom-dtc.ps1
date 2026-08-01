<#
.SYNOPSIS
    Explores how CarCom stores diagnostic trouble codes (DTCs) for an ECU: the
    metadata table, the code -> description catalogue, and the stored
    procedures VIDA uses to read them. First step to reading DTCs over the
    Volvo protocol. Read-only.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\carcom-dtc.ps1 `
        -EcuVariantId 965 -Out ecm -OutDir "Z:\projects\volvo-toolkit\logs"
#>

[CmdletBinding()]
param(
    [int]$EcuVariantId = 965,
    [string]$Out = "ecm",
    [string]$OutDir = ".",
    [string]$Lang = "en-US",
    [string]$Server
)

$ErrorActionPreference = 'Continue'
if (-not (Test-Path -LiteralPath $OutDir)) { New-Item -ItemType Directory -Force -Path $OutDir | Out-Null }
$report = Join-Path $OutDir "carcom-dtc-$Out.txt"
$csv = Join-Path $OutDir "carcom-dtc-$Out.csv"
function W { param([string]$t = "") ; Write-Host $t ; Add-Content -LiteralPath $report -Value $t -Encoding UTF8 }
Set-Content -LiteralPath $report -Value "CarCom DTC explore variant $EcuVariantId $(Get-Date)" -Encoding UTF8

function Invoke-Sql {
    param([string]$ConnString, [string]$Query)
    $conn = New-Object System.Data.SqlClient.SqlConnection $ConnString
    $conn.Open()
    try {
        $cmd = $conn.CreateCommand(); $cmd.CommandText = $Query; $cmd.CommandTimeout = 180
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

# 1. DTC metadata table + block types that are DTCs.
Dump "T147_BlockMetaDTC columns" @"
SELECT COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_NAME='T147_BlockMetaDTC' ORDER BY ORDINAL_POSITION
"@
Dump "T147_BlockMetaDTC sample" "SELECT TOP 10 * FROM T147_BlockMetaDTC"

# 2. How VIDA reads DTCs - the procedures show the request/join.
foreach ($p in @('vadis_GetAllPossibleDtcsOnEcu','vadis_GetNumberOfDtcsOnEcu',
                 'vadis_GetDtcSymptomsForEcu')) {
    W ""; W "==== PROC $p"
    try {
        $d = Invoke-Sql $connString "SELECT OBJECT_DEFINITION(OBJECT_ID('dbo.$p')) AS def"
        W ([string]$d.Rows[0][0])
    } catch { W "  (error: $($_.Exception.Message))" }
}

# 3. The DTC read request for this ECU: DTC blocks (type 10/32) and the parent
#    block that carries the on-wire request/service (like REID does for params).
Dump "DTC request blocks for variant $EcuVariantId (parent types + CompareValue)" @"
SELECT DISTINCT bt.identifier AS blocktype, bpt.identifier AS parenttype,
       bvparent.CompareValue AS request, COUNT(*) AS n
FROM T100_EcuVariant ev
JOIN T144_BlockChild bc ON bc.fkT100_EcuVariant = ev.id
JOIN T141_Block b ON b.id = bc.fkT141_Block_Child AND b.fkT142_BlockType IN (10,32)
JOIN T142_BlockType bt ON bt.id = b.fkT142_BlockType
JOIN T141_Block bp ON bp.id = bc.fkT141_Block_Parent
JOIN T142_BlockType bpt ON bpt.id = bp.fkT142_BlockType
LEFT JOIN T150_BlockValue bvparent ON bvparent.fkT141_Block = bp.id
WHERE ev.id = $EcuVariantId
GROUP BY bt.identifier, bpt.identifier, bvparent.CompareValue
ORDER BY n DESC
"@

# raw CompareValue, to see how the on-wire code is encoded (prefix + code).
Dump "raw DTC CompareValue sample" @"
SELECT TOP 12 t150.CompareValue AS rawvalue, t141.name AS description
FROM T141_Block t141
JOIN T144_BlockChild t144 ON t144.fkT141_Block_Child = t141.id AND t144.fkT141_Block_Parent = 1
JOIN T150_BlockValue t150 ON t150.fkT141_Block = t141.id
WHERE t144.fkT100_EcuVariant = $EcuVariantId AND t141.fkT142_BlockType = 10
ORDER BY t150.CompareValue
"@

# 4. The DTC catalogue: code + description (from vadis_GetAllPossibleDtcsOnEcu).
W ""; W "dumping DTC catalogue -> $csv"
$rows = Invoke-Sql $connString @"
SELECT DISTINCT
  SUBSTRING(t150.CompareValue, 3, 6) AS code,
  t141.name AS description
FROM T141_Block t141
JOIN T144_BlockChild t144 ON t144.fkT141_Block_Child = t141.id AND t144.fkT141_Block_Parent = 1
JOIN T150_BlockValue t150 ON t150.fkT141_Block = t141.id
WHERE t144.fkT100_EcuVariant = $EcuVariantId AND t141.fkT142_BlockType = 10
ORDER BY code
"@
$cols = @("code","description")
Set-Content -LiteralPath $csv -Value ($cols -join "`t") -Encoding UTF8
$n = 0
foreach ($r in $rows.Rows) {
    $v = @(); foreach ($c in $cols) { $v += (([string]$r[$c]) -replace "`t"," " -replace "`r"," " -replace "`n"," ") }
    Add-Content -LiteralPath $csv -Value ($v -join "`t") -Encoding UTF8
    $n++
}
W "wrote $n DTC rows. done."
