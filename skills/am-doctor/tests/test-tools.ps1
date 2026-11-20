<#
.SYNOPSIS
  Tests for the am-doctor 'tools' group (issue #254): git, gh sign-in, and a real Python versus
  the Microsoft Store alias. Hermetic: small .cmd stubs in a temp folder stand in for git, gh and
  python (-GitExe, -GhExe, -PythonExe; 'none' = not installed). Never runs the real git, gh or
  python, and never calls the network.

  Cases:
    1. git missing -> GIT_MISSING warn, exit 1. git stub -> GIT ok with its version line.
       A git that fails to run -> GIT_MISSING warn.
    2. gh missing -> GH_MISSING info (not a problem: exit 0 when the rest is fine).
       gh signed in -> GH ok, and the account name never shows. gh signed out -> GH_SIGNED_OUT info.
    3. Python: a stub that prints the Python 3 marker -> PYTHON ok. The Microsoft Store alias
       message ("Python was not found; run without arguments to install from the Microsoft Store")
       -> PYTHON_MISSING warn "only the Microsoft Store alias". A Python 2 -> PYTHON_MISSING warn.
       None -> PYTHON_MISSING warn "not found".
    5. Python Launcher (issue #254 comment 2): 'python' is the Store alias, 'py -3' works ->
       PYTHON info that names 'py -3' (no PYTHON_MISSING, exit 0). 'py' gets '-3' first.
    6. pyRevit's own CPython (bin\cengines\CPY*\python, issue #254 comment 1): it is the only
       working Python -> PYTHON_PYREVIT_ONLY warn, exit 1. With a real Python too -> that one wins.
    7. pip and pytest: pip but no pytest -> PYTEST_MISSING info with 'pip install --user pytest'.
       No pip -> PYTEST_MISSING info "has no pip". Both present -> no PYTEST_MISSING line.
    4. (runs last) -Fix PYTHON_MISSING / GIT_MISSING / GH_SIGNED_OUT / PYTHON_PYREVIT_ONLY /
       PYTEST_MISSING -> exit 4 with the manual step.

  powershell -NoProfile -ExecutionPolicy Bypass -File skills\am-doctor\tests\test-tools.ps1
  Exit 0 = all cases pass.
#>
$ErrorActionPreference = 'Stop'
$Doctor = (Resolve-Path (Join-Path $PSScriptRoot '..\scripts\doctor.ps1')).Path
$Root = Join-Path ([IO.Path]::GetTempPath()) ("am-doctor-tools-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
$script:Fail = 0; $script:Asserts = 0

function Assert-True([string]$name, [bool]$ok, [string]$detail) {
    $script:Asserts++
    if ($ok) { Write-Host "PASS $name" } else { $script:Fail++; Write-Host "FAIL $name :`n$detail" }
}
function Get-Lines([string]$out, [string]$pattern) { @(($out -split "`r?`n") | Where-Object { $_ -match $pattern }) }
function New-Stub([string]$name, [string[]]$lines) {
    $p = Join-Path $Root "stubs\$name.cmd"
    New-Item -ItemType Directory -Force (Split-Path -Parent $p) | Out-Null
    (@('@echo off') + $lines) -join "`r`n" | Set-Content -LiteralPath $p -Encoding Ascii
    return $p
}
function Invoke-Doctor([string]$git, [string]$gh, [string]$py, [string[]]$extra = @('-Only', 'tools')) {
    $out = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Doctor @extra -GitExe $git -GhExe $gh -PythonExe $py -LogDir (Join-Path $Root 'logs') 2>&1 | Out-String
    return @{ Code = $LASTEXITCODE; Out = $out }
}

try {
    New-Item -ItemType Directory -Force $Root | Out-Null
    $env:ARCHI_MAGIC_LOG_DIR = Join-Path $Root 'logs'   # keep test runs out of the real shared log
    $gitOk = New-Stub 'git-ok' @('echo git version 2.47.0.windows.1')
    $gitBad = New-Stub 'git-bad' @('echo fatal: broken 1>&2', 'exit /b 1')
    $ghIn = New-Stub 'gh-in' @('echo github.com', 'echo   Logged in to github.com account jdoe-test (keyring)', 'exit /b 0')
    $ghOut = New-Stub 'gh-out' @('echo You are not logged into any GitHub hosts. To log in, run: gh auth login 1>&2', 'exit /b 1')
    $pyOk = New-Stub 'python-ok' @('echo AMPY3.12')
    $pyAlias = New-Stub 'python-alias' @('echo Python was not found; run without arguments to install from the Microsoft Store, or disable this shortcut from Settings ^> Apps ^> Advanced app settings ^> App execution aliases.', 'exit /b 9009')
    $py2 = New-Stub 'python2' @('echo AMPY2.7')
    # The launcher stub answers only when '-3' comes first, like 'py -3'.
    $pyLauncher = New-Stub 'py-launcher' @('if not "%1"=="-3" (echo wrong launcher args 1>&2 & exit /b 2)', 'echo AMPY3.12', 'echo AMMOD pip=1 pytest=1')
    $pyNoPytest = New-Stub 'python-nopytest' @('echo AMPY3.12', 'echo AMMOD pip=1 pytest=0')
    $pyNoPip = New-Stub 'python-nopip' @('echo AMPY3.12', 'echo AMMOD pip=0 pytest=0')
    $pyFull = New-Stub 'python-full' @('echo AMPY3.12', 'echo AMMOD pip=1 pytest=1')
    $pyRevitPy = Join-Path $Root 'stubs\pyRevit-Master\bin\cengines\CPY3123\python.cmd'
    New-Item -ItemType Directory -Force (Split-Path -Parent $pyRevitPy) | Out-Null
    (@('@echo off', 'echo AMPY3.12', 'echo AMMOD pip=0 pytest=0') -join "`r`n") | Set-Content -LiteralPath $pyRevitPy -Encoding Ascii

    # 1. git.
    $r = Invoke-Doctor 'none' $ghIn $pyOk
    Assert-True '1: git missing -> GIT_MISSING warn, exit 1' ($r.Code -eq 1 -and (Get-Lines $r.Out '^GIT_MISSING \| warn \|.*git not found').Count -eq 1) "exit $($r.Code)`n$($r.Out)"
    $r = Invoke-Doctor $gitOk $ghIn $pyOk
    Assert-True '1: git present -> GIT ok with the version, exit 0' ($r.Code -eq 0 -and (Get-Lines $r.Out '^GIT \| ok \|.*git version 2\.47\.0').Count -eq 1) "exit $($r.Code)`n$($r.Out)"
    $r = Invoke-Doctor $gitBad $ghIn $pyOk
    Assert-True '1: git fails to run -> GIT_MISSING warn' ($r.Code -eq 1 -and (Get-Lines $r.Out '^GIT_MISSING \| warn \|.*did not run').Count -eq 1) "exit $($r.Code)`n$($r.Out)"

    # 2. gh.
    $r = Invoke-Doctor $gitOk 'none' $pyOk
    Assert-True '2: gh missing -> GH_MISSING info, exit 0' ($r.Code -eq 0 -and (Get-Lines $r.Out '^GH_MISSING \| info \|').Count -eq 1) "exit $($r.Code)`n$($r.Out)"
    $r = Invoke-Doctor $gitOk $ghIn $pyOk
    Assert-True '2: gh signed in -> GH ok' ((Get-Lines $r.Out '^GH \| ok \|.*signed in to github\.com').Count -eq 1) $r.Out
    Assert-True '2: the account name never shows' ($r.Out -notmatch 'jdoe-test') $r.Out
    $r = Invoke-Doctor $gitOk $ghOut $pyOk
    Assert-True '2: gh signed out -> GH_SIGNED_OUT info, exit 0' ($r.Code -eq 0 -and (Get-Lines $r.Out '^GH_SIGNED_OUT \| info \|.*not signed in').Count -eq 1) "exit $($r.Code)`n$($r.Out)"

    # 3. Python.
    $r = Invoke-Doctor $gitOk $ghIn $pyOk
    Assert-True '3: real Python -> PYTHON ok 3.12' ((Get-Lines $r.Out '^PYTHON \| ok \|.*Python 3\.12').Count -eq 1) $r.Out
    $r = Invoke-Doctor $gitOk $ghIn $pyAlias
    Assert-True '3: Store alias -> PYTHON_MISSING warn "only the Microsoft Store alias", exit 1' ($r.Code -eq 1 -and (Get-Lines $r.Out "^PYTHON_MISSING \| warn \|.*no Python: 'python' is only the Microsoft Store alias").Count -eq 1) "exit $($r.Code)`n$($r.Out)"
    $r = Invoke-Doctor $gitOk $ghIn $py2
    Assert-True '3: Python 2 -> PYTHON_MISSING warn' ($r.Code -eq 1 -and (Get-Lines $r.Out '^PYTHON_MISSING \| warn \|.*not a working Python 3').Count -eq 1) "exit $($r.Code)`n$($r.Out)"
    $r = Invoke-Doctor $gitOk $ghIn 'none'
    Assert-True '3: no Python -> PYTHON_MISSING warn "not found"' ($r.Code -eq 1 -and (Get-Lines $r.Out '^PYTHON_MISSING \| warn \|.*not found').Count -eq 1) "exit $($r.Code)`n$($r.Out)"

    # 5. Python Launcher: python is the Store alias, py -3 works.
    $r = Invoke-Doctor $gitOk $ghIn $pyAlias @('-Only', 'tools', '-PyExe', $pyLauncher)
    Assert-True "5: Store alias + py launcher -> PYTHON info names 'py -3', exit 0" ($r.Code -eq 0 -and (Get-Lines $r.Out "^PYTHON \| info \|.*'python' is only the Microsoft Store alias, but 'py -3' runs Python 3\.12").Count -eq 1) "exit $($r.Code)`n$($r.Out)"
    Assert-True '5: py launcher works -> no PYTHON_MISSING line' ((Get-Lines $r.Out '^PYTHON_MISSING').Count -eq 0) $r.Out
    $r = Invoke-Doctor $gitOk $ghIn 'none' @('-Only', 'tools', '-PyExe', $pyLauncher)
    Assert-True "5: no python, py launcher only -> PYTHON info 'py -3'" ($r.Code -eq 0 -and (Get-Lines $r.Out "^PYTHON \| info \|.*'python' is not found, but 'py -3' runs").Count -eq 1) "exit $($r.Code)`n$($r.Out)"
    $r = Invoke-Doctor $gitOk $ghIn $pyAlias @('-Only', 'tools', '-PyExe', 'none')
    Assert-True '5: Store alias, py none -> PYTHON_MISSING warn' ($r.Code -eq 1 -and (Get-Lines $r.Out '^PYTHON_MISSING \| warn \|').Count -eq 1) "exit $($r.Code)`n$($r.Out)"

    # 6. pyRevit's own CPython.
    $r = Invoke-Doctor $gitOk $ghIn $pyRevitPy
    Assert-True '6: only pyRevit CPython -> PYTHON_PYREVIT_ONLY warn, exit 1' ($r.Code -eq 1 -and (Get-Lines $r.Out "^PYTHON_PYREVIT_ONLY \| warn \|.*Only pyRevit's own CPython 3\.12 runs.*no pip and no pytest").Count -eq 1) "exit $($r.Code)`n$($r.Out)"
    Assert-True '6: pyRevit CPython is not reported as PYTHON ok' ((Get-Lines $r.Out '^PYTHON \| ok').Count -eq 0) $r.Out
    $r = Invoke-Doctor $gitOk $ghIn $pyRevitPy @('-Only', 'tools', '-PyExe', $pyLauncher)
    Assert-True "6: pyRevit CPython + py launcher -> PYTHON info 'py -3', no PYTHON_PYREVIT_ONLY" ($r.Code -eq 0 -and (Get-Lines $r.Out "^PYTHON \| info \|.*'py -3' runs").Count -eq 1 -and (Get-Lines $r.Out '^PYTHON_PYREVIT_ONLY').Count -eq 0) "exit $($r.Code)`n$($r.Out)"

    # 7. pip and pytest.
    $r = Invoke-Doctor $gitOk $ghIn $pyNoPytest
    Assert-True '7: pip, no pytest -> PYTEST_MISSING info with pip install --user pytest, exit 0' ($r.Code -eq 0 -and (Get-Lines $r.Out '^PYTEST_MISSING \| info \|.*has pip but no pytest.*python -m pip install --user pytest').Count -eq 1) "exit $($r.Code)`n$($r.Out)"
    $r = Invoke-Doctor $gitOk $ghIn $pyNoPip
    Assert-True '7: no pip -> PYTEST_MISSING info "has no pip"' ($r.Code -eq 0 -and (Get-Lines $r.Out '^PYTEST_MISSING \| info \|.*has no pip').Count -eq 1) "exit $($r.Code)`n$($r.Out)"
    $r = Invoke-Doctor $gitOk $ghIn $pyFull
    Assert-True '7: pip and pytest -> PYTHON ok, no PYTEST_MISSING' ((Get-Lines $r.Out '^PYTHON \| ok').Count -eq 1 -and (Get-Lines $r.Out '^PYTEST_MISSING').Count -eq 0) $r.Out

    # 4. Manual fixes.
    foreach ($c in @(@{ Code = 'PYTHON_MISSING'; Want = 'winget install --id Python\.Python' }, @{ Code = 'GIT_MISSING'; Want = 'winget install --id Git\.Git' }, @{ Code = 'GH_SIGNED_OUT'; Want = '& "<path>\\gh\.exe" auth login' }, @{ Code = 'PYTHON_PYREVIT_ONLY'; Want = 'no pip and no pytest' }, @{ Code = 'PYTEST_MISSING'; Want = 'pip install --user pytest' })) {
        $r = Invoke-Doctor $gitOk $ghIn $pyOk @('-Fix', $c.Code)
        Assert-True "4: -Fix $($c.Code) -> exit 4 with the manual step" ($r.Code -eq 4 -and $r.Out -match $c.Want) "exit $($r.Code)`n$($r.Out)"
    }
} finally {
    Remove-Item Env:\ARCHI_MAGIC_LOG_DIR -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $Root -Recurse -Force -ErrorAction SilentlyContinue
}
Write-Host ("{0} assertion(s), {1} failure(s)" -f $script:Asserts, $script:Fail)
if ($script:Asserts -eq 0) { Write-Host 'FAIL: no assertions ran'; exit 1 }
if ($script:Fail -gt 0) { exit 1 }
exit 0
