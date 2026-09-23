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
- electrical panel or distribution board -> `IfcElectricDistributionBoard`
- EVSE and otherwise-unclassified electrical endpoint equipment -> `IfcElectricAppliance`
- receptacle -> `IfcOutlet`
- junction box -> `IfcJunctionBox`
- luminaire -> `IfcLightFixture`
- switch -> `IfcSwitchingDevice`
- canonical port -> `IfcDistributionPort`, nested under its owner
- EMT/PVC conduit route span -> `IfcCableCarrierSegment` with `CONDUITSEGMENT`
- tray route span -> `IfcCableCarrierSegment` with `CABLETRAYSEGMENT`
- trunking/wireway route span -> `IfcCableCarrierSegment` with `CABLETRUNKINGSEGMENT`
- route fitting -> `IfcCableCarrierFitting` (or `IfcCableFitting` for cable routes)
- route -> `IfcDistributionSystem` grouping its segments and fittings
- circuit -> `IfcDistributionCircuit`
- conductor -> `IfcCableSegment`

Route spans and fittings have native `IfcDistributionPort` objects and explicit `IfcRelConnectsPorts` links so they are editable as a connected distribution path in Bonsai. Canonical endpoint ports are connected to the first and last route spans.

## Identity and lossless round trip

Every canonical entity that becomes an IFC rooted object receives a deterministic `GlobalId` computed from its canonical ID. Renaming or moving an object therefore does not change identity. Import rejects a canonical object whose `GlobalId` no longer matches its canonical ID instead of treating replacement as an edit.

IFC does not natively carry every canonical v1 field, especially provenance, confidence, arbitrary attributes, route fitting order, and source-specific metadata. The custom `OABM_Canonical` property set carries a lossless JSON shadow of the canonical entity for those fields. This is serialization metadata, not an independent domain model: on import, IFC-native editable values such as names, placements, port ownership/connectivity, wall axes, and route segment axes override the shadow before the normal `BuildingModel` validator runs. Native connectivity is authoritative even when the native connection set is empty, so a Bonsai disconnect is preserved. Likewise, deleting every native span for a canonical route is rejected explicitly instead of resurrecting stale route geometry from the shadow. Export also raises `IfcAdapterError` if IfcOpenShell cannot materialize explicit canonical port connectivity rather than silently producing divergent native IFC. Because native IFC ports support a single connected peer, canonical fan-out on one port is rejected explicitly; export also verifies the materialized native connectivity graph exactly matches the canonical graph before continuing.

Generated IFC-only objects such as route span ports and spatial containers use `OABM_Adapter` metadata so the importer can distinguish adapter structure from canonical entities.

## Two properties, and which tolerance applies to each

These are separate claims and conflating them hid a real defect.

**Canonical serialization is deterministic, and exact.** The same model serializes to the same
bytes every time. This is a property of the model contract, it is what the golden known-answer
hashes pin, and no tolerance applies to it. `BuildingModel.from_dict(m.to_dict()).to_dict() ==
m.to_dict()` holds byte for byte.

**An IFC round trip preserves semantics and geometry, within a stated tolerance.** Ids, types,
relationships, references and every non-numeric field come back *identical*. Coordinates and
orientations come back within `oabm.ifc.GEOMETRIC_TOLERANCE`, currently `1e-9`.

The tolerance exists for one specific reason. A quaternion is written to IFC as an axis plus a
reference direction and rebuilt from them, and that trigonometry does not always land on the
identical double. Committed fixtures did not show this because their rotations are axis-aligned:
the components are zeros and ones and survive exactly. A real captured orientation is arbitrary
and differs in the last bits, so asserting byte equality on the round trip made the property
true only for inputs chosen to satisfy it.

`1e-9` is deliberately tiny — a nanometre in metres, and far below any angle a survey or a
drawing can express. It absorbs the last bits of a double; it does not excuse geometric drift.
Structure is still compared exactly: `oabm.ifc.geometry_matches()` requires identical keys,
identical list lengths and identical non-numeric values, and allows only leaf floats to differ.

`test_an_arbitrary_rotation_survives_the_round_trip_but_not_bit_for_bit` asserts both halves,
including that byte equality *fails* for a non-axis-aligned rotation. If that assertion ever
starts passing, the fixture has stopped guarding against the self-selecting case.

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
