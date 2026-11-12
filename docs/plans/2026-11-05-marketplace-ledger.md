# archi-magic: Marketplace Feature Ledger & Roadmap (2026-11-05)

Ledger tracking the 50 enhancement ideas surfaced from the scan of 1,672 public Revit add-in listings on Autodesk App Store (Issue #178), combined with the platform enhancement epics (#179 to #188).

Demand rating reflects cumulative user reviews from similar commercial and free add-ins.

---

## 1. Executive Summary & Delivery Status

GitHub issue states checked on 2026-11-12. Open issues may already have components on main. The totals cover the 60 marketplace and platform entries below.

| Wave or tier | Tracked | Closed on GitHub | Open on GitHub |
|---|---:|---:|---:|
| Platform epics (#179–#188) | 10 | 6 | 4 |
| P1 (#128–#137) | 10 | 9 | 1 |
| P2 (#138–#157) | 20 | 1 | 19 |
| P3 (#158–#177) | 20 | 5 | 15 |
| **Total** | **60** | **21** | **39** |


---

## 2. Platform Epics Ledger (#179 to #188)

| Issue | Title | Category | Target Skill | Status | Delivery Artifacts |
|---|---|---|---|---|---|
| #179 | Recipe library: save agent runs as versioned recipes | Platform | `am-safe-change` | Closed | `scripts/recipe_library.py`, `tests/test_recipe_library.py`, fast-path docs |
| #180 | Check packs: team-authored rules with severity | Platform | `am-issue-board` | Closed | `scripts/check_packs.py`, `tests/test_check_packs.py`, built-in packs |
| #181 | Spec sheet PDF to parametric family | Intelligence | `am-markup-intake` | Open | Spec-family preview merged in PR #212. Native family verification pending. |
| #182 | Use project documents as task input (ACC, SharePoint, Excel) | Integration | `am-markup-intake` | Closed | Project document intake merged in PR #209. |
| #183 | Model version compare: what changed since date/sync | Safety | `am-safe-change` | Closed | `scripts/model_compare.py`, `tests/test_model_compare.py`, diff docs |
| #184 | Local time-saved log and ROI report | Doctor / Value | `am-doctor` | Closed | `scripts/time_log.py`, `tests/test_time_log.py`, CLI commands |
| #185 | Narrated screen recordings as task input | Intelligence | `am-markup-intake` | Closed | Narrated intake merged in PR #211. |
| #186 | Epic: MEP support (routing, sizing and MEP QA) | Epic | `am-issue-board` / new | Open | RFC 003. Native MEP verification pending. |
| #187 | Epic: Web workspace (projects, previews, issue board) | Epic | `am-bim-viewer` / web | Open | RFC 002. Browser checks used synthetic sources. Real project verification pending. |
| #188 | Epic: AutoCAD support (read and write DWG) | Epic | CAD connector | Open | RFC 001. Native AutoCAD verification pending. |

---

## 3. Tier P1: High Demand & Immediate Fit (#128 to #137)

| Issue | Title | Demand (Reviews) | Target Skill | Status | Implementation Details |
|---|---|---|---|---|---|
| #128 | Excel round trip for schedules with safe write-back | 233 | `am-schedule-view` | Closed | `scripts/schedule_roundtrip.py`, CSV export/import, read-only field locks, test suite |
| #129 | Natural-language element selection | 101 | `am-revit-api-docs` | Closed | Selection recipe with filtered collector and `show_elements` highlight |
| #130 | Join geometry by rules with a preview | 130 | `am-safe-change` | Closed | Hierarchy rule join recipe with `JoinGeometryUtils` and preview |
| #131 | Renumber any category by pick order, path or location | 82 | `am-safe-change` | Closed | Coordinate and path renumbering recipe with collision prevention |
| #132 | Room finishing: floors, walls and ceilings from rooms | 101 | `am-safe-change` | Open | Finish workflow merged in PR #206. Native verification pending. |
| #133 | Auto tag untagged elements in views | 63 | `am-revit-api-docs` | Closed | IndependentTag collector recipe with duplicate detection |
| #134 | Align and distribute tags, text and elements | 159 | `am-revit-api-docs` | Closed | Coordinate alignment and bounding-box distribution recipe |
| #135 | Publish-set preflight before export | 286 | `am-model-health` | Closed | `scripts/publish_preflight.py`, 4 preflight checks, markdown report, test suite |
| #136 | Bulk-bind shared parameters to categories | 111 | `am-param-qa` | Closed | Category binding recipe with `CategorySet` and `SharedParameterElement` |
| #137 | Transfer standards between open models | 85 | `am-safe-change` | Closed | Multi-document copy recipe using `ElementTransformUtils.CopyElements` |

---

## 4. Tier P2: Clear Demand & Good Fit (#138 to #157)

| Issue | Title | Demand (Reviews) | Target Skill | Status | Evidence |
|---|---|---|---|---|---|
| #138 | Revision management: add revisions to many sheets and report clouds | 49 | `am-schedule-view` | Open | |
| #139 | Warnings triage with fixes and issue-board hand-off | 35 | `am-issue-board` | Open | |
| #140 | Model cleanup plan: purge unused views, templates, scope boxes and CAD | 46 | `am-model-health` | Open | |
| #141 | Color-by-parameter view filters | 23 | `am-param-qa` | Open | |
| #142 | Copy view filters and overrides between views and templates | 23 | `am-safe-change` | Open | |
| #143 | Batch duplicate views with a template and a name rule | 41 | `am-safe-change` | Open | |
| #144 | Place many views on sheets and replicate legends | 26 | `am-schedule-view` | Open | |
| #145 | Create sheets from a list with title block values | 29 | `am-schedule-view` | Open | |
| #146 | Auto-dimension grids, walls and openings | 26 | `am-revit-api-docs` | Open | |
| #147 | Door and window type legends generated from the model | 40 | `am-schedule-view` | Open | |
| #148 | BCF import and export for am-issue-board | 123 | `am-issue-board` | Open | BCF support merged in PR #208. Native board readback pending. |
| #149 | Review Navisworks clash results inside Revit | 38 | `am-issue-board` | Open | Navisworks intake merged in PR #217. Native clash-view verification pending. |
| #150 | Linked-model manager: report, path check and reload plan | 47 | `am-model-health` | Open | Link audit merged in PR #218. Native verification pending. |
| #151 | Split walls, floors and columns by level or grid | 52 | `am-safe-change` | Open | |
| #152 | Openings and sleeves where MEP crosses walls and floors | 24 | `am-safe-change` | Open | |
| #153 | Family library search and place | 87 | `am-revit-api-docs` | Open | |
| #154 | Quantity and material takeoff report | 17 | `am-area-report` | Open | |
| #155 | Embodied carbon quick estimate from materials | 13 | `am-material-audit` | Closed | |
| #156 | Code-check helpers: occupancy load and egress width | 4 | `am-area-report` | Open | Occupancy and egress reporting merged in PR #220. Native verification pending. |
| #157 | Architecture vs structure mismatch check | 16 | `am-issue-board` | Open | Structural comparison merged in PR #221. Native board verification pending. |

---

## 5. Tier P3: Narrower or Strategic Scope (#158 to #177)

| Issue | Title | Demand (Reviews) | Target Skill | Status | Notes |
|---|---|---|---|---|---|
| #158 | Zoom, section box and focused 3D view from selection | 62 | `am-bim-viewer` | Open | Focus-view workflow merged in PR #222. Native verification pending. |
| #159 | Find hidden elements and unhide them | 17 | `am-revit-api-docs` | Open | Visibility workflow merged in PR #223. Native verification pending. |
| #160 | Interior elevations and sections per room | 30 | `am-safe-change` | Open | Room marker generator |
| #161 | Copy a parameter value to another parameter | 15 | `am-param-qa` | Open | Parameter copy recipe |
| #162 | Spreadsheet-style bulk parameter editor | 28 | `am-param-qa` | Closed | Aligns with #128 schedule roundtrip |
| #163 | Transfer parameters between families | 18 | `am-param-qa` | Open | Family definition copier |
| #164 | Family preview image generator | 20 | `am-bim-viewer` | Open | Thumbnail batch exporter |
| #165 | Batch load families and create types from a table | 13 | `am-safe-change` | Open | Family type generator |
| #166 | CAD-to-BIM: walls, doors and rooms from DWG layers | 46 | CAD connector | Open | Layout preview merged in PR #214. Native placement verification pending. |
| #167 | Topography helpers: align to elements and convert to floors | 22 | `am-safe-change` | Open | Toposolid helpers |
| #168 | Match offset, rotation and reference level | 28 | `am-safe-change` | Open | Transform match recipe |
| #169 | Create levels and grids from a list | 5 | `am-safe-change` | Open | Datum creation tool |
| #170 | Keynote file editor and keynote check | 12 | `am-issue-board` | Closed | Keynote table validator |
| #171 | Spell check and standard wording for text notes | 8 | `am-shared-text-check` | Closed | Text note dictionary check |
| #172 | Wall and floor type legend and schedule | 8 | `am-schedule-view` | Open | Legend generator |
| #173 | Linked element inspector | 15 | `am-revit-api-docs` | Open | Link query tool |
| #174 | Batch process many models | 40 | `am-doctor` | Open | Batch audit merged in PR #215. Five-model native run pending. |
| #175 | Modelling effort log from journals | 50 | `am-doctor` | Closed | Journal parser |
| #176 | Revit backup file cleanup | 17 | `am-doctor` | Closed | `scripts/cleanup_backups.py`, scan, archive, delete, test suite |
| #177 | Positioning: list archi-magic as MCP app on Autodesk store | 54 | Docs / Distribution | Open | Owner decision pending. See the [decision proposal](../decisions/2026-11-09-autodesk-marketplace.md). |
