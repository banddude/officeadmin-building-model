# Next ten functional outcomes

This batch starts at reviewed `717bb36374fd0e9946d4d7b9c3dc8af205c22834` in a
separate worktree. It does not change the published BIM/OA branches. These are
ten functional outcomes, not ten closed issues. Existing umbrella issues remain
open; no new issues, production inputs or financial data are introduced.

| # | Outcome and existing issue | Before | Current status / working acceptance | Changed files |
|---|---|---|---|---|
| 1 | Assembly revision comparison ([#235](https://github.com/banddude/officeadmin-building-model/issues/235), existing #274 API) | Either populated assembly snapshot refused. | Implemented locally: actual regenerated room yields 49 changed records with stable IDs and a +0.0933381656966453 m3 signed geometry-volume delta; a denser ceiling reports real membership additions/removals. Existing null-absent-side quantity contract preserved. | `qa/revision_diff.py`, `test_assembly_revision_diff.py`, existing guard regression |
| 2 | Assembly schedules with physical drawings ([#233](https://github.com/banddude/officeadmin-building-model/issues/233), [#240](https://github.com/banddude/officeadmin-building-model/issues/240)) | Schedules and cut drawing sets with default schedules refused. | Implemented locally: 143 physical component rows once each, separate parent metadata marked not takeoff, retained unknown materials, complete source index alongside actual plane cuts. | `drawings/generator.py`, `test_assembly_schedules.py`, delivery/guard regressions |
| 3 | Supported selected installation details in assembly context ([#233](https://github.com/banddude/officeadmin-building-model/issues/233)) | Any unrelated assembly blocked the selected detail. | Implemented locally: explicit equipment envelope detail succeeds from the complete original model; unsupported/missing selected assembly sources still refuse. No stripping of canonical context. | `drawings/details.py`, `test_assembly_elevations_details.py`, detail regression |
| 4 | Physical assembly elevations ([#240](https://github.com/banddude/officeadmin-building-model/issues/240), [#233](https://github.com/banddude/officeadmin-building-model/issues/233)) | Elevation API refused populated assemblies. | Implemented locally through explicit projection admission: all 143 physical children retain IDs and actual mesh bounds; near/far clipping and architecture visibility apply; aggregate host bodies are suppressed. Wireframe triangulation is disclosed; no hidden-line or occlusion claim. | `drawings/assembly_cuts.py`, `drawings/generator.py`, `test_assembly_elevations_details.py` |
| 5 | Assembly-aware legacy route consolidation ([#228](https://github.com/banddude/officeadmin-building-model/issues/228), [#240](https://github.com/banddude/officeadmin-building-model/issues/240)) | Consolidation rejects populated architecture. | Not implemented. Must preserve canonical architecture and collision envelopes; no blind guard removal. | Planned: `routing/consolidate.py` |
| 6 | Unambiguous consolidated-corner bend preflight ([#228](https://github.com/banddude/officeadmin-building-model/issues/228)) | Valid cross-route corner elbow reports `elbow_off_turn`. | Not implemented. Must check both legs/setbacks and radius; ambiguous, short and mismatched cases must refuse. | Planned: `qa/route_bends.py` |
| 7 | Panel facts with physical-route context ([#233](https://github.com/banddude/officeadmin-building-model/issues/233), existing #282 API) | Whole report and route snapshots refused physical routes. | Implemented locally: an actual quarter-arc route retains the same supplied 120 VA panel subtotal and unknown-load row as legacy geometry; whole-model bindings differ. Mixed route order is invariant and cannot multiply loads. Typed route snapshots preserve discriminator/geometry and reject forged bindings. | `reports/panel_schedules.py`, `test_physical_panel_report.py`, physical consumer guard regression |
| 8 | Analytic whole-model physical-route quantities ([#228](https://github.com/banddude/officeadmin-building-model/issues/228)) | Quantity extraction rejects physical routes. | Not implemented. Exact analytic arc length, source provenance and missing product facts required; mesh chords are not a takeoff. | Planned: `quantities/core.py` |
| 9 | Physical routes in ordinary DrawingViews/sheets ([#233](https://github.com/banddude/officeadmin-building-model/issues/233)) | Normal drawing entrypoints refuse physical routes. | Not implemented. Reuse bounded projection with explicit view limits and saved selection semantics; SVG/DXF/PDF behavior must be demonstrated. | Planned: `drawings/generator.py` and existing physical adapters |
| 10 | Native curved-route IFC and source-bound readback ([#228](https://github.com/banddude/officeadmin-building-model/issues/228)) | IFC export/reimport rejects physical routes. | Not implemented. Actual line/arc solids and strict readback must retain identity, supplied OD/radius and geometry; no polyline flattening. | Planned: `ifc/adapter.py` |

## First checkpoint

Five implemented outcomes: **1, 2, 3, 4, 7**. Five remain: **5, 6, 8, 9, 10**.
Implementation is not independent acceptance or whole-issue closure. Focused
checks exercise real model regeneration, ordinary drawing views, selected details
and typed panel facts. Existing unrelated refusals remain intact.

Local external synthetic receipts in `next-ten-proof-first-five` retain before /
after models, revision JSON, drawing set with schedules, actual plan/elevation /
equipment-detail SVGs, a quarter-arc panel model/report, `REPORT.json` and hashes.
These receipts are outside the public repository. No raster/PDF/IFC output, full
build, full aggregate, production deployment or OA expansion is claimed here.

Independent review/full gates remain pending. Broader materialization still needs
its own functional implementation and meaningful acceptance evidence.
