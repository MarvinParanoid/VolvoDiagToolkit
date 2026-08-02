<#
.SYNOPSIS
    Finds the Bluetooth / audio-streaming configuration option in CarCom and,
    crucially, WHICH MODULE owns it. That answers the write-feasibility question
    without touching the car: an option on the CEM is CarConfig (0xFC) and needs
    the per-car PIN; an option on ICM / AUD / PHM is module-local and (like the
    ICM clock/temp writes we captured) needs no security. Read-only DB query.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File "Z:\projects\volvo-toolkit\scripts\carcom-bt.ps1" `
        -OutDir "Z:\projects\volvo-toolkit\logs"
#>

[CmdletBinding()]
param(
    [string]$OutDir = ".",
    [string]$Server
)

$ErrorActionPreference = 'Continue'
if (-not (Test-Path -LiteralPath $OutDir)) { New-Item -ItemType Directory -Force -Path $OutDir | Out-Null }
$report = Join-Path $OutDir "carcom-bt.txt"
function W { param([string]$t = "") ; Write-Host $t ; Add-Content -LiteralPath $report -Value $t -Encoding UTF8 }
Set-Content -LiteralPath $report -Value "CarCom Bluetooth/audio-source search $(Get-Date)" -Encoding UTF8

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

$servers = if ($Server) { @($Server) } else { @('.\VIDA', 'localhost\VIDA', '.\SQLEXPRESS') }
$connString = $null
foreach ($srv in $servers) {
    $cs = "Server=$srv;Database=carcom;Integrated Security=SSPI;Connect Timeout=5;"
    try { [void](Invoke-Sql $cs "SELECT 1"); $connString = $cs; W "connected: $srv"; break } catch { }
}
if (-not $connString) { W "could not connect (pass -Server '.\INSTANCE')"; exit 1 }

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
        W ("  ({0} rows)" -f $t.Rows.Count)
    } catch { W "  (error: $($_.Exception.Message))" }
}

# 1. The decisive one: any config VALUE whose text means Bluetooth/streaming,
#    with the module that owns it and the numeric value that selects it.
Dump "config VALUES meaning Bluetooth / streaming (module + value)" @"
SELECT DISTINCT
  e.name                                            AS module,
  et.identifier                                     AS ecutype,
  bt.identifier                                     AS blocktype,
  dbo.GetTextFromLang(b.fkT190_Text,'en-US')        AS option_en,
  bv.CompareValue                                   AS value_num,
  dbo.GetTextFromLang(bv.fkT190_Text_Value,'en-US') AS value_en
FROM T101_Ecu e
JOIN T102_EcuType et ON et.id = e.fkT102_EcuType
JOIN T100_EcuVariant v ON v.fkT101_Ecu = e.id
JOIN T144_BlockChild bc ON bc.fkT100_EcuVariant = v.id
JOIN T141_Block b ON b.id = bc.fkT141_Block_Child
JOIN T142_BlockType bt ON bt.id = b.fkT142_BlockType
JOIN T150_BlockValue bv ON bv.fkT141_Block = b.id
JOIN T191_TextData vt ON vt.fkT190_Text = bv.fkT190_Text_Value
WHERE vt.data LIKE '%bluetooth%' OR vt.data LIKE '%blueto%'
   OR vt.data LIKE '%stream%' OR vt.data LIKE '%A2DP%'
ORDER BY module, option_en, value_num
"@

# 2. Any config OPTION whose NAME mentions audio source / Bluetooth / phone,
#    grouped by module - catches the "audio source" selector even when its
#    values are worded generically.
Dump "config OPTIONS named audio-source / BT / phone (module + name)" @"
SELECT DISTINCT
  e.name                                     AS module,
  et.identifier                              AS ecutype,
  bt.identifier                              AS blocktype,
  dbo.GetTextFromLang(b.fkT190_Text,'en-US') AS option_en,
  dbo.GetTextFromLang(b.fkT190_Text,'ru-RU') AS option_ru
FROM T101_Ecu e
JOIN T102_EcuType et ON et.id = e.fkT102_EcuType
JOIN T100_EcuVariant v ON v.fkT101_Ecu = e.id
JOIN T144_BlockChild bc ON bc.fkT100_EcuVariant = v.id
JOIN T141_Block b ON b.id = bc.fkT141_Block_Child
JOIN T142_BlockType bt ON bt.id = b.fkT142_BlockType
JOIN T191_TextData txt ON txt.fkT190_Text = b.fkT190_Text
WHERE txt.data LIKE '%bluetooth%' OR txt.data LIKE '%blueto%'
   OR txt.data LIKE '%stream%' OR txt.data LIKE '%A2DP%'
   OR txt.data LIKE '%audio source%' OR txt.data LIKE '%media source%'
   OR txt.data LIKE '%source list%'
ORDER BY module, option_en
"@

W ""; W "done. In the first table: if the owning module is CEM -> it is CarConfig"
W "(0xFC) and needs the PIN; if it is ICM / AUD / PHM / BPM -> module-local, no PIN."
