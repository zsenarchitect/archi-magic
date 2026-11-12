# RFC 003: MEP Support Architecture (Issue #186)

- **Author**: archi-magic architecture team
- **Status**: Phase 0 Scoping Approved
- **Target Milestones**: Phase 0 (Scoping & Go/No-Go) -> Phase 1 (Read-Only MEP QA) -> Phase 2 (Safe Parameter Adjustments) -> Phase 3 (Assisted Routing & Sizing)

## 1. Context and Problem Statement
archi-magic historically focused on architectural standards, rooms, doors, schedules, and geometry cleanup. Architectural teams routinely coordinate linked MEP models, detect service clearances, and audit consultant submissions. In integrated firms, in-house mechanical, electrical, and plumbing designers also require automated verification of distribution systems.

## 2. Scoping and Stakeholder Analysis (Phase 0)

| Dimension | Primary Focus (Architectural Coordination) | Secondary Focus (In-House MEP Design) |
|---|---|---|
| **Target Users** | BIM Managers, Project Architects, Coordinators | Mechanical / Electrical / Plumbing Engineers |
| **Primary Need** | Clearance checks, system classification consistency, unhosted terminals, penetrations | Open connector detection, flow continuity, duct/pipe sizing calculations |
| **First Workflow** | Non-invasive MEP QA audit with findings exported to `am-issue-board` | Parameter standardization across system categories |
| **Success Metric** | 0 unflagged clash clearances; 100% connector audit coverage on MEP links | Automated detection of 100% unconnected ends |
| **Go / No-Go Decision** | **GO**: Proceed with Phase 1 read audits and Phase 2 safe edits | **Deferred**: Complex hydraulic/aerodynamic CFD sizing remains with native Revit engines |

## 3. Phased Implementation Roadmap

### Phase 1: Read-Only MEP QA Audits (Target for Initial Release)
- **Connector Continuity Check**: Traverse MEP connectors (`ConnectorManager`) across ducts, pipes, conduits, and fittings to detect open/disconnected ends (`MEP-CON-01`).
- **System Classification & Naming**: Verify that all mechanical equipment, air terminals, and fixtures belong to assigned systems (`Supply Air`, `Return Air`, `Domestic Cold Water`) and match naming rules.
- **Service Clearance Envelope Validation**: Compute bounding clearance envelopes around equipment requiring maintenance access (e.g. 36" electrical clearance, filter pull zones, VAV box access) and test against architectural ceilings and partitions (`MEP-CLR-01`).
- **Penetration Candidate Detection**: Flag MEP runs penetrating fire-rated architectural assemblies lacking sleeve or opening families (`MEP-PEN-01`, coordinating with Issue #152).
- **Issue Board Integration**: Send all identified violations directly to `am-issue-board` with element IDs, system names, and 3D XYZ coordinates.

### Phase 2: Safe Parameter Standardization
- **Bulk System Assignment**: Assign unallocated duct/pipe runs to designated systems using verified recipes.
- **Elevation and Offset Standardization**: Standardize invert and centerline elevations across runs.
- **Pre-execution Preview**: Compute a parameter delta table via `am-safe-change` before any database modifications.

### Phase 3: Assisted Routing and Sizing
- **Assisted Routing**: Suggest connection routes between two open connectors with standard 45°/90° elbows, avoiding architectural bounding obstacles.
- **Rule-Based Sizing**: Suggest cross-sectional duct/pipe dimensions based on lookup velocity or friction tables.
- **Safety Guarantee**: Every geometry routing transaction is wrapped in an `am-safe-change` dry-run transaction. Users inspect changes visually in 3D and must confirm via the native Revit Keep/Discard dialog.

### Review-only sizing table

Run `python skills/am-safe-change/scripts/mep_sizing_preview.py sizing.json > sizing-preview.json` and open the result in the existing change-preview renderer. Use a table reviewed for the project and retain the input alongside the preview. The preview identifies the table and binds the input bytes by SHA-256.

The example uses synthetic values. Each rule matches service, profile, system, demand kind and demand unit exactly. Put the preferred qualifying rule first. The first matching rule with capacity at or above demand supplies the nominal dimensions. A missing rule or insufficient capacity blocks that row. Dimension units are explicit and the preview does no conversions. Use diameter for round sections and width then height for rectangular or oval sections.

```json
{
  "table_id": "Fixture table",
  "rules": [
    {
      "id": "D-100", "service": "duct", "profile": "rectangular",
      "system": "Supply Air", "demand_kind": "flow", "demand_unit": "L/s",
      "max_demand": 100, "dimensions": [300, 200], "dimension_unit": "mm"
    }
  ],
  "requests": [
    {
      "id": "branch-01", "service": "duct", "profile": "rectangular",
      "system": "Supply Air", "demand_kind": "flow", "demand_unit": "L/s",
      "demand": 80
    }
  ]
}
```

Supported services are `duct`, `pipe`, `conduit` and `cable_tray`. A preview with blocked rows returns exit 1. Invalid input returns exit 2 and emits no preview. The preview carries no model-write steps. Revit type availability, routing, clearances and Keep/Discard still require native qualification.

### Read reviewed route endpoints

Use `read_mep.py` with `describe: true` to obtain the saved source identity. Send the returned identity back under `source` with `route_endpoints`, a list of two objects with `element_id` (decimal string), `element_uid` and `connector_id` (integer). Use one project or one reviewed link instance per query. The result returns the two open physical ends in `endpoints_json` with their owner identities, native category, domain, profile, position in host internal feet, and three host-space axes. The connector normal is the third axis. Round and rectangular endpoints include nominal dimensions in feet when the native read succeeds. Other profiles retain their native names with unavailable dimensions. The query rereads the endpoints and source before returning. [Autodesk connector coordinate system](https://help.autodesk.com/cloudhelp/2026/ENU/Revit-API-MainReference/files/html/cb6d725d-654a-f6f3-fed0-96cc618a42f1.htm) · [Autodesk vector transform](https://help.autodesk.com/cloudhelp/2026/ENU/Revit-API-MainReference/files/html/4d5b7075-1b79-639d-5da2-eb23372bc888.htm)

The endpoint record is separate from the MEP audit snapshot. It supplies geometry to the route preview below. Native fitting placement, solid clearance, and live Revit qualification remain pending.

### Review-only route preview

Save the parsed `endpoints_json` object as the `endpoints` field of a route input. Add `minimum_segment_ft`, `clearance_ft` and `obstacles`. To keep the route inside a reviewed area, supply `routing_bounds_ft` with its `min_ft` and `max_ft` coordinates. Each obstacle is an object with `min_ft` and `max_ft` arrays of three coordinates in the same host coordinate system. Supply the obstacle bounds and clearance for the reviewed routing scope. Run `python skills/am-safe-change/scripts/mep_route_preview.py route-input.json > route-preview.json` and open the output in the change-preview renderer.

The preview tests a straight connection, axis-aligned turns and offsets to either side of the supplied obstacle boxes. Where a corner can be shortened while preserving segment length, it tests a pair of 45-degree turns. It keeps the shortest qualifying centerline from those candidates. The search checks both endpoint normals, minimum segment length, and the swept cross-section against obstacle boxes with the supplied clearance. It currently accepts equal round or rectangular connector dimensions. A blocked preview returns exit 1 and preserves the input digest. Invalid input returns exit 2. If the direct and single-offset candidates fail, the preview searches connected offsets around multiple obstacles. It reports separately when the edge-check budget is reached. A routing area constrains the swept section and clearance as well as the centerline. Native fitting volumes, fitting availability, transitions, pipe slope, and obstacle coverage still need verification in Revit. The preview carries no model-write steps.

### Read obstacle bounds from Revit

For a host or reviewed loaded link, run `read_mep.py` with its described `source` and `route_obstacles`, a list of objects containing the reviewed `element_id` and `element_uid`. The query reads model bounding boxes and transforms all eight corners into host coordinates. It rereads the source and boxes and stops if an obstacle has moved, changed identity, or lost its bounds. [Autodesk element bounding box](https://help.autodesk.com/cloudhelp/2026/ENU/Revit-API-MainReference/files/html/def2f9f2-b23a-bcea-43a3-e6de41b014c8.htm)

Pass each parsed `obstacles_json` object in the route input's `obstacle_records` array. Use one record per host or link placement, with at most 32 boxes across the records and any manually supplied boxes. The route preview requires a common host identity and carries the native obstacle records in the result. Select every obstruction in the reviewed route area and exclude the two connector owners. Bounding boxes may be larger than the actual solids. Native model coverage and exact fitting clearance still need Revit verification.

### Check a round duct or pipe elbow preference

In the host project, run `read_mep.py` with the described `source` and a `route_elbow` object containing `curve_type_id` (decimal string), `curve_type_uid` and `diameter_ft`. The query asks the selected type's native routing preference manager for an elbow at that diameter on both connectors. It repeats the lookup before returning. A resolved choice includes the fitting type ID and UID. A missing preference has a null elbow. [Autodesk routing preferences](https://help.autodesk.com/cloudhelp/2026/ENU/Revit-API/files/Revit_API_Developers_Guide/Discipline_Specific_Functionality/MEP_Engineering/Revit_API_Revit_API_Developers_Guide_Discipline_Specific_Functionality_MEP_Engineering_Routing_Preferences_html.html) · [Autodesk fitting lookup](https://help.autodesk.com/cloudhelp/2026/ENU/Revit-API-MainReference/files/html/b0900fd7-828b-c0f7-0729-24191b0d43a3.htm)

Pass the parsed `elbow_json` as `elbow_record` in the route input. The preview binds it to the endpoint source, connector domain and diameter. A missing elbow blocks bent candidates while allowing a clear straight connection. The preference lookup does not test whether the family can form the proposed angle or clear nearby geometry. Check those outcomes in a disposable Revit placement trial.

For trial preparation, use a separate host query with `route_trial_types`. Supply the elbow request under `elbow`, plus `system_type` and `level` objects with the exact `id` and `uid` from the reviewed project. The query rereads all selections and returns their verified identities and system classification in `trial_types_json`. See the [rollback trial plan](../plans/2026-11-12-mep-route-rollback.md) for the remaining placement and readback requirements.

Use `route_trial_owners` with the same two selected endpoint ID, UID and connector ID objects as `route_endpoints`. This separate host-only query returns `trial_owners_json`, including owner types and bounds, every physical connector on each owner, reciprocal neighbor identities and the observed system membership. The reader compares a second inventory before returning. Retain this record with the reviewed route for the placement trial. [Autodesk connector traversal](https://help.autodesk.com/cloudhelp/2026/ENU/Revit-API/files/Revit_API_Developers_Guide/Discipline_Specific_Functionality/MEP_Engineering/Revit_API_Revit_API_Developers_Guide_Discipline_Specific_Functionality_MEP_Engineering_Connectors_html.html) · [Autodesk mechanical system API](https://help.autodesk.com/cloudhelp/2026/ENU/Revit-API-MainReference/files/html/ef83dd58-07d6-4f9a-8dc6-f4b1fd8246d2.htm)

Run `mep_route_trial_preflight.py` as a Revit `run_query` on the reviewed standalone disposable project. Pass `disposable_fixture: true`, the exact input text as `route_input_json`, the rendered review's parsed `preview`, and the parsed `owners_record` and `types_record` from the two trial queries. Include reviewed routing bounds and native obstacle records in the input. If the reviewed area has no obstacles, set `empty_obstacle_scope: true`. The preflight recomputes the preview and rereads the bound records. It returns the source identity and digests of the review and native baselines. It opens no transaction. The placement trial will use this preflight before creating any curve or fitting.

### Run and verify a rollback-only route trial

After the preflight query, pass the same inputs to `probe_mep_route.py` with the parsed preflight result under `preflight`. Use the qualified CPython bridge with `run_modify` and `dry_run: true` on the disposable project. The trial creates round duct or pipe segments and the reviewed elbows, measures the native geometry and physical connections, and rolls back its transaction group. A pending transaction, regeneration failure or unresolved rollback needs independent reconciliation before retry.

Recover the original query and complete trial response with `get_run`. Run the original preflight query independently again with the exact addition IDs from the trial under `check_absent_ids`. Run `python skills/am-safe-change/scripts/mep_route_trial_preflight.py original.json trial.json fresh.json` to compare the full saved responses, preflight digests, change IDs and absence readback. Both the native placement and the bridge evidence contract require qualification in licensed Revit. The trial cannot keep changes.
