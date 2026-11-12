# Create sheets from a reviewed list

Use Revit 2026 with the CPython engine and a saved project. Work on a disposable copy during qualification.

1. For Excel, run the project document extractor with `--sheet`, the chosen `--header-row`, and `--literal-text-column "Sheet Number"`. Keep the workbook local. Review the source rows and title block labels. Use a literal text cell for a sheet number such as `001`. A chat list can use the same request fields.
2. Call `get_context` and `get_skill("revit-scripting")`. Run `scripts/create_sheets.py` with `run_query`. Set the scripts directory as the workspace. Pass the reviewed request, including exact loaded title block type IDs.

   ```json
   {
     "request": {
       "rows": [
         {
           "source_row": 2,
           "number": "001",
           "name": "Cover sheet",
           "title_block_type_id": "80",
           "parameters": [
             {
               "scope": "title_block",
               "parameter": {"kind": "guid", "id": "12345678-1234-1234-1234-123456789abc"},
               "kind": "string",
               "value": "Issue for review"
             }
           ]
         }
       ]
     }
   }
   ```

   Use `sheet` or `title_block` for instance scope. Use a shared parameter GUID, a reviewed built-in metadata identity, or a project parameter ID and UID. Omit blank workbook assignments. For Excel, include `source` in the request with the workbook file name, digest as `sha256`, sheet, header row and reviewed column mapping. Verify the digest against the same workbook before both runs.

3. Save the full read as `read.json`. Run `python sheet_creation_preview.py read.json > plan.json` from the scripts directory. Review the table with am-schedule-view. Existing sheets and placeholders that hold a requested number remain unchanged. Correct blocked rows before continuing. A batch with zero creates needs no modify run.
4. Recheck the current document and policy. Run `create_sheets.py` with `run_modify` and `dry_run:true`. Set `mode` to `"apply"` and pass the exact reviewed JSON as `plan`. Save the full result as `dry-run.json`. Repeat the original read and save it as `fresh.json`. Run this check.

   ```sh
   python sheet_creation_preview.py plan.json --run dry-run.json --fresh fresh.json --decision rolled_back
   ```

5. Follow am-safe-change. Run the same approved plan and use Keep or Discard. Under auto policy, obtain the required second approval. Save the full result as `real-run.json`.
6. After Keep, run a fresh query with `mode` set to `"verify"`. Pass the approved plan and the decoded `receipt_json` from the kept run as `receipt`. Save the result as `verify.json`. Verify exact additions and the independently read sheets.

   ```sh
   python sheet_creation_preview.py plan.json --run real-run.json --fresh verify.json --decision committed --dry-run dry-run.json
   ```

7. After Discard, repeat the original read and use `--decision discarded` with the real run and fresh read. Follow the am-safe-change worksharing hand-off for any kept sheets and title blocks.
