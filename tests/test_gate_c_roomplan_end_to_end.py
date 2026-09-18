from __future__ import annotations

from dataclasses import replace
import math
from pathlib import Path

import ifcopenshell
import pytest

from oabm.drawings import (
    Bounds2,
    PlanSpec,
    VisibilityPolicy,
    generate_drawing_set,
)
from oabm.ifc import canonical_id_to_ifc_guid, from_ifc, to_ifc
from oabm.importers.roomplan import load_captured_room
from oabm.model import (
    BuildingModel,
    Circuit,
    Conductor,
    ElectricalDevice,
    ElectricalEquipment,
    Point3,
    Port,
    Pose,
    Provenance,
    Size3,
    Vector3,
)
from oabm.qa import validate_lane_model
from oabm.quantities import extract_quantities
from oabm.routing import route_between_ports


ROOT = Path(__file__).resolve().parents[1]
ROOMPLAN_FIXTURE = ROOT / "fixtures" / "roomplan" / "captured-room-3d.json"
ROOMPLAN_SOURCE_ID = "fixture:captured-room-3d"
ELECTRICAL_SOURCE_ID = "fixture:gate-c-roomplan-electrical"
PANEL_ID = "equip:gate-c-panel"
EVSE_ID = "device:gate-c-evse"
SOURCE_PORT_ID = "port:gate-c-panel-load"
LOAD_PORT_ID = "port:gate-c-evse-feed"
CIRCUIT_ID = "circuit:gate-c-evse"


def _roomplan_model() -> BuildingModel:
    return load_captured_room(
        ROOMPLAN_FIXTURE,
        source_id=ROOMPLAN_SOURCE_ID,
        name="Gate C RoomPlan capture",
    )


def _electrical_context(model: BuildingModel) -> BuildingModel:
    """Add explicit synthetic design intent without teaching RoomPlan electrical inference."""

    level = model.levels[0]
    space = model.spaces[0]
    provenance = (
        Provenance(
            source_kind="synthetic-fixture",
            source_id=ELECTRICAL_SOURCE_ID,
            method="public-safe-electrical-context",
        ),
    )

    panel = ElectricalEquipment(
        id=PANEL_ID,
        name="Gate C Panel",
        equipment_type="panelboard",
        pose=Pose(position=Point3(x=-1.6, y=0.0, z=3.8)),
        level_id=level.id,
        space_id=space.id,
        size=Size3(x=0.4, y=0.2, z=0.6),
        system="power",
        rated_voltage_v=240.0,
        provenance=provenance,
    )
    evse = ElectricalDevice(
        id=EVSE_ID,
        name="Gate C EVSE",
        device_type="evse",
        pose=Pose(position=Point3(x=1.6, y=0.0, z=3.8)),
        level_id=level.id,
        space_id=space.id,
        size=Size3(x=0.3, y=0.2, z=0.5),
        system="power",
        rated_voltage_v=240.0,
        provenance=provenance,
    )
    source_port = Port(
        id=SOURCE_PORT_ID,
        owner_id=panel.id,
        domain="electrical",
        role="load",
        pose=Pose(position=Point3(x=-1.4, y=0.0, z=3.8)),
        direction=Vector3(x=1.0, y=0.0, z=0.0),
        nominal_diameter_m=0.021,
        provenance=provenance,
    )
    load_port = Port(
        id=LOAD_PORT_ID,
        owner_id=evse.id,
        domain="electrical",
        role="feed",
        pose=Pose(position=Point3(x=1.4, y=0.0, z=3.8)),
        direction=Vector3(x=-1.0, y=0.0, z=0.0),
        nominal_diameter_m=0.021,
        provenance=provenance,
    )
    circuit = Circuit(
        id=CIRCUIT_ID,
        source_port_id=source_port.id,
        load_port_ids=(load_port.id,),
        circuit_number="1",
        voltage_v=240.0,
        poles=2,
        phase="1ph",
        load_va=9600.0,
        provenance=provenance,
    )
    conductors = (
        Conductor(
            id="conductor:gate-c-line",
            circuit_id=circuit.id,
            role="line",
            material="copper",
            size="#6 AWG",
            count=2,
            provenance=provenance,
        ),
        Conductor(
            id="conductor:gate-c-ground",
            circuit_id=circuit.id,
            role="equipment-ground",
            material="copper",
            size="#10 AWG",
            count=1,
            provenance=provenance,
        ),
    )

    return replace(
        model,
        electrical_equipment=(panel,),
        electrical_devices=(evse,),
        ports=(source_port, load_port),
        circuits=(circuit,),
        conductors=conductors,
    )


def _route_model(model: BuildingModel) -> BuildingModel:
    route, fittings = route_between_ports(
        model,
        SOURCE_PORT_ID,
        LOAD_PORT_ID,
        "emt",
    )
    circuits = tuple(
        replace(circuit, route_ids=(route.id,))
        if circuit.id == CIRCUIT_ID
        else circuit
        for circuit in model.circuits
    )
    conductors = tuple(
        replace(conductor, route_ids=(route.id,))
        if conductor.circuit_id == CIRCUIT_ID
        else conductor
        for conductor in model.conductors
    )
    return replace(
        model,
        routes=(route,),
        route_fittings=fittings,
        circuits=circuits,
        conductors=conductors,
    )


def _route_length(model: BuildingModel) -> float:
    route = model.routes[0]
    return math.fsum(
        math.dist((a.x, a.y, a.z), (b.x, b.y, b.z))
        for a, b in zip(route.centerline.points, route.centerline.points[1:])
    )


def _quantity(report, category: str, item_type: str) -> float:
    return math.fsum(
        item.quantity
        for item in report.items
        if item.category == category and item.item_type == item_type
    )


def _product_ports(ifc: ifcopenshell.file, product) -> set[int]:
    ports: set[int] = set()
    for relation in ifc.by_type("IfcRelNests"):
        if relation.RelatingObject.id() == product.id():
            ports.update(
                item.id()
                for item in relation.RelatedObjects
                if item.is_a("IfcDistributionPort")
            )
    for relation in ifc.by_type("IfcRelConnectsPortToElement"):
        if relation.RelatedElement.id() == product.id():
            ports.add(relation.RelatingPort.id())
    return ports


def _ifc_port_graph(ifc: ifcopenshell.file) -> dict[int, set[int]]:
    graph: dict[int, set[int]] = {}
    for relation in ifc.by_type("IfcRelConnectsPorts"):
        left = relation.RelatingPort.id()
        right = relation.RelatedPort.id()
        graph.setdefault(left, set()).add(right)
        graph.setdefault(right, set()).add(left)

    for class_name in ("IfcCableCarrierSegment", "IfcCableCarrierFitting"):
        for product in ifc.by_type(class_name):
            ports = sorted(_product_ports(ifc, product))
            if len(ports) != 2:
                continue
            left, right = ports
            graph.setdefault(left, set()).add(right)
            graph.setdefault(right, set()).add(left)
    return graph


def _reachable(graph: dict[int, set[int]], start: int, end: int) -> bool:
    pending = [start]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if current == end:
            return True
        if current in seen:
            continue
        seen.add(current)
        pending.extend(graph.get(current, ()) - seen)
    return False


def test_gate_c_roomplan_input_flows_through_route_ifc_quantities_and_derived_view() -> None:
    # Real input family -> canonical model.
    imported = _roomplan_model()
    assert imported.provenance[0].source_kind == "roomplan"
    assert imported.provenance[0].source_id == ROOMPLAN_SOURCE_ID
    assert imported.walls
    assert imported.obstacles
    assert not imported.electrical_equipment
    assert not imported.electrical_devices

    source = _electrical_context(imported)
    assert source.model_id == imported.model_id
    assert source.levels == imported.levels
    assert source.spaces == imported.spaces
    assert source.walls == imported.walls
    assert source.slabs == imported.slabs
    assert source.openings == imported.openings
    assert source.obstacles == imported.obstacles

    # Canonical model -> deterministic route. The RoomPlan table is directly
    # between the ports, so imported scan geometry must change the route.
    first = _route_model(source)
    second = _route_model(source)
    assert first.routes == second.routes
    assert first.route_fittings == second.route_fittings
    assert validate_lane_model(first) == validate_lane_model(second)

    route = first.routes[0]
    obstacle = imported.obstacles[0]
    obstacle_free_route, _ = route_between_ports(
        replace(source, obstacles=()),
        SOURCE_PORT_ID,
        LOAD_PORT_ID,
        "emt",
    )
    assert route.centerline != obstacle_free_route.centerline
    assert len(obstacle_free_route.centerline.points) == 2
    assert max(point.z for point in route.centerline.points) > (
        obstacle.geometry.pose.position.z + obstacle.geometry.size.z / 2.0
    )
    assert route.provenance[0].source_kind == "router"
    assert route.provenance[0].source_id == imported.model_id

    # Routed canonical model -> connected native IFC -> exact canonical model.
    ifc = to_ifc(first)
    segments = [
        item
        for item in ifc.by_type("IfcCableCarrierSegment")
        if (item.Name or "").startswith(f"{route.id} segment ")
    ]
    native_fittings = [
        ifc.by_guid(canonical_id_to_ifc_guid(fitting.id))
        for fitting in first.route_fittings
    ]
    assert len(segments) == len(route.centerline.points) - 1
    assert all(segment.PredefinedType == "CONDUITSEGMENT" for segment in segments)
    assert all(
        item is not None and item.is_a("IfcCableCarrierFitting")
        for item in native_fittings
    )

    source_port = ifc.by_guid(canonical_id_to_ifc_guid(SOURCE_PORT_ID))
    load_port = ifc.by_guid(canonical_id_to_ifc_guid(LOAD_PORT_ID))
    assert source_port is not None and load_port is not None
    assert _reachable(_ifc_port_graph(ifc), source_port.id(), load_port.id())

    round_tripped = from_ifc(ifc)
    assert round_tripped.to_dict() == first.to_dict()

    # Same round-tripped canonical model -> quantities.
    report = extract_quantities(round_tripped)
    length_m = _route_length(round_tripped)
    assert _quantity(report, "route_length", "emt") == pytest.approx(length_m)
    assert _quantity(report, "fitting", "elbow-90") == len(
        round_tripped.route_fittings
    )
    assert _quantity(report, "conductor_length", "line") == pytest.approx(
        2.0 * length_m
    )
    assert _quantity(
        report, "conductor_length", "equipment-ground"
    ) == pytest.approx(length_m)
    assert _quantity(report, "device", "evse") == 1
    assert _quantity(report, "equipment", "panelboard") == 1
    assert report.warnings == ()
    assert report.to_json(indent=None) == extract_quantities(
        round_tripped
    ).to_json(indent=None)

    # Same round-tripped canonical model -> deterministic derived drawing.
    plan = PlanSpec(
        id="plan:gate-c-roomplan",
        level_id=round_tripped.levels[0].id,
        cut_height_m=0.6,
        view_depth_below_m=0.3,
        view_depth_above_m=0.3,
        bounds=Bounds2(min_x=-2.5, min_y=-2.0, max_x=2.5, max_y=2.0),
        visibility=VisibilityPolicy(
            obstacles=True,
            labels=False,
            dimensions=False,
        ),
    )
    drawing_set = generate_drawing_set(round_tripped, plans=(plan,))
    assert drawing_set.to_json(indent=None) == generate_drawing_set(
        round_tripped, plans=(plan,)
    ).to_json(indent=None)

    view = drawing_set.views[0]
    source_ids = {
        source_id
        for primitive in view.primitives
        for source_id in primitive.source_ids
    }
    assert route.id in source_ids
    assert PANEL_ID in source_ids
    assert EVSE_ID in source_ids
    assert obstacle.id in source_ids
    assert {wall.id for wall in imported.walls}.issubset(source_ids)

    source_index = {
        reference.canonical_id: reference
        for reference in drawing_set.source_index
    }
    imported_wall = imported.walls[0]
    assert source_index[imported_wall.id].provenance[0].source_kind == "roomplan"
    assert source_index[imported_wall.id].provenance[0].source_id == ROOMPLAN_SOURCE_ID
    assert source_index[route.id].provenance[0].source_kind == "router"

    schedules = {schedule.id: schedule for schedule in drawing_set.schedules}
    assert PANEL_ID in {
        row.source_id for row in schedules["schedule:equipment"].rows
    }
    assert EVSE_ID in {
        row.source_id for row in schedules["schedule:devices"].rows
    }
    assert CIRCUIT_ID in {
        row.source_id for row in schedules["schedule:circuits"].rows
    }
