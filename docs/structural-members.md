# Explicit structural members (contract 1.4)

Issue #290 adds caller-confirmed rectangular structural members to the
canonical model. It is the first bounded framing slice under #240; [whole-route
raceway selections](raceway-selections.md) use the explicit 1.2 boundary and
[shared supports](shared-supports.md) the explicit 1.3 boundary. Recognition
from private plans, unknown other-trade systems, framing design, structural
calculations, drilling/notching decisions, code compliance, prices, and labor
are not delivered by this boundary and stay open in #240/#228/#227.

## Version and migration

`BuildingModel` keeps defaulting to `1.0.0`; the JSON of every older version
omits `structural_members`, and their schemas and fixture hashes stay
unchanged. Versions 1.0 through 1.3 reject a document containing
`structural_members` instead of silently dropping it. `upgrade_to_v1_4(model)`
is the explicit lossless opt-in: version becomes 1.4.0, every prior
collection (boxes, raceway selections, support inventory) stays valid, and
nothing is removed or downgraded. `contracts/oabm-model-v1.4.schema.json`
requires the (possibly empty) `structural_members` array plus every earlier
collection, and rejects unknown fields. Downgrading a populated model is
refused by the same explicit-version rules as every earlier boundary.

## Entity and evidence

`StructuralMember` carries an explicit `kind` (beam, joist, stud, header,
plate, or rafter), a known `level_id`, a typed `Pose` and positive `Size3`,
and optional `material` that stays unknown until a caller supplies it. The
member is an explicit confirmed input with field-qualified provenance and
confidence; nothing infers framing from typical construction, and no
producer infers members from plans. Duplicate IDs are refused by the
canonical identity rules, and the model validator requires the referenced
level to exist.

## Router, exports, and quantities

The router treats each member as conservative hard no-go geometry around its
canonical identity (the posed box expanded by clearance and route radius). A
rotated member's axis-aligned bounds deliberately cover more volume than the
member itself; that overexclusion is documented here, and no
penetration/boring/notching permission is implied. IFC exports each member
once as an `IfcBeam` (`ObjectType` carries the kind) with its canonical ID,
and the strict IFC round trip retains it. GLB draws one node per member with
the canonical ID and kind/material in `extras`. Quantities report the known
member count (`structural_member`, each) and rectangular solid volume
(`structural_member_volume`, m3) per kind and material; unchanged legacy
reports stay byte-identical, and nothing prices or estimates labor.

## Limits

Members are explicit confirmed inputs. This boundary does not recognize
framing, design structure, verify structural adequacy, permit penetrations,
or promise a production viewer; existing canonical slabs remain slabs.
#240 and #227 stay open.
