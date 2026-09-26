# IFC / Bonsai adapter

The IFC lane is an adapter around the canonical `oabm.model` v1 contract. It does not define a second building or electrical domain model.

## Scope

`oabm.ifc.to_ifc()` writes IFC4 with metre project units and deterministic IFC `GlobalId` values derived from canonical stable IDs. `oabm.ifc.from_ifc()` reads an OABM-authored IFC back into the v1 canonical model. It intentionally rejects a generic IFC that does not carry OABM round-trip metadata rather than guessing canonical semantics.

The current mapping is:

- model -> `IfcProject`
- level -> `IfcBuildingStorey`
- space -> `IfcSpace`
- wall -> `IfcWall`
- slab -> `IfcSlab`
- ceiling -> `IfcCovering`
- opening -> `IfcOpeningElement` / void relationship
- electrical panel or distribution board (equipment or device) -> `IfcElectricDistributionBoard` with `DISTRIBUTIONBOARD`
- receptacle, convenience/special-purpose outlet or EVSE -> `IfcOutlet` with `POWEROUTLET`
- data outlet -> `IfcOutlet` with `DATAOUTLET`
- CATV/TV outlet -> `IfcOutlet` with `AUDIOVISUALOUTLET`
- telephone outlet -> `IfcOutlet` with `TELEPHONEOUTLET` (the type IFC4 actually defines)
- junction box -> `IfcJunctionBox`, with `POWER` or `DATA` when the canonical type distinguishes them
- luminaire -> `IfcLightFixture`
- exit sign -> `IfcLightFixture` with `SECURITYLIGHTING`
- switch or disconnect -> `IfcSwitchingDevice`
- occupancy sensor -> `IfcSensor` with `MOVEMENTSENSOR` (IFC4 has no `OCCUPANCYSENSOR` member)
- smoke and smoke/CO alarms -> `IfcSensor` with `SMOKESENSOR`; heat detector -> `IfcSensor` with `HEATSENSOR` (IFC4's `IfcAlarmTypeEnum` has no smoke or heat members)
- access control device -> `IfcSensor` with `IDENTIFIERSENSOR`
- speaker -> `IfcAudioVisualAppliance` with `SPEAKER`
- exhaust or ceiling fan -> `IfcFan` (IFC4's fan types describe mechanics, not application, so no `PredefinedType`)
- any other device or equipment type -> `IfcElectricAppliance`

The canonical `device_type` is always kept in the IFC device's `ObjectType`, so no distinction is lost even where IFC4 has only a nearest enum member (equipment types are identified by their IFC class and `PredefinedType` alone). Exported device classes round-trip: `from_ifc` restores the same canonical types.

- canonical port -> `IfcDistributionPort`, nested under its owner
- EMT/PVC conduit route span -> `IfcCableCarrierSegment` with `CONDUITSEGMENT`
- tray route span -> `IfcCableCarrierSegment` with `CABLETRAYSEGMENT`
- trunking/wireway route span -> `IfcCableCarrierSegment` with `CABLETRUNKINGSEGMENT`
- route fitting -> `IfcCableCarrierFitting` (or `IfcCableFitting` for cable routes)
- route -> `IfcDistributionSystem` grouping its segments and fittings
- circuit -> `IfcDistributionCircuit`
- conductor -> `IfcCableSegment`

Route spans and fittings have native `IfcDistributionPort` objects and explicit `IfcRelConnectsPorts` links so they are editable as a connected distribution path in Bonsai. Each route end attaches to an adapter-owned attachment port of its own (stable key `{route_id}#attach:start` or `#attach:end`), nested on the canonical endpoint port's owner at that port's position, and the first or last route span connects to that attachment port. Canonical ports carry only canonical peer connectivity. IFC4 bounds `IfcPort.ConnectedTo` and `IfcPort.ConnectedFrom` at one relationship each, so wiring route ends to the canonical port itself would cap a panel port at two home runs. Every logical connection is exactly one `IfcRelConnectsPorts` with a deterministic `GlobalId`, and every route and circuit `IfcSystem` serves the emitted `IfcBuilding` through `IfcRelServicesBuildings`.

## Identity and lossless round trip

Every canonical entity that becomes an IFC rooted object receives a deterministic `GlobalId` computed from its canonical ID. Renaming or moving an object therefore does not change identity. Import rejects a canonical object whose `GlobalId` no longer matches its canonical ID instead of treating replacement as an edit.

IFC does not natively carry every canonical v1 field, especially provenance, confidence, arbitrary attributes, route fitting order, and source-specific metadata. The custom `OABM_Canonical` property set carries a lossless JSON shadow of the canonical entity for those fields. This is serialization metadata, not an independent domain model: on import, IFC-native editable values such as names, placements, port ownership/connectivity, wall axes, and route segment axes override the shadow before the normal `BuildingModel` validator runs. Native connectivity is authoritative even when the native connection set is empty, so a Bonsai disconnect is preserved. Likewise, deleting every native span for a canonical route is rejected explicitly instead of resurrecting stale route geometry from the shadow. Export also raises `IfcAdapterError` if IfcOpenShell cannot materialize explicit canonical port connectivity rather than silently producing divergent native IFC. Because native IFC ports support a single connected peer, canonical fan-out on one port is rejected explicitly; export also verifies the materialized native connectivity graph exactly matches the canonical graph before continuing.

Generated IFC-only objects such as route span ports, route attachment ports (which also record `CanonicalPortId`) and spatial containers use `OABM_Adapter` metadata so the importer can distinguish adapter structure from canonical entities; `from_ifc` ignores them.

## Bonsai proof

The deterministic fixture `fixtures/model/v1/garage-route.json` is the round-trip proof. Tests materialize it to IFC, write and reopen the STEP file, inspect the expected electrical distribution classes/connectivity, make a Bonsai-equivalent native IFC edit, save/reopen again, and rebuild the canonical model. Stable canonical IDs and unchanged semantics survive; edited IFC-native fields are reflected back in the canonical object.

Bonsai can open the emitted IFC directly. Editing should retain the `OABM_Canonical` property sets and canonical objects' `GlobalId` values. Replacing a canonical object with a newly-created IFC object is intentionally treated as replacement, not an identity-preserving edit.

## API

```python
from oabm.ifc import from_ifc, to_ifc
from oabm.model import BuildingModel

model = BuildingModel.load("fixtures/model/v1/garage-route.json")
to_ifc(model, "garage-route.ifc")
round_tripped = from_ifc("garage-route.ifc")
```

Install IFC support with `pip install -e '.[ifc]'`; the repository `dev` extra also includes IfcOpenShell so CI exercises this lane.
