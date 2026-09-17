from __future__ import annotations

import json
import math
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping

import ifcopenshell
import ifcopenshell.api.aggregate
import ifcopenshell.api.context
import ifcopenshell.api.feature
import ifcopenshell.api.geometry
import ifcopenshell.api.project
import ifcopenshell.api.pset
import ifcopenshell.api.root
import ifcopenshell.api.spatial
import ifcopenshell.api.system
import ifcopenshell.api.unit
import ifcopenshell.guid
import ifcopenshell.util.placement
import numpy as np

from oabm.model import (
    SCHEMA_VERSION,
    BuildingModel,
    Point3,
    Polyline3D,
    Pose,
    Quaternion,
    Vector3,
)

IFC_SCHEMA = "IFC4"
CANONICAL_PSET = "OABM_Canonical"
ADAPTER_PSET = "OABM_Adapter"
_IFC_NAMESPACE = uuid.UUID("0c569baf-3d91-5d5e-a755-9eb9435c67ae")
_EPS = 1e-7

_COLLECTION_BY_KIND = {
    "level": "levels",
    "space": "spaces",
    "wall": "walls",
    "slab": "slabs",
    "ceiling": "ceilings",
    "opening": "openings",
    "electrical_equipment": "electrical_equipment",
    "electrical_device": "electrical_devices",
    "port": "ports",
    "obstacle": "obstacles",
    "route_constraint": "route_constraints",
    "route": "routes",
    "route_fitting": "route_fittings",
    "circuit": "circuits",
    "conductor": "conductors",
}


class IfcAdapterError(ValueError):
    """Raised when IFC cannot be mapped to the canonical OABM contract safely."""


def canonical_id_to_ifc_guid(canonical_id: str) -> str:
    """Return a deterministic IFC GlobalId for a canonical or adapter-stable key."""

    if not isinstance(canonical_id, str) or not canonical_id:
        raise IfcAdapterError("canonical_id must be a non-empty string")
    value = uuid.uuid5(_IFC_NAMESPACE, canonical_id)
    return ifcopenshell.guid.compress(value.hex)


def to_ifc(model: BuildingModel, destination: str | Path | None = None) -> ifcopenshell.file:
    """Materialize a canonical model as IFC4 suitable for Bonsai editing.

    Canonical entities retain deterministic IFC GlobalIds. IFC-native geometry,
    ports, systems, and connectivity are authored where IFC has an equivalent.
    The ``OABM_Canonical`` property set stores only the lossless contract shadow
    needed for fields IFC does not carry directly (for example provenance).
    """

    ifc = ifcopenshell.api.project.create_file(version=IFC_SCHEMA)
    project = _create_root(ifc, "IfcProject", model.model_id, model.name)
    metre = ifcopenshell.api.unit.add_si_unit(ifc, unit_type="LENGTHUNIT")
    ifcopenshell.api.unit.assign_unit(ifc, units=[metre])
    model_context = ifcopenshell.api.context.add_context(ifc, context_type="Model")
    axis_context = ifcopenshell.api.context.add_context(
        ifc,
        context_type="Model",
        context_identifier="Axis",
        target_view="GRAPH_VIEW",
        parent=model_context,
    )
    body_context = ifcopenshell.api.context.add_context(
        ifc,
        context_type="Model",
        context_identifier="Body",
        target_view="MODEL_VIEW",
        parent=model_context,
    )

    header = {
        "model_id": model.model_id,
        "name": model.name,
        "schema_version": model.schema_version,
        "coordinate_system": model.to_dict()["coordinate_system"],
        "provenance": model.to_dict()["provenance"],
        "attributes": model.to_dict()["attributes"],
    }
    _add_canonical_pset(ifc, project, "model", 0, header)

    site = _create_root(
        ifc,
        "IfcSite",
        f"{model.model_id}#ifc-site",
        "OABM Site",
    )
    building = _create_root(
        ifc,
        "IfcBuilding",
        f"{model.model_id}#ifc-building",
        model.name or "OABM Building",
    )
    _mark_adapter(ifc, site, Role="spatial-container", ModelId=model.model_id)
    _mark_adapter(ifc, building, Role="spatial-container", ModelId=model.model_id)
    ifcopenshell.api.aggregate.assign_object(ifc, relating_object=project, products=[site])
    ifcopenshell.api.aggregate.assign_object(ifc, relating_object=site, products=[building])

    entity_ifc: dict[str, Any] = {}
    storeys: dict[str, Any] = {}
    spaces: dict[str, Any] = {}

    model_document = model.to_dict()

    for ordinal, level in enumerate(model.levels):
        item = _create_root(ifc, "IfcBuildingStorey", level.id, level.name)
        item.Elevation = float(level.elevation_m)
        _set_pose(ifc, item, Pose(position=Point3(x=0, y=0, z=level.elevation_m)))
        _add_canonical_pset(ifc, item, "level", ordinal, model_document["levels"][ordinal])
        ifcopenshell.api.aggregate.assign_object(ifc, relating_object=building, products=[item])
        entity_ifc[level.id] = item
        storeys[level.id] = item

    for ordinal, space in enumerate(model.spaces):
        item = _create_root(ifc, "IfcSpace", space.id, space.name)
        _add_canonical_pset(ifc, item, "space", ordinal, model_document["spaces"][ordinal])
        _set_pose(ifc, item, Pose(position=Point3(x=0, y=0, z=0)))
        _assign_polyline_representation(
            ifc,
            item,
            axis_context,
            (*space.footprint.points, space.footprint.points[0]),
            identifier="Axis",
        )
        ifcopenshell.api.aggregate.assign_object(
            ifc, relating_object=storeys[space.level_id], products=[item]
        )
        entity_ifc[space.id] = item
        spaces[space.id] = item

    def add_product(
        entity: Any,
        kind: str,
        ordinal: int,
        ifc_class: str,
        *,
        predefined_type: str | None = None,
        pose: Pose | None = None,
        assign_container: bool = True,
    ) -> Any:
        item = _create_root(
            ifc,
            ifc_class,
            entity.id,
            entity.name,
            predefined_type=predefined_type,
        )
        entity_ifc[entity.id] = item
        _add_canonical_pset(
            ifc,
            item,
            kind,
            ordinal,
            model_document[_COLLECTION_BY_KIND[kind]][ordinal],
        )
        if pose is not None:
            _set_pose(ifc, item, pose)
        if assign_container:
            _assign_spatial_container(ifc, entity, item, storeys, spaces)
        return item

    for ordinal, wall in enumerate(model.walls):
        item = add_product(wall, "wall", ordinal, "IfcWall")
        _assign_polyline_representation(ifc, item, axis_context, wall.centerline.points)

    for ordinal, slab in enumerate(model.slabs):
        item = add_product(slab, "slab", ordinal, "IfcSlab", predefined_type="FLOOR")
        _assign_polyline_representation(
            ifc,
            item,
            axis_context,
            (*slab.footprint.points, slab.footprint.points[0]),
        )

    for ordinal, ceiling in enumerate(model.ceilings):
        item = add_product(
            ceiling,
            "ceiling",
            ordinal,
            "IfcCovering",
            predefined_type="CEILING",
        )
        _assign_polyline_representation(
            ifc,
            item,
            axis_context,
            (*ceiling.footprint.points, ceiling.footprint.points[0]),
        )

    for ordinal, opening in enumerate(model.openings):
        item = add_product(
            opening,
            "opening",
            ordinal,
            "IfcOpeningElement",
            pose=opening.pose,
            assign_container=False,
        )
        host = entity_ifc.get(opening.host_id)
        if host is not None:
            ifcopenshell.api.feature.add_feature(ifc, feature=item, element=host)

    for ordinal, obstacle in enumerate(model.obstacles):
        add_product(obstacle, "obstacle", ordinal, "IfcBuildingElementProxy")

    for ordinal, constraint in enumerate(model.route_constraints):
        add_product(constraint, "route_constraint", ordinal, "IfcAnnotation")

    for ordinal, equipment in enumerate(model.electrical_equipment):
        ifc_class, predefined = _equipment_ifc_type(equipment.equipment_type)
        add_product(
            equipment,
            "electrical_equipment",
            ordinal,
            ifc_class,
            predefined_type=predefined,
            pose=equipment.pose,
        )

    for ordinal, device in enumerate(model.electrical_devices):
        ifc_class, predefined = _device_ifc_type(device.device_type)
        item = add_product(
            device,
            "electrical_device",
            ordinal,
            ifc_class,
            predefined_type=predefined,
            pose=device.pose,
        )
        if hasattr(item, "ObjectType"):
            item.ObjectType = device.device_type

    canonical_ports: dict[str, Any] = {}
    for ordinal, port in enumerate(model.ports):
        owner = entity_ifc.get(port.owner_id)
        if owner is None or not owner.is_a("IfcDistributionElement"):
            raise IfcAdapterError(
                f"port {port.id!r} owner {port.owner_id!r} is not an IFC distribution element"
            )
        item = ifcopenshell.api.system.add_port(ifc, element=owner)
        item.GlobalId = canonical_id_to_ifc_guid(port.id)
        item.Name = port.name
        item.FlowDirection = _flow_direction(port.role)
        try:
            item.PredefinedType = "CABLE"
            item.SystemType = "ELECTRICAL"
        except (AttributeError, TypeError, ValueError):
            pass
        _set_pose(ifc, item, port.pose)
        _add_canonical_pset(ifc, item, "port", ordinal, model_document["ports"][ordinal])
        entity_ifc[port.id] = item
        canonical_ports[port.id] = item

    # Explicit canonical port connectivity is represented natively where IFC can
    # express it. The canonical sidecar remains the lossless fallback if a source
    # model uses a fan-out that IFC ports cannot represent one-to-many.
    linked: set[tuple[str, str]] = set()
    for port in model.ports:
        for other_id in port.connected_port_ids:
            pair = tuple(sorted((port.id, other_id)))
            if pair in linked:
                continue
            try:
                ifcopenshell.api.system.connect_port(
                    ifc,
                    port1=canonical_ports[port.id],
                    port2=canonical_ports[other_id],
                    direction="NOTDEFINED",
                )
            except Exception:
                # Preserve exact canonical connectivity in OABM_Canonical when IFC
                # cardinality prevents another native connection on the same port.
                pass
            linked.add(pair)

    fitting_ifc: dict[str, Any] = {}
    fitting_ports: dict[str, tuple[Any, Any]] = {}
    for ordinal, fitting in enumerate(model.route_fittings):
        route = next((r for r in model.routes if r.id == fitting.route_id), None)
        route_type = route.route_type if route is not None else "emt"
        ifc_class = "IfcCableFitting" if route_type == "cable" else "IfcCableCarrierFitting"
        item = _create_root(
            ifc,
            ifc_class,
            fitting.id,
            fitting.name,
            predefined_type=_fitting_predefined_type(fitting.fitting_type),
        )
        _set_pose(ifc, item, fitting.pose)
        _add_canonical_pset(
            ifc,
            item,
            "route_fitting",
            ordinal,
            model_document["route_fittings"][ordinal],
        )
        fitting_ifc[fitting.id] = item
        entity_ifc[fitting.id] = item
        p0 = _add_adapter_port(
            ifc,
            item,
            f"{fitting.id}#port:0",
            fitting.pose.position,
            Vector3(x=1, y=0, z=0),
            route_id=fitting.route_id,
            role="fitting-port",
        )
        p1 = _add_adapter_port(
            ifc,
            item,
            f"{fitting.id}#port:1",
            fitting.pose.position,
            Vector3(x=-1, y=0, z=0),
            route_id=fitting.route_id,
            role="fitting-port",
        )
        fitting_ports[fitting.id] = (p0, p1)
        _assign_spatial_container(ifc, fitting, item, storeys, spaces, model=model)

    for ordinal, route in enumerate(model.routes):
        route_system = ifcopenshell.api.system.add_system(ifc, ifc_class="IfcDistributionSystem")
        route_system.GlobalId = canonical_id_to_ifc_guid(route.id)
        route_system.Name = route.name
        try:
            route_system.PredefinedType = "ELECTRICAL"
        except (AttributeError, TypeError, ValueError):
            pass
        _add_canonical_pset(ifc, route_system, "route", ordinal, model_document["routes"][ordinal])
        entity_ifc[route.id] = route_system

        segments: list[Any] = []
        segment_ports: list[tuple[Any, Any]] = []
        points = route.centerline.points
        for index, (start, end) in enumerate(zip(points, points[1:])):
            segment_class, predefined = _segment_ifc_type(route.route_type)
            segment_key = f"{route.id}#segment:{index}"
            segment = _create_root(
                ifc,
                segment_class,
                segment_key,
                f"{route.id} segment {index + 1}",
                predefined_type=predefined,
            )
            _mark_adapter(
                ifc,
                segment,
                Role="route-segment",
                RouteId=route.id,
                SegmentIndex=index,
            )
            _set_pose(ifc, segment, Pose(position=Point3(x=0, y=0, z=0)))
            _assign_polyline_representation(ifc, segment, axis_context, (start, end))
            _assign_round_body(ifc, segment, body_context, start, end, route.nominal_diameter_m)
            direction = _direction(start, end)
            p_start = _add_adapter_port(
                ifc,
                segment,
                f"{segment_key}#port:start",
                start,
                _neg(direction),
                route_id=route.id,
                role="segment-start",
            )
            p_end = _add_adapter_port(
                ifc,
                segment,
                f"{segment_key}#port:end",
                end,
                direction,
                route_id=route.id,
                role="segment-end",
            )
            segments.append(segment)
            segment_ports.append((p_start, p_end))

        route_fittings = [
            next(f for f in model.route_fittings if f.id == fitting_id)
            for fitting_id in route.fitting_ids
        ]
        route_products = [*segments, *(fitting_ifc[f.id] for f in route_fittings)]
        if route_products:
            ifcopenshell.api.system.assign_system(
                ifc, products=route_products, system=route_system
            )

        if segment_ports:
            _connect(ifc, canonical_ports[route.start_port_id], segment_ports[0][0])
            _connect(ifc, segment_ports[-1][1], canonical_ports[route.end_port_id])
            boundary_fittings = _fittings_by_boundary(points, route_fittings)
            for boundary in range(len(segment_ports) - 1):
                left = segment_ports[boundary][1]
                right = segment_ports[boundary + 1][0]
                chain = boundary_fittings.get(boundary + 1, [])
                previous = left
                for fitting in chain:
                    ports = fitting_ports[fitting.id]
                    _connect(ifc, previous, ports[0])
                    previous = ports[1]
                _connect(ifc, previous, right)

        level_id = _route_level_id(model, route)
        if level_id and level_id in storeys:
            for product in route_products:
                if not _has_spatial_container(product):
                    ifcopenshell.api.spatial.assign_container(
                        ifc, relating_structure=storeys[level_id], products=[product]
                    )

    circuit_ifc: dict[str, Any] = {}
    for ordinal, circuit in enumerate(model.circuits):
        item = ifcopenshell.api.system.add_system(ifc, ifc_class="IfcDistributionCircuit")
        item.GlobalId = canonical_id_to_ifc_guid(circuit.id)
        item.Name = circuit.name
        try:
            item.PredefinedType = "ELECTRICAL"
        except (AttributeError, TypeError, ValueError):
            pass
        _add_canonical_pset(ifc, item, "circuit", ordinal, model_document["circuits"][ordinal])
        circuit_ifc[circuit.id] = item
        entity_ifc[circuit.id] = item

        members: list[Any] = []
        source_owner = _owner_ifc_for_port(model, circuit.source_port_id, entity_ifc)
        if source_owner is not None:
            members.append(source_owner)
        for port_id in circuit.load_port_ids:
            owner = _owner_ifc_for_port(model, port_id, entity_ifc)
            if owner is not None and owner not in members:
                members.append(owner)
        for route_id in circuit.route_ids:
            route_system = entity_ifc.get(route_id)
            if route_system is not None:
                for rel in getattr(route_system, "IsGroupedBy", ()) or ():
                    for product in rel.RelatedObjects:
                        if product not in members:
                            members.append(product)
        if members:
            ifcopenshell.api.system.assign_system(ifc, products=members, system=item)

    for ordinal, conductor in enumerate(model.conductors):
        item = _create_root(
            ifc,
            "IfcCableSegment",
            conductor.id,
            conductor.name,
            predefined_type="CABLESEGMENT",
        )
        _add_canonical_pset(
            ifc,
            item,
            "conductor",
            ordinal,
            model_document["conductors"][ordinal],
        )
        entity_ifc[conductor.id] = item
        if conductor.route_ids:
            route = next((r for r in model.routes if r.id == conductor.route_ids[0]), None)
            if route is not None:
                _assign_polyline_representation(ifc, item, axis_context, route.centerline.points)
                level_id = _route_level_id(model, route)
                if level_id and level_id in storeys:
                    ifcopenshell.api.spatial.assign_container(
                        ifc, relating_structure=storeys[level_id], products=[item]
                    )
        circuit = circuit_ifc.get(conductor.circuit_id)
        if circuit is not None:
            ifcopenshell.api.system.assign_system(ifc, products=[item], system=circuit)

    if destination is not None:
        ifc.write(str(Path(destination)))
    return ifc


def from_ifc(source: str | Path | ifcopenshell.file) -> BuildingModel:
    """Read an OABM-authored IFC4 file back into the canonical v1 model.

    The function is intentionally strict: a generic third-party IFC without the
    OABM round-trip metadata is not guessed into canonical semantics. Bonsai may
    freely edit and save an OABM IFC as long as canonical entities and their
    stable GlobalIds / metadata are retained.
    """

    ifc = ifcopenshell.open(str(source)) if isinstance(source, (str, Path)) else source
    projects = ifc.by_type("IfcProject")
    if len(projects) != 1:
        raise IfcAdapterError(f"expected exactly one IfcProject, found {len(projects)}")
    project = projects[0]
    model_meta = _canonical_metadata(project)
    if model_meta is None or model_meta["kind"] != "model":
        raise IfcAdapterError("IFC is missing OABM_Canonical model metadata")
    header = model_meta["json"]
    if header.get("schema_version") != SCHEMA_VERSION:
        raise IfcAdapterError(
            f"unsupported OABM schema version {header.get('schema_version')!r}"
        )
    expected_project_guid = canonical_id_to_ifc_guid(header["model_id"])
    if project.GlobalId != expected_project_guid:
        raise IfcAdapterError(
            f"project GlobalId no longer matches stable model identity {header['model_id']!r}"
        )

    document: dict[str, Any] = {
        "model_id": header["model_id"],
        "name": project.Name,
        "schema_version": header["schema_version"],
        "coordinate_system": header["coordinate_system"],
        "levels": [],
        "spaces": [],
        "walls": [],
        "slabs": [],
        "ceilings": [],
        "openings": [],
        "electrical_equipment": [],
        "electrical_devices": [],
        "ports": [],
        "obstacles": [],
        "route_constraints": [],
        "routes": [],
        "route_fittings": [],
        "circuits": [],
        "conductors": [],
        "provenance": header.get("provenance", []),
        "attributes": header.get("attributes", {}),
    }

    canonical_items: dict[str, tuple[Any, dict[str, Any], int, str]] = {}
    for item in ifc.by_type("IfcRoot"):
        if item == project:
            continue
        meta = _canonical_metadata(item)
        if meta is None or meta["kind"] == "model":
            continue
        kind = meta["kind"]
        if kind not in _COLLECTION_BY_KIND:
            raise IfcAdapterError(f"unknown OABM canonical kind {kind!r}")
        data = dict(meta["json"])
        canonical_id = data.get("id")
        if not canonical_id:
            raise IfcAdapterError(f"{item.is_a()} {item.GlobalId} has no canonical id")
        expected_guid = canonical_id_to_ifc_guid(canonical_id)
        if item.GlobalId != expected_guid:
            raise IfcAdapterError(
                f"stable identity changed for {canonical_id!r}: {item.GlobalId!r} != {expected_guid!r}"
            )
        if canonical_id in canonical_items:
            raise IfcAdapterError(f"duplicate canonical IFC entity {canonical_id!r}")
        canonical_items[canonical_id] = (item, data, meta["ordinal"], kind)

    route_segments = _route_segments(ifc)
    native_connections = _native_canonical_port_connections(ifc, canonical_items)

    buckets: dict[str, list[tuple[int, dict[str, Any]]]] = {
        name: [] for name in _COLLECTION_BY_KIND.values()
    }
    for canonical_id, (item, data, ordinal, kind) in canonical_items.items():
        _apply_ifc_overrides(
            ifc,
            item,
            data,
            kind,
            route_segments=route_segments,
            native_connections=native_connections,
            canonical_items=canonical_items,
        )
        buckets[_COLLECTION_BY_KIND[kind]].append((ordinal, data))

    for collection, items in buckets.items():
        items.sort(key=lambda value: value[0])
        document[collection] = [data for _, data in items]

    try:
        return BuildingModel.from_dict(document)
    except Exception as exc:
        raise IfcAdapterError(f"IFC edit no longer forms a valid canonical model: {exc}") from exc


def round_trip(model: BuildingModel) -> BuildingModel:
    """Convenience in-memory canonical -> IFC -> canonical round trip."""

    return from_ifc(to_ifc(model))


def _create_root(
    ifc: ifcopenshell.file,
    ifc_class: str,
    stable_key: str,
    name: str | None,
    *,
    predefined_type: str | None = None,
) -> Any:
    kwargs: dict[str, Any] = {"ifc_class": ifc_class, "name": name}
    if predefined_type is not None:
        kwargs["predefined_type"] = predefined_type
    try:
        item = ifcopenshell.api.root.create_entity(ifc, **kwargs)
    except (RuntimeError, TypeError, ValueError):
        kwargs.pop("predefined_type", None)
        item = ifcopenshell.api.root.create_entity(ifc, **kwargs)
    item.GlobalId = canonical_id_to_ifc_guid(stable_key)
    return item


def _add_canonical_pset(
    ifc: ifcopenshell.file,
    product: Any,
    kind: str,
    ordinal: int,
    payload: Mapping[str, Any],
) -> None:
    pset = ifcopenshell.api.pset.add_pset(ifc, product=product, name=CANONICAL_PSET)
    ifcopenshell.api.pset.edit_pset(
        ifc,
        pset=pset,
        properties={
            "SchemaVersion": SCHEMA_VERSION,
            "CanonicalKind": kind,
            "CanonicalOrdinal": int(ordinal),
            "CanonicalJson": ifc.createIfcText(
                json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
            ),
        },
    )


def _mark_adapter(ifc: ifcopenshell.file, product: Any, **properties: Any) -> None:
    pset = ifcopenshell.api.pset.add_pset(ifc, product=product, name=ADAPTER_PSET)
    ifcopenshell.api.pset.edit_pset(ifc, pset=pset, properties=properties)


def _canonical_metadata(item: Any) -> dict[str, Any] | None:
    props = _property_set(item, CANONICAL_PSET)
    if props is None:
        return None
    try:
        payload = json.loads(str(props["CanonicalJson"]))
        return {
            "kind": str(props["CanonicalKind"]),
            "ordinal": int(props["CanonicalOrdinal"]),
            "json": payload,
        }
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise IfcAdapterError(f"invalid {CANONICAL_PSET} on {item.is_a()} {item.GlobalId}") from exc


def _property_set(item: Any, name: str) -> dict[str, Any] | None:
    for rel in getattr(item, "IsDefinedBy", ()) or ():
        if not rel.is_a("IfcRelDefinesByProperties"):
            continue
        definition = rel.RelatingPropertyDefinition
        if not definition.is_a("IfcPropertySet") or definition.Name != name:
            continue
        values: dict[str, Any] = {}
        for prop in definition.HasProperties:
            if not prop.is_a("IfcPropertySingleValue"):
                continue
            nominal = prop.NominalValue
            if nominal is None:
                values[prop.Name] = None
            else:
                values[prop.Name] = getattr(nominal, "wrappedValue", nominal)
        return values
    return None


def _pose_matrix(pose: Pose) -> np.ndarray:
    q = pose.rotation
    x, y, z, w = float(q.x), float(q.y), float(q.z), float(q.w)
    matrix = np.eye(4, dtype=float)
    matrix[:3, :3] = np.array(
        [
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ],
        dtype=float,
    )
    matrix[:3, 3] = (pose.position.x, pose.position.y, pose.position.z)
    return matrix


def _set_pose(ifc: ifcopenshell.file, product: Any, pose: Pose) -> None:
    ifcopenshell.api.geometry.edit_object_placement(
        ifc, product=product, matrix=_pose_matrix(pose), is_si=True
    )


def _pose_from_product(product: Any) -> dict[str, Any] | None:
    placement = getattr(product, "ObjectPlacement", None)
    if placement is None:
        return None
    matrix = np.asarray(ifcopenshell.util.placement.get_local_placement(placement), dtype=float)
    position = matrix[:3, 3]
    q = _quaternion_from_matrix(matrix[:3, :3])
    return {
        "position": {"x": float(position[0]), "y": float(position[1]), "z": float(position[2])},
        "rotation": {"x": q[0], "y": q[1], "z": q[2], "w": q[3]},
    }


def _quaternion_from_matrix(rotation: np.ndarray) -> tuple[float, float, float, float]:
    m = rotation
    trace = float(m[0, 0] + m[1, 1] + m[2, 2])
    if trace > 0:
        s = math.sqrt(trace + 1.0) * 2
        w = 0.25 * s
        x = (m[2, 1] - m[1, 2]) / s
        y = (m[0, 2] - m[2, 0]) / s
        z = (m[1, 0] - m[0, 1]) / s
    elif m[0, 0] > m[1, 1] and m[0, 0] > m[2, 2]:
        s = math.sqrt(1.0 + m[0, 0] - m[1, 1] - m[2, 2]) * 2
        w = (m[2, 1] - m[1, 2]) / s
        x = 0.25 * s
        y = (m[0, 1] + m[1, 0]) / s
        z = (m[0, 2] + m[2, 0]) / s
    elif m[1, 1] > m[2, 2]:
        s = math.sqrt(1.0 + m[1, 1] - m[0, 0] - m[2, 2]) * 2
        w = (m[0, 2] - m[2, 0]) / s
        x = (m[0, 1] + m[1, 0]) / s
        y = 0.25 * s
        z = (m[1, 2] + m[2, 1]) / s
    else:
        s = math.sqrt(1.0 + m[2, 2] - m[0, 0] - m[1, 1]) * 2
        w = (m[1, 0] - m[0, 1]) / s
        x = (m[0, 2] + m[2, 0]) / s
        y = (m[1, 2] + m[2, 1]) / s
        z = 0.25 * s
    values = np.asarray([x, y, z, w], dtype=float)
    norm = float(np.linalg.norm(values))
    if norm <= _EPS:
        return (0.0, 0.0, 0.0, 1.0)
    values /= norm
    # q and -q are the same rotation. Canonicalize the sign for stable JSON.
    if values[3] < 0:
        values *= -1
    return tuple(float(v) for v in values)  # type: ignore[return-value]


def _assign_polyline_representation(
    ifc: ifcopenshell.file,
    product: Any,
    context: Any,
    points: Iterable[Point3],
    *,
    identifier: str = "Axis",
) -> None:
    points = tuple(points)
    if len(points) < 2:
        return
    cartesian = [
        ifc.create_entity("IfcCartesianPoint", Coordinates=(float(p.x), float(p.y), float(p.z)))
        for p in points
    ]
    polyline = ifc.create_entity("IfcPolyline", Points=cartesian)
    representation = ifc.create_entity(
        "IfcShapeRepresentation",
        ContextOfItems=context,
        RepresentationIdentifier=identifier,
        RepresentationType="Curve3D",
        Items=[polyline],
    )
    ifcopenshell.api.geometry.assign_representation(
        ifc, product=product, representation=representation
    )


def _assign_round_body(
    ifc: ifcopenshell.file,
    product: Any,
    context: Any,
    start: Point3,
    end: Point3,
    nominal_diameter_m: float | None,
) -> None:
    if nominal_diameter_m is None or nominal_diameter_m <= 0:
        return
    cartesian = [
        ifc.create_entity("IfcCartesianPoint", Coordinates=(float(p.x), float(p.y), float(p.z)))
        for p in (start, end)
    ]
    directrix = ifc.create_entity("IfcPolyline", Points=cartesian)
    solid = ifc.create_entity(
        "IfcSweptDiskSolid",
        Directrix=directrix,
        Radius=float(nominal_diameter_m) / 2.0,
        InnerRadius=None,
        StartParam=None,
        EndParam=None,
    )
    representation = ifc.create_entity(
        "IfcShapeRepresentation",
        ContextOfItems=context,
        RepresentationIdentifier="Body",
        RepresentationType="AdvancedSweptSolid",
        Items=[solid],
    )
    ifcopenshell.api.geometry.assign_representation(
        ifc, product=product, representation=representation
    )


def _equipment_ifc_type(token: str) -> tuple[str, str | None]:
    token = token.lower()
    if token in {"panel", "panelboard", "distribution-board", "distribution_board"}:
        return "IfcElectricDistributionBoard", "DISTRIBUTIONBOARD"
    if token in {"switchboard", "switchgear"}:
        return "IfcElectricDistributionBoard", "SWITCHBOARD"
    if token == "transformer":
        return "IfcTransformer", None
    return "IfcElectricAppliance", None


def _device_ifc_type(token: str) -> tuple[str, str | None]:
    token = token.lower()
    if token in {"receptacle", "outlet", "power-outlet", "power_outlet"}:
        return "IfcOutlet", "POWEROUTLET"
    if token in {"junction-box", "junction_box", "jbox"}:
        return "IfcJunctionBox", None
    if token in {"luminaire", "light", "light-fixture", "light_fixture"}:
        return "IfcLightFixture", None
    if token in {"switch", "disconnect"}:
        return "IfcSwitchingDevice", None
    return "IfcElectricAppliance", None


def _segment_ifc_type(route_type: str) -> tuple[str, str | None]:
    token = route_type.lower()
    if token == "cable":
        return "IfcCableSegment", "CABLESEGMENT"
    if token in {"tray", "cabletray", "cable-tray"}:
        return "IfcCableCarrierSegment", "CABLETRAYSEGMENT"
    if token in {"trunking", "wireway"}:
        return "IfcCableCarrierSegment", "CABLETRUNKINGSEGMENT"
    return "IfcCableCarrierSegment", "CONDUITSEGMENT"


def _fitting_predefined_type(token: str) -> str:
    token = token.lower()
    if "tee" in token:
        return "TEE"
    if "cross" in token:
        return "CROSS"
    if "reducer" in token:
        return "REDUCER"
    return "BEND"


def _flow_direction(role: str) -> str:
    token = role.lower()
    if token in {"source", "supply", "out"}:
        return "SOURCE"
    if token in {"sink", "load", "in"}:
        return "SINK"
    if token in {"bidirectional", "source-and-sink", "sourceandsink"}:
        return "SOURCEANDSINK"
    return "NOTDEFINED"


def _assign_spatial_container(
    ifc: ifcopenshell.file,
    entity: Any,
    product: Any,
    storeys: Mapping[str, Any],
    spaces: Mapping[str, Any],
    *,
    model: BuildingModel | None = None,
) -> None:
    space_id = getattr(entity, "space_id", None)
    level_id = getattr(entity, "level_id", None)
    if space_id and space_id in spaces:
        ifcopenshell.api.spatial.assign_container(
            ifc, relating_structure=spaces[space_id], products=[product]
        )
    elif level_id and level_id in storeys:
        ifcopenshell.api.spatial.assign_container(
            ifc, relating_structure=storeys[level_id], products=[product]
        )
    elif model is not None and hasattr(entity, "route_id"):
        route = next((r for r in model.routes if r.id == entity.route_id), None)
        if route is not None:
            inferred = _route_level_id(model, route)
            if inferred and inferred in storeys:
                ifcopenshell.api.spatial.assign_container(
                    ifc, relating_structure=storeys[inferred], products=[product]
                )


def _has_spatial_container(product: Any) -> bool:
    return bool(getattr(product, "ContainedInStructure", ()) or ())


def _add_adapter_port(
    ifc: ifcopenshell.file,
    owner: Any,
    stable_key: str,
    position: Point3,
    direction: Vector3,
    *,
    route_id: str,
    role: str,
) -> Any:
    port = ifcopenshell.api.system.add_port(ifc, element=owner)
    port.GlobalId = canonical_id_to_ifc_guid(stable_key)
    port.Name = stable_key
    port.FlowDirection = "NOTDEFINED"
    try:
        port.PredefinedType = "CABLECARRIER"
        port.SystemType = "ELECTRICAL"
    except (AttributeError, TypeError, ValueError):
        pass
    _set_pose(ifc, port, Pose(position=position))
    _mark_adapter(ifc, port, Role=role, RouteId=route_id, StableKey=stable_key)
    return port


def _connect(ifc: ifcopenshell.file, first: Any, second: Any) -> None:
    ifcopenshell.api.system.connect_port(
        ifc, port1=first, port2=second, direction="NOTDEFINED"
    )


def _direction(start: Point3, end: Point3) -> Vector3:
    dx, dy, dz = end.x - start.x, end.y - start.y, end.z - start.z
    magnitude = math.sqrt(dx * dx + dy * dy + dz * dz)
    if magnitude <= _EPS:
        raise IfcAdapterError("route segment has zero length")
    return Vector3(x=dx / magnitude, y=dy / magnitude, z=dz / magnitude)


def _neg(value: Vector3) -> Vector3:
    return Vector3(x=-value.x, y=-value.y, z=-value.z)


def _fittings_by_boundary(points: tuple[Point3, ...], fittings: list[Any]) -> dict[int, list[Any]]:
    result: dict[int, list[Any]] = {}
    for fitting in fittings:
        if len(points) < 3:
            continue
        distances = [
            _point_distance(fitting.pose.position, point) for point in points[1:-1]
        ]
        if not distances:
            continue
        local_index = min(range(len(distances)), key=distances.__getitem__)
        if distances[local_index] <= 1e-5:
            boundary = local_index + 1
            result.setdefault(boundary, []).append(fitting)
    return result


def _point_distance(first: Point3, second: Point3) -> float:
    return math.sqrt(
        (first.x - second.x) ** 2
        + (first.y - second.y) ** 2
        + (first.z - second.z) ** 2
    )


def _route_level_id(model: BuildingModel, route: Any) -> str | None:
    port = next((p for p in model.ports if p.id == route.start_port_id), None)
    if port is None:
        return None
    owner = next(
        (
            item
            for item in (*model.electrical_equipment, *model.electrical_devices)
            if item.id == port.owner_id
        ),
        None,
    )
    return getattr(owner, "level_id", None)


def _owner_ifc_for_port(
    model: BuildingModel, port_id: str, entity_ifc: Mapping[str, Any]
) -> Any | None:
    port = next((p for p in model.ports if p.id == port_id), None)
    return entity_ifc.get(port.owner_id) if port is not None else None


def _route_segments(ifc: ifcopenshell.file) -> dict[str, list[tuple[int, Any]]]:
    result: dict[str, list[tuple[int, Any]]] = {}
    for class_name in ("IfcCableCarrierSegment", "IfcCableSegment"):
        for item in ifc.by_type(class_name):
            meta = _property_set(item, ADAPTER_PSET)
            if not meta or meta.get("Role") != "route-segment":
                continue
            route_id = str(meta["RouteId"])
            index = int(meta["SegmentIndex"])
            result.setdefault(route_id, []).append((index, item))
    for items in result.values():
        items.sort(key=lambda pair: pair[0])
    return result


def _native_canonical_port_connections(
    ifc: ifcopenshell.file,
    canonical_items: Mapping[str, tuple[Any, dict[str, Any], int, str]],
) -> dict[str, set[str]]:
    id_by_step = {
        item.id(): canonical_id
        for canonical_id, (item, _data, _ordinal, kind) in canonical_items.items()
        if kind == "port"
    }
    connections: dict[str, set[str]] = {canonical_id: set() for canonical_id in id_by_step.values()}
    for rel in ifc.by_type("IfcRelConnectsPorts"):
        left = id_by_step.get(rel.RelatingPort.id())
        right = id_by_step.get(rel.RelatedPort.id())
        if left and right:
            connections[left].add(right)
            connections[right].add(left)
    return connections


def _apply_ifc_overrides(
    ifc: ifcopenshell.file,
    item: Any,
    data: dict[str, Any],
    kind: str,
    *,
    route_segments: Mapping[str, list[tuple[int, Any]]],
    native_connections: Mapping[str, set[str]],
    canonical_items: Mapping[str, tuple[Any, dict[str, Any], int, str]],
) -> None:
    if "name" in data:
        data["name"] = item.Name

    if kind == "level":
        if getattr(item, "Elevation", None) is not None:
            data["elevation_m"] = float(item.Elevation)
        return

    if kind in {"electrical_equipment", "electrical_device", "opening", "route_fitting"}:
        pose = _pose_from_product(item)
        if pose is not None:
            data["pose"] = pose
        return

    if kind == "port":
        pose = _pose_from_product(item)
        if pose is not None:
            data["pose"] = pose
        owner = _canonical_port_owner(ifc, item, canonical_items)
        if owner is not None:
            data["owner_id"] = owner
        native = native_connections.get(data["id"], set())
        if native:
            data["connected_port_ids"] = sorted(native)
        return

    if kind == "wall":
        points = _polyline_points(item)
        if points is not None and len(points) >= 2:
            data["centerline"] = {
                "kind": "polyline3d",
                "points": [_point_dict(point) for point in points],
            }
        return

    if kind == "route":
        segments = route_segments.get(data["id"], [])
        if segments:
            points: list[tuple[float, float, float]] = []
            expected_index = 0
            for index, segment in segments:
                if index != expected_index:
                    raise IfcAdapterError(
                        f"route {data['id']!r} has non-contiguous segment index {index}"
                    )
                segment_points = _polyline_points(segment)
                if segment_points is None or len(segment_points) != 2:
                    raise IfcAdapterError(
                        f"route segment {segment.GlobalId!r} must have a two-point Axis representation"
                    )
                start, end = segment_points
                if points and _tuple_distance(points[-1], start) > 1e-5:
                    raise IfcAdapterError(
                        f"route {data['id']!r} segment {index} is disconnected"
                    )
                if not points:
                    points.append(start)
                points.append(end)
                expected_index += 1
            data["centerline"] = {
                "kind": "polyline3d",
                "points": [_point_tuple_dict(point) for point in points],
            }
        return


def _canonical_port_owner(
    ifc: ifcopenshell.file,
    port: Any,
    canonical_items: Mapping[str, tuple[Any, dict[str, Any], int, str]],
) -> str | None:
    canonical_id_by_step = {
        item.id(): canonical_id for canonical_id, (item, _data, _ordinal, _kind) in canonical_items.items()
    }
    for rel in ifc.by_type("IfcRelNests"):
        if any(related.id() == port.id() for related in rel.RelatedObjects):
            return canonical_id_by_step.get(rel.RelatingObject.id())
    for rel in ifc.by_type("IfcRelConnectsPortToElement"):
        if rel.RelatingPort.id() == port.id():
            return canonical_id_by_step.get(rel.RelatedElement.id())
    return None


def _polyline_points(product: Any) -> list[tuple[float, float, float]] | None:
    representation = getattr(product, "Representation", None)
    if representation is None:
        return None
    placement = getattr(product, "ObjectPlacement", None)
    matrix = (
        np.asarray(ifcopenshell.util.placement.get_local_placement(placement), dtype=float)
        if placement is not None
        else np.eye(4)
    )
    for shape in representation.Representations:
        if shape.RepresentationIdentifier != "Axis":
            continue
        for item in shape.Items:
            if not item.is_a("IfcPolyline"):
                continue
            result: list[tuple[float, float, float]] = []
            for point in item.Points:
                coords = list(point.Coordinates)
                while len(coords) < 3:
                    coords.append(0.0)
                local = np.asarray([float(coords[0]), float(coords[1]), float(coords[2]), 1.0])
                world = matrix @ local
                result.append((float(world[0]), float(world[1]), float(world[2])))
            return result
    return None


def _point_dict(point: tuple[float, float, float]) -> dict[str, float]:
    return {"x": point[0], "y": point[1], "z": point[2]}


def _point_tuple_dict(point: tuple[float, float, float]) -> dict[str, float]:
    return _point_dict(point)


def _tuple_distance(first: tuple[float, float, float], second: tuple[float, float, float]) -> float:
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(first, second)))
