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
import ifcopenshell.api.style
import ifcopenshell.api.system
import ifcopenshell.api.unit
import ifcopenshell.guid
import ifcopenshell.util.placement
import ifcopenshell.util.pset
import numpy as np

from oabm.model import (
    DERIVATION_INFERRED,
    DERIVATION_OBSERVED,
    DERIVATION_USER,
    SCHEMA_VERSION,
    Box3D,
    BuildingModel,
    Point3,
    Polyline3D,
    Pose,
    Quaternion,
    Size3,
    Vector3,
)
from oabm.model.entities import (
    WALL_CONSTRUCTION_CONCRETE,
    WALL_CONSTRUCTION_FRAMED,
    WALL_CONSTRUCTION_GLAZED,
    WALL_CONSTRUCTION_MASONRY,
)

IFC_SCHEMA = "IFC4"
CANONICAL_PSET = "OABM_Canonical"
ADAPTER_PSET = "OABM_Adapter"
PROVENANCE_PSET = "OABM_Provenance"
_IFC_NAMESPACE = uuid.UUID("0c569baf-3d91-5d5e-a755-9eb9435c67ae")
_EPS = 1e-7
# An export is derived bytes, not an event: the same canonical model must
# serialize to the same file on every run, so the STEP header carries this
# fixed instant instead of the wall clock (see docs/ifc-adapter.md).
_FIXED_EXPORT_TIMESTAMP = "1970-01-01T00:00:00"
# Relationship classes whose GlobalId the adapter always derives from
# canonical identity at creation (port links and system-service links). Their
# keys are not reconstructible from generic structure alone, so the
# determinism pass never restamps them.
_PINNED_RELATIONSHIP_CLASSES = frozenset(
    {"IfcRelConnectsPorts", "IfcRelServicesBuildings"}
)
_OABM_PSET_NAMES = frozenset({CANONICAL_PSET, ADAPTER_PSET, PROVENANCE_PSET})
# Description carried by a caller-supplied group and its assignment, and the
# marker that tells the determinism pass their GlobalIds were already derived
# from the group name at creation (api-created group assignments for routes
# and circuits carry no description and stay restampable).
_CALLER_GROUP_DESCRIPTION = "caller-supplied group"
# The standard material per ``Wall.construction`` token: one shared
# ``IfcMaterial`` per token actually used by at least one wall (created in
# sorted token order), so Bonsai and Revit can filter walls by construction
# with no OABM knowledge. The token is canonical (see ``oabm.model``); the
# material is derived output that ``from_ifc`` never reads back.
_MATERIAL_BY_TOKEN: Mapping[str, tuple[str, str]] = {
    WALL_CONSTRUCTION_CONCRETE: ("Concrete", "concrete"),
    WALL_CONSTRUCTION_FRAMED: ("Framed partition", "framing"),
    WALL_CONSTRUCTION_GLAZED: ("Glass", "glass"),
    WALL_CONSTRUCTION_MASONRY: ("Masonry", "masonry"),
}
# Description carried by every per-token material association, and the
# marker that tells the determinism pass its GlobalId was already derived
# from the token at creation.
_WALL_MATERIAL_REL_DESCRIPTION = "wall material link"
# GlobalId key prefix of a wall material association (#164).
_WALL_MATERIAL_KEY_PREFIX = "wall-construction-material:"

#: The IFC4 ``PEnum_ElementStatus`` values, the only legal values of the
#: caller-supplied ``element_status`` option. Which id gets which status is
#: the caller's decision; the exporter never derives one.
_ELEMENT_STATUS_VALUES = frozenset({
    "NEW",
    "EXISTING",
    "DEMOLISH",
    "TEMPORARY",
    "OTHER",
    "NOTKNOWN",
    "UNSET",
})
# The one shared translucent style a glazed wall's Body items carry, so Bonsai
# draws glass: a light blue-grey surface at Transparency 0.65.
_GLASS_STYLE_NAME = "OABM Glazed"
_GLASS_TRANSPARENCY = 0.65
_GLASS_COLOUR_RGB = (0.80, 0.86, 0.90)
# Planarity/horizontality slop for derived Body authoring, in metres. Canonical
# architecture geometry sits on level planes; anything beyond this is treated as
# not determinable rather than approximated.
_BODY_SLOP_M = 1e-6

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


def to_ifc(
    model: BuildingModel,
    destination: str | Path | None = None,
    *,
    groups: Mapping[str, Iterable[str]] | None = None,
    element_status: Mapping[str, str] | None = None,
) -> ifcopenshell.file:
    """Materialize a canonical model as IFC4 suitable for Bonsai editing.

    Exporting one model twice yields byte-identical STEP files: the header
    time stamp is fixed, and every ``IfcRoot`` GlobalId is derived from model
    content (see ``_finalize_deterministic_bytes``).

    Canonical entities retain deterministic IFC GlobalIds. IFC-native geometry,
    ports, systems, and connectivity are authored where IFC has an equivalent.
    The ``OABM_Canonical`` property set stores only the lossless contract shadow
    needed for fields IFC does not carry directly (for example provenance), and
    the plain ``OABM_Provenance`` property set restates that provenance legibly
    for viewers with no OABM knowledge; ``from_ifc`` reads only the blob.

    Architecture entities carry two independent shape representations. The
    ``Axis`` curve (wall centerline, space/slab/ceiling footprint) is the
    canonical geometry Bonsai may edit. The ``Body`` swept solid is a viewer
    view derived from the canonical dimension fields only: a wall is an
    ``IfcExtrudedAreaSolid`` rectangle per straight centerline segment
    extruded up ``height_m``, slabs and ceilings extrude the footprint polygon
    down ``thickness_m``, spaces extrude it up ``height_m`` — all authored in
    world coordinates — and an opening gets a void box (``size.x`` x host wall
    ``thickness_m`` x ``size.z``) authored in its product-local coordinates,
    because its ``ObjectPlacement`` already carries the canonical pose, so
    ``IfcRelVoidsElement`` actually cuts. Electrical devices, equipment and
    box obstacles get the same centered local box from their ``Size3``
    (obstacle placements carry the box pose); conductors and route fittings
    deliberately get no Body and record why. An entity missing a dimension its
    Body needs gets no Body and no invented default; the reason is recorded on
    its ``OABM_Adapter`` property set (``Body=no`` with ``BodyReason``).

    The keyword-only ``groups`` option maps a caller-chosen group name (for
    example ``"ALTERNATES"``) to the canonical entity ids that belong to it,
    so an alternate scope is one selectable, hideable set in Bonsai or Revit.
    Each group becomes one standard ``IfcGroup`` with ``Description``
    ``caller-supplied group`` and one ``IfcRelAssignsToGroup`` whose
    ``RelatedObjects`` are the members' IFC products, passed sorted by
    canonical id; a route member contributes its segment and fitting products
    (exactly what its ``IfcDistributionSystem`` already groups), a conductor
    its own product, and ids that match nothing are ignored. The exporter
    never decides what belongs together: it only writes the grouping the
    caller names. An id may belong to at most one group
    (``IfcAdapterError`` otherwise). Group and relationship GlobalIds derive
    from the group name, so a grouped export stays byte-deterministic, and
    ``groups=None`` or ``{}`` leaves the written bytes unchanged.
    ``from_ifc`` ignores these groups — they carry ``OABM_Adapter`` metadata,
    no canonical payload — so the canonical round trip is unaffected.

    The keyword-only ``element_status`` option maps a canonical entity id to
    one of the IFC4 ``PEnum_ElementStatus`` values (``NEW``, ``EXISTING``,
    ``DEMOLISH``, ``TEMPORARY``, ``OTHER``, ``NOTKNOWN``, ``UNSET``), and the
    value is written as the standard ``Status`` property of that product's
    applicable common property set (for example ``Pset_OutletTypeCommon`` for
    an outlet, ``Pset_WallCommon`` for a wall), so Bonsai and Revit can filter
    rework phase with no OABM knowledge. Which id gets which status is the
    caller's decision; an unknown value raises ``IfcAdapterError``. A route id
    expands to its segment and fitting products, like ``groups``. A product
    whose class has no ``Status``-bearing common set (a space, for example)
    and an id that matches nothing are skipped and counted; ``to_ifc`` has no
    summary channel, so those counts are only documented here. The set's
    GlobalId is pinned from ``status-pset:`` plus the canonical id, and
    ``element_status=None`` or ``{}`` leaves the written bytes unchanged.
    ``from_ifc`` reads only ``OABM_Canonical`` and ignores ``Status``, so the
    canonical round trip is unaffected.

    A wall whose canonical ``construction`` token is set is associated with one
    shared ``IfcMaterial`` per token (``Glass``, ``Masonry``, ``Concrete``,
    ``Framed partition``) through an ``IfcRelAssociatesMaterial``, so Bonsai
    and Revit can filter by construction. Glazed walls also carry one shared
    translucent surface style (Transparency 0.65, light blue-grey) on their
    Body items. The material is derived output: ``from_ifc`` keeps reading
    ``construction`` from the canonical pset, so ``round_trip`` stays exact,
    and a model whose walls all lack a token writes the same bytes as a
    default export without the option ever being present.
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
        if space.height_m is None:
            _mark_adapter(ifc, item, Body="no", BodyReason="no canonical height")
        else:
            reason = _assign_polygon_extrusion_body(
                ifc, item, body_context, space.footprint, space.height_m, upward=True
            )
            _mark_body(ifc, item, reason)
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
        _mark_body(ifc, item, _assign_wall_body(ifc, item, body_context, wall))

    _assign_wall_materials(ifc, model, entity_ifc)

    for ordinal, slab in enumerate(model.slabs):
        item = add_product(slab, "slab", ordinal, "IfcSlab", predefined_type="FLOOR")
        _assign_polyline_representation(
            ifc,
            item,
            axis_context,
            (*slab.footprint.points, slab.footprint.points[0]),
        )
        reason = _assign_polygon_extrusion_body(
            ifc, item, body_context, slab.footprint, slab.thickness_m, upward=False
        )
        _mark_body(ifc, item, reason)

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
        if ceiling.thickness_m is None:
            _mark_adapter(ifc, item, Body="no", BodyReason="no canonical thickness")
        else:
            reason = _assign_polygon_extrusion_body(
                ifc, item, body_context, ceiling.footprint, ceiling.thickness_m, upward=False
            )
            _mark_body(ifc, item, reason)

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
        host_wall = next((w for w in model.walls if w.id == opening.host_id), None)
        if host_wall is not None:
            _mark_body(ifc, item, _assign_opening_body(ifc, item, body_context, opening, host_wall))
        else:
            known_host = any(
                entity.id == opening.host_id
                for collection in (
                    model.slabs,
                    model.ceilings,
                    model.spaces,
                    model.electrical_equipment,
                    model.electrical_devices,
                )
                for entity in collection
            )
            reason = (
                "opening host is not a wall; no canonical wall thickness"
                if known_host
                else "opening host not found in the model; no canonical wall thickness"
            )
            _mark_adapter(ifc, item, Body="no", BodyReason=reason)

    for ordinal, obstacle in enumerate(model.obstacles):
        box_pose = obstacle.geometry.pose if isinstance(obstacle.geometry, Box3D) else None
        item = add_product(
            obstacle,
            "obstacle",
            ordinal,
            "IfcBuildingElementProxy",
            pose=box_pose,
        )
        _mark_body(ifc, item, _assign_obstacle_body(ifc, item, body_context, obstacle.geometry))

    for ordinal, constraint in enumerate(model.route_constraints):
        add_product(constraint, "route_constraint", ordinal, "IfcAnnotation")

    for ordinal, equipment in enumerate(model.electrical_equipment):
        ifc_class, predefined = _equipment_ifc_type(equipment.equipment_type)
        item = add_product(
            equipment,
            "electrical_equipment",
            ordinal,
            ifc_class,
            predefined_type=predefined,
            pose=equipment.pose,
        )
        _mark_body(ifc, item, _assign_box_body(ifc, item, body_context, equipment.size))

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
        _mark_body(ifc, item, _assign_box_body(ifc, item, body_context, device.size))

    canonical_ports: dict[str, Any] = {}
    canonical_port_owners: dict[str, Any] = {}
    model_ports = {port.id: port for port in model.ports}
    for ordinal, port in enumerate(model.ports):
        owner = entity_ifc.get(port.owner_id)
        if owner is None or not owner.is_a("IfcDistributionElement"):
            raise IfcAdapterError(
                f"port {port.id!r} owner {port.owner_id!r} is not an IFC distribution element"
            )
        canonical_port_owners[port.id] = owner
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

    # Explicit canonical connectivity must be present in native IFC when export
    # succeeds. Never let the JSON shadow conceal an unexpected IfcOpenShell/API
    # failure, because Bonsai edits need a single authoritative representation.
    fanout_ports = sorted(
        port.id for port in model.ports if len(port.connected_port_ids) > 1
    )
    if fanout_ports:
        raise IfcAdapterError(
            "canonical port connectivity cannot be represented natively without loss; "
            f"IFC ports support one connected peer: {fanout_ports!r}"
        )

    # Port connections are collected and materialized once, at the end.
    # ``ifcopenshell.api.system.connect_port`` is not used: it purges existing
    # connections on either port and writes two reciprocal relationships for one
    # logical connection, which is how canonical peer links were silently lost.
    #
    # Canonical ports carry canonical peer connectivity only. Route ends attach
    # to adapter-owned ports of their own (see the route loop), because IFC4
    # gives a port one relationship per role and a panel port is routinely the
    # start of many home runs.
    port_links: list[tuple[Any, Any]] = []
    linked: set[tuple[str, str]] = set()
    for port in model.ports:
        for other_id in port.connected_port_ids:
            pair = tuple(sorted((port.id, other_id)))
            if pair in linked:
                continue
            port_links.append((canonical_ports[port.id], canonical_ports[other_id]))
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
        _mark_adapter(
            ifc,
            item,
            Body="no",
            BodyReason="fitting is a placement-only occurrence on its conduit run",
        )
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
            # Adapter-generated segments are reachable under their stable
            # segment keys, like the canonical products; caller-supplied
            # groups expand a route member through them.
            entity_ifc[segment_key] = segment

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
            # Each route end gets its own attachment port, nested on the
            # canonical port's owner at the canonical port's position. Wiring
            # the span to the canonical port itself would spend one of that
            # port's two IFC4 role slots per route end, so a panel port that
            # starts more than two home runs could not be exported.
            start_attachment = _add_route_attachment_port(
                ifc,
                canonical_port_owners[route.start_port_id],
                model_ports[route.start_port_id],
                route_id=route.id,
                end="start",
            )
            end_attachment = _add_route_attachment_port(
                ifc,
                canonical_port_owners[route.end_port_id],
                model_ports[route.end_port_id],
                route_id=route.id,
                end="end",
            )
            _connect(port_links, start_attachment, segment_ports[0][0])
            _connect(port_links, segment_ports[-1][1], end_attachment)
            boundary_fittings = _fittings_by_boundary(points, route_fittings)
            for boundary in range(len(segment_ports) - 1):
                left = segment_ports[boundary][1]
                right = segment_ports[boundary + 1][0]
                chain = boundary_fittings.get(boundary + 1, [])
                previous = left
                for fitting in chain:
                    ports = fitting_ports[fitting.id]
                    _connect(port_links, previous, ports[0])
                    previous = ports[1]
                _connect(port_links, previous, right)

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
        _mark_adapter(
            ifc,
            item,
            Body="no",
            BodyReason="conductor is represented by its route's conduit solid; see route_ids",
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

    _assign_caller_groups(ifc, groups or {}, entity_ifc)
    _assign_element_status(ifc, element_status, entity_ifc)

    # An IfcSystem is only reachable to a consumer once it is declared to serve
    # a spatial structure. Without IfcRelServicesBuildings a route or circuit is
    # present in the file but belongs to no building, so viewers and MVD
    # checkers drop it.
    for canonical_id in (
        *(route.id for route in model.routes),
        *(circuit.id for circuit in model.circuits),
    ):
        system = entity_ifc.get(canonical_id)
        if system is None or not system.is_a("IfcSystem"):
            continue
        ifc.create_entity(
            "IfcRelServicesBuildings",
            GlobalId=canonical_id_to_ifc_guid(f"{canonical_id}#services-building"),
            RelatingSystem=system,
            RelatedBuildings=[building],
        )

    _materialize_port_connections(ifc, port_links)

    # Verify canonical port connectivity LAST, once every native connection has
    # been written. Anything that drops a link must surface here, not silently.
    expected_connections = {
        port.id: set(port.connected_port_ids) for port in model.ports
    }
    native_connections = _native_port_connections(ifc, canonical_ports)
    if native_connections != expected_connections:
        mismatched = sorted(
            port_id
            for port_id, expected in expected_connections.items()
            if native_connections.get(port_id, set()) != expected
        )
        raise IfcAdapterError(
            "canonical port connectivity cannot be represented natively without loss "
            f"for ports {mismatched!r}"
        )

    _finalize_deterministic_bytes(ifc)

    if destination is not None:
        ifc.write(str(Path(destination)))
    return ifc


def _finalize_deterministic_bytes(ifc: ifcopenshell.file) -> None:
    """Pin every serialized value that would otherwise vary between runs.

    Two exports of one canonical model are two renderings of the same content
    and must be byte-identical. Two things would otherwise differ:

    - the STEP header's ``FILE_NAME`` time stamp, which ifcopenshell sets from
      the wall clock, is pinned to ``_FIXED_EXPORT_TIMESTAMP``;
    - ``ifcopenshell.api`` mints random GlobalIds for the entities it creates
      internally -- property sets, ``IfcRelDefinesByProperties``, aggregates,
      spatial containment, group assignments, feature voids/fills. Every such
      ``IfcRoot`` entity is restamped here from a key derived only from model
      content (see ``_assign_deterministic_global_ids``).
    """

    ifc.header.file_name.time_stamp = _FIXED_EXPORT_TIMESTAMP
    _normalize_relationship_reference_order(ifc)
    _assign_deterministic_global_ids(ifc)


def _normalize_relationship_reference_order(ifc: ifcopenshell.file) -> None:
    """Write SET-valued relationship references in one deterministic order.

    IFC relationship ``SET`` attributes (``RelatedObjects``, ``RelatedElements``,
    ``RelatedBuildings``) are unordered by schema, but ifcopenshell serializes
    them in whatever order its internal container happens to hold, which varies
    between processes. Sorting by STEP id fixes the written order; STEP ids are
    themselves deterministic because the build is. Ordered LIST attributes are
    never touched (singular references are not lists at all).
    """

    for rel in ifc.by_type("IfcRelationship"):
        for name, value in rel.get_info().items():
            if not name.startswith("Related") or not isinstance(value, (list, tuple)):
                continue
            ordered = sorted(value, key=lambda item: item.id())
            if list(value) != ordered:
                setattr(rel, name, ordered)


def _carries_oabm_pset(item: Any) -> bool:
    """Whether ``item`` carries one of the adapter's own property sets.

    Carriers are exactly the entities whose GlobalId the adapter already pins
    from canonical identity at creation (the project, canonical products,
    adapter ports and spatial containers), so their GlobalIds are final.
    """

    for rel in getattr(item, "IsDefinedBy", ()) or ():
        if not rel.is_a("IfcRelDefinesByProperties"):
            continue
        definition = rel.RelatingPropertyDefinition
        if definition.is_a("IfcPropertySet") and definition.Name in _OABM_PSET_NAMES:
            return True
    return False


def _is_pinned_root(item: Any) -> bool:
    """Whether ``item``'s GlobalId is already derived from model content."""

    if item.is_a("IfcPort") or item.is_a() in _PINNED_RELATIONSHIP_CLASSES:
        return True
    if (
        item.is_a("IfcRelAssignsToGroup")
        and item.Description == _CALLER_GROUP_DESCRIPTION
    ):
        # A caller-supplied group assignment pins its GlobalId at creation,
        # derived from the group name. The api-created group assignments of
        # the route and circuit systems carry no description, so they are
        # still restamped here from their content.
        return True
    if (
        item.is_a("IfcRelAssociatesMaterial")
        and item.Description == _WALL_MATERIAL_REL_DESCRIPTION
    ):
        # A per-token material association pins its GlobalId at creation,
        # derived from the token, like the caller-supplied group
        # assignments above.
        return True
    if item.is_a("IfcPropertySet") and item.Name == PROVENANCE_PSET:
        # The legible provenance set pins its GlobalId to its owner's
        # canonical identity at creation (``<canonical id>#OABM_Provenance``).
        return True
    if item.is_a("IfcPropertySet") and _is_status_pset(item):
        # A caller-supplied element Status set pins its GlobalId to its
        # owner's canonical identity at creation
        # (``status-pset:<canonical id>``).
        return True
    return _carries_oabm_pset(item)


def _assign_deterministic_global_ids(ifc: ifcopenshell.file) -> None:
    """Give every unpinned ``IfcRoot`` entity a content-derived GlobalId.

    Canonical products, ports, spatial containers, systems, port links,
    service links and the legible provenance property sets already carry
    deterministic GlobalIds (``from_ifc`` verifies the canonical ones), so
    they are never touched. The rest -- property sets and the relationships
    ``ifcopenshell.api`` created with random GlobalIds -- are restamped from
    a stable key over model content:

    - a property set is keyed by its name plus the sorted GlobalIds of the
      objects it describes;
    - a relationship is keyed by its IFC class plus every relating/related
      reference it holds.

    A key collision fails loudly: two distinct entities that claim one
    content-derived identity mean the key rule is too weak, and silently
    sharing a GlobalId would corrupt the file. Creation order is never part of
    a key.
    """

    claimed: dict[str, str] = {}

    def claim(guid: str, owner: str) -> str:
        if guid in claimed:
            raise IfcAdapterError(
                "deterministic GlobalId collision between "
                f"{claimed[guid]!r} and {owner!r}"
            )
        claimed[guid] = owner
        return guid

    pinned_ids: set[int] = set()
    for item in ifc.by_type("IfcRoot"):
        if _is_pinned_root(item):
            pinned_ids.add(item.id())
            claim(item.GlobalId, f"pinned {item.is_a()} {item.GlobalId!r}")

    # Property sets first: relationship keys reference the sets they assign,
    # so the sets' final GlobalIds must exist before any relationship is keyed.
    defining_rels: dict[int, list[Any]] = {}
    for rel in ifc.by_type("IfcRelDefinesByProperties"):
        definition = rel.RelatingPropertyDefinition
        defining_rels.setdefault(definition.id(), []).append(rel)

    final_ids: dict[int, str] = {}
    keyed_psets: list[tuple[str, Any]] = []
    for pset in ifc.by_type("IfcPropertySet"):
        if pset.id() in pinned_ids:
            continue
        described = sorted(
            {
                related.GlobalId
                for rel in defining_rels.get(pset.id(), ())
                for related in rel.RelatedObjects
            }
        )
        keyed_psets.append(
            (f"pset:{pset.Name or ''}:{','.join(described)}", pset)
        )
    for key, pset in sorted(keyed_psets, key=lambda item: (item[0], item[1].id())):
        pset.GlobalId = claim(
            canonical_id_to_ifc_guid(key), f"property set {pset.Name!r}"
        )
        final_ids[pset.id()] = pset.GlobalId

    # Relationships in dependency order: a relationship that references
    # another unpinned relationship waits for one round; a cycle or an
    # unresolvable reference fails loudly instead of hashing a random value.
    remaining = [
        rel
        for rel in ifc.by_type("IfcRelationship")
        if rel.id() not in pinned_ids
    ]
    while remaining:
        ready: list[tuple[str, Any]] = []
        deferred: list[Any] = []
        for rel in remaining:
            key, resolvable = _relationship_stable_key(rel, pinned_ids, final_ids)
            if resolvable:
                ready.append((key, rel))
            else:
                deferred.append(rel)
        if not ready:
            raise IfcAdapterError(
                "cannot derive deterministic GlobalIds: relationships "
                f"{sorted(rel.is_a() for rel in deferred)!r} reference each other "
                "without any pinned identity"
            )
        for key, rel in sorted(ready, key=lambda item: (item[0], item[1].id())):
            rel.GlobalId = claim(canonical_id_to_ifc_guid(key), rel.is_a())
            final_ids[rel.id()] = rel.GlobalId
        remaining = deferred


def _relationship_stable_key(
    rel: Any, pinned_ids: set[int], final_ids: Mapping[int, str]
) -> tuple[str, bool]:
    """Build the stable key for one relationship, or report it unresolvable.

    The key is the IFC class followed by every attribute value in schema
    order; references to IfcRoot entities contribute their final GlobalId,
    references to GlobalId-less entities (for example ``IfcMaterial``) their
    class and name. Unresolvable means a referenced relationship does not have
    a final GlobalId yet and the caller must wait for a later round.
    """

    parts = [rel.is_a()]
    resolvable = True
    for name, value in rel.get_info().items():
        if name in ("id", "GlobalId"):
            continue
        part, part_resolvable = _reference_key(value, pinned_ids, final_ids)
        parts.append(f"{name}={part}")
        resolvable = resolvable and part_resolvable
    return "|".join(parts), resolvable


def _reference_key(
    value: Any, pinned_ids: set[int], final_ids: Mapping[int, str]
) -> tuple[str, bool]:
    resolvable = True
    if value is None:
        return "-", resolvable
    if isinstance(value, (list, tuple)):
        rendered = [
            _reference_key(item, pinned_ids, final_ids) for item in value
        ]
        resolvable = all(item[1] for item in rendered)
        return ",".join(sorted(item[0] for item in rendered)), resolvable
    if isinstance(value, ifcopenshell.entity_instance):
        if value.id() in final_ids:
            return final_ids[value.id()], resolvable
        if value.id() in pinned_ids or not value.is_a("IfcRoot"):
            # Pinned IfcRoot entities were born deterministic; entities
            # without a GlobalId (IfcMaterial, geometry) are identified by
            # class and name.
            ident = getattr(value, "GlobalId", None) or (
                f"{value.is_a()}:{getattr(value, 'Name', None) or ''}"
            )
            return ident, resolvable
        return "", False
    return repr(value), resolvable


def from_ifc(source: str | Path | ifcopenshell.file) -> BuildingModel:
    """Read an OABM-authored IFC4 file back into the canonical v1 model.

    The function is intentionally strict: a generic third-party IFC without the
    OABM round-trip metadata is not guessed into canonical semantics. Bonsai may
    freely edit and save an OABM IFC as long as canonical entities and their
    stable GlobalIds / metadata are retained.

    Only ``Axis`` shape representations are read back as geometry (wall
    centerlines, footprints, route spans). ``Body`` solids are a derived export
    view authored from canonical dimension fields; they are never read back as
    canonical geometry, so a Bonsai edit of the axis stays authoritative and
    the derived solid can never contradict the contract.
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


def _assign_caller_groups(
    ifc: ifcopenshell.file,
    groups: Mapping[str, Iterable[str]],
    entity_ifc: Mapping[str, Any],
) -> None:
    """Write the caller-supplied groups as standard IfcGroups.

    The caller decides membership (an alternate scope, a phase, anything);
    the adapter only draws it, as one ``IfcGroup`` per name with one
    ``IfcRelAssignsToGroup``, so a viewer user can select, isolate or hide
    the whole scope in one click. Groups are created sorted by name and
    members are passed sorted by canonical id, and every GlobalId is derived
    from the group name, so a grouped export is as byte-deterministic as a
    plain one. ``from_ifc`` never reads these groups — the ``IfcGroup``
    carries only ``OABM_Adapter`` metadata — so the canonical round trip is
    unchanged.

    An id claimed by two groups is a caller error: it raises before the file
    is written. Ids that match nothing are ignored. A group whose ids all
    match nothing still gets its ``IfcGroup``, but no assignment, whose
    ``RelatedObjects`` the schema bounds at one or more.
    """

    if not groups:
        return
    names = sorted(groups)
    for name in names:
        if not isinstance(name, str) or not name:
            raise IfcAdapterError("group names must be non-empty strings")
    owner_of: dict[str, str] = {}
    members_of: dict[str, set[str]] = {}
    for name in names:
        for member_id in groups[name]:
            previous = owner_of.get(member_id)
            if previous is not None and previous != name:
                raise IfcAdapterError(
                    f"canonical id {member_id!r} belongs to both group "
                    f"{previous!r} and group {name!r}; an id may belong to "
                    "at most one group"
                )
            owner_of[member_id] = name
            members_of.setdefault(name, set()).add(member_id)
    canonical_by_step = {
        product.id(): canonical_id for canonical_id, product in entity_ifc.items()
    }
    for name in names:
        expanded: dict[str, Any] = {}
        for member_id in sorted(members_of[name]):
            for canonical_id, product in _caller_group_member_products(
                member_id, entity_ifc, canonical_by_step
            ):
                expanded[canonical_id] = product
        group = _create_root(ifc, "IfcGroup", f"group:{name}", name)
        group.Description = _CALLER_GROUP_DESCRIPTION
        _mark_adapter(ifc, group, Role="caller-group")
        if expanded:
            ifc.create_entity(
                "IfcRelAssignsToGroup",
                GlobalId=canonical_id_to_ifc_guid(f"group-rel:{name}"),
                Name=name,
                Description=_CALLER_GROUP_DESCRIPTION,
                RelatingGroup=group,
                RelatedObjects=[expanded[key] for key in sorted(expanded)],
            )


def _caller_group_member_products(
    member_id: str,
    entity_ifc: Mapping[str, Any],
    canonical_by_step: Mapping[int, str],
) -> list[tuple[str, Any]]:
    """Expand one caller-supplied group member id into (canonical id, product).

    A route's own products are the segments and fittings its
    ``IfcDistributionSystem`` groups — the route system itself is a
    container, not a selectable solid, and a group over the route should
    highlight what the viewer draws. A circuit expands the same way. Any
    other canonical id contributes its own product. Ids that match nothing
    contribute nothing.
    """

    item = entity_ifc.get(member_id)
    if item is None:
        return []
    if item.is_a("IfcSystem"):
        return [
            (canonical_by_step[product.id()], product)
            for rel in getattr(item, "IsGroupedBy", ()) or ()
            for product in rel.RelatedObjects
            if product.id() in canonical_by_step
        ]
    return [(member_id, item)]


def _is_status_pset(item: Any) -> bool:
    """Whether ``item`` is one of the adapter's caller-supplied Status sets.

    The adapter itself writes no other ``Pset_``-named property set (its own
    sets are ``OABM_*``), so a ``Pset_``-named set carrying a ``Status``
    property is one the ``element_status`` option created.
    """

    if not item.is_a("IfcPropertySet") or not (item.Name or "").startswith("Pset_"):
        return False
    return any(prop.Name == "Status" for prop in item.HasProperties)


def _status_pset_name(
    product: Any,
    template: Any,
    cache: dict[str, str | None],
) -> str | None:
    """The product class's ``Status``-bearing common set, or ``None``.

    ifcopenshell's IFC4 pset templates decide applicability (an outlet maps
    to ``Pset_OutletTypeCommon``, a wall to ``Pset_WallCommon``). When several
    applicable sets carry ``Status``, the first by name wins, so the choice
    is deterministic. Results are cached per IFC class.
    """

    ifc_class = product.is_a()
    if ifc_class not in cache:
        bearing: list[str] = []
        try:
            applicable = sorted(
                template.get_applicable(ifc_class), key=lambda item: item.Name or ""
            )
        except (AttributeError, KeyError, RuntimeError, TypeError, ValueError):
            applicable = []
        for candidate in applicable:
            try:
                has_status = any(
                    prop.Name == "Status" for prop in candidate.HasPropertyTemplates
                )
            except (AttributeError, KeyError, TypeError):
                has_status = False
            if has_status:
                bearing.append(candidate.Name)
        cache[ifc_class] = bearing[0] if bearing else None
    return cache[ifc_class]


def _pset_entity(product: Any, name: str) -> Any | None:
    """The product's existing property set ``name``, or ``None``."""

    for rel in getattr(product, "IsDefinedBy", ()) or ():
        if not rel.is_a("IfcRelDefinesByProperties"):
            continue
        definition = rel.RelatingPropertyDefinition
        if definition.is_a("IfcPropertySet") and definition.Name == name:
            return definition
    return None


def _assign_element_status(
    ifc: ifcopenshell.file,
    element_status: Mapping[str, str] | None,
    entity_ifc: Mapping[str, Any],
) -> None:
    """Write the caller-supplied element statuses as standard ``Status``.

    The caller decides which element is new, existing or to demolish; the
    exporter only writes it where IFC viewers look for it: the ``Status``
    property of the product's applicable common property set (chosen by
    ifcopenshell's IFC4 templates, first by name when several apply). A route
    id expands to its segment and fitting products, exactly like
    ``groups``. Ids that match nothing are ignored, and products whose class
    has no ``Status``-bearing set are skipped; ``to_ifc`` has no summary
    channel, so both behaviours are documented here instead of counted.
    Members are applied sorted by canonical id and each set's GlobalId is
    pinned from ``status-pset:`` plus its canonical id, so a status-carrying
    export is as byte-deterministic as a plain one.
    """

    if not element_status:
        return
    unknown = sorted({str(value) for value in element_status.values()} - _ELEMENT_STATUS_VALUES)
    if unknown:
        raise IfcAdapterError(
            f"element_status values must be one of "
            f"{sorted(_ELEMENT_STATUS_VALUES)!r}, got {unknown!r}"
        )

    canonical_by_step = {
        product.id(): canonical_id for canonical_id, product in entity_ifc.items()
    }
    template = ifcopenshell.util.pset.get_template("IFC4")
    status_psets: dict[str, str | None] = {}
    for member_id in sorted(element_status):
        value = str(element_status[member_id])
        if member_id not in entity_ifc:
            continue
        for canonical_id, product in _caller_group_member_products(
            member_id, entity_ifc, canonical_by_step
        ):
            pset_name = _status_pset_name(product, template, status_psets)
            if pset_name is None:
                continue
            pset = _pset_entity(product, pset_name)
            if pset is None:
                pset = ifcopenshell.api.pset.add_pset(ifc, product=product, name=pset_name)
                pset.GlobalId = canonical_id_to_ifc_guid(f"status-pset:{canonical_id}")
            ifcopenshell.api.pset.edit_pset(ifc, pset=pset, properties={"Status": value})


def _assign_wall_materials(
    ifc: ifcopenshell.file,
    model: BuildingModel,
    entity_ifc: Mapping[str, Any],
) -> None:
    """Associate each wall's ``construction`` token with one shared IfcMaterial.

    One ``IfcMaterial`` per token actually used by at least one wall, created
    in sorted token order, and one ``IfcRelAssociatesMaterial`` per token
    relating it to that token's wall products sorted by canonical id — the
    standard IFC material a Bonsai or Revit user filters by with no OABM
    knowledge. The association's GlobalId derives from the token (see
    ``_WALL_MATERIAL_KEY_PREFIX``) and is pinned at creation, so the
    determinism pass leaves it alone and exports stay byte-deterministic. The
    material is derived output: ``from_ifc`` keeps reading ``construction``
    from the ``OABM_Canonical`` pset and ignores the association, so the round
    trip stays exact. A model whose walls all lack a token creates nothing
    here, which keeps those exports byte-identical to a plain default export.

    Glazed walls additionally carry one shared translucent surface style on
    their Body items (see ``_assign_glazed_wall_style``).
    """

    walls_by_token: dict[str, list[Any]] = {}
    for wall in model.walls:
        if wall.construction is not None:
            walls_by_token.setdefault(wall.construction, []).append(wall)
    if not walls_by_token:
        return
    materials: dict[str, Any] = {}
    for token in sorted(walls_by_token):
        name, category = _MATERIAL_BY_TOKEN[token]
        materials[token] = ifc.create_entity("IfcMaterial", Name=name, Category=category)
    for token in sorted(walls_by_token):
        ifc.create_entity(
            "IfcRelAssociatesMaterial",
            GlobalId=canonical_id_to_ifc_guid(f"{_WALL_MATERIAL_KEY_PREFIX}{token}"),
            Description=_WALL_MATERIAL_REL_DESCRIPTION,
            RelatedObjects=[
                entity_ifc[wall.id]
                for wall in sorted(walls_by_token[token], key=lambda item: item.id)
            ],
            RelatingMaterial=materials[token],
        )
    _assign_glazed_wall_style(ifc, model, entity_ifc)


def _assign_glazed_wall_style(
    ifc: ifcopenshell.file,
    model: BuildingModel,
    entity_ifc: Mapping[str, Any],
) -> None:
    """Give every glazed wall's Body items one shared translucent style.

    The style is standard IFC presentation styling — one ``IfcSurfaceStyle``
    with an ``IfcSurfaceStyleRendering`` item (Transparency 0.65 over a light
    blue-grey colour), assigned to the Body items through ``IfcStyledItem``
    exactly the way ``ifcopenshell.api.style.assign_representation_styles``
    does — so Bonsai draws glass. The style is created only when at least one
    glazed wall has a Body to carry it; a glazed wall with no Body (a recorded
    ``BodyReason``) still gets its material but no style.
    """

    glazed_bodies = [
        (wall, _body_representation(entity_ifc[wall.id]))
        for wall in model.walls
        if wall.construction == WALL_CONSTRUCTION_GLAZED
    ]
    if not any(body is not None for _, body in glazed_bodies):
        return
    style = ifcopenshell.api.style.add_style(ifc, name=_GLASS_STYLE_NAME)
    ifcopenshell.api.style.add_surface_style(
        ifc,
        style=style,
        ifc_class="IfcSurfaceStyleRendering",
        attributes={
            "SurfaceColour": {
                "Name": None,
                "Red": _GLASS_COLOUR_RGB[0],
                "Green": _GLASS_COLOUR_RGB[1],
                "Blue": _GLASS_COLOUR_RGB[2],
            },
            "Transparency": _GLASS_TRANSPARENCY,
            "ReflectanceMethod": "GLASS",
        },
    )
    for _, body in glazed_bodies:
        if body is None:
            continue
        ifcopenshell.api.style.assign_representation_styles(
            ifc, shape_representation=body, styles=[style]
        )


def _body_representation(product: Any) -> Any | None:
    """The product's ``Body`` shape representation, or ``None`` without one."""

    representation = getattr(product, "Representation", None)
    for shape in getattr(representation, "Representations", ()) or ():
        if shape.RepresentationIdentifier == "Body":
            return shape
    return None


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
    visible_properties: dict[str, Any] = {}
    attributes = payload.get("attributes", {})
    if kind == "electrical_equipment" and isinstance(attributes, Mapping):
        placement = attributes.get("placement")
        if isinstance(placement, Mapping):
            visible_properties["PlacementStatus"] = str(placement.get("status", "unknown"))
            visible_properties["SourceLocationObserved"] = False
    if kind in {"circuit", "route", "conductor"} and isinstance(attributes, Mapping):
        design = attributes.get("design")
        status = design.get("status") if isinstance(design, Mapping) else attributes.get("design_status")
        if status is not None:
            visible_properties["DesignStatus"] = str(status)
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
            **visible_properties,
        },
    )
    _add_provenance_pset(ifc, product, payload)


#: Longest ``Sources``/``Methods`` text, with the truncation marker counted in.
_PROVENANCE_TEXT_LIMIT = 1000
_TRUNCATION_MARKER = " …"


def _truncated_list_text(values: list[str]) -> str:
    """``"; "``-joined text, cut deterministically when past the text limit.

    A cut value always ends with the truncation marker, so a consumer can
    tell a clipped list from a complete one; equal inputs always cut at the
    same character.
    """

    text = "; ".join(values)
    if len(text) <= _PROVENANCE_TEXT_LIMIT:
        return text
    return text[: _PROVENANCE_TEXT_LIMIT - len(_TRUNCATION_MARKER)] + _TRUNCATION_MARKER


def _entity_derivation(records: list[Mapping[str, Any]]) -> str:
    """Entity-level derivation class, from the UNSCOPED records only.

    A scoped record qualifies named fields, so it never votes on the entity
    class: a measured wall with an assumed thickness reads ``"observed"``
    here while ``InferredClaims`` names ``thickness_m``. Inferred wins over
    user, user over observed — the honest reading is the least flattering
    one. A record whose ``derivation`` is unset states nothing and reads as
    observed unless an inferred or user record is present, matching the
    display rule the GLB exporter applies to the same absence. With no
    unscoped records at all the entity states nothing: ``"unstated"``.
    """

    unscoped = [
        str(record.get("derivation") or DERIVATION_OBSERVED)
        for record in records
        if record.get("scope_paths") is None
    ]
    if DERIVATION_INFERRED in unscoped:
        return DERIVATION_INFERRED
    if DERIVATION_USER in unscoped:
        return DERIVATION_USER
    if unscoped:
        return DERIVATION_OBSERVED
    return "unstated"


def _scoped_claims(records: list[Mapping[str, Any]], derivation: str) -> str:
    """Sorted, comma-separated union of the scopes claimed by one class.

    These are the per-claim scopes the entity-level ``Derivation`` label
    deliberately does not carry: the scoping survives legibly instead of
    collapsing the entity into one class.
    """

    paths = sorted({
        str(path)
        for record in records
        if record.get("derivation") == derivation
        for path in (record.get("scope_paths") or ())
    })
    return ",".join(paths)


def _sources_text(records: list[Mapping[str, Any]]) -> str:
    pairs = sorted({
        f"{record['source_kind']}:{record['source_id']}"
        for record in records
        if record.get("source_kind") and record.get("source_id")
    })
    return _truncated_list_text(pairs)


def _methods_text(records: list[Mapping[str, Any]]) -> str:
    methods = sorted({
        str(record["method"]) for record in records if record.get("method")
    })
    return _truncated_list_text(methods)


def _add_provenance_pset(
    ifc: ifcopenshell.file, product: Any, payload: Mapping[str, Any]
) -> None:
    """Legible provenance as a plain custom property set, ``OABM_Provenance``.

    Every exported product that comes from a canonical entity carries it, so
    an ordinary IFC consumer (Bonsai, Revit, Navisworks) can tell a measured
    wall from one whose thickness this tool chose without implementing
    OABM's private format. The lossless channel stays
    ``OABM_Canonical.CanonicalJson`` and remains the only import channel:
    ``from_ifc`` reads the blob and ignores this pset, so the pset is
    legible, never a second truth. The pset's GlobalId is derived from the
    entity id plus ``#OABM_Provenance``, so exports stay deterministic.
    """

    records = [
        record
        for record in (payload.get("provenance") or ())
        if isinstance(record, Mapping)
    ]
    stable_key = str(payload.get("id") or payload.get("model_id") or "")
    pset = ifcopenshell.api.pset.add_pset(ifc, product=product, name=PROVENANCE_PSET)
    pset.GlobalId = canonical_id_to_ifc_guid(f"{stable_key}#OABM_Provenance")
    properties: dict[str, Any] = {
        "Derivation": ifc.createIfcLabel(_entity_derivation(records)),
        "InferredClaims": ifc.createIfcText(_scoped_claims(records, DERIVATION_INFERRED)),
        "UserClaims": ifc.createIfcText(_scoped_claims(records, DERIVATION_USER)),
    }
    confidence = payload.get("confidence")
    if confidence is not None:
        properties["Confidence"] = ifc.createIfcReal(float(confidence))
    attributes = payload.get("attributes")
    if isinstance(attributes, Mapping):
        design_status = attributes.get("design_status")
        if design_status is not None:
            properties["DesignStatus"] = ifc.createIfcLabel(str(design_status))
    properties["Sources"] = ifc.createIfcText(_sources_text(records))
    properties["Methods"] = ifc.createIfcText(_methods_text(records))
    ifcopenshell.api.pset.edit_pset(ifc, pset=pset, properties=properties)


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


def _ensure_placement(ifc: ifcopenshell.file, product: Any) -> None:
    """Give a product an identity placement when it does not already have one.

    IFC4 ``IfcProduct.PlacementForShapeRepresentation`` requires any product
    carrying an ``IfcShapeRepresentation`` to also carry an ``ObjectPlacement``.
    Canonical geometry is authored in world coordinates, so identity is the
    correct placement and moves nothing.
    """

    if getattr(product, "ObjectPlacement", None) is not None:
        return
    _set_pose(ifc, product, Pose(position=Point3(x=0.0, y=0.0, z=0.0)))


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
    _ensure_placement(ifc, product)
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
    _ensure_placement(ifc, product)
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


def _cartesian_point(
    ifc: ifcopenshell.file, coordinates: tuple[float, float, float]
) -> Any:
    return ifc.create_entity(
        "IfcCartesianPoint",
        Coordinates=(
            float(coordinates[0]),
            float(coordinates[1]),
            float(coordinates[2]),
        ),
    )


def _placement_3d(
    ifc: ifcopenshell.file,
    location: tuple[float, float, float],
    axis: tuple[float, float, float],
    ref_direction: tuple[float, float, float],
) -> Any:
    return ifc.create_entity(
        "IfcAxis2Placement3D",
        Location=_cartesian_point(ifc, location),
        Axis=ifc.create_entity("IfcDirection", DirectionRatios=axis),
        RefDirection=ifc.create_entity("IfcDirection", DirectionRatios=ref_direction),
    )


def _mark_body(ifc: ifcopenshell.file, product: Any, reason: str | None) -> None:
    """Record whether a product got a derived Body solid, and why not.

    ``OABM_Adapter`` carries ``Body=yes`` or ``Body=no`` plus a ``BodyReason``
    string. A missing Body is never silently defaulted; the reason names the
    canonical dimension that was absent.
    """

    if reason is None:
        _mark_adapter(ifc, product, Body="yes")
    else:
        _mark_adapter(ifc, product, Body="no", BodyReason=reason)


def _assign_body_representation(
    ifc: ifcopenshell.file, product: Any, context: Any, solids: list[Any]
) -> None:
    """Attach ``solids`` as the product's ``Body`` representation.

    The architecture solids arrive in world coordinates (like the Axis
    curves), so identity is the correct ObjectPlacement for them; the opening
    void arrives in its product's local coordinates, where the placement
    carries the canonical pose.
    """

    _ensure_placement(ifc, product)
    representation = ifc.create_entity(
        "IfcShapeRepresentation",
        ContextOfItems=context,
        RepresentationIdentifier="Body",
        RepresentationType="SweptSolid",
        Items=list(solids),
    )
    ifcopenshell.api.geometry.assign_representation(
        ifc, product=product, representation=representation
    )


def _assign_wall_body(
    ifc: ifcopenshell.file,
    product: Any,
    context: Any,
    wall: Any,
) -> str | None:
    """Author a wall Body: an extruded rectangle per straight centerline
    segment (explicit ``IfcExtrudedAreaSolid``, not
    ``add_wall_representation``).

    Each segment contributes its plan rectangle (its length by
    ``thickness_m``, centered across it) extruded upward through
    ``height_m``. The result rests on the canonical axis at the storey
    elevation, in canonical metres. Returns a no-Body reason, or ``None``
    when a Body was authored.
    """

    points = wall.centerline.points
    for start, end in zip(points, points[1:]):
        plan_length = math.hypot(end.x - start.x, end.y - start.y)
        if plan_length <= _EPS:
            return "centerline segment is vertical; no plan rectangle to sweep"
        if abs(end.z - start.z) > _BODY_SLOP_M:
            return "centerline is not horizontal; the wall base has no single elevation"
    solids: list[Any] = []
    for start, end in zip(points, points[1:]):
        plan_length = math.hypot(end.x - start.x, end.y - start.y)
        direction = ((end.x - start.x) / plan_length, (end.y - start.y) / plan_length)
        profile = ifc.create_entity(
            "IfcRectangleProfileDef",
            ProfileType="AREA",
            XDim=float(plan_length),
            YDim=float(wall.thickness_m),
        )
        solid = ifc.create_entity(
            "IfcExtrudedAreaSolid",
            SweptArea=profile,
            Position=_placement_3d(
                ifc,
                ((start.x + end.x) / 2.0, (start.y + end.y) / 2.0, float(start.z)),
                (0.0, 0.0, 1.0),
                (direction[0], direction[1], 0.0),
            ),
            ExtrudedDirection=ifc.create_entity(
                "IfcDirection", DirectionRatios=(0.0, 0.0, 1.0)
            ),
            Depth=float(wall.height_m),
        )
        solids.append(solid)
    _assign_body_representation(ifc, product, context, solids)
    return None


def _assign_polygon_extrusion_body(
    ifc: ifcopenshell.file,
    product: Any,
    context: Any,
    polygon: Any,
    depth_m: float,
    *,
    upward: bool,
) -> str | None:
    """Extrude a canonical footprint polygon into a Body solid.

    The footprint is the profile on its own plane. ``upward=True`` extrudes
    toward +Z (a space volume rises from its floor plane); ``upward=False``
    extrudes below the plane (a slab or ceiling plate hangs beneath its
    footprint plane, which is the plate's top face). Returns a no-Body
    reason, or ``None`` when a Body was authored.
    """

    elevations = [float(point.z) for point in polygon.points]
    if max(elevations) - min(elevations) > _BODY_SLOP_M:
        return "footprint is not planar; the extrusion plane is ambiguous"
    if _polygon_plan_area(polygon.points) <= _EPS:
        return "footprint has no plan area to extrude"
    # IfcArbitraryClosedProfileDef.OuterCurve must be 2D (Dim == 2), so the
    # profile polyline drops Z: the solid Position carries the elevation.
    curve_points = [
        ifc.create_entity("IfcCartesianPoint", Coordinates=(float(point.x), float(point.y)))
        for point in polygon.points
    ]
    curve_points.append(curve_points[0])
    curve = ifc.create_entity("IfcPolyline", Points=curve_points)
    profile = ifc.create_entity("IfcArbitraryClosedProfileDef", ProfileType="AREA", OuterCurve=curve)
    direction_z = 1.0 if upward else -1.0
    solid = ifc.create_entity(
        "IfcExtrudedAreaSolid",
        SweptArea=profile,
        Position=_placement_3d(
            ifc, (0.0, 0.0, elevations[0]), (0.0, 0.0, 1.0), (1.0, 0.0, 0.0)
        ),
        ExtrudedDirection=ifc.create_entity(
            "IfcDirection", DirectionRatios=(0.0, 0.0, direction_z)
        ),
        Depth=float(depth_m),
    )
    _assign_body_representation(ifc, product, context, [solid])
    return None


def _assign_opening_body(
    ifc: ifcopenshell.file,
    product: Any,
    context: Any,
    opening: Any,
    host_wall: Any,
) -> str | None:
    """Author the opening's void Body: width x host thickness x height box.

    The box follows the canonical centered pose + size convention every lane
    reads: ``size.x`` along the pose X axis, the host wall ``thickness_m``
    along the pose Y axis and ``size.z`` along the pose Z axis, centered on
    ``pose.position``. Unlike the architecture solids it is authored in the
    opening product's LOCAL coordinates, because the product's
    ``ObjectPlacement`` already carries the canonical pose — authoring in
    world coordinates would apply the pose twice, and a Bonsai move of the
    opening then moves its void with it, which is the correct native
    behavior. The opening's own ``size.y`` depth is deliberately not the void
    depth: the host wall thickness is the canonical dimension that determines
    a cut-through.
    """

    height = float(opening.size.z)
    profile = ifc.create_entity(
        "IfcRectangleProfileDef",
        ProfileType="AREA",
        XDim=float(opening.size.x),
        YDim=float(host_wall.thickness_m),
    )
    solid = ifc.create_entity(
        "IfcExtrudedAreaSolid",
        SweptArea=profile,
        Position=_placement_3d(
            ifc, (0.0, 0.0, -height / 2.0), (0.0, 0.0, 1.0), (1.0, 0.0, 0.0)
        ),
        ExtrudedDirection=ifc.create_entity(
            "IfcDirection", DirectionRatios=(0.0, 0.0, 1.0)
        ),
        Depth=height,
    )
    _assign_body_representation(ifc, product, context, [solid])
    return None


def _assign_box_body(
    ifc: ifcopenshell.file,
    product: Any,
    context: Any,
    size: Size3 | None,
) -> str | None:
    """Author a device/equipment Body: a ``size.x`` x ``size.y`` x ``size.z``
    box centered on the pose.

    Like the opening void it is authored in the product's LOCAL coordinates,
    because the product's ``ObjectPlacement`` already carries the canonical
    pose — authoring in world coordinates would apply the pose twice, and a
    Bonsai move of the device must move its solid with it. A device without a
    canonical size gets no Body and no default box. Returns a no-Body reason,
    or ``None`` when a Body was authored.
    """

    if size is None:
        return "no canonical size"
    depth = float(size.z)
    profile = ifc.create_entity(
        "IfcRectangleProfileDef",
        ProfileType="AREA",
        XDim=float(size.x),
        YDim=float(size.y),
    )
    solid = ifc.create_entity(
        "IfcExtrudedAreaSolid",
        SweptArea=profile,
        Position=_placement_3d(
            ifc, (0.0, 0.0, -depth / 2.0), (0.0, 0.0, 1.0), (1.0, 0.0, 0.0)
        ),
        ExtrudedDirection=ifc.create_entity(
            "IfcDirection", DirectionRatios=(0.0, 0.0, 1.0)
        ),
        Depth=depth,
    )
    _assign_body_representation(ifc, product, context, [solid])
    return None


def _assign_obstacle_body(
    ifc: ifcopenshell.file,
    product: Any,
    context: Any,
    geometry: Any,
) -> str | None:
    """Author an obstacle Body from its canonical box, or refuse without one.

    A ``box3d`` obstacle gets the same centered local box as a device, with
    the product placement carrying the box pose. A polyline or polygon
    obstacle has no canonical volumetric extent to extrude, so it gets no
    Body and no invented depth. Returns a no-Body reason, or ``None`` when a
    Body was authored.
    """

    if isinstance(geometry, Box3D):
        return _assign_box_body(ifc, product, context, geometry.size)
    kind = getattr(geometry, "kind", "unknown")
    return f"obstacle geometry is a {kind}; no canonical volumetric extent"


def _polygon_plan_area(points: Any) -> float:
    area = 0.0
    for first, second in zip(points, (*points[1:], points[0])):
        area += first.x * second.y - second.x * first.y
    return abs(area) / 2.0


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
    # Convenience receptacles, special-purpose outlets and EVSE are all power
    # outlets in IFC4; data, TV and telephone outlets get their own outlet
    # PredefinedType. The canonical device_type stays in ObjectType either way.
    if token in {
        "receptacle",
        "outlet",
        "power-outlet",
        "power_outlet",
        "receptacle_duplex",
        "receptacle_quad",
        "combination_outlet",
        "special_purpose_outlet",
        "evse",
    }:
        return "IfcOutlet", "POWEROUTLET"
    if token == "data_outlet":
        return "IfcOutlet", "DATAOUTLET"
    if token in {"catv_outlet", "tv_outlet"}:
        return "IfcOutlet", "AUDIOVISUALOUTLET"
    if token in {"telephone_outlet", "telephone"}:
        # IFC4 defines TELEPHONEOUTLET in IfcOutletTypeEnum; it is more
        # precise than the generic COMMUNICATIONSOUTLET.
        return "IfcOutlet", "TELEPHONEOUTLET"
    if token in {"junction-box", "junction_box", "jbox"}:
        return "IfcJunctionBox", None
    if token in {"junction_box_power", "power_junction_box"}:
        return "IfcJunctionBox", "POWER"
    if token in {"junction_box_data", "data_junction_box", "telephone_junction_box"}:
        return "IfcJunctionBox", "DATA"
    if token in {"luminaire", "light", "light-fixture", "light_fixture"}:
        return "IfcLightFixture", None
    if token == "exit_sign":
        return "IfcLightFixture", "SECURITYLIGHTING"
    if token in {"switch", "disconnect"}:
        return "IfcSwitchingDevice", None
    # IFC4's IfcSensorTypeEnum carries the smoke (SMOKESENSOR), heat
    # (HEATSENSOR) and CO (COSENSOR) members these devices need, but it has
    # no OCCUPANCYSENSOR, and IfcAlarmTypeEnum has no smoke or heat members,
    # so alarms are sensors here and occupancy takes its nearest member. The
    # canonical type stays in ObjectType either way.
    if token == "occupancy_sensor":
        return "IfcSensor", "MOVEMENTSENSOR"
    # Lighting-control devices share the occupancy sensor's nearest-member
    # treatment: a vacancy sensor is a movement sensor, a daylight sensor is
    # IFC4's LIGHTSENSOR. A wireless remote controls the load from the wall,
    # so it is a KEYPAD switching device, and a line-voltage power pack is
    # the CONTACTOR relay switching the lighting load. The canonical type
    # stays in ObjectType either way.
    if token == "vacancy_sensor":
        return "IfcSensor", "MOVEMENTSENSOR"
    if token == "daylight_sensor":
        return "IfcSensor", "LIGHTSENSOR"
    if token == "wireless_remote":
        return "IfcSwitchingDevice", "KEYPAD"
    if token == "lighting_power_pack":
        return "IfcSwitchingDevice", "CONTACTOR"
    if token in {"smoke_alarm", "smoke_co_alarm"}:
        return "IfcSensor", "SMOKESENSOR"
    if token == "heat_detector":
        return "IfcSensor", "HEATSENSOR"
    if token == "access_control_device":
        return "IfcSensor", "IDENTIFIERSENSOR"
    if token == "speaker":
        return "IfcAudioVisualAppliance", "SPEAKER"
    # IFC4's IfcFanTypeEnum describes fan mechanics, not application, so
    # exhaust and ceiling fans keep the class without a PredefinedType.
    if token in {"exhaust_fan", "ceiling_fan"}:
        return "IfcFan", None
    if token in {"panelboard", "distribution-board", "distribution_board"}:
        return "IfcElectricDistributionBoard", "DISTRIBUTIONBOARD"
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
    **metadata: Any,
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
    _mark_adapter(
        ifc, port, Role=role, RouteId=route_id, StableKey=stable_key, **metadata
    )
    return port


def _add_route_attachment_port(
    ifc: ifcopenshell.file,
    owner: Any,
    canonical_port: Any,
    *,
    route_id: str,
    end: str,
) -> Any:
    """Add the adapter port where one route end meets its canonical port.

    The attachment port is nested on the canonical port's owner at the
    canonical port's position and carries only ``OABM_Adapter`` metadata, so
    ``from_ifc`` never mistakes it for a canonical entity. Its GlobalId comes
    from ``{route_id}#attach:{end}``, so it is stable across exports and
    independent of how many other routes share the canonical port.
    """

    return _add_adapter_port(
        ifc,
        owner,
        f"{route_id}#attach:{end}",
        canonical_port.pose.position,
        canonical_port.direction,
        route_id=route_id,
        role=f"route-attach-{end}",
        CanonicalPortId=canonical_port.id,
    )


def _connect(port_links: list[tuple[Any, Any]], first: Any, second: Any) -> None:
    port_links.append((first, second))


def _port_label(port: Any) -> str:
    """Name a port in an error by its canonical id, else its adapter key.

    Canonical ports carry the canonical ``name`` (often null) as their IFC
    Name, so the Name alone cannot identify them; adapter ports use their
    stable key as their Name.
    """

    try:
        meta = _canonical_metadata(port)
    except IfcAdapterError:
        meta = None
    if meta is not None and meta["kind"] == "port" and meta["json"].get("id"):
        return str(meta["json"]["id"])
    return str(port.Name or port.GlobalId)


def _create_port_connection(
    ifc: ifcopenshell.file, relating: Any, related: Any, stable_key: str
) -> Any:
    """Write one IfcRelConnectsPorts directly.

    ``ifcopenshell.api.system.connect_port`` is deliberately not used: it purges
    any existing connection on either port first, and for a NOTDEFINED direction
    it writes two reciprocal relationships for a single logical connection while
    also overwriting ``FlowDirection`` with NOTDEFINED.
    """

    return ifc.create_entity(
        "IfcRelConnectsPorts",
        GlobalId=canonical_id_to_ifc_guid(stable_key),
        RelatingPort=relating,
        RelatedPort=related,
    )


def _materialize_port_connections(
    ifc: ifcopenshell.file, port_links: list[tuple[Any, Any]]
) -> None:
    """Write every collected port connection as one IfcRelConnectsPorts.

    IFC4 bounds ``IfcPort.ConnectedTo`` (as RelatingPort) and
    ``IfcPort.ConnectedFrom`` (as RelatedPort) at SET [0:1] each, so each
    relationship consumes one role slot on each of its two ports. Slot use is
    read from the file's own inverse attributes, so a relationship already in
    the file counts. Each link is oriented as given when both slots are free,
    flipped when only the opposite orientation fits, and refused with the ports
    named when neither does.

    ``to_ifc`` never reaches the refusal: canonical ports carry only canonical
    peer links, which the fanout guard bounds at one, and every adapter port
    (segment end, fitting end, route attachment) takes part in at most one link.
    """

    for first, second in port_links:
        if first.id() == second.id():
            continue
        if not first.ConnectedTo and not second.ConnectedFrom:
            pair = (first, second)
        elif not first.ConnectedFrom and not second.ConnectedTo:
            pair = (second, first)
        else:
            raise IfcAdapterError(
                "canonical port connectivity cannot be represented natively without "
                "loss; IFC4 IfcPort bounds ConnectedTo and ConnectedFrom at one "
                f"relationship each, and ports {_port_label(first)!r} <-> "
                f"{_port_label(second)!r} cannot be oriented within that bound"
            )
        key = f"portlink:{pair[0].GlobalId}:{pair[1].GlobalId}"
        try:
            _create_port_connection(ifc, pair[0], pair[1], key)
        except Exception as exc:
            raise IfcAdapterError(
                "failed to materialize canonical port connection "
                f"{_port_label(first)!r} <-> {_port_label(second)!r}"
            ) from exc


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


def _native_port_connections(
    ifc: ifcopenshell.file,
    ports_by_id: Mapping[str, Any],
) -> dict[str, set[str]]:
    id_by_step = {
        item.id(): canonical_id for canonical_id, item in ports_by_id.items()
    }
    connections: dict[str, set[str]] = {
        canonical_id: set() for canonical_id in ports_by_id
    }
    for rel in ifc.by_type("IfcRelConnectsPorts"):
        left = id_by_step.get(rel.RelatingPort.id())
        right = id_by_step.get(rel.RelatedPort.id())
        if left and right:
            connections[left].add(right)
            connections[right].add(left)
    return connections


def _native_canonical_port_connections(
    ifc: ifcopenshell.file,
    canonical_items: Mapping[str, tuple[Any, dict[str, Any], int, str]],
) -> dict[str, set[str]]:
    ports_by_id = {
        canonical_id: item
        for canonical_id, (item, _data, _ordinal, kind) in canonical_items.items()
        if kind == "port"
    }
    return _native_port_connections(ifc, ports_by_id)


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
    """Override the canonical JSON shadow with native, Bonsai-editable values.

    Only ``Axis`` shape representations are read back as geometry (see
    ``_polyline_points``). ``Body`` solids are a derived export view; they are
    deliberately never read as canonical geometry so a native solid can never
    overwrite or contradict the canonical dimension fields.
    """

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
        if data["id"] not in native_connections:
            raise IfcAdapterError(
                f"canonical port {data['id']!r} is missing from native connectivity scan"
            )
        data["connected_port_ids"] = sorted(native_connections[data["id"]])
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
        if not segments:
            raise IfcAdapterError(
                f"route {data['id']!r} has no native route segments; "
                "refusing to resurrect deleted geometry from OABM_Canonical"
            )
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
