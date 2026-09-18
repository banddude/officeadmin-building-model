from __future__ import annotations

from dataclasses import replace
import math
from pathlib import Path

import ifcopenshell
import ifcopenshell.api.geometry
import ifcopenshell.util.placement
import numpy as np
import pytest

from oabm.ifc import canonical_id_to_ifc_guid, from_ifc, to_ifc
from oabm.model import BuildingModel
from oabm.qa import iter_entities, load_golden_cases, load_golden_model, validate_lane_model
from oabm.quantities import extract_quantities
from oabm.routing import route_between_ports


ROOT = Path(__file__).resolve().parents[1]
GOLDEN_ROOT = ROOT / "fixtures" / "golden" / "v1"
SOURCE_PORT_ID = "port:garage-panel-load"
LOAD_PORT_ID = "port:garage-evse-feed"
CIRCUIT_ID = "circuit:garage-evse"


def _golden_garage() -> BuildingModel:
    case = next(case for case in load_golden_cases(GOLDEN_ROOT) if case.name == "synthetic-garage")
    return load_golden_model(case)


def _unrouted_garage() -> BuildingModel:
    """Use the QA-owned garage as input, not its hand-authored expected route."""

    golden = _golden_garage()
    circuits = tuple(replace(circuit, route_ids=()) for circuit in golden.circuits)
    conductors = tuple(replace(conductor, route_ids=()) for conductor in golden.conductors)
    return replace(
        golden,
        routes=(),
        route_fittings=(),
        circuits=circuits,
        conductors=conductors,
    )


def _route_garage(model: BuildingModel) -> BuildingModel:
    route, fittings = route_between_ports(
        model,
        SOURCE_PORT_ID,
        LOAD_PORT_ID,
        "emt",
    )
    circuits = tuple(
        replace(circuit, route_ids=(route.id,)) if circuit.id == CIRCUIT_ID else circuit
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


def _canonical_ids(model: BuildingModel) -> set[str]:
    return {model.model_id, *(entity.id for entity in iter_entities(model))}


def _ifc_port_graph(ifc: ifcopenshell.file) -> dict[int, set[int]]:
    graph: dict[int, set[int]] = {}
    for relation in ifc.by_type("IfcRelConnectsPorts"):
        left = relation.RelatingPort.id()
        right = relation.RelatedPort.id()
        graph.setdefault(left, set()).add(right)
        graph.setdefault(right, set()).add(left)

    # IfcRelConnectsPorts joins adjacent products. Continuity through a conduit
    # segment or fitting is represented by the pair of ports owned by that
    # distribution element, so include that native element-internal link too.
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


def test_gate_b_real_router_output_round_trips_through_connected_ifc_and_quantities() -> None:
    source = _unrouted_garage()

    first = _route_garage(source)
    second = _route_garage(source)
    assert first.routes == second.routes
    assert first.route_fittings == second.route_fittings

    route = first.routes[0]
    assert route.provenance[0].source_kind == "router"
    assert route.provenance[0].method == "deterministic-rectilinear-v1"
    assert route.centerline.points[0] == next(p for p in first.ports if p.id == SOURCE_PORT_ID).pose.position
    assert route.centerline.points[-1] == next(p for p in first.ports if p.id == LOAD_PORT_ID).pose.position
    assert len({point.z for point in route.centerline.points}) > 1
    assert validate_lane_model(first) == validate_lane_model(second)

    ifc = to_ifc(first)
    segments = [
        item
        for item in ifc.by_type("IfcCableCarrierSegment")
        if (item.Name or "").startswith(f"{route.id} segment ")
    ]
    fittings = [
        ifc.by_guid(canonical_id_to_ifc_guid(fitting.id))
        for fitting in first.route_fittings
    ]
    assert len(segments) == len(route.centerline.points) - 1
    assert all(segment.PredefinedType == "CONDUITSEGMENT" for segment in segments)
    assert all(item is not None and item.is_a("IfcCableCarrierFitting") for item in fittings)
    assert all(len(_product_ports(ifc, product)) == 2 for product in (*segments, *fittings))

    source_port = ifc.by_guid(canonical_id_to_ifc_guid(SOURCE_PORT_ID))
    load_port = ifc.by_guid(canonical_id_to_ifc_guid(LOAD_PORT_ID))
    graph = _ifc_port_graph(ifc)
    assert _reachable(graph, source_port.id(), load_port.id())

    round_tripped = from_ifc(ifc)
    assert round_tripped.to_dict() == first.to_dict()

    report = extract_quantities(round_tripped)
    length_m = _route_length(round_tripped)
    assert _quantity(report, "route_length", "emt") == pytest.approx(length_m)
    assert _quantity(report, "fitting", "elbow-90") + _quantity(report, "fitting", "elbow-45") + _quantity(report, "fitting", "elbow") == len(round_tripped.route_fittings)
    assert _quantity(report, "conductor_length", "line") == pytest.approx(2 * length_m)
    assert _quantity(report, "conductor_length", "equipment-ground") == pytest.approx(length_m)
    assert _quantity(report, "device", "evse") == 1
    assert _quantity(report, "equipment", "panelboard") == 1
    assert report.warnings == ()
    assert report.to_json(indent=None) == extract_quantities(round_tripped).to_json(indent=None)


def test_gate_b_bonsai_equivalent_edit_preserves_identity_route_semantics_and_connectivity(
    tmp_path: Path,
) -> None:
    model = _route_garage(_unrouted_garage())
    route = model.routes[0]
    original_ids = _canonical_ids(model)

    source_path = tmp_path / "gate-b-before.ifc"
    edited_path = tmp_path / "gate-b-after.ifc"
    to_ifc(model, source_path)

    ifc = ifcopenshell.open(str(source_path))
    panel = ifc.by_guid(canonical_id_to_ifc_guid("equip:garage-panel"))
    evse = ifc.by_guid(canonical_id_to_ifc_guid("device:garage-evse"))
    panel.Name = "Garage Panel - Bonsai edit"

    matrix = np.asarray(
        ifcopenshell.util.placement.get_local_placement(evse.ObjectPlacement),
        dtype=float,
    ).copy()
    matrix[2, 3] += 0.10
    ifcopenshell.api.geometry.edit_object_placement(
        ifc,
        product=evse,
        matrix=matrix,
        is_si=True,
        should_transform_children=False,
    )

    final_segment = next(
        segment
        for segment in ifc.by_type("IfcCableCarrierSegment")
        if segment.Name == f"{route.id} segment {len(route.centerline.points) - 1}"
    )
    axis = next(
        representation
        for representation in final_segment.Representation.Representations
        if representation.RepresentationIdentifier == "Axis"
    )
    polyline = next(item for item in axis.Items if item.is_a("IfcPolyline"))
    end = route.centerline.points[-1]
    polyline.Points[-1].Coordinates = (end.x, end.y, end.z + 0.10)
    ifc.write(str(edited_path))

    reopened_ifc = ifcopenshell.open(str(edited_path))
    source_port = reopened_ifc.by_guid(canonical_id_to_ifc_guid(SOURCE_PORT_ID))
    load_port = reopened_ifc.by_guid(canonical_id_to_ifc_guid(LOAD_PORT_ID))
    assert _reachable(
        _ifc_port_graph(reopened_ifc),
        source_port.id(),
        load_port.id(),
    )

    edited = from_ifc(edited_path)
    assert _canonical_ids(edited) == original_ids
    assert next(item for item in edited.electrical_equipment if item.id == "equip:garage-panel").name == "Garage Panel - Bonsai edit"
    assert next(item for item in edited.electrical_devices if item.id == "device:garage-evse").pose.position.z == pytest.approx(end.z + 0.10)
    edited_route = next(item for item in edited.routes if item.id == route.id)
    assert edited_route.centerline.points[-1].z == pytest.approx(end.z + 0.10)
    assert next(item for item in edited.circuits if item.id == CIRCUIT_ID).route_ids == (route.id,)
    assert all(conductor.route_ids == (route.id,) for conductor in edited.conductors)
    assert validate_lane_model(edited)
