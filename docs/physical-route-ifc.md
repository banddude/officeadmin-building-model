# Analytic physical-route IFC

`to_ifc(model, physical_routes=True)` and `round_trip(..., physical_routes=True)`
explicitly admit canonical `line_arc_v1` paths. The default export boundary still
refuses them. Schema versions, identities, supplied actual OD, centerline radius,
joint/segment records, request bindings and quantities are preserved.

Each canonical segment has one native product under its existing route system.
Its Axis is a two-point `IfcPolyline` or a quarter `IfcTrimmedCurve` over an
oriented `IfcCircle`. Its closed Body is a circular-profile extrusion for a line
or a circular-profile quarter revolution for an arc, using the actual supplied
OD/2 and centerline radius. An explicit radian unit fixes parameter/rotation
interpretation. Curves are never flattened into chords. Existing
group/layer adapters select these same products, without additional solids.
The existing route remains the only owner of the conduit envelope. No purchased
elbow/product is inferred. Physical conductor traversals can be quantified, but
their post-routing graph bindings require retained-source transport for IFC and
are refused by this bounded source profile rather than being stripped.

Readback requires the complete native segment inventory, stable GUIDs and
canonical segment identities, expected IFC product type, unchanged placements,
actual Axis/Body geometry, OD, circle center/radius/orientation and parameter
trims. Physical-containing imports require exactly one unprefixed SI METRE length
unit and one unprefixed SI RADIAN angular unit. Missing, duplicate, prefixed or
conversion-based length units refuse rather than silently rescaling geometry
behind unchanged canonical metadata. Native physical edits refuse rather than
resurrecting missing geometry
from metadata or reusing a stale physical request. Regenerate a fresh canonical
route for a geometry change. Route JSON admission is bounded before parsing and
checks original decimal physical numbers before float conversion.

For this bounded IFC profile, physical routes must bind the unchanged shared
pre-routing source. Only the known generated physical routes are excluded to
reconstruct it; all legacy routes and other canonical facts stay present. Source
Name/pose edits refuse even if endpoint ports stay fixed. Source models with
post-routing dependent changes or differing/sequential source snapshots require
an explicit retained-source transport and remain unsupported here; the importer
does not strip dependent facts or rewrite bindings to make them pass. A
physical-containing native file also refuses untagged/unowned conduit or cable
products rather than dropping extra solids during reconstruction.

Physical route-owned additional canonical ports remain an explicit unsupported
binding: analytic point-on-curve ownership needs its own implementation. Existing
endpoint equipment/device ports and route-system connectivity are retained.
Actual swept geometry is a derived model view, not surveyed installation fit,
product compatibility, engineering approval or a live clearance certification.
These solids describe the supplied outside envelope; their volume is not a
conduit-wall material takeoff or an inference of inner diameter.

Focused checks cover real STEP-byte reparse, strict IFC4 EXPRESS validation,
one/two bends, opposite turns, odd-nanometre radii, deterministic exports, exact
canonical round trips and refusal of native geometry/inventory tampering.
Actual native kernel evaluation independently checks closed-solid volumes against
analytic cylinder/quarter-torus volumes and each bend's physical bounds. Schema
validity and matching metadata alone do not prove rendered geometry is correct.
