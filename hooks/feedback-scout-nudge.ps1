<#
.SYNOPSIS
  archi-magic hook: about every 2 hours, ask the agent to run the feedback scout.

.DESCRIPTION
  Runs on SessionStart and on UserPromptSubmit. It reads one small state file,
  %LOCALAPPDATA%\archi-magic\feedback-scout.json, with the time of the last
  nudge (last_nudge) and the time of the last am-feedback post
  (last_feedback_post, optional). The later of the two is the anchor. When the
  interval has passed since the anchor, it writes the new nudge time and prints
  one JSON object:
    - systemMessage: a short line for the user.
    - additionalContext: tells the agent to dispatch the read-only agent
      archi-magic:am-feedback-scout in the background right away, without
      asking first, with since = the anchor. The scout only drafts; posting stays
      with the am-feedback skill after the user says "Post".
  So a manual am-feedback post inside the interval delays the next nudge, and
  the scout does not read again the window that the post covered (issue #274).

  The first run only writes the start time and prints nothing, so a new
  install does not nudge at once. Inside the interval it prints nothing.

  -MarkPost: the am-feedback skill runs this after a post. It writes
  last_feedback_post = now, keeps last_nudge, prints one line and exits 0.

  Interval: 120 minutes. Set ARCHI_MAGIC_SCOUT_INTERVAL_MIN to change it;
  0 turns the scout nudge off. Opt out of all feedback nudges with
  ARCHI_MAGIC_NO_FEEDBACK_NUDGE=1.

  Speed: one small file read and write, no transcript read, no network.
  Never blocks a session: always exit 0. Every failure goes to the shared
  archi-magic log.

  Test parameters: -HookInputJson (the hook input JSON), -StateFile,
  -NowUtc (ISO 8601).
#>
param(
    [string]$HookInputJson = '',
    [string]$StateFile = '',
    [string]$NowUtc = '',
    [switch]$MarkPost
)
$ErrorActionPreference = 'Stop'

function Write-Log([string]$Level, [string]$Message, [hashtable]$Data) {
    try {
        . (Join-Path $PSScriptRoot '..\scripts\lib\am-log.ps1')
        # Write-AmLog warns on a log failure; keep stdout clean for the hook JSON.
        Write-AmLog -Skill 'hooks' -Step 'feedback-scout-nudge' -Level $Level -Message $Message -Data $Data 3>$null
    } catch { }
}

function ConvertTo-Utc([string]$Text) {
    if (-not $Text) { return $null }
    try { return [datetime]::Parse($Text, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::AdjustToUniversal) }
    catch { return $null }
}

function Save-State([string]$Path, [datetime]$When, $LastPost) {
    $dir = Split-Path -Parent $Path
    if ($dir -and -not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    $props = [ordered]@{ last_nudge = $When.ToString('o') }
    if ($null -ne $LastPost) { $props['last_feedback_post'] = ([datetime]$LastPost).ToString('o') }
    $obj = New-Object PSObject -Property $props
    # UTF-8 without a byte-order mark.
    [IO.File]::WriteAllText($Path, ($obj | ConvertTo-Json -Compress), (New-Object Text.UTF8Encoding $false))
}

function Get-StatePath {
    if ($StateFile) { return $StateFile }
    $base = $env:LOCALAPPDATA
    if (-not $base -and $env:USERPROFILE) { $base = Join-Path $env:USERPROFILE 'AppData\Local' }
    if (-not $base) { return '' }
    return (Join-Path $base 'archi-magic\feedback-scout.json')
}

function Get-NowUtc {
    if ($NowUtc) { return (ConvertTo-Utc $NowUtc) }
    return (Get-Date).ToUniversalTime()
}

if ($MarkPost) {
    # Called by the am-feedback skill after a post. It works also when the nudge is off.
    try {
        $now = Get-NowUtc
        $path = Get-StatePath
        if (-not $path) { Write-Output 'feedback post time not saved: no LOCALAPPDATA folder'; exit 0 }
        $lastNudge = $now
        if (Test-Path -LiteralPath $path) {
            try {
                $old = ConvertTo-Utc ([string](([IO.File]::ReadAllText($path) | ConvertFrom-Json).last_nudge))
                if ($null -ne $old) { $lastNudge = $old }
            } catch { }
        }
        Save-State $path $lastNudge $now
        Write-Output ('feedback post time saved: ' + $now.ToString('o'))
        Write-Log 'info' 'Feedback post time saved' $null
    } catch {
        Write-Output 'feedback post time not saved (see the archi-magic log)'
        Write-Log 'error' ('Feedback post time not saved: ' + $_.Exception.Message) $null
    }
    exit 0
}

try {
    if ($env:ARCHI_MAGIC_NO_FEEDBACK_NUDGE -eq '1') { exit 0 }

    $interval = 120
    if ($env:ARCHI_MAGIC_SCOUT_INTERVAL_MIN) {
        $parsed = 0
        if ([int]::TryParse($env:ARCHI_MAGIC_SCOUT_INTERVAL_MIN, [ref]$parsed)) { $interval = $parsed }
        else { Write-Log 'warn' 'ARCHI_MAGIC_SCOUT_INTERVAL_MIN is not a number; using 120' $null }
    }
    if ($interval -le 0) { exit 0 }

    $hookEvent = 'UserPromptSubmit'
    if ($HookInputJson) {
        try {
            $hookInput = $HookInputJson | ConvertFrom-Json
            if ($hookInput.hook_event_name) { $hookEvent = [string]$hookInput.hook_event_name }
        } catch { }
    }
    if ($hookEvent -ne 'SessionStart' -and $hookEvent -ne 'UserPromptSubmit') { exit 0 }

    $now = Get-NowUtc
    $path = Get-StatePath
    if (-not $path) { exit 0 }

    $last = $null
    $lastPost = $null
    if (Test-Path -LiteralPath $path) {
        try {
            $state = [IO.File]::ReadAllText($path) | ConvertFrom-Json
            $last = [datetime]::Parse([string]$state.last_nudge, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::AdjustToUniversal)
            # Optional field; an unreadable post time is ignored.
            $lastPost = ConvertTo-Utc ([string]$state.last_feedback_post)
        } catch {
            Write-Log 'warn' 'Scout state unreadable; starting a new interval' $null
            $last = $null
        }
    }

    # First run, or an unreadable state file: start the clock, nudge later.
    if ($null -eq $last) {
        Save-State $path $now $lastPost
        exit 0
    }
    # The anchor is the later of the last nudge and the last am-feedback post.
    $anchor = $last
    $anchorName = 'previous nudge'
    if ($null -ne $lastPost -and $lastPost -gt $last) { $anchor = $lastPost; $anchorName = 'last am-feedback post' }
    if (($now - $anchor).TotalMinutes -lt $interval) { exit 0 }

    Save-State $path $now $lastPost
    $since = $anchor.ToString('o')
    $user = "archi-magic: $interval minutes since the last feedback scout. " +
            'A read-only scout now runs in the background over recent archi-magic sessions. You will see what is worth reporting when it is done. Nothing is posted without your "Post".'
    $ctx = "archi-magic feedback scout is due (interval $interval minutes; $anchorName $since UTC). " +
           "Dispatch the agent archi-magic:am-feedback-scout in the background now, with the input since=$since. " +
           'Do not wait for a pause and do not ask the user first: the scout is read-only and runs in parallel with the current work. ' +
           'When it returns, show the user its themes as a short list and offer to post them, ' +
           'but do not interrupt a model change, a dry run or a Keep/Discard prompt to do so. ' +
           'Post nothing yourself: if the user wants to post, use the am-feedback skill, which posts only after the user says "Post".'
    $out = [ordered]@{
        systemMessage      = $user
        hookSpecificOutput = [ordered]@{ hookEventName = $hookEvent; additionalContext = $ctx }
    }
    [Console]::Out.Write((New-Object PSObject -Property $out | ConvertTo-Json -Compress -Depth 3))
    Write-Log 'info' 'Feedback scout nudge shown' @{ event = $hookEvent; interval = $interval; anchor = $anchorName }
} catch {
    Write-Log 'error' ('Feedback scout nudge failed: ' + $_.Exception.Message) $null
}
exit 0
