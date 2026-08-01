<#
.SYNOPSIS
    Extracts the full ECM parameter set (identifier, offset, length, scaling
    formula, unit, name, range) from VIDA's CarCom for the Bosch EDC16C engine
    controller - our D4164T. Writes a CSV to the shared folder.

.DESCRIPTION
    Uses the join that vadis_GetParameterData revealed: a PARAM block
    (BlockType 8) is a child of an identifier block whose BlockValue.CompareValue
    is the on-wire request; the PARAM block carries offset/length and, via its
    own BlockValue, the unit and the T155_Scaling formula.

    First lists the EDC16C ECU variants with their parameter counts, then dumps
    every parameter of one variant (the richest, or -EcuVariantId) to
    carcom-ecm.csv. Read-only.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\carcom-ecm.ps1 `
        -OutDir "Z:\projects\volvo-toolkit\logs"
#>

[CmdletBinding()]
param(
    [string]$OutDir = ".",
    [int]$EcuVariantId = 0,
    [string]$Lang = "en-US",
    [string]$Server
)

$ErrorActionPreference = 'Continue'
$report = Join-Path $OutDir "carcom-ecm.txt"
$csv = Join-Path $OutDir "carcom-ecm.csv"
function W { param([string]$t = "") ; Write-Host $t ; Add-Content -LiteralPath $report -Value $t -Encoding UTF8 }
Set-Content -LiteralPath $report -Value "CarCom ECM extract $(Get-Date)  lang=$Lang" -Encoding UTF8

function Invoke-Sql {
    param([string]$ConnString, [string]$Query)
    $conn = New-Object System.Data.SqlClient.SqlConnection $ConnString
    $conn.Open()
    try {
        $cmd = $conn.CreateCommand(); $cmd.CommandText = $Query; $cmd.CommandTimeout = 180
        $adapter = New-Object System.Data.SqlClient.SqlDataAdapter $cmd
        $table = New-Object System.Data.DataTable
        [void]$adapter.Fill($table)
        return ,$table
    }
    finally { $conn.Close() }
}

$servers = if ($Server) { @($Server) } else { @('.\VIDA', 'localhost\VIDA') }
$connString = $null
foreach ($srv in $servers) {
    $cs = "Server=$srv;Database=carcom;Integrated Security=SSPI;Connect Timeout=5;"
    try { [void](Invoke-Sql $cs "SELECT 1"); $connString = $cs; W "connected: $srv"; break } catch { }
}
if (-not $connString) { W "could not connect to carcom"; exit 1 }

# ---- languages (so we know the code to ask GetTextFromLang for) --------

function Dump {
    param([string]$Title, [string]$Query)
    W ""
    W "-- $Title"
    try {
        $t = Invoke-Sql $connString $Query
        $cols = @(); foreach ($c in $t.Columns) { $cols += $c.ColumnName }
        W ("  " + ($cols -join " | "))
        foreach ($row in $t.Rows) {
            $vals = @(); foreach ($c in $cols) { $vals += ([string]$row[$c]) }
            W ("  " + ($vals -join " | "))
        }
    } catch { W "  (error: $($_.Exception.Message))" }
}

Dump "T193_Language (all columns)" "SELECT TOP 40 * FROM T193_Language"

# ---- EDC16C variants ----------------------------------------------------
# ECU ids 700 and 851 are 'ECM Bosch EDC16C' (and its GGD twin).

W ""
W "EDC16C ECM variants (id | DiagNumber | commAddress | #params):"
$variants = Invoke-Sql $connString @"
SELECT v.id, v.identifier AS diagnum, cfg.commAddress AS addr,
       COUNT(DISTINCT b.id) AS params
FROM T100_EcuVariant v
JOIN T101_Ecu e ON e.id = v.fkT101_Ecu AND e.id IN (700, 851)
LEFT JOIN T120_Config_EcuVariant cev ON cev.fkT100_EcuVariant = v.id
LEFT JOIN T121_Config cfg ON cfg.id = cev.fkT121_Config
LEFT JOIN T144_BlockChild bc ON bc.fkT100_EcuVariant = v.id
LEFT JOIN T141_Block b ON b.id = bc.fkT141_Block_Child AND b.fkT142_BlockType = 8
GROUP BY v.id, v.identifier, cfg.commAddress
ORDER BY params DESC
"@
$best = 0; $bestParams = -1
foreach ($r in $variants.Rows) {
    W ("  {0} | {1} | {2} | {3}" -f $r["id"], $r["diagnum"], $r["addr"], $r["params"])
    if ([int]$r["params"] -gt $bestParams) { $bestParams = [int]$r["params"]; $best = [int]$r["id"] }
}

# Pick OUR variant by the identifiers we actually captured on the wire
# (0x003A/0x007E/0x00AE etc.). The variant that answers those is ours,
# regardless of part number.
W ""
W "variants ranked by how many of our captured identifiers they carry:"
$match = Invoke-Sql $connString @"
SELECT v.id, v.identifier AS diagnum, cfg.commAddress AS addr,
       COUNT(DISTINCT bvparent.CompareValue) AS hits
FROM T100_EcuVariant v
JOIN T101_Ecu e ON e.id = v.fkT101_Ecu AND e.id IN (700, 851)
LEFT JOIN T120_Config_EcuVariant cev ON cev.fkT100_EcuVariant = v.id
LEFT JOIN T121_Config cfg ON cfg.id = cev.fkT121_Config
JOIN T144_BlockChild bc ON bc.fkT100_EcuVariant = v.id
JOIN T141_Block b ON b.id = bc.fkT141_Block_Child AND b.fkT142_BlockType = 8
JOIN T141_Block bp ON bp.id = bc.fkT141_Block_Parent
JOIN T150_BlockValue bvparent ON bvparent.fkT141_Block = bp.id
WHERE bvparent.CompareValue IN
  ('0x0005','0x002E','0x003A','0x0050','0x0063','0x007E','0x009E','0x00A7','0x00AE')
GROUP BY v.id, v.identifier, cfg.commAddress
ORDER BY hits DESC
"@
$matchBest = 0; $matchHits = -1
foreach ($r in $match.Rows) {
    W ("  {0} | {1} | addr {2} | {3}/9 hits" -f $r["id"], $r["diagnum"], $r["addr"], $r["hits"])
    if ([int]$r["hits"] -gt $matchHits) { $matchHits = [int]$r["hits"]; $matchBest = [int]$r["id"] }
}

if ($EcuVariantId -eq 0) {
    if ($matchHits -ge 6) {
        $EcuVariantId = $matchBest
        W ("auto-selected variant {0} (matched {1}/9 of our identifiers)" -f $matchBest, $matchHits)
    } else {
        $EcuVariantId = $best
        W ("no strong identifier match ({0}/9); falling back to richest variant {1}" -f $matchHits, $best)
    }
}

# Which parent block type carries the request identifier (non-empty
# CompareValue) for this variant? That is the A6 read selector.
Dump "parent block types with a CompareValue (variant $EcuVariantId)" @"
SELECT bparent.fkT142_BlockType AS parenttype, bt.identifier AS typename,
       COUNT(*) AS n, MIN(bvparent.CompareValue) AS example
FROM T100_EcuVariant ev
JOIN T144_BlockChild bc ON bc.fkT100_EcuVariant = ev.id
JOIN T141_Block b ON b.id = bc.fkT141_Block_Child AND b.fkT142_BlockType = 8
JOIN T141_Block bparent ON bparent.id = bc.fkT141_Block_Parent
JOIN T150_BlockValue bvparent ON bvparent.fkT141_Block = bparent.id
LEFT JOIN T142_BlockType bt ON bt.id = bparent.fkT142_BlockType
WHERE ev.id = $EcuVariantId AND bvparent.CompareValue IS NOT NULL AND bvparent.CompareValue <> ''
GROUP BY bparent.fkT142_BlockType, bt.identifier
ORDER BY n DESC
"@

W ""
W "dumping parameters for EcuVariant $EcuVariantId (lang $Lang) -> $csv"

# ---- full parameter dump for the chosen variant ------------------------

$rows = Invoke-Sql $connString @"
SELECT DISTINCT
  bvparent.CompareValue                         AS identifier,
  bparent.fkT142_BlockType                      AS parenttype,
  b.offset                                      AS byteoffset,
  b.length                                      AS bytelength,
  dt.name                                       AS datatype,
  s.definition                                  AS scaling,
  dbo.GetTextFromLang(bv.fkT190_Text_Unit, '$Lang') AS unit,
  dbo.GetTextFromLang(b.fkT190_Text, '$Lang')       AS name,
  meta.asMinRange                               AS minrange,
  meta.asMaxRange                               AS maxrange
FROM T100_EcuVariant ev
JOIN T144_BlockChild bc ON bc.fkT100_EcuVariant = ev.id
JOIN T141_Block b       ON b.id = bc.fkT141_Block_Child AND b.fkT142_BlockType = 8
JOIN T141_Block bparent ON bparent.id = bc.fkT141_Block_Parent
JOIN T150_BlockValue bvparent ON bvparent.fkT141_Block = bparent.id
LEFT JOIN T150_BlockValue bv ON bv.fkT141_Block = b.id
LEFT JOIN T143_BlockDataType dt ON dt.id = b.fkT143_BlockDataType
LEFT JOIN T155_Scaling s ON s.id = bv.fkT155_Scaling
LEFT JOIN T148_BlockMetaPARA meta ON meta.fkT141_Block = b.id AND meta.fkT100_EcuVariant = ev.id
WHERE ev.id = $EcuVariantId AND b.fkT190_Text != 0
  AND bvparent.CompareValue IS NOT NULL AND bvparent.CompareValue <> ''
ORDER BY bvparent.CompareValue, b.offset
"@

$cols = @("identifier","parenttype","byteoffset","bytelength","datatype","scaling","unit","name","minrange","maxrange")
Set-Content -LiteralPath $csv -Value ($cols -join "`t") -Encoding UTF8
$n = 0
foreach ($r in $rows.Rows) {
    $vals = @()
    foreach ($c in $cols) {
        $v = [string]$r[$c]
        $v = $v -replace "`t", " " -replace "`r", " " -replace "`n", " "
        $vals += $v
    }
    Add-Content -LiteralPath $csv -Value ($vals -join "`t") -Encoding UTF8
    $n++
}
W "wrote $n parameter rows to carcom-ecm.csv"
W "done."
