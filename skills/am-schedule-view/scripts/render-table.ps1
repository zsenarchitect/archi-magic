<#
.SYNOPSIS
  Render schedule JSON or change-preview JSON as one self-contained, interactive HTML page.

.DESCRIPTION
  Stable CLI (other skills call it by this path):
    render-table.ps1 -Json <file> -Out <html> [-Json2 <file>] [-CsvDir <folder>]

  Accepted input (detected from the JSON "kind" field):
    "schedules"       read_schedules.py read mode: {schedules:[{name,id,columns[],rows[][],rowKinds[]}]}
    "schedule-list"   read_schedules.py list mode: {schedules:[{name,id,category,rows,onSheet}]}
    "change-preview"  shared contract with am-safe-change:
                      {kind,title,keyColumn,columns[],changedColumns[],before[][],after[][],notes[],warnings[{row,text}]}

  -Json2 adds a second "schedules" file (a later snapshot). The page labels the two sets [A] and [B]
  and its Compare button shows added, removed and changed rows.
  -CsvDir also writes one CSV per schedule (UTF-8 with BOM, so Excel reads it). This runs on the
  user's machine, never inside Revit.

  The JSON is embedded as text: it is never round-tripped through ConvertTo-Json (PS 5.1 truncates
  nested arrays). Output is UTF-8 without BOM.

  Exit codes: 0 ok, 1 bad input (missing file, JSON does not parse, unknown kind), 2 template missing.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File render-table.ps1 -Json sched.json -Out sched.html
#>
param(
    [Parameter(Mandatory = $true)][string]$Json,
    [Parameter(Mandatory = $true)][string]$Out,
    [string]$Json2,
    [string]$CsvDir
)
$ErrorActionPreference = 'Stop'
$utf8 = New-Object Text.UTF8Encoding $false
$skillDir = Split-Path -Parent $PSScriptRoot
$template = Join-Path $skillDir 'assets\viewer-template.html'
if (-not (Test-Path -LiteralPath $template)) { Write-Host "FAIL: template not found: $template"; exit 2 }

# PS 5.1 ConvertFrom-Json stops at about 2 MB of text; a 5000-row schedule is larger. Use
# JavaScriptSerializer with no length cap where it exists (5.1), else ConvertFrom-Json (PS 7).
$script:ser = $null
try {
    Add-Type -AssemblyName System.Web.Extensions -ErrorAction Stop
    $script:ser = New-Object System.Web.Script.Serialization.JavaScriptSerializer
    $script:ser.MaxJsonLength = [int]::MaxValue
    $script:ser.RecursionLimit = 256
} catch { $script:ser = $null }

function ConvertFrom-JsonText([string]$text) {
    if ($script:ser) { return , $script:ser.DeserializeObject($text) }
    return , ($text | ConvertFrom-Json)
}
# Field access that works on both a dictionary (JavaScriptSerializer) and a PSObject (ConvertFrom-Json).
function Get-Field($o, [string]$name) {
    if ($null -eq $o) { return $null }
    if ($o -is [System.Collections.IDictionary]) { if ($o.ContainsKey($name)) { return , $o[$name] } else { return $null } }
    $p = $o.PSObject.Properties[$name]
    if ($p) { return , $p.Value } else { return $null }
}
function Test-Field($o, [string]$name) {
    if ($null -eq $o) { return $false }
    if ($o -is [System.Collections.IDictionary]) { return $o.ContainsKey($name) }
    return [bool]$o.PSObject.Properties[$name]
}

function Get-DeletionJson($preview) {
    if (-not (Test-Field $preview 'deletions')) { return '[]' }
    $items = Get-Field $preview 'deletions'
    if ($items -isnot [System.Collections.IList]) { throw 'Use an array for planned deletions.' }
    $seen = @{}
    $deletions = @(
        foreach ($item in $items) {
            $value = Get-Field $item 'id'
            $id = [string]$value
            $number = [long]0
            if (($value -isnot [string] -and $value -isnot [int] -and $value -isnot [long]) -or
                $id -cnotmatch '\A[1-9][0-9]{0,18}\z' -or -not [long]::TryParse($id, [ref]$number) -or
                $seen.ContainsKey($id)) {
                throw 'Give each deletion one distinct positive Revit ElementId.'
            }
            $category = Get-Field $item 'category'
            $name = Get-Field $item 'name'
            if ($category -isnot [string] -or $name -isnot [string] -or
                [string]::IsNullOrWhiteSpace($category) -or [string]::IsNullOrWhiteSpace($name)) {
                throw 'Name the category and exact element for each deletion.'
            }
            $seen[$id] = $true
            [ordered]@{ id = $id; category = $category; name = $name }
        }
    )
    # Convert only deletion IDs to decimal strings before the browser sees them.
    return (ConvertTo-Json -InputObject $deletions -Compress -Depth 3).Replace('<', '\u003c')
}

function Read-Payload([string]$path) {
    if (-not (Test-Path -LiteralPath $path)) { Write-Host "FAIL: JSON file not found: $path"; exit 1 }
    $raw = [IO.File]::ReadAllText((Resolve-Path -LiteralPath $path).ProviderPath, $utf8)
    if ($raw.Length -gt 0 -and $raw[0] -eq [char]0xFEFF) { $raw = $raw.Substring(1) }
    try { $obj = ConvertFrom-JsonText $raw } catch { Write-Host "FAIL: $path is not valid JSON: $($_.Exception.Message)"; exit 1 }
    # read_schedules.py returns {payloadFormat:"ascii-json", payload:"<json>"} (see its to_ascii_json).
    # Accept that wrapper, or the whole agent run record {result:{payload}}, as well as plain JSON.
    $wrap = $null
    $res = Get-Field $obj 'result'
    if (Test-Field $res 'payload') { $wrap = $res }
    elseif (Test-Field $obj 'payload') { $wrap = $obj }
    if ($wrap) {
        $raw = [string](Get-Field $wrap 'payload')
        try { $obj = ConvertFrom-JsonText $raw } catch { Write-Host "FAIL: the payload in $path is not valid JSON"; exit 1 }
    } elseif ((Test-Field $res 'kind') -and $script:ser) {
        # A run record whose result is the data itself (no payload wrapper).
        $obj = $res
        $raw = $script:ser.Serialize($res)
    }
    $kind = [string](Get-Field $obj 'kind')
    if (-not $kind) {
        if (Test-Field $obj 'before') { $kind = 'change-preview' }
        elseif (Test-Field $obj 'schedules') { $kind = 'schedules' }
    }
    if (@('schedules', 'schedule-list', 'change-preview') -notcontains $kind) {
        Write-Host "FAIL: $path has unknown kind '$kind' (expected schedules, schedule-list or change-preview)"; exit 1
    }
    if ($kind -eq 'change-preview') {
        foreach ($f in @('keyColumn', 'columns', 'before', 'after')) {
            if (-not (Test-Field $obj $f)) { Write-Host "FAIL: change-preview JSON has no '$f' field"; exit 1 }
        }
    }
    # Make the text safe inside <script type="application/json">. Every "<" in valid JSON sits in a
    # string, so the JSON escape \u003c keeps the data identical and leaves no "</script" or "<!--".
    $safe = $raw.Replace('<', '\u003c')
    return @{ Raw = $safe; Obj = $obj; Kind = $kind }
}

function ConvertTo-CsvLine($cells) {
    $out = foreach ($c in @($cells)) {
        $s = [string]$c
        if ($s -match '[",\r\n]') { '"' + $s.Replace('"', '""') + '"' } else { $s }
    }
    return ($out -join ',')
}

if (-not $script:ser) { Write-Host 'WARN: JavaScriptSerializer is not available; using ConvertFrom-Json (PS 5.1 limit is about 2 MB of JSON)' }
$p1 = Read-Payload $Json
$p2 = $null
if ($Json2) {
    $p2 = Read-Payload $Json2
    if ($p1.Kind -ne 'schedules' -or $p2.Kind -ne 'schedules') { Write-Host 'FAIL: -Json2 compares two "schedules" files only'; exit 1 }
}

$title = switch ($p1.Kind) {
    'change-preview' { 'Change preview' }
    'schedule-list' { 'Schedules in model' }
    default { 'Schedules' }
}
$html = [IO.File]::ReadAllText($template, $utf8)
$reviewOnly = Get-Field $p1.Obj 'review_only'
if ($p1.Kind -eq 'change-preview' -and $reviewOnly -is [bool] -and $reviewOnly) {
    $html = $html -replace '(?s)<!-- online-fonts:start -->.*?<!-- online-fonts:end -->', ''
}
$html = $html.Replace('__PAGE_TITLE__', $title)
$html = $html.Replace('__DATA2_JSON__', $(if ($p2) { $p2.Raw } else { 'null' }))
$html = $html.Replace('__DATA_JSON__', $p1.Raw)
$deleteJson = '[]'
if ($p1.Kind -eq 'change-preview') {
    try { $deleteJson = Get-DeletionJson $p1.Obj }
    catch { Write-Host "FAIL: $($_.Exception.Message)"; exit 1 }
}
$html = $html.Replace('<script type="application/json" id="deletions">__DELETIONS_JSON__</script>',
                      '<script type="application/json" id="deletions">' + $deleteJson + '</script>')

$outFull = [IO.Path]::GetFullPath($(if ([IO.Path]::IsPathRooted($Out)) { $Out } else { Join-Path (Get-Location).ProviderPath $Out }))
$outDir = Split-Path -Parent $outFull
if (-not (Test-Path -LiteralPath $outDir)) { New-Item -ItemType Directory -Path $outDir -Force | Out-Null }
[IO.File]::WriteAllText($outFull, $html, $utf8)
Write-Host "OK: wrote $outFull ($($p1.Kind))"

if ($CsvDir) {
    if ($p1.Kind -ne 'schedules') { Write-Host "WARN: -CsvDir applies to schedules JSON only; no CSV written for $($p1.Kind)" }
    else {
        if (-not (Test-Path -LiteralPath $CsvDir)) { New-Item -ItemType Directory -Path $CsvDir -Force | Out-Null }
        $bom = New-Object Text.UTF8Encoding $true
        # Get-Field returns an array as one object; foreach / [object[]] enumerate it.
        $scheds = Get-Field $p1.Obj 'schedules'
        foreach ($s in $scheds) {
            $lines = New-Object System.Collections.Generic.List[string]
            $lines.Add((ConvertTo-CsvLine (Get-Field $s 'columns')))
            $kinds = [object[]](Get-Field $s 'rowKinds'); if ($null -eq $kinds) { $kinds = @() }
            $rows = [object[]](Get-Field $s 'rows'); if ($null -eq $rows) { $rows = @() }
            for ($i = 0; $i -lt $rows.Count; $i++) {
                if ($kinds.Count -gt $i -and $kinds[$i] -eq 'blank') { continue }
                $lines.Add((ConvertTo-CsvLine $rows[$i]))
            }
            $name = ([string](Get-Field $s 'name')) -replace '[\\/:*?"<>|]+', '_'
            $csv = Join-Path $CsvDir ($name + '.csv')
            [IO.File]::WriteAllText($csv, (($lines -join "`r`n") + "`r`n"), $bom)
            Write-Host "OK: wrote $csv ($($lines.Count - 1) rows)"
        }
    }
}
exit 0
