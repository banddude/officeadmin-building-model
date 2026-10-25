# Building assemblies: isolated contract 1.6 draft

**Scope:** explicit canonical version 1.6, the first bounded implementation
slice of [issue #356](https://github.com/banddude/officeadmin-building-model/issues/356).
The historical draft filename is retained for stable links. A separate
[straight timber-wall producer](building-assembly-wall-producer-v1.md) resolves
versioned defaults and creates framing/drywall around rectangular opening
voids. Separate [insert](building-assembly-opening-inserts-v1.md) and
[ceiling](building-assembly-suspended-ceiling-producer-v1.md) producers add
simple door/window parts and a supported rectangular grid layout below an
existing Slab. GLB and quantities support the physical records; unsupported consumers
refuse populated assemblies. This is not a complete building/electrical workflow.

## Decision captured in the candidate

The intended future data flow is reviewed `Wall`/`Opening`/room/deck facts plus
an agent-selected immutable preset revision, then deterministic owned physical
components. Plan/takeoff interpretation stays upstream. The canonical records store
only an immutable catalog/preset identity and revision reference, selector, and
typed resolved-value snapshots. It does **not** define or ship a preset catalog,
recipe decoder, parameter-definition/default resolver, component-template
format, or compatibility resolver. The reference plus snapshot is not an
executable recipe and cannot generate geometry. Preset defaults are allowed
only when declared, versioned, snapshotted, and labelled as assumptions;
missing required facts without a selected default or override stay unresolved.
The separate wall producer implements a finite executable catalog and default
resolver; the canonical references alone do not execute recipes.
This contract does not infer materials, hidden trades, structure, capacity,
fire rating, code compliance, or construction authority.

The contract explicitly uses `1.6.0`. The default remains `1.0.0`; versions
1.0 through 1.5 retain their current reader and byte semantics. `upgrade_to_v1_6`
is explicit and adds empty assembly collections; it never invents a preset,
material, dimension, or component. The candidate is intentionally not a
producer API and does not make a selected preset capable of geometry generation.

## Candidate records

`BuildingAssembly` records the kind, host and level, aggregate-host vs opening
void representation, catalog/preset identities and revisions, selector,
generator version, typed resolved parameter snapshots, exact owned child IDs,
and input/output SHA-256 digests. Parameter values carry a strict kind, canonical
unit, origin (`plan_observed`, `preset_default`, `user_override`), and
field-qualified provenance. A plan-observed value names a typed canonical
source entity and field, must equal that field, and must retain matching
observed source-page/element provenance. Host and deck provenance must be
entity-wide or explicitly cover the source geometry fields used (for example,
wall centerline/thickness/height, opening pose/size, or slab footprint/thickness);
the union of matching observed records may supply those fields. A `name`-only
observation cannot stand in for geometry evidence. A preset default binds to the selected
catalog/preset revision and its parameter ID; a user override binds to that
parameter ID and user derivation. A default remains an assumption, not an
observed fact. Assignment selection provenance binds the immutable selection
reference and actor; observed host/deck provenance binds the source facts in the
input digest. Component material can be explicitly unknown; unknown is not a
material or geometry basis.

`AssemblyComponent` is the single physical identity for one generated item. Its
geometry union is limited to canonical boxes, straight C/U/T profile
extrusions (or bounded source-bound manufacturer section outlines), planar
simple panels, and finite straight-segment round-wire sweeps. Parameterized C
profiles have inward return lips whose vertices form a checked simple outline;
the dependency-neutral strict geometry kernel rejects self-crossing, touching,
zero-area, or untriangulable sections before they enter the model. Panel area
and face checks use that same robust local-anchor math, avoiding cancellation
for translated coordinates. Tolerated input quaternions are normalized for
calculations without rewriting their stored values. Wire end treatment is
`cut` only in this candidate: hooks/loops have no declared end geometry and
are refused until a later typed geometry exists. A component's
geometry basis distinguishes specified parameterized geometry,
manufacturer-actual geometry, and a caller-selected envelope. A known material
identity requires `material_id`-scoped observed evidence for `plan_specified`
or `manufacturer_actual`, and `material_id`-scoped user evidence for
`caller_selected`; all `material_id`-scoped evidence must agree with the
declared basis, which Python checks across records. Manufacturer outlines
require observed `points_m`-scoped provenance bound to their source. An envelope may
conservatively bound collision but is not an exact material volume. A wall
assignment includes an explicit ordered axis/base/left-normal frame. A
suspended-ceiling assignment must reference exactly one existing `Slab` deck
or one owned `deck_panel`, never both. Canonical slab footprint is its top
face and slab thickness extends downward; hanger top anchors are accepted only
on that top face or its exact underside, never at an invented mid-plane.
A generated horizontal deck may be a yawed box or horizontal panel, but child
containment uses its actual footprint, not a world-axis AABB; tilted boxes and
sloped panels are refused. Sloped source slabs/ceilings are also refused in
this bounded slice. Ceiling children, including finite wire extents, must fit
the horizontal convex ceiling-host and selected deck footprints and the
host/deck vertical envelope. Each hanger wire
carries one typed top/bottom attachment record; anchors must be on the selected
deck and named runner physical surfaces, not merely inside a host envelope.

The first wall slice accepts only a two-point, straight horizontal wall axis.
The assignment snapshots its start, ordered unit axis, canonical left normal,
and base elevation. Source wall thickness and height are never rewritten. Door
and window voids use a center in model coordinates. Their width follows the
ordered host axis; their depth matches the source wall thickness. Height and
sill elevation remain explicit.
The validator checks orientation, center plane, fit and overlap, doors at the
wall base, and a resolved framing/layer/gap stack exactly matching source
thickness within `1e-9 m`. A mismatch is refused rather than repaired.

Ownership is exact: every component ID is globally unique, listed once by one
assembly, assigned one level, and no child is unowned. At most one active wall
or ceiling aggregate and one opening-insert assignment exist per host. An
assembly kind has a closed allowed component-role set; incompatible roles and
geometry variants fail closed. Every child ID is recomputed from its owner ID
and semantic `part_key`; list-index keys and repeated part keys are refused.
The JSON Schema enforces structural shapes, required provenance scopes, text
bounds and per-record caps. Cross-record uniqueness (including parameter IDs
and semantic part keys), source-reference equality, geometric fit and digest
freshness remain explicit Python semantic checks; these are not claimed as
ordinary Draft 2020-12 schema constraints.

An
aggregate host remains a spatial boundary only and must not also be emitted as
a body, obstacle/surface, or billable material. An opening remains visibly
unfilled unless a separate explicit `opening_insert` assignment owns its insert
components. An overlapping unowned v1.4 `StructuralMember` is
`member_overlap_unresolved`; neither physical identity is deleted or equated.

Input digests cover exact current host/opening/deck observations with their
provenance, preset identity/revision, generator version, frame, selection
evidence, and sorted resolved parameters. Output digests cover the sorted full
owned component records. Model construction/load recomputes each digest and
rejects stale source facts or children. Immutable candidate construction is
all-or-nothing at the model boundary. Regeneration, route invalidation, and a general dependency engine remain
unsupported. The first wall producer refuses replacement or a source model
with existing routes; it does not silently regenerate dependent geometry.

## Boundedness and deferred work

Contract records cap resolved parameters at 128, components at 4,096, profile
points at 64, panel vertices at 128, and wire segments at 32. These are software
resource caps, not construction defaults. A future producer/mesh path must
preflight predicted component, vertex, and allocation costs before allocating;
it must fail closed rather than truncate. The allowed-route-domain cell cap
and 250,000 derived mesh vertex cap belong to their later routing/geometry
implementation, not this model collection.

The geometry kernel is reused for strict outline validation and shared
quaternion/wire-bound calculations. The separate first wall producer includes
a finite immutable preset catalog/default resolver, framed opening voids and
drywall. Additional finite catalogs resolve explicit door/fixed-window inserts
and suspended-ceiling T runners, tiles and straight hanger wires. Perimeter
support hardware, tee connector tabs and hanger anchors remain unmodeled;
geometry contact does not establish engineering approval.
GLB and quantities support owned components and suppress duplicate
aggregate-host physical output. GLB checks the total assembly vertex budget
before mesh creation and rejects coordinates that cannot preserve the supported
geometry within its declared float32 precision tolerance.

Not implemented: plan/importer/AI recognition; full-room corners/joins;
metal-stud presets or a generated-deck producer;
assembly route-domain adaptation and route invalidation; component-aware IFC,
drawings, revision-diff, confidence-review or review bundles; automatic
penetration logic or construction/engineering approval. Unsupported output
entrypoints fail closed on populated assembly collections. Canonical
serialization, explicitly electrical-only reports, and empty upgraded models
remain available. This slice does not claim a complete building or electrical
system, and issue #356 remains broader than this first proof.
