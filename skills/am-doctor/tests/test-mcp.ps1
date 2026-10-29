<#
.SYNOPSIS
  Tests for the am-doctor MCP check and the -Fix MCP_MISSING / MCP_WRONG_PATH fallback. Hermetic:
  the stub claude program from am-pyrevit7-setup\tests\stub.ps1 (-ClaudeCli), fake setup scripts
  (-SetupScript) and temp folders. Never runs the real claude.exe, the real setup or pyrevit.

  Cases:
    - The registration names the pyRevit 7 exe with mixed slashes: MCP ok, not MCP_WRONG_PATH.
    - A different exe: MCP_WRONG_PATH (negative control). No registration: MCP_MISSING.
    - -Fix MCP_MISSING / MCP_WRONG_PATH, setup exits 5: the doctor prints the manual
      'claude mcp add pyrevit -s user -- "<full path>" mcp' with backslashes only.
    - Setup exits 0, or another setup fix (AGENT_DISABLED) fails: no manual command.

  powershell -NoProfile -ExecutionPolicy Bypass -File skills\am-doctor\tests\test-mcp.ps1
  Exit 0 = all cases pass.
#>
$ErrorActionPreference = 'Stop'
$Doctor = (Resolve-Path (Join-Path $PSScriptRoot '..\scripts\doctor.ps1')).Path
$Stub = (Resolve-Path (Join-Path $PSScriptRoot '..\..\am-pyrevit7-setup\tests\stub.ps1')).Path
$Root = Join-Path ([IO.Path]::GetTempPath()) ("am-doctor-mcp-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
$script:Fail = 0; $script:Asserts = 0

function Assert-True([string]$name, [bool]$ok, [string]$detail) {
    $script:Asserts++
    if ($ok) { Write-Host "PASS $name" } else { $script:Fail++; Write-Host "FAIL $name :`n$detail" }
}
function New-StubClaude([string]$dir, $mcp) {
    New-Item -ItemType Directory -Force $dir | Out-Null
    $claude = Join-Path $dir 'claude.cmd'
    "@set `"AM_STUB_ARGS=%*`"`r`n@powershell -NoProfile -ExecutionPolicy Bypass -File `"$Stub`" claude" | Set-Content -LiteralPath $claude -Encoding Ascii
    $state = [ordered]@{ enabled = $true; policy = 'ask'; mcp = $mcp }
    [IO.File]::WriteAllText((Join-Path $dir 'state.json'), ($state | ConvertTo-Json -Depth 5), (New-Object Text.UTF8Encoding $false))
    $env:AM_STUB_DIR = $dir
    return $claude
}
function Invoke-Doctor([string[]]$argv) {
    $out = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Doctor @argv -LogDir (Join-Path $Root 'logs') 2>&1 | Out-String
    return @{ Code = $LASTEXITCODE; Out = $out }
}
function Get-Line([string]$out, [string]$pattern) { @(($out -split "`r?`n") | Where-Object { $_ -match $pattern }) }

try {
    New-Item -ItemType Directory -Force $Root | Out-Null
    $env:ARCHI_MAGIC_LOG_DIR = Join-Path $Root 'logs'   # keep test runs out of the real shared log
    $py7Root = Join-Path $Root 'user\pyRevit-Master'
    $py7 = Join-Path $py7Root 'bin\pyrevit.exe'
    $mixed = ($py7Root -replace '\\pyRevit-Master$', '/pyRevit-Master') + '/bin/pyrevit.exe'

    # 1. Same exe, mixed slashes: ok.
    $claude = New-StubClaude (Join-Path $Root 'c1') ([ordered]@{ scope = 'user'; command = $mixed; args = 'mcp'; env = @() })
    $r = Invoke-Doctor @('-Only', 'mcp', '-Py7Root', $py7Root, '-ClaudeCli', $claude)
    Assert-True 'mixed-slash same exe: MCP ok' ((Get-Line $r.Out '^MCP \| ok \|').Count -eq 1) $r.Out
    Assert-True 'mixed-slash same exe: no MCP_WRONG_PATH, exit 0' ((Get-Line $r.Out '^MCP_WRONG_PATH \|').Count -eq 0 -and $r.Code -eq 0) "exit $($r.Code)`n$($r.Out)"

    # 2. Different exe: MCP_WRONG_PATH (negative control).
    $claude = New-StubClaude (Join-Path $Root 'c2') ([ordered]@{ scope = 'user'; command = 'C:\Program Files\pyRevit-Master\bin\pyrevit.exe'; args = 'mcp'; env = @() })
    $r = Invoke-Doctor @('-Only', 'mcp', '-Py7Root', $py7Root, '-ClaudeCli', $claude)
    Assert-True 'different exe: MCP_WRONG_PATH, exit 1' ((Get-Line $r.Out '^MCP_WRONG_PATH \| error \|').Count -eq 1 -and $r.Code -eq 1) "exit $($r.Code)`n$($r.Out)"

    # 3. No registration: MCP_MISSING.
    $claude = New-StubClaude (Join-Path $Root 'c3') $null
    $r = Invoke-Doctor @('-Only', 'mcp', '-Py7Root', $py7Root, '-ClaudeCli', $claude)
    Assert-True 'no registration: MCP_MISSING' ((Get-Line $r.Out '^MCP_MISSING \| error \|').Count -eq 1) $r.Out

    # 4-7. -Fix with fake setup scripts. -Py7Root has a forward slash; the printed path must not.
    $fail5 = Join-Path $Root 'setup-fail.ps1'; Set-Content -LiteralPath $fail5 -Value 'exit 5' -Encoding Ascii
    $ok0 = Join-Path $Root 'setup-ok.ps1'; Set-Content -LiteralPath $ok0 -Value 'exit 0' -Encoding Ascii
    $slashRoot = ($py7Root -replace '\\pyRevit-Master$', '/pyRevit-Master')
    $want = 'claude mcp add pyrevit -s user -- "' + $py7 + '" mcp'

    $r = Invoke-Doctor @('-Fix', 'MCP_MISSING', '-Py7Root', $slashRoot, '-SetupScript', $fail5)
    $add = @(Get-Line $r.Out 'claude mcp add pyrevit')
    Assert-True 'MCP_MISSING setup fails: exit 5 and manual add with full backslash path' ($r.Code -eq 5 -and $add.Count -eq 1 -and $add[0].EndsWith("Run: $want") -and $add[0] -notmatch '/') "exit $($r.Code); want '$want'`n$($r.Out)"

    $r = Invoke-Doctor @('-Fix', 'MCP_WRONG_PATH', '-Py7Root', $slashRoot, '-SetupScript', $fail5)
    $add = @(Get-Line $r.Out 'claude mcp add pyrevit')
    Assert-True 'MCP_WRONG_PATH setup fails: remove line, then manual add' ($r.Code -eq 5 -and $add.Count -eq 1 -and $add[0].EndsWith("Run: $want") -and (Get-Line $r.Out 'claude mcp remove pyrevit -s user').Count -eq 1) "exit $($r.Code)`n$($r.Out)"

    $r = Invoke-Doctor @('-Fix', 'MCP_MISSING', '-Py7Root', $slashRoot, '-SetupScript', $ok0)
    Assert-True 'MCP_MISSING setup succeeds: no manual command' ($r.Code -eq 0 -and (Get-Line $r.Out 'claude mcp add').Count -eq 0) "exit $($r.Code)`n$($r.Out)"

    $r = Invoke-Doctor @('-Fix', 'AGENT_DISABLED', '-Py7Root', $slashRoot, '-SetupScript', $fail5)
    Assert-True 'AGENT_DISABLED setup fails: no MCP manual command' ($r.Code -eq 5 -and (Get-Line $r.Out 'claude mcp add').Count -eq 0) "exit $($r.Code)`n$($r.Out)"
} finally {
    Remove-Item Env:\AM_STUB_DIR, Env:\ARCHI_MAGIC_LOG_DIR -ErrorAction SilentlyContinue
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $Root
}

Write-Host ("{0} assertion(s), {1} failure(s)" -f $script:Asserts, $script:Fail)
if ($script:Asserts -eq 0) { Write-Host 'FAIL: no assertions ran'; exit 1 }
if ($script:Fail -gt 0) { exit 1 }
exit 0
