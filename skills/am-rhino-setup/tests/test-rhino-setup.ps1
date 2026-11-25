<#
.SYNOPSIS
  Tests for rhino-setup.ps1 (issues #290 and #305). Hermetic: temp settings file, temp USERPROFILE,
  APPDATA, CLAUDE_CONFIG_DIR and ProgramFiles, a PATH with only Windows folders (plus a node stub
  for the default-run case). Never reads or writes the real ~/.claude, never runs Yak, Rhino or
  RhinoCode (the fake Rhino files are empty and never started).

  Cases:
    1. -AskRules on a new settings file: 56 ask rules, console names only the first 3.
    2. -AskRules again: nothing to change, no backup.
    3. -NoAsk: removes only this script's ask rules, adds the two server allow rules, keeps
       every other key and rule, writes a backup.
    4. Default run (no switch) in do-not-ask mode: does not add the ask rules back.
    5. Explicit -AskRules: asks again, removes the two server allow rules, keeps the user's own.
    6. -Verify on a machine that is not set up: exit 6, short output, each problem named once,
       RhinoCode fallback hint.
    7. -NoAsk with -AskRules: exit 5. Invalid JSON: exit 5 and the file is unchanged.

  powershell -NoProfile -ExecutionPolicy Bypass -File skills\am-rhino-setup\tests\test-rhino-setup.ps1
  Exit 0 = all cases pass.
#>
$ErrorActionPreference = 'Stop'
$Setup = (Resolve-Path (Join-Path $PSScriptRoot '..\scripts\rhino-setup.ps1')).Path
$Root = Join-Path ([IO.Path]::GetTempPath()) ("am-rhino-setup-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
$script:Fail = 0; $script:Asserts = 0
$PluginAsk = 'mcp__plugin_rhino-mcp_rhino__run_python'
$ManualAsk = 'mcp__rhino__g2_start'
$FourthName = 'mcp__plugin_rhino-mcp_rhino__open_doc'

function Assert-True([string]$name, [bool]$ok, [string]$detail) {
    $script:Asserts++
    if ($ok) { Write-Host "PASS $name" } else { $script:Fail++; Write-Host "FAIL $name :`n$detail" }
}
function Read-Settings([string]$p) { return ([IO.File]::ReadAllText($p) | ConvertFrom-Json) }

$envNames = @('USERPROFILE', 'APPDATA', 'CLAUDE_CONFIG_DIR', 'ProgramFiles', 'PATH')
function Invoke-Setup([string]$HomeDir, [string[]]$ArgList, [switch]$WithNode) {
    $saved = @{}
    foreach ($n in $envNames) { $saved[$n] = [Environment]::GetEnvironmentVariable($n, 'Process') }
    try {
        $win = $env:SystemRoot
        $path = "$win\System32;$win;$win\System32\WindowsPowerShell\v1.0"
        if ($WithNode) { $path = (Join-Path $HomeDir 'bin') + ';' + $path }
        [Environment]::SetEnvironmentVariable('USERPROFILE', (Join-Path $HomeDir 'user'), 'Process')
        [Environment]::SetEnvironmentVariable('APPDATA', (Join-Path $HomeDir 'appdata'), 'Process')
        [Environment]::SetEnvironmentVariable('CLAUDE_CONFIG_DIR', (Join-Path $HomeDir 'claude'), 'Process')
        [Environment]::SetEnvironmentVariable('ProgramFiles', (Join-Path $HomeDir 'pf'), 'Process')
        [Environment]::SetEnvironmentVariable('PATH', $path, 'Process')
        $exe = Join-Path $PSHOME 'powershell.exe'
        # Native stderr with EAP=Stop is a terminating error in PS 5.1; collect it as text instead.
        $ErrorActionPreference = 'Continue'
        $out = (& $exe -NoProfile -ExecutionPolicy Bypass -File $Setup @ArgList 2>&1 | ForEach-Object { "$_" }) -join "`n"
        return @{ Code = $LASTEXITCODE; Out = $out }
    } finally {
        $ErrorActionPreference = 'Stop'
        foreach ($n in $envNames) { [Environment]::SetEnvironmentVariable($n, $saved[$n], 'Process') }
    }
}
function New-Home([string]$name, [switch]$FakeRhino, [switch]$FakePackage, [switch]$NodeStub) {
    $h = Join-Path $Root $name
    foreach ($d in 'user', 'appdata', 'claude', 'pf', 'bin') { New-Item -ItemType Directory -Force (Join-Path $h $d) | Out-Null }
    if ($FakeRhino) {
        $sys = Join-Path $h 'pf\Rhino 8\System'
        New-Item -ItemType Directory -Force $sys | Out-Null
        foreach ($f in 'Rhino.exe', 'RhinoCode.exe', 'Yak.exe') { [IO.File]::WriteAllBytes((Join-Path $sys $f), [byte[]]@()) }
    }
    if ($FakePackage) {
        New-Item -ItemType Directory -Force (Join-Path $h 'appdata\McNeel\Rhinoceros\packages\8.0\Rhino-MCP-Platform\0.3.0') | Out-Null
    }
    if ($NodeStub) {
        [IO.File]::WriteAllText((Join-Path $h 'bin\node.cmd'), "@echo v20.0.0`r`n")
    }
    return $h
}

try {
    New-Item -ItemType Directory -Force $Root | Out-Null

    # 1. -AskRules on a new file.
    $h = New-Home 'one'
    $s = Join-Path $h 'claude\settings.json'
    $r = Invoke-Setup $h @('-AskRules', '-SettingsPath', $s)
    $ask = @((Read-Settings $s).permissions.ask)
    Assert-True 'askrules: exit 0' ($r.Code -eq 0) $r.Out
    Assert-True 'askrules: 56 rules written, both name forms' ($ask.Count -eq 56 -and ($ask -contains $PluginAsk) -and ($ask -contains $ManualAsk)) ($ask -join ', ')
    Assert-True 'askrules: console shows count and first names only' ($r.Out -match 'Added 56 ask rule\(s\): .+ and 53 more' -and $r.Out -notmatch [regex]::Escape($FourthName)) $r.Out

    # 2. Run again: nothing to change.
    $r = Invoke-Setup $h @('-AskRules', '-SettingsPath', $s)
    Assert-True 'askrules again: no change, no backup' ($r.Code -eq 0 -and $r.Out -match 'already present' -and @(Get-ChildItem (Split-Path $s) -Filter '*.am-rhino-backup-*').Count -eq 0) $r.Out

    # 3. -NoAsk on a file with other keys and the user's own rules.
    $h = New-Home 'three' -FakeRhino -FakePackage -NodeStub
    $s = Join-Path $h 'claude\settings.json'
    $seed = [ordered]@{
        model = 'example-model'
        permissions = [ordered]@{ allow = @('Bash(git status)'); ask = @('Bash(git push:*)', $PluginAsk, $ManualAsk); deny = @('Bash(rm -rf:*)') }
        env = [ordered]@{ EXAMPLE = '1' }
    }
    [IO.File]::WriteAllText($s, ($seed | ConvertTo-Json -Depth 10), (New-Object Text.UTF8Encoding $false))
    $r = Invoke-Setup $h @('-NoAsk', '-SettingsPath', $s)
    $j = Read-Settings $s
    $allow = @($j.permissions.allow); $ask = @($j.permissions.ask)
    Assert-True 'noask: exit 0' ($r.Code -eq 0) $r.Out
    Assert-True 'noask: removes only Rhino ask rules' (($ask -join '|') -eq 'Bash(git push:*)') ($ask -join ', ')
    Assert-True 'noask: adds both server allow rules, keeps user allow' (($allow -contains 'Bash(git status)') -and ($allow -contains 'mcp__plugin_rhino-mcp_rhino') -and ($allow -contains 'mcp__rhino')) ($allow -join ', ')
    Assert-True 'noask: other keys unchanged' ($j.model -eq 'example-model' -and $j.env.EXAMPLE -eq '1' -and (@($j.permissions.deny) -join '|') -eq 'Bash(rm -rf:*)') ($j | ConvertTo-Json -Depth 10)
    Assert-True 'noask: backup written' (@(Get-ChildItem (Split-Path $s) -Filter '*.am-rhino-backup-*').Count -eq 1) $r.Out

    # 4. Default run in do-not-ask mode: the ask rules stay out.
    $r = Invoke-Setup $h @('-SettingsPath', $s) -WithNode
    $ask = @((Read-Settings $s).permissions.ask)
    Assert-True 'default run: respects do-not-ask mode' ((($ask -join '|') -eq 'Bash(git push:*)') -and $r.Out -match 'Ask rules skipped' -and $r.Out -match 'Do-not-ask mode: on') $r.Out

    # 5. Explicit -AskRules turns asking back on.
    $r = Invoke-Setup $h @('-AskRules', '-SettingsPath', $s)
    $j = Read-Settings $s
    $allow = @($j.permissions.allow); $ask = @($j.permissions.ask)
    Assert-True 'explicit askrules: asks again' ($r.Code -eq 0 -and $ask.Count -eq 57 -and $ask[0] -eq 'Bash(git push:*)') $r.Out
    Assert-True 'explicit askrules: removes server allow rules, keeps user allow' ((($allow -join '|') -eq 'Bash(git status)')) ($allow -join ', ')

    # 6. -Verify on a machine that is not set up (no node, no plugin, no package, no ask rules).
    $h = New-Home 'six' -FakeRhino
    $s = Join-Path $h 'claude\settings.json'
    [IO.File]::WriteAllText($s, '{"permissions":{"allow":[]}}', (New-Object Text.UTF8Encoding $false))
    $r = Invoke-Setup $h @('-Verify', '-SettingsPath', $s)
    $lines = @(($r.Out -split "`r?`n") | Where-Object { $_.Trim() })
    $warnings = @($lines | Where-Object { $_ -match 'WARNING:' })
    Assert-True 'verify: exit 6' ($r.Code -eq 6) $r.Out
    Assert-True 'verify: missing rules as count and first names' ($r.Out -match '56 of 56 ask rules missing \(.+ and 53 more\)' -and $r.Out -notmatch [regex]::Escape($FourthName)) $r.Out
    Assert-True 'verify: each problem named once (warning lines = problem count)' ($r.Out -match "Finished with $($warnings.Count) problem\(s\), one per WARNING line above" -and @($lines | Where-Object { $_ -match 'ask rules missing' }).Count -eq 1) $r.Out
    Assert-True 'verify: under 25 lines' ($lines.Count -lt 25) "$($lines.Count) lines`n$($r.Out)"
    Assert-True 'verify: RhinoCode fallback hint' ($r.Out -match 'RhinoCode fallback \(info\)') $r.Out
    $full = [regex]::Match($r.Out, 'Full list: (\S+ask-rules-missing\.txt)').Groups[1].Value
    Assert-True 'verify: full list in the log folder' ($full -and (Test-Path $full) -and @([IO.File]::ReadAllLines($full) | Where-Object { $_ }).Count -eq 56) "file: $full"
    Assert-True 'verify: settings not changed' ([IO.File]::ReadAllText($s) -eq '{"permissions":{"allow":[]}}') ([IO.File]::ReadAllText($s))

    # 7. Conflicting switches and invalid JSON.
    $r = Invoke-Setup $h @('-NoAsk', '-AskRules', '-SettingsPath', $s)
    Assert-True 'noask + askrules: exit 5' ($r.Code -eq 5) $r.Out
    [IO.File]::WriteAllText($s, '{ not json', (New-Object Text.UTF8Encoding $false))
    $r = Invoke-Setup $h @('-NoAsk', '-SettingsPath', $s)
    Assert-True 'noask on invalid JSON: exit 5, file unchanged' ($r.Code -eq 5 -and [IO.File]::ReadAllText($s) -eq '{ not json') $r.Out
} finally {
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $Root
}

Write-Host ''
Write-Host "$($script:Asserts - $script:Fail) of $($script:Asserts) checks passed."
if ($script:Fail -gt 0) { exit 1 }
exit 0
