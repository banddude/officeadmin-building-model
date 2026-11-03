# Opt-in site/service model 1.8: source checkpoint

This branch implements an unexecuted source checkpoint under existing #361.
It is not a completed utility package or engineering acceptance. Private plans,
source coordinates, component selections and project outputs are not public
fixtures. [Design and boundaries](model-utility-set-design.md) explain the lane.

## Model boundary

`upgrade_to_v1_8(model)` explicitly selects `1.8.0`. Default `1.0.0` and canonical
schemas 1.0 through 1.7 are unchanged. New fields are omitted from old serialized
versions; older documents reject new fields even when empty. All old upgrade
helpers reject 1.8 input. These compatibility claims require runtime verification
before publication; synthetic regression cases are written but currently unrun.

Migration preserves existing records and adds empty site/service arrays. It
refuses populated 1.7 physical routes before changing anything. Their contexts,
numeric admission, source versions, source hashes and request hashes remain 1.7;
there is no automatic rebinding into 1.8. This slice retains legacy spatial
routes, boxes, selections, supports, structure, fittings and assemblies.

New canonical arrays:

- `site_features`: explicit property polygon, source-traced or owner-projected
  building outline, street reference line, gas marker/outline/enclosure,
  property-line intercept and owner-linked working surface.
- `site_requirements`: supplied horizontal separation or working-surface
  requirement. Reference basis distinguishes point-to-point, anchor-to-boundary,
  envelope-to-envelope and surface-to-equipment. Unknown geometry/orientation
  remains unresolved, not a compliant bounding box.
- `service_nodes`: a supplied service-diagram reference to a source, integrated
  meter-main or panelboard. Placement is explicitly `unresolved`, `plan_only`,
  or `placed_equipment`. The last references an existing equipment owner and
  cannot override its position. Plan-only XY is not a spatial equipment pose.
- `service_connections`: explicit supplied service relationships. A known
  spatial circuit can be referenced only when its equipment/ports agree. An
  unresolved relationship does not invent a circuit, port, feeder size or route.
- `service_equipment_facts`: one authoritative record per resolved physical
  owner for supplied ratings, integrated service-disconnect function, MLO state
  and circuit positions. Unknowns remain null; conflicting aliases are refused.
- `plan_service_laterals`: an explicitly supplied private plan path between a
  service node and property-boundary intercept. It has no invented depth and is
  not included as a 3D route or a second conduit quantity.

Global IDs and scoped provenance use the existing canonical contract. Dangling
owners, incompatible geometry, conflicting frames, duplicate service facts,
ambiguous/cyclic service supply and stale lateral endpoints are refused.

## Geometry and calibration

`SitePlanGeometry` reuses `Point2D` and admits a point, simple open polyline or
simple closed polygon with implicit closure, at most 256 points and no truncated
output. It has no Z or default elevation. Area uses differences from a local
origin and stable summation, preserving small translated polygons.

`SiteSourceEvidence` retains file SHA-256, source ID, one-based page, sheet,
region, original coordinate units, drawing units, printed scale and destination
model frame. Source coordinates use the normalized displayed-page top-left
origin, +X right and +Y down. PDF points are exactly 1/72 inch. Normalizing page
rotation/crop is an upstream evidence operation; it is not guessed by this type.

The six transform values are `(a,b,c,d,tx,ty)`:
`X = a*x + b*y + tx`, `Y = c*x + d*y + ty`.
Only a metric similarity/reflection is admitted. Both column lengths must equal
the source-unit-to-metre factor times the supplied printed scale denominator;
the columns must be orthogonal, with negative determinant to convert page Y-down
to model XY. Shear, unequal-axis scaling and conflicting printed scale refuse.
The affine already includes unit and scale conversion; do not apply it twice.
This is not a general distorted-scan calibration or PDF inference engine.

For the 1.8 site projection, supplied `true_north_radians` is clockwise from
model +Y toward +X; output north is the same bearing in degrees. Unknown north
refuses projection. Other source frame conventions must be explicitly registered
upstream and retained in the evidence.

## Callable source path

`oabm.electrical.service_site.move_plan_service_node` applies a supplied user
field edit to an already located plan-only node. It preserves IDs and original
evidence, updates dependent private-lateral starts and owned proposed work
surfaces atomically, and refuses invalid geometry or relocation of existing
surfaces. It never moves unknown 3D ports or rebinds physical routing receipts.

`oabm.drawings.model_utility.project_model_site` verifies supplied source bytes,
then returns an existing site drawing request plus a model-hash/ownership receipt.
Only paper scale/layout settings are supplied separately. Omitted unlocated
service references are explicit. Known placed equipment projects its actual
rotated box footprint; unknown extents remain crosses. Measurements/dimensions
retain their geometric basis. Unknown enclosure/front-interface facts remain
unresolved. No measured marker separation is called enclosure clearance.

The default single-line projector never selects a service subset merely because
service records exist. It refuses unsupported mixed service references or direct
equipment facts instead of
silently omitting the canonical branch graph. `project_topology(...,
service_only=True)` and the corresponding `--service-only` command explicitly
select the service graph, retaining complete disjoint projected/supporting/omitted
owner inventories and visible scope diagnostics. Existing default topology
document fields remain unchanged. Three-document utility packaging must opt in
deliberately and retain this scope report.

Unresolved terminal IDs remain null. A meter-main renders as one integrated
owner and cannot carry contradictory false disconnect facts. Known referenced
circuits pass the same shared canonical graph admission as the default SLD,
including domains, terminal/network roles, conductor roles, port-only links,
voltage and topology checks; only view aliases and service-edge IDs change.

`oabm.reports.service_demand.bind_service_demand_inputs` creates a new report from
an explicitly unbound, separately source-verified load profile and the current
model. `calculate_service_demand` accepts an already bound request and refuses a
stale model hash. General first-block/remainder factors, HVAC/floor components
and sensitivity additions are supplied with source/basis; no factor or missing
group is inferred. Exact values are integer/rational strings; display amps are
finite numbers. Connected circuit VA is not overwritten or equated with demand.
A demand voltage contradicting a placed service owner's known voltage is refused.
Source readers check the descriptor actually opened, bound every streamed byte,
and refuse changes while hashing; path swaps or growth cannot evade admission.

The [three-document command](model-utility-delivery.md) now connects the source
projections, dimensions, supplied load report and shared company PDF backend.
Its synthetic workflow cases are written but remain unrun under the hold.

Legacy spatial exports, quantities, general drawing generators and callers of
the routing refusal guard reject populated site/service records before silently
omitting them. A remaining consumer audit, runtime execution and the private
three-sheet comparison/delivery proof are still required. No issue is complete
at this checkpoint.

### Numeric JSON normalization

New site/service float-typed facts use the existing typed decoder: direct Python
construction may retain integer spelling on first serialization, while decoding
normalizes float-typed fields to floats. Numeric values and identities roundtrip;
typed-decoded serialization is stable on subsequent roundtrips. Source hashes bind
the actual admitted snapshot bytes, not an assumed equivalence between integer
and float spellings. No legacy serializer normalization is introduced.

### Informational anchor reference distance

`SiteRequirement.kind = "horizontal_reference_distance"` is an informational
point-to-point measurement between explicit canonical owners. It requires
`reference_basis = "point_to_point"` and forbids non-null minimum, width, depth
and orientation fields. Null optional fields retain ordinary serialization.
Unknown anchors remain unresolved. Known anchors produce coordinate-only
distance with owner/evidence/model bindings and never a
`meets_supplied_horizontal_minimum` boolean. Its measured-legend label explicitly
says REFERENCE ANCHORS ONLY, without a clearance threshold. A separate minimum
enclosure separation can remain unresolved alongside it; the informational
measurement cannot satisfy that requirement. Existing minimum-separation and
working-surface admission rules are unchanged. Older schema files are unchanged.
