# Session feedback: detail section from a marked-up screenshot to a new sheet

Source: one live session (Revit 2026.4, pyRevit 7.0.0.26278, large workshared model).
Project and user names are left out on purpose.

## The task

The user pasted a plan screenshot with a green line (section cut plus arrow) and a red dash-dot line. They asked: "connect to Revit, create a section detail at those locations, place on a new sheet, assign the template like the other detail views." They also asked the agent to ask questions first.

Result: one detail-type section, view template applied, new sheet created, view placed. The user confirmed the position. About 8 tool calls to Revit, 2 of them writes (a dry run and the commit).

## What worked

- **Questions first, four at once.** Which lines are sections, which way they look, where the template comes from, how to set up the sheet. One round, then no more interruptions. The answers "find it yourself" and "match neighbours" moved the work to the agent, which is where it belongs.
- **Learning the house style from the model.** The agent counted the view templates used by existing Detail views, read the neighbouring sheet (title block, series, group, order) and picked the next free sheet number. Nothing was hard-coded.
- **Dry run before commit.** The dry run caught nothing wrong here, but it gave a change set (view, sheet, title block, viewport) the user could trust.
- **Capture to verify.** `capture_view` on the new view showed real content, so the section was not empty or on the wrong level.

## Gaps and friction

1. **No skill covers "detail view from a plan marker".** `create.create_section_view` makes a Section-family view. The house detail views are Detail-family (`ViewFamily.Detail`), so the agent fell back to raw `ViewSection.CreateDetail(doc, type_id, BoundingBoxXYZ)`. The box transform (BasisX along the cut, BasisY up, BasisZ opposite the look direction) is easy to get wrong. A tested helper would remove the risk.
2. **Screenshot markup is not in the model.** The green line existed only in the user's image. The agent estimated the position from the one model line it could see (the red dash-dot line, 13.75 ft long) to get a pixel-to-foot scale. It works, but the result is approximate and the user has to be told so. A better flow: ask the user to drop a detail line or a reference point, or snap to a named element, or confirm the estimate with a capture overlay before any write.
3. **`Name` on a ViewFamilyType fails in IronPython.** `t.Name` raised `AttributeError: Name`. `DB.Element.Name.GetValue(t)` works. Related to existing issue 6 (`ElementId.IntegerValue` is gone in 2026, use `.Value`). Both cost a retry.
4. **`capture_view` takes a view name, not a sheet number.** `view="A-477"` returned `view_not_found`. The sheet name or id is needed. Worth a line in the views and drawings guidance, or accept sheet numbers in the tool.
5. **Choosing a template is a judgment call.** Detail views in the model used about 30 different templates. The agent chose the one used by the most recent sheet in the same area. It should say so and show the runner-up templates so the user can swap in one step.
6. **Large "added" counts after a dry run.** The preview listed 24 added elements (work plane grid, sun path, schedule graphics) for one view and one sheet. This matches the existing issue about separating "edited" from "regenerated". Group the preview by what the user asked for.
7. **Sheet open and jump links.** The user wanted a one-click jump to the new sheet. `ui.request_view_change(sheet)` in `run_query` does it. This should be the standard last step after creating a sheet, plus a "reopen" button in the wrap-up.

## Proposals

- **New skill or recipe `am-detail-sheet`** (write through `am-safe-change`):
  1. Ask the four questions above in one round.
  2. Find the Detail view family type and the template by counting what existing Detail views use. Show the top 3.
  3. Resolve the cut location. Prefer a model element or a user-placed line. If from a screenshot, say it is an estimate and capture it for confirmation.
  4. Create the view with `CreateDetail`, set name and template.
  5. Create the sheet from the neighbour's title block, copy its series, group, order and revision parameters, pick the next free number.
  6. Place on the sheet, capture the view, open the sheet.
- **Add to `am-revit-api-docs`:** `Element.Name.GetValue` for types in IronPython, and the Detail vs Section view-family difference.
- **Add to `am-safe-change`:** a "new view plus new sheet" preview that lists the created view, the sheet and the viewport on three lines and hides the regenerated helper elements.
- **Pre-flight note:** the open document may have unsynced changes. Tell the user after a commit that nothing was synchronised with central.

## Done when

A user can say "make a section detail here and put it on a new sheet like the others" and get the same result without the agent rediscovering `CreateDetail`, the box transform, or the template choice.

---

## Update: the rest of the session (5 details, layout, numbering, plan markers)

The same session went on for several more rounds. These are the extra lessons.

### What the user asked next
1. Resize the detail to focus on the floor transition.
2. Read four pink marks on a second screenshot and add details for them on the same sheet.
3. Lay the viewports out and assign view titles.
4. Learn the detail-number sequence from other sheets.
5. Make the new sections show in the 1st floor finish plan.

### New gaps and fixes

8. **Section box direction is easy to get backwards.** For `ViewSection.CreateDetail`, Revit reads `BasisZ` of the box transform as the **look direction**. Pass `BasisX = look x up` (right), `BasisY = up`, `BasisZ = look`. The first attempt used the opposite sign. Revit silently flipped the view to look south, and the user saw nothing wrong until the crop was resized. Check after creation: `CropBox.Transform.BasisX` is the right direction, `-BasisZ` is the look direction, so print them and compare with the intended compass direction. A helper `create_detail_view(origin, look, width, height, depth, type, template)` with this check would remove the mistake.
9. **Look for the model's own transition edges before trusting a screenshot.** Reading each floor's sketch profile (`floor.SketchId` then `Sketch.Profile`) near a mark gave exact edge lines (for example x = -160.2, x = -147.1, y = 171.8). Snap the cut to those edges and say so. This turned vague pixel estimates into positions that really show a transition. Also say when a mark has no edge near it (one pink mark showed a plain slab).
10. **Marks can be skipped by mistake.** After the user said "all 4", only 2 of 4 details were made because 2 looked redundant. The user then asked for the rest. If a mark looks redundant or flat, make it and flag it, or ask once. Do not silently drop it.
11. **Detail numbers follow a row scheme.** Compared across A-401, A-402, A-470 to A-474 and A-480: the tens digit is the row (1x = bottom row, 2x = next up, and so on), the units digit counts from the right edge (10 is the rightmost in the bottom row, 11 is to its left). Rows sit against the right side and gaps fall on the left. Older sheets (A-476, A-486) use plain 1, 2, 3. A sheet-layout helper should read this from the neighbouring sheets and number the new viewports to match. Set numbers through a temporary unique value first, because duplicates on one sheet are refused.
12. **Viewport type.** New viewports were "No Title". The neighbour sheet uses `D_Standard_6" Title`. Copy the viewport type from the neighbour. Call `ChangeTypeId`, then `Regenerate`, then `SetBoxCenter`, because the box size changes with the title.
13. **Sheet geometry.** The title block was 4.0 x 3.0 ft and the first stacking attempt placed a viewport off the sheet (y < 0). Read the title block and neighbour viewport boxes and keep inside them. Placing "by pitch" without a bounds check is not safe. Capture the sheet by its **name**, not its number, to check.
14. **Section markers did not show in the plan.** The plan's view template had the Sections category hidden, so none of the new markers drew. Unhiding it would show about 130 other markers. A view filter solved it: a filter on the Sections category with the rule Sheet Number is not "A-477", set to hidden, applied in the template. Only markers for details on the new sheet show, and later additions show automatically. Facts worth recording:
    - The marker elements report category "Views" but the Sections category controls them. `Category.GetCategory(doc, OST_Viewers)` returns None.
    - The filterable parameters on the Sections category include Sheet Number, View Name, Family, Family and Type and Title on Sheet. The Views category only offers Workset, so a filter created on it fails.
    - A template change affects every plan that uses it (12 here). Say so before applying, and offer a per-plan template copy.
    - A dry run cannot show this. Applying and capturing the plan was the only check, so tell the user it is a visual check after approval.
15. **API traps met in this session** (add to the Revit 2026 notes):
    - `DB.ElementId(14254608)` is ambiguous in IronPython. Use `DB.ElementId(System.Int64(14254608))`.
    - `DB.Element.Name.GetValue(t)` works for ViewFamilyType but raises on FloorType. Use `t.get_Parameter(BuiltInParameter.SYMBOL_NAME_PARAM).AsString()` for types.
    - `doc.ActiveView` becomes the sheet after `ui.request_view_change(sheet)`. Look a plan up by id or name in later queries.
    - Edits to view templates and filters show up in the change set as many modified "Views" elements (44 here). That is normal; the dry-run summary should say "dependent plans refreshed".

### Proposal additions to `am-detail-sheet`
- Step "resolve cuts": snap to model edges, list what each cut shows, flag any cut that shows no change.
- Step "make views": use the direction helper above and assert the look direction after creation.
- Step "sheet": copy the viewport type and title, number by the row scheme, right-align rows, keep inside the title block, then capture by sheet name.
- Step "plan markers": check whether the Sections category is hidden by the plan's template. If so, propose the filter approach, name the number of plans affected, and ask before changing a shared template.
- Final step: open the sheet in Revit and offer a reopen button.
