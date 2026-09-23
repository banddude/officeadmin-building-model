# Quantities

`extract_quantities` turns a validated `BuildingModel` into a `TakeoffReport`. It derives
everything from canonical semantics: route centerlines, canonical fittings, explicit conductor
route references, wall centerlines and dimensions, slab/ceiling/space footprints, and opening
types. It never remeasures a drawing, reconstructs geometry, or infers a route.

## What it measures

Electrical: `route_length`, `fitting`, `conductor_length`, `device`, `equipment`.

Architectural: `wall_length`, `wall_face_area`, `wall_volume`, `slab_area`, `slab_volume`,
`ceiling_area`, `space_floor_area`, `space_volume`, `opening_count`.

Units are explicit on the report: `m`, `m2`, `m3`, `ea`.

`wall_face_area` is **one face**, stated as `faces: "single"` in the variant. A caller wanting
both sides doubles it. Emitting both faces as separate lines would be summed into a double
count by anyone who did not read the variant.

Wall `thickness_m` and `height_m` are in the variant of every wall line so materially different
walls stay on separate lines. Membership of the variant is a grouping choice and is deliberately
**not** consumption: the length arithmetic never multiplies a thickness in.

Polygon areas use Newell's method, so a planar polygon is measured correctly in any orientation.
Projecting onto XY would under-report every sloped footprint and silently return zero for a
vertical one.

## Scope, instead of silence

A caller must be able to tell "this model has nothing to measure" from "this lane does not
measure what you gave it". An empty `items` tuple cannot say which, so `TakeoffReport.scope`
states it: the kinds this lane can measure, the kinds present, the kinds actually measured, and
a reason for every present kind it did not measure.

`level`, `port`, `obstacle`, `route_constraint` and `circuit` are not quantities in their own
right and each carries a stated reason — a circuit is measured through its conductors and routes,
so counting it again would double-count that work. These reasons live in the scope statement
rather than in warnings: they are present in virtually every well-formed model, and warning on
them every time would train callers to ignore the channel that real problems use.

A warning **is** emitted when a kind this lane does measure produced nothing
(`kind_not_measured`), and whenever `items` is empty (`empty_takeoff`, carrying
`scope.empty_reason`). An empty report always says why it is empty.

`scope.declined_derivations` names derivations the lane refused. Today that is
`opening_area_deducted_from_host_wall`: the canonical contract does not state which two of
`Opening.size` x/y/z form the in-wall face, nor the frame `Size3` is expressed in. Today's
producers happen to agree on x=width and z=height, but convergent convention is not a rule a
conforming importer must follow, and deducting on it would silently shrink a measured wall area.
Openings are counted; no area is deducted.

## Provenance rides on the entity; a derivation class belongs to a claim

Each line carries `derivation`, classifying **that quantity** rather than the entity it came
from. A quantity declares the entity fields its arithmetic consumed, and a `Provenance` record
naming an `assumed_dimension` reaches only the quantities that consumed that dimension. A record
naming none speaks for the whole entity and reaches everything derived from it.

So for a scanned wall whose thickness the importer supplied:

| line | consumes | class |
| --- | --- | --- |
| `wall_length` | centerline | observed |
| `wall_face_area` | centerline, height | observed |
| `wall_volume` | centerline, height, thickness | inferred |

Both appear in the same report. Tainting the area because the entity carries one inferred record
would report a measurement as a guess.

Precedence when several records are relevant: **inferred**, then **unstated**, then **user**,
then **observed**. Unstated (`derivation=None`) outranks user and observed deliberately, and a
quantity with no relevant record at all is unstated, never observed. A record that never said how
it came to exist is not a claim of observation, and reading it as one fails in the dangerous
direction.
