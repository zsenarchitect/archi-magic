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

  Test parameters (-AddinsRoot, -UserAddinsRoot, -Py7Root, -LogDir, -SetupLogRoot, -McpName,
  -InstalledPluginsJson, -ClaudeSettingsJson, -Only) point the checks at other folders or names.
  Defaults are the real locations. The deps check only reads those two JSON files (no network).

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File .\doctor.ps1
  powershell -NoProfile -ExecutionPolicy Bypass -File .\doctor.ps1 -Fix REPOINT_NEEDED
  powershell -NoProfile -ExecutionPolicy Bypass -File .\doctor.ps1 -Report
#>
[CmdletBinding()]
param(
    [ValidateSet('', 'PY7_MISSING', 'REPOINT_NEEDED', 'INSTALL_CRASH', 'AGENT_DISABLED', 'AGENT_NOT_RESPONDING',
        'AGENT_BLOCKED_DIALOG', 'NO_MODEL_OPEN', 'MCP_MISSING', 'MCP_WRONG_PATH', 'POLICY_AUTO', 'DOCS_OFFLINE', 'DEPS_MISSING', 'HEADLESS_OLD_PYREVIT')]
    [string]$Fix = '',
    [switch]$Report,
    [switch]$OpenIssue,
    [int]$Days = 14,
    [string]$AddinsRoot = 'C:\ProgramData\Autodesk\Revit\Addins',
    [string]$UserAddinsRoot = (Join-Path $env:APPDATA 'Autodesk\Revit\Addins'),
    [string]$Py7Root = (Join-Path $env:APPDATA 'pyRevit-Master'),
    [string]$LogDir = '',
    [string]$SetupLogRoot = (Join-Path $env:TEMP 'pyrevit7-setup'),
    [string]$McpName = 'pyrevit',
    # Test parameters: another Claude Code CLI and another setup script. Defaults are the real ones.
    [string]$ClaudeCli = '',
    [string]$SetupScript = '',
    # Claude Code's config folder: CLAUDE_CONFIG_DIR when set, else %USERPROFILE%\.claude.
    [string]$InstalledPluginsJson = (Join-Path $(if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR } else { Join-Path $env:USERPROFILE '.claude' }) 'plugins\installed_plugins.json'),
    [string]$ClaudeSettingsJson = (Join-Path $(if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR } else { Join-Path $env:USERPROFILE '.claude' }) 'settings.json'),
    [int]$BusyWaitSec = 25,
    # Pass -RevitWasRunning when Revit answered earlier in this session (a get_context or run
    # succeeded) or the user says Revit just closed. Without it, "no Revit process" is only an
    # info line: the doctor cannot tell a Revit that never started from one that just closed.
    [switch]$RevitWasRunning,
    [string[]]$Only = @()
)
# -Only accepts 'a,b' as one string too (powershell -File passes it that way).
$Only = @($Only | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim().ToLowerInvariant() } | Where-Object { $_ })
$badOnly = @($Only | Where-Object { $_ -notin 'py7', 'repoint', 'install', 'agent', 'revit', 'mcp', 'docs', 'deps' })
if ($badOnly.Count) { Write-Host "[am-doctor] Unknown -Only value(s): $($badOnly -join ', '). Use: py7, repoint, install, agent, revit, mcp, docs, deps."; exit 2 }
$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'

$IssuesUrl = 'https://github.com/zsenarchitect/archi-magic/issues'
$PluginRoot = Join-Path $PSScriptRoot '..\..\..'
if (-not $SetupScript) { $SetupScript = Join-Path $PSScriptRoot '..\..\am-pyrevit7-setup\scripts\pyrevit7-setup.ps1' }
$Py7 = Join-Path $Py7Root 'bin\pyrevit.exe'

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

$FixMap = @{
    PY7_MISSING          = @{ Kind = 'setup'; Args = @('-Install', '-Repoint', '-EnableAgent', '-Verify'); Text = 'am-pyrevit7-setup: run setup (Install, Repoint, EnableAgent, Verify).' }
    REPOINT_NEEDED       = @{ Kind = 'setup'; Args = @('-Repoint', '-Verify'); Text = 'am-pyrevit7-setup -Repoint -Verify' }
    INSTALL_CRASH        = @{ Kind = 'setup'; Args = @('-Install', '-Force'); Text = 'am-pyrevit7-setup -Install -Force (the script no longer passes /LOG). Close Revit first.' }
    AGENT_DISABLED       = @{ Kind = 'setup'; Args = @('-EnableAgent'); Text = 'am-pyrevit7-setup -EnableAgent' }
    MCP_MISSING          = @{ Kind = 'setup'; Args = @('-EnableAgent'); Text = 'am-pyrevit7-setup -EnableAgent, then start a new Claude Code session.' }
    MCP_WRONG_PATH       = @{ Kind = 'setup'; Args = @('-EnableAgent'); Text = 'am-pyrevit7-setup -EnableAgent, then start a new Claude Code session.' }
    POLICY_AUTO          = @{ Kind = 'py7'; Args = @('configs', 'agent', 'policy', 'ask'); Text = 'pyrevit configs agent policy ask' }
    AGENT_NOT_RESPONDING = @{ Kind = 'manual'; Text = 'User: in Revit, click pyRevit tab > Reload (it can need two reloads), or restart Revit. Then run the doctor again.' }
    AGENT_BLOCKED_DIALOG = @{ Kind = 'manual'; Text = 'User: close the dialog in Revit, then run the doctor again.' }
    NO_MODEL_OPEN        = @{ Kind = 'manual'; Text = 'User: open a model in Revit (an Autodesk sample such as Snowdon Towers is fine).' }
    DOCS_OFFLINE         = @{ Kind = 'manual'; Text = 'No action needed. The cached docs copy is in use. Check the network later.' }
    HEADLESS_OLD_PYREVIT = @{ Kind = 'manual'; Text = 'Interactive Revit and the archi-magic skills are not affected. For your own pyrevit run scripts (batch, CI): ask IT to uninstall the machine-wide pyRevit in Program Files.' }
    DEPS_MISSING         = @{ Kind = 'deps'; Text = 'claude plugin install <name>@archi-magic for each missing one named in the evidence (in a session: /reload-plugins first, it installs missing dependencies), then quit and restart the Claude app.' }
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
        foreach ($src in $machine) {
            $year = $src.Directory.Name
            $user = @(Get-ChildItem -Path (Join-Path (Join-Path $UserAddinsRoot $year) 'pyRevit*.addin') -ErrorAction SilentlyContinue)
            if ($user.Count -eq 0) {
                Add-Finding 'REPOINT_NEEDED' 'error' "Revit ${year}: machine manifest $($src.Name) has no per-user override in $(Redact (Join-Path $UserAddinsRoot $year))" $FixMap.REPOINT_NEEDED.Text
                continue
            }
            $good = $null; $bad = @()
            foreach ($u in $user) {
                $asm = [regex]::Match([IO.File]::ReadAllText($u.FullName), '<Assembly>\s*(.*?)\s*</Assembly>').Groups[1].Value
                if ($asm -and $asm.StartsWith($Py7Root.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase) -and (Test-Path -LiteralPath $asm)) { $good = $asm }
                else { $bad += "$($u.Name) -> $(if ($asm) { Redact $asm } else { '(no Assembly)' })" }
            }
            if ($good) { Add-Finding 'REPOINT' 'ok' "Revit ${year}: per-user manifest loads $(Redact $good)" '-' }
            else { Add-Finding 'REPOINT_NEEDED' 'error' "Revit ${year}: per-user manifest does not point at an existing 7.x loader ($($bad -join '; '))" $FixMap.REPOINT_NEEDED.Text }
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
            elseif ($pol.Out -match '\bauto\b') { Add-Finding 'POLICY_AUTO' 'warn' "$($pol.Out). Model changes run without Keep/Discard approval." $FixMap.POLICY_AUTO.Text }
            elseif ($pol.Out -match '\bask\b') { Add-Finding 'AGENT_POLICY' 'ok' $pol.Out '-' }
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
                        Add-Finding 'AGENT_STATUS' 'unchecked' "Revit stayed busy for 3 checks: $busy" 'Finish or cancel the open Revit command, or close the dialog, then run the doctor again.'
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
    $years = @(Get-ChildItem 'C:\Program Files\Autodesk' -Directory -Filter 'Revit 20*' -ErrorAction SilentlyContinue | ForEach-Object { $_.Name -replace '\D', '' } | Sort-Object)
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
