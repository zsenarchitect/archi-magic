<#
.SYNOPSIS
  Run the Python skill tests on this computer, as CI does: pytest in each skills/*/tests
  folder that has test_*.py files.

.DESCRIPTION
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run-python-tests.ps1 [-Skill am-doctor,am-markup-intake] [-Python <python.exe>]

  -Skill   Only these skills (folder names). Default: every skill with tests.
  -Python  Use this python.exe. Default: the first that runs Python 3 of
           'py -3', 'python', 'python3'. The Microsoft Store alias and the CPython in
           the pyRevit folder (bin\cengines) are never used: they cannot run the tests.

  Install the test packages first (per user, no admin):
    py -3 -m pip install --user -r requirements-dev.txt

  Unlike CI, the script runs every folder and does not stop at the first failure.
  Some tests can fail on Windows on a clean main (see CONTRIBUTING.md). Compare
  with a clean branch before you report a failure as new.

  Exit codes: 0 all passed, 1 a test folder failed, 2 no usable Python or unknown
  skill, 3 pytest is not installed.
  Windows PowerShell 5.1 and PowerShell 7. ASCII only.
#>
param(
    [string[]]$Skill = @(),
    [string]$Python = ''
)
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot

# Returns the major version that "<exe> <prefix> -c ..." prints, or '' when it does not run.
function Get-PyMajor([string]$exe, [string[]]$prefix) {
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = & $exe @prefix -c 'import sys; print(sys.version_info[0])' 2>$null
        if ($LASTEXITCODE -ne 0) { return '' }
        return ((@($out) -join '').Trim())
    } catch {
        return ''
    } finally {
        $ErrorActionPreference = $old
    }
}

$candidates = @()
if ($Python) {
    $candidates += , @($Python, @())
} else {
    foreach ($c in @(@('py', @('-3')), @('python', @()), @('python3', @()))) {
        $cmd = Get-Command $c[0] -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($cmd) { $candidates += , @($cmd.Source, $c[1]) }
    }
}
$pyExe = $null; $pyPrefix = @()
foreach ($c in $candidates) {
    if ($c[0] -match '[\\/]bin[\\/]cengines[\\/]') {
        Write-Host "SKIP $($c[0]): the pyRevit CPython has no pip and no pytest."
        continue
    }
    if ((Get-PyMajor $c[0] $c[1]) -eq '3') { $pyExe = $c[0]; $pyPrefix = $c[1]; break }
}
if (-not $pyExe) {
    Write-Host 'FAIL: no Python 3 found. "python" can be only the Microsoft Store alias.'
    Write-Host '  Install one per user (no admin): winget install --id Python.Python.3.12 --scope user'
    Write-Host '  Then start a new terminal, or pass -Python <full path to python.exe>.'
    exit 2
}
$pyLabel = (@($pyExe) + $pyPrefix) -join ' '
Write-Host "Python: $pyLabel"

$old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
& $pyExe @pyPrefix -c 'import pytest' 2>$null
$hasPytest = ($LASTEXITCODE -eq 0)
$ErrorActionPreference = $old
if (-not $hasPytest) {
    Write-Host 'FAIL: pytest is not installed for this Python. Install the test packages (per user, no admin):'
    Write-Host "  $pyLabel -m pip install --user -r `"$(Join-Path $Root 'requirements-dev.txt')`""
    exit 3
}

$dirs = @(Get-ChildItem -LiteralPath (Join-Path $Root 'skills') -Directory | Sort-Object Name | Where-Object {
    $t = Join-Path $_.FullName 'tests'
    (Test-Path -LiteralPath $t) -and @(Get-ChildItem -LiteralPath $t -File -Filter 'test_*.py').Count -gt 0
})
# powershell -File passes "a,b" as one string: split it here.
$Skill = @($Skill | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim() } | Where-Object { $_ })
if ($Skill.Count -gt 0) {
    $names = @($dirs | ForEach-Object { $_.Name })
    $unknown = @($Skill | Where-Object { $names -notcontains $_ })
    if ($unknown.Count -gt 0) {
        Write-Host "FAIL: no Python tests for: $($unknown -join ', ')"
        exit 2
    }
    $dirs = @($dirs | Where-Object { $Skill -contains $_.Name })
}
if ($dirs.Count -eq 0) { Write-Host 'FAIL: no Python test folders found'; exit 2 }

$results = @()
foreach ($d in $dirs) {
    Write-Host "== skills/$($d.Name)/tests"
    Push-Location -LiteralPath $d.FullName
    try {
        $ErrorActionPreference = 'Continue'
        & $pyExe @pyPrefix -m pytest -q -p no:cacheprovider tests
        $rc = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = 'Stop'
        Pop-Location
    }
    $results += New-Object psobject -Property @{ Skill = $d.Name; Code = $rc }
}

Write-Host ''
Write-Host "Python test folders run: $($results.Count)"
$failed = @($results | Where-Object { $_.Code -ne 0 })
foreach ($r in $results) {
    $state = if ($r.Code -eq 0) { 'pass' } else { "FAIL (pytest exit $($r.Code))" }
    Write-Host ("  {0,-28} {1}" -f $r.Skill, $state)
}
if ($failed.Count -gt 0) {
    Write-Host "FAIL: $($failed.Count) of $($results.Count) folder(s)."
    exit 1
}
Write-Host 'PASS'
exit 0
