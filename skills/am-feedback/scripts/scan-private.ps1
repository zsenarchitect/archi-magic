<#
.SYNOPSIS
  Private-data scan for am-feedback drafts, for computers without Python (issue #273).

.DESCRIPTION
  The same scan as scan_private.py. Both scripts read the built-in patterns from
  private-patterns.txt next to this script, so no agent copies a backslash through
  a shell. Before the scan, a self-test proves that every pattern finds its own
  test line.

  Inputs:
    -TermsFile       one fixed term per line (case-insensitive, not a regex),
                     for example the project and client names of the session.
    -TermsRegexFile  one regular expression per line (the local deny list format).
    The local deny list (ARCHI_MAGIC_PRIVATE_TERMS, else
    %LOCALAPPDATA%\archi-magic\private-terms.txt) is read as regexes when it
    exists, unless -NoLocalTerms is given. Blank lines and lines that start
    with "#" are skipped.
    All other arguments are the draft files.

  Output and exit code (not the grep convention):
    <file>:<line>: <pattern name>   one line for each hit
    SCAN CLEAN: ...   exit 0  read every file, self-test passed, no hit
    SCAN HITS: ...    exit 1  at least one hit; fix the draft and scan again
    SCAN ERROR: ...   exit 2  a file, a term file or a pattern could not be used
  A run that printed SCAN ERROR, or no SCAN line at all, is never clean.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File scan-private.ps1 -TermsFile <terms.txt> <draft1.md> <draft2.md>
#>
[CmdletBinding(PositionalBinding = $false)]
param(
    [string]$TermsFile = '',
    [string]$TermsRegexFile = '',
    [switch]$NoLocalTerms,
    [switch]$SelfTest,
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$Path = @()
)
$ErrorActionPreference = 'Stop'
$Opts = [Text.RegularExpressions.RegexOptions]::IgnoreCase
$Utf8Strict = New-Object Text.UTF8Encoding($false, $true)

function Write-Line([string]$Text) { [Console]::Out.WriteLine($Text) }

function Stop-Scan([string]$Message) {
    Write-Line ("SCAN ERROR: {0}. This is not a clean scan." -f $Message)
    exit 2
}

function Read-ScanText([string]$P) {
    if (-not $P -or -not (Test-Path -LiteralPath $P -PathType Leaf)) { Stop-Scan "not a file: $P" }
    $full = (Resolve-Path -LiteralPath $P).ProviderPath
    try { $bytes = [IO.File]::ReadAllBytes($full) } catch { Stop-Scan "cannot read $P" }
    $start = 0
    if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) { $start = 3 }
    try { return $Utf8Strict.GetString($bytes, $start, $bytes.Length - $start) } catch { Stop-Scan "not UTF-8 text: $P" }
}

function Split-Lines([string]$Text) { return ,([regex]::Split($Text, "\r\n|\n|\r")) }

function Get-TermLines([string]$P) {
    $out = New-Object System.Collections.ArrayList
    $lines = Split-Lines (Read-ScanText $P)
    for ($i = 0; $i -lt $lines.Count; $i++) {
        $s = $lines[$i].Trim()
        if ($s -and -not $s.StartsWith('#')) { [void]$out.Add(@{ N = $i + 1; S = $s }) }
    }
    return ,$out
}

function Get-RegexTerms([string]$P, [string]$Label) {
    $out = New-Object System.Collections.ArrayList
    foreach ($t in (Get-TermLines $P)) {
        try { $rx = New-Object Text.RegularExpressions.Regex($t.S, $Opts) }
        catch { Stop-Scan ("{0} line {1} is not a valid regular expression" -f $Label, $t.N) }
        [void]$out.Add(@{ Label = ("{0} line {1}" -f $Label, $t.N); Rx = $rx })
    }
    return ,$out
}

function Get-Hits([string]$Line) {
    $hits = New-Object System.Collections.ArrayList
    $masked = $Line
    foreach ($a in $script:Allow) {
        $masked = $a.Rx.Replace($masked, [Text.RegularExpressions.MatchEvaluator] { param($m) ' ' * $m.Value.Length })
    }
    foreach ($d in $script:Deny) { if ($d.Rx.IsMatch($masked)) { [void]$hits.Add($d.Name) } }
    foreach ($f in $script:Fixed) {
        if ($Line.IndexOf($f.Term, [StringComparison]::OrdinalIgnoreCase) -ge 0) { [void]$hits.Add($f.Label) }
    }
    foreach ($r in $script:Regexes) { if ($r.Rx.IsMatch($Line)) { [void]$hits.Add($r.Label) } }
    return ,$hits
}

try {
    # Built-in patterns.
    $script:Deny = New-Object System.Collections.ArrayList
    $script:Allow = New-Object System.Collections.ArrayList
    $script:Fixed = New-Object System.Collections.ArrayList
    $script:Regexes = New-Object System.Collections.ArrayList
    $patLines = Split-Lines (Read-ScanText (Join-Path $PSScriptRoot 'private-patterns.txt'))
    for ($i = 0; $i -lt $patLines.Count; $i++) {
        $line = $patLines[$i]
        if (-not $line.Trim() -or $line.TrimStart().StartsWith('#')) { continue }
        $parts = $line.Split("`t")
        if ($parts.Count -ne 4 -or ($parts[0] -ne 'deny' -and $parts[0] -ne 'allow')) {
            Stop-Scan ("pattern file line {0} is not 'kind<TAB>name<TAB>regex<TAB>canary'" -f ($i + 1))
        }
        try { $rx = New-Object Text.RegularExpressions.Regex($parts[2], $Opts) }
        catch { Stop-Scan ("pattern file line {0} does not compile" -f ($i + 1)) }
        $item = @{ Name = $parts[1]; Rx = $rx; Canary = $parts[3] }
        if ($parts[0] -eq 'deny') { [void]$script:Deny.Add($item) } else { [void]$script:Allow.Add($item) }
    }
    if ($script:Deny.Count -eq 0) { Stop-Scan 'pattern file has no deny pattern' }

    # Terms.
    if ($TermsFile) {
        $base = Split-Path -Leaf $TermsFile
        foreach ($t in (Get-TermLines $TermsFile)) {
            [void]$script:Fixed.Add(@{ Label = ("term file line {0} ({1})" -f $t.N, $base); Term = $t.S })
        }
    }
    if ($TermsRegexFile) {
        foreach ($r in (Get-RegexTerms $TermsRegexFile ("regex file " + (Split-Path -Leaf $TermsRegexFile)))) { [void]$script:Regexes.Add($r) }
    }
    $localNote = 'local deny list: not used'
    if (-not $NoLocalTerms) {
        $local = $env:ARCHI_MAGIC_PRIVATE_TERMS
        if (-not $local -and $env:LOCALAPPDATA) { $local = Join-Path $env:LOCALAPPDATA 'archi-magic\private-terms.txt' }
        if ($local -and (Test-Path -LiteralPath $local -PathType Leaf)) {
            $found = Get-RegexTerms $local 'local deny list'
            foreach ($r in $found) { [void]$script:Regexes.Add($r) }
            $localNote = "local deny list: $($found.Count) pattern(s)"
        } else { $localNote = 'local deny list: none' }
    }

    # Self-test: every pattern must find its own test line.
    foreach ($d in $script:Deny) {
        if (-not ((Get-Hits $d.Canary) -contains $d.Name)) { Stop-Scan ("self-test: pattern '{0}' missed its test line" -f $d.Name) }
    }
    foreach ($a in $script:Allow) {
        $h = Get-Hits $a.Canary
        if ($h.Count -gt 0) { Stop-Scan ("self-test: allowed text '{0}' still gives a hit ({1})" -f $a.Name, ($h -join ', ')) }
    }
    if ((Get-Hits 'plain text with no private data').Count -gt 0) { Stop-Scan 'self-test: a plain line gives a hit' }
    foreach ($f in $script:Fixed) {
        if (-not ((Get-Hits ('x ' + $f.Term + ' x')) -contains $f.Label)) { Stop-Scan ("self-test: {0} does not match itself" -f $f.Label) }
    }
    if ($SelfTest) {
        Write-Line ("SELF-TEST OK: {0} built-in pattern(s), {1} allow rule(s)" -f $script:Deny.Count, $script:Allow.Count)
        exit 0
    }

    # Read every draft first: one unreadable or empty draft makes the whole scan an error.
    $Path = @($Path | Where-Object { $_ })
    if ($Path.Count -eq 0) { Stop-Scan 'no draft file given' }
    $texts = New-Object System.Collections.ArrayList
    foreach ($p in $Path) {
        $text = Read-ScanText $p
        if (-not $text.Trim()) { Stop-Scan "empty file: $p" }
        [void]$texts.Add(@{ Path = $p; Text = $text })
    }

    $hitCount = 0
    foreach ($t in $texts) {
        $lines = Split-Lines $t.Text
        for ($i = 0; $i -lt $lines.Count; $i++) {
            foreach ($name in (Get-Hits $lines[$i])) {
                Write-Line ("{0}:{1}: {2}" -f $t.Path, ($i + 1), $name)
                $hitCount++
            }
        }
    }
    $summary = "{0} file(s), {1} built-in pattern(s), {2} fixed term(s), {3} regex term(s), {4}, self-test passed" -f `
        $texts.Count, $script:Deny.Count, $script:Fixed.Count, $script:Regexes.Count, $localNote
    if ($hitCount -gt 0) {
        Write-Line ("SCAN HITS: {0} hit(s) in {1}. Fix the drafts and scan again." -f $hitCount, $summary)
        exit 1
    }
    Write-Line "SCAN CLEAN: $summary"
    exit 0
} catch {
    Write-Line ("SCAN ERROR: {0}. This is not a clean scan." -f $_.Exception.Message)
    exit 2
}
