<#
.SYNOPSIS
    Extracts one module's full parameter set from CarCom (identifier, offset,
    scaling formula, unit, name, range) to CSV. Generic: pick the module by
    name. Same join as the ECM extraction; reusable for CEM, ABS, DIM, ...

.PARAMETER NameLike
    ECU name pattern, e.g. "Central electronic module (D2)". Wrapped in %..%.

.PARAMETER Ids
    Optional comma-separated hex identifiers we captured on the wire, used to
    rank variants by match (e.g. "1A02,1A04"). If none match, the richest
    variant is used.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\carcom-module.ps1 `
        -NameLike "Central electronic module (D2)" -Ids "1A02,1A04" `
        -OutDir "Z:\projects\volvo-toolkit\logs" -Out cem
#>

[CmdletBinding()]
param(
    [string]$NameLike = "Central electronic module (D2)",
    [string]$Ids = "",
    [string]$Out = "module",
    [string]$OutDir = ".",
    [int]$EcuVariantId = 0,
    [string]$Lang = "en-US",
    [string]$Server
)

$ErrorActionPreference = 'Continue'
$report = Join-Path $OutDir "carcom-$Out.txt"
$csv = Join-Path $OutDir "carcom-$Out.csv"
function W { param([string]$t = "") ; Write-Host $t ; Add-Content -LiteralPath $report -Value $t -Encoding UTF8 }
Set-Content -LiteralPath $report -Value "CarCom module extract '$NameLike' $(Get-Date)" -Encoding UTF8

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

$safeName = $NameLike -replace "'", "''"

# Variants of this module, with parameter counts and comm address.
W ""
W "variants of '$NameLike' (id | DiagNumber | commAddr | #params):"
$variants = Invoke-Sql $connString @"
SELECT v.id, v.identifier AS diagnum, cfg.commAddress AS addr,
       COUNT(DISTINCT b.id) AS params
FROM T101_Ecu e
JOIN T100_EcuVariant v ON v.fkT101_Ecu = e.id
LEFT JOIN T120_Config_EcuVariant cev ON cev.fkT100_EcuVariant = v.id
LEFT JOIN T121_Config cfg ON cfg.id = cev.fkT121_Config
LEFT JOIN T144_BlockChild bc ON bc.fkT100_EcuVariant = v.id
LEFT JOIN T141_Block b ON b.id = bc.fkT141_Block_Child AND b.fkT142_BlockType = 8
WHERE e.name LIKE '%$safeName%'
GROUP BY v.id, v.identifier, cfg.commAddress
ORDER BY params DESC
"@
$best = 0; $bestParams = -1
foreach ($r in $variants.Rows) {
    W ("  {0} | {1} | {2} | {3}" -f $r["id"], $r["diagnum"], $r["addr"], $r["params"])
    if ([int]$r["params"] -gt $bestParams) { $bestParams = [int]$r["params"]; $best = [int]$r["id"] }
}

# Optional fingerprint by captured ids.
if ($Ids -and $EcuVariantId -eq 0) {
    $idList = ($Ids.Split(",") | ForEach-Object {
        $h = $_.Trim(); "'0x" + $h + "','0x" + ("{0:X4}" -f [Convert]::ToInt32($h,16)) + "'"
    }) -join ","
    W ""
    W "variants carrying our captured ids ($Ids):"
    $match = Invoke-Sql $connString @"
SELECT v.id, COUNT(DISTINCT bvparent.CompareValue) AS hits
FROM T101_Ecu e
JOIN T100_EcuVariant v ON v.fkT101_Ecu = e.id
JOIN T144_BlockChild bc ON bc.fkT100_EcuVariant = v.id
JOIN T141_Block b ON b.id = bc.fkT141_Block_Child AND b.fkT142_BlockType = 8
JOIN T141_Block bp ON bp.id = bc.fkT141_Block_Parent
JOIN T150_BlockValue bvparent ON bvparent.fkT141_Block = bp.id
WHERE e.name LIKE '%$safeName%' AND bvparent.CompareValue IN ($idList)
GROUP BY v.id
ORDER BY hits DESC
"@
    $mh = -1
    foreach ($r in $match.Rows) {
        W ("  variant {0}: {1} hits" -f $r["id"], $r["hits"])
        if ([int]$r["hits"] -gt $mh) { $mh = [int]$r["hits"]; if ($mh -ge 1) { $EcuVariantId = [int]$r["id"] } }
    }
}

if ($EcuVariantId -eq 0) { $EcuVariantId = $best }
W ""
W "dumping parameters for EcuVariant $EcuVariantId -> $csv"

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
W "wrote $n rows to carcom-$Out.csv. done."
