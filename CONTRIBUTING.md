# Contributing to archi-magic

Thank you for helping. This page is for people and for AI agents (such as Claude Code) that work on this repo. The README has the full list of what each check does (see "For maintainers"). This page says what to run, what to write in a pull request, and a few rules that came from real sessions.

## The short version

1. Fork the repo, make a branch from `upstream/main`, change one thing, open a pull request. `main` is protected: it accepts only pull requests that pass the `check` job. See "Branches, pushes and CI".
2. Run the checks below before you push, as far as you can.
3. In the pull request, say what you **verified** and what you did **not verify**. Be plain about it.
4. After each push, watch the CI checks of the pull request (`gh pr checks <n> --watch`) and report the result.
5. Keep private data out of issues, pull requests and documents (see "Privacy" and its checklist).

## Before you start

- Install git, gh and a real Python (README, "Prerequisites"). For the Python skill tests on Windows, see "Python skill tests on Windows" below.
- Check your git identity before the first commit: `git config user.name` and `git config user.email`. On a new PC both can be empty, and the command then exits with code 1. Ask the user for the name and e-mail to use; do not invent them.
- Read `README.md`, section "For maintainers" (how to add a skill, what the checks do).
- Read `HOUSE-RULES.md`. Edit the rules only there. Every `SKILL.md` ends with a copy that `scripts/sync-house-rules.ps1` writes.
- For anything the user sees (the 3D viewer, tables, cursors, colors), read `design/DESIGN-SYSTEM.md` **first**, before you write the feature. See "Design".
- Some files are copies. The 3D viewer files in `skills/am-bim-viewer/viewer/` are copied from `mods/am-bim-viewer/viewer/`. Edit the source, then run `scripts/sync-shared-files.ps1` (and `scripts/sync-design-tokens.ps1` first when you change tokens). The check fails when a copy differs from its source.

## What to run

These are the checks CI runs (see `.github/workflows/check-skills.yml`). Run the ones that touch your change.

| Check | Command | When |
|---|---|---|
| Skills, names, README rows, house rules, shared files, your local private-terms list | `pwsh -File scripts/check-skills.ps1` (also under Windows PowerShell 5.1) | any change to `skills/`, `HOUSE-RULES.md`, README, shared files |
| No pinned plugin version | `pwsh -File scripts/check-version-bump.ps1` | any change to a `plugin.json` or the marketplace |
| Repo checks mutation table | `.\scripts\tests\test-checks.ps1` | a change to `scripts/check-*.ps1` |
| Theme logic | `node design/tests/theme.test.js` | a change to `design/` |
| 3D viewer (camera, gumball, walk, pet) | in `skills/am-bim-viewer`: `npm ci`, then `npm test`, then `npm run mutate` | a change to the viewer core or template |
| Viewer in a real browser | open a built page and run `skills/am-bim-viewer/tests/browser-check.js` in the page | a change to the viewer page |
| Python skill tests | `python3 -m pytest -q tests` in each `skills/*/tests` that has `test_*.py`. On Windows: `scripts\run-python-tests.ps1` (see below) | a change to those skills |
| Hooks and doctor | `.\hooks\tests\*.ps1`, `.\skills\am-doctor\tests\*.ps1` | a change to hooks or `am-doctor` |
| Every `.ps1` parses under Windows PowerShell 5.1 | CI runs it | scripts must be ASCII-only and 5.1-compatible |

If you cannot run one of them, do not skip it silently. Write it under "Not verified" in the pull request and let CI decide.

### Python skill tests on Windows

- **Get a Python with pip.** The CPython inside the pyRevit folder (`bin\cengines`) has no pip and no pytest, so it cannot run the tests. A per-user install needs no admin (about 1 minute):
  ```
  winget install --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements
  ```
- **Call it as `py -3` when `python` is the Microsoft Store alias.** The alias prints `Python was not found` (exit 49 in PowerShell; in Bash the exit code can be 0). The per-user install also adds the Python Launcher `py`. In a session that started before the install, neither is on PATH yet: use the full path that winget prints, or start a new session. `am-doctor -Only tools` names the command that works.
- **Install the test packages for your user.** `requirements-dev.txt` lists the packages that CI installs (see `.github/workflows/check-skills.yml`):
  ```
  py -3 -m pip install --user -r requirements-dev.txt
  ```
  pip can warn that its Scripts folder is not on PATH. That is fine: always run `py -3 -m pytest`, not `pytest`.
- **Run the tests as CI does** with the local runner. It finds `py -3`, `python` or `python3`, skips the Store alias and the pyRevit CPython, and runs pytest in each `skills/*/tests` folder:
  ```
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run-python-tests.ps1
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run-python-tests.ps1 -Skill am-doctor,am-markup-intake
  ```
  It runs every folder and prints one pass or FAIL line for each. Exit 0 means all passed. Exit 3 means pytest is not installed: run the install line above. `-Python <python.exe>` picks a Python.
- **Run one skill's tests** by hand from the skill folder: `cd skills/<skill>`, then `py -3 -m pytest -q tests`.
- **CI runs the Python tests on Linux and on Windows.** The Linux run (`check` job) must pass. The Windows run (`windows-pytest` job) uses the runner above. It only warns for now, because some tests fail on Windows on a clean `main` (symlinks, line endings, default text encoding). Before you report a Windows failure as yours, run the same test on a clean branch from `upstream/main`, or read the `windows-pytest` log of a recent `main` run.

## Pull requests

- One topic for each pull request. Small is good.
- Use this shape in the description:
  - **What**: what changed and why.
  - **Files**: the main files.
  - **Verified**: what you ran or looked at, and what you saw.
  - **Not verified**: what you did not run, and why.
- Link the issue (`Closes #nn`).
- Add a new commit when you change an open pull request. Do not rewrite its history (amend, force-push). A visible correction is more honest, and an agent session may be blocked from force-pushing anyway.
- Skills: new skills follow the steps in the README ("Adding a skill"), including a row in both tables.

## Branches, pushes and CI

These steps come from sessions where a stale base, a missing write permission or an unchecked CI run cost time. The commands use `zsenarchitect/archi-magic` as the upstream repo and `upstream` as its git remote.

- **Check your access before any push.** Run `gh repo view zsenarchitect/archi-magic --json viewerPermission`.
  - `ADMIN`, `MAINTAIN` or `WRITE`: you can push a branch to the repo.
  - `READ` (or `TRIAGE`): do not push to it. The push waits on a sign-in prompt and then fails with `403`. Push to your fork (`gh repo fork --remote`) and open the pull request from the fork. If you cannot make a fork, open an issue for the maintainer.
- **Branch from `upstream/main`, never from your fork's `main`.** `gh repo fork --clone` can reuse an old fork whose `main` is weeks behind, and a branch cut from it conflicts with `main`. Do this:
  ```
  git fetch upstream
  git switch -c <branch> upstream/main
  ```
  When `main` moves on while your pull request is open, merge it in (`git fetch upstream`, then `git merge upstream/main`). Do not rebase or force-push.
- **Read upstream from `upstream/main`, not from the local marketplace clone.** `~/.claude/plugins/marketplaces/archi-magic` and the plugin cache can be weeks old. To decide whether a skill or a file is already upstream, use `git show upstream/main:<path>` after a fetch, or GitHub.
- **Check the pull request state before you add commits.** Run `gh pr view <n> --repo zsenarchitect/archi-magic --json state,mergedAt`. If it is `MERGED` or `CLOSED`, do not push to its branch. Make a new branch from `upstream/main` and open a new pull request.
- **Watch CI after every push.** Run `gh pr checks <n> --repo zsenarchitect/archi-magic --watch` and report the result. `mergeable` does not say that the checks passed. A pull request from a fork can wait for maintainer approval before CI runs: then write "CI waits for approval", not "CI passed".
- **Delete your personal copy after a merge.** If you kept a skill in your personal skills folder (`~/.claude/skills/<name>`) while you worked on it, delete that copy after the skill is merged (ask the user first), then type `/reload-plugins`. A personal copy with the same name shows next to the plugin copy and drifts from it. When you copy it back into the repo, it can overwrite the synced house rules, and `check-skills.ps1` fails with `house rules differ from HOUSE-RULES.md`.

## Working with an AI agent

These tips come from sessions where an agent did most of the work.

- **Write files with the file-writing tool, not with shell heredocs.** A multi-line `bash` heredoc or a `sed` one-liner with an apostrophe in it can fail with `unexpected EOF while looking for matching`. Write the file, then splice it with `head` and `tail`.
- **An agent session may not be allowed to run the repo's checks** (for example `check-skills.ps1`, `npm test`) in a cloned repo. If so, the agent checks the rules by hand (read the script), says so under "Not verified", and CI is the judge. A human can run them in a terminal.
- **Use a fork and, for parallel work, `git worktree`.** Give each independent pull request its own worktree and branch. Do not touch the other branches.
- **Merging several PRs into protected `main`:** merge one at a time. The protected `main` branch requires strict up-to-date checks. After each merge, update the next branch (`gh pr update-branch`), wait for both CI jobs to conclude, read the head SHA *after* the checks finish, and merge with `gh pr merge --merge --match-head-commit <sha>`. Treat `UNKNOWN` mergeability right after a merge as a transient wait state (not a failure).
- **PowerShell 5.1 JSON-as-argument trap:** Windows PowerShell 5.1 strips embedded double quotes when passing JSON as a native argument to `powershell.exe -File ... $json`. Never pass JSON as a command argument; pipe it on stdin (`$json | powershell.exe ...`) or write it to a temp file.
- **PowerShell 5.1 BOM trap:** in Windows PowerShell 5.1, `Set-Content -Encoding utf8` and `Out-File -Encoding utf8` write a UTF-8 byte-order mark (BOM, the bytes `EF BB BF`), and `>` writes UTF-16. Many JSON readers, Claude Code among them, then fail (`JSON Parse error: Unrecognized token`). To write JSON or other UTF-8 text, use:
  ```powershell
  [IO.File]::WriteAllText($p, $text, (New-Object Text.UTF8Encoding $false))
  ```
  Never edit files under `~/.claude/plugins/` by hand. Use `/plugin`, or the `extraKnownMarketplaces` entry in `settings.json` (README, "Install procedure"). One BOM in `known_marketplaces.json` stopped the plugin from loading for weeks, and no hook could warn.
- **PowerShell: call a quoted exe path with `&`.** `"<path>\gh.exe" auth login` fails with `Unexpected token 'auth'`. Write `& "<path>\gh.exe" auth login`. A tool that winget installed a moment ago is not on PATH in the current session, so you often need its full path.
- **Show what you did not do.** If the agent could not test a feature live, say what it checked instead (state values, screenshots) and what it could not (sound, long motion).
- **Browser pane quirks** (see the `am-bim-viewer` skill for the viewer): a page outside the project folder is a static snapshot; opening the same URL again does not reload the page (use a new file name or close the tab); about nine open tabs block new ones.

## Design

`design/DESIGN-SYSTEM.md` is the rule for anything the user sees. Its first rule is "Value, not hue": black, white and grays only. It also lists things not to use (colored chips, rounded cards, emoji, and more).

If your feature needs something the design system does not allow (a mascot, a status color, a rounded shape), **do not decide alone.** Say so in the pull request, name the exact things that break the rule, and ask the maintainer. Good options to offer:

- a grayscale version by default, with color as an opt-in;
- a written exception for one named feature, added to `DESIGN-SYSTEM.md`.

Check this **before** you write the feature, not after.

## Assets and licenses

- Do not add third-party characters, logos, images, 3D models or sounds to the repo unless the license is clear and recorded (name, source, license) next to the file.
- Do not download assets from sources you cannot trust or whose license you cannot read. Do not copy art from games, films or brands.
- Prefer original work that is built in code (shapes, a small SVG, a synthesized sound). It keeps the page one self-contained file and carries no third-party license.
- Dependencies are pinned, and `scripts/check-upstream.ps1` watches them. Add a new one only with a reason.

## Privacy and Neutrality

archi-magic is a public open-source repository and must NEVER contain any client or office project information. Only abstract skills and abilities belong in archi-magic, not office-specific or project-specific workflows.

Issues, pull requests, documents and test files must not contain project, client or building names, firm abbreviations, file paths with user names, element ids, e-mail addresses, tokens or links to document-management systems. Replace them with neutral words ("the project model", "a manufacturer .rvt", "Firm A"). Scan and desensitize your text before you post it.

Pages made by `am-bim-viewer` contain the geometry, element names, ids and parameters of the model. Do not commit them and do not post them. Keep them out of the repo (for example in a folder that you ignore).

### Private-data checklist (before every commit and pull request)

A search for the project number and the client name is not enough. One merged skill still had office-specific family, type, tag and room names. Look for all of these, in the text, the scripts, the tests and the pull request description:

- project, client, building and firm names and abbreviations, project numbers;
- person names, user names, e-mail addresses, tokens;
- file paths that contain a user name (a profile or home folder), model and file names, links to document-management systems;
- real element ids;
- names that you copied from a model: **family names, type names (wall, floor, door types), tag type names, room names and numbers**, view and sheet names. A name from a project model is project data, even when it looks generic;
- **script default values** and examples: a default family or type name in a script is the same as a name in the text. Make it an input that the user gives, or use a neutral example (`<family>`, "Wall Type A", "Project A");
- test fixtures, sample JSON and screenshots.

Then scan the whole change, not only the lines you remember.

**Local deny list.** Keep your own list of private terms in `%LOCALAPPDATA%\archi-magic\private-terms.txt`: one regular expression per line, case-insensitive. Blank lines and lines that start with `#` are skipped. Keep this file outside the repo and **never commit it**.

- `scripts/check-skills.ps1` reads the list when it exists and fails when a pattern matches any text file in a skill folder. It names the file, the line and the line number in your list, never the term. Another location: `-PrivateTerms <file>` or the `ARCHI_MAGIC_PRIVATE_TERMS` environment variable. With no list, the check does not change (CI has no list).
- For the rest of the change (README, docs, hooks, the pull request text), scan the diff with the same list. It must print nothing:
  ```powershell
  $terms = @(Get-Content -Encoding UTF8 "$env:LOCALAPPDATA\archi-magic\private-terms.txt" | ForEach-Object { $_.Trim() } | Where-Object { $_ -and -not $_.StartsWith('#') })
  git diff upstream/main...HEAD | Select-String -Pattern $terms
  ```

## Feedback

You can send feedback in three ways:

- an **issue** for one problem or idea (the note that archi-magic offers after a troubled session can file one);
- a **pull request** that adds a document `docs/feedback/<date>-<topic>.md` for a whole session, with these sections: the task, what worked, gaps and friction, proposals, done when;
- a **comment** on an existing issue, when yours is the same topic.

Scan every feedback text for private data first (see "Privacy").
