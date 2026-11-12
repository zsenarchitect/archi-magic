# Place existing views, legends, and schedules

Use Revit 2026 with CPython and a saved project. Qualify the native placement and unit behavior on a disposable copy first. Use a known six-inch physical offset on two paper sizes to confirm that both native anchors match the requested half foot.

1. Call `get_context` and `get_skill("revit-scripting")`. Run `scripts/place_sheet_views.py` with `run_query`, the scripts directory as workspace, and `{"mode":"sheet-catalog","sheet_ids":["10","11"]}`.
2. Measure each usable rectangle on the exact destination sheet. Exclude stamp and revision graphics. Use absolute sheet coordinates in feet for both the rectangle and placement. Use the title block IDs, UIDs and revision digests from the catalog. Choose the source view IDs. A viewport uses its box center. A schedule uses its insertion point.

   ```json
   {
     "request": {
       "clearance_ft": 0.05,
       "placements": [
         {"view_id": "90", "sheet_ids": ["10", "11"], "point_ft": [1.0, 0.5]}
       ],
       "sheets": {
         "10": {
           "usable_ft": [0.1, 0.1, 3.0, 2.0],
           "title_block_id": "30",
           "title_block_uid": "<catalog UID>",
           "revision_sha256": "<catalog digest>"
         },
         "11": {
           "usable_ft": [0.1, 0.1, 2.0, 1.5],
           "title_block_id": "31",
           "title_block_uid": "<catalog UID>",
           "revision_sha256": "<catalog digest>"
         }
       }
     }
   }
   ```

3. Run the same script with `run_query` and the request. Save the full record as `read.json`. Run `python sheet_placement_preview.py read.json > plan.json`. Render the plan with am-schedule-view. Stop on blocked rows. Exact repeats need no transaction. The native dry run measures the actual body, label and schedule bounds.
4. Check destination sheet ownership and the current project and policy. Run the script with `run_modify`, `dry_run:true`, `mode:"apply"` and the exact reviewed `plan`. Save the full record as `dry.json`. Run a fresh original query and save it as `fresh.json`.

   ```sh
   python sheet_placement_preview.py plan.json --run dry.json --fresh fresh.json --decision rolled_back
   ```

5. Use the same approved plan for the real run. Follow am-safe-change and use Keep or Discard. Under auto policy, get the required second approval. Save the full run as `real.json`.
6. After Keep, query the script with `mode:"verify"`, the approved plan, and the decoded kept `receipt_json` as `receipt`. Save the full query as `verified.json`. Check exact placements and the native bounds.

   ```sh
   python sheet_placement_preview.py plan.json --run real.json --fresh verified.json --decision committed --dry-run dry.json
   ```

7. After Discard, repeat the original query and use `--decision discarded` with its full record. Check for borrowing after dry run or Discard. Report kept placement IDs and complete the am-safe-change worksharing hand-off.

The workflow preserves existing placements. It supports one placement per ordinary view and repeats legends and whole schedules on different sheets. Views with a Saved Position, split schedules and schedules filtered by sheet require a separate workflow.
