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
| 5 | Assembly-aware legacy route consolidation ([#228](https://github.com/banddude/officeadmin-building-model/issues/228), [#240](https://github.com/banddude/officeadmin-building-model/issues/240)) | Consolidation rejects populated architecture. | Implemented locally with passing enclosed-room checks: duplicate clear runs become one path; all architecture/143 children remain unchanged; every new path checked against radius/clearance-padded physical children/deck and hard blockers. Wall collisions refuse. Source-bound routing/product/support identities remain untouched with report reasons. Review repair additionally excludes every unknown-diameter member before recomputing overlaps, including mixed known/unknown bundles; new regressions await guarded execution. | `routing/consolidate.py`, `test_assembly_consolidation.py`, guard regression |
| 6 | Unambiguous consolidated-corner bend preflight ([#228](https://github.com/banddude/officeadmin-building-model/issues/228)) | Valid cross-route corner elbow reports `elbow_off_turn`. | Implemented locally with passing actual consolidation checks: shared canonical endpoint, same-type two-leg admission; both endpoint setbacks charged, including two corners sharing one straight; short, ambiguous, unknown/mismatched radius and incorrect angle/type remain diagnostics. | `qa/route_bends.py`, `test_route_bend_preflight.py`, bend/delivery docs |
| 7 | Panel facts with physical-route context ([#233](https://github.com/banddude/officeadmin-building-model/issues/233), existing #282 API) | Whole report and route snapshots refused physical routes. | Implemented locally: an actual quarter-arc route retains the same supplied 120 VA panel subtotal and unknown-load row as legacy geometry; whole-model bindings differ. Mixed route order is invariant and cannot multiply loads. Typed route snapshots preserve discriminator/geometry and reject forged bindings. | `reports/panel_schedules.py`, `test_physical_panel_report.py`, physical consumer guard regression |
| 8 | Analytic whole-model physical-route quantities ([#228](https://github.com/banddude/officeadmin-building-model/issues/228)) | Quantity extraction rejects physical routes. | Implemented locally with passing known-answer quarter-arc, odd-nanometre radius, two-bend and mixed/grouped route checks. Exact analytic line/pi coefficients feed final approximate metres, nested measurement evidence retained, supplied OD distinct from unknown trade size/product, overlap-check scope explicit. | `quantities/core.py`, internal legacy overlap subset, `test_physical_model_quantities.py` |
| 9 | Physical routes in ordinary DrawingViews/sheets ([#233](https://github.com/banddude/officeadmin-building-model/issues/233)) | Normal drawing entrypoints refuse physical routes. | Implemented locally: explicit horizontal XY centerlines retain quarter-circle primitives through ordinary DrawingViews, sheet SVG, native DXF ARC and vector PDF with page/curve readback. Saved resolver hide/ghost/reset applies. Vertical/oblique projections and partial XY crops refuse. Additive drawing output version 1.1.0; legacy output remains 1.0.0. | `drawings/generator.py`, `route_arcs.py`, model/SVG/sheet adapters, `test_physical_drawing_sheets.py`, dedicated docs |
| 10 | Native curved-route IFC and source-bound readback ([#228](https://github.com/banddude/officeadmin-building-model/issues/228)) | IFC export/reimport rejects physical routes. | Implemented locally: explicit physical-route admission writes actual line/circle-trimmed swept-disk solids. Strict IFC4 EXPRESS validation and real STEP-byte reparse preserve canonical IDs/geometry/OD/radius/quantities; tampered or missing native geometry refuses. Extra route-owned canonical ports remain unsupported. | `ifc/adapter.py`, `ifc/physical_routes.py`, exact physical comparison, `test_physical_ifc.py`, dedicated docs |

## First checkpoint

Five implemented outcomes: **1, 2, 3, 4, 7**. Five remain: **5, 6, 8, 9, 10**.
Implementation is not independent acceptance or whole-issue closure. Focused
checks exercise real model regeneration, ordinary drawing views, selected details
and typed panel facts. Existing unrelated refusals remain intact.

## Second checkpoint

Eight locally implemented outcomes: **1–8**. Remaining **9, 10** require actual
curved DrawingView/CAD/sheet and native IFC implementation. A guarded focused run
passed 62 tests across bend preflight, physical quantities, room consolidation,
legacy overlaps and quantities. Independent first-five review found frame
classification and grazing depth-clip defects; both source repairs and new
regressions passed in the preceding focused run (42 passed, four fixture failures
subsequently corrected in the 62-pass run). Full aggregate and independent review
of these later changes remain pending.

## Third checkpoint

All **ten** outcomes now have bounded local implementations and focused passing
working-output checks. This means ten functional outcomes, not ten independently
accepted fixes or closed issues. In addition to the 62-test second checkpoint,
81 drawing/SVG/CAD checks passed, including actual native ARC and vector PDF
readback. A subsequent final-source 23-test run passed the physical IFC, curved
sheet and analytic quantity cases, including strict EXPRESS validation, STEP-byte
round trips, native tamper refusals and bounded exact-decimal route JSON intake.
These runs overlap; they are not a unique aggregate test count.

Remaining acceptance work: independent source review of later commits, broader
compatibility/full gates and visual inspection of the actual curved sheet.
The first external artifact run did not start at 26.97 GiB free; that blocked
receipt is retained. A later fresh preflight admitted the run at 27.20 GiB and
finished at 26.66 GiB, retaining 15 synthetic files (1,011,153 bytes) outside the
repository in `next-ten-proof-remaining-five`: actual room/bend outputs, analytic
takeoff, ordinary drawing JSON/SVG, sheet SVG/DXF/PDF and native IFC. STEP-file
readback had zero strict EXPRESS errors and exact canonical round-trip equality.
`REPORT.json` and `SHA256SUMS` retain the outcome. No raster/interactive visual
acceptance, deployment or publication is claimed. Existing published BIM/OA
heads are unchanged.

Boundaries remain explicit: consolidation preserves paths with original bounded
routing/product/support bindings instead of transferring their proofs; its fresh
collision receipt checks new path hard-blocker envelopes only. Physical drawings
admit horizontal analytic XY centerlines only. IFC extra physical route-owned
canonical port binding remains refused. None of these derived consumers supplies
new engineering, product compatibility or live-clearance approval.

Local external synthetic receipts in `next-ten-proof-first-five` retain before /
after models, revision JSON, drawing set with schedules, actual plan/elevation /
equipment-detail SVGs, a quarter-arc panel model/report, `REPORT.json` and hashes.
These receipts are outside the public repository. No raster/PDF/IFC output, full
build, full aggregate, production deployment or OA expansion is claimed here.

Independent review/full gates remain pending. Broader materialization still needs
its own functional implementation and meaningful acceptance evidence.
