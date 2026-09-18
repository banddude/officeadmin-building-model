# Gate B: synthetic garage engine vertical slice

Issue #11 is the convergence proof for the already-merged QA, deterministic routing, IFC/Bonsai, and quantity lanes. It does not introduce a new model or duplicate any lane algorithm.

## Source fixture

The input is the QA-owned `fixtures/golden/v1/synthetic-garage.json`.

That golden document intentionally contains a hand-authored route so QA can exercise route/fitting invariants in isolation. Gate B loads the same fixture, removes that expected route/fittings, clears the circuit/conductor route references, and uses the remaining canonical room, panel, EVSE, ports, obstacle, route constraint, circuit, and conductor semantics as the engine input.

This distinction matters: the route that passes Gate B must come from `oabm.routing.route_between_ports()`, not from the fixture.

## Vertical slice

`tests/test_gate_b_synthetic_garage.py` proves one coherent flow:

1. Load the public-safe synthetic garage through `oabm.qa`.
2. Remove the fixture's hand-authored downstream route artifacts.
3. Run the real deterministic router from the panel load port to the EVSE feed port.
4. Attach the emitted canonical `Route` and ordered `RouteFitting` objects and point the existing circuit/conductors at that route.
5. Validate the canonical handoff and deterministic repeatability.
6. Materialize that exact routed model through `oabm.ifc.to_ifc()`.
7. Verify native IFC conduit segments, fittings, distribution ports, and a connected port graph from the panel endpoint to the EVSE endpoint.
8. Rebuild the canonical model through `oabm.ifc.from_ifc()` and require an exact round trip.
9. Run `oabm.quantities.extract_quantities()` on the round-tripped model and verify conduit length, fittings, conductor lengths, EVSE count, and panel count derive from the same canonical route/model.
10. Save and reopen the IFC, perform a Bonsai-equivalent native panel rename and EVSE/route-end geometry edit, save again, and require stable canonical IDs, route/circuit/conductor semantics, and IFC connectivity to survive.

## Bonsai acceptance

### Verified Bonsai open/edit/save/reopen proof

Acceptance was run on 2026-09-18 using **Blender 5.2.1 LTS** with the installed **Bonsai 0.8.5** extension.

The routed public-safe synthetic garage IFC was opened through Bonsai's `bim.load_project` operator. Inside the active Bonsai IFC session:

- the canonical panel was renamed through Bonsai's IFC API layer,
- the EVSE was moved +0.10 m in Z in Blender and written back with Bonsai's `bim.edit_object_placement` operator,
- the final conduit segment axis endpoint was moved +0.10 m in Z in the active Bonsai IFC model,
- the project was saved through Bonsai's `bim.save_project` operator.

The saved IFC was reopened with `oabm.ifc.from_ifc()`. Acceptance verified:

- the full set of **23 canonical IDs** was unchanged,
- panel identity survived and the edited name round-tripped,
- EVSE canonical pose Z changed from 1.20 m to 1.30 m,
- the canonical route retained its stable ID and its endpoint Z changed to 1.30 m,
- the circuit still referenced the same canonical route,
- all three conductors still referenced that route,
- native IFC port connectivity remained continuous from the panel endpoint to the EVSE endpoint,
- canonical lane validation passed after the Bonsai save/reopen.

This is the required Gate B proof that the emitted connected conduit/fittings are editable through Bonsai itself and can be saved and round-tripped without losing canonical identity, route semantics, or connectivity.


The automated edit uses the same native IFC entities and placement/axis data that Bonsai edits. For an interactive inspection, generate an IFC with `oabm.ifc.to_ifc()` from the routed Gate B model, open it in Bonsai, and inspect the distribution path:

- the panel and EVSE retain deterministic canonical identity,
- EMT spans are `IfcCableCarrierSegment` objects with `CONDUITSEGMENT`,
- route bends are native `IfcCableCarrierFitting` objects,
- native `IfcDistributionPort` connections form a continuous path,
- saving the edited IFC and reading it with `oabm.ifc.from_ifc()` preserves canonical IDs and connectivity.

Replacing a canonical IFC object with a newly created object is not an identity-preserving edit and remains intentionally rejected by the IFC adapter.

## Boundary

Gate B changes no files under `src/oabm/model/` or `contracts/`. Routing still owns route choice, IFC still owns materialization/round trip, quantities still own takeoff derivation, and QA still owns the synthetic garage fixture and handoff validation.
