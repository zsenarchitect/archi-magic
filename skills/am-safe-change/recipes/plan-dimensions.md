# Review plan dimensions

Use Revit 2026 with a saved host project and an independent floor plan. Assign and review a horizontal work plane. The reader uses this plane to capture geometry and does not change the model.

1. Call `get_context` and `get_skill("revit-scripting")`. Confirm the active plan and choose a linear dimension type.
2. Run `scripts/read_plan_dimensions.py` through `run_query`. Set the skill's scripts directory as the workspace. Pass a `request` with the exact decimal strings `view_id` and `dimension_type_id`, plus up to 20 `requests`. Each row has a unique `key`, a `mode`, two internal-foot endpoints in `line`, and `source_ids` as decimal strings.
3. Use `grid` for a straight grid string. For `overall`, use two wall IDs and a matching `faces` array with `side` set to `interior` or `exterior` for each wall. Include the exact `stable_ref` if the side exposes more than one face.
4. For opening discovery, pass the view ID and `opening_wall_ids` as a separate request. It lists supported candidates and unresolved inserts. Use `opening` with the wall ID followed by the opening ID. If native references have no resolvable plane, save the returned `opening` candidate. Supply it under `opening_evidence.opening` with two independently measured view-plane `traces` in left and right reference order. Each trace is a pair of XYZ points in internal feet.
5. Save the decoded `snapshot_json` and run `python scripts/plan_dimension_preview.py snapshot.json > preview.json` from the skill directory. Render the preview with am-schedule-view. Check measured spans and unresolved references before requesting a native qualification trial.

The reference forms and visibility checks still need qualification in a disposable Revit project. This workflow ends with a review-only preview. [Autodesk view sketch plane](https://help.autodesk.com/cloudhelp/2026/ENU/Revit-API-MainReference/files/html/2531634f-f0d4-3cb3-7f8a-fe809d33a61f.htm) · [Autodesk hosted insert discovery](https://help.autodesk.com/cloudhelp/2026/ENU/Revit-API-MainReference/files/html/58990230-38cb-3af7-fd25-96ed3215a43d.htm)

## Qualify dimension creation with rollback

1. Use a saved, writable, non-workshared disposable model. Keep the bridge in ask policy. Check the destination view's ownership and obtain approval for the exact preview.
2. Run `python scripts/plan_dimension_preview.py snapshot.json --probe-plan > probe-plan.json`. Keep both outputs from the same snapshot. The qualification payload contains only resolved proposals.
3. Recheck context and policy. Run `scripts/probe_plan_dimensions.py` with `run_modify`, `dry_run: true`, and the skill scripts directory as workspace. Supply `disposable_fixture: true` and the decoded payload under `plan`.
4. Save the full trial record. If it reports `trial_rolled_back`, repeat the original query and supply its exact `temporary_dimension_ids` as `check_absent_ids` alongside `request`. Save the complete original, trial, and fresh records as separate JSON files. Run `python scripts/verify_plan_dimension_probe.py probe-plan.json original.json trial.json fresh.json`. Stop unless it reports `plan_dimension_rollback_verified`.

The probe verifies the geometry and exact added dimensions while a transaction group can still roll back. Persistent Keep/Discard remains pending live Revit qualification. [Autodesk NewDimension](https://help.autodesk.com/cloudhelp/2026/ENU/Revit-API-MainReference/files/html/475aab91-19d3-5884-d3eb-18dfc0d4004f.htm) · [Autodesk transaction group rollback](https://help.autodesk.com/cloudhelp/2026/ENU/Revit-API-MainReference/files/html/2efcf628-bb40-bf36-a2e4-eaeca4cca461.htm)
