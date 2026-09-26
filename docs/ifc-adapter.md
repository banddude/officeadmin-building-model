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

## Axis and Body representations

Architecture entities carry two independent shape representations:

- `Axis` — the canonical centerline or footprint curve. This is the only
  representation `from_ifc` reads back as geometry: a native wall-axis or
  footprint edit in Bonsai flows into the canonical model through
  `_apply_ifc_overrides`.
- `Body` — a swept solid derived from the canonical dimension fields, written
  so viewers (Blender, Bonsai, Revit, Navisworks) see surfaces instead of
  lines or nothing. The Body is a view, never a second source of truth: it is
  authored in canonical metres on the canonical axes from canonical fields
  only, and import ignores it entirely, so `round_trip` stays exact.

Bodies are written only where the canonical fields fully determine them
(`IfcExtrudedAreaSolid` throughout; every product with a Body also carries the
`ObjectPlacement` the IFC4 `PlacementForShapeRepresentation` rule requires):

- wall: per straight centerline segment, the plan rectangle (segment length ×
  `thickness_m`) centered across the centerline and extruded up `height_m`;
- slab and ceiling: the `footprint` polygon extruded down `thickness_m` (the
  footprint plane is the plate's top face);
- space: the `footprint` polygon extruded up `height_m`;
- opening: a void box `size.x` wide, the host wall `thickness_m` deep and
  `size.z` high, centered on the opening pose (authored in the opening's local
  coordinates, since its placement already carries the pose), so
  `IfcRelVoidsElement` actually cuts the host.

An entity missing a dimension its Body needs gets no Body and no default size.
The reason is recorded on its `OABM_Adapter` property set (`Body=no` plus
`BodyReason`, for example a ceiling without `thickness_m` or a space without
`height_m`). Entities with a Body record `Body=yes`.

The per-kind picture, including every recorded reason:

| Canonical kind | `Axis` | `Body` | `BodyReason` when there is no Body |
| --- | --- | --- | --- |
| wall | centerline | rectangle per straight segment, `thickness_m` wide, extruded up `height_m` | `centerline segment is vertical; no plan rectangle to sweep`, or `centerline is not horizontal; the wall base has no single elevation` |
| slab | footprint | footprint extruded down `thickness_m` | `footprint is not planar; the extrusion plane is ambiguous`, or `footprint has no plan area to extrude` |
| ceiling | footprint | footprint extruded down `thickness_m` | `no canonical thickness`, or the slab footprint reasons |
| space | footprint | footprint extruded up `height_m` | `no canonical height`, or the slab footprint reasons |
| opening | — | void box `size.x` × host wall `thickness_m` × `size.z`, centered on the pose, local coordinates | `opening host is not a wall; no canonical wall thickness`, or `opening host not found in the model; no canonical wall thickness` |
| electrical device | — | centered box `size.x` × `size.y` × `size.z`, local coordinates | `no canonical size` |
| electrical equipment | — | centered box `size.x` × `size.y` × `size.z`, local coordinates | `no canonical size` |
| obstacle | — | centered box from its `box3d` geometry, local coordinates; the placement carries the box pose | `obstacle geometry is a polyline3d; no canonical volumetric extent` (likewise `polygon3d`) |
| route span | two-point span | swept disk of `nominal_diameter_m` along the span | — |
| route fitting | — | none by decision — placement-only occurrence | `fitting is a placement-only occurrence on its conduit run` |
| conductor | route centerline | none by decision — wires are drawn by the GLB export, not in IFC | `conductor is represented by its route's conduit solid; see route_ids` |

Device, equipment and obstacle boxes are authored in the product's local
coordinates because the product's `ObjectPlacement` already carries the
canonical pose; a Bonsai move of the product moves its solid with it, and the
moved placement flows back into the canonical pose on import.

## Identity and lossless round trip

Every canonical entity that becomes an IFC rooted object receives a deterministic `GlobalId` computed from its canonical ID. Renaming or moving an object therefore does not change identity. Import rejects a canonical object whose `GlobalId` no longer matches its canonical ID instead of treating replacement as an edit.

IFC does not natively carry every canonical v1 field, especially provenance, confidence, arbitrary attributes, route fitting order, and source-specific metadata. The custom `OABM_Canonical` property set carries a lossless JSON shadow of the canonical entity for those fields. This is serialization metadata, not an independent domain model: on import, IFC-native editable values such as names, placements, port ownership/connectivity, wall axes, and route segment axes override the shadow before the normal `BuildingModel` validator runs. Native connectivity is authoritative even when the native connection set is empty, so a Bonsai disconnect is preserved. Likewise, deleting every native span for a canonical route is rejected explicitly instead of resurrecting stale route geometry from the shadow. Export also raises `IfcAdapterError` if IfcOpenShell cannot materialize explicit canonical port connectivity rather than silently producing divergent native IFC. Because native IFC ports support a single connected peer, canonical fan-out on one port is rejected explicitly; export also verifies the materialized native connectivity graph exactly matches the canonical graph before continuing.

Generated IFC-only objects such as route span ports, route attachment ports (which also record `CanonicalPortId`) and spatial containers use `OABM_Adapter` metadata so the importer can distinguish adapter structure from canonical entities; `from_ifc` ignores them.

## Provenance in IFC

Every exported product that comes from a canonical entity — architecture elements, electrical distribution elements, ports, and the `IfcDistributionSystem`/`IfcDistributionCircuit` of routes and circuits, plus the `IfcProject` model header — also carries a plain custom property set named `OABM_Provenance`. It is an ordinary `IfcPropertySet` of single-value properties, so any IFC viewer (Bonsai, Revit, Navisworks) shows it with no OABM knowledge: a consumer can tell a measured wall from one whose thickness this tool chose without implementing OABM's private format.

| Property | IFC4 type | Value |
| --- | --- | --- |
| `Derivation` | IfcLabel | Entity-level class from the UNSCOPED records (those with `scope_paths` null): `inferred` if any unscoped record is inferred, else `user` if any is user, else `observed` if unscoped records remain, else `unstated` when there are none. A record with an unset derivation states nothing and reads as observed unless an inferred or user record is present. |
| `InferredClaims` | IfcText | Sorted, comma-separated union of `scope_paths` from the scoped inferred records (for example `thickness_m`), or an empty string. |
| `UserClaims` | IfcText | The same for scoped user records. |
| `Confidence` | IfcReal | The entity's canonical `confidence`. |
| `DesignStatus` | IfcLabel | The entity's `attributes["design_status"]` when present (for example `proposed`); the property is omitted otherwise. |
| `Sources` | IfcText | Sorted, unique `source_kind:source_id` pairs of all records, `"; "`-joined, truncated deterministically at 1000 characters with a trailing ` …` so a cut list is visible as cut. |
| `Methods` | IfcText | Sorted, unique `method` values, joined and truncated the same way. |

The scoped claims are the point of the split: a scan-measured wall whose thickness is the importer's default reads `Derivation=observed` with `InferredClaims=thickness_m` instead of collapsing into one class. An entity-level label can never carry that distinction, so the per-claim scope travels beside it.

The property set is legible, not an import channel. `OABM_Canonical.CanonicalJson` remains the lossless shadow, and `from_ifc` reads only that blob and ignores `OABM_Provenance` — the two say the same thing, and the blob stays authoritative. Each property set's `GlobalId` is derived from the entity ID plus `#OABM_Provenance` with the same stable-GUID helper as everything else, so exports are deterministic and the property set identity survives round trips.

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
