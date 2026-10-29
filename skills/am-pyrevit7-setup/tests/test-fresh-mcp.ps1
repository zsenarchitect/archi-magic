<#
.SYNOPSIS
  Tests for pyrevit7-setup.ps1 -EnableAgent on a fresh machine: no pyrevit MCP server registered yet.
  Hermetic: temp roots and the stub pyrevit/claude programs (stub.ps1). Never touches the real
  per-user manifests, the real pyRevit config or the real Claude Code MCP registration.

  The real claude.exe writes 'No MCP server named "pyrevit"' to STDERR and exits 1. Under
  $ErrorActionPreference = 'Stop', Windows PowerShell 5.1 turns that redirected stderr line into a
  terminating error, so setup died before it registered anything. The stub writes to stderr too.

  Scenarios (each: setup -EnableAgent, then -Undo):
    - install: 'pyrevit mcp install claude' registers the server (mcp get on a missing server first).
    - direct : 'pyrevit mcp install claude' registers nothing, so setup runs 'mcp remove' on a missing
               server, then 'mcp add' with the full path.
  Each expects: setup exit 0, a user-scope registration whose command is the py7 path, journal
  action 'added', undo exit 0 and no registration left.

  Must run under Windows PowerShell 5.1 (powershell.exe), where the bug lives:
  powershell -NoProfile -ExecutionPolicy Bypass -File skills\am-pyrevit7-setup\tests\test-fresh-mcp.ps1
#>
$ErrorActionPreference = 'Stop'
$Setup = (Resolve-Path (Join-Path $PSScriptRoot '..\scripts\pyrevit7-setup.ps1')).Path
$Stub = (Resolve-Path (Join-Path $PSScriptRoot 'stub.ps1')).Path
$Base = Join-Path ([IO.Path]::GetTempPath()) ("am-setup-fresh-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
$script:Fail = 0; $script:Asserts = 0

function Check([bool]$cond, [string]$msg, [System.Collections.Generic.List[string]]$problems) {
    $script:Asserts++
    if (-not $cond) { $problems.Add($msg) }
}

function Invoke-Fresh([string]$root, [bool]$installNoop) {
    $problems = New-Object System.Collections.Generic.List[string]
    $machine = Join-Path $root 'machine'; $user = Join-Path $root 'user'; $py7Root = Join-Path $root 'pyRevit-Master'
    $backups = Join-Path $root 'backups'; $stubDir = Join-Path $root 'stub'
    foreach ($d in $machine, $user, $py7Root, $backups, $stubDir) { New-Item -ItemType Directory -Force $d | Out-Null }

    $py7 = Join-Path $stubDir 'pyrevit.cmd'; $claude = Join-Path $stubDir 'claude.cmd'
    "@set `"AM_STUB_ARGS=%*`"`r`n@powershell -NoProfile -ExecutionPolicy Bypass -File `"$Stub`" py7" | Set-Content -LiteralPath $py7 -Encoding Ascii
    "@set `"AM_STUB_ARGS=%*`"`r`n@powershell -NoProfile -ExecutionPolicy Bypass -File `"$Stub`" claude" | Set-Content -LiteralPath $claude -Encoding Ascii
    $state0 = '{"enabled":false,"policy":"ask","mcp":null}'
    [IO.File]::WriteAllText((Join-Path $stubDir 'state.json'), $state0, (New-Object Text.UTF8Encoding $false))
    $env:AM_STUB_DIR = $stubDir; $env:AM_STUB_PY7 = $py7
    if ($installNoop) { $env:AM_STUB_INSTALL_NOOP = '1' } else { Remove-Item Env:\AM_STUB_INSTALL_NOOP -ErrorAction SilentlyContinue }

    $common = @('-UserAddinsRoot', $user, '-MachineAddinsRoot', $machine, '-Py7Root', $py7Root, '-Py7Exe', $py7, '-ClaudeCli', $claude, '-BackupRoot', $backups)
    $out = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Setup -EnableAgent @common 2>&1 | Out-String
    $code = $LASTEXITCODE
    Check ($code -eq 0) "setup exit $code`n$out" $problems
    $mid = [IO.File]::ReadAllText((Join-Path $stubDir 'state.json')) | ConvertFrom-Json
    Check ($mid.mcp -and $mid.mcp.scope -eq 'user' -and $mid.mcp.command -eq $py7) "registration after setup: $($mid | ConvertTo-Json -Compress -Depth 5)" $problems
    $journals = @(Get-ChildItem $backups -Recurse -Filter journal.json)
    Check ($journals.Count -eq 1) "expected 1 journal, found $($journals.Count)" $problems
    if ($journals.Count -eq 1) {
        $j = [IO.File]::ReadAllText($journals[0].FullName) | ConvertFrom-Json
        Check ($j.mcp.action -eq 'added' -and -not $j.mcp.previous) "journal MCP entry: $($j.mcp | ConvertTo-Json -Compress -Depth 5)" $problems
    }
    if ($installNoop) {
        $calls = [IO.File]::ReadAllText((Join-Path $stubDir 'calls.log'))
        Check ($calls -match '(?m)^claude mcp remove pyrevit -s user' -and $calls -match '(?m)^claude mcp add pyrevit') "direct path not taken; calls:`n$calls" $problems
    }

    $out = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Setup -Undo @common 2>&1 | Out-String
    $code = $LASTEXITCODE
    Check ($code -eq 0) "undo exit $code`n$out" $problems
    $end = [IO.File]::ReadAllText((Join-Path $stubDir 'state.json')) | ConvertFrom-Json
    Check (-not $end.mcp) "registration left after undo: $($end | ConvertTo-Json -Compress -Depth 5)" $problems
    return ,$problems
}

try {
    New-Item -ItemType Directory -Force $Base | Out-Null
    $env:ARCHI_MAGIC_LOG_DIR = Join-Path $Base 'logs'   # keep test runs out of the real shared log
    foreach ($case in @(@{ name = 'install'; noop = $false }, @{ name = 'direct'; noop = $true })) {
        $p = Invoke-Fresh (Join-Path $Base $case.name) $case.noop
        if ($p.Count -eq 0) { Write-Host "PASS fresh machine ($($case.name)): -EnableAgent registers pyrevit MCP, -Undo removes it" }
        else { $script:Fail++; Write-Host "FAIL fresh machine ($($case.name)):`n  - $($p -join "`n  - ")" }
    }
} finally {
    Remove-Item Env:\AM_STUB_DIR, Env:\AM_STUB_PY7, Env:\AM_STUB_INSTALL_NOOP, Env:\ARCHI_MAGIC_LOG_DIR -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $Base -Recurse -Force -ErrorAction SilentlyContinue
}
Write-Host "$script:Asserts assertion(s), $script:Fail failed"
if ($script:Fail -or $script:Asserts -eq 0) { exit 1 }
exit 0
