<#
.SYNOPSIS
  Tests for the am-doctor agent policy report. Hermetic: the stub pyrevit program from
  am-pyrevit7-setup\tests\stub.ps1 (-Py7Exe) and temp folders. Never runs the real pyrevit.exe,
  and never changes the real agent policy.

  Cases:
    - ask: AGENT_POLICY ok, and the line names the switch command.
    - readonly: AGENT_POLICY info (a valid policy, not 'unchecked'), with the switch command.
    - auto: POLICY_AUTO warn, the fix sets 'ask'.
    - Unknown output: AGENT_POLICY unchecked (negative control).
    - Diagnose mode reads the policy and never sets it (no 'configs agent policy <value>' call).

  powershell -NoProfile -ExecutionPolicy Bypass -File skills\am-doctor\tests\test-policy.ps1
  Exit 0 = all cases pass.
#>
$ErrorActionPreference = 'Stop'
$Doctor = (Resolve-Path (Join-Path $PSScriptRoot '..\scripts\doctor.ps1')).Path
$Stub = (Resolve-Path (Join-Path $PSScriptRoot '..\..\am-pyrevit7-setup\tests\stub.ps1')).Path
$Root = Join-Path ([IO.Path]::GetTempPath()) ("am-doctor-policy-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
$script:Fail = 0; $script:Asserts = 0

function Assert-True([string]$name, [bool]$ok, [string]$detail) {
    $script:Asserts++
    if ($ok) { Write-Host "PASS $name" } else { $script:Fail++; Write-Host "FAIL $name :`n$detail" }
}
function New-StubPy7([string]$dir, [string]$policy) {
    New-Item -ItemType Directory -Force $dir | Out-Null
    $py7 = Join-Path $dir 'pyrevit.cmd'
    "@set `"AM_STUB_ARGS=%*`"`r`n@powershell -NoProfile -ExecutionPolicy Bypass -File `"$Stub`" py7" | Set-Content -LiteralPath $py7 -Encoding Ascii
    $state = [ordered]@{ enabled = $true; policy = $policy; mcp = $null }
    [IO.File]::WriteAllText((Join-Path $dir 'state.json'), ($state | ConvertTo-Json -Depth 5), (New-Object Text.UTF8Encoding $false))
    $env:AM_STUB_DIR = $dir
    return $py7
}
function Invoke-Doctor([string]$py7) {
    # Assert on the policy lines only: the agent status part depends on whether Revit runs on this machine.
    $out = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Doctor -Only agent -Py7Exe $py7 -Py7Root (Join-Path $Root 'no-py7') -LogDir (Join-Path $Root 'logs') -BusyWaitSec 0 2>&1 | Out-String
    return @{ Code = $LASTEXITCODE; Out = $out }
}
function Get-Line([string]$out, [string]$pattern) { @(($out -split "`r?`n") | Where-Object { $_ -match $pattern }) }
function Get-SetCalls([string]$dir) {
    $log = Join-Path $dir 'calls.log'
    if (-not (Test-Path -LiteralPath $log)) { return @() }
    @(Get-Content -LiteralPath $log | Where-Object { $_ -match '^py7 configs agent policy \S+' })
}

try {
    New-Item -ItemType Directory -Force $Root | Out-Null
    $env:ARCHI_MAGIC_LOG_DIR = Join-Path $Root 'logs'   # keep test runs out of the real shared log
    $switchCmd = [regex]::Escape('pyrevit configs agent policy readonly|ask|auto')

    # 1. ask: ok, with the switch command and the "only when the user asks" rule.
    $d = Join-Path $Root 'ask'; $py7 = New-StubPy7 $d 'ask'
    $r = Invoke-Doctor $py7
    $l = @(Get-Line $r.Out '^AGENT_POLICY \| ok \|')
    Assert-True 'ask: AGENT_POLICY ok' ($l.Count -eq 1 -and $l[0] -match 'Agent policy: ask') $r.Out
    Assert-True 'ask: names the switch command and the user-asks rule' ($l.Count -eq 1 -and $l[0] -match $switchCmd -and $l[0] -match 'only when the user asks') $r.Out
    Assert-True 'ask: no POLICY_AUTO' ((Get-Line $r.Out '^POLICY_AUTO \|').Count -eq 0) $r.Out

    # 2. readonly: a valid policy, reported as info, not unchecked.
    $d = Join-Path $Root 'readonly'; $py7 = New-StubPy7 $d 'readonly'
    $r = Invoke-Doctor $py7
    $l = @(Get-Line $r.Out '^AGENT_POLICY \| info \|')
    Assert-True 'readonly: AGENT_POLICY info' ($l.Count -eq 1 -and $l[0] -match 'Agent policy: readonly' -and $l[0] -match $switchCmd) $r.Out
    Assert-True 'readonly: not unchecked' ((Get-Line $r.Out '^AGENT_POLICY \| unchecked \|').Count -eq 0) $r.Out

    # 3. auto: warn, and the fix sets ask.
    $d = Join-Path $Root 'auto'; $py7 = New-StubPy7 $d 'auto'
    $r = Invoke-Doctor $py7
    $l = @(Get-Line $r.Out '^POLICY_AUTO \| warn \|')
    Assert-True 'auto: POLICY_AUTO warn with the fix to ask' ($l.Count -eq 1 -and $l[0] -match 'pyrevit configs agent policy ask') $r.Out
    Assert-True 'auto: no AGENT_POLICY ok' ((Get-Line $r.Out '^AGENT_POLICY \| ok \|').Count -eq 0) $r.Out

    # 4. Unknown output: unchecked (negative control).
    $d = Join-Path $Root 'odd'; $py7 = New-StubPy7 $d 'sometimes'
    $r = Invoke-Doctor $py7
    Assert-True 'unknown policy: AGENT_POLICY unchecked' ((Get-Line $r.Out '^AGENT_POLICY \| unchecked \|').Count -eq 1) $r.Out

    # 5. Diagnose mode never sets the policy, and the stub state is unchanged.
    foreach ($p in 'ask', 'readonly', 'auto') {
        $d = Join-Path $Root $p
        $calls = Get-SetCalls $d
        $s = [IO.File]::ReadAllText((Join-Path $d 'state.json')) | ConvertFrom-Json
        $read = @(Get-Content -LiteralPath (Join-Path $d 'calls.log') | Where-Object { $_ -eq 'py7 configs agent policy' })
        Assert-True "${p}: policy read once, never set" ($calls.Count -eq 0 -and $read.Count -eq 1 -and $s.policy -eq $p) "set calls: $($calls -join '; '); reads: $($read.Count); state: $($s.policy)"
    }
} finally {
    Remove-Item Env:\AM_STUB_DIR, Env:\ARCHI_MAGIC_LOG_DIR -ErrorAction SilentlyContinue
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $Root
}

Write-Host ("{0} assertion(s), {1} failure(s)" -f $script:Asserts, $script:Fail)
if ($script:Asserts -eq 0) { Write-Host 'FAIL: no assertions ran'; exit 1 }
if ($script:Fail -gt 0) { exit 1 }
exit 0
