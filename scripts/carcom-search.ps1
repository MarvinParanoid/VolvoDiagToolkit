<#
.SYNOPSIS
    Searches CarCom for DPF / soot / regeneration parameters on the DV6b ECM,
    across every block type and both English and Russian text, to find anything
    the REID live-data list missed.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\carcom-search.ps1 `
        -OutDir "Z:\projects\volvo-toolkit\logs"
#>

[CmdletBinding()]
param(
    [string]$OutDir = ".",
    [string]$Server
)

$ErrorActionPreference = 'Continue'
$report = Join-Path $OutDir "carcom-search.txt"
function W { param([string]$t = "") ; Write-Host $t ; Add-Content -LiteralPath $report -Value $t -Encoding UTF8 }
Set-Content -LiteralPath $report -Value "CarCom DPF/soot search $(Get-Date)" -Encoding UTF8

function Invoke-Sql {
    param([string]$ConnString, [string]$Query)
    $conn = New-Object System.Data.SqlClient.SqlConnection $ConnString
    $conn.Open()
    try {
        $cmd = $conn.CreateCommand(); $cmd.CommandText = $Query; $cmd.CommandTimeout = 240
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
        if ($t.Rows.Count -eq 0) { W "  (no rows)" }
        foreach ($row in $t.Rows) {
            $vals = @(); foreach ($c in $cols) { $vals += (([string]$row[$c]) -replace "`r"," " -replace "`n"," ") }
            W ("  " + ($vals -join " | "))
        }
    } catch { W "  (error: $($_.Exception.Message))" }
}

# DV6b variants (same set that matched our engine).
$dv6b = "486,802,804,965,1460,1772,2076,2496,2759"

# Keyword filter. English only, kept ASCII so PowerShell 2.0 reads the script
# correctly - the DB stores English names too, and the text join spans every
# language, so an English match still finds the block.
$kw = "(txt.data LIKE '%particul%' OR txt.data LIKE '%soot%' OR txt.data LIKE '%regener%' " +
      "OR txt.data LIKE '%DPF%' OR txt.data LIKE '%FAP%' OR txt.data LIKE '%ash%' " +
      "OR txt.data LIKE '%filter%' OR txt.data LIKE '%distance since%' OR txt.data LIKE '%loading%' " +
      "OR txt.data LIKE '%trap%')"

# 1. Any PARAM (BlockType 8), ANY parent block type, whose name matches.
Dump "DPF/soot/regen PARAM values on DV6b (all parent types)" @"
SELECT DISTINCT
  bvparent.CompareValue AS identifier, bpt.identifier AS parenttype,
  b.length AS bits, dt.name AS datatype, s.definition AS scaling,
  dbo.GetTextFromLang(bv.fkT190_Text_Unit,'en-US') AS unit,
  dbo.GetTextFromLang(b.fkT190_Text,'en-US') AS name_en,
  dbo.GetTextFromLang(b.fkT190_Text,'ru-RU') AS name_ru
FROM T100_EcuVariant ev
JOIN T144_BlockChild bc ON bc.fkT100_EcuVariant = ev.id
JOIN T141_Block b ON b.id = bc.fkT141_Block_Child AND b.fkT142_BlockType = 8
JOIN T141_Block bp ON bp.id = bc.fkT141_Block_Parent
JOIN T142_BlockType bpt ON bpt.id = bp.fkT142_BlockType
LEFT JOIN T150_BlockValue bvparent ON bvparent.fkT141_Block = bp.id
LEFT JOIN T150_BlockValue bv ON bv.fkT141_Block = b.id
LEFT JOIN T143_BlockDataType dt ON dt.id = b.fkT143_BlockDataType
LEFT JOIN T155_Scaling s ON s.id = bv.fkT155_Scaling
JOIN T191_TextData txt ON txt.fkT190_Text = b.fkT190_Text
WHERE ev.id IN ($dv6b) AND $kw
ORDER BY name_en, identifier
"@

# 2. Any BLOCK of any type (not just PARAM) whose name matches - catches
#    routines, DTC blocks, service blocks, status identifiers.
Dump "any DPF/soot/regen block on DV6b (all block types)" @"
SELECT DISTINCT
  bt.identifier AS blocktype,
  cv.CompareValue AS compareValue,
  dbo.GetTextFromLang(b.fkT190_Text,'en-US') AS name_en,
  dbo.GetTextFromLang(b.fkT190_Text,'ru-RU') AS name_ru
FROM T100_EcuVariant ev
JOIN T144_BlockChild bc ON bc.fkT100_EcuVariant = ev.id
JOIN T141_Block b ON b.id = bc.fkT141_Block_Child
JOIN T142_BlockType bt ON bt.id = b.fkT142_BlockType
LEFT JOIN T150_BlockValue cv ON cv.fkT141_Block = b.id
JOIN T191_TextData txt ON txt.fkT190_Text = b.fkT190_Text
WHERE ev.id IN ($dv6b) AND $kw
ORDER BY blocktype, name_en
"@

# 3. Full REID + other live-data parents for variant 486, ALL names (so we can
#    eyeball anything soot/regen-ish that the keyword filter missed).
Dump "all live-data (REID/BLOFF/RTOFF) params, variant 486" @"
SELECT DISTINCT bvparent.CompareValue AS id, bpt.identifier AS ptype,
  s.definition AS scaling,
  dbo.GetTextFromLang(bv.fkT190_Text_Unit,'en-US') AS unit,
  dbo.GetTextFromLang(b.fkT190_Text,'ru-RU') AS name_ru,
  dbo.GetTextFromLang(b.fkT190_Text,'en-US') AS name_en
FROM T100_EcuVariant ev
JOIN T144_BlockChild bc ON bc.fkT100_EcuVariant = ev.id
JOIN T141_Block b ON b.id = bc.fkT141_Block_Child AND b.fkT142_BlockType = 8
JOIN T141_Block bp ON bp.id = bc.fkT141_Block_Parent
JOIN T142_BlockType bpt ON bpt.id = bp.fkT142_BlockType
JOIN T150_BlockValue bvparent ON bvparent.fkT141_Block = bp.id
LEFT JOIN T150_BlockValue bv ON bv.fkT141_Block = b.id
LEFT JOIN T155_Scaling s ON s.id = bv.fkT155_Scaling
WHERE ev.id = 486 AND bvparent.CompareValue IS NOT NULL AND bvparent.CompareValue <> ''
ORDER BY ptype, id
"@

W ""; W "done."
