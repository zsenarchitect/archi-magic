<#
.SYNOPSIS
  Tests for pyrevit7-setup.ps1: (1) pyrevit.exe stderr under $ErrorActionPreference = 'Stop', and
  (2) an MCP registration whose command spells the pyRevit 7 path with mixed slashes.
  Hermetic: temp roots and the stub pyrevit/claude programs (stub.ps1). Never touches the real
  per-user manifests, the real pyRevit config or the real Claude Code MCP registration.

  Cases:
    - stderr : every pyrevit call also writes a line to STDERR (AM_STUB_PY7_STDERR=1). Windows
               PowerShell 5.1 turns redirected native stderr into a terminating error under EAP Stop.
               -EnableAgent, -Verify and -Undo must still exit 0 and still parse policy and agent state.
    - slashes: the registration names the same exe as 'C:\...\stub/pyrevit.cmd'. -EnableAgent must keep
               it (no remove, no add, no MCP journal entry) and -Verify must accept it (exit 0).
    - other  : negative control. A registration for a different exe is still replaced.

  Must run under Windows PowerShell 5.1 (powershell.exe):
  powershell -NoProfile -ExecutionPolicy Bypass -File skills\am-pyrevit7-setup\tests\test-stderr-paths.ps1
#>
$ErrorActionPreference = 'Stop'
$Setup = (Resolve-Path (Join-Path $PSScriptRoot '..\scripts\pyrevit7-setup.ps1')).Path
$Stub = (Resolve-Path (Join-Path $PSScriptRoot 'stub.ps1')).Path
$Base = Join-Path ([IO.Path]::GetTempPath()) ("am-setup-stderr-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
$script:Fail = 0; $script:Asserts = 0

function Check([bool]$cond, [string]$msg, [System.Collections.Generic.List[string]]$problems) {
    $script:Asserts++
    if (-not $cond) { $problems.Add($msg) }
}

function New-Env([string]$root, $mcp) {
    $machine = Join-Path $root 'machine'; $user = Join-Path $root 'user'; $py7Root = Join-Path $root 'pyRevit-Master'
    $backups = Join-Path $root 'backups'; $stubDir = Join-Path $root 'stub'
    foreach ($d in $machine, $user, $py7Root, $backups, $stubDir) { New-Item -ItemType Directory -Force $d | Out-Null }
    $py7 = Join-Path $stubDir 'pyrevit.cmd'; $claude = Join-Path $stubDir 'claude.cmd'
    "@set `"AM_STUB_ARGS=%*`"`r`n@powershell -NoProfile -ExecutionPolicy Bypass -File `"$Stub`" py7" | Set-Content -LiteralPath $py7 -Encoding Ascii
    "@set `"AM_STUB_ARGS=%*`"`r`n@powershell -NoProfile -ExecutionPolicy Bypass -File `"$Stub`" claude" | Set-Content -LiteralPath $claude -Encoding Ascii
    $state = [ordered]@{ enabled = $false; policy = 'ask'; mcp = $null }
    if ($mcp) { $state.mcp = $mcp }
    [IO.File]::WriteAllText((Join-Path $stubDir 'state.json'), ($state | ConvertTo-Json -Depth 5), (New-Object Text.UTF8Encoding $false))
    $env:AM_STUB_DIR = $stubDir; $env:AM_STUB_PY7 = $py7
    return @{
        StubDir = $stubDir; Py7 = $py7; Backups = $backups
        Common = @('-UserAddinsRoot', $user, '-MachineAddinsRoot', $machine, '-Py7Root', $py7Root, '-Py7Exe', $py7, '-ClaudeCli', $claude, '-BackupRoot', $backups)
    }
}
function Invoke-Setup($e, [string[]]$switches) {
    $common = $e.Common
    $out = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Setup @switches @common 2>&1 | Out-String
    return @{ Code = $LASTEXITCODE; Out = $out }
}
function Get-State($e) { [IO.File]::ReadAllText((Join-Path $e.StubDir 'state.json')) | ConvertFrom-Json }
function Get-Calls($e) { $p = Join-Path $e.StubDir 'calls.log'; if (Test-Path $p) { [IO.File]::ReadAllText($p) } else { '' } }
function Report([string]$name, $problems) {
    if ($problems.Count -eq 0) { Write-Host "PASS $name" }
    else { $script:Fail++; Write-Host "FAIL $name :`n  - $($problems -join "`n  - ")" }
}

try {
    New-Item -ItemType Directory -Force $Base | Out-Null
    $env:ARCHI_MAGIC_LOG_DIR = Join-Path $Base 'logs'   # keep test runs out of the real shared log

    # 1. pyrevit writes stderr on every call.
    $p = New-Object System.Collections.Generic.List[string]
    $e = New-Env (Join-Path $Base 'stderr') $null
    $env:AM_STUB_PY7_STDERR = '1'
    $r = Invoke-Setup $e @('-EnableAgent')
    Check ($r.Code -eq 0) "-EnableAgent exit $($r.Code)`n$($r.Out)" $p
    $s = Get-State $e
    Check ($s.enabled -eq $true -and $s.policy -eq 'ask') "agent state after -EnableAgent: $($s | ConvertTo-Json -Compress -Depth 5)" $p
    Check ($r.Out -match 'Agent policy: ask') "-EnableAgent did not read the policy back`n$($r.Out)" $p
    $r = Invoke-Setup $e @('-Verify')
    Check ($r.Code -eq 0) "-Verify exit $($r.Code)`n$($r.Out)" $p
    Check ($r.Out -match 'pyRevit \(per-user\): 7\.0\.0') "-Verify did not read the version`n$($r.Out)" $p
    Check ($r.Out -notmatch 'Agent host is not enabled') "-Verify misread agent status`n$($r.Out)" $p
    $r = Invoke-Setup $e @('-Undo')
    Check ($r.Code -eq 0) "-Undo exit $($r.Code)`n$($r.Out)" $p
    $s = Get-State $e
    Check ($s.enabled -eq $false -and -not $s.mcp) "state after -Undo: $($s | ConvertTo-Json -Compress -Depth 5)" $p
    Remove-Item Env:\AM_STUB_PY7_STDERR -ErrorAction SilentlyContinue
    Report 'pyrevit stderr under EAP Stop: -EnableAgent, -Verify, -Undo exit 0 and parse output' $p

    # 2. Registration names the same exe with mixed slashes.
    $p = New-Object System.Collections.Generic.List[string]
    $root = Join-Path $Base 'slashes'
    $py7Path = Join-Path $root 'stub\pyrevit.cmd'
    $mixed = $py7Path.Substring(0, $py7Path.LastIndexOf('\')) + '/pyrevit.cmd'
    $e = New-Env $root ([ordered]@{ scope = 'user'; command = $mixed; args = 'mcp'; env = @() })
    $r = Invoke-Setup $e @('-Verify')   # before -EnableAgent, so it reads the mixed-slash spelling
    Check ($r.Code -eq 0 -and $r.Out -match 'pyrevit registered with the pyRevit 7 path') "-Verify rejected the mixed-slash path (exit $($r.Code))`n$($r.Out)" $p
    $r = Invoke-Setup $e @('-EnableAgent')
    Check ($r.Code -eq 0) "-EnableAgent exit $($r.Code)`n$($r.Out)" $p
    $calls = Get-Calls $e
    Check ($calls -notmatch '(?m)^claude mcp (remove|add)' -and $calls -notmatch '(?m)^py7 mcp install') "same exe was re-registered; calls:`n$calls" $p
    $s = Get-State $e
    Check ($s.mcp -and $s.mcp.command -eq $mixed) "registration changed: $($s.mcp | ConvertTo-Json -Compress -Depth 5)" $p
    $journals = @(Get-ChildItem $e.Backups -Recurse -Filter journal.json -ErrorAction SilentlyContinue)
    $mcpJournaled = @($journals | Where-Object { ([IO.File]::ReadAllText($_.FullName) | ConvertFrom-Json).mcp })
    Check ($mcpJournaled.Count -eq 0) "an MCP change was journaled for the same exe" $p
    Report 'mixed-slash registration of the same exe: accepted by -Verify, kept by -EnableAgent' $p

    # 3. Negative control: a different exe is still replaced.
    $p = New-Object System.Collections.Generic.List[string]
    $root = Join-Path $Base 'other'
    $e = New-Env $root ([ordered]@{ scope = 'user'; command = 'C:/Program Files/pyRevit-Master/bin/pyrevit.exe'; args = 'mcp'; env = @() })
    $r = Invoke-Setup $e @('-EnableAgent')
    Check ($r.Code -eq 0) "-EnableAgent exit $($r.Code)`n$($r.Out)" $p
    $s = Get-State $e
    Check ($s.mcp -and $s.mcp.command -eq $e.Py7) "different exe not replaced: $($s.mcp | ConvertTo-Json -Compress -Depth 5)" $p
    $journals = @(Get-ChildItem $e.Backups -Recurse -Filter journal.json -ErrorAction SilentlyContinue)
    $act = @($journals | ForEach-Object { ([IO.File]::ReadAllText($_.FullName) | ConvertFrom-Json).mcp.action })
    Check ($act -contains 'replaced') "journal MCP action: $($act -join ',')" $p
    Report 'different exe: still replaced and journaled' $p
} finally {
    Remove-Item Env:\AM_STUB_DIR, Env:\AM_STUB_PY7, Env:\AM_STUB_PY7_STDERR, Env:\ARCHI_MAGIC_LOG_DIR -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $Base -Recurse -Force -ErrorAction SilentlyContinue
}
Write-Host "$script:Asserts assertion(s), $script:Fail failed"
if ($script:Fail -or $script:Asserts -eq 0) { exit 1 }
exit 0
