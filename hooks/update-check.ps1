<#
.SYNOPSIS
  archi-magic SessionStart hook: tell the user when a newer archi-magic release exists.

.DESCRIPTION
  Compares every installed archi-magic plugin (archi-magic, and archi-magic-* after the planned
  split) with the latest GitHub release tag (r<yyyy.MM.dd>-<sha12>). The installed commits come from
  Claude Code's installed_plugins.json (gitCommitSha), read through scripts\lib\am-plugins.ps1, which
  follows CLAUDE_CONFIG_DIR. If that file cannot be read, the commit folder name of this copy is used.
  At most one release call per set of installed versions per -MaxAgeHours
  (cache: %LOCALAPPDATA%\archi-magic\update-check.json).

  Missing skills: when a plugin is stale, the notice names the skills that the latest release has and
  the installed copy does not have (for example a new am-feedback). Installed names are the skills\
  folder names of this copy and of every installed archi-magic* folder. Latest names come from, in
  order: -LatestSkills; the cache folder of the latest commit (<plugin folder>\<sha12>\skills) when it
  is already on disk; the cache file, when it holds names for this release tag; else ONE GitHub
  contents call (skills?ref=<tag>), only for a stale install, at most 3 s, and skipped when the hook
  has used more than 5 s. The names are cached with the tag, so this call also runs at most once per
  release. The Marketplace clone is not used: it moves only with a plugin update, so it equals the
  installed copy. Limits: only the root skills\ folder of the release is compared (the planned split
  must change this), and on any failure (offline, rate limit) the notice has no list. An empty or
  unread name set on either side gives no list, never "every skill is missing".

  Assumption (docs/plans/2026-10-24-plugin-split.md, A4): every archi-magic plugin's version is the
  repo commit. If the split trial shows per-folder versions, this comparison must change before the
  split ships, or unchanged plugins would always look stale.

  Output: nothing when up to date, offline, or not an installed copy. When outdated, one JSON object:
  systemMessage (shown to the user, names only the stale plugins) and additionalContext (for Claude).
  Never blocks a session: always exit 0. Every failure goes to the shared archi-magic log.
  Opt out: set ARCHI_MAGIC_NO_UPDATE_CHECK=1.

  Test parameters: -PluginRoot, -CacheDir, -LatestTag (skips the network), -ApiUrl, -InstalledPluginsJson,
  -LatestSkills (comma-separated names, skips the skills call), -SkillsApiUrl ({0} = tag). With
  -LatestTag, the skills call runs only when -SkillsApiUrl is given too, so tests stay offline.
#>
param(
    [string]$PluginRoot = $env:CLAUDE_PLUGIN_ROOT,
    [string]$CacheDir = $(if ($env:LOCALAPPDATA) { Join-Path $env:LOCALAPPDATA 'archi-magic' } else { '' }),
    [string]$LatestTag = '',
    [string]$ApiUrl = 'https://api.github.com/repos/zsenarchitect/archi-magic/releases/latest',
    [int]$MaxAgeHours = 24,
    [string]$InstalledPluginsJson = '',
    [string]$LatestSkills = '',
    [string]$SkillsApiUrl = 'https://api.github.com/repos/zsenarchitect/archi-magic/contents/skills?ref={0}'
)
$ErrorActionPreference = 'Stop'
$clock = [Diagnostics.Stopwatch]::StartNew()
$skillsCallAllowed = (-not $LatestTag) -or $PSBoundParameters.ContainsKey('SkillsApiUrl')

function Get-SkillDirNames([string]$root) {
    # Folder names under <root>\skills. Empty when the folder is missing.
    $d = Join-Path $root 'skills'
    if (-not $root -or -not (Test-Path -LiteralPath $d)) { return @() }
    return @(Get-ChildItem -LiteralPath $d -Directory -ErrorAction SilentlyContinue | ForEach-Object { $_.Name })
}

function Write-Log([string]$Level, [string]$Message, [hashtable]$Data) {
    try {
        . (Join-Path $PSScriptRoot '..\scripts\lib\am-log.ps1')
        # Write-AmLog warns on a log failure; keep stdout clean for the hook JSON.
        Write-AmLog -Skill 'hooks' -Step 'update-check' -Level $Level -Message $Message -Data $Data 3>$null
    } catch { }
}

try {
    if ($env:ARCHI_MAGIC_NO_UPDATE_CHECK -eq '1' -or -not $PluginRoot) { exit 0 }

    # Only an installed copy has a commit folder as its version. A source checkout or pinned version: no check.
    $self = Split-Path -Leaf $PluginRoot.TrimEnd('\', '/')
    $parent = Split-Path -Leaf (Split-Path -Parent $PluginRoot.TrimEnd('\', '/'))
    if (-not ($parent -eq 'archi-magic' -or $parent -like 'archi-magic-*') -or $self -notmatch '^[0-9a-f]{12}$') { exit 0 }

    # Installed archi-magic plugins and their commits. Fallback: this copy only.
    $installed = @()
    try {
        . (Join-Path $PSScriptRoot '..\scripts\lib\am-plugins.ps1')
        if (-not $InstalledPluginsJson) { $InstalledPluginsJson = Get-AmInstalledPluginsPath }
        $inst = Read-AmInstalledPlugins $InstalledPluginsJson
        if ($inst.Ok) {
            # Assign first: the function returns its array wrapped (, @()), so piping the call directly
            # would send the whole array as one object.
            $entries = Get-AmArchiMagicEntries $inst
            $installed = @($entries | Where-Object { $_.Sha } | ForEach-Object { @{ Name = $_.Name; Sha = $_.Sha; Path = $_.InstallPath } })
        }
    } catch { Write-Log 'warn' ('Update check: installed plugins not read: ' + $_.Exception.Message) $null }
    if ($installed.Count -eq 0) { $installed = @(@{ Name = $parent; Sha = $self; Path = $PluginRoot }) }
    $versionKey = (@($installed | ForEach-Object { "$($_.Name)=$($_.Sha)" } | Sort-Object) -join ',')

    $cacheFile = if ($CacheDir) { Join-Path $CacheDir 'update-check.json' } else { '' }
    $tag = $LatestTag
    $c = $null
    if ($cacheFile -and (Test-Path -LiteralPath $cacheFile)) {
        try { $c = [IO.File]::ReadAllText($cacheFile) | ConvertFrom-Json } catch { $c = $null }
    }
    $checkedAt = ''
    if (-not $tag -and $c) {
        try {
            $age = (Get-Date).ToUniversalTime() - [datetime]::Parse($c.checked_at).ToUniversalTime()
            # The cache is valid only for the installed versions that wrote it, so an update triggers a fresh check.
            if ($c.installed -eq $versionKey -and $c.latest_tag -and $age.TotalHours -lt $MaxAgeHours) { $tag = [string]$c.latest_tag; $checkedAt = [string]$c.checked_at }
        } catch { }
    }
    function Save-Cache([string]$skillsTag, [string[]]$skillNames) {
        if (-not $cacheFile -or -not $tag) { return }
        try {
            if (-not (Test-Path -LiteralPath $CacheDir)) { New-Item -ItemType Directory -Force -Path $CacheDir | Out-Null }
            $when = if ($script:checkedAt) { $script:checkedAt } else { (Get-Date).ToUniversalTime().ToString('o') }
            $rec = [ordered]@{ installed = $versionKey; latest_tag = $tag; checked_at = $when }
            if ($skillsTag) { $rec.latest_skills_tag = $skillsTag; $rec.latest_skills = @($skillNames) }
            [IO.File]::WriteAllText($cacheFile, (New-Object PSObject -Property $rec | ConvertTo-Json -Compress))
        } catch { Write-Log 'warn' ('Update check cache not written: ' + $_.Exception.Message) $null }
    }
    if (-not $tag) {
        try {
            [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
            $rel = Invoke-RestMethod -Uri $ApiUrl -TimeoutSec 4 -UseBasicParsing -Headers @{ 'User-Agent' = 'archi-magic-update-check' }
            $tag = [string]$rel.tag_name
        } catch {
            Write-Log 'warn' ('Update check skipped: ' + $_.Exception.Message) @{ installed = $versionKey }
            exit 0
        }
        # Keep skill names cached for this same tag; drop them for another tag.
        if ($c -and $c.latest_skills_tag -eq $tag) { Save-Cache $tag @($c.latest_skills) } else { Save-Cache '' @() }
    }

    $m = [regex]::Match($tag, '^r\d{4}\.\d{2}\.\d{2}-([0-9a-f]{12})$')
    if (-not $m.Success) {
        Write-Log 'warn' "Update check: unexpected release tag '$tag'" @{ installed = $versionKey }
        exit 0
    }
    $latest = $m.Groups[1].Value
    $stale = @($installed | Where-Object { $_.Sha -ne $latest })
    if ($stale.Count -eq 0) { exit 0 }

    # Skills in the latest release that no installed archi-magic copy has. Never fails the notice.
    $missing = @()
    try {
        $have = @(@(Get-SkillDirNames $PluginRoot) + @($installed | ForEach-Object { Get-SkillDirNames ([string]$_.Path) }) | Sort-Object -Unique)
        $newest = @()
        if ($LatestSkills) {
            $newest = @($LatestSkills -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
        }
        if ($newest.Count -eq 0) {
            # The latest commit may already be in the plugin cache (for example after a partial update).
            $newest = @(Get-SkillDirNames (Join-Path (Split-Path -Parent $PluginRoot.TrimEnd('\', '/')) $latest))
        }
        if ($newest.Count -eq 0 -and $c -and $c.latest_skills_tag -eq $tag) { $newest = @($c.latest_skills | Where-Object { $_ }) }
        if ($newest.Count -eq 0 -and $have.Count -gt 0 -and $skillsCallAllowed -and $SkillsApiUrl) {
            $left = 8 - $clock.Elapsed.TotalSeconds
            if ($clock.Elapsed.TotalSeconds -gt 5) {
                Write-Log 'warn' 'Update check: skills list skipped (time budget used)' @{ latest = $latest }
            } else {
                try {
                    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
                    $sec = [int][Math]::Max(1, [Math]::Min(3, [Math]::Floor($left)))
                    $items = Invoke-RestMethod -Uri ($SkillsApiUrl -f $tag) -TimeoutSec $sec -UseBasicParsing -Headers @{ 'User-Agent' = 'archi-magic-update-check' }
                    $newest = @(@($items) | ForEach-Object { $_ } | Where-Object { $_.type -eq 'dir' -and $_.name } | ForEach-Object { [string]$_.name })
                    if ($newest.Count -gt 0) { Save-Cache $tag $newest }
                } catch {
                    Write-Log 'warn' ('Update check: skills list not read: ' + $_.Exception.Message) @{ latest = $latest }
                }
            }
        }
        # Empty-set guard: with no names on either side, show no list (latest minus nothing is not "all missing").
        if ($have.Count -gt 0 -and $newest.Count -gt 0) {
            $missing = @($newest | Where-Object { $have -notcontains $_ } | Sort-Object -Unique)
        } elseif ($newest.Count -eq 0) {
            Write-Log 'info' 'Update check: no skills list for the latest release; notice without missing skills' @{ latest = $latest }
        }
    } catch { Write-Log 'warn' ('Update check: missing skills not computed: ' + $_.Exception.Message) $null }
    $missingText = ''
    if ($missing.Count -gt 0) {
        $missingText = (@($missing | Select-Object -First 10) -join ', ')
        if ($missing.Count -gt 10) { $missingText += " and $($missing.Count - 10) more" }
    }

    $list = (@($stale | ForEach-Object { "$($_.Name) $($_.Sha)" }) -join ', ')
    Write-Log 'info' 'Update available' @{ installed = $versionKey; latest = $latest; missing_skills = $missing.Count }
    $user = "archi-magic update available (installed: $list; latest $latest). "
    if ($missingText) { $user += "Skills you do not have yet: $missingText. " }
    $user += 'To update: type /plugin, open the Marketplaces tab, select archi-magic, choose Update marketplace. ' +
             'Then type /reload-plugins (or start a new session). To get updates by itself, choose Enable auto-update there.'
    $ctx = "archi-magic is out of date: $list; latest release $tag. "
    if ($missingText) { $ctx += "Skills in the latest release that are not loaded in this session: $missingText. " }
    $ctx += 'If the user names an archi-magic skill or command that you do not have (for example "feedback am" for am-feedback), it may be new in the latest release: say so and offer the update steps. ' +
            'If the user asks about it: /plugin > Marketplaces > archi-magic > Update marketplace (or Installed > the plugin > Update now), then /reload-plugins, which also installs any new dependency. ' +
            'Install commands must use the qualified name, for example archi-magic@archi-magic. ' +
            '"/plugin marketplace update archi-magic" alone refreshes the listing only and does not update the plugin. Do not update plugins without the user asking.'
    $out = [ordered]@{
        systemMessage      = $user
        hookSpecificOutput = [ordered]@{ hookEventName = 'SessionStart'; additionalContext = $ctx }
    }
    [Console]::Out.Write((New-Object PSObject -Property $out | ConvertTo-Json -Compress -Depth 3))
} catch {
    Write-Log 'error' ('Update check failed: ' + $_.Exception.Message) $null
}
exit 0
