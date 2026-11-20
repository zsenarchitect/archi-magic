<#
.SYNOPSIS
  Read or save where this user's office keeps its standard files (issue #277).

.DESCRIPTION
  One small JSON file on this computer, never in the archi-magic repository:
    %LOCALAPPDATA%\archi-magic\office-paths.json
  An office can share one file instead: set ARCHI_MAGIC_OFFICE_PATHS to its full path.

  Names (one location each):
    project_template   the office Revit template. Often a .rvt model in a document platform
                       or a standards folder, not a .rte file in the Revit template folder.
    detail_library     the office detail library.
    family_library     the office family (.rfa) library folder.
    standards_folder   the folder with the office BIM standards.

  A location is free text on one line: a path (\\fileserver\standards\Templates\Office.rte)
  or a place in a document platform (Docs > Standards > Office Template.rvt).

  The script never searches a drive and never opens a network path. No network.

  -Action get              one line per saved name: name<TAB>location<TAB>saved (yyyy-MM-dd).
                           Prints "none" when nothing is saved.
  -Action get -Name <n>    one line for that name, or "not-set".
  -Action set -Name <n> -Value <location>   saves (replaces) that name.
  -Action remove -Name <n> removes that name.
  -Action path             prints the full path of the file.

  Exit 0 = done. Exit 2 = bad input. Exit 1 = no file location, or the file cannot be read
  or written (the message says which).

  Test parameters: -File (the JSON file), -NowUtc (ISO 8601).

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File office-paths.ps1 -Action get
.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File office-paths.ps1 -Action set -Name project_template -Value "\\fileserver\standards\Templates\Office.rte"
#>
param(
    [string]$Action = 'get',
    [string]$Name = '',
    [string]$Value = '',
    [string]$File = '',
    [string]$NowUtc = ''
)
$ErrorActionPreference = 'Stop'

$Names = @('project_template', 'detail_library', 'family_library', 'standards_folder')
$MaxLength = 500

function Fail([int]$Code, [string]$Message) {
    [Console]::Error.WriteLine('office-paths: ' + $Message)
    exit $Code
}

function Get-StatePath {
    if ($File) { return $File }
    if ($env:ARCHI_MAGIC_OFFICE_PATHS) { return $env:ARCHI_MAGIC_OFFICE_PATHS }
    $base = $env:LOCALAPPDATA
    if (-not $base -and $env:USERPROFILE) { $base = Join-Path $env:USERPROFILE 'AppData\Local' }
    if (-not $base) { return '' }
    return (Join-Path (Join-Path $base 'archi-magic') 'office-paths.json')
}

# -is [PSCustomObject] is true for almost any wrapped object; compare the real type instead.
function Test-JsonObject($o) {
    return ($null -ne $o -and $o.GetType().FullName -eq 'System.Management.Automation.PSCustomObject')
}

# Returns an ordered dictionary: name -> @{ location; saved }. Keeps names this script does not know,
# so a newer version or a hand edit is not lost on the next save. A missing file gives an empty one.
function Read-State([string]$Path) {
    $state = [ordered]@{}
    if (-not (Test-Path -LiteralPath $Path)) { return $state }
    $text = [IO.File]::ReadAllText($Path)
    if (-not $text.Trim()) { return $state }
    $obj = $text | ConvertFrom-Json
    if (-not (Test-JsonObject $obj)) { throw 'not a JSON object' }
    foreach ($p in $obj.PSObject.Properties) {
        $v = $p.Value
        if ($v -is [string]) {
            if ($v) { $state[$p.Name] = [ordered]@{ location = $v; saved = '' } }
        } elseif ((Test-JsonObject $v) -and $v.location) {
            # PowerShell 7 turns an ISO date string into a DateTime; write it back in ISO form.
            $saved = $v.saved
            if ($saved -is [datetime]) { $saved = $saved.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ') }
            $state[$p.Name] = [ordered]@{ location = [string]$v.location; saved = [string]$saved }
        }
    }
    return $state
}

function Write-State([string]$Path, $State) {
    $dir = Split-Path -Parent $Path
    if ($dir -and -not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    $obj = New-Object PSObject
    foreach ($k in $State.Keys) {
        $entry = New-Object PSObject -Property ([ordered]@{ location = $State[$k].location; saved = $State[$k].saved })
        $obj | Add-Member -NotePropertyName $k -NotePropertyValue $entry
    }
    $json = ConvertTo-Json -InputObject $obj -Depth 4
    if ($State.Count -eq 0) { $json = '{}' }
    # UTF-8 without a byte-order mark: Windows PowerShell 5.1 Set-Content would add one.
    [IO.File]::WriteAllText($Path, $json, (New-Object Text.UTF8Encoding $false))
}

function Format-Line([string]$Key, $Entry) {
    $day = ''
    if ($Entry.saved) {
        try {
            $day = [datetime]::Parse($Entry.saved, [Globalization.CultureInfo]::InvariantCulture,
                [Globalization.DateTimeStyles]::AdjustToUniversal).ToString('yyyy-MM-dd')
        } catch { $day = '' }
    }
    return ($Key + "`t" + $Entry.location + "`t" + $day)
}

$Action = $Action.Trim().ToLowerInvariant()
if (@('get', 'set', 'remove', 'path') -notcontains $Action) { Fail 2 "unknown -Action '$Action'. Use get, set, remove or path." }
$Name = $Name.Trim().ToLowerInvariant()
if ($Name -and $Names -notcontains $Name) { Fail 2 ("unknown -Name '{0}'. Use one of: {1}." -f $Name, ($Names -join ', ')) }
if (($Action -eq 'set' -or $Action -eq 'remove') -and -not $Name) { Fail 2 ("-Action $Action needs -Name ({0})." -f ($Names -join ', ')) }

$path = Get-StatePath
if (-not $path) { Fail 1 'no file location: LOCALAPPDATA is not set. Give -File or set ARCHI_MAGIC_OFFICE_PATHS.' }
if ($Action -eq 'path') { Write-Output $path; exit 0 }

try { $state = Read-State $path } catch { Fail 1 ("cannot read {0}: {1}. Fix or delete the file, then save again." -f $path, $_.Exception.Message) }

if ($Action -eq 'get') {
    if ($Name) {
        if ($state.Contains($Name)) { Write-Output (Format-Line $Name $state[$Name]) } else { Write-Output 'not-set' }
        exit 0
    }
    $lines = @()
    foreach ($k in $Names) { if ($state.Contains($k)) { $lines += (Format-Line $k $state[$k]) } }
    if ($lines.Count -eq 0) { Write-Output 'none' } else { $lines | ForEach-Object { Write-Output $_ } }
    exit 0
}

if ($Action -eq 'set') {
    # One line of text: control characters (tab, new line) would break the get output.
    $clean = ([regex]::Replace($Value, '[\x00-\x1F\x7F]', ' ')).Trim()
    if ($clean.Length -ge 2 -and $clean.StartsWith('"') -and $clean.EndsWith('"')) { $clean = $clean.Substring(1, $clean.Length - 2).Trim() }
    if (-not $clean) { Fail 2 '-Action set needs -Value (the location).' }
    if ($clean.Length -gt $MaxLength) { Fail 2 "-Value is longer than $MaxLength characters." }
    $now = (Get-Date).ToUniversalTime()
    if ($NowUtc) {
        try {
            $now = [datetime]::Parse($NowUtc, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::AdjustToUniversal)
        } catch { Fail 2 "-NowUtc '$NowUtc' is not a date." }
    }
    $state[$Name] = [ordered]@{ location = $clean; saved = $now.ToString('yyyy-MM-ddTHH:mm:ssZ') }
    try { Write-State $path $state } catch { Fail 1 ("cannot write {0}: {1}" -f $path, $_.Exception.Message) }
    Write-Output ("Saved {0}: {1}" -f $Name, $clean)
    exit 0
}

# remove
if (-not $state.Contains($Name)) { Write-Output "not-set"; exit 0 }
$state.Remove($Name)
try { Write-State $path $state } catch { Fail 1 ("cannot write {0}: {1}" -f $path, $_.Exception.Message) }
Write-Output "Removed $Name."
exit 0
