# Responsibility relay graph

This project is sequenced by artifacts and dependencies, not by time estimates.

```text
                         MODEL CONTRACT v1
                                |
       +------------------------+-------------------------+
       |            |           |           |             |
       v            v           v           v             v
   IFC/Bonsai    3D Router   RoomPlan    PDF Arch     Outputs
       |            |                       |
       |            |                       +------+
       |            |                              |
       |            +------------+             PDF Electrical
       |                         |                  |
       +-------------+-----------+------------------+
                     |
              ENGINE VERTICAL SLICE
             synthetic garage proof
                     |
             +-------+--------+
             |                |
         Quantities       Drawing views
             |                |
             +-------+--------+
                     |
             REAL INPUT END TO END
                     |
                REAL JOB PILOT
```

## Gate A: model contract

Unlocks the independent implementation lanes. It fixes stable IDs, coordinates, units, levels, geometry entities, devices, ports, route representation, provenance, and serialization.

## Parallel lanes after Gate A

- IFC / Bonsai: canonical model <-> IFC, conduit/cable-carrier representation, fittings, ports, connectivity, round trip.
- Deterministic routing: consumes canonical building geometry and endpoints, emits canonical 3D routes and fitting decisions.
- RoomPlan: preserves scan geometry in 3D and emits canonical building objects.
- PDF architecture: extracts scale, registration, walls, openings, spaces, levels, and architecture.
- PDF electrical: extracts panels, devices, symbols, notes, circuits, and mounting metadata. Recognition is parallel; final spatial hosting uses architectural geometry.
- Quantities: develops against synthetic canonical route fixtures, then integrates with real router output.
- Drawings: develops against synthetic canonical model fixtures and only derives views.
- QA / fixtures: continuously owns known-answer fixtures and cross-workstream regression tests.

## Gate B: engine proof

IFC adapter + router converge on the synthetic garage. Acceptance means connected conduit and fittings are editable in Bonsai and round-trip without losing meaning.

## Gate C: real input proof

Gate B plus at least one real input lane, RoomPlan or PDF, yields input -> model -> route -> IFC -> quantities.

## Gate D: PDF semantic model

PDF architecture and PDF electrical converge so electrical objects are spatially hosted on the building model with provenance and confidence.

## Final convergence

Run the pipeline on a private real project outside the public fixture set. Compare it with the existing manual workflow and convert discrepancies into rules and synthetic regression fixtures.
