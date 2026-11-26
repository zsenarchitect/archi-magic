<#
.SYNOPSIS
  Render schedule JSON or change-preview JSON as one self-contained, interactive HTML page.

.DESCRIPTION
  Stable CLI (other skills call it by this path):
    render-table.ps1 -Json <file> -Out <html> [-Json2 <file>] [-CsvDir <folder>]
    render-table.ps1 -RunId <run_id> -Out <html> [-RunsDir <folder>] [-CsvDir <folder>]

  -RunId reads the pyRevit run record <runs>\<date>-<time>-<run_id>\response.json, so you do not
  need to search the runs folder. The default runs folder is %APPDATA%\pyRevit\agent\runs.

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

  The page also holds a plain view: the same rows as plain HTML tables, written by this script.
  It shows when page scripts do not run (for example, a static snapshot in the Browser pane).
  The page script hides it and draws the interactive table.

  Exit codes: 0 ok, 1 bad input (missing file, JSON does not parse, unknown kind), 2 template missing.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File render-table.ps1 -Json sched.json -Out sched.html
#>
param(
    [string]$Json,
    [Parameter(Mandatory = $true)][string]$Out,
    [string]$Json2,
    [string]$CsvDir,
    [string]$RunId,
    [string]$RunsDir
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

function Find-RunRecord([string]$dir, [string]$id) {
    # pyRevit writes each run to <runs>\<date>-<time>-<run_id>\response.json.
    if (-not (Test-Path -LiteralPath $dir -PathType Container)) { Write-Host "FAIL: run folder not found: $dir"; exit 1 }
    $hits = @(Get-ChildItem -LiteralPath $dir -Directory |
        Where-Object { $_.Name -eq $id -or $_.Name.EndsWith('-' + $id) } |
        Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName 'response.json') } |
        Sort-Object LastWriteTime -Descending)
    if ($hits.Count -eq 0) { Write-Host "FAIL: no run folder with response.json for run id $id in $dir"; exit 1 }
    if ($hits.Count -gt 1) { Write-Host "WARN: $($hits.Count) run folders end with $id; using the newest: $($hits[0].Name)" }
    $path = Join-Path $hits[0].FullName 'response.json'
    Write-Host "OK: run record $path"
    return $path
}

# ---- Plain view (issue #296) ----
# The Browser pane can show a local file as a static snapshot that runs no scripts. Then the page
# shows this plain view: the same rows as plain HTML tables, written here. The page script hides it.
function ConvertTo-HtmlText($v) {
    if ($null -eq $v) { return '' }
    if ($v -is [bool]) { if ($v) { return 'true' } else { return 'false' } }
    if ($v -is [System.Collections.IDictionary] -or $v -is [System.Management.Automation.PSCustomObject] -or
        ($v -is [System.Collections.IEnumerable] -and $v -isnot [string])) {
        $v = ConvertTo-Json -InputObject $v -Compress -Depth 6
    }
    $s = [System.Net.WebUtility]::HtmlEncode([string]$v)
    # "_" as an entity, so model text can never form a template placeholder such as __DATA_JSON__.
    return $s.Replace('_', '&#95;')
}
function Get-List($o, [string]$name) {
    $v = Get-Field $o $name
    if ($null -eq $v) { return , @() }
    return , @($v)
}
function Get-Cell($row, [int]$i) {
    if ($null -eq $row -or $i -ge $row.Count) { return '' }
    $v = $row[$i]
    if ($null -eq $v) { return '' }
    return [string]$v
}
function Add-PlainTable($sb, $columns, $rows, $kinds) {
    [void]$sb.Append('<div class="wrap"><table><thead><tr>')
    foreach ($c in $columns) { [void]$sb.Append('<th>').Append((ConvertTo-HtmlText $c)).Append('</th>') }
    [void]$sb.Append('</tr></thead><tbody>')
    for ($i = 0; $i -lt $rows.Count; $i++) {
        $k = 'body'
        if ($kinds.Count -gt $i -and $null -ne $kinds[$i]) { $k = [string]$kinds[$i] }
        if (@('group', 'total', 'blank') -contains $k) { [void]$sb.Append('<tr class="' + $k + '">') } else { [void]$sb.Append('<tr>') }
        foreach ($cell in @($rows[$i])) { [void]$sb.Append('<td>').Append((ConvertTo-HtmlText $cell)).Append('</td>') }
        [void]$sb.Append('</tr>')
    }
    [void]$sb.Append('</tbody></table></div>')
}
function Add-PlainSchedules($sb, $obj, [string]$tag) {
    $kind = [string](Get-Field $obj 'kind')
    $scheds = Get-List $obj 'schedules'
    $prefix = ''
    if ($tag) { $prefix = '[' + $tag + '] ' }
    if ($kind -eq 'schedule-list') {
        $rows = @(foreach ($s in $scheds) {
            $onSheet = 'No'
            if ((Get-Field $s 'onSheet') -eq $true) { $onSheet = 'Yes' }
            , @((Get-Field $s 'name'), (Get-Field $s 'category'), (Get-Field $s 'kind'), (Get-Field $s 'rows'),
                $onSheet, ((Get-List $s 'sheets') -join ', '), (Get-Field $s 'id'))
        })
        [void]$sb.Append('<h2>' + (ConvertTo-HtmlText ($prefix + 'Schedules in this model')) + ' (' + $rows.Count + ')</h2>')
        Add-PlainTable $sb @('Name', 'Category', 'Type', 'Rows', 'On sheet', 'Sheets', 'Id') $rows @()
        return
    }
    if ($scheds.Count -eq 0) { [void]$sb.Append('<p class="st-note">No schedules in this file.</p>') }
    foreach ($s in $scheds) {
        $rows = Get-List $s 'rows'
        [void]$sb.Append('<h2>' + (ConvertTo-HtmlText ($prefix + [string](Get-Field $s 'name'))) + '</h2>')
        $meta = @()
        $cat = [string](Get-Field $s 'category')
        if ($cat) { $meta += $cat }
        $meta += ([string]$rows.Count + ' rows')
        $note = [string](Get-Field $s 'note')
        if ($note) { $meta += $note }
        [void]$sb.Append('<p class="st-meta">' + (ConvertTo-HtmlText ($meta -join ' | ')) + '</p>')
        Add-PlainTable $sb (Get-List $s 'columns') $rows (Get-List $s 'rowKinds')
    }
    $missing = Get-List $obj 'missing'
    if ($missing.Count -gt 0) {
        [void]$sb.Append('<div class="notes"><h2>Not found</h2><ul>')
        foreach ($m in $missing) { [void]$sb.Append('<li>' + (ConvertTo-HtmlText $m) + '</li>') }
        [void]$sb.Append('</ul></div>')
    }
}
function Add-PlainPreview($sb, $cp) {
    $cols = Get-List $cp 'columns'
    $keyName = [string](Get-Field $cp 'keyColumn')
    $k = 0
    for ($c = 0; $c -lt $cols.Count; $c++) { if ([string]$cols[$c] -eq $keyName) { $k = $c; break } }
    $hot = @{}
    foreach ($n in (Get-List $cp 'changedColumns')) { $hot[[string]$n] = $true }
    $before = Get-List $cp 'before'
    $after = Get-List $cp 'after'
    $bmap = @{}
    foreach ($r in $before) { $key = Get-Cell @($r) $k; if (-not $bmap.ContainsKey($key)) { $bmap[$key] = @($r) } }
    $warn = @{}
    $warnings = Get-List $cp 'warnings'
    foreach ($w in $warnings) {
        $key = [string](Get-Field $w 'row')
        if (-not $warn.ContainsKey($key)) { $warn[$key] = @() }
        $warn[$key] += [string](Get-Field $w 'text')
    }
    $recs = New-Object System.Collections.Generic.List[object]
    $seen = @{}
    foreach ($r in $after) {
        $a = @($r); $key = Get-Cell $a $k; $seen[$key] = $true
        $b = $null; $st = 'added'; $diff = @{}
        if ($bmap.ContainsKey($key)) {
            $b = $bmap[$key]; $st = 'same'
            for ($c = 0; $c -lt $cols.Count; $c++) {
                if ((Get-Cell $b $c) -cne (Get-Cell $a $c)) { $diff[$c] = $true; $st = 'changed' }
            }
        }
        $recs.Add(@{ St = $st; A = $a; B = $b; Diff = $diff; Key = $key })
    }
    foreach ($r in $before) {
        $b = @($r); $key = Get-Cell $b $k
        if (-not $seen.ContainsKey($key)) { $seen[$key] = $true; $recs.Add(@{ St = 'removed'; A = $null; B = $b; Diff = @{}; Key = $key }) }
    }
    $count = @{ changed = 0; same = 0; added = 0; removed = 0 }
    foreach ($rec in $recs) { $count[$rec.St]++ }

    $params = Get-Field $cp 'params'
    if ($params -is [System.Collections.IDictionary] -or $params -is [System.Management.Automation.PSCustomObject]) {
        $names = if ($params -is [System.Collections.IDictionary]) { @($params.Keys) } else { @($params.PSObject.Properties | ForEach-Object { $_.Name }) }
        if ($names.Count -gt 0) {
            [void]$sb.Append('<p class="interp"><span class="am-micro">Interpreted as</span>')
            foreach ($n in $names) { [void]$sb.Append('<span class="kv"><b>' + (ConvertTo-HtmlText $n) + '</b><span>' + (ConvertTo-HtmlText (Get-Field $params $n)) + '</span></span>') }
            [void]$sb.Append('</p>')
        }
    }
    [void]$sb.Append('<div class="bannerrow">')
    if ((Get-Field $cp 'blocked') -eq $true) {
        $btext = 'Apply is blocked: resolve warnings first.'
        if ((Get-Field $cp 'review_only') -eq $true) { $btext = 'The preview contains blocked edits.' }
        [void]$sb.Append('<span class="banner blocked" role="alert"><span class="am-status bad" aria-hidden="true"></span><span>' + $btext + '</span></span>')
    }
    [void]$sb.Append('<span class="banner"><span class="am-status" aria-hidden="true"></span><span>Preview only. Nothing in Revit has changed yet.</span></span>')
    [void]$sb.Append('<span class="chips"><span class="chip"><b>' + $count.changed + '</b> changed</span><span class="chip"><b>' + $count.same + '</b> unchanged</span>')
    if ($count.added) { [void]$sb.Append('<span class="chip"><b>' + $count.added + '</b> added</span>') }
    if ($count.removed) { [void]$sb.Append('<span class="chip"><b>' + $count.removed + '</b> removed</span>') }
    [void]$sb.Append('<span class="chip"><b>' + $warnings.Count + '</b> warnings</span></span></div>')

    $deletions = Get-List $cp 'deletions'
    if ($deletions.Count -gt 0) {
        [void]$sb.Append('<div class="notes"><h2>Delete: ' + $deletions.Count + ' element(s)</h2><ul>')
        foreach ($d in $deletions) {
            [void]$sb.Append('<li>ID ' + (ConvertTo-HtmlText (Get-Field $d 'id')) + ' | ' + (ConvertTo-HtmlText (Get-Field $d 'category')) + ' | ' + (ConvertTo-HtmlText (Get-Field $d 'name')) + '</li>')
        }
        [void]$sb.Append('</ul></div>')
    }
    if ($warnings.Count -gt 0) {
        [void]$sb.Append('<div class="warnings"><h2><span class="am-status warn" aria-hidden="true"></span>Warnings</h2><ul>')
        foreach ($w in $warnings) { [void]$sb.Append('<li>' + (ConvertTo-HtmlText (Get-Field $w 'row')) + ' ' + (ConvertTo-HtmlText (Get-Field $w 'text')) + '</li>') }
        [void]$sb.Append('</ul></div>')
    }
    $notes = Get-List $cp 'notes'
    $steps = Get-List $cp 'steps'
    if ($notes.Count -gt 0 -or $steps.Count -gt 0) {
        [void]$sb.Append('<div class="notes">')
        if ($notes.Count -gt 0) {
            [void]$sb.Append('<h2>Notes</h2><ul>')
            foreach ($t in $notes) { [void]$sb.Append('<li>' + (ConvertTo-HtmlText $t) + '</li>') }
            [void]$sb.Append('</ul>')
        }
        if ($steps.Count -gt 0) {
            [void]$sb.Append('<h2>Steps, in the order they run (' + $steps.Count + ')</h2><ol>')
            foreach ($st in $steps) {
                $v = $st
                if (Test-Field $st 'value') { $v = Get-Field $st 'value' }
                if ($v -is [System.Collections.IList] -and $v.Count -eq 3) {
                    [void]$sb.Append('<li><span class="mono">' + (ConvertTo-HtmlText $v[0]) + '</span> <span class="old">' + (ConvertTo-HtmlText $v[1]) + '</span><span class="arr">/</span><span class="new">' + (ConvertTo-HtmlText $v[2]) + '</span></li>')
                } elseif ($v -is [System.Collections.IList]) {
                    [void]$sb.Append('<li>' + (ConvertTo-HtmlText ((@($v) | ForEach-Object { [string]$_ }) -join '  ')) + '</li>')
                } else {
                    [void]$sb.Append('<li>' + (ConvertTo-HtmlText $v) + '</li>')
                }
            }
            [void]$sb.Append('</ol>')
        }
        [void]$sb.Append('</div>')
    }

    $marks = @{ changed = '&middot;'; added = '+'; removed = '&minus;'; same = '' }
    $head = New-Object System.Text.StringBuilder
    [void]$head.Append('<thead><tr><th title="Change">&#916;</th>')
    foreach ($c in $cols) {
        $cls = ''
        if ($hot.ContainsKey([string]$c)) { $cls = ' class="hot"' }
        [void]$head.Append('<th' + $cls + '>' + (ConvertTo-HtmlText $c) + '</th>')
    }
    [void]$head.Append('</tr></thead>')
    $shown = New-Object System.Text.StringBuilder
    $same = New-Object System.Text.StringBuilder
    $nShown = 0; $nSame = 0
    foreach ($rec in $recs) {
        $w = $warn[$rec.Key]
        $target = $shown
        if ($rec.St -eq 'same' -and -not $w) { $target = $same; $nSame++ } else { $nShown++ }
        $cls = 'd-' + $rec.St
        if ($w) { $cls += ' has-warn' }
        [void]$target.Append('<tr class="' + $cls + '"><td class="mk" title="' + $rec.St + '"><span class="m">' + $marks[$rec.St] + '</span>')
        if ($w) { [void]$target.Append('<span class="am-status warn" title="' + (ConvertTo-HtmlText ($w -join ' / ')) + '"></span>') }
        [void]$target.Append('</td>')
        for ($c = 0; $c -lt $cols.Count; $c++) {
            $bv = Get-Cell $rec.B $c; $av = Get-Cell $rec.A $c
            if ($rec.St -eq 'changed' -and $rec.Diff.ContainsKey($c)) {
                if (-not $bv) { $bv = '(empty)' }
                if (-not $av) { $av = '(empty)' }
                [void]$target.Append('<td class="chg"><span class="old">' + (ConvertTo-HtmlText $bv) + '</span><span class="arr">/</span><span class="new">' + (ConvertTo-HtmlText $av) + '</span></td>')
            } elseif ($rec.St -eq 'removed') {
                [void]$target.Append('<td>' + (ConvertTo-HtmlText $bv) + '</td>')
            } else {
                [void]$target.Append('<td>' + (ConvertTo-HtmlText $av) + '</td>')
            }
        }
        [void]$target.Append('</tr>')
    }
    if ($recs.Count -eq 0) { [void]$sb.Append('<p class="st-note">Nothing to compare.</p>') }
    if ($nShown -gt 0) { [void]$sb.Append('<div class="wrap"><table>' + $head.ToString() + '<tbody>' + $shown.ToString() + '</tbody></table></div>') }
    if ($nSame -gt 0) {
        [void]$sb.Append('<details><summary>Show ' + $nSame + ' unchanged</summary><div class="wrap"><table>' + $head.ToString() + '<tbody>' + $same.ToString() + '</tbody></table></div></details>')
    }
    [void]$sb.Append('<p class="st-meta">Rows are matched by ' + (ConvertTo-HtmlText $cols[$k]) + '. Rows keep the order of the preview file.</p>')
}
function Get-PlainHtml($p1, $p2) {
    $sb = New-Object System.Text.StringBuilder
    $eyebrow = 'schedules'
    $title = [string](Get-Field $p1.Obj 'pageTitle')
    if ($p1.Kind -eq 'change-preview') { $eyebrow = 'change preview'; $title = [string](Get-Field $p1.Obj 'title') }
    elseif ($p1.Kind -eq 'schedule-list') { $eyebrow = 'schedule list' }
    if (-not $title) { $title = [string](Get-Field $p1.Obj 'document') }
    [void]$sb.Append('<div class="st-head"><span class="am-micro">' + $eyebrow + '</span>')
    if ($title) { [void]$sb.Append('<h1>' + (ConvertTo-HtmlText $title) + '</h1>') }
    [void]$sb.Append('<p class="st-note">Plain view. Page scripts did not run here (for example, in a static snapshot of the Browser pane). Open the file in a web browser to sort, filter, compare and export.</p></div>')
    if ($p1.Kind -eq 'change-preview') { Add-PlainPreview $sb $p1.Obj }
    else {
        $tag = ''
        if ($p2) { $tag = 'A' }
        Add-PlainSchedules $sb $p1.Obj $tag
        if ($p2) { Add-PlainSchedules $sb $p2.Obj 'B' }
    }
    return $sb.ToString()
}

if ($RunId) {
    if ($Json) { Write-Host 'FAIL: give -Json or -RunId, not both'; exit 1 }
    if ($RunId -notmatch '^[A-Za-z0-9][A-Za-z0-9_.-]*$') { Write-Host "FAIL: run id has characters that are not allowed: $RunId"; exit 1 }
    if (-not $RunsDir) {
        if (-not $env:APPDATA) { Write-Host 'FAIL: APPDATA is not set; give -RunsDir'; exit 1 }
        $RunsDir = Join-Path $env:APPDATA 'pyRevit\agent\runs'
    }
    $Json = Find-RunRecord $RunsDir $RunId
} elseif (-not $Json) { Write-Host 'FAIL: give -Json <file> or -RunId <run_id>'; exit 1 }

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
# Insert the plain view last. Its marker is the first one in the body, before the embedded JSON.
$marker = '__STATIC_HTML__'
$at = $html.IndexOf($marker)
if ($at -lt 0) { Write-Host "FAIL: template has no $marker marker"; exit 2 }
$html = $html.Substring(0, $at) + (Get-PlainHtml $p1 $p2) + $html.Substring($at + $marker.Length)

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
