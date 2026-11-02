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
| 5 | Assembly-aware legacy route consolidation ([#228](https://github.com/banddude/officeadmin-building-model/issues/228), [#240](https://github.com/banddude/officeadmin-building-model/issues/240)) | Consolidation rejects populated architecture. | Verified locally: two clear 3.5 m runs become one; all architecture/143 children stay unchanged; every new path checked against radius/clearance-padded physical children/deck and hard blockers. Wall collisions refuse. Routing/product/support bindings remain untouched with reasons. Every unknown-diameter member is excluded before recomputing overlaps, including both mixed known/unknown orders; passing regressions preserve originals with no success receipt. | `routing/consolidate.py`, `test_assembly_consolidation.py`, guard regression |
| 6 | Unambiguous consolidated-corner bend preflight ([#228](https://github.com/banddude/officeadmin-building-model/issues/228)) | Valid cross-route corner elbow reports `elbow_off_turn`. | Implemented locally with passing actual consolidation checks: shared canonical endpoint, same-type two-leg admission; both endpoint setbacks charged, including two corners sharing one straight; short, ambiguous, unknown/mismatched radius and incorrect angle/type remain diagnostics. | `qa/route_bends.py`, `test_route_bend_preflight.py`, bend/delivery docs |
| 7 | Panel facts with physical-route context ([#233](https://github.com/banddude/officeadmin-building-model/issues/233), existing #282 API) | Whole report and route snapshots refused physical routes. | Implemented locally: an actual quarter-arc route retains the same supplied 120 VA panel subtotal and unknown-load row as legacy geometry; whole-model bindings differ. Mixed route order is invariant and cannot multiply loads. Typed route snapshots preserve discriminator/geometry and reject forged bindings. | `reports/panel_schedules.py`, `test_physical_panel_report.py`, physical consumer guard regression |
| 8 | Analytic whole-model physical-route quantities ([#228](https://github.com/banddude/officeadmin-building-model/issues/228)) | Quantity extraction rejects physical routes. | Verified locally: analytic quarter-arc, odd-nanometre radius, two-bend and mixed/grouped route checks. Exact line/pi coefficients feed final approximate metres, nested evidence retained, OD distinct from unknown trade size/product, overlap scope explicit. Conductor confidence also follows low-confidence joint/segment/requirements owners; actual length and provenance stay correct. | `quantities/core.py`, internal legacy overlap subset, `test_physical_model_quantities.py` |
| 9 | Physical routes in ordinary DrawingViews/sheets ([#233](https://github.com/banddude/officeadmin-building-model/issues/233)) | Normal drawing entrypoints refuse physical routes. | Implemented locally: explicit horizontal XY centerlines retain quarter-circle primitives through ordinary DrawingViews, sheet SVG, native DXF ARC and vector PDF with page/curve readback. Saved resolver hide/ghost/reset applies. Vertical/oblique projections and partial XY crops refuse. Additive drawing output version 1.1.0; legacy output remains 1.0.0. | `drawings/generator.py`, `route_arcs.py`, model/SVG/sheet adapters, `test_physical_drawing_sheets.py`, dedicated docs |
| 10 | Native curved-route IFC and source-bound readback ([#228](https://github.com/banddude/officeadmin-building-model/issues/228)) | IFC export/reimport rejects physical routes. | Verified locally after actual-render repair: explicit admission writes capped circular extrusions and quarter revolutions, with analytic line/trimmed-circle Axis and explicit radians. Strict EXPRESS, actual native-kernel volume/bounds, STEP-byte reparse and canonical/quantity identity pass. Stale source Name/pose, unowned extra conduit and native geometry tampering refuse. Requires unchanged shared pre-routing source; sequential/dependent histories and extra route-owned ports remain unsupported. | `ifc/adapter.py`, `ifc/physical_routes.py`, exact physical comparison, `test_physical_ifc.py`, dedicated docs |

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
These first-checkpoint receipts are historical and outside the public repository.
The final demonstration below supersedes their acceptance scope.

## Review repairs and actual visual demonstration

Independent review identified and prompted repairs for unknown member diameter
borrowing, conductor evidence confidence, stale physical IFC source edits and
extra unowned native conduit products. Actual rendering then found two additional
defects that schema/text checks missed: incorrectly interpreted/uncapped native
IFC sweeps and erased PDF titleblock glyphs. The original failed files/probes
remain retained. The IFC now uses explicit radian units and capped analytic
extrusions/revolutions; PDF conversion retains fill-only text with its halo limit
disclosed. No canonical geometry, identities or quantities were changed.

The final serial guarded run passed **39 focused checks**, including the review
repairs, strict EXPRESS validation, actual native BRep volume/bounds and actual
Poppler titleblock pixel checks. Fresh engine outputs for all ten outcomes are
retained in `next-ten-final-demonstration`, with `END-TO-END.json`, source/quantity
equality assertions and hashes. Functional source head is `d62c5cd` before this
documentation-only update.

Actual visual inspection covered whole and front-depth-clipped before/after
elevations, the selected equipment envelope, the room plan, all seven paginated
schedule pages, the curved PDF, unmodified sheet SVG through native librsvg, and
actual IFC surfaces generated by `ifcopenshell.geom` from the exported STEP file.
The changed header is visibly deeper; long IDs wrap without dropped rows; unknown
materials remain explicit; the native bend joins both straights with closed caps.
Native BRep OD-envelope volume is `0.0010658478510905548 m3`, versus analytic
`0.001065847851090555 m3`. Display-mesh tessellation differs by about 0.066 percent;
the takeoff stays analytic. Debug product colors do not assert source materials.

All ten bounded local outcomes now have actual-output evidence. Independent review
of final repairs and broader/full gates remain pending. This is not ten closed
issues, production deployment, publication or interactive OfficeAdmin acceptance.
Existing published BIM/OA heads remain unchanged. Browser opening of local SVG
was denied by its HTTP(S)-only protocol policy; no alternate browser/HTTP serving
was attempted. Offline native SVG/PDF/IFC rendering supplied the visual evidence.
