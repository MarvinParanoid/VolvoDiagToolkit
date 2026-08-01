<#
.SYNOPSIS
    Finds the exact ECM variant whose identifiers match the ones our car
    answered on the wire (0x3A/0x7E/0xAE ...), then dumps its full parameter
    set with formulas and units to CSV.

.DESCRIPTION
    The on-wire request is [C8+n][commAddress][A6][id_hi][id_lo][01] to CAN
    0x0FFFFE, 29-bit - confirmed by T121_Config (canAddress FFFFE, commAddress
    11, addressSize 29). The identifier on the wire is a CarCom REID. This
    fingerprints engine variants by our captured ids (trying both the 4-digit
    '0x007E' and 2-digit '0x7E' forms), picks the best match, and dumps it.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\carcom-final.ps1 `
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
$report = Join-Path $OutDir "carcom-final.txt"
$csv = Join-Path $OutDir "carcom-final.csv"
function W { param([string]$t = "") ; Write-Host $t ; Add-Content -LiteralPath $report -Value $t -Encoding UTF8 }
Set-Content -LiteralPath $report -Value "CarCom final $(Get-Date)  lang=$Lang" -Encoding UTF8

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

# Our captured on-wire identifiers, both hex-string forms CarCom might store.
$idList = "'0x0005','0x05','0x002E','0x2E','0x003A','0x3A','0x0050','0x50'," +
          "'0x0063','0x63','0x007E','0x7E','0x009E','0x9E','0x00A7','0xA7','0x00AE','0xAE'"

if ($EcuVariantId -eq 0) {
    W ""
    W "engine variants ranked by how many of our on-wire ids they carry:"
    $match = Invoke-Sql $connString @"
SELECT TOP 20 v.id, v.identifier AS diagnum, e.name AS ecu, cfg.commAddress AS addr,
       COUNT(DISTINCT bvparent.CompareValue) AS hits
FROM T100_EcuVariant v
JOIN T101_Ecu e ON e.id = v.fkT101_Ecu
JOIN T102_EcuType et ON et.id = e.fkT102_EcuType AND et.identifier = '284101'
LEFT JOIN T120_Config_EcuVariant cev ON cev.fkT100_EcuVariant = v.id
LEFT JOIN T121_Config cfg ON cfg.id = cev.fkT121_Config
JOIN T144_BlockChild bc ON bc.fkT100_EcuVariant = v.id
JOIN T141_Block b ON b.id = bc.fkT141_Block_Child AND b.fkT142_BlockType = 8
JOIN T141_Block bp ON bp.id = bc.fkT141_Block_Parent
JOIN T150_BlockValue bvparent ON bvparent.fkT141_Block = bp.id
WHERE bvparent.CompareValue IN ($idList)
GROUP BY v.id, v.identifier, e.name, cfg.commAddress
ORDER BY hits DESC
"@
    $bestHits = -1
    foreach ($r in $match.Rows) {
        W ("  {0} | {1} | {2} | addr {3} | {4} hits" -f $r["id"], $r["diagnum"], $r["ecu"], $r["addr"], $r["hits"])
        if ([int]$r["hits"] -gt $bestHits) { $bestHits = [int]$r["hits"]; $EcuVariantId = [int]$r["id"] }
    }
    W ("selected variant {0} ({1} id hits)" -f $EcuVariantId, $bestHits)
}

# Show exactly what the selected variant calls our captured ids, so we can
# validate scale against the raw values we recorded.
W ""
W "our captured ids in variant $EcuVariantId:"
$ours = Invoke-Sql $connString @"
SELECT bvparent.CompareValue AS id, bpt.identifier AS ptype,
       b.length AS bits, dt.name AS datatype, s.definition AS scaling,
       dbo.GetTextFromLang(bv.fkT190_Text_Unit,'$Lang') AS unit,
       dbo.GetTextFromLang(b.fkT190_Text,'$Lang') AS name
FROM T100_EcuVariant ev
JOIN T144_BlockChild bc ON bc.fkT100_EcuVariant = ev.id
JOIN T141_Block b ON b.id = bc.fkT141_Block_Child AND b.fkT142_BlockType = 8
JOIN T141_Block bp ON bp.id = bc.fkT141_Block_Parent
JOIN T142_BlockType bpt ON bpt.id = bp.fkT142_BlockType
JOIN T150_BlockValue bvparent ON bvparent.fkT141_Block = bp.id
LEFT JOIN T150_BlockValue bv ON bv.fkT141_Block = b.id
LEFT JOIN T143_BlockDataType dt ON dt.id = b.fkT143_BlockDataType
LEFT JOIN T155_Scaling s ON s.id = bv.fkT155_Scaling
WHERE ev.id = $EcuVariantId AND bvparent.CompareValue IN ($idList)
ORDER BY bvparent.CompareValue
"@
foreach ($r in $ours.Rows) {
    W ("  {0} [{1}] {2}bit {3} | {4} | {5} | {6}" -f $r["id"], $r["ptype"], $r["bits"], $r["datatype"], $r["scaling"], $r["unit"], $r["name"])
}

# Full parameter dump for the selected variant.
W ""
W "dumping all parameters of variant $EcuVariantId -> $csv"
$rows = Invoke-Sql $connString @"
SELECT DISTINCT
  bvparent.CompareValue AS identifier, bpt.identifier AS parenttype,
  b.length AS bits, dt.name AS datatype, s.definition AS scaling,
  dbo.GetTextFromLang(bv.fkT190_Text_Unit,'$Lang') AS unit,
  dbo.GetTextFromLang(b.fkT190_Text,'$Lang') AS name,
  meta.asMinRange AS minrange, meta.asMaxRange AS maxrange
FROM T100_EcuVariant ev
JOIN T144_BlockChild bc ON bc.fkT100_EcuVariant = ev.id
JOIN T141_Block b ON b.id = bc.fkT141_Block_Child AND b.fkT142_BlockType = 8
JOIN T141_Block bp ON bp.id = bc.fkT141_Block_Parent
JOIN T142_BlockType bpt ON bpt.id = bp.fkT142_BlockType
JOIN T150_BlockValue bvparent ON bvparent.fkT141_Block = bp.id
LEFT JOIN T150_BlockValue bv ON bv.fkT141_Block = b.id
LEFT JOIN T143_BlockDataType dt ON dt.id = b.fkT143_BlockDataType
LEFT JOIN T155_Scaling s ON s.id = bv.fkT155_Scaling
LEFT JOIN T148_BlockMetaPARA meta ON meta.fkT141_Block = b.id AND meta.fkT100_EcuVariant = ev.id
WHERE ev.id = $EcuVariantId AND b.fkT190_Text != 0
  AND bvparent.CompareValue IS NOT NULL AND bvparent.CompareValue <> ''
ORDER BY bvparent.CompareValue
"@
$cols = @("identifier","parenttype","bits","datatype","scaling","unit","name","minrange","maxrange")
Set-Content -LiteralPath $csv -Value ($cols -join "`t") -Encoding UTF8
$n = 0
foreach ($r in $rows.Rows) {
    $vals = @()
    foreach ($c in $cols) { $vals += (([string]$r[$c]) -replace "`t"," " -replace "`r"," " -replace "`n"," ") }
    Add-Content -LiteralPath $csv -Value ($vals -join "`t") -Encoding UTF8
    $n++
}
W "wrote $n rows. done."
