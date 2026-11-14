# Create named levels and grids

Use this recipe in Revit 2024 through 2026. Save or sync the host project before reading. Choose units and project coordinates before you make the request.

1. Call `get_context` and `get_skill("revit-scripting")`. Run `scripts/create_datums.py` with `run_query`. Set the scripts directory as the workspace. Pass the datum list as `request`.

   ```json
   {
     "units": "mm",
     "levels": [
       {"name": "Level 1", "elevation": 0},
       {"name": "Level 2", "spacing": 3000}
     ],
     "grids": [
       {
         "axis": "X", "start": 0, "extent": [-1000, 12000],
         "spacing": "3x8000, 2x6000",
         "names": ["A", "B", "C", "D", "E", "F"]
       }
     ]
   }
   ```

2. Use `mm`, `m`, or `ft` for all values in the request. A relative level row uses the previous requested elevation. A grid run starts with its first named line. Each interval adds one line. X runs step along internal X and draw parallel Y lines. Y runs do the reverse. Extents use coordinates along the perpendicular axis.
3. Save the full read result as `read.json`. Recover the complete record with `get_run` if necessary. Run this command from the scripts directory.

   ```sh
   python3 datum_preview.py compute read.json datums.json --preview preview.json
   ```

4. Render `preview.json` with am-schedule-view. Review each location and every existing-name warning. Matching names skip creation, including case variants and multi-segment grids. Relative spacing still uses the requested location of a skipped row. Stop if the preview is blocked or there are no new datums. This recipe has zero existing write targets.
5. Confirm the preview. Run `create_datums.py` with `run_modify` in dry-run mode. Pass `mode` as `"apply"`, the `plan` from `datums.json`, and the contents of `preview.json` as `preview`. Save the full result as `dry-run.json`. Repeat the original read and save it as `fresh-read.json`. Run both checks.

   ```sh
   python3 datum_preview.py side-effects dry-run.json datums.json
   python3 datum_preview.py baseline fresh-read.json datums.json
   ```

6. Check the current document and policy. Follow am-safe-change. Run the same approved input and use Keep or Discard.
7. After Keep, run `create_datums.py` in `run_query` with `mode` set to `"verify"`. Pass the same `plan`. Pass the actual kept `datums` array from `datums_json` as `created`. Save the fresh result as `verify.json` and the kept result as `kept.json`.

   ```sh
   python3 datum_preview.py compare verify.json datums.json --kept-receipt kept.json
   ```

8. After Discard, rerun the original read and baseline check. Finish with the worksharing hand-off in am-safe-change. Use the actual kept IDs for a kept run.
