<#
.SYNOPSIS
  Tests for the pending feedback offer and its one-time claim. No network.
#>
$ErrorActionPreference = 'Stop'
$Hook = (Resolve-Path (Join-Path $PSScriptRoot '..\session-start-feedback.ps1')).Path
$HostExe = (Get-Process -Id $PID).Path
$Root = Join-Path ([IO.Path]::GetTempPath()) ("am-session-start-feedback-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
$script:Fail = 0
$script:Asserts = 0

function Invoke-Hook([string]$Feedback, [string[]]$Arguments = @()) {
    $out = & $HostExe -NoProfile -ExecutionPolicy Bypass -File $Hook -FeedbackFile $Feedback @Arguments 2>&1 | Out-String
    return @{ Code = $LASTEXITCODE; Out = $out.Trim() }
}
function Assert-True([string]$Name, [bool]$OK, [string]$Detail) {
    $script:Asserts++
    if ($OK) { Write-Host "PASS $Name" }
    else { $script:Fail++; Write-Host "FAIL $Name : $Detail" }
}
function Write-Note([string]$Name, [string]$Signal, [string]$Date) {
    $path = Join-Path $Root $Name
    $note = @{
        signals = @($Signal, 'unrecognized-signal')
        activity = 'revit_modify'
        title = 'Private title'
        body = 'Private issue body'
        ended_at = $Date
        transcript = 'C:\private\transcript.jsonl'
    }
    [IO.File]::WriteAllText($path, ($note | ConvertTo-Json -Compress))
    return $path
}

try {
    New-Item -ItemType Directory -Force $Root | Out-Null
    $env:ARCHI_MAGIC_LOG_DIR = Join-Path $Root 'logs'
    $result = Invoke-Hook (Join-Path $Root 'missing.json')
    Assert-True 'no note: exit 0, no output' ($result.Code -eq 0 -and $result.Out -eq '') $result.Out

    $file = Write-Note 'note.json' 'revit_busy' '2026-11-06T20:38:47.8193309Z'
    $result = Invoke-Hook $file
    $notice = $result.Out | ConvertFrom-Json
    Assert-True 'startup: no user-facing question' ($result.Code -eq 0 -and -not $notice.systemMessage) $result.Out
    Assert-True 'startup: static private notice, no saved incident data' (
        $notice.hookSpecificOutput.hookEventName -eq 'SessionStart' -and
        $notice.hookSpecificOutput.additionalContext -match 'Leave it alone in unrelated sessions' -and
        $result.Out -notmatch 'revit_busy|Private|transcript'
    ) $result.Out
    Assert-True 'startup: leaves note available' (Test-Path -LiteralPath $file) 'pending file missing'

    $result = Invoke-Hook $file @('-Read')
    $note = $result.Out | ConvertFrom-Json
    Assert-True 'read: shows allowlisted signal, title, exact event time and digest' (
        $note.title -match 'revit_busy' -and @($note.signals).Count -eq 1 -and
        $note.note_sha256 -cmatch '^[0-9a-f]{64}$' -and
        [DateTimeOffset]$note.ended_at -eq [DateTimeOffset]'2026-11-06T20:38:47.8193309Z'
    ) $result.Out
    Assert-True 'read: reports the saved activity' ($note.activity -eq 'The session tried a Revit modification.') $result.Out
    Assert-True 'read: omits private body and transcript' ($result.Out -notmatch 'Private|transcript') $result.Out
    Assert-True 'read: does not claim note' (Test-Path -LiteralPath $file) 'pending file missing'
    Assert-True 'legacy note: do not present the session end as the incident time' (
        -not $note.occurred_at -and -not $note.incident_line
    ) $result.Out

    $incidentFile = Write-Note 'incident.json' 'revit_busy' '2026-11-07T02:00:00Z'
    $incident = [IO.File]::ReadAllText($incidentFile) | ConvertFrom-Json
    $incident | Add-Member -NotePropertyName occurred_at -NotePropertyValue '2026-11-06T15:38:47-05:00'
    [IO.File]::WriteAllText($incidentFile, ($incident | ConvertTo-Json -Compress))
    $incidentResult = Invoke-Hook $incidentFile @('-Read')
    $incidentRead = $incidentResult.Out | ConvertFrom-Json
    Assert-True 'read: stable incident line uses event time in UTC' (
        $incidentRead.incident_line -ceq 'Archi Magic incident: revit_busy at 2026-11-06T20:38:47.0000000+00:00'
    ) $incidentResult.Out
    $firstIncidentClaim = Invoke-Hook $incidentFile @('-Claim', $incidentRead.note_sha256)
    Assert-True 'first incident claim permits one offer' (
        ($firstIncidentClaim.Out | ConvertFrom-Json).status -eq 'claimed'
    ) $firstIncidentClaim.Out
    $incident.ended_at = '2026-11-08T02:00:00Z'
    [IO.File]::WriteAllText($incidentFile, ($incident | ConvertTo-Json -Compress))
    $repeat = Invoke-Hook $incidentFile
    Assert-True 'resumed incident does not announce another pending note' ($repeat.Out -eq '') $repeat.Out
    $repeat = Invoke-Hook $incidentFile @('-Read')
    Assert-True 'resumed incident does not return a second offer' ($repeat.Out -eq '') $repeat.Out
    $resumedDigest = (Get-FileHash -LiteralPath $incidentFile -Algorithm SHA256).Hash.ToLowerInvariant()
    $repeat = Invoke-Hook $incidentFile @('-Claim', $resumedDigest)
    Assert-True 'a second claim consumes the duplicate without offering it' (
        $repeat.Out -eq '' -and -not (Test-Path -LiteralPath $incidentFile)
    ) $repeat.Out
    $receipts = @(Get-ChildItem -LiteralPath $Root -Filter 'incident.json.seen-*')
    Assert-True 'the one-time receipt contains no incident details' (
        $receipts.Count -eq 1 -and $receipts[0].Length -eq 0
    ) ($receipts | Out-String)
    $incident.occurred_at = '2026-11-07T15:38:47-05:00'
    [IO.File]::WriteAllText($incidentFile, ($incident | ConvertTo-Json -Compress))
    $otherIncident = (Invoke-Hook $incidentFile @('-Read')).Out | ConvertFrom-Json
    Assert-True 'a later event with the same signal is still eligible' (
        $otherIncident.incident_line -ceq 'Archi Magic incident: revit_busy at 2026-11-07T20:38:47.0000000+00:00'
    ) ($otherIncident | ConvertTo-Json -Compress)

    $file = Write-Note 'note.json' 'pipe_error' '2026-11-12T18:00:00Z'
    $result = Invoke-Hook $file @('-Claim', $note.note_sha256)
    Assert-True 'stale claim: does not consume the newer note' ($result.Out -eq '' -and (Test-Path -LiteralPath $file)) $result.Out
    $note = (Invoke-Hook $file @('-Read')).Out | ConvertFrom-Json
    Assert-True 'stale claim: newer note is still readable' ($note.title -match 'pipe_error') $note.title

    $result = Invoke-Hook $file @('-Claim', $note.note_sha256)
    $claim = $result.Out | ConvertFrom-Json
    Assert-True 'matching claim: confirms exact digest' (
        $claim.status -eq 'claimed' -and $claim.note_sha256 -ceq $note.note_sha256
    ) $result.Out
    Assert-True 'matching claim: pending file is gone' (-not (Test-Path -LiteralPath $file)) 'pending file remains'
    $result = Invoke-Hook $file @('-Claim', $note.note_sha256)
    Assert-True 'same note cannot be claimed twice' ($result.Code -eq 0 -and $result.Out -eq '') $result.Out
    $result = Invoke-Hook $file
    Assert-True 'next startup is quiet after dismissal or offer' ($result.Code -eq 0 -and $result.Out -eq '') $result.Out

    $file = Join-Path $Root 'corrupt.json'
    [IO.File]::WriteAllText($file, '{not json')
    $result = Invoke-Hook $file
    Assert-True 'corrupt note never shows a prompt' ($result.Code -eq 0 -and $result.Out -eq '') $result.Out

    $env:ARCHI_MAGIC_NO_FEEDBACK_NUDGE = '1'
    $file = Write-Note 'optout.json' 'tool_failed' '2026-11-12T18:00:00Z'
    $result = Invoke-Hook $file
    Assert-True 'opt out: quiet and clears note' ($result.Out -eq '' -and -not (Test-Path -LiteralPath $file)) $result.Out
} finally {
    Remove-Item Env:\ARCHI_MAGIC_LOG_DIR -ErrorAction SilentlyContinue
    Remove-Item Env:\ARCHI_MAGIC_NO_FEEDBACK_NUDGE -ErrorAction SilentlyContinue
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $Root
}

Write-Host ("{0} assertion(s), {1} failure(s)" -f $script:Asserts, $script:Fail)
if ($script:Asserts -eq 0 -or $script:Fail -gt 0) { exit 1 }
exit 0
