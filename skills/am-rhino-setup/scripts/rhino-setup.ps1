<#
.SYNOPSIS
  Prepare this Windows user for McNeel's official 'rhino-mcp' Claude Code plugin:
  check Rhino 8 and Node.js, install the Rhino-MCP-Platform Rhino package per-user,
  and make Claude Code ask before every Rhino tool that can change a file or the model.

.DESCRIPTION
  Safe to re-run. Steps (default: Install, AskRules, Verify, in this order):
    -Install   Check Rhino and Node.js, run 'Yak.exe install Rhino-MCP-Platform' for the
               current user (never --all-users), and print the plugin install command
               for Claude Code.
               Skips yak when the package is already installed (-Force runs it again).
    -AskRules  Merge permissions.ask rules for the Rhino tools that can change a file or
               the model into the Claude Code user settings. Both tool-name forms are
               added: plugin (mcp__plugin_rhino-mcp_rhino__*) and manual (mcp__rhino__*).
               Backup first, never drops existing keys or rules, no write when nothing
               changes. The file is re-serialized (formatting can change, content cannot).
    -Verify    Print the final state. Read-only.
    -NoAsk     Do-not-ask mode (issue #290). Only when the user asks for it in their own words.
               Removes this script's Rhino ask rules from permissions.ask and adds one allow rule
               per Rhino server name (mcp__plugin_rhino-mcp_rhino, mcp__rhino) to
               permissions.allow. Same backup and self-check as -AskRules. A later default run
               sees the allow rule and does not add the ask rules again. An explicit -AskRules
               turns asking back on (adds the ask rules, removes those two allow rules).

  Long rule lists print as a count and the first names only. The full list goes to a text
  file in the log folder.

  The MCP layer itself is McNeel's plugin (github.com/mcneel/RhinoAI, cc-plugin), installed
  in Claude Code with: /plugin install rhino-mcp@archi-magic
  This script does not register any MCP server and does not copy the router.

  Never needs admin. Never starts, stops or kills Rhino.

  Exit codes: 0 ok, 2 required Rhino version not installed, 3 package install failed,
  5 settings.json not changed (invalid JSON, unexpected shape, or -NoAsk with -AskRules),
  6 verify found problems (normal on a machine that is not set up yet; each problem is one
  WARNING line), 7 Node.js not found. (4 is not used.)

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File .\rhino-setup.ps1
  powershell -NoProfile -ExecutionPolicy Bypass -File .\rhino-setup.ps1 -Verify
  powershell -NoProfile -ExecutionPolicy Bypass -File .\rhino-setup.ps1 -AskRules -SettingsPath C:\temp\settings.json
  powershell -NoProfile -ExecutionPolicy Bypass -File .\rhino-setup.ps1 -NoAsk
#>
[CmdletBinding()]
param(
    [switch]$Install,
    [switch]$AskRules,
    [switch]$Verify,
    [switch]$NoAsk,
    [switch]$Force,
    [ValidateSet('8', '9')][string]$RhinoVersion = '8',
    # Claude Code's config folder: CLAUDE_CONFIG_DIR when set, else %USERPROFILE%\.claude.
    [string]$SettingsPath = (Join-Path $(if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR } else { Join-Path $env:USERPROFILE '.claude' }) 'settings.json')
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
if ($NoAsk -and $AskRules) {
    Write-Host '[rhino-setup] FAILED: -NoAsk and -AskRules ask for opposite things. Use one of them.' -ForegroundColor Red
    exit 5
}
# An explicit -AskRules turns asking back on, also in do-not-ask mode. The default run respects that mode.
$ExplicitAskRules = [bool]$AskRules
if (-not ($Install -or $AskRules -or $Verify -or $NoAsk)) {
    $Install = $true; $AskRules = $true; $Verify = $true
}

$Stamp   = Get-Date -Format 'yyyyMMdd-HHmmss'
$WorkDir = Join-Path $env:TEMP "rhino-setup\$Stamp"
New-Item -ItemType Directory -Force $WorkDir | Out-Null
$LogFile = Join-Path $WorkDir 'setup.log'
Start-Transcript -Path $LogFile -Append | Out-Null

$PackageName   = 'Rhino-MCP-Platform'
$PluginName    = 'rhino-mcp'            # cc-plugin/.claude-plugin/plugin.json "name"
$PluginInstall = '/plugin install rhino-mcp@archi-magic'
$NodeUrl       = 'https://nodejs.org/en/download'
# Tools from rhino/plugin/Tools/*.cs that can change a file or the model.
# Read-only and view-only tools (list_objects, get_context, get_selection, get_viewport_image,
# get_commands, set_camera, set_selection, zoom_*, *_describe_*, *_search_*, *_get_canvas_graph)
# stay prompt-free.
$WriteTools = @(
    'run_python', 'run_csharp', 'run_command',
    'open_doc', 'save_doc', 'close_doc',
    'set_layer_material', 'manage_plugin_commands',
    # Router tools (rhino-mcp-router 0.3.0 tools/list): start or close a Rhino instance; closing can lose unsaved work.
    'spawn_slot', 'close_slot',
    'g1_apply_graph', 'g1_clear_canvas', 'g1_delete_component',
    'g1_connect', 'g1_connect_many', 'g1_place_component', 'g1_place_slider', 'g1_solve_graph', 'g1_start',
    'g2_apply_graph', 'g2_clear_canvas', 'g2_delete_component',
    'g2_connect', 'g2_connect_many', 'g2_place_component', 'g2_place_slider', 'g2_solve_canvas', 'g2_start'
)
# Plugin-bundled server: mcp__plugin_<plugin>_<server>__<tool> (Claude Code docs, "Plugin MCP tool names").
# Manual registration as 'rhino' (McNeel's MCPConnect): mcp__rhino__<tool>.
$AskList = @($WriteTools | ForEach-Object { "mcp__plugin_${PluginName}_rhino__$_" }) +
           @($WriteTools | ForEach-Object { "mcp__rhino__$_" })
# Do-not-ask mode: a server-level rule (no tool name) allows every tool of that server.
# Ask rules win over allow rules in Claude Code, so -NoAsk must also remove the ask rules.
$AllowList = @("mcp__plugin_${PluginName}_rhino", 'mcp__rhino')
$RhinoData = Join-Path $env:APPDATA 'McNeel\Rhinoceros'
$StagedExe = Join-Path $RhinoData 'ai\bin\rhino-mcp-router.exe'
$script:Problems = New-Object System.Collections.Generic.List[string]

function Say($msg)  { Write-Host "[rhino-setup] $msg" }
function Warn($msg) { Write-Host "[rhino-setup] WARNING: $msg" -ForegroundColor Yellow; $script:Problems.Add($msg) }
function Fail($code, $msg) {
    Write-Host "[rhino-setup] FAILED: $msg" -ForegroundColor Red
    Say "Log: $LogFile"
    Stop-Transcript | Out-Null
    exit $code
}

# Native exes write to stderr; with EAP=Stop, PS 5.1 turns that into a terminating error.
function Invoke-Native([string]$Exe, [string[]]$ArgList) {
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = (& $Exe @ArgList 2>&1 | ForEach-Object { "$_" }) -join "`n"
        $code = $LASTEXITCODE
    } finally { $ErrorActionPreference = $old }
    return [pscustomobject]@{ Out = $out; Code = $code }
}

function Get-RhinoDirs([string]$Version) {
    if ($Version -eq '8') { $names = @('Rhino 8') } else { $names = @('Rhino 9', 'Rhino 9 BETA', 'Rhino 9 WIP') }
    $found = @()
    foreach ($n in $names) {
        $d = Join-Path $env:ProgramFiles $n
        if (Test-Path (Join-Path $d 'System\Rhino.exe')) { $found += $d }
    }
    return $found
}

function Get-PackageVersions([string]$Version) {
    $base = Join-Path $RhinoData "packages\$Version.0\$PackageName"
    if (-not (Test-Path $base)) { return @() }
    return @(Get-ChildItem $base -Directory | Sort-Object { try { [version]($_.Name -replace '[^\d\.].*$', '') } catch { [version]'0.0' } } -Descending)
}

function Get-Node {
    $cmd = Get-Command node -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $cmd) { return $null }
    $v = (Invoke-Native $cmd.Source @('--version')).Out.Trim()
    return [pscustomobject]@{ Path = $cmd.Source; Version = $v }
}

function Test-PluginInstalled {
    $f = Join-Path $(if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR } else { Join-Path $env:USERPROFILE '.claude' }) 'plugins\installed_plugins.json'
    if (-not (Test-Path $f)) { return @() }
    try { $j = [IO.File]::ReadAllText($f) | ConvertFrom-Json } catch { return @() }
    if (-not $j.plugins) { return @() }
    return @($j.plugins.PSObject.Properties.Name | Where-Object { $_ -like "$PluginName@*" })
}

function Find-ClaudeCli {
    # The Claude desktop app's own exe (AnthropicClaude\claude.exe) is NOT the CLI. Never use it.
    $cmd = Get-Command claude -ErrorAction SilentlyContinue |
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

function Read-JsonFile([string]$Path) {
    # Returns @{ Ok; Obj; Text; Error }. Reads UTF-8 explicitly (PS 5.1 default is ANSI).
    $text = [IO.File]::ReadAllText($Path, (New-Object Text.UTF8Encoding $false))
    if ($text.Length -gt 0 -and $text[0] -eq [char]0xFEFF) { $text = $text.Substring(1) }
    if ($text.Trim().Length -eq 0) { return @{ Ok = $false; Error = 'file is empty'; Text = $text } }
    try { $obj = $text | ConvertFrom-Json } catch { return @{ Ok = $false; Error = $_.Exception.Message; Text = $text } }
    if (-not ($obj -is [System.Management.Automation.PSCustomObject])) {
        return @{ Ok = $false; Error = 'top level is not a JSON object'; Text = $text }
    }
    return @{ Ok = $true; Obj = $obj; Text = $text }
}

function Get-PermList($obj, [string]$Key) {
    if (-not $obj) { return @() }
    $p = $obj.PSObject.Properties['permissions']
    if (-not $p -or -not ($p.Value -is [System.Management.Automation.PSCustomObject])) { return @() }
    $a = $p.Value.PSObject.Properties[$Key]
    if (-not $a -or $null -eq $a.Value) { return @() }
    return @($a.Value)
}
function Get-AskRules($obj) { return @(Get-PermList $obj 'ask') }

# Issue #305: 56 rule names in the console drown the one fact. Print a count and the first names.
function Format-Short([string[]]$Names, [int]$Show = 3) {
    $Names = @($Names)
    if ($Names.Count -le $Show) { return ($Names -join ', ') }
    return "$(($Names[0..($Show - 1)]) -join ', ') and $($Names.Count - $Show) more"
}
function Save-FullList([string]$Name, [string[]]$Names) {
    $f = Join-Path $WorkDir $Name
    [IO.File]::WriteAllText($f, ((@($Names) -join "`r`n") + "`r`n"), (New-Object Text.UTF8Encoding $false))
    return $f
}

function Update-Permissions([string]$Path, [hashtable]$Add, [hashtable]$Remove) {
    # Changes only permissions.ask and permissions.allow. $Add / $Remove: @{ ask = @(..); allow = @(..) }.
    # Returns $true when the file was written, $false when nothing had to change.
    Say "Settings file: $Path"
    $dir = Split-Path -Parent $Path
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force $dir | Out-Null }
    $exists = Test-Path $Path
    if ($exists) {
        $r = Read-JsonFile $Path
        if (-not $r.Ok) { Fail 5 "Not valid JSON ($($r.Error)). Nothing was written. Fix the file, then run the script again." }
        $obj = $r.Obj
    } else {
        $obj = New-Object PSObject
    }
    $before = $obj | ConvertTo-Json -Depth 100 -Compress

    $permProp = $obj.PSObject.Properties['permissions']
    if ($permProp -and -not ($permProp.Value -is [System.Management.Automation.PSCustomObject])) {
        Fail 5 "'permissions' is not a JSON object. Nothing was written."
    }
    # Plan first, so a run with nothing to do never creates keys.
    $plan = [ordered]@{}
    foreach ($key in @('ask', 'allow')) {
        $toAdd = @($Add[$key] | Where-Object { $_ }); $toRemove = @($Remove[$key] | Where-Object { $_ })
        $prop = if ($permProp) { $permProp.Value.PSObject.Properties[$key] } else { $null }
        if ($prop -and -not ($prop.Value -is [System.Array])) { Fail 5 "'permissions.$key' is not a JSON array. Nothing was written." }
        $current = if ($prop) { @($prop.Value) } else { @() }
        $removed = @($current | Where-Object { $toRemove -contains $_ })
        $kept = @($current | Where-Object { $toRemove -notcontains $_ })
        $added = @($toAdd | Where-Object { $kept -notcontains $_ })
        if ($added.Count -eq 0 -and $removed.Count -eq 0) { continue }
        $plan[$key] = @{ Original = $current; New = [object[]]($kept + $added); Added = $added; Removed = $removed; Created = (-not $prop) }
    }
    if ($plan.Count -eq 0) { return $false }

    $createdPerm = $false
    if (-not $permProp) {
        $obj | Add-Member -NotePropertyName permissions -NotePropertyValue (New-Object PSObject)
        $createdPerm = $true
    }
    $perm = $obj.permissions
    foreach ($key in $plan.Keys) {
        if ($plan[$key].Created) { $perm | Add-Member -NotePropertyName $key -NotePropertyValue $plan[$key].New }
        else { $perm.$key = $plan[$key].New }
    }
    $newText = $obj | ConvertTo-Json -Depth 100

    # Semantic guard: the new JSON with the two arrays put back must equal the original.
    $check = $newText | ConvertFrom-Json
    foreach ($key in $plan.Keys) {
        if ($plan[$key].Created) { $check.permissions.PSObject.Properties.Remove($key) }
        else { $check.permissions.$key = [object[]]$plan[$key].Original }
    }
    if ($createdPerm) { $check.PSObject.Properties.Remove('permissions') }
    $after = $check | ConvertTo-Json -Depth 100 -Compress
    if ($after -ne $before) { Fail 5 'Merge self-check failed: the new JSON would change more than permissions.ask and permissions.allow. Nothing was written.' }

    $backup = $null
    if ($exists) {
        $backup = "$Path.am-rhino-backup-$Stamp"
        Copy-Item $Path $backup -Force
        Say "Backup: $backup"
    }
    $tmp = "$Path.tmp-$Stamp"
    try {
        [IO.File]::WriteAllText($tmp, $newText, (New-Object Text.UTF8Encoding $false))
        # [NullString]::Value: a plain $null becomes "" for a string parameter, which Replace rejects.
        if ($exists) { [IO.File]::Replace($tmp, $Path, [NullString]::Value) } else { Move-Item $tmp $Path }
    } catch {
        Remove-Item $tmp -Force -ErrorAction SilentlyContinue
        Fail 5 "Could not write $Path ($($_.Exception.Message)). The original file is unchanged."
    }

    $re = Read-JsonFile $Path
    if (-not $re.Ok) { Fail 5 "After write, $Path does not parse ($($re.Error)). Restore it from $backup." }
    foreach ($key in $plan.Keys) {
        $now = @(Get-PermList $re.Obj $key)
        foreach ($rule in $plan[$key].Added) { if ($now -notcontains $rule) { Fail 5 "After write, $key rule '$rule' is missing." } }
        foreach ($rule in $plan[$key].Removed) { if ($now -contains $rule) { Fail 5 "After write, $key rule '$rule' is still there." } }
        if ($plan[$key].Added.Count -gt 0) {
            $full = Save-FullList "$key-rules-added.txt" $plan[$key].Added
            Say "Added $($plan[$key].Added.Count) $key rule(s): $(Format-Short $plan[$key].Added). Full list: $full"
        }
        if ($plan[$key].Removed.Count -gt 0) {
            $full = Save-FullList "$key-rules-removed.txt" $plan[$key].Removed
            Say "Removed $($plan[$key].Removed.Count) $key rule(s): $(Format-Short $plan[$key].Removed). Full list: $full"
        }
    }
    return $true
}

Say "Log file: $LogFile"
$rhinoDirs = @(Get-RhinoDirs $RhinoVersion)

# ---------------------------------------------------------------- Install
if ($Install) {
    Say "== Prerequisites and $PackageName for Rhino $RhinoVersion =="
    if ($rhinoDirs.Count -eq 0) { Fail 2 "Rhino $RhinoVersion is not installed under $env:ProgramFiles. Install it from https://www.rhino3d.com/download first." }
    Say "Rhino ${RhinoVersion}: $($rhinoDirs[0])"

    $node = Get-Node
    if (-not $node) {
        Fail 7 "Node.js is not installed. The rhino-mcp plugin needs it to start. Download the Windows installer from $NodeUrl. If the installer asks for admin rights, ask your IT team to install Node.js. Then run this script again."
    }
    Say "Node.js: $($node.Version) ($($node.Path))"

    $yak = Join-Path $rhinoDirs[0] 'System\Yak.exe'
    if (-not (Test-Path $yak)) { Fail 3 "Yak.exe not found at $yak." }
    $have = @(Get-PackageVersions $RhinoVersion)
    if ($have.Count -gt 0 -and -not $Force) {
        Say "$PackageName $($have[0].Name) is already installed for Rhino $RhinoVersion. Skipping (use -Force to run yak install again)."
    } else {
        Say "Running: `"$yak`" install $PackageName (current user only)"
        $r = Invoke-Native $yak @('install', $PackageName)
        $r.Out -split "`n" | ForEach-Object { Say "yak: $_" }
        $have = @(Get-PackageVersions $RhinoVersion)
        if ($r.Code -ne 0 -or $have.Count -eq 0) {
            Say 'Manual install (no admin needed):'
            Say "  1. Open Rhino $RhinoVersion."
            Say '  2. Type PackageManager and press Enter.'
            Say "  3. Search for $PackageName and click Install."
            Say '  4. Close Rhino, open it again, then run this script again.'
            Fail 3 "yak install exited $($r.Code)."
        }
        Say "Installed $PackageName $($have[0].Name)."
    }

    if ((Test-PluginInstalled).Count -gt 0) {
        Say "Claude Code plugin '$PluginName' is installed: $((Test-PluginInstalled) -join ', ')"
    } else {
        Say "NEXT: in Claude Code, type:  $PluginInstall"
        Say '      Then restart the Claude desktop app and open a new Code session.'
    }
}

# ---------------------------------------------------------------- AskRules / NoAsk
# Do-not-ask mode is on when a Rhino server allow rule is in permissions.allow (set by -NoAsk or by hand).
$noAskMode = $false
if (Test-Path $SettingsPath) {
    $s0 = Read-JsonFile $SettingsPath
    if ($s0.Ok) {
        $allow0 = @(Get-PermList $s0.Obj 'allow')
        $noAskMode = @($AllowList | Where-Object { $allow0 -contains $_ }).Count -gt 0
    }
}
if ($NoAsk) {
    Say '== Do-not-ask mode: Rhino tools run without a Claude Code prompt =='
    if (-not (Update-Permissions $SettingsPath @{ allow = $AllowList } @{ ask = $AskList })) {
        Say 'Do-not-ask mode is already on. settings.json not changed.'
    }
    Say 'Claude no longer asks before Rhino tools. Save the file before each change. Use Undo in Rhino to reverse a change.'
    Say 'To turn asking back on, run this script with -AskRules.'
} elseif ($AskRules) {
    if ($noAskMode -and -not $ExplicitAskRules) {
        Say '== Ask rules skipped: do-not-ask mode is on (a Rhino allow rule is in settings.json) =='
        Say 'The user chose this mode. To turn asking back on, run this script with -AskRules.'
    } else {
        Say '== Ask before Rhino tools that change a file or the model =='
        if (-not (Update-Permissions $SettingsPath @{ ask = $AskList } @{ allow = $AllowList })) {
            Say "All $($AskList.Count) ask rules already present. settings.json not changed."
        }
    }
}

# ---------------------------------------------------------------- Verify
if ($Verify) {
    Say '== Verify =='
    if ($rhinoDirs.Count -gt 0) { Say "Rhino ${RhinoVersion}: $($rhinoDirs -join ', ')" } else { Warn "Rhino $RhinoVersion is not installed." }
    if ($RhinoVersion -eq '8') {
        $r9 = @(Get-RhinoDirs '9')
        if ($r9.Count -gt 0) { Say "Also found Rhino 9: $($r9 -join ', '). Not configured (the router uses Rhino 8 by default)." }
    }
    $running = @(Get-Process Rhino -ErrorAction SilentlyContinue)
    Say "Rhino windows open: $($running.Count)"
    if ($running.Count -gt 1) { Say 'More than one Rhino is open. Claude can edit a different document than the one you look at.' }

    $node = Get-Node
    if ($node) { Say "Node.js: $($node.Version) ($($node.Path))" } else { Warn "Node.js not found. The rhino-mcp plugin cannot start. Get it from $NodeUrl." }

    $pk = @(Get-PackageVersions $RhinoVersion)
    if ($pk.Count -gt 0) { Say "${PackageName}: $(($pk | ForEach-Object Name) -join ', ') (Rhino $RhinoVersion)" } else { Warn "$PackageName is not installed for Rhino $RhinoVersion. Run -Install." }
    # Info only: McNeel's router-launcher.mjs finds the router in the installed package itself.
    if (Test-Path $StagedExe) { Say "Router (info): $StagedExe" }
    else {
        # The package ships win-x64 and win-arm64 routers; name the one for this CPU, not the first alphabetically.
        $cpu = if ($env:PROCESSOR_ARCHITEW6432) { $env:PROCESSOR_ARCHITEW6432 } else { $env:PROCESSOR_ARCHITECTURE }
        $rid = if ($cpu -eq 'ARM64') { 'win-arm64' } else { 'win-x64' }
        $payload = @(Get-ChildItem (Join-Path $RhinoData "packages\*\Rhino-MCP-Platform\*\router\$rid\rhino-mcp-router.exe") -ErrorAction SilentlyContinue | Select-Object -First 1)
        if ($payload.Count -gt 0) { Say "Router (info): not staged yet; the plugin launcher will use $($payload[0].FullName)" }
        else { Say 'Router (info): not found yet; the plugin launcher locates it in the installed Rhino package.' }
    }

    $installed = @(Test-PluginInstalled)
    if ($installed.Count -gt 0) { Say "Claude Code plugin: $($installed -join ', ')" }
    else { Warn "Claude Code plugin '$PluginName' is not installed. In Claude Code, type: $PluginInstall" }
    # Issue #305: without Node.js or the plugin, the RhinoCode CLI can still reach a running Rhino.
    if ((-not $node -or $installed.Count -eq 0) -and $rhinoDirs.Count -gt 0) {
        $rhinoCode = Join-Path $rhinoDirs[0] 'System\RhinoCode.exe'
        if (Test-Path $rhinoCode) { Say "RhinoCode fallback (info): $rhinoCode works without Node.js and the plugin. See 'Fallback: RhinoCode CLI' in am-rhino-setup." }
    }

    $claude = Find-ClaudeCli
    if ($claude) {
        $get = Invoke-Native $claude @('mcp', 'get', 'rhino')
        if ($get.Code -eq 0) {
            $manual = 'Manual MCP server registration named rhino found (for example from the MCPConnect command).'
            if ($installed.Count -gt 0) { Warn "$manual The plugin also provides one. Two Rhino servers confuse Claude. Remove one: claude mcp remove rhino -s user" }
            else { Say $manual }
        }
    }

    if (Test-Path $SettingsPath) {
        $s = Read-JsonFile $SettingsPath
        if (-not $s.Ok) { Warn "$SettingsPath is not valid JSON ($($s.Error))." }
        else {
            $ask = @(Get-AskRules $s.Obj)
            $allow = @(Get-PermList $s.Obj 'allow')
            $allowOn = @($AllowList | Where-Object { $allow -contains $_ })
            $present = @($AskList | Where-Object { $ask -contains $_ })
            $miss = @($AskList | Where-Object { $ask -notcontains $_ })
            if ($allowOn.Count -gt 0) {
                Say "Do-not-ask mode: on ($($allowOn -join ', ') in permissions.allow). Rhino tools run without a prompt. Run -AskRules to turn asking back on."
                if ($present.Count -gt 0) { Warn "Do-not-ask mode is on, but $($present.Count) Rhino ask rule(s) remain and still prompt ($(Format-Short $present)). Run -NoAsk again." }
            } elseif ($miss.Count -eq 0) { Say "Ask rules: all $($AskList.Count) present in $SettingsPath" }
            else {
                $full = Save-FullList 'ask-rules-missing.txt' $miss
                Warn "$($miss.Count) of $($AskList.Count) ask rules missing ($(Format-Short $miss)). Run -AskRules. Full list: $full"
            }
        }
    } else { Warn "$SettingsPath not found: no ask rules. Run -AskRules." }
    Say 'Check by hand in Rhino: run SetDotNetRuntime and make sure it says NETCore (the plugin does not load on .NET Framework).'
}

Say "Log: $LogFile"
Stop-Transcript | Out-Null
if ($script:Problems.Count -gt 0) {
    # Issue #305: name each problem once. The WARNING lines above already hold the details.
    Write-Host "[rhino-setup] Finished with $($script:Problems.Count) problem(s), one per WARNING line above. Exit code 6." -ForegroundColor Yellow
    exit 6
}
Write-Host '[rhino-setup] Done. No problems found.' -ForegroundColor Green
exit 0
