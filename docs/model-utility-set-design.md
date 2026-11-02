# Model-derived utility sheet set: implementation boundary

Design checkpoint for existing issue #361. This document is a proposal, not an
implemented API or an acceptance claim. Real project plans, measurements, source
identifiers and generated project outputs stay in the private companion repo.
Public fixtures must describe an unrelated synthetic site.

## Verified current behavior

`drawings.site_plan.prepare_site_plan` consumes independent feature coordinates.
Its audit explicitly reports `canonical_model_created: false` and supplied
drawing geometry only. `drawings.production_set` reads that request separately
from its canonical model input. Rebuilding this request does not make it a
projection of model edits.

`drawings.single_line.project_topology` already derives electrical connectivity
from canonical equipment, ports and circuits. It supports meter and disconnect
types separately but has no integrated meter-main symbol/type admission.
`single_line_drafting` attributes are explicitly presentation-only. They must
not become a parallel service or site truth source.

`BuildingModel` has equipment poses, equipment sizes, ports, circuits, routes,
obstacles and building geometry. It has no semantic property boundary, street,
gas-service point, working-surface or site-clearance records. Repurposing a
space, obstacle or electrical route as a property line would misstate its
meaning. A second site-coordinate dictionary in drawing attributes would leave
the original defect intact.

## Proposed bounded implementation

1. Agree the canonical contract delta before implementing a drawing adapter.
   Add opt-in typed site records with explicit kinds, stable IDs, metre geometry,
   provenance/confidence and validated source references. Only the minimal
   boundary/building-outline/street/gas-service/working-surface/intercept facts
   required by this sheet set belong in the first slice. Add explicit references
   for equipment-related working space and separation requirements. Unknown
   geometry or approved product envelopes must remain unknown. Preserve old
   schemas and serialized bytes; provide an explicit migration if a new contract
   version is required. Do not invent a new issue or silently relabel a version.
2. Use existing equipment, port, circuit and route objects for the electrical
   service graph and private lateral. Admit one integrated meter-main object to
   the single-line projector and render its combined function in one equipment
   envelope. Do not insert a fictitious second service-disconnect object.
   Unestablished utility-side geometry, equipment models and existing feeder
   ratings/routes remain visibly unresolved, not guessed coordinates or sizes.
3. Project the canonical site records and actual equipment/route geometry into
   the existing site drawing request. The adapter retains canonical IDs and a
   normalized model hash, uses one coordinate frame, and emits coverage for all
   selected records. Presentation settings may select scale and paper layout;
   they cannot carry competing physical positions. Derive dimensions from the
   declared geometric references. A clearance envelope or a level surface is
   not evidence of an approved equipment enclosure or of code compliance.
4. Add source-bound load-calculation inputs referencing the canonical service
   equipment and supplied component evidence. Keep accepted demand calculations
   distinct from connected circuit VA. Report each supplied component, formula,
   allowance, assumption and sensitivity. Do not change canonical connected
   loads to make them equal to demand, or infer missing feeder/product facts.
5. Add one command that consumes the complete model, retained source/calculation
   inputs and shared profile. Derive site, single-line and load bodies, then use
   the existing company-sheet PDF machinery. Export one PDF per document into a
   fresh directory with an allowlisted filename and a verified size no greater
   than 10 MB. Retain hashes, exact command, source bindings, canonical IDs,
   unresolved facts and a regeneration manifest. No upload or portal submission
   is part of this command.

## Acceptance to execute after the owner hold is released

- Review the private architect site page and relevant elevation/electrical pages
  visually. Record page scale, frame, dimensions and stable evidence references.
  Register the views from explicit anchors; do not convert a verbal approximate
  shift into an exact physical location. Keep this evidence private.
- Build an unrelated synthetic canonical site and service graph for public tests.
  Prove projection ownership, one integrated service disconnect, exact geometric
  dimensions, truthful unknowns and a separately computed supplied demand table.
- Edit the model equipment position and related canonical endpoint geometry,
  regenerate once, and verify that the site, single-line binding, load binding
  and manifest all use the edited model. Verify unchanged stable IDs and no
  duplicate geometry or demand/connected-load counting.
- Generate the real three-document set privately. Render every page and compare
  it to the source architect sheet. Check locations, lateral/property-line join,
  supplied separation requirement and work surface, north, scale, labels, load
  basis and sensitivities, actual PDF sizes and filenames.
- Deliver to the private project only after the parent verifies exact upload
  authority. No public plan/crop/output attachment; no third-party submission.

## Current blockers

The canonical site contract decision is required before implementing the site
adapter; repository instructions prohibit a drawing-lane substitute. Source PDFs
are retrieved privately but scale/position visual acceptance has not run under
the current execution hold. Private delivery authorization is held by the parent.
None of these limits justify counting this issue as completed.
