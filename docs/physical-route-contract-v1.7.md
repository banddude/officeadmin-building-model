# Canonical physical routes, opt-in model 1.7

This is the structural contract stage of [issue #228](https://github.com/banddude/officeadmin-building-model/issues/228).
It does not implement a physical router, obstacle-clearance verifier, fitting
selection, code design, sizing, supports or physical-route drawings. A valid
model or matching hash is **not a verified installation**. No saved attribute
or receipt is accepted as geometric certification.

## Version and one source of truth

Default model version remains 1.0.0. Versions 1.0 through 1.6, their schemas,
legacy Route type, and existing serialization are unchanged.
`upgrade_to_v1_7(model)` is explicit and preserves old routes as old routes.
It does not infer an actual diameter, bend radius, curved geometry or source
authority. All older upgrade helpers refuse a 1.7 model, even an empty one.

`BuildingModel.routes` is the sole route collection. It contains unchanged
legacy `Route` records and strictly tagged `PhysicalRoute` records.
The latter require `geometry_kind: "line_arc_v1"`, have no `centerline`,
and cannot hide another path in attributes. The schema is
`contracts/oabm-model-v1.7.schema.json`. Missing and unknown tags, extra
fields, or a physical record with legacy shadow geometry are rejected.

A physical route owns:

* Stable, globally unique `PhysicalRouteJoint` entities with `route_id`
  and `position`. The ordered joint tuple is the one nonbranching chain.
* Stable `LineSegment3D` and `QuarterArc3D` entities. They reference the
  adjacent shared joint IDs and their owning route, not duplicated positions.
* A `PhysicalRouteRequirements` entity with six supplied dimensions and
  field-qualified user/observed provenance.
* A typed retained `PhysicalRoutingContext` request snapshot.

The joint/segment/requirements/domain IDs join global identity validation.
Provenance belongs to each actual owner. An indexed string such as
`segments.2.radius` is not an invented scope mechanism.

## Geometry and dimensions

Line segments have `kind: "line"`. Quarter arcs have
`kind: "quarter_arc"`, a positive unit-axis `plane_normal`, and
`turn: "positive_quarter" | "negative_quarter"`. Negative normals are
noncanonical encodings, not silently normalized alternatives.

Arc centre, radius and tangent are derived exactly from its two joints and
turn. There is no independently editable centre or sharp vertex. Both varying
coordinate deltas have the same nonzero absolute magnitude; the remaining
coordinate is constant. This is a quarter circle in XY, XZ or YZ.

Each segment is positive. Adjacent lines must already be coalesced; two arcs
need an intervening positive line. Shared tangents must agree exactly.
First/last segments must be lines. At ports the first traversal tangent follows
the start port outward vector, and the final tangent opposes the end port
outward vector. Port directions must be exactly signed unit axes.

Requirements are supplied, not selected or inferred:

| Field | Exact admitted range, metres |
| --- | --- |
| `outside_diameter_m` | 0.001 to 1 |
| `centerline_radius_m` | 0.001 to 10 |
| `clearance_m` | 0 to 100 |
| `start_straight_min_m` | 0 to 2000 |
| `end_straight_min_m` | 0 to 2000 |
| `between_bends_straight_min_m` | 0 to 2000 |

Additionally, radius minus half the actual OD must be at least 0.00001 m.
Every arc radius equals the supplied centreline radius exactly. A lone straight
must meet the maximum of the two terminal minima, not their sum: these are
distances to the first bend/opposite terminal, not disjoint reserved zones.

These bounds are computational limits, **not engineering recommendations**.
Actual OD is never inferred from a nominal trade size.

The intended physical solid is the normal-disc sweep with flat terminal caps,
not a capsule or a sampled display tube. This stage validates the exact
centreline structure but does not test that solid against obstacles, itself or
the allowed domain. For example a structurally valid line may still cross a
blocker. `physical_segment_facts` returns derived exact centreline facts only.

## Exact numeric admission

The required profile is `physical-routing-decimal-nm-v1`. Authored coordinates
and dimensions are exact decimal multiples of 1e-9 m. Consumed coordinates are
bounded to [-1000, 1000] m. Integer nanometre ticks and exact rationals are used
for connectivity, tangency and radius tests. No tolerance permits a gap or kink.

`BuildingModel.from_json` validates original physical-route/context numeric
tokens **before** binary-float conversion. A token such as
`0.30000000000000001` does not silently become `0.3`.
In-memory dataclasses and `from_dict` instead interpret the deterministic
canonical rendering of the supplied number. They cannot recover a lexeme a
caller already rounded. Booleans and nonfinite values are not admitted.

Validation checks that a supplied number can serialize and reparse with
identical ticks. There is no snapping. Generated-output producers must retain
this closure; a future search must report unresolved numerics or continue
search rather than emit altered geometry.

Derived half dimensions may be exact rational half ticks. Domain/blocker boxes
use positive full dimensions from 1e-9 through 2000 m, with all extrema in the
coordinate bounds. This first profile requires identity box rotation. A
different quaternion is explicitly unsupported, not silently enlarged to an
AABB or rotated by an implicit adapter.

The portable schema states exact decimal `multipleOf: 1e-9`. Clients should
validate JSON decimals with decimal-aware arithmetic rather than treat a
binary floating division artifact as a schema violation.

Legacy-only 1.7 models retain legacy numeric semantics. The global loader does
not impose physical-profile rounding or new bounds on unrelated old geometry.

## Request and current-source binding

`PhysicalRoutingContext` retains request identity, numerical profile, source
model schema/frame, route type, endpoint IDs, an explicit allowed domain, coverage entries,
objective, execution limits, and source/request hashes. It is not a persisted
verification result.

The domain has 1 through 128 canonical boxes; the future geometric check is
against their **union**, not independently eroded boxes or their bounding box.
There are 1 through 1024 route primitives and at most 10000 coverage entries
and 4096 resolved blocker boxes. Bend penalty is explicitly supplied in
[0, 1000] m. Execution limits are positive integers bounded by:

* 128 candidate routes;
* 200000 candidate grid nodes;
* 500000 pushed states;
* 64 interval refinement depth;
* 1000000 total refinement cells.

These are retained limits, not a search implementation.

The digest encoding is `physical-routing-binding-json-v1`. The source hash
uses UTF-8 bytes of
`BuildingModel.from_dict(model.to_dict()).to_json(indent=None)`.
The request hash uses analogous typed normalization, sorted compact JSON,
full context except its own digest, and full requirements. Typed `1` and
`1.0` normalize intentionally. There is no tolerance, coordinate rounding,
entity omission or reordered geometry. Original source bytes are evidence,
while this normalized snapshot is the binding representation.

Use:

```python
validate_physical_context_source(
    current_pre_route_model, candidate_route,
    source_json=original_source_json,  # Supply when input originated as JSON.
)
```

With `source_json`, consumed original source numeric lexemes are checked before
ordinary decoding, and its normalized snapshot must equal the supplied model.
Successful structural admission returns `"raw_json_lexemes"`. Without bytes,
the helper returns `"canonical_python_snapshot"` and makes no claim about
unavailable original tokens. Both results are **binding modes, not safety
results**. Future verification receipts must preserve the distinction.

The source must already be explicitly upgraded to 1.7.0. The bound route type
must exactly match the physical route and drives constraint applicability.
The source must be the current pre-route model, not the output containing
itself. Generic model loading checks the retained request hash/structure but
does not compare a pre-route hash to a self-containing output. A future
verification entrypoint must explicitly reconstruct the base by removing only
the candidate in question. Every other change, including a new blocker or
same-ID modified geometry, remains in the source binding.

## Complete supplied geometry boundary

Actual blocker geometry remains in canonical Obstacle, applicable hard
RouteConstraint and StructuralMember records. Coverage never creates a second
untracked obstacle list. Obstacle/constraint `clearance_m` is consumed directly
and must be an exact supplied value within [0, 100] m. A StructuralMember
has no additional-clearance field: this profile explicitly defines its extra
clearance as zero, while route clearance and the future fixed certification
margin still apply. Larger member-specific margins require an additional
canonical hard Obstacle; the mandatory member is never replaced or omitted.

Fresh inventory covers obstacles, active route constraints, structural members,
walls/slabs/ceilings/openings, geometry-bearing equipment/devices/boxes, assembly
and support components, dimension-derived fitting-selection envelopes, and
preexisting routes. Unknown populated collections
fail closed. A device/equipment with explicit `size=None` is geometry
unspecified, not an automatically exempted physical body.

Every active route constraint is considered using its empty/type/wildcard
scope. Only hard keep-out/avoid boxes are supported in this first admission
profile; active preferences, required corridors and unknown semantics refuse
rather than silently alter or ignore the request.

Each inventoried source has one explicit `PhysicalGeometryCoverage` record.
Direct blockers bind their own identity. Hard sources cannot be excluded.
Other physical sources may carry an explicit caller-authorized
`outside_proof_scope` declaration, never an automatically generated exemption.
Such a request cannot support an unqualified whole-building clearance claim.
The synthetic acceptance examples do not use exclusions.

Supplied-envelope references must target canonical obstacles. Their enclosure
adapter is not implemented, so source admission refuses them rather than
asserting that the envelope conservatively contains the original source.
Unsupported direct source geometry also refuses. A changed source hash or
missing coverage requires fresh admission. Stage one does not produce a
`verified_route` result, even with a completely matching request.

## Compatibility boundaries

Generic canonical JSON load/save and structural validation support physical
routes. Existing downstream route consumers do not yet support their solids.
They explicitly refuse physical-only or mixed models before filtering out
hidden routes, invoking callbacks, mutating a model, or writing output.

This includes existing routing/placement/consolidation, product selectors,
IFC, GLB, drawings/delivery wrappers, quantities, geometric reports, bend QA,
revision/replay paths and unsupported source snapshots. Legacy fitting,
raceway-selection and support references to physical routes are also rejected
by canonical validation, not only by producers. Identity-only hashing may
remain generic where it does not claim geometry support.

No physical route is converted into a sharp polyline for an old consumer.
Empty/legacy-only upgraded models remain usable in old supported workflows.
