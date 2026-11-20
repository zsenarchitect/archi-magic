<#
.SYNOPSIS
  Tests for am-welcome office-paths.ps1 (issue #277). Hermetic: temp files and a fixed clock.
  Never reads or writes the real %LOCALAPPDATA%\archi-magic\office-paths.json.

  powershell -NoProfile -ExecutionPolicy Bypass -File skills\am-welcome\tests\test-office-paths.ps1
  Exit 0 = all cases pass. Also runs under pwsh (tests\test_office_paths.py starts it in CI).
#>
$ErrorActionPreference = 'Stop'
$Tool = (Resolve-Path (Join-Path (Join-Path $PSScriptRoot '..') (Join-Path 'scripts' 'office-paths.ps1'))).Path
# Run the helper with the same PowerShell that runs this test (powershell.exe or pwsh).
$Shell = (Get-Process -Id $PID).Path
$Root = Join-Path ([IO.Path]::GetTempPath()) ("am-office-paths-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
$script:Fail = 0; $script:Asserts = 0
$Now = '2026-01-02T03:04:05Z'
$Unc = '\\fileserver\standards\Templates\Office.rte'

function Assert-True([string]$name, [bool]$ok, [string]$detail) {
    $script:Asserts++
    if ($ok) { Write-Host "PASS $name" } else { $script:Fail++; Write-Host "FAIL $name :`n$detail" }
}
function Invoke-Tool([string[]]$ToolArgs) {
    $all = @('-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', $Tool) + $ToolArgs
    # Windows PowerShell 5.1 turns native stderr into an error record; do not stop on it.
    $ErrorActionPreference = 'Continue'
    $out = & $Shell @all 2>&1 | ForEach-Object { "$_" } | Out-String
    return @{ Code = $LASTEXITCODE; Out = $out.Trim(); Lines = @(($out -split "`r?`n") | Where-Object { $_ }) }
}

try {
    New-Item -ItemType Directory -Force $Root | Out-Null
    $f = Join-Path (Join-Path $Root 'sub') 'office-paths.json'

    # 1. Nothing saved yet: one clear answer, no file made.
    $r = Invoke-Tool @('-Action', 'get', '-File', $f)
    Assert-True 'missing file: get prints none' ($r.Code -eq 0 -and $r.Out -eq 'none') $r.Out
    $r = Invoke-Tool @('-Action', 'get', '-Name', 'project_template', '-File', $f)
    Assert-True 'missing file: get one name prints not-set' ($r.Code -eq 0 -and $r.Out -eq 'not-set') $r.Out
    Assert-True 'missing file: get makes no file' (-not (Test-Path -LiteralPath $f)) ''

    # 2. Save one location: folder made, UTF-8 without BOM, date from the clock.
    $r = Invoke-Tool @('-Action', 'set', '-Name', 'project_template', '-Value', $Unc, '-File', $f, '-NowUtc', $Now)
    Assert-True 'set: exit 0' ($r.Code -eq 0) $r.Out
    $bytes = [IO.File]::ReadAllBytes($f)
    Assert-True 'set: no byte-order mark' (-not ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF)) ''
    $r = Invoke-Tool @('-Action', 'get', '-Name', 'project_template', '-File', $f)
    Assert-True 'get one name: name, location and date' ($r.Code -eq 0 -and $r.Out -eq ("project_template`t" + $Unc + "`t2026-01-02")) $r.Out

    # 3. A second name; get lists the known names in a fixed order.
    $r = Invoke-Tool @('-Action', 'set', '-Name', 'standards_folder', '-Value', '"Docs > Standards > BIM"', '-File', $f, '-NowUtc', $Now)
    $r = Invoke-Tool @('-Action', 'get', '-File', $f)
    Assert-True 'get all: two lines in fixed order' ($r.Lines.Count -eq 2 -and $r.Lines[0].StartsWith("project_template`t") -and $r.Lines[1].StartsWith("standards_folder`t")) $r.Out
    Assert-True 'set: surrounding quotes removed' ($r.Lines[1] -eq "standards_folder`tDocs > Standards > BIM`t2026-01-02") $r.Out

    # 4. Replace a location; a tab or new line in the value becomes a space.
    $r = Invoke-Tool @('-Action', 'set', '-Name', 'project_template', '-Value', ("Docs > Templates`t> Office.rvt"), '-File', $f, '-NowUtc', '2026-03-04T00:00:00Z')
    $r = Invoke-Tool @('-Action', 'get', '-Name', 'project_template', '-File', $f)
    Assert-True 'set again: replaces, one line, new date' ($r.Out -eq "project_template`tDocs > Templates > Office.rvt`t2026-03-04") $r.Out

    # 5. Bad input: exit 2, file not changed.
    $before = [IO.File]::ReadAllText($f)
    $r = Invoke-Tool @('-Action', 'set', '-Name', 'office_template', '-Value', $Unc, '-File', $f)
    Assert-True 'unknown name: exit 2' ($r.Code -eq 2 -and $r.Out -match 'project_template') $r.Out
    $r = Invoke-Tool @('-Action', 'scan', '-File', $f)
    Assert-True 'unknown action: exit 2' ($r.Code -eq 2) $r.Out
    $r = Invoke-Tool @('-Action', 'set', '-Name', 'detail_library', '-File', $f)
    Assert-True 'set with no value: exit 2' ($r.Code -eq 2) $r.Out
    $r = Invoke-Tool @('-Action', 'set', '-Value', $Unc, '-File', $f)
    Assert-True 'set with no name: exit 2' ($r.Code -eq 2) $r.Out
    $r = Invoke-Tool @('-Action', 'set', '-Name', 'detail_library', '-Value', ('x' * 501), '-File', $f)
    Assert-True 'set with a too long value: exit 2' ($r.Code -eq 2) $r.Out
    Assert-True 'bad input: file not changed' ([IO.File]::ReadAllText($f) -eq $before) ''

    # 6. A name this version does not know (hand edit or newer version) is kept on save.
    [IO.File]::WriteAllText($f, '{"project_template":{"location":"A","saved":"2026-01-02T03:04:05Z"},"future_name":{"location":"B","saved":""}}')
    $r = Invoke-Tool @('-Action', 'set', '-Name', 'family_library', '-Value', '\\fileserver\standards\Families', '-File', $f, '-NowUtc', $Now)
    $obj = [IO.File]::ReadAllText($f) | ConvertFrom-Json
    Assert-True 'unknown key kept on save' ($r.Code -eq 0 -and $obj.future_name.location -eq 'B' -and $obj.project_template.location -eq 'A') ([IO.File]::ReadAllText($f))
    $r = Invoke-Tool @('-Action', 'get', '-File', $f)
    Assert-True 'get all: only known names, saved date kept' ($r.Lines.Count -eq 2 -and $r.Lines[0] -eq "project_template`tA`t2026-01-02" -and $r.Out -notmatch 'future_name') $r.Out

    # 7. Remove.
    $r = Invoke-Tool @('-Action', 'remove', '-Name', 'family_library', '-File', $f)
    $r2 = Invoke-Tool @('-Action', 'get', '-Name', 'family_library', '-File', $f)
    Assert-True 'remove: name gone' ($r.Code -eq 0 -and $r2.Out -eq 'not-set') ($r.Out + "`n" + $r2.Out)
    $r = Invoke-Tool @('-Action', 'remove', '-Name', 'family_library', '-File', $f)
    Assert-True 'remove a name that is not set: exit 0, not-set' ($r.Code -eq 0 -and $r.Out -eq 'not-set') $r.Out

    # 8. An unreadable file: exit 1, never overwritten.
    $bad = Join-Path $Root 'bad.json'
    [IO.File]::WriteAllText($bad, '{ not json')
    $r = Invoke-Tool @('-Action', 'get', '-File', $bad)
    Assert-True 'broken JSON: get exit 1' ($r.Code -eq 1) $r.Out
    $r = Invoke-Tool @('-Action', 'set', '-Name', 'project_template', '-Value', $Unc, '-File', $bad)
    Assert-True 'broken JSON: set exit 1, file kept' ($r.Code -eq 1 -and [IO.File]::ReadAllText($bad) -eq '{ not json') $r.Out
    [IO.File]::WriteAllText($bad, '["a"]')
    $r = Invoke-Tool @('-Action', 'get', '-File', $bad)
    Assert-True 'JSON array: get exit 1' ($r.Code -eq 1) $r.Out

    # 9. An office-wide file through ARCHI_MAGIC_OFFICE_PATHS (no -File).
    $shared = Join-Path $Root 'shared.json'
    $old = $env:ARCHI_MAGIC_OFFICE_PATHS
    try {
        $env:ARCHI_MAGIC_OFFICE_PATHS = $shared
        $r = Invoke-Tool @('-Action', 'path')
        Assert-True 'env var: path action names the shared file' ($r.Code -eq 0 -and $r.Out -eq $shared) $r.Out
        $r = Invoke-Tool @('-Action', 'set', '-Name', 'detail_library', '-Value', '\\fileserver\standards\Details', '-NowUtc', $Now)
        Assert-True 'env var: set writes the shared file' ($r.Code -eq 0 -and (Test-Path -LiteralPath $shared)) $r.Out
    } finally {
        $env:ARCHI_MAGIC_OFFICE_PATHS = $old
    }
} finally {
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $Root
}

Write-Host ("{0} assertion(s), {1} failure(s)" -f $script:Asserts, $script:Fail)
if ($script:Asserts -eq 0) { Write-Host 'FAIL: no assertions ran'; exit 1 }
if ($script:Fail -gt 0) { exit 1 }
exit 0
