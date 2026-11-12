<#
.SYNOPSIS
  Expose pending feedback privately at SessionStart. Leave unrelated sessions quiet.

.DESCRIPTION
  With no switch, give the agent a static notice. Do not show a user message or
  consume the note. The am-feedback skill reads it only for relevant feedback.
  -Read prints the sanitized note and its digest. -Claim consumes only the
  matching note, after the GitHub duplicate check and before the one offer.
  Claim uses a same-directory rename so two sessions cannot offer the same note.
  A local receipt keeps the same timed incident or identical legacy note quiet.
  The SessionStart path never contacts GitHub and always exits 0.
#>
param(
    [string]$FeedbackFile = '',
    [switch]$Read,
    [string]$Claim = ''
)
$ErrorActionPreference = 'Stop'

function Write-Log([string]$Level, [string]$Message, [hashtable]$Data) {
    try {
        . (Join-Path $PSScriptRoot '..\scripts\lib\am-log.ps1')
        # Write-AmLog warns on a log failure; keep stdout clean for the hook JSON.
        Write-AmLog -Skill 'hooks' -Step 'session-start-feedback' -Level $Level -Message $Message -Data $Data 3>$null
    } catch { }
}

function Get-FeedbackDigest([byte[]]$Bytes) {
    $hash = [Security.Cryptography.SHA256]::Create()
    try { return [BitConverter]::ToString($hash.ComputeHash($Bytes)).Replace('-', '').ToLowerInvariant() }
    finally { $hash.Dispose() }
}

function Get-ReceiptPath([string]$Path, $Note) {
    $key = $Note.note_sha256
    if ($Note.incident_line) {
        $key = Get-FeedbackDigest ([Text.Encoding]::UTF8.GetBytes($Note.incident_line))
    }
    return "$Path.seen-$key"
}

function Register-Incident([string]$ReceiptPath) {
    try {
        $stream = [IO.File]::Open($ReceiptPath, [IO.FileMode]::CreateNew)
    } catch [IO.IOException] {
        if ([IO.File]::Exists($ReceiptPath)) { return $false }
        throw
    }
    $stream.Dispose()
    return $true
}

function Read-SavedNote([string]$Path) {
    if ((Get-Item -LiteralPath $Path).Length -gt 65536) { throw 'Feedback note exceeds 64 KiB.' }
    $bytes = [IO.File]::ReadAllBytes($Path)
    if ($bytes.Length -gt 65536) { throw 'Feedback note exceeds 64 KiB.' }
    $raw = [Text.Encoding]::UTF8.GetString($bytes).TrimStart([char]0xfeff)
    $note = $raw | ConvertFrom-Json
    $titles = [ordered]@{
        pipe_error = 'Revit connection dropped (pipe_error)'
        revit_busy = 'Revit was busy (revit_busy) and blocked the session'
        no_instances_found = 'No Revit instances found'
        tool_failed = 'A tool call failed'
        user_reported_broken = 'Something felt broken in the session'
    }
    $signals = @($titles.Keys | Where-Object { @($note.signals) -ccontains $_ })
    if ($signals.Count -eq 0 -or -not $note.ended_at) { throw 'Feedback signal or date is missing.' }
    $date = [DateTimeOffset]$note.ended_at
    $incident = $null
    if ($note.occurred_at) {
        $occurred = [DateTimeOffset]$note.occurred_at
        $incident = $occurred.ToUniversalTime().ToString('o')
    }
    $activity = switch -CaseSensitive ([string]$note.activity) {
        'revit_query' { 'The session tried a read-only Revit query.' }
        'revit_modify' { 'The session tried a Revit modification.' }
        'revit_context' { 'The session checked Revit availability or context.' }
        default { 'The saved note does not identify the operation.' }
    }
    return [ordered]@{
        activity = $activity
        title = $titles[$signals[0]]
        signals = $signals
        ended_at = $date.ToUniversalTime().ToString('o')
        occurred_at = $incident
        incident_line = if ($incident) { 'Archi Magic incident: ' + $signals[0] + ' at ' + $incident }
        note_sha256 = Get-FeedbackDigest $bytes
    }
}

function Write-Json($Record) {
    [Console]::Out.Write(($Record | ConvertTo-Json -Compress -Depth 4))
}

try {
    if ($Read -and $Claim) { throw 'Use either Read or Claim.' }
    if ($Claim -and $Claim -cnotmatch '^[0-9a-f]{64}$') { throw 'Use the exact SHA-256 returned by Read.' }
    if (-not $FeedbackFile) {
        $base = $env:LOCALAPPDATA
        if (-not $base -and $env:USERPROFILE) { $base = Join-Path $env:USERPROFILE 'AppData\Local' }
        if (-not $base) { exit 0 }
        $FeedbackFile = Join-Path $base 'archi-magic\session-feedback.json'
    }
    if (-not (Test-Path -LiteralPath $FeedbackFile -PathType Leaf)) { exit 0 }
    if ($env:ARCHI_MAGIC_NO_FEEDBACK_NUDGE -eq '1') {
        Remove-Item -Force -LiteralPath $FeedbackFile
        exit 0
    }

    if ($Claim) {
        $claimed = $FeedbackFile + '.claim-' + [Guid]::NewGuid().ToString('N')
        [IO.File]::Move($FeedbackFile, $claimed)
        $consumed = $false
        $offer = $false
        try {
            $note = Read-SavedNote $claimed
            if ($note.note_sha256 -ceq $Claim) {
                $receipt = Get-ReceiptPath $FeedbackFile $note
                $offer = Register-Incident $receipt
                $consumed = $true
            }
        } finally {
            if ($consumed) {
                [IO.File]::Delete($claimed)
            } else {
                try { [IO.File]::Move($claimed, $FeedbackFile) }
                catch { Write-Log 'warn' 'Feedback claim could not be restored; retained a local recovery file' $null }
            }
        }
        if ($offer) {
            Write-Json @{ status = 'claimed'; note_sha256 = $Claim }
            Write-Log 'info' 'Feedback note claimed' $null
        }
        exit 0
    }

    $note = Read-SavedNote $FeedbackFile
    $receipt = Get-ReceiptPath $FeedbackFile $note
    if ([IO.File]::Exists($receipt)) { exit 0 }
    if ($Read) {
        Write-Json $note
        exit 0
    }

    Write-Json @{
        hookSpecificOutput = @{
            hookEventName = 'SessionStart'
            additionalContext = 'A local archi-magic feedback note is pending. Leave it alone in unrelated sessions. If the user brings up that same incident or asks for feedback about it, follow the Saved session feedback section of am-feedback. Do not ask a filing question at session start.'
        }
    }
} catch {
    Write-Log 'warn' ('Feedback note not used: ' + $_.Exception.Message) $null
}
exit 0
