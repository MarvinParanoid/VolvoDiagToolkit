<#
.SYNOPSIS
    Cracks how CarCom turns a logical identifier into an on-wire request, by
    dumping the IDENT block metadata (T146_BlockMetaIDENT) and the full request
    subtree for a couple of known parameters.

.DESCRIPTION
    The logical identifier VIDA shows (e.g. 0x000B) is not what goes on the
    wire; the request is assembled from IDENT-family child blocks (ADDR, REID,
    BLOFF, REOFF, RTOFF). This dumps that metadata and, for a chosen parameter,
    the whole block tree, so we can map our captured frames (CD 11 A6 00 XX 01)
    to CarCom parameters and read the rest with correct requests. Read-only.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\carcom-ident.ps1 `
        -OutDir "Z:\projects\volvo-toolkit\logs" -EcuVariantId 112
#>

[CmdletBinding()]
param(
    [string]$OutDir = ".",
    [int]$EcuVariantId = 112,
    [string]$Server
)

$ErrorActionPreference = 'Continue'
$report = Join-Path $OutDir "carcom-ident.txt"
function W { param([string]$t = "") ; Write-Host $t ; Add-Content -LiteralPath $report -Value $t -Encoding UTF8 }
Set-Content -LiteralPath $report -Value "CarCom ident/request $(Get-Date)  variant=$EcuVariantId" -Encoding UTF8

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
    W ""; W "-- $Title"
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

# What columns does the IDENT metadata table carry?
Dump "T146_BlockMetaIDENT columns" @"
SELECT COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_NAME='T146_BlockMetaIDENT' ORDER BY ORDINAL_POSITION
"@
Dump "T146_BlockMetaIDENT sample" "SELECT TOP 20 * FROM T146_BlockMetaIDENT"

# Config for our variant: comm address, protocol, init.
Dump "T121_Config columns" @"
SELECT COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_NAME='T121_Config' ORDER BY ORDINAL_POSITION
"@
Dump "config for variant $EcuVariantId" @"
SELECT c.* FROM T121_Config c
JOIN T120_Config_EcuVariant cev ON cev.fkT121_Config = c.id
WHERE cev.fkT100_EcuVariant = $EcuVariantId
"@

# For a few known parameters, walk the tree: PARAM block -> its parent (the
# IDENT-family block carrying the request) -> that parent's siblings under the
# same IDENT root. Show each block's type, CompareValue and IDENT meta.
foreach ($pname in @('Intake manifold pressure', 'Atmospheric pressure', 'Air mass/stroke')) {
    Dump "request tree for PARAM like '$pname'" @"
SELECT TOP 40
  para.id AS paramBlock, dbo.GetTextFromLang(para.fkT190_Text,'en-US') AS paramName,
  parent.id AS parentBlock, ptype.identifier AS parentType,
  pv.CompareValue AS parentCompareValue,
  meta.*
FROM T144_BlockChild bc
JOIN T141_Block para   ON para.id = bc.fkT141_Block_Child AND para.fkT142_BlockType = 8
JOIN T141_Block parent ON parent.id = bc.fkT141_Block_Parent
JOIN T142_BlockType ptype ON ptype.id = parent.fkT142_BlockType
LEFT JOIN T150_BlockValue pv ON pv.fkT141_Block = parent.id
LEFT JOIN T146_BlockMetaIDENT meta ON meta.fkT141_Block = parent.id
WHERE bc.fkT100_EcuVariant = $EcuVariantId
  AND dbo.GetTextFromLang(para.fkT190_Text,'en-US') = '$pname'
"@
}

# Also: the IDENT root and ALL its children for the intake-manifold request,
# to see the ADDR/REID/offset pieces that form the on-wire bytes.
Dump "full IDENT subtree around intake manifold pressure" @"
SELECT TOP 60
  root.id AS rootBlock, rt.identifier AS rootType,
  child.id AS childBlock, ct.identifier AS childType,
  dbo.GetTextFromLang(child.fkT190_Text,'en-US') AS childName,
  cv.CompareValue AS childCompareValue,
  child.offset AS childOffset, child.length AS childLength,
  m.*
FROM T144_BlockChild bcRoot
JOIN T141_Block para ON para.id = bcRoot.fkT141_Block_Child AND para.fkT142_BlockType = 8
   AND dbo.GetTextFromLang(para.fkT190_Text,'en-US') = 'Intake manifold pressure'
JOIN T141_Block root ON root.id = bcRoot.fkT141_Block_Parent
JOIN T142_BlockType rt ON rt.id = root.fkT142_BlockType
JOIN T144_BlockChild bcChild ON bcChild.fkT141_Block_Parent = root.id
   AND bcChild.fkT100_EcuVariant = $EcuVariantId
JOIN T141_Block child ON child.id = bcChild.fkT141_Block_Child
JOIN T142_BlockType ct ON ct.id = child.fkT142_BlockType
LEFT JOIN T150_BlockValue cv ON cv.fkT141_Block = child.id
LEFT JOIN T146_BlockMetaIDENT m ON m.fkT141_Block = child.id
WHERE bcRoot.fkT100_EcuVariant = $EcuVariantId
"@

W ""; W "done."
