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

## Proposed contract and compatibility details for independent review

The narrow extension is authorized within #361. The following is the proposed
shape to review before implementation, not a second serialized site format.

### Canonical site inventory

Add `site_features: tuple[SiteFeature, ...]` to `BuildingModel`. A feature extends
the existing `Entity` identity/evidence/confidence fields and supplies:

| Field | Proposed boundary |
| --- | --- |
| `kind` | Closed tokens: property boundary, building outline, street edge, gas service, property-line intercept, working surface |
| `geometry` | Typed plan-only XY point/open polyline/closed polygon, reusing canonical `Point2D`; no invented Z |
| `status` | Explicit existing/proposed/unknown, never inferred from a drawing style |
| `equipment_id` | Optional canonical equipment reference, required for an equipment-associated work surface; never a copied equipment position |

Property boundaries, building outlines and working surfaces use simple XY
polygons; street edges use explicit polylines; a property-line intercept is a
point joined to the electrical route's canonical endpoint port. Gas service can
retain a source marker point or an explicitly supplied enclosure geometry. The
validator must distinguish these, so a point marker never acquires an invented
physical size. An unlocated utility service point is not a point at the origin.

All XY coordinates use the model frame and metres. Unknown elevation is not
encoded as zero and these records cannot become 3D routing obstacles.
No feature-specific coordinate
frame or duplicated presentation position is permitted. Property and street
labels use supplied names. Canonical `true_north_radians` remains the north
source; a sheet north arrow is a projection through the declared view transform.
Any later projection from known 3D geometry must explicitly verify its plan
basis; tilted geometry cannot be flattened without disclosure.

Add `site_requirements: tuple[SiteRequirement, ...]` only for supplied separation
and working-space relationships required by this delivery. Each record extends
`Entity`, references canonical owners, and states its requirement kind, geometric
reference basis and supplied minimum dimensions in metres. It must not contain a
copied measurement or a `compliant: true` engineering claim. The projection
computes dimensions from the referenced current geometry. A source-marker to
equipment-region distance is labeled with that basis; unknown gas enclosure
extent cannot support a claimed enclosure-to-enclosure clearance. A modeled work
surface is distinct from field-confirmed construction and approved access.

This avoids three competing inventories: original feature coordinates, drawing
anchors and independent dimension endpoints. Equipment features are selected by
canonical equipment IDs; their position is always `ElectricalEquipment.pose`.
Private lateral geometry remains an existing canonical route with ports, not an
additional site line claiming the same conduit quantity. Its intercept must lie
on the supplied property boundary within a documented geometric tolerance.

### Service graph and load facts

The existing open `ElectricalEquipment.equipment_type` field can express
`meter_main`; its single-line admission/rendering needs a combined symbol for one
canonical object. `Port.owner_id` and `Circuit` references already express
meter-main to main-lug-only panel to subpanel connectivity. No topology may be
inferred from nearby positions or physically overlapping route geometry.

Shared service facts that both the site and single-line/load consumers require
must be typed in this extension, not stored in `single_line_drafting`. Review a
small owner-bound service-facts record for continuous metering rating, equipment
rating, integrated service-disconnect function, main-lug-only termination and
panel circuit-position count. Unknown make/model and feeder facts stay null or
explicitly unresolved. Do not use a note or equipment name as a machine-readable
claim of approval, selected product or rating.

The load input remains an explicit calculation request bound to the complete
canonical source and referenced service owner. It supplies component VA, basis,
evidence, group membership and accepted demand/sensitivity assumptions. A derived
calculation report preserves these inputs and recomputes its arithmetic. This
does not create a second equipment/connectivity model and does not overwrite
`Circuit.load_va` with calculated demand. No general-purpose code-design engine
or new catalog-selection contract is required by this operation.

### Version and reader/writer behavior

Recommend explicit opt-in model version `1.8.0`, retaining default `1.0.0`.
Follow the existing self-contained `contracts/oabm-model-v1.x.schema.json`
pattern with exact field sets and existing primitive/evidence definitions.
`upgrade_to_v1_8` preserves all current canonical records and identities and adds
only empty inventories; it must not manufacture property lines, source evidence,
service ratings or calculation inputs. There is no automatic downgrade or silent
schema relabeling to make another consumer accept the model.

- Extend existing capability sets through 1.8 for boxes, raceways, supports,
  structure, fittings and assemblies. Keep the first 1.8 slice explicitly
  incompatible with populated `PhysicalRoute` inventory: migration refuses it
  before changing any source record. Physical routing and its exact numeric
  JSON admission remain on 1.7. Their contexts retain 1.7 source-version/hash
  semantics. No `source_model_sha256` or request hash is recomputed as part of
  upgrade. Supporting a bound physical route in a new source schema requires
  a separately reviewed binding migration, not a membership-set addition.
- Reject populated new inventories under versions 1.0 through 1.7. Omit new
  fields entirely when writing those versions, including empty defaults, so
  pre-existing serialized bytes and fixture hashes remain unchanged.
- Older `upgrade_to_v1_*` helpers must refuse 1.8 input rather than downgrade it
  and discard new semantic objects. Add explicit downgrade regressions for every
  helper, not just the new upgrade operation.
- Register site/requirement/service records in the model's global ID inventory,
  strict JSON decoding, schema and cross-reference validator. Validate kind/type
  agreement, finite geometry, simple polygons, endpoint/host ownership and scoped
  provenance. Preserve existing bounded detached source admission in new public
  request/export boundaries.
- Unsupported consumers must explicitly refuse populated new inventories until
  they have an honest coverage implementation. Empty upgraded models may retain
  existing behavior only after source review and tests show it is safe. Do not
  globally admit 1.8 and silently omit site objects from an IFC or GLB export.

### Source identity and projection receipts

Reuse `Provenance` with field-qualified scopes. Private evidence should carry a
stable source ID, exact source-file SHA-256, one-based page/sheet identity,
observed versus user-supplied/assumed derivation, and the supplied source-to-model
registration evidence. Keep these receipts in the private model, not public
fixtures. A hash authenticates bytes, not their engineering correctness.

The derived site request and each sheet receipt retain the complete normalized
model hash, canonical IDs and exact consumed evidence. Rebuilding after a field
edit must rebuild these projections, not mutate a cached independent site file.
Stale source bindings refuse before output reservation; no nearest-feature match,
PDF inference, automatic source reconciliation or assumed street-side route is
part of this command.

## Current execution and acceptance limits

Independent review is defining the narrow site/service contract before its
implementation; repository instructions prohibit a drawing-lane substitute. Source PDFs
are retrieved privately but scale/position visual acceptance has not run under
the current execution hold. Private delivery authorization is held by the parent.
None of these limits justify counting this issue as completed.

## Source implementation checkpoint

`model/site.py` now defines the proposed strict plan geometry, source evidence,
site feature/requirement and owner-bound service-fact records. Geometry has a
256-point admission cap and rejects degenerate, crossing or overlapping paths.
These are source-only candidate types: they are deliberately not registered in
`BuildingModel`, exported as a supported schema version, or consumed by any
existing consumer yet. Synthetic type-level cases are written but unrun under
the owner hold. Cross-record owner/frame/source resolution, versioned schema and
migration integration are next, followed by actual projections and regeneration.

The unresolved-placement decision must cover both `ElectricalEquipment.pose`
and `Port.pose`/`direction`; making equipment placement optional alone would
still fabricate terminal geometry. The bounded optional-placement option would
permit explicit null placement only in 1.8, keep graph identity/connectivity,
and make spatial consumers refuse before geometry access. Plan-only equipment
location and lateral facts also need a canonical representation without lifting
XY into a fake 3D route. These points remain explicit design decisions for the
next integration pass, not silently solved by placeholder poses or attributes.
