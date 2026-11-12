<#
.SYNOPSIS
  archi-magic SessionEnd hook: notice a troubled session and save a feedback note.

.DESCRIPTION
  Claude Code runs this hook once when a session ends. It reads the session
  transcript (transcript_path from the hook input JSON on stdin) and looks for
  a small explicit list of failure signals only:
    - Revit connection errors: revit_busy, pipe_error, no instances found.
    - Tool failures: a tool result with is_error true, or invalid_params.
    - The user saying something is broken or wrong.
  A retry is covered by the failure signal: a retry always follows a failure.

  If no signal is found, it prints nothing and writes nothing. Quiet sessions
  stay quiet.

  If a signal is found, it writes one small JSON note to
  %LOCALAPPDATA%\archi-magic\session-feedback.json (override with
  -FeedbackFile; test use only). It prints nothing: Claude Code discards a
  SessionEnd hook's JSON output (systemMessage included), so the note is shown
  privately by the SessionStart hook (hooks/session-start-feedback.ps1).
  The feedback workflow claims it only when relevant. Nothing is ever filed
  automatically.

  Speed: a SessionEnd hook from a plugin gets about 1.5 seconds (the plugin
  timeout in hooks.json cannot raise that budget). This script reads only the
  last 256 KB of the transcript and makes one regex pass. It never uses the
  network. The transcript is written in the background and may lag the last
  turn, so a signal in the final turn can be missed; that is fine, the hook
  is a hint, not a record.

  Never blocks a session: always exit 0. Every failure goes to the shared
  archi-magic log. Opt out: set ARCHI_MAGIC_NO_FEEDBACK_NUDGE=1.

  Test parameters: -HookInputJson (the hook input JSON), -TranscriptPath,
  -FeedbackFile, -TailBytes.
#>
param(
    [string]$HookInputJson = '',
    [string]$TranscriptPath = '',
    [string]$FeedbackFile = '',
    [int]$TailBytes = 262144
)
$ErrorActionPreference = 'Stop'

function Write-Log([string]$Level, [string]$Message, [hashtable]$Data) {
    try {
        . (Join-Path $PSScriptRoot '..\scripts\lib\am-log.ps1')
        # Write-AmLog warns on a log failure; keep stdout clean for the hook JSON.
        Write-AmLog -Skill 'hooks' -Step 'session-end-feedback' -Level $Level -Message $Message -Data $Data 3>$null
    } catch { }
}

try {
    if ($env:ARCHI_MAGIC_NO_FEEDBACK_NUDGE -eq '1') { exit 0 }

    $sessionId = ''
    if (-not $TranscriptPath) {
        if (-not $HookInputJson) { exit 0 }
        try { $hookInput = $HookInputJson | ConvertFrom-Json } catch { exit 0 }
        $TranscriptPath = [string]$hookInput.transcript_path
        $sessionId = [string]$hookInput.session_id
    }
    if (-not $TranscriptPath -or -not (Test-Path -LiteralPath $TranscriptPath)) { exit 0 }

    # Read only the tail: a plugin SessionEnd hook gets about 1.5 seconds.
    $tail = ''
    try {
        $fs = New-Object System.IO.FileStream($TranscriptPath, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
        try {
            $start = [Math]::Max(0, $fs.Length - $TailBytes)
            [void]$fs.Seek($start, [System.IO.SeekOrigin]::Begin)
            $reader = New-Object System.IO.StreamReader($fs, [System.Text.Encoding]::UTF8)
            $tail = $reader.ReadToEnd()
            $reader.Close()
        } finally { $fs.Close() }
    } catch {
        Write-Log 'warn' ('Feedback scan: transcript not read: ' + $_.Exception.Message) $null
        exit 0
    }

    # One pass over the tail lines. -match is case-insensitive by default.
    $found = New-Object System.Collections.Generic.List[string]
    $toolActivities = @{}
    $failureActivities = @{}
    $failureTimes = @{}
    foreach ($line in ($tail -split "`r?`n")) {
        if ($line -match 'note_sha256.{0,12}[0-9a-f]{64}') { continue }
        if ($line -match '"type"\s*:\s*"tool_(use|result)"') {
            $event = $null
            try { $event = $line | ConvertFrom-Json } catch { }
            foreach ($block in @($event.message.content)) {
                if ($block.type -eq 'tool_use' -and $block.id) {
                    switch -Regex ([string]$block.name) {
                        '(^|__)run_query$' { $toolActivities[$block.id] = 'revit_query' }
                        '(^|__)run_modify$' { $toolActivities[$block.id] = 'revit_modify' }
                        '(^|__)(get_context|list_revit_instances)$' { $toolActivities[$block.id] = 'revit_context' }
                    }
                } elseif ($block.type -eq 'tool_result') {
                    $content = $block.content | ConvertTo-Json -Compress -Depth 4
                    $activity = if ($block.tool_use_id) { $toolActivities[$block.tool_use_id] }
                    $stamp = [DateTimeOffset]::MinValue
                    $timestamp = $event.timestamp
                    if ($timestamp -is [DateTime] -and $timestamp.Kind -ne [DateTimeKind]::Unspecified) {
                        $stamp = [DateTimeOffset]$timestamp
                    } elseif ($timestamp -is [DateTimeOffset]) {
                        $stamp = $timestamp
                    } elseif ($timestamp -is [string] -and $timestamp -match '(Z|[+-][0-9]{2}:[0-9]{2})$') {
                        [void][DateTimeOffset]::TryParse($timestamp, [ref]$stamp)
                    }
                    $matched = @(
                        if ($content -match 'pipe_error') { 'pipe_error' }
                        if ($content -match 'revit_busy') { 'revit_busy' }
                        if ($content -match 'no instances found') { 'no_instances_found' }
                        if ($block.is_error -eq $true -or $content -match 'invalid_params') { 'tool_failed' }
                    )
                    foreach ($signal in $matched) {
                        if ($stamp -ne [DateTimeOffset]::MinValue -and
                            (-not $failureTimes.ContainsKey($signal) -or $stamp -gt $failureTimes[$signal])) {
                            $failureTimes[$signal] = $stamp
                            $failureActivities[$signal] = $activity
                        } elseif (-not $failureTimes.ContainsKey($signal) -and $activity) {
                            $failureActivities[$signal] = $activity
                        }
                    }
                }
            }
        }
        if (($line -match 'revit_busy') -and -not $found.Contains('revit_busy')) { $found.Add('revit_busy') }
        if (($line -match 'pipe_error') -and -not $found.Contains('pipe_error')) { $found.Add('pipe_error') }
        if (($line -match 'no instances found') -and -not $found.Contains('no_instances_found')) { $found.Add('no_instances_found') }
        if ((($line -match '"is_error"\s*:\s*true') -or ($line -match 'invalid_params')) -and -not $found.Contains('tool_failed')) { $found.Add('tool_failed') }
        if (($line -match '"role"\s*:\s*"user"') -and ($line -match '\bbroken\b|\bwrong\b|\bbug\b|crash|doesn''t work|does not work|not working') -and -not $found.Contains('user_reported_broken')) { $found.Add('user_reported_broken') }
    }
    if ($found.Count -eq 0) { exit 0 }

    $titles = [ordered]@{
        pipe_error = 'Revit connection dropped (pipe_error)'
        revit_busy = 'Revit was busy (revit_busy) and blocked the session'
        no_instances_found = 'No Revit instances found'
        tool_failed = 'A tool call failed'
        user_reported_broken = 'Something felt broken in the session'
    }
    $primary = @($titles.Keys | Where-Object { $found.Contains($_) })[0]
    $title = $titles[$primary]
    $activity = $failureActivities[$primary]
    if (-not $activity) { $activity = 'unknown' }

    $when = (Get-Date).ToUniversalTime().ToString('o')
    $incident = if ($failureTimes.ContainsKey($primary)) { $failureTimes[$primary].ToUniversalTime().ToString('o') }
    $body = "Signals seen at the end of the session: " + ($found -join ', ') + ".`n" +
            "Session ended (UTC): $when.`n" +
            "File it at https://github.com/zsenarchitect/archi-magic/issues/new or with: gh issue create --repo zsenarchitect/archi-magic --title `"$title`""

    if (-not $FeedbackFile) {
        $base = $env:LOCALAPPDATA
        if (-not $base) { $base = Join-Path $env:USERPROFILE 'AppData\Local' }
        if (-not $base) { exit 0 }
        $FeedbackFile = Join-Path $base 'archi-magic\session-feedback.json'
    }
    $note = [ordered]@{
        signals    = @($found)
        activity   = $activity
        title      = $title
        body       = $body
        session_id = $sessionId
        ended_at   = $when
        occurred_at = $incident
        transcript = $TranscriptPath
    }
    try {
        $dir = Split-Path -Parent $FeedbackFile
        if ($dir -and -not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
        [IO.File]::WriteAllText($FeedbackFile, (New-Object PSObject -Property $note | ConvertTo-Json -Compress -Depth 3))
    } catch {
        Write-Log 'warn' ('Feedback note not written: ' + $_.Exception.Message) $null
        exit 0
    }
    Write-Log 'info' 'Feedback note saved' @{ signals = ($found -join ',') }
} catch {
    Write-Log 'error' ('Feedback scan failed: ' + $_.Exception.Message) $null
}
exit 0
