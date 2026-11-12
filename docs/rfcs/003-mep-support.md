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

The endpoint record is separate from the MEP audit snapshot. It provides geometry for subsequent routing review. Route generation, collision and fitting checks, and live Revit qualification are pending.

### Review-only route preview

Save the parsed `endpoints_json` object as the `endpoints` field of a route input. Add `minimum_segment_ft`, `clearance_ft` and `obstacles`. To keep the route inside a reviewed area, supply `routing_bounds_ft` with its `min_ft` and `max_ft` coordinates. Each obstacle is an object with `min_ft` and `max_ft` arrays of three coordinates in the same host coordinate system. Supply the obstacle bounds and clearance for the reviewed routing scope. Run `python skills/am-safe-change/scripts/mep_route_preview.py route-input.json > route-preview.json` and open the output in the change-preview renderer.

The preview tests a straight connection, axis-aligned turns and offsets to either side of the supplied obstacle boxes. Where a corner can be shortened while preserving segment length, it tests a pair of 45-degree turns. It keeps the shortest qualifying centerline from those candidates. The search checks both endpoint normals, minimum segment length, and the swept cross-section against obstacle boxes with the supplied clearance. It currently accepts equal round or rectangular connector dimensions. A blocked preview returns exit 1 and preserves the input digest. Invalid input returns exit 2. If the direct and single-offset candidates fail, the preview searches connected offsets around multiple obstacles. It reports separately when the edge-check budget is reached. A routing area constrains the swept section and clearance as well as the centerline. Native fitting volumes, fitting availability, transitions, pipe slope, and obstacle coverage still need verification in Revit. The preview carries no model-write steps.

### Read obstacle bounds from Revit

For a host or reviewed loaded link, run `read_mep.py` with its described `source` and `route_obstacles`, a list of objects containing the reviewed `element_id` and `element_uid`. The query reads model bounding boxes and transforms all eight corners into host coordinates. It rereads the source and boxes and stops if an obstacle has moved, changed identity, or lost its bounds. [Autodesk element bounding box](https://help.autodesk.com/cloudhelp/2026/ENU/Revit-API-MainReference/files/html/def2f9f2-b23a-bcea-43a3-e6de41b014c8.htm)

Pass each parsed `obstacles_json` object in the route input's `obstacle_records` array. Use one record per host or link placement, with at most 32 boxes across the records and any manually supplied boxes. The route preview requires a common host identity and carries the native obstacle records in the result. Select every obstruction in the reviewed route area and exclude the two connector owners. Bounding boxes may be larger than the actual solids. Native model coverage and exact fitting clearance still need Revit verification.
