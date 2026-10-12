# Physical electrical boxes (contract 1.1)

Issue #265 adds physical boxes and their selection outcomes to the canonical
model. [Whole-route raceway/cable selections](raceway-selections.md) use the
explicit 1.2 boundary; [shared supports](shared-supports.md) use the explicit 1.3
boundary. Mixed segment selection remains separate work. Automatic PDF box selection, box-fill compliance, listed-product
verification, prices, and complete installation quantities are not delivered by
this boundary.

## Version and migration

`BuildingModel` continues to default to `1.0.0`. Its JSON omits the new
collections, and the original schema and fixture hashes remain unchanged.
Version 1.0 rejects `electrical_boxes` and `box_selection_outcomes`, even when
empty. `upgrade_to_v1_1(model)` explicitly changes the version and supplies empty
collections without changing existing IDs or facts. The standalone
`contracts/oabm-model-v1.1.schema.json` requires both collections; unknown
versions and unknown fields are rejected. Downgrading a populated model is
refused. Python's existing model namespace exposes the supported versions;
consumers must inspect the document version rather than infer it from imports.

## Producer and evidence

`oabm.electrical.select_electrical_boxes` requires an explicitly upgraded model,
`BoxIntent` records, and `BoxCatalogEntry` records. Each intent specifies its
unique identity, actor, occupant devices, requested catalog product, independent
pose, architectural host, and mounting. Catalog product identity includes catalog,
revision, and product keys. The catalog supplies box type and exterior `Size3`
(in metres), with its own field-qualified provenance. No symbol graphics supply
these dimensions. User intent qualifies pose, host, mounting, and occupants;
host level assignment is separately inferred from the selected canonical host.
Field-qualified catalog evidence retains its scope; an unscoped record is
qualified to the consumed catalog fields, including box type.

`listed_volume_m3` is optional, explicitly supplied interior capacity in cubic
metres. It is never calculated from exterior width × depth × height. Unknown
capacity remains null; the envelope supplies only a consistency upper bound.
Product selection and supplied capacity do not establish code compliance.

Exactly one active outcome exists per intent. A selected outcome points to the
physical box with the same intent, occupants, and product. A ganged box has
multiple unique occupant device IDs and counts as one physical box. Devices
cannot occupy multiple boxes or conflicting outcomes. Device size and pose stay
independent. A box must reference an existing architectural wall, slab, or
ceiling and a matching level; device hosting claims cannot contradict it.

Missing intent, pose, host, mounting, catalog entry, or catalog size produces a
typed unresolved outcome with reason codes, not a physical solid. Missing intent
for a device means no intent was supplied, not that every device requires a box.
The producer recomputes its own complete inventory with IDs based on model and
intent identity. Repeating inputs is idempotent; changing product dimensions
preserves identity without retaining obsolete decision history. Other-source
inventory is refused. Removing/resizing boxes with incompatible live ports or
routes fails reference validation rather than silently deleting dependent work.

## Consumers and lane boundaries

Boxes own canonical ports inside/on their independent oriented envelopes and
can be endpoints for the existing router. They are not `Obstacle` or `Opening`
workarounds and do not add routing-grid obstacle axes.

IFC exports a native `IfcJunctionBox` with exterior Body and placement,
readable catalog/mounting/occupant/capacity properties, and native distribution
ports. Selection outcomes are non-geometric annotations. Canonical metadata
carries the actual model version. Native placement edits read back from IFC and
add scoped user edit provenance; ports must remain consistent with the new
placement. Altered/missing exterior Body geometry is refused because retaining
catalog sizing claims after that edit would be misleading. This does not turn
the exterior envelope into listed interior capacity.

GLB exports the independent box solid and includes its extent in reference
plane framing. Outcome metadata exposes unresolved work without invented
meshes. Plans/elevations/sections derive box geometry, and schedules expose box
products and all outcomes. Quantities count one box per physical object,
regardless of gang size; unresolved selections and unverified fill remain
warnings. Device takeoff comparison continues to count devices/equipment only.
QA handoff iteration includes both typed collections.

The box producer runs after PDF architecture/electrical convergence, once host
and frame claims exist. Pure importer inputs containing physical boxes or
outcomes are refused by convergence, so independent poses cannot be silently
unregistered or dropped. Empty explicitly versioned 1.1 lane models carry that
version through convergence. Automatic PDF host/size recovery remains unmet and
must be resolved as a separate producer capability.

The generated public synthetic tests in `tests/test_electrical_boxes.py` cover
migration, resolved/ganged/unresolved selections, provenance, malformed links,
native IFC edits, and producer → router → IFC/readback → quantities/drawings/QA.
They are engineering fixtures, not a real installation compliance certificate.
