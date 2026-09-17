# Architecture

## Principle

OfficeAdmin needs an application-neutral semantic building and electrical model. Blender/Bonsai is a deep editor and IFC is the primary interoperability representation, but neither Blender scene state nor a flattened 2D floor plan is the canonical operational model.

## Layers

1. Canonical model: levels, spaces, surfaces, openings, devices, ports, routes, circuits, provenance.
2. Importers: RoomPlan, PDF architecture, PDF electrical, IFC/Bonsai, manual authoring.
3. Routing: deterministic 3D pathfinding and fitting decisions.
4. Interop: IFC serialization, deserialization, connectivity, quantities, round trip.
5. Derived products: takeoff, estimating inputs, plans, elevations, sections, schedules.
6. OfficeAdmin adapter: future thin integration boundary back into the main app.

## First proof

Synthetic garage -> canonical model -> deterministic route -> IFC conduit/fittings/ports -> edit in Bonsai -> read back -> quantities.
