# Canonical quantities and explicit limits

`extract_quantities(model)` derives electrical and architectural quantities from
canonical entities. It does not remeasure source PDFs, reconstruct missing
geometry or invent construction assemblies. No prices or private inputs belong
in this lane.

## Building geometry

- Wall length is the ordered 3D centerline length.
- A straight horizontal wall has two gross face-area lines, left and right when
  looking along the centerline with +Z up. Each is length × height. End faces are
  not included. Its gross reference volume is length × height × thickness.
- Multiple-segment/curved or nonhorizontal walls retain length, but emit
  `wall_solid_undefined` instead of guessing corner joins, offset faces or volumes.
- Slabs and ceilings report actual planar surface area, not horizontal projected
  area. Spaces report their footprint surface area. Polygons must be simple,
  nondegenerate and planar. Nonplanarity, crossings, touches, backtracking and
  zero-length edges produce `unmeasurable_polygon`, never a measured zero.
- Area predicates use an absolute 1e-9 metre tolerance in a translated local
  frame. This is a refusal/validation tolerance, not coordinate snapping. It
  never grows with project coordinates and changes no canonical geometry.
- Horizontal footprints with a supplied thickness/height produce gross volumes.
  A tilted surface still produces its measured area, but volume is declined:
  the canonical contract does not resolve vertical versus normal extrusion.
  Missing thickness/height also produces a warning rather than a default.
- Openings are counted by type and size. Opening face axes, clipping, overlap
  unions and net host-face deductions are not established by the contract.
  `opening_deduction_undefined` makes that explicit. All wall areas remain gross.

Thickness, height and construction variants keep materially different wall lines
apart. Architectural IDs can participate in the existing caller-supplied bid
groups. No group, quantity, warning or evidence ordering depends on collection
order.

## Evidence

Architectural lines retain all entity evidence in `provenance`, including evidence
for variants. `quantity_provenance` is the exact subset applying to the fields
consumed by the scalar measurement, using canonical `provenance_applies_to`.
Legacy free-form attributes do not establish scope. An unscoped inferred record
covers the whole entity; invalid typed paths fail canonical validation.

`quantity_derivation` reports `inferred` if any consumed record is inferred,
`unknown` if evidence is absent/unclassified, `user` if any otherwise classified
record is user-supplied, and `observed` only if all consumed evidence is observed.
This is separate from design authority: a user-directed design can still contain
an inferred measurement. Aggregation keeps these classes separate. For example,
measured wall length/height with inferred thickness produces measured face areas
but inferred volume. Variant evidence is retained without pretending it was a
consumed face-area dimension.

Existing electrical measurement rules and source records remain unchanged.

## Coverage is not completeness

Every canonical entity with no resulting line is named in
`unmeasured_entities`. That includes organizational/connection entities such as
levels, ports and circuits; absence of a line is not a measured zero. An entirely
empty result distinguishes an empty model from a model containing unmeasured
entities. Coverage is enumerated from the actual `BuildingModel` dataclass type;
a new unclassified canonical entity collection raises `QuantityError` instead
of silently disappearing.

`assemblies_unresolved` explicitly says gross surfaces/volumes are not studs,
plates, sheathing, drywall, finishes, grid, hangers, fasteners or a complete bill
of materials. An `assembly_resolver` labels lines but does not supply those
construction rules. #80 and #227 remain open for this fuller goal, supported
opening deductions and separately recorded real raw/helped acceptance. This
change must not be described as a finished building-material takeoff.
