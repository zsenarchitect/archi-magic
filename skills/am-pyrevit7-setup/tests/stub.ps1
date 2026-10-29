# Test stub for pyrevit.exe and claude.exe. State lives in state.json next to this file; every call
# is appended to calls.log. Used only by the tests in this folder. Never touches real pyRevit or Claude config.
# Like the real claude.exe, 'mcp get' and 'mcp remove' on a missing server write to STDERR and exit 1.
# AM_STUB_INSTALL_NOOP=1 makes 'pyrevit mcp install claude' register nothing (forces the direct-add path).
# AM_STUB_PY7_STDERR=1 makes every pyrevit call also write one log line to STDERR (exit code unchanged),
# like a real pyrevit.exe that logs a warning. The line holds no word the setup script parses.
# The .cmd wrapper passes the raw argument line in AM_STUB_ARGS, because powershell.exe -File
# rebinds a literal '--' in its own argument list. Test paths contain no spaces.
param([string]$Kind)
$argv = @(([string]$env:AM_STUB_ARGS).Trim() -split '\s+' | Where-Object { $_ } | ForEach-Object { $_.Trim('"') })
$dir = $PSScriptRoot
if ($env:AM_STUB_DIR) { $dir = $env:AM_STUB_DIR }
$statePath = Join-Path $dir 'state.json'
$state = [IO.File]::ReadAllText($statePath) | ConvertFrom-Json
Add-Content -LiteralPath (Join-Path $dir 'calls.log') -Value ("$Kind " + ($argv -join ' '))
function Save { [IO.File]::WriteAllText($statePath, ($state | ConvertTo-Json -Depth 6), (New-Object Text.UTF8Encoding $false)) }
$line = $argv -join ' '
if ($Kind -eq 'py7') {
    if ($env:AM_STUB_PY7_STDERR -eq '1') { [Console]::Error.WriteLine('[stub log] pyrevit wrote a warning line') }
    switch -regex ($line) {
        '^--version$' { 'pyRevit CLI v7.0.0.26278'; exit 0 }
        '^configs agent$' { if ($state.enabled) { 'Agent host is Enabled' } else { 'Agent host is Disabled' }; exit 0 }
        '^configs agent enable$' { $state.enabled = $true; Save; exit 0 }
        '^configs agent disable$' { $state.enabled = $false; Save; exit 0 }
        '^configs agent policy$' { "Agent policy: $($state.policy)"; exit 0 }
        '^configs agent policy (\w+)$' { $state.policy = $Matches[1]; Save; exit 0 }
        '^mcp install claude$' {
            if ($env:AM_STUB_INSTALL_NOOP -eq '1') { exit 0 }
            $state.mcp = [pscustomobject]@{ scope = 'user'; command = $env:AM_STUB_PY7; args = 'mcp'; env = @() }; Save; exit 0
        }
        '^agent status$' { 'Agent host: enabled'; exit 0 }
        default { "stub py7: unknown command $line"; exit 9 }
    }
}
if ($Kind -eq 'claude') {
    if ($line -eq 'mcp get pyrevit') {
        if (-not $state.mcp) { [Console]::Error.WriteLine('No MCP server named "pyrevit". Configured servers: '); exit 1 }
        $scopeText = @{ user = 'User config (available in all your projects)'; local = 'Local config (private to you in this project)'; project = 'Project config (shared via .mcp.json)' }[[string]$state.mcp.scope]
        'pyrevit:'
        "  Scope: $scopeText"
        '  Status: + Connected'
        '  Type: stdio'
        "  Command: $($state.mcp.command)"
        "  Args: $($state.mcp.args)"
        '  Environment:'
        foreach ($e in @($state.mcp.env)) { if ($e) { "    $e" } }
        ''
        'To remove this server, run: claude mcp remove pyrevit -s user'
        exit 0
    }
    if ($line -match '^mcp remove pyrevit -s (\w+)$') {
        if ($state.mcp -and $state.mcp.scope -eq $Matches[1]) { $state.mcp = $null; Save; exit 0 }
        [Console]::Error.WriteLine("No MCP server named pyrevit in $($Matches[1]) scope"); exit 1
    }
    if ($argv.Count -ge 5 -and $argv[0] -eq 'mcp' -and $argv[1] -eq 'add' -and $argv[2] -eq 'pyrevit') {
        # Like the real CLI: options first, then the command and its args. Windows PowerShell 5.1 can
        # drop a literal '--' when it calls a native program, so '--' is optional here too.
        $scope = 'local'; $envs = @(); $i = 3
        while ($i -lt $argv.Count) {
            if ($argv[$i] -eq '-s') { $scope = $argv[$i + 1]; $i += 2; continue }
            if ($argv[$i] -eq '-e') { $envs += $argv[$i + 1]; $i += 2; continue }
            if ($argv[$i] -eq '--') { $i++ }
            break
        }
        $rest = @($argv[$i..($argv.Count - 1)])
        $state.mcp = [pscustomobject]@{ scope = $scope; command = $rest[0]; args = (@($rest | Select-Object -Skip 1) -join ' '); env = $envs }
        Save; exit 0
    }
    "stub claude: unknown command $line"; exit 9
}
exit 9
