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

The automated edit uses the same native IFC entities and placement/axis data that Bonsai edits. For an interactive inspection, generate an IFC with `oabm.ifc.to_ifc()` from the routed Gate B model, open it in Bonsai, and inspect the distribution path:

- the panel and EVSE retain deterministic canonical identity,
- EMT spans are `IfcCableCarrierSegment` objects with `CONDUITSEGMENT`,
- route bends are native `IfcCableCarrierFitting` objects,
- native `IfcDistributionPort` connections form a continuous path,
- saving the edited IFC and reading it with `oabm.ifc.from_ifc()` preserves canonical IDs and connectivity.

Replacing a canonical IFC object with a newly created object is not an identity-preserving edit and remains intentionally rejected by the IFC adapter.

## Boundary

Gate B changes no files under `src/oabm/model/` or `contracts/`. Routing still owns route choice, IFC still owns materialization/round trip, quantities still own takeoff derivation, and QA still owns the synthetic garage fixture and handoff validation.
