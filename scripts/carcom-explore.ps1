<#
.SYNOPSIS
    Discovers VIDA's SQL Server and dumps the CarCom schema, so we can pull
    parameter definitions (identifier, scale, unit) straight from VIDA instead
    of reverse engineering each one on the wire.

.DESCRIPTION
    VIDA installs a local SQL Server with its diagnostic databases. This script
    finds the instance, connects (Windows auth first, then a few known VIDA
    logins), lists the databases, and for the parameter database dumps the
    table list and the schema of the tables that matter. It writes a report to
    -OutDir so it can be read off the shared folder.

    Read-only: it runs SELECTs and schema queries, nothing else.

    Uses System.Data.SqlClient, present on .NET 3.5, so it runs on the stock
    Windows 7 SP1 PowerShell 2.0 with nothing to install.

.PARAMETER OutDir
    Where to write the report. Put it on the shared folder so it can be read
    from the host.

.PARAMETER Server
    Force a specific server\instance instead of auto-detecting.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\carcom-explore.ps1 `
        -OutDir "\\vmware-host\Shared Folders\projects\volvo-toolkit\logs"
#>

[CmdletBinding()]
param(
    [string]$OutDir = ".",
    [string]$Server
)

$ErrorActionPreference = 'Continue'
$report = Join-Path $OutDir "carcom-explore.txt"

function Write-Report {
    param([string]$Text = "")
    Write-Host $Text
    Add-Content -LiteralPath $report -Value $Text -Encoding UTF8
}

Set-Content -LiteralPath $report -Value "CarCom exploration $(Get-Date)" -Encoding UTF8

# ---- find candidate instances ------------------------------------------

function Get-SqlInstances {
    $names = @()
    foreach ($path in @(
        'HKLM:\SOFTWARE\Microsoft\Microsoft SQL Server\Instance Names\SQL',
        'HKLM:\SOFTWARE\Wow6432Node\Microsoft\Microsoft SQL Server\Instance Names\SQL')) {
        if (Test-Path $path) {
            $props = Get-ItemProperty -Path $path
            foreach ($p in $props.PSObject.Properties) {
                if ($p.Name -notmatch '^PS') { $names += $p.Name }
            }
        }
    }
    return $names | Sort-Object -Unique
}

$servers = @()
if ($Server) {
    $servers += $Server
}
else {
    foreach ($inst in Get-SqlInstances) {
        if ($inst -eq 'MSSQLSERVER') { $servers += '.'; $servers += 'localhost' }
        else { $servers += ".\$inst"; $servers += "localhost\$inst" }
    }
    # VIDA's usual instance name, in case the registry enumeration missed it.
    $servers += '.\VIDA'; $servers += 'localhost\VIDA'
    $servers = $servers | Sort-Object -Unique
}

Write-Report "candidate servers:"
$servers | ForEach-Object { Write-Report "  $_" }

# ---- connection attempts ------------------------------------------------

# Windows auth first; then logins VIDA is known to create. Passwords are not
# secrets here - they ship with VIDA.
$authModes = @(
    "Integrated Security=SSPI;",
    "User ID=sa;Password=Volvo1234;",
    "User ID=vida;Password=Volvo1234;",
    "User ID=sa;Password=;"
)

function Invoke-Sql {
    param([string]$ConnString, [string]$Query)
    $conn = New-Object System.Data.SqlClient.SqlConnection $ConnString
    $conn.Open()
    try {
        $cmd = $conn.CreateCommand()
        $cmd.CommandText = $Query
        $cmd.CommandTimeout = 30
        $adapter = New-Object System.Data.SqlClient.SqlDataAdapter $cmd
        $table = New-Object System.Data.DataTable
        [void]$adapter.Fill($table)
        # Comma-wrap: without it PowerShell enumerates the DataTable into its
        # rows and the caller loses the table (its .Rows then reads as null).
        return ,$table
    }
    finally { $conn.Close() }
}

$connString = $null
foreach ($srv in $servers) {
    foreach ($auth in $authModes) {
        $cs = "Server=$srv;Database=master;$auth" + "Connect Timeout=5;"
        try {
            $v = Invoke-Sql $cs "SELECT @@VERSION AS v"
            # Assign BEFORE anything that might throw, so a formatting hiccup on
            # the version string can never lose a working connection.
            $connString = $cs
            Write-Report ""
            Write-Report "CONNECTED: $srv  ($auth)"
            try { Write-Report ("  " + ([string]$v.Rows[0][0])) } catch {}
            break
        }
        catch {
            # keep trying quietly
        }
    }
    if ($connString) { break }
}

if (-not $connString) {
    Write-Report ""
    Write-Report "Could not connect to any SQL Server instance with the logins tried."
    Write-Report "Find the instance and login VIDA uses (check its config), then re-run"
    Write-Report "with -Server, or tell me the instance name and I will adjust the script."
    exit 1
}

$base = $connString -replace 'Database=master;', 'Database={0};'

# ---- databases ----------------------------------------------------------

Write-Report ""
Write-Report "databases:"
$dbs = Invoke-Sql $connString "SELECT name FROM sys.databases ORDER BY name"
$dbNames = @()
foreach ($row in $dbs.Rows) { $dbNames += $row["name"]; Write-Report "  $($row["name"])" }

# The parameter database is usually CarCom; scan any that look diagnostic.
$targets = $dbNames | Where-Object { $_ -match 'carcom|diag|vida|epc' }
if (-not $targets) { $targets = $dbNames | Where-Object { $_ -notmatch '^(master|tempdb|model|msdb)$' } }

foreach ($db in $targets) {
    Write-Report ""
    Write-Report "==================== database: $db ===================="
    $cs = [string]::Format($base, $db)

    # tables that look relevant to parameters
    try {
        $tables = Invoke-Sql $cs @"
SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES
WHERE TABLE_TYPE='BASE TABLE'
  AND (TABLE_NAME LIKE '%Ecu%' OR TABLE_NAME LIKE '%Block%'
       OR TABLE_NAME LIKE '%Param%' OR TABLE_NAME LIKE '%Text%'
       OR TABLE_NAME LIKE '%Conv%' OR TABLE_NAME LIKE '%Scal%'
       OR TABLE_NAME LIKE '%Unit%' OR TABLE_NAME LIKE '%Value%'
       OR TABLE_NAME LIKE '%Ident%')
ORDER BY TABLE_NAME
"@
    }
    catch {
        Write-Report "  (could not read tables: $($_.Exception.Message))"
        continue
    }

    foreach ($t in $tables.Rows) {
        $name = $t["TABLE_NAME"]
        $count = "?"
        try { $count = (Invoke-Sql $cs "SELECT COUNT(*) c FROM [$name]").Rows[0]["c"] } catch {}
        Write-Report ""
        Write-Report ("  TABLE {0}  ({1} rows)" -f $name, $count)
        try {
            $cols = Invoke-Sql $cs @"
SELECT COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_NAME='$name' ORDER BY ORDINAL_POSITION
"@
            foreach ($c in $cols.Rows) {
                Write-Report ("      {0}  {1}" -f $c["COLUMN_NAME"], $c["DATA_TYPE"])
            }
        }
        catch {}
    }

    # stored procedures VIDA uses to fetch parameter data
    try {
        $procs = Invoke-Sql $cs @"
SELECT name FROM sys.procedures
WHERE name LIKE '%Param%' OR name LIKE '%Ecu%' OR name LIKE '%CAN%' OR name LIKE '%Value%'
ORDER BY name
"@
        if ($procs.Rows.Count -gt 0) {
            Write-Report ""
            Write-Report "  stored procedures of interest:"
            foreach ($p in $procs.Rows) { Write-Report "      $($p["name"])" }
        }
    }
    catch {}
}

Write-Report ""
Write-Report "done. Send this file back and the next script will extract our ECU's parameters."
