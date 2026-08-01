<#
.SYNOPSIS
    Explores how CarCom stores an ECU's configuration / programmed values (the
    coded options VIDA reads and writes): which block type carries them, whether
    they are read with the same on-wire service as live data, their scaling and
    allowed values. First step to READING configuration. Read-only.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File "Z:\projects\volvo-toolkit\scripts\carcom-config.ps1" `
        -EcuVariantId 965 -Out ecm -OutDir "Z:\projects\volvo-toolkit\logs"
    powershell -ExecutionPolicy Bypass -File "Z:\projects\volvo-toolkit\scripts\carcom-config.ps1" `
        -EcuVariantId 1175 -Out cem -OutDir "Z:\projects\volvo-toolkit\logs"
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
$report = Join-Path $OutDir "carcom-config-$Out.txt"
function W { param([string]$t = "") ; Write-Host $t ; Add-Content -LiteralPath $report -Value $t -Encoding UTF8 }
Set-Content -LiteralPath $report -Value "CarCom config explore variant $EcuVariantId $(Get-Date)" -Encoding UTF8

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

# 1. Every block type this ECU actually has, most-populated first. The config /
#    programmed-value type shows up here next to PARAM (live data) and DTC.
Dump "block types present for variant $EcuVariantId (id | type | n blocks)" @"
SELECT bt.id, bt.identifier AS blocktype, bt.metaTable, COUNT(*) AS n
FROM T144_BlockChild bc
JOIN T141_Block b ON b.id = bc.fkT141_Block_Child
JOIN T142_BlockType bt ON bt.id = b.fkT142_BlockType
WHERE bc.fkT100_EcuVariant = $EcuVariantId
GROUP BY bt.id, bt.identifier, bt.metaTable
ORDER BY n DESC
"@

# 1b. THE key breakdown: for every PARAM (type 8) block, the TYPE of its parent
#     request block. Live data, configuration and freeze-frame differ only by
#     this parent category. The parent's CompareValue is the on-wire request.
Dump "parent request-block types carrying PARAMs for variant $EcuVariantId" @"
SELECT bpt.id, bpt.identifier AS parenttype,
       COUNT(DISTINCT bp.id) AS requests, COUNT(*) AS params
FROM T144_BlockChild bc
JOIN T141_Block b ON b.id = bc.fkT141_Block_Child AND b.fkT142_BlockType = 8
JOIN T141_Block bp ON bp.id = bc.fkT141_Block_Parent
JOIN T142_BlockType bpt ON bpt.id = bp.fkT142_BlockType
WHERE bc.fkT100_EcuVariant = $EcuVariantId
GROUP BY bpt.id, bpt.identifier
ORDER BY params DESC
"@

# 2. All 85 block types with their parent - to read the taxonomy and spot the
#    PARAMETER / PROGRAMMED / CONFIG / CARCONFIG families.
Dump "T142_BlockType taxonomy (all)" @"
SELECT bt.id, bt.identifier, p.identifier AS parent, bt.metaTable
FROM T142_BlockType bt
LEFT JOIN T142_BlockType p ON p.id = bt.fkT142_BlockType_Parent
ORDER BY bt.id
"@

# 3. The stored procedures VIDA uses to read parameters / configuration. Their
#    joins reveal the on-wire request block and whether it is the A6 read service.
foreach ($p in @('se_GetParameters','se_GetParameterValuesByEcuType',
                 'vadis_GetParameterData','vadis_GetAllEcuDataForProfile',
                 'service_GetParameters','nevis_GetParametersByEcuVariant')) {
    W ""; W "==== PROC $p"
    try {
        $d = Invoke-Sql $connString "SELECT OBJECT_DEFINITION(OBJECT_ID('dbo.$p')) AS def"
        $def = [string]$d.Rows[0][0]
        if ($def) { W $def } else { W "  (not found)" }
    } catch { W "  (error: $($_.Exception.Message))" }
}

# 4. The configuration catalogue: REID params (read via A6, which we already
#    support) that carry ENUMERATED value labels - the programmed / coded
#    options VIDA shows as "configuration". identifier -> param -> value:label.
$csv = Join-Path $OutDir "carcom-config-$Out.csv"
W ""; W "dumping configuration catalogue -> $csv"
$rows = Invoke-Sql $connString @"
SELECT
  bvparent.CompareValue AS identifier,
  dbo.GetTextFromLang(b.fkT190_Text,'$Lang') AS param,
  b.offset AS bitoffset, b.length AS bitlen, b.fkT143_BlockDataType AS datatype,
  bv.operator AS operator, bv.CompareValue AS value,
  dbo.GetTextFromLang(bv.fkT190_Text_Value,'$Lang') AS valuetext,
  s.definition AS scaling,
  dbo.GetTextFromLang(bv.fkT190_Text_Unit,'$Lang') AS unit,
  bv.sortorder AS sortorder
FROM T144_BlockChild bc
JOIN T141_Block b ON b.id = bc.fkT141_Block_Child AND b.fkT142_BlockType = 8
JOIN T141_Block bp ON bp.id = bc.fkT141_Block_Parent
JOIN T142_BlockType bpt ON bpt.id = bp.fkT142_BlockType AND bpt.identifier IN ('REID','BLOFF','RTOFF')
JOIN T150_BlockValue bvparent ON bvparent.fkT141_Block = bp.id
JOIN T150_BlockValue bv ON bv.fkT141_Block = b.id
LEFT JOIN T155_Scaling s ON s.id = bv.fkT155_Scaling
WHERE bc.fkT100_EcuVariant = $EcuVariantId
  AND bv.fkT190_Text_Value IS NOT NULL AND bv.fkT190_Text_Value <> 0
  AND bvparent.CompareValue IS NOT NULL AND bvparent.CompareValue <> ''
ORDER BY bvparent.CompareValue, b.offset, bv.sortorder
"@
$cols = @("identifier","param","bitoffset","bitlen","datatype","operator","value","valuetext","scaling","unit","sortorder")
Set-Content -LiteralPath $csv -Value ($cols -join "`t") -Encoding UTF8
$n = 0
foreach ($r in $rows.Rows) {
    $v = @(); foreach ($c in $cols) { $v += (([string]$r[$c]) -replace "`t"," " -replace "`r"," " -replace "`n"," ") }
    Add-Content -LiteralPath $csv -Value ($v -join "`t") -Encoding UTF8
    $n++
}
W "wrote $n configuration value rows."
# 5. The static car-configuration block map: BLOFF / IDENT fields (VIN, market
#    code, car-config numbers, audio, sub type) with byte offsets. Read via the
#    B9 identity service we already decode - so this maps that block's payload.
$bcsv = Join-Path $OutDir "carcom-ident-$Out.csv"
W ""; W "dumping identity/config field map -> $bcsv"
$brows = Invoke-Sql $connString @"
SELECT
  bvparent.CompareValue AS identifier, bpt.identifier AS reqtype,
  dbo.GetTextFromLang(b.fkT190_Text,'$Lang') AS field,
  b.offset AS bitoffset, b.length AS bitlen,
  dt.name AS datatype, s.definition AS scaling,
  dbo.GetTextFromLang(bv.fkT190_Text_Unit,'$Lang') AS unit
FROM T144_BlockChild bc
JOIN T141_Block b ON b.id = bc.fkT141_Block_Child AND b.fkT142_BlockType = 8
JOIN T141_Block bp ON bp.id = bc.fkT141_Block_Parent
JOIN T142_BlockType bpt ON bpt.id = bp.fkT142_BlockType AND bpt.identifier IN ('BLOFF','RTOFF','ADDR')
JOIN T150_BlockValue bvparent ON bvparent.fkT141_Block = bp.id
LEFT JOIN T150_BlockValue bv ON bv.fkT141_Block = b.id
LEFT JOIN T143_BlockDataType dt ON dt.id = b.fkT143_BlockDataType
LEFT JOIN T155_Scaling s ON s.id = bv.fkT155_Scaling
WHERE bc.fkT100_EcuVariant = $EcuVariantId
  AND bvparent.CompareValue IS NOT NULL AND bvparent.CompareValue <> ''
ORDER BY bvparent.CompareValue, b.offset
"@
$bcols = @("identifier","reqtype","field","bitoffset","bitlen","datatype","scaling","unit")
Set-Content -LiteralPath $bcsv -Value ($bcols -join "`t") -Encoding UTF8
$bn = 0
foreach ($r in $brows.Rows) {
    $v = @(); foreach ($c in $bcols) { $v += (([string]$r[$c]) -replace "`t"," " -replace "`r"," " -replace "`n"," ") }
    Add-Content -LiteralPath $bcsv -Value ($v -join "`t") -Encoding UTF8
    $bn++
}
W "wrote $bn identity/config field rows."
W ""; W "done. Send carcom-config-$Out.txt, carcom-config-$Out.csv, carcom-ident-$Out.csv back."
