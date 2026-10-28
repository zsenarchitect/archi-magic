# Session feedback: import a manufacturer type, the 3D viewer round trip, and building a pet feature

Source: one long live session (Revit 2026.4, pyRevit 7.0.0.26278, Claude desktop app on Windows 11, Claude Code 2.1.309, Git Bash and PowerShell tools).
Project, client and user names are left out on purpose.

## The tasks, in order

1. Open two manufacturer `.rvt` files, find the floor type and material of an entry mat, and import them into the project model.
2. Read a manufacturer product data sheet (PDF) on a machine with no Python.
3. Show a model in 3D in `am-bim-viewer`, move elements in the viewer, apply the moves to Revit through `am-safe-change`, then undo one.
4. File feedback as GitHub issues (#39, #40, #41), then turn each into a pull request through a fork (#42, #44, #45, #46, #47).
5. Add a "pet" feature to the viewer (#43): a cartoon cat that walks to a clicked surface. About 25 follow-up requests from the user arrived while the work ran (turning, running, jumping, idle actions, routing, meowing, petting, thoughts).

## What worked

- **Test hooks plus a freeze hook.** The viewer page exposes `window.__bim`. With a few hooks (`pet.get`, `pet.step`, `pet.freeze`, `pet.setAct`) the agent drove every behavior from the Browser pane, read exact state, and took screenshots of single poses. Without them, the feature could not have been verified at all.
- **Pure logic with callbacks.** The walking, jumping and route search take `env.ground` and `env.blocked` callbacks, so the same code runs in the page and in node tests with fake environments.
- **A scene generator.** A 20-line PowerShell script wrote a test scene as `am-bim-viewer/1` JSON (a floor, a ledge, a gap, a long wall with a doorway, a sealed pen). One scene covered jump, route and "stuck" cases. See proposal 5.
- **Parallel forks.** Two independent PRs were written by two sub-agents in separate `git worktree` folders while the main thread kept working. No conflicts.
- **Honest PR bodies.** Each PR has a "Verified" and a "Not verified" section. It made the missing checks (gap 1) visible to the maintainer.
- **Dry run, then approval.** `run_modify` with `dry_run=true`, then the real run with the Keep prompt, worked for the type import, a ceiling move and its undo.

## Gaps and friction

1. **An agent cannot run the repo's checks.** In a cloned repo, the session's auto-mode classifier refused `scripts/check-skills.ps1`, `npm ci`, `npm test`, `git commit --amend` and `git push --force`. The agent checked the rules of `check-skills.ps1` by hand and wrote "not verified" in five PRs. A maintainer cannot tell which checks matter or how a contributor runs them. Proposal 1.
2. **Shell quoting.** Several multi-line `bash` heredocs and `sed` one-liners with apostrophes failed with `unexpected EOF while looking for matching '''`. Writing the file with the Write tool and then splicing it with `head` and `tail` always worked. Proposal 1 (a one-line rule).
3. **The Browser pane has quirks that cost many retries.**
   - A local file outside the project folder is a static snapshot (already in #40).
   - Navigating to the **same URL again does not reload** the page. State from the earlier run stays. A new file name per build, or closing the tab, fixes it.
   - With about 9 tabs open, `navigate` fails with "couldn't open file ... the user declined access" and `tabs_create` says "tab cap reached". The message does not say so. Close old tabs.
   - `get_page_text`, `screenshot` and `find` refuse local-file tabs on some calls, while `javascript_tool` and `computer` zoom work.
   - `location.reload()` from the page did nothing.
4. **The exported viewer page contains model data.** The page embeds geometry, element names, ids and parameters. Saved in a project folder (the fix in #40), it can be committed or shared by accident. The skill should say so after the export, and the default output folder should be easy to ignore.
5. **The pet breaks the design system, and nobody was asked.** `design/DESIGN-SYSTEM.md` says "Value, not hue" (black, white and grays only), says 3D categories use value steps, and lists colored chips, rounded cards and emoji under "Don't". `design.test.mjs` forbids hue in the page CSS. The pet is orange, the hearts are pink, and the thought cloud is rounded. The agent kept the CSS inside the tokens and put the color in 3D shapes and in a few inline styles in JS, which passes the CSS test but not the spirit of the rule. The agent noticed it late, after most of the feature was written. The PR now says so and asks the maintainer to decide (proposal 4).
6. **Third-party characters and downloaded assets.** The user asked for a famous game character and for "a better model from the internet". The agent declined the download (untrusted source, unclear license, public MIT repo) and built an original character from code shapes. There is no written policy in the repo, so each contributor decides alone. Proposal 4.
7. **Long approval waits are invisible.** A `run_modify` waited 470 s for the Keep prompt in Revit while the agent was silent. Say before running: "Look at Revit: a Keep / Discard prompt is waiting."
8. **A tool that is not installed.** The user asked to "use Blender". It was not installed, and the agent cannot download a large installer without a clear yes. A short "Is this tool installed? How do you want to get it?" pre-check would save a round trip. This is general, not archi-magic specific.
9. **Questions that came too late.** The user had the wrong `.rvt` open twice. The agent compared the PDF with the model to find out. A first question "which file is the product, which is the target?" is already in #39; the same applies to `am-bim-viewer` ("which model is open, and which 3D view?").
10. **Feedback has only an issue route.** `am-feedback` (#42) posts issues. A contributor with no write access, or who wants the text reviewed, needs a PR route. This document is that route. Proposal 3.

Gaps already filed: #39 (type import recipe and Revit 2026 API gotchas), #40 (viewer round trip), #41 (am-feedback), #46 and #47 (their PRs).

## Proposals

1. **Add `CONTRIBUTING.md`** with a short, tested checklist:
   - the commands CI runs (`check-skills.ps1`, `check-version-bump.ps1`, the node tests, `mutate`) and which of them an agent session may not be able to run;
   - "write files with the Write tool, not shell heredocs" and "do not rewrite history on an open PR (an agent may be blocked from force-pushing); add a commit";
   - how to state "Verified" and "Not verified" in a PR.
2. **Add to `am-bim-viewer` SKILL.md** a troubleshooting table: blank page (outside the project folder), same URL does not reload (new file name), tab cap, local-file tabs and which tools work. Add a privacy note: the page contains model data.
3. **Add a PR route to `am-feedback`**: when the user has no write access or asks for a PR, write `docs/feedback/<date>-<slug>.md` in the format of the earlier feedback documents (the task, what worked, gaps, proposals, done when), fork, branch, open the PR. Same scrub step as for issues.
4. **Write two short policies** in `CONTRIBUTING.md` or `design/`:
   - *Assets*: no third-party characters, logos or downloaded models or sounds in the repo unless the license is clear and recorded; prefer original shapes built in code.
   - *Color and playful features*: read `design/DESIGN-SYSTEM.md` before a UI feature. A feature that wants hue, rounded shapes or emoji (a mascot, a status icon) must say so in the PR and ask the maintainer. Options to offer: a grayscale version by default with color as an opt-in, or a written exception for one named feature. The agent should check this rule before it writes the feature, not after.
5. **Add a small test scene fixture** for viewer features (for example `skills/am-bim-viewer/tests/scenes/obstacles.json` and a `make-scene` script): a floor, a ledge, a gap, a wall with a doorway, a sealed pen. A feature that needs geometry (walk, pet, future measure tools) can be tried and tested on the same scene.
6. **Convention for viewer test hooks**: every interactive viewer feature adds a block to `window.__bim.<feature>` with `get`, a way to step it by hand, and `freeze`. List it in the `am-bim-viewer` SKILL.md so tests and agents can rely on it.

## Done when

A contributor, human or agent, can open a PR knowing which checks to run and which one the agent session cannot run; the viewer troubleshooting covers the Browser pane quirks above; feedback can reach the maintainers as a PR without write access; and there is one written rule for assets and one for color.

---

## Update: what the last stretch of the session showed

After the first version of this document, the session went on with more requests for the pet. These are the extra lessons.

### New gaps

11. **A new interaction needs a feedback checklist.** The user asked, late, for "a visual echo when I click a surface" and for "the cursor to change to a hand when petting". Both are cheap, but nobody listed them. A click or a hover on something new needs: a cursor, a hover or press state, a short toast or echo, a row in the help panel, a test hook, and a line in `SKILL.md`. The viewer already has a cursor table (`CURSOR_SHAPES`), a help panel and `__bim` hooks, so a checklist would catch all of it in one pass (proposal 7).
12. **Limits live in constants, but users ask about them.** The user asked: "what is the terrain and the small obstacle? can the cat jump?" The answer (steps of 0.8 ft are walked, ledges up to 2.5 ft are jumped, drops up to 8 ft, gaps up to about 3 ft, walls above 2.5 ft are not) was only in a `PET` constants block. The numbers should be in a table in `SKILL.md`, with the scale rule ("multiplied by the pet scale in a large model"). Done for the pet in the PR; the same habit applies to every limit (walk collision distance, export caps).
13. **Requests arrive faster than plans.** About 25 short messages came while code was running ("also ...", "should ...", one-word messages such as "feedback"). The task list and the `Agent` tool helped, but a one-word message is ambiguous (issue, PR, comment, or "the thing we just did"). `am-feedback` should ask one short question when the target or the form is unclear, and offer the likely options with buttons.
14. **Rewriting an open PR is blocked, and that is fine.** Amend and force-push were refused. New commits worked and kept the history honest (including a visible correction). Say so in `CONTRIBUTING.md` (proposal 1).

### New proposal

7. **Add an "interaction checklist" to the `am-bim-viewer` SKILL.md** for any new click, hover or drag behavior: cursor, hover or press state, echo or toast, help row, test hook (with `freeze`), a table of limits, and a check against `design/DESIGN-SYSTEM.md` before writing the feature.
