<#
.SYNOPSIS
    Finds the Phone Module variants (Telephone 394201 and Bluetooth Phone Module
    394202) and dumps a variant's configuration options - to locate the setting
    that enables Bluetooth audio streaming. Read-only.

.EXAMPLE
    # 1) discover the variants (uses a harmless default for the config dump):
    powershell -ExecutionPolicy Bypass -File "Z:\projects\volvo-toolkit\scripts\carcom-phm.ps1" `
        -OutDir "Z:\projects\volvo-toolkit\logs"
    # 2) then re-run with the Bluetooth Phone Module variant id it printed:
    powershell -ExecutionPolicy Bypass -File "Z:\projects\volvo-toolkit\scripts\carcom-phm.ps1" `
        -EcuVariantId <id> -Out bt -OutDir "Z:\projects\volvo-toolkit\logs"
#>

[CmdletBinding()]
param(
    [int]$EcuVariantId = 509,          # 509 = the profile-default Telephone
    [string]$Out = "phm",
    [string]$OutDir = ".",
    [string]$Lang = "en-US",
    [string]$Server
)

$ErrorActionPreference = 'Continue'
if (-not (Test-Path -LiteralPath $OutDir)) { New-Item -ItemType Directory -Force -Path $OutDir | Out-Null }
$report = Join-Path $OutDir "carcom-phm-$Out.txt"
function W { param([string]$t = "") ; Write-Host $t ; Add-Content -LiteralPath $report -Value $t -Encoding UTF8 }
Set-Content -LiteralPath $report -Value "CarCom PHM explore variant $EcuVariantId $(Get-Date)" -Encoding UTF8

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

# 1. All variants of both phone ecutypes, so we can pick the Bluetooth one.
Dump "Phone Module variants (ecutype 394201 Telephone / 394202 Bluetooth)" @"
SELECT v.id AS variantId, v.identifier AS diagnum, t.identifier AS ecutype, t.description
FROM T100_EcuVariant v
JOIN T101_Ecu e ON e.id = v.fkT101_Ecu
JOIN T102_EcuType t ON t.id = e.fkT102_EcuType
WHERE t.identifier IN ('394201','394202')
ORDER BY t.identifier, v.id
"@

# 2. The configuration options of the chosen variant, with value labels — the
#    place a 'Bluetooth / streaming / audio' option would appear.
$csv = Join-Path $OutDir "carcom-phm-$Out.csv"
W ""; W "dumping config options of variant $EcuVariantId -> $csv"
$rows = Invoke-Sql $connString @"
SELECT
  bvparent.CompareValue AS identifier,
  dbo.GetTextFromLang(b.fkT190_Text,'$Lang') AS param,
  b.offset AS bitoffset, b.length AS bitlen,
  bv.CompareValue AS value,
  dbo.GetTextFromLang(bv.fkT190_Text_Value,'$Lang') AS valuetext,
  s.definition AS scaling
FROM T144_BlockChild bc
JOIN T141_Block b ON b.id = bc.fkT141_Block_Child AND b.fkT142_BlockType = 8
JOIN T141_Block bp ON bp.id = bc.fkT141_Block_Parent
JOIN T142_BlockType bpt ON bpt.id = bp.fkT142_BlockType AND bpt.identifier IN ('REID','BLOFF','RTOFF')
JOIN T150_BlockValue bvparent ON bvparent.fkT141_Block = bp.id
LEFT JOIN T150_BlockValue bv ON bv.fkT141_Block = b.id
LEFT JOIN T155_Scaling s ON s.id = bv.fkT155_Scaling
WHERE bc.fkT100_EcuVariant = $EcuVariantId
  AND bvparent.CompareValue IS NOT NULL AND bvparent.CompareValue <> ''
ORDER BY bvparent.CompareValue, b.offset, bv.sortorder
"@
$cols = @("identifier","param","bitoffset","bitlen","value","valuetext","scaling")
Set-Content -LiteralPath $csv -Value ($cols -join "`t") -Encoding UTF8
$n = 0
foreach ($r in $rows.Rows) {
    $v = @(); foreach ($c in $cols) { $v += (([string]$r[$c]) -replace "`t"," " -replace "`r"," " -replace "`n"," ") }
    Add-Content -LiteralPath $csv -Value ($v -join "`t") -Encoding UTF8
    $n++
}
W "wrote $n option rows."

# 3. Anything whose text mentions streaming/bluetooth/audio, across ALL text —
#    a quick net in case the option lives under a name we did not expect.
Dump "params mentioning bluetooth/stream/audio/media for variant $EcuVariantId" @"
SELECT DISTINCT bvparent.CompareValue AS identifier,
       dbo.GetTextFromLang(b.fkT190_Text,'$Lang') AS param,
       dbo.GetTextFromLang(bv.fkT190_Text_Value,'$Lang') AS valuetext
FROM T144_BlockChild bc
JOIN T141_Block b ON b.id = bc.fkT141_Block_Child
JOIN T141_Block bp ON bp.id = bc.fkT141_Block_Parent
JOIN T150_BlockValue bvparent ON bvparent.fkT141_Block = bp.id
LEFT JOIN T150_BlockValue bv ON bv.fkT141_Block = b.id
WHERE bc.fkT100_EcuVariant = $EcuVariantId
  AND (dbo.GetTextFromLang(b.fkT190_Text,'$Lang') LIKE '%luetooth%'
    OR dbo.GetTextFromLang(b.fkT190_Text,'$Lang') LIKE '%tream%'
    OR dbo.GetTextFromLang(b.fkT190_Text,'$Lang') LIKE '%A2DP%'
    OR dbo.GetTextFromLang(b.fkT190_Text,'$Lang') LIKE '%udio%'
    OR dbo.GetTextFromLang(b.fkT190_Text,'$Lang') LIKE '%edia%')
"@

W ""; W "done. Send carcom-phm-$Out.txt and .csv back."
