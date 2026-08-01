<#
.SYNOPSIS
    Dumps the CarCom stored-procedure definitions and lookup tables that
    describe how VIDA turns a parameter into a request, scale and unit.

.DESCRIPTION
    The stored procedures encode the exact joins VIDA uses; reading them beats
    guessing the schema. This also samples the scaling table (the formulas as
    text), the small type/category lookups, and the engine ECU rows so we can
    find our variant. Read-only, writes to -OutDir on the shared folder.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\carcom-procs.ps1 `
        -OutDir "Z:\projects\volvo-toolkit\logs"
#>

[CmdletBinding()]
param(
    [string]$OutDir = ".",
    [string]$Server
)

$ErrorActionPreference = 'Continue'
$report = Join-Path $OutDir "carcom-procs.txt"
function W { param([string]$t = "") ; Write-Host $t ; Add-Content -LiteralPath $report -Value $t -Encoding UTF8 }
Set-Content -LiteralPath $report -Value "CarCom procs/lookups $(Get-Date)" -Encoding UTF8

function Invoke-Sql {
    param([string]$ConnString, [string]$Query)
    $conn = New-Object System.Data.SqlClient.SqlConnection $ConnString
    $conn.Open()
    try {
        $cmd = $conn.CreateCommand(); $cmd.CommandText = $Query; $cmd.CommandTimeout = 60
        $adapter = New-Object System.Data.SqlClient.SqlDataAdapter $cmd
        $table = New-Object System.Data.DataTable
        [void]$adapter.Fill($table)
        return ,$table
    }
    finally { $conn.Close() }
}

# ---- connect to the carcom database directly ---------------------------

$servers = if ($Server) { @($Server) } else { @('.\VIDA', 'localhost\VIDA') }
$connString = $null
foreach ($srv in $servers) {
    $cs = "Server=$srv;Database=carcom;Integrated Security=SSPI;Connect Timeout=5;"
    try { [void](Invoke-Sql $cs "SELECT 1"); $connString = $cs; W "connected: $srv"; break }
    catch { }
}
if (-not $connString) { W "could not connect to carcom"; exit 1 }

# ---- stored procedure definitions --------------------------------------

$procs = @(
    'general_GetEcuId',
    'se_GetEcuAddresses', 'se_GetEcuTypes',
    'se_GetIdentifiersByEcuAddress', 'se_GetIdentifiersByEcuType',
    'se_GetParametersByEcuAddress', 'se_GetParametersByEcuType',
    'se_GetParameterValuesByEcuType', 'se_browser_GetBlockValues',
    'service_GetParameters', 'service_GetParameterValues',
    'vadis_GetParameterData', 'vadis_GetEcuVariantData'
)
foreach ($p in $procs) {
    W ""
    W "======================== PROC $p ========================"
    try {
        $d = Invoke-Sql $connString "SELECT OBJECT_DEFINITION(OBJECT_ID('dbo.$p')) AS def"
        $text = if ($d.Rows.Count -gt 0) { [string]$d.Rows[0][0] } else { "" }
        if ($text) { W $text } else { W "  (no definition returned)" }
    }
    catch { W "  (error: $($_.Exception.Message))" }
}

# ---- lookup tables ------------------------------------------------------

function Dump {
    param([string]$Title, [string]$Query)
    W ""
    W "------------------------ $Title ------------------------"
    try {
        $t = Invoke-Sql $connString $Query
        $cols = @(); foreach ($c in $t.Columns) { $cols += $c.ColumnName }
        W ("  " + ($cols -join " | "))
        foreach ($row in $t.Rows) {
            $vals = @(); foreach ($c in $cols) { $vals += ([string]$row[$c]) }
            W ("  " + ($vals -join " | "))
        }
    }
    catch { W "  (error: $($_.Exception.Message))" }
}

Dump "T143_BlockDataType (all)" "SELECT id, name FROM T143_BlockDataType ORDER BY id"
Dump "T142_BlockType (all)" "SELECT id, identifier, metaTable FROM T142_BlockType ORDER BY id"
Dump "T192_TextCategory (all)" "SELECT id, identifier, description FROM T192_TextCategory ORDER BY id"
Dump "T163_ProfileValueType (all)" "SELECT id, identifier FROM T163_ProfileValueType ORDER BY id"
Dump "T155_Scaling sample (TOP 60)" "SELECT TOP 60 id, type, definition FROM T155_Scaling ORDER BY id"

# Engine ECUs, to find our variant. name/identifier are plain varchar here.
Dump "T101_Ecu engine-ish" @"
SELECT id, identifier, name, fkT102_EcuType FROM T101_Ecu
WHERE name LIKE '%ECM%' OR name LIKE '%engine%' OR name LIKE '%D416%'
   OR identifier LIKE '%ECM%'
ORDER BY name
"@

Dump "T102_EcuType engine-ish" @"
SELECT id, identifier, description FROM T102_EcuType
WHERE description LIKE '%ECM%' OR description LIKE '%engine%'
   OR description LIKE '%D416%' OR description LIKE '%EDC16%'
ORDER BY description
"@

# Our ECM reported these part numbers in the identity block; try to match a
# variant identifier to one of them.
Dump "T100_EcuVariant matching our part numbers" @"
SELECT TOP 50 v.id, v.identifier, v.status, e.name AS ecu_name
FROM T100_EcuVariant v
LEFT JOIN T101_Ecu e ON e.id = v.fkT101_Ecu
WHERE v.identifier LIKE '%31135693%' OR v.identifier LIKE '%8645516%'
   OR v.identifier LIKE '%EDC16%' OR v.identifier LIKE '%D4164%'
ORDER BY v.identifier
"@

W ""
W "done."
