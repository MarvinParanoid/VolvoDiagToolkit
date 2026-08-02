<#
.SYNOPSIS
    Extracts the security-access code(s) CarCom stores for an ECU variant - the
    code VIDA sends with the 0xA3 unlock (A3 02 <code>) before writing to a
    module. Read-only: it only reads VIDA's own database.

    For your own car only. This is the code for this vehicle's modules, pinned
    to its ECU variants; do not use it against a car that is not yours.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File "Z:\projects\volvo-toolkit\scripts\carcom-security.ps1" `
        -EcuVariantId 1175 -Out cem -OutDir "Z:\projects\volvo-toolkit\logs"
    powershell -ExecutionPolicy Bypass -File "Z:\projects\volvo-toolkit\scripts\carcom-security.ps1" `
        -EcuVariantId 965 -Out ecm -OutDir "Z:\projects\volvo-toolkit\logs"
#>

[CmdletBinding()]
param(
    [int]$EcuVariantId = 1175,
    [string]$Out = "cem",
    [string]$OutDir = ".",
    [string]$Server
)

$ErrorActionPreference = 'Continue'
if (-not (Test-Path -LiteralPath $OutDir)) { New-Item -ItemType Directory -Force -Path $OutDir | Out-Null }
$report = Join-Path $OutDir "carcom-security-$Out.txt"
function W { param([string]$t = "") ; Write-Host $t ; Add-Content -LiteralPath $report -Value $t -Encoding UTF8 }
Set-Content -LiteralPath $report -Value "CarCom security codes variant $EcuVariantId $(Get-Date)" -Encoding UTF8

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

# 1. The stored procedure VIDA uses to fetch a security code - shows how it
#    resolves the code and any transform before it goes on the wire.
W ""; W "==== PROC vadis_GetSecurityCodeFromEcuType"
try {
    $d = Invoke-Sql $connString "SELECT OBJECT_DEFINITION(OBJECT_ID('dbo.vadis_GetSecurityCodeFromEcuType')) AS def"
    $def = [string]$d.Rows[0][0]
    if ($def) { W $def } else { W "  (not found)" }
} catch { W "  (error: $($_.Exception.Message))" }

# 2. The code-type catalogue, so the codes below are labelled (e.g. which is
#    the config/parametrisation unlock vs a flash/EEPROM code).
Dump "T172_SecurityCodeType (all)" "SELECT id, identifier, description FROM T172_SecurityCodeType ORDER BY id"

# 3. The actual code(s) for this ECU variant.
Dump "security codes for variant $EcuVariantId" @"
SELECT sc.id, ct.identifier AS codetype, sc.code, sc.description
FROM T170_SecurityCode_EcuVariant ev
JOIN T171_SecurityCode sc ON sc.id = ev.fkT171_SecurityCode
LEFT JOIN T172_SecurityCodeType ct ON ct.id = sc.fkT172_SecurityCodeType
WHERE ev.fkT100_EcuVariant = $EcuVariantId
ORDER BY ct.identifier, sc.id
"@

# 4. Wider hunt (added 2026-08-03): the real code may not be in carcom (its
#    T171 values can be placeholders). Look across ALL of VIDA's databases and
#    inside every stored proc / function body for security-code logic.
Dump "all VIDA databases" "SELECT name FROM sys.databases ORDER BY name"

Dump "carcom procs/functions mentioning security/pin/seed/code" @"
SELECT o.name, o.type_desc
FROM sys.sql_modules m JOIN sys.objects o ON o.object_id = m.object_id
WHERE m.definition LIKE '%security%' OR m.definition LIKE '%seed%'
   OR m.definition LIKE '%SecurityCode%' OR m.definition LIKE '%pin%'
ORDER BY o.name
"@

# The full body of the fetch proc — reveals whether the code is looked up
# (then it's in a table somewhere) or computed (an algorithm we can lift).
W ""; W "==== FULL body: vadis_GetSecurityCodeFromEcuType"
try {
    $d = Invoke-Sql $connString "SELECT OBJECT_DEFINITION(OBJECT_ID('dbo.vadis_GetSecurityCodeFromEcuType')) AS def"
    if ($d.Rows.Count) { W ([string]$d.Rows[0].def) } else { W "  (proc not found)" }
} catch { W "  (error: $($_.Exception.Message))" }

# Peek at the other DBs Tigo2000 named — table names hinting at codes/security.
foreach ($db in @('DiagSwdlRepository','DiagSwdlSession','servicerep_en-US')) {
    Dump "tables in [$db] hinting security/code/pin/ecu" @"
SELECT TABLE_NAME FROM [$db].INFORMATION_SCHEMA.TABLES
WHERE TABLE_NAME LIKE '%Security%' OR TABLE_NAME LIKE '%Code%'
   OR TABLE_NAME LIKE '%Pin%' OR TABLE_NAME LIKE '%Ecu%'
ORDER BY TABLE_NAME
"@
}

W ""; W "done. Send carcom-security-$Out.txt back."
