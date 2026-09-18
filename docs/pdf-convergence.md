# Architectural + electrical PDF convergence

`oabm.importers.pdf_convergence` is Gate D. It joins the already-canonical
outputs of the architectural PDF and electrical PDF lanes into one canonical
`BuildingModel`.

It does not parse PDFs, classify symbols, infer architectural geometry, or
define another model. Those responsibilities remain in
`pdf_architecture`, `pdf_electrical`, and `oabm.model`.

## Input contract

`converge_pdf_models(architecture, electrical)` accepts the canonical outputs
of the two merged PDF lanes.

Before convergence:

- the electrical lane must have explicit page registration;
- its `CoordinateSystem.frame_id` and registered target frame must equal the
  architectural model frame;
- unregistered single-page or incomplete multi-page electrical geometry is
  rejected instead of being overlaid by coincidence;
- IDs and source recognition from both lanes are treated as authoritative input
  identity and are not regenerated.

The architecture lane remains the source for levels, spaces, walls, slabs,
ceilings, openings, and their geometry. The electrical lane remains the source
for recognized equipment, devices, ports, circuits, and electrical evidence.

## Spatial attachment

For every recognized electrical equipment/device, convergence uses the
registered canonical position to resolve architectural attachment:

1. resolve a level;
2. resolve a containing space on that level when unique;
3. only when the electrical source carries a physical host hint, resolve that
   host against architectural geometry.

A single architectural level is sufficient level evidence. With multiple
levels, registered Z and plan containment are reconciled. Conflicting or
multiple candidates remain ambiguity rather than being selected by ordering.

Space containment uses the canonical space footprint.

A `wall` host hint selects the nearest wall on the resolved level only when it
is within the configured distance and not tied with another plausible wall.
`ceiling` and `floor` hints require unique polygon containment in a canonical
ceiling or slab. Unsupported host families, including a `pole` when the
canonical architecture contains no corresponding host object, remain
unresolved.

No host hint means no invented `host_id`.

## Vertical placement

The electrical recognition lane intentionally leaves a source-page position at
the registered page plane. If it carries one explicit mounting height,
convergence places the object at:

`resolved level elevation + mounting height`.

Ambiguous mounting heights are preserved as ambiguity and are not applied.

A uniquely resolved slab or ceiling can supply its host plane. If that plane
conflicts materially with an explicit mounting height, the physical host remains
unresolved instead of silently overriding either source.

Ports preserve their canonical IDs and local offset from their owners. When an
owner moves vertically during convergence, its ports receive the same
translation so circuit references remain valid.

## Provenance, confidence, and ambiguity

All source provenance from both lanes is retained.

A spatially resolved electrical entity receives an additional
`pdf-convergence` provenance record identifying the architectural
level/space/host used. Its confidence is never increased by convergence: the
result is bounded by the electrical recognition confidence and the confidence
of the architectural entities used for attachment.

Unresolved evidence is stored deterministically under
`BuildingModel.attributes.pdf_convergence.ambiguities` and summarized per
electrical entity. Representative codes include:

- `level_unresolved`, `level_ambiguous`, `level_evidence_conflict`;
- `space_unresolved`, `space_ambiguous`;
- `mounting_height_ambiguous`, `mounting_height_without_level`;
- `host_hint_ambiguous`, `wall_host_unresolved`, `wall_host_ambiguous`;
- `ceiling_host_unresolved` / `ceiling_host_ambiguous`;
- `floor_host_unresolved` / `floor_host_ambiguous`;
- `host_vertical_conflict` and `unsupported_host_hint`.

The rule is conservative: ambiguity changes metadata, not canonical identity.

## Determinism and public proof

`fixtures/pdf_convergence/v1/simple-garage-convergence.json` reuses the
existing public-safe synthetic architecture and electrical fixtures and adds
only their shared-frame registration plus known spatial answers.

Regression tests prove:

- registered electrical objects resolve to the canonical level/space and a
  wall-mounted EVSE resolves to the existing canonical wall;
- original electrical entity/port/circuit IDs and references survive;
- source provenance and confidence are preserved/conservatively combined;
- input collection ordering does not affect canonical JSON;
- unregistered electrical geometry is rejected;
- equally plausible wall hosts remain explicit ambiguity.
