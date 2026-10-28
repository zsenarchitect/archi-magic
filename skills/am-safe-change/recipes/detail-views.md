# Recipe: detail views on a sheet

Example request: "Add two floor-transition section details to sheet A401, next to Detail 21, and match the existing viewports."

This recipe covers the field report in issue #50. A plain `Viewport.Create` leaves odd label settings and lands off the requested point. The next free detail number breaks the sheet's own pattern. This recipe copies the label settings, centers the box, and proposes numbers in the sheet pattern.

Files (paths relative to the am-safe-change skill folder):

| Step | File | Where it runs |
|---|---|---|
| 3 Read | `scripts/read_detail_views.py` | Revit, `run_query` |
| 4 Compute | `scripts/detail_preview.py compute` (uses `scripts/lib/detail_views.py`) | Terminal, CPython 3 |
| 7 Execute | `scripts/apply_detail_views.py` | Revit, `run_modify` (dry run, then real) |
| 7, 8 Compare | `scripts/detail_preview.py compare` | Terminal, CPython 3 |

Scope: each new detail copies the crop box and the view template from an existing section view. A brand-new section from scratch is out of scope. Tell the user that limit in step 2.

## Step 2: questions to ask

Ask only the questions that the request leaves open. Use AskUserQuestion, one decision per question:

1. **Which sheet?**
   - The sheet the user names. Name it back in the scope sentence.
2. **Which viewport is the reference?**
   - The viewport the new ones must match (Recommended): the closest existing viewport on the same sheet. The recipe copies its label offset, label line length, and viewport type.
   - Another viewport the user names.
3. **Which section does each new detail copy?**
   - The user names one source section per new detail. The new detail copies its crop box and view template.
4. **Where does each new viewport go?**
   - The user gives the requested box center on the sheet, in feet. Or: next to a named viewport, and you read that viewport's box center and propose a point beside it.
5. **Detail numbers?**
   - Use the proposed pattern numbers (Recommended): the compute step proposes them from the sheet's existing numbers. See "How the numbering works" below.
   - The user names the numbers.

Then say the scope back in one sentence, for example: "I will add 2 detail views to sheet A401, copy Detail 21's label settings, and number them 22 and 30."

Keynote tags: tell the user one limit before the preview. Revit cannot place a material keynote tag on a cut floor layer. See "Keynote tags" below. The preview uses text notes for keynotes when the user asks for them.

## Step 3: read

`run_query` with the content of `scripts/read_detail_views.py`. Use `inputs: {"sheet_id": <id>}` to read one sheet, or no inputs to read all sheets. Save the JSON result as `read.json`. The result has every sheet with its viewports (detail number, viewport type, label offset, label line length, box center) and every section view that can serve as a source.

## Step 4: compute

```powershell
python "<skill dir>\scripts\detail_preview.py" compute --read read.json --sheet 100 --reference 1 --detail "North transition:7.5:2.0:50" --detail "South transition:7.5:4.0:51" --out preview.json
```

Each `--detail` value is `"name:x:y:source_view_id"`. x and y are the requested box center on the sheet, in feet. The command prints the chat table. Exit code 1 means the preview is BLOCKED.

How the computation works (`scripts/lib/detail_views.py`):

- Label settings: every new viewport copies `LabelOffset` and `LabelLineLength` from the reference viewport, and its viewport type from the same reference. A plain `Viewport.Create` followed by `ChangeTypeId` left offset near -1.16 ft and line length near 0.26 ft in the field report. The existing viewports had offset (0.01, 0.01) and line length 0.81.
- Box center: the apply script calls `SetBoxCenter` with the requested point after `Viewport.Create`. A plain `Create` landed about 1.4 ft from the requested center. The verify step re-reads the box center and compares it with the preview.
- How the numbering works: the first new number continues the highest run (max + 1). Each later new number starts the next decade (the smallest multiple of 10 above the previous proposal). Example: existing numbers 10-12, 20-21, two new details, give 22 and 30. This rule is a proposal, not a fact about the sheet. The preview always shows it. The user can change it in step 6.
- Blocking warnings (Apply is not allowed):
  - The reference viewport is not on the target sheet.
  - The reference viewport has no label settings or no viewport type to copy.
  - A source section is not in the read result.
  - A proposed detail number is already used on the sheet.
- Other warnings: a detail number on the sheet is not an integer, so the compute step skips it.

## Step 7: execute

1. `run_modify` with `dry_run: true`, the content of `scripts/apply_detail_views.py`, and `inputs: {"preview_path": "<full path to preview.json>"}`. Save the result as `dryrun.json`.
2. Compare:
   `python "<skill dir>\scripts\detail_preview.py" compare --preview preview.json --actual dryrun.json`
   It must print `MATCHES PREVIEW`. If it prints `DIFFERENT`, stop.
3. Show the side-effect counts:
   `python "<skill dir>\scripts\detail_preview.py" side-effects --preview preview.json --dryrun dryrun.json`
   Show the two numbers in chat: viewports the script created on purpose, and dependent elements Revit regenerated (new views and their viewports). Say in one sentence why dependents change (the command prints it). If the command warns about an unexpected category, tell the user and ask whether to continue before you apply.
4. `run_modify` with the same script and inputs, without dry run. The user clicks Keep or Discard in Revit.

The apply script first checks that the sheet still exists, that the reference viewport still has the preview's label settings, and that every proposed detail number is still free. Then it replays the preview's `steps` in one transaction. It returns the values of every new viewport after the steps, re-read after `SetBoxCenter`.

## Step 8: verify

Run `read_detail_views.py` again, save it as `verify.json`, then:
`python "<skill dir>\scripts\detail_preview.py" compare --preview preview.json --actual verify.json`

Capture check: when you capture a view to show the user, check that the cut geometry is present. `capture_view` sometimes returns a section with the cut geometry missing, then returns it correctly on the next call. This looks like a regen timing issue after a commit. If the capture misses the cut geometry, wait a few seconds and capture again. Do not report the first capture as the result.

## Keynote tags

Revit cannot place a material keynote tag on a cut floor layer. `IndependentTag.Create` with a face reference fails with "The reference can not be tagged". An element reference creates the tag, but `TagText` reads empty for every keynote and material tag type tried. The `TM_ADDBY_MATERIAL` mode creates a Material Tags tag that also reads empty. The workaround from the field report is a text note that carries the keynote value from the floor type's `Keynote` parameter. Use text notes when the user asks for keynotes on these details. The full note is in `am-revit-api-docs`.

## Tests

`python -m pytest "<skill dir>\tests" -q`, or `python "<skill dir>\tests\test_detail_views.py"` without pytest.
