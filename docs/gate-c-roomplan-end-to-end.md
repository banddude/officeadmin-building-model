# Gate C: RoomPlan real-input end-to-end proof

Issue #13 is the first convergence proof that starts from an input-family document rather than an already canonical building fixture. It reuses the merged RoomPlan, routing, IFC, quantity, drawing, and QA lanes. It does not add a second model or move responsibilities between lanes.

## Public-safe input

The proof uses `fixtures/roomplan/captured-room-3d.json`, the synthetic CapturedRoom-compatible fixture already owned by the RoomPlan lane.

That fixture is public-safe and exercises the actual RoomPlan input shape, including 3D transforms, a raised story, straight and curved walls, openings, a floor/space, and a captured table object. The RoomPlan importer maps the table to a canonical `Obstacle`, exactly as issue #5 defines.

RoomPlan does not contain electrical design intent and the importer must not infer it. The Gate C test therefore adds a tiny, explicit synthetic canonical electrical context after import: one panel, one EVSE, their ports, one circuit, and conductors. Those objects are marked with `synthetic-fixture` provenance and are deliberately kept outside the RoomPlan importer.

## End-to-end proof

`tests/test_gate_c_roomplan_end_to_end.py` proves one coherent flow:

1. Load the CapturedRoom JSON through `oabm.importers.roomplan.load_captured_room()`.
2. Require the imported building geometry, stable canonical identities, RoomPlan provenance, and scan-derived obstacle to be present in the canonical `BuildingModel`.
3. Add explicit public-safe electrical design intent without changing the imported RoomPlan building entities.
4. Run `oabm.routing.route_between_ports()` from the panel to the EVSE and require deterministic route/fitting output.
5. Prove the scan-derived table materially affects routing: with the imported obstacle present, the route rises above it; with only that obstacle removed, the same endpoints route directly.
6. Materialize the routed canonical model through `oabm.ifc.to_ifc()` and require native conduit segments, fittings, and continuous IFC port connectivity.
7. Rebuild the canonical model through `oabm.ifc.from_ifc()` and require an exact canonical round trip, including the RoomPlan source metadata and stable identities.
8. Run `oabm.quantities.extract_quantities()` on that round-tripped model and verify conduit length, fittings, conductor lengths, panel count, and EVSE count from the same canonical route/model.
9. Generate a plan and schedules through `oabm.drawings.generate_drawing_set()` from that same round-tripped model.
10. Require the derived plan to reference the imported RoomPlan walls and obstacle plus the routed panel, EVSE, and route, while the drawing source index preserves RoomPlan and router provenance.
11. Require quantity and drawing serialization to be deterministic.

This is the issue #13 chain:

`RoomPlan JSON -> canonical model -> deterministic route -> IFC -> exact canonical round trip -> quantities -> derived drawing view`

## Boundary

Gate C changes no files under `src/oabm/model/` or `contracts/`.

- RoomPlan still owns only CapturedRoom ingestion.
- Electrical design intent is explicit canonical input, not RoomPlan inference.
- Routing still owns route choice and fittings.
- IFC still owns native materialization, connectivity, and round trip.
- Quantities still derive takeoff values from canonical model/routes.
- Drawings still derive views and schedules from the canonical model and never become a second source of truth.

The proof uses no customer plans, addresses, production exports, OfficeAdmin production data, or private project material.
