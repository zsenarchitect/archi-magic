# Copy or combine instance parameters

Use this recipe in Revit 2024 through 2026. Choose one built-in category and an instance destination parameter. Enter exact parameter names as shown in Revit. Save or sync before reading the model.

1. Follow the am-safe-change context and policy steps. Use `scripts/parameter_copy.py` in `run_query`. Set the scripts directory as the workspace. Pass `{"mode":"read","request":{"category_id":"-2000014","source":"Comments","destination":"Description"}}`. Add `element_ids` to select specific instances within that category. Write element IDs as decimal strings.
2. To combine values, replace `source` with `"parts":[{"parameter":"Level"},{"literal":" / "},{"parameter":"Number"}]`. Use a text destination. Revit supplies display values for numeric and reference parts. The recipe joins the parts in order.
3. Save the full run record as `read.json`. Recover the result with `get_run` if needed. From the scripts directory, run `python parameter_copy_preview.py compute read.json changes.json --preview preview.json`. Render `preview.json` with am-schedule-view. Stop if the preview is blocked or there are no changes. The plan in `changes.json` records category, design-option counts, and a target ID hash.
4. Check ownership with `ownership.py`. Use `preview.json` for the target IDs. Add the ownership result to that preview. Review and approve the exact changes.
5. Run `parameter_copy.py` with `run_modify` and `dry_run: true`. Pass `mode: "apply"`, the `plan` from `changes.json`, and the current contents of `preview.json` as `preview`. Save the full run record as `dry-run.json`. Run `python parameter_copy_preview.py side-effects changes.json dry-run.json`. Review any reported differences or additional elements before proceeding.
6. Repeat the original query. Run `python parameter_copy_preview.py baseline changes.json fresh-read.json`. Check the current model and policy before the real run.
7. Run the same approved input with `run_modify`. Use Keep or Discard in Revit. Follow the am-safe-change approval instructions if policy is `auto`.
8. After Keep, repeat the query and run `python parameter_copy_preview.py compare changes.json kept-read.json`. After Discard, use `baseline` to verify the original values. Finish with the am-safe-change ownership hand-off.

A category run supports up to 500 instances. A copy requires compatible Revit storage and data types. The recipe reports missing, ambiguous, unset, read-only, grouped, or unsupported targets before a write. Select a smaller batch when the read exceeds its budget.
