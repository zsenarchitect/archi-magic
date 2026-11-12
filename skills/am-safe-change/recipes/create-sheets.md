# Create sheets from a reviewed list

Use Revit 2026 with the CPython engine and a saved project. Work on a disposable copy during qualification.

1. Call `get_context` and `get_skill("revit-scripting")`. Run `scripts/create_sheets.py` with `run_query`, the scripts directory as workspace, and `{"mode":"title-blocks"}`. Review the loaded family and type names with their exact IDs.
2. For Excel, put sheet numbers such as `001` in literal text cells. Create a local mapping that pairs each workbook title block label with the reviewed Revit type ID.

   ```json
   {
     "sheet": "Drawing Index",
     "header_row": 1,
     "columns": {"number": "Sheet Number", "name": "Sheet Name", "title_block": "Title Block"},
     "title_block_types": {"A1 sheet": "80"},
     "parameters": [
       {
         "column": "Issue text",
         "scope": "title_block",
         "parameter": {"kind": "guid", "id": "12345678-1234-1234-1234-123456789abc"},
         "kind": "string"
       }
     ]
   }
   ```

   Run `python sheet_workbook.py index.xlsx mapping.json > request.json` from the scripts directory. Install the am-markup-intake document dependencies for XLSX. Review the source rows, title block IDs, and mapped values. Blank parameter cells omit the assignment. For a chat list, prepare `{"request":{"rows":[...]}}` directly. Give each row a string `number`, `name`, `title_block_type_id`, and any `parameters` with their typed `value`. Use `sheet` or `title_block` for instance scope.

   Run `create_sheets.py` with `run_query` and the reviewed request as inputs.

3. Save the full read as `read.json`. Run `python sheet_creation_preview.py read.json > plan.json` from the scripts directory. Review the table with am-schedule-view. Existing sheets and placeholders that hold a requested number remain unchanged. Correct blocked rows before continuing. A batch with zero creates needs no modify run.
4. For Excel, run `python sheet_workbook.py index.xlsx mapping.json --check plan.json`. Stop if the workbook or mapping changed. Recheck the current document and policy. Run `create_sheets.py` with `run_modify` and `dry_run:true`. Set `mode` to `"apply"` and pass the exact reviewed JSON as `plan`. Save the full result as `dry-run.json`. Repeat the original read and save it as `fresh.json`. Run this check.

   ```sh
   python sheet_creation_preview.py plan.json --run dry-run.json --fresh fresh.json --decision rolled_back
   ```

5. For Excel, repeat the workbook check. Follow am-safe-change. Run the same approved plan and use Keep or Discard. Under auto policy, obtain the required second approval. Save the full result as `real-run.json`.
6. After Keep, run a fresh query with `mode` set to `"verify"`. Pass the approved plan and the decoded `receipt_json` from the kept run as `receipt`. Save the result as `verify.json`. Verify exact additions and the independently read sheets.

   ```sh
   python sheet_creation_preview.py plan.json --run real-run.json --fresh verify.json --decision committed --dry-run dry-run.json
   ```

7. After Discard, repeat the original read and use `--decision discarded` with the real run and fresh read. Follow the am-safe-change worksharing hand-off for any kept sheets and title blocks.
