# RFC 001: AutoCAD and DWG Support Architecture (Issue #188)

- **Author**: archi-magic architecture team
- **Status**: Phase 0 Scoping Approved
- **Target Milestones**: Phase 0 (Scoping) -> Phase 1 (Read Audits) -> Phase 2 (Safe Edits) -> Phase 3 (CAD-to-BIM)

## 1. Context and Problem Statement
archi-magic currently interfaces with Revit and Rhino. Many architectural office workflows continue to exchange and reference 2D/3D DWG drawings from civil, structural, and MEP consultants, as well as legacy detail libraries. Users require reliable, safe DWG inspection, layer audits, xref validation, and write-back capabilities.

## 2. Connection Path Evaluation (Phase 0)

| Option | Method | Pros | Cons | Recommendation |
|---|---|---|---|---|
| **A. In-process .NET / ObjectARX** | C# / C++ plugin loaded in running AutoCAD | Full access to AutoCAD database, transactions, and UI | Requires AutoCAD license and active running instance | **Adopt for Phase 2** (Interactive edits with Keep/Discard) |
| **B. Headless Python library (`ezdxf`)** | Direct DXF/DWG file parsing in Python | Headless, zero license cost, runs in background tasks | Direct binary DWG write requires conversion (e.g. ODA File Converter or DXF intermediate) | **Adopt for Phase 1** (Fast headless read audits) |
| **C. Windows COM Automation** | `win32com` / pyautocad | Simple scripting, drives AutoCAD window | Fragile across versions, modal dialogs block COM calls | Rejected |

### Decision
- **Read-only audits (Phase 1)** use headless Python parsing (`ezdxf` with DXF/ODA conversion) for instant layer, xref, block, and unit audits without launching AutoCAD.
- **Phase 1 decoder update**: Use ezdxf 1.4.5 for DXF and an explicitly selected local LibreDWG 0.14 reader for DWG. The seeded R2000 fixture retained its Xref path in native DWG JSON. LibreDWG's DWG-to-DXF export omitted that path. Native decoding supports AC1015 and AC1032 as validated formats. The read-only product reports incomplete scope when nested Xrefs, units or other applicable records cannot be checked.
- **Interactive modifications (Phase 2)** use a lightweight `.NET` agent connector running inside AutoCAD, adhering strictly to the `am-safe-change` model: change preview, dry-run, and explicit Keep/Discard prompt in the AutoCAD viewport.

## 3. Phased Implementation Roadmap

### Phase 1: Read-Only DWG Audits
- **Layer Standards Audit**: verify layer naming against office or project conventions (e.g. AIA CAD Layer Guidelines: `A-WALL`, `A-DOOR`), color, and linetype standards.
- **Xref Inspector**: extract all external reference paths, detect broken/unresolved links, and report circular references.
- **Block & Attribute Inventory**: count title block instances, verify attribute fill completeness, and detect unpurged anonymous blocks.
- **Scale and Units Verification**: Check `$INSUNITS` against the project’s insertion unit and `$MEASUREMENT` against its hatch and linetype standard. [AutoCAD insertion units](https://help.autodesk.com/cloudhelp/2026/ENU/AutoCAD-Core/files/GUID-A58A87BB-482B-4042-A00A-EEF55A2B4FD8.htm) · [AutoCAD hatch and linetype standard](https://help.autodesk.com/cloudhelp/2026/ENU/AutoCAD-Core/files/GUID-1D074C55-0B63-482E-8A37-A52AC0C7C8FE.htm)

### Phase 2: Safe Edits with Explicit Approval
- **Layer Mapping and Renaming**: compute old/new layer mapping with preview table.
- **Xref Path Repathing**: repath absolute network drive letters to relative or cloud-synchronized paths.
- **Purge Candidates**: identify and purge unused layers, blocks, dimension styles, and empty text notes.
- **Safety Rule**: No DWG modification is written directly without dry-run comparison and explicit user approval in the CAD viewport.

### Phase 3: CAD-to-BIM Hand-off (Issue #166)
- Map approved DWG layers to native Revit elements (walls from `A-WALL`, doors from `A-DOOR`, rooms from boundary polylines).
- Export verified Revit schedules and sheet views back to clean, standardized DWG backgrounds.
