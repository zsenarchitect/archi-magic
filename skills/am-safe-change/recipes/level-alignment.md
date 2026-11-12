# Review rotation, offset, and reference level

Use a saved Revit 2026 project and a CPython bridge. Start with point-based one-level family instances. This workflow produces a read-only preview.

1. Call `get_context` and `get_skill("revit-scripting")`. Verify the active host project and the selected element IDs.
2. Run `scripts/read_level_alignment.py` with `run_query` and set the workspace to the skill's scripts directory. Pass a `request` object with `operation` set to `rotation`, `offset`, or `nearest_level`. Supply 1 to 30 decimal strings in `target_ids`. Supply one distinct `reference_id` for rotation or offset matching.
3. For offset or nearest-level review, first read without `verified_frame`. Record the returned `project` and `frame`. In a disposable native fixture, measure two distinct levels in Revit internal coordinates using a reference independent of this query. Use a shifted project base and shared-origin display setting when qualifying the conversion. Repeat the read with `verified_frame` containing the recorded `project`, `frame`, and `anchors`. Each anchor has the exact level `id`, `uid`, and independently measured `internal_z_ft`. Stop if the new response reports `frame_verified: false`.
4. Save the decoded `snapshot_json` as `snapshot.json`. Run `python scripts/level_alignment_preview.py snapshot.json > preview.json` from this skill folder. Render the preview with am-schedule-view. Exit 0 means a reviewable preview. Exit 1 means the preview contains blocked rows. Exit 2 means the snapshot could not be used.

The reader compares a candidate conversion using `Level.ProjectElevation` and the base point's internal Z. A live fixture must validate that conversion before it can support a model write. See the [reviewed qualification plan](../../../docs/plans/2026-11-12-level-offset-rotation-reviewed.md). [Autodesk ProjectElevation](https://help.autodesk.com/cloudhelp/2026/ENU/Revit-API-MainReference/files/html/895ef506-bfea-cc4e-31f8-aad2af6672e4.htm) · [Autodesk coordinate frames](https://help.autodesk.com/cloudhelp/2024/ENU/Revit-API/files/Revit_API_Developers_Guide/Advanced_Topics/Place_and_Locations/Revit_API_Revit_API_Developers_Guide_Advanced_Topics_Place_and_Locations_Place_html.html)

## Qualify the rollback probe

1. Use a saved, writable, non-workshared disposable copy. Keep the bridge in ask policy. Review the exact preview and confirm ownership before obtaining approval for the trial.
2. Run `python scripts/level_alignment_preview.py snapshot.json --probe-plan > probe-plan.json`. Keep the preview and plan from the same saved snapshot.
3. Recheck context and policy. Run `scripts/probe_level_alignment.py` with `run_modify`, `dry_run: true`, and the skill scripts directory as workspace. Pass `disposable_fixture: true` and the decoded plan under `plan`.
4. Save the complete run result. A successful trial reports `trial_rolled_back`, the exact modified IDs, and the measured temporary result. Repeat the original query independently and compare the complete baseline. Stop if the probe reports an unresolved transaction or the fresh baseline differs.

The probe rolls back its transaction group after checking exact change events. A successful probe is evidence for the tested fixture and operation. Live qualification and a verified Keep/Discard path are still required for persistent changes. [Autodesk transaction group rollback](https://help.autodesk.com/cloudhelp/2026/ENU/Revit-API-MainReference/files/html/2efcf628-bb40-bf36-a2e4-eaeca4cca461.htm)
