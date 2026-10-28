# Contributing to archi-magic

Thank you for helping. This page is for people and for AI agents (such as Claude Code) that work on this repo. The README has the full list of what each check does (see "For maintainers"). This page says what to run, what to write in a pull request, and a few rules that came from real sessions.

## The short version

1. Fork the repo, make a branch, change one thing, open a pull request. `main` is protected: it accepts only pull requests that pass the `check` job.
2. Run the checks below before you push, as far as you can.
3. In the pull request, say what you **verified** and what you did **not verify**. Be plain about it.
4. Keep private data out of issues, pull requests and documents (see "Privacy").

## Before you start

- Read `README.md`, section "For maintainers" (how to add a skill, what the checks do).
- Read `HOUSE-RULES.md`. Edit the rules only there. Every `SKILL.md` ends with a copy that `scripts/sync-house-rules.ps1` writes.
- For anything the user sees (the 3D viewer, tables, cursors, colors), read `design/DESIGN-SYSTEM.md` **first**, before you write the feature. See "Design".
- Some files are copies. The 3D viewer files in `skills/am-bim-viewer/viewer/` are copied from `mods/am-bim-viewer/viewer/`. Edit the source, then run `scripts/sync-shared-files.ps1` (and `scripts/sync-design-tokens.ps1` first when you change tokens). The check fails when a copy differs from its source.

## What to run

These are the checks CI runs (see `.github/workflows/check-skills.yml`). Run the ones that touch your change.

| Check | Command | When |
|---|---|---|
| Skills, names, README rows, house rules, shared files | `pwsh -File scripts/check-skills.ps1` (also under Windows PowerShell 5.1) | any change to `skills/`, `HOUSE-RULES.md`, README, shared files |
| No pinned plugin version | `pwsh -File scripts/check-version-bump.ps1` | any change to a `plugin.json` or the marketplace |
| Repo checks mutation table | `.\scripts\tests\test-checks.ps1` | a change to `scripts/check-*.ps1` |
| Theme logic | `node design/tests/theme.test.js` | a change to `design/` |
| 3D viewer (camera, gumball, walk, pet) | in `skills/am-bim-viewer`: `npm ci`, then `npm test`, then `npm run mutate` | a change to the viewer core or template |
| Viewer in a real browser | open a built page and run `skills/am-bim-viewer/tests/browser-check.js` in the page | a change to the viewer page |
| Python skill tests | `python3 -m pytest -q tests` in each `skills/*/tests` that has `test_*.py` | a change to those skills |
| Hooks and doctor | `.\hooks\tests\*.ps1`, `.\skills\am-doctor\tests\*.ps1` | a change to hooks or `am-doctor` |
| Every `.ps1` parses under Windows PowerShell 5.1 | CI runs it | scripts must be ASCII-only and 5.1-compatible |

If you cannot run one of them, do not skip it silently. Write it under "Not verified" in the pull request and let CI decide.

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

## Working with an AI agent

These tips come from sessions where an agent did most of the work.

- **Write files with the file-writing tool, not with shell heredocs.** A multi-line `bash` heredoc or a `sed` one-liner with an apostrophe in it can fail with `unexpected EOF while looking for matching`. Write the file, then splice it with `head` and `tail`.
- **An agent session may not be allowed to run the repo's checks** (for example `check-skills.ps1`, `npm test`) in a cloned repo. If so, the agent checks the rules by hand (read the script), says so under "Not verified", and CI is the judge. A human can run them in a terminal.
- **Use a fork and, for parallel work, `git worktree`.** Give each independent pull request its own worktree and branch. Do not touch the other branches.
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

## Privacy

Issues, pull requests, documents and test files must not contain project, client or building names, file paths with user names, element ids, e-mail addresses, tokens or links to document-management systems. Replace them with neutral words ("the project model", "a manufacturer .rvt"). Scan your text before you post it.

Pages made by `am-bim-viewer` contain the geometry, element names, ids and parameters of the model. Do not commit them and do not post them. Keep them out of the repo (for example in a folder that you ignore).

## Feedback

You can send feedback in three ways:

- an **issue** for one problem or idea (the note that archi-magic offers after a troubled session can file one);
- a **pull request** that adds a document `docs/feedback/<date>-<topic>.md` for a whole session, with these sections: the task, what worked, gaps and friction, proposals, done when;
- a **comment** on an existing issue, when yours is the same topic.

Scan every feedback text for private data first (see "Privacy").
