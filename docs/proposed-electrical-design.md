# Proposed electrical design for architectural-only plans

An architectural set may locate devices without locating the source panel or showing circuiting. The electrical PDF importer records only what is present in the source. A drawing with no panel or circuit evidence keeps zero source-observed panels, circuits, and circuit ports. This design workflow operates on the registered canonical model after import.

## Proposal flow

For a registered architectural model containing devices but no observed panel
or circuits, `propose_architectural_electrical_design(model)` performs the full
proposal without a manual panel hint. It proposes one provisional panel per
populated level and returns the canonical designed model plus placement
proposals. It refuses mixed observed circuiting or an existing panel until
that input is reconciled.

1. `propose_equipment_placement(model, identity_key=..., equipment_type=..., name=..., level_id=..., served_device_ids=...)` scores points on canonical walls in canonical spaces. Electrical/service/utility room semantics dominate; distance to loads is a small preference. Door/opening, hard-obstacle, keep-out, and front working-clearance checks reject known conflicts. The proposal includes the selected candidate, alternatives, reasons, scores, confidence, and stable caller-owned identity. If usable geometry is absent, it raises `PlacementError` instead of inventing a coordinate.
2. `apply_equipment_proposal` creates canonical `ElectricalEquipment` and a source `Port` with `derivation="inferred"`. Its `placement.status` is `inferred`, never source-observed. No circuits are created in this step.
3. `design_proposed_circuits` uses the selected canonical devices and a `CircuitDesignRules` profile. The default preliminary profile groups at most four devices in a deterministic geometric sweep; it assumes 120 V, single-pole, 21.3 mm EMT, two insulated 12 AWG copper conductors and a ground. These values are **design assumptions**, not extracted facts or a code-compliance determination. It creates canonical load ports, circuits, conductors, routes, and fittings. Circuit numbers and membership are marked `designed` with inferred provenance. Quantities derive only from the resulting canonical objects.
4. A Blender/web caller displays the proposal and can call `set_user_equipment_placement` with a registered canonical point, space, wall, direction, and stable user decision ID to accept or move it. The same equipment and circuit IDs remain stable; dependent routes are recalculated, so route and conductor quantities update. The prior proposal remains in equipment placement history. A caller can replace circuit grouping through `design_proposed_circuits(..., user_groups=..., user_input_id=...)`; duplicate or incomplete grouping is rejected.

## Presentation and exports

Show `placement.status` on equipment and `design.status` on circuits. Use canonical provenance `derivation` as the source of truth for whether each object is observed, inferred, or user-directed. Quantity items expose the circuit design origin (`system-designed` or `user-directed`), inferred route geometry, and placement status as separate fields, along with all provenance. This keeps a user's circuit regrouping visible even while the route geometry and panel placement remain inferred. Drawing source references carry provenance derivation. IFC equipment and design objects expose `PlacementStatus` / `DesignStatus` in `OABM_Canonical` alongside the lossless canonical JSON shadow. A proposed design must never be labeled as an existing condition from the source drawings.

Moving the panel changes the geometry of dependent routes and derived lengths. The prototype does not certify service capacity, panel schedules, code limits, or constructability from an architectural plan alone. Those checks require separate validated inputs and design review.
