<#
.SYNOPSIS
  Tests for hooks/session-end-feedback.ps1. Hermetic: temp transcript files,
  temp feedback file and log folders. No network, no real session.

  powershell -NoProfile -ExecutionPolicy Bypass -File hooks\tests\test-session-end-feedback.ps1
  Exit 0 = all cases pass.
#>
$ErrorActionPreference = 'Stop'
$HostExe = (Get-Process -Id $PID).Path
$Hook = (Resolve-Path (Join-Path $PSScriptRoot '..\session-end-feedback.ps1')).Path
$Root = Join-Path ([IO.Path]::GetTempPath()) ("am-session-end-feedback-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
$script:Fail = 0; $script:Asserts = 0

function Invoke-Hook([string]$transcript, [string]$feedback, [string[]]$extra = @()) {
    $out = & $HostExe -NoProfile -ExecutionPolicy Bypass -File $Hook -TranscriptPath $transcript -FeedbackFile $feedback @extra 2>&1 | Out-String
    return @{ Code = $LASTEXITCODE; Out = $out.Trim() }
}
function Invoke-HookJson([string]$json, [string]$feedback) {
    # Call the hook the way hooks/hooks.json does: hook JSON on stdin, script called in-process.
    # Passing the JSON as a native argument loses its double quotes under Windows PowerShell 5.1 (#67).
    $cmd = "`$in = [Console]::In.ReadToEnd(); & '$Hook' -HookInputJson `$in -FeedbackFile '$feedback'; exit `$LASTEXITCODE"
    $out = $json | & $HostExe -NoProfile -ExecutionPolicy Bypass -Command $cmd 2>&1 | Out-String
    return @{ Code = $LASTEXITCODE; Out = $out.Trim() }
}
function Assert-True([string]$name, [bool]$ok, [string]$detail) {
    $script:Asserts++
    if ($ok) { Write-Host "PASS $name" } else { $script:Fail++; Write-Host "FAIL $name : $detail" }
}
function Write-Transcript([string]$name, [string[]]$lines) {
    $p = Join-Path $Root $name
    [IO.File]::WriteAllLines($p, $lines)
    return $p
}
function Read-Note([string]$feedback) {
    if (-not (Test-Path -LiteralPath $feedback)) { return $null }
    return [IO.File]::ReadAllText($feedback) | ConvertFrom-Json
}
function Get-LogText { (Get-ChildItem (Join-Path $Root 'logs') -Filter *.jsonl -ErrorAction SilentlyContinue | Get-Content -Raw) -join '' }

$quietUser = '{"type":"user","message":{"role":"user","content":"How many rooms are on Level 1?"}}'
$quietTool = '{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_result","content":"42 rooms","is_error":false}]}}'

try {
    New-Item -ItemType Directory -Force $Root | Out-Null
    $env:ARCHI_MAGIC_LOG_DIR = Join-Path $Root 'logs'

    # 1. Quiet session: no signals, no file, exit 0, no output.
    $t = Write-Transcript 'quiet.jsonl' @($quietUser, $quietTool)
    $f = Join-Path $Root 'note1.json'
    $r = Invoke-Hook $t $f
    Assert-True 'quiet: exit 0' ($r.Code -eq 0) "exit $($r.Code)"
    Assert-True 'quiet: no output' ($r.Out -eq '') "out '$($r.Out)'"
    Assert-True 'quiet: no note file' (-not (Test-Path -LiteralPath $f)) 'note written'

    # 2. pipe_error: note written, signal named, exit 0, no output.
    $t = Write-Transcript 'pipe.jsonl' @($quietUser, '{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_result","content":"pipe_error: connection lost","is_error":true}]}}')
    $f = Join-Path $Root 'note2.json'
    $r = Invoke-Hook $t $f
    $n = Read-Note $f
    Assert-True 'pipe_error: exit 0, no output' ($r.Code -eq 0 -and $r.Out -eq '') "exit $($r.Code) out '$($r.Out)'"
    Assert-True 'pipe_error: signal named' ($n -and $n.signals -contains 'pipe_error') ''
    Assert-True 'pipe_error: title names pipe_error' ($n -and $n.title -match 'pipe_error') ''
    Assert-True 'pipe_error: body names the repo' ($n -and $n.body -match 'zsenarchitect/archi-magic') ''

    # 3. revit_busy: signal named.
    $t = Write-Transcript 'busy.jsonl' @('{"type":"user","message":{"role":"user","content":[{"type":"tool_result","content":"revit_busy after 30 s","is_error":true}]}}')
    $f = Join-Path $Root 'note3.json'
    $r = Invoke-Hook $t $f
    $n = Read-Note $f
    Assert-True 'revit_busy: signal named' ($n -and $n.signals -contains 'revit_busy') ''
    Assert-True 'revit_busy: title names revit_busy' ($n -and $n.title -match 'revit_busy') ''

    # 4. No instances found: signal named.
    $t = Write-Transcript 'noinst.jsonl' @('{"type":"user","message":{"role":"user","content":[{"type":"tool_result","content":"list_revit_instances found no instances found","is_error":true}]}}')
    $f = Join-Path $Root 'note4.json'
    $r = Invoke-Hook $t $f
    $n = Read-Note $f
    Assert-True 'no instances: signal named' ($n -and $n.signals -contains 'no_instances_found') ''

    # 5. Tool failure via is_error, and via invalid_params.
    $t = Write-Transcript 'toolerr.jsonl' @('{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_result","content":"boom","is_error":true}]}}')
    $f = Join-Path $Root 'note5.json'
    $r = Invoke-Hook $t $f
    $n = Read-Note $f
    Assert-True 'is_error: tool_failed named' ($n -and $n.signals -contains 'tool_failed') ''
    $t = Write-Transcript 'invalid.jsonl' @('{"type":"user","message":{"role":"user","content":[{"type":"tool_result","content":"run_query returned invalid_params","is_error":true}]}}')
    $f = Join-Path $Root 'note5b.json'
    $r = Invoke-Hook $t $f
    $n = Read-Note $f
    Assert-True 'invalid_params: tool_failed named' ($n -and $n.signals -contains 'tool_failed') ''

    # 6. User words count only on user lines: assistant saying it does not count.
    $t = Write-Transcript 'userwords.jsonl' @('{"type":"user","message":{"role":"user","content":"This does not work, the schedule is wrong"}}')
    $f = Join-Path $Root 'note6.json'
    $r = Invoke-Hook $t $f
    $n = Read-Note $f
    Assert-True 'user words: signal named' ($n -and $n.signals -contains 'user_reported_broken') ''
    $t = Write-Transcript 'assistantwords.jsonl' @('{"type":"assistant","message":{"role":"assistant","content":"That approach does not work, try another one"}}')
    $f = Join-Path $Root 'note6b.json'
    $r = Invoke-Hook $t $f
    Assert-True 'assistant words: no note' (-not (Test-Path -LiteralPath $f)) 'note written'

    # 7. Missing transcript: exit 0, no file.
    $f = Join-Path $Root 'note7.json'
    $r = Invoke-Hook (Join-Path $Root 'does-not-exist.jsonl') $f
    Assert-True 'missing transcript: exit 0, no note' ($r.Code -eq 0 -and -not (Test-Path -LiteralPath $f)) "exit $($r.Code)"

    # 8. Hook input JSON: transcript_path is read from stdin JSON.
    $t = Write-Transcript 'viajson.jsonl' @('{"type":"user","message":{"role":"user","content":[{"type":"tool_result","content":"pipe_error","is_error":true}]}}')
    $f = Join-Path $Root 'note8.json'
    $json = "{`"session_id`":`"s1`",`"transcript_path`":`"$($t.Replace('\', '\\'))`",`"hook_event_name`":`"SessionEnd`"}"
    $r = Invoke-HookJson $json $f
    $n = Read-Note $f
    Assert-True 'hook JSON: transcript read' ($n -and $n.signals -contains 'pipe_error') ''
    Assert-True 'hook JSON: session id kept' ($n -and $n.session_id -eq 's1') ''

    # 9. Malformed hook JSON: exit 0, no file.
    $f = Join-Path $Root 'note9.json'
    $r = Invoke-HookJson 'not json' $f
    Assert-True 'bad hook JSON: exit 0, no note' ($r.Code -eq 0 -and -not (Test-Path -LiteralPath $f)) "exit $($r.Code)"

    # 10. Opt-out: signals present, still no note.
    $env:ARCHI_MAGIC_NO_FEEDBACK_NUDGE = '1'
    $t = Write-Transcript 'optout.jsonl' @('{"type":"user","message":{"role":"user","content":[{"type":"tool_result","content":"pipe_error","is_error":true}]}}')
    $f = Join-Path $Root 'note10.json'
    $r = Invoke-Hook $t $f
    Remove-Item Env:\ARCHI_MAGIC_NO_FEEDBACK_NUDGE
    Assert-True 'opt-out: no note' (-not (Test-Path -LiteralPath $f)) 'note written'

    # 11. Tail only: a signal before the tail window is not seen.
    $head = 'x' * 300
    $t = Write-Transcript 'tail.jsonl' @("pipe_error $head", $quietUser, $quietTool)
    $f = Join-Path $Root 'note11.json'
    $r = Invoke-Hook $t $f @('-TailBytes', '100')
    Assert-True 'tail window: head signal missed' (-not (Test-Path -LiteralPath $f)) 'note written'
    $t = Write-Transcript 'tail2.jsonl' @($quietUser, 'pipe_error at the tail')
    $f = Join-Path $Root 'note11b.json'
    $r = Invoke-Hook $t $f @('-TailBytes', '100')
    $n = Read-Note $f
    Assert-True 'tail window: tail signal seen' ($n -and $n.signals -contains 'pipe_error') ''

    $t = Write-Transcript 'activity.jsonl' @(
        '{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","id":"r1","name":"mcp__pyrevit__run_modify","input":{"model":"Private project"}}]}}',
        '{"type":"user","timestamp":"2026-11-06T15:38:47-05:00","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"r1","is_error":true,"content":"revit_busy"}]}}',
        '{"type":"user","timestamp":"2026-11-06T19:00:00Z","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"unknown","is_error":true,"content":"revit_busy"}]}}'
    )
    $f = Join-Path $Root 'activity.json'
    $r = Invoke-Hook $t $f
    $n = Read-Note $f
    Assert-True 'activity: failing tool is captured without its input' (
        $n.activity -eq 'revit_modify' -and $n.body -notmatch 'Private project'
    ) $r.Out
    Assert-True 'activity: capture the actual incident time in UTC' (
        $n.occurred_at -and [DateTimeOffset]$n.occurred_at -eq [DateTimeOffset]'2026-11-06T20:38:47Z' -and
        [DateTimeOffset]$n.ended_at -ne [DateTimeOffset]$n.occurred_at
    ) ($n | ConvertTo-Json -Compress)
    $f2 = Join-Path $Root 'activity-after-resume.json'
    $r = Invoke-Hook $t $f2
    $n2 = Read-Note $f2
    Assert-True 'activity: reopening the same incident keeps the incident time' (
        $n2.occurred_at -eq $n.occurred_at
    ) ($n2 | ConvertTo-Json -Compress)

    $t = Write-Transcript 'prior-note.jsonl' @('{"type":"user","message":{"role":"user","content":[{"type":"tool_result","content":"note_sha256: 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef, revit_busy"}]}}')
    $f = Join-Path $Root 'prior-note.json'
    $r = Invoke-Hook $t $f
    Assert-True 'reading saved feedback does not create a new note' (-not (Test-Path -LiteralPath $f)) $r.Out

    $t = Write-Transcript 'unrelated-tool.jsonl' @(
        '{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","id":"s1","name":"Bash","input":{"command":"cat missing.txt"}}]}}',
        '{"type":"user","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"s1","is_error":true,"content":"cat: missing.txt: No such file"}]}}'
    )
    $f = Join-Path $Root 'unrelated-tool.json'
    $r = Invoke-Hook $t $f
    Assert-True 'an unrelated tool failure does not create a Revit feedback note' (
        $r.Code -eq 0 -and -not (Test-Path -LiteralPath $f)
    ) $r.Out

    $t = Write-Transcript 'recap-of-filed-incident.jsonl' @(
        '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"Issue #200 covered the earlier revit_busy error."}]}}'
    )
    $f = Join-Path $Root 'recap-of-filed-incident.json'
    $r = Invoke-Hook $t $f
    Assert-True 'assistant recap of a filed error does not create a new note' (
        $r.Code -eq 0 -and -not (Test-Path -LiteralPath $f)
    ) $r.Out

    # 12. A note is logged.
    Assert-True 'note saved: logged' ((Get-LogText) -match '"step":"session-end-feedback","level":"info"') (Get-LogText)
} finally {
    Remove-Item Env:\ARCHI_MAGIC_LOG_DIR -ErrorAction SilentlyContinue
    Remove-Item Env:\ARCHI_MAGIC_NO_FEEDBACK_NUDGE -ErrorAction SilentlyContinue
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $Root
}

Write-Host ("{0} assertion(s), {1} failure(s)" -f $script:Asserts, $script:Fail)
if ($script:Asserts -eq 0) { Write-Host 'FAIL: no assertions ran'; exit 1 }
if ($script:Fail -gt 0) { exit 1 }
exit 0
