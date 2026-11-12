# Repair reviewed Revit warning pairs

Use this procedure to qualify issue #139 in a saved disposable Revit project. Use the standard am-safe-change approval steps. Have the user choose which element to keep and which to remove.

The reader supports two native warning cases. It checks unhosted furniture duplicates and exactly coincident straight room separation lines. Send blocked cases to the issue board.

1. Call `get_context` and `get_skill("revit-scripting")`. Record the Revit year, engine, policy, and project. Use the `scripts` directory as the runtime workspace.
2. Read the warning triage report in am-model-health. Use a reviewed pair from one native warning. Run `warning_repair.py` with `run_query`, `mode: "read"`, and a request of this form:

   ```json
   {
     "pairs": [
       {
         "failure_id": "<warning definition GUID>",
         "keep_id": "<decimal Revit ID>",
         "remove_id": "<decimal Revit ID>"
       }
     ]
   }
   ```

3. Save the full runtime response as `read.json`. Recover the complete script result with `get_run` if it is paged. Put it in the response's `result` field. Run `python warning_repair_preview.py read.json plan.json preview.json`.
4. Check ownership for the removal IDs. Show the keeper, removal, and expected warning-count change for each pair. Obtain approval of the preview. Stop when the preview is blocked.
5. Call `get_context` again. Run `warning_repair.py` with `run_modify` and `dry_run: true`. Use `mode: "apply"` and pass the contents of `plan.json` and the reviewed `preview.json` as `plan` and `preview`. Save the complete result as `dry.json`.
6. Repeat the original query with the same request. Save the fresh response as `restored.json`. The verifier requires a complete query record with a run ID distinct from the dry run. Run `python warning_repair_preview.py verify-dry-run plan.json dry.json --fresh restored.json`. Continue only after this command succeeds. Report any new borrowing separately from model changes.
7. Recheck the project and policy. Obtain the separate post-dry-run approval under auto policy. Run the same approved plan for Keep or Discard. If the dry run left removal elements borrowed, have the user review the exact IDs. Pass those IDs as `approved_borrowing` in the apply inputs. Save the full response as `final.json`.
8. After Keep, run a fresh query with `mode: "verify"` and the approved `plan`. Save it as `verified.json`. Run `python warning_repair_preview.py verify-keep plan.json final.json --fresh verified.json`.
9. After Discard, repeat the original query and save its response as `restored.json`. Run `python warning_repair_preview.py verify-discard plan.json final.json --fresh restored.json`. Follow the worksharing handoff if elements remain borrowed.

Treat an unresolved transaction as an unresolved model state. Ask the user to check Revit before continuing. Record the verification on a disposable model before using this procedure on a project model.
