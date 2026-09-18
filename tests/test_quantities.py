from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from oabm.model import (
    BuildingModel,
    Circuit,
    Conductor,
    ContractError,
    ElectricalDevice,
    ElectricalEquipment,
    Point3,
    Polyline3D,
    Port,
    Pose,
    Provenance,
    Route,
    Vector3,
)
from oabm.quantities import QuantityItem, QuantityReport, extract_quantities, route_length_m

ROOT = Path(__file__).resolve().parents[1]
GARAGE = ROOT / "fixtures" / "model" / "v1" / "garage-route.json"
MINIMAL = ROOT / "fixtures" / "model" / "v1" / "minimal-room.json"


def _garage() -> BuildingModel:
    return BuildingModel.load(GARAGE)


def _item(
    report: QuantityReport,
    category: str,
    item_type: str,
    **specification: object,
) -> QuantityItem:
    matches = []
    for item in report.items:
        if item.category != category or item.item_type != item_type:
            continue
        spec = dict(item.specification)
        if all(spec.get(key) == value for key, value in specification.items()):
            matches.append(item)
    assert len(matches) == 1, matches
    return matches[0]


def _single_route_model(*, route_type: str = "emt", conductor_count: int = 3) -> BuildingModel:
    provenance = (Provenance(source_kind="synthetic", source_id="fixture:3d-route"),)
    source = ElectricalEquipment(
        id="equip:source",
        equipment_type="panelboard",
        pose=Pose(position=Point3(x=0, y=0, z=0)),
        system="480V-3ph",
        rated_voltage_v=480,
        provenance=provenance,
    )
    destination = ElectricalDevice(
        id="device:box",
        device_type="junction_box",
        pose=Pose(position=Point3(x=3, y=4, z=12)),
        system="480V-3ph",
        rated_voltage_v=480,
        provenance=provenance,
    )
    source_port = Port(
        id="port:source",
        owner_id=source.id,
        domain="power",
        role="source",
        pose=source.pose,
        direction=Vector3(x=1, y=0, z=0),
        provenance=provenance,
    )
    destination_port = Port(
        id="port:destination",
        owner_id=destination.id,
        domain="power",
        role="sink",
        pose=destination.pose,
        direction=Vector3(x=-1, y=0, z=0),
        provenance=provenance,
    )
    route = Route(
        id="route:3d",
        route_type=route_type,
        start_port_id=source_port.id,
        end_port_id=destination_port.id,
        centerline=Polyline3D(points=(source_port.pose.position, destination_port.pose.position)),
        nominal_diameter_m=0.027,
        provenance=provenance,
    )
    circuit = Circuit(
        id="circuit:test",
        source_port_id=source_port.id,
        load_port_ids=(destination_port.id,),
        route_ids=(route.id,),
        provenance=provenance,
    )
    conductor = Conductor(
        id="conductor:test",
        circuit_id=circuit.id,
        role="phase",
        material="copper",
        size="#4 AWG",
        insulation="THHN",
        count=conductor_count,
        route_ids=(route.id,),
        provenance=provenance,
    )
    return BuildingModel(
        model_id="model:3d-route",
        electrical_equipment=(source,),
        electrical_devices=(destination,),
        ports=(source_port, destination_port),
        routes=(route,),
        circuits=(circuit,),
        conductors=(conductor,),
        provenance=provenance,
    )


def test_garage_fixture_has_known_takeoff_quantities_and_traceability() -> None:
    report = extract_quantities(_garage())

    raceway = _item(report, "route_length", "emt", nominal_diameter_m=0.021)
    assert raceway.quantity == pytest.approx(7.3)
    assert raceway.unit == "m"
    assert raceway.source_entity_ids == ("route:panel-evse",)
    assert raceway.route_ids == ("route:panel-evse",)

    fittings = _item(
        report,
        "fitting",
        "elbow-90",
        angle_radians=pytest.approx(1.5707963267948966),
        nominal_diameter_m=0.021,
    )
    assert fittings.quantity == 2.0
    assert fittings.unit == "ea"
    assert fittings.source_entity_ids == ("fitting:drop-elbow", "fitting:rise-elbow")

    line = _item(
        report,
        "conductor_length",
        "line",
        material="copper",
        size="#6 AWG",
        insulation="THHN/THWN-2",
    )
    assert line.quantity == pytest.approx(14.6)
    assert line.unit == "m"
    assert line.source_entity_ids == ("conductor:l1", "conductor:l2")

    ground = _item(report, "conductor_length", "equipment-ground", size="#10 AWG")
    assert ground.quantity == pytest.approx(7.3)

    assert _item(report, "device", "evse").quantity == 1.0
    assert _item(report, "equipment", "panelboard").quantity == 1.0
    assert report.length_unit == "m"
    assert report.angle_unit == "rad"
    assert report.diagnostics == ()
    assert all(item.assembly_key.startswith("oabm:") for item in report.items)
    assert all(item.provenance for item in report.items)
    assert all(item.confidence == 1.0 for item in report.items)


def test_route_length_is_true_3d_length_and_cable_routes_are_quantified() -> None:
    model = _single_route_model(route_type="cable", conductor_count=3)
    route = model.routes[0]
    assert route_length_m(route) == pytest.approx(13.0)

    report = extract_quantities(model)
    cable = _item(report, "route_length", "cable", nominal_diameter_m=0.027)
    conductor = _item(report, "conductor_length", "phase", size="#4 AWG")
    box = _item(report, "device", "junction_box")

    assert cable.quantity == pytest.approx(13.0)
    assert cable.unit == "m"
    assert conductor.quantity == pytest.approx(39.0)
    assert conductor.unit == "m"
    assert box.quantity == 1.0
    assert box.unit == "ea"


def test_repeatability_is_independent_of_canonical_collection_order() -> None:
    model = _garage()
    reordered = replace(
        model,
        route_fittings=tuple(reversed(model.route_fittings)),
        conductors=tuple(reversed(model.conductors)),
        electrical_devices=tuple(reversed(model.electrical_devices)),
        electrical_equipment=tuple(reversed(model.electrical_equipment)),
    )

    first = extract_quantities(model)
    second = extract_quantities(model)
    third = extract_quantities(reordered)
    assert first == second == third
    assert first.to_json(indent=None) == second.to_json(indent=None) == third.to_json(indent=None)


def test_duplicate_route_reference_does_not_double_count_conductor_length() -> None:
    model = _garage()
    route_id = model.routes[0].id
    duplicate_reference = replace(model.conductors[0], route_ids=(route_id, route_id))
    model = replace(model, conductors=(duplicate_reference, *model.conductors[1:]))

    line = _item(extract_quantities(model), "conductor_length", "line", size="#6 AWG")
    assert line.quantity == pytest.approx(14.6)


def test_duplicate_route_fitting_reference_does_not_double_count_fittings() -> None:
    model = _garage()
    route = model.routes[0]
    duplicated = replace(route, fitting_ids=(route.fitting_ids[0], *route.fitting_ids))
    model = replace(model, routes=(duplicated,))

    fittings = _item(extract_quantities(model), "fitting", "elbow-90")
    assert fittings.quantity == 2.0


def test_unrouted_conductor_is_reported_and_no_length_is_inferred_from_circuit() -> None:
    model = _garage()
    unrouted = replace(model.conductors[0], route_ids=())
    model = replace(model, conductors=(unrouted, *model.conductors[1:]))

    report = extract_quantities(model)
    assert report.diagnostics == (
        report.diagnostics[0],
    )
    assert report.diagnostics[0].code == "unrouted_conductor"
    assert report.diagnostics[0].source_entity_id == unrouted.id
    line = _item(report, "conductor_length", "line", size="#6 AWG")
    assert line.quantity == pytest.approx(7.3)


def test_extraction_revalidates_malformed_references_at_the_handoff_boundary() -> None:
    model = _garage()
    object.__setattr__(model.conductors[0], "route_ids", ("route:missing",))

    with pytest.raises(ContractError, match="references missing id 'route:missing'"):
        extract_quantities(model)


def test_architecture_geometry_is_not_remeasured_as_takeoff() -> None:
    model = BuildingModel.load(MINIMAL)
    report = extract_quantities(model)
    assert report.items == ()
    assert report.diagnostics == ()
