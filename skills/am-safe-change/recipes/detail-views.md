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
| New section: read markers | `scripts/read_section_markers.py` | Revit, `run_query` |
| New section: plan cuts | `scripts/section_mark_plan.py plan` (uses `scripts/lib/section_marks.py`) | Terminal |
| Markers: read views, placements and markers | `scripts/detail_read_references.py` | Revit, `run_query` |
| Markers: same condition, audit, plan, compare | `scripts/detail_marker_plan.py` (uses `scripts/lib/detail_marker_logic.py`) | Terminal, CPython 3 |
| Markers: execute | `scripts/detail_apply_markers.py` | Revit, `run_modify` (dry run, then real) |

Scope: each new detail copies an existing section view: its crop box and its view template. See "Copy an existing cut" for the two copy methods. For a brand-new section at a mark on a plan (for example from a reviewer's PDF markup), use "New section from a markup" below. To point a plan marker to another detail, use "Point a marker to another detail". To remove a detail view, use "Remove a detail view".

Look direction (issue #257): `ViewSection.CreateDetail` reads the box `BasisZ` as the look-into direction. A view's `CropBox.BasisZ` points the other way, towards the viewer (the same as `View.ViewDirection`). `apply_detail_views.py` therefore turns the source crop box 180 degrees about its up axis before `CreateDetail` (same world volume), and stops when the new view's `ViewDirection` differs from the source's. The result lists both directions under `directions`.

## Step 2: questions to ask

First do the "Same condition check" below. Do not offer a new view before it.

Ask only the questions that the request leaves open. Use AskUserQuestion, one decision per question:

0. **Use the existing detail?** Ask this only when the same condition check finds a match.
   - Point the marker to the existing detail (Recommended). Name the detail, its number and its sheet. Then use "Point a marker to another detail". No new view.
   - Make a new detail view. Say that the sheet then shows the same condition twice.
1. **Which sheet?**
   - The sheet the user names. Name it back in the scope sentence.
2. **Which viewport is the reference?**
   - The viewport the new ones must match (Recommended): the closest existing viewport on the same sheet. The recipe copies its label offset, label line length, and viewport type.
   - Another viewport the user names.
3. **Which section does each new detail copy?**
   - The user names one source section per new detail. The new detail copies its crop box and view template.
4. **How does the new detail copy the section?** See "Copy an existing cut".
   - Duplicate the view (Recommended): `View.Duplicate`. It also keeps the other view settings, such as "Hide at scales coarser than".
   - Create a detail from the crop box: `ViewSection.CreateDetail`. Offer it when the duplicate checks stop the dry run.
5. **Where does each new viewport go?**
   - The user gives the requested box center on the sheet, in feet. Or: next to a named viewport, and you read that viewport's box center and propose a point beside it.
6. **Detail numbers?**
   - Use the proposed pattern numbers (Recommended): the compute step proposes them from the sheet's existing numbers. See "How the numbering works" below.
   - The user names the numbers.

Then say the scope back in one sentence, for example: "I will add 2 detail views to sheet A401, copy Detail 21's label settings, and number them 22 and 30."

Keynote tags: tell the user one limit before the preview. Revit cannot place a material keynote tag on a cut floor layer. See "Keynote tags" below. The preview uses text notes for keynotes when the user asks for them.

## Same condition check (duplicate by condition)

A position check finds a new cut near an existing marker. It does not find a detail of the same condition that was cut on another level. In issue #257 the sheet already had a detail for the same pair of floor finishes, cut on another level. The agent recommended a new view, the user kept it, then asked to delete it. Do this check before every new detail, also in the markup path.

1. Read: `run_query` with the content of `scripts/detail_read_references.py` and `inputs: {"sheet_id": <detail sheet id>}`. For a cut from a markup, also add `"cuts": [{"name": "M1", "start": [x, y], "end": [x, y], "z_min": <ft>, "z_max": <ft>}]`. Save the result as `refs.json`. It lists each view with its sheet placement and title on sheet, and the floor types that each detail on the sheet shows. For each cut, it lists the floor types whose bounding box meets a thin box along the cut. That list is a candidate list. Confirm it with the user.
2. Compare:

   ```powershell
   python "<skill dir>\scripts\detail_marker_plan.py" same-condition --read refs.json --sheet <id> --term "carpet" --term "porcelain tile" --cut M1
   ```

   Give each part of the condition as one `--term` (for example the two finish names), or the floor types with `--cut` or `--floor-type`. A term matches the view name or the title on sheet, as whole words, in any order.
3. Exit code 1 means a placed detail shows every part of the condition. Ask question 0 in step 2. Recommend the existing detail. Show the partial matches too, with what each one misses.
4. Exit code 0 means no detail shows the whole condition. Say so in one sentence, then continue.

## Step 3: read

`run_query` with the content of `scripts/read_detail_views.py`. Use `inputs: {"sheet_id": <id>}` to read one sheet, or no inputs to read all sheets. Save the JSON result as `read.json`. The result has every sheet with its viewports (detail number, viewport type, label offset, label line length, box center) and every section view that can serve as a source.

## Step 4: compute

```powershell
python "<skill dir>\scripts\detail_preview.py" compute --read read.json --sheet 100 --reference 1 --detail "North transition:7.5:2.0:50" --detail "South transition:7.5:4.0:51" --out preview.json
```

Each `--detail` value is `"name:x:y:source_view_id"` or `"name:x:y:source_view_id:method"`. x and y are the requested box center on the sheet, in feet. The method is `duplicate` or `create_detail`. Give the method for all details with `--copy-method duplicate`. A method in a `--detail` value wins. The default is `create_detail`. The command prints the chat table, with one note that names the copy method of each detail. Exit code 1 means the preview is BLOCKED.

How the computation works (`scripts/lib/detail_views.py`):

- Label settings: every new viewport copies `LabelOffset` and `LabelLineLength` from the reference viewport, and its viewport type from the same reference. A plain `Viewport.Create` followed by `ChangeTypeId` left offset near -1.16 ft and line length near 0.26 ft in the field report. The existing viewports had offset (0.01, 0.01) and line length 0.81.
- Box center: the apply script calls `SetBoxCenter` with the requested point after `Viewport.Create`. A plain `Create` landed about 1.4 ft from the requested center. The verify step re-reads the box center and compares it with the preview.
- How the numbering works: the first new number continues the highest run (max + 1). Each later new number starts the next decade (the smallest multiple of 10 above the previous proposal). Example: existing numbers 10-12, 20-21, two new details, give 22 and 30. This rule is a proposal, not a fact about the sheet. The preview always shows it. The user can change it in step 6.
- Blocking warnings (Apply is not allowed):
  - The reference viewport is not on the target sheet.
  - The reference viewport has no label settings or no viewport type to copy.
  - A source section is not in the read result.
  - A proposed detail number is already used on the sheet.
  - A copy method is not `duplicate` or `create_detail`.
- Other warnings: a detail number on the sheet is not an integer, so the compute step skips it.

## Step 7: execute

1. `run_modify` with `dry_run: true`, the content of `scripts/apply_detail_views.py`, and `inputs: {"preview_path": "<full path to preview.json>"}`. Save the result as `dryrun.json`.
2. Compare:
   `python "<skill dir>\scripts\detail_preview.py" compare --preview preview.json --actual dryrun.json`
   It must print `MATCHES PREVIEW`. If it prints `DIFFERENT`, stop.
3. Show the side-effect counts:
   `python "<skill dir>\scripts\detail_preview.py" side-effects --preview preview.json --dryrun dryrun.json`
   Show the two numbers in chat: viewports the script created on purpose, and dependent elements Revit regenerated (new views and their viewports). Say in one sentence why dependents change (the command prints it). If the command warns about an unexpected category, tell the user and ask whether to continue before you apply.
   Give it the whole saved run record (with `changes`). Then it also names the helper elements of each new section view. These are expected. Do not run the dry run again or read the model to explain them. In issue #257, one new section view plus one viewport gave 10 added elements:
   - the new view and its section marker (Views);
   - the planned viewport, plus one hidden viewport on no sheet (Viewports, `SheetId` -1, title "No Title");
   - the sun path (Sun Path) and the work plane grid (Work Plane Grid);
   - a sketch plane and an extent element (no category);
   - the sheet that gets the viewport (Sheets).
   The command marks a count above these numbers, or another category, as unexpected, and exits with code 1. Then tell the user and ask before you apply. For a run with no recipe script, `python "<skill dir>\scripts\detail_marker_plan.py" helpers --dryrun dryrun.json --new-views <n> --viewports <n>` gives the same names.
4. `run_modify` with the same script and inputs, without dry run. The user clicks Keep or Discard in Revit.

The apply script first checks that the sheet still exists, that the reference viewport still has the preview's label settings, and that every proposed detail number is still free. It also checks that no view has the name of a new detail, and that each source for `duplicate` can be duplicated (`CanViewBeDuplicated`). If a check fails, it stops with no change. Then it replays the preview's `steps` in one transaction. Each new view gets the detail name from the preview. It returns the values of every new viewport after the steps, re-read after `SetBoxCenter`. The `directions` list gives the copy method of each new view, its view direction and the source's. For `duplicate`, `same_crop` must be true.

## Step 8: verify

Run `read_detail_views.py` again, save it as `verify.json`, then:
`python "<skill dir>\scripts\detail_preview.py" compare --preview preview.json --actual verify.json`

Marker audit: run `detail_read_references.py` with no inputs, save it as `refs-after.json`, then:
`python "<skill dir>\scripts\detail_marker_plan.py" audit --read refs-after.json`
It lists each reference marker whose target view is on no sheet, with the view the marker sits in and the target view. Such a marker shows `-/---`. A plan filter can hide it, and Revit gives no warning. Exit code 1 means it found one. Report each one and ask the user what to do (see "Remove a detail view").

When the user says they did a step by hand (for example "I deleted the detail"), read the model again before you close the task. Run the marker audit. A manual delete of the viewport leaves the view and its markers in the model.

Capture check: when you capture a view to show the user, check that the cut geometry is present. `capture_view` sometimes returns a section with the cut geometry missing, then returns it correctly on the next call. This looks like a regen timing issue after a commit. If the capture misses the cut geometry, wait a few seconds and capture again. Do not report the first capture as the result.

## New section from a markup

Use this path when the user marks new cuts on a plan, for example in a PDF markup or a screenshot, and no section exists there yet. It comes from issue #257: about 20 new detail sections from one markup.

1. **Read the marks.** Use `am-markup-intake`. A flattened PDF has no annotations, so the marks are found by color (see its "Flattened PDF" section). For each section mark, write a name, the two ends of the cut line in model coordinates (feet), and one point on the thick bar or the arrow (`bar_side`). The look direction is towards the bar side. Reviewers often draw the head and the tail mirrored, so the bar side decides, not the order of the ends. When a position comes from an image, it is an estimate. Snap it to a model edge when you can (for example a floor sketch edge near the mark), and say so.
2. **Check your first guess.** Colored marks on a PDF are usually the reviewer's, not model markers. Read the model before you assume a mark is an existing marker. A view filter in the plan template can hide existing markers.
3. **Read the existing markers.** Run `scripts/read_section_markers.py` with `run_query`. Save the result as `markers.json`. It lists the cut line of every section and detail view.
4. **Plan the cuts and remove duplicates.** Never cut the same spot twice.

   ```powershell
   python "<skill dir>\scripts\section_mark_plan.py" plan --marks marks.json --markers markers.json --bottom-z <ft> --top-z <ft> --depth <ft> --padding 1 --tolerance-ft 1.0 --level-z <plan level elevation> --out plan.json
   ```

   A mark closer than the tolerance to an existing marker (and within 10 degrees of parallel) is a duplicate. So is a mark that repeats an earlier mark. Duplicates get no box. Show each one to the user with the existing marker and the distance. Exit code 1 means duplicates were found.

   Then do the "Same condition check" for each new mark, with the mark's cut in `cuts`. A position check does not find a detail of the same condition on another level. When a detail matches, recommend a reference section to the existing detail (`--add-reference` in "Point a marker to another detail"). A new view is the second option.
5. **Ask** (AskUserQuestion, one decision per question): the detail view type and template (count the templates of existing Detail views and offer the top 3), and where the markers show:
   - **Markers on the finish floor plans only (Recommended)**: see step 9.
   - **Markers in every view that cuts them**: the Revit default.
6. **Preview** one row for each new view: name, look direction as a compass direction, cut line, template, sheet.
7. **Apply** (one transaction, dry run first). For each planned box, build a `BoundingBoxXYZ` from `plan.json` (`box.origin`, `basis_x`, `basis_y`, `basis_z`, `min`, `max`; the frame is right-handed, as `BoundingBoxXYZ` requires) and call `ViewSection.CreateDetail(doc, detail_type_id, box)`. Then check the direction at once: `ViewDirection` must be minus the planned look (`section_marks.view_direction_ok`). If not, stop. Do not flip it in a second run. Set the name and the template.
8. **Hide grids, levels and other markers in each new detail view.** Level heads, grid bubbles and the markers of other views made the viewports larger than their crop. Hide the categories Grids, Levels, Sections, Elevations and Callouts in each new view (`View.SetCategoryHidden`). When the view template controls Visibility/Graphics, the view cannot change it. Then propose a template that hides them, and say which views use that template.
9. **Markers on the finish floor plans only.** A detail section shows its marker in every view that cuts it. It has no "Show in" setting. What worked:
   - On each new section, set "Hide at scales coarser than" (`SECTION_COARSER_SCALE_PULLDOWN_IMPERIAL`, or `..._METRIC`) to 12" = 1'-0". Read the parameter first and keep its storage type. Check the value in the dry run.
   - On each finish floor plan, add a view-specific reference section at the same spot: `ViewSection.CreateReferenceSection(doc, plan_id, new_view_id, head, tail)`. Use the ends of the cut line. Check the head and tail against the look direction. `detail_apply_markers.py` does this with an `add_reference` step (see "Point a marker to another detail").
   - To move a cut later: set the scale back, move the section, then set the scale again. Tell the user this in the summary.
   - A view filter is another way. It must be on the Sections category with the rule "Sheet Number does not equal <sheet>". A rule on the markers' own category fails with "One of the given rules refers to a parameter that does not apply to this filter's categories", because that category offers only Workset. A template change reaches every plan that uses the template. Say how many before you apply.
10. **Use the target view id.** In every later query, find the plan or the new view by its id (or name). Do not use `doc.ActiveView`. After a view change, `doc.ActiveView` can be the sheet, and a query then finds nothing, with no error.
11. **Verify.** Re-read each new view's `ViewDirection` and compare it with the planned look. Then do step 8 of the skill. Capture a sheet by its name, not its number.

## Point a marker to another detail

Use this path to point an existing plan marker (a reference section, callout or elevation) to another view, or to add a reference section to an existing detail. It comes from issue #257. The agent wrote its own script for these steps, and so it skipped the recipe's checks. Use the recipe scripts. Follow the 8 steps of the skill: preview, ownership check, approval, dry run, Keep or Discard, verify.

1. **Read.** `run_query` with `scripts/detail_read_references.py` (no inputs, or `sheet_id`). Save it as `refs.json`. Each entry in `references` has the marker id, its kind, the view it sits in (`host_view_id`) and the view it points to (`target_view_id`).
2. **Compute.**

   ```powershell
   python "<skill dir>\scripts\detail_marker_plan.py" plan --read refs.json --retarget "<marker id>:<view id>" --out markers.preview.json
   ```

   Add a reference section with `--add-reference "<host view id>:<target view id>:hx,hy,hz:tx,ty,tz"` (head and tail in feet). The preview has one row for each marker: the view it sits in, the view it points to now, and the view it will point to, with the detail number and the sheet of each. Exit code 1 means the preview is BLOCKED. A reference section can point only to a section, detail or drafting view. A marker cannot point to the view it sits in.
3. **Ownership.** Do step 4 of the skill. The preview lists the edited ids in `ownership_ids`.
4. **Execute.** `run_modify` with `dry_run: true`, the content of `scripts/detail_apply_markers.py`, and `inputs: {"preview_path": "<full path to markers.preview.json>"}`. Save the whole run record as `dryrun.json`. Then:
   `python "<skill dir>\scripts\detail_marker_plan.py" compare --preview markers.preview.json --actual dryrun.json`
   It must print `MATCHES PREVIEW`. Then do sections 1 to 3 of [check-and-verify.md](check-and-verify.md). Then run the same script without dry run. The user clicks Keep or Discard.
   - A `retarget` step calls `ReferenceableViewUtils.ChangeReferencedView`. The script first checks that the marker still points to the view in the preview.
   - An `add_reference` step calls `ViewSection.CreateReferenceSection`. That method returns nothing. The script finds the new marker as the one new entry in the host view's `GetReferenceSections()`.
5. **Verify.** Read `detail_read_references.py` again, save it as `refs-after.json`, then run `compare` with `--actual refs-after.json`, and the marker audit (step 8).

New detail, then point the marker to it: when the user chose a new view in question 0, make the view with steps 3 to 8 of this recipe first. To copy an existing cut, use the `duplicate` method (see "Copy an existing cut"). After Keep, read again and point the marker to the new view with this path. Two small changes are safer than one script that the recipe does not check.

## Remove a detail view

Use this path when the user asks to delete a detail, or when the marker audit finds a view on no sheet with markers that point to it. It comes from issue #257.

Rules:
- Deleting a section view also deletes every reference marker that still points to it. Revit gives no warning.
- Deleting only the viewport keeps the view in the model, on no sheet. Its markers still point to it and show `-/---`. A plan filter that hides markers of views on no sheet then hides them, so the plan has no marker at all. In a printed set, the marker is simply missing.
- So: point every marker to another view first, then delete the view. Never delete a plan marker without asking. Ask about each marker separately.

Steps:

1. **Read.** `run_query` with `scripts/detail_read_references.py` and `inputs: {"dependents_of": [<view id>]}`. Save it as `refs.json`. `dependents` lists every element that Revit deletes with the view (`GetDependentElements`), with its category and name.
2. **State.** `python "<skill dir>\scripts\detail_marker_plan.py" state --read refs.json --view <view id>` prints one of these:
   - `placed`: the view has a viewport on a sheet.
   - `viewport removed`: the view is on no sheet. For example, the user deleted the viewport by hand. The same steps apply.
   - `view missing`: nothing to delete. Run the marker audit for markers that still show `-/---`.
   It also lists every marker that points to the view, and the view each one sits in.
3. **Ask** (AskUserQuestion, one question for each marker):
   - Point it to another detail (Recommended). Offer the detail that the "Same condition check" finds.
   - Delete the marker with the view. The marker goes into the Delete list.
4. **Compute.**

   ```powershell
   python "<skill dir>\scripts\detail_marker_plan.py" plan --read refs.json --retarget "<marker id>:<other view id>" --remove-view <view id> --out remove.preview.json
   ```

   Use `--delete-marker <marker id>` only for a marker that the user chose to delete. A marker with no decision blocks the preview, and the output prints the question to ask. The steps are in this order: the retargets, then the delete. The `deletions` list has the view, its hidden viewport, its other dependents and the markers that the user chose to delete. It does not have the retargeted markers.
5. **Delete approval.** Run `python "<skill dir>\scripts\delete_approval.py" remove.preview.json`. Show the full Delete list and record the SHA-256, as step 6 of the skill says. Do step 4 of the skill (ownership) too.
6. **Execute.** Dry run with `scripts/detail_apply_markers.py` and `inputs: {"preview_path": "<full path to remove.preview.json>"}`. Save the whole run record as `dryrun.json`. Then:
   - `python "<skill dir>\scripts\detail_marker_plan.py" compare --preview remove.preview.json --actual dryrun.json`
   - `python "<skill dir>\scripts\delete_approval.py" remove.preview.json --dry-run dryrun.json --approved-preview-sha256 <digest>`
   Both must pass. After the retargets, the apply script reads the view's dependents again. If they differ from the Delete list, it stops, and Revit rolls everything back. Then run the real change. The user clicks Keep or Discard.
7. **Verify.** Read `detail_read_references.py` again. Run `compare` with the fresh read and the marker audit (step 8). The view must be gone, and each retargeted marker must point to its new view.

## Copy an existing cut

`apply_detail_views.py` copies a source section in one of two ways. Each step of the preview has a `method`.

- `duplicate` (Recommended): `View.Duplicate(ViewDuplicateOption.Duplicate)`. In issue #257 it kept the crop, view direction, template, far clip and "Hide at scales coarser than" setting of an existing section, with no flip. The dry run and the read after Keep showed the same view direction as the source. The copy sits at the same spot as the source. The script checks `CanViewBeDuplicated` before the transaction. After the copy, it checks that the new view has the source's `ViewDirection` and crop box (`same_crop`). If not, it stops, and Revit rolls everything back. `View.Duplicate` names the copy "<source> Copy 1". The script gives it the detail name from the preview.
- `create_detail` (the default when a step has no method): `ViewSection.CreateDetail` with the source crop box turned 180 degrees (see "Look direction"). It copies the crop box (with its depth) and the view template. It does not copy the other view settings, such as "Hide at scales coarser than". When the user needs them, compare them with the source in the dry run.

Use `duplicate` for the case of issue #257: "copy the old cut into a new detail", then point the plan marker to the new detail. Do the "Same condition check" first. Then make the copy with steps 4 to 8 (`--copy-method duplicate`). After Keep, point the marker to the new view with "Point a marker to another detail".

`View.Duplicate` was seen on one section view type in Revit 2026 only. The direction and crop checks catch a different result in another Revit year or view type. If the dry run stops on them, use `create_detail`.

## Keynote tags

Revit cannot place a material keynote tag on a cut floor layer. `IndependentTag.Create` with a face reference fails with "The reference can not be tagged". An element reference creates the tag, but `TagText` reads empty for every keynote and material tag type tried. The `TM_ADDBY_MATERIAL` mode creates a Material Tags tag that also reads empty. The workaround from the field report is a text note that carries the keynote value from the floor type's `Keynote` parameter. Use text notes when the user asks for keynotes on these details. The full note is in `am-revit-api-docs`.

## Tests

`python -m pytest "<skill dir>\tests" -q`, or `python "<skill dir>\tests\test_detail_views.py"` without pytest. The marker logic tests are in `tests\test_detail_marker_logic.py`.
