<#
.SYNOPSIS
  archi-magic doctor: check pyRevit 7, the Revit manifests, the pyRevit Agent Runtime, the
  Claude Code MCP registration and the last 14 days of archi-magic logs. Read-only by default.

.DESCRIPTION
  Prints one line per check:  CODE | severity | evidence | fix
    severity: ok, info, warn, error, unchecked
  'unchecked' means the doctor could not check that part. It is never shown as healthy.

  Modes:
    (default)      Diagnose. Changes nothing.
    -Fix <CODE>    Run ONLY the fix command mapped to CODE. Ask the user first.
                   Never elevates, never stops Revit.
    -Report        Diagnose, then print a GitHub issue draft (paths redacted).
    -Report -OpenIssue  Also copy the body to the clipboard and open the new-issue page
                   (title only in the URL). Use only after the user says yes.

  Exit codes (diagnose / -Report):
    0 healthy, 1 problems found (warn or error), 2 could not check something and no problem found.
  Exit codes (-Fix): the fix command's exit code; 4 = no automatic fix (user action needed);
    2 = fix command not available.

  Test parameters (-AddinsRoot, -UserAddinsRoot, -RevitInstallRoot, -Py7Root, -Py7Exe, -LogDir,
  -SetupLogRoot, -McpName, -InstalledPluginsJson, -ClaudeSettingsJson, -KnownMarketplacesJson,
  -PersonalSkillsDir, -DoctorBackupRoot, -GitExe, -GhExe, -PythonExe, -PyExe, -Only) point the checks
  at other folders or programs.
  -Py7Exe replaces <Py7Root>\bin\pyrevit.exe (for example with a test stub), as in pyrevit7-setup.ps1.
  Defaults are the real locations. The deps check only reads those two JSON files (no network).
  The Claude Code files follow CLAUDE_CONFIG_DIR when it is set, else %USERPROFILE%\.claude.

  Groups for -Only: py7, repoint, install, agent, revit, mcp, docs, deps,
    config (Claude Code JSON files with a byte-order mark or bad JSON, personal skill copies that
            shadow a plugin skill; issue #253),
    tools  (git, gh sign-in, a real Python and not only the Microsoft Store alias; issue #254).
  -GitExe / -GhExe / -PythonExe / -PyExe: a path to use instead of the PATH search; 'none' = not
    installed. -PyExe is the Python Launcher (run as 'py -3'). With -PythonExe or -PyExe, the doctor
    tests only those two and does not look for pyRevit's own CPython.
  -Fix CONFIG_BOM copies each Claude Code JSON file that starts with a byte-order mark to
    %LOCALAPPDATA%\archi-magic\backups\am-doctor\<timestamp>\ (-DoctorBackupRoot), then writes it
    again as UTF-8 without a byte-order mark. Ask the user first.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File .\doctor.ps1
  powershell -NoProfile -ExecutionPolicy Bypass -File .\doctor.ps1 -Fix REPOINT_NEEDED
  powershell -NoProfile -ExecutionPolicy Bypass -File .\doctor.ps1 -Report
#>
[CmdletBinding()]
param(
    [ValidateSet('', 'PY7_MISSING', 'REPOINT_NEEDED', 'YEAR_NOT_ATTACHED', 'INSTALL_CRASH', 'AGENT_DISABLED', 'AGENT_NOT_RESPONDING',
        'AGENT_BLOCKED_DIALOG', 'AGENT_BUSY_SESSION', 'NO_MODEL_OPEN', 'MCP_MISSING', 'MCP_WRONG_PATH', 'POLICY_AUTO', 'DOCS_OFFLINE', 'DEPS_MISSING', 'HEADLESS_OLD_PYREVIT',
        'ADDIN_NAME_CLASH', 'ADDIN_DUPLICATE', 'ADDIN_OTHER_ID', 'ADDIN_SHARED_ID',
        'CONFIG_BOM', 'CONFIG_BAD_JSON', 'SKILL_SHADOW', 'GIT_MISSING', 'GH_MISSING', 'GH_SIGNED_OUT', 'PYTHON_MISSING', 'PYTHON_PYREVIT_ONLY', 'PYTEST_MISSING')]
    [string]$Fix = '',
    [switch]$Report,
    [switch]$OpenIssue,
    [int]$Days = 14,
    [string]$AddinsRoot = 'C:\ProgramData\Autodesk\Revit\Addins',
    [string]$UserAddinsRoot = (Join-Path $env:APPDATA 'Autodesk\Revit\Addins'),
    [string]$RevitInstallRoot = 'C:\Program Files\Autodesk',
    [string]$Py7Root = (Join-Path $env:APPDATA 'pyRevit-Master'),
    [string]$LogDir = '',
    [string]$SetupLogRoot = (Join-Path $env:TEMP 'pyrevit7-setup'),
    [string]$McpName = 'pyrevit',
    # Test parameters: another Claude Code CLI, pyrevit.exe and setup script. Defaults are the real ones.
    [string]$ClaudeCli = '',
    [string]$Py7Exe = '',
    [string]$SetupScript = '',
    # Claude Code's config folder: CLAUDE_CONFIG_DIR when set, else %USERPROFILE%\.claude.
    [string]$InstalledPluginsJson = (Join-Path $(if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR } else { Join-Path $env:USERPROFILE '.claude' }) 'plugins\installed_plugins.json'),
    [string]$ClaudeSettingsJson = (Join-Path $(if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR } else { Join-Path $env:USERPROFILE '.claude' }) 'settings.json'),
    [string]$KnownMarketplacesJson = (Join-Path $(if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR } else { Join-Path $env:USERPROFILE '.claude' }) 'plugins\known_marketplaces.json'),
    [string]$PersonalSkillsDir = (Join-Path $(if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR } else { Join-Path $env:USERPROFILE '.claude' }) 'skills'),
    [string]$DoctorBackupRoot = $(if ($env:LOCALAPPDATA) { Join-Path $env:LOCALAPPDATA 'archi-magic\backups\am-doctor' } else { '' }),
    # Test parameters for the tools group: a path instead of the PATH search, or 'none' for "not installed".
    [string]$GitExe = '',
    [string]$GhExe = '',
    [string]$PythonExe = '',
    # The Python Launcher (py.exe). With -PythonExe or -PyExe, the doctor uses only these two (no PATH search).
    [string]$PyExe = '',
    [int]$BusyWaitSec = 25,
    # Pass -RevitWasRunning when Revit answered earlier in this session (a get_context or run
    # succeeded) or the user says Revit just closed. Without it, "no Revit process" is only an
    # info line: the doctor cannot tell a Revit that never started from one that just closed.
    [switch]$RevitWasRunning,
    [string[]]$Only = @()
)
# -Only accepts 'a,b' as one string too (powershell -File passes it that way).
$Only = @($Only | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim().ToLowerInvariant() } | Where-Object { $_ })
$badOnly = @($Only | Where-Object { $_ -notin 'py7', 'repoint', 'install', 'agent', 'revit', 'mcp', 'docs', 'deps', 'config', 'tools' })
if ($badOnly.Count) { Write-Host "[am-doctor] Unknown -Only value(s): $($badOnly -join ', '). Use: py7, repoint, install, agent, revit, mcp, docs, deps, config, tools."; exit 2 }
$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'

$IssuesUrl = 'https://github.com/zsenarchitect/archi-magic/issues'
$PluginRoot = Join-Path $PSScriptRoot '..\..\..'
if (-not $SetupScript) { $SetupScript = Join-Path $PSScriptRoot '..\..\am-pyrevit7-setup\scripts\pyrevit7-setup.ps1' }
$Py7 = if ($Py7Exe) { $Py7Exe } else { Join-Path $Py7Root 'bin\pyrevit.exe' }
$PolicySwitch = 'Change it only when the user asks: pyrevit configs agent policy readonly|ask|auto (get_context shows the new policy at once, no Revit restart)'

# One spelling per file: 'C:\x\Roaming/pyRevit-Master/bin/pyrevit.exe' equals the backslash form.
# GetFullPath throws on an empty or odd string: keep it raw then.
function Get-NormalPath([string]$path) {
    $p = ([string]$path).Trim().Trim('"')
    try { return [IO.Path]::GetFullPath($p) } catch { return $p }
}

$amLogPath = Join-Path $PluginRoot 'scripts\lib\am-log.ps1'
$script:AmLogLoaded = $false
if (Test-Path -LiteralPath $amLogPath) {
    try { . $amLogPath; $script:AmLogLoaded = $true } catch { Write-Warning "archi-magic log helper not loaded: $($_.Exception.Message)" }
}
if (-not $script:AmLogLoaded) {
    function Write-AmLog { }
    function ConvertTo-AmRedacted([string]$Text) { return $Text }
    function Get-AmLogDir {
        if ($env:ARCHI_MAGIC_LOG_DIR) { return $env:ARCHI_MAGIC_LOG_DIR }
        return (Join-Path $env:LOCALAPPDATA 'archi-magic\logs')
    }
}
if (-not $LogDir) { $LogDir = Get-AmLogDir }
$amPluginsPath = Join-Path $PluginRoot 'scripts\lib\am-plugins.ps1'
$script:AmPluginsLoaded = $false
if (Test-Path -LiteralPath $amPluginsPath) {
    try { . $amPluginsPath; $script:AmPluginsLoaded = $true } catch { Write-Warning "archi-magic plugin reader not loaded: $($_.Exception.Message)" }
}

$script:Findings = New-Object System.Collections.Generic.List[object]
function Add-Finding($code, $severity, $evidence, $fixText) {
    $script:Findings.Add([pscustomobject]@{ Index = $script:Findings.Count; Code = $code; Severity = $severity; Evidence = ([string]$evidence).Trim().TrimEnd('.'); Fix = $fixText })
}
function Test-Want($group) { return ($Only.Count -eq 0 -or $Only -contains $group) }

# Run a native exe with a timeout. Returns @{ Ok; ExitCode; Out; TimedOut; Error }.
function Invoke-Native([string]$exe, [string[]]$argv, [int]$timeoutSec = 60) {
    $r = @{ Ok = $false; ExitCode = $null; Out = ''; TimedOut = $false; Error = '' }
    try {
        $psi = New-Object Diagnostics.ProcessStartInfo
        $psi.FileName = $exe
        $psi.Arguments = (($argv | ForEach-Object { if ($_ -match '[\s"]') { '"' + ($_ -replace '"', '\"') + '"' } else { $_ } }) -join ' ')
        $psi.UseShellExecute = $false
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $psi.CreateNoWindow = $true
        $psi.StandardOutputEncoding = [Text.Encoding]::UTF8
        $psi.StandardErrorEncoding = [Text.Encoding]::UTF8
        $p = [Diagnostics.Process]::Start($psi)
        $o = $p.StandardOutput.ReadToEndAsync()
        $e = $p.StandardError.ReadToEndAsync()
        if (-not $p.WaitForExit($timeoutSec * 1000)) {
            try { $p.Kill() } catch { }
            $r.TimedOut = $true; $r.Error = "timed out after $timeoutSec s"
            return $r
        }
        $p.WaitForExit()
        $r.ExitCode = $p.ExitCode
        $r.Out = ($o.Result + "`n" + $e.Result).Trim()
        $r.Ok = $true
    } catch { $r.Error = $_.Exception.Message }
    return $r
}

function Find-ClaudeCli {
    # Same search as pyrevit7-setup.ps1. The Claude desktop app's own exe (AnthropicClaude\claude.exe) is NOT the CLI.
    if ($ClaudeCli) { if (Test-Path -LiteralPath $ClaudeCli) { return $ClaudeCli } else { return $null } }
    $cmd =Get-Command claude -ErrorAction SilentlyContinue |
        Where-Object { $_.Source -notmatch 'AnthropicClaude' } | Select-Object -First 1
    if ($cmd) { return $cmd.Source }
    foreach ($c in @("$env:USERPROFILE\.local\bin\claude.exe", "$env:APPDATA\npm\claude.cmd")) {
        if (Test-Path $c) { return $c }
    }
    $bundled = Get-ChildItem (Join-Path $env:APPDATA 'Claude\claude-code') -Recurse -Filter claude.exe -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($bundled) { return $bundled.FullName }
    return $null
}

function Get-Py7FileVersion {
    if (-not (Test-Path -LiteralPath $Py7)) { return $null }
    try {
        $fv = [string](Get-Item -LiteralPath $Py7).VersionInfo.FileVersion
        $m = [regex]::Match($fv, '^(\d+)\.(\d+)\.(\d+)')
        if ($m.Success) { return [version]$m.Value }
    } catch { }
    return [version]'0.0.0'
}

function Get-LogEntries {
    $list = New-Object System.Collections.Generic.List[object]
    $since = (Get-Date).ToUniversalTime().AddDays(-$Days)
    if (-not (Test-Path -LiteralPath $LogDir)) { return , $list }
    foreach ($f in (Get-ChildItem -LiteralPath $LogDir -Filter '*.jsonl' -ErrorAction SilentlyContinue | Sort-Object Name)) {
        foreach ($line in [IO.File]::ReadAllLines($f.FullName)) {
            if (-not $line.Trim()) { continue }
            try {
                $o = $line | ConvertFrom-Json
                # PS 7 turns ISO strings into DateTime; PS 5.1 keeps strings. Handle both.
                $ts = if ($o.ts -is [datetime]) { $o.ts.ToUniversalTime() } else {
                    [datetime]::Parse([string]$o.ts, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::AdjustToUniversal -bor [Globalization.DateTimeStyles]::AssumeUniversal)
                }
                if ($ts -ge $since) { $list.Add([pscustomobject]@{ Ts = $ts; Entry = $o; Raw = $line }) }
            } catch { }
        }
    }
    return , $list
}

function Redact([string]$s) { return (ConvertTo-AmRedacted $s) }

# Installed Revit years: the "Revit 20*" folders under the Revit install root (the same
# source -Report uses below). -RevitInstallRoot is the test seam: tests point it at a
# temp folder. A Revit installed to a custom path outside it is a known gap, not a finding.
function Get-InstalledRevitYears([string]$root) {
    $years = @()
    foreach ($d in @(Get-ChildItem -LiteralPath $root -Directory -Filter 'Revit 20*' -ErrorAction SilentlyContinue)) {
        $y = ($d.Name -replace '\D', '')
        if ($y -match '^\d{4}$') { $years += $y }
    }
    return @($years | Sort-Object -Unique)
}

# Plugin dependencies (read-only, offline): scripts\lib\am-plugins.ps1 walks the dependency tree of
# every installed archi-magic* plugin. A plugin counts as missing ("failed to load") when its entry is
# absent, or its installPath or installPath\.claude-plugin\plugin.json is missing on disk.
# Returns @{ State = ok|missing|unchecked|notinstalled|disabled; Missing = @('<name>@<market>'); Evidence }.
function Get-DepsState {
    if (-not $script:AmPluginsLoaded) { return @{ State = 'unchecked'; Missing = @(); Evidence = 'scripts\lib\am-plugins.ps1 not found in this copy' } }
    $r = Get-AmDepsState -InstalledPluginsJson $InstalledPluginsJson -SettingsJson $ClaudeSettingsJson -FallbackManifest (Join-Path $PluginRoot '.claude-plugin\plugin.json')
    $r.Evidence = Redact $r.Evidence
    return $r
}

# One field of a Revit add-in manifest (Assembly, AddInId, Name). AddInId comes back upper case, no braces.
function Get-AddinField([string]$path, [string]$tag) {
    try { $xml = [IO.File]::ReadAllText($path) } catch { return '' }
    $v = [regex]::Match($xml, "<$tag>\s*(.*?)\s*</$tag>").Groups[1].Value
    if ($tag -eq 'AddInId') { $v = $v.Trim('{', '}', ' ').ToUpperInvariant() }
    return $v
}

# ------------------------------------------------------------------ Claude Code JSON files (issue #253)
# Windows PowerShell 5.1 'Set-Content -Encoding utf8' and 'Out-File' write a byte-order mark (BOM).
# Claude Code then cannot parse the file ("JSON Parse error: Unrecognized token"), and no plugin loads,
# so no archi-magic hook can warn. [IO.File]::ReadAllText and ConvertFrom-Json skip a BOM: read the bytes.
function Get-ClaudeJsonFiles {
    return @(
        @{ Label = 'known_marketplaces.json'; Path = $KnownMarketplacesJson },
        @{ Label = 'installed_plugins.json'; Path = $InstalledPluginsJson },
        @{ Label = 'settings.json'; Path = $ClaudeSettingsJson }
    )
}
# Returns @{ Exists; Bom = '' | 'UTF-8' | 'UTF-16 LE' | 'UTF-16 BE'; Text (no BOM); DecodeError; ParseError }.
# ParseError never quotes the file text: settings.json can hold tokens, and -Report copies findings.
function Read-JsonFileState([string]$path) {
    $r = @{ Exists = $false; Bom = ''; Text = $null; DecodeError = ''; ParseError = '' }
    if (-not $path -or -not (Test-Path -LiteralPath $path -PathType Leaf)) { return $r }
    $r.Exists = $true
    $b = [IO.File]::ReadAllBytes($path)
    $skip = 0
    $enc = New-Object Text.UTF8Encoding($false, $true)   # strict: invalid bytes throw
    if ($b.Length -ge 3 -and $b[0] -eq 0xEF -and $b[1] -eq 0xBB -and $b[2] -eq 0xBF) { $r.Bom = 'UTF-8'; $skip = 3 }
    elseif ($b.Length -ge 2 -and $b[0] -eq 0xFF -and $b[1] -eq 0xFE) { $r.Bom = 'UTF-16 LE'; $skip = 2; $enc = New-Object Text.UnicodeEncoding($false, $false, $true) }
    elseif ($b.Length -ge 2 -and $b[0] -eq 0xFE -and $b[1] -eq 0xFF) { $r.Bom = 'UTF-16 BE'; $skip = 2; $enc = New-Object Text.UnicodeEncoding($true, $false, $true) }
    try { $t = $enc.GetString($b, $skip, $b.Length - $skip) } catch { $r.DecodeError = 'the bytes are not valid ' + $(if ($r.Bom) { $r.Bom } else { 'UTF-8' }); return $r }
    # Two edits in a row can leave a second BOM, now as the first character.
    $t = $t.TrimStart([char]0xFEFF)
    $r.Text = $t
    if (-not $t.Trim()) { $r.ParseError = 'the file is empty'; return $r }
    try { $null = ConvertFrom-Json -InputObject $t -ErrorAction Stop } catch { $r.ParseError = 'not valid JSON' }
    return $r
}

# Skill names in a skills folder: each sub-folder with a SKILL.md, by folder name and by its frontmatter name.
function Get-SkillNames([string]$skillsDir) {
    $names = @()
    if (-not $skillsDir -or -not (Test-Path -LiteralPath $skillsDir)) { return $names }
    foreach ($d in @(Get-ChildItem -LiteralPath $skillsDir -Directory -ErrorAction SilentlyContinue)) {
        $md = Join-Path $d.FullName 'SKILL.md'
        if (-not (Test-Path -LiteralPath $md)) { continue }
        $names += $d.Name
        try {
            $m = [regex]::Match([IO.File]::ReadAllText($md), '(?m)\A---\s*\r?\n(?:.*\r?\n)*?name:\s*["'']?([^"''\r\n]+?)["'']?\s*\r?\n')
            if ($m.Success -and $m.Groups[1].Value -ne $d.Name) { $names += $m.Groups[1].Value }
        } catch { }
    }
    return @($names | Select-Object -Unique)
}

# ------------------------------------------------------------------ tools (issue #254)
# Returns @{ Path; OnPath } or $null. $override: a path (tests), or 'none' = not installed.
function Resolve-Tool([string]$override, [string[]]$names, [string[]]$extraPaths) {
    if ($override -eq 'none') { return $null }
    if ($override) { if (Test-Path -LiteralPath $override) { return @{ Path = $override; OnPath = $true } } else { return $null } }
    foreach ($n in $names) {
        $c = Get-Command $n -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($c) { return @{ Path = $c.Source; OnPath = $true } }
    }
    foreach ($p in $extraPaths) { if ($p -and (Test-Path -LiteralPath $p)) { return @{ Path = $p; OnPath = $false } } }
    return $null
}
# Runs one Python candidate. Returns @{ Version ('3.12' or ''); StoreAlias; Out }.
# -I (isolated) keeps the current folder and user site folders out of sys.path.
function Test-PythonCandidate([string]$exe, [string[]]$pre) {
    $code = "import sys;print('AMPY%d.%d' % sys.version_info[:2])"
    $r = Invoke-Native $exe (@($pre) + @('-I', '-c', $code)) 30
    $out = [string]$r.Out
    $ver = ''
    $m = [regex]::Match($out, 'AMPY(\d+)\.(\d+)')
    if ($r.Ok -and $m.Success -and [int]$m.Groups[1].Value -ge 3) { $ver = "$($m.Groups[1].Value).$($m.Groups[2].Value)" }
    # The Microsoft Store alias (%LOCALAPPDATA%\Microsoft\WindowsApps\python.exe) prints "Python was not
    # found; run without arguments to install from the Microsoft Store ..." and runs no Python.
    $alias = (-not $ver) -and (($out -match 'Microsoft Store|App execution aliases') -or ($exe -match '\\Microsoft\\WindowsApps\\'))
    return @{ Version = $ver; StoreAlias = $alias; Out = $out }
}
# pyRevit ships its own CPython in <pyRevit>\bin\cengines\CPY*\. It has no pip and no pytest, so it
# cannot run the skill tests or install a package (issue #254 comment 1).
function Test-PyRevitPython([string]$exe) { return ([string]$exe -match '(?i)[\\/]bin[\\/]cengines[\\/]') }
# Checks pip and pytest for a working Python. -I would hide a 'pip install --user' pytest, so this
# run keeps the user site folder, ignores PYTHON* variables (-E) and drops the current folder from
# sys.path before it imports anything. Returns @{ Known; Pip; Pytest }.
function Test-PythonModules([string]$exe, [string[]]$pre) {
    $code = "import sys;sys.path[:]=[p for p in sys.path if p not in ('','.')];import importlib.util as u;print('AMMOD pip=%d pytest=%d' % tuple(int(u.find_spec(m) is not None) for m in ('pip','pytest')))"
    $r = Invoke-Native $exe (@($pre) + @('-E', '-c', $code)) 60
    $m = [regex]::Match([string]$r.Out, 'AMMOD pip=([01]) pytest=([01])')
    if (-not ($r.Ok -and $m.Success)) { return @{ Known = $false; Pip = $false; Pytest = $false } }
    return @{ Known = $true; Pip = ($m.Groups[1].Value -eq '1'); Pytest = ($m.Groups[2].Value -eq '1') }
}

$FixMap = @{
    PY7_MISSING          = @{ Kind = 'setup'; Args = @('-Install', '-Repoint', '-EnableAgent', '-Verify'); Text = 'am-pyrevit7-setup: run setup (Install, Repoint, EnableAgent, Verify).' }
    REPOINT_NEEDED       = @{ Kind = 'setup'; Args = @('-Repoint', '-Verify'); Text = 'am-pyrevit7-setup -Repoint -Verify' }
    YEAR_NOT_ATTACHED    = @{ Kind = 'setup'; Args = @('-Repoint', '-Verify'); Text = 'am-pyrevit7-setup -Repoint -Verify' }
    INSTALL_CRASH        = @{ Kind = 'setup'; Args = @('-Install', '-Force'); Text = 'am-pyrevit7-setup -Install -Force (the script no longer passes /LOG). Close Revit first.' }
    AGENT_DISABLED       = @{ Kind = 'setup'; Args = @('-EnableAgent'); Text = 'am-pyrevit7-setup -EnableAgent' }
    MCP_MISSING          = @{ Kind = 'setup'; Args = @('-EnableAgent'); Text = 'am-pyrevit7-setup -EnableAgent, then start a new Claude Code session.' }
    MCP_WRONG_PATH       = @{ Kind = 'setup'; Args = @('-EnableAgent'); Text = 'am-pyrevit7-setup -EnableAgent, then start a new Claude Code session.' }
    POLICY_AUTO          = @{ Kind = 'py7'; Args = @('configs', 'agent', 'policy', 'ask'); Text = 'pyrevit configs agent policy ask' }
    AGENT_NOT_RESPONDING = @{ Kind = 'manual'; Text = 'If a REPOINT_NEEDED or ADDIN_* line shows, Revit probably loaded the old pyRevit or none: fix that first, then restart Revit. Else, User: in Revit, click pyRevit tab > Reload (it can need two reloads), or restart Revit. Then run the doctor again.' }
    AGENT_BLOCKED_DIALOG = @{ Kind = 'manual'; Text = 'User: close the dialog in Revit, then run the doctor again.' }
    AGENT_BUSY_SESSION   = @{ Kind = 'manual'; Text = 'User: wait for the other session to finish, then run the doctor again. Run Revit-touching sessions in sequence.' }
    NO_MODEL_OPEN        = @{ Kind = 'manual'; Text = 'User: open a model in Revit (an Autodesk sample such as Snowdon Towers is fine).' }
    DOCS_OFFLINE         = @{ Kind = 'manual'; Text = 'No action needed. The cached docs copy is in use. Check the network later.' }
    HEADLESS_OLD_PYREVIT = @{ Kind = 'manual'; Text = 'Interactive Revit and the archi-magic skills are not affected. For your own pyrevit run scripts (batch, CI): ask IT to uninstall the machine-wide pyRevit in Program Files.' }
    DEPS_MISSING         = @{ Kind = 'deps'; Text = 'claude plugin install <name>@archi-magic for each missing one named in the evidence (in a session: /reload-plugins first, it installs missing dependencies), then quit and restart the Claude app.' }
    # Issue #258: the per-user manifest needs its own file name and the machine-wide AddInId.
    ADDIN_NAME_CLASH     = @{ Kind = 'setup'; Args = @('-Repoint', '-Verify'); Text = 'am-pyrevit7-setup -Repoint -Verify (writes pyRevit7.addin and removes the clashing per-user manifest; the undo journal keeps a copy), then restart Revit.' }
    ADDIN_DUPLICATE      = @{ Kind = 'setup'; Args = @('-Repoint', '-Verify'); Text = 'am-pyrevit7-setup -Repoint -Verify (keeps one per-user pyRevit manifest; the undo journal keeps a copy of the others), then restart Revit.' }
    ADDIN_OTHER_ID       = @{ Kind = 'setup'; Args = @('-Repoint', '-Verify'); Text = 'am-pyrevit7-setup -Repoint -Verify (copies the machine-wide AddInId, so Revit loads only one pyRevit), then restart Revit.' }
    ADDIN_SHARED_ID      = @{ Kind = 'manual'; Text = 'Expected. Close the "Duplicated AddInId" dialog at Revit start. pyRevit > About must show 7.x. If it shows 6.x or no pyRevit tab shows, run the doctor again and send IT-REQUEST.md (am-pyrevit7-setup) to IT.' }
    # Issue #253: Claude Code JSON files and personal skill copies.
    CONFIG_BOM           = @{ Kind = 'config'; Text = 'doctor -Fix CONFIG_BOM (copies the file to a backup folder, then writes it again as UTF-8 without the byte-order mark). Then type /reload-plugins or restart the Claude app.' }
    CONFIG_BAD_JSON      = @{ Kind = 'manual'; Text = 'Do not edit files under ~/.claude/plugins by hand. For a plugins file: rename it (keep it as a backup), then add the marketplace again with /plugin marketplace add zsenarchitect/archi-magic. For settings.json: show the user the file and fix the JSON with their approval. To see the error: Get-Content -Raw <file> | ConvertFrom-Json' }
    SKILL_SHADOW         = @{ Kind = 'manual'; Text = 'Ask the user. Move each copy out of the personal skills folder (keep it as a backup) unless they edit it on purpose, then type /reload-plugins. The plugin copy updates; a personal copy does not.' }
    # Issue #254: programs that the install steps and some skills need.
    GIT_MISSING          = @{ Kind = 'manual'; Text = 'Install git for your user (no admin): winget install --id Git.Git --exact --scope user. Then restart the Claude app so the new PATH loads. Claude Code needs git to add and update the archi-magic marketplace.' }
    GH_MISSING           = @{ Kind = 'manual'; Text = 'Optional: only am-feedback and doctor -Report -OpenIssue post to GitHub, and without gh they show the text to paste. Install: winget install --id GitHub.cli --exact. Then sign in (GH_SIGNED_OUT).' }
    GH_SIGNED_OUT        = @{ Kind = 'manual'; Text = 'The user signs in: gh auth login. In PowerShell with a full path, put & first: & "<path>\gh.exe" auth login. Never type a token for the user.' }
    PYTHON_MISSING       = @{ Kind = 'manual'; Text = 'Install Python 3 for your user (no admin): winget install --id Python.Python.3.12 --exact --scope user. If python still prints the Microsoft Store message, the user turns off the python.exe and python3.exe App execution aliases (Windows Settings > Apps > Advanced app settings). Until then, use the no-Python path of a skill, or skip its Python step.' }
    PYTHON_PYREVIT_ONLY  = @{ Kind = 'manual'; Text = 'Install Python 3 for your user (no admin): winget install --id Python.Python.3.12 --exact --scope user. Then call it by its full path (for example %LOCALAPPDATA%\Programs\Python\Python312\python.exe) or start a new session. Do not use the CPython in the pyRevit folder for skill scripts or tests: it has no pip and no pytest, and you cannot add packages to it.' }
    PYTEST_MISSING       = @{ Kind = 'manual'; Text = 'Only for contributors who run the skill tests. Install pytest for your user: <python> -m pip install --user pytest (for example py -3 -m pip install --user pytest). Then run <python> -m pytest -q tests in the skill folder. A warning that the Scripts folder is not on PATH is fine.' }
}

# ------------------------------------------------------------------ -Fix CONFIG_BOM
if ($Fix -eq 'CONFIG_BOM') {
    $targets = @(foreach ($cf in (Get-ClaudeJsonFiles)) {
        $s = Read-JsonFileState $cf.Path
        if ($s.Exists -and $s.Bom) { @{ Label = $cf.Label; Path = $cf.Path; State = $s } }
    })
    if ($targets.Count -eq 0) { Write-Host '[am-doctor] Nothing to fix: no Claude Code JSON file starts with a byte-order mark.'; exit 0 }
    if (-not $DoctorBackupRoot) { Write-Host '[am-doctor] Fix not available: no backup folder (LOCALAPPDATA is not set).'; exit 2 }
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $bdir = Join-Path $DoctorBackupRoot $stamp; $n = 2
    while (Test-Path -LiteralPath $bdir) { $bdir = Join-Path $DoctorBackupRoot "$stamp-$n"; $n++ }
    New-Item -ItemType Directory -Force $bdir | Out-Null
    Write-AmLog -Skill 'am-doctor' -Step 'fix' -Level 'info' -Message "Fix CONFIG_BOM started: $($targets.Count) file(s)."
    $code = 0
    foreach ($t in $targets) {
        $s = $t.State
        $shown = Redact $t.Path
        if ($s.DecodeError) { Write-Host "[am-doctor] ${shown}: not changed, $($s.DecodeError)."; $code = 1; continue }
        $bak = Join-Path $bdir $t.Label
        Copy-Item -LiteralPath $t.Path -Destination $bak -Force
        if ((Get-FileHash -LiteralPath $bak -Algorithm SHA256).Hash -ne (Get-FileHash -LiteralPath $t.Path -Algorithm SHA256).Hash) {
            Write-Host "[am-doctor] ${shown}: not changed, the backup copy does not match."; $code = 1; continue
        }
        try { [IO.File]::WriteAllText($t.Path, $s.Text, (New-Object Text.UTF8Encoding $false)) }
        catch { Write-Host "[am-doctor] ${shown}: could not write it ($($_.Exception.Message)). The backup is $(Redact $bak)."; $code = 1; continue }
        $after = Read-JsonFileState $t.Path
        if ($after.Bom) { Write-Host "[am-doctor] ${shown}: the byte-order mark is still there."; $code = 1 }
        elseif ($after.ParseError) { Write-Host "[am-doctor] ${shown}: removed the $($s.Bom) byte-order mark, but the file is still not valid JSON (CONFIG_BAD_JSON)."; $code = 1 }
        else { Write-Host "[am-doctor] ${shown}: removed the $($s.Bom) byte-order mark. The file is valid JSON now." }
    }
    Write-Host "[am-doctor] Backup folder: $(Redact $bdir)"
    Write-Host '[am-doctor] Next: type /reload-plugins in Claude Code, or restart the Claude app. Then run the doctor again.'
    $lvl = if ($code -eq 0) { 'info' } else { 'error' }
    Write-AmLog -Skill 'am-doctor' -Step 'fix' -Level $lvl -Message 'Fix CONFIG_BOM finished.' -ExitCode $code
    exit $code
}

if ($Fix -eq 'DEPS_MISSING') {
    $d = Get-DepsState
    if ($d.State -ne 'missing') { Write-Host "[am-doctor] Nothing to fix: $($d.Evidence)."; exit 0 }
    $claude = Find-ClaudeCli
    if (-not $claude) { Write-Host '[am-doctor] Fix not available: Claude Code CLI not found (AnthropicClaude\claude.exe is not the CLI).'; exit 2 }
    $code = 0
    foreach ($key in $d.Missing) {
        Write-Host "[am-doctor] Running fix for DEPS_MISSING: claude plugin install $key"
        Write-AmLog -Skill 'am-doctor' -Step 'fix' -Level 'info' -Message "Fix DEPS_MISSING started: claude plugin install $key"
        & $claude plugin install $key
        if ($LASTEXITCODE -ne 0) { $code = $LASTEXITCODE; Write-Host "[am-doctor] claude plugin install $key exit code $LASTEXITCODE." }
    }
    $lvl = if ($code -eq 0) { 'info' } else { 'error' }
    Write-AmLog -Skill 'am-doctor' -Step 'fix' -Level $lvl -Message 'Fix DEPS_MISSING finished.' -ExitCode $code
    Write-Host "[am-doctor] Fix DEPS_MISSING exit code $code. Quit and restart the Claude app so the plugins load, then run the doctor again."
    exit $code
}

# ------------------------------------------------------------------ -Fix
if ($Fix) {
    $f = $FixMap[$Fix]
    if ($f.Kind -eq 'manual') {
        Write-Host "[am-doctor] $Fix has no automatic fix. $($f.Text)"
        exit 4
    }
    if ($f.Kind -eq 'setup') {
        if (-not (Test-Path -LiteralPath $SetupScript)) { Write-Host "[am-doctor] Fix not available: setup script not found at $(Redact $SetupScript)."; exit 2 }
        $setupFull = (Resolve-Path -LiteralPath $SetupScript).Path
        Write-Host "[am-doctor] Running fix for ${Fix}: pyrevit7-setup.ps1 $($f.Args -join ' ')"
        Write-AmLog -Skill 'am-doctor' -Step 'fix' -Level 'info' -Message "Fix $Fix started: setup $($f.Args -join ' ')"
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $setupFull @($f.Args)
        $code = $LASTEXITCODE
        if ($code -ne 0 -and ($Fix -eq 'MCP_MISSING' -or $Fix -eq 'MCP_WRONG_PATH')) {
            # Give the user the one command that registers the server by hand.
            $py7Full = Get-NormalPath $Py7
            Write-Host "[am-doctor] The setup script did not finish (exit code $code). Register the pyrevit MCP server by hand."
            if ($Fix -eq 'MCP_WRONG_PATH') { Write-Host "[am-doctor] First remove the old registration: claude mcp remove $McpName -s user" }
            Write-Host "[am-doctor] Run: claude mcp add $McpName -s user -- `"$py7Full`" mcp"
            Write-Host '[am-doctor] Then start a new Claude Code session.'
            Write-AmLog -Skill 'am-doctor' -Step 'fix' -Level 'warn' -Message "Fix $Fix setup exit $code; printed the manual claude mcp add command."
        }
    } else {
        if (-not (Test-Path -LiteralPath $Py7)) { Write-Host "[am-doctor] Fix not available: $(Redact $Py7) not found."; exit 2 }
        Write-Host "[am-doctor] Running fix for ${Fix}: pyrevit $($f.Args -join ' ')"
        Write-AmLog -Skill 'am-doctor' -Step 'fix' -Level 'info' -Message "Fix $Fix started: pyrevit $($f.Args -join ' ')"
        & $Py7 @($f.Args)
        $code = $LASTEXITCODE
    }
    $lvl = if ($code -eq 0) { 'info' } else { 'error' }
    Write-AmLog -Skill 'am-doctor' -Step 'fix' -Level $lvl -Message "Fix $Fix finished." -ExitCode $code
    Write-Host "[am-doctor] Fix $Fix exit code $code. Run the doctor again to confirm."
    exit $code
}

# ------------------------------------------------------------------ checks
$py7Ver = $null
$py7Ok = $false
$logs = Get-LogEntries

if (Test-Want 'py7') {
    try {
        $py7Ver = Get-Py7FileVersion
        if (-not $py7Ver) {
            Add-Finding 'PY7_MISSING' 'error' "$(Redact $Py7) not found" $FixMap.PY7_MISSING.Text
        } elseif ($py7Ver.Major -lt 7) {
            Add-Finding 'PY7_MISSING' 'error' "$(Redact $Py7) is version $py7Ver, not 7+" $FixMap.PY7_MISSING.Text
        } else {
            $py7Ok = $true
            Add-Finding 'PY7_INSTALLED' 'ok' "pyRevit $py7Ver at $(Redact $Py7)" '-'
        }
    } catch { Add-Finding 'PY7_INSTALLED' 'unchecked' "could not read $(Redact $Py7): $($_.Exception.Message)" 'Run: Test-Path "%APPDATA%\pyRevit-Master\bin\pyrevit.exe"' }
} elseif (Test-Path -LiteralPath $Py7) { $py7Ok = $true }

if (Test-Want 'repoint') {
    try {
        $machine = @(Get-ChildItem -Path (Join-Path $AddinsRoot '*\pyRevit*.addin') -ErrorAction SilentlyContinue)
        if ($machine.Count -eq 0) {
            Add-Finding 'REPOINT' 'ok' "no machine-wide pyRevit manifests under $(Redact $AddinsRoot); nothing to override" '-'
        }
        $userAll = @(Get-ChildItem -Path (Join-Path $UserAddinsRoot '*\pyRevit*.addin') -ErrorAction SilentlyContinue)
        foreach ($g in @($machine | Group-Object { $_.Directory.Name } | Sort-Object Name)) {
            $year = $g.Name
            $srcs = @($g.Group | Sort-Object Name)
            $src = $srcs[0]
            $srcNames = @($srcs | ForEach-Object { $_.Name })
            $user = @($userAll | Where-Object { $_.Directory.Name -eq $year } | Sort-Object Name)
            if ($user.Count -eq 0) {
                Add-Finding 'REPOINT_NEEDED' 'error' "Revit ${year}: machine manifest $($src.Name) has no per-user override in $(Redact (Join-Path $UserAddinsRoot $year))" $FixMap.REPOINT_NEEDED.Text
                continue
            }
            $good = $null; $bad = @()
            foreach ($u in $user) {
                $asm = Get-AddinField $u.FullName 'Assembly'
                if ($asm -and $asm.StartsWith($Py7Root.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase) -and (Test-Path -LiteralPath $asm)) { $good = $asm }
                else { $bad += "$($u.Name) -> $(if ($asm) { Redact $asm } else { '(no Assembly)' })" }
            }
            # Issue #258. A per-user manifest with the SAME FILE NAME as the machine-wide one: Revit logs
            # "The addin file -pyRevit.addin- in all user folder is duplicated", and Revit 2026.4 then
            # loaded no pyRevit at all. A per-user manifest with another file name and the SAME AddInId
            # loaded pyRevit 7, and Revit refused the machine-wide one ("Duplicated AddInId" dialog).
            # A DIFFERENT AddInId makes Revit load both pyRevit versions. So: own name, same AddInId.
            $yearProblem = ($user.Count -gt 1)   # ADDIN_DUPLICATE, reported below for every year
            $machineId = Get-AddinField $src.FullName 'AddInId'
            $shared = @()
            foreach ($u in $user) {
                $uid = Get-AddinField $u.FullName 'AddInId'
                $sameId = [bool]($machineId -and $uid -and $uid -eq $machineId)
                if ($srcNames -contains $u.Name) {
                    $yearProblem = $true
                    Add-Finding 'ADDIN_NAME_CLASH' 'error' "Revit ${year}: per-user $($u.Name) has the same file name$(if ($sameId) { ' and the same AddInId' }) as the machine-wide manifest. Revit logs 'The addin file -$($u.Name)- in all user folder is duplicated', and Revit 2026.4 then loaded no pyRevit" $FixMap.ADDIN_NAME_CLASH.Text
                } elseif ($machineId -and $uid -and -not $sameId) {
                    $yearProblem = $true
                    Add-Finding 'ADDIN_OTHER_ID' 'warn' "Revit ${year}: per-user $($u.Name) has another AddInId than the machine-wide $($src.Name), so Revit loads both pyRevit versions" $FixMap.ADDIN_OTHER_ID.Text
                } elseif ($sameId) { $shared += $u.Name }
            }
            if (-not $good) { Add-Finding 'REPOINT_NEEDED' 'error' "Revit ${year}: per-user manifest does not point at an existing 7.x loader ($($bad -join '; '))" $FixMap.REPOINT_NEEDED.Text }
            elseif (-not $yearProblem) {
                Add-Finding 'REPOINT' 'ok' "Revit ${year}: per-user manifest $($user[0].Name) loads $(Redact $good)" '-'
                if ($shared.Count) {
                    Add-Finding 'ADDIN_SHARED_ID' 'info' "Revit ${year}: per-user $($shared -join ', ') and the machine-wide $($src.Name) share one AddInId. Revit loads one pyRevit and shows a 'Duplicated AddInId' dialog for the other at start (expected)" $FixMap.ADDIN_SHARED_ID.Text
                }
            }
        }
        # More than one pyRevit manifest in one per-user year folder: Revit can load only one pyRevit
        # loader, and which one wins is not fixed (issue #258: pyRevit.addin + pyRevit7.addin loaded 6.4 again).
        foreach ($g in @($userAll | Group-Object { $_.Directory.Name } | Where-Object { $_.Count -gt 1 } | Sort-Object Name)) {
            $list = @($g.Group | Sort-Object Name | ForEach-Object { $a = Get-AddinField $_.FullName 'Assembly'; "$($_.Name) -> $(if ($a) { Redact $a } else { '(no Assembly)' })" })
            Add-Finding 'ADDIN_DUPLICATE' 'error' "Revit $($g.Name): $($g.Count) per-user pyRevit manifests ($($list -join '; ')). Revit loads only one pyRevit, and which one wins is not fixed" $FixMap.ADDIN_DUPLICATE.Text
        }
        # Installed Revit years with no pyRevit manifest at all (issue #88): the machine
        # loop above only visits years IT attached, so a year installed after setup (or
        # never attached) was silently skipped. A year counts as attached when either
        # a machine-wide or a per-user pyRevit manifest exists for it.
        $installedYears = Get-InstalledRevitYears $RevitInstallRoot
        if ($installedYears.Count -gt 0) {
            $manifestYears = @(@($machine | ForEach-Object { $_.Directory.Name }) +
                @(Get-ChildItem -Path (Join-Path $UserAddinsRoot '*\pyRevit*.addin') -ErrorAction SilentlyContinue | ForEach-Object { $_.Directory.Name }) |
                Sort-Object -Unique)
            foreach ($year in $installedYears) {
                if ($manifestYears -notcontains $year) {
                    Add-Finding 'YEAR_NOT_ATTACHED' 'warn' "Revit ${year}: installed but no pyRevit manifest under $(Redact $AddinsRoot) or $(Redact $UserAddinsRoot); Revit starts without pyRevit" $FixMap.YEAR_NOT_ATTACHED.Text
                }
            }
        }
        # Headless 'pyrevit run' does not read the per-user override: it uses the machine-wide (AllUsers)
        # attachment, so it loads whatever install the machine manifest names (seen live: pyRevit 6.4 in
        # Program Files while interactive Revit loaded 7.0). Info only: archi-magic uses 'pyrevit agent run'.
        $old = @{}
        foreach ($src in $machine) {
            $asm = [regex]::Match([IO.File]::ReadAllText($src.FullName), '<Assembly>\s*(.*?)\s*</Assembly>').Groups[1].Value
            if (-not $asm -or $asm.StartsWith($Py7Root.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) { continue }
            if (-not (Test-Path -LiteralPath $asm)) { continue }
            # Group by install folder (netfx and netcore loaders are one install); its pyrevit.exe has the pyRevit version.
            $i = $asm.IndexOf('\bin\', [StringComparison]::OrdinalIgnoreCase)
            $root = if ($i -gt 0) { $asm.Substring(0, $i) } else { Split-Path -Parent $asm }
            $exe = Join-Path $root 'bin\pyrevit.exe'
            $ver = if (Test-Path -LiteralPath $exe) { ([string](Get-Item -LiteralPath $exe).VersionInfo.ProductVersion) -replace '^([^+]*\+[^.]*)\..*$', '$1' } else { '' }
            $key = "$(Redact $root)$(if ($ver) { " (pyRevit $ver)" })"
            if (-not $old.ContainsKey($key)) { $old[$key] = @() }
            $old[$key] += $src.Directory.Name
        }
        foreach ($k in $old.Keys) {
            Add-Finding 'HEADLESS_OLD_PYREVIT' 'info' "pyrevit run (headless) uses the machine-wide manifest for Revit $(($old[$k] | Sort-Object) -join ', '), which loads the install at $k, not your pyRevit 7" $FixMap.HEADLESS_OLD_PYREVIT.Text
        }
    } catch { Add-Finding 'REPOINT' 'unchecked' "could not read manifests: $($_.Exception.Message)" 'Run: am-pyrevit7-setup -Verify' }
}

if (Test-Want 'install') {
    try {
        $hits = New-Object System.Collections.Generic.List[string]
        $since = (Get-Date).AddDays(-$Days)
        if (Test-Path -LiteralPath $SetupLogRoot) {
            $files = @(Get-ChildItem -LiteralPath $SetupLogRoot -Recurse -File -Filter '*.log' -ErrorAction SilentlyContinue | Where-Object { $_.LastWriteTime -ge $since })
            foreach ($f in $files) {
                $m = Select-String -LiteralPath $f.FullName -Pattern 'Access violation.*ucrtbase\.dll|Installer exit code 1\b' -List -ErrorAction SilentlyContinue
                if ($m) { $hits.Add("$(Redact $f.FullName): $($m.Line.Trim())") }
            }
        }
        foreach ($l in $logs) {
            $e = $l.Entry
            if ($e.skill -eq 'am-pyrevit7-setup' -and ([string]$e.message -match 'Access violation.*ucrtbase\.dll|Installer exit code 1\b')) { $hits.Add("log $($l.Ts.ToString('yyyy-MM-dd HH:mm'))Z: $($e.message)") }
        }
        if ($hits.Count -eq 0) {
            Add-Finding 'INSTALL_LOGS' 'ok' "no installer crash in setup logs of the last $Days days" '-'
        } else {
            $ev = "$($hits.Count) crash line(s), e.g. $($hits[0].TrimEnd('.'))"
            if ($py7Ok) {
                Add-Finding 'INSTALL_CRASH' 'info' "$ev. pyRevit 7 is installed now, so the crash is resolved." 'No action. If you reinstall, use am-pyrevit7-setup -Install -Force (no /LOG).'
            } else {
                Add-Finding 'INSTALL_CRASH' 'error' "$ev. Cause: the installer /LOG flag." $FixMap.INSTALL_CRASH.Text
            }
        }
    } catch { Add-Finding 'INSTALL_LOGS' 'unchecked' "could not read setup logs: $($_.Exception.Message)" 'Look in %TEMP%\pyrevit7-setup' }
}

$revit = @()
try { $revit = @(Get-Process Revit -ErrorAction SilentlyContinue) } catch { }

if (Test-Want 'agent') {
    if (-not $py7Ok) {
        Add-Finding 'AGENT' 'unchecked' "blocked: pyRevit 7 not found at $(Redact $Py7)" 'Fix PY7_MISSING first.'
    } else {
        $cfg = Invoke-Native $Py7 @('configs', 'agent') 60
        if (-not $cfg.Ok) { Add-Finding 'AGENT_CONFIG' 'unchecked' "pyrevit configs agent failed: $($cfg.Error)" 'Run: pyrevit configs agent' }
        elseif ($cfg.Out -match '\bDisabled\b') { Add-Finding 'AGENT_DISABLED' 'error' "pyrevit configs agent: $($cfg.Out)" $FixMap.AGENT_DISABLED.Text }
        elseif ($cfg.Out -match '\bEnabled\b') {
            Add-Finding 'AGENT_CONFIG' 'ok' "pyrevit configs agent: $($cfg.Out)" '-'
            $pol = Invoke-Native $Py7 @('configs', 'agent', 'policy') 60
            if (-not $pol.Ok) { Add-Finding 'AGENT_POLICY' 'unchecked' "pyrevit configs agent policy failed: $($pol.Error)" 'Run: pyrevit configs agent policy' }
            elseif ($pol.Out -match '\bauto\b') { Add-Finding 'POLICY_AUTO' 'warn' "$($pol.Out). Model changes run without Keep/Discard approval. If the user chose auto on purpose, report it only" "$($FixMap.POLICY_AUTO.Text) (back to the default; only when the user asks)" }
            elseif ($pol.Out -match '\bask\b') { Add-Finding 'AGENT_POLICY' 'ok' "$($pol.Out). Each model change waits for Keep/Discard in Revit" $PolicySwitch }
            elseif ($pol.Out -match '\breadonly\b') { Add-Finding 'AGENT_POLICY' 'info' "$($pol.Out). Queries run; model changes are refused" $PolicySwitch }
            else { Add-Finding 'AGENT_POLICY' 'unchecked' "unknown policy output: $($pol.Out)" 'Run: pyrevit configs agent policy' }

            if ($revit.Count -eq 0) {
                if ($RevitWasRunning) {
                    Add-Finding 'AGENT_STATUS' 'warn' 'Revit closed after it answered earlier in this session. Unsaved work may be lost.' 'Run am-revit-crash-doctor for a read-only crash report.'
                } else {
                    Add-Finding 'AGENT_STATUS' 'info' 'Revit is not running, so the agent connection was not tested.' 'Open Revit and a model, then run the doctor again.'
                }
            } else {
                # 'revit_busy' is transient (Revit is in a command or a dialog): wait and retry before judging.
                $st = Invoke-Native $Py7 @('agent', 'status') 60
                for ($try = 1; $try -le 2 -and $st.Ok -and $st.Out -match 'revit_busy' -and $st.Out -notmatch '(?m):\s*responding\s*$'; $try++) {
                    Write-Host "[am-doctor] Revit is busy. Waiting $BusyWaitSec s, then checking the agent again ($try of 2)..."
                    Start-Sleep -Seconds $BusyWaitSec
                    $st = Invoke-Native $Py7 @('agent', 'status') 60
                }
                if (-not $st.Ok) { Add-Finding 'AGENT_STATUS' 'unchecked' "pyrevit agent status failed: $($st.Error)" 'Run: pyrevit agent status' }
                elseif ($st.Out -match '(?m):\s*responding\s*$') {
                    $line = ([regex]::Match($st.Out, '(?m)^.*:\s*responding\s*$')).Value.Trim()
                    Add-Finding 'AGENT_STATUS' 'ok' $line '-'
                } elseif ($st.Out -match 'revit_busy') {
                    $busy = ((($st.Out -split "`r?`n") | Where-Object { $_ -match 'revit_busy' }) | Select-Object -First 1).Trim()
                    # The busy line names the open windows when the agent reports them.
                    # The exact shape is not fixed, so match the common forms loosely.
                    $names = ''
                    if ($busy -match '(?i)open windows?\s*[:=]\s*(.+)') { $names = ($Matches[1].Trim() -replace '\)\s*$', '') }
                    elseif ($busy -match '(?i)dialogs?\s*[:=]\s*(.+)') { $names = ($Matches[1].Trim() -replace '\)\s*$', '') }
                    elseif ($busy -match '(?i)\(([^)]*dialog[^)]*)\)') { $names = $Matches[1].Trim() }
                    if ($names) {
                        Add-Finding 'AGENT_BLOCKED_DIALOG' 'warn' "Revit is blocked by a modal dialog: $names ($busy)" 'Close the dialog in Revit, then run the doctor again.'
                    } else {
                        # No windows named: the holder is probably a second agent session, not a dialog.
                        Add-Finding 'AGENT_BUSY_SESSION' 'warn' "Revit stayed busy for 3 checks and the busy line names no open windows. Another agent session probably holds Revit. ($busy)" 'Wait for the other session to finish, then run the doctor again. Run Revit-touching sessions in sequence.'
                    }
                } else {
                    $inst = foreach ($p in $revit) {
                        $j = Join-Path $env:APPDATA "pyRevit\agent\instances\$($p.Id).json"
                        "pid $($p.Id) instance file " + $(if (Test-Path -LiteralPath $j) { 'present' } else { 'missing' })
                    }
                    $first = (($st.Out -split "`r?`n") | Where-Object { $_.Trim() } | Select-Object -Last 1)
                    Add-Finding 'AGENT_NOT_RESPONDING' 'error' "Revit running but agent status says: $first; $($inst -join '; ') (%APPDATA%\pyRevit\agent\instances)" $FixMap.AGENT_NOT_RESPONDING.Text
                }
            }
        } else { Add-Finding 'AGENT_CONFIG' 'unchecked' "unknown output: $($cfg.Out)" 'Run: pyrevit configs agent' }
    }
}

if (Test-Want 'revit') {
    if ($revit.Count -eq 0) {
        Add-Finding 'REVIT' 'info' 'Revit is not running.' 'Open Revit and a model to use the pyrevit tools.'
    } else {
        foreach ($p in $revit) {
            $t = [string]$p.MainWindowTitle
            if (-not $t) { Add-Finding 'REVIT' 'unchecked' "pid $($p.Id): no window title (Revit may still be starting)" 'Wait for Revit to load, then run the doctor again.' }
            elseif ($t -match '\[Home\]') { Add-Finding 'NO_MODEL_OPEN' 'warn' "pid $($p.Id) window title: $t" $FixMap.NO_MODEL_OPEN.Text }
            else { Add-Finding 'REVIT_MODEL' 'ok' "pid $($p.Id) has a document open (title: $t)" '-' }
        }
    }
}

if (Test-Want 'mcp') {
    $claude = Find-ClaudeCli
    if (-not $claude) {
        Add-Finding 'MCP' 'unchecked' 'Claude Code CLI not found (AnthropicClaude\claude.exe is not the CLI)' 'Open Claude Code once, then run the doctor again.'
    } else {
        $g = Invoke-Native $claude @('mcp', 'get', $McpName) 90
        if (-not $g.Ok) { Add-Finding 'MCP' 'unchecked' "claude mcp get $McpName failed: $($g.Error)" "Run: claude mcp get $McpName" }
        elseif ($g.Out -match 'No MCP server named') { Add-Finding 'MCP_MISSING' 'error' "claude mcp get ${McpName}: no MCP server with this name" $FixMap.MCP_MISSING.Text }
        elseif ($g.Out -match '(?m)^\s*Command:\s*(.+?)\s*$') {
            $cmdPath = $Matches[1]
            if ((Get-NormalPath $cmdPath) -ieq (Get-NormalPath $Py7)) { Add-Finding 'MCP' 'ok' "$McpName MCP command is $(Redact $cmdPath)" '-' }
            else { Add-Finding 'MCP_WRONG_PATH' 'error' "$McpName MCP command is '$(Redact $cmdPath)', expected $(Redact $Py7)" $FixMap.MCP_WRONG_PATH.Text }
        } else { Add-Finding 'MCP' 'unchecked' "unknown output from claude mcp get ${McpName}: $(Redact $g.Out)" "Run: claude mcp get $McpName" }
    }
}

if (Test-Want 'deps') {
    try {
        $d = Get-DepsState
        switch ($d.State) {
            'ok'           { Add-Finding 'DEPS' 'ok' $d.Evidence '-' }
            'missing'      { Add-Finding 'DEPS_MISSING' 'error' "$($d.Evidence). archi-magic fails to load without them" $FixMap.DEPS_MISSING.Text }
            'notinstalled' { Add-Finding 'DEPS' 'info' $d.Evidence '-' }
            'disabled'     { Add-Finding 'DEPS' 'info' $d.Evidence 'Enable archi-magic in /plugin, then run the doctor again.' }
            default        { Add-Finding 'DEPS' 'unchecked' $d.Evidence 'Run: claude plugin list' }
        }
    } catch { Add-Finding 'DEPS' 'unchecked' "could not check plugin dependencies: $($_.Exception.Message)" 'Run: claude plugin list' }
}

if (Test-Want 'config') {
    # Claude Code JSON files: no byte-order mark, valid JSON (issue #253).
    try {
        $okList = @(); $notFound = @()
        foreach ($cf in (Get-ClaudeJsonFiles)) {
            $s = Read-JsonFileState $cf.Path
            $shown = Redact $cf.Path
            if (-not $s.Exists) { $notFound += $cf.Label; continue }
            if ($s.Bom) {
                Add-Finding 'CONFIG_BOM' 'error' "$shown starts with a $($s.Bom) byte-order mark (BOM). Claude Code cannot read it ('JSON Parse error: Unrecognized token'), so plugins and their hooks do not load. A PowerShell 5.1 edit (Set-Content or Out-File) adds it" $FixMap.CONFIG_BOM.Text
            }
            if ($s.DecodeError) { Add-Finding 'CONFIG_BAD_JSON' 'error' "${shown}: $($s.DecodeError)" $FixMap.CONFIG_BAD_JSON.Text }
            elseif ($s.ParseError) { Add-Finding 'CONFIG_BAD_JSON' 'error' "${shown}: $($s.ParseError)$(if ($s.Bom) { ' (also after the byte-order mark)' })" $FixMap.CONFIG_BAD_JSON.Text }
            elseif (-not $s.Bom) { $okList += $cf.Label }
        }
        if ($okList.Count) { Add-Finding 'CONFIG' 'ok' "no byte-order mark, valid JSON: $($okList -join ', ')$(if ($notFound.Count) { " (not found: $($notFound -join ', '))" })" '-' }
        elseif ($notFound.Count -eq 3) { Add-Finding 'CONFIG' 'info' "no Claude Code JSON file found ($($notFound -join ', ')) in $(Redact (Split-Path -Parent $ClaudeSettingsJson))" '-' }
    } catch { Add-Finding 'CONFIG' 'unchecked' "could not read the Claude Code JSON files: $($_.Exception.Message)" 'Run: doctor.ps1 -Only config' }

    # Personal skill copies (%USERPROFILE%\.claude\skills\<name>) with the name of a plugin skill. Both
    # copies load, and the personal one does not update, so they drift (issue #253).
    try {
        $owners = @{}
        foreach ($n in (Get-SkillNames (Join-Path $PluginRoot 'skills'))) { if (-not $owners.ContainsKey($n)) { $owners[$n] = 'archi-magic' } }
        if ($script:AmPluginsLoaded) {
            $inst = Read-AmInstalledPlugins $InstalledPluginsJson
            if ($inst.Ok) {
                foreach ($e in $inst.Entries.Values) {
                    if (-not $e.InstallPath) { continue }
                    foreach ($n in (Get-SkillNames (Join-Path $e.InstallPath 'skills'))) { if (-not $owners.ContainsKey($n)) { $owners[$n] = $e.Key } }
                }
            }
        }
        $personal = @(Get-SkillNames $PersonalSkillsDir)
        $shadow = @($personal | Where-Object { $owners.ContainsKey($_) } | Sort-Object)
        if ($shadow.Count) {
            $list = @($shadow | ForEach-Object { "$_ ($($owners[$_]))" })
            Add-Finding 'SKILL_SHADOW' 'warn' "$($shadow.Count) personal skill cop$(if ($shadow.Count -eq 1) { 'y' } else { 'ies' }) in $(Redact $PersonalSkillsDir) with the name of a plugin skill: $($list -join ', '). Both copies load, and the personal copy does not update" $FixMap.SKILL_SHADOW.Text
        } else {
            Add-Finding 'SKILLS' 'ok' "no personal skill copy in $(Redact $PersonalSkillsDir) has the name of a plugin skill ($($personal.Count) personal, $($owners.Count) plugin skills)" '-'
        }
    } catch { Add-Finding 'SKILLS' 'unchecked' "could not compare personal and plugin skills: $($_.Exception.Message)" 'Run: doctor.ps1 -Only config' }
}

if (Test-Want 'tools') {
    # git: Claude Code clones the marketplace with it (issue #254).
    try {
        $git = Resolve-Tool $GitExe @('git') @((Join-Path $env:ProgramFiles 'Git\cmd\git.exe'), $(if ($env:LOCALAPPDATA) { Join-Path $env:LOCALAPPDATA 'Programs\Git\cmd\git.exe' }))
        if (-not $git) {
            Add-Finding 'GIT_MISSING' 'warn' "git not found. Claude Code needs it to add and update the archi-magic marketplace ('Command 'git' not found')" $FixMap.GIT_MISSING.Text
        } else {
            $v = Invoke-Native $git.Path @('--version') 30
            $line = (([string]$v.Out -split "`r?`n") | Where-Object { $_ -match 'git version' } | Select-Object -First 1)
            if (-not $v.Ok -or -not $line) { Add-Finding 'GIT_MISSING' 'warn' "git at $(Redact $git.Path) did not run: $(if ($v.Error) { $v.Error } else { "exit $($v.ExitCode)" })" $FixMap.GIT_MISSING.Text }
            elseif (-not $git.OnPath) { Add-Finding 'GIT_MISSING' 'warn' "$($line.Trim()) is at $(Redact $git.Path), but not on PATH for this session" 'Restart the Claude app so the new PATH loads, then run the doctor again.' }
            else { Add-Finding 'GIT' 'ok' "$($line.Trim()) ($(Redact $git.Path))" '-' }
        }
    } catch { Add-Finding 'GIT' 'unchecked' "could not check git: $($_.Exception.Message)" 'Run: git --version' }

    # gh: am-feedback posts issues with it. Optional, so only info. The account name is never shown.
    try {
        $gh = Resolve-Tool $GhExe @('gh') @((Join-Path $env:ProgramFiles 'GitHub CLI\gh.exe'), $(if ($env:LOCALAPPDATA) { Join-Path $env:LOCALAPPDATA 'Programs\GitHub CLI\gh.exe' }))
        if (-not $gh) {
            Add-Finding 'GH_MISSING' 'info' 'gh (GitHub CLI) not found. am-feedback then gives you the text and the link to paste it' $FixMap.GH_MISSING.Text
        } else {
            $where = if ($gh.OnPath) { '' } else { ". It is not on PATH for this session: in PowerShell call it as & `"$(Redact $gh.Path)`"" }
            $a = Invoke-Native $gh.Path @('auth', 'status', '--hostname', 'github.com') 30
            if (-not $a.Ok) { Add-Finding 'GH' 'info' "gh auth status did not finish: $($a.Error)$where" 'Run: gh auth status' }
            elseif ($a.ExitCode -eq 0) { Add-Finding 'GH' 'ok' "gh is signed in to github.com$where" '-' }
            elseif ([string]$a.Out -match '(?i)not logged in|gh auth login|token .*invalid|Failed to log in') { Add-Finding 'GH_SIGNED_OUT' 'info' "gh is installed ($(Redact $gh.Path)) but not signed in to github.com$where" $FixMap.GH_SIGNED_OUT.Text }
            else { Add-Finding 'GH' 'info' "gh auth status exit $($a.ExitCode); the sign-in is not confirmed (offline?)$where" 'Run: gh auth status' }
        }
    } catch { Add-Finding 'GH' 'unchecked' "could not check gh: $($_.Exception.Message)" 'Run: gh auth status' }

    # Python: many skill scripts run 'python'. The Microsoft Store alias is not a Python (issue #254).
    try {
        # Candidates in order: python, python3, the Python Launcher 'py -3' (per-user installs often
        # have only py on PATH; issue #254 comment 2), then pyRevit's own CPython (not on PATH).
        $cands = @()
        if ($PythonExe -or $PyExe) {
            foreach ($c in @(@{ Cmd = 'python'; Path = $PythonExe; Pre = @() }, @{ Cmd = 'py'; Path = $PyExe; Pre = @('-3') })) {
                if ($c.Path -and $c.Path -ne 'none' -and (Test-Path -LiteralPath $c.Path)) { $cands += $c }
            }
        } else {
            foreach ($c in @(@{ Cmd = 'python'; Pre = @() }, @{ Cmd = 'python3'; Pre = @() }, @{ Cmd = 'py'; Pre = @('-3') })) {
                $gc = Get-Command $c.Cmd -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
                if ($gc) { $cands += @{ Cmd = $c.Cmd; Path = $gc.Source; Pre = $c.Pre } }
            }
            $cengines = Join-Path $Py7Root 'bin\cengines'
            if (Test-Path -LiteralPath $cengines) {
                foreach ($f in @(Get-ChildItem -Path (Join-Path $cengines 'CPY*\python.exe') -File -ErrorAction SilentlyContinue | Sort-Object FullName -Descending)) {
                    $cands += @{ Cmd = $f.FullName; Path = $f.FullName; Pre = @() }
                }
            }
        }
        $first = $null; $works = $null; $pyrevitPy = $null
        foreach ($c in $cands) {
            $t = Test-PythonCandidate $c.Path $c.Pre
            $c.Result = $t
            if (Test-PyRevitPython $c.Path) {
                if ($t.Version -and -not $pyrevitPy) { $pyrevitPy = $c }
                continue
            }
            if ($t.Version) { $works = $c; break }
            if (-not $first) { $first = $c }   # the first host command that does not run Python 3
        }
        $pyState = if (-not $first) { 'not found' } elseif ($first.Result.StoreAlias) { 'only the Microsoft Store alias' } else { 'not a working Python 3' }
        $cmdText = ''
        if ($works -and $works.Cmd -eq 'python') {
            $cmdText = 'python'
            Add-Finding 'PYTHON' 'ok' "python runs Python $($works.Result.Version) ($(Redact $works.Path))" '-'
        } elseif ($works) {
            $cmdText = (@($works.Cmd) + @($works.Pre)) -join ' '
            Add-Finding 'PYTHON' 'info' "'python' is $pyState, but '$cmdText' runs Python $($works.Result.Version) ($(Redact $works.Path)). Where a skill says 'python scripts/...', run '$cmdText scripts/...'" '-'
        } elseif ($pyrevitPy) {
            Add-Finding 'PYTHON_PYREVIT_ONLY' 'warn' "no Python for skill scripts: 'python' is $pyState. Only pyRevit's own CPython $($pyrevitPy.Result.Version) runs ($(Redact $pyrevitPy.Path)). It has no pip and no pytest, so it cannot run the skill tests or install a package" $FixMap.PYTHON_PYREVIT_ONLY.Text
        } elseif ($first -and $first.Result.StoreAlias) {
            Add-Finding 'PYTHON_MISSING' 'warn' "no Python: 'python' is only the Microsoft Store alias ($(Redact $first.Path)). Skill scripts that run python cannot run" $FixMap.PYTHON_MISSING.Text
        } else {
            Add-Finding 'PYTHON_MISSING' 'warn' "no Python: $(if ($first) { "'$($first.Cmd)' at $(Redact $first.Path) is not a working Python 3" } else { 'python, python3 and py not found' }). Skill scripts that run python cannot run" $FixMap.PYTHON_MISSING.Text
        }
        # pip and pytest: only the skill tests need pytest, so this is info (issue #254 comment 1).
        if ($works) {
            $mods = Test-PythonModules $works.Path $works.Pre
            if ($mods.Known -and -not $mods.Pip) {
                Add-Finding 'PYTEST_MISSING' 'info' "'$cmdText' has no pip, so it cannot install pytest for the skill tests. Skill scripts that use only the standard library still run" 'Only for contributors who run the skill tests: install Python per user with winget (it includes pip): winget install --id Python.Python.3.12 --exact --scope user'
            } elseif ($mods.Known -and -not $mods.Pytest) {
                Add-Finding 'PYTEST_MISSING' 'info' "'$cmdText' has pip but no pytest. Only the skill tests need pytest" "Only for contributors who run the skill tests: $cmdText -m pip install --user pytest, then $cmdText -m pytest -q tests in the skill folder"
            }
        }
    } catch { Add-Finding 'PYTHON' 'unchecked' "could not check Python: $($_.Exception.Message)" 'Run: python --version' }
}

if (Test-Want 'docs') {
    $fails = @($logs | Where-Object { $_.Entry.skill -eq 'am-pyrevit-docs' -and $_.Entry.step -eq 'cache-refresh' })
    if ($fails.Count -gt 0) {
        $last = $fails[-1]
        Add-Finding 'DOCS_OFFLINE' 'info' "$($fails.Count) docs refresh failure(s) in $Days days; last $($last.Ts.ToString('yyyy-MM-dd HH:mm'))Z: $($last.Entry.message)" $FixMap.DOCS_OFFLINE.Text
    } else {
        Add-Finding 'DOCS' 'ok' "no docs refresh failures in the last $Days days of logs" '-'
    }
}

# ------------------------------------------------------------------ output
$problems = @($script:Findings | Where-Object { $_.Severity -in 'warn', 'error' })
$unchecked = @($script:Findings | Where-Object { $_.Severity -eq 'unchecked' })
$exitCode = if ($problems.Count -gt 0) { 1 } elseif ($unchecked.Count -gt 0) { 2 } else { 0 }

Write-Host 'CODE | severity | evidence | fix'
$order = @{ error = 0; warn = 1; unchecked = 2; info = 3; ok = 4 }
foreach ($f in ($script:Findings | Sort-Object @{ Expression = { $order[$_.Severity] } }, Index)) {
    Write-Host ("{0} | {1} | {2} | {3}" -f $f.Code, $f.Severity, $f.Evidence, $f.Fix)
}
$summary = switch ($exitCode) {
    0 { 'RESULT: healthy (exit 0)' }
    1 { "RESULT: $($problems.Count) problem(s) found (exit 1): $((($problems | ForEach-Object Code) | Select-Object -Unique) -join ', ')" }
    2 { "RESULT: NOT VERIFIED - could not check: $((($unchecked | ForEach-Object Code) | Select-Object -Unique) -join ', ') (exit 2)" }
}
if ($exitCode -eq 1 -and $unchecked.Count -gt 0) { $summary += "; also could not check: $((($unchecked | ForEach-Object Code) | Select-Object -Unique) -join ', ')" }
Write-Host $summary

if ($Report) {
    $years = @(Get-InstalledRevitYears $RevitInstallRoot)
    $pluginVer = if ($script:AmLogLoaded) { Get-AmPluginVersion } else { 'unknown' }
    $codes = @(($problems + $unchecked) | ForEach-Object Code | Select-Object -Unique)
    $title = if ($codes.Count) { "am-doctor: $($codes -join ', ')" } else { 'am-doctor: problem not detected by the doctor' }
    $tail = @($logs | Select-Object -Last 20 | ForEach-Object { Redact $_.Raw })
    $body = New-Object System.Collections.Generic.List[string]
    $body.Add('## What happened'); $body.Add(''); $body.Add('(Describe what you tried and what you saw.)'); $body.Add('')
    $body.Add('## Versions'); $body.Add('')
    $body.Add("- archi-magic: $pluginVer")
    $body.Add("- pyRevit (per-user): $(if ($py7Ver) { $py7Ver } else { 'not found' })")
    $body.Add("- Revit installed: $(if ($years.Count) { $years -join ', ' } else { 'none found' })")
    $body.Add("- Windows: $([Environment]::OSVersion.Version)")
    $body.Add("- PowerShell: $($PSVersionTable.PSVersion)")
    $body.Add(''); $body.Add('## Doctor findings'); $body.Add(''); $body.Add('```')
    foreach ($f in $script:Findings) { $body.Add((Redact ("{0} | {1} | {2}" -f $f.Code, $f.Severity, $f.Evidence))) }
    $body.Add($summary); $body.Add('```'); $body.Add('')
    $body.Add("## Last $($tail.Count) archi-magic log lines"); $body.Add(''); $body.Add('```')
    if ($tail.Count) { foreach ($t in $tail) { $body.Add($t) } } else { $body.Add('(no log lines)') }
    $body.Add('```')
    $bodyText = $body -join "`n"
    Write-Host ''
    Write-Host '===== GitHub issue draft ====='
    Write-Host "Where: $IssuesUrl/new"
    Write-Host "Title: $title"
    Write-Host '----- Body -----'
    Write-Host $bodyText
    Write-Host '===== end of draft ====='
    if ($OpenIssue) {
        try {
            Set-Clipboard -Value $bodyText
            Start-Process ("$IssuesUrl/new?title=" + [uri]::EscapeDataString($title))
            Write-Host '[am-doctor] Body copied to the clipboard. Paste it into the issue page that opened.'
        } catch { Write-Host "[am-doctor] Could not open the issue page: $($_.Exception.Message). Open $IssuesUrl/new and paste the body above." }
    }
}
exit $exitCode
